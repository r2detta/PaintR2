# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking to `paintr2_bake`, the mesh map baker that lives outside Blender.

Why there is an outside at all: Cycles' bake blocks Blender's main thread, so
the window freezes and the desktop offers to kill it; it holds the whole mesh
twice (Blender's copy and Cycles' BVH), which is 8.6 GB at eight million faces;
and it pays its ~0.8 s of setup once per map. A separate process fixes all
three by construction -- it can be watched, it can be killed, and it holds only
what it needs.

This module is the Blender half. It writes the mesh out in the layout
`PaintR2Bake/src/format.h` describes, runs the binary, and reads the maps back.
It deliberately knows nothing about operators or the UI: what it exposes is
`bake()`, and `ops/` decides how to wait for it.

**The Cycles path stays.** `meshmaps.py` is not replaced by this and does not
depend on it. With no binary built, `available()` returns None and everything
carries on as before -- which is also what happens on a machine we have not
shipped a binary for.
"""

import math
import os
import queue
import struct
import subprocess
import threading

import bmesh
import bpy
import numpy as np

# Mirrors `enum MapBit` in src/format.h. The two lists have to agree, and the
# magic below is what catches it when they stop.
MAP_BITS = {
    'AO': 1 << 0,
    'THICKNESS': 1 << 1,
    'POSITION': 1 << 2,
    'NORMAL': 1 << 3,
    'HEIGHT': 1 << 4,
    'CURVATURE': 1 << 5,
    'BENT_NORMAL': 1 << 6,
}

BIT_MAPS = {bit: name for name, bit in MAP_BITS.items()}

FLAG_FLIP_GREEN = 1 << 0
FLAG_AO_UNIFORM = 1 << 1
FLAG_AO_IGNORE_BACKFACE = 1 << 2

# Named slots in the header's `params` array, mirroring `src/format.h`. Named
# on both sides so neither counts positions by hand -- an off-by-one here is a
# parameter silently arriving as another parameter.
PARAM_EXTRUSION = 0
PARAM_RAY_DISTANCE = 1
PARAM_THICKNESS_REACH = 2
PARAM_HEIGHT_RANGE = 3
PARAM_CURVATURE_SCALE = 4
PARAM_AO_MIN_DISTANCE = 5
PARAM_AO_SPREAD = 6
PARAM_AO_ATTENUATION = 7
PARAM_CURVATURE_LOW = 8
PARAM_CURVATURE_HIGH = 9

PARAM_COUNT = 16

ATTENUATION = {'NONE': 0.0, 'LINEAR': 1.0, 'SMOOTH': 2.0}

INPUT_MAGIC = b"PR2BAKE1"
OUTPUT_MAGIC = b"PR2MAPS1"
VERSION = 4

# `<` throughout: little-endian and unpadded, so the struct module and the C
# `#pragma pack(1)` describe the same bytes on any host.
_HEADER = struct.Struct("<8s5I16f5I")
_OUTPUT_HEADER = struct.Struct("<8s3I")

BINARY_NAME = "paintr2_bake"


def binary_path():
    """Where the baker would be, shipped or built.

    Two places, in order: `bin/` inside the installed extension, which is what
    a user gets, and `PaintR2Bake/bin/` beside the source tree, which is what a
    developer gets without having to install anything.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "bin", BINARY_NAME),
        # Beside the source tree, so a developer gets the freshly built one
        # without installing anything.
        os.path.join(os.path.dirname(here), "PaintR2Bake", "bin", BINARY_NAME),
    )

    for path in candidates:
        if not os.path.isfile(path):
            continue
        if not os.access(path, os.X_OK):
            # A zip does not always carry the executable bit through, and an
            # extension is installed by unzipping. Restoring it here rather
            # than failing turns "the baker is missing" -- which is what the
            # caller would otherwise conclude -- back into "the baker works".
            try:
                os.chmod(path, os.stat(path).st_mode | 0o111)
            except OSError:
                continue
            if not os.access(path, os.X_OK):
                continue
        return path

    return None


def available():
    """The baker's path, or None. None is not an error -- it is the Cycles path."""
    return binary_path()


# --- getting a mesh out of Blender -------------------------------------------


def _evaluated(obj, depsgraph):
    """The object's mesh with its modifiers applied, and world-space matrices.

    A high-poly is usually a subdivision surface rather than eight million real
    vertices, and baking the unsubdivided cage would be a quiet wrong answer
    rather than a visible failure. `evaluated_get` is what makes the modifiers
    count.
    """
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    # A normal is a direction and rides the inverse transpose. Using the plain
    # matrix on a non-uniformly scaled object tilts every normal, and an AO
    # bake reads that as the surface facing somewhere it does not.
    normal_matrix = np.linalg.inv(matrix[:3, :3]).T
    return evaluated, mesh, matrix, normal_matrix


def _to_world(coords, matrix):
    return (coords @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32)


def _directions_to_world(vectors, normal_matrix):
    world = vectors @ normal_matrix.T
    length = np.linalg.norm(world, axis=1, keepdims=True)
    return np.divide(world, np.maximum(length, 1e-12)).astype(np.float32)


def _triangulate_ngons(mesh):
    """Split every face past four corners, in place.

    `calc_tangents` is MikkTSpace and MikkTSpace is defined on tris and quads
    only: one n-gon anywhere on a model and the whole call aborts. That used to
    take the object out of the bake entirely -- `prepare` raised, the operator
    put the reason in its skipped list, and the report still opened with
    "Baked N map(s)", so the model that produced nothing was the one nobody
    looked at.

    The mesh here is the throwaway `to_mesh()` copy, so this is the same
    triangulation the renderer does on its way to the screen and the user's
    model never sees it. Quads are left alone -- MikkTSpace handles them, and
    cutting them would change the tangents on every model that was fine.
    """
    if not any(len(polygon.vertices) > 4 for polygon in mesh.polygons):
        return

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bmesh.ops.triangulate(
            bm, faces=[face for face in bm.faces if len(face.verts) > 4]
        )
        bm.to_mesh(mesh)
    finally:
        bm.free()


def low_poly_soup(obj, depsgraph):
    """The low-poly as a triangle soup: 3 corners of position, normal, uv,
    tangent and bitangent sign.

    A soup rather than an indexed mesh, and the redundancy is the point. UVs
    and tangents live on corners, not on vertices -- a vertex on a seam carries
    two of each -- so an indexed form would need the vertices split first. The
    low-poly is low by definition, so paying three times for its data is
    cheaper than the code that would avoid it.

    Normals come from `corner_normals`, which is Blender's split normals: sharp
    edges and custom normals are respected. Vertex normals would smooth a
    hard-surface model's edges and the bake would disagree with the viewport.

    **The tangent is Blender's, not ours.** `calc_tangents()` runs MikkTSpace,
    which is the basis Blender's own shader will decode the normal map with. A
    tangent recomputed here from the UVs would be close and not identical, and
    the result of that is not an error message -- it is every dent lit slightly
    askew, on a map that looks right until it is next to one that is.
    """
    evaluated, mesh, matrix, normal_matrix = _evaluated(obj, depsgraph)
    try:
        if not mesh.uv_layers.active:
            raise RuntimeError(f"{obj.name} has no UV map")

        _triangulate_ngons(mesh)

        mesh.calc_loop_triangles()
        triangles = len(mesh.loop_triangles)
        if triangles == 0:
            raise RuntimeError(f"{obj.name} has no faces")

        # Must precede reading `loops[].tangent`; without it every tangent is
        # zero and the normal map comes back flat rather than wrong, which is
        # at least a visible failure.
        mesh.calc_tangents(uvmap=mesh.uv_layers.active.name)

        loops = np.empty(triangles * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", loops)

        loop_vertex = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vertex)

        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", coords)
        coords = coords.reshape(-1, 3)

        corner_normals = np.empty(len(mesh.loops) * 3, dtype=np.float64)
        mesh.corner_normals.foreach_get("vector", corner_normals)
        corner_normals = corner_normals.reshape(-1, 3)

        tangents = np.empty(len(mesh.loops) * 3, dtype=np.float64)
        mesh.loops.foreach_get("tangent", tangents)
        tangents = tangents.reshape(-1, 3)

        signs = np.empty(len(mesh.loops), dtype=np.float32)
        mesh.loops.foreach_get("bitangent_sign", signs)

        uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        mesh.uv_layers.active.uv.foreach_get("vector", uvs)
        uvs = uvs.reshape(-1, 2)

        positions = _to_world(coords[loop_vertex[loops]], matrix)
        normals = _directions_to_world(corner_normals[loops], normal_matrix)
        # A tangent lies *in* the surface, so unlike a normal it rides the
        # plain matrix. Sending it through the inverse transpose would tilt it
        # out of the surface on any non-uniformly scaled object.
        world_tangents = tangents[loops] @ matrix[:3, :3].T
        length = np.linalg.norm(world_tangents, axis=1, keepdims=True)
        world_tangents = np.divide(world_tangents, np.maximum(length, 1e-12))

        soup = np.empty((triangles * 3, 12), dtype=np.float32)
        soup[:, 0:3] = positions
        soup[:, 3:6] = normals
        soup[:, 6:8] = uvs[loops]
        soup[:, 8:11] = world_tangents
        soup[:, 11] = signs[loops]
        return triangles, soup
    finally:
        evaluated.to_mesh_clear()


def high_poly_indexed(obj, depsgraph):
    """The high-poly as positions, normals and indices.

    Indexed here and not a soup, for the opposite reason: this is where the
    volume is. Sixteen million triangles as a soup is 774 MB of vertices before
    anything is traced; indexed it is 128 MB. It needs no UVs at all -- the
    low-poly owns those -- but it does need normals, because what a high-poly
    bake writes into the normal map is *this* surface's direction.
    """
    evaluated, mesh, matrix, normal_matrix = _evaluated(obj, depsgraph)
    try:
        mesh.calc_loop_triangles()
        triangles = len(mesh.loop_triangles)
        vertices = len(mesh.vertices)
        if triangles == 0 or vertices == 0:
            raise RuntimeError(f"{obj.name} has no faces to bake from")

        coords = np.empty(vertices * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", coords)
        positions = _to_world(coords.reshape(-1, 3), matrix)

        normals = np.empty(vertices * 3, dtype=np.float64)
        mesh.vertex_normals.foreach_get("vector", normals)
        normals = _directions_to_world(normals.reshape(-1, 3), normal_matrix)

        indices = np.empty(triangles * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", indices)

        return vertices, triangles, positions, normals, indices.astype(np.uint32)
    finally:
        evaluated.to_mesh_clear()


def occluder_mesh(objects, depsgraph):
    """Other models, as one indexed mesh that only stops AO rays.

    Positions and indices and nothing else -- no UVs, no normals. The baker
    shades nothing off these; it only asks whether a ray from the model being
    baked ran into one of them. Modifiers count, for the same reason they do
    on a high-poly: what is drawn next to the model is what shadows it.

    Returns (verts, tris, positions, indices), or None when there is nothing.
    """
    positions = []
    indices = []
    base = 0
    for obj in objects:
        evaluated, mesh, matrix, _normal_matrix = _evaluated(obj, depsgraph)
        try:
            mesh.calc_loop_triangles()
            triangles = len(mesh.loop_triangles)
            vertices = len(mesh.vertices)
            if triangles == 0 or vertices == 0:
                continue

            coords = np.empty(vertices * 3, dtype=np.float64)
            mesh.vertices.foreach_get("co", coords)
            positions.append(_to_world(coords.reshape(-1, 3), matrix))

            tris = np.empty(triangles * 3, dtype=np.int64)
            mesh.loop_triangles.foreach_get("vertices", tris)
            indices.append((tris + base).astype(np.uint32))
            base += vertices
        finally:
            evaluated.to_mesh_clear()

    if not indices:
        return None
    stacked = np.concatenate(indices)
    return base, len(stacked) // 3, np.concatenate(positions), stacked


# --- the wire ----------------------------------------------------------------


def write_input(path, low, high, size, samples, maps, params, flags=0,
                occluders=None):
    """Write the `.pr2b` the baker reads. `high` and `occluders` may be None.

    `occluders` is `occluder_mesh()`'s (verts, tris, positions, indices).
    """
    low_tris, soup = low

    if occluders is None or occluders[1] == 0:
        occ_verts = occ_tris = 0
        occ_pos = occ_idx = None
    else:
        occ_verts, occ_tris, occ_pos, occ_idx = occluders

    if high is None:
        high_verts = high_tris = 0
        high_pos = high_nrm = high_idx = None
    else:
        high_verts, high_tris, high_pos, high_nrm, high_idx = high

    padded = list(params) + [0.0] * (PARAM_COUNT - len(params))

    with open(path, "wb") as handle:
        handle.write(_HEADER.pack(
            INPUT_MAGIC, VERSION, size, samples, maps, flags,
            *padded[:PARAM_COUNT],
            low_tris, high_verts, high_tris, occ_verts, occ_tris,
        ))
        handle.write(soup.tobytes())
        if high is not None:
            handle.write(high_pos.tobytes())
            handle.write(high_nrm.tobytes())
            handle.write(high_idx.tobytes())
        if occ_tris:
            handle.write(occ_pos.tobytes())
            handle.write(occ_idx.tobytes())


def read_output(path):
    """Read a `.pr2m`. Returns {map name: (size, size, 4) float32}."""
    with open(path, "rb") as handle:
        magic, version, size, count = _OUTPUT_HEADER.unpack(
            handle.read(_OUTPUT_HEADER.size)
        )
        if magic != OUTPUT_MAGIC:
            raise RuntimeError("not a PaintR2 map file")
        if version != VERSION:
            raise RuntimeError(
                f"map file version {version}, this build speaks {VERSION}"
            )

        maps = {}
        texels = size * size * 4
        for _ in range(count):
            identifier = struct.unpack("<I", handle.read(4))[0]
            raw = np.frombuffer(
                handle.read(texels * 4), dtype=np.float32, count=texels
            )
            name = BIT_MAPS.get(identifier, f"UNKNOWN_{identifier}")
            maps[name] = raw.reshape(size, size, 4).copy()
        return maps


def run(binary, input_path, output_path, on_progress=None):
    """Run the baker to completion. Returns (ok, message).

    Progress arrives as `PROGRESS <fraction>` lines on stdout and is handed to
    `on_progress` as it comes, which is what lets a caller draw a bar instead
    of a frozen window. A failure is reported as `ERROR <text>` rather than as
    a return code alone, so what went wrong survives into Blender's report.
    """
    process = subprocess.Popen(
        [binary, input_path, output_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    message = ""
    for line in process.stdout:
        line = line.strip()
        if line.startswith("PROGRESS "):
            if on_progress is not None:
                try:
                    on_progress(float(line[9:]))
                except ValueError:
                    pass
        elif line.startswith("ERROR "):
            message = line[6:]
        elif line.startswith("DONE"):
            message = ""

    stderr = process.stderr.read()
    process.wait()

    if process.returncode != 0:
        return False, message or stderr.strip() or f"exit code {process.returncode}"
    return True, ""


# --- running it without freezing Blender -------------------------------------


class Job:
    """One baker process, watched from Blender's modal loop.

    The whole reason the baker is a separate program: this class can be polled
    from a modal operator, so Blender keeps drawing, keeps reporting progress
    and can be cancelled. Cycles' own bake blocks the main thread instead --
    the window stops answering and the desktop offers to kill it, which is the
    complaint that started this.

    Output is drained on a thread rather than polled from the pipe. A pipe that
    nobody reads fills at 64 KB and blocks the *writer*, so a baker printing
    progress would deadlock against a Blender that only checks it between
    timer ticks.
    """

    def __init__(self, binary, input_path, output_path):
        self.input_path = input_path
        self.output_path = output_path
        self.progress = 0.0
        self.device = ""
        self.error = ""
        self.stats = ""
        self._lines = queue.Queue()

        self._process = subprocess.Popen(
            [binary, input_path, output_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        for line in self._process.stdout:
            self._lines.put(line.strip())
        self._process.stdout.close()

    def poll(self):
        """Read whatever has arrived. True once the process has finished."""
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line.startswith("PROGRESS "):
                try:
                    self.progress = float(line[9:])
                except ValueError:
                    pass
            elif line.startswith("DEVICE "):
                self.device = line[7:]
            elif line.startswith("ERROR "):
                self.error = line[6:]
            elif line.startswith("STATS "):
                self.stats = line[6:]

        if self._process.poll() is None:
            return False

        if self._process.returncode != 0 and not self.error:
            self.error = (self._process.stderr.read().strip()
                          or f"exit code {self._process.returncode}")
        return True

    def cancel(self):
        """Stop the bake. The process is killed, not asked."""
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def cleanup(self):
        for path in (self.input_path, self.output_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def prepare(obj, high, directory, size, samples, maps, options,
            occluders=()):
    """Write the input file for one object. Returns its path.

    `occluders` are the other objects whose surfaces shadow this one's AO and
    bent normal -- see `meshmaps.occluders_for`. Nothing else reads them.

    `options` is a `meshmaps.BakeOptions`; taking it rather than a pile of
    numbers keeps the two backends reading the same settings, which is what
    makes switching between them a fair comparison instead of a surprise.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    low = low_poly_soup(obj, depsgraph)
    packed = high_poly_indexed(high, depsgraph) if high is not None else None
    wants_occlusion = maps & (MAP_BITS['AO'] | MAP_BITS['BENT_NORMAL'])
    blockers = (occluder_mesh(occluders, depsgraph)
                if occluders and wants_occlusion else None)

    from . import meshmaps

    diagonal = meshmaps.bbox_diagonal(obj)

    params = [0.0] * PARAM_COUNT
    params[PARAM_EXTRUSION] = options.extrusion
    # The cage ray distance and the AO reach are two different numbers that
    # used to share this slot. They still do -- the projection pass reads it
    # for the cage, the trace pass for AO -- so a bake with a high-poly takes
    # the cage number and one without takes the AO number.
    params[PARAM_RAY_DISTANCE] = (
        options.ray_distance if high is not None
        else options.resolved_ao_distance(diagonal)
    )
    params[PARAM_HEIGHT_RANGE] = options.height_range
    params[PARAM_CURVATURE_SCALE] = options.resolved_curvature_radius(diagonal)
    params[PARAM_AO_MIN_DISTANCE] = options.ao_min_distance
    params[PARAM_AO_SPREAD] = math.radians(
        max(1.0, min(180.0, options.ao_spread)) * 0.5
    )
    params[PARAM_AO_ATTENUATION] = ATTENUATION.get(options.ao_attenuation, 0.0)
    params[PARAM_CURVATURE_LOW] = options.curvature_low
    params[PARAM_CURVATURE_HIGH] = options.curvature_high

    flags = 0
    if options.flip_green:
        flags |= FLAG_FLIP_GREEN
    if options.ao_distribution == 'UNIFORM':
        flags |= FLAG_AO_UNIFORM
    if options.ao_ignore_backface:
        flags |= FLAG_AO_IGNORE_BACKFACE

    path = os.path.join(directory, f"paintr2_{abs(hash(obj.name)):x}.pr2b")
    write_input(path, low, packed, size, samples, maps, params, flags,
                occluders=blockers)
    return path


def apply_maps(obj, produced):
    """Turn the baker's arrays into Blender images, filled and stored.

    The alpha the baker writes is its coverage mask, which is exactly what
    `dilate.fill` needs -- so the same filling the Cycles path uses runs here
    too, rather than a second implementation that could drift from it.
    """
    from . import dilate, meshmaps, storage

    images = {}
    for identifier, pixels in produced.items():
        entry = meshmaps.entry_for(identifier)
        if entry is None:
            continue
        size = pixels.shape[0]
        image = meshmaps._ensure_image(
            meshmaps.map_name(obj, identifier), size, entry[2],
            float_buffer=entry[4],
        )
        image.pixels.foreach_set(pixels.reshape(-1))
        image.update()
        dilate.fill(image)
        storage.adopt_new_image(image)
        meshmaps._store(obj, identifier, image)
        images[identifier] = image
    return images

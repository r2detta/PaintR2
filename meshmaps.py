# SPDX-License-Identifier: GPL-3.0-or-later
"""Mesh maps: what the model itself is, baked into textures.

These are not layers and they are not channels. They are *data about the
mesh* -- where it is thick, where its edges are, which parts are buried --
and their job is to feed things later: the masks and generators that make a
model look used rather than clean.

Nothing consumes them yet (that is a later phase, deliberately). This module
produces them and writes them to disk; the shader graph can already read them
by hand through a Custom pattern.

Six maps. Four of them are baked from the model alone, and two only mean
something against a high-poly:

    AO          Cycles' own AO bake
    Position    Cycles' POSITION bake, remapped into the object's own box
    Thickness   AO again, on a copy whose normals point inward
    Curvature   computed from the mesh, written to vertex colours, baked
    Normal      the high-poly's shape as a tangent-space map    (high only)
    Height      how far the high-poly stands off the low        (high only)

**With a high-poly, every one of them is read off the high-poly instead** --
the low-poly only ever owns the UVs the result lands in. That is Blender's
selected-to-active, and the pairing that decides which high-poly goes with
which low-poly lives in `paintset.py`, not here.

Height is the one with no Cycles pass behind it, because height is not a
property of a surface -- it is a relationship between two. It is computed
from three bakes that do exist; see `_bake_height`.

A cage-less high-poly bake stands on two numbers, `cage_extrusion` and
`max_ray_distance`, and there is no value that is right for every model.
`measure_cage` measures the two surfaces instead of guessing at them.

Every finished map is filled out past its UV islands before it is handed
back -- see `dilate.py`. Blender's bake margin stops after its own handful of
pixels and everything beyond it is left black, which is the black that bleeds
*inward* across a seam as soon as anything filters the texture. That dark
line down every seam was the one thing that made these maps awkward to use.

Two of the maps are Monte Carlo estimates and come back grainy: AO, and
Thickness, which is AO again. Their one knob is `samples`, and it is the only
one worth having: a Cycles bake costs about 0.8 s before it traces anything,
so buying rays with extra *bakes* rather than extra samples costs twenty times
as much for the same result. Measured; see `_bake_ao`.

Curvature's noise is a different thing and has a different knob. It is exact
-- there is no render grain in it -- and what looks like noise is the mesh's
own tessellation. `curvature_iterations` cleans that on the mesh, before the
single bake that carries it to the texture. See `_refine`.

Two things here were measured rather than assumed, and both would have been
silent failures:

* **Thickness needs a ray distance.** On a closed mesh with Blender's default
  distance of 10, every inward ray hits and the map comes out uniformly
  black. The useful range is a fraction of the object's own size, which is
  why `ray_distance` is derived from the bounding box rather than typed.
* **Vertex colours reach a texture through EMIT, not DIFFUSE.** EMIT is
  independent of lighting, which is what a data bake wants.
"""

import contextlib
import math

import bmesh
import bpy
import numpy as np

from . import dilate
from . import storage


MAPS = (
    # (id,          label,        colour space, suffix, float buffer, needs high)
    #
    # Position is the one that needs floats, and needs them *during* the bake
    # rather than for its own precision: Cycles bakes world coordinates, which
    # are negative on one side of the origin, and a byte image clamps those to
    # zero before there is anything to remap. Measured on a model centred on
    # the origin: half the map arrived as a flat 0.502, the value a clamped
    # -X remaps to.
    #
    # The last column marks the two maps that are *only* meaningful against a
    # high-poly. Baking a tangent-space normal from a mesh onto itself gives a
    # flat blue image, and the height between a surface and itself is zero
    # everywhere -- neither is an error worth reporting, so they are skipped
    # with a reason rather than produced as a lie.
    # The last two columns: whether the map needs a high-poly, and whether it
    # needs the external baker. Bent Normal is the only one Cycles has no pass
    # for at all -- it is the average direction light arrives from, which falls
    # out of an AO trace for free and cannot be asked of a bake operator.
    ('AO', "Ambient Occlusion", 'Non-Color', "ao", False, False, False),
    ('CURVATURE', "Curvature", 'Non-Color', "curvature", False, False, False),
    ('THICKNESS', "Thickness", 'Non-Color', "thickness", False, False, False),
    ('POSITION', "Position", 'Non-Color', "position", True, False, False),
    ('NORMAL', "Normal", 'Non-Color', "normal", False, True, False),
    ('HEIGHT', "Height", 'Non-Color', "height", True, True, False),
    ('BENT_NORMAL', "Bent Normal", 'Non-Color', "bent", False, False, True),
)

MAP_IDS = tuple(entry[0] for entry in MAPS)

_BY_ID = {entry[0]: entry for entry in MAPS}

# The temporary colour attribute curvature is written into.
CURVATURE_ATTRIBUTE = "PaintR2 Curvature"

# How far a thickness ray travels, as a fraction of the object's bounding box
# diagonal. Measured on a model 3.7 units across: 0.25 and 0.5 gave a spread
# of 0.88 and 0.81, while Blender's default of 10 gave 0.06 -- a black image.
THICKNESS_REACH = 0.2

# Bake passes that are geometry rather than light. One sample is the whole
# answer for these -- and for POSITION, one sample is the *only* right answer.
#
# **Measured on 5.2: the POSITION pass accumulates across Cycles samples and
# is never divided by the sample count.** A plane spanning world x -1..1 bakes
# its maximum as 0.97 at one sample, 1.97 at two, 3.92 at four, 15.66 at
# sixteen -- exactly `samples * x` every time. NORMAL and AO are unaffected;
# they come back normalised at every sample count.
#
# This was a live bug and a quiet one. `_bake_position` ran at sixteen samples
# and then remapped the raw value into the object's bounding box, so the
# Position map was sixteen times too large before it was normalised and came
# out almost entirely clamped to flat 0 and flat 1. The test it had asked only
# whether the map reached 0 and 1 -- which a fully clamped map does.
#
# Forcing one sample fixes it at the source: the raw value then *is* the world
# coordinate, there is no factor to carry around, and an exact pass stops
# paying for samples it cannot use.
_EXACT_PASSES = frozenset({'POSITION', 'NORMAL', 'UV'})


def map_label(identifier):
    entry = _BY_ID.get(identifier)
    return entry[1] if entry else identifier.title()


def map_suffix(identifier):
    entry = _BY_ID.get(identifier)
    return entry[3] if entry else identifier.lower()


def map_name(obj, identifier):
    return f"{obj.name}_{map_suffix(identifier)}"


def needs_high(identifier):
    """True for a map that says nothing unless there is a high-poly."""
    entry = _BY_ID.get(identifier)
    return bool(entry[5]) if entry else False


def needs_external(identifier):
    """True for a map only the external baker can produce."""
    entry = _BY_ID.get(identifier)
    return bool(entry[6]) if entry else False


def entry_for(identifier):
    """The MAPS row for an identifier, or None."""
    return _BY_ID.get(identifier)


class BakeOptions:
    """Everything a high-poly bake needs beyond "which map".

    A plain object rather than a pile of arguments: these travel together
    through five functions, and every one of them would otherwise grow the
    same six parameters.

    `extrusion` and `ray_distance` are the two numbers a cage-less bake lives
    or dies by. The ray starts `extrusion` metres out along the low-poly's
    normal and travels inward for `ray_distance`; a high-poly detail sticking
    out further than the extrusion is missed, and one buried deeper than the
    ray reaches is missed too. There is no value that is right for every
    model, which is why `measure()` exists.
    """

    def __init__(self, extrusion=0.05, ray_distance=0.0, samples=16,
                 margin_type='ADJACENT_FACES', flip_green=False,
                 height_range=0.05, curvature_iterations=1,
                 ao_distance=1.0, ao_distance_relative=True,
                 ao_min_distance=0.0, ao_spread=180.0,
                 ao_distribution='COSINE', ao_attenuation='NONE',
                 ao_ignore_backface=False,
                 curvature_radius=0.02, curvature_radius_relative=True,
                 curvature_low=-1.0, curvature_high=1.0):
        self.extrusion = float(extrusion)
        self.ray_distance = float(ray_distance)
        self.samples = int(samples)
        self.margin_type = margin_type
        self.flip_green = bool(flip_green)
        self.height_range = max(1e-6, float(height_range))
        self.curvature_iterations = max(1, int(curvature_iterations))

        # Ambient occlusion. Only the external baker reads these; the Cycles
        # path has an `object.bake` operator with its own two knobs and no way
        # to be told any of this, which is said out loud in the panel rather
        # than left to be discovered.
        self.ao_distance = float(ao_distance)
        self.ao_distance_relative = bool(ao_distance_relative)
        self.ao_min_distance = float(ao_min_distance)
        self.ao_spread = float(ao_spread)
        self.ao_distribution = ao_distribution
        self.ao_attenuation = ao_attenuation
        self.ao_ignore_backface = bool(ao_ignore_backface)

        self.curvature_radius = float(curvature_radius)
        self.curvature_radius_relative = bool(curvature_radius_relative)
        self.curvature_low = float(curvature_low)
        self.curvature_high = float(curvature_high)

    def resolved_ao_distance(self, diagonal):
        """The AO reach in world units, whichever way it was written."""
        if self.ao_distance <= 0.0:
            return 0.0
        return (self.ao_distance * diagonal if self.ao_distance_relative
                else self.ao_distance)

    def resolved_curvature_radius(self, diagonal):
        if self.curvature_radius_relative:
            return self.curvature_radius * diagonal
        return self.curvature_radius

    @classmethod
    def from_scene(cls, scene):
        props = getattr(scene, "paintr2", None)
        if props is None:
            return cls()
        return cls(
            extrusion=props.cage_extrusion,
            ray_distance=props.max_ray_distance,
            samples=props.mesh_map_samples,
            margin_type=props.bake_margin_type,
            flip_green=props.normal_flip_green,
            height_range=props.height_range,
            curvature_iterations=props.curvature_iterations,
            ao_distance=props.ao_distance,
            ao_distance_relative=props.ao_distance_relative,
            ao_min_distance=props.ao_min_distance,
            ao_spread=props.ao_spread,
            ao_distribution=props.ao_distribution,
            ao_attenuation=props.ao_attenuation,
            ao_ignore_backface=props.ao_ignore_backface,
            curvature_radius=props.curvature_radius,
            curvature_radius_relative=props.curvature_radius_relative,
            curvature_low=props.curvature_low,
            curvature_high=props.curvature_high,
        )


class MapResult:
    """What one bake produced."""

    def __init__(self):
        self.images = {}        # identifier -> Image
        self.error = None
        self.skipped = []       # (identifier, reason)


# --- geometry helpers --------------------------------------------------------


def bbox_diagonal(obj):
    """The object's bounding box diagonal in world units."""
    corners = [obj.matrix_world @ mathutils_vector(c) for c in obj.bound_box]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    return math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))) or 1.0


def mathutils_vector(triple):
    from mathutils import Vector

    return Vector(triple)


def world_bounds(obj):
    """(lo, hi) of the object's bounding box in world space."""
    corners = [obj.matrix_world @ mathutils_vector(c) for c in obj.bound_box]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    return lo, hi


# --- measuring the cage -------------------------------------------------------
#
# Extrusion and ray distance are the two numbers a cage-less high-poly bake
# lives or dies by, and the usual way to find them is to bake, squint at the
# result, change one, bake again. They do not have to be guessed: the models
# are right there, and the distance between them is a thing that can be
# measured.
#
# For every vertex of the low-poly, the nearest point on the high-poly is
# found, and its side is read off the low-poly's own normal:
#
#     outward   the high-poly sticks out past the low   -> extrusion must clear it
#     inward    the high-poly is buried inside the low  -> the ray must reach it
#
# The reported numbers are the *measured maximums*. What the fields get is
# those times a safety factor, because a vertex is a sample and the surface
# between two of them can bulge further than either.


class CageMeasure:
    """What the two models actually cost, in metres."""

    def __init__(self):
        self.outward = 0.0
        self.inward = 0.0
        self.samples = 0
        self.error = None

    @property
    def ok(self):
        return self.error is None and self.samples > 0

    def extrusion(self, safety=1.15):
        return self.outward * safety

    def ray_distance(self, safety=1.15):
        # The ray starts at the extruded position, so it has to cross the
        # extrusion again before it reaches anything buried.
        return (self.outward + self.inward) * safety * 2.0


def _world_bvh(obj):
    """A BVH of `obj`'s evaluated surface, in world space.

    Built from transformed polygons rather than with `BVHTree.FromObject`.
    **Measured on 5.2: FromObject builds the tree in the object's own local
    space.** Querying it with a world point silently returns the distance to
    somewhere else entirely -- a high-poly sitting 0.1 above its low-poly
    measured as 0.0 apart, because in local space both are at the origin.
    """
    from mathutils.bvhtree import BVHTree

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        count = len(mesh.vertices)
        if count == 0 or len(mesh.polygons) == 0:
            return None

        coords = np.empty(count * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", coords)
        coords = coords.reshape(-1, 3)

        matrix = np.array(obj.matrix_world, dtype=np.float64)
        world = coords @ matrix[:3, :3].T + matrix[:3, 3]

        polygons = [list(polygon.vertices) for polygon in mesh.polygons]
        return BVHTree.FromPolygons(
            [tuple(point) for point in world], polygons
        )
    finally:
        evaluated.to_mesh_clear()


def measure_cage(low, high):
    """How far apart the two surfaces are. Returns a CageMeasure."""
    result = CageMeasure()

    if low is None or high is None or low.type != 'MESH' or high.type != 'MESH':
        result.error = RuntimeError("measuring needs two mesh objects")
        return result

    try:
        tree = _world_bvh(high)
    except (RuntimeError, ValueError) as error:
        result.error = error
        return result

    if tree is None:
        result.error = RuntimeError(f"{high.name} has no faces to measure")
        return result

    matrix = low.matrix_world
    # A normal is a direction, so it transforms by the inverse transpose --
    # a non-uniformly scaled object would otherwise report normals that are
    # not perpendicular to its own surface, and every side test would be
    # decided by the scale rather than by the geometry.
    normal_matrix = matrix.to_3x3().inverted_safe().transposed()

    mesh = low.data
    count = len(mesh.vertices)
    if count == 0:
        result.error = RuntimeError(f"{low.name} has no geometry")
        return result

    coords = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    normals = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("normal", normals)

    coords = coords.reshape(-1, 3)
    normals = normals.reshape(-1, 3)

    outward = 0.0
    inward = 0.0
    hits = 0

    for index in range(count):
        point = matrix @ mathutils_vector(coords[index])
        nearest, _face_normal, _face, distance = tree.find_nearest(point)
        if nearest is None:
            continue

        hits += 1
        direction = normal_matrix @ mathutils_vector(normals[index])
        length = direction.length
        if length > 1e-9:
            direction = direction / length
            side = (nearest - point).dot(direction)
        else:
            side = 0.0

        if side >= 0.0:
            outward = max(outward, float(distance))
        else:
            inward = max(inward, float(distance))

    result.outward = outward
    result.inward = inward
    result.samples = hits
    if hits == 0:
        result.error = RuntimeError("the two models do not overlap anywhere")
    return result


# --- curvature ---------------------------------------------------------------


def curvature_values(obj, smooth=1, iterations=1):
    """A 0..1 curvature per vertex. 0.5 is flat, above is convex.

    Read off the mesh rather than off a normal map, so no high-poly is needed
    and the result is deterministic. `BMEdge.calc_face_angle_signed` is
    Blender's own measure of how two faces meet: positive where the surface
    bulges, negative where it folds inward.

    Each edge's angle is spread onto its two vertices, weighted by edge
    length -- a long crease should count for more than a short one -- and the
    accumulated angle is then squashed into 0..1.

    The raw field is then cleaned in two separate ways, in this order:

    `iterations`
        Edge-preserving refinement -- `_refine`. Averages each vertex with the
        neighbours that *agree* with it, so tessellation wobble flattens out
        and creases do not. This is the knob to raise for a cleaner map.

    `smooth`
        Plain averaging with every neighbour -- `_smooth_over_edges`. Blurs
        creases along with the noise, which is sometimes what is wanted.

    They are not the same operation and neither replaces the other: one
    removes noise, the other removes detail.

    The known cost of computing curvature this way at all: the detail is
    limited by how dense the mesh is. A four-poly box has four edges to say
    anything with, and no number of passes invents a fifth.
    """
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    totals = np.zeros(len(bm.verts), dtype=np.float64)
    weights = np.zeros(len(bm.verts), dtype=np.float64)

    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        try:
            angle = edge.calc_face_angle_signed()
        except ValueError:
            continue
        weight = edge.calc_length()
        for vert in edge.verts:
            totals[vert.index] += angle * weight
            weights[vert.index] += weight

    bm.free()

    with np.errstate(invalid='ignore', divide='ignore'):
        mean = np.where(weights > 0.0, totals / np.maximum(weights, 1e-9), 0.0)

    # An angle of +-pi/2 is a hard right-angle edge; anything past that is
    # rarer than it is useful, so that is where the scale is pinned.
    values = 0.5 + np.clip(mean / math.pi, -0.5, 0.5)

    edges = _edge_pairs(mesh)
    count = len(mesh.vertices)

    refinements = max(0, int(iterations))
    if refinements:
        # Measured once, off the raw field. Recomputing it every pass would
        # shrink it as the field cleans up, and the filter would keep
        # tightening until it stopped doing anything at all.
        scale = _noise_scale(values)
        for _ in range(refinements):
            values = _refine(edges, values, count, scale)

    for _ in range(max(0, int(smooth))):
        values = _smooth_over_edges(edges, values, count)

    return values.astype(np.float32)


def _edge_pairs(mesh):
    """The mesh's edges as an (n, 2) array of vertex indices."""
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", edges)
    return edges.reshape(-1, 2)


def _smooth_over_edges(edges, values, count):
    """One pass of averaging each vertex with its neighbours."""
    totals = values.copy()
    counts = np.ones(count, dtype=np.float64)

    np.add.at(totals, edges[:, 0], values[edges[:, 1]])
    np.add.at(counts, edges[:, 0], 1.0)
    np.add.at(totals, edges[:, 1], values[edges[:, 0]])
    np.add.at(counts, edges[:, 1], 1.0)

    return totals / counts


def _noise_scale(values):
    """How far a vertex normally sits from the middle -- the noise floor.

    Median absolute deviation, scaled by the constant that turns it into the
    standard deviation of a normal distribution. It is a *robust* spread: a
    crease is a large and rare deviation, and a handful of them barely move a
    median. What comes back is therefore the size of the wobble rather than
    the size of the detail -- which is exactly the number `_refine` needs, a
    line between "this difference is noise" and "this difference is a feature".

    The floor matters. On a clean mesh most vertices are flat 0.5 and the
    deviation is genuinely zero; without a floor the weight below would divide
    by it.
    """
    middle = np.median(values)
    spread = float(np.median(np.abs(values - middle))) * 1.4826
    return max(spread, 1e-3)


def _refine(edges, values, count, scale):
    """One edge-preserving pass: average only with neighbours that agree.

    Plain smoothing treats a crease and a bad triangle the same way, because
    both are a vertex that disagrees with its neighbours -- so raising it
    trades noise for detail one for one, and there is no setting that gets
    both. This weights each neighbour by how close it already is:

        w = exp(-((neighbour - self) / scale) ** 2)

    A neighbour within the noise floor counts nearly in full and its wobble
    averages out. A neighbour across a crease is several times `scale` away,
    its weight is effectively zero, and the crease survives the pass intact.

    That is what makes the pass worth repeating. Plain smoothing converges on
    a flat field, so more of it is always worse; this converges on the field
    with the noise gone and the edges still in it, so more of it is better
    until it stops changing.
    """
    left = edges[:, 0]
    right = edges[:, 1]

    difference = (values[right] - values[left]) / scale
    weight = np.exp(-difference * difference)

    totals = values.copy()          # each vertex counts for itself, at 1.0
    counts = np.ones(count, dtype=np.float64)

    np.add.at(totals, left, values[right] * weight)
    np.add.at(counts, left, weight)
    np.add.at(totals, right, values[left] * weight)
    np.add.at(counts, right, weight)

    return totals / counts


# --- the temporary shading a data bake needs ---------------------------------


class _DataShading:
    """Point every material slot at one source, for the length of a bake.

    A data bake must not read the object's real materials -- what is wanted is
    the attribute, not the look. So each slot is temporarily reduced to
    `source -> Emission -> Output`, and put back afterwards.

    Materials are rebuilt from a saved node list rather than patched, because
    a PaintR2 material's wiring is generated anyway and everything else is
    restored from the copy taken here.
    """

    def __init__(self, obj, attribute=None):
        self.obj = obj
        self.attribute = attribute
        self.materials = []
        self.temporary = []
        self.lent = None

    def __enter__(self):
        usable = [
            slot.material for slot in self.obj.material_slots
            if slot.material is not None and slot.material.use_nodes
        ]

        if not usable:
            # A high-poly is a sculpt, and a sculpt usually has no material at
            # all. Cycles then shades it with its default grey, an EMIT bake of
            # which is black -- so the curvature map would come back empty and
            # nothing would say why. One is lent for the length of the bake.
            self.lent = bpy.data.materials.new("PaintR2 Map Shading")
            self.lent.use_nodes = True
            self.obj.data.materials.append(self.lent)
            usable = [self.lent]

        for material in usable:
            self.materials.append(material)
            self._install(material)
        return self

    def _install(self, material):
        tree = material.node_tree

        emission = tree.nodes.new('ShaderNodeEmission')
        emission.name = "PaintR2 Map Emission"
        output = tree.nodes.new('ShaderNodeOutputMaterial')
        output.name = "PaintR2 Map Output"
        output.is_active_output = True
        self.temporary.append((material, [emission, output]))

        if self.attribute is not None:
            source = tree.nodes.new('ShaderNodeVertexColor')
            source.name = "PaintR2 Map Source"
            source.layer_name = self.attribute
            tree.links.new(source.outputs["Color"], emission.inputs["Color"])
            self.temporary[-1][1].append(source)
        else:
            emission.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)

        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])

    def __exit__(self, *exc):
        for material, nodes in self.temporary:
            if material is self.lent:
                continue
            for node in nodes:
                try:
                    material.node_tree.nodes.remove(node)
                except (RuntimeError, ReferenceError):
                    pass
            # Whatever output was active before becomes active again as soon
            # as ours is gone; a PaintR2 material also gets its wiring back.
            _restore_output(material)

        if self.lent is not None:
            # Borrowed, so it goes back whole: the slot as well as the nodes.
            materials = self.obj.data.materials
            for index in reversed(range(len(materials))):
                if materials[index] is self.lent:
                    materials.pop(index=index)
            bpy.data.materials.remove(self.lent)
            self.lent = None
        return False


def _restore_output(material):
    from . import nodegen

    if hasattr(material, "paintr2") and len(material.paintr2.layers):
        nodegen.compile_material(material)
        return

    for node in material.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeOutputMaterial':
            node.is_active_output = True
            return


# --- bake plumbing -----------------------------------------------------------


def _ensure_image(name, size, colorspace, float_buffer=False):
    existing = bpy.data.images.get(name)
    if existing is not None:
        if tuple(existing.size) == (size, size) and existing.is_float == float_buffer:
            existing.colorspace_settings.name = colorspace
            return existing
        bpy.data.images.remove(existing)

    image = bpy.data.images.new(
        name, width=size, height=size, alpha=True, float_buffer=float_buffer
    )
    image.colorspace_settings.name = colorspace
    return image


def _add_targets(obj, image):
    """A selected, active Image Texture in every material slot."""
    added = []
    for slot in obj.material_slots:
        material = slot.material
        if material is None or not material.use_nodes:
            continue
        tree = material.node_tree
        node = tree.nodes.new('ShaderNodeTexImage')
        node.name = "PaintR2 Map Target"
        node.image = image
        for other in tree.nodes:
            other.select = False
        node.select = True
        tree.nodes.active = node
        added.append((material, node))
    return added


def _remove_targets(added):
    for material, node in added:
        try:
            material.node_tree.nodes.remove(node)
        except (RuntimeError, ReferenceError):
            pass


def _bake_device():
    """'GPU' when Cycles has a compute device configured, else 'CPU'.

    `scene.cycles.device` defaults to `'CPU'` and is a *scene* setting, so
    having picked CUDA in the preferences does not move it -- a user who
    configured their GPU years ago still bakes on the processor and has no
    reason to suspect it. Measured on a 126k-face high-poly, AO at 2048 with
    128 samples: 11.04 s on a 12900K, 3.14 s on a 3090 Ti. Nothing about that
    is a choice worth making by hand, so it is not offered as one.

    Read off the preferences rather than assumed, because `device = 'GPU'`
    with no device enabled is not an error -- Cycles falls back to the CPU
    quietly, and the setting would then be a lie in the scene the user gets
    back.

    **OptiX is deliberately not preferred over CUDA.** It is the faster
    backend for rendering and it is the slower one here: the same bake
    measured 3.14 s on CUDA and 10.33 s on OptiX with the kernels already
    warm. Whatever the preferences say is what gets used -- this function
    only decides GPU or not.
    """
    addon = bpy.context.preferences.addons.get("cycles")
    if addon is None:
        return 'CPU'

    preferences = addon.preferences
    if getattr(preferences, "compute_device_type", 'NONE') == 'NONE':
        return 'CPU'

    try:
        devices = preferences.devices
    except AttributeError:
        return 'CPU'

    wanted = preferences.compute_device_type
    for device in devices:
        if device.use and device.type == wanted:
            return 'GPU'
    return 'CPU'


class _BakeSettings:
    """Push the scene into a state a data bake needs, and pop it back."""

    def __init__(self, scene, margin, ao_distance=None, options=None,
                 samples=None):
        self.scene = scene
        self.margin = margin
        self.ao_distance = ao_distance
        self.options = options or BakeOptions()
        # None means "whatever the user asked for". A number overrides it, and
        # the only caller that passes one is the exact-pass path -- see
        # `_EXACT_PASSES`.
        self.samples = samples
        self.previous = {}
        self.temp_world = None

    def __enter__(self):
        scene = self.scene
        bake = scene.render.bake
        self.previous = {
            "margin": bake.margin,
            "use_clear": bake.use_clear,
            "target": bake.target,
            "engine": scene.render.engine,
        }
        bake.margin = self.margin
        # Off, deliberately: `_blank` does the clearing instead. See it for
        # why -- Blender's own clear makes the alpha channel useless.
        bake.use_clear = False
        bake.target = 'IMAGE_TEXTURES'

        if scene.render.engine != 'CYCLES':
            try:
                scene.render.engine = 'CYCLES'
            except TypeError:
                self.previous["engine"] = None

        if hasattr(scene, "cycles"):
            self.previous["device"] = scene.cycles.device
            scene.cycles.device = _bake_device()

            self.previous["samples"] = scene.cycles.samples
            wanted = self.samples if self.samples else self.options.samples
            scene.cycles.samples = max(1, wanted)

        # An AO bake reads its ray distance off the world, and a scene without
        # a world has nothing to read it from -- so one is lent for the bake.
        if scene.world is None:
            self.temp_world = bpy.data.worlds.new("PaintR2 Bake World")
            scene.world = self.temp_world

        if self.ao_distance is not None:
            self.previous["ao_distance"] = scene.world.light_settings.distance
            scene.world.light_settings.distance = self.ao_distance

        return self

    def __exit__(self, *exc):
        scene = self.scene
        bake = scene.render.bake
        bake.margin = self.previous["margin"]
        bake.use_clear = self.previous["use_clear"]
        bake.target = self.previous["target"]

        if "ao_distance" in self.previous and scene.world is not None:
            scene.world.light_settings.distance = self.previous["ao_distance"]

        if self.temp_world is not None:
            scene.world = None
            bpy.data.worlds.remove(self.temp_world)

        if self.previous.get("engine"):
            scene.render.engine = self.previous["engine"]
        if "samples" in self.previous and hasattr(scene, "cycles"):
            scene.cycles.samples = self.previous["samples"]
        if "device" in self.previous and hasattr(scene, "cycles"):
            scene.cycles.device = self.previous["device"]
        return False


def _bake_into(obj, image, bake_type, margin, ao_distance=None, attribute=None,
               source=None, options=None, **extra):
    """Run one bake into `image`, on `obj`'s UVs. Raises on failure.

    `source` is what the shading is read *from*. With no source this is a bake
    of `obj` onto itself, which is what the four mesh maps have always been.
    With one, it is Blender's selected-to-active: rays leave `obj`'s surface,
    land on `source`, and what `source` shades there is written into `obj`'s
    UVs. That is the whole of high-poly baking.

    The two cage numbers are passed to the operator rather than left on the
    scene. Blender falls back to `scene.render.bake` for any property the call
    does not set, and "the value we meant, unless something else on the scene
    says otherwise" is not a thing a bake should be built on.

    An EMIT bake shades whichever object is being read -- `source` when there
    is one, `obj` when there is not. Getting that backwards would put the
    low-poly's vertex colours on the map and look almost right.
    """
    context = bpy.context
    scene = context.scene
    options = options or BakeOptions()

    targets = _add_targets(obj, image)
    if not targets:
        raise RuntimeError(f"{obj.name} has no material to bake through")

    # `is not None` is not defensive noise: removing an object leaves a null
    # slot in `view_layer.objects` until the view layer is updated, and the
    # thickness bake removes its temporary copy right before this runs.
    selection = [
        o for o in context.view_layer.objects
        if o is not None and o.select_get()
    ]
    active = context.view_layer.objects.active

    shaded = source if source is not None else obj

    try:
        # Not `bpy.ops.object.select_all`: it fails in Texture Paint mode,
        # which is the mode the model is in for as long as the PaintR2 window
        # is open. See bake.select_only.
        for scene_object in context.view_layer.objects:
            if scene_object.select_get():
                scene_object.select_set(False)

        if source is not None:
            # Selected-to-active is exactly that: the high-poly is selected,
            # the low-poly is active, and the low-poly must NOT be selected --
            # a selected active object bakes onto itself as well and the map
            # comes back as the low-poly's own shading in places.
            source.select_set(True)
            extra.setdefault("use_selected_to_active", True)
            extra.setdefault("cage_extrusion", options.extrusion)
            extra.setdefault("max_ray_distance", options.ray_distance)
        else:
            obj.select_set(True)
        context.view_layer.objects.active = obj

        dilate.blank(image)

        with _BakeSettings(scene, margin, ao_distance, options,
                           samples=1 if bake_type in _EXACT_PASSES else None):
            if scene.render.engine != 'CYCLES':
                raise RuntimeError(
                    "Mesh maps need the Cycles render engine, which is not "
                    "available in this Blender build"
                )
            extra.setdefault("margin_type", options.margin_type)
            if bake_type == 'EMIT':
                with _DataShading(shaded, attribute):
                    bpy.ops.object.bake(type='EMIT', **extra)
            else:
                bpy.ops.object.bake(type=bake_type, **extra)
    finally:
        _remove_targets(targets)
        for scene_object in context.view_layer.objects:
            if scene_object is None:
                continue
            scene_object.select_set(scene_object in selection)
        context.view_layer.objects.active = active


# --- what the rays can see ---------------------------------------------------


# The maps other models may shadow. Thickness is left out on purpose: it is
# how deep *this* shape is, and a neighbour pressed against it would read as
# the model being thick where it is not. Everything else is either not traced
# (Curvature, Position) or traced selected-to-active, where Cycles only ever
# hits the selected high-poly.
OCCLUDED_MAPS = frozenset({'AO', 'BENT_NORMAL'})


def occluders_for(obj, models, high_of=None):
    """What shadows `obj`'s AO besides itself: every other model, as the
    surface that stands for it.

    `high_of(model)` returns a model's high-poly or None. With a high-poly
    bake the high-poly *is* the shape -- it is what `obj`'s own AO is read
    off -- so a neighbour is represented by its high-poly too, and by its
    low-poly only when it has none. Both at once would shadow twice.
    """
    out = []
    for model in models:
        if model is None or model == obj:
            continue
        partner = high_of(model) if high_of is not None else None
        out.append(partner if partner is not None else model)
    return out


@contextlib.contextmanager
def render_only(keep):
    """Take every object but `keep` out of the bake for the duration.

    **`hide_render`, and not `hide_set`, and this was measured:** a cube
    hidden in the viewport over a plane still darkened the plane's AO bake to
    0.337; with `hide_render` it came back 1.0. A Cycles bake traces whatever
    would render, so viewport hiding -- including the session's isolation --
    decides nothing about what shadows a map. Without this, *every* visible
    object in the file was an occluder, whatever the settings said.

    Only objects this switches off are switched back on, so one the user had
    already excluded from rendering stays excluded.
    """
    names = {o.name for o in keep if o is not None}
    switched = []
    for scene_object in bpy.context.scene.objects:
        if scene_object.name in names or scene_object.hide_render:
            continue
        scene_object.hide_render = True
        switched.append(scene_object.name)
    try:
        yield
    finally:
        for name in switched:
            scene_object = bpy.data.objects.get(name)
            if scene_object is not None:
                scene_object.hide_render = False


# --- the four maps -----------------------------------------------------------


def _bake_ao(obj, image, margin, high=None, options=None):
    """One bake. Grain is bought down with samples, not with more bakes.

    **Measured, and worth measuring because the opposite is intuitive:** a
    Cycles bake carries roughly 0.8 s of fixed cost -- scene sync, BVH build,
    kernel setup -- before it traces its first ray, and that cost is per
    *bake*, not per sample. Splitting one ray budget across passes pays it
    again every time. Thickness on a 500-face Suzanne at 1024, 512 rays either
    way:

        16 samples x 32 passes    32.78 s
        512 samples x 1 pass       1.61 s

    Twenty times slower for the same rays -- and *noisier*, because each pass
    lands in a byte image and is quantised to 1/255 before anything can be
    averaged, while Cycles accumulates its own samples in float.

    So an averaging loop buys nothing here and PaintR2 no longer has one.
    Samples is the knob.
    """
    _bake_into(obj, image, 'AO', margin, source=high, options=options)


def _bake_position(obj, image, margin, high=None, options=None):
    """POSITION, then remapped into the box of whatever was read.

    Cycles bakes world-space position, which is a number that means nothing
    on its own -- move the object and every pixel changes. Normalised against
    the bounding box it becomes "where on this model am I", which is what the
    map is for.

    The box is the high-poly's when there is one: the positions written are
    points on the high-poly's surface, and normalising them against the
    low-poly's box would push anything that sticks out past 0 or 1 and clamp
    it flat.
    """
    _bake_into(obj, image, 'POSITION', margin, source=high, options=options)

    lo, hi = world_bounds(high if high is not None else obj)
    span = [max(hi[i] - lo[i], 1e-6) for i in range(3)]

    pixels = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(-1, 4)
    for channel in range(3):
        pixels[:, channel] = np.clip(
            (pixels[:, channel] - lo[channel]) / span[channel], 0.0, 1.0
        )
    image.pixels.foreach_set(pixels.reshape(-1))
    image.update()


def _bake_thickness(obj, image, margin, reach, high=None, options=None):
    """AO on a copy whose normals point inward.

    The copy is what makes it safe: flipping the real mesh and flipping it
    back would leave the user's model changed if anything raised in between.

    With a high-poly it is the *high-poly* that gets flipped and read; the
    low-poly still owns the UVs the result lands in. Thickness is a fact
    about the shape, and the shape is the high-poly's.
    """
    origin = high if high is not None else obj
    options = options or BakeOptions()

    copy = origin.copy()
    copy.data = origin.data.copy()
    bpy.context.collection.objects.link(copy)
    try:
        copy.data.flip_normals()
        if high is not None:
            _bake_into(obj, image, 'AO', margin, ao_distance=reach,
                       source=copy, options=options)
        else:
            _bake_into(copy, image, 'AO', margin, ao_distance=reach,
                       options=options)
    finally:
        data = copy.data
        bpy.data.objects.remove(copy, do_unlink=True)
        bpy.data.meshes.remove(data)
        # And make the view layer notice, so the next bake does not walk over
        # the hole the removal left in its object list.
        bpy.context.view_layer.update()


def _bake_curvature(obj, image, margin, smooth, high=None, options=None):
    """Computed on the mesh, carried to the texture through vertex colours.

    Which mesh: the high-poly's when there is one. That is the point of having
    it -- the low-poly's four-poly box has four edges to say anything with,
    and the high-poly has the creases the map is wanted for.

    Curvature is exact rather than sampled, so iterations mean something else
    here than they do for AO: there is no Cycles grain to average away, and
    what there is instead is tessellation wobble in the field being baked.
    The iterations clean that, on the mesh, before the single EMIT pass takes
    it to the texture. One bake either way -- the count costs milliseconds,
    not another render.
    """
    origin = high if high is not None else obj
    options = options or BakeOptions()
    mesh = origin.data
    values = curvature_values(origin, smooth=smooth,
                              iterations=options.curvature_iterations)

    existing = mesh.color_attributes.get(CURVATURE_ATTRIBUTE)
    if existing is not None:
        mesh.color_attributes.remove(existing)

    attribute = mesh.color_attributes.new(
        name=CURVATURE_ATTRIBUTE, type='FLOAT_COLOR', domain='POINT'
    )
    flat = np.repeat(values, 4).reshape(-1, 4)
    flat[:, 3] = 1.0
    attribute.data.foreach_set("color", flat.reshape(-1))

    try:
        _bake_into(obj, image, 'EMIT', margin, attribute=CURVATURE_ATTRIBUTE,
                   source=high, options=options)
    finally:
        leftover = mesh.color_attributes.get(CURVATURE_ATTRIBUTE)
        if leftover is not None:
            mesh.color_attributes.remove(leftover)


def _bake_normal(obj, image, margin, high, options):
    """The high-poly's shape, as a tangent-space normal map on the low.

    The green channel is a choice, not a detail. OpenGL reads +Y up, DirectX
    reads it down, and a map baked for the wrong one lights every dent as a
    bump. Blender bakes +Y; `flip_green` writes the other convention by
    swizzling the channel at bake time rather than inverting pixels after,
    which would also invert the parts of the image the margin filled.
    """
    _bake_into(
        obj, image, 'NORMAL', margin, source=high, options=options,
        normal_space='TANGENT',
        normal_g='NEG_Y' if options.flip_green else 'POS_Y',
    )


def _bake_height(obj, image, margin, high, options):
    """How far the high-poly stands off the low, along the low's normal.

    Cycles has no height pass -- height is not a property of a surface, it is
    a *relationship between two* -- so it is measured from three bakes that
    do exist, in the low-poly's own UV space:

        POSITION, selected to active   where the high-poly surface is
        POSITION, on the low alone     where the low-poly surface is
        NORMAL in object space, alone  which way the low-poly faces

    The height at a texel is then `dot(high - low, normal)`, which is signed:
    positive where the high-poly stands proud, negative where it is cut in.

    Written with 0.5 as flat, the same convention the Height *channel* uses
    and the one every engine reads. `height_range` is the distance that
    reaches 0 and 1; anything further is clamped, and the map says so by
    going flat white or flat black rather than by wrapping around.

    The two position bakes must be float buffers. A byte image clamps a
    negative world coordinate to zero, and a model on the -X side of the
    origin would have half its height computed from a flat 0.
    """
    size = image.size[0]

    high_pos = _ensure_image(
        f"{map_name(obj, 'HEIGHT')}_src_high", size, 'Non-Color', float_buffer=True
    )
    low_pos = _ensure_image(
        f"{map_name(obj, 'HEIGHT')}_src_low", size, 'Non-Color', float_buffer=True
    )
    low_nrm = _ensure_image(
        f"{map_name(obj, 'HEIGHT')}_src_normal", size, 'Non-Color', float_buffer=True
    )

    try:
        _bake_into(obj, high_pos, 'POSITION', margin, source=high, options=options)
        _bake_into(obj, low_pos, 'POSITION', margin, options=options)
        _bake_into(obj, low_nrm, 'NORMAL', margin, options=options,
                   normal_space='OBJECT')

        high_px = _read(high_pos)
        low_px = _read(low_pos)
        nrm_px = _read(low_nrm)

        # An object-space normal bake writes (n + 1) / 2, and it writes the
        # object's own space -- so it has to come back to world before it can
        # be dotted with a difference of world positions.
        normals = nrm_px[:, :3] * 2.0 - 1.0
        matrix = np.array(
            obj.matrix_world.to_3x3().inverted_safe().transposed(),
            dtype=np.float32,
        )
        normals = normals @ matrix.T
        length = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.divide(normals, np.maximum(length, 1e-9))

        offset = high_px[:, :3] - low_px[:, :3]
        height = np.sum(offset * normals, axis=1)

        # Where no ray found the high-poly, the bake wrote (0, 0, 0, 0) and
        # the difference is a distance to the origin rather than to anything.
        # Measured: alpha comes back exactly 0 there and exactly 1 elsewhere,
        # so it is the mask, not a guess at one. Note this is the bake writing
        # the zero itself rather than the blank showing through -- it holds
        # under either clear; see `dilate.blank`.
        missed = high_px[:, 3] < 0.5
        height[missed] = 0.0

        out = np.clip(0.5 + height / (2.0 * options.height_range), 0.0, 1.0)

        pixels = np.empty((out.shape[0], 4), dtype=np.float32)
        pixels[:, 0] = out
        pixels[:, 1] = out
        pixels[:, 2] = out
        # Alpha is the low-poly's own UV coverage, carried over from its
        # position bake, because this is the one map whose pixels are computed
        # rather than rendered -- writing a flat 1 here would tell `dilate`
        # that the empty half of the texture had been baked, and it would
        # leave it black. The high-poly's coverage is deliberately *not* what
        # is used: a texel the rays missed is still a texel of the low-poly,
        # and 0.5 (flat) is the right answer there.
        pixels[:, 3] = np.where(low_px[:, 3] >= 0.5, 1.0, 0.0)
        image.pixels.foreach_set(pixels.reshape(-1))
        image.update()
    finally:
        for scratch in (high_pos, low_pos, low_nrm):
            if scratch.name in bpy.data.images:
                bpy.data.images.remove(scratch)


def _read(image):
    """An image as an (n, 4) float array."""
    flat = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(flat)
    return flat.reshape(-1, 4)


# --- public ------------------------------------------------------------------


def bake_maps(obj, identifiers, size=1024, margin=8, smooth=1,
              high=None, options=None, occluders=()):
    """Bake the named maps for `obj`. Returns a MapResult.

    `occluders` are the other objects allowed to shadow AO -- `occluders_for`
    builds them. Nothing else in the scene takes part in any bake here: every
    other object is out of the render for the duration (`render_only`).

    `high` is the high-poly to read from, or None for the self-bake this
    module has always done. It is the caller's job to find it -- `paintset.py`
    does that, and keeping the lookup out of here is what lets a bake be run
    on a pair the collections know nothing about.

    Every map that comes out of here is filled edge to edge: the bake's own
    margin, and then `dilate.fill` for everything past it. No map leaves this
    function with black in it that the bake did not mean.
    """
    result = MapResult()
    options = options or BakeOptions()

    if obj is None or obj.type != 'MESH':
        result.error = RuntimeError("Select a mesh object")
        return result
    if not obj.data.uv_layers:
        result.error = RuntimeError(f"{obj.name} has no UV map")
        return result
    if high is not None and not obj.material_slots:
        result.error = RuntimeError(f"{obj.name} has no material to bake through")
        return result

    reach = bbox_diagonal(high if high is not None else obj) * THICKNESS_REACH

    for identifier in identifiers:
        entry = _BY_ID.get(identifier)
        if entry is None:
            result.skipped.append((identifier, "unknown map"))
            continue

        if needs_high(identifier) and high is None:
            result.skipped.append((identifier, "needs a high-poly"))
            continue

        if needs_external(identifier):
            # Cycles has no pass for it. Skipped with a reason rather than
            # produced as something else -- see the note on MAPS.
            result.skipped.append((identifier, "needs the external baker"))
            continue

        image = _ensure_image(
            map_name(obj, identifier), size, entry[2], float_buffer=entry[4]
        )

        participants = [obj, high]
        if identifier in OCCLUDED_MAPS:
            participants.extend(occluders)

        try:
            with render_only(participants):
                _bake_one(identifier, obj, image, margin, reach, smooth, high,
                          options)
        except Exception as error:  # noqa: BLE001
            result.error = error
            return result

        # Past the margin the bake left black with no alpha, and black is what
        # bleeds back across a seam the moment anything filters the texture.
        # Filling is not a finishing touch on these maps -- an AO or curvature
        # map with a dark line down every seam is not usable. See dilate.py.
        dilate.fill(image)

        storage.adopt_new_image(image)
        result.images[identifier] = image
        _store(obj, identifier, image)

    return result


def _bake_one(identifier, obj, image, margin, reach, smooth, high, options):
    """Dispatch one map to the function that bakes it. Raises on failure."""
    if identifier == 'AO':
        _bake_ao(obj, image, margin, high, options)
    elif identifier == 'POSITION':
        _bake_position(obj, image, margin, high, options)
    elif identifier == 'THICKNESS':
        _bake_thickness(obj, image, margin, reach, high, options)
    elif identifier == 'CURVATURE':
        _bake_curvature(obj, image, margin, smooth, high, options)
    elif identifier == 'NORMAL':
        _bake_normal(obj, image, margin, high, options)
    elif identifier == 'HEIGHT':
        _bake_height(obj, image, margin, high, options)


def _store(obj, identifier, image):
    maps = getattr(obj, "paintr2_maps", None)
    if maps is not None:
        setattr(maps, identifier.lower(), image)


def stored(obj, identifier):
    """The map already baked for `obj`, or None."""
    maps = getattr(obj, "paintr2_maps", None)
    return None if maps is None else getattr(maps, identifier.lower(), None)


class PR2_MeshMaps(bpy.types.PropertyGroup):
    """Where an object keeps the maps baked from it.

    Pointers, so the images are the .blend's to keep and `storage` writes them
    beside it like any other painted data.
    """

    ao: bpy.props.PointerProperty(name="Ambient Occlusion", type=bpy.types.Image)
    curvature: bpy.props.PointerProperty(name="Curvature", type=bpy.types.Image)
    thickness: bpy.props.PointerProperty(name="Thickness", type=bpy.types.Image)
    position: bpy.props.PointerProperty(name="Position", type=bpy.types.Image)
    normal: bpy.props.PointerProperty(name="Normal", type=bpy.types.Image)
    height: bpy.props.PointerProperty(name="Height", type=bpy.types.Image)
    bent_normal: bpy.props.PointerProperty(name="Bent Normal", type=bpy.types.Image)


CLASSES = (PR2_MeshMaps,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.paintr2_maps = bpy.props.PointerProperty(type=PR2_MeshMaps)


def unregister():
    if hasattr(bpy.types.Object, "paintr2_maps"):
        del bpy.types.Object.paintr2_maps
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

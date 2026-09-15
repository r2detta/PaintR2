# SPDX-License-Identifier: GPL-3.0-or-later
"""Filling UV regions of a layer image -- Substance Painter's polygon fill.

Three tools, one job. They differ only in *which faces* a click selects:

    Polygon Fill      the triangle or polygon under the pointer
    Mesh Fill         every face connected to it, or the whole object
    UV Island Fill    every face in the same UV island

After that the pipeline is shared: take the faces' UV triangles, rasterise
them into a coverage mask, and composite that mask into the active layer's
image. The image is the same one the brush paints into (`paint.active_paint_image`),
so a fill lands on a layer or on its mask by the same rule a stroke does.

Why our own rasteriser
----------------------

Blender can do this with face select masking plus its Fill brush, but that
writes a selection into the user's mesh, needs a mode change, and hands us
whatever antialiasing it feels like. Doing it here means the edge quality,
the seam bleed, the blend modes and the preview highlight are all ours -- and,
because the raster half is plain numpy, it is testable without a GPU context.
That matters: `gpu.texture.from_image` segfaults outside a draw handler (see
`window.py`), and a fill runs from an operator, not from a draw callback.

Coordinates
-----------

Blender's `Image.pixels` starts at the bottom-left row and UV `v = 0` is the
bottom edge, so a UV maps to a pixel with no flip anywhere:

    x = u * width        y = v * height

Colour spaces
-------------

A byte image with an sRGB colorspace stores *encoded* values -- linear 0.5
reads back as 0.737 through `pixels` (see `bake.py`). The fill colour comes
from the brush, which is scene-linear, so it is encoded to match the image it
is being written into. Mask images are Non-Color and take the value raw.
"""

import numpy as np


# --- raster ------------------------------------------------------------------
#
# No bpy below this line and none above it either, up to the `faces` section.
# The whole point is that a fill can be checked with three triangles and an
# array, without Blender deciding what "covered" means.


# Blend modes the fill offers. A subset of `Brush.blend`'s 24 -- the ones that
# mean something for a flat colour laid over a region. The identifiers match
# Blender's so the dropdown can show its labels.
BLEND_MODES = (
    'MIX',
    'MUL',
    'ADD',
    'SUB',
    'LIGHTEN',
    'DARKEN',
    'ERASE_ALPHA',
    'ADD_ALPHA',
)


def _sample_offsets(samples):
    """Sub-pixel sample positions for `samples` x `samples` supersampling.

    Antialiasing is not a luxury here. A UV triangle's edge never lands on the
    pixel grid, so a one-sample fill comes out with a staircase along every
    island border -- the single most obvious difference between this and a
    tool that looks finished.
    """
    samples = max(1, int(samples))
    step = 1.0 / samples
    return [
        ((i + 0.5) * step, (j + 0.5) * step)
        for j in range(samples)
        for i in range(samples)
    ]


def rasterize(triangles, width, height, samples=2, coverage=None,
              conservative=False):
    """Coverage of `triangles` over a `width` x `height` pixel grid.

    `triangles` is an (N, 3, 2) array of UV coordinates. Returns
    `(coverage, bbox)`: a float32 (height, width) array in 0..1, and the
    half-open pixel rectangle `(x0, y0, x1, y1)` that was touched. Everything
    outside the box is still zero, which is what lets the caller read back and
    write only a band of the image instead of all of it.

    Triangles are accumulated by *addition*, then clipped. Taking the maximum
    instead would leave a seam down every shared edge: two neighbouring
    triangles each cover half of the pixels along the edge between them, and
    max(0.5, 0.5) is 0.5 -- a visible line through the middle of a filled
    region. Adding gives the 1.0 that region deserves while an outer edge,
    covered once, keeps its 0.5.

    **`conservative` is what "no gap without bleed" means.** A UV island's
    border never lands on the pixel grid, so the texels along it are only
    partly inside the island. Sampled (`samples > 1`) they come out at 0.5 and
    the fill is half-strength along every seam; sampled once they come out at
    0 or 1 depending on which side of the texel *centre* the border fell, so
    the fill stops short of the island. Either way the renderer, filtering
    across that texel, shows what was underneath -- which is the gap, and
    which is why a bleed was needed to hide it.

    Conservative coverage answers the other question: does the triangle touch
    this texel *at all*. Every texel it touches is filled outright, so the
    island's whole footprint is covered and nothing is left half-done. It is
    not a bleed -- it never reaches a texel the island does not touch -- and
    it is exact for one texel out of the two the border falls between.

    The test is the edge function against the texel *square* instead of its
    centre. Each edge is linear in (x, y), so the largest value it takes over
    a unit square is its value at the centre plus half the sum of the absolute
    coefficients; the square overlaps the half-plane exactly when that is
    still on the inside. Three such half-planes bound a superset of the
    triangle -- exact along the edges, over-inclusive by at most one texel
    diagonally off a corner, which is the standard trade and invisible in a
    fill.
    """
    if coverage is None:
        coverage = np.zeros((height, width), dtype=np.float32)

    triangles = np.asarray(triangles, dtype=np.float64)
    if triangles.size == 0:
        return coverage, (0, 0, 0, 0)
    triangles = triangles.reshape(-1, 3, 2)

    # UVs outside 0..1 tile. A whole triangle sitting on tile (2, -1) is moved
    # back onto the first tile rather than being dropped; one straddling a tile
    # border still gets clipped, which is the same thing Blender's projective
    # painting does and not worth more than this note.
    triangles = triangles - np.floor(triangles.mean(axis=1))[:, None, :]

    scale = np.array((width, height), dtype=np.float64)
    triangles = triangles * scale

    # Conservative coverage is a yes/no question about a whole texel, so
    # sub-pixel samples have nothing to say about it: one probe at the centre,
    # with the edges widened to cover the square around it.
    offsets = [(0.5, 0.5)] if conservative else _sample_offsets(samples)
    weight = np.float32(1.0 / len(offsets))

    # And it reaches one texel further out than the triangle's own box, since
    # a texel whose centre is outside the triangle can still be touched by it.
    margin = 1 if conservative else 0

    bx0, by0 = width, height
    bx1, by1 = 0, 0

    for tri in triangles:
        (ax, ay), (bx, by), (cx, cy) = tri

        area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if area == 0.0:
            continue  # degenerate in UV space: zero-area island, nothing to fill

        x0 = max(0, int(np.floor(min(ax, bx, cx))) - margin)
        x1 = min(width, int(np.ceil(max(ax, bx, cx))) + 1 + margin)
        y0 = max(0, int(np.floor(min(ay, by, cy))) - margin)
        y1 = min(height, int(np.ceil(max(ay, by, cy))) + 1 + margin)
        if x1 <= x0 or y1 <= y0:
            continue

        xs = np.arange(x0, x1, dtype=np.float64)[None, :]
        ys = np.arange(y0, y1, dtype=np.float64)[:, None]

        # How far each edge is pushed out to make its test cover the texel
        # square rather than the point at its centre. Each edge function is
        # `A*x + B*y + C`, so over a unit square it moves by at most
        # (|A| + |B|) / 2 either way -- see the docstring.
        if conservative:
            pad0 = 0.5 * (abs(by - cy) + abs(cx - bx))
            pad1 = 0.5 * (abs(cy - ay) + abs(ax - cx))
            pad2 = 0.5 * (abs(ay - by) + abs(bx - ax))
        else:
            pad0 = pad1 = pad2 = 0.0

        acc = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
        for ox, oy in offsets:
            px = xs + ox
            py = ys + oy

            # Edge functions. Their signs agree with the triangle's winding,
            # so one comparison direction covers both windings.
            w0 = (bx - px) * (cy - py) - (cx - px) * (by - py)
            w1 = (cx - px) * (ay - py) - (ax - px) * (cy - py)
            w2 = (ax - px) * (by - py) - (bx - px) * (ay - py)

            if area > 0.0:
                inside = ((w0 + pad0 >= 0.0) & (w1 + pad1 >= 0.0)
                          & (w2 + pad2 >= 0.0))
            else:
                inside = ((w0 - pad0 <= 0.0) & (w1 - pad1 <= 0.0)
                          & (w2 - pad2 <= 0.0))

            acc += inside.astype(np.float32) * weight

        coverage[y0:y1, x0:x1] += acc

        bx0, by0 = min(bx0, x0), min(by0, y0)
        bx1, by1 = max(bx1, x1), max(by1, y1)

    if bx1 <= bx0 or by1 <= by0:
        return coverage, (0, 0, 0, 0)

    np.clip(coverage[by0:by1, bx0:bx1], 0.0, 1.0,
            out=coverage[by0:by1, bx0:bx1])
    return coverage, (bx0, by0, bx1, by1)


def dilate(coverage, radius, bbox):
    """Grow `coverage` outward by `radius` pixels. Returns the new bbox.

    This is the fill's version of `bake_margin`: a texture sampled with
    filtering or mipmaps reads pixels just outside the UV island, and if those
    are still the old colour a dark line appears along every seam. Bleeding
    the fill past the island edge is what stops that.

    Chebyshev growth -- a 3x3 maximum, `radius` times -- because it only ever
    raises a pixel to a value one of its neighbours already had. The interior
    is already 1.0, so nothing inside the island moves.
    """
    radius = int(radius)
    x0, y0, x1, y1 = bbox
    if radius <= 0 or x1 <= x0 or y1 <= y0:
        return bbox

    height, width = coverage.shape
    nx0 = max(0, x0 - radius)
    ny0 = max(0, y0 - radius)
    nx1 = min(width, x1 + radius)
    ny1 = min(height, y1 + radius)

    sub = coverage[ny0:ny1, nx0:nx1]
    rows, cols = sub.shape

    for _ in range(radius):
        padded = np.pad(sub, 1)
        sub = np.maximum.reduce([
            padded[a:a + rows, b:b + cols]
            for a in (0, 1, 2)
            for b in (0, 1, 2)
        ])

    coverage[ny0:ny1, nx0:nx1] = sub
    return (nx0, ny0, nx1, ny1)


def composite(pixels, coverage, color, strength=1.0, mode='MIX'):
    """Lay `color` over `pixels` wherever `coverage` says so, in place.

    `pixels` is an (rows, width, 4) float32 view of a band of the image and
    `coverage` the matching (rows, width) mask. `color` is RGB in whatever
    encoding the image stores (see `encode_color`).

    Only 'MIX' raises alpha, and it raises it to the coverage: painting an
    opaque stroke onto a transparent layer has to make that layer opaque, or
    the compiler's `factor = alpha x mask x opacity` multiplies the fill away
    to nothing. The colour-only modes leave alpha alone -- they are meant for
    tinting what is already there -- and the two alpha modes leave colour
    alone.
    """
    factor = (coverage * float(strength)).astype(np.float32)[:, :, None]
    if not factor.any():
        return pixels

    rgb = pixels[:, :, :3]
    alpha = pixels[:, :, 3:]
    src = np.asarray(color, dtype=np.float32)[:3][None, None, :]

    if mode == 'ERASE_ALPHA':
        np.clip(alpha - factor, 0.0, 1.0, out=alpha)
        return pixels
    if mode == 'ADD_ALPHA':
        np.clip(alpha + factor, 0.0, 1.0, out=alpha)
        return pixels

    if mode == 'MIX':
        target = np.broadcast_to(src, rgb.shape)
    elif mode == 'MUL':
        target = rgb * src
    elif mode == 'ADD':
        target = np.clip(rgb + src, 0.0, 1.0)
    elif mode == 'SUB':
        target = np.clip(rgb - src, 0.0, 1.0)
    elif mode == 'LIGHTEN':
        target = np.maximum(rgb, src)
    elif mode == 'DARKEN':
        target = np.minimum(rgb, src)
    else:
        target = np.broadcast_to(src, rgb.shape)

    rgb *= (1.0 - factor)
    rgb += target * factor

    if mode == 'MIX':
        alpha += (1.0 - alpha) * factor

    return pixels


# --- faces -------------------------------------------------------------------
#
# Everything from here down needs a mesh. The three tools reduce to one
# question -- which face indices does this click select -- and the answer for
# two of them is a connectivity label computed once per mesh and cached.

import bpy


# What a click selects.
SCOPES = ('TRIANGLE', 'FACE', 'PART', 'OBJECT', 'ISLAND')


def _face_loop_layout(mesh):
    """(loop_start, loop_total) per face, as int arrays."""
    count = len(mesh.polygons)
    starts = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)

    totals = np.empty(count, dtype=np.int32)
    try:
        mesh.polygons.foreach_get("loop_total", totals)
    except (RuntimeError, TypeError):
        # `loop_total` is derivable: faces own consecutive runs of loops.
        totals[:-1] = starts[1:] - starts[:-1]
        totals[-1] = len(mesh.loops) - starts[-1]

    return starts, totals


def _uv_array(mesh, uv_layer=None):
    """(loops, 2) float32 of the active UV map, or None when there is none."""
    layer = uv_layer or mesh.uv_layers.active
    if layer is None:
        return None

    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uvs)
    return uvs.reshape(-1, 2)


class _Topology:
    """Face connectivity of one mesh: mesh parts and UV islands.

    Both are the same computation over different edges. Two faces belong to
    the same *part* when they share a mesh edge; they belong to the same *UV
    island* when they share a mesh edge **and** agree on that edge's UVs --
    which is what a seam is, stated the other way round.

    Built once and cached, because a click must not pay for a walk over a
    hundred thousand faces.
    """

    def __init__(self, mesh, uv_layer=None):
        self.face_count = len(mesh.polygons)
        self.part_of = np.zeros(self.face_count, dtype=np.int32)
        self.island_of = np.zeros(self.face_count, dtype=np.int32)

        if self.face_count == 0:
            return

        starts, totals = _face_loop_layout(mesh)
        loop_count = len(mesh.loops)

        face_of_loop = np.repeat(
            np.arange(self.face_count, dtype=np.int32), totals
        )
        local = np.arange(loop_count, dtype=np.int32) - starts[face_of_loop]
        next_loop = starts[face_of_loop] + (local + 1) % totals[face_of_loop]

        edge_of_loop = np.empty(loop_count, dtype=np.int32)
        mesh.loops.foreach_get("edge_index", edge_of_loop)

        # Loops that name the same edge sit next to each other once sorted;
        # consecutive pairs inside a run are enough to connect the run.
        order = np.argsort(edge_of_loop, kind="stable")
        shared = edge_of_loop[order][1:] == edge_of_loop[order][:-1]
        left = order[:-1][shared]
        right = order[1:][shared]

        self.part_of = _label(self.face_count, face_of_loop[left],
                              face_of_loop[right])

        uvs = _uv_array(mesh, uv_layer)
        if uvs is None:
            self.island_of = self.part_of
            return

        # A UV vertex is a (mesh vertex, UV) pair. Quantising the UV keeps
        # coordinates that only differ by float noise together; 1/65536 is far
        # finer than any texel an island edge can land on.
        verts = np.empty(loop_count, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", verts)
        quantised = np.round(np.asarray(uvs, dtype=np.float64) * 65536.0)
        key = np.stack(
            (verts.astype(np.int64), quantised[:, 0].astype(np.int64),
             quantised[:, 1].astype(np.int64)),
            axis=1,
        )
        _, uv_id = np.unique(key, axis=0, return_inverse=True)
        uv_id = uv_id.reshape(-1)

        a1, a2 = uv_id[left], uv_id[next_loop[left]]
        b1, b2 = uv_id[right], uv_id[next_loop[right]]
        joined = ((a1 == b1) & (a2 == b2)) | ((a1 == b2) & (a2 == b1))

        self.island_of = _label(
            self.face_count,
            face_of_loop[left][joined],
            face_of_loop[right][joined],
        )


def _label(count, left, right):
    """Connected-component labels for `count` nodes joined by (left, right)."""
    parent = np.arange(count, dtype=np.int32)

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression, iterative
            parent[node], node = root, parent[node]
        return root

    for a, b in zip(left.tolist(), right.tolist()):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    return np.array([find(i) for i in range(count)], dtype=np.int32)


# One mesh at a time is enough: a click is always about the mesh under the
# pointer, and the entry is rebuilt the moment its shape changes.
_topology_cache = {}
_TOPOLOGY_CACHE_SIZE = 4


def _topology_key(mesh):
    """What makes a cached topology still valid.

    Counts alone are not enough: Smart UV Project re-lays every island without
    changing a single count, and a cache keyed on counts would go on filling
    the islands the mesh used to have. So the UV coordinates go into the key
    too -- hashing them is a few hundred microseconds against the union-find
    rebuild they guard, and it is paid per click rather than per frame.

    Doing it here rather than from a `depsgraph_update_post` handler is
    deliberate. That handler ran on every update Blender makes, brush strokes
    included, and putting a hook of ours in the middle of somebody else's
    stroke is a cost and a risk with nothing to show for it.
    """
    layer = mesh.uv_layers.active

    uvs = _uv_array(mesh, layer)
    uv_hash = 0 if uvs is None else hash(uvs.tobytes())

    return (
        mesh.name,
        len(mesh.polygons),
        len(mesh.loops),
        len(mesh.edges),
        layer.name if layer is not None else "",
        uv_hash,
    )


def topology(mesh):
    key = _topology_key(mesh)
    found = _topology_cache.get(key)
    if found is None:
        found = _Topology(mesh)
        if len(_topology_cache) >= _TOPOLOGY_CACHE_SIZE:
            _topology_cache.clear()
        _topology_cache[key] = found
    return found


def invalidate_topology():
    """Drop the cache. Called when the mesh under the pointer may have moved."""
    _topology_cache.clear()


def face_set(mesh, face_index, scope):
    """The face indices a click on `face_index` selects, as an int array.

    'TRIANGLE' is handled by the caller, which knows the hit point and so can
    say which half of a quad was struck; here it behaves like 'FACE'.
    """
    count = len(mesh.polygons)
    if not 0 <= face_index < count:
        return np.empty(0, dtype=np.int32)

    if scope in ('FACE', 'TRIANGLE'):
        return np.array([face_index], dtype=np.int32)
    if scope == 'OBJECT':
        return np.arange(count, dtype=np.int32)

    labels = topology(mesh)
    source = labels.island_of if scope == 'ISLAND' else labels.part_of
    return np.nonzero(source == source[face_index])[0].astype(np.int32)


# How far from edge-on a face has to be to count as facing the viewer.
EDGE_ON = 1e-4


def project_points(points, matrix, width, height):
    """World points -> region pixels. Returns (xy, visible).

    `matrix` is `rv3d.perspective_matrix`, so this is the same transform the
    viewport draws with. `visible` is False for anything behind the eye: those
    come back through the perspective divide as a mirrored point in front of
    it, and a lasso would happily select the far side of the model.
    """
    if len(points) == 0:
        return np.zeros((0, 2), dtype=np.float64), np.zeros(0, dtype=bool)

    matrix = np.array(matrix, dtype=np.float64)
    homogeneous = np.empty((len(points), 4), dtype=np.float64)
    homogeneous[:, :3] = points
    homogeneous[:, 3] = 1.0

    clip = homogeneous @ matrix.T
    w = clip[:, 3]
    visible = w > 1e-6

    safe = np.where(visible, w, 1.0)
    ndc = clip[:, :2] / safe[:, None]

    xy = np.empty((len(points), 2), dtype=np.float64)
    xy[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * width
    xy[:, 1] = (ndc[:, 1] * 0.5 + 0.5) * height
    return xy, visible


def _world_points(obj, local):
    """`local` (N, 3) taken into world space by the object's matrix."""
    matrix = np.array(obj.matrix_world)
    out = local @ matrix[:3, :3].T
    out += matrix[:3, 3]
    return out


def face_centres(mesh):
    centres = np.empty(len(mesh.polygons) * 3, dtype=np.float32)
    mesh.polygons.foreach_get("center", centres)
    return centres.reshape(-1, 3).astype(np.float64)


def inside_polygon(points, shape):
    """Which of `points` fall inside the closed polygon `shape`.

    Even-odd ray casting, done for every point at once -- a lasso over a
    hundred thousand faces is a loop that has to not be a Python loop.
    """
    if len(points) == 0 or len(shape) < 3:
        return np.zeros(len(points), dtype=bool)

    shape = np.asarray(shape, dtype=np.float64)
    x, y = points[:, 0], points[:, 1]
    inside = np.zeros(len(points), dtype=bool)

    x0, y0 = shape[:, 0], shape[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)

    for ax, ay, bx, by in zip(x0, y0, x1, y1):
        if ay == by:
            continue
        crosses = (ay > y) != (by > y)
        # Where the edge crosses this row, in x. Only the ones to the right
        # of the point count.
        with np.errstate(invalid='ignore', divide='ignore'):
            at = ax + (y - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (x < at)

    return inside


def faces_in_shape(obj, shape, matrix, width, height, front_only=False,
                   view_direction=None):
    """Every face of `obj` the screen-space polygon `shape` takes.

    A face counts when its centre is inside, or when any of its corners is:
    the centre alone loses a face bigger than the rectangle, and any-corner
    alone takes faces that merely lean into it. Together they read the way
    Blender's own box select does.

    Nothing here tests occlusion. `front_only` is the switch that decides
    whether the far side of the model comes along, and it is the same switch
    a click obeys -- one rule for both gestures. It needs a `view_direction`
    to mean anything; the caller has the region and reads it from there rather
    than having it guessed out of the projection matrix.
    """
    mesh = obj.data
    count = len(mesh.polygons)
    if count == 0 or len(shape) < 3:
        return np.zeros(0, dtype=np.int64)

    centres, visible = project_points(
        _world_points(obj, face_centres(mesh)), matrix, width, height
    )
    taken = visible & inside_polygon(centres, shape)

    # The corners, for the faces the centre test did not take. Vertices are
    # projected once and read through the loop table, so a mesh is walked
    # twice rather than once per face.
    coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coords)
    corner_xy, corner_visible = project_points(
        _world_points(obj, coords.reshape(-1, 3).astype(np.float64)),
        matrix, width, height,
    )
    corner_in = corner_visible & inside_polygon(corner_xy, shape)

    if corner_in.any():
        loop_vertices = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        starts, _totals = _face_loop_layout(mesh)

        hit_loops = corner_in[loop_vertices]

        # Any corner in -> the face is in. `np.add.reduceat` sums each face's
        # run of loops in one pass.
        if len(starts):
            sums = np.add.reduceat(hit_loops.astype(np.int32), starts)
            taken |= sums > 0

    faces = np.nonzero(taken)[0]
    if front_only and view_direction is not None and len(faces):
        faces = front_facing(obj, faces, view_direction)

    return faces


def front_facing(obj, faces, view_direction):
    """`faces` reduced to the ones turned towards the viewer.

    `view_direction` points from the eye into the scene, in world space. A
    face is kept when its normal opposes it -- the same test Blender's own
    backface culling makes.

    Object space would be cheaper but wrong: the object may be rotated,
    mirrored or scaled negatively, and a normal read straight off the mesh
    then points the other way from the one on screen.
    """
    if faces is None or len(faces) == 0:
        return faces

    mesh = obj.data
    normals = np.empty(len(mesh.polygons) * 3, dtype=np.float32)
    mesh.polygons.foreach_get("normal", normals)
    normals = normals.reshape(-1, 3)

    # The normal matrix: the inverse transpose, so a non-uniform scale does
    # not tilt the normals.
    matrix = np.array(obj.matrix_world.to_3x3().inverted().transposed())
    world = normals[faces] @ matrix.T

    view = np.array(view_direction, dtype=np.float32)
    length = np.linalg.norm(view)
    if length == 0.0:
        return faces
    view = view / length

    # Not `< 0`: a face exactly edge-on comes out at -1e-8 as often as at
    # +1e-8, and a cube turned ninety degrees then keeps two faces instead of
    # one. An edge-on face shows no area anyway, so it is not kept.
    return faces[(world @ view) < -EDGE_ON]


def uv_triangles(mesh, faces=None, uv_layer=None, triangles=None):
    """(N, 3, 2) UV triangles, ready to rasterise.

    Give it `faces` (polygon indices) or `triangles` (indices into
    `mesh.loop_triangles`), or neither for the whole mesh -- the same contract
    as `face_triangles_3d`, in UV space instead of object space.

    Blender's tessellation is reused rather than a fan of our own: an n-gon
    with a concave outline fans into triangles that stick out of it, and a
    fill that bleeds outside the polygon it was aimed at is worse than no
    fill at all.

    It takes a whole set at once because a mirrored click is up to eight of
    them and the mesh's UV and corner arrays are read on every call -- and
    because rasterising the union once is the only way a fill at half alpha
    stays even where two mirrored sets overlap.
    """
    uvs = _uv_array(mesh, uv_layer)
    if uvs is None:
        return np.empty((0, 3, 2), dtype=np.float32)

    try:
        mesh.calc_loop_triangles()
    except (AttributeError, RuntimeError):
        pass  # 4.1+ keeps the cache up to date on its own

    count = len(mesh.loop_triangles)
    if count == 0:
        return np.empty((0, 3, 2), dtype=np.float32)

    loops = np.empty(count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", loops)
    loops = loops.reshape(-1, 3)

    if triangles is not None:
        wanted = [index for index in triangles if 0 <= index < count]
        if not wanted:
            return np.empty((0, 3, 2), dtype=np.float32)
        loops = loops[np.asarray(wanted, dtype=np.int32)]
    elif faces is not None:
        polygons = np.empty(count, dtype=np.int32)
        mesh.loop_triangles.foreach_get("polygon_index", polygons)
        wanted = np.zeros(len(mesh.polygons), dtype=bool)
        wanted[np.asarray(faces, dtype=np.int32)] = True
        loops = loops[wanted[polygons]]

    return uvs[loops]


def triangle_index_at(mesh, face_index, point):
    """Index into `mesh.loop_triangles` of the triangle `point` lands in.

    Falls back to the face's first triangle when the point cannot be placed --
    a near-miss on a silhouette should still fill something rather than
    nothing. None only when the face has no triangles at all.
    """
    from mathutils.geometry import intersect_point_tri

    candidates = [
        index for index, tri in enumerate(mesh.loop_triangles)
        if tri.polygon_index == face_index
    ]
    if not candidates:
        return None
    if point is None:
        return candidates[0]

    vertices = mesh.vertices
    for index in candidates:
        tri = mesh.loop_triangles[index]
        a, b, c = (vertices[i].co for i in tri.vertices)
        if intersect_point_tri(point, a, b, c) is not None:
            return index

    return candidates[0]


# --- mirroring ---------------------------------------------------------------
#
# The header's `X Y Z` write the mesh's own `use_mesh_mirror_*`, which is what
# Blender's brush obeys: reflections through the object's origin, along its
# local axes. A fill has to obey the same flags or the two tools disagree about
# what "mirror" means in the same window.
#
# The reflection is done on the *click*, not on the coverage. Mirroring the
# coverage in UV space would be free, but it is only correct on models whose
# UVs are themselves mirrored -- and silently wrong on every other one.
# Reflecting the point and asking the mesh what is there costs one nearest-
# point query per plane and is right either way.

MIRROR_AXES = ('X', 'Y', 'Z')

# How far a reflected point may land from real geometry and still count as the
# same place, as a fraction of the object's bounding box. Zero would only work
# on a perfectly symmetrical mesh; too generous and an asymmetrical model
# starts filling whatever happens to be nearest.
MIRROR_TOLERANCE = 0.01


def mirror_points(point, axes):
    """Every reflection of `point` across the enabled planes.

    One axis gives one extra point, two give three, three give seven -- the
    planes compose, the same way Blender's do.
    """
    indices = [index for index, axis in enumerate(MIRROR_AXES) if axis in axes]
    if not indices:
        return []

    out = []
    for mask in range(1, 1 << len(indices)):
        mirrored = list(point)
        for bit, index in enumerate(indices):
            if mask & (1 << bit):
                mirrored[index] = -mirrored[index]
        out.append(mirrored)
    return out


def mirror_tolerance(obj):
    """`MIRROR_TOLERANCE` in the object's own units."""
    corners = [tuple(corner) for corner in obj.bound_box]
    if len(corners) < 7:
        return 1e-4
    diagonal = sum(
        (corners[6][axis] - corners[0][axis]) ** 2 for axis in range(3)
    ) ** 0.5
    return max(1e-5, diagonal * MIRROR_TOLERANCE)


def face_at(obj, point, tolerance):
    """The polygon index nearest `point`, or None when nothing is near enough."""
    try:
        found, _location, _normal, index = obj.closest_point_on_mesh(
            point, distance=tolerance
        )
    except (RuntimeError, TypeError, ValueError):
        return None

    if not found or index < 0:
        return None
    return index


def click_targets(obj, face_index, point, axes=()):
    """[(face index, point)] a click reaches: the face itself, then its mirrors.

    Shared by the stroke and the hover preview on purpose. A highlight that
    showed one face while the click filled three would be worse than no
    highlight at all.
    """
    targets = [(face_index, point)]
    if not axes or point is None:
        return targets

    tolerance = mirror_tolerance(obj)
    seen = {face_index}
    for mirrored in mirror_points(point, axes):
        index = face_at(obj, mirrored, tolerance)
        if index is None or index in seen:
            continue
        seen.add(index)
        targets.append((index, mirrored))

    return targets


# --- the hover preview -------------------------------------------------------
#
# The same face set, in object space rather than UV space. It is drawn over the
# model so that "which faces would this click take" is answered before the
# click rather than after it -- which for Mesh Fill and UV Island Fill is the
# difference between a tool you can aim and one you have to try.


def face_triangles_3d(mesh, faces=None, triangles=None):
    """(N, 3, 3) object-space triangles, ready for a GPU batch.

    Give it `faces` (polygon indices) or `triangles` (indices into
    `mesh.loop_triangles`), or neither for the whole mesh. Deliberately takes
    a whole set rather than one at a time: the mesh's coordinate and corner
    arrays are read on every call, and with mirroring on, one hover resolves
    to as many as eight face sets.

    Vectorised through numpy rather than walked in Python: a mesh part can be
    a hundred thousand faces, and this runs whenever the pointer crosses into
    a new one.
    """
    vertex_count = len(mesh.vertices)
    if vertex_count == 0:
        return np.empty((0, 3, 3), dtype=np.float32)

    coordinates = np.empty(vertex_count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coordinates)
    coordinates = coordinates.reshape(-1, 3)

    try:
        mesh.calc_loop_triangles()
    except (AttributeError, RuntimeError):
        pass

    count = len(mesh.loop_triangles)
    if count == 0:
        return np.empty((0, 3, 3), dtype=np.float32)

    corners = np.empty(count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", corners)
    corners = corners.reshape(-1, 3)

    if triangles is not None:
        wanted = [index for index in triangles if 0 <= index < count]
        if not wanted:
            return np.empty((0, 3, 3), dtype=np.float32)
        corners = corners[np.asarray(wanted, dtype=np.int32)]
    elif faces is not None:
        polygons = np.empty(count, dtype=np.int32)
        mesh.loop_triangles.foreach_get("polygon_index", polygons)
        wanted = np.zeros(len(mesh.polygons), dtype=bool)
        wanted[np.asarray(faces, dtype=np.int32)] = True
        corners = corners[wanted[polygons]]

    return coordinates[corners]


# --- applying to an image ----------------------------------------------------


def encode_color(image, color):
    """`color`, scene-linear, in whatever encoding `image` stores.

    A byte image with an sRGB colorspace holds *encoded* values: writing a
    linear 0.5 straight into `pixels` produces a texture that reads back as
    0.5 and therefore looks far too dark. Float buffers and Non-Color images
    -- masks -- take the value as it is.
    """
    color = tuple(color)[:3]

    encode = not getattr(image, "is_float", False)
    if encode:
        name = getattr(image.colorspace_settings, "name", "")
        encode = name in ("sRGB", "Filmic sRGB")

    if not encode:
        return color

    return tuple(
        c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055
        for c in color
    )


class Target:
    """One image being filled, plus the buffer that mirrors it.

    The buffer is held for the length of a stroke rather than per click.
    `foreach_get` on a 2K image moves 67 MB, and paying that on both sides of
    every mouse-move during a drag fill is the difference between a tool that
    keeps up and one that stutters. Reading once and writing per step halves
    it; nothing smaller is available, because `pixels` has no partial upload.
    """

    def __init__(self, image):
        self.image = image
        self.width, self.height = image.size
        self.pixels = None

        # What this stroke has overwritten so far, for undo: the union of
        # every rectangle it has touched, holding the pixels as they were
        # before it started. Grown a rectangle at a time rather than copied
        # whole -- a polygon fill on a 4K texture touches a few percent of it,
        # and a full copy per stroke would cost 268 MB to record that.
        self.undo_bbox = None
        self.undo_pixels = None

    @property
    def valid(self):
        return self.width > 0 and self.height > 0

    def read(self):
        buffer = np.empty(self.width * self.height * 4, dtype=np.float32)
        self.image.pixels.foreach_get(buffer)
        self.pixels = buffer.reshape(self.height, self.width, 4)
        return self.pixels

    def write(self):
        if self.pixels is None:
            return
        self.image.pixels.foreach_set(self.pixels.reshape(-1))
        self.image.update()

    def snapshot(self, bbox):
        """Record `bbox` as it is now, if it is not recorded already.

        Correct because of the order it runs in: it is called *before* the
        composite, so anything inside the new rectangle and outside the old
        one still holds its original pixels. Anything inside both is already
        recorded, and the recorded copy wins.
        """
        if self.pixels is None:
            return

        x0, y0, x1, y1 = (int(v) for v in bbox)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.width, x1), min(self.height, y1)
        if x1 <= x0 or y1 <= y0:
            return

        if self.undo_bbox is None:
            self.undo_bbox = (x0, y0, x1, y1)
            self.undo_pixels = np.array(self.pixels[y0:y1, x0:x1], copy=True)
            return

        ox0, oy0, ox1, oy1 = self.undo_bbox
        if x0 >= ox0 and y0 >= oy0 and x1 <= ox1 and y1 <= oy1:
            return

        ux0, uy0 = min(ox0, x0), min(oy0, y0)
        ux1, uy1 = max(ox1, x1), max(oy1, y1)

        grown = np.array(self.pixels[uy0:uy1, ux0:ux1], copy=True)
        grown[oy0 - uy0:oy1 - uy0, ox0 - ux0:ox1 - ux0] = self.undo_pixels

        self.undo_bbox = (ux0, uy0, ux1, uy1)
        self.undo_pixels = grown

    def commit_undo(self):
        """Hand this stroke's snapshot to the history ring, and forget it."""
        from . import history

        if self.undo_pixels is not None:
            history.push(self.image, self.undo_bbox, self.undo_pixels)

        self.undo_bbox = None
        self.undo_pixels = None


def fill(target, triangles, color, strength=1.0, mode='MIX', samples=2,
         bleed=0, conservative=False):
    """Rasterise `triangles` into `target` and composite the colour in.

    Returns True when anything was touched. The caller decides when to push
    the result back to Blender -- a drag fill writes once per step, a click
    writes once.

    `conservative` fills every texel the triangles touch, instead of the
    fraction of each one they cover -- the difference between a fill that
    needs a bleed to hide its seams and one that does not. See `rasterize`.
    """
    if not target.valid or len(triangles) == 0:
        return False

    if target.pixels is None:
        target.read()

    coverage, bbox = rasterize(
        triangles, target.width, target.height, samples=samples,
        conservative=conservative,
    )
    if bbox[2] <= bbox[0]:
        return False

    bbox = dilate(coverage, bleed, bbox)

    # Before the composite, never after: the snapshot is what was there.
    target.snapshot(bbox)

    _x0, y0, _x1, y1 = bbox
    composite(
        target.pixels[y0:y1],
        coverage[y0:y1],
        encode_color(target.image, color),
        strength=strength,
        mode=mode,
    )
    return True

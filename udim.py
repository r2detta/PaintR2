# SPDX-License-Identifier: GPL-3.0-or-later
"""Reaching the pixels of any UDIM tile.

`image.pixels` and `image.size` only ever see the first tile of a TILED image.
Measured on 5.2.3: `tiles.active_index` changes nothing, and renumbering a tile
does not reorder the list. There is no other Python route to the rest except
through the disk, which costs about 1.3 s per 4K tile against 0.08 s here.

What decides "first" is one pointer: `Image.tiles` is a `ListBase`, a
`{first, last}` pair inside the `Image` struct. `on_tile` points `first` at the
tile asked for, lets the caller use `image.pixels`, and puts it back. Nothing
else is touched: not the tile, not the links between tiles, not the cached
buffers.

No offset is written down. The pair is found by value, looking for the two
pointers Blender itself reports through `as_pointer()`, so a struct that grows
in a later release does not make this write to the wrong place. If the pair is
not there, or the image does not answer with the size of the tile asked for,
`TileAccessError` is raised and nothing has been written.

Rules for what goes inside the block, all of them because the list really is
wrong while it runs:

* Main thread only, and `image.pixels` / `image.size` only. No operators, no
  redraw, no saving: a `.blend` written inside would store the wrong order.
* Not while a bake or a render job runs. Other threads read this list.
* Keep it short. Copy the pixels out, leave the block, then do the work.

The pointer is looked up on every entry rather than remembered: undo rebuilds
datablocks, so an address from before a stroke may be gone after it.

The rest of the module answers the question before that one: which tiles a
material needs. A material is UDIM or it is not, decided once by "Set Models"; this only reads UVs, and reading them never switches anything on.
"""

import contextlib
import ctypes

import bpy
import numpy as np

# Far past the end of any `Image` this add-on targets (the list sits at byte
# 1664 on 5.2.3). The search stops at the match, so this only bounds a failure.
_SEARCH_BYTES = 8192
_POINTER_BYTES = ctypes.sizeof(ctypes.c_void_p)


# Blender's own limit: tile 2000 is u 9, v 99.
_ROWS = 100
_COLUMNS = 10

# Projection painting compares UVs modulo 1, so two faces this close once the
# tile is taken away are not a seam to it and get no bleed. Measured: 0.0001
# apart is still not a seam, 0.002 apart is.
_UNSEAMED_REACH = 0.001

# How far past its tile's border a face corner may sit before the face counts
# as crossing it: two texels of a 4K tile. A corner placed on the border by
# snapping lands within float error of it, not past it.
_BORDER_SLACK = 0.0005

# The bake margin a UDIM target needs. Blender's ADJACENT_FACES continues an
# island with the face across its seam, and when that face is on another tile
# it reads the wrong one: measured on a sphere cut across two tiles, Cycles AO
# came back with 487 margin texels near 0.5 along the cut, where the same model
# on one plain image had 8. EXTEND brought the UDIM bake back to the plain one.
BAKE_MARGIN_TYPE = 'EXTEND'

# How far a stroke should bleed on a UDIM image. Tile textures reach further
# into a seam as they get smaller on screen than a plain image does on the same
# pixels. Measured: lines at 2, closed at 8.
SEAM_BLEED = 8


class TileAccessError(RuntimeError):
    """The tile list could not be reached safely. Nothing was written."""


def is_tiled(image):
    return image is not None and image.source == 'TILED'


def tiles_of(image):
    """Tile numbers to loop over: `[None]` for a plain image, so one loop fits both.

    `on_tile(image, None)` does nothing, which is what makes that work.
    """
    if not is_tiled(image):
        return [None]
    return [tile.number for tile in image.tiles]


def _first_pointer(image):
    """The `ListBase.first` of `image.tiles`, as a writable ctypes cell."""
    tiles = image.tiles
    first = tiles[0].as_pointer()
    last = tiles[len(tiles) - 1].as_pointer()
    base = image.as_pointer()

    for offset in range(0, _SEARCH_BYTES, _POINTER_BYTES):
        cell = ctypes.c_size_t.from_address(base + offset)
        if cell.value != first:
            continue
        if ctypes.c_size_t.from_address(base + offset + _POINTER_BYTES).value == last:
            return cell

    raise TileAccessError(f"no tile list found in image '{image.name}'")


@contextlib.contextmanager
def on_tile(image, number):
    """`image.pixels` and `image.size` address tile `number` inside the block.

    Plain images, `number=None` and an image with one tile pass straight
    through. A tile that does not exist or has no pixels yet raises
    `TileAccessError`.
    """
    if number is None or not is_tiled(image):
        yield image
        return

    tile = next((t for t in image.tiles if t.number == number), None)
    if tile is None:
        raise TileAccessError(f"image '{image.name}' has no tile {number}")
    if tile.size[0] == 0:
        raise TileAccessError(f"tile {number} of '{image.name}' has no pixels yet")

    if len(image.tiles) == 1:
        yield image
        return

    cell = _first_pointer(image)
    previous = cell.value
    cell.value = tile.as_pointer()
    try:
        if tuple(image.size) != tuple(tile.size):
            raise TileAccessError(
                f"image '{image.name}' did not move to tile {number}"
            )
        yield image
    finally:
        cell.value = previous


# --- which tiles --------------------------------------------------------------


def tile_numbers(columns, rows):
    """Tile numbers for arrays of tile columns and rows; 0 off the grid."""
    valid = (columns >= 0) & (columns < _COLUMNS) & (rows >= 0) & (rows < _ROWS)
    return np.where(valid, 1001 + columns + _COLUMNS * rows, 0)


def read(image, number=None):
    """Tile `number`'s pixels as a (height, width, 4) float32 array.

    `number=None` reads a plain image whole. The copy is made inside the block
    and the work is the caller's, outside it.
    """
    width, height = tile_size(image, number)
    pixels = np.empty(width * height * 4, dtype=np.float32)
    with on_tile(image, number) as target:
        target.pixels.foreach_get(pixels)
    return pixels.reshape(height, width, 4)


def write(image, number, pixels):
    """Put `pixels` into tile `number` (or a plain image) and tell Blender."""
    with on_tile(image, number) as target:
        target.pixels.foreach_set(np.ascontiguousarray(pixels, np.float32).reshape(-1))
        target.update()


def tile_size(image, number):
    """(width, height) of tile `number`, or None if `image` does not have it.

    `number=None` is the image itself, which is what a plain image answers to.
    """
    if number is None or not is_tiled(image):
        return tuple(image.size)
    tile = next((t for t in image.tiles if t.number == number), None)
    return tuple(tile.size) if tile is not None else None


def _face_tiles(mesh):
    """Tile number of each face of `mesh`, 0 where its UV has no valid tile.

    Per face and by the mean of its corners, so a corner sitting exactly on a
    tile border does not put the face in the next tile.
    """
    count = len(mesh.polygons)
    layer = mesh.uv_layers.active
    if layer is None or not count:
        return np.full(count, 1001, dtype=np.int32)

    starts = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    sizes = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", sizes)
    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uvs)
    uvs = uvs.reshape(-1, 2)

    u = np.floor(np.add.reduceat(uvs[:, 0], starts) / sizes).astype(np.int32)
    v = np.floor(np.add.reduceat(uvs[:, 1], starts) / sizes).astype(np.int32)

    return tile_numbers(u, v)


def object_tiles(obj, slot_index=None):
    """(tiles, invalid faces) of `obj`, or of the faces of one material slot."""
    faces = _face_tiles(obj.data)
    if slot_index is not None and len(faces):
        indices = np.empty(len(faces), dtype=np.int32)
        obj.data.polygons.foreach_get("material_index", indices)
        faces = faces[indices == slot_index]

    return [int(number) for number in np.unique(faces[faces > 0])], \
        int((faces == 0).sum())


def material_tiles(material):
    """(tiles, invalid faces) of every object using `material`, at least `[1001]`.

    Walks objects rather than meshes: a slot linked to the object is not in
    `mesh.materials`.
    """
    tiles = set()
    invalid = 0

    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for index, slot in enumerate(obj.material_slots):
            if slot.material == material:
                found, bad = object_tiles(obj, index)
                tiles.update(found)
                invalid += bad

    return sorted(tiles) or [1001], invalid


def tiles_for(material):
    """The tiles an image made for `material` should have, or None if plain."""
    document = getattr(material, "paintr2", None)
    if document is None or not document.is_udim:
        return None
    return material_tiles(material)[0]


def same_tiles(image, tiles, size, float_buffer):
    """Whether `image` is already the UDIM image `tiles` at `size` would be."""
    if not is_tiled(image) or image.is_float != bool(float_buffer):
        return False
    have = {t.number: tuple(t.size) for t in image.tiles}
    return have == {number: (size, size) for number in tiles}


def crossing_faces(mesh):
    """How many faces reach past the border of the tile they are on.

    A face is on one tile, by the middle of its UVs (`_face_tiles`), and the
    tools that work a tile at a time follow that: a fill, and the external
    baker, stop at the border, and the part of the face beyond it stays as it
    was. So a UV island that runs over a border is reported at "Set Models";
    keeping every island inside one tile is the rule UDIM tools ask for.
    Faces off the grid altogether are `object_tiles`' to report, not this.
    """
    count = len(mesh.polygons)
    layer = mesh.uv_layers.active
    if layer is None or not count:
        return 0

    starts = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    sizes = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", sizes)
    uvs = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uvs)
    uvs = uvs.reshape(-1, 2)

    column = np.floor(np.add.reduceat(uvs[:, 0], starts) / sizes)
    row = np.floor(np.add.reduceat(uvs[:, 1], starts) / sizes)
    on_grid = tile_numbers(column.astype(np.int32), row.astype(np.int32)) > 0

    origin = np.repeat(np.stack([column, row], axis=1), sizes, axis=0)
    local = uvs - origin
    outside = ((local < -_BORDER_SLACK) | (local > 1.0 + _BORDER_SLACK)).any(axis=1)
    reaching = np.add.reduceat(outside.astype(np.int32), starts) > 0
    return int((reaching & on_grid).sum())


def unseamed_edges(mesh):
    """How many edges cross a tile border with the same UV on both sides.

    The stroke does not bleed across these, which leaves a thin line of whatever
    is underneath. It is what moving part of a connected unwrap by a whole tile
    produces, and only pulling the shells a little apart in the UV editor cures
    it: nothing here can bleed for the stroke.
    """
    layer = mesh.uv_layers.active
    count = len(mesh.polygons)
    if layer is None or count < 2:
        return 0

    starts = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    sizes = np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", sizes)

    loops = len(mesh.loops)
    vertex = np.empty(loops, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", vertex)
    edge = np.empty(loops, dtype=np.int32)
    mesh.loops.foreach_get("edge_index", edge)
    uvs = np.empty(loops * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uvs)
    uvs = uvs.reshape(-1, 2)

    tile_of_loop = np.repeat(_face_tiles(mesh), sizes)
    following = np.arange(1, loops + 1, dtype=np.int32)
    following[starts + sizes - 1] = starts

    # Pairs of loops on one edge: an edge with any other number of faces has
    # no "both sides".
    order = np.argsort(edge, kind='stable')
    _, first, seen = np.unique(edge[order], return_index=True, return_counts=True)
    first = first[seen == 2]
    a, b = order[first], order[first + 1]

    crossing = (tile_of_loop[a] != tile_of_loop[b]) \
        & (tile_of_loop[a] > 0) & (tile_of_loop[b] > 0)
    a, b = a[crossing], b[crossing]

    # Winding normally runs opposite, so `b`'s start is `a`'s end; not always.
    opposite = vertex[a] != vertex[b]
    b_start = np.where(opposite, following[b], b)
    b_end = np.where(opposite, b, following[b])

    near = np.ones(len(a), dtype=bool)
    for one, other in ((a, b_start), (following[a], b_end)):
        delta = uvs[one] - uvs[other]
        delta -= np.round(delta)
        near &= (np.abs(delta) < _UNSEAMED_REACH).all(axis=1)

    return int(near.sum())


# --- making one ---------------------------------------------------------------


def add_tiles(image, tiles):
    """Give `image` whichever of `tiles` it lacks. Returns the numbers added.

    Each new tile is the size of the first and flooded with the colour the
    image was made with: a tile keeps its `generated_color` through saving and
    reopening (measured), so a base layer's new tile comes out the base colour,
    a paint layer's transparent and a mask's its own white or black.
    """
    missing = sorted(set(tiles) - {t.number for t in image.tiles})
    if not missing:
        return []

    first = image.tiles[0]
    width, height = first.size
    color = tuple(first.generated_color)

    for number in missing:
        image.tiles.new(number)
    for number in missing:
        image.tiles.active_index = [t.number for t in image.tiles].index(number)
        with bpy.context.temp_override(edit_image=image):
            bpy.ops.image.tile_fill(
                color=color, width=width, height=height, float=image.is_float,
                alpha=True,
            )
    image.tiles.active_index = 0
    return missing


def new_image(name, size, color, tiles, colorspace='sRGB', float_buffer=False):
    """A TILED image with `tiles`, each `size` square and flooded `color`.

    Filled tile by tile: a tile from `tiles.new` has no pixels and size (0, 0)
    until `tile_fill` gives it some, and `images.new` makes only the first.
    """
    image = bpy.data.images.new(
        name, size, size, alpha=True, float_buffer=float_buffer, tiled=True
    )
    image.colorspace_settings.name = colorspace

    # The list stays sorted only if it is built in order.
    tiles = sorted(tiles)
    image.tiles[0].number = tiles[0]
    for number in tiles[1:]:
        image.tiles.new(number)

    for index in range(len(image.tiles)):
        image.tiles.active_index = index
        with bpy.context.temp_override(edit_image=image):
            bpy.ops.image.tile_fill(
                color=color, width=size, height=size, float=float_buffer,
                alpha=True,
            )
    image.tiles.active_index = 0

    return image


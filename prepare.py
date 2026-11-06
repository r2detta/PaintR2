# SPDX-License-Identifier: GPL-3.0-or-later
"""Getting models ready to paint: the paint set, and the materials in it.

"Set Models" does two things, and the first one is what everything else in
PaintR2 now stands on:

1. **The selection becomes the paint set.** The chosen models are moved into
   the `Paint` collection and `Paint_high` is made beside it. From then on
   the window is pointed at that collection, not at whatever happens to be
   selected -- see paintset.py.

2. **Every material slot becomes a PaintR2 material, in place.** A model with
   no material at all gets `<ObjectName>_Paint`. A model that already has
   materials keeps their names: `Body` stays `Body`, and what changes is what
   is inside it.

That second half is destructive and is meant to be. The old behaviour made a
`Body_paint` copy and swapped the slot, which kept the original safe but gave
every export a `_paint` in its filename and gave the file two materials where
the artist had one. The name is the texture set's name; it has to be the name
the artist chose.

The operations here are idempotent. Running "Set Models" twice reuses the
documents and images it made the first time instead of resetting them --
`document.is_paint` is what says which is which.

This module is pure data manipulation; the operator in ops/op_prepare.py owns
the reporting.
"""

import bpy

from . import nodegen
from . import paintset
from . import storage
from . import udim


PAINT_SUFFIX = "_Paint"


def paint_name(base_name):
    """`Suzanne` -> `Suzanne_Paint`. Already-suffixed names are left alone."""
    if base_name.endswith(PAINT_SUFFIX):
        return base_name
    return base_name + PAINT_SUFFIX


def is_paint_material(material):
    """True when `material`'s surface is compiled from a PaintR2 stack.

    The flag first, and a stack second. The second half is what carries a
    file made before the flag existed: back then the answer was a `_paint`
    suffix on the name, and now that names are kept there is no name to read
    -- but a material with layers in it is a material this add-on built, on
    any reading.
    """
    if material is None:
        return False
    document = getattr(material, "paintr2", None)
    if document is None:
        return False
    return bool(document.is_paint) or bool(len(document.layers))


# --- images -----------------------------------------------------------------


def ensure_image(name, resolution, color, colorspace='sRGB', tiles=None):
    """Return (image, created). Reuses an existing image with the same name.

    A reused image is left exactly as it is -- resizing or reflooding it would
    throw away work the user has already painted.

    `colorspace` is not cosmetic. A mask is data, not a picture: painting 0.5
    into an sRGB image and reading it back gives 0.74, so a mask that looks
    half-open would cut three quarters of the layer. Masks are Non-Color; the
    Base Color image a brush paints stays sRGB.

    `tiles` makes it a UDIM image with those tiles, `resolution` each.
    """
    image = bpy.data.images.get(name)
    if image is not None:
        return image, False

    if tiles:
        image = udim.new_image(name, resolution, tuple(color), tiles, colorspace)
    else:
        image = bpy.data.images.new(
            name,
            width=resolution,
            height=resolution,
            alpha=True,
        )
        image.colorspace_settings.name = colorspace
        image.generated_color = tuple(color)

    # Write it out immediately when there is a folder to write into. A
    # GENERATED image loses its pixels the moment the .blend is saved, so the
    # sooner it becomes a real file the better. See storage.py.
    storage.adopt_new_image(image)

    # Before the memfile step of whatever made it: written with the tags of
    # its creation, it would lose its first strokes to a later Ctrl+Z.
    from . import history

    history.guard_images([image])

    return image, True


def image_for(material, name, resolution, color, colorspace='sRGB', tiles=None):
    """`ensure_image` for a layer of `material`: UDIM when the material is.

    `tiles` is for a material no object uses yet; otherwise its UVs decide.
    """
    if material.paintr2.is_udim:
        tiles = tiles or udim.material_tiles(material)[0]
    else:
        tiles = None
    return ensure_image(name, resolution, color, colorspace, tiles)


def extend_tiles(material):
    """Give `material`'s UDIM images the tiles its UVs have reached since.

    Run where a stale tile set would show: "Set Models", opening the window and
    before a bake. Never removes a tile -- one no UV uses any more may still
    hold paint someone wants back. Returns how many tiles were added.
    """
    if not getattr(getattr(material, "paintr2", None), "is_udim", False):
        return 0

    tiles = udim.material_tiles(material)[0]
    added = 0
    for image in storage.layer_images(material):
        if not udim.is_tiled(image) or set(tiles) <= set(udim.tiles_of(image)):
            continue
        packed = image.packed_file is not None
        if packed:
            # Into memory while the packed copy is still there to load from:
            # adding the tile unpacks the image.
            for tile in udim.tiles_of(image):
                udim.read(image, tile)
        added += len(udim.add_tiles(image, tiles))
        if packed and not storage.repack(image):
            print(f"[PaintR2] {image.name} took a new tile and could not be "
                  f"packed again; save the .blend to keep it")
    if added:
        print(f"[PaintR2] {material.name}: {added} new UDIM tile(s)")
    return added


def unique_image_name(base):
    """A name no existing image holds, for when reuse is *not* wanted."""
    if base not in bpy.data.images:
        return base

    index = 2
    while f"{base} {index}" in bpy.data.images:
        index += 1
    return f"{base} {index}"


def base_image_name(material):
    """What the base layer's image is called: `Body_Base`.

    Not the material's own name any more. It used to be, and it could be
    while every paint material carried a `_paint` suffix -- but a material
    called `Body` would now reach for an image called `Body`, and an
    unrelated texture already loaded under that name would be adopted as the
    base layer without a word.
    """
    return f"{material.name}_Base"


# --- documents --------------------------------------------------------------


BASE_LAYER_NAME = "Base"


def seed_document(material, resolution, color, use_udim=False, tiles=None):
    """Give `material` a stack with one base paint layer, if it has none.

    `use_udim` only counts the moment the material becomes a PaintR2 material.
    A document that already is one keeps what it was given then.
    """
    document = material.paintr2
    if not document.is_paint and not len(document.layers):
        document.is_udim = use_udim
    document.is_paint = True

    if not len(document.layers):
        image, _ = image_for(
            material, base_image_name(material), resolution, color, tiles=tiles
        )
        layer = document.add_layer(BASE_LAYER_NAME, 'PAINT', above_active=False)
        layer.image = image

    nodegen.compile_material(material)
    return material


def reset_material(material, resolution, color, use_udim=False):
    """Wipe what `material` was and make it a PaintR2 material of the same name.

    The whole node tree goes, not only the nodes PaintR2 owns: the point is
    that the surface afterwards is the stack and nothing else. Everything the
    material *is* to Blender otherwise -- its users, its slots, its name --
    is untouched, which is what makes this a reset rather than a replacement.
    """
    material.use_nodes = True
    material.node_tree.nodes.clear()

    # Back to Blender's default too. A material that arrived on BLENDED
    # renders its own back faces over its front ones the moment anything in
    # it becomes see-through, and the fade is exactly that moment.
    if getattr(material, "surface_render_method", 'DITHERED') != 'DITHERED':
        material.surface_render_method = 'DITHERED'

    document = material.paintr2
    document.layers.clear()
    document.active_index = 0
    document.active_effect = -1
    document.solo_mode = 'NONE'
    document.solo_index = -1
    # A reset is the start over: `seed_document` takes it from here as new.
    document.is_paint = False

    return seed_document(material, resolution, color, use_udim)


def ensure_paint_material(name, resolution, color, use_udim=False, tiles=None):
    """Return (material, created): a PaintR2 material called `name`.

    Used for the slot that has no material of its own to keep the name of.
    An existing document is never touched -- that is what makes running "Set
    Models" a second time harmless.

    `tiles` is here because the material is seeded before it is in a slot,
    when no object can yet answer for its UVs.
    """
    material = bpy.data.materials.get(name)
    created = material is None
    if created:
        material = bpy.data.materials.new(name)

    seed_document(material, resolution, color, use_udim, tiles)
    return material, created


# --- objects ----------------------------------------------------------------


class PrepareReport:
    """What `prepare_objects` did, for the operator to report back."""

    def __init__(self):
        self.created = []       # material names made this run
        self.reset = []         # material names emptied and rebuilt this run
        self.reused = []        # material names that were already ours
        self.moved = []         # object names moved into the Paint collection
        self.no_uv = []         # object names with no UV map
        self.bad_tiles = []     # names of UDIM objects with faces off the grid
        self.crossing = []      # names of UDIM objects with islands over a tile border
        self.unseamed = []      # names of UDIM objects whose UVs run on across tiles
        self.skipped = []       # (object name, reason)

    @property
    def touched(self):
        return len(self.created) + len(self.reset) + len(self.reused)

    def summary(self):
        parts = []
        if self.moved:
            parts.append(f"{len(self.moved)} model(s) into {paintset.PAINT}")
        if self.created:
            parts.append(f"{len(self.created)} material created")
        if self.reset:
            parts.append(f"{len(self.reset)} reset")
        if self.reused:
            parts.append(f"{len(self.reused)} already prepared")
        if not parts:
            parts.append("nothing to do")
        return ", ".join(parts)


def _unique_material_name(base):
    if base not in bpy.data.materials:
        return base

    index = 2
    while f"{base} {index}" in bpy.data.materials:
        index += 1
    return f"{base} {index}"


def _tiles_of_slot(obj, index, use_udim, report):
    """The tiles a slot of `obj` needs, or None when UDIM is not wanted."""
    if not use_udim:
        return None
    tiles, invalid = udim.object_tiles(obj, index)
    if invalid and obj.name not in report.bad_tiles:
        report.bad_tiles.append(obj.name)
    return tiles or [1001]


def prepare_object(obj, resolution, color, report, use_udim=False):
    """Give every material slot of `obj` a PaintR2 stack.

    With no slots at all, one is added named after the object. With existing
    slots, each keeps both its position *and* its material -- what changes is
    what the material contains.
    """
    if obj.type != 'MESH':
        report.skipped.append((obj.name, "not a mesh"))
        return

    mesh = obj.data

    if not mesh.uv_layers:
        report.no_uv.append(obj.name)

    # One of the two, not both: an island running over a border also has
    # edges the brush does not bleed across, and the cure for that -- keep
    # it inside one tile -- is the crossing warning's.
    if use_udim and udim.crossing_faces(mesh):
        report.crossing.append(obj.name)
    elif use_udim and udim.unseamed_edges(mesh):
        report.unseamed.append(obj.name)

    if not obj.material_slots:
        material, created = ensure_paint_material(
            _unique_material_name(paint_name(obj.name)), resolution, color,
            use_udim, _tiles_of_slot(obj, None, use_udim, report),
        )
        mesh.materials.append(material)
        (report.created if created else report.reused).append(material.name)
        return

    for index, slot in enumerate(obj.material_slots):
        source = slot.material

        if source is None:
            # An empty slot has no name to keep, so it borrows the object's.
            base = paint_name(obj.name) if index == 0 else \
                paint_name(f"{obj.name}_{index}")
            material, created = ensure_paint_material(
                _unique_material_name(base), resolution, color,
                use_udim, _tiles_of_slot(obj, index, use_udim, report),
            )
            slot.material = material
            (report.created if created else report.reused).append(material.name)
            continue

        if is_paint_material(source):
            # Already ours. Seeding is idempotent and only fills in what is
            # missing, so this never touches a painted layer.
            seed_document(source, resolution, color)
            report.reused.append(source.name)
            continue

        if use_udim:
            _tiles_of_slot(obj, index, use_udim, report)
        reset_material(source, resolution, color, use_udim)
        report.reset.append(source.name)


def prepare_objects(objects, resolution, color, context=None, use_udim=False):
    """Make `objects` the paint set and prepare their materials.

    Returns a PrepareReport. The collection move comes first: a material
    prepared for a model that is not in the set would be a texture set the
    window never shows.
    """
    context = context or bpy.context
    report = PrepareReport()

    meshes = [obj for obj in objects if obj is not None and obj.type == 'MESH']
    for obj in objects:
        if obj is not None and obj.type != 'MESH':
            report.skipped.append((obj.name, "not a mesh"))

    if not meshes:
        return report

    if use_udim:
        # A floor, not a value: this is the user's Bleed setting from here on.
        settings = context.scene.tool_settings.image_paint
        settings.seam_bleed = max(settings.seam_bleed, udim.SEAM_BLEED)

    paint, _high = paintset.ensure_pair(context)
    report.moved = [obj.name for obj in paintset.move_into(meshes, paint)]

    for obj in meshes:
        prepare_object(obj, resolution, color, report, use_udim)

    # A material that was already ours may have models with new UVs in it now.
    for obj in meshes:
        for slot in obj.material_slots:
            if slot.material is not None:
                extend_tiles(slot.material)

    return report

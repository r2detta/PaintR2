# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer mask operators.

A mask is a greyscale value multiplied into the layer's mix factor. White
shows the layer, black hides it. Any layer type can carry one -- masking a
Fill layer is how you paint a flat colour onto part of a model without
painting the colour itself.

There are two kinds and a layer may have both, in which case they multiply:

* a painted **image** mask, and
* a **procedural** mask -- a generated pattern that stays editable forever
  (procedural.py). `mask_enabled` is the switch for the whole slot.
"""

import os

import bpy
from bpy_extras.io_utils import ExportHelper

from .. import nodegen
from .. import prepare
from .. import procedural
from .. import storage
from ..ops.op_layers import active_material


def _active_layer(context):
    material = active_material(context)
    if material is None:
        return None, None

    document = material.paintr2
    return material, document.active_layer


class PAINTR2_OT_mask_add(bpy.types.Operator):
    """Add a mask to the active layer"""

    bl_idname = "paintr2.mask_add"
    bl_label = "Add Mask"
    bl_options = {'REGISTER', 'UNDO'}

    fill: bpy.props.EnumProperty(
        name="Fill",
        items=(
            ('WHITE', "White", "Mask starts fully revealing the layer"),
            ('BLACK', "Black", "Mask starts hiding the layer completely"),
        ),
        default='WHITE',
    )

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and layer.mask_image is None

    def execute(self, context):
        material, layer = _active_layer(context)
        if layer is None:
            return {'CANCELLED'}

        if layer.mask_image is not None:
            self.report({'INFO'}, "That layer already has a mask")
            return {'CANCELLED'}

        value = 1.0 if self.fill == 'WHITE' else 0.0
        props = context.scene.paintr2

        # A mask is data, not colour, and the space has to be set as the
        # image is made rather than after: the flood colour is written through
        # whatever the image says it is at the time.
        image, _ = prepare.ensure_image(
            prepare.unique_image_name(f"{material.name}_{layer.name}_mask"),
            int(props.image_resolution),
            (value, value, value, 1.0),
            colorspace='Non-Color',
        )
        storage.adopt_new_image(image)

        layer.mask_image = image
        layer.mask_enabled = True
        material.paintr2.set_active_part('MASK')

        nodegen.compile_material(material)
        self.report({'INFO'}, f"Mask added to {layer.name}")
        return {'FINISHED'}


def _drop_mask_effects(layer, material):
    """Clear a layer's mask effect stack. Called wherever the mask goes.

    A mask effect is a filter *on the mask*: it has no meaning without one,
    and a stack that outlives its mask is a row that does nothing and cannot
    be explained.
    """
    if not len(layer.mask_effects):
        return

    document = material.paintr2
    while len(layer.mask_effects):
        layer.mask_effects.remove(0)

    document.active_effect = -1


class PAINTR2_OT_mask_remove(bpy.types.Operator):
    """Remove the mask from the active layer"""

    bl_idname = "paintr2.mask_remove"
    bl_label = "Remove Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and layer.mask_image is not None

    def execute(self, context):
        material, layer = _active_layer(context)
        if layer is None or layer.mask_image is None:
            return {'CANCELLED'}

        # The image datablock is left in the file; Blender drops it on reload
        # if nothing else refers to it, and keeping it means an accidental
        # removal is recoverable.
        layer.mask_image = None
        material.paintr2.set_active_part('COLOR')

        # And the mask's effects go with it. They filter the mask; with no
        # mask there is nothing for them to filter, and leaving them in the
        # list is how a row ends up sitting under a layer doing nothing --
        # which is exactly what was reported.
        _drop_mask_effects(layer, material)

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_mask_procedural_add(bpy.types.Operator):
    """Give the active layer a generated mask instead of a painted one"""

    bl_idname = "paintr2.mask_procedural_add"
    bl_label = "Add Procedural Mask"
    bl_options = {'REGISTER', 'UNDO'}

    kind: bpy.props.EnumProperty(
        name="Pattern",
        items=procedural.KINDS,
        default='GRADIENT',
    )

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and not layer.mask_procedural.enabled

    def execute(self, context):
        material, layer = _active_layer(context)
        if layer is None:
            return {'CANCELLED'}

        if layer.mask_procedural.enabled:
            self.report({'INFO'}, "That layer already has a procedural mask")
            return {'CANCELLED'}

        layer.mask_procedural.kind = self.kind
        if procedural.edits_in_graph(self.kind):
            procedural.ensure_custom_tree(
                layer.mask_procedural, material, f"{layer.name} Mask"
            )
        # Last, so the single recompile the flag triggers already sees the
        # pattern it is meant to build.
        layer.mask_procedural.enabled = True
        layer.mask_enabled = True
        # A mask that has just been made is the half being worked on, the same
        # as a painted one -- otherwise the chip appears and nothing points at
        # it, and the next filter would land on the colour.
        material.paintr2.set_active_part('MASK')

        nodegen.compile_material(material)
        self.report({'INFO'},
                    f"{procedural.kind_label(self.kind)} mask on {layer.name}")
        return {'FINISHED'}


class PAINTR2_OT_mask_procedural_remove(bpy.types.Operator):
    """Remove the generated mask from the active layer"""

    bl_idname = "paintr2.mask_procedural_remove"
    bl_label = "Remove Procedural Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and layer.mask_procedural.enabled

    def execute(self, context):
        material, layer = _active_layer(context)
        if layer is None or not layer.mask_procedural.enabled:
            return {'CANCELLED'}

        # Only switched off: the parameters stay, so putting it back gives
        # you the gradient you had rather than a fresh one.
        layer.mask_procedural.enabled = False
        if layer.mask_image is None:
            material.paintr2.set_active_part('COLOR')
            # That was the whole mask. Its effects go too.
            _drop_mask_effects(layer, material)

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_mask_procedural_bake(bpy.types.Operator):
    """Freeze the generated mask into pixels you can paint on"""

    bl_idname = "paintr2.mask_procedural_bake"
    bl_label = "Bake Procedural Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and layer.mask_procedural.enabled

    def execute(self, context):
        from .. import bake

        material, layer = _active_layer(context)
        if layer is None:
            return {'CANCELLED'}

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select the model this material is on")
            return {'CANCELLED'}

        result = bake.bake_mask(obj, material, layer)
        if result.error is not None:
            self.report({'ERROR'}, str(result.error))
            return {'CANCELLED'}

        # Painting is the reason to bake it, so the brush is pointed at it.
        material.paintr2.set_active_part('MASK')
        self.report({'INFO'}, f"Baked the pattern into {layer.name}'s mask")
        return {'FINISHED'}


class PAINTR2_OT_mask_toggle(bpy.types.Operator):
    """Turn a layer's mask on or off without deleting it"""

    bl_idname = "paintr2.mask_toggle"
    bl_label = "Toggle Mask"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(
        name="Layer",
        description="Which layer, or -1 for the active one",
        default=-1,
    )

    @classmethod
    def poll(cls, context):
        # Either kind of mask -- the switch turns the whole mask slot off.
        _material, layer = _active_layer(context)
        return layer is not None and layer.has_mask

    def execute(self, context):
        material = active_material(context)
        if material is None:
            return {'CANCELLED'}

        document = material.paintr2
        # Shift-clicking a mask chip names the row it landed on, which is not
        # necessarily the active layer: the gesture switches the mask it is
        # pointing at without first making that layer the selected one.
        if self.index >= 0:
            if self.index >= len(document.layers):
                return {'CANCELLED'}
            layer = document.layers[self.index]
        else:
            layer = document.active_layer

        if layer is None or not layer.has_mask:
            return {'CANCELLED'}

        layer.mask_enabled = not layer.mask_enabled
        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_mask_invert(bpy.types.Operator):
    """Invert the active layer's mask"""

    bl_idname = "paintr2.mask_invert"
    bl_label = "Invert Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and layer.mask_image is not None

    def execute(self, context):
        _material, layer = _active_layer(context)
        image = layer.mask_image if layer else None
        if image is None:
            return {'CANCELLED'}

        # numpy, not a Python loop: a 2048x2048 mask is 16.7 million floats,
        # and `list(image.pixels)` alone takes seconds.
        import numpy

        width, height = image.size
        pixels = numpy.empty(width * height * image.channels, dtype=numpy.float32)
        image.pixels.foreach_get(pixels)

        channels = pixels.reshape(-1, image.channels)
        channels[:, :3] = 1.0 - channels[:, :3]   # alpha is left alone

        image.pixels.foreach_set(pixels)
        # foreach_set writes the buffer without telling Blender the image
        # changed, so both of these are needed: one to refresh what is drawn,
        # one so storage.py knows there is work worth saving.
        image.update()
        image.update_tag()

        self.report({'INFO'}, f"Inverted the mask on {layer.name}")
        return {'FINISHED'}


class PAINTR2_OT_mask_export(bpy.types.Operator, ExportHelper):
    """Write one layer's painted mask to an image file

    One mask, on its own. A document with thirty layers has one mask in it
    that somebody else needs -- an opacity map for an engine, a decal cutout,
    something to take into another tool -- and the way to get it out was to
    find the image in the Blend File view and save it from there.
    """

    bl_idname = "paintr2.mask_export"
    bl_label = "Export Mask"
    bl_options = {'REGISTER'}

    index: bpy.props.IntProperty(
        name="Layer",
        description="Which layer's mask, or -1 for the active one",
        default=-1,
        options={'HIDDEN'},
    )

    file_format: bpy.props.EnumProperty(
        name="Format",
        description="What to write. PNG is 8 or 16 bit; OpenEXR keeps the "
        "mask as float",
        items=(
            ('PNG', "PNG", "8 or 16 bit, lossless"),
            ('OPEN_EXR', "OpenEXR", "32 bit float"),
            ('TARGA', "Targa", "8 bit, uncompressed"),
            ('TIFF', "TIFF", "8 or 16 bit"),
        ),
        default='PNG',
    )

    filename_ext = ".png"
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.exr;*.tga;*.tif;*.tiff",
        options={'HIDDEN'},
    )

    _EXTENSIONS = {
        'PNG': ".png", 'OPEN_EXR': ".exr", 'TARGA': ".tga", 'TIFF': ".tif",
    }

    @classmethod
    def poll(cls, context):
        _material, layer = _active_layer(context)
        return layer is not None and (layer.has_mask
                                      or bool(len(layer.mask_effects)))

    def _layer(self, context):
        material = active_material(context)
        if material is None:
            return None, None

        document = material.paintr2
        if self.index >= 0:
            if self.index >= len(document.layers):
                return material, None
            return material, document.layers[self.index]
        return material, document.active_layer

    def invoke(self, context, event):
        material, layer = self._layer(context)
        if layer is None or not (layer.has_mask or len(layer.mask_effects)):
            self.report({'ERROR'}, "That layer has no mask")
            return {'CANCELLED'}

        # A name that says what the file is without being opened, and that
        # does not collide with the next layer's mask.
        stem = bpy.path.clean_name(f"{material.name}_{layer.name}_mask")
        self.filepath = stem + self._EXTENSIONS.get(self.file_format, ".png")

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    @staticmethod
    def _is_plain(layer):
        """True when the mask is the painted image and nothing else.

        Then the file can be the pixels themselves, which is exact. Anything
        else -- a pattern, a filter on the mask -- only exists as shader nodes
        and has to be evaluated to become a picture.
        """
        return (layer.mask_image is not None
                and not layer.mask_procedural.enabled
                and not len(layer.mask_effects))

    def _copy_of(self, image):
        """A throwaway image carrying `image`'s pixels.

        **Not `image.copy()`.** Measured: copying a `GENERATED` image
        re-generates it from `generated_color` instead of duplicating the
        buffer, so the copy comes out flat in the colour the mask was created
        with and every stroke painted into it is gone. That is exactly what a
        mask export looked like. Assigning `pixels` across is what actually
        carries the painting.
        """
        made = bpy.data.images.new(
            f"{image.name}_export", image.size[0], image.size[1],
            alpha=True, float_buffer=image.is_float,
        )
        made.colorspace_settings.name = image.colorspace_settings.name
        made.pixels = image.pixels[:]
        return made

    def execute(self, context):
        material, layer = self._layer(context)
        if layer is None or not (layer.has_mask or len(layer.mask_effects)):
            self.report({'ERROR'}, "That layer has no mask")
            return {'CANCELLED'}

        path = bpy.path.ensure_ext(
            self.filepath, self._EXTENSIONS.get(self.file_format, ".png")
        )

        directory = os.path.dirname(path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as error:
                self.report({'ERROR'}, f"Could not make {directory}: {error}")
                return {'CANCELLED'}

        # What gets written is the mask the model is actually cut by: painted
        # pixels, times the pattern, through the whole mask effect stack. A
        # mask that is only paint takes the short path and is copied exactly;
        # anything else exists as shader nodes and has to be baked to become a
        # picture at all.
        from .. import bake

        note = "mask"
        if self._is_plain(layer):
            image = self._copy_of(layer.mask_image)
        else:
            result = bake.bake_mask_result(
                context.active_object, material, layer
            )
            if result.error is not None:
                self.report({'ERROR'}, f"Could not bake the mask: {result.error}")
                return {'CANCELLED'}
            image = result.images[0][1]
            note = "baked mask"

        try:
            image.filepath_raw = path
            image.file_format = self.file_format
            image.save()
        except (OSError, RuntimeError) as error:
            self.report({'ERROR'}, f"Could not write {path}: {error}")
            return {'CANCELLED'}
        finally:
            # Throwaway either way: the layer's own mask is never the thing
            # being saved, because `save()` writes `filepath_raw` back onto
            # the datablock and that would move where the document keeps it.
            try:
                bpy.data.images.remove(image)
            except (RuntimeError, ReferenceError):
                pass

        self.report({'INFO'}, f"Wrote {layer.name}'s {note} to {path}")
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_mask_add,
    PAINTR2_OT_mask_remove,
    PAINTR2_OT_mask_procedural_add,
    PAINTR2_OT_mask_procedural_remove,
    PAINTR2_OT_mask_procedural_bake,
    PAINTR2_OT_mask_toggle,
    PAINTR2_OT_mask_invert,
    PAINTR2_OT_mask_export,
)

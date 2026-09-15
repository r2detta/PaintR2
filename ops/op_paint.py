# SPDX-License-Identifier: GPL-3.0-or-later
"""Painting and layer-image persistence operators."""

import bpy

from .. import paint
from .. import storage


class PAINTR2_OT_paint_mode(bpy.types.Operator):
    """Enter Texture Paint aimed at the active layer, or leave it"""

    bl_idname = "paintr2.paint_mode"
    bl_label = "Paint Mode"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        if paint.is_painting(context):
            error = paint.leave(context)
            if error:
                self.report({'ERROR'}, error)
                return {'CANCELLED'}
            return {'FINISHED'}

        image, error = paint.enter(context)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        self.report({'INFO'}, f"Painting into {image.name}")
        return {'FINISHED'}


class PAINTR2_OT_sync_canvas(bpy.types.Operator):
    """Point the paint canvas at the active layer again"""

    bl_idname = "paintr2.sync_canvas"
    bl_label = "Sync Paint Canvas"
    bl_options = {'REGISTER'}

    def execute(self, context):
        image = paint.sync_canvas(context)
        if image is None:
            self.report({'WARNING'}, "No paint layer active")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Canvas: {image.name}")
        return {'FINISHED'}


class PAINTR2_OT_save_layers(bpy.types.Operator):
    """Write every painted layer to the folder beside the .blend file"""

    bl_idname = "paintr2.save_layers"
    bl_label = "Save Layer Images"
    bl_options = {'REGISTER'}

    def execute(self, context):
        saved, error = storage.save_all(only_needed=False)

        if error and not saved:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        if error:
            self.report({'WARNING'}, f"Saved {len(saved)}, but: {error}")
            return {'FINISHED'}

        self.report(
            {'INFO'},
            f"Saved {len(saved)} layer image(s) to {storage.external_directory()}",
        )
        return {'FINISHED'}


class PAINTR2_OT_pack_layers(bpy.types.Operator):
    """Embed every layer image inside the .blend file"""

    bl_idname = "paintr2.pack_layers"
    bl_label = "Pack Layer Images"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        # Nothing to pack is not a failure to report after the fact; it is a
        # button that should not have been pressable.
        return any(
            image.packed_file is None for image in storage.layer_images()
        )

    def execute(self, context):
        packed, error = storage.pack_all()

        if error and not packed:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        if error:
            self.report({'WARNING'}, f"Packed {len(packed)}, but: {error}")
            return {'FINISHED'}

        if not packed:
            self.report({'INFO'}, "Everything is already packed")
            return {'FINISHED'}

        self.report({'INFO'}, f"Packed {len(packed)} layer image(s)")
        return {'FINISHED'}


class PAINTR2_OT_unpack_layers(bpy.types.Operator):
    """Move packed layer images back out to disk"""

    bl_idname = "paintr2.unpack_layers"
    bl_label = "Unpack Layer Images"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(
            image.packed_file is not None for image in storage.layer_images()
        )

    def execute(self, context):
        unpacked, error = storage.unpack_all()

        if error and not unpacked:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        self.report({'INFO'}, f"Unpacked {len(unpacked)} layer image(s)")
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_paint_mode,
    PAINTR2_OT_sync_canvas,
    PAINTR2_OT_save_layers,
    PAINTR2_OT_pack_layers,
    PAINTR2_OT_unpack_layers,
)

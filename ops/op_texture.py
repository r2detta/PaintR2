# SPDX-License-Identifier: GPL-3.0-or-later
"""Getting an image file into a brush texture.

Blender does this with `image.open`, which loads a file and hands it to
whatever `context.space_data` the click came from -- an Image Editor's image,
a node's, a texture slot's. Inside a PaintR2 window there is no such space:
the panel is ours and not a Blender region, so the operator would load
the file and drop it.

So the file browser is ours too. It takes the property to write the same way
every panel row does -- an owner and a key, resolved by `window._panel_owner`
-- which keeps the two texture panels one panel written twice.
"""

import os

import bpy


class PAINTR2_OT_image_open(bpy.types.Operator):
    """Open an image file and put it in this slot"""

    bl_idname = "paintr2.image_open"
    bl_label = "Open Image"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_image: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    filter_movie: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    owner: bpy.props.StringProperty(options={'HIDDEN'})
    key: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from .. import window

        if not self.filepath or not os.path.isfile(bpy.path.abspath(self.filepath)):
            self.report({'ERROR'}, "Pick an image file")
            return {'CANCELLED'}

        # `check_existing` rather than a fresh datablock every time: opening
        # the same grunge map for the texture and again for its mask should
        # be one image in the file, not two.
        try:
            image = bpy.data.images.load(self.filepath, check_existing=True)
        except RuntimeError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        window._set_panel_prop(self.owner, self.key, image.name)

        holder = window._panel_owner(self.owner, self.key)
        if holder is None or getattr(holder, self.key, None) is not image:
            self.report({'ERROR'}, f"Could not put {image.name} in that slot")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Opened {image.name}")
        return {'FINISHED'}


class PAINTR2_OT_despeckle(bpy.types.Operator):
    """Put back the texels Blender's occlusion test dropped on this layer"""

    bl_idname = "paintr2.despeckle"
    bl_label = "Clean Specks"
    # No 'UNDO'. A memfile undo step does not contain the pixels this
    # writes, so claiming one would put a step on Blender's stack that undoes
    # everything about the file except the thing that changed. It records
    # itself on PaintR2's own ring instead, where Ctrl+Z can reach it.
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene.tool_settings, "image_paint", None)
        return settings is not None and settings.canvas is not None

    def execute(self, context):
        from .. import despeckle

        image = context.scene.tool_settings.image_paint.canvas
        try:
            fixed = despeckle.clean(image)
        except (RuntimeError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        if fixed:
            self.report({'INFO'}, f"Put back {fixed} texels in {image.name}")
        else:
            self.report({'INFO'}, f"No specks in {image.name}")
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_image_open,
    PAINTR2_OT_despeckle,
)

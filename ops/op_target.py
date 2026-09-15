# SPDX-License-Identifier: GPL-3.0-or-later
"""Choosing what is being painted: which model, and which of its materials.

Both answers are kept in Blender's own state rather than beside it:

    which model     view_layer.objects.active
    which material  object.active_material_index

That is deliberate and it is what made multi-material work cheap. Every
existing path -- `paint.active_document`, `op_layers.active_material`, the
status line, the bake -- already asked Blender those two questions, so
pointing the Objects/Materials panel at them meant the whole of the rest of
the add-on followed without being told. A `session.active_material`
of our own would have been a second answer for everything to disagree with.

The one thing Blender does not do for us is carry the *mode* across. Texture
Paint belongs to an object; making another object active leaves the brush
pointed at nothing. So switching models leaves paint mode on the way out and
enters it again on the way in.
"""

import bpy

from .. import isolation
from .. import paint
from .. import paintset
from .. import session


def _leave_paint(context):
    """Drop out of Texture Paint if we are in it. True when we were."""
    if not paint.is_painting(context):
        return False
    paint.leave(context)
    return True


class PAINTR2_OT_select_object(bpy.types.Operator):
    """Paint this model"""

    bl_idname = "paintr2.select_object"
    bl_label = "Select Model"
    bl_options = {'REGISTER', 'INTERNAL'}

    name: bpy.props.StringProperty(name="Object")

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            self.report({'ERROR'}, f"No object named {self.name}")
            return {'CANCELLED'}

        if obj is context.active_object:
            return {'CANCELLED'}

        was_painting = _leave_paint(context)

        view_layer = context.view_layer
        for other in context.selected_objects:
            other.select_set(False)

        try:
            obj.select_set(True)
            view_layer.objects.active = obj
        except RuntimeError as error:
            self.report({'ERROR'}, f"Could not select {obj.name}: {error}")
            return {'CANCELLED'}

        # The fade is what says which model is being worked on, so it moves
        # with the selection rather than waiting for the next redraw.
        isolation.sync_fade(context)

        if was_painting:
            _image, error = paint.enter(context)
            if error:
                # Not a failure of the switch: the model is selected, it just
                # cannot be painted yet. Saying which is the useful half.
                self.report({'WARNING'}, error)
        else:
            paint.sync_canvas(context)

        session.tag_redraw()
        return {'FINISHED'}


class PAINTR2_OT_select_material(bpy.types.Operator):
    """Paint this material's stack"""

    bl_idname = "paintr2.select_material"
    bl_label = "Select Material"
    bl_options = {'REGISTER', 'INTERNAL'}

    index: bpy.props.IntProperty(name="Slot", default=0)

    def execute(self, context):
        obj = context.active_object
        if obj is None or not obj.material_slots:
            return {'CANCELLED'}

        if not 0 <= self.index < len(obj.material_slots):
            self.report({'ERROR'}, "No such material slot")
            return {'CANCELLED'}

        obj.active_material_index = self.index

        # The canvas follows the slot: the brush has to write into the stack
        # the panel is now showing, or clicking a material would silently
        # leave the next stroke on the one before it.
        paint.sync_canvas(context)
        session.tag_redraw()
        return {'FINISHED'}


class PAINTR2_OT_isolate_refresh(bpy.types.Operator):
    """Hide everything outside the paint set again"""

    bl_idname = "paintr2.isolate_refresh"
    bl_label = "Refresh Isolation"
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return bool(paintset.paint_collection())

    def execute(self, context):
        hidden = isolation.isolate(context)
        isolation.sync_fade(context)
        session.tag_redraw()
        self.report({'INFO'}, f"{hidden} object(s) hidden")
        return {'FINISHED'}


class PAINTR2_OT_show_all(bpy.types.Operator):
    """Show everything the paint session hid"""

    bl_idname = "paintr2.show_all"
    bl_label = "Show Hidden Objects"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return isolation.is_isolated(context)

    def execute(self, context):
        shown = isolation.restore(context)
        session.tag_redraw()
        self.report({'INFO'}, f"{shown} object(s) shown")
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_select_object,
    PAINTR2_OT_select_material,
    PAINTR2_OT_isolate_refresh,
    PAINTR2_OT_show_all,
)

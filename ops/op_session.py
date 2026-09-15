# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators that open and close the PaintR2 window.

These are the only entry points the rest of Blender sees. `paintr2.open` is
what the sidebar button calls; `paintr2.close` exists so there is always a way
back even if the custom interface itself is wedged.
"""

import bpy

from .. import paint
from .. import session
from .. import window as window_mod


class PAINTR2_OT_open(bpy.types.Operator):
    """Open the PaintR2 painting window"""

    bl_idname = "paintr2.open"
    bl_label = "Open PaintR2"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.area is not None

    def execute(self, context):
        if session.state.is_open:
            window = session.find_window()
            if window is not None:
                self.report({'INFO'}, "PaintR2 is already open")
                return {'CANCELLED'}
            # Stale state from a window that vanished; clear it and retry.
            window_mod.close(window_already_gone=True)

        window, area, reason = window_mod.spawn(context)
        if window is None or area is None:
            # The reason comes from the step that actually failed. It used to
            # be one sentence for five different problems, and the advice in
            # it -- run this from the sidebar -- was being read by people who
            # had just pressed the button in the sidebar.
            self.report(
                {'ERROR'},
                f"Could not open the PaintR2 window: "
                f"{reason or 'no reason was reported'}",
            )
            return {'CANCELLED'}

        region = session.find_region(area)
        if region is None:
            window_mod.close()
            self.report({'ERROR'}, "The new window has no drawable region")
            return {'CANCELLED'}

        try:
            with context.temp_override(window=window, area=area, region=region):
                bpy.ops.paintr2.ui_modal('INVOKE_DEFAULT')
        except RuntimeError as error:
            window_mod.close()
            self.report({'ERROR'}, f"Could not start the interface: {error}")
            return {'CANCELLED'}

        # If the layer that happens to be active is paintable, be ready to
        # paint the moment the window appears.
        paint.auto_sync_mode(context)

        return {'FINISHED'}


class PAINTR2_OT_close(bpy.types.Operator):
    """Close the PaintR2 painting window"""

    bl_idname = "paintr2.close"
    bl_label = "Close PaintR2"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return session.state.is_open

    def execute(self, context):
        window_mod.close()
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_open,
    PAINTR2_OT_close,
)

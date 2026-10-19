# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators that open and close PaintR2.

These are the only entry points the rest of Blender sees. `paintr2.open` is
what the sidebar button calls; `paintr2.close` exists so there is always a way
back even if the custom interface itself is wedged.
"""

import bpy

from .. import session
from .. import window as window_mod


class PAINTR2_OT_open(bpy.types.Operator):
    """Open PaintR2 in this window"""

    bl_idname = "paintr2.open"
    bl_label = "Open PaintR2"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.area is not None

    def execute(self, context):
        if session.state.is_open:
            if session.find_window() is not None:
                self.report({'INFO'}, "PaintR2 is already open")
                return {'CANCELLED'}
            # Stale state from a window that vanished; clear it and retry.
            window_mod.close(window_already_gone=True)

        # Only the first step happens here: the window switches workspace on
        # the next pass of the event loop, and the interface is started from
        # there. A failure after this point is reported by that step.
        reason = window_mod.spawn(context)
        if reason:
            # The reason comes from the step that actually failed. It used to
            # be one sentence for five different problems, and the advice in
            # it -- run this from the sidebar -- was being read by people who
            # had just pressed the button in the sidebar.
            self.report({'ERROR'}, f"Could not open PaintR2: {reason}")
            return {'CANCELLED'}

        return {'FINISHED'}


class PAINTR2_OT_close(bpy.types.Operator):
    """Close PaintR2 and go back to the workspace it was opened from"""

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

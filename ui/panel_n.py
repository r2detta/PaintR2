# SPDX-License-Identifier: GPL-3.0-or-later
"""3D Viewport sidebar entry point.

This is the *only* native Blender UI PaintR2 has, and it stays deliberately
small: preparing a model and opening the window. It is
the first thing someone new to PaintR2 sees, so everything else -- layers,
painting, mesh maps, export -- belongs in the PaintR2 window, drawn by gui/.
"""

import bpy

from .. import session


class PAINTR2_PT_sidebar(bpy.types.Panel):
    bl_label = "PaintR2"
    bl_idname = "PAINTR2_PT_sidebar"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"

    def draw(self, context):
        layout = self.layout
        is_open = session.state.is_open
        props = context.scene.paintr2

        # Preparation comes first: a model needs a paint material before the
        # window is of any use.
        box = layout.box()
        box.label(text="Prepare", icon='MATERIAL')
        box.prop(props, "image_resolution")

        row = box.row()
        row.scale_y = 1.3
        row.operator("paintr2.set_models", text="Set Models", icon='CHECKMARK')

        targets = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not targets and context.active_object is not None:
            if context.active_object.type == 'MESH':
                targets = [context.active_object]
        box.label(text=f"{len(targets)} mesh selected", icon='INFO')

        # What the set currently holds. The set, not the selection: it is what
        # the window shows and what an export walks, and it stops being
        # obvious the moment the selection has moved on.
        from .. import paintset

        models = paintset.paint_objects()
        set_box = layout.box()
        set_box.label(
            text=f"{paintset.PAINT}: {len(models)} model(s)",
            icon='OUTLINER_COLLECTION',
        )
        row = set_box.row()
        row.enabled = bpy.ops.paintr2.unset_models.poll()
        row.operator(
            "paintr2.unset_models", text="Remove from Set", icon='LOOP_BACK'
        )

        from .. import isolation

        if isolation.is_isolated(context):
            hidden = len(props.hidden_objects)
            row = set_box.row()
            row.operator(
                "paintr2.show_all",
                text=f"Show {hidden} Hidden Object(s)",
                icon='HIDE_OFF',
            )

        layout.separator()

        column = layout.column()
        column.scale_y = 1.6
        if is_open:
            column.operator("paintr2.close", text="Close PaintR2", icon='WINDOW')
        else:
            column.operator("paintr2.open", text="Open PaintR2", icon='BRUSH_DATA')

        if not is_open:
            layout.label(text="Painting, baking and export", icon='INFO')
            layout.label(text="live in the PaintR2 window")


CLASSES = (
    PAINTR2_PT_sidebar,
)

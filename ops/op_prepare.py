# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Set Models" operator: build the paint set out of the selection."""

import bpy

from .. import paintset
from .. import prepare


class PAINTR2_OT_set_models(bpy.types.Operator):
    """Move the selected models into the Paint collection and make every \
material slot a PaintR2 material

    A slot that already has a material keeps its name; what is inside it is \
replaced. A model with no material gets one named after it. This cannot be \
undone from inside PaintR2 -- the original surface is gone.
    """

    bl_idname = "paintr2.set_models"
    bl_label = "Set Models"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_target_objects(context))

    def execute(self, context):
        targets = _target_objects(context)
        if not targets:
            self.report({'ERROR'}, "Select at least one mesh object")
            return {'CANCELLED'}

        props = context.scene.paintr2
        resolution = int(props.image_resolution)
        color = tuple(props.image_base_color)

        report = prepare.prepare_objects(
            targets, resolution, color, context, props.image_udim
        )

        if not report.touched and not report.moved:
            self.report({'WARNING'}, "Nothing prepared: no mesh objects selected")
            return {'CANCELLED'}

        # The set just changed shape, so what the window shows has to change
        # with it: a model added while the window is open must appear, and
        # everything else must stay hidden.
        _resync_session(context)

        # Every problem found is said, in one line. Only the first used to be,
        # so a model with faces off the grid never heard about the island
        # that also ran over a border.
        warnings = [
            f"{label} on: {_names(names)} - {cure}"
            for names, label, cure in (
                (report.bad_tiles, "UVs outside the UDIM grid",
                 "those faces get no texture"),
                (report.crossing, "UV islands cross a tile border",
                 "fills and the external baker stop at the border; "
                 "keep each island inside one tile"),
                (report.unseamed, "Thin lines may show",
                 "part of a UV shell sits a whole tile away from the rest; "
                 "pull the parts slightly apart"),
                (report.no_uv, "No UV map", "unwrap before painting"),
            )
            if names
        ]
        if warnings:
            self.report({'WARNING'}, ". ".join([report.summary()] + warnings))
        else:
            self.report({'INFO'}, report.summary())

        return {'FINISHED'}


class PAINTR2_OT_unset_models(bpy.types.Operator):
    """Take the selected models out of the Paint collection

    Nothing about the material is undone: the stack, its images and the \
compiled shader all stay, so putting the model back in re-opens the same work.
    """

    bl_idname = "paintr2.unset_models"
    bl_label = "Remove from Paint Set"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(paintset.in_set(obj) for obj in _target_objects(context))

    def execute(self, context):
        from .. import paint, session

        targets = [obj for obj in _target_objects(context) if paintset.in_set(obj)]
        if not targets:
            self.report({'WARNING'}, "Nothing in the paint set is selected")
            return {'CANCELLED'}

        # Painting into a model that is about to leave the set would be
        # painting into something the window no longer shows.
        if paint.is_painting(context) and context.active_object in targets:
            paint.leave(context)

        moved = paintset.move_out(targets, context)
        _resync_session(context)

        if session.state.is_open and not paintset.paint_objects():
            self.report({'WARNING'},
                        f"{len(moved)} removed - the paint set is now empty")
            return {'FINISHED'}

        self.report({'INFO'}, f"{len(moved)} model(s) removed from "
                              f"{paintset.PAINT}")
        return {'FINISHED'}


def _names(names):
    """At most three object names for a status line, then an ellipsis."""
    return ", ".join(names[:3]) + ("..." if len(names) > 3 else "")


def _resync_session(context):
    """Bring the open window's isolation and fade back in step with the set."""
    from .. import isolation, session

    if not session.state.is_open:
        return

    isolation.isolate(context)
    isolation.sync_fade(context)
    session.tag_redraw()


def _target_objects(context):
    """Selected mesh objects, falling back to the active one."""
    selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
    if selected:
        return selected

    active = context.active_object
    return [active] if active is not None and active.type == 'MESH' else []


CLASSES = (PAINTR2_OT_set_models, PAINTR2_OT_unset_models)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Picking an HDRI off disk.

The only part of the lighting controls that cannot be a property row: opening
a file browser is an operator's job, not a panel's. Everything the browser
comes back with is written into `scene.paintr2.display.hdri_path`, whose update
hook rebuilds the world -- so this operator knows nothing about node trees.
"""

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper

from .. import session


class PAINTR2_OT_hdri_open(bpy.types.Operator, ImportHelper):
    """Choose an environment map to light the model with"""

    bl_idname = "paintr2.hdri_open"
    bl_label = "Open HDRI"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".exr"
    filter_glob: StringProperty(
        default="*.exr;*.hdr;*.png;*.jpg;*.jpeg;*.tif;*.tiff",
        options={'HIDDEN'},
    )

    def execute(self, context):
        settings = context.scene.paintr2.display
        settings.hdri_path = self.filepath
        # Choosing a map is choosing to be lit by it. Making the user find the
        # switch afterwards would be a step with no decision in it.
        if not settings.use_lighting:
            settings.use_lighting = True
        session.tag_redraw()
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_hdri_open,
)

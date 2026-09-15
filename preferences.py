# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on preferences.

Kept deliberately small. Anything that describes *a painting job* belongs in
properties.py (saved per .blend); only machine-level choices belong here.
"""

import bpy
from bpy.props import (
    BoolProperty,
    FloatProperty,
    FloatVectorProperty,
    StringProperty,
)
from bpy.types import AddonPreferences


class PR2_Preferences(AddonPreferences):
    # Extensions are registered under their package name; __package__ is the
    # only correct value here (it differs between legacy add-ons and extensions).
    bl_idname = __package__

    ui_scale: FloatProperty(
        name="Interface Scale",
        description=(
            "Extra scale applied on top of Blender's own UI scale, for the "
            "PaintR2 window only"
        ),
        default=1.0,
        min=0.5,
        max=3.0,
        subtype='FACTOR',
    )

    # The wireframe overlay draws in Blender's *theme* colours, and a theme is
    # application-wide -- there is no per-viewport wire colour to write (the
    # space only offers THEME / OBJECT / RANDOM). So this is an override with
    # a borrowing rule, not a setting: it is written while the PaintR2 window
    # has the wireframe up and taken back off the moment it does not.
    wire_color_custom: BoolProperty(
        name="Custom Wireframe Colour",
        description=(
            "Draw the wireframe in the colour below while the PaintR2 window "
            "is showing it. Off, or with the window closed, Blender's own "
            "theme colour is used"
        ),
        default=False,
    )

    wire_color: FloatVectorProperty(
        name="Wireframe Colour",
        description="The colour the wireframe is drawn in inside the PaintR2 window",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        # Not Blender's black: turning the switch on has to be visible, and
        # black wire on a dark texture is the thing being fixed.
        default=(1.0, 1.0, 1.0),
    )

    # The development loop this exists for: change a PNG, close and reopen the
    # window, look at it. Without it the only folder read is the one inside
    # the installed package, so trying an icon means taking a build.
    icon_folder: StringProperty(
        name="Icon Folder",
        description=(
            "Read the interface icons from this folder instead of the ones "
            "shipped with the add-on. A name with no file in it falls back to "
            "the packaged icon, and then to the drawn one, so a folder with "
            "two icons in it works"
        ),
        subtype='DIR_PATH',
        default="",
    )

    debug_overlay: BoolProperty(
        name="Debug Overlay",
        description="Draw region size, mouse position and event info in the window",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "ui_scale")

        row = col.row(align=True)
        row.prop(self, "wire_color_custom")
        sub = row.row(align=True)
        sub.active = self.wire_color_custom
        sub.prop(self, "wire_color", text="")

        col.separator()
        col.prop(self, "icon_folder")

        col.separator()
        col.prop(self, "debug_overlay")


def get(context=None):
    """Return the add-on preferences, or None if the add-on is mid-unregister."""
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


CLASSES = (PR2_Preferences,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

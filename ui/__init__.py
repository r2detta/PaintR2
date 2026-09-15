# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Blender UI registration."""

import bpy

from . import panel_n

_MODULES = (panel_n,)


def _all_classes():
    for module in _MODULES:
        yield from module.CLASSES


def register():
    for cls in _all_classes():
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(list(_all_classes())):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

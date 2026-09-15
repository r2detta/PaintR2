# SPDX-License-Identifier: GPL-3.0-or-later
"""Operator registration."""

import bpy

from . import op_export
from . import op_fill
from . import op_layers
from . import op_lighting
from . import op_mask
from . import op_meshmaps
from . import op_modal
from . import op_paint
from . import op_prepare
from . import op_session
from . import op_target
from . import op_texture

_MODULES = (
    op_export,
    op_fill,
    op_layers,
    op_lighting,
    op_mask,
    op_meshmaps,
    op_modal,
    op_paint,
    op_prepare,
    op_session,
    op_target,
    op_texture,
)


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

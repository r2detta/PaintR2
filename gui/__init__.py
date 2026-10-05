# SPDX-License-Identifier: GPL-3.0-or-later
"""PaintR2's self-drawn interface toolkit.

Blender's Python API cannot register a new editor (Space) type, and the layout
PaintR2 needs -- a canvas with a layer stack -- does not map
onto Blender's panel system. So the window borrows an existing editor, covers
it completely with a `'POST_PIXEL'` draw handler, and a modal operator feeds
the widget tree events.

    theme.py    colors and metrics
    draw.py     GPU primitives (rects, lines, text, clipping)
    widget.py   Widget / Container base classes, UIEvent
    widgets.py  the starter widget set
    root.py     UIRoot: layout, dispatch, the main frame

Nothing in this package imports bpy operators or touches session state, so it
stays independent of how the window is opened.
"""

from . import theme
from . import draw
from . import widget
from . import widgets
from . import root

from .root import UIRoot
from .widget import UIEvent

__all__ = ("theme", "draw", "widget", "widgets", "root", "UIRoot", "UIEvent")

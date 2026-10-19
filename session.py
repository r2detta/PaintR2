# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime session state for the PaintR2 window.

Nothing here is ever saved to the .blend. It only tracks the live window that
the custom GPU interface is drawn into, so that the draw handler, the modal
event loop and the watchdog all agree on *which* window/space is ours.

Blender hands out temporary Python wrappers for windows, areas, spaces and
regions -- holding a reference across a redraw is not safe. Integer pointers
from `as_pointer()` are stable for the lifetime of the datablock, so those are
what we store, and we re-resolve the wrappers on every use.
"""

import bpy


class SessionState:
    """Everything the live PaintR2 window needs, in one place."""

    __slots__ = (
        "window_ptr",
        "area_ptr",
        "space_ptr",
        "shader_area_ptr",
        "workspace_ptr",
        "return_workspace_ptr",
        "return_mode",
        "draw_handle",
        "modal_running",
        "root",
        "closing",
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.window_ptr = 0
        self.area_ptr = 0
        self.space_ptr = 0
        # The shader graph editor beside ours, when it is open. Zero is not
        # "closed by us" but "not there" -- Blender's own controls can close
        # it, and the pointer stops resolving the moment they do.
        self.shader_area_ptr = 0
        # The PaintR2 workspace the window was switched to, the one it came
        # from, and the object mode it was in -- what Close gives back.
        self.workspace_ptr = 0
        self.return_workspace_ptr = 0
        self.return_mode = ""
        self.draw_handle = None
        self.modal_running = False
        self.root = None
        # Guards against close() re-entering itself from the modal cancel path.
        self.closing = False

    @property
    def is_open(self):
        return self.window_ptr != 0


state = SessionState()


def find_window():
    """Return the live Window wrapper for this session, or None if it is gone."""
    if not state.window_ptr:
        return None

    wm = bpy.data.window_managers[0] if bpy.data.window_managers else None
    if wm is None:
        return None

    for win in wm.windows:
        if win.as_pointer() == state.window_ptr:
            return win
    return None


def find_area(window=None):
    """Return the live Area wrapper hosting the PaintR2 interface, or None."""
    if not state.area_ptr:
        return None

    window = window or find_window()
    if window is None or window.screen is None:
        return None

    for area in window.screen.areas:
        if area.as_pointer() == state.area_ptr:
            return area
    return None


def find_space(area=None):
    """Return the live Space wrapper of the PaintR2 area, or None.

    `is_our_space` answers "is this ours?"; this answers "which is ours?", and
    anything reaching for the view matrix needs the second question.
    """
    area = area if area is not None else find_area()
    if area is None:
        return None

    for space in area.spaces:
        if space.as_pointer() == state.space_ptr:
            return space
    return None


def find_region(area=None):
    """Return the main 'WINDOW' region of the PaintR2 area, or None."""
    area = area if area is not None else find_area()
    if area is None:
        return None

    for region in area.regions:
        if region.type == 'WINDOW':
            return region
    return None


def is_our_space(space):
    """True when `space` is the space the PaintR2 interface lives in.

    Draw handlers registered on a Space subclass fire for *every* editor of
    that type, so every callback has to check this before drawing.
    """
    return space is not None and space.as_pointer() == state.space_ptr


def tag_redraw():
    """Ask the PaintR2 area to redraw, if it still exists."""
    area = find_area()
    if area is not None:
        area.tag_redraw()


def _deferred_tag():
    tag_redraw()
    return None


def tag_redraw_soon():
    """Redraw once Blender has finished with the event being handled.

    Handing an event through to Blender means whatever it changes is still
    unchanged when the modal operator returns -- tagging a redraw there paints
    the value the shortcut just replaced. A zero-delay timer runs after the
    event loop has caught up, which is the first moment the new value can be
    read. Registered at most once, so a burst of key presses costs one redraw.
    """
    if not state.is_open:
        return
    if bpy.app.timers.is_registered(_deferred_tag):
        return
    bpy.app.timers.register(_deferred_tag, first_interval=0.0)

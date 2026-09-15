# SPDX-License-Identifier: GPL-3.0-or-later
"""Application handlers that keep the session honest.

The pointers in session.py are only valid for the current file and the current
Blender run. Loading a file or quitting invalidates them, so both events must
drop the session -- otherwise the draw handler keeps firing against freed
memory.

Teardown here never tries to close the window through an operator: during a
file load there is no valid context for that. It only removes the handler and
forgets the session; Blender rebuilds the window layout on its own.
"""

import bpy
from bpy.app.handlers import persistent

from . import isolation
from . import session
from . import storage
from . import window as window_mod


def _drop_session():
    state = session.state
    if not state.is_open and state.draw_handle is None:
        return

    window_mod.close(window_already_gone=True)


@persistent
def _on_load_pre(*_args):
    _drop_session()


@persistent
def _on_load_post(*_args):
    # A file that was saved with a session open must not claim to still have
    # one; the window is gone either way.
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and hasattr(scene, "paintr2"):
        scene.paintr2.session_was_open = False

    # And a file saved mid-session opens with half the scene hidden and no
    # window to say why. The record of what was hidden is on the scene for
    # exactly this: there is no session here, so there is nothing for
    # the isolation to be *for*, and it is undone. Opening the window again
    # re-isolates in one step.
    if scene is not None and hasattr(scene, "paintr2"):
        if len(scene.paintr2.hidden_objects):
            isolation.restore(bpy.context)

    # Solo is a way of looking at a stack, not part of it. A file that
    # opens onto one grey layer, with the reason two windows away, is a bug
    # report; so the view goes back to the whole stack on load.
    for material in bpy.data.materials:
        document = getattr(material, "paintr2", None)
        if document is None or document.solo_mode == 'NONE':
            continue
        document.solo_clear()


@persistent
def _on_exit_pre(*_args):
    _drop_session()


@persistent
def _on_save_pre(*_args):
    """Painted layers only exist in memory until something writes them out."""
    storage.on_save_pre()


@persistent
def _on_save_post(*_args):
    storage.on_save_post()


_HANDLERS = (
    (bpy.app.handlers.load_pre, _on_load_pre),
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.exit_pre, _on_exit_pre),
    (bpy.app.handlers.save_pre, _on_save_pre),
    (bpy.app.handlers.save_post, _on_save_post),
)


def register():
    for handler_list, function in _HANDLERS:
        if function not in handler_list:
            handler_list.append(function)


def unregister():
    for handler_list, function in _HANDLERS:
        if function in handler_list:
            handler_list.remove(function)

# SPDX-License-Identifier: GPL-3.0-or-later
"""PaintR2 - layer-based texture painting inside Blender.

The problem this solves: Blender can only paint into one image at a time and
has no way to stack, blend and flatten paint layers into an exportable texture.
Node-based mixing gets you the look but not a file. PaintR2 adds a real layer
stack that composites down to an image you can export.

Architecture
------------
    properties.py   scene-level RNA settings, saved into the .blend
    document.py     the layer stack, hung off Material -- the source of truth
    procedural.py   generated layer sources and masks (gradient, and the seams
                    cut for the kinds that come after it)
    paintset.py     the Paint / Paint_high collections: which models the
                    session is painting, and the high-poly each bakes from
    isolation.py    hides everything outside the paint set while the window
                    is open, and fades the models not being worked on
    clipboard.py    copies layers between materials, images and all
    nodegen.py      compiles a layer stack into a shader node group,
                    in whichever of the four shader modes is picked
    lighting.py     the world PaintR2 lights the model with, and the
                    transform on the HDRI in it
    bake.py         bakes the compiled shader down to an exportable texture
    dilate.py       fills a baked image out past its UV islands, so nothing
                    filtered across a seam picks up black
    paint.py        aims Blender's texture paint at the active layer
    storage.py      keeps painted layers alive across saves (disk / pack)
    preferences.py  AddonPreferences
    prepare.py      "Set Models": the paint set, and every material slot in
                    it reset to a PaintR2 stack under its own name
    session.py      runtime-only state for the live window
    window.py       opening/dressing/tearing down the dedicated window
    handlers.py     load/exit handlers that drop stale sessions
    ops/            operators (open, close, the modal event loop)
    ui/             the native 3D Viewport sidebar panel
    gui/            the self-drawn GPU interface toolkit

Blender 5.2 constraints that shape this code:
    * A new editor (Space) type cannot be registered from Python, so the window
      borrows an existing editor and covers it with a draw handler.
    * bgl is gone; GPU state goes through gpu.state.*_set().
    * GLSL-string shaders are gone; only builtin shaders are used.
    * The 'TRI_FAN' batch type was removed, so shapes emit explicit triangles.
    * bpy.props data is never accessed dict-style (scene["paintr2"]).
"""

import bpy

from . import preferences
from . import properties
from . import document
from . import handlers
from . import meshmaps
from . import ops
from . import ui
from . import window
from .gui import draw as gui_draw


_MODULES = (
    preferences,
    properties,
    document,
    # Mesh maps hang off Object, not off Material, so they register on their
    # own rather than through document.py.
    meshmaps,
    ops,
    ui,
)


def register():
    for module in _MODULES:
        module.register()

    handlers.register()


def unregister():
    # Close before anything is torn down; the draw handler must not outlive
    # the classes it draws with.
    window.close()

    handlers.unregister()
    window.release_icon_textures()
    window.release_previews()
    gui_draw.reset_gpu_cache()

    for module in reversed(_MODULES):
        module.unregister()

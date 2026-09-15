# SPDX-License-Identifier: GPL-3.0-or-later
"""The brush picker: listing Blender's brush assets and activating one.

In Blender 5.x a brush is an asset, not a datablock you pick from a list. What
identifies the active one is a weak reference:

    tool_settings.image_paint.brush_asset_reference
        .asset_library_type        'ESSENTIALS'
        .asset_library_identifier  ''
        .relative_asset_identifier 'brushes/essentials_brushes-mesh_texture.blend/Brush/Paint Hard'

Blender's own picker is the asset shelf, which PaintR2 does not show, so
the list is read straight out of the essentials library and activation goes
through `brush.asset_activate` -- the same operator the shelf runs.

Reading the library means opening a .blend, so the result is cached: the
essentials library ships with Blender and cannot change while it is running.
"""

import os

import bpy


# The texture-paint brushes live in one file. Sculpt, weight and grease pencil
# have their own, and none of them belongs in a texture painting window.
_ESSENTIALS_BLEND = "brushes/essentials_brushes-mesh_texture.blend"

ESSENTIALS = 'ESSENTIALS'

# The other library a brush can come from: this file. A brush lands there when
# it has to hold a texture -- see `localize`.
LOCAL = 'LOCAL'

# The order Blender's shelf reads in: what you paint with, then what you erase
# with, then the rest. Anything the library gains that is not listed here
# still shows up, at the end, in alphabetical order.
_PREFERRED = (
    "Paint Hard",
    "Paint Hard Pressure",
    "Paint Soft",
    "Paint Soft Pressure",
    "Airbrush",
    "Paint Pixel Art",
    "Erase Hard",
    "Erase Hard Pressure",
    "Erase Soft",
    "Erase Pixel Art",
    "Fill",
    "Smear",
    "Blur",
    "Clone",
    "Mask",
)

_cache = None


def _library_path():
    return os.path.join(
        bpy.utils.system_resource('DATAFILES'), "assets", _ESSENTIALS_BLEND
    )


def _identifier(name):
    return f"{_ESSENTIALS_BLEND}/Brush/{name}"


def available():
    """[(relative_asset_identifier, display name)], in the order to show them.

    The essentials library first, in Blender's own order, then whatever this
    file holds of its own -- a brush becomes the file's own when a texture is
    put on it, and it has to be pickable again afterwards.

    The essentials half is cached; the local half cannot be, because it grows
    while Blender runs. Empty when neither can be read -- a custom Blender
    build, say. The caller shows an empty picker rather than crashing.
    """
    return _essentials() + _local()


def _local():
    """The texture paint brushes this file holds as assets, by name.

    `Brush/<name>` is what Blender calls a local asset: an ID type and a name,
    because the file it is relative to is this one. Nothing else is shaped
    like that, which is how `_library_type` tells the two libraries apart.
    """
    found = []
    for brush in bpy.data.brushes:
        if brush.library is not None or brush.asset_data is None:
            continue
        if not getattr(brush, "use_paint_image", False):
            continue
        found.append((f"Brush/{brush.name}", brush.name))

    found.sort(key=lambda pair: pair[1])
    return found


def _library_type(identifier):
    """Which library an identifier names, read off its shape.

    An essentials identifier names the .blend it lives in; a local one cannot,
    because the file it is relative to is the one already open. So the caller
    passes a name around and never a library type beside it.
    """
    return ESSENTIALS if ".blend/" in identifier else LOCAL


def _essentials():
    """The brushes Blender ships, in the order to show them. Cached."""
    global _cache
    if _cache is not None:
        return _cache

    path = _library_path()
    names = []
    if os.path.exists(path):
        try:
            with bpy.data.libraries.load(path, link=True, assets_only=True) as (src, _dst):
                names = list(src.brushes)
        except (OSError, RuntimeError):
            names = []

    order = {name: index for index, name in enumerate(_PREFERRED)}
    names.sort(key=lambda name: (order.get(name, len(order)), name))

    _cache = [(_identifier(name), name) for name in names]
    return _cache


def active_identifier(context=None):
    """The `relative_asset_identifier` of the brush in use, or ""."""
    context = context or bpy.context
    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None:
        return ""

    reference = getattr(tool_settings.image_paint, "brush_asset_reference", None)
    if reference is None:
        return ""
    return getattr(reference, "relative_asset_identifier", "") or ""


# Texture paint, as `context.mode` spells it. Not `'TEXTURE_PAINT'` -- that is
# the string `object.mode_set` takes, and the two differ.
PAINT_MODE = 'PAINT_TEXTURE'


def can_activate(context=None):
    """Whether it is safe to switch brushes at all.

    `brush.asset_activate` SEGFAULTS outside a paint mode -- not an exception,
    a crash, measured in 5.2 background and reproducible: in Object mode it
    warns `Tool 'builtin.brush' not found for space 'VIEW_3D'` and dies. It has
    to reach the tool system to set the active tool, and outside paint mode
    there is nothing there to reach.

    So mode is checked before the operator runs, every time.
    """
    context = context or bpy.context
    return getattr(context, "mode", None) == PAINT_MODE


def activate(identifier, context=None):
    """Make the brush named by `identifier` the active one.

    Returns True when Blender reports the switch happened. Anything else --
    an identifier from a stale cache, a library that moved, the wrong mode --
    leaves the current brush alone rather than crashing or clearing it.
    """
    if not identifier:
        return False

    if not can_activate(context):
        return False

    try:
        result = bpy.ops.brush.asset_activate(
            asset_library_type=_library_type(identifier),
            asset_library_identifier="",
            relative_asset_identifier=identifier,
        )
    except RuntimeError:
        return False

    return 'FINISHED' in result


def localize(context=None):
    """Make the active brush this file's own, and go on painting with it.

    Blender's essentials brushes are linked, and a linked datablock may not
    point at a local one -- so a texture cannot be put on one. The write does
    not fail, it does nothing, and Blender's own Texture panel passes that on
    unchanged. This is the way out of it: the brush is copied into the
    file, marked as an asset and activated from the local library. It keeps
    every setting it had, it shows up in the picker beside the essentials
    brushes, and it can hold a texture.

    Safe to call before any assignment: a brush that is already local comes
    back unchanged. Returns None when there is nothing to localize, or when
    the copy could not be made active -- in which case the copy is thrown
    away rather than left behind as a brush nobody asked for.
    """
    from . import paint

    context = context or bpy.context

    brush = paint.active_brush(context)
    if brush is None:
        return None
    if brush.library is None:
        return brush
    if not can_activate(context):
        return None

    local = brush.copy()
    # Named for the reason it exists. The picker shows it next to the brush it
    # came from, and two rows reading "Paint Hard" would name nothing.
    local.name = f"{brush.name} (textured)"
    local.asset_mark()

    if not activate(f"Brush/{local.name}", context):
        print("[PaintR2] could not activate the local copy of "
              f"{brush.name}; leaving the linked brush in place")
        bpy.data.brushes.remove(local)
        return None

    return paint.active_brush(context)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Painting into the active layer.

Blender's Texture Paint has two target modes. `'MATERIAL'` hunts for the
active Image Texture node of the material; `'IMAGE'` paints into a canvas you
hand it directly. PaintR2 uses `'IMAGE'`, and has to: its layer images live
inside a node group, where `'MATERIAL'` mode cannot reach them.

So the whole job of this module is keeping one thing true --

    tool_settings.image_paint.canvas == the active PAINT layer's image

-- whenever the active layer, the active material or the active object
changes. `document.py` calls `sync_canvas` from its update hooks.

Blender 5.2 note: `unified_paint_settings` moved off `ToolSettings` and onto
`ImagePaint`, and brushes are assets now (`Paint Hard` is the default).
"""

import bpy


PAINT_MODE = 'TEXTURE_PAINT'


def active_document(context=None):
    """The layer stack of the active object's active material slot."""
    context = context or bpy.context

    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None

    material = obj.active_material
    if material is None or not hasattr(material, "paintr2"):
        return None

    return material.paintr2


def active_paint_image(context=None):
    """The image of the active layer, if that layer can be painted into.

    A layer has two paintable images of its own -- its content and its mask --
    and `active_part` decides which one the brush writes into. That is why a
    Fill layer becomes paintable the moment it gets a mask.

    A **paint effect** is a third, and it wins while it is the selected one:
    it is an image inside one half of the layer, and selecting it is what says
    the brush goes there.
    """
    document = active_document(context)
    if document is None:
        return None

    layer = document.active_layer
    if layer is None:
        return None

    # The selected effect comes first when it is one that can be painted --
    # see `PR2_Layer.paintable_image`.
    return layer.paintable_image(document.active_effect)


def sync_canvas(context=None):
    """Point Blender's paint canvas at the active PAINT layer.

    Returns the image now being painted into, or None when there is nothing
    paintable (no document, or the active layer is a FILL layer).
    """
    context = context or bpy.context

    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None:
        return None

    image = active_paint_image(context)
    if image is None:
        return None

    image_paint = tool_settings.image_paint
    if image_paint.mode != 'IMAGE':
        image_paint.mode = 'IMAGE'
    if image_paint.canvas != image:
        image_paint.canvas = image

    return image


def is_painting(context=None):
    context = context or bpy.context
    obj = context.active_object
    return obj is not None and obj.mode == PAINT_MODE


def enter(context=None):
    """Switch to Texture Paint and aim it at the active layer.

    Returns (image, error_message).
    """
    context = context or bpy.context

    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None, "Select a mesh object"

    if not obj.data.uv_layers:
        return None, f"{obj.name} has no UV map - unwrap it first"

    document = active_document(context)
    if document is None or not len(document.layers):
        return None, "No PaintR2 stack here - run Set Models first"

    image = active_paint_image(context)
    if image is None:
        return None, "Nothing to paint into - add a mask, or pick a paint layer"

    if obj.mode != PAINT_MODE:
        try:
            bpy.ops.object.mode_set(mode=PAINT_MODE)
        except RuntimeError as error:
            return None, str(error)

    sync_canvas(context)
    return image, None


def leave(context=None):
    context = context or bpy.context

    obj = context.active_object
    if obj is not None and obj.mode == PAINT_MODE:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError as error:
            return str(error)
    return None


def auto_sync_mode(context=None):
    """Follow the active layer in and out of Texture Paint.

    Selecting something paintable -- a paint layer, any layer's mask, or a
    paint effect inside either -- should just let you paint; selecting a plain
    Fill layer should not leave the brush armed over nothing. So the mode follows the selection rather
    than being a button the user has to remember.

    Returns the image now being painted into, or None.
    """
    context = context or bpy.context

    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None

    image = active_paint_image(context)
    painting = obj.mode == PAINT_MODE

    if image is not None:
        if not painting:
            if not obj.data.uv_layers:
                return None  # texture paint would be meaningless without UVs
            try:
                bpy.ops.object.mode_set(mode=PAINT_MODE)
            except RuntimeError:
                return None
        sync_canvas(context)
        return image

    if painting:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass

    return None


def set_brush_cursor(visible, context=None):
    """Show or hide Blender's brush circle.

    Hiding it while the pointer is over PaintR2's own panels matters for more
    than looks: the brush cursor invalidates the region on every mouse move,
    and because the interface shares one region with the viewport, that means
    re-rendering the whole 3D scene just to move the pointer over a list.
    """
    context = context or bpy.context

    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None:
        return

    image_paint = tool_settings.image_paint
    if image_paint.show_brush != visible:
        image_paint.show_brush = visible


def active_brush(context=None):
    context = context or bpy.context
    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None:
        return None
    return tool_settings.image_paint.brush


# --- brush values ------------------------------------------------------------
#
# Unified paint settings override the brush. Measured defaults in 5.2:
#
#     use_unified_color    = True     -> brush.color is ignored
#     use_unified_size     = True     -> brush.size is ignored
#     use_unified_strength = False    -> brush.strength is used
#
# So `brush.color = ...` is silently a no-op on a fresh file, which is exactly
# why PaintR2's first brush controls did nothing. Blender's own UI solves this
# with `UnifiedPaintPanel.prop_unified`, which picks the owner per property;
# `brush_owner` is the same rule.

# Property name -> the toggle that hands it to the unified settings instead.
_UNIFIED_TOGGLES = {
    "color": "use_unified_color",
    "secondary_color": "use_unified_color",
    "size": "use_unified_size",
    "unprojected_size": "use_unified_size",
    "strength": "use_unified_strength",
    "weight": "use_unified_weight",
    "input_samples": "use_unified_input_samples",
    "use_color_jitter": "use_unified_color",
    "hue_jitter": "use_unified_color",
    "saturation_jitter": "use_unified_color",
    "value_jitter": "use_unified_color",
}


def unified_settings(context=None):
    """`ImagePaint.unified_paint_settings` -- moved here in 5.2, off ToolSettings."""
    context = context or bpy.context
    tool_settings = getattr(context.scene, "tool_settings", None)
    if tool_settings is None:
        return None
    return getattr(tool_settings.image_paint, "unified_paint_settings", None)


def brush_owner(prop, context=None):
    """Which datablock actually holds `prop` right now: unified settings or brush.

    Read *and* write through this. Reading the wrong one shows a value the
    stroke will not use; writing the wrong one changes nothing at all.
    """
    context = context or bpy.context
    brush = active_brush(context)

    toggle = _UNIFIED_TOGGLES.get(prop)
    if toggle is None:
        return brush

    settings = unified_settings(context)
    if settings is None:
        return brush

    if getattr(settings, toggle, False):
        return settings
    return brush


def brush_get(prop, default=None, context=None):
    owner = brush_owner(prop, context)
    if owner is None:
        return default
    return getattr(owner, prop, default)


def brush_set(prop, value, context=None):
    owner = brush_owner(prop, context)
    if owner is None:
        return
    try:
        setattr(owner, prop, value)
    except (AttributeError, TypeError, ValueError):
        pass

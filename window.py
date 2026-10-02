# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening, dressing and tearing down the dedicated PaintR2 window.

Why it works this way
---------------------
Blender's Python API cannot register a new editor type, and there is no
"blank window" to ask for. Two operators come close:

* `wm.window_new` pops the active area into a window with no topbar at all --
  visually perfect, but the result is a *child/temp* window (`window.parent`
  is set, its screen is named "temp"). The OS treats it as a utility window:
  no maximize button, odd title-bar dragging, and pop-up menus opened inside
  it can close it outright.
* `wm.window_new_main` gives a real, independent top-level window. It clones
  the whole workspace layout, so the extra editors have to be closed, and
  Blender's topbar and status bar strips stay visible. In exchange the window
  behaves like every other window on the desktop.

PaintR2 uses `window_new_main`. The window is then:

    1. reduced to a single area,
    2. switched to a 3D Viewport,
    3. stripped of the regions, gizmos and overlays that would show through --
       but NOT of the tool header or the asset shelf, which are Blender's
       brush strip and brush picker and are kept as they are,
    4. given a 'POST_PIXEL' draw handler that paints PaintR2's panels on top.

The panels do not cover the middle of the area, so the live viewport shows
through there -- that is the canvas. Painting, navigation and selection are
Blender's own, running in a real View3D region; PaintR2 only owns the chrome
around them.

The modal operator in ops/op_modal.py routes events: over a panel it consumes
them, over the viewport it passes them straight through to Blender.

Teardown has to survive the user closing the window with the OS button, which
gives us no callback -- hence the watchdog timer.
"""

import math
import os
import time
import traceback

import bpy
import gpu
import numpy as np

from . import isolation
from . import lighting
from . import nodegen
from . import preferences
from . import session
from .gui import icons
from .gui import theme


# The host is always a 3D Viewport: painting happens through Blender's own
# paint operators, which need a real View3D region with a real RegionView3D.
# An offscreen render would look the same but would carry none of that -- no
# navigation, no brush engine, no tool system.
HOST_AREA_TYPE = 'VIEW_3D'
HOST_SPACE_CLASS_NAME = "SpaceView3D"

# Regions of the host editor that must be hidden, so the viewport fills the
# whole area and PaintR2 draws every piece of chrome itself.
#
# ALL of them. This is a rule, not a preference.
# Keeping any Blender region inside our window was tried three times and broke
# the interface three times: the tool header stacked a second dark strip on
# ours, the asset shelf ate the bottom of the canvas, and in Object mode the
# strip stayed there taking up room with nothing in it. Blender's regions do
# not meet our POST_PIXEL drawing at the same scale or in the same metrics,
# and their real sizes only exist in a real window, so nothing about them can
# be verified before shipping.
#
# Whatever those regions offered, PaintR2 draws itself and binds to the very
# same RNA property -- see gui/brush_panel.py.
#
# CAREFUL: these `show_region_*` properties TOGGLE, they do not assign.
# Writing False to an already-hidden region makes it visible again -- measured
# in 5.2: a fresh wm.window_new area has TOOL_HEADER hidden (1x1), and
# `show_region_tool_header = False` turned it into a visible 2432x26 strip.
# So each one is only written when its region is actually showing, and the
# region's real size is what decides that (the property getter lags a redraw
# behind).
_REGION_FLAGS = (
    ("show_region_header", 'HEADER'),
    ("show_region_tool_header", 'TOOL_HEADER'),
    ("show_region_toolbar", 'TOOLS'),
    ("show_region_ui", 'UI'),
    ("show_region_hud", 'HUD'),
    ("show_region_asset_shelf", 'ASSET_SHELF'),
)

# Viewport decorations that would end up buried under a PaintR2 panel.
_GIZMO_FLAGS = (
    "show_gizmo_navigate",
)

_OVERLAY_FLAGS = (
    "show_text",
    "show_stats",
)

_WATCHDOG_INTERVAL = 0.5

_draw_error_reported = False

# Fingerprint of the state the chrome mirrors but does not own; see
# `poll_external_changes`.
_external_signature = None


# --- host editor helpers ----------------------------------------------------


def _host_space_class():
    return getattr(bpy.types, HOST_SPACE_CLASS_NAME)


def _set_flags(owner, flags, value):
    """Set every flag that exists on `owner`, ignoring the ones that do not.

    Only safe for genuine boolean properties -- not for the `show_region_*`
    family, which toggles. See `_hide_regions`.
    """
    if owner is None:
        return
    for flag in flags:
        if hasattr(owner, flag):
            try:
                setattr(owner, flag, value)
            except (AttributeError, TypeError):
                pass


def _region_is_visible(region):
    """Blender reports a hidden region as 1x1 rather than 0x0."""
    return region.width > 1 and region.height > 1


# Region types already asked to go away, and not yet seen gone. One request
# per appearance is the whole rule -- see `_hide_regions`.
_hide_asked = set()

# Icon artwork read off disk: {name: Image} and {name: GPUTexture}. `None`
# rather than an empty dict means "not looked for yet" -- an empty one is a
# perfectly good answer, and it is what a folder with no PNGs in it gives.
_icon_images = {}
_icon_textures = None


def _hide_regions(area, space):
    """Ask every showing chrome region of `area` to go away. Once each.

    The "once each" is the load-bearing part. A region's size is what tells us
    whether it is up, and that size is stale for a while after the flag is
    written -- measured at up to half a second in a PaintR2 window, far longer
    than the 0.15 s tick this runs on. So a pass that simply re-tested the size
    would write the flag again while the first write was still in flight, and
    the second write undoes the first: the asset shelf slid up, down, up, down
    across the bottom of the canvas for as long as the window was open.

    Hence the latch. A region is asked once; nothing more is written until it
    has actually been seen gone, which also re-arms it for the next time
    Blender brings it back (a mode change does that). A write refused as
    read-only is not an ask -- the mode simply has no such region right now --
    so it is not latched and will be tried again when it becomes real.
    """
    by_type = {region.type: region for region in area.regions}

    for flag, region_type in _REGION_FLAGS:
        if not hasattr(space, flag):
            continue

        region = by_type.get(region_type)
        if region is None:
            continue

        if not _region_is_visible(region):
            _hide_asked.discard(region_type)
            continue

        if region_type in _hide_asked:
            continue

        # The flag agreeing that the region is up is the second half of the
        # guard: writing False to something already hidden is what goes
        # wrong, and the size alone is too slow to be trusted twice.
        try:
            if getattr(space, flag) is False:
                continue
            setattr(space, flag, False)
        except (AttributeError, TypeError):
            continue

        _hide_asked.add(region_type)


def _use_material_shading(space):
    """Make the host show the compiled stack rather than the paint canvas.

    Solid shading does NOT display the material while an object is in Texture
    Paint: Blender draws the *paint canvas* across the surface instead. That is
    normally a feature -- one image, painted directly. Here it is wrong, and
    badly so: the canvas is a single layer, so the whole model turns into
    whatever that one layer holds. A fresh paint layer is transparent, which
    reads as black, and only the pixels a brush has touched appear.

    Measured on 5.2 (white base layer, empty paint layer on top, same scene):

        shading SOLID     -> surface renders 0.047   (the empty canvas)
        shading MATERIAL  -> surface renders 1.0     (the compiled stack)

    RENDERED also shows the real shader, so a user who picked it keeps it.
    """
    shading = getattr(space, "shading", None)
    if shading is None:
        return

    if shading.type not in ('MATERIAL', 'RENDERED'):
        shading.type = 'MATERIAL'


def _dress_host(area, space):
    """Strip the host viewport down to bare pixels. Every region, no exception."""
    _hide_regions(area, space)
    _use_material_shading(space)
    _set_flags(space, _GIZMO_FLAGS, False)
    _set_flags(getattr(space, "overlay", None), _OVERLAY_FLAGS, False)


def keep_host_stripped():
    """Put back down any region Blender has raised since the window opened.

    Dressing the host once is not enough, and the asset shelf -- Blender's own
    brush picker -- is the proof. It is a mode region: in Object mode it does
    not exist, so at spawn `_hide_regions` measures it at 1x1 and rightly
    leaves it alone (writing to a hidden region *shows* it, see `_REGION_FLAGS`).
    The first time the object enters Texture Paint the shelf is laid out, and
    a strip of brush thumbnails with catalog tabs above it appears across the
    bottom of our canvas -- a piece of Blender interface inside the window,
    which is not allowed. The tool header comes back the same way.

    So the regions are re-checked on the modal timer instead of only at spawn.
    A pass is one dict of the area's regions and a size test each; once
    everything is down it writes no RNA at all, because the visibility guard
    stops it.

    Only the regions. Shading and overlays are left alone on purpose -- those
    are the user's to change while the window is open.
    """
    area = session.find_area()
    if area is None:
        return

    space = session.find_space(area)
    if space is None:
        return

    _hide_regions(area, space)


# --- draw handler -----------------------------------------------------------


def _draw_callback():
    """Fires for every editor of the host type; bails out unless it is ours."""
    global _draw_error_reported

    state = session.state
    if state.root is None:
        return

    context = bpy.context
    if not session.is_our_space(getattr(context, "space_data", None)):
        return

    region = getattr(context, "region", None)
    if region is None or region.width <= 0 or region.height <= 0:
        return

    try:
        prefs = preferences.get(context)
        state.root.debug = bool(prefs and prefs.debug_overlay)

        scene = getattr(context, "scene", None)
        ui_state = scene.paintr2.ui if scene is not None else None

        # Per frame, so switching Blender theme is visible immediately. It is
        # forty RNA reads against a viewport render; the cost is not the point.
        if theme.refresh(context):
            state.root.request_redraw()

        state.root.update(
            region.width,
            region.height,
            theme.scale(context),
            ui_state,
        )
        state.root.set_status(_status_text(context))

        if scene is not None and scene.paintr2.ui.show_targets_panel:
            objects, materials, active_object, active_material = \
                _target_rows(context)
        else:
            # Closed: no rows are built at all. Walking the collection and
            # every material slot on every redraw to fill a panel nobody can
            # see is the cost the switch exists to avoid.
            objects, materials, active_object, active_material = [], [], -1, -1

        state.root.sync_targets(
            objects, materials, active_object, active_material
        )

        from . import clipboard as clipboard_mod

        state.root.sync_clipboard(clipboard_mod.count())

        rows, active_index, active_info = _layer_rows(context)
        state.root.sync_layers(rows, active_index, active_info)
        state.root.sync_thumbnails(_thumbnails(context))
        _ensure_icon_textures()

        # Before either strip is synced: it decides which one is laid out.
        state.root.set_tool(active_tool(context))

        # A click on the model in the viewport changes the active object
        # without our panel being involved; the fade follows it here.
        _follow_active_object(context)

        state.root.sync_properties(_layer_panel_rows(context))
        state.root.sync_display(_display_rows(context))

        # Only while it is up. Building these walks the paint set and reads
        # every material's channels, and nothing shows the answer the rest of
        # the time -- the same rule the Objects panel already keeps.
        if state.root.export_dialog_open:
            footnote, can_export = _export_summary(context)
            state.root.sync_export_dialog(
                _export_dialog_rows(context), footnote, can_export
            )

        state.root.sync_brush(_brush_info(context))
        # Built once, shown twice: the strip's popovers and the panel beside
        # the layers are the same rows.
        brush_panels = _brush_panels(context)
        state.root.sync_brush_panels(brush_panels)
        state.root.sync_brush_properties(_brush_property_rows(brush_panels))
        state.root.sync_fill(_fill_info(context))

        state.root.sync_symmetry(
            _symmetry_flags(context), _show_symmetry_planes(context)
        )
        state.root.sync_wireframe(wireframe_shown())
        state.root.sync_shader_graph(shader_graph_is_open())
        state.root.set_symmetry_shapes(_symmetry_shapes(context))

        # Under the chrome, like the mirror planes: it belongs to the model.
        _draw_fill_preview(context)

        state.root.draw()
    except Exception:
        # A raising draw handler spams the console once per frame, so report
        # it once and shut the interface down rather than flood the log.
        if not _draw_error_reported:
            _draw_error_reported = True
            print("[PaintR2] draw handler failed, closing the window:")
            traceback.print_exc()
        bpy.app.timers.register(_deferred_close, first_interval=0.0)


# Which object the fade was last pointed at, as a pointer. Clicking a model
# in the viewport changes the active object without going through our panel,
# and the fade has to follow that too.
_fade_target = 0


def _follow_active_object(context):
    """Re-aim the fade when the active object has changed. Cheap when it has not.

    Called from the draw callback, so the comparison is what matters: it runs
    on every redraw and does nothing on all but the one where the selection
    moved. `sync_fade` itself only recompiles the materials whose answer
    flipped, and it goes through `request_compile`, so the rebuild happens on
    a timer rather than inside the draw.
    """
    global _fade_target

    obj = getattr(context, "active_object", None)
    pointer = obj.as_pointer() if obj is not None else 0
    if pointer == _fade_target:
        return

    _fade_target = pointer
    isolation.sync_fade(context)


# A message the status strip shows instead of the usual reading, and when it
# stops. This is the window's only way to answer back.
#
# An operator reports through `self.report`, which Blender shows in the status
# bar and the Info editor -- and PaintR2 has stripped every region of the host
# area, so both of those are one pixel wide. A bake wrote six textures and the
# only thing the user could see was ten seconds of a frozen window; "it does
# not work" was the correct reading of the evidence.
_notice = ("", 0.0)
NOTICE_SECONDS = 10.0


def notify(message, seconds=NOTICE_SECONDS):
    """Say something in the status strip for a while. Clears itself."""
    global _notice
    _notice = (str(message), time.monotonic() + float(seconds))
    session.tag_redraw()


def _status_text(context):
    """What the bottom strip says: the object and mode the viewport is in.

    Unless something has just been done and said so. A notice outranks the
    reading -- the object and its mode are visible on screen anyway, and the
    result of a command that took ten seconds is not.
    """
    message, until = _notice
    if message and time.monotonic() < until:
        return message

    obj = getattr(context, "active_object", None)
    if obj is None:
        return "No active object"

    mode = obj.mode.replace("_", " ").title()
    text = f"{obj.name}  -  {mode}"

    material = obj.active_material
    if material is not None:
        text += f"  |  {material.name}"

    return text


def _effect_rows(layer, layer_index, document, on_mask):
    """One row per effect on the selected half of `layer`.

    One half at a time, and only the selected one. Both stacks at once was
    two lists of identical-looking rows distinguished by a glyph, under a
    layer whose two halves were not separately selectable in the first place
    -- so there was no way to say which list a new filter would join except
    by asking, every time.

    Now the chip with the border round it says which half is being worked on,
    and this is that half's filters. Nothing selected shows nothing, which is
    the honest reading of "no filters here to look at".
    """
    from . import effects as effects_mod

    stack = effects_mod.visible_stack(layer, on_mask)
    active_effect = document.active_effect

    out = []
    for position, effect in enumerate(stack):
        out.append({
            "type": 'EFFECT',
            # The row still carries its owner's stack index, so everything
            # that walks rows -- selection, drag, the group tree -- reads
            # an effect row as belonging to that layer.
            "index": layer_index,
            "effect": position,
            "on_mask": on_mask,
            "depth": layer.depth + 1,
            "name": effect.label(),
            "selected": False,
            "expanded": False,
            "visible": effect.enabled,
            "blend": effect.blend_mode,
            "opacity": effect.opacity,
            "scalar_only": on_mask,
            "color": None,
            "kind": (effect.pattern.kind
                     if effect.effect_type == 'PATTERN' else None),
            "effect_type": effect.effect_type,
            "has_mask": False,
            "mask_procedural": False,
            "mask_on": True,
            "target": 'NONE',
            "tag": 'NONE',
            "solo": "",
            "soloed_elsewhere": False,
            "color_effects": 0,
            "mask_effects": 0,
            "active_effect": position == active_effect,
        })
    return out


def _layer_rows(context):
    """Snapshot the active stack as plain dicts for the interface.

    Plain data on purpose: RNA wrappers are transient, so nothing that
    outlives this call may hold one. Returns (rows, active_index, active_info).
    """
    from . import document as doc
    from . import effects as effects_mod
    from . import paint

    document = paint.active_document(context)
    if document is None:
        return [], -1, None

    # A collapsed group's contents are skipped, so a row's position in this
    # list is NOT its position in the stack any more -- every row carries the
    # index it came from and the interface hands that number back.
    solo_mode, solo_index = document.solo_target()

    rows = []
    index = 0
    while index < len(document.layers):
        layer = document.layers[index]
        written = layer.channel_ids()
        rows.append(
            {
                "index": index,
                "name": layer.name,
                "selected": layer.selected,
                "type": layer.layer_type,
                # Only meaningful on a PROCEDURAL row: which pattern it is, so
                # the chip can say a ramp and a node group apart.
                "kind": layer.procedural.kind
                if layer.layer_type == 'PROCEDURAL' else None,
                "depth": layer.depth,
                "expanded": layer.expanded,
                # How many filters each half carries. Drawn as a thin mark
                # under the chip -- the only way to see that the *other* half
                # has filters on it without selecting it and looking.
                "color_effects": len(layer.effects),
                "mask_effects": len(effects_mod.visible_stack(layer, True)),
                "visible": layer.visible,
                "blend": layer.blend_mode,
                # True when every channel this layer writes to is a single
                # number, so the blend menu can drop the modes that would
                # quietly do nothing.
                "scalar_only": bool(written) and not any(
                    doc.channel_kind(c) == 'COLOR' for c in written
                ),
                "opacity": layer.opacity,
                "color": tuple(layer.channel('BASE_COLOR').color)
                if layer.layer_type == 'FILL'
                and layer.writes('BASE_COLOR') else None,
                "has_mask": layer.has_mask,
                # A painted mask specifically: only that one is a file the
                # row menu can offer to write out.
                "paint_mask": layer.mask_image is not None,
                "mask_procedural": layer.mask_procedural.enabled,
                "mask_on": layer.mask_enabled,
                # Which half of this layer is selected, if it is the active
                # one. A layer that is not active has no selected half: the
                # panel edits one layer, and two bordered chips on two
                # different rows would say otherwise.
                "target": (layer.active_part if index == document.active_index
                           else 'NONE'),
                "tag": layer.tag_color,
                # 'LAYER', 'MASK' or "" -- what is being looked at on this row.
                # Every other row draws dimmed while one of them says
                # something, or a solo is a mystery the moment the pointer
                # leaves the row that caused it.
                "solo": solo_mode if index == solo_index else "",
                "soloed_elsewhere": solo_mode != 'NONE' and index != solo_index,
            }
        )

        # The selected half's filters, as rows of their own under the layer.
        # Only for the active layer, and only for the half that is selected:
        # the filters shown are the filters of the thing being worked on, and
        # nothing else is on screen to be confused with them.
        #
        # This is also what replaced the twisty. A stack that opens and closes
        # by itself as the selection moves needs no second control saying the
        # same thing, and a twenty-layer document never shows more than one
        # layer's filters at a time.
        if index == document.active_index and layer.active_part != 'NONE':
            rows.extend(
                _effect_rows(layer, index, document,
                             layer.active_part == 'MASK')
            )

        if layer.layer_type == 'GROUP' and not layer.expanded:
            index = document.subtree_end(index)
        else:
            index += 1

    active_layer = document.active_layer
    active_info = None
    if active_layer is not None:
        # `has_mask` is either kind; the menus need to know which, because the
        # two can be added, removed and inverted independently.
        active_info = {
            "has_mask": active_layer.has_mask,
            "paint_mask": active_layer.mask_image is not None,
            "proc_mask": active_layer.mask_procedural.enabled,
            "mask_on": active_layer.mask_enabled,
            "is_group": active_layer.layer_type == 'GROUP',
            "has_children": document.subtree_end(document.active_index)
            > document.active_index + 1,
            "selected_count": len(document.selected_indices()),
            # Which half is selected, so the Effect button can say whether it
            # has anywhere to put what it would add.
            "part": active_layer.active_part,
            "solo": solo_mode,
        }

    # Which row the list draws as *the* active one. An effect being edited
    # makes that the effect's row, not its owner's: they were both highlighted
    # before, and two highlighted rows is two answers to "what am I editing".
    # The owner keeps its half-strength selected wash, which reads as "the one
    # this belongs to" rather than as a second selection.
    active_row = -1
    effect_row = -1
    for position, row in enumerate(rows):
        if row["index"] != document.active_index:
            continue
        if row.get("type") == 'EFFECT':
            if row.get("active_effect"):
                effect_row = position
        elif active_row < 0:
            active_row = position

    if effect_row >= 0:
        active_row = effect_row

    return rows, active_row, active_info


def _target_rows(context):
    """The Objects / Materials panel's two lists, as plain dicts.

    Both read Blender's own state: the models are what is in the `Paint`
    collection, the materials are the active object's slots, and which one of
    each is active is the view layer's active object and that object's active
    material index. Nothing here is a second record of anything.
    """
    from . import paintset
    from . import prepare

    active = getattr(context, "active_object", None)

    objects = []
    active_object = -1
    for index, obj in enumerate(paintset.paint_objects()):
        if obj is active:
            active_object = index
        objects.append({
            "name": obj.name,
            "materials": len(obj.material_slots),
            "no_uv": not obj.data.uv_layers,
        })

    materials = []
    active_material = -1
    if active is not None and active.type == 'MESH':
        for index, slot in enumerate(active.material_slots):
            material = slot.material
            if index == active.active_material_index:
                active_material = len(materials)

            if material is None:
                materials.append({
                    "name": f"<empty slot {index + 1}>",
                    "slot": index,
                    "layers": 0,
                    "prepared": False,
                    "color": None,
                })
                continue

            document = material.paintr2
            materials.append({
                "name": material.name,
                "slot": index,
                "layers": len(document.layers),
                "prepared": prepare.is_paint_material(material),
                "color": tuple(material.diffuse_color),
                "bake": document.bake_enabled,
            })

    return objects, materials, active_object, active_material


def _select_object(name):
    _run(f"select {name}", bpy.ops.paintr2.select_object, name=name)


def _select_material(slot):
    _run(f"select material slot {slot}",
         bpy.ops.paintr2.select_material, index=slot)


def _toggle_material_bake(slot):
    """Tick or untick a material for the mesh map bake."""
    obj = getattr(bpy.context, "active_object", None)
    if obj is None or not 0 <= slot < len(obj.material_slots):
        return
    material = obj.material_slots[slot].material
    if material is None:
        return
    document = material.paintr2
    document.bake_enabled = not document.bake_enabled
    bpy.ops.ed.undo_push(message="Toggle Material Bake")
    session.tag_redraw()


def _copy_layers():
    """True when something was copied, so an empty stack lets Ctrl C past."""
    from . import clipboard

    if not bpy.ops.paintr2.layer_copy.poll():
        return False
    _run("copy layers", bpy.ops.paintr2.layer_copy)
    return clipboard.has_content()


def _paste_layers():
    """True when something was pasted."""
    if not bpy.ops.paintr2.layer_paste.poll():
        return False
    _run("paste layers", bpy.ops.paintr2.layer_paste)
    return True


def _procedural_block(owner, proc, label, context, with_color):
    """One collapsible group of a procedural's parameters.

    Built from `procedural.param_names`, so a new kind of pattern shows up
    here without this function being touched -- the panel is the parameter
    list, in order, described straight off the RNA definitions.
    """
    from . import procedural

    rows = []

    # Only worth a row once there is a choice to make. With one kind it would
    # be a dropdown that can only say what it already says.
    if len(procedural.KINDS) > 1:
        rows.append(_row(owner, "kind", context=context))
        rows.append(_separator())

    for name in procedural.param_names(proc, with_color=with_color):
        rows.append(_row(owner, name, context=context))

    # A kind with no parameters is not a kind with nothing to edit: it is one
    # whose editor is the shader graph. The button is what says so, and it is
    # the only thing in this panel that is not read off an RNA property --
    # because what it opens is an editor, not a value.
    if procedural.edits_in_graph(proc.kind):
        rows.append(_separator())
        rows.append({
            "kind": 'button',
            "label": "Edit Nodes",
            "action": f"edit_{owner}",
            "desc": "Open this group in the shader graph beside the canvas",
            "active": proc.node_tree is not None,
        })

    rows = _compact(rows)
    if not rows:
        return None

    return {
        "kind": 'panel',
        "key": owner,
        "label": label,
        "open": True,
        "rows": rows,
    }


def _channel_rows(context):
    """The Channels block: what the active layer writes to, and with what.

    One row per channel the *document* carries (a channel the material has not
    got is not a choice the layer has to make). The row's toggle is the
    layer's own switch; its value is dimmed rather than hidden while the
    toggle is off, because the value survives being switched off and coming
    back is the whole point.

    Two channels have no value to show and say so instead:

    * a PAINT layer's Base Color -- that comes from the image, not a swatch;
    * any channel of a Custom pattern -- that comes from the node group.

    Both are drawn as the plain checkbox their state actually is.
    """
    from . import document as doc
    from . import paint
    from . import procedural

    document = paint.active_document(context)
    layer = document.active_layer if document is not None else None
    if layer is None or layer.layer_type == 'GROUP':
        return []

    # An adjustment layer has no channel of its own to set. What it does to a
    # channel is decided by which adjustment it is, and the block below this
    # one already names the channels its kind leaves alone -- so a Base Color
    # swatch and a Roughness slider here were five controls that changed
    # nothing. See `Layer.writes`.
    if layer.layer_type == 'ADJUSTMENT':
        return []

    if len(document.selected_indices()) > 1:
        return []

    from_nodes = (
        layer.layer_type == 'PROCEDURAL'
        and procedural.edits_in_graph(layer.procedural.kind)
    )

    rows = []
    for identifier in document.active_channels():
        owner = f"ch:{identifier}"
        label = doc.channel_label(identifier)
        enabled = layer.writes(identifier)

        painted = identifier == 'BASE_COLOR' and layer.layer_type == 'PAINT'
        if painted or from_nodes:
            note = "painted" if painted else "from nodes"
            rows.append(_row(owner, "enabled", text=f"{label}  ({note})",
                             context=context))
            continue

        key = "color" if doc.channel_kind(identifier) == 'COLOR' else "value"
        rows.append(_row(
            owner, key, label=label, active=enabled, slider=True,
            context=context,
            suffix=_compact([
                _toggle(owner, "enabled", 'EYE', context, alt_icon='EYE_OFF'),
            ]),
        ))

    rows = _compact(rows)
    if not rows:
        return []

    return [{
        "kind": 'panel',
        "key": "channels",
        "label": "Channels",
        "open": True,
        "rows": rows,
    }]


def _adjustment_rows(context):
    """The Adjustment block: which kind, and its parameters.

    Built the same way the pattern block is: a kind, then the names in
    `ADJUSTMENT_PARAMS` in order, each row described off its RNA definition.
    Adding a kind adds no layout here.
    """
    from . import adjustment
    from . import paint

    document = paint.active_document(context)
    layer = document.active_layer if document is not None else None
    if layer is None or layer.layer_type != 'ADJUSTMENT':
        return []

    if len(document.selected_indices()) > 1:
        return []

    rows = [_row('adjust', "kind", context=context)]

    names = adjustment.param_names(layer.adjustment)
    if names:
        rows.append(_separator())
    for name in names:
        rows.append(_row('adjust', name, context=context))

    # A kind that only means something for a colour says so rather than
    # leaving the user to wonder why the roughness never moved.
    if layer.adjustment.kind in adjustment.COLOR_ONLY:
        skipped = [
            doc_label
            for identifier in document.active_channels()
            for doc_label in (_channel_label(identifier),)
            if layer.writes(identifier)
            and not adjustment.applies_to(layer.adjustment.kind, identifier)
        ]
        if skipped:
            rows.append(_separator())
            rows.append({
                "kind": 'label',
                "label": "No effect on " + ", ".join(skipped),
            })

    rows = _compact(rows)
    if not rows:
        return []

    return [{
        "kind": 'panel',
        "key": "adjustment",
        "label": adjustment.kind_label(layer.adjustment.kind),
        "open": True,
        "rows": rows,
    }]


def _channel_label(identifier):
    from . import document as doc

    return doc.channel_label(identifier)


def _layer_panel_rows(context):
    """Everything the section under the layer list shows, in order.

    Channels first: what a layer *is* comes before how its pattern is shaped.
    """
    # An effect being selected takes the section over: it is what was just
    # clicked, and showing the layer's channels instead would be answering a
    # question nobody asked.
    effect_rows = _effect_property_rows(context)
    if effect_rows:
        return _compact(effect_rows)

    rows = list(_channel_rows(context))

    for block in (_adjustment_rows(context), _procedural_rows(context)):
        if rows and block:
            rows.append(_separator())
        rows.extend(block)

    return _compact(rows)


def _effect_property_rows(context):
    """The selected effect's parameters, or [] when none is selected.

    Built the same way everything else in this panel is: the parameter names
    come from the registry, the ranges and tooltips off the RNA
    definitions. A filter added to `adjustment.KINDS` shows up here without
    this function being touched.
    """
    from . import adjustment
    from . import paint
    from . import procedural

    document = paint.active_document(context)
    if document is None or len(document.selected_indices()) != 1:
        return []

    effect = document.active_effect_item()
    if effect is None:
        return []

    where = "Mask" if document.active_on_mask else "Color"
    rows = [{
        "kind": 'label',
        "label": f"{effect.label()}  --  on the {where}",
    }, _separator()]

    if effect.effect_type == 'PAINT':
        # There is nothing to set. What a paint effect becomes is painted, and
        # saying so beats an empty panel that reads as a missing one.
        rows.append({
            "kind": 'label',
            "label": "Paint into it with the brush",
            "active": False,
        })
        if effect.image is None:
            rows.append({"kind": 'label', "label": "No image", "active": False})
        return rows

    if effect.effect_type == 'FILTER':
        rows.append(_row('effect_filter', "kind", label="Filter", context=context))
        for name in adjustment.PARAMS.get(effect.filter.kind, ()):
            rows.append(_row('effect_filter', name, context=context))
    else:
        rows.append(_row('effect_pattern', "kind", label="Pattern",
                         context=context))
        if procedural.edits_in_graph(effect.pattern.kind):
            rows.append({
                "kind": 'button',
                "label": "Edit in the Shader Graph",
                "desc": "Open the node editor on this pattern's group",
                "action": ('effect_graph',),
            })
        else:
            for name in procedural.PARAMS.get(effect.pattern.kind, ()):
                rows.append(_row('effect_pattern', name, context=context))
            for name in procedural.COLOR_PARAMS.get(effect.pattern.kind, ()):
                rows.append(_row('effect_pattern', name, context=context))

    # Blend, opacity and Remove are deliberately *not* here. Each of them
    # already has a place the user reaches for first -- the row's own chips,
    # and the trash button -- and a second copy in a properties panel is a
    # second thing to keep in sync for no new ability. What is left is what
    # only this panel can say: the effect's own parameters.
    return rows


def _procedural_rows(context):
    """The properties section under the layer list.

    Empty unless the active layer has something generated about it: the
    pattern it draws, the pattern masking it, or both at once -- and empty
    while several layers are selected, because then there is no answer to
    "which one would this edit".
    """
    from . import paint
    from . import procedural

    document = paint.active_document(context)
    layer = document.active_layer if document is not None else None
    if layer is None:
        return []

    if len(document.selected_indices()) > 1:
        return []

    rows = []

    if layer.layer_type == 'PROCEDURAL':
        block = _procedural_block(
            'proc', layer.procedural,
            procedural.kind_label(layer.procedural.kind),
            context, with_color=True,
        )
        if block is not None:
            rows.append(block)

    if layer.mask_procedural.enabled:
        if rows:
            rows.append(_separator())
        block = _procedural_block(
            'proc_mask', layer.mask_procedural,
            f"{procedural.kind_label(layer.mask_procedural.kind)} Mask",
            context, with_color=False,
        )
        if block is not None:
            rows.append(block)

    return rows


# --- icons from files -------------------------------------------------------
#
# Icons started as vector drawings; the rule now is "a file wins, the vector
# is the fallback". This is the reading half of it. `gui/icons.py` never opens a
# datablock -- it holds a name-to-texture dict that this module fills, the same
# arrangement as the layer thumbnails.
#
# A name with no file keeps its drawn version, so the set can be replaced one
# icon at a time and a half-finished folder is a working add-on.


def _packaged_icon_folder():
    return os.path.join(os.path.dirname(__file__), "icons")


def icon_folders():
    """Where icons are looked for, in order. The user's folder wins.

    Two folders rather than one so a folder holding two redrawn icons is a
    working set: everything it does not carry comes from the package, and
    everything neither carries is drawn.
    """
    folders = []

    from . import preferences

    prefs = preferences.get()
    chosen = (getattr(prefs, "icon_folder", "") or "").strip()
    if chosen:
        path = bpy.path.abspath(chosen)
        if os.path.isdir(path):
            folders.append(path)

    folders.append(_packaged_icon_folder())
    return folders


def _build_icon_textures():
    """Load every icon PNG that exists and hand the textures to the toolkit.

    Called from the draw handler and only there: `gpu.texture.from_image`
    **segfaults** without a GPU context, which is the same trap the layer
    thumbnails go through `_texture_for` for.

    The images are reloaded as they are picked up, so the development loop is
    "redraw the PNG, restart the add-on, look at it" -- without that,
    `check_existing=True` would hand back the datablock holding the old
    picture for as long as the file stayed open.
    """
    global _icon_textures

    _icon_textures = {}
    if bpy.app.background:
        return _icon_textures

    folders = icon_folders()
    for name in icons.names():
        path = next(
            (candidate for candidate in
             (os.path.join(folder, f"{name}.png") for folder in folders)
             if os.path.exists(candidate)),
            None,
        )
        if path is None:
            continue

        image = _icon_images.get(name)
        if image is None:
            try:
                image = bpy.data.images.load(path, check_existing=True)
            except RuntimeError as error:
                print(f"[PaintR2] could not load icon {name}: {error}")
                continue
            image.reload()
            _icon_images[name] = image

        texture = _texture_for(image)
        if texture is not None:
            _icon_textures[name] = texture

    icons.set_textures(_icon_textures)
    return _icon_textures


def _ensure_icon_textures():
    if _icon_textures is None:
        _build_icon_textures()


def release_icon_textures():
    """Drop the textures and forget the images. Called on unregister.

    The image datablocks are left in the file. `check_existing=True` may well
    have handed back one somebody else opened, and removing that would take an
    icon out of their hands to save a few kilobytes.
    """
    global _icon_textures

    _icon_textures = None
    _icon_images.clear()
    icons.set_textures({})


def _texture_for(image):
    """A GPUTexture for `image`, or None.

    `gpu.texture.from_image` shares memory with Blender and caches after the
    first call, so this is cheap enough to do every redraw -- and it means
    thumbnails follow brush strokes without any invalidation logic.

    DANGER: with no GPU context this does not raise, it **segfaults**. Blender
    dies outright, taking unsaved work with it. The background check is not
    optional, and this must only ever be called from inside a draw handler.
    """
    if image is None or bpy.app.background:
        return None

    try:
        return gpu.texture.from_image(image)
    except (RuntimeError, SystemError, ValueError):
        return None


def _thumbnails(context):
    """{layer index: (content texture, mask texture)} for the active stack.

    Keyed by the layer's own index, not by its position on screen: a collapsed
    group means the two stop matching, and the row carries the index that does.
    """
    from . import paint

    document = paint.active_document(context)
    if document is None:
        return {}

    thumbnails = {}
    for index, layer in enumerate(document.layers):
        content = _texture_for(layer.image) if layer.layer_type == 'PAINT' else None
        thumbnails[index] = (content, _texture_for(layer.mask_image))

    return thumbnails


# Texture paint mirrors on the MESH's flags, not on the paint settings'.
#
# `ImagePaint.use_symmetry_x` exists and is writable -- every `Paint` struct
# carries it -- but nothing in texture paint reads it, and Blender's own
# interface never shows it there. Measured in its UI source:
#
#     space_view3d.py:177          PAINT_TEXTURE -> ob.use_mesh_mirror_x/y/z
#     space_view3d_toolbar.py:1330 Symmetry panel -> mesh.use_mirror_x/y/z
#
# `Object.use_mesh_mirror_*` is an alias onto `Mesh.use_mirror_*`; writing
# either moves the other. Going through the object is what Blender's own tool
# header does. This being per mesh rather than per scene also means the state
# belongs to the object being painted, which is why the buttons re-read it
# whenever the active object changes.
_MIRROR_PROPERTIES = {'X': "use_mesh_mirror_x", 'Y': "use_mesh_mirror_y",
                      'Z': "use_mesh_mirror_z"}


def _mirror_object(context=None):
    """The object whose mirror flags texture paint obeys, or None."""
    context = context or bpy.context
    obj = getattr(context, "active_object", None)
    if obj is None or obj.type != 'MESH':
        return None
    return obj


def _symmetry_flags(context):
    """{axis: bool} for the header's mirror buttons, or None if unreadable.

    PaintR2 keeps no copy of this: the buttons show what the mesh holds, and
    mirroring is done by Blender's own stroke code, around the object's origin
    along its local axes.
    """
    obj = _mirror_object(context)
    if obj is None:
        return None
    return {
        axis: bool(getattr(obj, prop))
        for axis, prop in _MIRROR_PROPERTIES.items()
    }


# Which two local axes a mirror plane spans. Mirroring "across X" means the
# plane perpendicular to X, so the quad is drawn in Y and Z.
_PLANE_AXES = {'X': (1, 2), 'Y': (0, 2), 'Z': (0, 1)}

# How far past the object's bounding box the plane is drawn. Flush with the
# box the edges disappear into the silhouette.
_PLANE_MARGIN = 0.08


def _show_symmetry_planes(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return False
    return bool(scene.paintr2.ui.show_symmetry_planes)


def plane_corners(obj, axis):
    """The four world-space corners of one mirror plane across `obj`.

    The plane passes through the object's *origin* along its own axes -- that
    is where Blender mirrors -- and is sized to the object's bounding box on
    the other two, so it covers the model and nothing else. On a moved or
    rotated object it goes with the object, which is the point: it shows where
    the mirroring actually is.
    """
    from mathutils import Vector

    corners = [Vector(corner) for corner in obj.bound_box]
    low = Vector([min(corner[i] for corner in corners) for i in range(3)])
    high = Vector([max(corner[i] for corner in corners) for i in range(3)])

    span = high - low
    for index in range(3):
        # A flat object still needs a plane with some size to it.
        padding = max(span[index] * _PLANE_MARGIN, span.length * 0.02, 1e-4)
        low[index] -= padding
        high[index] += padding

    first, second = _PLANE_AXES[axis]

    quad = []
    for a, b in ((low, low), (high, low), (high, high), (low, high)):
        point = Vector((0.0, 0.0, 0.0))
        point[first] = a[first]
        point[second] = b[second]
        quad.append(obj.matrix_world @ point)

    return quad


def _symmetry_shapes(context):
    """The enabled mirror planes, projected into region pixels.

    Returned as flat 2D loops with a colour, because gui/ knows nothing about
    matrices or views.
    """
    from bpy_extras import view3d_utils

    if not _show_symmetry_planes(context):
        return ()

    obj = _mirror_object(context)
    if obj is None:
        return ()

    axes = [
        axis for axis, prop in _MIRROR_PROPERTIES.items() if getattr(obj, prop)
    ]
    if not axes:
        return ()

    region = getattr(context, "region", None)
    space = getattr(context, "space_data", None)
    rv3d = getattr(space, "region_3d", None)

    if region is None or rv3d is None:
        return ()

    shapes = []
    for axis in axes:
        loop = []
        for corner in plane_corners(obj, axis):
            projected = view3d_utils.location_3d_to_region_2d(region, rv3d, corner)
            if projected is None:
                # A corner behind the camera; the quad cannot be drawn.
                loop = []
                break
            loop.append((projected.x, projected.y))

        if loop:
            shapes.append({"color": theme.AXIS[axis], "loop": loop})

    return tuple(shapes)


# --- interface callbacks ----------------------------------------------------
# These run from the modal operator's event handling, never from the draw
# handler, so writing to RNA here is safe.


def _leave_mask_view(document=None):
    """Come out of the alt-click mask view as soon as the user looks away.

    Alt-clicking a mask chip shows that one mask, in grey, instead of the
    stack -- a way of checking a mask, not a mode to work in. It has no
    visible control to get out of, so anything that says "I am looking at
    something else now" has to be the way out: picking another layer, picking
    the other half of the same one, clicking the empty space under the list,
    or making a new layer.

    Only the *mask* view. Alt-clicking a layer isolates it, and that one is
    something people do want to keep while they work in it.
    """
    from . import nodegen
    from . import paint

    document = document or paint.active_document()
    if document is None:
        return

    mode, _index = document.solo_target()
    if mode != 'MASK':
        return

    document.solo_clear()

    material = getattr(document, "id_data", None)
    if material is not None:
        nodegen.compile_material(material)


def _select_layer(index, extend=False, span=False, part='NONE'):
    """Click a row. `extend` is ctrl (add or drop), `span` is shift (a range).

    `part` is which half of the layer the click landed on -- its content chip,
    its mask chip, or neither. One message rather than two, because it is one
    click: a separate "and now select the mask" would be a second undo step
    for something the user did once.

    Adding to a selection says nothing about halves: with several layers
    selected there is no single layer for a half to belong to.
    """
    from . import paint

    document = paint.active_document()
    if document is None or not (0 <= index < len(document.layers)):
        return

    # Looking at another layer, or at the other half of this one, is looking
    # away from the mask that was being checked.
    _leave_mask_view(document)

    if extend:
        document.select_extend(index)
    elif span:
        document.select_range(index)
    else:
        document.select_only(index)
        document.set_active_part(part)

    # Selecting is the whole gesture: if the layer can be painted, arm the
    # brush; if it cannot, put the object back to Object mode.
    paint.auto_sync_mode()
    session.tag_redraw()


def _box_select_layers(indices, extend=False):
    """Select every layer a rubber band covered.

    The active layer becomes the topmost one in the band, which is the one
    the eye is already on: a band drawn upwards from the empty space ends on
    it, and a band drawn downwards starts there.
    """
    from . import paint

    document = paint.active_document()
    if document is None:
        return

    wanted = {index for index in indices if 0 <= index < len(document.layers)}
    if extend:
        wanted |= set(document.selected_indices())

    if not wanted:
        document.select_none()
    else:
        _leave_mask_view(document)
        for position, layer in enumerate(document.layers):
            picked = position in wanted
            if layer.selected != picked:
                layer.selected = picked
        document.active_index = min(wanted)
        document.active_effect = -1
        # A band picks layers, never a half of one: with several selected
        # there is no layer for a half to belong to.
        document.set_active_part('NONE')

    paint.auto_sync_mode()
    session.tag_redraw()


def _toggle_layer(index):
    from . import paint

    document = paint.active_document()
    if document is None or not (0 <= index < len(document.layers)):
        return

    layer = document.layers[index]
    layer.visible = not layer.visible  # triggers a recompile via the update hook
    session.tag_redraw()


# --- the brush strip --------------------------------------------------------
#
# Every one of these goes through `paint.brush_owner`, never straight to the
# brush: unified paint settings own colour and size by default, and writing
# `brush.color` in that state is a silent no-op.


# The largest size the strip's slider reaches, in pixels. Blender's own soft
# maximum for `Brush.size`; the property accepts more, and a value typed
# elsewhere still shows -- clamped -- rather than being rewritten.
_BRUSH_SIZE_MAX = 500

_brush_blend_modes = None


def _brush_blend_items():
    """`Brush.blend` as [(identifier, label)], read from RNA rather than typed.

    24 modes in 5.2, and not the same list as the compositor's mix node -- so
    it is read from the property itself and cached.
    """
    global _brush_blend_modes
    if _brush_blend_modes is None:
        try:
            items = bpy.types.Brush.bl_rna.properties["blend"].enum_items
            _brush_blend_modes = [(item.identifier, item.name) for item in items]
        except (KeyError, AttributeError):
            _brush_blend_modes = []
    return _brush_blend_modes


def _brush_info(context):
    """What the brush strip shows, or None when there is no brush to show."""
    from . import brushes
    from . import paint

    brush = paint.active_brush(context)
    if brush is None:
        return None

    return {
        "brushes": brushes.available(),
        "brush": brushes.active_identifier(context),
        "blends": _brush_blend_items(),
        "blend": brush.blend,
        "color": tuple(paint.brush_get("color", (1.0, 1.0, 1.0), context)),
        "secondary_color": tuple(
            paint.brush_get("secondary_color", (0.0, 0.0, 0.0), context)
        ),
        "size": int(paint.brush_get("size", 50, context) or 0),
        "size_max": _BRUSH_SIZE_MAX,
        "strength": float(paint.brush_get("strength", 1.0, context) or 0.0),
        "size_pressure": bool(getattr(brush, "use_pressure_size", False)),
        "strength_pressure": bool(getattr(brush, "use_pressure_strength", False)),
    }


# --- following Blender's own changes ----------------------------------------


def _external_state(context):
    """A fingerprint of everything the chrome shows but Blender owns.

    Cheap RNA reads only -- no textures, no projections, nothing that needs a
    GPU context -- because this runs on the modal operator's heartbeat.
    """
    brush = _brush_info(context)
    if brush is not None:
        # The two item lists are cached constants; comparing them every tick
        # would be the expensive part of an otherwise cheap check.
        brush = {
            key: value
            for key, value in brush.items()
            if key not in ("brushes", "blends")
        }

    rows, active_index, active_info = _layer_rows(context)

    settings = _fill_settings(context)
    fill_state = None
    if settings is not None:
        # The values, not the item lists: those are constants, and comparing
        # them on every heartbeat would be the expensive half of a cheap check.
        fill_state = (
            settings.blend,
            round(float(settings.strength), 4),
            settings.mode,
            settings.samples,
            int(settings.bleed),
            settings.polygon_scope,
            settings.mesh_scope,
        )

    return (
        _status_text(context),
        _symmetry_flags(context),
        _show_symmetry_planes(context),
        rows,
        active_index,
        active_info,
        brush,
        active_tool(context),
        fill_state,
        wireframe_shown(),
    )


def poll_external_changes(context=None):
    """True when something outside PaintR2 changed what the interface shows.

    The interface repaints when *it* handles an event. Blender's own
    shortcuts, its operators and its other windows write the very same RNA
    properties without going through us, and nothing then redraws this region:
    `paint.brush_colors_flip` -- the X key -- only tags the brush cursor
    overlay, so the swatches went on showing the colours from before the swap
    until the pointer happened to move. Comparing a fingerprint of the
    mirrored state is what closes that gap; the modal operator calls this on
    its timer.
    """
    global _external_signature

    try:
        signature = _external_state(context or bpy.context)
    except (AttributeError, KeyError, RuntimeError):
        # Mid-teardown, or no scene yet. Nothing to redraw for.
        return False

    if signature == _external_signature:
        return False

    _external_signature = signature
    return True


def _set_brush(identifier):
    from . import brushes

    if brushes.activate(identifier):
        # The asset switch lands in RNA behind the operator call; read it back
        # on the next trip through the event loop, not on this one.
        session.tag_redraw_soon()


def _set_brush_blend(value):
    from . import paint
    brush = paint.active_brush()
    if brush is not None:
        try:
            brush.blend = value
        except (AttributeError, TypeError, ValueError):
            pass


def _set_brush_color(color):
    from . import paint
    paint.brush_set("color", tuple(color)[:3])


def _set_brush_secondary_color(color):
    from . import paint
    paint.brush_set("secondary_color", tuple(color)[:3])


def _swap_brush_colors():
    """The same flip Blender's X shortcut does, honouring unified settings."""
    from . import paint

    primary = tuple(paint.brush_get("color", (1.0, 1.0, 1.0)) or ())[:3]
    secondary = tuple(paint.brush_get("secondary_color", (0.0, 0.0, 0.0)) or ())[:3]
    paint.brush_set("color", secondary)
    paint.brush_set("secondary_color", primary)


def _set_brush_size(pixels):
    from . import paint
    paint.brush_set("size", max(1, int(pixels)))


def _set_brush_strength(value):
    from . import paint
    paint.brush_set("strength", min(1.0, max(0.0, float(value))))


# Pressure is a brush property, never a unified one -- Blender has no
# `use_unified_pressure`.
_PRESSURE_PROPERTIES = {
    'SIZE': "use_pressure_size",
    'STRENGTH': "use_pressure_strength",
}


def _set_brush_pressure(which, enabled):
    from . import paint
    brush = paint.active_brush()
    prop = _PRESSURE_PROPERTIES.get(which)
    if brush is None or prop is None:
        return
    try:
        setattr(brush, prop, bool(enabled))
    except (AttributeError, TypeError):
        pass


# --- the fill tools ----------------------------------------------------------
#
# Three tools, one setting group. Polygon Fill, Mesh Fill and UV Island Fill
# differ in exactly one thing -- how far a click reaches -- so the strip is
# built once and told which scope property it is currently showing. Everything
# else about them is shared, and the colour is shared with the brush as
# well, which is why no colour appears in `PR2_FillSettings`.


_FILL_SCOPE_PROPERTIES = {
    'POLY_FILL': "polygon_scope",
    'MESH_FILL': "mesh_scope",
}

# What each tool asks `fill.face_set` for. UV Island Fill has no choice to
# offer; the other two read theirs off the strip.
_FILL_DEFAULT_SCOPES = {
    'POLY_FILL': 'FACE',
    'MESH_FILL': 'PART',
    'UV_FILL': 'ISLAND',
}


def _scene_props(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "paintr2"):
        return None
    return scene.paintr2


def fill_select_mode(context=None):
    """'CLICK', 'RECT' or 'LASSO'. Always a click when the tool is a brush."""
    settings = _fill_settings(context)
    return getattr(settings, "select_mode", 'CLICK') if settings else 'CLICK'


def fill_shape_points(points, mode):
    """The gesture as a closed polygon in region pixels.

    A rectangle is two corners on screen and four points here; a lasso is what
    the hand drew. One shape either way, so the selection has one code path.
    """
    points = list(points or ())
    if mode == 'RECT':
        if len(points) < 2:
            return []
        (x0, y0), (x1, y1) = points[0], points[-1]
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return points


def set_fill_shape(points, mode):
    """Hand the box or lasso being drawn to the interface, or clear it."""
    global _fill_shape

    shape = fill_shape_points(points, mode) if points else None
    if shape is not None and len(shape) < 2:
        shape = None

    if shape == _fill_shape:
        return

    _fill_shape = shape
    state = session.state
    if state.root is not None:
        state.root.set_fill_shape(shape, filled=mode == 'RECT')
    session.tag_redraw()


def _fill_settings(context=None):
    props = _scene_props(context)
    return None if props is None else props.fill


def active_tool(context=None):
    """Which tool the row has selected. 'BRUSH' whenever nothing says otherwise."""
    props = _scene_props(context)
    return 'BRUSH' if props is None else props.active_tool


def is_fill_tool(context=None):
    return active_tool(context) != 'BRUSH'


def fill_scope(context=None):
    """The scope a click should use, resolved from the tool and the strip."""
    tool = active_tool(context)
    key = _FILL_SCOPE_PROPERTIES.get(tool)
    settings = _fill_settings(context)
    if key is None or settings is None:
        return _FILL_DEFAULT_SCOPES.get(tool, 'FACE')
    return getattr(settings, key, _FILL_DEFAULT_SCOPES.get(tool, 'FACE'))


# --- the wireframe overlay ---------------------------------------------------
#
# `overlay.show_wireframes` is Blender's own Overlays > Geometry > Wireframe,
# written on our host space -- the same property its popover writes, so the two
# never disagree. Its companion `wireframe_threshold` defaults to 1.0,
# which means every face edge, and that is exactly what a fill tool wants: you
# cannot aim at a polygon you cannot see.

# True while the wireframe is up because a fill tool asked for it rather than
# because the user did. Only then is it ours to put away again -- a hand on the
# switch has to win, or the button would fight whoever pressed it.
_wireframe_is_ours = False


def _host_overlay():
    space = session.find_space()
    return getattr(space, "overlay", None) if space is not None else None


def wireframe_shown():
    """Whether the host viewport is drawing face edges, or None if unreadable."""
    overlay = _host_overlay()
    if overlay is None:
        return None
    return bool(getattr(overlay, "show_wireframes", False))


def _write_wireframe(enabled):
    overlay = _host_overlay()
    if overlay is None:
        return False
    try:
        overlay.show_wireframes = bool(enabled)
    except (AttributeError, TypeError):
        return False

    # The colour is borrowed only while the wireframe is up, so it is taken
    # and given back in the same breath as the overlay rather than a tick
    # later -- switching the wireframe on must not show one frame of black.
    sync_wire_theme()

    session.tag_redraw()
    return True


def _set_wireframe(enabled):
    """The header button. Pressing it hands the overlay back to the user."""
    global _wireframe_is_ours
    if _write_wireframe(enabled):
        _wireframe_is_ours = False


def _follow_tool_wireframe(identifier):
    """A fill tool brings the wireframe up; leaving it takes ours back down.

    Only ours: if the wireframe was already on when the tool was picked, or the
    user turned it on afterwards, it stays on. Switching tools is not a reason
    to undo somebody's overlay.
    """
    global _wireframe_is_ours

    if identifier != 'BRUSH':
        if wireframe_shown() is False and _write_wireframe(True):
            _wireframe_is_ours = True
        return

    if _wireframe_is_ours:
        _write_wireframe(False)
        _wireframe_is_ours = False


def release_wireframe():
    """Called when the window closes: never leave our overlay behind."""
    global _wireframe_is_ours
    if _wireframe_is_ours:
        _write_wireframe(False)
        _wireframe_is_ours = False


# --- the wireframe colour ----------------------------------------------------
#
# There is no per-viewport wire colour to write. `shading.wireframe_color_type`
# offers THEME, OBJECT and RANDOM and nothing else: OBJECT reads
# `object.color`, which lives in the .blend and is the user's, so the only
# place a colour can come from is the theme -- and a theme is
# application-wide.
#
# Hence the borrowing rule: the four theme colours the wireframe can
# draw in are written while the PaintR2 window has the wireframe up, and put
# back the moment it does not. Which of the four is used depends on the
# object's state -- active, selected, plain, or in edit mode -- so all four
# carry the same colour, or the model would change colour when it happened to
# be selected.
_WIRE_THEME_KEYS = ("wire", "wire_edit", "object_active", "object_selected")

# What was there before we wrote, and what we wrote. Both None while the theme
# is Blender's own. The second one is how a hand on the theme is noticed: if
# the colour is no longer what we put there, the backup is stale and gets
# dropped rather than restored over somebody's edit.
_wire_theme_backup = None
_wire_theme_applied = None


def _theme_view3d():
    try:
        return bpy.context.preferences.themes[0].view_3d
    except (AttributeError, IndexError, KeyError):
        return None


def _read_wire_theme(view3d):
    """The four colours as Blender is holding them right now."""
    values = {}
    for key in _WIRE_THEME_KEYS:
        colour = getattr(view3d, key, None)
        if colour is None:
            return None
        values[key] = tuple(colour)
    return values


def _write_wire_theme(view3d, colour):
    for key in _WIRE_THEME_KEYS:
        try:
            setattr(view3d, key, colour)
        except (AttributeError, TypeError, ValueError):
            return False
    return True


def _same_colour(a, b):
    """Equal as far as a theme can tell, which is one part in 255.

    Theme colours are `char` in DNA: ask for 0.3 and Blender keeps 77/255 =
    0.301961. So "did this change" is answered a hair over half a step, not at
    float precision -- a nudge finer than that cannot alter what is stored,
    and treating it as a change would rewrite the theme on every tick. Half a
    step exactly is not enough: 0.3 lands 1e-8 past it once the value has been
    through a 32-bit round trip.
    """
    return all(abs(x - y) <= 1.0 / 500.0 for x, y in zip(a, b))


def _wire_theme_is_still_ours(view3d):
    """False once anything but us has written one of the four colours.

    Compares against what was *read back* after our own write, never against
    what we asked for: those two differ by up to half a byte, and a colour we
    wrote ourselves must never read as somebody else's edit.
    """
    if _wire_theme_applied is None:
        return False
    for key in _WIRE_THEME_KEYS:
        colour = getattr(view3d, key, None)
        if colour is None or tuple(colour) != _wire_theme_applied.get(key):
            return False
    return True


def _wanted_wire_colour():
    """The colour the theme should be carrying right now, or None for Blender's.

    Three things have to hold at once: the window is open, its wireframe is
    actually up, and the preference is switched on. Anything else hands the
    theme back -- that is what "only while the add-on is in use" means.
    """
    if not session.state.is_open:
        return None
    if wireframe_shown() is not True:
        return None

    prefs = preferences.get()
    if prefs is None or not getattr(prefs, "wire_color_custom", False):
        return None

    colour = getattr(prefs, "wire_color", None)
    return tuple(colour) if colour is not None else None


def _take_wire_theme(view3d, colour, backup=None):
    """Write `colour`, remembering what it displaced and what Blender kept."""
    global _wire_theme_backup, _wire_theme_applied

    if backup is None:
        backup = _read_wire_theme(view3d)
    if backup is None or not _write_wire_theme(view3d, colour):
        return False

    _wire_theme_backup = backup
    _wire_theme_applied = _read_wire_theme(view3d)
    session.tag_redraw()
    return True


def _put_wire_theme_back(view3d):
    """Write the saved colours back and stop holding the theme."""
    global _wire_theme_backup, _wire_theme_applied

    for key, colour in (_wire_theme_backup or {}).items():
        try:
            setattr(view3d, key, colour)
        except (AttributeError, TypeError, ValueError):
            pass

    _wire_theme_backup = None
    _wire_theme_applied = None
    session.tag_redraw()


def sync_wire_theme():
    """Take the theme colour, hand it back, or leave it alone. Called on a tick.

    Cheap on purpose: with nothing borrowed and nothing wanted it reads three
    pieces of state and writes nothing at all.
    """
    global _wire_theme_backup, _wire_theme_applied

    view3d = _theme_view3d()
    if view3d is None:
        return

    wanted = _wanted_wire_colour()

    if _wire_theme_backup is not None:
        if not _wire_theme_is_still_ours(view3d):
            # Somebody edited the theme while we were holding it. It is theirs
            # now: the backup is stale, so it is dropped rather than written
            # over their edit. The next pass takes a fresh one.
            _wire_theme_backup = None
            _wire_theme_applied = None
            return

        if wanted is None:
            _put_wire_theme_back(view3d)
        elif not _same_colour(wanted, _wire_theme_applied[_WIRE_THEME_KEYS[0]]):
            _take_wire_theme(view3d, wanted, backup=_wire_theme_backup)
        return

    if wanted is not None:
        _take_wire_theme(view3d, wanted)


def release_wire_theme():
    """Give the theme back. The window closing is the last chance to do it."""
    global _wire_theme_backup, _wire_theme_applied

    if _wire_theme_backup is None:
        return

    view3d = _theme_view3d()
    if view3d is not None and _wire_theme_is_still_ours(view3d):
        _put_wire_theme_back(view3d)
    else:
        _wire_theme_backup = None
        _wire_theme_applied = None


def mirror_axes(context=None):
    """The axes the header's mirror buttons have on, as a tuple like ('X', 'Z').

    Read off the *active* object -- the one the header's `X Y Z` are showing
    and the one Blender's own stroke mirrors around -- so the brush and the
    fill tools always agree about what mirroring means in this window.
    """
    flags = _symmetry_flags(context or bpy.context)
    if not flags:
        return ()
    return tuple(axis for axis in ('X', 'Y', 'Z') if flags.get(axis))


def _set_active_tool(identifier):
    props = _scene_props()
    if props is None:
        return
    try:
        props.active_tool = identifier
    except (AttributeError, TypeError, ValueError):
        return

    # A fill tool owns the left mouse button, so Blender's brush circle would
    # be advertising something that is not going to happen.
    from . import paint
    paint.set_brush_cursor(identifier == 'BRUSH')

    # You cannot aim at a polygon you cannot see.
    _follow_tool_wireframe(identifier)

    # The highlight belongs to the tool that drew it.
    clear_fill_preview()
    session.tag_redraw()


def _set_fill_prop(key, value):
    settings = _fill_settings()
    if settings is None or not key:
        return
    try:
        setattr(settings, key, value)
    except (AttributeError, TypeError, ValueError) as error:
        print(f"[PaintR2] could not set fill.{key}: {error}")
    session.tag_redraw()


def _enum_items(holder, key):
    """[(identifier, label)] of an enum property, read from RNA not typed here."""
    prop = holder.bl_rna.properties.get(key)
    if prop is None:
        return []
    return [(item.identifier, item.name) for item in prop.enum_items]


def _fill_option_rows(context):
    """The Options popover: what the strip has no room for."""
    settings = _fill_settings(context)
    perfect = bool(settings and settings.pixel_perfect)

    rows = [
        _row('fill', "pixel_perfect", context=context),
        # Drawn dim rather than hidden while Pixel Perfect is on: a control
        # that vanishes is a control the user goes looking for. There is
        # nothing for sub-pixel samples to do when every touched pixel is
        # filled whole.
        _row('fill', "samples", active=not perfect, context=context),
        _row('fill', "bleed", context=context),
    ]

    # Only where it means something: a Polygon Fill takes the one face under
    # the pointer, which is facing you by definition.
    tool = context.scene.paintr2.active_tool
    if tool in ('MESH_FILL', 'UV_FILL'):
        rows.append(_separator())
        rows.append(_row('fill', "front_only", context=context))

    return _compact(rows)


def _fill_info(context):
    """What the fill strip shows, or None when there is nothing to fill into."""
    from . import paint

    settings = _fill_settings(context)
    if settings is None:
        return None

    # The same test the brush strip makes: no paintable image means no target,
    # and a strip full of live controls over nothing is a lie.
    if paint.active_paint_image(context) is None:
        return None

    scope_key = _FILL_SCOPE_PROPERTIES.get(active_tool(context))

    return {
        "scopes": _enum_items(settings, scope_key) if scope_key else [],
        "scope": getattr(settings, scope_key) if scope_key else None,
        "blends": _enum_items(settings, "blend"),
        "blend": settings.blend,
        "strength": float(settings.strength),
        "mode": settings.mode,
        "select": settings.select_mode,
        "options": _fill_option_rows(context),
        "options_width": 240,
    }


# --- the Display panel -------------------------------------------------------
#
# Two blocks: how the model is lit, and how the compiled stack is shown on it.
# They sit together because they answer the same question -- "why does the
# paint look like that" -- and the answer is nearly always one of the two.
#
# Every row is built the same way the brush popovers are: described from
# RNA on each redraw, so range, precision, subtype and tooltip come from the
# property rather than from anything typed here.

DISPLAY_PANEL_WIDTH = 250


def _lighting_rows(context):
    """The Lighting block. Collapsed to its switch when the switch is off.

    The dependent rows are left out rather than drawn dim: a dimmed row in
    this toolkit still takes a click, and a control that says it is inactive
    and then acts is worse than one that is not there. It is also the rule
    the panel already follows -- Blender only draws Spacing when the stroke
    method is Space, and so do we, because the row simply is not in the list.
    """
    from . import lighting

    scene = getattr(context, "scene", None)
    if scene is None:
        return []

    settings = scene.paintr2.display
    rows = [_row('display', "use_lighting", text="Enabled", context=context)]

    if not settings.use_lighting:
        rows.append({
            "kind": 'label',
            "label": "Blender's studio light",
            "active": False,
        })
        return _compact(rows)

    rows.append(_separator())

    presets = lighting.studio_hdris(context)
    if presets:
        # Hand-built rather than an EnumProperty: the list comes from the
        # user's preferences, and a dynamic enum callback stores an index --
        # install a studiolight and every saved file points at a different
        # map. A path written into a StringProperty cannot drift.
        rows.append({
            "kind": 'enum',
            "owner": 'display',
            "key": "hdri_path",
            "label": "HDRI",
            "desc": "The environment map that lights the model",
            "value": settings.hdri_path,
            "items": presets,
            "active": True,
            "suffix": [],
        })

    rows.append({
        "kind": 'button',
        "label": "Open HDRI File...",
        "action": ('hdri_browse',),
        "active": True,
    })
    rows.append({
        "kind": 'label',
        "label": lighting.hdri_label(settings.hdri_path),
        "active": False,
    })

    rows.append(_separator())
    rows.append(_row('display', "strength", context=context))
    rows.append(_row('display', "rotation_x", label="Rotation X", context=context))
    rows.append(_row('display', "rotation_y", label="Y", context=context))
    rows.append(_row('display', "rotation_z", label="Z", context=context))
    rows.append({
        "kind": 'button',
        "label": "Reset Rotation",
        "action": ('hdri_reset',),
        "active": True,
    })

    rows.append(_separator())
    rows.append(_row('display', "show_background", text="Background",
                     context=context))
    if not settings.show_background:
        rows.append(_row('display', "background_color", label="Backdrop",
                         context=context))
    rows.append(_row('display', "use_scene_lights", text="Scene Lights",
                     context=context))

    return _compact(rows)


def _scene_rows(context):
    """The Scene block: what the window shows of the set, beside the model.

    Two things, and they are the two halves of the same answer to "why can I
    only see this one model": everything outside the set is hidden, and
    inside it everything but the active one is dimmed. Both are switchable
    from here, because the moment either surprises someone the first thing
    they need is the switch.
    """
    from . import isolation
    from . import paintset

    models = paintset.paint_objects()
    hidden = len(context.scene.paintr2.hidden_objects) if context.scene else 0

    rows = [
        {
            "kind": 'label',
            "label": f"{len(models)} model(s) in {paintset.PAINT}",
            "active": bool(models),
        },
        _row('display', "fade_inactive", text="Dim Other Models",
             context=context),
    ]

    if isolation.fade_enabled(context):
        rows.append(_row('display', "fade_amount", label="Brightness",
                         slider=True, context=context))

    rows.append(_separator())
    rows.append({
        "kind": 'label',
        "label": f"{hidden} object(s) hidden" if hidden
                 else "Nothing hidden",
        "active": bool(hidden),
    })
    rows.append({
        "kind": 'button',
        "label": "Show Hidden Objects",
        "desc": "Put back everything the session hid. They stay visible until "
                "the set is isolated again",
        "action": ('show_all',),
        "active": bool(hidden),
    })
    rows.append({
        "kind": 'button',
        "label": "Isolate Paint Set",
        "desc": "Hide everything outside the Paint collection again",
        "action": ('isolate',),
        "active": bool(models),
    })
    return _compact(rows)


def _shader_rows(context):
    """The Shader block: how this material's stack is put on the surface."""
    from . import document as doc
    from . import paint

    document = paint.active_document(context)
    if document is None:
        return [{
            "kind": 'label',
            "label": "No paint material",
            "active": False,
        }]

    rows = [_row('doc', "shader_mode", label="Mode", context=context)]

    if document.shader_mode == 'CHANNEL':
        # Only the channels this material actually carries. Offering the other
        # three would be offering a black model with no explanation.
        active = document.active_channels()
        rows.append({
            "kind": 'enum',
            "owner": 'doc',
            "key": "preview_channel",
            "label": "Channel",
            "desc": "Which channel the Channel mode shows",
            "value": document.preview_target() or document.preview_channel,
            "items": [(c, doc.channel_label(c)) for c in active],
            "active": bool(active),
            "suffix": [],
        })

    rows.append(_separator())
    # The other half of "show me the colour I painted": an unlit surface still
    # comes back through the view transform, so Standard is what makes an
    # unlit read match the value in the swatch.
    rows.append(_row('view', "view_transform", label="View", context=context))
    rows.append(_row('view', "look", label="Look", context=context))
    rows.append(_row('view', "exposure", context=context))

    return _compact(rows)


def _map_rows(context):
    """The Maps block: what to bake, and what has been baked.

    The reason this exists in the window at all: getting a mesh map right is
    baking it, looking at it, changing one number and baking it again. Every
    trip out to the sidebar to do that is a trip back to find the model again.
    """
    from . import external
    from . import meshmaps
    from . import paint

    scene = getattr(context, "scene", None)
    if scene is None:
        return []

    props = scene.paintr2
    obj = getattr(context, "active_object", None)
    document = paint.active_document(context)

    binary = external.available()
    use_external = props.mesh_map_backend != 'CYCLES' and binary is not None

    rows = [
        _row('scene', "mesh_map_resolution", label="Size", context=context),
        _row('scene', "mesh_map_backend", label="Baker", context=context),
        # The two questions a bake of more than one model has to answer:
        # which of them get maps, and whether they shadow one another.
        _row('scene', "bake_models", label="Bake", context=context),
        _row('scene', "bake_occluders", label="Occlusion", context=context),
        _separator(),
    ]

    # Which maps, as their own checkboxes -- and a map that cannot be produced
    # right now is drawn dim rather than hidden, so the reason it will not
    # appear is visible before the bake instead of in the report after it.
    for identifier in meshmaps.MAP_IDS:
        available = True
        if meshmaps.needs_high(identifier) and not props.use_highpoly_bake:
            available = False
        if meshmaps.needs_external(identifier) and not use_external:
            available = False

        baked = meshmaps.stored(obj, identifier) if obj is not None else None
        suffix = []
        if baked is not None and document is not None:
            # The eye is the whole point of the panel: one click puts the map
            # that was just baked on the model.
            showing = document.shader_mode == 'MESH_MAP' and \
                document.preview_map == baked
            suffix.append({
                "kind": 'toggle',
                "icon": 'EYE',
                "value": showing,
                "action": ('view_map', identifier),
                "desc": f"Show the baked {meshmaps.map_label(identifier)} on "
                        f"the model",
            })

        rows.append(_row(
            'scene', "bake_map_" + identifier.lower(),
            active=available, suffix=suffix, context=context,
        ))

    rows.append(_separator())

    # Per-map settings, each behind the map it belongs to. Only the external
    # baker reads them -- Cycles has an operator with two knobs and no way to
    # be told any of this -- so with Cycles selected they are drawn dim and
    # say why, rather than silently doing nothing.
    if props.bake_map_ao:
        rows.append({
            "kind": 'panel',
            "key": "bake_ao",
            "label": "Ambient Occlusion",
            "open": False,
            "rows": _compact([
                # The one AO setting both bakers read, so it sits above the
                # "External baker only" note. Same property as the Samples row
                # under the panels, which Thickness shares.
                _row('scene', "mesh_map_samples", label="Samples",
                     context=context),
                None if use_external else {
                    "kind": 'label',
                    "label": "External baker only",
                    "active": False,
                },
                _row('scene', "ao_distance", label="Max Distance",
                     active=use_external, context=context),
                _row('scene', "ao_distance_relative", active=use_external,
                     context=context),
                _row('scene', "ao_min_distance", label="Min Distance",
                     active=use_external, context=context),
                _row('scene', "ao_spread", label="Spread", active=use_external,
                     context=context),
                _row('scene', "ao_distribution", label="Distribution",
                     active=use_external, context=context),
                _row('scene', "ao_attenuation", label="Attenuation",
                     active=use_external, context=context),
                _row('scene', "ao_ignore_backface", active=use_external,
                     context=context),
            ]),
        })

    if props.bake_map_curvature:
        rows.append({
            "kind": 'panel',
            "key": "bake_curvature",
            "label": "Curvature",
            "open": False,
            "rows": _compact([
                _row('scene', "curvature_radius", label="Radius",
                     active=use_external, context=context),
                _row('scene', "curvature_radius_relative", active=use_external,
                     context=context),
                _row('scene', "curvature_low", label="Range Min",
                     active=use_external, context=context),
                _row('scene', "curvature_high", label="Range Max",
                     active=use_external, context=context),
                _separator(),
                # These two are the Cycles path's own smoothing, and they are
                # the only curvature controls it has.
                _row('scene', "curvature_iterations", active=not use_external,
                     context=context),
                _row('scene', "curvature_smooth", active=not use_external,
                     context=context),
            ]),
        })

    rows.append(_separator())
    rows.append(_row('scene', "mesh_map_samples", label="Samples", context=context))
    rows.append(_row('scene', "bake_margin", label="Margin", context=context))
    rows.append(_row('scene', "bake_margin_type", label="", context=context))

    rows.append(_separator())
    rows.append(_row('scene', "use_highpoly_bake", context=context))
    if props.use_highpoly_bake:
        from . import paintset

        collection, note = paintset.status(obj)
        rows.append({
            "kind": 'label',
            "label": f"{collection}  {note}".strip() or "No pair",
            "active": bool(collection),
        })

    # What stops the bake, said before the button rather than after it.
    problem = ""
    if obj is None or obj.type != 'MESH':
        problem = "Select a mesh object"
    elif not obj.data.uv_layers:
        problem = "No UV map - unwrap first"
    elif props.mesh_map_backend == 'EXTERNAL' and binary is None:
        problem = "External baker not installed"
    elif not any(getattr(props, "bake_map_" + i.lower())
                 for i in meshmaps.MAP_IDS):
        problem = "No map selected"
    else:
        from .ops import op_meshmaps
        if not op_meshmaps.bake_groups(context):
            problem = "No material ticked for baking"

    if problem:
        rows.append({"kind": 'label', "label": problem, "active": False})

    # Counted by the operator's own rule, so the button cannot promise a
    # different number of texture sets than the bake then makes. Maps are
    # per material, so that is what is counted.
    from .ops import op_meshmaps
    groups = op_meshmaps.bake_groups(context)

    rows.append({
        "kind": 'button',
        "label": "Bake Mesh Maps" if len(groups) <= 1
        else f"Bake {len(groups)} Materials",
        "desc": "Bake the ticked maps, one set per material of the models "
                "the Bake row names",
        "action": ('bake_maps',),
        "active": not problem,
    })

    if document is not None and document.shader_mode == 'MESH_MAP':
        rows.append({
            "kind": 'button',
            "label": "Back to the Stack",
            "desc": "Stop showing a mesh map and go back to the layers",
            "action": ('view_map', ""),
        })

    return _compact(rows)


def _display_rows(context):
    """The whole left panel, or nothing when it is closed.

    Two pages, one panel: Display is how the model is lit and shaded, Bake is
    what is read off the model itself. They share the column because only one
    of them is ever being used, and a second panel would take a third of the
    window to say so.
    """
    scene = getattr(context, "scene", None)
    if scene is None or not scene.paintr2.ui.show_left_panel:
        return []

    if scene.paintr2.ui.left_panel_page == 'BAKE':
        return [
            {
                "kind": 'panel',
                "key": "bake_maps",
                "label": "Mesh Maps",
                "open": True,
                "rows": _map_rows(context),
            },
            {
                "kind": 'panel',
                "key": "bake_export",
                "label": "Export",
                "open": False,
                "rows": _export_rows(context),
            },
            {
                "kind": 'panel',
                "key": "bake_storage",
                "label": "Layer Images",
                "open": False,
                "rows": _storage_rows(context),
            },
        ]

    return [
        {
            "kind": 'panel',
            "key": "display_lighting",
            "label": "Lighting",
            "open": True,
            "rows": _lighting_rows(context),
        },
        {
            "kind": 'panel',
            "key": "display_shader",
            "label": "Shader",
            "open": True,
            "rows": _shader_rows(context),
        },
        {
            "kind": 'panel',
            "key": "display_scene",
            "label": "Models",
            "open": False,
            "rows": _scene_rows(context),
        },
    ]


def _export_rows(context):
    """The Export block: the same bake the sidebar runs, from in here."""
    from . import bake as bake_mod

    scene = getattr(context, "scene", None)
    if scene is None:
        return []

    props = scene.paintr2
    obj = getattr(context, "active_object", None)

    rows = [
        _row('scene', "export_resolution", label="Size", context=context),
        _row('scene', "export_format", label="Format", context=context),
        _separator(),
    ]

    materials = bake_mod.paint_materials(obj) if obj is not None else []
    if not materials:
        rows.append({"kind": 'label', "label": "No PaintR2 material here",
                     "active": False})

    # The ellipsis is not decoration: it is the convention that says a button
    # opens a dialog rather than doing the thing. This one used to do the
    # thing -- six files, ten frozen seconds, no word about either.
    rows.append({
        "kind": 'button',
        "label": "Bake & Export...",
        "desc": "Choose what to bake and where to write it",
        "action": ('bake_export',),
        "active": bool(materials),
    })
    return _compact(rows)


def _export_targets(context):
    """The models this export would write, and the channels they agree on.

    The same answer `op_export` computes, asked here so the dialog can say
    what pressing Export will actually do. Duplicating the rule would be a way
    for the two to disagree, so the operator's own helpers are what run.
    """
    from .ops import op_export

    scene = getattr(context, "scene", None)
    if scene is None:
        return [], {}

    targets = op_export._targets(context)
    if scene.paintr2.export_active_only and targets:
        active = getattr(context, "active_object", None)
        chosen = active if active in targets else targets[0]
        # With the models that share its materials -- the operator bakes
        # those too, or the texture would hold only the active one's UVs.
        targets = next((g for g in op_export._texture_groups(targets)
                        if chosen in g), [chosen])

    channels = {}
    for obj in targets:
        if obj is None:
            continue
        channels[obj.name] = op_export._channels_for(scene.paintr2, obj)
    return [obj for obj in targets if obj is not None], channels


def _export_dialog_rows(context):
    """What the export dialog asks before it bakes anything.

    Every row is a scene property, so the dialog writes through the same
    channel every other panel does and the settings are still there next time.
    The channel switches are filtered to what the models actually carry: a
    tick for a channel no material has is not a choice, it is a lie.
    """
    from . import document as doc

    scene = getattr(context, "scene", None)
    if scene is None:
        return []

    rows = [
        _row('scene', "export_directory", label="Folder", context=context),
        _row('scene', "export_resolution", label="Size", context=context),
        _row('scene', "export_format", label="Format", context=context),
        _row('scene', "bake_margin", label="Margin", context=context),
        _separator(),
        _row('scene', "export_active_only", label="", text="Active Model Only",
             context=context),
        _separator(),
    ]

    targets, _channels = _export_targets(context)

    # Which channels are on offer is the union of what the models carry --
    # every one of them is a file somebody might want, and a channel only one
    # of two models has is still a channel that model exports.
    offered = []
    for obj in targets:
        from . import bake as bake_mod

        for material in bake_mod.paint_materials(obj):
            for identifier in material.paintr2.active_channels():
                if identifier not in offered:
                    offered.append(identifier)

    if offered:
        rows.append({"kind": 'label', "label": "Channels", "active": True})
        for identifier in doc.CHANNEL_IDS:
            if identifier not in offered:
                continue
            rows.append(_row(
                'scene', "export_" + identifier.lower(), label="",
                text=doc.channel_label(identifier), context=context,
            ))

    return _compact(rows)


def _export_summary(context):
    """`(footnote, can_export)` -- what Export is about to write, and whether
    there is anything to write at all.

    A confirm button that is about to produce six files should say six, and a
    confirm button that would produce none should not be pressable. Both were
    the same bug: the window gave no account of the work either before or
    after it ran.
    """
    scene = getattr(context, "scene", None)
    if scene is None:
        return "", False

    targets, channels = _export_targets(context)
    files = sum(len(channels.get(obj.name, ())) for obj in targets)

    if not targets:
        return "No model to export", False
    if not files:
        return "No channel switched on", False

    where = bpy.path.abspath(scene.paintr2.export_directory)
    models = f"{len(targets)} model" + ("s" if len(targets) != 1 else "")
    return f"{files} file(s) from {models}  ->  {where}", True


def _storage_rows(context):
    """The Layer Images block: where the painted pixels actually live.

    The same two commands the sidebar carries, in here. A window you can paint
    a whole model in and then have to leave to save what you painted is a
    window with a hole in it -- and the reason the sidebar spells the state
    out in a line of text is the same here: pressing Pack and seeing nothing
    change is indistinguishable from Pack not working.
    """
    from . import storage

    images = storage.layer_images()
    packed = storage.packed_images()
    unsaved = storage.unsaved_images()
    folder = storage.external_directory()

    if not images:
        note = "No layer images yet"
    elif len(packed) == len(images):
        note = f"{len(packed)} image packed in the .blend"
    elif packed:
        note = f"{len(packed)} of {len(images)} packed"
    elif folder is None:
        note = "Only in memory - save or pack"
    elif unsaved:
        note = f"{len(unsaved)} image not on disk"
    else:
        note = f"{len(images)} image beside the .blend"

    rows = [{"kind": 'label', "label": note, "active": bool(images)}]

    rows.append({
        "kind": 'button',
        "label": "Save Layers",
        "desc": "Write every painted layer to the folder beside the .blend",
        "action": ('save_layers',),
        "active": folder is not None,
    })

    # Pack and Unpack are one button that swaps, the same as in the sidebar:
    # with everything already packed, "Pack" can only say what it already says.
    if images and len(packed) == len(images):
        rows.append({
            "kind": 'button',
            "label": "Unpack",
            "desc": "Move the packed layer images back out to disk",
            "action": ('unpack_layers',),
            "active": folder is not None,
        })
    else:
        rows.append({
            "kind": 'button',
            "label": "Pack",
            "desc": "Embed every layer image inside the .blend file",
            "action": ('pack_layers',),
            "active": bool(images),
        })

    return _compact(rows)


def _set_targets_panel(is_open):
    """Show or hide the Objects / Materials panel."""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return
    scene.paintr2.ui.show_targets_panel = bool(is_open)
    session.tag_redraw()


def _set_display_panel(is_open, page='DISPLAY'):
    """Open, close or turn the page of the left panel."""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return

    ui_state = scene.paintr2.ui
    ui_state.show_left_panel = bool(is_open)
    if is_open:
        ui_state.left_panel_page = page
    session.tag_redraw()


def _display_action(action, *args):
    """Everything in the Display panel that is not a plain property write."""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return

    if action == 'hdri_browse':
        # INVOKE, not EXEC: the operator's whole job is to put Blender's file
        # browser up, and that only happens on invoke.
        try:
            bpy.ops.paintr2.hdri_open('INVOKE_DEFAULT')
        except RuntimeError as error:
            print(f"[PaintR2] open HDRI failed: {error}")

    elif action == 'hdri_reset':
        settings = scene.paintr2.display
        for key in ("rotation_x", "rotation_y", "rotation_z"):
            setattr(settings, key, 0.0)

    elif action == 'bake_maps':
        _bake_mesh_maps()

    elif action == 'bake_export':
        _bake_and_export()

    elif action == 'browse':
        # ('browse', owner, key) from a path row. One destination so far, and
        # naming it here rather than in the toolkit is the point: gui/ knows
        # it asked to browse, not what a folder is.
        if args[:2] == ('scene', "export_directory"):
            try:
                bpy.ops.paintr2.export_browse('INVOKE_DEFAULT')
            except RuntimeError as error:
                print(f"[PaintR2] export browse failed: {error}")

    elif action == 'save_layers':
        _run("save layers", bpy.ops.paintr2.save_layers)

    elif action == 'pack_layers':
        _run("pack layers", bpy.ops.paintr2.pack_layers)

    elif action == 'unpack_layers':
        _run("unpack layers", bpy.ops.paintr2.unpack_layers)

    elif action == 'view_map':
        _view_mesh_map(args[0] if args else "")

    elif action == 'show_all':
        _run("show hidden objects", bpy.ops.paintr2.show_all)

    elif action == 'isolate':
        _run("isolate the paint set", bpy.ops.paintr2.isolate_refresh)

    session.tag_redraw()


def _bake_and_export():
    """Bake and write the textures, then say on the status strip what landed.

    The saying is half the command. The operator reports through
    `self.report`, which in this window reaches a status bar one pixel wide
    (see `notify`), so the result is put where the user is actually looking.

    Counted before the bake, not after: `_export_targets` walks the paint set
    and reads each material's channels, and the bake leaves a fresh image
    datablock per channel behind it -- asking afterwards would be counting the
    output of the thing being described rather than describing it.
    """
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return

    targets, channels = _export_targets(bpy.context)
    expected = sum(len(channels.get(obj.name, ())) for obj in targets)
    where = bpy.path.abspath(scene.paintr2.export_directory)

    try:
        bpy.ops.paintr2.bake_texture(
            save_files=True,
            active_only=bool(scene.paintr2.export_active_only),
        )
    except RuntimeError as error:
        print(f"[PaintR2] bake and export failed: {error}")
        notify(f"Export failed: {error}")
        return

    notify(f"Exported {expected} texture(s) to {where}")


def _bake_mesh_maps():
    """Run whichever baker the settings ask for, from inside the window.

    The whole reason the Bake page exists: getting a map right is bake, look,
    change a number, bake again -- and every one of those loops used to leave
    the window.
    """
    from . import external

    props = getattr(bpy.context.scene, "paintr2", None)
    if props is None:
        return

    use_external = (
        props.mesh_map_backend != 'CYCLES' and external.available() is not None
    )
    if use_external:
        # INVOKE, so the modal version runs and the window keeps drawing --
        # a blocking bake here would freeze the interface it was started from.
        try:
            bpy.ops.paintr2.bake_mesh_maps_external('INVOKE_DEFAULT')
            return
        except RuntimeError as error:
            print(f"[PaintR2] external bake failed: {error}")

    _run("bake mesh maps", bpy.ops.paintr2.bake_mesh_maps)


def _view_mesh_map(identifier):
    """Put a baked map on the model, or take it off again.

    An empty identifier means "back to the stack". Clicking the map already
    being shown means the same thing -- the eye is a switch, and a switch you
    can only turn on is a trap.
    """
    from . import meshmaps
    from . import paint

    document = paint.active_document(bpy.context)
    if document is None:
        return

    obj = getattr(bpy.context, "active_object", None)
    image = meshmaps.stored(obj, identifier) if (obj and identifier) else None

    showing = document.shader_mode == 'MESH_MAP'
    if image is None or (showing and document.preview_map == image):
        if showing:
            document.shader_mode = document.shader_mode_previous or 'PBR'
        return

    if not showing:
        # Remembered so the eye can put back what was there, rather than
        # dropping everyone into PBR whatever they were looking at.
        document.shader_mode_previous = document.shader_mode

    document.preview_map = image
    document.shader_mode = 'MESH_MAP'


# --- the hover preview -------------------------------------------------------
#
# What a click *would* fill, shown before the click. For Polygon Fill this is
# a nicety; for Mesh Fill and UV Island Fill it is the tool, because neither
# the mesh part nor the UV island under the pointer is something you can see
# by looking at the model.
#
# Drawn as a 3D batch rather than by projecting the faces to region pixels
# every frame: a mesh part can be a hundred thousand triangles, and projecting
# those in Python sixty times a second is not a thing that can be made to
# work. The vertex array is built once per hovered face set and reused until
# the pointer crosses into another one.

# {"key", "matrix", "verts"} of what is under the pointer, or None.
_fill_preview = None
# The box or lasso being drawn, as a closed polygon in region pixels.
_fill_shape = None
# (key, GPUBatch) -- built inside the draw handler, where there is a context.
_fill_preview_batch = None
_fill_preview_shader = None


def _preview_key(mesh, face_index, scope, triangle_index, topology=None):
    """What makes two hovers the same face set, so the batch can be kept."""
    if scope == 'TRIANGLE':
        return ('TRIANGLE', triangle_index)
    if scope == 'FACE':
        return ('FACE', face_index)
    if scope == 'OBJECT':
        return ('OBJECT', 0)

    if topology is None:
        from . import fill as fill_mod
        topology = fill_mod.topology(mesh)

    source = topology.island_of if scope == 'ISLAND' else topology.part_of
    if face_index >= len(source):
        return (scope, face_index)
    return (scope, int(source[face_index]))


def clear_fill_preview():
    """Returns True when there was something to clear, i.e. redraw."""
    global _fill_preview
    if _fill_preview is None:
        return False
    _fill_preview = None
    return True


def _preview_ray(region, rv3d, x, y):
    from .ops.op_fill import _ray

    return _ray(region, rv3d, x, y)


def update_fill_preview(context, region, rv3d, x, y):
    """Point the preview at whatever is under (x, y). True when it moved."""
    global _fill_preview

    from . import fill as fill_mod
    from .ops.op_fill import hit_test

    if not is_fill_tool(context) or region is None or rv3d is None:
        return clear_fill_preview()

    # In box or lasso mode the pointer is not aimed at a face -- it is a
    # corner of a shape. A highlight under it would be answering a question
    # nobody asked, and it would say the wrong faces.
    if fill_select_mode(context) != 'CLICK':
        return clear_fill_preview()

    try:
        hit = hit_test(context, region, rv3d, x, y)
    except (AttributeError, RuntimeError, ReferenceError):
        return clear_fill_preview()

    if hit is None:
        return clear_fill_preview()

    obj, face_index, local = hit
    mesh = obj.data
    scope = fill_scope(context)

    # The mirror planes turn one hover into as many as eight face sets, and the
    # highlight has to show all of them: one that showed a single face while
    # the click filled three would be worse than no highlight at all.
    targets = fill_mod.click_targets(obj, face_index, local, mirror_axes(context))

    topology = None
    if scope in ('PART', 'ISLAND'):
        topology = fill_mod.topology(mesh)

    keys, faces, triangles = [], [], []
    for index, point in targets:
        triangle = None
        if scope == 'TRIANGLE':
            triangle = fill_mod.triangle_index_at(mesh, index, point)

        sub = _preview_key(mesh, index, scope, triangle, topology)
        if sub in keys:
            continue
        keys.append(sub)

        if scope == 'TRIANGLE':
            if triangle is not None:
                triangles.append(triangle)
        else:
            faces.append(fill_mod.face_set(mesh, index, scope))

    key = (obj.name, scope) + tuple(keys)

    matrix = obj.matrix_world.copy()
    if _fill_preview is not None and _fill_preview["key"] == key:
        # The set is the same but the object may have been moved since.
        if _fill_preview["matrix"] == matrix:
            return False
        _fill_preview["matrix"] = matrix
        return True

    # One call for every set at once: the mesh's coordinate and corner arrays
    # are read on each one, and reading them eight times per pointer move is
    # the sort of thing that turns a highlight into a stutter.
    if scope == 'TRIANGLE':
        vertices = fill_mod.face_triangles_3d(mesh, triangles=triangles)
    elif faces:
        chosen = np.unique(np.concatenate(faces))
        # The preview answers "what would this click fill", so it has to make
        # the same cut the fill will -- a highlight that shows the far side of
        # the model lighting up and then does not fill it is a lie.
        settings = _fill_settings(context)
        if getattr(settings, "front_only", False) and scope in ('PART', 'ISLAND'):
            _origin, direction = _preview_ray(region, rv3d, x, y)
            chosen = fill_mod.front_facing(obj, chosen, direction)
        vertices = fill_mod.face_triangles_3d(mesh, chosen)
    else:
        vertices = fill_mod.face_triangles_3d(mesh, [])

    if len(vertices) == 0:
        return clear_fill_preview()

    _fill_preview = {"key": key, "matrix": matrix, "verts": vertices}
    return True


def _draw_fill_preview(context):
    """Wash the hovered face set over the model, clipped to the viewport gap."""
    global _fill_preview_batch, _fill_preview_shader

    state = session.state
    if _fill_preview is None or state.root is None or bpy.app.background:
        return

    viewport = state.root.viewport
    if not viewport.visible or viewport.width <= 0 or viewport.height <= 0:
        return

    space = getattr(context, "space_data", None)
    rv3d = getattr(space, "region_3d", None) if space is not None else None
    if rv3d is None:
        return

    if _fill_preview_shader is None:
        _fill_preview_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader = _fill_preview_shader

    if _fill_preview_batch is None or _fill_preview_batch[0] != _fill_preview["key"]:
        from gpu_extras.batch import batch_for_shader

        vertices = _fill_preview["verts"].reshape(-1, 3)
        _fill_preview_batch = (
            _fill_preview["key"],
            batch_for_shader(shader, 'TRIS', {"pos": vertices}),
        )

    colour = theme.ACCENT[:3] + (0.35,)

    gpu.state.blend_set('ALPHA')
    # Front faces only. Without a depth test the far side of a closed mesh
    # would glow through the near side and the highlight would read as twice
    # the region it is.
    gpu.state.face_culling_set('BACK')
    gpu.state.scissor_test_set(True)
    region = getattr(context, "region", None)
    gpu.state.scissor_set(
        (region.x if region else 0) + viewport.x,
        (region.y if region else 0) + viewport.y,
        viewport.width,
        viewport.height,
    )
    try:
        # BOTH stacks. `push_pop()` restores the model-view matrix and nothing
        # else -- the projection needs `push_pop_projection()` of its own, and
        # without it the viewport's perspective matrix stayed loaded for the
        # rest of the frame. Everything drawn after this point is in pixels,
        # so the whole interface was projected into the far distance and the
        # window went blank the moment the pointer touched the model. Measured
        # in 5.2.1: after `with push_pop(): load_projection_matrix(m)` the
        # projection is still `m`.
        with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
            gpu.matrix.load_matrix(rv3d.view_matrix @ _fill_preview["matrix"])
            gpu.matrix.load_projection_matrix(rv3d.window_matrix)
            shader.uniform_float("color", colour)
            _fill_preview_batch[1].draw(shader)
    finally:
        gpu.state.scissor_test_set(False)
        gpu.state.face_culling_set('NONE')
        gpu.state.blend_set('NONE')


# --- the brush popovers ------------------------------------------------------
#
# Blender's tool header carries four popovers -- Brush, Stroke, Falloff and
# Cursor -- and that header is kept out of our window, so they are rebuilt
# here. Not approximated: every row below is the same RNA property Blender's
# own panel writes, drawn under the same conditions, in the same order. The
# sources are `bl_ui/properties_paint_common.py` (StrokePanel, FalloffPanel,
# DisplayPanel, brush_settings_advanced) and `space_view3d_toolbar.py`.
#
# The spec is plain dicts because `gui/` is not allowed to touch bpy. It is
# rebuilt on every redraw, which is what makes the conditional rows work:
# Blender shows Spacing only for the Space stroke method, and so do we,
# because on any other method the row is simply not in the list.

# The enum lists RNA narrows behind a callback Python has no way to call,
# by (owner, property). Filled by `prime_enum_items` when the window opens.
_ENUM_ACCEPTED = {}

# The rows whose choices are narrower than the property declares. Both of the
# brush's texture slots are asked, so which one narrows is measured rather
# than remembered here.
_NARROWED_ENUMS = (('tex', "map_mode"), ('tex_mask', "mask_map_mode"))


def prime_enum_items():
    """Measure the enum lists RNA narrows by paint mode.

    `map_mode` declares six mappings and a texture paint brush takes five --
    Area Plane is sculpt's. RNA narrows the list with a callback, and from
    Python only the declared list is readable, so a dropdown built straight
    off `prop.enum_items` offers a mapping that cannot be picked.

    So the answer is measured: each identifier is offered to a scratch brush
    of the mode this window paints in, and the ones it takes are kept. Two
    things make this a function called at window open rather than something a
    row works out for itself. It makes a datablock, and a datablock must not
    be made inside a draw handler -- which is where every panel spec is built.
    And the answer cannot change while Blender is running, so measuring it
    once is measuring it as often as it is worth measuring.

    A scratch brush rather than the user's own: nothing here should leave a
    mark on a brush somebody is painting with. If one cannot be made the
    cache stays empty and the rows fall back to the declared list -- one item
    too many, which writes nothing and says so in the console, is a better
    failure than a Mapping row with no mappings in it.
    """
    from . import paint

    if _ENUM_ACCEPTED:
        return

    try:
        probe = bpy.data.brushes.new(name="PaintR2 enum probe",
                                     mode=paint.PAINT_MODE)
    except (RuntimeError, TypeError) as error:
        print(f"[PaintR2] could not measure enum items: {error}")
        return

    try:
        for owner, key in _NARROWED_ENUMS:
            slot = (probe.mask_texture_slot if owner == 'tex_mask'
                    else probe.texture_slot)
            prop = slot.bl_rna.properties.get(key)
            if prop is None:
                continue
            accepted = set()
            for item in prop.enum_items:
                try:
                    setattr(slot, key, item.identifier)
                except (TypeError, ValueError):
                    continue
                accepted.add(item.identifier)
            _ENUM_ACCEPTED[(owner, key)] = accepted
    finally:
        bpy.data.brushes.remove(probe)


# Which `bpy.data` collection holds a pointer row's choices, by the RNA type
# the property points at. One line per pointer row that exists; the brush's
# two texture slots are the first.
_ID_COLLECTIONS = {'Texture': "textures", 'Image': "images"}

# Panel widths, from Blender's own `bl_ui_units_x` (one unit is 20px).
_PANEL_WIDTHS = {'BRUSH': 280, 'TEXTURE': 300, 'MASK_TEXTURE': 300,
                 'STROKE': 280, 'FALLOFF': 260, 'CURSOR': 330, 'OPTIONS': 260}

# How many points the falloff curve is sampled at for drawing. Enough that
# the polyline reads as a curve rather than a chain of segments.
_CURVE_SAMPLES = 48


def _panel_owner(owner, key="", context=None):
    """The datablock a row writes to.

    `'auto'` is Blender's own rule for the properties unified paint settings
    can take over -- colour jitter, input samples -- and goes through
    `paint.brush_owner`, the same test `UnifiedPaintPanel.prop_unified` makes.
    `'fill'` is ours: the fill tools' own settings, which Blender has no
    equivalent of. `'proc'` and `'proc_mask'` are the active layer's two
    procedural blocks -- the pattern it draws and the pattern masking it.
    """
    from . import paint

    context = context or bpy.context

    if owner == 'unified':
        return paint.unified_settings(context)
    if owner == 'settings':
        tool_settings = getattr(context.scene, "tool_settings", None)
        return None if tool_settings is None else tool_settings.image_paint
    if owner in ('tex', 'tex_mask'):
        # The brush's two texture slots -- the pattern a stroke stamps, and the
        # one that decides how much of it lands. Both are `BrushTextureSlot`,
        # so one branch serves both panels and the rows are the same rows.
        brush = paint.active_brush(context)
        if brush is None:
            return None
        return brush.texture_slot if owner == 'tex' else brush.mask_texture_slot
    if owner in ('tex_data', 'tex_mask_data'):
        # The texture datablock in that slot -- what the texture *is*, as
        # against where the slot puts it. Blender keeps this in the Properties
        # editor's Texture tab, which this window puts out of reach.
        brush = paint.active_brush(context)
        if brush is None:
            return None
        return brush.texture if owner == 'tex_data' else brush.mask_texture
    if owner == 'auto':
        return paint.brush_owner(key, context)
    if owner == 'fill':
        return _fill_settings(context)
    if owner == 'adjust':
        document = paint.active_document(context)
        layer = document.active_layer if document is not None else None
        return None if layer is None else layer.adjustment
    if owner in ('proc', 'proc_mask'):
        document = paint.active_document(context)
        layer = document.active_layer if document is not None else None
        if layer is None:
            return None
        return layer.procedural if owner == 'proc' else layer.mask_procedural
    if owner.startswith('ch:'):
        # One channel record of the active layer. The identifier travels in
        # the owner string so the row is written once for all five.
        document = paint.active_document(context)
        layer = document.active_layer if document is not None else None
        if layer is None:
            return None
        return layer.channel(owner[3:])
    if owner == 'doc':
        return paint.active_document(context)
    if owner in ('effect', 'effect_filter', 'effect_pattern'):
        document = paint.active_document(context)
        effect = document.active_effect_item() if document else None
        if effect is None:
            return None
        if owner == 'effect_filter':
            return effect.filter
        if owner == 'effect_pattern':
            return effect.pattern
        return effect
    if owner == 'scene':
        # The scene-level PaintR2 block: bake resolution, which maps, samples.
        # Written from the window's Bake panel and from the sidebar alike --
        # the same property, so the two can never disagree.
        scene = getattr(context, "scene", None)
        return None if scene is None else scene.paintr2
    if owner == 'display':
        scene = getattr(context, "scene", None)
        return None if scene is None else scene.paintr2.display
    if owner == 'view':
        # Blender's colour management. It belongs beside the shader modes
        # because it is the other half of "show me the colour I painted": an
        # unlit surface still comes back through the view transform, and AgX
        # is not the colour that was written.
        return getattr(context.scene, "view_settings", None)
    return paint.active_brush(context)


def _ui_step(prop):
    """How much one click of a number field's arrow moves the value.

    Integers step by one -- that is what Blender's arrows do, whatever the
    property's drag step says. Floats step by a hundredth of their soft range,
    which lands on the same 0.01 Blender uses for a 0..1 factor.
    """
    if prop.type == 'INT':
        return 1
    if prop.subtype == 'ANGLE':
        # One degree, in the radians the property is stored in. A hundredth of
        # a -360..360 range would be seven degrees a click.
        return math.radians(1.0)
    span = float(prop.soft_max) - float(prop.soft_min)
    if span <= 0.0 or span > 1e6:
        return 0.01
    return span / 100.0


def _row(owner, key, label=None, text=None, slider=False, active=True,
         suffix=(), kind=None, index=None, context=None):
    """One property row, described from the RNA definition itself.

    Range, precision, subtype and tooltip are read off the property rather
    than typed here, so a row cannot drift from what Blender would draw.
    Returns None when the property is not there to be drawn.

    `index` picks one component out of a vector -- Offset X, Size Y. The
    component travels inside the row's key, as `offset[1]`, the way a channel
    travels inside the owner string: the write channel carries a name and
    nothing else, and three rows sharing one name would be one row as far as
    a drag in progress could tell.
    """
    holder = _panel_owner(owner, key, context)
    if holder is None:
        return None

    prop = holder.bl_rna.properties.get(key)
    if prop is None:
        return None

    try:
        value = getattr(holder, key)
    except AttributeError:
        return None

    row = {
        "owner": owner,
        "key": key if index is None else f"{key}[{index}]",
        "label": prop.name if label is None else label,
        # Blender's tooltip leads with the property's name and follows with
        # its description; a few properties carry no description at all, and
        # falling back to the name is better than a row that says nothing.
        "desc": prop.description or prop.name,
        "active": bool(active),
        "suffix": list(suffix),
    }

    if prop.type == 'BOOLEAN':
        row.update(kind='bool', value=bool(value),
                   text=prop.name if text is None else text)
        row["label"] = ""
        return row

    if prop.type == 'ENUM':
        items = [(item.identifier, item.name) for item in prop.enum_items]
        accepted = _ENUM_ACCEPTED.get((owner, key))
        if accepted is not None:
            items = [pair for pair in items if pair[0] in accepted]
        row.update(kind=kind or 'enum', value=value, items=items)
        return row

    if prop.type == 'STRING':
        # A folder is not typed nearly as often as it is picked, so a path row
        # carries a browse button and Blender's own file browser behind it.
        # The text is still editable -- "//textures/" is faster to type than
        # to navigate to, and it is the default for a reason.
        subtype = prop.subtype
        row.update(
            kind=kind or ('path' if subtype in {'DIR_PATH', 'FILE_PATH'} else 'text'),
            value=str(value),
            directory=subtype == 'DIR_PATH',
        )
        return row

    if prop.type in {'INT', 'FLOAT'}:
        if prop.array_length >= 3 and index is None:
            row.update(kind='color', value=tuple(value))
            return row
        if index is not None and prop.array_length:
            value = value[index]
        row.update(
            kind='value',
            value=float(value),
            min=float(prop.soft_min),
            max=float(prop.soft_max),
            step=_ui_step(prop),
            precision=int(getattr(prop, "precision", 3) or 3),
            subtype=prop.subtype,
            int=prop.type == 'INT',
            slider=bool(slider) or prop.subtype == 'FACTOR',
        )
        unit = getattr(prop, "unit", 'NONE')
        display = _unit_display(prop, float(value), context)
        if display is not None:
            row.update(unit=unit, display=display)
        return row

    return None


def _unit_display(prop, value, context):
    """What Blender writes in a field carrying a unit -- "5 cm", "0 m".

    A length is not printed with a format string: which unit it lands in
    depends on the scene's unit system and on how big the number is. So
    Blender's own formatter does it, and `gui/props.py` prints the string it
    gets rather than deciding anything.

    Rotation is left out on purpose: `PropertyPanel.format_value` already
    turns radians into degrees, which is the same answer by a shorter road and
    is what the falloff and jitter rows have been showing all along.

    Returns None when the property has no unit, or when the scene has units
    switched off -- both of which mean a plain number.
    """
    if getattr(prop, "unit", 'NONE') != 'LENGTH':
        return None

    settings = getattr(getattr(context or bpy.context, "scene", None),
                       "unit_settings", None)
    system = getattr(settings, "system", 'NONE')
    if system == 'NONE':
        return None

    try:
        return bpy.utils.units.to_string(
            system, 'LENGTH', value * float(settings.scale_length),
            precision=int(getattr(prop, "precision", 3) or 3),
        )
    except (ValueError, TypeError):
        return None


def _vector_rows(owner, key, context, active=True):
    """A three-component property, one row per axis.

    Blender's own layout under property split: the first row carries the
    property's name and the two under it carry only the axis, because they
    are plainly the rest of the same thing. Which is why this reads
    "Offset X / Y / Z" rather than the name three times.
    """
    holder = _panel_owner(owner, key, context)
    prop = None if holder is None else holder.bl_rna.properties.get(key)
    if prop is None:
        return []

    return _compact([
        _row(owner, key, index=index, active=active, context=context,
             label=f"{prop.name} {axis}" if index == 0 else axis)
        for index, axis in enumerate("XYZ"[:prop.array_length])
    ])


def _idblock_row(owner, key, context, buttons=('new',), active=True):
    """A datablock pointer, drawn the way Blender's `template_ID` is.

    A dropdown listing what `bpy.data` holds of that type, the buttons that
    put something in the slot, and an X that empties it. The list is built
    here rather than in the panel for the reason every other row is: `gui/`
    never touches bpy, so what it gets is names.

    `buttons` names the ways in, in order. A texture can be made from nothing,
    so it gets `new`; an image cannot -- a blank generated image is not a
    brush texture -- so it gets `open` and a file browser instead.

    Every button carries the property it is about, so the Texture and Texture
    Mask panels are the same row twice with nothing to tell apart.
    """
    holder = _panel_owner(owner, key, context)
    if holder is None:
        return None

    prop = holder.bl_rna.properties.get(key)
    if prop is None or prop.type != 'POINTER':
        return None

    current = getattr(holder, key, None)
    collection = _ID_COLLECTIONS.get(
        getattr(getattr(prop, "fixed_type", None), "identifier", "")
    )
    if collection is None:
        return None

    return {
        "kind": 'idblock',
        "owner": owner,
        "key": key,
        "label": "",
        "desc": prop.description or prop.name,
        "active": bool(active),
        "value": "" if current is None else current.name,
        "items": [(block.name, block.name)
                  for block in getattr(bpy.data, collection)],
        "buttons": [dict(_IDBLOCK_BUTTONS[name],
                         action=(_IDBLOCK_BUTTONS[name]["action"], owner, key))
                    for name in buttons],
        "unlink": ('idblock_unlink', owner, key),
        "suffix": [],
    }


# The ways something gets into a datablock slot: the label the button carries
# while the slot is empty, the icon it shrinks to once it is full, and the
# action it sends. Named, so a row asks for "new" rather than spelling a
# button out -- and so the same button reads the same on every panel.
_IDBLOCK_BUTTONS = {
    'new': {"label": "New", "icon": 'PLUS', "action": 'idblock_new',
            "desc": "Add a new data-block"},
    'open': {"label": "Open", "icon": 'FOLDER', "action": 'idblock_open',
             "desc": "Open an image file from disk"},
}


def _toggle(owner, key, icon, context=None, alt_icon=None):
    """One of the small square buttons Blender aligns onto the end of a row."""
    holder = _panel_owner(owner, key, context)
    if holder is None:
        return None
    prop = holder.bl_rna.properties.get(key)
    if prop is None:
        return None

    value = bool(getattr(holder, key, False))
    return {
        "owner": owner,
        "key": key,
        "value": value,
        "icon": (alt_icon if (alt_icon and not value) else icon),
        "desc": prop.description or prop.name,
    }


def _separator():
    return {"kind": 'separator'}


def _compact(rows):
    """Drop the rows that could not be built, and any separator left dangling."""
    out = []
    for row in rows:
        if row is None:
            continue
        if row.get("kind") == 'separator' and (not out or out[-1].get("kind") == 'separator'):
            continue
        out.append(row)
    while out and out[-1].get("kind") == 'separator':
        out.pop()
    return out


def _brush_panel_rows(context, brush):
    """The Brush popover: `brush_settings_advanced` for texture paint."""
    rows = []

    if brush.image_brush_type == 'FILL' and brush.color_type == 'GRADIENT':
        rows.append(_row('brush', "gradient_fill_mode", kind='enum_row',
                         context=context))
        rows.append(_separator())

    advanced = [
        _row('brush', "image_brush_type", label="Brush Type", context=context),
        _row('brush', "use_alpha", context=context),
    ]

    capabilities = brush.image_paint_capabilities
    if getattr(capabilities, "has_accumulate", False):
        advanced.append(_row('brush', "use_accumulate", context=context))

    if brush.image_brush_type == 'SOFTEN':
        advanced.append(_row('brush', "direction", kind='enum_row', context=context))
        advanced.append(_row('brush', "sharp_threshold", context=context))
        advanced.append(_row('brush', "blur_mode", context=context))
    elif brush.image_brush_type == 'MASK':
        advanced.append(_row('brush', "weight", label="Mask Value", slider=True,
                             context=context))

    advanced.append(_separator())
    advanced.append(_color_jitter_rows(context))

    rows.append({
        "kind": 'panel',
        "key": "brush_advanced",
        "label": "Advanced",
        "open": True,
        "rows": _compact(advanced),
    })
    return _compact(rows)


def _color_jitter_rows(context):
    """`draw_color_jitter_panel`: three sliders, each with two modifiers."""
    from . import paint

    settings = paint.unified_settings(context)
    unified = bool(getattr(settings, "use_unified_color", False))
    on = bool(_panel_value('auto', "use_color_jitter", context))

    rows = []
    for key, label in (
        ("hue_jitter", "Hue"),
        ("saturation_jitter", "Saturation"),
        ("value_jitter", "Value"),
    ):
        suffix = "hue" if key == "hue_jitter" else ("sat" if key.startswith("sat") else "val")
        rows.append(_row(
            'auto', key, label=label, slider=True, active=on, context=context,
            suffix=_compact([
                _toggle('auto', f"use_stroke_random_{suffix}", 'STROKE', context),
                _toggle('auto', f"use_random_press_{suffix}", 'PRESSURE', context),
            ]),
        ))

    return {
        "kind": 'panel',
        "key": "color_jitter",
        "label": "Randomize Color" + ("" if unified else " (brush)"),
        "open": False,
        "toggle": _toggle('auto', "use_color_jitter", 'BRUSH', context),
        "rows": _compact(rows),
    }


def _panel_value(owner, key, context, default=None):
    holder = _panel_owner(owner, key, context)
    return default if holder is None else getattr(holder, key, default)


def _texture_angle_rows(owner, brush, slot, context):
    """Angle, Rake, Random and Random Angle -- the four rows Blender groups.

    Every one of them is behind something the slot reports about itself: a
    mapping with no angle to speak of (Tiled, 3D) draws none of them, one with
    no angle *source* draws only the angle, and Random needs the brush to
    support a random angle at all. Asked rather than listed, so a mapping that
    changes what it supports changes the panel with it.
    """
    if not getattr(slot, "has_texture_angle", False):
        return []

    rows = [_row(owner, "angle", label="Angle", context=context)]
    if not getattr(slot, "has_texture_angle_source", False):
        return rows

    rows.append(_row(owner, "use_rake", text="Rake", context=context))

    if (getattr(brush.brush_capabilities, "has_random_texture_angle", False)
            and getattr(slot, "has_random_texture_angle", False)):
        rows.append(_row(owner, "use_random", text="Random", context=context))
        if slot.use_random:
            rows.append(_row(owner, "random_angle", label="Random Angle",
                             context=context))

    return rows


def _stencil_rows(owner, texture):
    """The two buttons a stencil mapping gets, and only then.

    Stencil is the one mapping the texture has a place on screen for, so it is
    the one that can be fitted to an image and reset. Fitting needs an image
    to take the aspect from, which is why it comes and goes with the texture's
    own type.
    """
    mask = owner == 'tex_mask'
    rows = []
    if texture is not None and texture.type == 'IMAGE':
        rows.append({"kind": 'button', "label": "Fit Stencil to Image",
                     "action": ('stencil_fit', mask)})
    rows.append({"kind": 'button', "label": "Reset Transform",
                 "action": ('stencil_reset', mask)})
    return rows


def _texture_data_rows(owner, texture, context):
    """The texture datablock's own settings: what it *is*, not where it lands.

    Blender keeps these in the Properties editor's Texture tab, and that tab
    is kept out of this window -- so without them a brush texture made here
    is a datablock with nothing in it and no way to fill it. Which is exactly
    what the first version shipped: New made an Image texture holding no
    image, and nothing on the panel could reach it.

    Returns nothing at all when the slot is empty. There is no texture to
    describe, and a sub-panel that opens onto "Type: None" is a row that
    exists to be disappointing.
    """
    if texture is None:
        return []

    rows = [
        _preview_row(texture),
        _row(owner, "type", label="Type", context=context),
    ]

    if texture.type == 'IMAGE':
        # The one row this panel exists for. `open` rather than `new`: a blank
        # generated image is not a brush texture, a file on disk is.
        rows.append(_idblock_row(owner, "image", context, buttons=('open',)))

    rows.append(_type_param_rows(owner, texture, context))

    rows.append(_separator())
    for key in ("intensity", "contrast", "saturation"):
        rows.append(_row(owner, key, context=context))
    rows.append(_row(owner, "use_clamp", context=context))

    return [{
        "kind": 'panel',
        # Keyed by owner, not by the texture's name: renaming a texture must
        # not fold the panel the user had open.
        "key": f"{owner}_data",
        "label": texture.name,
        "open": True,
        "rows": _compact(rows),
    }]


# What each texture's preview was made from, and the texture made of it:
# {datablock name: (fingerprint, GPUTexture or None)}.
_preview_cache = {}

# How many samples across a preview. Blender's own is 128; at the size this
# draws -- a square inside a 300px panel -- 64 is already more than the screen
# has room for, and 64x64 costs 2.5ms to fill. Measured.
_PREVIEW_SAMPLES = 64

# The properties every texture has that still change what it looks like. The
# rest of the fingerprint is the type's own, which is the same subtraction
# `_type_param_rows` draws.
_PREVIEW_BASE_KEYS = frozenset({
    "type", "intensity", "contrast", "saturation",
    "factor_red", "factor_green", "factor_blue", "use_clamp", "use_color_ramp",
})


def _preview_row(texture):
    """The square at the top of a texture's settings: what it actually makes.

    Blender draws this with `template_preview`, which hands the work to its
    preview renderer -- a job that fills `texture.preview` for textures its
    own UI is drawing, and for no others. Measured: `preview_ensure()` on a
    Clouds texture nothing is showing comes back 0x0 with no pixels in it. So
    that route is no use to a window that draws none of Blender's UI.

    The texture is asked directly instead, which is the better answer anyway:
    what is on screen is the function a stroke reads, not a render of it.
    """
    return {
        "kind": 'preview',
        "texture": _texture_preview(texture),
        "desc": "What this texture produces, sampled from the texture itself",
    }


def _texture_preview(texture):
    """A GPUTexture of `texture`, or None when there is no drawing it.

    `Texture.evaluate` reads the texture at a point -- the same function the
    stroke code reads it through -- and 64x64 of those costs 2.5ms, paid only
    when one of the texture's own settings has moved since the last one.

    DANGER, the same one `_texture_for` carries: a GPU texture must not be
    made without a GPU context. Draw handler only, and the background guard
    is not optional.
    """
    if texture is None or bpy.app.background:
        return None

    fingerprint = _texture_fingerprint(texture)
    cached = _preview_cache.get(texture.name)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    size = _PREVIEW_SAMPLES
    try:
        pixels = _preview_pixels(texture, size)
        buffer = gpu.types.Buffer('FLOAT', len(pixels), pixels)
        result = gpu.types.GPUTexture((size, size), format='RGBA16F',
                                      data=buffer)
    except (RuntimeError, SystemError, ValueError, TypeError) as error:
        print(f"[PaintR2] could not preview {texture.name}: {error}")
        result = None

    if len(_preview_cache) > 32:
        # Keyed by name, so renaming a texture leaves the old key behind.
        # Cheaper to sweep them when there are enough to notice than to hunt
        # for a key that cannot be reused the moment a datablock is freed.
        live = {block.name for block in bpy.data.textures}
        for stale in [name for name in _preview_cache if name not in live]:
            del _preview_cache[stale]

    _preview_cache[texture.name] = (fingerprint, result)
    return result


def _preview_pixels(texture, size):
    """RGBA floats for a `size` x `size` preview, bottom row first.

    `evaluate` hands back the colour in the first three components and the
    intensity in the fourth, and a greyscale texture leaves the colour at
    zero. Which of the two is the picture is therefore measured rather than
    assumed: if the colour never moves off black while the intensity does,
    the intensity is what there is to show.

    Sampled across -1..1, which is where a procedural texture at its default
    size has something to say -- Clouds out of the box covers 0.16 to 0.89
    over that square, measured.
    """
    step = 2.0 / max(1, size - 1)
    samples = []
    coloured = False

    for row in range(size):
        y = -1.0 + row * step
        for column in range(size):
            value = texture.evaluate((-1.0 + column * step, y, 0.0))
            samples.append(tuple(value))
            if not coloured and max(value[0], value[1], value[2]) > 1e-4:
                coloured = True

    pixels = []
    for value in samples:
        if coloured:
            pixels.extend((value[0], value[1], value[2], 1.0))
        else:
            pixels.extend((value[3], value[3], value[3], 1.0))
    return pixels


def _texture_fingerprint(texture):
    """Everything about a texture that changes what it looks like.

    Two halves: what its own type declares, and the few on the base class that
    colour the result. Nothing else is reachable from this window, so nothing
    else can go stale behind the cache -- the colour ramp is the one to
    remember if it is ever drawn, because its elements are not in here.
    """
    base = set(bpy.types.Texture.bl_rna.properties.keys())

    values = []
    for prop in texture.bl_rna.properties:
        key = prop.identifier
        if prop.is_readonly:
            continue
        if key in base and key not in _PREVIEW_BASE_KEYS:
            continue
        values.append((key, _fingerprint_value(getattr(texture, key, None))))
    return tuple(values)


def _fingerprint_value(value):
    """One property, in something that can be compared and kept.

    A datablock goes in by name: swapping the image in a texture changes the
    preview, and holding the datablock itself would keep it alive after the
    user deleted it.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "name"):
        return value.name
    if hasattr(value, "__len__"):
        return tuple(value)
    return repr(value)


def release_previews():
    """Drop every cached preview. Called on unregister, with the icons."""
    _preview_cache.clear()


def _type_param_rows(owner, texture, context):
    """The rows this texture's own type adds to the ones every texture has.

    `bpy.types.Texture` is what they all share; a Clouds texture is that plus
    six, an Image texture that plus twenty. Subtracting one from the other is
    Blender's per-type panel, near enough, and costs no list here to go stale
    when a type gains a setting.

    Open for a procedural, closed for an image: for Clouds or Voronoi these
    parameters *are* the texture, while for an image they are sampling details
    and the row that matters is already out above.
    """
    base = set(bpy.types.Texture.bl_rna.properties.keys())

    rows = _compact([
        _row(owner, prop.identifier, context=context)
        for prop in texture.bl_rna.properties
        # `image` is drawn above, by name, because it is not a detail.
        if prop.identifier not in base and prop.identifier != "image"
        and not prop.is_readonly
    ])
    if not rows:
        return None

    return {
        "kind": 'panel',
        "key": f"{owner}_params",
        "label": "Settings",
        "open": texture.type != 'IMAGE',
        "rows": rows,
    }


def _texture_panel_rows(context, brush):
    """The Texture popover: `VIEW3D_PT_tools_brush_texture`.

    The brush's own texture -- the pattern a stroke stamps. Blender picks the
    datablock with `template_ID_preview`, a grid of thumbnails; this is the
    same choice drawn as the list it is, because a preview grid needs Blender's
    preview images in a GPU texture and `gpu.texture.from_image` is the one
    call this window cannot make freely. Same property either way, so the
    pick lands in the same place.
    """
    slot = brush.texture_slot

    rows = [_idblock_row('brush', "texture", context)]
    rows.extend(_texture_data_rows('tex_data', brush.texture, context))
    rows.extend([
        _separator(),
        _row('tex', "map_mode", label="Mapping", context=context),
        _separator(),
    ])

    if slot.map_mode == 'STENCIL':
        rows.extend(_stencil_rows('tex', brush.texture))
        rows.append(_separator())

    rows.extend(_texture_angle_rows('tex', brush, slot, context))
    rows.append(_separator())
    rows.extend(_vector_rows('tex', "offset", context))
    rows.extend(_vector_rows('tex', "scale", context))

    return _compact(rows)


def _mask_texture_panel_rows(context, brush):
    """The Texture Mask popover: `VIEW3D_PT_tools_mask_texture`.

    The second texture. It does not paint; it decides how much of the stroke
    lands, which is what makes a brush read as a material rather than a stamp.

    The slot is the same type as the Texture panel's, so the angle and
    transform rows are the very same code. Two things differ. The mapping is
    `mask_map_mode`, a shorter list of the same enum -- a mask has no 3D or
    Area Plane. And Pressure Masking belongs to the brush rather than the
    slot, because it is about the pen, not about the texture.
    """
    slot = brush.mask_texture_slot

    rows = [_idblock_row('brush', "mask_texture", context)]
    rows.extend(_texture_data_rows('tex_mask_data', brush.mask_texture, context))
    rows.extend([
        _separator(),
        _row('tex_mask', "mask_map_mode", label="Mask Mapping", context=context),
        _row('brush', "use_pressure_masking", label="Pressure Masking",
             context=context),
        _separator(),
    ])

    # `mask_map_mode` and `map_mode` are one field with two lists of items, so
    # the stencil question is asked of the one that has a Stencil to name.
    if slot.map_mode == 'STENCIL':
        rows.extend(_stencil_rows('tex_mask', brush.mask_texture))
        rows.append(_separator())

    rows.extend(_texture_angle_rows('tex_mask', brush, slot, context))
    rows.append(_separator())
    rows.extend(_vector_rows('tex_mask', "offset", context))
    rows.extend(_vector_rows('tex_mask', "scale", context))

    return _compact(rows)


def _stroke_panel_rows(context, brush):
    """The Stroke popover: `StrokePanel.draw`, texture paint branch."""
    method = brush.stroke_method
    capabilities = brush.image_paint_capabilities

    rows = [
        _row('brush', "stroke_method", context=context),
        _separator(),
    ]

    if method == 'ANCHORED':
        rows.append(_row('brush', "use_edge_to_edge", text="Edge to Edge",
                         context=context))
    elif method == 'AIRBRUSH':
        rows.append(_row('brush', "rate", label="Rate", slider=True, context=context))
    elif method == 'SPACE':
        rows.append(_row(
            'brush', "spacing", label="Spacing", context=context,
            suffix=_compact([
                _toggle('brush', "use_pressure_spacing", 'PRESSURE', context)
            ]),
        ))
    elif method in {'LINE', 'CURVE'}:
        rows.append(_row('brush', "spacing", label="Spacing", context=context))

    if getattr(capabilities, "has_space_attenuation", False):
        rows.append(_row('brush', "use_space_attenuation", context=context))

    if method == 'CURVE':
        rows.append(_separator())
        rows.append({"kind": 'button', "label": "New Paint Curve",
                     "action": 'paintcurve_new'})
        rows.append({"kind": 'button', "label": "Draw Curve",
                     "action": 'paintcurve_draw',
                     "active": brush.paint_curve is not None})

    if method in {'SPACE', 'LINE', 'CURVE'}:
        rows.append(_separator())
        rows.append(_row('brush', "dash_ratio", label="Dash Ratio", context=context))
        rows.append(_row('brush', "dash_samples", label="Dash Length", context=context))

    rows.append(_separator())
    jitter_suffix = _compact([
        _toggle('brush', "use_pressure_jitter", 'PRESSURE', context)
    ])
    if brush.jitter_unit == 'BRUSH':
        rows.append(_row('brush', "jitter", slider=True, suffix=jitter_suffix,
                         context=context))
    else:
        rows.append(_row('brush', "jitter_absolute", suffix=jitter_suffix,
                         context=context))
    rows.append(_row('brush', "jitter_unit", kind='enum_row', context=context))

    rows.append(_separator())
    rows.append(_row(
        'auto', "input_samples", slider=True, context=context,
        suffix=_compact([
            _toggle('unified', "use_unified_input_samples", 'BRUSH', context)
        ]),
    ))

    if getattr(brush.brush_capabilities, "has_smooth_stroke", False):
        smooth = bool(brush.use_smooth_stroke)
        rows.append({
            "kind": 'panel',
            "key": "smooth_stroke",
            "label": "Stabilize Stroke",
            "open": False,
            "toggle": _toggle('brush', "use_smooth_stroke", 'BRUSH', context),
            "rows": _compact([
                _row('brush', "smooth_stroke_radius", label="Radius", slider=True,
                     active=smooth, context=context),
                _row('brush', "smooth_stroke_factor", label="Factor", slider=True,
                     active=smooth, context=context),
            ]),
        })

    return _compact(rows)


def _falloff_panel_rows(context, brush):
    """The Falloff popover: `FalloffPanel.draw` plus its Normal Falloff child.

    Blender's own header draws the preset as a row of icon buttons; ten of
    them do not fit a 250px panel legibly, so this is the same enum drawn as
    the list it is -- the sidebar version of the very same property.
    """
    rows = [_row('brush', "curve_distance_falloff_preset", label="Falloff",
                 context=context)]

    if brush.curve_distance_falloff_preset == 'CUSTOM':
        rows.append(_curve_row(brush))

    # The two pressure curves, each behind the switch that turns its input on
    # -- a curve for a pressure that is not being read is a control with
    # nothing on the other end of it. Same editor as the falloff curve; the
    # only difference is which mapping the row names.
    for key, label, switch, desc in (
        ("curve_size", "Size Pressure", "use_pressure_size",
         "How pen pressure maps to brush size - drag a point, Ctrl-click to "
         "add or remove one"),
        ("curve_strength", "Strength Pressure", "use_pressure_strength",
         "How pen pressure maps to brush strength - drag a point, Ctrl-click "
         "to add or remove one"),
    ):
        on = bool(getattr(brush, switch, False))
        rows.append({
            "kind": 'panel',
            "key": key,
            "label": label,
            "open": False,
            "toggle": _toggle('brush', switch, 'PRESSURE', context),
            "rows": _compact([_curve_row(brush, key, desc)]) if on else [],
        })

    normal_on = bool(_panel_value('settings', "use_normal_falloff", context, False))
    rows.append({
        "kind": 'panel',
        "key": "normal_falloff",
        "label": "Normal Falloff",
        "open": False,
        "toggle": _toggle('settings', "use_normal_falloff", 'BRUSH', context),
        "rows": _compact([
            _row('settings', "normal_angle", label="Angle", active=normal_on,
                 context=context),
        ]),
    })

    return _compact(rows)


def _curve_row(brush, key="curve_distance_falloff", desc=None):
    """One of the brush's curves, sampled for drawing plus its points.

    `key` is the `CurveMapping` property on the brush. A brush has more than
    one -- the distance falloff, and one per pressure input -- and they are
    all edited by the same widget, so the row names which.
    """
    mapping = getattr(brush, key, None)
    curve = _brush_curve(brush, key)
    if mapping is None or curve is None:
        return None

    # MEASURED: `CurveMap` has no `evaluate`. Sampling goes through the
    # *mapping* -- `CurveMapping.evaluate(curve_map, position)` -- and the
    # mapping has to have been initialized before it will answer.
    try:
        mapping.initialize()
    except (AttributeError, RuntimeError):
        pass

    samples = []
    for index in range(_CURVE_SAMPLES):
        x = index / (_CURVE_SAMPLES - 1)
        try:
            samples.append(min(1.0, max(0.0, mapping.evaluate(curve, x))))
        except (AttributeError, RuntimeError, TypeError):
            samples.append(0.0)

    points = [tuple(point.location) for point in curve.points]
    selected = next(
        (index for index, point in enumerate(curve.points) if point.select), -1
    )

    return {
        "kind": 'curve',
        "key": key,
        "label": "",
        "desc": desc or
        "Falloff curve - drag a point, Ctrl-click to add or remove one",
        "samples": samples,
        "points": points,
        "selected": selected,
        "clip": bool(getattr(mapping, "use_clip", True)),
    }


def _brush_curve(brush=None, key="curve_distance_falloff"):
    """The one CurveMap inside a brush's mapping, or None."""
    from . import paint

    brush = brush or paint.active_brush()
    mapping = getattr(brush, key, None)
    if mapping is None or not len(mapping.curves):
        return None
    return mapping.curves[0]


def _falloff_curve(brush=None):
    """The falloff curve. Kept as a name of its own: it is the one every
    other part of the module means when it says "the curve"."""
    return _brush_curve(brush)


def _cursor_panel_rows(context, brush):
    """The Cursor popover: `DisplayPanel.draw` with `is_popover` true."""
    show = bool(_panel_value('settings', "show_brush", context, True))
    overlay_active = show and bool(
        getattr(brush.brush_capabilities, "has_overlay", False)
    )

    rows = [
        _row('settings', "show_brush", text="Display Cursor", context=context),
        _separator(),
        _row('brush', "cursor_color_add", label="Cursor Color", active=show,
             context=context),
        _separator(),
        _row(
            'brush', "cursor_overlay_alpha", label="Falloff Opacity", active=show,
            context=context,
            suffix=_compact([
                _toggle('brush', "use_cursor_overlay_override", 'BRUSH', context),
                _toggle('brush', "use_cursor_overlay", 'EYE', context,
                        alt_icon='EYE_OFF'),
            ]),
        ),
    ]

    texture_suffix = [_toggle('brush', "use_primary_overlay_override", 'BRUSH', context)]
    if brush.texture_slot.map_mode != 'STENCIL':
        texture_suffix.append(
            _toggle('brush', "use_primary_overlay", 'EYE', context, alt_icon='EYE_OFF')
        )
    rows.append(_row(
        'brush', "texture_overlay_alpha", label="Texture Opacity",
        active=overlay_active, context=context, suffix=_compact(texture_suffix),
    ))

    mask_suffix = [_toggle('brush', "use_secondary_overlay_override", 'BRUSH', context)]
    if brush.mask_texture_slot.map_mode != 'STENCIL':
        mask_suffix.append(
            _toggle('brush', "use_secondary_overlay", 'EYE', context, alt_icon='EYE_OFF')
        )
    rows.append(_row(
        'brush', "mask_overlay_alpha", label="Mask Texture Opacity",
        active=overlay_active, context=context, suffix=_compact(mask_suffix),
    ))

    return _compact(rows)


def _options_panel_rows(context):
    """The Options popover: Blender's `VIEW3D_PT_tools_imagepaint_options`.

    This is the panel that decides which texels a stroke is allowed to reach,
    and every one of these is a projection-painting setting rather than a
    brush one -- so they all live on `image_paint`, not on the brush.

    **Bleed** is the one to reach for when a stroke comes out speckled. It
    extends each stroke past the edge of the face's UVs; with it too low, a
    texel that straddles a face boundary can be missed by both of the faces
    that share it, and the layer underneath shows through as a single bright
    pixel. That gets worse the smaller the model is on screen, because each
    face then covers fewer screen pixels for the projection to sample -- which
    is why it shows up when painting from far away and not when painting close.
    """
    return _compact([
        # Blender's occlusion test drops texels that sit on the edge two
        # triangles share, and the specks it leaves are what a stroke looks
        # like on a dense mesh from far away. Asked for, never
        # automatic: a pixel write between Blender's undo steps is exactly
        # what breaks, and it cost the user their undo stack once already.
        {"kind": 'button', "label": "Clean Specks", "action": 'despeckle'},
        _separator(),
        _row('settings', "seam_bleed", label="Bleed", context=context),
        _row('settings', "dither", context=context),
        _separator(),
        _row('settings', "use_occlude", text="Occlude", context=context),
        _row('settings', "use_backface_culling", text="Cull", context=context),
        _row('settings', "use_cavity", text="Cavity Mask", context=context),
        _separator(),
        {"kind": 'label', "label": "Tiling"},
        _row('settings', "tile_x", text="X", context=context),
        _row('settings', "tile_y", text="Y", context=context),
        _row('settings', "tile_z", text="Z", context=context),
    ])


def _brush_panels(context):
    """Every brush popover, or None when there is no brush to describe.

    Returned as a list so the strip lays them out in Blender's own order.
    """
    from . import paint

    brush = paint.active_brush(context)
    if brush is None:
        return None

    builders = (
        ('BRUSH', "Brush", _brush_panel_rows),
        # Blender's tool header puts the two texture popovers straight after
        # the brush, and so does this: what a stroke stamps and what masks it
        # belong beside what it is.
        ('TEXTURE', "Texture", _texture_panel_rows),
        ('MASK_TEXTURE', "Texture Mask", _mask_texture_panel_rows),
        ('STROKE', "Stroke", _stroke_panel_rows),
        ('FALLOFF', "Falloff", _falloff_panel_rows),
        ('CURSOR', "Cursor", _cursor_panel_rows),
        ('OPTIONS', "Options", lambda context, _brush: _options_panel_rows(context)),
    )

    panels = []
    for identifier, label, build in builders:
        try:
            rows = build(context, brush)
        except (AttributeError, KeyError, TypeError):
            # A property that moved between versions must cost one panel, not
            # the whole interface.
            traceback.print_exc()
            rows = []
        panels.append({
            "id": identifier,
            "label": label,
            "width": _PANEL_WIDTHS[identifier],
            "rows": rows,
        })
    return panels


def _brush_property_rows(panels):
    """The popovers again, stacked as the sections of one panel.

    Two ways of working, one set of rows. The strip's popovers are shortcuts
    -- open, change one thing, close -- and this is the panel you leave open
    beside the layers while painting. They are fed from the same specs, so a
    value changed in one is the same value in the other and there is no second
    description to drift.

    Brush is open and the rest are shut. Seven sections unrolled at once is a
    wall of rows nobody asked for, and Brush is the one holding what a stroke
    is made of.
    """
    return [
        {
            "kind": 'panel',
            "key": panel["id"],
            "label": panel["label"],
            "open": panel["id"] == 'BRUSH',
            "rows": panel["rows"],
        }
        for panel in (panels or ())
    ]


def _set_brush_panel(is_open):
    """Open or close the brush properties panel."""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return

    scene.paintr2.ui.show_brush_panel = bool(is_open)
    session.tag_redraw()


def _set_panel_prop(owner, key, value):
    """Write one property row back to the property it came from.

    Shared by every panel built out of rows -- the brush popovers, the fill
    options and the layer properties -- because `owner` already says which
    datablock the row belongs to.
    """
    key, index = _split_index(key)
    holder = _panel_owner(owner, key)
    if holder is None or not key:
        return

    holder = _writable(owner, holder, key)
    if holder is None:
        return

    try:
        if index is None:
            setattr(holder, key, _fit(holder, key, value))
        else:
            getattr(holder, key)[index] = _fit(holder, key, value)
    except (AttributeError, TypeError, ValueError, IndexError) as error:
        print(f"[PaintR2] could not set {key}: {error}")
    session.tag_redraw()


def _writable(owner, holder, key):
    """The brush a datablock row can actually write to.

    Blender's essentials brushes are linked, and a linked datablock may not
    point at a local one, so putting a texture on one does not fail -- it does
    nothing at all. The brush becomes this file's own first, and the row
    writes to that.

    Only pointer rows go through this. Every other brush property writes to a
    linked brush without complaint, which is why the other popovers never
    needed it.
    """
    if owner != 'brush' or getattr(holder, "library", None) is None:
        return holder

    prop = holder.bl_rna.properties.get(key)
    if prop is None or prop.type != 'POINTER':
        return holder

    from . import brushes

    return brushes.localize()


def _split_index(key):
    """`("offset", 1)` out of `"offset[1]"`, or `(key, None)`.

    A vector row writes one component, and which one has to survive the trip
    through a channel that carries a property name. See `_row`.
    """
    if not key.endswith("]") or "[" not in key:
        return key, None
    name, _, rest = key.partition("[")
    try:
        return name, int(rest[:-1])
    except ValueError:
        return key, None


def _fit(holder, key, value):
    """Turn what the panel sent into what the property will take.

    Three conversions, each read off the property rather than declared by the
    row that sent the value:

    - **a colour** is cut to the property's own length. The picker always
      hands back RGBA and a three-component colour -- a world backdrop, an
      object colour -- refuses four values.
    - **a name** becomes the datablock it names, for a pointer row. The panel
      never holds a datablock; it holds what the dropdown said.
    - **a typed string** becomes a number, for a property carrying a unit.
      "5 cm" is a length Blender can read and Python cannot, so the text comes
      through unparsed and is handed to `bpy.utils.units` here.
    """
    prop = holder.bl_rna.properties.get(key)
    if prop is None:
        return value

    if isinstance(value, tuple):
        length = int(getattr(prop, "array_length", 0) or 0)
        if length and len(value) != length:
            return value[:length] + (1.0,) * max(0, length - len(value))
        return value

    if prop.type == 'POINTER':
        return _datablock(prop, value)

    if isinstance(value, str) and prop.type == 'FLOAT':
        return _from_unit(prop, value)

    return value


def _datablock(prop, name):
    """The datablock a pointer row's dropdown named, or None to clear the slot.

    Which collection to look in comes from the property's own type, so the day
    a second kind of pointer row appears it costs one line in
    `_ID_COLLECTIONS` and nothing else.
    """
    if not name:
        return None
    collection = _ID_COLLECTIONS.get(
        getattr(getattr(prop, "fixed_type", None), "identifier", "")
    )
    if collection is None:
        return None
    return getattr(bpy.data, collection).get(name)


def _from_unit(prop, text):
    """Read a length back the way it was shown: "5 cm" is 0.05 metres.

    Blender is the only thing that can do this -- what "5" means on its own
    depends on the scene -- and it is the same conversion `_unit_display` made
    on the way out, run backwards, scene unit scale included.
    """
    settings = getattr(getattr(bpy.context, "scene", None), "unit_settings", None)
    system = getattr(settings, "system", 'NONE')
    unit = getattr(prop, "unit", 'NONE')
    if system == 'NONE' or unit == 'NONE':
        return float(text)

    scale = float(settings.scale_length) or 1.0
    return bpy.utils.units.to_value(system, unit, text) / scale


def _brush_panel_action(action, *args):
    """Everything in a popover that is not a plain property write."""
    # Curve actions carry the name of the curve they are about, because a
    # brush has several and they are all drawn by the same widget.
    key = args[0] if action.startswith("curve_") and args else ""
    rest = args[1:] if action.startswith("curve_") else args
    curve = _brush_curve(key=key) if key else None

    if action == 'curve_select' and curve is not None:
        index = rest[0]
        for position, point in enumerate(curve.points):
            point.select = position == index

    elif action == 'curve_move' and curve is not None:
        index, x, y = rest
        if 0 <= index < len(curve.points):
            curve.points[index].location = (x, y)
            _update_brush_curve(key)

    elif action == 'curve_add' and curve is not None:
        x, y = rest
        curve.points.new(x, y)
        _update_brush_curve(key)

    elif action == 'curve_remove' and curve is not None:
        index = rest[0]
        # A curve needs its two ends; Blender refuses to delete them too.
        if 1 < len(curve.points) and 0 <= index < len(curve.points):
            try:
                curve.points.remove(curve.points[index])
            except RuntimeError:
                pass
            _update_brush_curve(key)

    elif action == 'paintcurve_new':
        _run("new paint curve", bpy.ops.paintcurve.new)

    elif action == 'paintcurve_draw':
        _run("draw paint curve", bpy.ops.paintcurve.draw)

    elif action == 'idblock_new':
        _new_datablock(*rest)

    elif action == 'idblock_open':
        # 'INVOKE_DEFAULT' so the file browser opens; the operator carries the
        # slot to write, because the browser comes back long after this call.
        _run("open image", bpy.ops.paintr2.image_open, 'INVOKE_DEFAULT',
             owner=rest[0], key=rest[1])

    elif action == 'idblock_unlink':
        _set_panel_prop(rest[0], rest[1], "")

    elif action == 'stencil_fit':
        _run("fit stencil", bpy.ops.brush.stencil_fit_image_aspect, mask=rest[0])

    elif action == 'stencil_reset':
        _run("reset stencil", bpy.ops.brush.stencil_reset_transform, mask=rest[0])

    elif action == 'despeckle':
        _run("clean specks", bpy.ops.paintr2.despeckle)

    session.tag_redraw()


def _new_datablock(owner, key):
    """The New button of a pointer row: make one, and put it in the slot.

    Blender's own button runs `texture.new`, which makes a texture and hands
    it to whatever `context.texture_slot` the click came from. There is no
    such slot inside a PaintR2 window -- the panel is ours, not a Blender
    region -- so the datablock is made here and written to the property the
    row already names. That is the property Blender's template would have
    written anyway, update callback and all.
    """
    holder = _panel_owner(owner, key)
    prop = None if holder is None else holder.bl_rna.properties.get(key)
    if prop is None or prop.type != 'POINTER':
        return

    # Textures are the only pointer row there is, and the only kind of
    # datablock this knows how to make. A second one would say so here rather
    # than being made wrongly.
    fixed = getattr(getattr(prop, "fixed_type", None), "identifier", "")
    if _ID_COLLECTIONS.get(fixed) != "textures":
        return

    # An image texture, which is what Blender's `texture.new` makes: a brush
    # texture with nothing in it is an image slot waiting for an image.
    texture = bpy.data.textures.new(name="Texture", type='IMAGE')
    _set_panel_prop(owner, key, texture.name)

    # The write can still be refused -- a brush that could not be localized
    # takes nothing. Leaving the texture behind would put a datablock
    # nobody asked for in the user's file, once per click.
    holder = _panel_owner(owner, key)
    if holder is None or getattr(holder, key, None) is not texture:
        bpy.data.textures.remove(texture)


def _update_brush_curve(key="curve_distance_falloff"):
    """Recompute a mapping. Without this the brush keeps the old shape."""
    from . import paint

    brush = paint.active_brush()
    mapping = getattr(brush, key, None)
    if mapping is not None:
        mapping.update()


def _set_symmetry(axis, enabled):
    """Turn mirroring on or off for one axis.

    Writes the mesh's own `use_mesh_mirror_*` -- an alias of
    `mesh.use_mirror_*`, and the property Blender's own Symmetry panel writes.
    `ImagePaint.use_symmetry_*` exists and is writable and does nothing in
    texture paint; wiring to it once already cost a release.
    """
    obj = _mirror_object()
    prop = _MIRROR_PROPERTIES.get(axis)
    if obj is None or prop is None:
        return

    try:
        setattr(obj, prop, bool(enabled))
    except (AttributeError, TypeError):
        pass


def _set_symmetry_planes(enabled):
    """Show or hide the mirror planes. A display setting, saved in the file."""
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return

    scene.paintr2.ui.show_symmetry_planes = bool(enabled)
    session.tag_redraw()


def _run(description, operator, *args, **kwargs):
    """Call an operator from the interface, reporting failures to the console.

    `args` is there for the one thing an operator call takes positionally --
    the execution context, `'INVOKE_DEFAULT'`, which a file browser needs.
    """
    try:
        operator(*args, **kwargs)
    except RuntimeError as error:
        print(f"[PaintR2] {description} failed: {error}")
    session.tag_redraw()


def _add_layer(layer_type):
    from . import paint

    _leave_mask_view()
    _run("add layer", bpy.ops.paintr2.layer_add, layer_type=layer_type)
    paint.auto_sync_mode()


def _remove_layer(contents='KEEP'):
    """Delete the active layer. For a group, `contents` says what its children
    do: come up a level, or go with it."""
    from . import paint

    _run("remove layer", bpy.ops.paintr2.layer_remove, contents=contents)
    paint.auto_sync_mode()


def _solo_layer(mode, index=-1):
    """Show one layer, or one mask, instead of the whole stack."""
    _run("solo", bpy.ops.paintr2.layer_solo, mode=mode, index=index)


def _set_panel_width(side, width):
    """Persist a panel width the user dragged to."""
    scene = getattr(bpy.context, "scene", None)
    ui_state = scene.paintr2.ui if scene is not None else None
    if ui_state is None:
        return

    key = {'LAYERS': "right_panel_width",
           'BRUSH': "brush_panel_width"}.get(side, "left_panel_width")
    if getattr(ui_state, key) != int(width):
        setattr(ui_state, key, int(width))


def _fill_undo(redo=False):
    """Undo, or redo, the last fill. True when there was one.

    The answer matters: the interface only takes Ctrl+Z when we have a fill
    to undo, and passes it to Blender otherwise.
    """
    from . import history

    if not (history.depth()[1 if redo else 0]):
        return False

    try:
        result = bpy.ops.paintr2.fill_undo(redo=redo)
    except RuntimeError:
        return False
    return result == {'FINISHED'}


def _bake_mask():
    """Freeze the active layer's pattern into its mask image."""
    _run("bake mask", bpy.ops.paintr2.mask_procedural_bake)


def _effect_select(layer_index, effect_index, on_mask):
    from . import paint

    _run("select effect", bpy.ops.paintr2.effect_select,
         layer_index=layer_index, index=effect_index, on_mask=on_mask)
    # A paint effect is a paint target, so selecting one has to move the
    # brush the same way selecting a paint layer does.
    paint.auto_sync_mode()


def _effect_toggle(layer_index, effect_index, on_mask):
    _run("toggle effect", bpy.ops.paintr2.effect_toggle,
         layer_index=layer_index, index=effect_index, on_mask=on_mask)


def _effect_property(layer_index, effect_index, on_mask, key, value):
    """Write one field of one effect. No operator: this is a drag's inner
    loop, and an operator per pixel of an opacity drag is an undo stack
    nobody wants."""
    from . import paint

    document = paint.active_document(bpy.context)
    if document is None or not (0 <= layer_index < len(document.layers)):
        return

    stack = document.layers[layer_index].effect_stack(on_mask)
    if 0 <= effect_index < len(stack):
        setattr(stack[effect_index], key, value)


def _effect_blend(layer_index, effect_index, on_mask, value):
    _effect_property(layer_index, effect_index, on_mask, "blend_mode", value)


def _effect_opacity(layer_index, effect_index, on_mask, value):
    _effect_property(layer_index, effect_index, on_mask, "opacity", value)


def _effect_add(effect_type, kind=""):
    """Add a filter, a pattern or a paint image. Where it lands is the
    selected half."""
    from . import paint

    _run("add effect", bpy.ops.paintr2.effect_add,
         effect_type=effect_type, kind=kind)
    paint.auto_sync_mode()


def _effect_remove(layer_index=-1, effect_index=-1, on_mask=None):
    """Drop one effect. Which stack is the selected half, not an argument."""
    _run("remove effect", bpy.ops.paintr2.effect_remove, index=effect_index)


def _effect_move(layer_index, effect_index, on_mask, direction):
    _run("move effect", bpy.ops.paintr2.effect_move,
         index=effect_index, direction=direction)


def _tag_layers(color):
    """Put a colour tag on the selection."""
    _run("tag layers", bpy.ops.paintr2.layer_tag, color=color)


def _group_selected():
    """Fold the selected layers into a new group."""
    _run("group layers", bpy.ops.paintr2.layer_group_selected)


def _toggle_group(index):
    _run("toggle group", bpy.ops.paintr2.group_toggle, index=index)


def _move_layer(direction):
    _run("move layer", bpy.ops.paintr2.layer_move, direction=direction)


def _reorder_layer(source, after, depth, inside=False):
    """Drag-and-drop result, resolved from "under this layer" into a position.

    The interface can only say which layer the block landed under, because a
    closed group hides how many layers there are between one row and the next.
    Turning that into a gap is this side's job: the gap is past the whole
    subtree of the layer above, not just past the layer itself.
    """
    from . import paint

    document = paint.active_document()
    if document is None:
        return

    # `inside` is a drop onto a group: the block goes to the top of it, which
    # is the gap right after the group's own row. Otherwise the drop was
    # between two rows and lands past everything the row above owns.
    if after < 0:
        gap = 0
    elif inside:
        gap = after + 1
    else:
        gap = document.subtree_end(after)

    # A drag that started on a selected layer takes the whole selection with
    # it; one that started anywhere else takes only what was grabbed.
    selected = document.selected_indices()
    sources = ",".join(str(index) for index in selected) if source in selected else ""

    _run(
        "reorder layer",
        bpy.ops.paintr2.layer_reorder,
        from_index=source,
        sources=sources,
        to_index=gap,
        to_depth=max(0, depth),
    )
    paint.auto_sync_mode()


def _add_mask(fill='WHITE'):
    from . import paint

    _run("add mask", bpy.ops.paintr2.mask_add, fill=fill)
    paint.auto_sync_mode()


def _remove_mask():
    from . import paint

    _run("remove mask", bpy.ops.paintr2.mask_remove)
    paint.auto_sync_mode()


def _set_procedural_mask(add):
    from . import paint

    if add:
        _run("add procedural mask", bpy.ops.paintr2.mask_procedural_add)
    else:
        _run("remove procedural mask", bpy.ops.paintr2.mask_procedural_remove)
    paint.auto_sync_mode()


def _invert_mask():
    _run("invert mask", bpy.ops.paintr2.mask_invert)


def _export_mask(index=-1):
    """Ask for a folder and a name, then write that layer's mask there."""
    # INVOKE, not EXEC: the whole point is the file browser, and that only
    # comes up on invoke -- the same reason the HDRI picker is invoked.
    try:
        bpy.ops.paintr2.mask_export('INVOKE_DEFAULT', index=int(index))
    except RuntimeError as error:
        print(f"[PaintR2] export mask failed: {error}")
    session.tag_redraw()


def _toggle_mask(index=-1):
    """Switch a mask off or on. `index` is -1 for the active layer."""
    _run("toggle mask", bpy.ops.paintr2.mask_toggle, index=int(index))


def _layer_at(index):
    """The layer a row refers to, or None if the row is stale."""
    from . import paint

    document = paint.active_document()
    if document is None or not (0 <= index < len(document.layers)):
        return None
    return document.layers[index]


def _set_blend(index, identifier):
    layer = _layer_at(index)
    if layer is not None and layer.blend_mode != identifier:
        layer.blend_mode = identifier  # update hook recompiles
        session.tag_redraw()


def _set_opacity(index, value):
    layer = _layer_at(index)
    if layer is not None:
        layer.opacity = value
        session.tag_redraw()


def _rename_layer(index, name):
    _run("rename layer", bpy.ops.paintr2.layer_rename, index=index, name=name)


def _set_fill_color(index, color):
    layer = _layer_at(index)
    if layer is not None and layer.layer_type == 'FILL':
        layer.channel('BASE_COLOR').color = color  # update hook recompiles
        session.tag_redraw()


# --- the shader graph editor -------------------------------------------------
#
# The one place Blender's own interface is allowed near this window, and the
# distinction that makes it allowed is exact: no Blender region inside
# *our* region. This is not inside it. It is a second editor beside ours, with
# its own header and its own everything, and the boundary between the two is a
# Blender area split -- the same thing that separates any two editors.
#
# It has to be the real editor. A shader graph rebuilt in our toolkit would be
# a node editor written from scratch, and the Custom pattern exists precisely
# so that hand-built nodes can be hand-built with Blender's own tools.
#
# PaintR2 keeps the right half. That is not arbitrary: the layer panel is on
# the right edge of our region and the canvas is what gives way, so putting the
# graph on the left leaves the stack where the hand already expects it.
#
# Which half is which was **measured**, in a real window, because it cannot be
# read back at the time it matters:
#
#   * `screen.area_split(direction='VERTICAL')` gives the **new** area the
#     left half and pushes the original to the right. Original before:
#     x=2 w=2100. After: original x=1054 w=1048, new x=2 w=1049.
#   * Immediately after the call the new area still measures
#     **x=0 y=0 w=0 h=0** -- geometry is laid out on the next screen refresh,
#     not by the operator. So deciding "which one is on the left" by comparing
#     `area.x` reads one real number against one uninitialised zero, and in a
#     PaintR2 window -- one area, at x=0 -- the two tie and the coin lands
#     wrong. That is precisely the bug this replaced.
#
# Hence: no geometry is read. The new area is the left half, so the new area
# is the shader graph, and the session never has to move.


SHADER_GRAPH_FACTOR = 0.5


def _shader_area(window=None):
    """The live Area of the shader graph editor, or None if it is gone.

    Resolved by pointer every time, like every other area this add-on holds
    on to -- and closing it with Blender's own controls is a perfectly normal
    thing to do, so "gone" is an ordinary answer rather than an error.
    """
    state = session.state
    if not getattr(state, "shader_area_ptr", 0):
        return None

    window = window or session.find_window()
    if window is None or window.screen is None:
        return None

    for area in window.screen.areas:
        if area.as_pointer() == state.shader_area_ptr:
            return area

    state.shader_area_ptr = 0
    return None


def shader_graph_is_open():
    return _shader_area() is not None


def _open_shader_graph(context):
    """Split the window and turn the new -- left -- half into a shader editor."""
    window = session.find_window()
    area = session.find_area(window)
    if window is None or area is None or window.screen is None:
        return False

    before = {existing.as_pointer() for existing in window.screen.areas}
    region = session.find_region(area)

    try:
        with context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.area_split(
                direction='VERTICAL', factor=SHADER_GRAPH_FACTOR
            )
    except RuntimeError:
        traceback.print_exc()
        return False

    fresh = [
        existing
        for existing in window.screen.areas
        if existing.as_pointer() not in before
    ]
    if not fresh:
        return False

    # The new one, and the new one is the left half -- see the note above.
    # PaintR2 stays in the area it was already in, which is why nothing here
    # touches the session pointers or has to dress a host a second time.
    left = fresh[-1]
    left.type = 'NODE_EDITOR'

    space = left.spaces.active
    try:
        space.tree_type = 'ShaderNodeTree'
        space.shader_type = 'OBJECT'
    except (AttributeError, TypeError):
        pass

    session.state.shader_area_ptr = left.as_pointer()
    left.tag_redraw()
    area.tag_redraw()
    return True


def _close_shader_graph(context):
    """Close the editor and give the space back to the canvas."""
    window = session.find_window()
    area = _shader_area(window)
    if window is None or area is None:
        session.state.shader_area_ptr = 0
        return False

    try:
        with context.temp_override(window=window, area=area):
            bpy.ops.screen.area_close()
    except RuntimeError:
        traceback.print_exc()
        return False

    session.state.shader_area_ptr = 0
    session.tag_redraw()
    return True


def _set_shader_graph(should_open):
    """Toggle the editor. Deferred, because of where the click comes from.

    `screen.area_split` and `screen.area_close` reshape the window the modal
    operator is running in, from inside that operator's own event handling.
    Running them on a zero-delay timer moves them out to the point where
    Blender has finished with the event -- the same reason `request_close`
    does it, and the same fix.
    """
    def apply():
        context = bpy.context
        if not session.state.is_open:
            return None
        if bool(should_open) == shader_graph_is_open():
            return None
        if should_open:
            _open_shader_graph(context)
        else:
            _close_shader_graph(context)
        session.tag_redraw()
        return None

    bpy.app.timers.register(apply, first_interval=0.0)


def _show_node_tree(tree):
    """Point the shader graph at `tree`, opening the editor if it is shut.

    Pinning is what makes an arbitrary group showable: without it the editor
    derives its tree from the active object on every redraw, and a path set by
    hand is replaced within the frame. The pin is visible in the editor's
    header, so it is undoable by the person looking at it.
    """
    def apply():
        if not session.state.is_open:
            return None

        context = bpy.context
        if not shader_graph_is_open():
            _open_shader_graph(context)

        area = _shader_area()
        if area is None or tree is None:
            return None

        space = area.spaces.active
        try:
            space.pin = True
            space.node_tree = tree
        except (AttributeError, TypeError) as error:
            print(f"[PaintR2] could not show {tree.name}: {error}")
        area.tag_redraw()
        return None

    bpy.app.timers.register(apply, first_interval=0.0)


def _layer_panel_action(action, *_args):
    """Everything in the layer properties section that is not a value write."""
    from . import paint

    document = paint.active_document()

    if action == 'effect_remove':
        if document is not None:
            _effect_remove(document.active_index, document.active_effect,
                           document.active_on_mask)
        return

    if action == 'effect_graph':
        effect = document.active_effect_item() if document else None
        if effect is None:
            return
        # The group is normally built when the pattern is added, but the kind
        # can also be turned to Custom afterwards from the dropdown right
        # above this button -- and then there is nothing to show yet. Making
        # it here means the button always opens on something.
        tree = effect.pattern.node_tree
        if tree is None:
            from . import procedural

            obj = getattr(bpy.context, "active_object", None)
            material = obj.active_material if obj is not None else None
            if material is not None:
                tree = procedural.ensure_custom_tree(
                    effect.pattern, material, effect.label()
                )
        _show_node_tree(tree)
        return

    if action not in ('edit_proc', 'edit_proc_mask'):
        return

    layer = document.active_layer if document is not None else None
    if layer is None:
        return

    proc = layer.procedural if action == 'edit_proc' else layer.mask_procedural
    _show_node_tree(proc.node_tree)


def _add_adjustment(kind=""):
    """An empty kind means "the default" -- it is picked in the properties."""
    from . import adjustment
    from . import paint

    _leave_mask_view()
    _run("add adjustment", bpy.ops.paintr2.layer_add,
         layer_type='ADJUSTMENT',
         adjustment_kind=kind or adjustment.KINDS[0][0])
    paint.auto_sync_mode()


def _add_procedural(kind=""):
    from . import paint
    from . import procedural

    _leave_mask_view()
    _run("add procedural", bpy.ops.paintr2.procedural_add,
         kind=kind or procedural.KINDS[0][0])
    paint.auto_sync_mode()


def _deselect_layers():
    from . import paint

    _leave_mask_view()
    _run("deselect layers", bpy.ops.paintr2.layer_select_none)
    paint.auto_sync_mode()


def _deferred_close():
    close()
    return None


def _install_draw_handler():
    return _host_space_class().draw_handler_add(
        _draw_callback, (), 'WINDOW', 'POST_PIXEL'
    )


def _remove_draw_handler(handle):
    if handle is None:
        return
    try:
        _host_space_class().draw_handler_remove(handle, 'WINDOW')
    except (ValueError, RuntimeError):
        pass


# --- watchdog ---------------------------------------------------------------


def _watchdog():
    """Detect the window being closed by the OS button and clean up.

    Blender gives no notification for that, and the modal operator may not get
    another event, so this is the only reliable teardown trigger.
    """
    state = session.state
    if not state.is_open:
        return None

    if session.find_window() is None:
        close(window_already_gone=True)
        return None

    return _WATCHDOG_INTERVAL


def _start_watchdog():
    if not bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.register(_watchdog, first_interval=_WATCHDOG_INTERVAL)


def _stop_watchdog():
    if bpy.app.timers.is_registered(_watchdog):
        try:
            bpy.app.timers.unregister(_watchdog)
        except ValueError:
            pass


# --- layout reduction -------------------------------------------------------


def _preferred_area(screen):
    """The area worth keeping: the biggest 3D Viewport, else the biggest area."""
    viewports = [a for a in screen.areas if a.type == HOST_AREA_TYPE]
    pool = viewports or list(screen.areas)
    return max(pool, key=lambda a: a.width * a.height)


def _reduce_to_single_area(context, window):
    """Close every area but one, leaving the window as a single viewport.

    `window_new_main` clones the entire workspace layout -- properties,
    outliner, timeline and all. The clone gets its own screen, so closing
    areas here cannot disturb the layout of the main window.

    The area list is re-read on every pass because closing one reflows the
    rest; the guard is there so a stubborn area cannot spin this forever.
    """
    screen = window.screen
    if screen is None or not screen.areas:
        return None

    keep_ptr = _preferred_area(screen).as_pointer()

    for _ in range(16):
        if len(screen.areas) <= 1:
            break

        target = next(
            (a for a in screen.areas if a.as_pointer() != keep_ptr), None
        )
        if target is None:
            break

        try:
            with context.temp_override(window=window, area=target):
                bpy.ops.screen.area_close()
        except RuntimeError:
            # Some areas refuse to close. The window is still usable; the
            # extra editor just stays there.
            break

    if not screen.areas:
        return None

    return max(screen.areas, key=lambda a: a.width * a.height)


# --- public API -------------------------------------------------------------


def _refusal_reason(context):
    """Why Blender will not open a new main window right now.

    `wm.window_new_main` polls for a screen in its **normal** state. Maximizing
    an editor (Ctrl+Space) or going fullscreen (Ctrl+Alt+Space) swaps the
    screen for a non-normal one and the operator refuses -- with a context
    error that says nothing about which of the two it was. That is how opening
    PaintR2 came to fail with advice to run it from the sidebar, to somebody
    who was pressing the button in the sidebar.

    Named as precisely as the state can be read, and honestly vague when it
    cannot: a wrong reason is worse than no reason.
    """
    screen = getattr(context, "screen", None)
    if screen is None:
        return "there is no screen here to open a window from"

    if getattr(screen, "show_fullscreen", False):
        return ("an editor is maximized. Press Ctrl+Space to restore the "
                "layout, then open PaintR2")

    if getattr(screen, "is_temporary", False):
        return ("this is one of Blender's temporary windows. Open PaintR2 "
                "from the 3D Viewport in the main window")

    # The poll refused and none of the states we can read explain it. Say so
    # rather than inventing one; the console carries Blender's own words.
    return ("Blender refused, and the reason is not one this add-on can "
            "read. The console has its message")


def spawn(context):
    """Open the PaintR2 window. Returns (window, area, reason).

    `reason` is "" on success and a sentence saying what stopped it otherwise
    -- one sentence per way of failing, because "could not open a window" is
    five different problems wearing one coat and the user has to be told which
    of them they have.

    Does not start the modal event loop; the caller owns that, so that the
    operator reporting stays in one place.
    """
    global _draw_error_reported
    global _external_signature
    global _fade_target

    if session.state.is_open:
        return session.find_window(), session.find_area(), ""

    area = getattr(context, "area", None)
    region = getattr(context, "region", None)
    if area is None:
        return None, None, ("there is no editor in the context. Run this from "
                            "the 3D Viewport sidebar")

    known = {win.as_pointer() for win in context.window_manager.windows}

    try:
        with context.temp_override(window=context.window, area=area, region=region):
            # Asked before it is called, so the refusal can be explained
            # instead of caught: calling a Blender operator whose poll fails
            # raises a context error with no room for a reason in it.
            if not bpy.ops.wm.window_new_main.poll():
                return None, None, _refusal_reason(context)
            bpy.ops.wm.window_new_main()
    except RuntimeError as error:
        traceback.print_exc()
        return None, None, f"Blender could not make the window: {error}"

    new_windows = [
        win
        for win in context.window_manager.windows
        if win.as_pointer() not in known
    ]
    if not new_windows:
        return None, None, ("Blender reported no error but no window "
                            "appeared. The console may say more")

    window = new_windows[-1]
    if window.screen is None or not window.screen.areas:
        return None, None, "the new window came up with nothing in it"

    new_area = _reduce_to_single_area(context, window)
    if new_area is None:
        return None, None, "the new window had no editor left to use"

    new_area.type = HOST_AREA_TYPE

    space = new_area.spaces.active
    _hide_asked.clear()
    _dress_host(new_area, space)

    state = session.state
    state.reset()
    state.window_ptr = window.as_pointer()
    state.area_ptr = new_area.as_pointer()
    state.space_ptr = space.as_pointer()

    _draw_error_reported = False
    _external_signature = None
    _fade_target = 0

    # The tool is saved in the .blend, so a window can open straight into a
    # fill tool -- and then nobody has pressed anything for the wireframe to
    # follow. Only reachable now that the session knows its space.
    _follow_tool_wireframe(active_tool(context))

    # And if the wireframe was already up, nothing was written above -- the
    # colour still has to be taken.
    sync_wire_theme()

    state.root = _make_root()
    state.draw_handle = _install_draw_handler()

    # From here, a property drag asks for a recompile instead of doing one:
    # there is now an event loop for the timer to run on, and a hundred-layer
    # stack costs 123 ms a compile. See nodegen.request_compile.
    nodegen.set_deferred(True)

    _start_watchdog()

    # Only the models being painted, from here until the window closes. What
    # was hidden is written onto the scene so `close` -- or a later session,
    # if this one never gets to close -- can put it back.
    isolation.isolate(context)
    # `sync_fade` is what puts the stage into the materials that need it, so
    # there is nothing to recompile ahead of it.
    isolation.sync_fade(context)

    scene = getattr(context, "scene", None)
    if scene is not None:
        scene.paintr2.session_was_open = True
        # The switch is saved in the file, the world it needs is not driven by
        # anything until now: a session that closed lit by PaintR2 opens lit by
        # PaintR2. `refresh` also does nothing at all when the switch is off,
        # which is the case that must stay free.
        lighting.refresh(context, space)

    new_area.tag_redraw()
    return window, new_area, ""


def _make_root():
    """Build the widget tree, wiring its callbacks back to this module."""
    from .gui.root import UIRoot

    from .document import BLEND_MODES

    from .procedural import KINDS as PROCEDURAL_KINDS

    root = UIRoot(on_close=request_close)
    from .nodegen import _SCALAR_BLEND

    root.set_blend_items(
        [(item[0], item[1]) for item in BLEND_MODES],
        # The compiler's own table decides this: a mode with a float
        # arithmetic behind it, plus MIX which always has one.
        scalar_only={'MIX', *_SCALAR_BLEND},
    )
    root.set_procedural_kinds(
        [(item[0], item[1]) for item in PROCEDURAL_KINDS]
    )

    from .adjustment import KINDS as ADJUSTMENT_KINDS

    root.set_adjustment_kinds(
        [(item[0], item[1]) for item in ADJUSTMENT_KINDS]
    )



    root.on_layer_select = _select_layer
    root.on_layer_toggle = _toggle_layer
    root.on_layer_add = _add_layer
    root.on_layer_remove = _remove_layer
    root.on_layer_group_selected = _group_selected
    root.on_layer_solo = _solo_layer
    root.on_layer_tag = _tag_layers
    root.on_effect_select = _effect_select
    root.on_effect_toggle = _effect_toggle
    root.on_effect_blend = _effect_blend
    root.on_effect_opacity = _effect_opacity
    root.on_effect_add = _effect_add
    root.on_effect_remove = _effect_remove
    root.on_effect_move = _effect_move
    root.on_mask_bake = _bake_mask
    root.on_fill_undo = _fill_undo
    root.on_panel_width = _set_panel_width
    root.on_layer_move = _move_layer
    root.on_layer_reorder = _reorder_layer
    root.on_layer_rename = _rename_layer
    root.on_blend_change = _set_blend
    root.on_opacity_change = _set_opacity
    root.on_mask_add = _add_mask
    root.on_mask_remove = _remove_mask
    root.on_mask_invert = _invert_mask
    root.on_mask_toggle = _toggle_mask
    root.on_mask_export = _export_mask
    root.on_mask_procedural = _set_procedural_mask
    root.on_procedural_add = _add_procedural
    root.on_adjustment_add = _add_adjustment
    root.on_layer_deselect = _deselect_layers
    root.on_layer_box_select = _box_select_layers
    root.on_shader_graph = _set_shader_graph
    root.on_brush_panel_toggle = _set_brush_panel
    root.on_display_toggle = _set_display_panel
    root.on_targets_toggle = _set_targets_panel
    root.on_display_prop = _set_panel_prop
    root.on_display_action = _display_action
    root.on_export_run = _bake_and_export
    root.on_group_toggle = _toggle_group
    root.on_layer_prop = _set_panel_prop
    root.on_layer_prop_action = _layer_panel_action
    root.on_fill_color = _set_fill_color
    root.on_brush_select = _set_brush
    root.on_brush_blend = _set_brush_blend
    root.on_brush_color = _set_brush_color
    root.on_brush_secondary_color = _set_brush_secondary_color
    root.on_brush_swap = _swap_brush_colors
    root.on_brush_size = _set_brush_size
    root.on_brush_strength = _set_brush_strength
    root.on_brush_pressure = _set_brush_pressure
    root.on_brush_prop = _set_panel_prop
    root.on_brush_action = _brush_panel_action
    root.on_tool_change = _set_active_tool
    root.on_fill_prop = _set_fill_prop
    root.on_object_select = _select_object
    root.on_material_select = _select_material
    root.on_material_bake_toggle = _toggle_material_bake
    root.on_layer_copy = _copy_layers
    root.on_layer_paste = _paste_layers
    root.on_symmetry_toggle = _set_symmetry
    root.on_symmetry_plane_toggle = _set_symmetry_planes
    root.on_wireframe_toggle = _set_wireframe
    return root


def request_close():
    """Ask for a close from inside a draw or modal pass.

    Neither context may run `wm.window_close` directly -- a draw handler has no
    valid operator context, and a modal operator would be closing the window it
    is still running in. Deferring to a timer sidesteps both.
    """
    bpy.app.timers.register(_deferred_close, first_interval=0.0)


def close(window_already_gone=False):
    """Tear the session down. Safe to call when nothing is open."""
    global _external_signature, _wireframe_is_ours

    from . import history

    _external_signature = None
    _hide_asked.clear()
    # The fill history goes with the session: its steps are rectangles of
    # pixels, and anything else in Blender may have rewritten those while the
    # window was shut. There is no way to notice that from here, so the safe
    # answer is to forget them.
    history.clear()
    # Nothing is left waiting for a timer that is about to have no window to
    # run in -- and the flush is inside `set_deferred`.
    nodegen.set_deferred(False)
    state = session.state
    if state.closing:
        return
    state.closing = True

    try:
        _stop_watchdog()
        _remove_draw_handler(state.draw_handle)

        # Before the space goes out of reach: an overlay we switched on is
        # ours to switch off, and the window closing is the last chance. When
        # the window is already gone there is nothing left to write to, so the
        # flag is only cleared.
        if window_already_gone:
            _wireframe_is_ours = False
        else:
            release_wireframe()

        # The scene's own world comes back with it. The lighting is a setting
        # of the painting session, not an edit to the file -- and the space we
        # would restore the shading flags onto is about to stop existing, so
        # this is the last moment it can be done.
        space = session.find_space(session.find_area())
        if space is not None:
            lighting.restore(bpy.context, space)

        # The theme is not the space: it is still there after the window has
        # gone, so it is given back either way. Leaving it borrowed would mean
        # a Blender whose wire colour silently stayed ours.
        release_wire_theme()

        # And the scene comes back whole: everything the session hid is shown
        # again, and every fade alpha is put back. This runs whether or not
        # the window is still there, because the objects are.
        isolation.restore(bpy.context)

        if not window_already_gone:
            window = session.find_window()
            if window is not None:
                _close_window(window)

        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            scene.paintr2.session_was_open = False
    finally:
        state.reset()

    # After the reset, and that is the whole point: the fade stage asks
    # whether the session is open, so stripping it out of the materials can
    # only happen once the answer is no.
    isolation.recompile_set()


def _close_window(window):
    context = bpy.context
    try:
        with context.temp_override(window=window):
            bpy.ops.wm.window_close()
    except RuntimeError:
        traceback.print_exc()

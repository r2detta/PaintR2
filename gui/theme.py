# SPDX-License-Identifier: GPL-3.0-or-later
"""Colors and metrics for the custom interface.

Everything the GPU interface draws pulls its numbers from here, so restyling
never means touching widget code. Metrics are authored at 1x and multiplied by
`scale()` at layout time.

The colors are not ours: `refresh()` reads them out of the user's active
Blender theme and rebinds the module globals, so PaintR2 follows a custom or
light theme instead of sitting on top of it in its own palette. The literals
below are what the default dark theme derives to, and they stay as the
fallback for when there is no theme to read (background mode, tests).

Two measured facts this rests on:

* Theme colors have RNA subtype ``COLOR_GAMMA`` -- they are display-referred
  values, which is exactly what a ``'POST_PIXEL'`` handler writes. No
  conversion is needed in either direction.
* Blender's own widget radius is ``roundness * widget_unit``, not a fixed
  pixel count, so `radius()` derives from `WIDGET_UNIT` the same way. The
  default theme's 0.4 makes a 22px button distinctly round; the fixed 4px
  constant this replaced was half that, which is what made the interface read
  as "not Blender".
"""

import bpy

from .. import preferences


# --- Colors (display-referred RGBA, mirroring Blender's default dark theme) --

# Region backgrounds. Derived from the 3D view's own background rather than
# authored, so a light theme lightens the whole frame. The factors are the
# deliberate part: the chrome has to stay clearly darker than the live
# viewport it sits on, or the header blends into it and reads as missing.
BG_WINDOW = (0.117, 0.117, 0.117, 1.0)
BG_PANEL = (0.145, 0.145, 0.145, 1.0)
BG_CANVAS = (0.086, 0.086, 0.086, 1.0)
BG_HEADER = (0.124, 0.124, 0.124, 1.0)

BORDER = (0.086, 0.086, 0.086, 1.0)        # editor edges
SEPARATOR = (0.239, 0.239, 0.239, 1.0)     # widget outlines
OUTLINE = (0.239, 0.239, 0.239, 1.0)

WIDGET = (0.329, 0.329, 0.329, 1.0)
WIDGET_HOVER = (0.378, 0.378, 0.378, 1.0)
WIDGET_ACTIVE = (0.278, 0.447, 0.702, 1.0)
WIDGET_TEXT = (0.902, 0.902, 0.902, 1.0)
WIDGET_TEXT_DIM = (0.644, 0.644, 0.644, 1.0)
# What Blender writes on a *selected* widget. Not the same as WIDGET_TEXT:
# under a light theme that one is nearly black, and black on selection blue is
# unreadable.
WIDGET_TEXT_SEL = (1.0, 1.0, 1.0, 1.0)

ACCENT = (0.278, 0.447, 0.702, 1.0)
# Refusal: a delete button, and the frame a drop that will not be accepted
# gets. Not read from the theme -- Blender has no "no" colour, and red is the
# one this has to be.
DANGER = (0.741, 0.286, 0.259, 1.0)

# Layer colour tags, keyed the way Blender keys its collection tags. Read from
# the theme in `refresh()` so a layer tagged red is the same red the outliner
# uses; these are the factory values, and the fallback when a build has no
# collection colours.
TAG_COLORS = {
    'NONE': None,
    'COLOR_01': (0.886, 0.376, 0.357, 1.0),
    'COLOR_02': (0.945, 0.639, 0.333, 1.0),
    'COLOR_03': (0.945, 0.863, 0.333, 1.0),
    'COLOR_04': (0.482, 0.800, 0.482, 1.0),
    'COLOR_05': (0.365, 0.714, 0.918, 1.0),
    'COLOR_06': (0.553, 0.349, 0.855, 1.0),
    'COLOR_07': (0.776, 0.451, 0.722, 1.0),
    'COLOR_08': (0.478, 0.329, 0.255, 1.0),
}

# The order they are offered in, "no tag" first: clearing one is the most
# common thing done to a tag, and the eighth item down is not where that goes.
TAG_ORDER = ('NONE', 'COLOR_01', 'COLOR_02', 'COLOR_03', 'COLOR_04',
             'COLOR_05', 'COLOR_06', 'COLOR_07', 'COLOR_08')

TAG_LABELS = {
    'NONE': "No Tag",
    'COLOR_01': "Red",
    'COLOR_02': "Orange",
    'COLOR_03': "Yellow",
    'COLOR_04': "Green",
    'COLOR_05': "Blue",
    'COLOR_06': "Violet",
    'COLOR_07': "Pink",
    'COLOR_08': "Brown",
}

# Blender's own axis colours, so a mirror plane drawn in the viewport is the
# colour the user already reads as "X" everywhere else in the application.
AXIS = {
    'X': (1.000, 0.200, 0.322, 1.0),
    'Y': (0.545, 0.863, 0.000, 1.0),
    'Z': (0.157, 0.565, 1.000, 1.0),
}

# Fill for small in-row bars. Deliberately not ACCENT: a layer row already
# uses ACCENT for selection, and a second bright blue on the same row reads as
# a second selection.
METER = (0.303, 0.388, 0.515, 1.0)

# The mark under a chip whose half of the layer carries filters. Green because
# it has to survive on top of a selected row, where the background is the
# selection blue -- and because it is the one thing on the row that is not
# about selection at all, so it must not be a shade of it.
EFFECT_MARK = (0.361, 0.784, 0.435, 1.0)

# List rows. Blender highlights the hovered row with a translucent white wash
# rather than a second opaque colour, which is why LIST_HOVER has alpha.
LIST_SEL = (0.278, 0.447, 0.702, 1.0)
LIST_HOVER = (1.0, 1.0, 1.0, 0.2)
BOX_INNER = (0.114, 0.114, 0.114, 0.502)
# Behind an open sub-panel (AO, Curvature...): a white wash that lifts the box
# it sits in by SUBPANEL_LIFT of value, and a nested one a step further. The
# alpha is worked out by `_wash`, not typed -- see there for why 0.04 was not
# "slightly lighter" but a grey the number fields disappeared into.
SUBPANEL_LIFT = 0.03

# Menus and tooltips are their own colour set in Blender; matching that is
# what makes a popup of ours look like one of its own.
MENU_BACK = (0.094, 0.094, 0.094, 1.0)
MENU_OUTLINE = (0.141, 0.141, 0.141, 1.0)
MENU_TEXT = (0.867, 0.867, 0.867, 1.0)
MENU_SEL = (0.278, 0.447, 0.702, 1.0)

TOOLTIP_BACK = (0.114, 0.114, 0.114, 1.0)
TOOLTIP_OUTLINE = (0.141, 0.141, 0.141, 1.0)
TOOLTIP_TEXT = (0.851, 0.851, 0.851, 1.0)

# The 1px line Blender lays under every widget. Dark in the default theme, so
# it reads as the widget sitting slightly proud of the panel.
EMBOSS = (0.0, 0.0, 0.0, 0.149)

SHADOW_FAC = 0.2
SHADOW_WIDTH = 6

# Drag-and-drop feedback. The veil is deliberately light: the row being moved
# has to stay readable in its old place, and an earlier near-opaque version
# left a black hole in the stack until the drop landed.
DRAG_VEIL = (0.0, 0.0, 0.0, 0.40)
DRAG_SHADOW = (0.0, 0.0, 0.0, 0.35)

DEBUG_TEXT = (0.4, 0.9, 0.5, 1.0)


# --- Metrics (unscaled pixels) ----------------------------------------------

HEADER_HEIGHT = 30
PAD = 8
GAP = 4
BUTTON_HEIGHT = 22

# Colour picker: roomy field, horizontal hue rail and separate numeric inputs.
PICKER_WIDTH = 264
PICKER_PAD = 14
PICKER_HEADER_HEIGHT = 26
PICKER_FIELD_HEIGHT = 156
PICKER_HUE_HEIGHT = 12
PICKER_CHANNEL_HEIGHT = 24
PICKER_GAP = 10

# --- the layer list ---------------------------------------------------------
# Its own numbers rather than borrowed PAD/GAP: a list row carries six controls
# side by side and needs more room between them than a panel needs at its edge.
# A row leaves breathing room around 24px previews and numeric controls,
# with a small vertical gap between the rounded cards.

LIST_ROW = 42
LIST_ROW_PAD = 8
LIST_ROW_GAP = 6
LAYER_PANEL_PAD = 12
LAYER_PANEL_GAP = 10
LAYER_BUTTON = 28
LAYER_RADIUS = 6
LAYER_ROW_INSET_Y = 3
LAYER_CHIP = 24
LAYER_CONTROL_HEIGHT = 24
LAYER_OPACITY_WIDTH = 44
# Between the layer's own chip and its mask chip: they are a pair, so they sit
# closer to each other than to anything else.
LIST_CHIP_GAP = 4
# The inset the selection highlight is drawn with, left and right.
LIST_INSET = 2

# The scrollbar down the right edge of a list, and the gutter kept clear for
# it. The gutter is reserved whether or not the bar is showing: a list whose
# rows widen the moment the stack gets short enough to fit is a list that
# twitches.
SCROLLBAR_WIDTH = 4
SCROLLBAR_PAD = 3

# The colour tag strip down the left edge of a row. Narrow on purpose: it is
# read at a glance from a column of them, not looked at, and it must not take
# width from the name.
LIST_TAG_WIDTH = 4

# The filter mark under a chip: how thick, and how far below the chip it sits.
LIST_MARK_HEIGHT = 2
LIST_MARK_GAP = 2
# One level of nesting inside a group. Wide enough for the disclosure triangle
# the parent draws, so a child lines up under it.
LIST_INDENT = 14

# Blender's base widget size. Every corner radius is a fraction of this, which
# is how its buttons stay equally round at any height.
WIDGET_UNIT = 20

ROUNDNESS = 0.4
PANEL_ROUNDNESS = 0.4

# Vertical shading across a widget's inner colour, as Blender's -100..100
# integers. Zero in the default theme; honoured for the themes that set it.
SHADE_TOP = 0
SHADE_DOWN = 0

# Text. Not authored: Blender draws every piece of interface text at
# `ui_styles[0].<style>.points`, and all three of its styles are 11 points in
# the default preferences. Writing 12 and 10 here is what made our labels read
# as "some other application's" next to Blender's own -- a point off in either
# direction is visible at these sizes. `refresh()` rebinds them from the
# user's preferences, so raising the interface font size in Preferences moves
# our text with Blender's.
#
# Small and body are the same number on purpose. Blender has no smaller style:
# a menu row, a list row, a tooltip and a button all draw at `widget.points`.
FONT_SIZE = 11
FONT_SIZE_SMALL = 11
FONT_SIZE_TITLE = 11

# The drop shadow Blender puts under interface text. Easy to miss and the
# reason our labels looked thinner than its own: on a mid-grey widget the
# shadow is what separates a glyph from the fill behind it.
#
# MEASURED TRAP: the style stores 1 for the default "outline" shadow, and
# `blf.shadow` rejects 1 outright -- "expected arg to be in (0, 3, 5, 6)".
# The Python API spells outline 6 and the style spells it 1. `_SHADOW_LEVELS`
# is that translation; what is stored here is always a number blf accepts.
FONT_SHADOW = 6
FONT_SHADOW_OFFSET = (0, -1)
FONT_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.5)


# --- Reading the user's theme ------------------------------------------------


def _rgba(value, alpha=None):
    """Normalise a theme colour, which may be RGB or RGBA, to RGBA."""
    channels = tuple(value)
    if len(channels) >= 4:
        return channels[:3] + ((alpha,) if alpha is not None else (channels[3],))
    return channels[:3] + ((1.0 if alpha is None else alpha),)


def _shade(color, factor):
    """Scale a colour's brightness, keeping its alpha."""
    return tuple(min(1.0, max(0.0, c * factor)) for c in color[:3]) + (color[3],)


def _mix(a, b, amount):
    """Blend `a` towards `b`, keeping `a`'s alpha."""
    return tuple(
        a[index] + (b[index] - a[index]) * amount for index in range(3)
    ) + (a[3],)


def _to_linear(value):
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _wash(base, lift):
    """White at the alpha that lifts `base` by `lift` of displayed value.

    Blending happens on linear values, so a white wash is far stronger on a
    dark grey than its alpha suggests: 4% over the 0.11 panel grey came out at
    0.25, the same grey as a number field. The alpha is solved for instead:
    the linear step from `base` to `base + lift`, over what white has left to
    give.
    """
    value = max(base[:3])
    low = _to_linear(value)
    high = _to_linear(min(1.0, value + lift))
    alpha = (high - low) / (1.0 - low) if low < 1.0 else 0.0
    return (1.0, 1.0, 1.0, max(0.0, alpha))


SUBPANEL_BACK = _wash(BOX_INNER, SUBPANEL_LIFT)


def hover(color):
    """Blender's hover rule: the same colour, lifted.

    Public because a widget that is both *on* and hovered has to lift its own
    colour -- falling back to the shared hover grey would hide the fact that
    it is on, which is exactly what made the symmetry toggles look dead under
    the pointer.
    """
    return _shade(color, 1.15)


_hover = hover


def _derive(ui, view3d):
    """Turn a theme into the palette this module exposes.

    Kept as one pure function so a probe can call it with any theme and see
    what the interface would look like without opening a window.
    """
    regular = ui.wcol_regular
    box = ui.wcol_box
    item = ui.wcol_list_item
    menu_back = ui.wcol_menu_back
    menu_item = ui.wcol_menu_item
    tooltip = ui.wcol_tooltip

    background = _rgba(view3d.gradients.gradient)
    widget = _rgba(regular.inner)
    text = _rgba(regular.text)
    selection = _rgba(item.inner_sel)

    return {
        "BG_WINDOW": _shade(background, 0.62),
        "BG_PANEL": _shade(background, 0.77),
        "BG_HEADER": _shade(background, 0.66),
        "BG_CANVAS": _shade(background, 0.46),

        "BORDER": _rgba(ui.editor_border),
        "SEPARATOR": _rgba(regular.outline),
        "OUTLINE": _rgba(regular.outline),

        "WIDGET": widget,
        "WIDGET_HOVER": _hover(widget),
        "WIDGET_ACTIVE": _rgba(regular.inner_sel),
        "WIDGET_TEXT": text,
        "WIDGET_TEXT_DIM": _mix(text, widget, 0.45),
        "WIDGET_TEXT_SEL": _rgba(regular.text_sel),

        "ACCENT": selection,
        "AXIS": {
            'X': _rgba(ui.axis_x),
            'Y': _rgba(ui.axis_y),
            'Z': _rgba(ui.axis_z),
        },
        "METER": _mix(selection, widget, 0.5),

        "LIST_SEL": selection,
        "LIST_HOVER": _rgba(item.item),
        "BOX_INNER": _rgba(box.inner),
        "SUBPANEL_BACK": _wash(_rgba(box.inner), SUBPANEL_LIFT),

        "MENU_BACK": _rgba(menu_back.inner),
        "MENU_OUTLINE": _rgba(menu_back.outline),
        "MENU_TEXT": _rgba(menu_item.text),
        "MENU_SEL": _rgba(menu_item.inner_sel),

        "TOOLTIP_BACK": _rgba(tooltip.inner),
        "TOOLTIP_OUTLINE": _rgba(tooltip.outline),
        "TOOLTIP_TEXT": _rgba(tooltip.text),

        "EMBOSS": _rgba(ui.widget_emboss),
        "SHADOW_FAC": float(ui.menu_shadow_fac),
        "SHADOW_WIDTH": int(ui.menu_shadow_width),

        "ROUNDNESS": float(regular.roundness),
        "PANEL_ROUNDNESS": float(ui.panel_roundness),
        "SHADE_TOP": int(regular.shadetop) if regular.show_shaded else 0,
        "SHADE_DOWN": int(regular.shadedown) if regular.show_shaded else 0,
    }


# Style `shadow` -> the level `blf.shadow` will take. 1 is Blender's outline
# shadow, which blf spells 6; 2 and 4 are not values the preferences produce,
# and they round down to the nearest blur that exists.
_SHADOW_LEVELS = {0: 0, 1: 6, 2: 3, 3: 3, 4: 5, 5: 5}


def _derive_text(style):
    """Font metrics from a `ThemeStyle`: the same numbers Blender draws with.

    Blender scales `points` by the interface scale at draw time, which is what
    `Widget.px` does to these -- so they are stored unscaled, like every other
    metric in this module.
    """
    widget = style.widget
    return {
        "FONT_SIZE": float(widget.points),
        "FONT_SIZE_SMALL": float(widget.points),
        "FONT_SIZE_TITLE": float(style.panel_title.points),
        "FONT_SHADOW": _SHADOW_LEVELS.get(int(widget.shadow), 0),
        "FONT_SHADOW_OFFSET": (int(widget.shadow_offset_x),
                               int(widget.shadow_offset_y)),
        "FONT_SHADOW_COLOR": (float(widget.shadow_value),
                              float(widget.shadow_value),
                              float(widget.shadow_value),
                              float(widget.shadow_alpha)),
    }


def _derive_tags(active):
    """Layer tag colours from Blender's collection tags, or {} to keep ours.

    The same eight colours the outliner uses, because they mean the same
    thing: a tag is only useful if it reads as the colour the user already
    associates with that slot.
    """
    entries = getattr(active, "collection_color", None)
    if not entries:
        return {}

    colors = dict(TAG_COLORS)
    for index, entry in enumerate(entries[:8]):
        colors[f"COLOR_{index + 1:02d}"] = tuple(entry.color) + (1.0,)
    return {"TAG_COLORS": colors}


def refresh(context=None):
    """Re-read the palette from Blender's active theme. True when it changed.

    Called from the draw handler, so switching themes with the window open
    takes effect on the next frame. Reading ~40 floats out of RNA is nothing
    next to rendering the viewport behind us.
    """
    context = context or bpy.context
    try:
        preferences_ = context.preferences
        active = preferences_.themes[0]
        palette = _derive(active.user_interface, active.view_3d.space)
        palette.update(_derive_text(preferences_.ui_styles[0]))
        palette.update(_derive_tags(active))
    except (AttributeError, IndexError, TypeError):
        # No preferences to read (background mode, a stripped context). The
        # literals above already are the default theme, so there is nothing to
        # fall back to -- we are already there.
        return False

    current = globals()
    if all(current.get(key) == value for key, value in palette.items()):
        return False

    current.update(palette)
    return True


# --- Derived metrics ---------------------------------------------------------


def radius(height, ui_scale=1.0, roundness=None):
    """Blender's corner radius for a widget of `height` device pixels.

    `roundness * widget_unit`, clamped so a short widget turns into a pill
    rather than growing corners bigger than itself -- which is exactly what
    Blender does, and why its number fields and its buttons look related.
    """
    factor = ROUNDNESS if roundness is None else roundness
    return min(factor * WIDGET_UNIT * ui_scale, height * 0.5)


def scale(context=None):
    """Blender's UI scale multiplied by the add-on's own factor."""
    context = context or bpy.context
    ui_scale = context.preferences.system.ui_scale

    prefs = preferences.get(context)
    if prefs is not None:
        ui_scale *= prefs.ui_scale

    return ui_scale


def px(value, ui_scale):
    """Scale an authored metric to device pixels."""
    return int(round(value * ui_scale))

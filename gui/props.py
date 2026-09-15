# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender's property rows, drawn by us.

None of Blender's own regions may show inside a PaintR2 window, so
the four brush popovers -- Brush, Stroke, Falloff, Cursor -- are drawn here
instead, bound to the very same RNA properties. What Blender lays out with
``layout.prop()`` under ``use_property_split``, this module lays out by hand:

    +--------------------------------------------------+
    |          Stroke Method   [ Space            v ]  |
    |               Spacing    [      10%        ][~]  |
    |                          [x] Adjust Strength...  |
    |            Dash Ratio    [########## 1.000    ]  |
    |            Jitter Unit   [  View  ][   Brush  ]  |
    |  > [ ] Stabilize Stroke                          |
    +--------------------------------------------------+

Two rules shape everything below.

**No bpy.** The panel never reads or writes a property. It is handed a list of
plain dicts -- one per row, rebuilt from RNA every redraw by ``window.py`` --
and reports edits back through ``on_change(owner, key, value)``. That is what
keeps the toolkit testable without Blender, and it is also what makes the
conditional rows work: Blender only draws Spacing when the stroke method is
Space, and so do we, because the row simply is not in the list.

**Immediate mode.** The rows are re-described on every frame, so building a
widget per row would mean rebuilding the tree sixty times a second and losing
a drag in progress every time. The panel draws and hit-tests the list itself
and keeps only what cannot be derived from RNA: which sub-panels are open,
and what the pointer is currently dragging.

Row kinds
---------

``separator``  blank space
``label``      text, no control
``enum``       a value that opens a list             -- ``items``, ``value``
``enum_row``   the same, drawn expanded as segments  -- ``items``, ``value``
``bool``       a checkbox                            -- ``value``, ``text``
``value``      a number, dragged or typed            -- ``value``, ``min`` ..
``color``      a swatch that opens the picker        -- ``value`` (rgba)
``idblock``    a datablock: pick, make, clear        -- ``items``, ``buttons``
``preview``    a square picture, nothing to click    -- ``texture``
``panel``      a collapsible group                   -- ``rows``, ``toggle``
``curve``      the falloff curve editor              -- ``samples``, ``points``

Every row may carry ``suffix``: the small square toggles Blender aligns onto
the end of a row -- pressure, overlay visibility, "unified" -- as a list of
``{key, owner, value, icon, desc}``.
"""

import math
import time

from . import draw
from . import icons
from . import theme
from .colorpicker import ColorSwatch
from .widget import Widget
from .widgets import Menu, TextField


# Which small icon a suffix toggle draws, by name. Names rather than functions
# because the spec that carries them is built in window.py, which must not
# import anything out of gui/.
SUFFIX_ICONS = {
    'PRESSURE': icons.pressure,
    'BRUSH': icons.brush,
    'STROKE': icons.stroke,
    'EYE': icons.eye_open,
    'EYE_OFF': icons.eye_closed,
    'PLUS': icons.plus,
    'CROSS': icons.cross,
    'FOLDER': icons.folder,
}


def _dim(color, active):
    """Blender's inactive look: the same colour, faded into the panel."""
    if active:
        return color
    return tuple(color[:3]) + (color[3] * 0.4,)


class PropertyPanel(Widget):
    """A stack of property rows, drawn from a spec and edited in place."""

    # Blender's row is one widget unit tall with a couple of pixels between,
    # and its property split gives the label 40% of the width.
    ROW_HEIGHT = 20
    ROW_GAP = 2
    PADDING = 8
    SPLIT = 0.4
    INDENT = 10
    SEPARATOR = 6

    # A suffix toggle is square, so it is as tall as the row it sits on.
    SUFFIX = 20
    # How close to an edge a click on a number field counts as its step arrow.
    ARROW_ZONE = 14
    # How far the pointer travels for a full sweep of a number field that has
    # no bounds worth mapping a track to.
    DRAG_RANGE = 200

    CURVE_HEIGHT = 132
    CURVE_POINT = 8

    # A texture preview. Square, and tall enough that a noise pattern reads as
    # a pattern rather than as a grey smudge.
    PREVIEW_HEIGHT = 96

    DOUBLE_CLICK_SECONDS = 0.35

    def __init__(self, name="properties"):
        super().__init__(name)
        self.rows = []
        self.width_hint = 280

        # The name of a theme colour, not a colour: `theme.refresh` rebinds
        # those when Blender's theme changes, and a value captured at build
        # time would go stale. A popover draws its own frame around the panel
        # and leaves this None; a panel embedded in the layout sets it, which
        # also turns on clipping -- an embedded panel has a fixed height and
        # its rows must not spill onto whatever is below it.
        self.background = None

        # (owner, key, value) -- one write, straight through to RNA.
        self.on_change = None
        # (action, *args) -- curve edits and anything that is an operator
        # rather than a property.
        self.on_action = None

        # Overlays this panel opens. Its own, not the root's: the layer panel
        # has a menu of its own and the two must not fight over it.
        self.menu = Menu(name="prop_menu")
        self.field = TextField(name="prop_field")
        self.field.on_commit = self._on_typed
        self.swatch = ColorSwatch((1.0, 1.0, 1.0, 1.0), self._on_color, name="prop_color")
        self.swatch.visible = False

        # What survives a spec rebuild. Everything else is re-read from RNA.
        self._open = {}
        self._drag = None
        self._edit = None
        self._last_click = (None, 0.0)
        self._curve_drag = None
        self._hover_key = None

        # How far the content is scrolled up, in pixels. Only an embedded
        # panel scrolls: a popover grows to fit its rows, so it never has
        # anything hidden to scroll to.
        self.scroll = 0

    # --- tree -------------------------------------------------------------

    def attach(self, root, parent=None):
        super().attach(root, parent)
        for widget in (self.menu, self.field, self.swatch):
            widget.attach(root, self)

    def set_rows(self, rows, width=None):
        """Replace the spec. Cheap: it happens on every redraw."""
        self.rows = list(rows or ())
        if width:
            self.width_hint = width

    # --- geometry ---------------------------------------------------------

    def row_height(self, row):
        kind = row.get("kind")
        if kind == 'separator':
            return self.px(self.SEPARATOR)
        if kind == 'curve':
            return self.px(self.CURVE_HEIGHT)
        if kind == 'preview':
            return self.px(self.PREVIEW_HEIGHT)
        if kind == 'panel':
            height = self.px(self.ROW_HEIGHT)
            if self.is_open(row):
                inner = self.measure(row.get("rows") or ())
                if inner:
                    height += self.px(self.ROW_GAP) + inner
            return height
        return self.px(self.ROW_HEIGHT)

    def measure(self, rows):
        """Total height of `rows` in device pixels, gaps included."""
        gap = self.px(self.ROW_GAP)
        total = 0
        for index, row in enumerate(rows):
            if index:
                total += gap
            total += self.row_height(row)
        return total

    def content_height(self):
        pad = self.px(self.PADDING)
        return self.measure(self.rows) + pad * 2

    def scrolls(self):
        """Whether this panel is the kind that can scroll, and needs to."""
        return self.background is not None and self.max_scroll() > 0

    def max_scroll(self):
        if self.background is None:
            return 0
        return max(0, self.content_height() - self.height)

    def clamp_scroll(self):
        limit = self.max_scroll()
        wanted = max(0, min(self.scroll, limit))
        if wanted != self.scroll:
            self.scroll = wanted
            return True
        return False

    def gutter(self):
        """Width kept clear on the right for the scrollbar.

        Reserved only while there is something to scroll: unlike the layer
        list, whose length changes a row at a time, this panel's height jumps
        whenever a sub-panel is folded, so there is no steady layout for the
        gutter to protect.
        """
        if not self.scrolls():
            return 0
        return self.px(theme.SCROLLBAR_WIDTH) + self.px(theme.SCROLLBAR_PAD) * 2

    def is_open(self, row):
        """Whether a sub-panel is expanded.

        Until the user touches one it sits where the spec puts it, which is
        where Blender's own `bl_options = {'DEFAULT_CLOSED'}` puts it.
        """
        key = row.get("key") or row.get("label")
        return self._open.get(key, bool(row.get("open")))

    def _toggle_open(self, row):
        key = row.get("key") or row.get("label")
        self._open[key] = not self.is_open(row)

    def placed(self):
        """[(row, rect, depth)] for every visible row, laid out top-down.

        The scroll offset is applied here rather than at draw time, so
        hit-testing and drawing cannot disagree about where a row is -- the
        two used to be one function apart, and a scrolled panel where the
        clicks land on the wrong row is not a panel.
        """
        pad = self.px(self.PADDING)
        out = []
        self._place(
            self.rows,
            self.x + pad,
            self.y + self.height - pad + self.scroll,
            self.width - pad * 2 - self.gutter(),
            0,
            out,
        )
        return out

    def _place(self, rows, x, top, width, depth, out):
        gap = self.px(self.ROW_GAP)
        indent = self.px(self.INDENT)

        for row in rows:
            height = self.row_height(row)
            if row.get("kind") == 'panel':
                header = self.px(self.ROW_HEIGHT)
                out.append((row, (x, top - header, width, header), depth))
                if self.is_open(row):
                    self._place(
                        row.get("rows") or (),
                        x + indent,
                        top - header - gap,
                        width - indent,
                        depth + 1,
                        out,
                    )
            else:
                out.append((row, (x, top - height, width, height), depth))
            top -= height + gap

    def _columns(self, rect):
        """(label rect, value rect) for a property-split row."""
        x, y, width, height = rect
        gap = self.px(theme.GAP)
        label_w = int(width * self.SPLIT) - gap
        return (
            (x, y, max(0, label_w), height),
            (x + label_w + gap, y, max(0, width - label_w - gap), height),
        )

    def _idblock_rects(self, row, rect):
        """(browse field, [button rects], unlink rect) for a datablock row.

        Full width: Blender's `template_ID` spans the row rather than sitting
        in the value column, because the name it has to show is longer than
        anything else on the panel.

        **The two states do not look alike, and that is the point.** Blender
        draws an empty slot as a narrow browse dropdown and one wide button
        saying what it would do -- New, Open. A filled one is the name across
        the row with small icon buttons after it and an X to clear it. Drawn
        the same way here, because the first version of this row wrote the
        type's name in the empty field and a texture called "Texture" then
        looked exactly like no texture at all.
        """
        x, y, width, height = rect
        button = self.px(self.SUFFIX)
        gap = self.px(theme.GAP)
        buttons = row.get("buttons") or ()

        if not row.get("value"):
            browse = button * 2
            field = (x, y, browse, height)
            left = x + browse + gap
            span = max(0, width - browse - gap)
            rects = []
            for index in range(len(buttons)):
                start = left + int(span * index / max(1, len(buttons)))
                end = left + int(span * (index + 1) / max(1, len(buttons)))
                rects.append((start, y, max(0, end - start - gap), height))
            return field, rects, None

        cursor = x + width - button
        unlink = (cursor, y, button, height)
        rects = []
        for _ in buttons:
            cursor -= gap + button
            rects.insert(0, (cursor, y, button, height))

        return (x, y, max(0, cursor - gap - x), height), rects, unlink

    def _suffix_rects(self, row, value_rect):
        """Split the value column into (field, [toggle rects])."""
        suffix = row.get("suffix") or ()
        if not suffix:
            return value_rect, ()

        x, y, width, height = value_rect
        size = self.px(self.SUFFIX)
        rects = []
        cursor = x + width - size * len(suffix)
        field = (x, y, max(0, cursor - x), height)
        for _ in suffix:
            rects.append((cursor, y, size, height))
            cursor += size
        return field, rects

    # --- value formatting -------------------------------------------------

    @staticmethod
    def format_value(row, value=None):
        """What Blender writes inside the field, for this property's subtype."""
        if value is None:
            # A property carrying a unit was already printed by Blender
            # itself: which unit a length lands in is the scene's business,
            # not a format string's. See `window._unit_display`.
            display = row.get("display")
            if display:
                return display
            value = row.get("value", 0.0)
        subtype = row.get("subtype", 'NONE')
        precision = int(row.get("precision", 3) or 3)

        if row.get("int"):
            if subtype == 'PERCENTAGE':
                return f"{int(round(value))}%"
            if subtype == 'PIXEL':
                return f"{int(round(value))} px"
            return f"{int(round(value))}"

        if subtype == 'PERCENTAGE':
            return f"{value:.{max(0, precision - 2)}f}%"
        if subtype == 'ANGLE':
            # RNA stores an angle in radians and Blender never shows one: the
            # field says degrees, so a rotation reads as 45 rather than 0.785.
            return f"{math.degrees(value):.1f}\u00b0"
        return f"{value:.{precision}f}"

    @staticmethod
    def _parse(row, text):
        """Read a typed value back. Returns None when it is not a number."""
        if row.get("unit"):
            # "5 cm" is a length Blender reads and Python does not, so the
            # text goes back unparsed and `window._from_unit` converts it
            # against the scene the value belongs to.
            return text.strip() or None

        cleaned = text.strip().rstrip("%").rstrip("\u00b0")
        cleaned = cleaned.replace("px", "").strip()
        try:
            value = float(cleaned)
        except ValueError:
            return None
        if row.get("int"):
            return int(round(value))
        if row.get("subtype") == 'ANGLE':
            # Typed in degrees because that is what the field shows.
            return math.radians(value)
        return value

    # --- drawing ----------------------------------------------------------

    def draw(self):
        if self.background is None:
            for row, rect, depth in self.placed():
                self._draw_row(row, rect, depth)
            return

        if self.width <= 0 or self.height <= 0:
            return

        self.clamp_scroll()

        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)
        draw.rounded_rect(self.x, self.y, self.width, self.height, radius,
                          getattr(theme, self.background, theme.BOX_INNER))
        with draw.clip(self.x, self.y, self.width, self.height):
            for row, rect, depth in self.placed():
                self._draw_row(row, rect, depth)
        draw.rounded_rect_outline(self.x, self.y, self.width, self.height,
                                  radius, theme.OUTLINE)
        self._draw_scrollbar()

    def _draw_scrollbar(self):
        """The same bar the layer list has, for the same reason: without it a
        panel with more rows than room looks like a panel that is missing
        rows."""
        if not self.scrolls():
            return

        width = self.px(theme.SCROLLBAR_WIDTH)
        x = self.x + self.width - width - self.px(theme.SCROLLBAR_PAD)
        total = self.content_height()

        bar = max(self.px(24), int(self.height * self.height / total))
        travel = self.height - bar
        limit = self.max_scroll()
        position = (self.scroll / limit) if limit else 0.0
        y = self.y + self.height - bar - int(travel * position)

        radius = width * 0.5
        draw.rounded_rect(x, self.y, width, self.height, radius,
                          theme.OUTLINE[:3] + (theme.OUTLINE[3] * 0.5,))
        draw.rounded_rect(x, y, width, bar, radius, theme.WIDGET_TEXT_DIM)

    def _draw_row(self, row, rect, depth):
        kind = row.get("kind")
        active = bool(row.get("active", True))

        if kind == 'separator':
            return
        if kind == 'panel':
            self._draw_panel_header(row, rect)
            return
        if kind == 'curve':
            self._draw_curve(row, rect)
            return
        if kind == 'preview':
            self._draw_preview(row, rect)
            return
        if kind == 'label':
            self._draw_label(rect, row.get("label", ""), active, align='LEFT')
            return
        if kind == 'button':
            self._draw_button(row, rect, active)
            return
        if kind == 'idblock':
            self._draw_idblock(row, rect, active)
            return

        label_rect, value_rect = self._columns(rect)
        # A checkbox carries its own caption. Blender does the same: under
        # property split a boolean leaves the label column empty and writes
        # its name beside the box instead.
        if kind != 'bool':
            self._draw_label(label_rect, row.get("label", ""), active)

        field, suffix_rects = self._suffix_rects(row, value_rect)

        if kind == 'enum':
            self._draw_enum(row, field, active)
        elif kind == 'enum_row':
            self._draw_enum_row(row, field, active)
        elif kind == 'bool':
            self._draw_bool(row, field, active)
        elif kind == 'value':
            self._draw_number(row, field, active)
        elif kind == 'color':
            self._draw_color(row, field, active)
        elif kind in ('path', 'text'):
            self._draw_path(row, field, active)

        for spec, spot in zip(row.get("suffix") or (), suffix_rects):
            self._draw_suffix(spec, spot, active)

    def _draw_label(self, rect, text, active, align='RIGHT'):
        if not text:
            return
        x, y, width, height = rect
        size = self.px(theme.FONT_SIZE)
        text_width, text_height = draw.text_dimensions(text, size)
        colour = _dim(theme.WIDGET_TEXT, active)

        if align == 'RIGHT':
            x = x + width - min(text_width, width)
        draw.text_elided(
            x, y + (height - text_height) * 0.5, text, size, colour, width
        )

    def _widget_face(self, rect, hovered=False, sunken=False, corners=None):
        x, y, width, height = rect
        if sunken:
            background = theme.WIDGET_ACTIVE
        elif hovered:
            background = theme.WIDGET_HOVER
        else:
            background = theme.WIDGET
        draw.widget(
            x, y, width, height, theme.radius(height, self.ui_scale), background,
            outline=theme.OUTLINE,
            corners=draw.CORNER_ALL if corners is None else corners,
            shade_top=theme.SHADE_TOP,
            shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

    def _hovering(self, rect):
        x, y, width, height = rect
        mouse_x, mouse_y = self.root._last_mouse if self.root else (-1, -1)
        return x <= mouse_x < x + width and y <= mouse_y < y + height

    def _draw_enum(self, row, rect, active):
        x, y, width, height = rect
        self._widget_face(rect, self._hovering(rect))

        pad = self.px(theme.PAD)
        size = self.px(theme.FONT_SIZE)
        _, text_height = draw.text_dimensions("X", size)
        label = row.get("label_for") or _label_of(row.get("items"), row.get("value"))

        draw.text_elided(
            x + pad, y + (height - text_height) * 0.5, label, size,
            _dim(theme.WIDGET_TEXT, active), width - pad * 3,
        )
        mark = self.px(9)
        icons.chevron_down(
            x + width - pad * 0.5 - mark, y + (height - mark) * 0.5, mark,
            _dim(theme.WIDGET_TEXT_DIM, active),
        )

    def _draw_enum_row(self, row, rect, active):
        items = row.get("items") or ()
        if not items:
            return
        x, y, width, height = rect
        count = len(items)
        size = self.px(theme.FONT_SIZE)

        for index, (identifier, label) in enumerate(items):
            left = x + int(width * index / count)
            right = x + int(width * (index + 1) / count)
            spot = (left, y, right - left, height)
            on = identifier == row.get("value")
            corners = draw.CORNER_ALL
            if count > 1:
                if index == 0:
                    corners = draw.CORNER_LEFT
                elif index == count - 1:
                    corners = draw.CORNER_RIGHT
                else:
                    corners = draw.CORNER_NONE
            self._widget_face(
                spot, self._hovering(spot) and not on, sunken=on, corners=corners
            )
            colour = theme.WIDGET_TEXT_SEL if on else theme.WIDGET_TEXT
            draw.text_centered(
                left + (right - left) * 0.5, y + height * 0.5, label, size,
                _dim(colour, active),
            )

    def _checkbox_rect(self, rect):
        x, y, _width, height = rect
        box = self.px(14)
        return x, y + (height - box) // 2, box, box

    def _draw_bool(self, row, rect, active):
        box = self._checkbox_rect(rect)
        on = bool(row.get("value"))
        bx, by, size, _ = box

        draw.widget(
            bx, by, size, size, theme.radius(size, self.ui_scale, 0.2),
            theme.WIDGET_ACTIVE if on else theme.WIDGET,
            outline=theme.OUTLINE, emboss=theme.EMBOSS,
        )
        if on:
            icons.check(bx, by, size, _dim(theme.WIDGET_TEXT_SEL, active))

        text = row.get("text") or row.get("label") or ""
        if text:
            x, y, width, height = rect
            gap = self.px(theme.GAP)
            font = self.px(theme.FONT_SIZE)
            _, text_height = draw.text_dimensions(text, font)
            draw.text_elided(
                bx + size + gap, y + (height - text_height) * 0.5, text, font,
                _dim(theme.WIDGET_TEXT, active), width - size - gap * 2,
            )

    def _draw_number(self, row, rect, active):
        x, y, width, height = rect
        radius = theme.radius(height, self.ui_scale)
        hovered = self._hovering(rect)
        dragging = self._drag is not None and self._drag["id"] == _row_id(row)

        draw.widget(
            x, y, width, height, radius,
            theme.WIDGET_HOVER if (hovered or dragging) else theme.WIDGET,
            shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        if row.get("slider"):
            low = float(row.get("min", 0.0))
            high = float(row.get("max", 1.0))
            span = high - low
            fraction = 0.0 if span <= 0 else (float(row.get("value", 0.0)) - low) / span
            filled = int(width * min(1.0, max(0.0, fraction)))
            if filled > 0:
                corners = draw.CORNER_ALL if filled >= width else draw.CORNER_LEFT
                draw.rounded_rect(
                    x, y, filled, height, radius,
                    _dim(theme.ACCENT, active), corners=corners,
                )

        draw.rounded_rect_outline(x, y, width, height, radius, theme.OUTLINE)

        size = self.px(theme.FONT_SIZE)
        draw.text_centered(
            x + width * 0.5, y + height * 0.5, self.format_value(row), size,
            _dim(theme.WIDGET_TEXT, active),
        )

        # Blender puts step arrows on a number field under the pointer. They
        # are the only way to nudge a value by exactly one step.
        if hovered and not dragging and not row.get("slider"):
            mark = self.px(7)
            colour = _dim(theme.WIDGET_TEXT_DIM, active)
            icons.chevron_left(x + self.px(3), y + (height - mark) * 0.5, mark, colour)
            icons.chevron_right(
                x + width - self.px(3) - mark, y + (height - mark) * 0.5, mark, colour
            )

    # --- paths and free text ----------------------------------------------
    #
    # One kind with two faces. A `text` row is a field you type in; a `path`
    # row is the same field with a browse button welded to its right edge,
    # because a folder is picked far more often than it is typed. The button
    # does not write anything itself -- it fires `('browse', owner, key)` and
    # `window.py` puts Blender's own file browser up, the same way the HDRI
    # and mask-export rows already do. The toolkit has no business knowing
    # what a directory is.

    BROWSE_ZONE = 22

    def _path_rects(self, row, rect):
        """`(field_rect, browse_rect)`. `browse_rect` is None for plain text."""
        if row.get("kind") != 'path':
            return rect, None

        x, y, width, height = rect
        zone = self.px(self.BROWSE_ZONE)
        # Too narrow to split and still show a path: the field keeps the room
        # and the folder is picked by typing. A two-pixel button is a trap.
        if width < zone * 2:
            return rect, None
        gap = max(1, self.px(2))
        return (x, y, width - zone - gap, height), (x + width - zone, y, zone, height)

    def _draw_path(self, row, rect, active):
        field, browse = self._path_rects(row, rect)

        x, y, width, height = field
        radius = theme.radius(height, self.ui_scale)
        draw.widget(
            x, y, width, height, radius,
            theme.WIDGET_HOVER if self._hovering(field) else theme.WIDGET,
            shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )
        draw.rounded_rect_outline(x, y, width, height, radius, theme.OUTLINE)

        size = self.px(theme.FONT_SIZE)
        pad = self.px(theme.PAD)
        text = str(row.get("value") or "")
        _, text_height = draw.text_dimensions("X", size)
        draw.text_elided(
            x + pad, y + (height - text_height) * 0.5, text, size,
            _dim(theme.WIDGET_TEXT, active), width - pad * 2,
            keep='TAIL' if row.get("kind") == 'path' else 'HEAD',
        )

        if browse is None:
            return

        self._widget_face(browse, self._hovering(browse))
        bx, by, bwidth, bheight = browse
        mark = int(min(bwidth, bheight) * 0.6)
        icons.folder(
            bx + (bwidth - mark) * 0.5, by + (bheight - mark) * 0.5, mark,
            _dim(theme.WIDGET_TEXT, active),
        )

    def _press_path(self, row, rect, event):
        if not row.get("active", True):
            return

        field, browse = self._path_rects(row, rect)

        if browse is not None and _inside(browse, event.mouse_x, event.mouse_y):
            self._emit_action('browse', row.get("owner"), row.get("key"))
            return

        self._edit = (row.get("owner"), row.get("key"), row)
        self.field.open_at(
            field[0], field[1], field[2], field[3],
            text=str(row.get("value") or ""),
            over=self._overlay_root(),
        )

    def _draw_color(self, row, rect, active):
        x, y, width, height = rect
        colour = tuple(row.get("value") or (0.0, 0.0, 0.0, 1.0))
        if len(colour) == 3:
            colour = colour + (1.0,)
        radius = theme.radius(height, self.ui_scale)
        draw.rounded_rect(x, y, width, height, radius, colour)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.OUTLINE)

    def _draw_suffix(self, spec, rect, active):
        x, y, width, height = rect
        on = bool(spec.get("value"))
        self._widget_face(rect, self._hovering(rect) and not on, sunken=on)

        icon = SUFFIX_ICONS.get(spec.get("icon"))
        if icon is None:
            return
        # Blender puts a 16px icon in a 20px button; anything smaller and the
        # closed eye turns into three dots.
        size = int(min(width, height) * 0.8)
        icon(
            x + (width - size) * 0.5, y + (height - size) * 0.5, size,
            _dim(theme.WIDGET_TEXT_SEL if on else theme.WIDGET_TEXT, active),
        )

    def _draw_button(self, row, rect, active):
        """A full-width action. Blender's `layout.operator`, in our metrics."""
        self._widget_face(rect, self._hovering(rect) and active)
        draw.text_centered(
            rect[0] + rect[2] * 0.5, rect[1] + rect[3] * 0.5,
            row.get("label", ""), self.px(theme.FONT_SIZE),
            _dim(theme.WIDGET_TEXT, active),
        )

    def _draw_idblock(self, row, rect, active):
        """Blender's `template_ID`, in our metrics: pick one, make one, drop one."""
        field, button_rects, unlink_rect = self._idblock_rects(row, rect)
        buttons = row.get("buttons") or ()
        name = row.get("value") or ""

        pad = self.px(theme.PAD)
        size = self.px(theme.FONT_SIZE)
        mark = self.px(9)
        _, text_height = draw.text_dimensions("X", size)

        self._widget_face(field, self._hovering(field) and active)
        if name:
            draw.text_elided(
                field[0] + pad, field[1] + (field[3] - text_height) * 0.5,
                name, size, _dim(theme.WIDGET_TEXT, active),
                field[2] - pad * 2 - mark,
            )
        icons.chevron_down(
            field[0] + field[2] - pad * 0.5 - mark,
            field[1] + (field[3] - mark) * 0.5, mark,
            _dim(theme.WIDGET_TEXT_DIM, active),
        )

        # Wide and labelled while the slot is empty, a small icon once it is
        # full: the button is the only thing on the row with anything to say
        # until there is a name to show.
        for spec, spot in zip(buttons, button_rects):
            self._widget_face(spot, self._hovering(spot) and active)
            if name:
                icon = SUFFIX_ICONS.get(spec.get("icon"))
                if icon is not None:
                    glyph = int(min(spot[2], spot[3]) * 0.6)
                    icon(spot[0] + (spot[2] - glyph) * 0.5,
                         spot[1] + (spot[3] - glyph) * 0.5, glyph,
                         _dim(theme.WIDGET_TEXT, active))
            else:
                draw.text_centered(
                    spot[0] + spot[2] * 0.5, spot[1] + spot[3] * 0.5,
                    spec.get("label", ""), size,
                    _dim(theme.WIDGET_TEXT, active),
                )

        if unlink_rect is not None:
            self._widget_face(unlink_rect, self._hovering(unlink_rect) and active)
            glyph = int(min(unlink_rect[2], unlink_rect[3]) * 0.6)
            icons.cross(
                unlink_rect[0] + (unlink_rect[2] - glyph) * 0.5,
                unlink_rect[1] + (unlink_rect[3] - glyph) * 0.5, glyph,
                _dim(theme.WIDGET_TEXT, active),
            )

    def _draw_preview(self, row, rect):
        """A square of what the texture makes, centred in the row.

        Nothing to click. Blender's own preview is a button that opens a
        browser; this one is a picture, because picking the datablock is the
        row above it and doing it twice would be two ways to the same place.
        """
        x, y, width, height = rect
        size = min(height, width)
        left = x + (width - size) // 2
        radius = self.px(2)

        texture = row.get("texture")
        if texture is None:
            # No GPU context, or a texture that evaluates to nothing. An empty
            # frame says "there would be a picture here" without pretending.
            draw.rounded_rect(left, y, size, size, radius, theme.WIDGET)
        else:
            draw.image_rect(left, y, size, size, texture)

        draw.rounded_rect_outline(left, y, size, size, radius, theme.OUTLINE)

    def _draw_panel_header(self, row, rect):
        x, y, width, height = rect
        gap = self.px(theme.GAP)
        mark = self.px(9)
        colour = theme.WIDGET_TEXT

        if self._hovering(rect):
            draw.rounded_rect(
                x, y, width, height, theme.radius(height, self.ui_scale),
                theme.LIST_HOVER,
            )

        arrow = icons.chevron_down if self.is_open(row) else icons.chevron_right
        arrow(x, y + (height - mark) * 0.5, mark, theme.WIDGET_TEXT_DIM)

        cursor = x + mark + gap
        toggle = row.get("toggle")
        if toggle is not None:
            box = self.px(14)
            by = y + (height - box) // 2
            draw.widget(
                cursor, by, box, box, theme.radius(box, self.ui_scale, 0.2),
                theme.WIDGET_ACTIVE if toggle.get("value") else theme.WIDGET,
                outline=theme.OUTLINE, emboss=theme.EMBOSS,
            )
            if toggle.get("value"):
                icons.check(cursor, by, box, theme.WIDGET_TEXT_SEL)
            cursor += box + gap

        size = self.px(theme.FONT_SIZE)
        _, text_height = draw.text_dimensions("X", size)
        draw.text_elided(
            cursor, y + (height - text_height) * 0.5, row.get("label", ""), size,
            colour, x + width - cursor,
        )

    # --- the falloff curve ------------------------------------------------

    def _curve_box(self, rect):
        """The plotting area: the row, inset so a point at 0 or 1 still fits.

        Wider than tall, like Blender's own curve widget -- the two axes carry
        different things (distance across, strength up), so there is nothing
        for a square to be truer to.
        """
        x, y, width, height = rect
        pad = self.px(theme.GAP)
        inset = self.px(self.CURVE_POINT) // 2
        return (
            x + pad + inset,
            y + pad + inset,
            max(1, width - (pad + inset) * 2),
            max(1, height - (pad + inset) * 2),
        )

    def _draw_curve(self, row, rect):
        x, y, width, height = self._curve_box(rect)

        draw.rect(x, y, width, height, theme.BG_CANVAS)

        # Blender's curve widget rules the box in quarters; without them there
        # is no way to read where a control point actually sits.
        grid = theme.SEPARATOR[:3] + (0.5,)
        line = max(1, self.px(1))
        for step in range(1, 4):
            draw.rect(x + width * step / 4.0, y, line, height, grid)
            draw.rect(x, y + height * step / 4.0, width, line, grid)

        samples = row.get("samples") or ()
        if len(samples) > 1:
            points = [
                (x + width * index / (len(samples) - 1), y + height * value)
                for index, value in enumerate(samples)
            ]
            draw.polyline(points, theme.WIDGET_TEXT, max(1.0, self.px(1.5)))

        size = self.px(self.CURVE_POINT)
        for index, (px_, py_) in enumerate(row.get("points") or ()):
            cx = x + width * px_
            cy = y + height * py_
            selected = index == row.get("selected", -1)
            draw.rect(
                cx - size * 0.5, cy - size * 0.5, size, size,
                theme.WIDGET_TEXT_SEL if selected else theme.BG_CANVAS,
            )
            draw.rect_outline(
                cx - size * 0.5, cy - size * 0.5, size, size, theme.WIDGET_TEXT
            )

        draw.rect_outline(x, y, width, height, theme.OUTLINE)

    def _curve_hit(self, row, rect, mouse_x, mouse_y):
        x, y, width, height = self._curve_box(rect)
        if width <= 0 or height <= 0:
            return None
        reach = self.px(self.CURVE_POINT)
        for index, (px_, py_) in enumerate(row.get("points") or ()):
            cx = x + width * px_
            cy = y + height * py_
            if abs(mouse_x - cx) <= reach and abs(mouse_y - cy) <= reach:
                return index
        return None

    def _curve_coords(self, rect, mouse_x, mouse_y):
        x, y, width, height = self._curve_box(rect)
        if width <= 0 or height <= 0:
            return 0.0, 0.0
        return (
            min(1.0, max(0.0, (mouse_x - x) / width)),
            min(1.0, max(0.0, (mouse_y - y) / height)),
        )

    # --- events -----------------------------------------------------------

    def handle(self, event):
        if self._drag is not None:
            return self._drag_event(event)
        if self._curve_drag is not None:
            return self._curve_event(event)

        if event.wheel and self.scrolls() and self.contains(
            event.mouse_x, event.mouse_y
        ):
            self.scroll -= event.wheel * self.px(self.ROW_HEIGHT) * 2
            self.clamp_scroll()
            self.request_redraw()
            return True

        if event.is_move:
            # Tooltips are the root's, and it cannot see into an overlay: tell
            # it when the pointer has moved onto a different row so the delay
            # starts again there.
            key = self._row_key_at(event.mouse_x, event.mouse_y)
            if key != self._hover_key:
                self._hover_key = key
                if self.root is not None:
                    self.root.restart_tooltip()
            self.request_redraw()
            return False

        if not event.is_left_press:
            return False

        for row, rect, _depth in self.placed():
            if not _inside(rect, event.mouse_x, event.mouse_y):
                continue
            return self._press(row, rect, event)

        return False

    def _press(self, row, rect, event):
        kind = row.get("kind")

        if kind == 'panel':
            return self._press_panel(row, rect, event)
        if kind in ('separator', 'label', 'preview'):
            return True
        if kind == 'button':
            if row.get("active", True):
                # `("bake_export",)` is an action and no arguments, not one
                # argument that happens to be a tuple. Unpacked the same way a
                # suffix's action is, or the receiver gets a tuple where it
                # compares a string and every button silently does nothing.
                action = row.get("action")
                if action:
                    self._emit_action(*(action
                                        if isinstance(action, (list, tuple))
                                        else (action,)))
            return True
        if kind == 'curve':
            return self._press_curve(row, rect, event)
        if kind == 'idblock':
            return self._press_idblock(row, rect, event)

        _label_rect, value_rect = self._columns(rect)
        field, suffix_rects = self._suffix_rects(row, value_rect)

        for spec, spot in zip(row.get("suffix") or (), suffix_rects):
            if not _inside(spot, event.mouse_x, event.mouse_y):
                continue
            # A suffix is usually a property, but not always: some of them are
            # commands with a state (an eye that says "this is the one being
            # shown"). One carrying an action fires it instead of writing.
            action = spec.get("action")
            if action:
                self._emit_action(*(action if isinstance(action, (list, tuple))
                                    else (action,)))
            else:
                self._write(spec.get("owner"), spec.get("key"),
                            not bool(spec.get("value")))
            return True

        if not _inside(field, event.mouse_x, event.mouse_y):
            return True

        if kind == 'enum':
            self._open_enum(row, field)
        elif kind == 'enum_row':
            self._press_enum_row(row, field, event)
        elif kind == 'bool':
            self._write(row.get("owner"), row.get("key"), not bool(row.get("value")))
        elif kind == 'value':
            self._press_number(row, field, event)
        elif kind == 'color':
            self._open_color(row, field)
        elif kind in ('path', 'text'):
            self._press_path(row, field, event)
        return True

    def _press_idblock(self, row, rect, event):
        if not row.get("active", True):
            return True

        field, button_rects, unlink_rect = self._idblock_rects(row, rect)

        if unlink_rect and _inside(unlink_rect, event.mouse_x, event.mouse_y):
            self._emit_action(*row.get("unlink"))
            return True

        for spec, spot in zip(row.get("buttons") or (), button_rects):
            if _inside(spot, event.mouse_x, event.mouse_y):
                self._emit_action(*spec.get("action"))
                return True

        if _inside(field, event.mouse_x, event.mouse_y):
            # `open_at` declines an empty list, which is the right answer for
            # a file holding none of this kind: the buttons are the way in.
            self._open_enum(row, field)
        return True

    def _press_panel(self, row, rect, event):
        toggle = row.get("toggle")
        if toggle is not None:
            gap = self.px(theme.GAP)
            mark = self.px(9)
            box = self.px(14)
            left = rect[0] + mark + gap
            if left <= event.mouse_x < left + box:
                self._write(toggle.get("owner"), toggle.get("key"),
                            not bool(toggle.get("value")))
                return True

        self._toggle_open(row)
        self.request_redraw()
        return True

    def _press_enum_row(self, row, rect, event):
        items = row.get("items") or ()
        if not items:
            return
        x, _y, width, _height = rect
        index = int((event.mouse_x - x) / max(1, width) * len(items))
        index = min(len(items) - 1, max(0, index))
        identifier = items[index][0]
        if identifier != row.get("value"):
            self._write(row.get("owner"), row.get("key"), identifier)

    def _open_enum(self, row, rect):
        self._edit = (row.get("owner"), row.get("key"), row)
        self.menu.open_at(
            rect[0], rect[1], row.get("items") or (), self._on_enum_choice,
            title=row.get("label", ""), over=self._overlay_root(),
        )

    def _on_enum_choice(self, identifier):
        if self._edit is None:
            return
        owner, key, _row = self._edit
        self._edit = None
        self._write(owner, key, identifier)

    def _open_color(self, row, rect):
        colour = tuple(row.get("value") or (1.0, 1.0, 1.0, 1.0))
        if len(colour) == 3:
            colour = colour + (1.0,)
        self._edit = (row.get("owner"), row.get("key"), row)
        self.swatch.color = colour
        self.swatch.set_rect(*rect)
        if self.swatch.picker is not None:
            self.swatch.picker.open_for(self.swatch, over=self._overlay_root())

    def _on_color(self, colour):
        if self._edit is None:
            return
        owner, key, _row = self._edit
        self._write(owner, key, tuple(colour))

    # --- numbers ----------------------------------------------------------

    def _press_number(self, row, rect, event):
        key = row.get("key")
        identity = _row_id(row)
        now = time.monotonic()
        last_key, last_time = self._last_click
        self._last_click = (identity, now)

        if last_key == identity and now - last_time <= self.DOUBLE_CLICK_SECONDS:
            self._type_number(row, rect)
            return

        x, _y, width, _height = rect
        step = float(row.get("step", 1.0) or 1.0)
        zone = self.px(self.ARROW_ZONE)

        if not row.get("slider") and width > zone * 3:
            if event.mouse_x < x + zone:
                self._write(row.get("owner"), key,
                            self._clamp(row, float(row.get("value", 0.0)) - step))
                return
            if event.mouse_x >= x + width - zone:
                self._write(row.get("owner"), key,
                            self._clamp(row, float(row.get("value", 0.0)) + step))
                return

        self._drag = {
            "row": row,
            "id": _row_id(row),
            "key": key,
            "owner": row.get("owner"),
            "rect": rect,
            "start_x": event.mouse_x,
            "start_value": float(row.get("value", 0.0)),
            "moved": False,
        }
        self.root.capture(self)

        if row.get("slider"):
            self._write(row.get("owner"), key, self._at_x(row, rect, event.mouse_x))

    def _at_x(self, row, rect, mouse_x):
        x, _y, width, _height = rect
        low = float(row.get("min", 0.0))
        high = float(row.get("max", 1.0))
        ratio = 0.0 if width <= 0 else (mouse_x - x) / width
        return self._clamp(row, low + (high - low) * ratio)

    def _clamp(self, row, value):
        if isinstance(value, str):
            # A unit expression on its way to Blender's parser. It is clamped
            # there, by RNA, against the range this row only has a copy of.
            return value
        low = float(row.get("min", 0.0))
        high = float(row.get("max", 1.0))
        value = min(high, max(low, value))
        if row.get("int"):
            return int(round(value))
        return value

    def _drag_event(self, event):
        drag = self._drag
        row = drag["row"]

        if event.is_move:
            if not drag["moved"]:
                drag["moved"] = True
                # A drag is not half of a double click. Without this, letting
                # go and clicking again to nudge the value opened the text
                # field instead -- the same rule the layer list already keeps.
                self._last_click = (None, 0.0)
            if row.get("slider"):
                value = self._at_x(row, drag["rect"], event.mouse_x)
            else:
                # A hundred steps across the sweep, whatever the property's
                # range is. Mapping the range itself onto the widget would make
                # `jitter_absolute` -- a million pixels wide -- untouchable.
                reach = float(row.get("step", 1.0)) * 100
                travel = (event.mouse_x - drag["start_x"]) / self.px(self.DRAG_RANGE)
                if event.shift:
                    travel *= 0.1
                value = self._clamp(row, drag["start_value"] + travel * reach)
            self._write(drag["owner"], drag["key"], value)
            return True

        if event.is_left_release:
            self._drag = None
            self.root.release(self)
            self.request_redraw()
            return True

        return True

    def _type_number(self, row, rect):
        self._edit = (row.get("owner"), row.get("key"), row)
        self.field.open_at(
            rect[0], rect[1], rect[2], rect[3],
            text=self.format_value(row).rstrip("%").replace(" px", ""),
            over=self._overlay_root(),
        )

    def _on_typed(self, text):
        if self._edit is None:
            return
        owner, key, row = self._edit
        self._edit = None

        # A path is already what it means. Running it through `_parse` would
        # read "//textures/" as "not a number" and drop the edit on the floor.
        if row.get("kind") in ('path', 'text'):
            self._write(owner, key, text)
            return

        value = self._parse(row, text)
        if value is not None:
            self._write(owner, key, self._clamp(row, value))

    # --- curve ------------------------------------------------------------

    def _press_curve(self, row, rect, event):
        index = self._curve_hit(row, rect, event.mouse_x, event.mouse_y)
        x_value, y_value = self._curve_coords(rect, event.mouse_x, event.mouse_y)

        # Every curve action names the curve it is about. There is more than
        # one curve on a brush -- falloff, and one per pressure input -- and
        # an action that did not say which would edit whichever the owner
        # happened to reach for.
        key = row.get("key") or ""

        if index is None:
            if event.ctrl:
                self._emit_action('curve_add', key, x_value, y_value)
            return True

        if event.ctrl:
            self._emit_action('curve_remove', key, index)
            return True

        self._curve_drag = {"index": index, "rect": rect, "key": key}
        self._emit_action('curve_select', key, index)
        self.root.capture(self)
        return True

    def _curve_event(self, event):
        drag = self._curve_drag

        if event.is_move:
            x_value, y_value = self._curve_coords(
                drag["rect"], event.mouse_x, event.mouse_y
            )
            self._emit_action(
                'curve_move', drag.get("key", ""), drag["index"], x_value, y_value
            )
            return True

        if event.is_left_release:
            self._curve_drag = None
            self.root.release(self)
            self.request_redraw()
            return True

        return True

    # --- plumbing ---------------------------------------------------------

    def _overlay_root(self):
        """The overlay these popups open *inside* -- our popover, if any."""
        node = self.parent
        while node is not None:
            if getattr(node, "is_overlay", False):
                return node
            node = node.parent
        return None

    def _write(self, owner, key, value):
        if self.on_change is not None and key:
            self.on_change(owner, key, value)
        self.request_redraw()

    def _emit_action(self, action, *args):
        if self.on_action is not None:
            self.on_action(action, *args)
        self.request_redraw()

    def _row_key_at(self, mouse_x, mouse_y):
        for row, rect, _depth in self.placed():
            if _inside(rect, mouse_x, mouse_y):
                return _row_id(row) or row.get("label") or row.get("kind")
        return None

    def tooltip_at(self, mouse_x, mouse_y):
        for row, rect, _depth in self.placed():
            if not _inside(rect, mouse_x, mouse_y):
                continue
            if row.get("kind") == 'idblock':
                _field, button_rects, unlink_rect = self._idblock_rects(row, rect)
                if unlink_rect and _inside(unlink_rect, mouse_x, mouse_y):
                    return "Unlink data-block"
                for spec, spot in zip(row.get("buttons") or (), button_rects):
                    if _inside(spot, mouse_x, mouse_y):
                        return spec.get("desc", "")
                return row.get("desc", "")

            _label_rect, value_rect = self._columns(rect)
            _field, suffix_rects = self._suffix_rects(row, value_rect)
            for spec, spot in zip(row.get("suffix") or (), suffix_rects):
                if _inside(spot, mouse_x, mouse_y):
                    return spec.get("desc", "")
            return row.get("desc", "")
        return ""


class Popover(Widget):
    """A header button that drops a `PropertyPanel` under itself.

    Blender's popovers, drawn by us: the button shows the panel's name, the
    panel opens below it, and clicking anywhere else puts it away. It runs as
    a root overlay so the panel is drawn over the viewport rather than clipped
    by the strip it lives in -- and because it is an overlay, the dropdowns
    and number fields inside it stack on top of it rather than replacing it.
    """

    GAP = 2
    is_overlay = True

    def __init__(self, text="", name=""):
        super().__init__(name)
        self.text = text
        self.open = False
        self.panel = PropertyPanel(name=f"{name}_panel")
        self.panel.parent = self
        self.enabled = True

    # --- tree -------------------------------------------------------------

    def attach(self, root, parent=None):
        super().attach(root, parent)
        self.panel.attach(root, self)

    def children(self):
        # The panel is drawn as part of the popup, never in the layout.
        return ()

    # --- opening / closing -------------------------------------------------

    def toggle(self):
        if self.open:
            self.close()
        else:
            self.open_popup()

    def open_popup(self):
        self.open = True
        self._place()
        self.root.set_overlay(self)
        self.request_redraw()

    def close(self):
        if not self.open:
            return
        self.open = False
        if self.root is not None:
            self.root.clear_overlay(self)
        self.request_redraw()

    def _place(self):
        width = self.px(self.panel.width_hint)
        height = self.panel.content_height()

        x = self.x
        y = self.y - self.px(self.GAP) - height

        if self.root is not None:
            x = max(0, min(x, self.root.width - width))
            if y < 0:
                # No room below: hang it off the top edge of the button.
                y = min(self.root.height - height, self.y + self.height + self.px(self.GAP))
            y = max(0, y)

        self.panel.set_rect(x, y, width, height)

    # --- drawing ----------------------------------------------------------

    def draw(self):
        if not self.enabled:
            background = theme.BG_PANEL
        elif self.open:
            background = theme.WIDGET_ACTIVE
        elif self.hovered:
            background = theme.WIDGET_HOVER
        else:
            background = theme.WIDGET

        draw.widget(
            self.x, self.y, self.width, self.height, self.radius(), background,
            outline=theme.OUTLINE,
            shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        if not self.enabled:
            colour = theme.WIDGET_TEXT_DIM
        elif self.open:
            colour = theme.WIDGET_TEXT_SEL
        else:
            colour = theme.WIDGET_TEXT

        pad = self.px(theme.PAD) // 2
        size = self.px(theme.FONT_SIZE)
        mark = self.px(9)
        _, text_height = draw.text_dimensions("X", size)

        draw.text_elided(
            self.x + pad, self.y + (self.height - text_height) * 0.5, self.text,
            size, colour, self.width - pad - mark,
        )
        icons.chevron_down(
            self.x + self.width - pad - mark, self.y + (self.height - mark) * 0.5,
            mark, colour,
        )

    def draw_popup(self):
        if not self.open:
            return

        # Re-place every frame: the rows come from RNA and the panel grows and
        # shrinks as they appear -- picking Airbrush swaps Spacing for Rate.
        self._place()

        x, y, width, height = self.panel.x, self.panel.y, self.panel.width, self.panel.height
        radius = theme.radius(height, self.ui_scale, theme.PANEL_ROUNDNESS)

        draw.shadow(x, y, width, height, radius, theme.SHADOW_FAC,
                    self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(x, y, width, height, radius, theme.MENU_BACK)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.MENU_OUTLINE)

        with draw.clip(x, y, width, height):
            self.panel.draw()

    # --- events -----------------------------------------------------------

    def handle(self, event):
        if not self.enabled:
            return False
        if event.is_left_press and self.contains(event.mouse_x, event.mouse_y):
            self.toggle()
            return True
        return False

    def handle_popup(self, event):
        if not self.open:
            return False

        if self.panel.handle(event):
            return True

        inside = _inside(
            (self.panel.x, self.panel.y, self.panel.width, self.panel.height),
            event.mouse_x, event.mouse_y,
        )

        if event.is_left_press and not inside:
            # Clicking the button again is what closes it; letting the press
            # through as well would reopen it in the same breath.
            self.close()
            return self.contains(event.mouse_x, event.mouse_y)

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        # Everything over the panel belongs to the panel, even when no row
        # took it -- otherwise a click on its background reaches the viewport.
        return inside


def _row_id(row):
    """What tells one row from another: the owner as well as the key.

    A key alone was enough while every panel was its own popover. It stopped
    being enough when Texture and Texture Mask became two sections of one
    panel -- both carry `angle`, both carry `offset[0]`, and a drag that
    matched on the key alone lit up two rows and could not say which it had
    hold of.
    """
    key = row.get("key")
    return None if not key else (row.get("owner"), key)


def _inside(rect, mouse_x, mouse_y):
    x, y, width, height = rect
    return x <= mouse_x < x + width and y <= mouse_y < y + height


def _label_of(items, value):
    for identifier, label in items or ():
        if identifier == value:
            return label
    return "" if value is None else str(value)

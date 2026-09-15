# SPDX-License-Identifier: GPL-3.0-or-later
"""The starter widget set.

Deliberately minimal -- just enough to prove the toolkit works end to end
(layout, hit-testing, hover, click, redraw). Sliders, text fields, scrollable
lists and drag-reorderable layer rows come with the features that need them.
"""

import time

from . import draw
from . import icons
from . import theme
from .widget import Container, Widget


def _color(value, name):
    """Resolve a colour at draw time rather than at construction time.

    `refresh()` rebinds the palette when the user switches Blender theme, and
    a widget that grabbed its colour in `__init__` would keep drawing in the
    old one until the window was reopened. Passing the theme attribute's
    *name* instead of its value is what keeps that from happening.
    """
    if value is None:
        return getattr(theme, name)
    if isinstance(value, str):
        return getattr(theme, value)
    return value


def _segment_corners(index, count):
    """Which corners a segment of a joined strip rounds."""
    if count <= 1:
        return draw.CORNER_ALL
    if index == 0:
        return draw.CORNER_LEFT
    if index == count - 1:
        return draw.CORNER_RIGHT
    return draw.CORNER_NONE


class Label(Widget):
    """Non-interactive text."""

    ALIGN_LEFT = 'LEFT'
    ALIGN_CENTER = 'CENTER'
    ALIGN_RIGHT = 'RIGHT'

    def __init__(self, text="", size=None, color=None, align=ALIGN_LEFT, name=""):
        super().__init__(name)
        self.text = text
        self.size = size
        self.color = color
        self.align = align

    def draw(self):
        size = self.px(self.size if self.size is not None else theme.FONT_SIZE)
        colour = _color(self.color, "WIDGET_TEXT")
        cy = self.y + self.height * 0.5

        if self.align == self.ALIGN_CENTER:
            draw.text_centered(
                self.x + self.width * 0.5, cy, self.text, size, colour
            )
            return

        text_width, text_height = draw.text_dimensions(self.text, size)
        x = self.x
        if self.align == self.ALIGN_RIGHT:
            x = self.x + self.width - min(text_width, self.width)

        draw.text_elided(
            x, cy - text_height * 0.5, self.text, size, colour, self.width
        )


class Box(Widget):
    """A flat filled rectangle. Used for panel backgrounds and separators."""

    def __init__(self, color, name=""):
        super().__init__(name)
        self.color = color

    def draw(self):
        draw.rect(
            self.x, self.y, self.width, self.height,
            _color(self.color, "BG_PANEL"),
        )


class Button(Widget):
    """A clickable rounded rectangle with a centered label."""

    def __init__(self, text="", on_click=None, color=None, name=""):
        super().__init__(name)
        self.text = text
        self.on_click = on_click
        self.color = color
        self.pressed = False
        self.enabled = True

    def _background(self):
        if not self.enabled:
            return theme.BG_PANEL
        if self.pressed:
            return theme.hover(theme.WIDGET_ACTIVE)
        if self.hovered:
            return theme.WIDGET_HOVER
        return _color(self.color, "WIDGET")

    def draw(self):
        draw.widget(
            self.x,
            self.y,
            self.width,
            self.height,
            self.radius(),
            self._background(),
            outline=theme.OUTLINE,
            shade_top=theme.SHADE_TOP,
            shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        if not self.enabled:
            color = theme.WIDGET_TEXT_DIM
        elif self.pressed:
            color = theme.WIDGET_TEXT_SEL
        else:
            color = theme.WIDGET_TEXT
        draw.text_centered(
            self.x + self.width * 0.5,
            self.y + self.height * 0.5,
            self.text,
            self.px(theme.FONT_SIZE),
            color,
        )

    def handle(self, event):
        if not self.enabled:
            return False

        inside = self.contains(event.mouse_x, event.mouse_y)

        if event.is_left_press and inside:
            self.pressed = True
            self.root.capture(self)
            self.request_redraw()
            return True

        if self.pressed and event.is_left_release:
            self.pressed = False
            self.root.release(self)
            self.request_redraw()
            if inside and self.on_click is not None:
                self.on_click(self)
            return True

        return False


class ToggleButton(Widget):
    """A button that stays down. Blender's X / Y / Z symmetry buttons.

    `corners` lets a row of these be joined into one strip: only the outer
    edges are rounded, which is how Blender draws a set of related toggles.
    """

    def __init__(self, text="", on_toggle=None, tooltip="", icon=None, name=""):
        super().__init__(name)
        self.text = text
        self.icon = icon
        self.on_toggle = on_toggle
        self.tooltip = tooltip
        self.active = False
        self.enabled = True
        self.corners = draw.CORNER_ALL

    def _background(self):
        # Hovering lifts whatever the button already is. It must never replace
        # it: an "on" toggle that turns grey under the pointer reads as off,
        # so clicking it looks like nothing happened until the pointer leaves.
        if not self.enabled:
            return theme.BG_PANEL
        if self.active:
            return theme.hover(theme.WIDGET_ACTIVE) if self.hovered else theme.WIDGET_ACTIVE
        return theme.WIDGET_HOVER if self.hovered else theme.WIDGET

    def draw(self):
        draw.widget(
            self.x, self.y, self.width, self.height, self.radius(),
            self._background(),
            outline=theme.OUTLINE,
            corners=self.corners,
            shade_top=theme.SHADE_TOP,
            shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        if not self.enabled:
            color = theme.WIDGET_TEXT_DIM
        elif self.active:
            color = theme.WIDGET_TEXT_SEL
        else:
            color = theme.WIDGET_TEXT

        if self.icon is not None:
            size = int(min(self.width, self.height) * 0.62)
            self.icon(
                self.x + (self.width - size) * 0.5,
                self.y + (self.height - size) * 0.5,
                size,
                color,
            )
            return

        draw.text_centered(
            self.x + self.width * 0.5,
            self.y + self.height * 0.5,
            self.text,
            self.px(theme.FONT_SIZE),
            color,
        )

    def handle(self, event):
        if not self.enabled:
            return False
        if not (event.is_left_press and self.contains(event.mouse_x, event.mouse_y)):
            return False

        self.active = not self.active
        self.request_redraw()
        if self.on_toggle is not None:
            self.on_toggle(self.active)
        return True


class Panel(Container):
    """A titled, filled region that stacks its children vertically."""

    def __init__(self, title="", background=None, name=""):
        super().__init__(name)
        self.title = title
        self.background = background
        self.padding = theme.PAD
        self.gap = theme.GAP
        # Per-child heights in unscaled pixels; None means "fill remaining".
        self.child_heights = []

    def add(self, widget, height=None):
        self.child_heights.append(height)
        return super().add(widget)

    def clear(self):
        self.child_heights.clear()
        super().clear()

    def _title_height(self):
        return theme.FONT_SIZE_TITLE + theme.PAD if self.title else 0

    def on_resize(self):
        pad = self.px(self.padding)
        gap = self.px(self.gap)
        title_h = self.px(self._title_height())

        children = self.children()
        if not children:
            return

        inner_x = self.x + pad
        inner_width = max(0, self.width - pad * 2)
        top = self.y + self.height - title_h - pad
        available = max(0, self.height - title_h - pad * 2)

        fixed = 0
        flexible = 0
        for height in self.child_heights:
            if height is None:
                flexible += 1
            else:
                fixed += self.px(height)

        total_gap = gap * max(0, len(children) - 1)
        leftover = max(0, available - fixed - total_gap)
        flex_height = leftover // flexible if flexible else 0

        cursor = top
        for child, height in zip(children, self.child_heights):
            child_h = flex_height if height is None else self.px(height)
            cursor -= child_h
            child.set_rect(inner_x, cursor, inner_width, child_h)
            cursor -= gap

    def draw(self):
        draw.rect(
            self.x, self.y, self.width, self.height,
            _color(self.background, "BG_PANEL"),
        )

        if self.title:
            pad = self.px(self.padding)
            size = self.px(theme.FONT_SIZE_TITLE)
            _, text_height = draw.text_dimensions(self.title, size)
            draw.text_elided(
                self.x + pad,
                self.y + self.height - pad - text_height,
                self.title,
                size,
                theme.WIDGET_TEXT,
                self.width - pad * 2,
            )

        with draw.clip(self.x, self.y, self.width, self.height):
            super().draw()


class IconButton(Widget):
    """A square button whose face is a vector icon rather than text.

    `icon` is any callable with the signature `(x, y, size, color)` -- see
    gui/icons.py.
    """

    def __init__(self, icon, on_click=None, tooltip="", name=""):
        super().__init__(name)
        self.icon = icon
        self.on_click = on_click
        self.tooltip = tooltip
        self.pressed = False
        self.enabled = True
        self.active = False

    def _background(self):
        if not self.enabled:
            return theme.BG_PANEL
        if self.pressed:
            return theme.hover(theme.WIDGET_ACTIVE)
        if self.active:
            return theme.hover(theme.ACCENT) if self.hovered else theme.ACCENT
        if self.hovered:
            return theme.WIDGET_HOVER
        return theme.WIDGET

    def draw(self):
        draw.widget(
            self.x,
            self.y,
            self.width,
            self.height,
            self.radius(),
            self._background(),
            outline=theme.OUTLINE,
            shade_top=theme.SHADE_TOP,
            shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        size = int(min(self.width, self.height) * 0.62)
        x = self.x + (self.width - size) * 0.5
        y = self.y + (self.height - size) * 0.5

        if not self.enabled:
            color = theme.WIDGET_TEXT_DIM
        elif self.pressed or self.active:
            color = theme.WIDGET_TEXT_SEL
        else:
            color = theme.WIDGET_TEXT
        self.icon(x, y, size, color)

    def handle(self, event):
        if not self.enabled:
            return False

        inside = self.contains(event.mouse_x, event.mouse_y)

        if event.is_left_press and inside:
            self.pressed = True
            self.root.capture(self)
            self.request_redraw()
            return True

        if self.pressed and event.is_left_release:
            self.pressed = False
            self.root.release(self)
            self.request_redraw()
            if inside and self.on_click is not None:
                self.on_click(self)
            return True

        return False


class Slider(Widget):
    """A horizontal value bar you drag.

    `on_change` fires while dragging so the viewport updates live; the widget
    keeps its own value so the display stays smooth even if the owner rounds
    or clamps what it receives.
    """

    def __init__(self, label="", value=1.0, on_change=None, name=""):
        super().__init__(name)
        self.label = label
        self.value = value
        self.on_change = on_change
        self.dragging = False
        self.enabled = True
        # How to write the value out. The bar always runs 0..1 -- that is what
        # dragging a track means -- but what it *stands for* is the owner's
        # business: 0.35 is "35%" for opacity and "180 px" for a brush.
        self.display = None

    def _value_at(self, mouse_x):
        if self.width <= 0:
            return self.value
        ratio = (mouse_x - self.x) / self.width
        return min(1.0, max(0.0, ratio))

    def draw(self):
        radius = self.radius()
        background = theme.WIDGET_HOVER if (self.hovered or self.dragging) else theme.WIDGET
        draw.widget(
            self.x, self.y, self.width, self.height, radius, background,
            shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        # The filled part keeps the bar's own left corners and stops square,
        # the way Blender fills a number slider -- a rounded right edge would
        # read as a separate pill sitting inside the track.
        filled = int(self.width * min(1.0, max(0.0, self.value)))
        if filled > 0:
            colour = theme.ACCENT if self.enabled else theme.WIDGET_HOVER
            corners = draw.CORNER_ALL if filled >= self.width else draw.CORNER_LEFT
            draw.rounded_rect(
                self.x, self.y, filled, self.height, radius, colour, corners=corners
            )

        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.OUTLINE
        )

        size = self.px(theme.FONT_SIZE)
        if self.display is not None:
            value_text = self.display(self.value)
        else:
            value_text = f"{self.value * 100:.0f}%"

        colour = theme.WIDGET_TEXT
        cy = self.y + self.height * 0.5

        # Blender's own layout for a labelled slider: the name against the left
        # edge, the value against the right, both inside the track. Centring
        # the two together as one string -- which this used to do -- makes the
        # value wander as the label changes length, and it is the single thing
        # that read least like Blender in the brush strip.
        if not self.label:
            draw.text_centered(
                self.x + self.width * 0.5, cy, value_text, size, colour
            )
            return

        pad = self.px(theme.PAD)
        _, text_height = draw.text_dimensions("X", size)
        value_width, _ = draw.text_dimensions(value_text, size)
        baseline = cy - text_height * 0.5

        draw.text_elided(
            self.x + pad, baseline, self.label, size, colour,
            max(0, self.width - pad * 2 - value_width - self.px(theme.GAP)),
        )
        draw.text(
            self.x + self.width - pad - value_width, baseline, value_text,
            size, colour,
        )

    def handle(self, event):
        if not self.enabled:
            return False

        if event.is_left_press and self.contains(event.mouse_x, event.mouse_y):
            self.dragging = True
            self.root.capture(self)
            self._apply(self._value_at(event.mouse_x))
            return True

        if self.dragging:
            if event.is_move:
                self._apply(self._value_at(event.mouse_x))
                return True
            if event.is_left_release:
                self.dragging = False
                self.root.release(self)
                self.request_redraw()
                return True

        return False

    def _apply(self, value):
        if value == self.value:
            return
        self.value = value
        if self.on_change is not None:
            self.on_change(value)
        self.request_redraw()


class Dropdown(Widget):
    """A closed value that opens a list of options over everything else.

    The open list is drawn and hit-tested by the root as an overlay, so it can
    spill outside this widget's own rectangle without being clipped by the
    panel it lives in.
    """

    ROW_HEIGHT = 20

    def __init__(self, items=(), value=None, on_change=None, prefix="", name=""):
        super().__init__(name)
        self.items = list(items)      # [(identifier, label), ...]
        self.value = value
        self.on_change = on_change
        self.prefix = prefix
        self.open = False
        self.highlighted = -1
        # The open list may be wider than the closed one. A dropdown squeezed
        # into a layer row is too narrow to read "Linear Light" in.
        self.popup_width = None

    def label_for(self, value):
        for identifier, label in self.items:
            if identifier == value:
                return label
        return str(value)

    # --- closed state -----------------------------------------------------

    def draw(self):
        background = theme.WIDGET_HOVER if (self.hovered or self.open) else theme.WIDGET
        draw.widget(
            self.x, self.y, self.width, self.height, self.radius(), background,
            outline=theme.OUTLINE,
            shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
            emboss=theme.EMBOSS,
        )

        pad = self.px(theme.PAD)
        size = self.px(theme.FONT_SIZE)
        _, text_height = draw.text_dimensions("X", size)

        caption = self.label_for(self.value)
        if self.prefix:
            caption = f"{self.prefix}:  {caption}"

        draw.text_elided(
            self.x + pad,
            self.y + (self.height - text_height) * 0.5,
            caption,
            size,
            theme.WIDGET_TEXT,
            self.width - pad * 3,
        )

        # The same solid triangle Blender puts on anything that opens a list.
        mark = self.px(10)
        icons.chevron_down(
            self.x + self.width - pad * 0.5 - mark,
            self.y + (self.height - mark) * 0.5,
            mark,
            theme.WIDGET_TEXT_DIM,
        )

    def handle(self, event):
        if not self.enabled:
            return False

        if event.is_left_press and self.contains(event.mouse_x, event.mouse_y):
            self.open = not self.open
            if self.open:
                self.root.set_overlay(self)
            else:
                self.root.clear_overlay()
            self.request_redraw()
            return True
        return False

    # --- open state (driven by the root) ----------------------------------

    def popup_rect(self):
        """Where the open list goes: below the widget, flipped up if needed."""
        row = self.px(self.ROW_HEIGHT)
        height = row * len(self.items)
        width = self.px(self.popup_width) if self.popup_width else self.width

        x = self.x
        y = self.y - height

        if self.root is not None:
            if y < 0:
                y = self.y + self.height  # not enough room below, open upward
            # Nineteen blend modes make a tall list; keep it on screen rather
            # than letting half of it fall off the bottom edge.
            y = max(0, min(y, self.root.height - height))
            x = max(0, min(x, self.root.width - width))

        return x, y, width, height

    def draw_popup(self):
        x, y, width, height = self.popup_rect()
        row = self.px(self.ROW_HEIGHT)
        radius = theme.radius(height, self.ui_scale, theme.PANEL_ROUNDNESS)

        draw.shadow(x, y, width, height, radius, theme.SHADOW_FAC,
                    self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(x, y, width, height, radius, theme.MENU_BACK)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.MENU_OUTLINE)

        size = self.px(theme.FONT_SIZE_SMALL)
        pad = self.px(theme.PAD)
        inset = self.px(2)
        row_radius = theme.radius(row, self.ui_scale)

        for index, (identifier, label) in enumerate(self.items):
            row_y = y + height - (index + 1) * row

            if index == self.highlighted:
                draw.rounded_rect(x + inset, row_y, width - inset * 2, row,
                                  row_radius, theme.MENU_SEL)
            elif identifier == self.value:
                draw.rounded_rect(x + inset, row_y, width - inset * 2, row,
                                  row_radius, theme.LIST_HOVER)

            _, text_height = draw.text_dimensions(label, size)
            draw.text_elided(
                x + pad,
                row_y + (row - text_height) * 0.5,
                label,
                size,
                theme.MENU_TEXT,
                width - pad * 2,
            )

    def popup_index_at(self, mouse_x, mouse_y):
        x, y, width, height = self.popup_rect()
        if not (x <= mouse_x < x + width and y <= mouse_y < y + height):
            return -1

        row = self.px(self.ROW_HEIGHT)
        index = int((y + height - mouse_y) // row)
        return index if 0 <= index < len(self.items) else -1

    def handle_popup(self, event):
        """Return True when the event was consumed by the open list."""
        index = self.popup_index_at(event.mouse_x, event.mouse_y)

        if event.is_move:
            if index != self.highlighted:
                self.highlighted = index
                self.request_redraw()
            return True

        if event.is_left_press:
            if index >= 0:
                identifier = self.items[index][0]
                if identifier != self.value:
                    self.value = identifier
                    if self.on_change is not None:
                        self.on_change(identifier)
            self.close()
            return True

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        return False

    def close(self):
        self.open = False
        self.highlighted = -1
        if self.root is not None:
            self.root.clear_overlay()
        self.request_redraw()


class Menu(Widget):
    """A short list of actions, popped up at a point. Runs as a root overlay.

    Not a Dropdown: there is no current value and no closed state to draw --
    a menu is opened by something else (a button, a key press), does one thing
    and goes away. Items are supplied per opening, so the same instance can
    offer "Delete Layer / Delete Mask" one moment and mask options the next.
    """

    ROW_HEIGHT = 22
    MIN_WIDTH = 130
    PADDING = 8
    GAP = 4

    # A swatch column appears only when an item asks for one, so an ordinary
    # menu is laid out exactly as it was before colours existed.
    SWATCH = 10

    def __init__(self, name="menu"):
        super().__init__(name)
        self.visible = False
        # [(identifier, label)] or [(identifier, label, colour_or_None)]
        self.items = []
        self.title = ""
        self.on_choose = None
        self.highlighted = -1

    # --- opening / closing -------------------------------------------------

    def open_at(self, x, y, items, on_choose, title="", above=False, over=None):
        """Open at (x, y), clamped into the region.

        `y` is the menu's top edge by default, so it hangs below whatever was
        clicked. With `above`, `y` is its bottom edge instead -- which is what
        a button sitting at the bottom of a panel needs.

        `over` is the overlay this menu opens *inside*, if any: an enum picked
        in a popover must leave the popover standing.
        """
        self.items = list(items)
        self.title = title
        self.on_choose = on_choose
        self.highlighted = -1
        if not self.items:
            return False

        width = self._measure_width()
        height = self._measure_height()

        bottom = int(y) if above else int(y) - height

        x = max(0, min(int(x), self.root.width - width))
        y = max(0, min(bottom, self.root.height - height))

        self.set_rect(x, y, width, height)
        self.visible = True
        self.root.set_overlay(self, over=over)
        self.request_redraw()
        return True

    def close(self):
        self.visible = False
        self.highlighted = -1
        if self.root is not None:
            self.root.clear_overlay()
        self.request_redraw()

    def _swatch_width(self):
        """The colour column's width, or 0 when no item carries a colour."""
        if not any(len(item) > 2 for item in self.items):
            return 0
        return self.px(self.SWATCH) + self.px(self.GAP)

    def _choose(self, index):
        identifier = self.items[index][0]
        callback = self.on_choose
        self.close()
        if callback is not None:
            callback(identifier)

    # --- geometry ----------------------------------------------------------

    def _measure_width(self):
        size = self.px(theme.FONT_SIZE)
        pad = self.px(self.PADDING)

        widest = self.px(self.MIN_WIDTH)
        swatch = self._swatch_width()
        for item in self.items:
            width, _ = draw.text_dimensions(item[1], size)
            widest = max(widest, int(width) + pad * 2 + swatch)
        if self.title:
            width, _ = draw.text_dimensions(self.title, self.px(theme.FONT_SIZE_SMALL))
            widest = max(widest, int(width) + pad * 2)
        return widest

    def _title_height(self):
        return self.px(self.ROW_HEIGHT) if self.title else 0

    def _measure_height(self):
        return (
            self.px(self.ROW_HEIGHT) * len(self.items)
            + self._title_height()
            + self.px(self.GAP) * 2
        )

    def _row_rect(self, index):
        row = self.px(self.ROW_HEIGHT)
        top = self.y + self.height - self.px(self.GAP) - self._title_height()
        return self.x, top - (index + 1) * row, self.width, row

    def index_at(self, mouse_x, mouse_y):
        if not self.contains(mouse_x, mouse_y):
            return -1
        for index in range(len(self.items)):
            x, y, width, height = self._row_rect(index)
            if x <= mouse_x < x + width and y <= mouse_y < y + height:
                return index
        return -1

    # --- drawing -----------------------------------------------------------

    def draw_popup(self):
        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)

        draw.shadow(self.x, self.y, self.width, self.height, radius,
                    theme.SHADOW_FAC, self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(
            self.x, self.y, self.width, self.height, radius, theme.MENU_BACK
        )
        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.MENU_OUTLINE
        )

        pad = self.px(self.PADDING)

        if self.title:
            size = self.px(theme.FONT_SIZE_SMALL)
            _, text_height = draw.text_dimensions(self.title, size)
            row = self.px(self.ROW_HEIGHT)
            top = self.y + self.height - self.px(self.GAP) - row
            draw.text(
                self.x + pad,
                top + (row - text_height) // 2,
                self.title,
                size,
                theme.WIDGET_TEXT_DIM,
            )

        size = self.px(theme.FONT_SIZE)
        inset = self.px(2)
        swatch = self._swatch_width()
        mark = self.px(self.SWATCH)
        for index, item in enumerate(self.items):
            label = item[1]
            colour = item[2] if len(item) > 2 else None
            x, y, width, height = self._row_rect(index)
            if index == self.highlighted:
                draw.rounded_rect(
                    x + inset, y, width - inset * 2, height,
                    theme.radius(height, self.ui_scale), theme.MENU_SEL,
                )

            if swatch:
                # An empty slot for the item that clears the tag: the outline
                # alone says "none of them" without needing a word for it.
                box_y = y + (height - mark) // 2
                if colour is None:
                    draw.rect_outline(x + pad, box_y, mark, mark,
                                      theme.MENU_TEXT, max(1, self.px(1)))
                else:
                    draw.rect(x + pad, box_y, mark, mark, colour)

            _, text_height = draw.text_dimensions(label, size)
            draw.text_elided(
                x + pad + swatch,
                y + (height - text_height) // 2,
                label,
                size,
                theme.MENU_TEXT,
                width - pad * 2 - swatch,
            )

    # --- events ------------------------------------------------------------

    def handle_popup(self, event):
        index = self.index_at(event.mouse_x, event.mouse_y)

        if event.is_move:
            if index != self.highlighted:
                self.highlighted = index
                self.request_redraw()
            return True

        if event.is_left_press:
            if index >= 0:
                self._choose(index)
            else:
                self.close()
            return True

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        # Everything else is swallowed: a menu is modal while it is up.
        return True


class Row(Container):
    """Lays its children out left to right.

    By default the width is shared evenly. Give `item_width` to make every
    child that size instead and pack them to the left -- which is what a row
    of square icon buttons wants, so adding a fourth button later does not
    shrink the first three.
    """

    def __init__(self, gap=None, item_width=None, joined=False, name=""):
        super().__init__(name)
        # A joined row is one control made of segments -- Blender's X/Y/Z
        # symmetry buttons -- so there is no gap and only the outer corners of
        # the strip are rounded.
        self.joined = joined
        self.gap = (0 if joined else theme.GAP) if gap is None else gap
        self.item_width = item_width

    def on_resize(self):
        children = [child for child in self.children() if child.visible]
        if not children:
            return

        gap = self.px(self.gap)

        spacers = [child for child in children if isinstance(child, Spacer)]
        sized = len(children) - len(spacers)

        if self.item_width is not None:
            each = self.px(self.item_width)
        else:
            each = max(0, (self.width - gap * (len(children) - 1)) // max(1, sized))

        # Whatever the sized children and the gaps do not use is shared out
        # between the spacers, which is what puts a trailing button hard
        # against the right edge however many buttons precede it.
        slack = 0
        if spacers:
            used = each * sized + gap * max(0, len(children) - 1)
            slack = max(0, self.width - used) // len(spacers)

        x = self.x
        for index, child in enumerate(children):
            width = slack if isinstance(child, Spacer) else each
            child.set_rect(x, self.y, width, self.height)
            if self.joined and hasattr(child, "corners"):
                child.corners = _segment_corners(index, len(children))
            x += width + gap


class Spacer(Widget):
    """Nothing, with a width. Pushes what follows it to the far end of a Row.

    A Row packs its children to the left at a fixed size, which is right for a
    strip of icon buttons and wrong the moment one of them belongs at the other
    end. This is the gap that eats whatever is left over.
    """

    def draw(self):
        pass

    def handle(self, event):
        return False


class ListView(Widget):
    """A vertically scrolling list of fixed-height rows.

    Rows are deliberately *not* widgets. A `draw_row` callback paints each
    visible one, so a hundred-layer stack costs nothing to lay out and only
    the rows actually on screen are ever touched.

    The owner injects the behaviour:

        draw_row(item, index, x, y, w, h, active, hovered)
        on_activate(index, extend, span, local_x) -> row was selected;
                                                 local_x says where in it
        on_row_click(index, local_x, local_y, alt, shift)
                                                 -> True if it handled the
                                                    click itself (hit zones
                                                    like a visibility toggle),
                                                    or 'CAPTURE' to start a
                                                    horizontal value drag.
                                                    `alt` and `shift` are the
                                                    two modifiers a row
                                                    control reads, and they
                                                    are passed rather than
                                                    looked up so the row stays
                                                    a pure function of the
                                                    event
        on_row_drag(index, delta_x)              -> a captured drag moved
        on_row_double_click(index, lx, ly)       -> rename, and such
        on_reorder(from_index, gap_index, onto_index) -> a row was dropped
        on_empty_click(extend, span)             -> the space under the rows
        on_spring_open(index)                    -> a drag rested on a closed
                                                    container long enough

    Dragging is animated. Not for decoration: a list that only moves when the
    data changes gives the hand nothing to read mid-gesture, and every part of
    the animation here answers a question the pointer is asking. The rows part
    to show the slot the block will land in, the floating copy trails the
    pointer instead of being welded to it, the list scrolls itself when the
    pointer reaches an edge, and what just landed glows for a moment so the eye
    can find it again. `animate()` advances all of it; the owner drives that
    from its own tick.
    """

    # How far the pointer must move before a press turns into a drag. Below
    # this, the gesture is still a click -- otherwise selecting a row would
    # nudge the order every time the hand is not perfectly still.
    DRAG_THRESHOLD = 4

    # How much of a row's height counts as "onto" rather than "between", as a
    # fraction taken off each end. A quarter each way leaves the middle half
    # for dropping inside, which is enough to hit without being so much that
    # the edges become hard to reach.
    ONTO_BAND = 0.25

    # Blender does deliver `value='DOUBLE_CLICK'`, but only for buttons its own
    # keymap has a double-click entry for, so the list times the gap itself as
    # well. Both paths land on the same callback; firing twice is harmless.
    DOUBLE_CLICK_SECONDS = 0.4

    # And it has to be the same *place*, not merely the same row. A row can
    # hold several things worth clicking -- a layer's two chips sit six pixels
    # apart -- and reading "colour, then mask" as a double click on the colour
    # swallows the second click entirely. Every windowing system has this
    # threshold for the same reason; ours is in unscaled pixels.
    DOUBLE_CLICK_SLOP = 6

    # --- motion ------------------------------------------------------------
    # Every duration is in seconds and every one of them is short. A list that
    # takes a quarter of a second to answer is a list that feels slow, whatever
    # the animation looks like; these are all inside the window where motion
    # reads as weight rather than as delay.

    # How long the parted rows take to open and close. Long enough to be a
    # movement rather than a jump, short enough that a fast drag across ten
    # rows does not leave the list still catching up.
    GAP_SECONDS = 0.12

    # The floating copy chases the pointer rather than being pinned to it.
    # This is the fraction of the remaining distance it closes in a sixtieth
    # of a second -- high enough to keep up, low enough to lag into a curve
    # when the hand changes direction.
    GHOST_CHASE = 0.35

    # And it lifts as the drag begins instead of appearing at full size.
    LIFT_SECONDS = 0.09

    # How long the row that has just landed stays lit. This is the one piece
    # of feedback that outlives the gesture: after a drop into a group three
    # rows away, it is the only thing that says where the block went.
    SETTLE_SECONDS = 0.45

    # Auto-scroll while dragging: how close to an edge starts it, as a
    # fraction of a row, and the fastest it goes, in rows per second.
    SCROLL_MARGIN = 1.0
    SCROLL_SPEED = 9.0

    # How long a drag has to rest on a closed container before it opens.
    # Spring-loaded folders, the same gesture a file manager has: aiming into
    # a closed group otherwise means dropping it, opening it and dragging
    # again.
    SPRING_SECONDS = 0.55

    def __init__(self, row_height=26, name="list"):
        super().__init__(name)
        self.row_height = row_height
        self.rows = []
        self.active_index = -1
        self.hovered_index = -1
        # Where the pointer last was, so the owner can say which control
        # inside the hovered row it is over (rows are not widgets).
        self.hover_x = 0
        self.scroll = 0
        self.background = None
        self.empty_text = "Empty"

        self.draw_row = None
        self.on_activate = None
        self.on_row_click = None
        self.on_row_drag = None
        self.on_row_double_click = None
        self.on_reorder = None
        # (gap_index) -> how far in to draw the drop line, in pixels.
        self.drop_offset = None
        # (from_index, row_index) -> may the drag be dropped *onto* that row?
        # A list of plain rows says no to everything; one that nests says yes
        # to the rows that can hold others.
        self.drop_accepts = None
        # (from_index) -> every row travelling with the drag. A list where a
        # drag can carry a selection says so here; without it only the row
        # under the pointer moves.
        self.drag_rows = None
        # (index, local_x, local_y) -> the row was right-clicked. A list
        # without this has no place to put the commands that do not fit on the
        # row, and every one of them ends up as another button in the strip.
        self.on_row_context = None
        # (extend, span) -> the empty space under the last row was clicked.
        # Without it that click does nothing, which is not what any list does.
        self.on_empty_click = None
        # (indices, extend) -> a rubber band drawn from the empty space
        # covered these rows. The same gesture every file manager and every
        # node editor has: press on nothing, drag over things, get them all.
        # Without it a press on nothing can only ever mean "select none".
        self.on_box_select = None
        # (index) -> a drag has rested on this row long enough to ask for it
        # to open. Only ever called for a row `drop_accepts` allows.
        self.on_spring_open = None
        # (from_index, row_index) -> the row would look like a drop target but
        # is refused. Only asked about rows `drop_accepts` has already said no
        # to, and only to decide whether to say so on screen.
        self.drop_refuses = None

        # --- motion state --------------------------------------------------
        # Animated values and the targets they are chasing. All of them are in
        # pixels or in 0..1, all of them are advanced by `animate()`, and none
        # of them is ever read by the owner: this is how the list draws, not
        # what it means.
        self._anim_clock = 0.0
        # Where the parted rows currently are (`_gap_shown`) and where they
        # are heading (`_gap_index`, -1 for closed), plus how far open.
        self._gap_shown = -1
        self._gap_open = 0.0
        self._ghost_y = 0.0
        self._lift = 0.0
        # Row that has just been dropped, and how much of its glow is left.
        self._settle_index = -1
        self._settle = 0.0
        # Which row the spring-load timer is counting on, and since when.
        self._spring_index = -1
        self._spring_since = 0.0
        # Fractional scroll carried between frames, so a slow auto-scroll is
        # not rounded away to nothing every tick.
        self._scroll_carry = 0.0

        # Reorder state. `_press_index` is the row the button went down on;
        # `_drop_index` is a *gap* index in 0..len, not a row index; `_drag_y`
        # follows the pointer so the floating copy of the row can track it.
        self._press_index = -1
        self._press_x = 0
        self._press_y = 0
        self._press_double = False
        self._press_extend = False
        self._press_span = False
        self._dragging = False
        self._drop_index = -1
        # The row a drop would land *inside*, as opposed to between two rows.
        # Only one of the two is ever set.
        self._drop_onto = -1
        # The row that is being aimed inside and is saying no.
        self._drop_refused = -1
        self._drag_x = 0
        self._drag_y = 0

        # A horizontal value drag started inside a row (an opacity readout,
        # say). Separate from the reorder gesture, which is vertical.
        self._value_index = -1
        self._value_x = 0

        self._last_click_index = -1
        self._last_click_time = 0.0
        self._last_click_x = 0

        # The rubber band. `_box_from` is where the button went down, in
        # region coordinates, and None while no band is being drawn; the
        # modifiers are kept from the press because a band is a selection
        # gesture and ctrl has to mean "add to what is there" all the way
        # through it, not only at the moment the button comes up.
        self._box_from = None
        self._box_to = None
        self._box_extend = False


        # Scrollbar drag: how far down the bar the pointer grabbed it, so the
        # bar does not jump its own height on the first pixel of movement.
        self._scroll_drag = False
        self._scroll_grab = 0
        self._over_scrollbar = False

    # --- motion -----------------------------------------------------------

    def animating(self):
        """True while something still has to move. Drives the owner's tick."""
        return (
            self._dragging
            or self._settle > 0.0
            or self._lift > 0.0
            or self._gap_open > 0.0
            or self._gap_shown >= 0
        )

    def animate(self):
        """Advance every animated value. True when the list must be redrawn.

        Called from the owner's tick as well as from events, because the two
        things that most need to keep running -- the edge scroll and the
        spring-loaded open -- happen while the pointer is perfectly still.
        """
        now = time.monotonic()
        previous, self._anim_clock = self._anim_clock, now
        if not previous:
            return False

        # A stalled frame must not teleport anything: a tick that arrives late
        # is capped rather than played back at full speed.
        elapsed = min(max(now - previous, 0.0), 0.1)
        if elapsed <= 0.0:
            return False

        moved = self._animate_gap(elapsed)
        moved = self._animate_ghost(elapsed) or moved
        moved = self._animate_settle(elapsed) or moved
        moved = self._auto_scroll(elapsed) or moved
        moved = self._spring(now) or moved
        return moved

    def flash_active(self):
        """Light the active row for a moment. What a finished drop leaves behind.

        Deliberately keyed to the active row rather than to a row index taken
        at drop time: the stack is re-synced between the drop and the next
        frame, and by then the number the block was dropped at means something
        else. The active row is the one that moved, whatever it is now called.
        """
        self._settle = 1.0

    def _animate_gap(self, elapsed):
        """Part the rows where the block would land, and close them again.

        A slot that has to move somewhere else closes first and reopens in the
        new place. Sliding it sideways across four rows looks like the block is
        being dragged through them, which is the opposite of what it means.
        """
        target = self._drop_index if (self._dragging and self._drop_onto < 0) else -1
        wanted = 1.0 if (target >= 0 and target == self._gap_shown) else 0.0

        step = elapsed / self.GAP_SECONDS
        if self._gap_open < wanted:
            self._gap_open = min(wanted, self._gap_open + step)
        elif self._gap_open > wanted:
            self._gap_open = max(wanted, self._gap_open - step)
        elif self._gap_shown == target:
            return False

        if self._gap_open <= 0.0 and self._gap_shown != target:
            self._gap_shown = target
        return True

    def _animate_ghost(self, elapsed):
        """Let the floating copy trail the pointer, and lift it as it starts."""
        if not self._dragging:
            if self._lift <= 0.0:
                return False
            self._lift = max(0.0, self._lift - elapsed / self.LIFT_SECONDS)
            return True

        moved = False
        if self._lift < 1.0:
            self._lift = min(1.0, self._lift + elapsed / self.LIFT_SECONDS)
            moved = True

        # An exponential chase, made independent of how often this runs: the
        # constant is the share of the distance closed in a sixtieth of a
        # second, so a slow tick and a fast one land in the same place.
        keep = (1.0 - self.GHOST_CHASE) ** (elapsed * 60.0)
        if abs(self._drag_y - self._ghost_y) > 0.5:
            self._ghost_y += (self._drag_y - self._ghost_y) * (1.0 - keep)
            moved = True
        else:
            self._ghost_y = float(self._drag_y)
        return moved

    def _animate_settle(self, elapsed):
        if self._settle <= 0.0:
            return False
        self._settle = max(0.0, self._settle - elapsed / self.SETTLE_SECONDS)
        return True

    def _auto_scroll(self, elapsed):
        """Scroll the list when a drag reaches an edge of it.

        Without this a stack taller than the panel can only be reordered
        within the part of it that happens to be on screen -- the pointer
        cannot reach the rest without letting go.
        """
        if not self._dragging or self.max_scroll() <= 0:
            self._scroll_carry = 0.0
            return False

        pitch = self._pitch()
        margin = max(1.0, pitch * self.SCROLL_MARGIN)
        top = self.y + self.height

        if self._drag_y > top - margin:
            reach = (self._drag_y - (top - margin)) / margin
            direction = -1.0            # towards the top of the stack
        elif self._drag_y < self.y + margin:
            reach = ((self.y + margin) - self._drag_y) / margin
            direction = 1.0
        else:
            self._scroll_carry = 0.0
            return False

        # Carried between frames, so a gentle scroll near the edge is not
        # rounded down to zero pixels every tick and lost.
        reach = min(1.0, max(0.0, reach))
        self._scroll_carry += direction * reach * self.SCROLL_SPEED * pitch * elapsed
        step = int(self._scroll_carry)
        if step == 0:
            return False
        self._scroll_carry -= step

        before = self.scroll
        self.scroll += step
        self._clamp_scroll()
        if self.scroll == before:
            return False

        # The content moved under a pointer that did not: where the drop lands
        # has changed, and nothing else is going to recompute it.
        self._update_drop()
        return True

    def _spring(self, now):
        """Open a closed container the drag has been resting on.

        Spring-loaded folders. Without it, putting a layer into a closed group
        means dropping it somewhere else, opening the group and dragging again.
        """
        index = self._drop_onto if self._dragging else -1
        if index != self._spring_index:
            self._spring_index = index
            self._spring_since = now
            return False

        if index < 0 or self.on_spring_open is None:
            return False
        if now - self._spring_since < self.SPRING_SECONDS:
            return False

        # Once per rest. The clock is pushed out of reach rather than reset, so
        # the same row cannot fire twice without the pointer leaving it.
        self._spring_since = float("inf")
        self.on_spring_open(index)
        return True

    # --- geometry ---------------------------------------------------------

    def _pitch(self):
        return max(1, self.px(self.row_height))

    def _row_shift(self, index, pitch):
        """How far row `index` is pushed down to open the slot, in pixels.

        Only the drawing knows about this. Hit-testing deliberately runs on
        the unmoved layout: rows that shift out from under the pointer would
        change which gap it is over, which would move them back, and the list
        would sit there flickering between two answers.
        """
        if self._gap_shown < 0 or self._gap_open <= 0.0 or index < self._gap_shown:
            return 0
        return int(pitch * self._gap_open)

    def content_height(self):
        return len(self.rows) * self._pitch()

    def max_scroll(self):
        return max(0, self.content_height() - self.height)

    def _clamp_scroll(self):
        self.scroll = max(0, min(self.scroll, self.max_scroll()))

    def row_top(self, index):
        """Top edge of row `index`, in region coordinates (y grows upward)."""
        return self.y + self.height + self.scroll - index * self._pitch()

    def scroll_into_view(self, index):
        """Bring row `index` fully on screen. True when the list had to move.

        Needed by the keyboard: stepping the active row down a fifty-layer
        stack with the arrow keys otherwise walks it straight off the bottom
        and the list sits still while the selection moves out of sight.
        """
        if not (0 <= index < len(self.rows)):
            return False

        pitch = self._pitch()
        before = self.scroll
        top = index * pitch
        if top < self.scroll:
            self.scroll = top
        elif top + pitch > self.scroll + self.height:
            self.scroll = top + pitch - self.height

        self._clamp_scroll()
        if self.scroll != before:
            self.request_redraw()
            return True
        return False

    def index_at(self, mouse_y):
        offset = self.y + self.height + self.scroll - mouse_y
        if offset < 0:
            return -1
        index = int(offset // self._pitch())
        return index if 0 <= index < len(self.rows) else -1

    def gap_at(self, mouse_y):
        """Nearest gap between rows, as an insert position in 0..len(rows).

        Rounding rather than flooring is what makes a drop land where the
        pointer looks like it will: above the row when the pointer is in its
        upper half, below it in the lower half.
        """
        offset = self.y + self.height + self.scroll - mouse_y
        index = int(round(offset / self._pitch()))
        return max(0, min(len(self.rows), index))

    # --- drawing ----------------------------------------------------------

    def draw(self):
        # A rounded, outlined box -- what Blender draws a UIList into. The
        # rows themselves paint no background of their own, only selection and
        # hover, which is both how Blender's lists read and what keeps a
        # square row corner from poking out of a rounded box.
        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)
        draw.rounded_rect(
            self.x, self.y, self.width, self.height, radius,
            _color(self.background, "BOX_INNER"),
        )
        self._clamp_scroll()

        if not self.rows:
            draw.text_centered(
                self.x + self.width * 0.5,
                self.y + self.height * 0.5,
                self.empty_text,
                self.px(theme.FONT_SIZE),
                theme.WIDGET_TEXT_DIM,
            )
            with draw.clip(self.x, self.y, self.width, self.height):
                self._draw_box()
            draw.rounded_rect_outline(
                self.x, self.y, self.width, self.height, radius, theme.OUTLINE
            )
            return

        pitch = self._pitch()
        first = max(0, self.scroll // pitch)
        last = min(len(self.rows), first + self.height // pitch + 2)

        # One row past the end at each side: with the slot open, rows that
        # would be just off screen are pushed into view.
        last = min(len(self.rows), last + 1)

        with draw.clip(self.x, self.y, self.width, self.height):
            for index in range(first, last):
                if self.draw_row is None:
                    break
                self.draw_row(
                    self.rows[index],
                    index,
                    self.x,
                    self.row_top(index) - pitch - self._row_shift(index, pitch),
                    self.row_width(),
                    pitch,
                    index == self.active_index,
                    index == self.hovered_index,
                )

            self._draw_settle(pitch)

            if self._dragging:
                self._draw_drag_feedback(pitch)

            # Over the rows: the band is what the hand is doing right now.
            self._draw_box()

        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.OUTLINE
        )
        self._draw_scrollbar()

    def _draw_settle(self, pitch):
        """The fading mark a finished drop leaves on the row that moved."""
        if self._settle <= 0.0 or not (0 <= self.active_index < len(self.rows)):
            return

        # Eased out rather than linear: a glow that dies at a constant rate
        # looks switched off, one that trails away looks settled.
        strength = self._settle * self._settle
        top = self.row_top(self.active_index) - self._row_shift(
            self.active_index, pitch
        )
        radius = theme.radius(pitch, self.ui_scale)
        draw.rounded_rect_outline(
            self.x, top - pitch, self.width, pitch, radius,
            theme.ACCENT[:3] + (theme.ACCENT[3] * strength,),
            max(1, self.px(2)),
        )

    def _draw_drag_feedback(self, pitch):
        """Show the row travelling with the pointer, and where it will land.

        The row keeps drawing in its old slot under a light veil -- the stack
        has to stay readable mid-gesture -- and a fully drawn copy floats under
        the pointer, so what is being moved is visible the whole way across.
        """
        source = self._press_index
        if not (0 <= source < len(self.rows)) or self.draw_row is None:
            return

        travelling = (
            self.drag_rows(source) if self.drag_rows is not None else {source}
        )
        for index in sorted(travelling):
            if 0 <= index < len(self.rows):
                draw.rect(
                    self.x,
                    self.row_top(index) - pitch - self._row_shift(index, pitch),
                    self.width, pitch, theme.DRAG_VEIL,
                )

        # The floating copy. It chases the pointer rather than being welded to
        # it, which is what gives the block weight -- and it is clamped to the
        # list so it never escapes the panel.
        ghost_y = int(self._ghost_y - pitch * 0.5)
        ghost_y = max(self.y, min(ghost_y, self.y + self.height - pitch))

        # The lift is what the copy does as the gesture begins: it grows in
        # from the row's own width and its shadow opens up underneath.
        ease = self._lift * self._lift * (3.0 - 2.0 * self._lift)
        lift = max(1, int(self.px(4) * ease))
        radius = theme.radius(pitch, self.ui_scale)

        # Inset, so a drop target drawn around a full-width row is not buried
        # by the thing being dropped on it: the frame's edges stay visible
        # down both sides of the floating copy.
        ghost_x = self.x + lift
        ghost_w = max(self.px(40), self.width - lift * 2)

        draw.rounded_rect(
            ghost_x + lift, ghost_y - lift, ghost_w, pitch, radius,
            theme.DRAG_SHADOW,
        )
        self.draw_row(
            self.rows[source], source, ghost_x, ghost_y, ghost_w, pitch, True, False
        )
        draw.rounded_rect_outline(
            ghost_x, ghost_y, ghost_w, pitch, radius, theme.ACCENT, self.px(1)
        )

        # More than one row moving: say how many, or the ghost claims to be
        # carrying one layer while four of them are veiled behind it.
        if len(travelling) > 1:
            label = f"+{len(travelling) - 1}"
            size = self.px(theme.FONT_SIZE_SMALL)
            width, height = draw.text_dimensions(label, size)
            pad = self.px(4)
            box = width + pad * 2
            left = ghost_x + ghost_w - box - pad
            draw.rounded_rect(
                left, ghost_y + (pitch - height - pad) // 2, box, height + pad,
                theme.radius(height + pad, self.ui_scale), theme.ACCENT,
            )
            draw.text_centered(
                left + box * 0.5, ghost_y + pitch * 0.5, label, size,
                theme.WIDGET_TEXT_SEL,
            )

        # Last, so the ghost and its shadow can never bury it -- the whole
        # point of the line is that it is always readable.
        if self._drop_onto >= 0:
            # A box around the row, not a line between two: the drop lands
            # *in* it, and a line would say the opposite. Tinted as well as
            # outlined, because the row it marks is the one the floating copy
            # is hovering over and an outline alone can be hard to pick out.
            top = self.row_top(self._drop_onto) - self._row_shift(
                self._drop_onto, pitch
            )
            corner = theme.radius(pitch, self.ui_scale)
            draw.rounded_rect(
                self.x, top - pitch, self.width, pitch, corner,
                theme.ACCENT[:3] + (0.28,),
            )
            draw.rounded_rect_outline(
                self.x, top - pitch, self.width, pitch, corner, theme.ACCENT,
                max(2, self.px(3)),
            )

        elif self._drop_refused >= 0:
            # The same frame the accepted target gets, in the refusal colour,
            # with a bar through it. Saying no in the same language as yes is
            # what makes it read as an answer rather than as nothing happening.
            top = self.row_top(self._drop_refused) - self._row_shift(
                self._drop_refused, pitch
            )
            corner = theme.radius(pitch, self.ui_scale)
            draw.rounded_rect(
                self.x, top - pitch, self.width, pitch, corner,
                theme.DANGER[:3] + (0.20,),
            )
            draw.rounded_rect_outline(
                self.x, top - pitch, self.width, pitch, corner, theme.DANGER,
                max(2, self.px(2)),
            )
            bar = max(2, self.px(2))
            inset = self.px(8)
            draw.rect(self.x + inset, top - pitch * 0.5 - bar * 0.5,
                      self.width - inset * 2, bar, theme.DANGER)

        elif self._gap_shown >= 0 and self._gap_open > 0.0:
            thickness = max(2, self.px(2))
            # Down the middle of the slot the rows have opened, not on the
            # boundary they used to share: with the gap open, that boundary is
            # a place nothing is any more.
            line_y = (
                self.row_top(self._gap_shown)
                - pitch * self._gap_open * 0.5
                - thickness * 0.5
            )

            # Indented to where the row would actually sit, so the nesting the
            # drop lands at is visible before the button comes up.
            indent = 0
            if self.drop_offset is not None:
                indent = max(0, int(self.drop_offset(self._gap_shown)))
            indent = min(indent, max(0, self.width - thickness * 6))

            fade = theme.ACCENT[:3] + (theme.ACCENT[3] * self._gap_open,)
            draw.rect(self.x + indent, line_y, self.width - indent, thickness,
                      fade)

            # Small caps on the ends so the line reads as an insertion point.
            cap = thickness * 2
            draw.rect(self.x + indent, line_y - cap * 0.5, thickness, cap * 2,
                      fade)
            draw.rect(
                self.x + self.width - thickness,
                line_y - cap * 0.5,
                thickness,
                cap * 2,
                fade,
            )

    def gutter(self):
        """Width kept clear on the right for the scrollbar, always."""
        return self.px(theme.SCROLLBAR_WIDTH) + self.px(theme.SCROLLBAR_PAD) * 2

    def row_width(self):
        """How wide a row may draw. Not `width` -- the gutter is not a row's."""
        return max(0, self.width - self.gutter())

    def _scrollbar_rect(self):
        """The bar itself, or None when everything fits."""
        total = self.content_height()
        if total <= self.height or self.height <= 0:
            return None

        width = self.px(theme.SCROLLBAR_WIDTH)
        track_x = self.x + self.width - width - self.px(theme.SCROLLBAR_PAD)

        bar_height = max(self.px(24), int(self.height * self.height / total))
        travel = self.height - bar_height
        position = self.scroll / self.max_scroll() if self.max_scroll() else 0.0
        return (track_x, self.y + self.height - bar_height - int(travel * position),
                width, bar_height)

    def _draw_scrollbar(self):
        rect = self._scrollbar_rect()
        if rect is None:
            return

        x, y, width, height = rect
        radius = width * 0.5

        # The track is drawn too, faintly. A bar floating on the list back
        # says how far down you are but not how much is left.
        draw.rounded_rect(x, self.y, width, self.height, radius,
                          theme.OUTLINE[:3] + (theme.OUTLINE[3] * 0.5,))

        lit = self._scroll_drag or self._over_scrollbar
        draw.rounded_rect(x, y, width, height, radius,
                          theme.ACCENT if lit else theme.WIDGET_TEXT_DIM)

    def _scroll_to_bar(self, mouse_y, grab):
        """Put the bar's top under the pointer, keeping the grab offset."""
        rect = self._scrollbar_rect()
        if rect is None:
            return
        travel = self.height - rect[3]
        if travel <= 0:
            return
        top = mouse_y + grab
        position = (self.y + self.height - top) / travel
        self.scroll = int(round(self.max_scroll() * min(1.0, max(0.0, position))))
        self._clamp_scroll()
        self.request_redraw()

    # --- events -----------------------------------------------------------

    def cancel_double_click(self):
        """Forget the click a double click would have been the second half of.

        Called when something else takes over between the two -- a menu, a
        colour picker. The gesture is "click this thing twice", and a menu
        opened and dismissed in between is not part of it, however fast the
        hand was.
        """
        self._last_click_index = -1

    def _row_local(self, index, event):
        return (
            event.mouse_x - self.x,
            event.mouse_y - (self.row_top(index) - self._pitch()),
        )

    def _on_scrollbar(self, mouse_x, mouse_y):
        rect = self._scrollbar_rect()
        if rect is None:
            return False
        x, _, width, _ = rect
        # The whole track is a target, not just the bar: clicking below the
        # bar has to page down, and a 4px bar is not something a hand aims at.
        pad = self.px(theme.SCROLLBAR_PAD)
        return (x - pad <= mouse_x < x + width + pad
                and self.y <= mouse_y < self.y + self.height)

    def handle(self, event):
        inside = self.contains(event.mouse_x, event.mouse_y)
        busy = (self._press_index >= 0 or self._value_index >= 0
                or self._box_from is not None)

        # The scrollbar owns the mouse before the rows do -- it sits over the
        # right edge of every one of them.
        if self._scroll_drag:
            if event.is_move:
                self._scroll_to_bar(event.mouse_y, self._scroll_grab)
                return True
            if event.is_left_release:
                self._scroll_drag = False
                self.root.release(self)
                self.request_redraw()
            return True

        if event.is_left_press and not busy and self._on_scrollbar(
                event.mouse_x, event.mouse_y):
            bar = self._scrollbar_rect()
            top = bar[1] + bar[3]
            if bar[1] <= event.mouse_y < top:
                self._scroll_grab = top - event.mouse_y
            else:
                # Clicked the track: the bar jumps to the pointer, centred.
                self._scroll_grab = bar[3] // 2
                self._scroll_to_bar(event.mouse_y, self._scroll_grab)
            self._scroll_drag = True
            self.root.capture(self)
            self.request_redraw()
            return True

        if event.is_move and not busy:
            over = inside and self._on_scrollbar(event.mouse_x, event.mouse_y)
            if over != self._over_scrollbar:
                self._over_scrollbar = over
                self.request_redraw()

        if event.wheel and inside and not busy:
            self.scroll -= event.wheel * self._pitch()
            self._clamp_scroll()
            self.request_redraw()
            return True

        # A value drag started inside a row: only horizontal motion matters,
        # and the list keeps the mouse until the button comes back up.
        if self._value_index >= 0:
            if event.is_move:
                delta = event.mouse_x - self._value_x
                if delta and self.on_row_drag is not None:
                    self._value_x = event.mouse_x
                    self.on_row_drag(self._value_index, delta)
                return True
            if event.is_left_release:
                self._value_index = -1
                self.root.release(self)
                self.request_redraw()
            return True

        # A rubber band is being drawn from the empty space. It owns the
        # mouse for the same reason a reorder does: the pointer leaves the
        # list long before the gesture is over.
        if self._box_from is not None:
            if event.is_move:
                self._box_to = (event.mouse_x, event.mouse_y)
                self.request_redraw()
                return True
            if event.is_left_release:
                self._finish_box()
                return True
            return True

        # A press is held: this is either a click that has not finished yet or
        # a drag in progress. Either way the list owns the mouse until release.
        if self._press_index >= 0:
            if event.is_move:
                self._update_drag(event)
                return True
            if event.is_left_release:
                self._finish_drag()
                return True
            return True

        if event.is_move:
            index = self.index_at(event.mouse_y) if inside else -1
            # Recorded, but deliberately not a reason to redraw: moving along a
            # row changes nothing on screen, and this region carries the live
            # viewport, so a redraw here costs a full 3D render.
            self.hover_x = event.mouse_x
            if index != self.hovered_index:
                self.hovered_index = index
                self.request_redraw()
            return False

        if event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK' and inside:
            index = self.index_at(event.mouse_y)
            if index >= 0 and self.on_row_double_click is not None:
                local_x, local_y = self._row_local(index, event)
                self.on_row_double_click(index, local_x, local_y)
            return True

        if (event.type == 'RIGHTMOUSE' and event.is_press and inside
                and self.on_row_context is not None):
            index = self.index_at(event.mouse_y)
            if index >= 0:
                local_x, local_y = self._row_local(index, event)
                if self.on_row_context(index, local_x, local_y):
                    return True
            return False

        if event.is_left_press and inside:
            index = self.index_at(event.mouse_y)
            if index < 0:
                # The empty space under the last row. What that press turns
                # out to mean is not known yet: released where it went down it
                # is "none of them", which is what every list does; dragged, it
                # is a rubber band over the rows it crosses. So the answer is
                # deferred to the release rather than given here.
                self._last_click_index = -1
                self._box_from = (event.mouse_x, event.mouse_y)
                self._box_to = (event.mouse_x, event.mouse_y)
                self._box_extend = bool(event.ctrl or event.shift)
                self.root.capture(self)
                return True

            if self.on_row_click is not None:
                local_x, local_y = self._row_local(index, event)
                result = self.on_row_click(
                    index, local_x, local_y, event.alt, event.shift
                )

                if result:
                    # A click a control took is not half of a double click:
                    # toggling visibility and then reaching for the name must
                    # not be read as "rename this".
                    self._last_click_index = -1

                    if result == 'CAPTURE':
                        self._value_index = index
                        self._value_x = event.mouse_x
                        self.root.capture(self)
                        return True

                    self.request_redraw()
                    return True

            now = time.monotonic()
            # A click with a modifier held is a selection gesture and never
            # half of a double click: ctrl-clicking one row twice to add it and
            # drop it again must not be read as "rename this".
            modified = event.ctrl or event.shift
            self._press_double = (
                not modified
                and index == self._last_click_index
                and now - self._last_click_time <= self.DOUBLE_CLICK_SECONDS
                and abs(event.mouse_x - self._last_click_x)
                <= self.px(self.DOUBLE_CLICK_SLOP)
            )
            self._last_click_index = -1 if modified else index
            self._last_click_time = now
            self._last_click_x = event.mouse_x

            # Hold the press without committing to anything yet; whether this
            # is a click, a double click or a drag is decided by what the
            # pointer does before the button comes back up.
            self._press_index = index
            # Read at press time: by the time the button comes up the user may
            # have let the modifier go, and the gesture is what they meant when
            # they started it.
            self._press_extend = event.ctrl
            self._press_span = event.shift
            self._press_x = event.mouse_x
            self._press_y = event.mouse_y
            self._drag_x = event.mouse_x
            self._drag_y = event.mouse_y
            # The floating copy starts exactly where the pointer is, so the
            # chase has nothing to catch up on before the drag has begun.
            self._ghost_y = float(event.mouse_y)
            self._lift = 0.0
            self._dragging = False
            self._drop_index = -1
            self._drop_onto = -1
            self._anim_clock = time.monotonic()
            self.root.capture(self)
            return True

        return False

    def is_dragging(self):
        """True while a row is actually being carried, not merely pressed."""
        return self._dragging

    def is_scrolling(self):
        """True while the scrollbar is held."""
        return self._scroll_drag

    def _update_drag(self, event):
        if not self._dragging:
            if abs(event.mouse_y - self._press_y) < self.px(self.DRAG_THRESHOLD):
                return
            self._dragging = True
            self._anim_clock = time.monotonic()

        self._drag_x = event.mouse_x
        self._drag_y = event.mouse_y
        self._update_drop()
        self.animate()
        self.request_redraw()

    def _update_drop(self):
        """Read where the block would land from where the pointer is.

        Split out of `_update_drag` because the answer also changes without
        the pointer moving: an edge scroll slides the rows underneath it.

        The middle of a row that can hold others means "inside it"; its top
        and bottom edges mean "between". Which one the pointer is over is the
        whole of the gesture -- there is nothing else to read it from, and a
        gap alone cannot tell "into the group" from "underneath it".
        """
        onto = -1
        refused = -1
        if self.drop_accepts is not None:
            candidate = self.index_at(self._drag_y)
            if candidate >= 0:
                pitch = self._pitch()
                within = (self.row_top(candidate) - self._drag_y) / max(1, pitch)
                aimed_inside = self.ONTO_BAND <= within <= 1.0 - self.ONTO_BAND
                if aimed_inside:
                    if self.drop_accepts(self._press_index, candidate):
                        onto = candidate
                    elif self.drop_refuses is not None and self.drop_refuses(
                            self._press_index, candidate):
                        # Aimed inside something that looks like it could hold
                        # the block but will not. Silence here reads as a dead
                        # interface; the row says no instead.
                        refused = candidate

        self._drop_onto = onto
        self._drop_refused = refused
        self._drop_index = -1 if onto >= 0 else self.gap_at(self._drag_y)

    def _box_rows(self):
        """Every row the band covers, as row indices, top first.

        Read off the unmoved layout, the same as every other hit test here:
        the band is a rectangle over the list, and which rows it crosses is a
        question about where they are, not about how they are drawn.
        """
        if self._box_from is None or self._box_to is None:
            return []

        low = min(self._box_from[1], self._box_to[1])
        high = max(self._box_from[1], self._box_to[1])

        pitch = self._pitch()
        out = []
        for index in range(len(self.rows)):
            top = self.row_top(index)
            if top > low and top - pitch < high:
                out.append(index)
        return out

    def _box_moved(self):
        """Whether the band is a band at all, or a click that wobbled."""
        if self._box_from is None or self._box_to is None:
            return False
        slop = self.px(self.DRAG_THRESHOLD)
        return (abs(self._box_to[0] - self._box_from[0]) >= slop
                or abs(self._box_to[1] - self._box_from[1]) >= slop)

    def _finish_box(self):
        """The band came up. Either it selected rows, or it was a plain click."""
        moved = self._box_moved()
        rows = self._box_rows() if moved else []
        extend = self._box_extend

        self._box_from = None
        self._box_to = None
        self._box_extend = False
        self.root.release(self)

        if moved:
            if self.on_box_select is not None:
                self.on_box_select(rows, extend)
        elif self.on_empty_click is not None:
            # A press on nothing that did not move is still "none of them",
            # which is what it always was.
            self.on_empty_click(extend, False)

        self.request_redraw()

    def _draw_box(self):
        """The band itself: a filled accent wash inside a one-pixel frame."""
        if self._box_from is None or self._box_to is None:
            return

        x = min(self._box_from[0], self._box_to[0])
        y = min(self._box_from[1], self._box_to[1])
        width = abs(self._box_to[0] - self._box_from[0])
        height = abs(self._box_to[1] - self._box_from[1])
        if width < 1 or height < 1:
            return

        draw.rect(x, y, width, height,
                  theme.ACCENT[:3] + (theme.ACCENT[3] * 0.18,))
        draw.rect_outline(x, y, width, height, theme.ACCENT, max(1, self.px(1)))

    def _finish_drag(self):
        index = self._press_index
        dragging = self._dragging
        drop = self._drop_index
        onto = self._drop_onto
        double = self._press_double

        self._press_index = -1
        self._dragging = False
        self._press_double = False
        self._drop_index = -1
        self._drop_onto = -1
        self._drop_refused = -1
        self._spring_index = -1
        self._scroll_carry = 0.0
        self.root.release(self)

        if dragging:
            # A gesture that turned into a drag is not half of a double click,
            # however fast it was.
            self._last_click_index = -1

            # Dropping into the gap directly above or below the row it came
            # from is a no-op, not a move to the same place.
            # Dropping into the gap directly above or below the row it came
            # from is a no-op. Dropping *onto* a row never is: it says
            # something about nesting that no gap can say.
            moved = onto >= 0 or drop not in (index, index + 1)
            if moved and self.on_reorder is not None:
                self.on_reorder(index, drop, onto)
                # Where it went is the one thing the gesture cannot show: the
                # block may have landed inside a group three rows away. The
                # glow is what the eye follows back to it.
                self.flash_active()
        elif double and self.on_row_double_click is not None:
            self._last_click_index = -1
            self.on_row_double_click(
                index,
                self._press_x - self.x,
                self._press_y - (self.row_top(index) - self._pitch()),
            )
        elif self.on_activate is not None:
            # Always reported, even for the row that is already active: with a
            # modifier held it is adding to or dropping from a selection, and
            # without one it is collapsing that selection back to this row.
            #
            # `local_x` comes along because a row can have more than one thing
            # in it worth selecting -- a layer's content and its mask -- and
            # those cannot be handled in `on_row_click` without giving up the
            # double click, which that path consumes.
            self.active_index = index
            self.on_activate(
                index, self._press_extend, self._press_span,
                self._press_x - self.x,
            )
        elif index != self.active_index:
            self.active_index = index

        self.request_redraw()


class TextField(Widget):
    """Single-line text entry, run as a root overlay.

    Being the overlay is the whole trick: the root routes *every* event to the
    active overlay first, which is the only way this widget can own the
    keyboard in a window where Blender's own keymaps are otherwise live.

    There is no selection model -- a caret, arrow keys and backspace are what
    renaming a layer needs, and anything more would be invented weight.
    """

    PADDING = 5

    # Keys that move the caret, as (type -> function of (caret, length)).
    _MOTION = {
        'LEFT_ARROW': lambda caret, length: max(0, caret - 1),
        'RIGHT_ARROW': lambda caret, length: min(length, caret + 1),
        'HOME': lambda caret, length: 0,
        'END': lambda caret, length: length,
    }

    def __init__(self, name="text_field"):
        super().__init__(name)
        self.visible = False
        self.text = ""
        self.caret = 0
        self.on_commit = None
        self.on_cancel = None

    # --- opening / closing -------------------------------------------------

    def open_at(self, x, y, width, height, text="", over=None):
        self.text = text
        self.caret = len(text)
        self.set_rect(x, y, width, height)
        self.visible = True
        self.root.set_overlay(self, over=over)
        self.request_redraw()

    def close(self):
        self.visible = False
        if self.root is not None:
            # Named rather than "the top one": this field can be opened over
            # another overlay, and closing it must not close that one too.
            self.root.clear_overlay(self)
        self.request_redraw()

    def _commit(self):
        # Read the callback before closing: closing clears the overlay, and the
        # handler is free to reopen the field on another row.
        text, callback = self.text, self.on_commit
        self.close()
        if callback is not None:
            callback(text)

    def _cancel(self):
        callback = self.on_cancel
        self.close()
        if callback is not None:
            callback()

    # --- drawing -----------------------------------------------------------

    def _font_size(self):
        return self.px(theme.FONT_SIZE)

    def draw_popup(self):
        radius = self.radius()
        draw.rounded_rect(
            self.x, self.y, self.width, self.height, radius, theme.BG_CANVAS
        )
        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.ACCENT,
            max(1.0, self.px(1)),
        )

        pad = self.px(self.PADDING)
        size = self._font_size()
        inner = max(1, self.width - pad * 2)

        before, _ = draw.text_dimensions(self.text[: self.caret], size)
        _, text_height = draw.text_dimensions("Xy", size)

        # Keep the caret in view by scrolling the text left when it runs past
        # the right edge.
        offset = max(0, before - inner)
        text_y = self.y + (self.height - text_height) // 2

        with draw.clip(self.x + pad, self.y, inner, self.height):
            draw.text(self.x + pad - offset, text_y, self.text, size, theme.WIDGET_TEXT)

        caret_x = self.x + pad + before - offset
        draw.rect(
            caret_x, text_y - self.px(1), max(1, self.px(1)),
            text_height + self.px(2), theme.WIDGET_TEXT,
        )

    # --- events ------------------------------------------------------------

    def _caret_at(self, mouse_x):
        """Nearest caret position to `mouse_x`, measured character by character."""
        size = self._font_size()
        pad = self.px(self.PADDING)
        target = mouse_x - (self.x + pad)

        best, best_distance = 0, None
        for index in range(len(self.text) + 1):
            width, _ = draw.text_dimensions(self.text[:index], size)
            distance = abs(width - target)
            if best_distance is None or distance < best_distance:
                best, best_distance = index, distance
        return best

    def handle_popup(self, event):
        if event.is_left_press:
            if self.contains(event.mouse_x, event.mouse_y):
                self.caret = self._caret_at(event.mouse_x)
                self.request_redraw()
            else:
                # Clicking away keeps the edit. Losing a rename because the
                # pointer wandered would be the more annoying failure.
                self._commit()
            return True

        if not event.is_press:
            return True

        key = event.type

        if key in ('RET', 'NUMPAD_ENTER'):
            self._commit()
        elif key == 'ESC':
            self._cancel()
        elif key == 'BACK_SPACE':
            if self.caret > 0:
                self.text = self.text[: self.caret - 1] + self.text[self.caret :]
                self.caret -= 1
        elif key == 'DEL':
            self.text = self.text[: self.caret] + self.text[self.caret + 1 :]
        elif key in self._MOTION:
            self.caret = self._MOTION[key](self.caret, len(self.text))
        elif event.unicode and event.unicode.isprintable():
            self.text = self.text[: self.caret] + event.unicode + self.text[self.caret :]
            self.caret += len(event.unicode)

        self.request_redraw()
        return True


class ViewportArea(Widget):
    """A hole in the interface. The host 3D viewport shows through here.

    Draws no background at all -- Blender already rendered the viewport across
    the whole region, and every other PaintR2 widget is painted over it. Only
    a thin frame is added so the live area reads as part of the layout.

    It consumes no events either; the modal operator passes anything landing
    here straight to Blender, which is how navigation and painting keep
    working.
    """

    def __init__(self, name="viewport"):
        super().__init__(name)

    def draw(self):
        draw.rect_outline(
            self.x, self.y, self.width, self.height, theme.BORDER, self.px(1)
        )


class Placeholder(Widget):
    """A labelled empty area, marking where a real feature will land."""

    def __init__(self, text="", background=None, name=""):
        super().__init__(name)
        self.text = text
        self.background = background

    def draw(self):
        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)
        draw.rounded_rect(
            self.x, self.y, self.width, self.height, radius,
            _color(self.background, "BG_CANVAS"),
        )
        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.OUTLINE
        )

        if not self.text:
            return

        draw.text_centered(
            self.x + self.width * 0.5,
            self.y + self.height * 0.5,
            self.text,
            self.px(theme.FONT_SIZE),
            theme.WIDGET_TEXT_DIM,
        )

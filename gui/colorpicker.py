# SPDX-License-Identifier: GPL-3.0-or-later
"""A colour picker drawn from scratch.

Blender's own colour popup belongs to its UI system and cannot be summoned
into a window we paint ourselves, so this is a hand-drawn one.

A spacious saturation/value field, horizontal hue rail and separate channel
tracks keep the colour itself prominent. All controls share geometry between
drawing and hit testing, and follow Blender's theme and UI scale.

**The data is linear, everything on screen is sRGB.** The colours this
picker edits are Blender `COLOR` properties -- scene linear, which is what the
nodes, the bake and the brush read. What the user sees and touches is sRGB:
the square, the hue rail, the R/G/B sliders and their numbers, the hex code,
the eyedropper, and every swatch that shows a colour. The picker keeps its
HSV in sRGB and converts to linear only at `rgba()`, the value it writes
back. `display_color` is the one door from a stored colour to the screen.
"""

import colorsys
import math

from . import draw
from . import theme
from .widget import Widget
from .widgets import TextField


def _to_srgb(value):
    """One linear channel as the sRGB-encoded number a hex code carries."""
    value = min(1.0, max(0.0, value))
    if value <= 0.0031308:
        return value * 12.92
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def _from_srgb(value):
    """The inverse: an sRGB-encoded number back to linear."""
    value = min(1.0, max(0.0, value))
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def display_color(color, gamma=False):
    """A stored colour as the sRGB values to draw it with.

    Stored colours are scene linear -- Blender's `COLOR` subtype -- unless
    `gamma` says the property is `COLOR_GAMMA`, whose numbers already are
    display values. Alpha, if there is one, is kept as it is. Every chip that
    shows a colour goes through here, so a swatch, a layer row and the
    picker's preview are the same colour.
    """
    if gamma:
        rgb = tuple(min(1.0, max(0.0, channel)) for channel in color[:3])
    else:
        rgb = tuple(_to_srgb(channel) for channel in color[:3])
    return rgb + tuple(color[3:4])


def rgb_to_hex(color):
    """`(r, g, b)` linear -> `RRGGBB`, the way every other tool writes it."""
    return "".join(
        f"{int(round(_to_srgb(channel) * 255.0)):02X}" for channel in color[:3]
    )


def hex_to_srgb(text):
    """`RRGGBB` (or `RGB`, with or without a `#`) -> sRGB 0..1, or None.

    None rather than a guess: a field that turns a typo into black is a field
    that loses the colour that was in it.
    """
    text = (text or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return tuple(((value >> shift) & 0xFF) / 255.0 for shift in (16, 8, 0))


def hex_to_rgb(text):
    """`RRGGBB` -> linear, or None. The same code, decoded for storage."""
    color = hex_to_srgb(text)
    return None if color is None else tuple(_from_srgb(c) for c in color)


class ColorSwatch(Widget):
    """A colour rectangle that opens a picker when clicked."""

    def __init__(self, color=(1.0, 1.0, 1.0, 1.0), on_change=None, name="swatch"):
        super().__init__(name)
        self.color = tuple(color)
        self.on_change = on_change
        self.picker = None      # set by the owner, shared between swatches
        self.label = ""
        # True for a COLOR_GAMMA property: its numbers are display values
        # already, and go in and out of the picker unconverted.
        self.gamma = False

    def draw(self):
        radius = self.radius()

        if self.color[3] < 1.0:
            draw.checkerboard(
                self.x, self.y, self.width, self.height,
                self.px(5), (0.6, 0.6, 0.6, 1.0), (0.4, 0.4, 0.4, 1.0),
            )

        # Dimmed rather than hidden: an empty slot where a colour was reads as
        # a bug, a grey one reads as "nothing to colour right now".
        shown = display_color(self.color[:3], self.gamma)
        face = shown + (1.0,) if self.enabled else theme.BG_PANEL
        draw.widget(
            self.x, self.y, self.width, self.height, radius, face,
            outline=(theme.WIDGET_TEXT
                     if self.hovered and self.enabled else theme.OUTLINE),
            emboss=theme.EMBOSS,
        )

        if self.label:
            size = self.px(theme.FONT_SIZE_SMALL)
            _, text_height = draw.text_dimensions(self.label, size)
            # The label sits on the colour itself, so it is picked against the
            # swatch rather than against the theme -- otherwise a black brush
            # colour under a light theme writes dark text on dark paint.
            luminance = 0.2126 * shown[0] + 0.7152 * shown[1] + 0.0722 * shown[2]
            draw.text(
                self.x + self.px(4),
                self.y + (self.height - text_height) * 0.5,
                self.label,
                size,
                (0.06, 0.06, 0.06, 1.0) if luminance > 0.45 else (0.95, 0.95, 0.95, 1.0),
            )

    def handle(self, event):
        if not self.enabled:
            return False

        if event.is_left_press and self.contains(event.mouse_x, event.mouse_y):
            if self.picker is not None:
                self.picker.open_for(self)
            return True
        return False

    def apply(self, color):
        self.color = tuple(color)
        if self.on_change is not None:
            self.on_change(self.color)
        self.request_redraw()


class ColorPicker(Widget):
    """The popup itself. One instance per root, reused by every swatch.

    It is not part of the normal layout: the root draws and hit-tests it as an
    overlay so it can sit on top of everything.
    """

    WIDTH = theme.PICKER_WIDTH
    PAD = theme.PICKER_PAD
    HEADER_HEIGHT = theme.PICKER_HEADER_HEIGHT
    SQUARE = theme.PICKER_FIELD_HEIGHT
    STRIP_WIDTH = theme.PICKER_HUE_HEIGHT
    SLIDER_HEIGHT = theme.PICKER_CHANNEL_HEIGHT
    GAP = theme.PICKER_GAP

    def __init__(self, name="picker"):
        super().__init__(name)
        self.visible = False
        self.owner = None

        self.hue = 0.0
        self.saturation = 0.0
        self.value = 1.0
        self.alpha = 1.0

        self._dragging = None     # 'SQUARE' | 'HUE' | 'R' | 'G' | 'B' | 'A'

        # The hex code is edited in a real text field rather than in a buffer
        # of its own: the field already owns the keyboard properly, and it
        # opens *over* this picker rather than replacing it, so the square
        # stays on screen while a code is being typed.
        self.hex_field = TextField(name="picker_hex")
        self.hex_field.on_commit = self._on_hex_committed
        self.channel_field = TextField(name="picker_channel")
        self.channel_field.on_commit = self._on_channel_committed
        self._editing_channel = None

        # The eyedropper is armed, waiting for the click that says where.
        # While it is, every press in the window is a sample rather than
        # whatever it would otherwise have been -- which is exactly what an
        # eyedropper is, and why it disarms itself the moment it fires.
        self.sampling = False
        # Where the next draw pass should read a pixel, region-local. The read
        # cannot happen when the click arrives: there is no framebuffer to
        # read outside the draw pass, so the click only records the spot.
        self._sample_at = None

    def children(self):
        # The hex field is not laid out here -- it is an overlay of its own --
        # but it still has to be handed the root, and `attach` walks children.
        return (self.hex_field, self.channel_field)

    # --- opening / closing -------------------------------------------------

    def open_for(self, swatch, over=None):
        """Open on `swatch`. `over` is the overlay it opens inside, if any."""
        self.owner = swatch
        red, green, blue = display_color(swatch.color[:3],
                                         getattr(swatch, "gamma", False))
        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(red, green, blue)
        self.alpha = swatch.color[3]

        self.visible = True
        self._place_near(swatch)
        self.root.set_overlay(self, over=over)
        self.request_redraw()

    def close(self):
        self.visible = False
        self.owner = None
        self._dragging = None
        self.sampling = False
        self._sample_at = None
        if self.root is not None:
            # Named, not "whatever is on top": the hex field opens *over* this
            # picker, so closing the picker has to cut the stack from here --
            # and a picker that is not on the stack at all must not take some
            # other overlay down with it.
            self.root.clear_overlay(self)
        self.request_redraw()

    def _place_near(self, swatch):
        width = self.px(self.WIDTH)
        height = self._total_height()

        x = swatch.x
        y = swatch.y - height - self.px(self.GAP)

        if y < 0:
            y = swatch.y + swatch.height + self.px(self.GAP)
        if x + width > self.root.width:
            x = self.root.width - width - self.px(self.GAP)

        y = min(y, self.root.height - height - self.px(self.GAP))
        self.set_rect(max(0, x), max(0, y), width, height)

    def _total_height(self):
        return self.px(2 * self.PAD + self.HEADER_HEIGHT + self.GAP
                       + self.SQUARE + self.GAP + self.STRIP_WIDTH
                       + 26 + 4 * self.SLIDER_HEIGHT + 3 * 4 + 12 + 30)

    # --- geometry ----------------------------------------------------------

    def _header_rect(self):
        pad = self.px(self.PAD)
        height = self.px(self.HEADER_HEIGHT)
        return self.x + pad, self.y + self.height - pad - height, self.width - 2 * pad, height

    def _square_rect(self):
        x, y, width, _ = self._header_rect()
        height = self.px(self.SQUARE)
        return x, y - self.px(self.GAP) - height, width, height

    def _strip_rect(self):
        x, y, width, _ = self._square_rect()
        height = self.px(self.STRIP_WIDTH)
        return x, y - self.px(self.GAP) - height, width, height

    def _slider_rects(self):
        x, y, width, _ = self._strip_rect()
        height = self.px(self.SLIDER_HEIGHT)
        y -= self.px(26) + height
        return [(x, y - i * (height + self.px(4)), width, height) for i in range(4)]

    def _track_rect(self, row):
        x, y, width, height = row
        return x + self.px(24), y + (height - self.px(8)) / 2, width - self.px(82), self.px(8)

    def _bottom_row(self):
        """Generous eyedropper and hex targets below the channel controls."""
        x, y, width, _ = self._slider_rects()[-1]
        height = self.px(30)
        y -= self.px(12) + height
        button = height
        gap = self.px(8)
        return (x, y, button, height), (x + button + gap, y, width - button - gap, height)

    # --- colour ------------------------------------------------------------

    def srgb(self):
        """The colour as shown: what the square, sliders and hex are in."""
        return colorsys.hsv_to_rgb(self.hue, self.saturation, self.value)

    def srgba(self):
        return self.srgb() + (self.alpha,)

    def rgb(self):
        """The colour as stored, what is written back: linear, unless the
        swatch it was opened for holds a COLOR_GAMMA property."""
        if getattr(self.owner, "gamma", False):
            return self.srgb()
        return tuple(_from_srgb(channel) for channel in self.srgb())

    def rgba(self):
        return self.rgb() + (self.alpha,)

    def _emit(self):
        if self.owner is not None:
            self.owner.apply(self.rgba())

    def hex_code(self):
        return "".join(
            f"{int(round(min(1.0, max(0.0, c)) * 255.0)):02X}" for c in self.srgb()
        )

    def _set_srgb(self, color):
        """Take an sRGB `(r, g, b)` in, keeping the alpha that is there."""
        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(*color[:3])
        self._emit()
        self.request_redraw()

    def _set_rgb(self, color):
        """Take a stored `(r, g, b)` in, keeping the alpha that is there."""
        self._set_srgb(display_color(color[:3], getattr(self.owner, "gamma", False)))

    def _on_hex_committed(self, text):
        color = hex_to_srgb(text)
        if color is not None:
            self._set_srgb(color)
        else:
            # An unreadable code leaves the colour alone. It is a typo, not an
            # instruction to become black.
            self.request_redraw()

    def _open_hex_field(self):
        x, y, width, height = self._bottom_row()[1]
        self.hex_field.open_at(x, y, width, height, self.hex_code(), over=self)

    def _open_channel_field(self, channel):
        index = "RGBA".index(channel)
        x, y, width, height = self._slider_rects()[index]
        self._editing_channel = channel
        self.channel_field.open_at(x + width - self.px(46), y, self.px(46), height,
                                   f"{self.srgba()[index]:.3f}", over=self)

    def _on_channel_committed(self, text):
        try:
            amount = float(text.replace(',', '.'))
        except ValueError:
            return
        if math.isfinite(amount) and self._editing_channel is not None:
            self._set_channel(self._editing_channel, min(1.0, max(0.0, amount)))
            self.request_redraw()

    # --- the eyedropper ----------------------------------------------------

    def _arm_sampler(self):
        self.sampling = True
        self.request_redraw()

    def _take_sample(self, mouse_x, mouse_y):
        """Remember where to read, and let the next draw pass do the reading."""
        self._sample_at = (int(mouse_x), int(mouse_y))
        self.sampling = False
        self.request_redraw()

    def _resolve_sample(self):
        """Read the recorded pixel. Called from the draw pass, once.

        Before the picker paints itself, so a sample taken over the popup
        reads what is under it rather than the popup -- and after everything
        else in the window has been drawn, so it reads what the user is
        actually looking at.

        The pixel is display-referred, and so is the picker: it is taken as it
        is. `rgba()` decodes it on the way out, the inverse of the display
        transform under Blender's default sRGB display, which is what makes a
        colour picked off the model the colour that paints back the same.
        """
        spot, self._sample_at = self._sample_at, None
        if spot is None:
            return

        color = draw.read_pixel(*spot)
        if color is None:
            return

        self._set_srgb(tuple(color[:3]))

    def _set_channel(self, channel, amount):
        red, green, blue = self.srgb()
        if channel == 'R':
            red = amount
        elif channel == 'G':
            green = amount
        elif channel == 'B':
            blue = amount
        else:
            self.alpha = amount
            self._emit()
            return

        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(red, green, blue)
        self._emit()

    # --- drawing -----------------------------------------------------------

    def draw_popup(self):
        # First thing in the draw pass, before a single pixel of the popup is
        # laid down: an eyedropper that read the framebuffer *after* the popup
        # was drawn would sample the popup.
        self._resolve_sample()

        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)
        draw.shadow(self.x, self.y, self.width, self.height, radius,
                    theme.SHADOW_FAC, self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(
            self.x, self.y, self.width, self.height, radius, theme.MENU_BACK
        )
        draw.rounded_rect_outline(
            self.x, self.y, self.width, self.height, radius, theme.MENU_OUTLINE
        )

        self._draw_header()
        self._draw_square()
        self._draw_strip()
        self._draw_sliders()
        self._draw_bottom_row()

    def _draw_header(self):
        x, y, width, height = self._header_rect()
        size = self.px(theme.FONT_SIZE)
        _, text_height = draw.text_dimensions("Color", size)
        draw.text(x, y + (height - text_height) / 2, "Color", size, theme.MENU_TEXT)
        # An opaque preview beside an alpha preview makes opacity visible.
        chip_width = self.px(54)
        cx = x + width - chip_width
        color = self.srgb()
        with draw.blend_alpha():
            draw.checkerboard(cx, y, chip_width, height, self.px(5),
                              (0.55, 0.55, 0.55, 1), (0.3, 0.3, 0.3, 1))
            draw.rect(cx, y, chip_width / 2, height, color + (1.0,))
            draw.rect(cx + chip_width / 2, y, chip_width / 2, height, color + (self.alpha,))
        draw.rect_outline(cx, y, chip_width, height, theme.OUTLINE, self.px(1))

    def _draw_square(self):
        x, y, width, height = self._square_rect()
        hue_color = colorsys.hsv_to_rgb(self.hue, 1.0, 1.0) + (1.0,)
        black = (0.0, 0.0, 0.0, 1.0)
        white = (1.0, 1.0, 1.0, 1.0)
        draw.linear_gradient_rect(x, y, width, height, black, black, hue_color,
                                  white, encode=False)
        draw.rect_outline(x, y, width, height, theme.OUTLINE, self.px(1))
        self._draw_handle(x + width * self.saturation, y + height * self.value,
                          self.srgb() + (1.0,))

    def _draw_handle(self, x, y, color, radius=5):
        radius = self.px(radius)
        draw.circle(x, y, radius + self.px(2), (0.04, 0.04, 0.04, 1.0))
        draw.circle(x, y, radius + self.px(1), (1.0, 1.0, 1.0, 1.0))
        draw.circle(x, y, max(1, radius - self.px(1)), color)

    def _draw_strip(self):
        x, y, width, height = self._strip_rect()
        for i in range(6):
            left = colorsys.hsv_to_rgb(i / 6, 1, 1) + (1.0,)
            right = colorsys.hsv_to_rgb((i + 1) / 6, 1, 1) + (1.0,)
            draw.linear_gradient_rect(x + width * i / 6, y, width / 6, height,
                                      left, right, right, left, encode=False)
        self._draw_handle(x + width * self.hue, y + height / 2,
                          colorsys.hsv_to_rgb(self.hue, 1, 1) + (1.0,))

    def _draw_sliders(self):
        rgb = self.srgb()
        size = self.px(theme.FONT_SIZE_SMALL)
        x, y, width, _ = self._strip_rect()
        draw.text(x, y - self.px(19), "RGBA", size, theme.WIDGET_TEXT_DIM)
        label_width, _ = draw.text_dimensions("sRGB", size)
        draw.text(x + width - label_width, y - self.px(19), "sRGB", size, theme.WIDGET_TEXT_DIM)
        for index, (label, amount, row) in enumerate(zip("RGBA", rgb + (self.alpha,), self._slider_rects())):
            x, y, width, height = row
            _, text_height = draw.text_dimensions(label, size)
            text_y = y + (height - text_height) / 2
            draw.text(x, text_y, label, size, theme.WIDGET_TEXT_DIM)
            tx, ty, tw, th = self._track_rect(row)
            if index == 3:
                draw.checkerboard(tx, ty, tw, th, self.px(4),
                                  (0.55, 0.55, 0.55, 1), (0.3, 0.3, 0.3, 1))
                left, right = rgb + (0.0,), rgb + (1.0,)
            else:
                low, high = list(rgb), list(rgb)
                low[index], high[index] = 0.0, 1.0
                left, right = tuple(low) + (1.0,), tuple(high) + (1.0,)
            with draw.blend_alpha():
                draw.linear_gradient_rect(tx, ty, tw, th, left, right, right,
                                          left, encode=False)
            self._draw_handle(tx + tw * min(1, max(0, amount)), ty + th / 2,
                              theme.MENU_BACK, radius=3)
            vx, vw = x + width - self.px(46), self.px(46)
            draw.widget(vx, y, vw, height, theme.radius(height, self.ui_scale),
                        theme.BG_PANEL, outline=theme.OUTLINE, emboss=theme.EMBOSS)
            reading = f"{amount:.2f}"
            reading_width, _ = draw.text_dimensions(reading, size)
            draw.text(vx + (vw - reading_width) / 2, text_y, reading, size, theme.WIDGET_TEXT)

    def _draw_bottom_row(self):
        button, field = self._bottom_row()
        size = self.px(theme.FONT_SIZE_SMALL)

        # The eyedropper. Lit while it is armed, so "waiting for you to click
        # something" is visible rather than remembered.
        x, y, width, height = button
        radius = theme.radius(height, self.ui_scale)
        draw.widget(
            x, y, width, height, radius,
            theme.ACCENT if self.sampling else theme.WIDGET,
            outline=theme.OUTLINE, emboss=theme.EMBOSS,
        )
        self._draw_dropper_glyph(x, y, width, height)

        # The hex field. Drawn as the text box it becomes when clicked.
        x, y, width, height = field
        radius = theme.radius(height, self.ui_scale)
        draw.widget(x, y, width, height, radius, theme.WIDGET,
                    outline=theme.OUTLINE, emboss=theme.EMBOSS)

        text = "#" + self.hex_code()
        _, text_height = draw.text_dimensions(text, size)
        with draw.clip(x, y, width, height):
            draw.text(x + self.px(10), y + (height - text_height) * 0.5, text,
                      size, theme.WIDGET_TEXT)

    def _draw_dropper_glyph(self, x, y, width, height):
        """A pipette: a barrel on the diagonal with a tip at the bottom left.

        Inset from the button edges and scaled from a 16-unit grid.
        """
        inset = self.px(6)
        x, y = x + inset, y + inset
        unit = (min(width, height) - 2 * inset) / 16.0
        color = theme.WIDGET_TEXT_SEL if self.sampling else theme.WIDGET_TEXT
        weight = max(1.0, unit * 1.4)

        def point(gx, gy):
            return (x + gx * unit, y + gy * unit)

        draw.polyline([point(4.5, 4.5), point(10.5, 10.5)], color, weight)
        draw.polyline([point(9.0, 12.0), point(13.0, 8.0)], color,
                      max(1.0, unit * 2.4))
        draw.polygon(
            [point(2.5, 2.5), point(5.6, 4.2), point(4.2, 5.6)], color
        )

    # --- events ------------------------------------------------------------

    def _zone_at(self, mouse_x, mouse_y):
        sx, sy, sw, sh = self._square_rect()
        if sx <= mouse_x < sx + sw and sy <= mouse_y < sy + sh:
            return 'SQUARE'

        hx, hy, hw, hh = self._strip_rect()
        if hx <= mouse_x < hx + hw and hy <= mouse_y < hy + hh:
            return 'HUE'

        for label, (x, y, width, height) in zip(
            ("R", "G", "B", "A"), self._slider_rects()
        ):
            if x <= mouse_x < x + width and y <= mouse_y < y + height:
                if mouse_x >= x + width - self.px(46):
                    return 'VALUE_' + label
                return label

        for label, (x, y, width, height) in zip(
            ('DROPPER', 'HEX'), self._bottom_row()
        ):
            if x <= mouse_x < x + width and y <= mouse_y < y + height:
                return label

        return None

    def _apply_zone(self, zone, mouse_x, mouse_y):
        if zone == 'SQUARE':
            x, y, width, height = self._square_rect()
            self.saturation = min(1.0, max(0.0, (mouse_x - x) / max(1, width)))
            self.value = min(1.0, max(0.0, (mouse_y - y) / max(1, height)))
            self._emit()
        elif zone == 'HUE':
            x, _y, width, _height = self._strip_rect()
            self.hue = min(1.0, max(0.0, (mouse_x - x) / max(1, width)))
            self._emit()
        elif zone in ("R", "G", "B", "A"):
            rects = dict(zip(("R", "G", "B", "A"), self._slider_rects()))
            x, _y, width, _height = self._track_rect(rects[zone])
            self._set_channel(zone, min(1.0, max(0.0, (mouse_x - x) / max(1, width))))

        self.request_redraw()

    def handle_popup(self, event):
        """Called by the root while this picker is the active overlay."""
        inside = self.contains(event.mouse_x, event.mouse_y)

        # The eyedropper owns every press in the window while it is armed --
        # including the ones over the popup, so a colour can be taken off a
        # swatch as well as off the model. It fires once and disarms.
        if self.sampling:
            if event.is_left_press:
                self._take_sample(event.mouse_x, event.mouse_y)
                return True
            if event.is_press and event.type in ('ESC', 'RIGHTMOUSE'):
                self.sampling = False
                self.request_redraw()
                return True
            return True

        if self._dragging is not None:
            if event.is_move:
                self._apply_zone(self._dragging, event.mouse_x, event.mouse_y)
                return True
            if event.is_left_release:
                self._dragging = None
                return True
            return True

        if event.is_left_press:
            if not inside:
                self.close()
                return True

            zone = self._zone_at(event.mouse_x, event.mouse_y)
            if zone == 'DROPPER':
                self._arm_sampler()
                return True
            if zone == 'HEX':
                self._open_hex_field()
                return True
            if zone is not None and zone.startswith('VALUE_'):
                self._open_channel_field(zone[-1])
                return True
            if zone is not None:
                self._dragging = zone
                self._apply_zone(zone, event.mouse_x, event.mouse_y)
            return True

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        # Swallow everything else so the viewport underneath stays quiet.
        return inside or event.is_move

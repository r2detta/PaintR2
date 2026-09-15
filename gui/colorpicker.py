# SPDX-License-Identifier: GPL-3.0-or-later
"""A colour picker drawn from scratch.

Blender's own colour popup belongs to its UI system and cannot be summoned
into a window we paint ourselves, so this is a hand-drawn one:

    +-----------------+--+      saturation x value square, hue strip
    |                 |  |
    |   S x V         |H |
    |                 |  |
    +-----------------+--+
    | R ============= |         precise numeric entry
    | G ============= |
    | B ============= |
    | A ============= |
    | [/] #RRGGBB     |         eyedropper, and the hex the rest of the world
    +--------------------+      writes colours in

The square is a bilinear gradient -- white to hue across the top, both fading
to black at the bottom -- which is exactly how such a square is defined, so no
per-pixel shader is needed.

Colours are handled in Blender's linear space throughout; `mathutils.Color`
does the HSV conversion.

**Hex is sRGB, the sliders are linear.** A hex code is what a browser, an
image editor or a colour from a reference sheet is written in, and every one
of those means the sRGB-encoded byte. Handing `#808080` straight to a linear
slider would give a colour half again too bright, so the two conversions in
`_to_srgb` / `_from_srgb` are not decoration -- they are what makes a hex code
pasted in here the colour it is everywhere else.
"""

import colorsys

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


def rgb_to_hex(color):
    """`(r, g, b)` linear -> `RRGGBB`, the way every other tool writes it."""
    return "".join(
        f"{int(round(_to_srgb(channel) * 255.0)):02X}" for channel in color[:3]
    )


def hex_to_rgb(text):
    """`RRGGBB` (or `RGB`, with or without a `#`) -> linear, or None.

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
    return tuple(
        _from_srgb(((value >> shift) & 0xFF) / 255.0) for shift in (16, 8, 0)
    )


class ColorSwatch(Widget):
    """A colour rectangle that opens a picker when clicked."""

    def __init__(self, color=(1.0, 1.0, 1.0, 1.0), on_change=None, name="swatch"):
        super().__init__(name)
        self.color = tuple(color)
        self.on_change = on_change
        self.picker = None      # set by the owner, shared between swatches
        self.label = ""

    def draw(self):
        radius = self.radius()

        if self.color[3] < 1.0:
            draw.checkerboard(
                self.x, self.y, self.width, self.height,
                self.px(5), (0.6, 0.6, 0.6, 1.0), (0.4, 0.4, 0.4, 1.0),
            )

        # Dimmed rather than hidden: an empty slot where a colour was reads as
        # a bug, a grey one reads as "nothing to colour right now".
        face = self.color[:3] + (1.0,) if self.enabled else theme.BG_PANEL
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
            luminance = (
                0.2126 * self.color[0] + 0.7152 * self.color[1] + 0.0722 * self.color[2]
            )
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

    WIDTH = 200
    SQUARE = 150
    STRIP_WIDTH = 18
    SLIDER_HEIGHT = 16
    GAP = 4

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
        return (self.hex_field,)

    # --- opening / closing -------------------------------------------------

    def open_for(self, swatch, over=None):
        """Open on `swatch`. `over` is the overlay it opens inside, if any."""
        self.owner = swatch
        red, green, blue, alpha = swatch.color
        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(red, green, blue)
        self.alpha = alpha

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

        self.set_rect(max(0, x), max(0, y), width, height)

    def _total_height(self):
        return (
            self.px(self.SQUARE)
            + self.px(self.GAP)
            # Four sliders and the hex row, which is a slider tall.
            + 5 * (self.px(self.SLIDER_HEIGHT) + self.px(self.GAP))
            + self.px(theme.PAD) * 2
        )

    # --- geometry ----------------------------------------------------------

    def _square_rect(self):
        pad = self.px(theme.PAD)
        size = self.px(self.SQUARE)
        strip = self.px(self.STRIP_WIDTH)
        width = self.width - pad * 2 - strip - self.px(self.GAP)
        return self.x + pad, self.y + self.height - pad - size, width, size

    def _strip_rect(self):
        pad = self.px(theme.PAD)
        size = self.px(self.SQUARE)
        strip = self.px(self.STRIP_WIDTH)
        return (
            self.x + self.width - pad - strip,
            self.y + self.height - pad - size,
            strip,
            size,
        )

    def _slider_rects(self):
        pad = self.px(theme.PAD)
        height = self.px(self.SLIDER_HEIGHT)
        gap = self.px(self.GAP)
        square_y = self.y + self.height - pad - self.px(self.SQUARE)

        rects = []
        y = square_y - gap - height
        for _ in range(4):
            rects.append((self.x + pad, y, self.width - pad * 2, height))
            y -= height + gap
        return rects

    def _bottom_row(self):
        """`(eyedropper_rect, hex_rect)` -- the row under the four sliders."""
        pad = self.px(theme.PAD)
        height = self.px(self.SLIDER_HEIGHT)
        gap = self.px(self.GAP)

        _x, last_y, _w, _h = self._slider_rects()[-1]
        y = last_y - gap - height
        width = self.width - pad * 2

        button = height
        return (
            (self.x + pad, y, button, height),
            (self.x + pad + button + gap, y, max(0, width - button - gap), height),
        )

    # --- colour ------------------------------------------------------------

    def rgb(self):
        return colorsys.hsv_to_rgb(self.hue, self.saturation, self.value)

    def rgba(self):
        return self.rgb() + (self.alpha,)

    def _emit(self):
        if self.owner is not None:
            self.owner.apply(self.rgba())

    def hex_code(self):
        return rgb_to_hex(self.rgb())

    def _set_rgb(self, color):
        """Take a linear `(r, g, b)` in, keeping the alpha that is there."""
        self.hue, self.saturation, self.value = colorsys.rgb_to_hsv(*color[:3])
        self._emit()
        self.request_redraw()

    def _on_hex_committed(self, text):
        color = hex_to_rgb(text)
        if color is not None:
            self._set_rgb(color)
        else:
            # An unreadable code leaves the colour alone. It is a typo, not an
            # instruction to become black.
            self.request_redraw()

    def _open_hex_field(self):
        x, y, width, height = self._bottom_row()[1]
        self.hex_field.open_at(x, y, width, height, self.hex_code(), over=self)

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

        The pixel is display-referred; the sliders are linear. `_from_srgb`
        is the inverse of the display transform under Blender's default sRGB
        display, which is what makes a colour picked off the model the colour
        that paints back the same.
        """
        spot, self._sample_at = self._sample_at, None
        if spot is None:
            return

        color = draw.read_pixel(*spot)
        if color is None:
            return

        self._set_rgb(tuple(_from_srgb(channel) for channel in color))

    def _set_channel(self, channel, amount):
        red, green, blue = self.rgb()
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

        self._draw_square()
        self._draw_strip()
        self._draw_sliders()
        self._draw_bottom_row()

    def _draw_square(self):
        x, y, width, height = self._square_rect()
        hue_color = colorsys.hsv_to_rgb(self.hue, 1.0, 1.0) + (1.0,)
        black = (0.0, 0.0, 0.0, 1.0)
        white = (1.0, 1.0, 1.0, 1.0)

        draw.gradient_rect(x, y, width, height, black, black, hue_color, white)
        draw.rect_outline(x, y, width, height, theme.BORDER, self.px(1))

        cursor_x = x + width * self.saturation
        cursor_y = y + height * self.value
        radius = self.px(4)
        draw.rect_outline(
            cursor_x - radius, cursor_y - radius, radius * 2, radius * 2,
            (1.0, 1.0, 1.0, 1.0), self.px(1),
        )
        draw.rect_outline(
            cursor_x - radius - 1, cursor_y - radius - 1, radius * 2 + 2,
            radius * 2 + 2, (0.0, 0.0, 0.0, 1.0), self.px(1),
        )

    def _draw_strip(self):
        x, y, width, height = self._strip_rect()

        stops = [colorsys.hsv_to_rgb(i / 6.0, 1.0, 1.0) + (1.0,) for i in range(7)]
        draw.vertical_gradient(x, y, width, height, stops)
        draw.rect_outline(x, y, width, height, theme.BORDER, self.px(1))

        marker_y = y + height * (1.0 - self.hue)
        draw.rect(x, marker_y - self.px(1), width, self.px(2), (1.0, 1.0, 1.0, 1.0))

    def _draw_sliders(self):
        red, green, blue = self.rgb()
        values = (("R", red), ("G", green), ("B", blue), ("A", self.alpha))
        size = self.px(theme.FONT_SIZE_SMALL)

        for (label, amount), (x, y, width, height) in zip(values, self._slider_rects()):
            radius = theme.radius(height, self.ui_scale)
            draw.widget(x, y, width, height, radius, theme.WIDGET,
                        outline=theme.OUTLINE, emboss=theme.EMBOSS)
            filled = int(width * min(1.0, max(0.0, amount)))
            if filled:
                corners = draw.CORNER_ALL if filled >= width else draw.CORNER_LEFT
                draw.rounded_rect(x, y, filled, height, radius, theme.ACCENT,
                                  corners=corners)

            _, text_height = draw.text_dimensions(label, size)
            text_y = y + (height - text_height) * 0.5
            draw.text(x + self.px(4), text_y, label, size, theme.WIDGET_TEXT)

            reading = f"{amount:.2f}"
            reading_width, _ = draw.text_dimensions(reading, size)
            draw.text(
                x + width - self.px(4) - reading_width,
                text_y,
                reading,
                size,
                theme.WIDGET_TEXT,
            )

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
            draw.text(x + self.px(4), y + (height - text_height) * 0.5, text,
                      size, theme.WIDGET_TEXT)

    def _draw_dropper_glyph(self, x, y, width, height):
        """A pipette: a barrel on the diagonal with a tip at the bottom left.

        Drawn here rather than in `icons` because it is the only place it is
        used and it has to be sized against a 16-pixel button, not a 16-unit
        grid.
        """
        unit = min(width, height) / 16.0
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
            _x, y, _width, height = self._strip_rect()
            self.hue = min(1.0, max(0.0, 1.0 - (mouse_y - y) / max(1, height)))
            self._emit()
        elif zone in ("R", "G", "B", "A"):
            rects = dict(zip(("R", "G", "B", "A"), self._slider_rects()))
            x, _y, width, _height = rects[zone]
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
            if zone is not None:
                self._dragging = zone
                self._apply_zone(zone, event.mouse_x, event.mouse_y)
            return True

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        # Swallow everything else so the viewport underneath stays quiet.
        return inside or event.is_move

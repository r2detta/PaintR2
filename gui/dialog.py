# SPDX-License-Identifier: GPL-3.0-or-later
"""A centred modal panel: a title, a list of rows, and two buttons.

The window had no way to *ask* anything. Every command in it fires the moment
it is clicked, which is right for adding a layer and wrong for an export: a
bake writes files to a folder, at a size, in a format, for a set of channels,
and all four of those lived in a collapsed sub-panel the button did not
mention. Pressing it froze the window for ten seconds and produced no visible
change anywhere -- the textures were written, the report went to a status bar
PaintR2 has stripped, and the only honest reading from the user's seat was
that the export was broken.

So this is the shape that was missing. It is a root overlay, like a popover,
which is what lets the dropdowns and text fields *inside* it stack on top of
it rather than replace it. It is modal while it is up: everything that is not
one of its own rows is swallowed, so a click meant for the dialog cannot land
on the viewport behind it.

The body is a `PropertyPanel` and nothing more -- the rows come from RNA the
same way every other panel's do, so the dialog cannot drift from what the
properties actually are, and adding a setting to the export is adding a row.
"""

from . import draw
from . import theme
from .props import PropertyPanel, _inside
from .widget import Widget

# What the region behind a dialog is washed with. Not a theme colour and not
# derived from one: dimming is darkening under every theme Blender ships,
# light ones included, and a "dim" that brightened would be a strange thing to
# read off a palette.
DIM = (0.0, 0.0, 0.0, 0.45)


def _lighten(colour, amount):
    """`colour` nudged towards white. Follows the theme, unlike a literal.

    The confirm button is the accent colour and its hover state has to be the
    accent colour too -- picking a second blue out of the air would stop
    tracking the moment somebody changed their theme.
    """
    red, green, blue = colour[:3]
    alpha = colour[3] if len(colour) > 3 else 1.0
    return (
        min(1.0, red + amount),
        min(1.0, green + amount),
        min(1.0, blue + amount),
        alpha,
    )


class Dialog(Widget):
    """A modal panel with a title, rows, an optional footnote and two buttons.

    `on_confirm` fires when the confirm button is pressed, after the dialog has
    closed itself -- the work it starts blocks the main thread, and a dialog
    still on screen while that happens is a dialog the user watches freeze.
    """

    is_overlay = True

    WIDTH = 320
    TITLE_HEIGHT = 26
    BUTTON_HEIGHT = 22
    FOOTNOTE_HEIGHT = 18
    PADDING = 10
    GAP = 6

    def __init__(self, name="dialog"):
        super().__init__(name)
        self.open = False
        self.title = ""
        # A line under the rows saying what the buttons are about to do. The
        # count and the destination are the two things a confirm button cannot
        # carry and the user most needs before pressing it.
        self.footnote = ""
        self.confirm_text = "OK"
        self.cancel_text = "Cancel"
        self.confirm_enabled = True
        self.on_confirm = None
        self.panel = PropertyPanel(name=f"{name}_panel")
        self.panel.parent = self
        self._hovered_button = None

    # --- tree -------------------------------------------------------------

    def attach(self, root, parent=None):
        super().attach(root, parent)
        self.panel.attach(root, self)

    def children(self):
        # Drawn as part of the popup, never as part of the layout.
        return ()

    # --- opening / closing -------------------------------------------------

    def open_popup(self):
        if self.open:
            return
        self.open = True
        self._hovered_button = None
        self._place()
        self.root.set_overlay(self)
        self.request_redraw()

    def close(self):
        if not self.open:
            return
        self.open = False
        self._hovered_button = None
        if self.root is not None:
            self.root.clear_overlay(self)
        self.request_redraw()

    def _confirm(self):
        callback = self.on_confirm
        # Closed first, and deliberately: what confirm starts is a blocking
        # bake, and Blender will not repaint between here and the end of it.
        # A dialog left up would still be on screen, unresponsive, for the
        # whole of it.
        self.close()
        if callback is not None:
            callback()

    # --- geometry ----------------------------------------------------------

    def _place(self):
        """Centre the dialog on the region and size it to its rows."""
        width = self.px(self.WIDTH)
        pad = self.px(self.PADDING)
        rows = self.panel.content_height()

        height = (
            self.px(self.TITLE_HEIGHT)
            + rows
            + self.px(self.GAP)
            + (self.px(self.FOOTNOTE_HEIGHT) if self.footnote else 0)
            + self.px(self.BUTTON_HEIGHT)
            + pad * 2
        )

        root_width = self.root.width if self.root is not None else width
        root_height = self.root.height if self.root is not None else height

        # Slightly above centre: a dialog centred by arithmetic reads as low,
        # which is why Blender's own popups sit where they do.
        x = int((root_width - width) * 0.5)
        y = int((root_height - height) * 0.55)
        x = max(0, min(x, max(0, root_width - width)))
        y = max(0, min(y, max(0, root_height - height)))

        self.set_rect(x, y, width, height)
        self.panel.set_rect(
            x + pad,
            y + pad + self.px(self.BUTTON_HEIGHT) + self.px(self.GAP)
            + (self.px(self.FOOTNOTE_HEIGHT) if self.footnote else 0),
            width - pad * 2,
            rows,
        )

    def _button_rects(self):
        """`(cancel_rect, confirm_rect)` along the bottom edge."""
        pad = self.px(self.PADDING)
        gap = self.px(self.GAP)
        height = self.px(self.BUTTON_HEIGHT)
        inner = self.width - pad * 2
        half = (inner - gap) // 2
        y = self.y + pad
        return (
            (self.x + pad, y, half, height),
            (self.x + pad + half + gap, y, inner - half - gap, height),
        )

    def _footnote_rect(self):
        pad = self.px(self.PADDING)
        return (
            self.x + pad,
            self.y + pad + self.px(self.BUTTON_HEIGHT) + self.px(self.GAP) // 2,
            self.width - pad * 2,
            self.px(self.FOOTNOTE_HEIGHT),
        )

    # --- drawing ----------------------------------------------------------

    def draw_popup(self):
        if not self.open:
            return

        # Re-placed every frame: the rows are rebuilt from RNA and the list
        # grows and shrinks with them -- turning a channel off takes a row
        # away, and a dialog sized once would leave a gap where it was.
        self._place()

        radius = theme.radius(self.height, self.ui_scale, theme.PANEL_ROUNDNESS)

        # The region behind it dims. Without this a modal panel over a busy
        # viewport reads as one more floating strip rather than as something
        # waiting for an answer.
        draw.rect(0, 0, self.root.width, self.root.height, DIM)

        draw.shadow(self.x, self.y, self.width, self.height, radius,
                    theme.SHADOW_FAC, self.px(theme.SHADOW_WIDTH))
        draw.rounded_rect(self.x, self.y, self.width, self.height, radius,
                          theme.MENU_BACK)
        draw.rounded_rect_outline(self.x, self.y, self.width, self.height, radius,
                                  theme.MENU_OUTLINE)

        self._draw_title()

        with draw.clip(self.panel.x, self.panel.y,
                       self.panel.width, self.panel.height):
            self.panel.draw()

        if self.footnote:
            size = self.px(theme.FONT_SIZE_SMALL)
            x, y, width, height = self._footnote_rect()
            _, text_height = draw.text_dimensions("X", size)
            draw.text_elided(
                x, y + (height - text_height) * 0.5, self.footnote, size,
                theme.WIDGET_TEXT_DIM, width, keep='TAIL',
            )

        cancel, confirm = self._button_rects()
        self._draw_button(cancel, self.cancel_text, 'CANCEL', accent=False,
                          enabled=True)
        self._draw_button(confirm, self.confirm_text, 'CONFIRM', accent=True,
                          enabled=self.confirm_enabled)

    def _draw_title(self):
        pad = self.px(self.PADDING)
        size = self.px(theme.FONT_SIZE)
        height = self.px(self.TITLE_HEIGHT)
        top = self.y + self.height - height

        _, text_height = draw.text_dimensions("X", size)
        draw.text_elided(
            self.x + pad, top + (height - text_height) * 0.5, self.title, size,
            theme.WIDGET_TEXT, self.width - pad * 2 - self.px(14),
        )

        mark = max(1, self.px(1))
        draw.rect(self.x + pad, top, self.width - pad * 2, mark, theme.BORDER)

    def _draw_button(self, rect, text, which, accent, enabled):
        x, y, width, height = rect
        radius = theme.radius(height, self.ui_scale)
        hovered = enabled and self._hovered_button == which

        if not enabled:
            background = theme.BG_PANEL
        elif accent:
            background = _lighten(theme.ACCENT, 0.08) if hovered else theme.ACCENT
        else:
            background = theme.WIDGET_HOVER if hovered else theme.WIDGET

        draw.widget(x, y, width, height, radius, background,
                    shade_top=theme.SHADE_TOP, shade_down=theme.SHADE_DOWN,
                    emboss=theme.EMBOSS)
        draw.rounded_rect_outline(x, y, width, height, radius, theme.OUTLINE)

        colour = theme.WIDGET_TEXT if enabled else theme.WIDGET_TEXT_DIM
        if accent and enabled:
            colour = theme.WIDGET_TEXT_SEL
        draw.text_centered(x + width * 0.5, y + height * 0.5, text,
                           self.px(theme.FONT_SIZE), colour)

    # --- events -----------------------------------------------------------

    def handle_popup(self, event):
        if not self.open:
            return False

        if self.panel.handle(event):
            return True

        cancel, confirm = self._button_rects()

        if event.is_move:
            was = self._hovered_button
            if _inside(confirm, event.mouse_x, event.mouse_y):
                self._hovered_button = 'CONFIRM'
            elif _inside(cancel, event.mouse_x, event.mouse_y):
                self._hovered_button = 'CANCEL'
            else:
                self._hovered_button = None
            if was != self._hovered_button:
                self.request_redraw()
            return True

        if event.is_left_press:
            if self.confirm_enabled and _inside(confirm, event.mouse_x, event.mouse_y):
                self._confirm()
                return True
            if _inside(cancel, event.mouse_x, event.mouse_y):
                self.close()
                return True
            # Clicking off a dialog dismisses it. It is the same answer as
            # Cancel and the one every popup in this window already gives.
            if not self.contains(event.mouse_x, event.mouse_y):
                self.close()
                return True
            return True

        if event.type == 'ESC' and event.is_press:
            self.close()
            return True

        if event.type in ('RET', 'NUMPAD_ENTER') and event.is_press:
            if self.confirm_enabled:
                self._confirm()
            return True

        # Modal: nothing behind a dialog gets to answer while it is up.
        return True

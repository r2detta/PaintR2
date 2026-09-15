# SPDX-License-Identifier: GPL-3.0-or-later
"""Widget base classes and the event object the interface runs on.

The whole custom interface is a tree of `Widget`s. Each widget owns a
region-local rectangle, draws itself, and gets a chance to consume events.
Layout is top-down and immediate: the root assigns rectangles every time the
region size changes, children subdivide what they are given.

There is no retained layout solver here on purpose -- panels are a fixed
left/center/right split, and anything more elaborate can be added when a
feature actually needs it.
"""

from . import theme


class UIEvent:
    """A Blender modal event translated into region-local coordinates.

    `mouse_x` / `mouse_y` are relative to the interface region's bottom-left.
    `kind` collapses Blender's (type, value) pair into something widgets can
    branch on without repeating the same string comparisons everywhere.
    """

    __slots__ = (
        "type",
        "value",
        "mouse_x",
        "mouse_y",
        "prev_mouse_x",
        "prev_mouse_y",
        "ctrl",
        "shift",
        "alt",
        "unicode",
        "wheel",
    )

    def __init__(self, blender_event, region_x, region_y, prev=None):
        self.type = blender_event.type
        self.value = blender_event.value
        self.mouse_x = blender_event.mouse_x - region_x
        self.mouse_y = blender_event.mouse_y - region_y
        self.prev_mouse_x = prev[0] if prev else self.mouse_x
        self.prev_mouse_y = prev[1] if prev else self.mouse_y
        self.ctrl = blender_event.ctrl
        self.shift = blender_event.shift
        self.alt = blender_event.alt
        self.unicode = blender_event.unicode

        if self.type == 'WHEELUPMOUSE':
            self.wheel = 1
        elif self.type == 'WHEELDOWNMOUSE':
            self.wheel = -1
        else:
            self.wheel = 0

    @property
    def is_move(self):
        return self.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}

    @property
    def is_press(self):
        return self.value == 'PRESS'

    @property
    def is_release(self):
        return self.value == 'RELEASE'

    @property
    def is_left_press(self):
        return self.type == 'LEFTMOUSE' and self.value == 'PRESS'

    @property
    def is_left_release(self):
        return self.type == 'LEFTMOUSE' and self.value == 'RELEASE'

    @property
    def delta(self):
        return (self.mouse_x - self.prev_mouse_x, self.mouse_y - self.prev_mouse_y)

    def __repr__(self):
        return (
            f"UIEvent({self.type} {self.value} "
            f"@{self.mouse_x},{self.mouse_y})"
        )


class Widget:
    """Base class: a rectangle that draws itself and may consume events."""

    def __init__(self, name=""):
        self.name = name
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.visible = True
        # A disabled widget still draws -- dimmed -- but never acts. Every
        # control can be dimmed, so this belongs here rather than on each
        # subclass that happens to have needed it first.
        self.enabled = True
        self.hovered = False
        self.root = None
        self.parent = None
        # Shown after the pointer rests on this widget. Empty means silent.
        self.tooltip = ""

    # --- geometry ---------------------------------------------------------

    def set_rect(self, x, y, width, height):
        self.x = int(x)
        self.y = int(y)
        self.width = max(0, int(width))
        self.height = max(0, int(height))
        self.on_resize()

    def on_resize(self):
        """Hook for subclasses that need to re-layout their children."""

    def contains(self, mouse_x, mouse_y):
        return (
            self.visible
            and self.x <= mouse_x < self.x + self.width
            and self.y <= mouse_y < self.y + self.height
        )

    @property
    def ui_scale(self):
        # The scale lives on the root only; every widget reads through to it so
        # a scale change needs no propagation pass.
        return self.root._ui_scale if self.root is not None else 1.0

    def px(self, value):
        return int(round(value * self.ui_scale))

    def radius(self, height=None, roundness=None):
        """This widget's corner radius, the way Blender computes its own."""
        return theme.radius(
            self.height if height is None else height, self.ui_scale, roundness
        )

    # --- tree -------------------------------------------------------------

    def attach(self, root, parent=None):
        """Give this widget (and its children) access to the root."""
        self.root = root
        self.parent = parent
        for child in self.children():
            child.attach(root, self)

    def children(self):
        return ()

    def walk(self):
        """Yield this widget and every descendant, depth-first."""
        yield self
        for child in self.children():
            yield from child.walk()

    # --- behaviour --------------------------------------------------------

    def draw(self):
        """Render this widget. Subclasses override; containers call super()."""
        for child in self.children():
            if child.visible:
                child.draw()

    def handle(self, event):
        """Return True when the event was consumed.

        Children are offered the event first (front-to-back), so an overlapping
        child wins over its parent.
        """
        for child in reversed(tuple(self.children())):
            if not child.visible:
                continue
            if child.handle(event):
                return True
        return False

    def request_redraw(self):
        if self.root is not None:
            self.root.request_redraw()


class Container(Widget):
    """A widget that holds an ordered list of children."""

    def __init__(self, name=""):
        super().__init__(name)
        self._children = []

    def add(self, widget):
        self._children.append(widget)
        if self.root is not None:
            widget.attach(self.root, self)
        return widget

    def clear(self):
        self._children.clear()

    def children(self):
        return tuple(self._children)

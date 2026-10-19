# SPDX-License-Identifier: GPL-3.0-or-later
"""Every key this window answers to, in one table.

Written as data because the alternative was measured and did not work: the
bindings used to be `if event.type == ...` branches scattered through
`root.handle_event`, and a new one silently ate an older one whenever the two
overlapped -- there was no place to look to find out whether a key was taken.

A binding is: which key, which modifiers, *where the pointer has to be*, and
what it does. The context is part of the binding rather than a check inside
the action, because that is what makes conflicts findable: two bindings clash
only when key, modifiers *and* context all meet. `X` is the reason -- over the
layer list it deletes (asking only when more than one thing could be meant),
anywhere else on the chrome it swaps the brush colours, and neither is wrong.

No bpy here, the same as the rest of `gui/`: an action is a name, and the
owner decides what the name does.
"""

import sys


def on_mac():
    """macOS, where Command does what Ctrl does everywhere else.

    Read on every call rather than once, so a test can stand in for a Mac.
    """
    return sys.platform == "darwin"


def command_held(event):
    """Whether the platform's command modifier is down: Ctrl, or Cmd on a Mac.

    The one place that decides it, so that a binding written `ctrl=True` is
    Cmd+C to a Mac user, the same as in every other program they have -- and
    Ctrl still works there too, as it does in Blender.
    """
    if getattr(event, "ctrl", False):
        return True
    return on_mac() and bool(getattr(event, "oskey", False))


def ctrl_label():
    return "Cmd" if on_mac() else "Ctrl"


# Where the pointer has to be for a binding to fire.
#
#   LAYERS   the layer panel
#   CHROME   anywhere on our interface that is not the viewport
#   ANY      anywhere in the window, viewport included
CONTEXTS = ('LAYERS', 'CHROME', 'ANY')


class Binding:
    """One key, one action. Immutable; the table is built once."""

    __slots__ = ("key", "ctrl", "shift", "alt", "context", "action", "label",
                 "display")

    def __init__(self, key, action, label, context='LAYERS',
                 ctrl=False, shift=False, alt=False, display=""):
        self.key = key
        self.action = action
        self.label = label
        self.context = context
        self.ctrl = ctrl
        self.shift = shift
        self.alt = alt
        # What to write on screen when the modifier spelling is not what the
        # key is called: shift-slash is "?" to everyone who types it.
        self.display = display

    def matches(self, key, ctrl, shift, alt):
        return (
            self.key == key
            and self.ctrl == bool(ctrl)
            and self.shift == bool(shift)
            and self.alt == bool(alt)
        )

    def accelerator(self):
        """The binding as it is written on screen: "Ctrl G", "Shift M" --
        "Cmd G" on a Mac."""
        if self.display:
            return self.display

        parts = []
        if self.ctrl:
            parts.append(ctrl_label())
        if self.alt:
            parts.append("Alt")
        if self.shift:
            parts.append("Shift")
        parts.append(_KEY_LABELS.get(self.key, self.key))
        return " ".join(parts)


_KEY_LABELS = {
    'UP_ARROW': "Up",
    'DOWN_ARROW': "Down",
    'LEFT_ARROW': "Left",
    'RIGHT_ARROW': "Right",
}


# The table. Order matters only in that the first match wins, and no two
# entries are allowed to match the same event in the same context -- there is
# a test that says so.
BINDINGS = (
    Binding('X', 'delete', "Delete the layer, its mask or the selected filter",
            context='LAYERS'),
    Binding('X', 'brush_swap', "Swap brush colours", context='CHROME'),

    Binding('UP_ARROW', 'layer_prev', "Select the layer above"),
    Binding('DOWN_ARROW', 'layer_next', "Select the layer below"),
    Binding('UP_ARROW', 'layer_move_up', "Move the layer up", ctrl=True),
    Binding('DOWN_ARROW', 'layer_move_down', "Move the layer down", ctrl=True),

    Binding('G', 'group_selected', "Group the selected layers", ctrl=True),

    # On the chrome rather than on the list, because the gesture spans the
    # two panels: copy a layer over the stack, click another material in the
    # panel above it, paste. Binding them to the list alone would mean the
    # paste failing wherever the hand happened to have come to rest.
    Binding('C', 'layer_copy', "Copy the selected layers", context='CHROME',
            ctrl=True),
    Binding('V', 'layer_paste', "Paste them into this material",
            context='CHROME', ctrl=True),

    Binding('M', 'mask_toggle', "Turn the mask off, or back on"),
    Binding('M', 'mask_solo', "Show the mask on its own", shift=True),
    Binding('I', 'layer_solo', "Show this layer on its own"),

    # Ours only when we actually have a fill to undo -- the handler says so,
    # and a key nobody claims goes on to Blender. A brush stroke is still
    # Blender's to undo; a fill was never in its stack to begin with.
    Binding('Z', 'fill_undo', "Undo a fill", context='ANY', ctrl=True),
    Binding('Z', 'fill_redo', "Redo a fill", context='ANY', ctrl=True,
            shift=True),

    Binding('F1', 'shortcuts', "Show this list", context='CHROME'),
    Binding('SLASH', 'shortcuts', "Show this list", context='CHROME',
            shift=True, display="?"),
)


def resolve(key, ctrl, shift, alt, contexts):
    """The action a key press means here, or "" for nothing.

    `contexts` is every context the pointer is currently in, most specific
    first -- ('LAYERS', 'CHROME', 'ANY') over the layer panel.
    """
    for wanted in contexts:
        for binding in BINDINGS:
            if binding.context == wanted and binding.matches(key, ctrl, shift, alt):
                return binding.action
    return ""


def listing():
    """The table as (accelerator, label, context) rows, for showing on screen.

    Grouped by context in the order they are declared, because that is the
    order a reader needs: what the key does here, then what it does elsewhere.
    """
    rows = []
    for wanted in CONTEXTS:
        for binding in BINDINGS:
            if binding.context == wanted:
                rows.append((binding.accelerator(), binding.label, wanted))
    return rows


def conflicts():
    """Every pair of bindings that would fire on the same event. Empty is the
    only acceptable answer; a test asserts it."""
    found = []
    entries = list(BINDINGS)
    for first in range(len(entries)):
        for second in range(first + 1, len(entries)):
            one, two = entries[first], entries[second]
            if one.context != two.context:
                continue
            if one.matches(two.key, two.ctrl, two.shift, two.alt):
                found.append((one.action, two.action))
    return found

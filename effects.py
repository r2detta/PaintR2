# SPDX-License-Identifier: GPL-3.0-or-later
"""Effect stacks: filters and generated patterns, stacked inside one layer.

A layer is not
one thing. It is a source with a pile of work done on top of it -- a fill,
then a contrast, then a blur, then a gradient blended over the lot -- and
every one of those steps is separately editable, separately switchable, and
separately blended. Without it the only way to say "this layer, but with more
contrast" is to bake it and lose the ability to change your mind.

Three kinds of effect, and the distinction is what they read:

* a **filter** transforms what is already there. `adjustment.py` is the
  registry; the same builders that serve an adjustment layer serve here, so a
  filter added to the set arrives in both places at once.
* a **pattern** ignores what is there and produces something of its own.
  `procedural.py` is that registry -- including `Custom`, so a hand-built node
  group is stackable inside a layer exactly like a filter is.
* **paint** is an image of its own, brushed by hand, blended in by its alpha.
  On a mask that is the useful one: a generated mask that is nearly right,
  with the last of it painted on top -- and switchable, so the hand-painted
  part can be taken off and put back without losing it. Where a paint *layer*
  is a layer, this is a step inside one.

Both then **blend back** over what they were given, with their own mode and
their own opacity. That is the whole of the difference between an effect and
a rewrite: an effect at 50% Multiply is half a multiply, and turning it off
leaves the layer exactly as it was.

Where they apply
----------------
A layer carries two stacks: one on its **content** and one on its **mask**.
They are the same machinery pointed at different sockets -- the content stack
sees a colour (or a scalar, in a scalar channel), the mask stack sees the
0..1 factor. A blur on a mask and a blur on a colour are the same filter.

Order
-----
Index 0 is the **top** of the list and is applied **last**, which is the same
rule the layer stack itself follows. The list reads the way the result is
built: what is written highest is what happened most recently.

What is deliberately not here
-----------------------------
Anything needing more than the pixel under the shading point. Blur, glow,
bevel and edge detection all read a *neighbourhood*, and a Blender shader
graph has no access to one -- there is no neighbour to sample, only the point
being shaded. They would need the layer rasterized to a texture first, which
is a different feature with a different cost, and pretending otherwise by
shipping a "Blur" that does nothing would be worse than not having it.
"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup

from . import adjustment
from . import nodegen
from . import procedural


TYPES = (
    ('FILTER', "Filter", "Transforms what is already in the layer"),
    ('PATTERN', "Pattern", "Generates something of its own and blends it in"),
    ('PAINT', "Paint", "An image of its own, painted by hand and blended in"),
)


def _recompile(self, context):
    owner = self.id_data
    if isinstance(owner, bpy.types.Material):
        nodegen.request_compile(owner)


class PR2_Effect(PropertyGroup):
    """One step in a layer's effect stack."""

    effect_type: EnumProperty(
        name="Type",
        items=TYPES,
        default='FILTER',
        update=_recompile,
    )

    enabled: BoolProperty(
        name="Enabled",
        description="Switch the effect off without losing its settings",
        default=True,
        update=_recompile,
    )

    # Its own blend and its own opacity. Without these an effect is a rewrite
    # rather than an effect: half a multiply is a thing you want, and it is
    # not expressible by the filter's own parameters.
    blend_mode: EnumProperty(
        name="Blend",
        items=lambda self, context: _blend_items(),
        update=_recompile,
    )

    opacity: FloatProperty(
        name="Opacity",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )

    # The two registries, both present, one used. A PointerProperty to a
    # PropertyGroup is never None and costs a handful of floats; branching on
    # `effect_type` is cheaper to read than two collections that must be kept
    # in step.
    filter: PointerProperty(type=adjustment.PR2_Adjustment)
    pattern: PointerProperty(type=procedural.PR2_Procedural)

    # PAINT only. The brush writes into this, and `document.paintable_image`
    # hands it to Blender's canvas whenever this effect is the selected one.
    image: PointerProperty(
        name="Image",
        type=bpy.types.Image,
        update=_recompile,
    )

    def writes(self, channel):
        """Whether this effect acts on `channel`.

        Only a content effect asks: a mask is one 0..1 factor for every
        channel, so a mask effect has no channel to choose.
        """
        return bool(getattr(self, "on_" + channel.lower(), False))

    def label(self):
        """What the row says: the kind it currently is.

        Derived, never stored. The kind is chosen in the properties section
        *after* the effect is added -- pick Levels instead of Invert and the
        row has to say Levels, which it cannot do if the name was frozen at
        the moment it was created.
        """
        if self.effect_type == 'FILTER':
            return adjustment.kind_label(self.filter.kind)
        if self.effect_type == 'PAINT':
            return "Paint"
        return procedural.kind_label(self.pattern.kind)


def _add_channel_switches():
    """One switch per channel, `on_<channel>`, written from the table.

    Base Color's starts on: every effect in a file from before this switch
    acted on Base Color, the only channel there was, and must go on doing
    exactly that. A new effect is given the channel the rows are showing
    instead (`add`).
    """
    from . import document as doc

    for identifier in doc.CHANNEL_IDS:
        PR2_Effect.__annotations__["on_" + identifier.lower()] = BoolProperty(
            name=doc.channel_label(identifier),
            description=f"Apply this effect to {doc.channel_label(identifier)}",
            default=identifier == 'BASE_COLOR',
            update=_recompile,
        )


def _blend_items():
    """The document's blend modes, resolved late.

    Late because `document.py` imports this module: asking it for its table at
    class-definition time would be a cycle.
    """
    from . import document as doc

    return [(item[0], item[1], item[2]) for item in doc.BLEND_MODES]


# --- compilation -------------------------------------------------------------


def _mix_back(group, channel, effect, below, transformed, x, y, is_color,
              alpha=None):
    """Blend an effect's result over what it was given.

    Not a replacement: the effect's blend mode and opacity are the whole
    reason it is an effect. An opacity of 1 with MIX *is* a replacement, which
    is the default and the common case, and `_mix` folds that away by itself.

    `alpha` is a socket that varies across the surface, and only a paint
    effect has one. Without it a freshly added paint effect -- an image that
    is transparent everywhere, because nothing has been painted into it yet --
    would blend its empty black over the whole layer at full strength. With
    it, an unpainted pixel contributes nothing, which is what makes the effect
    do nothing until it is painted.
    """
    factor = float(effect.opacity)
    if alpha is not None:
        factor = nodegen._multiply(
            group, alpha, factor, x - 300.0, y - 200.0, "paint alpha",
        )

    return nodegen._mix(
        group, channel, effect.blend_mode, factor,
        below, transformed, x, y, is_color=is_color,
    )


def _build_paint(group, effect, x, y):
    """A paint effect's image, as (colour socket, alpha socket).

    (None, None) when it has no image -- an effect whose image was deleted out
    from under it contributes nothing rather than raising.

    On a mask the Color output feeds a Value socket and Blender averages the
    three channels, which is exactly what a mask wants; the same node serves
    both stacks.
    """
    if effect.image is None:
        return None, None

    node = group.nodes.new('ShaderNodeTexImage')
    node.image = effect.image
    node.label = "Paint"
    node.location = (x, y)
    return node.outputs["Color"], node.outputs["Alpha"]


def apply_stack(group, stack, channel, socket, x, y, is_mask=False):
    """Run `stack` over `socket` and return the result.

    `socket` may be None for a mask with nothing under it yet; the first
    pattern in the stack then becomes the mask, which is what makes
    "generator as a mask" work without a painted mask underneath.

    An effect that has nothing to say for this channel -- a hue shift on
    roughness -- is skipped rather than raising, the same rule the adjustment
    layer follows.
    """
    from . import document as doc

    if not len(stack):
        return socket

    is_color = (not is_mask) and doc.channel_is_color(channel)
    cursor = x

    # Index 0 is the top of the list and applied last, so the walk is
    # backwards -- the same direction `_composite` walks the layer stack.
    for index in range(len(stack) - 1, -1, -1):
        effect = stack[index]
        if not effect.enabled:
            continue

        produced = None
        alpha = None
        # A content effect acts on the channels it was given; a mask
        # effect acts on the mask, which is every channel at once.
        if not is_mask and not effect.writes(channel):
            continue

        if effect.effect_type == 'FILTER':
            if socket is None:
                # Nothing to filter. A filter with no input is not an error;
                # it simply has not been given anything yet.
                continue
            produced = adjustment.build(
                group, effect.filter, channel, socket, cursor, y,
                is_color=is_color,
            )
        elif effect.effect_type == 'PAINT':
            produced, alpha = _build_paint(group, effect, cursor, y)
        else:
            produced = (
                procedural.build_color(group, effect.pattern, cursor, y)
                if is_color
                else procedural.build_factor(group, effect.pattern, cursor, y)
            )

        if produced is None:
            continue

        if socket is None:
            socket = produced
        else:
            socket = _mix_back(
                group, channel, effect, socket, produced, cursor + 600.0, y,
                is_color, alpha,
            )
        cursor += 900.0

    return socket


# --- helpers the interface and the operators use -----------------------------


def stack_of(layer, on_mask=False):
    """The layer's content stack, or its mask stack."""
    return layer.mask_effects if on_mask else layer.effects


def visible_stack(layer, on_mask=False):
    """The stack as the interface should show it.

    A mask stack with no mask is empty, whatever the data says. The operators
    keep the two in step, and this is the second lock on the same door: a
    filter with nothing to filter must not be drawn as a row that does
    something.
    """
    if on_mask and not layer.has_mask:
        return ()
    return stack_of(layer, on_mask)


DEFAULT_KIND = {
    # What an effect starts as. The kind is picked afterwards, in the
    # properties section -- this is only what the row says until then, and it
    # is the filter with the fewest surprises: Levels does nothing at its
    # defaults, so adding one changes nothing until it is set up.
    'FILTER': 'LEVELS',
    'PATTERN': 'GRADIENT',
    # Paint has no kinds. It is one thing, and what it becomes is painted.
    'PAINT': '',
}


def add(layer, effect_type, kind="", on_mask=False, index=-1, channel=None):
    """Add an effect and return it. `index` -1 means "at the top".

    `kind` is optional: an effect added without one starts at its type's
    default and is changed from the properties section, which is how it is
    added from the interface. Naming it up front was a menu of eleven
    entries, and the thing being chosen belongs beside the parameters it
    changes rather than in front of them.
    """
    stack = stack_of(layer, on_mask)
    effect = stack.add()
    effect.effect_type = effect_type

    kind = kind or DEFAULT_KIND.get(effect_type, "")

    if effect_type == 'FILTER':
        effect.filter.kind = kind
    elif effect_type == 'PATTERN':
        effect.pattern.kind = kind
        effect.pattern.enabled = True
    # PAINT gets its image from the operator, which is the only caller that
    # knows the material's name and the document's resolution.

    # The channel it acts on: the one the rows are showing when it was added
    #. Changed afterwards from its own chips.
    if channel is not None:
        from . import document as doc

        for identifier in doc.CHANNEL_IDS:
            setattr(effect, "on_" + identifier.lower(), identifier == channel)

    # Added at the end, then moved to where it belongs: a collection can only
    # append, and "the top of the list" is index 0.
    target = len(stack) - 1 if index < 0 else max(0, min(index, len(stack) - 1))
    if target != len(stack) - 1:
        stack.move(len(stack) - 1, target)
    else:
        stack.move(len(stack) - 1, 0)
        target = 0

    return stack[target]


def remove(layer, index, on_mask=False):
    """Drop one effect. True when there was one to drop."""
    stack = stack_of(layer, on_mask)
    if not (0 <= index < len(stack)):
        return False
    stack.remove(index)
    return True


def move(layer, index, direction, on_mask=False):
    """Move an effect up or down its stack. Returns where it ended up."""
    stack = stack_of(layer, on_mask)
    if not (0 <= index < len(stack)):
        return index

    target = index - 1 if direction == 'UP' else index + 1
    if not (0 <= target < len(stack)):
        return index

    stack.move(index, target)
    return target


def kinds_for(effect_type):
    """The registry a type picks from, as (identifier, label, description)."""
    if effect_type == 'FILTER':
        return tuple(adjustment.KINDS)
    if effect_type == 'PAINT':
        # Nothing to pick. Paint is one thing.
        return ()
    return tuple(procedural.KINDS)


def paint_images(layer):
    """Every image a layer's effect stacks paint into.

    `storage` asks, so a paint effect's pixels are saved and packed with the
    rest of the document. An image nobody collects is an image that is lost on
    the next reload, which is the whole of why masks had to be added to that
    list once.
    """
    out = []
    for on_mask in (False, True):
        for effect in stack_of(layer, on_mask):
            if effect.effect_type == 'PAINT' and effect.image is not None:
                out.append(effect.image)
    return out


CLASSES = (PR2_Effect,)


def register():
    _add_channel_switches()
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

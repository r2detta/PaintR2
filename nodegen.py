# SPDX-License-Identifier: GPL-3.0-or-later
"""Compiles a layer stack into a shader node tree.

The stack in document.py is the source; this module is the compiler. Output
goes into a node group named `PaintR2 <material>`, which is rebuilt from
scratch on every change. Nothing inside that group is meant to be edited by
hand -- edits belong outside it, where they survive.

The group carries **one output per channel** the document uses -- today
that is Base Color alone -- and the material is wired through a
Principled BSDF:

    [PaintR2 group] Base Color -> [Principled] -> [Material Output]

That is the PBR mode, and the default. Two other modes wire the same group
to the same output differently, because judging a painted colour and judging a
finished material are not the same job:

    DIFFUSE   Base Color -> Principled, Metallic 0 / Roughness 1
    UNLIT     Base Color -> Emission -> output, no light on it at all

The mode lives on the document (`shader_mode`), so it is per material. None of
it can reach an export: bake.py sets up its own Emission per channel and reads
the group output directly, then puts the wiring back through this module.

Unlit was once the *only* wiring, and it was wrong as a default the moment
roughness existed -- you cannot judge a roughness value you cannot see under
light. It is right again as a mode you choose.

An ADJUSTMENT layer is the one type that contributes no colour of its own: it
transforms the running result instead, and the transformed version is mixed
back over the untransformed one by the same factor everything else uses. So
its mask still means "where" and its opacity still means "how much".

Compositing rule, applied bottom to top, **once per channel**:

    factor = layer_alpha * mask * opacity   (Base Color of a PAINT layer)
    factor = mask * opacity                 (everything else)
    result = Mix(blend_mode, factor, result, layer_contribution)

`layer_alpha` exists only for Base Color, and only on a PAINT layer, because
the brush only ever paints Base Color. Every other channel of every
layer is a flat value -- what makes it vary across the surface is the *mask*,
and the mask is per layer, not per channel, so painting it cuts all of that
layer's channels at once.

That single formula still produces "top layer overrides the one below" --
unpainted pixels have alpha 0, so whatever is underneath shows through.

A GROUP layer is compiled the same way, one level up: its children composite
over what is under the group, and the group mixes that result back over the
same thing with its own factor. So a group at 50% is half of everything inside
it, and a mask on a group hides all of it at once.

Blender 5.2 specifics that matter here, all measured:
  * ShaderNodeMix in RGBA mode exposes ten inputs all named "Factor"/"A"/"B";
    only the identifiers tell them apart. `inputs["A"]` returns the disabled
    float socket. Always go through `socket()`.
  * **ShaderNodeMix in FLOAT mode ignores `blend_type` entirely** -- every
    mode gives a plain lerp. So a scalar channel's blend mode goes through
    ShaderNodeMath instead, via `_SCALAR_BLEND`, and the ones with no float
    equivalent fall back to a lerp.
  * **Math node output is not clamped**: ADD gives 1.1, SUBTRACT gives -0.7.
    Every scalar channel is clamped on the way out (`_CLAMPED`; none exist
    while Base Color is the only channel).
  * Removing an output socket from a group interface **silently drops** the
    link that fed from it, and re-adding the socket does not bring the link
    back. So changing the channel set means re-wiring the material, which is
    what `rewire=True` is for.
  * Node group sockets use the 4.0+ interface API (`interface.new_socket`);
    the legacy `group.inputs` collection is gone.
"""

import time

import bpy


GROUP_PREFIX = "PaintR2 "

# Laid out left to right so the generated tree is readable if you open it, and
# one horizontal band per channel so a five-channel group reads as five rows
# rather than as a thicket.
_COLUMN_WIDTH = 260.0
_ROW_HEIGHT = 300.0
_CHANNEL_BAND = 1400.0

# The socket type each channel's group output gets. A tangent-space normal map
# is an image, and ShaderNodeNormalMap wants a colour, so NORMAL is a colour
# socket -- making it a vector would mean a conversion node at both ends.
_SOCKET_TYPES = {
    'COLOR': 'NodeSocketColor',
    'FLOAT': 'NodeSocketFloat',
}

# Blend modes that mean something for a single float, and the Math operation
# that means it. Everything not in here falls back to a plain lerp, because a
# float has no hue to take and no contrast to burn.
#
# The operation is applied to (below, layer) and the result is then mixed back
# over `below` by the factor -- so a half-masked Multiply is half multiplied.
_SCALAR_BLEND = {
    'MULTIPLY': 'MULTIPLY',
    'ADD': 'ADD',
    'SUBTRACT': 'SUBTRACT',
    'DIVIDE': 'DIVIDE',
    'DARKEN': 'MINIMUM',
    'LIGHTEN': 'MAXIMUM',
    'DIFFERENCE': 'ABSOLUTE',
}

# Scalar channels whose result is clamped to 0..1 before it leaves the group:
# Math nodes do not clamp (ADD gives 1.1, SUBTRACT -0.7). Empty while Base
# Color is the only channel; every scalar channel that comes back is
# defined on 0..1 and belongs here.
_CLAMPED = frozenset()


# --- socket helpers ---------------------------------------------------------


class _EffectsProxy:
    """`effects` reached lazily. It imports this module, so a plain import at
    the top would be a cycle -- and the module is only ever touched while
    compiling, which is long after everything is registered."""

    def __getattr__(self, name):
        from . import effects

        return getattr(effects, name)


_effects = _EffectsProxy()


def socket(node, identifier, outputs=False):
    """Find a socket by identifier.

    Necessary because ShaderNodeMix names ten different sockets "A", "B" and
    "Factor"; looking them up by name silently returns the wrong one. Public
    because procedural.py builds nodes of its own and needs the same care.
    """
    collection = node.outputs if outputs else node.inputs
    for item in collection:
        if item.identifier == identifier:
            return item
    raise KeyError(f"{node.bl_idname} has no socket {identifier!r}")


# --- the node group ---------------------------------------------------------


def group_name(material):
    return GROUP_PREFIX + material.name


def find_group(material):
    return bpy.data.node_groups.get(group_name(material))


def ensure_group(material, document=None):
    """Return the material's stack group, creating it if needed."""
    name = group_name(material)
    group = bpy.data.node_groups.get(name)
    if group is None:
        group = bpy.data.node_groups.new(name, 'ShaderNodeTree')

    if document is None:
        document = material.paintr2
    _ensure_group_outputs(group, document)
    return group


def _output_sockets(group):
    """The group's output sockets, by name."""
    return {
        item.name: item
        for item in group.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'
    }


def _ensure_group_outputs(group, document):
    """One output socket per channel the document uses, in CHANNELS order.

    Returns True when the set changed, which is what tells the caller the
    material has to be wired again: removing a socket takes the link that fed
    from it with it, silently, and putting the socket back does not bring the
    link back (measured).

    Sockets are also *reordered* to CHANNELS order, because a re-added one
    lands at the end and a group whose outputs drift out of order is a group
    nobody can read.
    """
    from . import document as doc

    wanted = [
        (doc.channel_label(identifier), doc.channel_kind(identifier))
        for identifier in document.active_channels()
    ]
    wanted_names = [name for name, _kind in wanted]

    existing = _output_sockets(group)
    changed = False

    for name, item in existing.items():
        if name not in wanted_names:
            group.interface.remove(item)
            changed = True

    for name, kind in wanted:
        if name not in existing:
            group.interface.new_socket(
                name, in_out='OUTPUT', socket_type=_SOCKET_TYPES[kind]
            )
            changed = True

    # Put them back in CHANNELS order. `move` takes the index the item should
    # end up at, so walking the wanted list front to back settles it.
    current = _output_sockets(group)
    for index, name in enumerate(wanted_names):
        item = current.get(name)
        if item is not None:
            group.interface.move(item, index)

    return changed


# --- per-layer node construction --------------------------------------------


def _paint_base_color(group, layer, x, y):
    """A PAINT layer's Base Color: its image, and the alpha that carves it."""
    node = group.nodes.new('ShaderNodeTexImage')
    node.label = layer.name
    node.location = (x, y)
    node.image = layer.image
    # Paint data is authored in sRGB; leaving interpolation on Linear keeps
    # brush strokes from looking blocky when the canvas is zoomed in.
    return node.outputs["Color"], node.outputs["Alpha"]


def _flat_color(group, layer, channel, x, y, index=-1):
    """A channel's constant colour, as an RGB node."""
    node = group.nodes.new('ShaderNodeRGB')
    node.label = f"{layer.name} {channel.lower()}"
    node.location = (x, y)
    node.outputs[0].default_value = tuple(layer.channel(channel).color)
    _bind_flat(node, index, channel)
    return node.outputs[0], None


def _flat_value(group, layer, channel, x, y, index=-1):
    """A channel's constant scalar, as a Value node."""
    node = group.nodes.new('ShaderNodeValue')
    node.label = f"{layer.name} {channel.lower()}"
    node.location = (x, y)
    node.outputs[0].default_value = float(layer.channel(channel).value)
    _bind_flat(node, index, channel)
    return node.outputs[0], None


def _flat(group, layer, channel, x, y, index=-1):
    """Whichever of the two the channel's kind calls for."""
    from . import document as doc

    if doc.channel_kind(channel) == 'COLOR':
        return _flat_color(group, layer, channel, x, y, index)
    return _flat_value(group, layer, channel, x, y, index)


def _layer_channel_source(group, layer, channel, x, y, index=-1):
    """What `layer` contributes to `channel`, as (socket, alpha).

    `alpha` is not None only for a PAINT layer's Base Color -- that is the one
    place a layer's contribution varies across the surface on its own. `socket`
    is None when the layer has nothing to give, which the caller treats as
    "skip", never as an error.

    The dispatch reads as a table because it is one:

        PAINT       Base Color  -> image + alpha
                    others      -> nothing (the brush paints Base Color only)
        FILL        any         -> flat value
        PROCEDURAL  Base Color  -> the pattern, mixed between two colours
                    others      -> flat value (the pattern does not modulate
                                   them; put the pattern on the mask for that)
        CUSTOM      any         -> the node group's own output for it
    """
    from . import procedural

    kind = layer.layer_type

    if kind == 'PAINT':
        if channel != 'BASE_COLOR' or layer.image is None:
            return None, None
        return _paint_base_color(group, layer, x, y)

    if kind == 'FILL':
        return _flat(group, layer, channel, x, y, index)

    if kind == 'PROCEDURAL':
        # A Custom pattern is a node group with an output per channel, so it
        # is the one kind that can drive more than Base Color with a picture.
        if procedural.edits_in_graph(layer.procedural.kind):
            found = procedural.build_channel(
                group, layer.procedural, channel, x, y
            )
            return (found, None) if found is not None else (None, None)

        if channel == 'BASE_COLOR':
            color = procedural.build_color(group, layer.procedural, x, y)
            return (color, None) if color is not None else (None, None)

        return _flat(group, layer, channel, x, y, index)

    return None, None


def _multiply(group, first, second, x, y, label=""):
    """`first * second`, where either side may be a socket or a plain float."""
    if isinstance(first, float) and isinstance(second, float):
        return first * second

    node = group.nodes.new('ShaderNodeMath')
    node.operation = 'MULTIPLY'
    node.label = label
    node.location = (x, y)

    for index, value in enumerate((first, second)):
        if isinstance(value, float):
            node.inputs[index].default_value = value
        else:
            group.links.new(value, node.inputs[index])

    return node.outputs[0]


def _mask_socket(group, layer, cache, key, x, y):
    """The layer's mask as a 0..1 socket, or None. Built once per layer.

    Cached because a mask is a property of the *layer*, not of a channel: the
    same painted mask cuts Base Color, Roughness and Height alike. Without the
    cache a five-channel document would emit the same Image Texture node five
    times and the mask would be sampled five times per pixel.
    """
    if key in cache:
        return cache[key]

    factors = []
    if layer.mask_enabled:
        if layer.mask_image is not None:
            mask = group.nodes.new('ShaderNodeTexImage')
            mask.image = layer.mask_image
            mask.label = f"{layer.name} mask"
            mask.location = (x, y - 340.0)
            # A greyscale image feeding a Value socket: Blender averages the
            # RGB, which is exactly what a mask wants.
            factors.append(mask.outputs["Color"])

        # A procedural mask is the pattern's own 0..1 factor -- no colours
        # involved. Both kinds of mask may be on at once; they multiply, so a
        # painted mask can carve into a gradient.
        if layer.mask_procedural.enabled:
            from . import procedural

            generated = procedural.build_factor(
                group, layer.mask_procedural, x - 900.0, y - 340.0
            )
            if generated is not None:
                factors.append(generated)

    result = None
    if factors:
        result = factors[0]
        for index, extra in enumerate(factors[1:]):
            result = _multiply(
                group, result, extra, x, y - 500.0 - index * 150.0,
                f"{layer.name} mask",
            )

    # The mask's own effect stack. Cached with the mask, so five channels see
    # one built stack rather than five copies of it -- the same reason the
    # mask itself is cached.
    #
    # `result` may still be None here: a layer with no painted mask and no
    # procedural one, but with a pattern effect on its mask stack. That is a
    # generator used *as* a mask, and it is a thing people do.
    # A mask stack with no mask under it filters nothing, and `result` being
    # None is exactly that case. The interface hides such a stack and the
    # operators clear it, but the compiler refuses it on its own terms too.
    if result is not None and layer.mask_enabled and len(layer.mask_effects):
        result = _effects.apply_stack(
            group, layer.mask_effects, 'BASE_COLOR', result,
            x - 900.0, y - 800.0, is_mask=True,
        )

    cache[key] = result
    return result


# --- writing a value into a tree that is already built -----------------------
#
# A rebuild is the only way to change the *shape* of the stack, and it costs
# what the table above says. An opacity is not a shape: it is one number, on
# one node, per channel. Dragging it rebuilt the whole tree anyway -- 31 ms a
# write on fifty layers, measured, against 0.12 ms for the write itself.
#
# So the compiler names the node that carries each layer's opacity, and the
# property hook writes straight into it. The name is the record: it says which
# layer and which channel, so a tree restored by an undo is read correctly by
# a map built for a newer one -- the node called "PR2 opacity 3 BASE_COLOR" is
# that layer's opacity in whatever tree it is found in.
#
# What cannot be written is when the value has no node at all: a layer with a
# mask and an opacity of exactly 1.0 contributes no term, because a factor of
# 1.0 is dropped. Then the shape has to change and the fallback is a rebuild --
# once, at the start of the drag, after which the node exists.

_OPACITY_PREFIX = "PR2 opacity"
_FLAT_PREFIX = "PR2 flat"

# {node group name: {layer index: [node names]}}. Rebuilt by every compile.
_opacity_bindings = {}

# Filled during a compile and swapped into the map above at the end of it.
_binding_nodes = None
_binding_broken = None


def _opacity_node_name(index, channel):
    return f"{_OPACITY_PREFIX} {index} {channel}"


def _flat_node_name(index, channel):
    return f"{_FLAT_PREFIX} {index} {channel}"


def _bind_flat(node, index, channel):
    """Name the RGB or Value node holding a layer's flat value in `channel`.

    Simpler than the opacity binding and it needs no record: a flat channel is
    always exactly one node, the value never changes the shape of anything,
    and the name says which layer and which channel it belongs to -- so the
    writer can look it up on its own.
    """
    if index >= 0 and node is not None:
        node.name = _flat_node_name(index, channel)


def write_flat(material, index, channel, value):
    """Put a layer's flat colour or value straight into the compiled tree.

    True when it was done. This is the colour a Fill layer *is*, and dragging
    it through the picker used to rebuild the tree per event exactly as the
    opacity did.
    """
    if material is None:
        return False

    group = find_group(material)
    if group is None:
        return False

    node = group.nodes.get(_flat_node_name(index, channel))
    if node is None or node.bl_idname not in ('ShaderNodeRGB', 'ShaderNodeValue'):
        return False

    target = node.outputs[0]
    try:
        if node.bl_idname == 'ShaderNodeRGB':
            target.default_value = tuple(value)
        else:
            target.default_value = float(value)
    except (TypeError, ValueError):
        return False
    return True


def _bind_opacity(node, index, channel):
    """Name `node` as the carrier of layer `index`'s opacity in `channel`."""
    if _binding_nodes is None or index < 0 or node is None:
        return
    node.name = _opacity_node_name(index, channel)
    # Read back rather than assumed: Blender makes a name unique by appending
    # to it, and a name we did not check is a node we cannot find again.
    _binding_nodes.setdefault(index, []).append(node.name)


def _unbind_opacity(index):
    """Say that this layer's opacity has nowhere to live in this channel."""
    if _binding_broken is not None and index >= 0:
        _binding_broken.add(index)


def _opacity_socket(node):
    """The input on `node` that holds an opacity, or None if it holds none."""
    if node is None:
        return None
    if node.bl_idname == 'ShaderNodeMix':
        return socket(node, "Factor_Float")
    if node.bl_idname == 'ShaderNodeMath' and node.operation == 'MULTIPLY':
        return node.inputs[1]
    return None


def write_opacity(material, index, value):
    """Put a layer's opacity into the compiled tree without rebuilding it.

    True when every channel that layer writes to was updated. False means the
    tree cannot carry the value as it stands and the caller has to rebuild --
    which is the honest answer, not a failure: it is what happens the first
    time a masked layer moves off 1.0.
    """
    if material is None:
        return False

    group = find_group(material)
    if group is None:
        return False

    names = _opacity_bindings.get(group.name, {}).get(int(index))
    if not names:
        return False

    # Every socket is found before any is written: a half-applied opacity is
    # a layer that is one value in Base Color and another in Roughness.
    targets = []
    for name in names:
        target = _opacity_socket(group.nodes.get(name))
        if target is None or target.is_linked:
            return False
        targets.append(target)

    for target in targets:
        target.default_value = float(value)
    return True


def _layer_factor(group, layer, alpha_socket, mask, x, y,
                  index=-1, channel=""):
    """Return the mix factor for a layer in one channel: a socket or a float.

    The factor is the product of everything that can hide the layer:

        alpha (Base Color of a paint layer)  x  mask (if any)  x  opacity

    Terms that would multiply by 1.0 are dropped, so an ordinary opaque layer
    produces no Math nodes at all and the generated tree stays readable.

    `index` and `channel` are what the opacity is registered under, so it can
    be written later without a rebuild. Three outcomes, and the caller has to
    know which:

    * no other factor -- the opacity is returned as a bare float and lands on
      the Mix node the caller is about to build, which the caller names;
    * other factors and opacity below 1.0 -- it is an input of the Math node
      built here, which is named here;
    * other factors and opacity at exactly 1.0 -- it is dropped and is nowhere,
      so the layer is marked unwritable and the next change rebuilds.
    """
    factors = [item for item in (alpha_socket, mask) if item is not None]

    if layer.opacity < 1.0 or not factors:
        factors.append(float(layer.opacity))
    else:
        _unbind_opacity(index)

    result = factors[0]
    last = None
    for position, extra in enumerate(factors[1:]):
        result = _multiply(
            group, result, extra, x, y - 170.0 - position * 150.0,
            f"{layer.name} factor",
        )
        last = result

    # The opacity was appended last, so if anything was multiplied at all it
    # is the second input of the final Math node.
    if last is not None and layer.opacity < 1.0:
        _bind_opacity(last.node, index, channel)

    return result


def _mix_color(group, blend_mode, factor, below_socket, layer_socket, x, y):
    """Blend `layer_socket` over `below_socket` as colour. Returns the result."""
    node = group.nodes.new('ShaderNodeMix')
    node.data_type = 'RGBA'
    node.blend_type = blend_mode
    node.clamp_factor = True
    node.location = (x, y)

    factor_input = socket(node, "Factor_Float")
    if isinstance(factor, float):
        factor_input.default_value = factor
    else:
        group.links.new(factor, factor_input)

    group.links.new(below_socket, socket(node, "A_Color"))
    group.links.new(layer_socket, socket(node, "B_Color"))

    return socket(node, "Result_Color", outputs=True)


def _mix_value(group, blend_mode, factor, below_socket, layer_socket, x, y):
    """Blend two floats. `blend_mode` goes through Math, not through Mix.

    ShaderNodeMix in FLOAT mode ignores `blend_type` outright -- measured, all
    nine modes returned the same plain lerp. So a mode that means something
    for a float is applied by a Math node first, and the factor then mixes
    that result back over what was below. A mode with no float meaning (Hue,
    Overlay, Color Burn...) is a plain lerp, which is what Mix would have
    given anyway.
    """
    top = layer_socket
    operation = _SCALAR_BLEND.get(blend_mode)

    if operation is not None:
        math_node = group.nodes.new('ShaderNodeMath')
        math_node.operation = operation
        math_node.location = (x, y + 170.0)

        if operation == 'ABSOLUTE':
            # |below - layer|: two nodes, because ABSOLUTE takes one operand.
            difference = group.nodes.new('ShaderNodeMath')
            difference.operation = 'SUBTRACT'
            difference.location = (x - 180.0, y + 170.0)
            group.links.new(below_socket, difference.inputs[0])
            group.links.new(layer_socket, difference.inputs[1])
            group.links.new(difference.outputs[0], math_node.inputs[0])
        else:
            group.links.new(below_socket, math_node.inputs[0])
            group.links.new(layer_socket, math_node.inputs[1])

        top = math_node.outputs[0]

    node = group.nodes.new('ShaderNodeMix')
    node.data_type = 'FLOAT'
    node.clamp_factor = True
    node.location = (x, y)

    factor_input = socket(node, "Factor_Float")
    if isinstance(factor, float):
        factor_input.default_value = factor
    else:
        group.links.new(factor, factor_input)

    group.links.new(below_socket, socket(node, "A_Float"))
    group.links.new(top, socket(node, "B_Float"))

    return socket(node, "Result_Float", outputs=True)


def _bind_bare_opacity(result, factor, index, channel):
    """Name the Mix node a bare opacity landed on.

    `_layer_factor` returns a float only when nothing else multiplies into the
    factor, and then that float *is* the opacity -- it goes into the Mix node's
    Factor input as a default, which is exactly a value that can be written
    again later. Both `_mix_color` and `_mix_value` return that node's output,
    so the node is one step back from what the caller was handed.
    """
    if isinstance(factor, float):
        _bind_opacity(result.node, index, channel)


def _mix(group, channel, blend_mode, factor, below_socket, layer_socket, x, y,
         is_color=None):
    """Blend one layer over what is below it, in the right arithmetic.

    `is_color` overrides what the channel would say. A mask is a 0..1 factor
    whatever channel it happens to be cutting, and mixing it as a colour puts
    a Color socket into a Value input -- which Blender averages, so it looks
    like it works and is not what anybody asked for.
    """
    from . import document as doc

    if is_color is None:
        is_color = doc.channel_kind(channel) == 'COLOR'

    if is_color:
        return _mix_color(
            group, blend_mode, factor, below_socket, layer_socket, x, y
        )
    return _mix_value(
        group, blend_mode, factor, below_socket, layer_socket, x, y
    )


def _clamp(group, channel, value_socket, x, y):
    """Keep a scalar channel inside 0..1 on its way out of the group.

    Math nodes do not clamp: ADD gives 1.1 and SUBTRACT gives -0.7 (measured).
    A roughness of 1.1 reaching Principled is not a look, it is a bug.
    """
    if channel not in _CLAMPED:
        return value_socket

    node = group.nodes.new('ShaderNodeClamp')
    node.label = f"{channel.lower()} 0..1"
    node.location = (x, y)
    group.links.new(value_socket, node.inputs["Value"])
    node.inputs["Min"].default_value = 0.0
    node.inputs["Max"].default_value = 1.0
    return node.outputs["Result"]


def _backdrop(group, channel, x, y):
    """What a channel reads where no layer contributes."""
    from . import document as doc

    default = doc.channel_default(channel)
    if doc.channel_kind(channel) == 'COLOR':
        node = group.nodes.new('ShaderNodeRGB')
        node.outputs[0].default_value = tuple(default)
    else:
        node = group.nodes.new('ShaderNodeValue')
        node.outputs[0].default_value = float(default)

    node.label = f"{channel.lower()} backdrop"
    node.location = (x, y)
    return node.outputs[0]


# --- compilation ------------------------------------------------------------


def _composite(node_group, document, channel, indices, result, cursor, cache):
    """Blend `indices` over `result` in one channel, bottom to top.

    Returns (socket, cursor). `indices` are siblings at one level of the tree,
    top first, so the walk goes backwards. A GROUP recurses: its children are
    composited over whatever is underneath the group, and the group then blends
    that whole result back over the same thing with its own opacity and mask.
    That is what makes a group's opacity mean "how much of everything inside"
    rather than "how much of each layer in turn".

    A group that is hidden, empty, or whose children all vanish adds nothing
    and no node is emitted for it -- and a layer that does not write to this
    channel is exactly as absent as a hidden one.

    `cache` is the per-layer mask cache, shared across every channel of one
    compile: a mask belongs to the layer, not to the channel it happens to be
    cutting.
    """
    band = _band_y(channel)

    for index in reversed(indices):
        layer = document.layers[index]
        if not layer.visible:
            continue
        # Solo: everything outside what is being looked at is as absent as a
        # hidden layer, which is exactly what solo means.
        if not document.participates(index):
            continue

        y = band + _ROW_HEIGHT

        if layer.layer_type == 'GROUP':
            inner, cursor = _composite(
                node_group, document, channel,
                document.child_indices(index), result, cursor, cache,
            )
            if inner is result:
                continue

            mask = _mask_socket(node_group, layer, cache, index, cursor, y)
            factor = _layer_factor(node_group, layer, None, mask, cursor, y,
                                   index, channel)
            result = _mix(
                node_group, channel, layer.blend_mode, factor, result, inner,
                cursor + _COLUMN_WIDTH, band,
            )
            _bind_bare_opacity(result, factor, index, channel)
            cursor += _COLUMN_WIDTH * 2
            continue

        if not layer.writes(channel):
            continue

        if layer.layer_type == 'ADJUSTMENT':
            from . import adjustment as adj

            changed = adj.build(
                node_group, layer.adjustment, channel, result, cursor, y
            )
            if changed is None:
                # A kind with nothing to say about this channel -- a hue shift
                # on a roughness. Not an error; the channel passes through.
                continue

            mask = _mask_socket(node_group, layer, cache, index, cursor, y)
            factor = _layer_factor(node_group, layer, None, mask, cursor, y,
                                   index, channel)
            # Mixed back over the untransformed result rather than replacing
            # it: that is what makes the mask mean "where" and the opacity
            # mean "how much", the same two words they mean everywhere else.
            result = _mix(
                node_group, channel, 'MIX', factor, result, changed,
                cursor + _COLUMN_WIDTH, band,
            )
            _bind_bare_opacity(result, factor, index, channel)
            cursor += _COLUMN_WIDTH * 2
            continue

        color_socket, alpha_socket = _layer_channel_source(
            node_group, layer, channel, cursor, y, index
        )
        if color_socket is None:
            continue

        # The layer's own effect stack, before anything else sees it: a filter
        # on a layer transforms *that layer*, not what is under it. That is
        # the whole difference between an effect and an adjustment layer.
        color_socket = _effects.apply_stack(
            node_group, layer.effects, channel, color_socket,
            cursor - _COLUMN_WIDTH * 6, y + 400.0,
        )

        mask = _mask_socket(node_group, layer, cache, index, cursor, y)
        factor = _layer_factor(node_group, layer, alpha_socket, mask, cursor, y,
                               index, channel)
        result = _mix(
            node_group, channel, layer.blend_mode, factor, result, color_socket,
            cursor + _COLUMN_WIDTH, band,
        )
        _bind_bare_opacity(result, factor, index, channel)
        cursor += _COLUMN_WIDTH * 2

    return result, cursor


def _band_y(channel):
    """Where a channel's nodes are laid out, so the group reads as bands."""
    from . import document as doc

    try:
        return -doc.CHANNEL_IDS.index(channel) * _CHANNEL_BAND
    except ValueError:
        return 0.0


def compile_group(group, document):
    """Rebuild `group`'s contents from `document`. Returns the layer count."""
    global _binding_nodes, _binding_broken

    # Collected while the tree is built, then swapped in as the map a later
    # opacity write reads. Module state rather than a threaded argument: a
    # compile never runs inside another one, and the alternative is an extra
    # parameter on every function between here and `_layer_factor`.
    _binding_nodes, _binding_broken = {}, set()
    try:
        return _compile_group(group, document)
    finally:
        _opacity_bindings[group.name] = {
            index: names for index, names in _binding_nodes.items()
            if index not in _binding_broken
        }
        _binding_nodes = _binding_broken = None


def _compile_group(group, document):
    group.nodes.clear()

    output_node = group.nodes.new('NodeGroupOutput')

    # One mask per layer, shared by every channel that layer writes to.
    mask_cache = {}
    reach = _COLUMN_WIDTH

    from . import document as doc

    # Solo Mask replaces the stack rather than filtering it: what is wanted
    # is the mask itself, as grey, not the layer the mask is cutting.
    solo_mode, solo_index = document.solo_target()
    # Whichever channel is actually being looked at first -- Base Color in
    # every ordinary document, but a material that does not carry it must
    # still be able to show a mask.
    active = document.active_channels()
    solo_channel = active[0] if active else None

    for channel in active:
        band = _band_y(channel)
        backdrop = _backdrop(group, channel, 0.0, band)

        if solo_mode == 'MASK':
            result, cursor = backdrop, _COLUMN_WIDTH
            if channel == solo_channel:
                layer = document.layers[solo_index]
                mask = _mask_socket(
                    group, layer, mask_cache, solo_index, _COLUMN_WIDTH, band
                )
                if mask is not None:
                    # A float into a colour socket is grey in Blender, which
                    # is what a mask should look like.
                    result = mask
                cursor = _COLUMN_WIDTH * 2
        else:
            result, cursor = _composite(
                group, document, channel, document.root_indices(),
                backdrop, _COLUMN_WIDTH, mask_cache,
            )
        result = _clamp(group, channel, result, cursor, band)

        name = doc.channel_label(channel)
        target = output_node.inputs.get(name)
        if target is not None:
            group.links.new(result, target)

        reach = max(reach, cursor)

    output_node.location = (reach + _COLUMN_WIDTH, 0.0)

    return sum(1 for layer in document.layers if layer.visible)


# Nodes this module owns in the *material* tree. Named, so they can be found
# and rebuilt without touching anything the user put there.
_OWNED = {
    "group": "PaintR2 Stack",
    "bsdf": "PaintR2 Surface",
    "bump": "PaintR2 Bump",
    "normal_map": "PaintR2 Normal Map",
    "emission": "PaintR2 Unlit",
    "map_image": "PaintR2 Map Preview",
    # The fade stage: three nodes between the surface and the output that
    # darken a model without changing what it is. Built only into the
    # materials that are actually faded -- see `_wire_fade`.
    "fade_info": "PaintR2 Fade Info",
    "fade_dark": "PaintR2 Fade Dark",
    "fade_mix": "PaintR2 Fade",
    "output": "PaintR2 Output",
}

# Which of the nodes above each shader mode uses. Everything else we own is
# removed when the mode changes, so switching to Unlit and back does not leave
# a Principled and a Bump chain sitting in the tree feeding nothing.
# The fade stage's own nodes, kept whenever the fade is on whatever the mode.
_FADE_NODES = {"fade_info", "fade_dark", "fade_mix"}

_MODE_NODES = {
    # "bump" and "normal_map" are in no mode any more. They stay in
    # _OWNED so a file saved with a Normal or Height channel has them pruned.
    'PBR': {"group", "bsdf", "output"},
    'DIFFUSE': {"group", "bsdf", "output"},
    'UNLIT': {"group", "emission", "output"},
    # The stack is still compiled and the group node still there -- switching
    # to a map preview and back must not disturb a single layer.
    'MESH_MAP': {"group", "emission", "map_image", "output"},
}


def _owned_node(tree, key, idname, location):
    """Find our node by name, or make it. Never touches anything else."""
    name = _OWNED[key]
    node = tree.nodes.get(name)
    if node is None or node.bl_idname != idname:
        if node is not None:
            tree.nodes.remove(node)
        node = tree.nodes.new(idname)
        node.name = name
        node.label = name
    node.location = location
    return node


def _is_untouched_default(tree):
    """True for the tree Blender makes on `use_nodes = True`, unmodified.

    Exactly a Principled feeding a Material Output, and nothing else. Clearing
    *that* is not destroying anyone's work -- nobody made it, and leaving it
    means a second BSDF and a second output node sitting beside ours, which is
    a material with two answers to "what is the surface".

    Anything else -- one extra node, one moved link -- is the user's, and is
    left exactly as it is.
    """
    nodes = list(tree.nodes)
    if len(nodes) != 2:
        return False

    kinds = {node.bl_idname for node in nodes}
    return kinds == {'ShaderNodeBsdfPrincipled', 'ShaderNodeOutputMaterial'}


def _surface_output(tree):
    """The output node our surface should drive.

    Ours if it exists; otherwise whichever output the material already has,
    because a material with an output already has an answer and adding a
    second one would make the question ambiguous. Only when there is none at
    all do we make one.
    """
    ours = tree.nodes.get(_OWNED["output"])
    if ours is not None and ours.bl_idname == 'ShaderNodeOutputMaterial':
        return ours

    existing = [
        node for node in tree.nodes
        if node.bl_idname == 'ShaderNodeOutputMaterial'
    ]
    if existing:
        # The active one if Blender marks one, else the first.
        return next((node for node in existing if node.is_active_output),
                    existing[0])

    return _owned_node(tree, "output", 'ShaderNodeOutputMaterial', (220.0, 0.0))


# Which materials are currently being drawn faded, by name. Written by
# `isolation.sync_fade`, read here. A set rather than a scene-wide flag,
# because of the thing that made the first version of this look wrong:
#
#   **The material being painted is compiled exactly as it would be with
#   the fade switched off.**
#
# Not "with the factor set to one" -- with the nodes absent. A stage that is
# there and inert is still a difference between the model being painted and
# the same model with the option off, and the first version of this had one
# that showed.
_faded_materials = set()


def set_faded_materials(names):
    """Name the materials that should carry the fade stage. Returns the set.

    The caller compares the answer with what each material already has and
    recompiles the ones that changed -- see `isolation.sync_fade`.
    """
    global _faded_materials

    _faded_materials = set(names)
    return _faded_materials


def carries_fade(material):
    """Whether `material`'s tree currently has the fade stage built into it.

    Read off the tree rather than recorded beside it: the tree is what is
    being drawn, and a second record is a second answer that can be wrong.
    """
    if material is None or material.node_tree is None:
        return False
    return _OWNED["fade_mix"] in material.node_tree.nodes


def _fade_is_on(material):
    """Whether this material should carry the fade stage.

    Three conditions. The window has to be open -- the fade is a way of
    looking at a set while painting it, not a property of the material, and
    one left with a Transparent BSDF in it renders differently in a file
    nobody opened PaintR2 in. The option has to be on. And this particular
    material has to be one of the faded ones.
    """
    from . import session

    if not session.state.is_open:
        return False

    if material is None or material.name not in _faded_materials:
        return False

    scene = getattr(bpy.context, "scene", None)
    props = getattr(scene, "paintr2", None) if scene is not None else None
    if props is None:
        return False
    return bool(props.display.fade_inactive)


def _wire_fade(tree, surface):
    """Put the fade stage between `surface` and the output. Returns the socket.

    The model is **darkened, not made see-through**. Transparency was
    the first answer and it was the wrong one three separate ways: a material
    carrying a Transparent BSDF is drawn down a different path even at alpha
    1.0, blended transparency does not write depth, and a see-through model
    shows its own interior. Mixing towards black instead is opaque, so none
    of that arises -- there is no sorting to get wrong.

    `Object Info -> Alpha` is what makes this per *object* rather than per
    material: two models sharing one material would still darken
    independently, because the alpha is read off the object being drawn.
    Writing it as a node instead of a material property is what buys that.
    """
    info = _owned_node(tree, "fade_info", 'ShaderNodeObjectInfo', (-360.0, -320.0))
    # Black, and an Emission rather than a Transparent BSDF: both branches of
    # the mix have to be opaque or the material becomes a transparent one
    # again. Emission at zero is the cheapest shader that renders as black.
    dark = _owned_node(
        tree, "fade_dark", 'ShaderNodeEmission', (-120.0, -260.0)
    )
    _set_input(dark, "Color", (0.0, 0.0, 0.0, 1.0))
    _set_input(dark, "Strength", 1.0)

    mix = _owned_node(tree, "fade_mix", 'ShaderNodeMixShader', (60.0, 0.0))

    # By index, not by name. A Mix Shader's factor socket is *called* "Factor"
    # in 5.2 and its identifier is still "Fac"; `inputs["Fac"]` happens to
    # match on the identifier, which is a coincidence worth not depending on.
    # The three sockets are Fac, Shader, Shader, in that order, always.
    tree.links.new(info.outputs["Alpha"], mix.inputs[0])
    tree.links.new(dark.outputs["Emission"], mix.inputs[1])
    tree.links.new(surface, mix.inputs[2])
    return mix.outputs["Shader"]


def _prune(tree, mode, fade=False):
    """Remove the nodes we own that this mode has no use for.

    Without this, switching to Unlit would leave the Principled, the Bump and
    the Normal Map behind: unlinked, invisible in the render, and confusing to
    anyone who opens the tree to see what PaintR2 built.
    """
    keep = set(_MODE_NODES.get(mode, _MODE_NODES['PBR']))
    if fade:
        keep |= _FADE_NODES
    for key, name in _OWNED.items():
        if key in keep or key == "output":
            continue
        node = tree.nodes.get(name)
        if node is not None:
            tree.nodes.remove(node)


def _clear_our_links(tree, output):
    """Drop the links between our own nodes, and only those.

    `output` joins the set because it is the node we drive even when it is the
    user's own -- but only that one. A link the user drew from somewhere else
    into somewhere else is not ours to remove.
    """
    ours = {name for name in _OWNED.values() if tree.nodes.get(name) is not None}
    ours.add(output.name)
    for link in list(tree.links):
        if link.from_node.name in ours and link.to_node.name in ours:
            tree.links.remove(link)


def _channel_output(group_node, document, channel):
    """The group socket carrying `channel`, or None when it is switched off."""
    from . import document as doc

    if not document.uses(channel):
        return None
    return group_node.outputs.get(doc.channel_label(channel))


def ensure_material_wiring(material, group):
    """Wire the group into the material, the way the shader mode asks for.

    Rebuilt rather than left alone, every time the channel set could have
    changed: removing an output socket from the group takes the link that fed
    from it with it, silently, and putting the socket back does not bring the
    link back (measured). So "the group node is already there, leave it" is a
    shortcut that stops being true the moment a channel is switched.

    What is *not* rebuilt is everything else in the material. The nodes this
    module owns carry `PaintR2 ` names; anything else the user added stays
    exactly where it is -- which is the "outside the group is yours" half of
    the contract, and the reason the old `tree.nodes.clear()` is gone.

    Four modes, and only the surface differs -- the group above is compiled
    the same way for all of them, so switching modes never touches a layer,
    a mask or a channel value:

        PBR       Base Color -> Principled. The look the material has.
        DIFFUSE   Base Color -> Principled, with Metallic 0 and Roughness 1.
                  Lit enough to read the form, flat enough that the colour
                  is still the colour.
        UNLIT     Base Color -> Emission -> output. No light touches it.
        MESH_MAP  a baked mesh map -> Emission -> output.
    """
    material.use_nodes = True
    tree = material.node_tree
    document = material.paintr2
    mode = getattr(document, "shader_mode", 'PBR')

    # The one case where clearing is right: the untouched two-node tree
    # Blender hands out with `use_nodes`. Leaving it would put a second BSDF
    # and a second output beside ours.
    if _is_untouched_default(tree):
        tree.nodes.clear()

    group_node = _owned_node(tree, "group", 'ShaderNodeGroup', (-620.0, 0.0))
    group_node.node_tree = group
    output = _surface_output(tree)

    fade = _fade_is_on(material)
    _prune(tree, mode, fade)
    _clear_our_links(tree, output)

    if mode == 'MESH_MAP':
        surface = _wire_mesh_map(tree, document)
    elif mode == 'UNLIT':
        surface = _wire_emission(tree, document, group_node)
    else:
        surface = _wire_principled(tree, document, group_node, mode)

    if surface is not None and fade:
        surface = _wire_fade(tree, surface)

    if surface is not None:
        tree.links.new(surface, output.inputs["Surface"])
    return group_node


def _wire_principled(tree, document, group_node, mode):
    """PBR and Diffuse. Returns the socket to drive the output with."""
    bsdf = _owned_node(tree, "bsdf", 'ShaderNodeBsdfPrincipled', (-120.0, 0.0))

    source = _channel_output(group_node, document, 'BASE_COLOR')
    if source is not None:
        tree.links.new(source, bsdf.inputs["Base Color"])

    if mode == 'DIFFUSE':
        # Written rather than linked, so the channels keep their values in the
        # group and come back the instant the mode does. A rough, non-metal,
        # unrelieved surface is the one that shows form without recolouring
        # anything: a specular highlight is a white patch over the paint.
        _set_input(bsdf, "Metallic", 0.0)
        _set_input(bsdf, "Roughness", 1.0)
        _set_input(bsdf, "IOR", 1.0)
        return bsdf.outputs["BSDF"]

    # Principled's own defaults, written back: there is no Roughness or
    # Metallic channel to drive them, and Diffuse may have left its
    # values on this node.
    _set_input(bsdf, "Metallic", 0.0)
    _set_input(bsdf, "Roughness", 0.5)
    _set_input(bsdf, "IOR", 1.5)
    return bsdf.outputs["BSDF"]


def _wire_emission(tree, document, group_node):
    """Unlit. Returns the socket to drive the output with."""
    emission = _owned_node(tree, "emission", 'ShaderNodeEmission', (-120.0, 0.0))
    _set_input(emission, "Strength", 1.0)

    source = _channel_output(group_node, document, 'BASE_COLOR')
    if source is not None:
        tree.links.new(source, emission.inputs["Color"])
    else:
        _set_input(emission, "Color", (0.5, 0.5, 0.5, 1.0))

    return emission.outputs["Emission"]


def _wire_mesh_map(tree, document):
    """A baked map straight onto the surface, unlit. Returns the socket.

    Deliberately not through the stack: the question a map preview answers is
    "did that bake come out right", and an answer with layers, lighting or a
    view transform's opinion in it is not an answer.

    With no map chosen the surface goes flat grey rather than black -- black
    reads as a broken material, grey reads as "nothing selected".
    """
    emission = _owned_node(tree, "emission", 'ShaderNodeEmission', (-120.0, 0.0))
    _set_input(emission, "Strength", 1.0)

    image = getattr(document, "preview_map", None)
    if image is None:
        _set_input(emission, "Color", (0.5, 0.5, 0.5, 1.0))
        return emission.outputs["Emission"]

    node = _owned_node(tree, "map_image", 'ShaderNodeTexImage', (-420.0, 0.0))
    node.image = image
    # Mesh maps are data, not pictures: they are baked and stored Non-Color,
    # and reading one back through sRGB would show a map nobody baked.
    node.interpolation = 'Closest' if max(image.size) <= 64 else 'Linear'
    tree.links.new(node.outputs["Color"], emission.inputs["Color"])

    return emission.outputs["Emission"]


def _set_input(node, name, value):
    socket = node.inputs.get(name)
    if socket is not None:
        try:
            socket.default_value = value
        except (AttributeError, TypeError, ValueError):
            pass


def compile_material(material):
    """Rebuild the node group for `material`. Safe to call on anything.

    The material's own wiring is rebuilt every time rather than only when the
    group node is missing. It has to be: a channel switched off removes a
    group output, and that silently takes the link feeding Principled from it;
    switching the channel back on does not bring the link back. Rebuilding is
    five node lookups and a handful of links, which is nothing against the
    tree that was just generated.
    """
    if material is None or not hasattr(material, "paintr2"):
        return False

    document = material.paintr2
    if not len(document.layers):
        return False

    global _last_compile_seconds

    started = time.perf_counter()
    group = ensure_group(material, document)
    compile_group(group, document)
    ensure_material_wiring(material, group)
    # Measured, not assumed: the debounce below sizes itself off this, so a
    # 100-layer stack on a slow machine and a 5-layer one on a fast one both
    # end up spending the same share of the clock rebuilding.
    _last_compile_seconds = time.perf_counter() - started
    return True


# --- deferred compiles ------------------------------------------------------
#
# Measured on this machine, one full compile of a plain Fill stack:
#
#      10 layers      2.1 ms
#      25 layers      9.4 ms
#      50 layers     34.2 ms
#     100 layers    123.0 ms
#
# Superlinear, and the cost is Blender's own `nodes.new` -- 500 Mix nodes take
# 0.44 s of a 0.62 s compile, and there is no version of this that makes fewer
# of them. At 100 layers, dragging an opacity slider asks for 123 ms of work
# per RNA write, which is a slider that moves eight times a second.
#
# So the property hooks ask for a compile instead of doing one, and the
# request is coalesced onto the next timer tick. Only the hooks: an operator
# fires once per click and has always been affordable.
#
# Two things came out of measuring that again. One: the interval cannot
# be a constant. At ten layers 1/30 s was fine -- 2 ms of work per 33 ms -- and
# at fifty it was hopeless, because 34 ms of work does not fit in 33 ms and the
# requests arrived faster than they could be served. `debounce_interval()`
# sizes itself off the last compile instead. Two: most of what a drag writes is
# not a shape at all, and those do not come through here any more -- see
# `write_opacity` and `write_flat`.
#
# Off by default, and switched on only while the window is open. A deferred
# compile needs `bpy.app.timers` to actually run, and in a background Blender
# executing a script they do not fire at all -- so outside the window the
# request *is* the compile, which is what every test and every headless bake
# already relies on.

DEBOUNCE_SECONDS = 1.0 / 30.0

# The longest the shader is allowed to lag behind the slider. Past this the
# preview stops reading as "following the drag" and starts reading as broken,
# so a stack heavy enough to need more than a third of a second gets a stale
# preview rather than an unusable window.
DEBOUNCE_MAX = 1.0 / 3.0

# What share of the clock a drag may spend rebuilding. A fixed 1/30 s interval
# was fine at ten layers (2 ms of work per 33 ms) and hopeless at fifty
# (34 ms of work per 33 ms): the requests arrived faster than they could be
# served, so every frame carried a full rebuild and the interface stopped
# answering. Measured, both numbers are in the table above.
COMPILE_BUDGET = 0.33

defer_enabled = False
_pending = set()
_timer_armed = False

# How long the last rebuild took, in seconds. Zero until one has run.
_last_compile_seconds = 0.0


def debounce_interval():
    """How long to wait before serving the requests that have piled up.

    Long enough that the rebuild it triggers is only `COMPILE_BUDGET` of the
    time until the next one -- so the window keeps two thirds of every second
    for drawing and answering the mouse, whatever the stack costs.
    """
    wanted = _last_compile_seconds / COMPILE_BUDGET if _last_compile_seconds else 0.0
    return min(DEBOUNCE_MAX, max(DEBOUNCE_SECONDS, wanted))


def _run_pending():
    global _timer_armed

    _timer_armed = False
    flush()
    return None


def request_compile(material):
    """Ask for `material` to be rebuilt, soon. Compiles now when it must.

    "Soon" is the next timer tick, and every request made before then folds
    into one compile. That is the whole of the debounce: a slider dragged
    across a hundred RNA writes produces one rebuild per frame, not a hundred.
    """
    global _timer_armed

    if material is None:
        return False

    if not defer_enabled or bpy.app.background:
        return compile_material(material)

    _pending.add(material.name)

    if not _timer_armed:
        _timer_armed = True
        try:
            bpy.app.timers.register(
                _run_pending, first_interval=debounce_interval()
            )
        except (AttributeError, ValueError):
            # No timer to hang it on: do the work rather than lose it.
            _timer_armed = False
            return flush()

    return True


def flush():
    """Compile everything waiting. Called before anything reads the tree.

    A bake, an export or a window closing must not see a stack one drag out
    of date, and none of them can wait for a timer.
    """
    if not _pending:
        return False

    names = sorted(_pending)
    _pending.clear()

    done = False
    for name in names:
        material = bpy.data.materials.get(name)
        if material is not None and compile_material(material):
            done = True
    return done


def set_deferred(enabled):
    """Turn deferral on or off. Off flushes, so nothing is left waiting."""
    global defer_enabled

    defer_enabled = bool(enabled)
    if not defer_enabled:
        flush()


def compile_all():
    """Recompile every material that has a stack. Used after loading a file."""
    count = 0
    for material in bpy.data.materials:
        if compile_material(material):
            count += 1
    return count

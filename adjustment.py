# SPDX-License-Identifier: GPL-3.0-or-later
"""Adjustment layers: a layer that changes what is under it rather than adding.

Every other layer type answers "what do you contribute". This one answers
"what do you do to what is already there" -- so the compiler treats it as a
transform on the running result rather than as a source to blend in.

Where it reaches is decided by two things it shares with every other layer:

* its **mask** says *where* -- an unmasked adjustment covers everything, a
  masked one only bites where the mask is open;
* its **channels** say *what* -- an adjustment with only Roughness ticked
  leaves the colour alone.

And its position says *how far down*: the compiler walks group by group, so an
adjustment inside a folder only ever sees that folder's result. It costs
nothing because `_composite` already recurses per group.

Adding a kind
-------------
The same three edits as procedural.py, in this file:

1. an entry in `KINDS`,
2. its parameter names in `PARAMS`,
3. a builder in `_BUILDERS`, taking the socket below and returning the
   transformed one.

A builder that has nothing to say for a channel returns None, and the layer
contributes nothing there rather than raising -- a Hue shift on a normal map
is exactly that case.
"""

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty
from bpy.types import PropertyGroup

from . import nodegen


KINDS = (
    ('HSV', "Hue / Saturation", "Shift the hue, saturation and brightness"),
    ('BRIGHT_CONTRAST', "Bright / Contrast", "Lift or crush the values"),
    ('LEVELS', "Levels", "Remap an input range onto an output range"),
    ('GAMMA', "Gamma", "Bend the midtones"),
    ('CLAMP', "Clamp", "Hold the values inside a range"),
    ('POSTERIZE', "Posterize", "Quantise into a fixed number of steps"),
    ('INVERT', "Invert", "Flip the values"),
)

PARAMS = {
    'HSV': ("hue", "saturation", "value"),
    'BRIGHT_CONTRAST': ("bright", "contrast"),
    'LEVELS': ("in_min", "in_max", "out_min", "out_max"),
    'GAMMA': ("gamma",),
    'CLAMP': ("clamp_min", "clamp_max"),
    'POSTERIZE': ("steps",),
    'INVERT': (),
}

# Kinds that only mean something for a colour. Asked before a builder is
# called, so the panel and the compiler agree without either of them guessing.
COLOR_ONLY = frozenset({'HSV'})


def _recompile(self, context):
    owner = self.id_data
    if isinstance(owner, bpy.types.Material):
        nodegen.request_compile(owner)


class PR2_Adjustment(PropertyGroup):
    """The parameters of one adjustment layer."""

    kind: EnumProperty(
        name="Adjustment",
        items=KINDS,
        default='HSV',
        update=_recompile,
    )

    # --- hue / saturation ----------------------------------------------------
    # Blender's own ranges, so the numbers mean what they mean in its node.

    hue: FloatProperty(
        name="Hue",
        description="0.5 leaves the hue alone; away from it rotates the wheel",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR', update=_recompile,
    )
    saturation: FloatProperty(
        name="Saturation",
        default=1.0, min=0.0, soft_max=2.0, update=_recompile,
    )
    value: FloatProperty(
        name="Value",
        description="Brightness. 1.0 leaves it alone",
        default=1.0, min=0.0, soft_max=2.0, update=_recompile,
    )

    # --- bright / contrast ---------------------------------------------------

    bright: FloatProperty(
        name="Bright", default=0.0, soft_min=-1.0, soft_max=1.0,
        update=_recompile,
    )
    contrast: FloatProperty(
        name="Contrast", default=0.0, soft_min=-1.0, soft_max=1.0,
        update=_recompile,
    )

    # --- levels --------------------------------------------------------------

    in_min: FloatProperty(
        name="Input Min", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )
    in_max: FloatProperty(
        name="Input Max", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )
    out_min: FloatProperty(
        name="Output Min", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )
    out_max: FloatProperty(
        name="Output Max", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )

    # --- gamma ---------------------------------------------------------------

    gamma: FloatProperty(
        name="Gamma", default=1.0, min=0.05, soft_max=5.0, update=_recompile,
    )

    # --- clamp ---------------------------------------------------------------

    clamp_min: FloatProperty(
        name="Min", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )
    clamp_max: FloatProperty(
        name="Max", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        update=_recompile,
    )

    # --- posterize -----------------------------------------------------------

    steps: IntProperty(
        name="Steps",
        description="How many distinct values are left. Two is a hard "
                    "threshold, which is what a mask most often wants",
        default=4, min=2, max=64, update=_recompile,
    )


# --- node construction -------------------------------------------------------
#
# Every builder takes the socket carrying what is below and returns the
# transformed socket. `is_color` says which arithmetic to use; a channel that
# a kind cannot express returns None.


def _build_hsv(group, adj, below, is_color, x, y):
    if not is_color:
        return None

    node = group.nodes.new('ShaderNodeHueSaturation')
    node.label = "Hue / Saturation"
    node.location = (x, y)
    node.inputs["Hue"].default_value = adj.hue
    node.inputs["Saturation"].default_value = adj.saturation
    node.inputs["Value"].default_value = adj.value
    node.inputs["Fac"].default_value = 1.0
    group.links.new(below, node.inputs["Color"])
    return node.outputs["Color"]


def _build_bright_contrast(group, adj, below, is_color, x, y):
    if is_color:
        node = group.nodes.new('ShaderNodeBrightContrast')
        node.label = "Bright / Contrast"
        node.location = (x, y)
        node.inputs["Bright"].default_value = adj.bright
        node.inputs["Contrast"].default_value = adj.contrast
        group.links.new(below, node.inputs["Color"])
        return node.outputs["Color"]

    # The same arithmetic Blender's node does, in Math nodes:
    #   out = (value - 0.5) * (1 + contrast) + 0.5 + bright
    centred = _math(group, 'SUBTRACT', below, 0.5, x, y)
    scaled = _math(group, 'MULTIPLY', centred, 1.0 + adj.contrast, x + 180.0, y)
    return _math(group, 'ADD', scaled, 0.5 + adj.bright, x + 360.0, y)


def _build_levels(group, adj, below, is_color, x, y):
    node = group.nodes.new('ShaderNodeMapRange')
    node.data_type = 'FLOAT_VECTOR' if is_color else 'FLOAT'
    node.interpolation_type = 'LINEAR'
    node.clamp = True
    node.label = "Levels"
    node.location = (x, y)

    if is_color:
        # The vector form takes four vectors; the same number in all three
        # components is "do this to every channel of the colour".
        group.links.new(below, nodegen.socket(node, "Vector"))
        for name, value in (("From_Min_FLOAT3", adj.in_min),
                            ("From_Max_FLOAT3", adj.in_max),
                            ("To_Min_FLOAT3", adj.out_min),
                            ("To_Max_FLOAT3", adj.out_max)):
            nodegen.socket(node, name).default_value = (value, value, value)
        return nodegen.socket(node, "Vector", outputs=True)

    group.links.new(below, nodegen.socket(node, "Value"))
    nodegen.socket(node, "From Min").default_value = adj.in_min
    nodegen.socket(node, "From Max").default_value = adj.in_max
    nodegen.socket(node, "To Min").default_value = adj.out_min
    nodegen.socket(node, "To Max").default_value = adj.out_max
    return nodegen.socket(node, "Result", outputs=True)


def _build_gamma(group, adj, below, is_color, x, y):
    if is_color:
        node = group.nodes.new('ShaderNodeGamma')
        node.label = "Gamma"
        node.location = (x, y)
        node.inputs["Gamma"].default_value = adj.gamma
        group.links.new(below, node.inputs["Color"])
        return node.outputs["Color"]

    return _math(group, 'POWER', below, adj.gamma, x, y)


def _build_invert(group, adj, below, is_color, x, y):
    if is_color:
        node = group.nodes.new('ShaderNodeInvert')
        node.label = "Invert"
        node.location = (x, y)
        node.inputs["Fac"].default_value = 1.0
        group.links.new(below, node.inputs["Color"])
        return node.outputs["Color"]

    node = group.nodes.new('ShaderNodeMath')
    node.operation = 'SUBTRACT'
    node.label = "Invert"
    node.location = (x, y)
    node.inputs[0].default_value = 1.0
    group.links.new(below, node.inputs[1])
    return node.outputs[0]


def _build_clamp(group, adj, below, is_color, x, y):
    """Hold the values inside a range. On a colour, every component alike."""
    low = min(adj.clamp_min, adj.clamp_max)
    high = max(adj.clamp_min, adj.clamp_max)

    if is_color:
        node = group.nodes.new('ShaderNodeMapRange')
        node.data_type = 'FLOAT_VECTOR'
        node.clamp = True
        node.label = "Clamp"
        node.location = (x, y)
        group.links.new(below, nodegen.socket(node, "Vector"))
        # Mapping low..high onto low..high with clamping on: the values inside
        # the range come out untouched and the ones outside are held at the
        # ends. One node, and no arithmetic to get wrong.
        for name, value in (("From_Min_FLOAT3", low), ("From_Max_FLOAT3", high),
                            ("To_Min_FLOAT3", low), ("To_Max_FLOAT3", high)):
            nodegen.socket(node, name).default_value = (value, value, value)
        return nodegen.socket(node, "Vector", outputs=True)

    node = group.nodes.new('ShaderNodeClamp')
    node.label = "Clamp"
    node.location = (x, y)
    node.inputs["Min"].default_value = low
    node.inputs["Max"].default_value = high
    group.links.new(below, node.inputs["Value"])
    return node.outputs["Result"]


def _build_posterize(group, adj, below, is_color, x, y):
    """Quantise into `steps` levels: floor(v * n) / (n - 1), clamped.

    Two steps is a hard threshold, which is the reason this is here at all --
    a painted mask with soft edges becomes a decision.
    """
    steps = max(2, int(adj.steps))

    if is_color:
        # Colour goes through the same arithmetic, component-wise, in Vector
        # Math nodes. Separating and rejoining would be three more nodes for
        # the same answer.
        scaled = _vector_math(group, 'SCALE', below, float(steps), x, y)
        floored = _vector_math(group, 'FLOOR', scaled, None, x + 180.0, y)
        return _vector_math(
            group, 'SCALE', floored, 1.0 / float(steps - 1), x + 360.0, y
        )

    scaled = _math(group, 'MULTIPLY', below, float(steps), x, y)
    floored = _math(group, 'FLOOR', scaled, 0.0, x + 180.0, y)
    quantised = _math(
        group, 'DIVIDE', floored, float(steps - 1), x + 360.0, y
    )
    node = group.nodes.new('ShaderNodeClamp')
    node.label = "Posterize"
    node.location = (x + 540.0, y)
    group.links.new(quantised, node.inputs["Value"])
    return node.outputs["Result"]


def _vector_math(group, operation, socket_in, constant, x, y):
    """One Vector Math node. `constant` is the scale, or None for unary ops."""
    node = group.nodes.new('ShaderNodeVectorMath')
    node.operation = operation
    node.location = (x, y)
    group.links.new(socket_in, node.inputs[0])
    if operation == 'SCALE' and constant is not None:
        node.inputs["Scale"].default_value = float(constant)
    return node.outputs["Vector"]


def _math(group, operation, socket_in, constant, x, y):
    """`socket_in <op> constant`, as one Math node."""
    node = group.nodes.new('ShaderNodeMath')
    node.operation = operation
    node.location = (x, y)
    group.links.new(socket_in, node.inputs[0])
    node.inputs[1].default_value = float(constant)
    return node.outputs[0]


_BUILDERS = {
    'HSV': _build_hsv,
    'BRIGHT_CONTRAST': _build_bright_contrast,
    'LEVELS': _build_levels,
    'GAMMA': _build_gamma,
    'CLAMP': _build_clamp,
    'POSTERIZE': _build_posterize,
    'INVERT': _build_invert,
}


def applies_to(kind, channel, is_color=None):
    """Whether `kind` has anything to say about `channel`.

    Asked by the panel as well as by the compiler, so a control that would do
    nothing is not drawn in the first place. `is_color` is the same override
    `build` takes -- a mask asks with it False.
    """
    from . import document as doc

    if is_color is None:
        is_color = doc.channel_kind(channel) == 'COLOR'

    if kind in COLOR_ONLY:
        return is_color
    return True


def build(group, adj, channel, below, x, y, is_color=None):
    """The transformed socket, or None when this kind means nothing here.

    `is_color` overrides what the channel would say, for the one caller that
    knows better: a mask is a 0..1 factor whatever channel it is cutting, and
    building colour nodes for it is how a Hue shift ends up applied to an
    opacity.
    """
    from . import document as doc

    if is_color is None:
        is_color = doc.channel_kind(channel) == 'COLOR'

    if not applies_to(adj.kind, channel, is_color):
        return None

    builder = _BUILDERS.get(adj.kind)
    if builder is None:
        return None

    return builder(group, adj, below, is_color, x, y)


# --- helpers the interface uses ----------------------------------------------


def param_names(adj):
    return list(PARAMS.get(adj.kind, ()))


def kind_label(kind):
    for identifier, label, _desc in KINDS:
        if identifier == kind:
            return label
    return kind.replace("_", " ").title()


CLASSES = (PR2_Adjustment,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

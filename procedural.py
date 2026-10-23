# SPDX-License-Identifier: GPL-3.0-or-later
"""Generated patterns: layer sources and masks that are computed, not painted.

A procedural is a small parameter block that compiles into shader nodes. The
same block serves two jobs, which is the whole point of it being one type:

* as a **layer source** it produces a colour, so the layer draws the pattern;
* as a **mask** it produces a factor in 0..1, so the pattern decides where the
  layer below it shows.

Nothing here paints. The parameters live in the .blend like every other part
of the document, and the node tree is derived from them on every recompile --
so a procedural can be re-edited forever, unlike a gradient baked into pixels.

Adding a kind
-------------
Three edits, all in this file:

1. an entry in `KINDS`,
2. its parameter names in `PARAMS` (the panel is built from that list, in that
   order, straight off the RNA definitions -- no layout is typed anywhere),
3. a builder in `_BUILDERS` returning the 0..1 factor socket.

A kind that produces a colour directly rather than a factor mixed between two
colours adds a fourth: an entry in `_COLOR_BUILDERS`. `CUSTOM` is the only one,
because a node group already hands back a colour.

Everything else -- the properties panel, the row icon, the compiler, the mask
plumbing -- already goes through those.

Two kinds exist. `GRADIENT` is a ramp in object space, aimed by two angles
rather than by a chosen axis. `CUSTOM` is the escape hatch: an empty node group
of your own, referenced by the compiled tree rather than copied into it, so
whatever you wire to its output is what the layer (or the mask) becomes.

`CUSTOM` is the one kind whose picture does not come from `PARAMS`. Its
parameter *is* the node group, and that is edited in the shader graph -- so the
panel shows a button that opens it instead of a list of numbers.
"""

import math

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup

from . import nodegen


KINDS = (
    ('GRADIENT', "Gradient", "A linear ramp across the object, at any angle"),
    ('NOISE', "Noise", "Fractal noise in object space, for wear and dirt"),
    ('CHECKER', "Checker", "A three-dimensional checkerboard"),
    ('CUSTOM', "Custom", "An empty node group you wire up yourself"),
)

# What the properties panel shows for each kind, in order. Labels, ranges and
# tooltips are not here: `window._row` reads them off the RNA definition, the
# same way the brush popovers do.
PARAMS = {
    'GRADIENT': ("angle", "tilt", "start", "end", "interpolation", "reverse"),
    'NOISE': ("scale", "detail", "roughness", "distortion", "contrast",
              "reverse"),
    'CHECKER': ("scale", "reverse"),
    # Nothing to type: a custom pattern is edited in the shader graph, and the
    # panel offers the button that opens it rather than a parameter list.
    'CUSTOM': (),
}

# Extra parameters that only make sense when the procedural *is* the layer.
# A mask has no colour -- it is the factor itself.
COLOR_PARAMS = {
    'GRADIENT': ("color_a", "color_b"),
    'NOISE': ("color_a", "color_b"),
    'CHECKER': ("color_a", "color_b"),
    # A custom group already produces a colour; there is nothing to mix.
    'CUSTOM': (),
}

# The socket a custom group hands back for the layer's colour, and the one a
# mask reads. One socket, both roles: as a layer it is the colour, as a mask
# Blender averages it down to a factor -- exactly what an image mask does.
CUSTOM_OUTPUT = "Base Color"

CUSTOM_TREE_PREFIX = "PaintR2 Custom"

# Kinds edited in the shader graph rather than in the properties panel. Their
# `PARAMS` entry is empty and the panel offers a button instead.
GRAPH_KINDS = frozenset({'CUSTOM'})

# Straight from ShaderNodeMapRange.interpolation_type -- checked against 5.2.
INTERPOLATIONS = (
    ('LINEAR', "Linear", "A straight ramp"),
    ('SMOOTHSTEP', "Smooth", "Eased at both ends"),
    ('SMOOTHERSTEP', "Smoother", "Eased harder at both ends"),
    ('STEPPED', "Constant", "Hard steps rather than a ramp"),
)

def _recompile(self, context):
    """Any parameter edit rebuilds the tree that was generated from it.

    Defined here rather than imported from document.py: this module is
    imported *by* document.py, and the hook only needs the owning material.
    """
    owner = self.id_data
    if isinstance(owner, bpy.types.Material):
        # A parameter drag is a burst of writes; one compile per frame is
        # what the tree can afford. See nodegen.request_compile.
        nodegen.request_compile(owner)


class PR2_Procedural(PropertyGroup):
    """One generated pattern. Lives on a layer, twice: as source and as mask."""

    # A PointerProperty to a PropertyGroup is never None, so this is what
    # "there is one here" means. The layer's source block ignores it -- a
    # PROCEDURAL layer is procedural by its type -- and the mask block uses it.
    enabled: BoolProperty(
        name="Enabled",
        default=False,
        update=_recompile,
    )

    kind: EnumProperty(
        name="Pattern",
        items=KINDS,
        default='GRADIENT',
        update=_recompile,
    )

    # --- gradient ------------------------------------------------------------
    # Object space: the ramp is fixed to the model, so it turns and moves with
    # it and does not slide around when the view changes.
    #
    # The direction is two angles, not an axis. An axis picker can only aim a
    # ramp six ways; a sweep and a tilt aim it anywhere on the sphere, and the
    # three axes are still in there -- tilt 90 is Z, tilt 0 with angle 0 is X,
    # tilt 0 with angle 90 is Y.

    angle: FloatProperty(
        name="Angle",
        description="Which way the ramp runs, swept around the object's Z "
                    "axis in degrees. Ignored at a tilt of 90 or -90, where "
                    "the ramp already points straight up or down",
        default=0.0,
        min=0.0,
        max=360.0,
        soft_min=0.0,
        soft_max=360.0,
        precision=1,
        update=_recompile,
    )

    tilt: FloatProperty(
        name="Tilt",
        description="How far the ramp is tilted out of the object's XY plane, "
                    "in degrees. 90 runs straight up Z, 0 runs flat",
        default=90.0,
        min=-90.0,
        max=90.0,
        soft_min=-90.0,
        soft_max=90.0,
        precision=1,
        update=_recompile,
    )

    start: FloatProperty(
        name="Start",
        description="Where the ramp begins, in object space",
        default=-1.0,
        soft_min=-10.0,
        soft_max=10.0,
        update=_recompile,
    )

    end: FloatProperty(
        name="End",
        description="Where the ramp ends, in object space",
        default=1.0,
        soft_min=-10.0,
        soft_max=10.0,
        update=_recompile,
    )

    interpolation: EnumProperty(
        name="Interpolation",
        items=INTERPOLATIONS,
        default='LINEAR',
        update=_recompile,
    )

    reverse: BoolProperty(
        name="Reverse",
        description="Swap the two ends of the ramp",
        default=False,
        update=_recompile,
    )

    # --- noise and checker ----------------------------------------------------
    # Object space again, for the same reason the gradient is: a pattern fixed
    # to the model does not swim when the view moves, and it crosses UV seams
    # without a seam of its own -- which is what a mask most needs.

    scale: FloatProperty(
        name="Scale",
        description="How large the pattern is. Bigger numbers, smaller features",
        default=5.0,
        min=0.0,
        soft_min=0.1,
        soft_max=50.0,
        update=_recompile,
    )

    detail: FloatProperty(
        name="Detail",
        description="How many finer layers of noise are added on top",
        default=2.0,
        min=0.0,
        max=15.0,
        soft_max=8.0,
        update=_recompile,
    )

    roughness: FloatProperty(
        name="Roughness",
        description="How much each finer layer of noise contributes",
        default=0.5,
        min=0.0,
        max=1.0,
        update=_recompile,
    )

    distortion: FloatProperty(
        name="Distortion",
        description="Warps the noise into itself. 0 leaves it undistorted",
        default=0.0,
        soft_min=-4.0,
        soft_max=4.0,
        update=_recompile,
    )

    contrast: FloatProperty(
        name="Contrast",
        description="Pulls the noise towards black and white. 0 leaves it as "
                    "it comes out, 1 is nearly a hard edge",
        default=0.0,
        min=0.0,
        max=1.0,
        update=_recompile,
    )

    color_a: FloatVectorProperty(
        name="Start Color",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(0.0, 0.0, 0.0, 1.0),
        update=_recompile,
    )

    color_b: FloatVectorProperty(
        name="End Color",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
        update=_recompile,
    )

    # --- custom ---------------------------------------------------------------
    # The group the user edits. Held as a pointer, never copied: the compiled
    # tree is thrown away and rebuilt on every change, and anything living
    # inside it would be thrown away with it. A `ShaderNodeGroup` pointing here
    # is what survives that, which is the whole reason hand-made nodes can be
    # trusted to stay hand-made.

    node_tree: PointerProperty(
        name="Node Group",
        description="The node group this pattern comes from",
        type=bpy.types.NodeTree,
        poll=lambda self, tree: tree.bl_idname == 'ShaderNodeTree',
        update=_recompile,
    )


# --- node construction -------------------------------------------------------


def direction(angle_degrees, tilt_degrees):
    """The unit vector two angles point at, in object space.

    A sweep around Z and a tilt away from the XY plane. Public because the
    test suite checks the six axis directions still come out of it exactly.
    """
    sweep = math.radians(float(angle_degrees))
    tilt = math.radians(float(tilt_degrees))
    flat = math.cos(tilt)
    return (flat * math.cos(sweep), flat * math.sin(sweep), math.sin(tilt))


def _build_gradient(group, proc, x, y):
    """TexCoord.Object -> distance along a direction -> Map Range. A 0..1 float.

    The dot product is what replaced the axis picker: projecting the object
    space position onto a unit vector *is* "how far along this direction",
    and with the vector free the ramp can be aimed anywhere. Pointing it at
    (0, 0, 1) reproduces exactly what reading the Z component used to give.

    Map Range does the rest on its own: it remaps start..end onto 0..1,
    clamps the ends, and carries the interpolation. `reverse` swaps the two
    ends rather than adding an invert node -- same result, one node fewer.
    """
    coords = group.nodes.new('ShaderNodeTexCoord')
    coords.location = (x, y)

    project = group.nodes.new('ShaderNodeVectorMath')
    project.operation = 'DOT_PRODUCT'
    project.label = "Gradient direction"
    project.location = (x + 180.0, y)
    group.links.new(coords.outputs["Object"], project.inputs[0])
    project.inputs[1].default_value = direction(proc.angle, proc.tilt)

    ramp = group.nodes.new('ShaderNodeMapRange')
    ramp.data_type = 'FLOAT'
    ramp.interpolation_type = proc.interpolation
    ramp.clamp = True
    ramp.label = "Gradient"
    ramp.location = (x + 360.0, y)

    # Vector Math has two outputs and only one of them is the scalar; the
    # other is a vector that would silently be averaged into the ramp.
    group.links.new(project.outputs["Value"], nodegen.socket(ramp, "Value"))

    first, second = float(proc.start), float(proc.end)
    if proc.reverse:
        first, second = second, first

    nodegen.socket(ramp, "From Min").default_value = first
    nodegen.socket(ramp, "From Max").default_value = second
    nodegen.socket(ramp, "To Min").default_value = 0.0
    nodegen.socket(ramp, "To Max").default_value = 1.0

    return nodegen.socket(ramp, "Result", outputs=True)


def _object_coords(group, x, y):
    """Object-space texture coordinates. A pattern that lives in the volume."""
    coords = group.nodes.new('ShaderNodeTexCoord')
    coords.location = (x, y)
    return coords.outputs["Object"]


def _uv_coords(group, x, y):
    """UV texture coordinates. A pattern that lives on the map.

    The active UV layer, which is the one the layer images are painted into --
    `TexCoord.UV` reads whatever the mesh has active, the same map the paint
    system uses, so a checker lines up with what is being painted rather than
    with a second unwrap nobody chose.
    """
    coords = group.nodes.new('ShaderNodeTexCoord')
    coords.location = (x, y)
    return coords.outputs["UV"]


def _shape(group, proc, value, x, y):
    """Push a 0..1 factor towards its ends, and swap them if asked.

    One Map Range does both. `contrast` narrows the window that is mapped onto
    0..1, so a noise that comes out as a grey mush becomes a mask with edges;
    `reverse` swaps the window's ends, which is an invert with no extra node.

    Emitted only when it would do something -- an untouched pattern compiles
    to exactly the nodes it did before this existed.
    """
    contrast = float(getattr(proc, "contrast", 0.0))
    reverse = bool(proc.reverse)
    if contrast <= 0.0 and not reverse:
        return value

    # Half the window at full contrast, centred: 0.5 +/- 0.5 at contrast 0,
    # 0.5 +/- 0.05 at contrast 1.
    span = max(0.05, 0.5 * (1.0 - contrast * 0.9))
    low, high = 0.5 - span, 0.5 + span
    if reverse:
        low, high = high, low

    ramp = group.nodes.new('ShaderNodeMapRange')
    ramp.data_type = 'FLOAT'
    ramp.clamp = True
    ramp.label = "Contrast"
    ramp.location = (x, y)
    group.links.new(value, nodegen.socket(ramp, "Value"))
    nodegen.socket(ramp, "From Min").default_value = low
    nodegen.socket(ramp, "From Max").default_value = high
    nodegen.socket(ramp, "To Min").default_value = 0.0
    nodegen.socket(ramp, "To Max").default_value = 1.0
    return nodegen.socket(ramp, "Result", outputs=True)


def _build_noise(group, proc, x, y):
    """Fractal noise in object space, as a 0..1 float.

    The Fac output, not Color: Fac is the scalar the node already computes and
    reading Color would have Blender average three of them back down to one.
    """
    noise = group.nodes.new('ShaderNodeTexNoise')
    noise.noise_dimensions = '3D'
    noise.label = "Noise"
    noise.location = (x + 180.0, y)
    group.links.new(_object_coords(group, x, y), noise.inputs["Vector"])

    for name, value in (
        ("Scale", proc.scale),
        ("Detail", proc.detail),
        ("Roughness", proc.roughness),
        ("Distortion", proc.distortion),
    ):
        socket = noise.inputs.get(name)
        if socket is not None:
            socket.default_value = float(value)

    return _shape(group, proc, noise.outputs["Fac"], x + 360.0, y)


def _build_checker(group, proc, x, y):
    """A checkerboard in UV space, as a 0..1 float.

    UV, not object space. A checker is a *texture* pattern -- what it is for is
    reading squares off the map, laying out a trim sheet, checking an unwrap --
    and in object space the squares cut through the model at whatever angle the
    geometry happens to sit, which is a different thing that nobody was asking
    for. Noise and Gradient stay in object space: those are shapes in the
    volume the model occupies, and following the seams is what would be wrong
    about them.

    Black and white swatches, so the Color output *is* the factor: the layer's
    own two colours are applied afterwards by `build_color`, and a mask wants
    the bare 0 or 1.
    """
    checker = group.nodes.new('ShaderNodeTexChecker')
    checker.label = "Checker"
    checker.location = (x + 180.0, y)
    group.links.new(_uv_coords(group, x, y), checker.inputs["Vector"])

    checker.inputs["Color1"].default_value = (0.0, 0.0, 0.0, 1.0)
    checker.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    checker.inputs["Scale"].default_value = float(proc.scale)

    return _shape(group, proc, checker.outputs["Fac"], x + 360.0, y)


def build_channel(group, proc, channel, x, y):
    """A custom group's output for one channel, or None.

    Only Custom has one: every other kind drives Base Color and leaves the
    rest to the layer's flat values. A channel whose socket is missing
    -- the group was built by an older version, or the user renamed it --
    contributes nothing rather than raising.

    The group node itself is emitted once per channel. That is deliberate:
    Blender's shader compiler folds identical group instances, and the
    alternative is a cache keyed on (layer, group) threaded through every
    call site for a saving that does not show up in a frame time.
    """
    from . import document as doc

    tree = proc.node_tree
    if tree is None:
        return None

    node = _instance(group, proc, x, y)
    if node is None:
        return None
    return node.outputs.get(doc.channel_label(channel))


def _instance(group, proc, x, y):
    """A ShaderNodeGroup pointing at the user's tree, or None.

    The one place a custom group is instanced -- as a layer, as a mask, as a
    pattern effect -- so it is also the one place the group's map inputs are
    fed (see "map inputs" below). Whatever material this compile is for, its
    own baked maps land on the group's sockets here.
    """
    tree = proc.node_tree
    if tree is None:
        return None

    node = group.nodes.new('ShaderNodeGroup')
    node.node_tree = tree
    node.label = tree.name
    node.location = (x + 360.0, y)

    material = proc.id_data
    if isinstance(material, bpy.types.Material):
        # Before the node is read: a socket added here is on the node already.
        ensure_map_inputs(tree, material)
        _feed_map_inputs(group, node, tree, material, x, y)
    return node


def _build_custom(group, proc, x, y):
    """The user's own group, instanced. Returns its Base Color output, or None.

    Instanced, not inlined: `compile_group` clears the generated tree on every
    recompile, so nodes copied in here would be destroyed by the next opacity
    drag. A `ShaderNodeGroup` pointing at a separate datablock survives that,
    and edits made in the shader graph reach the model with no recompile at
    all -- Blender's own dependency graph carries them.

    None when there is no group yet, or when its interface has no colour
    output: a half-built pattern contributes nothing rather than raising.

    This is the mask path as well -- a mask reads the group's Base Color and
    lets Blender average it down to a factor, the same way an image mask does.
    """
    node = _instance(group, proc, x, y)
    if node is None:
        return None

    output = node.outputs.get(CUSTOM_OUTPUT)
    if output is None:
        # Whatever the first colour output is, then. Renaming the socket in
        # the shader graph is an easy thing to do by accident and losing the
        # whole layer over it would be a poor trade.
        output = next(
            (item for item in node.outputs if item.type == 'RGBA'), None
        )
    return output


_BUILDERS = {
    'GRADIENT': _build_gradient,
    'NOISE': _build_noise,
    'CHECKER': _build_checker,
    'CUSTOM': _build_custom,
}

# Kinds that hand back a colour directly, so `build_color` must not mix them
# between two swatches. The same builder serves both roles: as a factor the
# colour is averaged down to a float, which is what an image mask already does.
_COLOR_BUILDERS = {
    'CUSTOM': _build_custom,
}


def build_factor(group, proc, x, y):
    """The pattern as a 0..1 float socket, or None for a kind we cannot build.

    None is not an error: a document written by a newer version can carry a
    kind this one has never heard of, and the compiler treats that as "this
    contributes nothing" rather than falling over.
    """
    builder = _BUILDERS.get(proc.kind)
    if builder is None:
        return None
    return builder(group, proc, x, y)


def build_color(group, proc, x, y):
    """The pattern as a colour socket.

    For most kinds that is the 0..1 factor mixed between two swatches. A kind
    listed in `_COLOR_BUILDERS` already produces a colour and is handed back
    untouched -- mixing it between two swatches would throw the colour away.
    """
    direct = _COLOR_BUILDERS.get(proc.kind)
    if direct is not None:
        return direct(group, proc, x, y)

    factor = build_factor(group, proc, x, y)
    if factor is None:
        return None

    mix = group.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.blend_type = 'MIX'
    mix.clamp_factor = True
    mix.label = "Gradient colour"
    mix.location = (x + 560.0, y)

    group.links.new(factor, nodegen.socket(mix, "Factor_Float"))
    nodegen.socket(mix, "A_Color").default_value = tuple(proc.color_a)
    nodegen.socket(mix, "B_Color").default_value = tuple(proc.color_b)

    return nodegen.socket(mix, "Result_Color", outputs=True)


# --- the custom group --------------------------------------------------------


def custom_tree_name(material, label):
    """A readable, unique name for a new custom group."""
    base = f"{CUSTOM_TREE_PREFIX} {material.name} {label}".strip()
    if base not in bpy.data.node_groups:
        return base

    index = 2
    while f"{base} {index}" in bpy.data.node_groups:
        index += 1
    return f"{base} {index}"


def ensure_custom_tree(proc, material, label="Custom"):
    """Give `proc` an empty group to draw, if it has not got one. Returns it.

    Empty means empty: one Group Output carrying a single Color socket, and
    nothing wired to it. That is the whole contract -- connect anything to
    that socket and it is on the model, because the compiled tree already
    holds a group node pointing here.

    A fake user is set deliberately. The document does hold a pointer, but a
    node group with no *node* using it is exactly the kind of datablock
    Blender drops on the next load, and losing an hour of hand-built nodes to
    that would be unforgivable. The cost is an orphan group left in the file
    when the layer is deleted, which the Blend File view can clear.
    """
    tree = proc.node_tree
    if tree is not None:
        _ensure_custom_output(tree)
        return tree

    tree = bpy.data.node_groups.new(
        custom_tree_name(material, label), 'ShaderNodeTree'
    )
    tree.use_fake_user = True

    _ensure_custom_output(tree)

    output = tree.nodes.new('NodeGroupOutput')
    output.location = (0.0, 0.0)

    # Whatever has been baked for this material is on the group from the
    # first time it is opened, so the Group Input node already offers it.
    ensure_map_inputs(tree, material)

    proc.node_tree = tree
    return tree


def _ensure_custom_output(tree):
    """Give the group one output per channel, whatever the document uses.

    **All five, always** -- not only the channels the document has switched
    on. The group is the user's workspace: they build a normal there before
    turning the Normal channel on, and a socket that appears and disappears
    under their hands would take their links with it. Which of the five is
    actually read is the *layer's* business (`layer.channel(...).enabled`),
    and that is a checkbox, not a rewiring.
    """
    from . import document as doc

    existing = {
        item.name: item
        for item in tree.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT'
    }

    for identifier in doc.CHANNEL_IDS:
        name = doc.channel_label(identifier)
        if name in existing:
            continue
        tree.interface.new_socket(
            name,
            in_out='OUTPUT',
            socket_type='NodeSocketColor'
            if doc.channel_kind(identifier) == 'COLOR'
            else 'NodeSocketFloat',
        )

    return tree.interface.items_tree


# --- map inputs: what a custom group asks the model for ----------------------
#
# A custom group that reads the model's own maps -- edge wear from curvature,
# dirt from occlusion -- used to carry an Image Texture node pointing at one
# material's bake. Shared between materials it kept pointing at the
# first one's. There is no way round that inside the group: a shader group
# cannot take an Image through a socket (measured on 5.2: Image, String and
# Object are not socket types a ShaderNodeTree accepts), and the group is one
# datablock however many materials instance it.
#
# So the group does not *hold* the map, it *asks* for it. It declares an input
# socket per map it wants, and the compiler -- which builds one tree per
# material and rebuilds it on every change -- samples that material's bake
# into the socket from outside. The same group on two materials reads two
# different maps, with no copy of anything.
#
# Which socket means which map is recorded on the group itself, as an ID
# property keyed by socket *identifier*: the identifier survives a rename
# (measured), and a record on the group travels with it into every layer,
# material and file it is appended to. Interface sockets cannot carry ID
# properties of their own (measured), which is why the record is one level
# up.
#
# A map that has not been baked leaves the socket unlinked and the socket's
# own default takes over -- white occlusion, flat curvature. The group keeps
# working and the model looks clean, which is the honest result.

MAP_INPUTS_KEY = "paintr2_map_inputs"

# Socket type and the value a socket carries while its map is not baked.
# Single-channel maps are Floats: inside the group they read as the value
# they are, and go straight into Map Range or Math. Blender averages an
# Image Texture's colour down to one when it links into a Float, which for a
# grey map is the map. The vector maps stay colours.
MAP_SOCKETS = {
    'AO': ('NodeSocketFloat', 1.0),
    'CURVATURE': ('NodeSocketFloat', 0.5),
    'THICKNESS': ('NodeSocketFloat', 1.0),
    'HEIGHT': ('NodeSocketFloat', 0.5),
    'POSITION': ('NodeSocketColor', (0.5, 0.5, 0.5, 1.0)),
    'NORMAL': ('NodeSocketColor', (0.5, 0.5, 1.0, 1.0)),
    'BENT_NORMAL': ('NodeSocketColor', (0.5, 0.5, 1.0, 1.0)),
}

MAP_IDS = tuple(MAP_SOCKETS)


def _input_items(tree):
    """{identifier: interface socket} for the group's inputs."""
    return {
        item.identifier: item
        for item in tree.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
    }


def _records(tree):
    """The binding record as a plain dict. An ID property group otherwise."""
    stored = tree.get(MAP_INPUTS_KEY)
    if stored is None:
        return {}
    if hasattr(stored, "to_dict"):
        stored = stored.to_dict()
    return {str(key): str(value) for key, value in dict(stored).items()}


def _map_by_name(name):
    """The map a socket called `name` is asking for by name alone, or None.

    The label (`Ambient Occlusion`) or the identifier (`AO`), either case:
    hand-built groups are not going to match the table's spelling exactly.
    """
    from . import meshmaps

    wanted = name.strip().lower()
    for identifier in MAP_IDS:
        if wanted in (
            meshmaps.map_label(identifier).lower(),
            identifier.lower().replace("_", " "),
        ):
            return identifier
    return None


def map_bindings(tree):
    """{socket identifier: map id} for every input the compiler should feed.

    A recorded binding wins. A socket the record has never heard of binds by
    its name as a fallback, so a group built by hand with an input called
    `Curvature` works without a click. A socket recorded as "" is one the
    user unbound on purpose: it keeps its name and is *not* matched by it
    again. A record for a socket that no longer exists is simply skipped.
    """
    if tree is None:
        return {}

    records = _records(tree)
    out = {}
    for identifier, item in _input_items(tree).items():
        if identifier in records:
            if records[identifier] in MAP_SOCKETS:
                out[identifier] = records[identifier]
            continue
        found = _map_by_name(item.name)
        if found is not None:
            out[identifier] = found
    return out


def bound_map(tree, map_id):
    """The identifier of the socket fed with `map_id`, or None."""
    for identifier, bound in map_bindings(tree).items():
        if bound == map_id:
            return identifier
    return None


def bind_map_input(tree, map_id):
    """Make sure the group has an input fed with `map_id`. Returns its socket.

    Reuses what is there: a socket already bound to this map is left alone,
    and one that was unbound (or matches by name) is bound again rather than
    doubled. Only when nothing fits is a new socket added, typed and
    defaulted from `MAP_SOCKETS`.
    """
    from . import meshmaps

    socket_type, default = MAP_SOCKETS[map_id]
    items = _input_items(tree)
    records = _records(tree)

    identifier = bound_map(tree, map_id)
    if identifier is None:
        # An unbound or name-matching socket this map can take back.
        for candidate, item in items.items():
            if records.get(candidate, None) == "" and _map_by_name(item.name) == map_id:
                identifier = candidate
                break

    if identifier is None:
        item = tree.interface.new_socket(
            meshmaps.map_label(map_id), in_out='INPUT', socket_type=socket_type,
        )
        try:
            item.default_value = default
        except (AttributeError, TypeError):
            pass
        identifier = item.identifier
    else:
        item = items[identifier]

    records[identifier] = map_id
    tree[MAP_INPUTS_KEY] = records
    return item


def unbind_map_input(tree, map_id):
    """Stop feeding `map_id`. The socket stays; only the binding goes.

    The user's links inside the group hang off that socket, and taking the
    socket away would take them too. Recorded as "" rather than deleted so
    the name does not bind it straight back.
    """
    identifier = bound_map(tree, map_id)
    if identifier is None:
        return False
    records = _records(tree)
    records[identifier] = ""
    tree[MAP_INPUTS_KEY] = records
    return True


def ensure_map_inputs(tree, material):
    """Bind every map `material` has baked that the group does not ask for yet.

    Nothing to tick (user decision): a baked map is a map the group
    can use, so it is offered the moment it exists -- on the first compile
    after a bake, or when the group is made for a material that already has
    maps. The one thing that holds a map back is a socket the user unbound
    on purpose (recorded as ""), which stays unbound.

    A group shared between materials ends up asking for the union of their
    maps; a material missing one leaves that socket on its default.
    """
    from . import meshmaps

    records = _records(tree)
    items = _input_items(tree)
    bound = set(map_bindings(tree).values())
    refused = {
        _map_by_name(items[identifier].name)
        for identifier, value in records.items()
        if value == "" and identifier in items
    }

    added = False
    for map_id in MAP_IDS:
        if map_id in bound or map_id in refused:
            continue
        if meshmaps.stored(material, map_id) is None:
            continue
        bind_map_input(tree, map_id)
        added = True
    return added


def _map_texture(group, tree, map_id, image, x, y):
    """The Image Texture sampling `image` for this group in this compile.

    One per (group, map) however many channels, masks and effects instance
    the group: `build_channel` emits a group node per channel, and five
    samplers of one map would be five texture reads per pixel. Found by name
    the way `nodegen._owned_node` finds its own; the compiled tree is cleared
    at the start of every compile, so a hit is always this compile's.
    """
    name = f"PR2 map {map_id} {tree.name}"
    node = group.nodes.get(name)
    if node is not None and node.image is image:
        return node

    node = group.nodes.new('ShaderNodeTexImage')
    node.name = name
    node.label = f"{tree.name} <- {map_id}"
    node.image = image
    # The same rule as the map preview: a tiny map is read point for point.
    node.interpolation = 'Closest' if max(image.size) <= 64 else 'Linear'
    node.location = (x - 300.0, y - 200.0)
    return node


def _feed_map_inputs(group, node, tree, material, x, y):
    """Link `material`'s baked maps into the group node's bound inputs."""
    from . import meshmaps

    bindings = map_bindings(tree)
    if not bindings:
        return

    for identifier, map_id in bindings.items():
        image = meshmaps.stored(material, map_id)
        if image is None:
            continue
        target = next(
            (item for item in node.inputs if item.identifier == identifier),
            None,
        )
        if target is None:
            continue
        sampler = _map_texture(group, tree, map_id, image, x, y)
        group.links.new(sampler.outputs["Color"], target)


def materials_using(tree):
    """Every material whose stack instances `tree`, anywhere in a layer."""
    found = []
    for material in bpy.data.materials:
        document = getattr(material, "paintr2", None)
        if document is None:
            continue
        for layer in document.layers:
            blocks = [layer.procedural, layer.mask_procedural]
            blocks += [effect.pattern for effect in layer.effects]
            blocks += [effect.pattern for effect in layer.mask_effects]
            if any(block.node_tree is tree for block in blocks):
                found.append(material)
                break
    return found


# --- helpers the interface uses ----------------------------------------------


def param_names(proc, with_color=False):
    """The parameters to show for `proc`, in panel order."""
    names = list(PARAMS.get(proc.kind, ()))
    if with_color:
        names += list(COLOR_PARAMS.get(proc.kind, ()))
    return names


def kind_label(kind):
    for identifier, label, _desc in KINDS:
        if identifier == kind:
            return label
    return kind.title()


def edits_in_graph(kind):
    """True for a kind whose parameters are nodes, not numbers.

    The panel asks this to decide whether to draw a button that opens the
    shader graph instead of a parameter list.
    """
    return kind in GRAPH_KINDS


CLASSES = (PR2_Procedural,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

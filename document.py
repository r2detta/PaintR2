# SPDX-License-Identifier: GPL-3.0-or-later
"""The layer stack data model -- the single source of truth.

A document hangs off a Material (`material.paintr2`) and owns an ordered list
of layers. Index 0 is the TOP layer, matching how the stack reads on screen;
the compiler walks it in reverse.

Groups are folded into that one flat list rather than nested inside each
other. A GROUP layer owns the layers that follow it with a greater `depth`:

    index  depth  layer
      0      0    Gradient
      1      0    Details          <- GROUP
      2      1      Scratches         its children, because depth is greater
      3      1      Rust
      4      0    Base             <- depth is back to 0, the group ended

Three rules keep that readable, and `normalize` is what enforces them:

    1. the first layer is at depth 0;
    2. depth only ever increases by one, and only after a GROUP;
    3. a layer that is not a GROUP has no children.

They are invariants of the *operators*, not of the data model -- the price of a
flat list, and the reason `normalize` runs after every structural change.

Everything here is saved in the .blend. The node tree built from it is a
derived product and is never edited by hand -- see nodegen.py.

Changing any property that affects the result triggers a recompile through
`_recompile`, which is what makes the viewport preview live.
"""

import re

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from . import adjustment
from . import procedural
from . import effects as effects_mod


# Straight from ShaderNodeMix.blend_type -- verified against 5.2. Ordered and
# labelled the way an image editor presents them rather than alphabetically.
BLEND_MODES = (
    ('MIX', "Normal", "Layer replaces what is below it"),
    ('DARKEN', "Darken", "Keeps the darker of the two"),
    ('MULTIPLY', "Multiply", "Darkens; white leaves the result unchanged"),
    ('BURN', "Color Burn", "Darkens by increasing contrast"),
    ('LIGHTEN', "Lighten", "Keeps the lighter of the two"),
    ('SCREEN', "Screen", "Brightens; black leaves the result unchanged"),
    ('DODGE', "Color Dodge", "Brightens by decreasing contrast"),
    ('ADD', "Add", "Adds the values together"),
    ('OVERLAY', "Overlay", "Multiply on dark areas, screen on light ones"),
    ('SOFT_LIGHT', "Soft Light", "Gentle contrast shift"),
    ('LINEAR_LIGHT', "Linear Light", "Strong contrast shift"),
    ('DIFFERENCE', "Difference", "Absolute difference of the two"),
    ('EXCLUSION', "Exclusion", "Lower-contrast difference"),
    ('SUBTRACT', "Subtract", "Subtracts the layer from what is below"),
    ('DIVIDE', "Divide", "Divides what is below by the layer"),
    ('HUE', "Hue", "Takes hue from the layer"),
    ('SATURATION', "Saturation", "Takes saturation from the layer"),
    ('COLOR', "Color", "Takes hue and saturation from the layer"),
    ('VALUE', "Value", "Takes brightness from the layer"),
)

# Blend modes only the Normal channel has. A tangent-space normal is a
# direction stored as a colour, and blending two of them as colours tilts the
# surface somewhere nobody asked for -- these two add the top layer's detail
# *onto* the surface below instead (measured).
# Normal's plain "Normal" (MIX) is the third option: a copy, which is how a
# masked layer flattens what is under it.
NORMAL_BLEND_MODES = (
    ('DETAIL', "Detail", "Reoriented normal mapping: the layer's detail "
     "follows the surface below it"),
    ('COMBINE', "Combine", "Whiteout blending: both normals' detail kept, "
     "cheaper than Detail"),
)

# Every mode a channel record can hold. One enum for all five channels because
# an RNA enum cannot change its items per instance; which of them a channel
# actually offers is `nodegen.blend_modes_for`.
CHANNEL_BLEND_MODES = BLEND_MODES + NORMAL_BLEND_MODES

# The channels a document carries, in the order everything uses them: the
# panel, the compiler, the export. A material carries all five, always -- a
# channel no layer writes to simply reads its default.
#
# `kind` decides which field of PR2_LayerChannel is read and which socket the
# group output gets:
#
#     COLOR   `color`, a colour socket
#     FLOAT   `value`, 0..1
#     SIGNED  `signed`, -1..1 with 0 flat -- Height (the flat point is 0 in
#             the tree and only an export encodes it as 0.5)
#     NORMAL  `normal_color`, a tangent-space normal as a colour; starts flat
#             (0.5, 0.5, 1), and with Normal (copy) blend a flat one flattens
#             what is under it
#
# `default` is two things at once and they have to agree: what a channel
# reads where no layer contributes (the compiler's backdrop), and the value a
# layer's record starts with. Roughness 0.5 and Metallic 0 are also
# Principled's own defaults, so a channel nobody touched looks like no channel.
#
# `colorspace` and `float_buffer` are the measured export rules: a PNG's bit
# depth comes from the image buffer, not
# from any save setting, and a Non-Color target is the difference between
# writing 0.31 and writing 0.60. Height needs the float buffer for a second
# reason: a byte buffer clips the negative half (measured).
#
# `blend` is the mode a layer's record starts with. Height adds:
# a full-cover layer at 0 must change nothing, and under MIX it would flatten
# every bump beneath it. Normal starts on Detail for the same reason.
CHANNELS = (
    # id,          label,        kind,     default,              colorspace,  float, blend
    ('BASE_COLOR', "Base Color", 'COLOR',  (0.8, 0.8, 0.8, 1.0), 'sRGB',      False, 'MIX'),
    ('ROUGHNESS',  "Roughness",  'FLOAT',  0.5,                  'Non-Color', False, 'MIX'),
    ('METALLIC',   "Metallic",   'FLOAT',  0.0,                  'Non-Color', False, 'MIX'),
    ('NORMAL',     "Normal",     'NORMAL', (0.5, 0.5, 1.0, 1.0), 'Non-Color', True,  'DETAIL'),
    ('HEIGHT',     "Height",     'SIGNED', 0.0,                  'Non-Color', True,  'ADD'),
)

CHANNEL_IDS = tuple(entry[0] for entry in CHANNELS)

# Field order matches CHANNELS, so `_channel_entry` can index either way.
_CHANNEL_BY_ID = {entry[0]: entry for entry in CHANNELS}

# The two kinds whose group socket is a colour.
_COLOR_KINDS = frozenset({'COLOR', 'NORMAL'})


def channel_entry(identifier):
    """The CHANNELS row for `identifier`, or None."""
    return _CHANNEL_BY_ID.get(identifier)


def channel_label(identifier):
    entry = channel_entry(identifier)
    return entry[1] if entry else identifier.title()


def channel_kind(identifier):
    entry = channel_entry(identifier)
    return entry[2] if entry else 'FLOAT'


def channel_is_color(identifier):
    """True when the channel travels as a colour: Base Color and Normal."""
    return channel_kind(identifier) in _COLOR_KINDS


def channel_default(identifier):
    entry = channel_entry(identifier)
    return entry[3] if entry else 0.0


def channel_colorspace(identifier):
    entry = channel_entry(identifier)
    return entry[4] if entry else 'Non-Color'


def channel_float(identifier):
    """True for a channel whose 8 bits are visibly not enough.

    Measured: a PNG's bit depth comes from the image buffer alone, so this is
    the whole of the 16-bit decision. Normal and Height need it.
    """
    entry = channel_entry(identifier)
    return bool(entry[5]) if entry else False


def channel_blend_default(identifier):
    """The blend mode a layer's record for this channel starts with."""
    entry = channel_entry(identifier)
    return entry[6] if entry else 'MIX'


def channel_value_attr(identifier):
    """Which field of PR2_LayerChannel holds this channel's flat value."""
    return {
        'COLOR': "color",
        'FLOAT': "value",
        'SIGNED': "signed",
        'NORMAL': "normal_color",
    }.get(channel_kind(identifier))


def channel_suffix(identifier):
    """What an exported file for this channel is called: `_basecolor`, ..."""
    return identifier.lower().replace("_", "")


# The property name a channel's per-layer record lives under. One place, so
# renaming the scheme is one edit.
def channel_attr(identifier):
    return "ch_" + identifier.lower()


LAYER_TYPES = (
    ('PAINT', "Paint", "An image you paint into", 'BRUSH_DATA', 0),
    ('FILL', "Fill", "A flat color covering the whole surface", 'SHADING_SOLID', 1),
    ('PROCEDURAL', "Procedural", "A generated pattern, editable forever",
     'NODE_TEXTURE', 2),
    ('GROUP', "Group", "A folder holding the layers under it", 'OUTLINER_COLLECTION', 3),
    ('ADJUSTMENT', "Adjustment", "Changes everything under it, in its group",
     'SHADERFX', 4),
)


_LAYER_PATH = re.compile(r"layers\[(\d+)\]")


def _layer_index(owner):
    """Which layer `owner` belongs to, or -1.

    Read off the RNA path rather than by searching the collection: the path is
    `paintr2.layers[7]` (or `paintr2.layers[7].ch_base_color`) and the number in
    it is the answer, which makes this a regex match instead of a walk down a
    hundred layers on every write.
    """
    try:
        match = _LAYER_PATH.search(owner.path_from_id())
    except (AttributeError, TypeError):
        return -1
    return int(match.group(1)) if match else -1


# `ch_base_color` back to `BASE_COLOR`, built from the table rather than typed.
_CHANNEL_BY_ATTR = {channel_attr(identifier): identifier
                    for identifier in CHANNEL_IDS}


def _channel_of(record):
    """Which channel a channel record is, or "" if it cannot be told."""
    try:
        attribute = record.path_from_id().rpartition(".")[2]
    except (AttributeError, TypeError):
        return ""
    return _CHANNEL_BY_ATTR.get(attribute, "")


def _flat_changed(self, context):
    """A flat colour or value is a number on one node, so it is written there.

    The same argument as `_opacity_changed`, for the other thing a drag writes
    a hundred times a second: a Fill layer's colour, pulled around the picker.
    There is no unwritable case here -- a flat channel is always exactly one
    RGB or Value node and its value never changes the shape of anything -- but
    a layer whose channel does not come from one of those (a Paint layer's
    Base Color is its image) has no node, and `write_flat` says so.
    """
    from . import nodegen

    owner = self.id_data
    if not isinstance(owner, bpy.types.Material):
        return

    index = _layer_index(self)
    channel = _channel_of(self)
    attribute = channel_value_attr(channel) if channel else None
    if attribute is None:
        nodegen.request_compile(owner)
        return
    value = getattr(self, attribute)

    if index < 0 or not channel or not nodegen.write_flat(
        owner, index, channel, value
    ):
        nodegen.request_compile(owner)


def _channel_image_changed(self, context):
    """An image went into (or out of) a channel slot: give it the channel's
    colour space, then rebuild -- an image is a different shape of tree.

    The colour space is the channel's, never the user's to pick: a
    roughness map read as sRGB is a roughness that is quietly wrong.
    """
    channel = _channel_of(self)
    if self.image is not None and channel:
        wanted = channel_colorspace(channel)
        try:
            if self.image.colorspace_settings.name != wanted:
                self.image.colorspace_settings.name = wanted
        except (AttributeError, TypeError, ValueError):
            pass
    _recompile(self, context)


def _projection_changed(self, context):
    """Position, rotation and scale are numbers on one Mapping node: written
    there, the way an opacity is. Mode and axis change the tree's shape and
    rebuild."""
    from . import nodegen

    owner = self.id_data
    if not isinstance(owner, bpy.types.Material):
        return
    index = _layer_index(self)
    if index < 0 or not nodegen.write_mapping(owner, index, self):
        nodegen.request_compile(owner)


def _uniform_changed(self, context):
    """Switching uniform scale on makes the scale uniform at once: Y and Z
    take X's value. Off changes nothing -- the values stay where they are."""
    if self.uniform_scale:
        value = float(self.scale[0])
        if tuple(self.scale) != (value, value, value):
            # One write, so the Mapping node is written once.
            self.scale = (value, value, value)
            return
    _projection_changed(self, context)


def _opacity_changed(self, context):
    """Opacity is a number, not a shape, so it does not rebuild the tree.

    Measured: a rebuild of a fifty-layer stack is 31 ms and dragging this
    wrote one per event, which is a slider the window cannot keep up with.
    Writing the value into the node that already holds it is 0.12 ms.

    The rebuild stays as the fallback, and it is needed exactly once per drag
    in one case: a layer with a mask at exactly 1.0 has no opacity term in the
    tree at all -- a factor of 1.0 is dropped -- so the first move off 1.0
    changes the shape. After that the node exists and the rest of the drag is
    the cheap path. `nodegen.write_opacity` is what knows the difference.
    """
    from . import nodegen

    owner = self.id_data
    if not isinstance(owner, bpy.types.Material):
        return

    index = _layer_index(self)
    if index < 0 or not nodegen.write_opacity(
        owner, index, self.opacity, 'BASE_COLOR'
    ):
        nodegen.request_compile(owner)


def _channel_opacity_changed(self, context):
    """A channel record's opacity: the same cheap write, for its channel.

    Base Color's opacity is `layer.opacity` and never comes through here --
    see `PR2_Layer.channel_opacity`.
    """
    from . import nodegen

    owner = self.id_data
    if not isinstance(owner, bpy.types.Material):
        return

    index = _layer_index(self)
    channel = _channel_of(self)
    if index < 0 or not channel or not nodegen.write_opacity(
        owner, index, self.opacity, channel
    ):
        nodegen.request_compile(owner)


def _recompile(self, context):
    """Property update hook: rebuild the node tree for the owning material."""
    from . import nodegen
    from . import paint

    owner = self.id_data
    if isinstance(owner, bpy.types.Material):
        # Asked for, not done: an opacity drag writes this a hundred times a
        # second and a hundred-layer stack costs 123 ms a compile.
        nodegen.request_compile(owner)

    # Swapping a layer's image (or turning it into a FILL layer) can change
    # what should be painted into.
    paint.sync_canvas(context)


def _sync_canvas(self, context):
    """Active-layer changes do not affect the shader, only the paint target."""
    from . import paint

    paint.sync_canvas(context)


def _rewire(self, context):
    """A channel was switched on or off for the whole document.

    More than a recompile: the group's *interface* changes, and removing an
    output socket silently drops the link that fed Principled from it --
    measured. So the material has to be wired again
    rather than left alone, which is why this is not `_recompile`.
    """
    from . import nodegen

    owner = self.id_data
    if isinstance(owner, bpy.types.Material):
        nodegen.request_compile(owner)


class PR2_LayerChannel(PropertyGroup):
    """What one layer contributes to one channel.

    One class for all five channels, and CHANNELS says which field a channel
    reads: `color` for Base Color, `value` for Roughness and Metallic,
    `signed` for Height, `normal_color` for Normal. Five classes would be the
    same code five times.

    `enabled` off means the layer contributes *nothing* to that channel -- no
    node is emitted for it. Switching it off keeps the value, so switching it
    back on is getting the same layer back.

    `blend_mode` and `opacity` are per channel -- except Base
    Color's, which stay on the layer itself as `layer.blend_mode` and
    `layer.opacity`. Those two are what every file, every row and every test
    already reads, and moving them would buy nothing but a migration; the
    asymmetry lives in exactly two methods, `PR2_Layer.channel_blend` and
    `channel_opacity`, and nothing else reads these fields for Base Color.

    The values start wrong for most channels (`value` is 0.5 for Metallic
    too, `blend_mode` MIX for Height): an RNA default cannot differ per
    instance. `PR2_Layer.init_channels` writes the real ones from CHANNELS,
    and `add_layer` calls it.

    A PAINT layer takes its Base Color from `layer.image`, not from here, and
    has no other channel at all: the brush paints Base Color only.
    """

    enabled: BoolProperty(
        name="Enabled",
        description="Whether this layer writes to this channel",
        default=False,
        update=_recompile,
    )

    color: FloatVectorProperty(
        name="Color",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(0.8, 0.8, 0.8, 1.0),
        update=_flat_changed,
    )

    value: FloatProperty(
        name="Value",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_flat_changed,
    )

    # Height. Its own field rather than `value` with a wider range because an
    # RNA range cannot change per instance, and Roughness must stop at 0..1.
    signed: FloatProperty(
        name="Value",
        default=0.0,
        min=-1.0,
        max=1.0,
        update=_flat_changed,
    )

    # Normal. A direction stored as a colour, so it is *data*: COLOR_GAMMA
    # keeps the swatch showing the numbers themselves -- (0.5, 0.5, 1) is the
    # familiar normal-map blue, not the pale lilac an sRGB encode would make
    # of it. Starts flat; the user may tint it to tilt the surface.
    normal_color: FloatVectorProperty(
        name="Normal",
        subtype='COLOR_GAMMA',
        size=4,
        min=0.0,
        max=1.0,
        default=(0.5, 0.5, 1.0, 1.0),
        update=_flat_changed,
    )

    blend_mode: EnumProperty(
        name="Blend",
        items=CHANNEL_BLEND_MODES,
        default='MIX',
        update=_recompile,
    )

    opacity: FloatProperty(
        name="Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_channel_opacity_changed,
    )

    # A picture in this channel, on a Fill layer. Empty, the
    # channel is its flat value. Set, it replaces Base Color's and Normal's
    # value outright and is multiplied by the slider on the others. Held
    # by path, the way Blender holds any image it opened -- packing it into
    # the .blend later is Blender's own business and changes nothing here.
    image: PointerProperty(
        name="Image",
        description="A texture for this channel, projected by the layer's "
        "Projection settings",
        type=bpy.types.Image,
        update=_channel_image_changed,
    )

    # Normal only. A DirectX normal map has its green pointing down; this
    # reads it as OpenGL without touching the file.
    flip_green: BoolProperty(
        name="Convert DirectX to OpenGL",
        description="The image is a DirectX normal map: flip its green "
        "channel as it is read. The file is not changed",
        default=False,
        update=_recompile,
    )


PROJECTION_MODES = (
    ('UV', "UV", "Along the model's UV map"),
    ('PLANAR', "Planar", "Straight through the model along one of its own "
     "axes, ignoring the UVs"),
)


class PR2_Projection(PropertyGroup):
    """How a Fill layer's channel images land on the model.

    One per layer, shared by every image the layer carries -- a colour map and
    its normal map stay registered with each other. A layer that wants two
    images placed differently is two layers.

    The numbers are a texture transform, centred on the image: Position moves
    the picture, Rotation turns it about its middle, Scale makes it bigger.
    In UV only X and Y of Position and Scale, and Z of Rotation, mean
    anything; the panel shows just those.
    """

    mode: EnumProperty(
        name="Projection",
        items=PROJECTION_MODES,
        default='UV',
        update=_recompile,
    )

    axis: EnumProperty(
        name="Axis",
        description="Which of the model's own axes Planar projects along",
        items=(
            ('X', "X", "Project along the model's X axis"),
            ('Y', "Y", "Project along the model's Y axis"),
            ('Z', "Z", "Project along the model's Z axis"),
        ),
        default='Z',
        update=_recompile,
    )

    position: FloatVectorProperty(
        name="Position", size=3, subtype='XYZ', default=(0.0, 0.0, 0.0),
        update=_projection_changed,
    )
    rotation: FloatVectorProperty(
        name="Rotation", size=3, subtype='EULER', default=(0.0, 0.0, 0.0),
        update=_projection_changed,
    )
    scale: FloatVectorProperty(
        name="Scale", size=3, subtype='XYZ', default=(1.0, 1.0, 1.0),
        update=_projection_changed,
    )

    # On, changing any one scale field sets all three to it (the panel's
    # write does that -- see `window._set_panel_prop`).
    uniform_scale: BoolProperty(
        name="Uniform",
        description="Scale X, Y and Z together",
        default=False,
        update=_uniform_changed,
    )


class PR2_Layer(PropertyGroup):
    """One entry in the stack."""

    name: StringProperty(
        name="Name",
        default="Layer",
        update=_recompile,
    )

    layer_type: EnumProperty(
        name="Type",
        items=LAYER_TYPES,
        default='PAINT',
        update=_recompile,
    )

    # PAINT layers paint into this image; ignored by the other types.
    image: PointerProperty(
        name="Image",
        type=bpy.types.Image,
        update=_recompile,
    )

    # --- channels ------------------------------------------------------------
    # One record per channel, in CHANNELS order. Fixed fields rather than a
    # collection: the channel set is fixed, and `channel()` is the only place
    # that maps an identifier onto a field -- so making it a collection later
    # is one function.
    #
    # This replaced the single `color` field a FILL layer used to have; a fill
    # colour is now `layer.channel('BASE_COLOR').color`.

    ch_base_color: PointerProperty(type=PR2_LayerChannel)
    ch_roughness: PointerProperty(type=PR2_LayerChannel)
    ch_metallic: PointerProperty(type=PR2_LayerChannel)
    ch_normal: PointerProperty(type=PR2_LayerChannel)
    ch_height: PointerProperty(type=PR2_LayerChannel)

    # Where this layer's channel images land. Present on every
    # layer, read only by a Fill that carries an image.
    projection: PointerProperty(type=PR2_Projection)

    def has_channel_images(self):
        """True for a Fill layer with an image in any channel it writes."""
        return self.layer_type == 'FILL' and any(
            self.channel(c).image is not None for c in self.channel_ids()
        )

    def channel(self, identifier):
        """This layer's record for one channel. The single access point."""
        return getattr(self, channel_attr(identifier))

    def writes(self, identifier):
        """True when this layer contributes to `identifier`.

        An ADJUSTMENT layer answers the same way as every other layer: by its
        channel records. With the channel picker an adjustment starts on the
        channel being shown, and a
        Levels meant for Roughness no longer touches the colour. A kind that
        means nothing for a channel -- a hue shift on Height -- is still
        skipped by `adjustment.applies_to`, whatever the box says.
        """
        # The brush paints Base Color and nothing else: a
        # paint layer's other channels are not switched off, they do not exist.
        if self.layer_type == 'PAINT' and identifier != 'BASE_COLOR':
            return False
        return self.channel(identifier).enabled

    def channel_blend(self, identifier):
        """This layer's blend mode in one channel.

        Base Color's lives on the layer (see PR2_LayerChannel for why); every
        other channel's on its own record.
        """
        if identifier == 'BASE_COLOR':
            return self.blend_mode
        return self.channel(identifier).blend_mode

    def channel_opacity(self, identifier):
        """This layer's opacity in one channel. Same split as the blend."""
        if identifier == 'BASE_COLOR':
            return self.opacity
        return self.channel(identifier).opacity

    def channel_value(self, identifier):
        """The flat value this layer holds for a channel, or None (Normal)."""
        attribute = channel_value_attr(identifier)
        if attribute is None:
            return None
        return getattr(self.channel(identifier), attribute)

    def channel_ids(self):
        """Every channel this layer writes to, in CHANNELS order."""
        return [c for c in CHANNEL_IDS if self.writes(c)]

    def enable_default_channels(self):
        """Open the channels a freshly made layer writes to: Base Color only.

        A layer added through `add_layer` gets this for free. It is a method
        rather than a default on the property because a PropertyGroup pointer
        cannot carry per-instance defaults -- and because a layer built by
        hand (a test, a future import) that writes to nothing compiles to
        nothing, silently, which is a sharp edge worth one named call.
        """
        self.init_channels()
        if self.layer_type == 'GROUP':
            # A group contributes its children, never anything of its own.
            return
        self.channel('BASE_COLOR').enabled = True

    def init_channels(self):
        """Give every channel record the value and blend CHANNELS starts it on.

        The RNA defaults are one set for five channels and wrong for most of
        them -- Metallic would start at 0.5 and Height on MIX. Base Color's
        colour is left alone: `add_layer` has already chosen it.
        """
        for identifier in CHANNEL_IDS:
            record = self.channel(identifier)
            attribute = channel_value_attr(identifier)
            if attribute in ("value", "signed"):
                setattr(record, attribute, float(channel_default(identifier)))
            record.blend_mode = channel_blend_default(identifier)
            record.opacity = 1.0

    # PROCEDURAL layers draw this pattern; ignored by the other types. It is
    # always present (a PointerProperty to a PropertyGroup cannot be None) --
    # `layer_type` is what says whether it is the one being drawn.
    procedural: PointerProperty(
        name="Pattern",
        type=procedural.PR2_Procedural,
    )

    # ADJUSTMENT layers use this; ignored by the other types, and always
    # present for the same reason `procedural` is -- a PointerProperty to a
    # PropertyGroup is never None, and `layer_type` is what says it matters.
    adjustment: PointerProperty(
        name="Adjustment",
        type=adjustment.PR2_Adjustment,
    )

    opacity: FloatProperty(
        name="Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        # Its own hook, not the general one: this is the property a drag
        # writes a hundred times a second, and it is the one that never needs
        # the tree rebuilt to change. See `_opacity_changed`.
        update=_opacity_changed,
    )

    # --- mask ----------------------------------------------------------------
    # A mask is a greyscale image multiplied into the layer's mix factor:
    # white shows the layer, black hides it. Any layer type can carry one,
    # which is what makes a Fill layer useful -- paint the mask, not the fill.

    mask_image: PointerProperty(
        name="Mask",
        type=bpy.types.Image,
        update=_recompile,
    )

    # The other kind of mask: a generated pattern rather than an image. Its
    # own `enabled` is what says there is one, and the two can be on together
    # -- they multiply, so a painted mask can carve into a gradient.
    mask_procedural: PointerProperty(
        name="Procedural Mask",
        type=procedural.PR2_Procedural,
    )

    mask_enabled: BoolProperty(
        name="Use Mask",
        default=True,
        update=_recompile,
    )

    # --- the two halves of a layer -------------------------------------------
    # A layer is not one thing: it is a Color and a Mask, and every command
    # that acts "on the layer" has to be told which of the two it means. This
    # is that answer, and it is one property rather than two because the two
    # can never both be true -- a second flag would only be a way for them to
    # disagree.
    #
    # NONE is a real state, not a missing value. It is what a row is in when
    # the layer itself was clicked rather than either of its chips, and it is
    # what makes "there is nowhere to put this filter" sayable instead of
    # guessed at. Which is also why it is the default: a fresh layer has not
    # been asked about yet.
    #
    # The brush reads it too -- see `paintable_image` -- so aiming the brush
    # and choosing where a filter lands are the same gesture rather than two
    # that can drift apart.
    active_part: EnumProperty(
        name="Active Part",
        items=(
            ('NONE', "Neither", "Neither half of the layer is selected"),
            ('COLOR', "Color", "The layer's own content"),
            ('MASK', "Mask", "The layer's mask"),
        ),
        default='NONE',
        update=_sync_canvas,
    )

    # The two effect stacks. One on what the layer draws, one on what its
    # mask lets through -- the same machinery pointed at different sockets.
    # See effects.py for why an effect blends back rather than replacing.
    effects: CollectionProperty(type=effects_mod.PR2_Effect)
    mask_effects: CollectionProperty(type=effects_mod.PR2_Effect)

    def effect_stack(self, on_mask=False):
        return self.mask_effects if on_mask else self.effects

    # A fixed palette, not a free colour: Blender's collection tags are the
    # language, the eight stay readable on the list background, and a closed
    # set is what makes "filter by tag" possible later. The identifiers
    # are Blender's own so the colours can be read from its theme.
    tag_color: EnumProperty(
        name="Color Tag",
        description="A colour stripe down the left edge of the row",
        items=(
            ('NONE', "No Tag", "No stripe"),
            ('COLOR_01', "Red", ""),
            ('COLOR_02', "Orange", ""),
            ('COLOR_03', "Yellow", ""),
            ('COLOR_04', "Green", ""),
            ('COLOR_05', "Blue", ""),
            ('COLOR_06', "Violet", ""),
            ('COLOR_07', "Pink", ""),
            ('COLOR_08', "Brown", ""),
        ),
        default='NONE',
    )

    @property
    def has_mask(self):
        return self.mask_image is not None or self.mask_procedural.enabled

    @property
    def has_procedural_mask(self):
        return self.mask_procedural.enabled

    def paintable_image(self, effect_index=-1):
        """The image the brush should write into for this layer, or None.

        The brush paints Base Color and nothing else, so a PAINT layer
        that has closed its Base Color channel has nothing to paint into --
        the same as a Fill layer. Its mask is still paintable, because a mask
        belongs to the layer rather than to any one channel.

        With no part selected the content is what the brush gets. "Neither is
        selected" is a statement about the *filter* target, not a refusal to
        paint: a freshly added paint layer must be paintable without first
        having to click its own thumbnail.

        A **paint effect** wins over both when it is the selected one. That is
        the whole of what selecting it means: the brush goes where the panel
        is pointing, and pointing at a paint effect and then painting the mask
        underneath it would be the panel and the brush disagreeing.
        """
        if effect_index >= 0:
            stack = self.effect_stack(self.active_part == 'MASK')
            if effect_index < len(stack):
                effect = stack[effect_index]
                if effect.effect_type == 'PAINT':
                    return effect.image

        if self.active_part == 'MASK' and self.mask_image is not None:
            return self.mask_image
        if self.layer_type == 'PAINT' and self.writes('BASE_COLOR'):
            return self.image
        return None

    blend_mode: EnumProperty(
        name="Blend",
        items=BLEND_MODES,
        default='MIX',
        update=_recompile,
    )

    visible: BoolProperty(
        name="Visible",
        default=True,
        update=_recompile,
    )

    # --- structure -----------------------------------------------------------

    depth: IntProperty(
        name="Depth",
        description="How deeply nested in groups this layer is",
        default=0,
        min=0,
    )

    # Groups only. A collapsed group still owns its children -- they are simply
    # not drawn, which is why this changes nothing about the compiled result.
    expanded: BoolProperty(
        name="Expanded",
        default=True,
    )

    # Selection is a pointer at the stack, not a property of the picture, so
    # it triggers nothing. The *active* layer (`active_index`) is always one of
    # the selected ones; the rest are along for whatever comes next.
    selected: BoolProperty(
        name="Selected",
        default=False,
    )


class PR2_Document(PropertyGroup):
    """The stack belonging to one material."""

    layers: CollectionProperty(type=PR2_Layer)

    # --- how the stack is shown ----------------------------------------------
    # Per material, not per scene: the mode rewires *this* material's node
    # tree, so two objects can be looked at two different ways at once. None
    # of it reaches an export -- bake.py swaps in its own Emission per channel
    # and reads the group output straight, so what the surface is wired to
    # while you paint cannot change what comes out of a bake.

    # Whether this material is one PaintR2 owns the surface of. A flag, not a
    # name rule: "Set Models" now keeps the material's own name, so
    # `endswith("_paint")` stopped being able to answer -- and the flag is
    # also what makes running Set Models twice harmless, because it is what
    # says "this one has already been reset, do not reset it again".
    is_paint: BoolProperty(
        name="PaintR2 Material",
        description="This material's surface is compiled from a PaintR2 stack",
        default=False,
    )

    # Whether a mesh map bake makes this material's maps. Maps are per
    # material, so this is the one place to say "not this texture set
    # this time" -- ticked in the Objects / Materials list.
    bake_enabled: BoolProperty(
        name="Bake",
        description="Bake mesh maps for this material. Unticked materials "
        "keep the maps they have",
        default=True,
    )

    shader_mode: EnumProperty(
        name="Shader",
        description="How the compiled stack is shown on the model",
        # Numbered, because a file stores the number: 3 was the Channel mode,
        # and without the numbers Mesh Map would slide into its slot.
        items=(
            ('PBR', "PBR", "Base Color through a Principled BSDF. The look "
             "the material actually has", 'NONE', 0),
            ('UNLIT', "Unlit", "Base Color straight to the output, with no "
             "light on it at all. What a colour really is, rather than what "
             "the light is doing to it", 'NONE', 1),
            ('DIFFUSE', "Diffuse", "Lit, but matte and flat: no metal, no "
             "gloss. The form reads and the colour does not shift", 'NONE', 2),
            ('MESH_MAP', "Mesh Map", "A baked mesh map on the model, with no "
             "light and no layers. For checking a bake without leaving the "
             "window", 'NONE', 4),
        ),
        default='PBR',
        update=_rewire,
    )

    # What the mode was before a mesh map was put on the model, so the eye
    # that turned the preview on can put back what was there.
    shader_mode_previous: StringProperty(
        name="Previous Shader",
        description="Internal: the mode to go back to after a map preview",
        default='PBR',
        options={'HIDDEN'},
    )

    # Which baked map the Mesh Map mode shows. A pointer rather than a map
    # name, so the wiring does not have to rebuild a name to find it -- the
    # maps are the material's own. Display only, like the mode itself -- a
    # bake never reads it.
    preview_map: PointerProperty(
        name="Mesh Map",
        description="The baked map the Mesh Map mode puts on the model",
        type=bpy.types.Image,
        update=_rewire,
    )

    # Solo: what is being *looked at*, not what the material is. It lives on
    # the document so the compiler can read it without reaching for a scene,
    # it is cleared when a file is loaded, and a bake forces it off for its
    # duration -- so nobody ever opens a file to a grey model with no
    # explanation, and nothing exported ever carries it.

    solo_mode: EnumProperty(
        name="Solo",
        description="Show one layer, or one mask, instead of the whole stack",
        items=(
            ('NONE', "Off", "Show the whole stack"),
            ('LAYER', "Layer", "Show only the soloed layer"),
            ('MASK', "Mask", "Show the soloed layer's mask, as grey"),
        ),
        default='NONE',
        update=_recompile,
    )

    solo_index: IntProperty(
        name="Solo Layer",
        description="Which layer is being shown on its own",
        default=-1,
        update=_recompile,
    )

    # Which effect the properties section is editing. On the document, not on
    # the layer: it is a selection, and the panel has exactly one of those.
    #
    # *Which stack* it is in is not stored beside it. It used to be, as
    # `active_effect_on_mask`, and that was one flag too many: the layer
    # already says which of its halves is selected, and two records of the
    # same fact are two records that can disagree. The stack is the selected
    # part's stack, always.
    active_effect: IntProperty(default=-1)

    @property
    def active_part(self):
        """Which half of the active layer is selected: NONE, COLOR or MASK.

        NONE whenever there is no active layer, so callers can ask without
        first checking -- "no layer" and "no part of a layer" mean the same
        thing to everything that reads this.
        """
        layer = self.active_layer
        return 'NONE' if layer is None else layer.active_part

    @property
    def active_on_mask(self):
        """True when the selected part is the mask. The stack selector."""
        return self.active_part == 'MASK'

    def active_effect_item(self):
        """The effect being edited, or None.

        None whenever no part is selected, whatever `active_effect` says: the
        properties section follows the selection, and an effect on a stack
        that is not on screen is not the thing being edited.
        """
        layer = self.active_layer
        if layer is None or layer.active_part == 'NONE':
            return None
        on_mask = layer.active_part == 'MASK'
        if on_mask and not layer.has_mask:
            return None
        stack = layer.effect_stack(on_mask)
        index = self.active_effect
        return stack[index] if 0 <= index < len(stack) else None

    def set_active_part(self, part):
        """Point the panel at one half of the active layer.

        Clears the effect selection: the two stacks are different lists, so an
        index carried across from one to the other names a different effect,
        or none at all. Cheaper to drop it than to explain it.
        """
        layer = self.active_layer
        if layer is None:
            return
        if part == 'MASK' and not layer.has_mask:
            part = 'NONE'
        if layer.active_part != part:
            layer.active_part = part
        self.active_effect = -1

    def solo_target(self):
        """`(mode, index)`, with a stale or impossible solo read as off.

        The index is a position in a list that reorders and shrinks under it,
        so it is validated on every read rather than trusted -- a solo left
        pointing past the end would compile a black model.
        """
        mode = self.solo_mode
        index = self.solo_index
        if mode == 'NONE' or not (0 <= index < len(self.layers)):
            return 'NONE', -1
        if mode == 'MASK' and not self.layers[index].has_mask:
            return 'NONE', -1
        return mode, index

    def solo_clear(self):
        """Back to the whole stack. Safe to call when it already is."""
        if self.solo_mode != 'NONE':
            self.solo_mode = 'NONE'
        if self.solo_index != -1:
            self.solo_index = -1

    def participates(self, index):
        """Whether layer `index` is compiled at all, given the solo state.

        With one layer soloed, that layer, everything inside it, and the
        groups it sits in are compiled; nothing else is. The groups have to
        stay or the layer would lose the opacity and the mask it is under,
        and "solo" would quietly mean something different inside a group.
        """
        mode, solo = self.solo_target()
        if mode != 'LAYER':
            return True
        if index == solo:
            return True
        if solo < index < self.subtree_end(solo):
            return True
        return index < solo < self.subtree_end(index)

    def uses(self, identifier):
        """True when this material carries `identifier` at all.

        Every material carries all five. The question is kept
        because the compiler, the bake and the export ask it, and an unknown
        identifier must still get a "no".
        """
        return identifier in CHANNEL_IDS

    def active_channels(self):
        """Every channel this document carries, in CHANNELS order."""
        return [c for c in CHANNEL_IDS if self.uses(c)]

    # A UI cursor: it does not affect the compiled result, so no recompile --
    # but it does decide which image the brush paints into.
    #
    # Which channel the layer rows show the blend and opacity of, and which
    # channel a new effect goes to. A UI cursor like the one
    # below it: nothing compiles off it and the viewport ignores it.
    view_channel: EnumProperty(
        name="Channel",
        description="Which channel the layer rows show the blend mode and "
        "opacity of",
        items=[(entry[0], entry[1], "") for entry in CHANNELS],
        default='BASE_COLOR',
    )

    # -1 is a real state, not an error: nothing is selected. Clicking the empty
    # space under the rows puts the panel there, and it is what "add this to a
    # new layer rather than to the layer I happen to be on" needs to mean.
    active_index: IntProperty(
        name="Active Layer",
        default=0,
        update=_sync_canvas,
    )

    @property
    def active_layer(self):
        if 0 <= self.active_index < len(self.layers):
            return self.layers[self.active_index]
        return None

    @property
    def has_selection(self):
        return self.active_layer is not None

    # --- the tree, read out of the flat list ---------------------------------

    def subtree_end(self, index):
        """One past the last descendant of `index`. `index` itself if it has none."""
        layers = self.layers
        if not (0 <= index < len(layers)):
            return index

        base = layers[index].depth
        end = index + 1
        while end < len(layers) and layers[end].depth > base:
            end += 1
        return end

    def subtree(self, index):
        """The range `index` and everything under it occupies."""
        return range(index, self.subtree_end(index))

    def child_indices(self, index):
        """The direct children of the group at `index`, top first."""
        layers = self.layers
        if not (0 <= index < len(layers)):
            return []

        base = layers[index].depth
        end = self.subtree_end(index)
        out = []
        cursor = index + 1
        while cursor < end:
            if layers[cursor].depth == base + 1:
                out.append(cursor)
                cursor = self.subtree_end(cursor)
            else:
                # Only reachable from a malformed stack; `normalize` removes it.
                cursor += 1
        return out

    def root_indices(self):
        """The indices at depth 0, top first."""
        out = []
        cursor = 0
        while cursor < len(self.layers):
            if self.layers[cursor].depth == 0:
                out.append(cursor)
                cursor = self.subtree_end(cursor)
            else:
                cursor += 1
        return out

    def parent_index(self, index):
        """The group `index` sits in, or -1 when it is at the top level."""
        layers = self.layers
        if not (0 <= index < len(layers)) or layers[index].depth == 0:
            return -1

        depth = layers[index].depth
        for candidate in range(index - 1, -1, -1):
            if layers[candidate].depth < depth:
                return candidate
        return -1

    # --- selection -----------------------------------------------------------

    def selected_indices(self):
        """Every selected layer, top first. Empty when nothing is selected.

        The active layer counts as selected whether or not its flag says so:
        that keeps a document written before selection existed -- or one whose
        flags were cleared by an undo -- behaving like a single selection
        rather than like none. An active index of -1 is the deliberate empty
        case and short-circuits that fallback.
        """
        if self.active_index < 0:
            return []

        found = [
            index for index, layer in enumerate(self.layers) if layer.selected
        ]
        if found:
            return found
        return [self.active_index] if self.active_index < len(self.layers) else []

    def select_none(self):
        """Drop the whole selection. Nothing is active afterwards.

        Reachable by clicking the empty space under the last row, which is
        what every list in every editor does, and what makes "put this on a
        new layer" sayable without first finding a layer to not mean.
        """
        for layer in self.layers:
            if layer.selected:
                layer.selected = False
        self.active_index = -1
        self.active_effect = -1

    def select_only(self, index):
        """Make `index` the whole selection, and the active layer.

        The effect selection goes with it. It indexes a stack belonging to the
        layer being left behind, so carrying it over would point the
        properties section at whatever happens to sit at that position on the
        new one.
        """
        for position, layer in enumerate(self.layers):
            wanted = position == index
            if layer.selected != wanted:
                layer.selected = wanted
        if 0 <= index < len(self.layers):
            self.active_index = index
        self.active_effect = -1

    def select_extend(self, index):
        """Add or drop `index` from the selection, the way ctrl-click does.

        Dropping the active layer moves the active mark to whatever is still
        selected -- there is always exactly one active layer, or the panel has
        nothing to point its editing at.
        """
        if not (0 <= index < len(self.layers)):
            return

        # A document that never had a selection has to gain one before it can
        # be added to, or the first ctrl-click would select two layers.
        if not any(layer.selected for layer in self.layers):
            self.select_only(self.active_index)

        layer = self.layers[index]
        layer.selected = not layer.selected

        self.active_effect = -1

        if layer.selected:
            self.active_index = index
        elif index == self.active_index:
            remaining = [
                position for position, other in enumerate(self.layers)
                if other.selected
            ]
            # Ctrl-clicking the last selected row leaves nothing selected,
            # which is a state the panel now has: it is the same place the
            # empty space under the list puts you.
            self.active_index = remaining[0] if remaining else -1

    def select_range(self, index):
        """Select everything between the active layer and `index`, inclusive.

        With nothing active there is no other end to the range, so a shift
        click is read as a plain one -- the same thing a file manager does.
        """
        if not (0 <= index < len(self.layers)):
            return

        if self.active_index < 0:
            self.select_only(index)
            return

        first = min(self.active_index, index)
        last = max(self.active_index, index)
        for position, layer in enumerate(self.layers):
            wanted = first <= position <= last
            if layer.selected != wanted:
                layer.selected = wanted
        self.active_index = index
        self.active_effect = -1

    def top_blocks(self, indices):
        """`indices` reduced to the blocks that actually have to be moved.

        A layer whose group is also in the list comes along with that group,
        so it is not a block of its own -- otherwise moving a group and one of
        its children would try to move the child twice, out of and back into
        the thing carrying it. Sorted, so the blocks keep their stacking order.
        """
        out = []
        covered = -1
        for index in sorted(set(indices)):
            if not (0 <= index < len(self.layers)) or index < covered:
                continue
            out.append(index)
            covered = self.subtree_end(index)
        return out

    def move_blocks(self, starts, gap, depth):
        """Move several subtrees to `gap`, each landing at `depth`.

        `starts` are block roots in the list as it stands; `gap` is an
        insertion point in that same numbering. Returns where the first block
        ended up, or -1 if there was nothing to do.

        Done as a permutation rather than as a series of moves: every move
        shifts the indices the next one was computed from, and a block landing
        in the middle of another block is the kind of thing that only shows up
        on the fifth layer of a real stack. So the final order is worked out
        once, up front, and then realised.
        """
        layers = self.layers
        blocks = [list(range(start, self.subtree_end(start))) for start in starts]
        if not blocks:
            return -1

        moving = {index for block in blocks for index in block}

        # Dropping a block inside itself would tear the list in half.
        for block in blocks:
            if block[0] < gap < block[-1] + 1:
                return -1

        rest = [index for index in range(len(layers)) if index not in moving]
        cut = sum(1 for index in rest if index < gap)

        flat = [index for block in blocks for index in block]
        order = rest[:cut] + flat + rest[cut:]

        # Depths are read before anything moves, and written after: the whole
        # block shifts by whatever its root has to shift by, so what is inside
        # a moved group stays inside it.
        shift = {}
        for block in blocks:
            delta = depth - layers[block[0]].depth
            for index in block:
                shift[index] = delta
        wanted = [layers[index].depth + shift.get(index, 0) for index in order]

        # Realise the permutation. `current` tracks where each original index
        # has ended up as the moves shuffle things around.
        current = list(range(len(layers)))
        for position, original in enumerate(order):
            found = current.index(original)
            if found != position:
                layers.move(found, position)
                current.insert(position, current.pop(found))

        for position, value in enumerate(wanted):
            depth_value = max(0, value)
            if layers[position].depth != depth_value:
                layers[position].depth = depth_value

        return cut

    def normalize(self):
        """Put the three depth rules back. Cheap, and run after every edit.

        A flat list cannot enforce nesting on its own, so this is where the
        tree is repaired rather than trusted: depth is clamped to one more than
        the layer above, and only a GROUP is allowed to have anything under it.
        """
        previous_depth = -1
        previous_is_group = False

        for layer in self.layers:
            allowed = previous_depth + 1 if previous_is_group else previous_depth
            depth = max(0, min(layer.depth, max(0, allowed)))
            if layer.depth != depth:
                layer.depth = depth

            previous_depth = depth
            previous_is_group = layer.layer_type == 'GROUP'

    def add_layer(self, name, layer_type='PAINT', above_active=True):
        """Append a layer and return it. Index 0 is the top of the stack.

        The new layer lands beside the active one, at the same depth, so
        adding a layer while inside a group puts it in that group.

        It starts writing to Base Color only. Opening the other four by
        default would mean every new layer claiming a roughness and a metallic
        it has no opinion about, and the whole point of the per-channel switch
        is that a layer says nothing about a channel it does not touch.
        """
        layer = self.layers.add()
        layer.name = name
        layer.layer_type = layer_type

        layer.enable_default_channels()
        if layer_type == 'ADJUSTMENT':
            # An adjustment changes the channel being looked at when it was
            # added -- Base Color only if that is the one.
            for identifier in CHANNEL_IDS:
                layer.channel(identifier).enabled = (
                    identifier == self.view_channel)

        if above_active and len(self.layers) > 1:
            target = min(max(self.active_index, 0), len(self.layers) - 1)
            depth = self.layers[target].depth
            self.layers.move(len(self.layers) - 1, target)
            self.active_index = target
            self.layers[target].depth = depth
        else:
            self.active_index = len(self.layers) - 1

        self.normalize()
        self.select_only(self.active_index)
        return self.layers[self.active_index]

    def move_subtree(self, index, destination):
        """Move `index` and everything under it so it starts at `destination`.

        Returns where it ended up. `destination` is an index in the list as it
        stands *before* the move; a block moved downwards therefore lands
        earlier than the number suggests, which is what `shift` corrects.
        """
        layers = self.layers
        end = self.subtree_end(index)
        size = end - index

        destination = max(0, min(destination, len(layers) - size))
        if destination == index:
            return index

        # `CollectionProperty.move` shifts one item at a time; a block is that
        # done `size` times, which is also why the direction matters.
        if destination < index:
            for offset in range(size):
                layers.move(index + offset, destination + offset)
        else:
            for _offset in range(size):
                layers.move(index, destination + size - 1)

        return destination


CLASSES = (
    PR2_LayerChannel,
    PR2_Projection,
    PR2_Layer,
    PR2_Document,
)


def register():
    # PR2_Layer points at PR2_Procedural, PR2_Adjustment and PR2_Effect, so
    # those classes have to exist first -- and PR2_Effect points at the other
    # two, so it registers last of the three.
    procedural.register()
    adjustment.register()
    effects_mod.register()

    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Material.paintr2 = PointerProperty(
        type=PR2_Document,
        name="PaintR2",
    )


def unregister():
    if hasattr(bpy.types.Material, "paintr2"):
        del bpy.types.Material.paintr2

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

    effects_mod.unregister()
    adjustment.unregister()
    procedural.unregister()

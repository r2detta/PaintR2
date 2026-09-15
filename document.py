# SPDX-License-Identifier: GPL-3.0-or-later
"""The layer stack data model -- the single source of truth.

A document hangs off a Material (`material.paintr2`) and owns an ordered list
of layers. Index 0 is the TOP layer, matching how the stack reads on screen
and how Photoshop/Substance number things; the compiler walks it in reverse.

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

# The channels a document can carry, in the order everything uses them: the
# panel, the compiler, the export. A fixed tuple rather than a collection --
# the channel set is not the user's to extend, and adding AO or Opacity later
# is one line here plus one branch in the material wiring.
#
# `kind` decides which field of PR2_LayerChannel is read and which socket the
# group output gets. `default` is what a channel reads where no layer
# contributes -- the compiler's backdrop.
#
# Height's default is 0.5 and that is the whole of its convention: **0.5 is
# flat**, below it is a dent and above it is a bump. Storing it signed around
# zero was tried and is wrong twice over -- the value property is 0..1, so a
# dent was clamped away before it reached the compiler, and an unsigned PNG
# cannot carry a negative anyway, so the exported map came out black. Half is
# also what every engine expects to read.
#
# `colorspace` and `float_buffer` are the measured export rules: a PNG's
# bit depth comes from the image buffer, not from any save setting, and a
# Non-Color target is the difference between writing 0.31 and writing 0.60.
CHANNELS = (
    ('BASE_COLOR', "Base Color", 'COLOR', (0.8, 0.8, 0.8, 1.0), 'sRGB', False),
    ('ROUGHNESS', "Roughness", 'FLOAT', 0.5, 'Non-Color', False),
    ('METALLIC', "Metallic", 'FLOAT', 0.0, 'Non-Color', False),
    ('NORMAL', "Normal", 'COLOR', (0.5, 0.5, 1.0, 1.0), 'Non-Color', True),
    ('HEIGHT', "Height", 'FLOAT', 0.5, 'Non-Color', True),
)

CHANNEL_IDS = tuple(entry[0] for entry in CHANNELS)

# Field order matches CHANNELS, so `_channel_entry` can index either way.
_CHANNEL_BY_ID = {entry[0]: entry for entry in CHANNELS}


def channel_entry(identifier):
    """The CHANNELS row for `identifier`, or None."""
    return _CHANNEL_BY_ID.get(identifier)


def channel_label(identifier):
    entry = channel_entry(identifier)
    return entry[1] if entry else identifier.title()


def channel_kind(identifier):
    entry = channel_entry(identifier)
    return entry[2] if entry else 'FLOAT'


def channel_default(identifier):
    entry = channel_entry(identifier)
    return entry[3] if entry else 0.0


def channel_colorspace(identifier):
    entry = channel_entry(identifier)
    return entry[4] if entry else 'Non-Color'


def channel_float(identifier):
    """True for a channel whose 8 bits are visibly not enough.

    Measured: a PNG's bit depth comes from the image buffer alone, so this is
    the whole of the 16-bit decision -- Normal breaks along its gradients and
    Height bands into steps at 8 bits.
    """
    entry = channel_entry(identifier)
    return bool(entry[5]) if entry else False


def channel_suffix(identifier):
    """What an exported file for this channel is called: `_basecolor`, ..."""
    return identifier.lower().replace("_", "")


# The property name a channel's per-layer record lives under. One place, so
# renaming the scheme is one edit.
def channel_attr(identifier):
    return "ch_" + identifier.lower()


def channel_use_attr(identifier):
    return "use_" + identifier.lower()


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
    `paintr2.layers[7]` (or `paintr2.layers[7].ch_roughness`) and the number in
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
    kind = channel_kind(channel) if channel else ""
    value = self.color if kind == 'COLOR' else self.value

    if index < 0 or not channel or not nodegen.write_flat(
        owner, index, channel, value
    ):
        nodegen.request_compile(owner)


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
    if index < 0 or not nodegen.write_opacity(owner, index, self.opacity):
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

    One class for all five channels: `color` is read for a COLOR channel and
    `value` for a FLOAT one, and CHANNELS says which. Five classes would be
    the same code five times.

    `enabled` off means the layer contributes *nothing* to that channel -- no
    node is emitted for it, which is also what keeps a five-channel document
    from compiling five times the tree it needs.

    Base Color is the exception in one respect only: a PAINT layer takes its
    Base Color from `layer.image`, not from here. Every other channel of every
    layer type is a flat value, because the brush only ever paints Base Color
    -- to make a channel vary across the surface you paint the *mask*,
    which cuts all of the layer's channels at once.
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

    # 0..1 for every scalar channel. Height's midpoint means flat rather than
    # nothing (see CHANNELS), which is what keeps a dent expressible in a
    # range that has no negative half.
    value: FloatProperty(
        name="Value",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_flat_changed,
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

    def channel(self, identifier):
        """This layer's record for one channel. The single access point."""
        return getattr(self, channel_attr(identifier))

    def writes(self, identifier):
        """True when this layer contributes to `identifier`.

        An ADJUSTMENT layer is the exception, and it writes to all of them.
        It has no colour of its own to claim a channel with -- it transforms
        whatever is underneath -- so the question "does this layer touch
        Roughness" is answered by the *kind*, which `adjustment.applies_to`
        already answers and which the compiler already honours by passing the
        channel through untouched. A second answer in the form of five
        checkboxes was a way to switch off a channel the adjustment was never
        going to change anyway, and a way to leave Roughness switched off on a
        Levels layer that was supposed to be flattening it.
        """
        if self.layer_type == 'ADJUSTMENT':
            return True
        return self.channel(identifier).enabled

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
        if self.layer_type == 'GROUP':
            # A group contributes its children, never anything of its own.
            return
        self.channel('BASE_COLOR').enabled = True

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

    # --- channels ------------------------------------------------------------
    # Which channels this material has at all. Two levels of switch exist and
    # they mean different things:
    #
    #   document.use_*        does this material have such a channel
    #   layer.ch_*.enabled    does this layer contribute to it
    #
    # A channel switched off here is not compiled, not wired into Principled
    # and not exported; the per-layer values it had are kept, so switching it
    # back on returns what was there. Managed from the sidebar.
    #
    # Normal and Height start off: every material carrying a Bump chain it
    # does not need is a cost paid for nothing.

    use_base_color: BoolProperty(
        name="Base Color", default=True, update=_rewire,
        description="The painted colour of the surface",
    )
    use_roughness: BoolProperty(
        name="Roughness", default=True, update=_rewire,
        description="How rough the surface is: 0 is a mirror, 1 is chalk",
    )
    use_metallic: BoolProperty(
        name="Metallic", default=True, update=_rewire,
        description="Whether the surface behaves as a metal",
    )
    use_normal: BoolProperty(
        name="Normal", default=False, update=_rewire,
        description="A tangent-space normal map, for detail the mesh has not got",
    )
    use_height: BoolProperty(
        name="Height", default=False, update=_rewire,
        description="Surface relief, shaded through a Bump node",
    )

    # How far the Height channel pushes the surface, in scene units. Measured:
    # the Bump node's response saturates around 0.05 and the useful range
    # depends on the object's scale, so this has to be a control rather than a
    # constant.
    height_scale: FloatProperty(
        name="Height Scale",
        description="How far the Height channel displaces the shading normal",
        default=0.02,
        min=0.0,
        soft_max=0.5,
        precision=4,
        update=_recompile,
    )

    normal_scale: FloatProperty(
        name="Normal Scale",
        description="Strength of the Normal channel",
        default=1.0,
        min=0.0,
        max=4.0,
        update=_recompile,
    )

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

    shader_mode: EnumProperty(
        name="Shader",
        description="How the compiled stack is shown on the model",
        items=(
            ('PBR', "PBR", "Every channel through a Principled BSDF. The look "
             "the material actually has"),
            ('UNLIT', "Unlit", "Base Color straight to the output, with no "
             "light on it at all. What a colour really is, rather than what "
             "the light is doing to it"),
            ('DIFFUSE', "Diffuse", "Lit, but matte and flat: no metal, no "
             "gloss, no relief. The form reads and the colour does not shift"),
            ('CHANNEL', "Channel", "One channel on its own, as a grey image"),
            ('MESH_MAP', "Mesh Map", "A baked mesh map on the model, with no "
             "light and no layers. For checking a bake without leaving the "
             "window"),
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
    # name: the maps belong to the *object* and the wiring only has the
    # material, so the window resolves it once when the map is picked. Display
    # only, like the mode itself -- a bake never reads it.
    preview_map: PointerProperty(
        name="Mesh Map",
        description="The baked map the Mesh Map mode puts on the model",
        type=bpy.types.Image,
        update=_rewire,
    )

    preview_channel: EnumProperty(
        name="Preview",
        description="Which channel the Channel mode shows",
        items=[
            (identifier, label, f"Show the {label} channel on its own")
            for identifier, label, _kind, _default, _space, _float in CHANNELS
        ],
        default='BASE_COLOR',
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

    def preview_target(self):
        """The channel Channel mode should show, given what this material has.

        The stored choice when the material carries it, otherwise the first
        channel it does carry -- a preview of a channel that does not exist
        is a black model and no explanation.
        """
        active = self.active_channels()
        if not active:
            return None
        if self.preview_channel in active:
            return self.preview_channel
        return active[0]

    def uses(self, identifier):
        """True when this material carries `identifier` at all."""
        return getattr(self, channel_use_attr(identifier))

    def active_channels(self):
        """Every channel this document carries, in CHANNELS order."""
        return [c for c in CHANNEL_IDS if self.uses(c)]

    # A UI cursor: it does not affect the compiled result, so no recompile --
    # but it does decide which image the brush paints into.
    #
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

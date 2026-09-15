# SPDX-License-Identifier: GPL-3.0-or-later
"""Baking a compiled stack down to exportable textures.

An EMIT bake reads exactly the emission colour, which makes it the one bake
type that returns a channel untouched by lighting. So the recipe is: point
Emission at the channel being baked, bake, put the material back.

That used to be free -- the material *was* wired through Emission. Since the
channels arrived it is wired through a Principled BSDF instead (what you paint
is no longer what you see; roughness has to be seen under light). So the
Emission detour is now set up and torn down around each bake, by
`_channel_emission`, and the material is restored by recompiling it.

Measured facts on Blender 5.2:

* **Baking requires Cycles.** Under EEVEE `object.bake` fails outright with
  "Current render engine does not support baking", so the engine is switched
  for the duration of the bake and put back afterwards.
* `RenderSettings.engine` is a **dynamic** enum: introspecting
  `bl_rna.properties["engine"].enum_items` lists only `BLENDER_EEVEE`, yet
  assigning `'CYCLES'` works because the Cycles add-on registers its engine at
  runtime. Never decide Cycles is missing by reading that list -- try the
  assignment.
* The bake target is the **active Image Texture node at the material's top
  level**. Nodes inside a node group are not eligible, which is exactly why a
  temporary target node is added and removed around the bake.
* One `object.bake` call covers every material slot at once, each writing
  into its own target. No need to bake per material.
* A byte image with an sRGB colorspace stores *encoded* values: a linear 0.5
  reads back as ~0.737 through `image.pixels`. That is correct, not a bug --
  the PNG on disk ends up properly sRGB encoded.
"""

import contextlib
import os

import bpy

from . import dilate
from . import meshmaps
from . import nodegen


BAKE_TARGET_NAME = "PaintR2 Bake Target"

FILE_FORMATS = {
    'PNG': ".png",
    'OPEN_EXR': ".exr",
    'TARGA': ".tga",
    'JPEG': ".jpg",
}


class BakeResult:
    """What a bake produced, for the operator to report."""

    def __init__(self):
        self.images = []        # (material, image) pairs
        self.saved = []         # filepaths written
        self.error = None       # the exception that stopped the bake
        self.skipped = []       # (material name, reason)

    @property
    def ok(self):
        return self.error is None and bool(self.images)


# --- baking one channel through Emission ------------------------------------


class _SoloSuspended:
    """Turn solo off for the length of a bake, and put it back after.

    Solo is a way of looking at the stack, never a property of it. It
    lives inside the compiled group, though, which is exactly what a bake
    reads -- so without this, baking while looking at one layer would export
    that one layer and nothing would say why.
    """

    def __init__(self, materials):
        self.saved = []
        for material in materials:
            document = getattr(material, "paintr2", None)
            if document is None or document.solo_mode == 'NONE':
                continue
            self.saved.append(
                (material, document.solo_mode, document.solo_index)
            )
            document.solo_clear()
            nodegen.compile_material(material)

    def restore(self):
        for material, mode, index in self.saved:
            document = material.paintr2
            document.solo_index = index
            document.solo_mode = mode
            nodegen.compile_material(material)
        self.saved = []


class _EmissionDetour:
    """Point the material's surface at one channel, for the length of a bake.

    A context manager because the restore matters more than the setup: a bake
    that raises must not leave the user looking at a material wired to an
    Emission node. Restoring is a recompile -- the wiring is generated, so
    regenerating it is both the simplest and the most certain way back.
    """

    def __init__(self, material, channel):
        self.material = material
        self.channel = channel
        self.nodes = []

    def __enter__(self):
        from . import document as doc

        tree = self.material.node_tree
        group_node = tree.nodes.get(nodegen._OWNED["group"])
        output = nodegen._surface_output(tree)
        if group_node is None or output is None:
            raise RuntimeError("this material has no PaintR2 stack in it")

        source = group_node.outputs.get(doc.channel_label(self.channel))
        if source is None:
            raise RuntimeError(f"the stack has no {self.channel} output")

        emission = tree.nodes.new('ShaderNodeEmission')
        emission.name = "PaintR2 Bake Emission"
        emission.location = (-120.0, 320.0)
        self.nodes.append(emission)

        tree.links.new(source, emission.inputs["Color"])
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
        return self

    def __exit__(self, *exc):
        tree = self.material.node_tree
        for node in self.nodes:
            try:
                tree.nodes.remove(node)
            except (RuntimeError, ReferenceError):
                pass
        # The wiring is a generated product; regenerating it is the restore.
        nodegen.ensure_material_wiring(
            self.material, nodegen.ensure_group(self.material)
        )
        return False


# --- temporary bake targets -------------------------------------------------


def _add_bake_target(material, image):
    """Add the node `object.bake` will write into, and make it active.

    `object.bake` looks for a node that is BOTH active and selected. Getting
    that state right needs care:

    bpy_struct wrappers are transient -- `tree.nodes.active` hands back a
    *different Python object* for the same node, so `other is node` is never
    True. An `other.select = other is node` loop therefore deselects
    everything including the target, and the bake fails with "No active and
    selected image texture node found". Clear first, then set; never identity-
    compare bpy_structs.
    """
    tree = material.node_tree

    node = tree.nodes.new('ShaderNodeTexImage')
    node.name = BAKE_TARGET_NAME
    node.label = BAKE_TARGET_NAME
    node.image = image
    # Parked away from the compiled layout so it never overlaps anything.
    node.location = (400.0, 400.0)

    for other in tree.nodes:
        other.select = False

    node.select = True
    tree.nodes.active = node

    return node


def _remove_bake_target(material, node):
    if node is None:
        return
    try:
        material.node_tree.nodes.remove(node)
    except (RuntimeError, ReferenceError):
        pass


def _new_bake_image(name, size, float_buffer=False, colorspace='sRGB'):
    """A fresh image to bake into, replacing any previous one of that name."""
    existing = bpy.data.images.get(name)
    if existing is not None:
        matches = (
            tuple(existing.size) == (size, size)
            and existing.is_float == bool(float_buffer)
        )
        if matches:
            existing.colorspace_settings.name = colorspace
            return existing
        # Wrong size or wrong precision: start over rather than baking into a
        # mismatched buffer.
        bpy.data.images.remove(existing)

    image = bpy.data.images.new(
        name,
        width=size,
        height=size,
        alpha=True,
        float_buffer=float_buffer,
    )
    # Before the bake, never after: Cycles writes through whatever the target
    # says it is, and an sRGB target silently encodes the value.
    image.colorspace_settings.name = colorspace
    return image


def bake_image_name(material, channel='BASE_COLOR'):
    """`Body_paint_roughness`. The material name is already unique."""
    from . import document as doc

    return f"{material.name}_{doc.channel_suffix(channel)}"


# --- the bake ---------------------------------------------------------------


def paint_materials(obj):
    """Material slots of `obj` that actually carry a PaintR2 stack."""
    found = []
    for slot in obj.material_slots:
        material = slot.material
        if material is None:
            continue
        if not hasattr(material, "paintr2") or not len(material.paintr2.layers):
            continue
        found.append(material)
    return found


def doc_colorspace(channel):
    """The colour space a channel's bake target must be created in.

    Measured, and quiet when it is wrong: a 0.3137 emission baked into an sRGB
    target reads back as 0.5961. Every channel but Base Color is data, not a
    picture, and has to be Non-Color.
    """
    from . import document as doc

    entry = doc.channel_entry(channel)
    return entry[4] if entry else 'Non-Color'


def _wants_float(channel, forced=False):
    """Whether this channel's target needs a float buffer, i.e. 16-bit PNG.

    Measured: the bit depth of a saved PNG comes from the image buffer and
    from nothing else -- the save settings are ignored. So this one flag is
    the whole of the decision, and Normal and Height are the two channels
    8 bits visibly fails.
    """
    from . import document as doc

    return bool(forced) or doc.channel_float(channel)


def bake_channels(obj, channels, size, margin=16, float_buffer=False,
                  on_channel=None, settings=None):
    """Bake several channels, one after another. Returns {channel: BakeResult}.

    One bake per channel is the only way there is: Cycles has no bake type
    that reads "the Roughness of this stack", so each channel takes its turn
    being wired to an Emission and read back. Measured at 0.67 s per channel
    at 2K, which is what makes the loop acceptable rather than clever.

    The scene's bake settings are taken once for the whole loop rather than
    per channel -- see `bake_settings` for what that costs in the window. An
    export of several models passes its own `settings` in and they are taken
    once for all of them: six round trips became two, and two became one.

    `on_channel(channel, index)` is called before each bake. This is the only
    seam a progress report has: the bake blocks the main thread from the first
    channel to the last, so anything that wants to say how far along it is has
    to say so from in here.
    """
    channels = list(channels)
    out = {}
    scene = bpy.context.scene

    held = (
        contextlib.nullcontext(settings) if settings is not None
        else bake_settings(scene, margin)
    )

    with held as current:
        for index, channel in enumerate(channels):
            if on_channel is not None:
                on_channel(channel, index)
            out[channel] = bake_object(
                obj, size, margin=margin, float_buffer=float_buffer,
                channel=channel, settings=current,
            )
            if out[channel].error is not None:
                # One channel failing means the material is in a state the next
                # bake would inherit; stop rather than pile failures up.
                break
    return out


def bake_object(obj, size, margin=16, float_buffer=False, channel='BASE_COLOR',
                settings=None):
    """Bake one channel of every PaintR2 material on `obj` into its own image.

    Returns a BakeResult. The scene's bake settings are restored afterwards,
    the temporary target nodes are always removed and the material is always
    wired back -- even if the bake raises.

    `settings` is the dict a surrounding `bake_settings` block is already
    holding. Given one, this does not push or pop the scene's bake settings at
    all -- the caller owns them for the whole run. Left out, one bake manages
    its own, which is what a single call has always done.
    """
    result = BakeResult()

    if obj is None or obj.type != 'MESH':
        result.error = RuntimeError("Select a mesh object")
        return result

    if not obj.data.uv_layers:
        result.error = RuntimeError(f"{obj.name} has no UV map")
        return result

    materials = paint_materials(obj)
    if not materials:
        result.error = RuntimeError("No PaintR2 material on this object")
        return result

    # A channel the material does not carry has no output socket to bake, and
    # asking for one is a mistake worth naming rather than a black image.
    missing = [m.name for m in materials if not m.paintr2.uses(channel)]
    if missing:
        result.error = RuntimeError(
            f"{channel.title()} is not switched on for {', '.join(missing)}"
        )
        return result

    context = bpy.context
    scene = context.scene

    # Whatever is being looked at, the bake reads the whole stack.
    solo = _SoloSuspended(materials)

    # Anything a drag left waiting on a timer lands now: a bake cannot wait
    # for one, and reading a stack one drag out of date is silent and wrong.
    nodegen.flush()

    # Make sure the shader matches the stack before reading it.
    for material in materials:
        nodegen.compile_material(material)

    targets = []
    detours = []
    for material in materials:
        image = _new_bake_image(
            bake_image_name(material, channel), size,
            float_buffer=_wants_float(channel, float_buffer),
            colorspace=doc_colorspace(channel),
        )
        targets.append((material, image, _add_bake_target(material, image)))
        detours.append(_EmissionDetour(material, channel))

    owns_settings = settings is None
    previous = _push_bake_settings(scene, margin) if owns_settings else settings
    previous_selection = [o for o in context.view_layer.objects if o.select_get()]
    previous_active = context.view_layer.objects.active

    try:
        select_only(context, obj)

        if previous.get("engine") is None and scene.render.engine != BAKE_ENGINE:
            raise RuntimeError(
                "Baking needs the Cycles render engine, which is not available "
                "in this Blender build"
            )

        for detour in detours:
            detour.__enter__()

        for _material, image, _node in targets:
            dilate.blank(image)

        bpy.ops.object.bake(type='EMIT')

        # Everything past the margin is still black with no alpha, and an
        # exported texture carries that black into whatever renders it: the
        # first mip level averages it into the island's edge texels and the
        # seam shows as a dark line. Filling is part of writing the texture,
        # not a polish step -- see dilate.py.
        for _material, image, _node in targets:
            dilate.fill(image)

        result.images = [(material, image) for material, image, _ in targets]
    except Exception as error:  # noqa: BLE001
        result.error = error
    finally:
        for detour in detours:
            detour.__exit__(None, None, None)

        for material, _, node in targets:
            _remove_bake_target(material, node)

        solo.restore()

        if owns_settings:
            _pop_bake_settings(scene, previous)

        for scene_object in context.view_layer.objects:
            scene_object.select_set(scene_object in previous_selection)
        context.view_layer.objects.active = previous_active

    return result


def bake_mask(obj, material, layer, margin=4):
    """Freeze a layer's procedural mask into its painted mask image.

    A pattern that can be re-edited forever is the point of a procedural --
    until the moment you want to paint on it. Then it has to become
    pixels, and this is where that happens: the pattern is baked through
    Cycles the same way a channel is, straight into the layer's mask image.

    The layer keeps both masks and they multiply, so the procedural is turned
    *off* afterwards -- leaving it on would apply the same pattern twice.

    Returns a `BakeResult`. The material is left exactly as it was found.
    """
    from . import procedural
    from . import storage

    result = BakeResult()

    if not layer.mask_procedural.enabled:
        result.error = RuntimeError("that layer has no procedural mask")
        return result

    tree = material.node_tree
    output = nodegen._surface_output(tree)
    if output is None:
        result.error = RuntimeError("this material has no output to bake from")
        return result

    image = layer.mask_image
    if image is None:
        from . import prepare

        image, _ = prepare.ensure_image(
            prepare.unique_image_name(f"{material.name}_{layer.name}_mask"),
            int(bpy.context.scene.paintr2.image_resolution),
            (1.0, 1.0, 1.0, 1.0),
            colorspace='Non-Color',
        )
        layer.mask_image = image

    context = bpy.context
    scene = context.scene

    nodegen.flush()

    made = []
    target = None
    previous = _push_bake_settings(scene, margin)
    previous_selection = [o for o in context.view_layer.objects if o.select_get()]
    previous_active = context.view_layer.objects.active

    try:
        if previous.get("engine") is None and scene.render.engine != BAKE_ENGINE:
            raise RuntimeError(
                "Baking needs the Cycles render engine, which is not available "
                "in this Blender build"
            )

        before = set(tree.nodes)
        factor = procedural.build_factor(
            tree, layer.mask_procedural, -1400.0, 900.0
        )
        made = [node for node in tree.nodes if node not in before]
        if factor is None:
            raise RuntimeError("that pattern cannot be baked")

        emission = tree.nodes.new('ShaderNodeEmission')
        emission.name = "PaintR2 Mask Bake"
        emission.location = (-200.0, 900.0)
        made.append(emission)

        tree.links.new(factor, emission.inputs["Color"])
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])

        target = _add_bake_target(material, image)

        select_only(context, obj)

        dilate.blank(image)
        bpy.ops.object.bake(type='EMIT')
        dilate.fill(image)

        # The pattern is in the pixels now; leaving it switched on as well
        # would multiply the same mask by itself.
        layer.mask_procedural.enabled = False
        layer.mask_enabled = True

        storage.adopt_new_image(image)
        result.images = [(material, image)]
    except Exception as error:  # noqa: BLE001
        result.error = error
    finally:
        _remove_bake_target(material, target)
        for node in made:
            try:
                tree.nodes.remove(node)
            except (RuntimeError, ReferenceError):
                pass

        # The wiring is generated; regenerating it is the restore.
        nodegen.compile_material(material)

        _pop_bake_settings(scene, previous)

        for scene_object in context.view_layer.objects:
            scene_object.select_set(scene_object in previous_selection)
        context.view_layer.objects.active = previous_active

    return result


def bake_mask_result(obj, material, layer, size=0, margin=4):
    """Bake a layer's *finished* mask into a new image. Returns a BakeResult.

    The finished one: painted pixels, times the procedural pattern, through
    the whole mask effect stack. That is what the model is actually cut by,
    and it is the only thing worth calling "this layer's mask".

    Not `bake_mask`, which freezes a pattern *into* the layer's own mask image
    and switches the pattern off. This one touches nothing -- the image it
    produces belongs to the caller, and the layer is exactly as it was found.

    `size` of 0 means the mask image's own resolution, which keeps an export
    of a plain painted mask at the size it was painted at.
    """
    from . import storage

    result = BakeResult()

    if obj is None or obj.type != 'MESH':
        result.error = RuntimeError("Select a mesh object")
        return result
    if not obj.data.uv_layers:
        result.error = RuntimeError(f"{obj.name} has no UV map")
        return result
    if not layer.has_mask and not len(layer.mask_effects):
        result.error = RuntimeError("that layer has no mask")
        return result

    tree = material.node_tree
    output = nodegen._surface_output(tree)
    if output is None:
        result.error = RuntimeError("this material has no output to bake from")
        return result

    if not size:
        painted = layer.mask_image
        size = painted.size[0] if painted is not None else int(
            bpy.context.scene.paintr2.image_resolution
        )

    context = bpy.context
    scene = context.scene

    nodegen.flush()

    made = []
    target = None
    image = None
    # A mask switched off is still a mask, and exporting one is a perfectly
    # ordinary thing to ask for -- the switch is about what the model shows,
    # not about what the mask is. So it is turned on for the bake and put back.
    was_enabled = layer.mask_enabled
    previous = _push_bake_settings(scene, margin)
    previous_selection = [o for o in context.view_layer.objects if o.select_get()]
    previous_active = context.view_layer.objects.active

    try:
        if previous.get("engine") is None and scene.render.engine != BAKE_ENGINE:
            raise RuntimeError(
                "Baking needs the Cycles render engine, which is not available "
                "in this Blender build"
            )

        if not was_enabled:
            layer.mask_enabled = True

        before = set(tree.nodes)
        # The compiler's own mask builder, pointed at the material's tree
        # instead of at the generated group. It emits nothing group-specific,
        # so the same function that decides what the mask *is* decides what
        # gets baked -- there is no second description to drift out of step.
        factor = nodegen._mask_socket(tree, layer, {}, 0, -1400.0, 900.0)
        made = [node for node in tree.nodes if node not in before]
        if factor is None:
            raise RuntimeError("that mask has nothing to bake")

        emission = tree.nodes.new('ShaderNodeEmission')
        emission.name = "PaintR2 Mask Export"
        emission.location = (-200.0, 900.0)
        made.append(emission)

        tree.links.new(factor, emission.inputs["Color"])
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])

        image = _new_bake_image(
            f"{material.name}_{layer.name}_mask_export", size,
            float_buffer=False, colorspace='Non-Color',
        )
        target = _add_bake_target(material, image)

        select_only(context, obj)
        dilate.blank(image)
        bpy.ops.object.bake(type='EMIT')
        dilate.fill(image)

        result.images = [(material, image)]
        image = None
    except Exception as error:  # noqa: BLE001
        result.error = error
    finally:
        if image is not None:
            # It never became a result, so it must not be left in the file.
            try:
                bpy.data.images.remove(image)
            except (RuntimeError, ReferenceError):
                pass

        _remove_bake_target(material, target)
        for node in made:
            try:
                tree.nodes.remove(node)
            except (RuntimeError, ReferenceError):
                pass

        if layer.mask_enabled != was_enabled:
            layer.mask_enabled = was_enabled

        # The wiring is generated; regenerating it is the restore.
        nodegen.compile_material(material)

        _pop_bake_settings(scene, previous)

        for scene_object in context.view_layer.objects:
            scene_object.select_set(scene_object in previous_selection)
        context.view_layer.objects.active = previous_active

    return result


BAKE_ENGINE = 'CYCLES'


def select_only(context, obj):
    """Make `obj` the whole selection, without going through an operator.

    **MEASURED: `bpy.ops.object.select_all` fails in Texture Paint mode** --
    "context is incorrect" -- and the model is in Texture Paint mode for the
    whole time the PaintR2 window is open. Every bake started from inside the
    window therefore died on its first line, which is exactly what "bake does
    not happen in the add-on interface" was.

    `select_set` has no such restriction. It is also the shorter path: the
    operator only ever existed here to clear a selection.
    """
    for scene_object in context.view_layer.objects:
        if scene_object.select_get():
            scene_object.select_set(False)

    if obj is not None:
        obj.select_set(True)
        context.view_layer.objects.active = obj


def _push_bake_settings(scene, margin):
    """Apply the settings a clean EMIT bake needs, returning the old ones."""
    bake = scene.render.bake
    previous = {
        "margin": bake.margin,
        "use_clear": bake.use_clear,
        "target": bake.target,
        "engine": scene.render.engine,
    }

    bake.margin = margin
    # Off, so `_blank` can do the clearing instead. Blender's own clear blanks
    # to (0, 0, 0, 1) -- opaque black -- which leaves every texel claiming to
    # have been written and the alpha channel useless as a coverage mask.
    # `dilate.fill` needs that mask; see dilate.blank for the measurement.
    bake.use_clear = False
    bake.target = 'IMAGE_TEXTURES'

    # EEVEE cannot bake at all. The enum introspects as EEVEE-only even when
    # Cycles is installed, so the only way to find out is to try.
    if scene.render.engine != BAKE_ENGINE:
        try:
            scene.render.engine = BAKE_ENGINE
        except TypeError:
            previous["engine"] = None  # nothing to restore; Cycles is absent

    if hasattr(scene, "cycles"):
        previous["samples"] = scene.cycles.samples
        # EMIT is a straight shader evaluation; extra samples buy nothing.
        scene.cycles.samples = 1

        # One sample, but still one sample per texel of a 4K image across
        # every material on the object. `scene.cycles.device` defaults to CPU
        # and the preference the user set years ago does not move it; see
        # meshmaps._bake_device.
        previous["device"] = scene.cycles.device
        scene.cycles.device = meshmaps._bake_device()

    return previous


def _pop_bake_settings(scene, previous):
    bake = scene.render.bake
    bake.margin = previous["margin"]
    bake.use_clear = previous["use_clear"]
    bake.target = previous["target"]

    if "device" in previous and hasattr(scene, "cycles"):
        scene.cycles.device = previous["device"]

    if previous.get("engine"):
        scene.render.engine = previous["engine"]

    if "samples" in previous and hasattr(scene, "cycles"):
        scene.cycles.samples = previous["samples"]


@contextlib.contextmanager
def bake_settings(scene, margin):
    """Hold the EMIT bake settings for a whole run of bakes, not one.

    `_push_bake_settings` switches `render.engine` to Cycles and back, and
    doing that per channel is what an export used to cost: six round trips for
    two models of three channels. In a background Blender that is free -- there
    is no viewport to re-sync. In the window it is not: the PaintR2 region *is*
    a 3D viewport showing the very material being rewired, so each switch makes
    Blender drop and rebuild the draw engine's state for it.

    So the settings are taken once around the whole export and `bake_object` is
    told not to take them again. It still takes them for itself when called
    alone -- every test and the mask bake rely on that, and a single bake has
    nothing to amortise.
    """
    previous = _push_bake_settings(scene, margin)
    try:
        yield previous
    finally:
        _pop_bake_settings(scene, previous)


def cycles_available():
    """True when the engine can be switched to Cycles, i.e. baking can work."""
    scene = bpy.context.scene
    if scene is None:
        return False
    if scene.render.engine == BAKE_ENGINE:
        return True

    original = scene.render.engine
    try:
        scene.render.engine = BAKE_ENGINE
    except TypeError:
        return False
    finally:
        try:
            scene.render.engine = original
        except TypeError:
            pass
    return True


# --- saving -----------------------------------------------------------------


def save_image(image, directory, file_format='PNG'):
    """Write `image` into `directory`, returning the absolute path."""
    directory = bpy.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)

    extension = FILE_FORMATS.get(file_format, ".png")
    path = os.path.join(directory, image.name + extension)

    image.filepath_raw = path
    image.file_format = file_format
    image.save()

    return path

# SPDX-License-Identifier: GPL-3.0-or-later
"""What the painting session shows, and what it hides.

Two separate jobs, both about looking rather than about the document:

**Isolation.** While the window is open only the models in `Paint` are
visible; everything else in the scene is hidden and put back on close. What
was hidden is recorded on the scene rather than in memory, because
memory does not survive a crash and an unrecoverable scene full of invisible
objects is a worse failure than any it prevents. Only objects *we* hid are
recorded, so a model the user had already hidden stays hidden afterwards.

**Fade.** The model being worked on is drawn as it is; the rest of the set
is **darkened** so it still reads as context without competing. Darkened and
not made see-through: transparency was tried and it went wrong three ways at
once (D147b), all of them consequences of a transparent surface having to be
sorted against itself. One shader stage, built by `nodegen` and driven by
`object.color`'s alpha -- an Object Info node reads it per object, so two
models sharing a material would still darken independently.

A bake needs the things it reads to be visible, so `suspended()` puts
everything back for the duration and re-isolates afterwards.
"""

import contextlib

import bpy

from . import paintset


# What `object.color`'s alpha was before we wrote a fade into it, by object
# name. Cosmetic state: if a crash loses it the alpha is restored to 1.0,
# which is what it is on every object nobody has deliberately coloured.
_original_alpha = {}



def _scene_props(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return scene.paintr2 if scene is not None else None


# --- isolation ----------------------------------------------------------------


def isolate(context=None):
    """Hide everything that is not a model in the paint set. Returns the count.

    Additive on purpose: running it a second time (after Set Models added a
    model, say) hides what is new and leaves the record of what was hidden
    before intact. Un-hiding is `restore`'s job and nobody else's.
    """
    context = context or bpy.context
    props = _scene_props(context)
    view_layer = getattr(context, "view_layer", None)
    if props is None or view_layer is None:
        return 0

    recorded = {entry.name for entry in props.hidden_objects}
    hidden = 0

    for obj in view_layer.objects:
        # An object in a collection excluded from the view layer has no
        # visibility to set, and asking raises. There is nothing to hide
        # there either -- it is already not being drawn.
        try:
            already_hidden = obj.hide_get(view_layer=view_layer)
        except RuntimeError:
            continue

        if paintset.in_set(obj):
            # A model that was hidden before the session started is shown
            # now: it is in the set, and the set is what the window is for.
            if obj.name in recorded and already_hidden:
                with contextlib.suppress(RuntimeError):
                    obj.hide_set(False, view_layer=view_layer)
            continue

        if already_hidden:
            continue

        try:
            obj.hide_set(True, view_layer=view_layer)
        except RuntimeError:
            continue

        if obj.name not in recorded:
            props.hidden_objects.add().name = obj.name
            recorded.add(obj.name)
        hidden += 1

    return hidden


def restore(context=None):
    """Show again everything `isolate` hid, and forget the record."""
    context = context or bpy.context
    props = _scene_props(context)
    view_layer = getattr(context, "view_layer", None)
    if props is None:
        return 0

    shown = 0
    for entry in props.hidden_objects:
        obj = bpy.data.objects.get(entry.name)
        if obj is None:
            continue
        try:
            obj.hide_set(False, view_layer=view_layer)
        except RuntimeError:
            # Not in this view layer any more. Nothing to show, nothing to
            # report -- the record is about to be dropped either way.
            continue
        shown += 1

    props.hidden_objects.clear()
    clear_fade(context)
    return shown


def is_isolated(context=None):
    props = _scene_props(context)
    return bool(props is not None and len(props.hidden_objects))


@contextlib.contextmanager
def suspended(context=None):
    """Everything visible for the duration, then isolated again.

    A bake reads geometry through the view layer: a hidden high-poly is not
    in the depsgraph, so a mesh-map bake against one produces nothing and
    says nothing. This is the wrapper every bake goes through.
    """
    context = context or bpy.context
    props = _scene_props(context)
    view_layer = getattr(context, "view_layer", None)

    names = [entry.name for entry in props.hidden_objects] if props else []
    was_isolated = bool(names)

    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        with contextlib.suppress(RuntimeError):
            obj.hide_set(False, view_layer=view_layer)

    try:
        yield
    finally:
        if was_isolated:
            for name in names:
                obj = bpy.data.objects.get(name)
                if obj is None:
                    continue
                with contextlib.suppress(RuntimeError):
                    obj.hide_set(True, view_layer=view_layer)


# --- fade ---------------------------------------------------------------------


def fade_enabled(context=None):
    props = _scene_props(context)
    return bool(props is not None and props.display.fade_inactive)


def _materials_of(obj):
    """The materials `obj` is drawn with, without duplicates."""
    out = []
    for slot in obj.material_slots:
        material = slot.material
        if material is not None and material.name not in out:
            out.append(material.name)
    return out


def faded_materials(context=None):
    """The materials that should be drawn faded: every one the active model
    is *not* using.

    A material shared between the active model and another one does not
    qualify. There is one node tree behind both, so it cannot be opaque for
    one and see-through for the other -- and of the two possible mistakes,
    leaving the model being painted looking exactly right is the better one.
    """
    context = context or bpy.context
    active = getattr(context, "active_object", None)

    protected = set(_materials_of(active)) if active is not None else set()

    wanted = set()
    for obj in paintset.paint_objects():
        if obj is active:
            continue
        wanted.update(_materials_of(obj))
    return wanted - protected


def sync_fade(context=None):
    """Point the fade at the active model, alphas and shaders together.

    Two halves, and both have to move at once. The alpha is what says how
    faded a model is; the shader stage is what reads it. A material that is
    not one of the faded ones is compiled with no transparency in it at all
    -- see `nodegen._fade_is_on` for why that matters rather than setting a
    factor to one.

    Cheap when nothing changed: the recompile only runs for materials whose
    faded-or-not answer actually flipped, which is what makes this safe to
    call from the draw callback on every redraw.
    """
    from . import nodegen

    context = context or bpy.context
    props = _scene_props(context)
    if props is None:
        return

    objects = paintset.paint_objects()
    if not objects:
        nodegen.set_faded_materials(())
        return

    if not props.display.fade_inactive:
        clear_fade(context)
        return

    wanted = faded_materials(context)
    nodegen.set_faded_materials(wanted)

    # Every material in the set, not only the wanted ones: a material that
    # has just stopped being faded has to lose the stage as well.
    for name in _set_materials():
        material = bpy.data.materials.get(name)
        if material is None:
            continue
        if nodegen.carries_fade(material) != (name in wanted):
            nodegen.request_compile(material)

    active = getattr(context, "active_object", None)
    amount = props.display.fade_amount

    for obj in objects:
        if obj.name not in _original_alpha:
            _original_alpha[obj.name] = obj.color[3]
        wanted_alpha = 1.0 if obj is active else amount
        if obj.color[3] != wanted_alpha:
            obj.color[3] = wanted_alpha


def _set_materials():
    """Every material name used by a model in the set, without duplicates."""
    names = []
    for obj in paintset.paint_objects():
        for name in _materials_of(obj):
            if name not in names:
                names.append(name)
    return names


def recompile_set():
    """Rebuild every material in the set, so the fade stage follows the flag.

    Called when the session closes, and whenever the option is switched.
    Without the close call a saved file keeps a Transparent BSDF in every
    material that happened to be faded and renders through it.
    """
    from . import nodegen

    for name in _set_materials():
        material = bpy.data.materials.get(name)
        if material is not None and getattr(material, "paintr2", None) is not None:
            nodegen.compile_material(material)


def clear_fade(context=None):
    """Put every alpha we wrote back the way it was, and drop the fade set."""
    from . import nodegen

    nodegen.set_faded_materials(())

    known = set(_original_alpha)
    for name, alpha in list(_original_alpha.items()):
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.color[3] = alpha
    _original_alpha.clear()

    # Anything in the set we have no record for: the record is memory and the
    # session may have outlived it. 1.0 is the only defensible guess, and an
    # object left at 0.35 alpha with no window open is a bug nobody can see
    # the cause of.
    for obj in paintset.paint_objects():
        if obj.name not in known and obj.color[3] != 1.0:
            obj.color[3] = 1.0

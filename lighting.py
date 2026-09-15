# SPDX-License-Identifier: GPL-3.0-or-later
"""The scene world PaintR2 lights the model with, and the transform on it.

Blender's Material Preview lights the viewport with a *studiolight* -- an HDRI
the viewport owns rather than the scene. It has exactly one control that moves
the light: `studiolight_rotate_z`. There is no tilt, no pitch, no way to bring
the key light down over the model, because the property does not exist
(measured on 5.2, `View3DShading` carries `studiolight_rotate_z` and nothing
else). So a real transform cannot be built on top of it.

What can carry a transform is a **world**, because a world is a node tree:

    [Tex Coord] Generated -> [Mapping] -> [Environment] -> [Background] -\
                              rotation      hdri image      strength      \
                                                                       [Mix] -> [Output]
    [RGB backdrop] -> [Background] --------------------------------------/
    [Light Path] Is Camera Ray -> Mix.Fac

So this module owns one world, named `PaintR2 Env`, and rebuilds its node tree
from `scene.paintr2.display` whenever a control moves. The scene's own world is
remembered by name and put back when PaintR2 stops driving the lighting -- the
lighting is a painting-session setting, not an edit to the file's world.

Two things are worth knowing about the tree above.

**The Mapping node is what makes the HDRI turn.** An Environment Texture with
nothing in its Vector socket is mapped with Z up and cannot be moved; feeding
it `Generated` off a Texture Coordinate node (which, in a world, is the view
direction) through a Mapping node makes all three rotations real.

**The background is hidden with a Light Path, not with a viewport setting.**
`Is Camera Ray` is 1 only for the ray that reaches the eye, so mixing a flat
colour in on that ray hides the HDRI from view while it keeps lighting the
model exactly as before. That works in EEVEE and in Cycles and does not care
which shading mode the viewport is in, which no viewport background setting
can claim.

The whole tree is cleared and rebuilt rather than patched. That is safe here
in a way it is not in a material: nothing in this world is the user's -- the
world itself is ours, made by us, named by us, and handed back when we are
done.
"""

import os

import bpy


WORLD_NAME = "PaintR2 Env"


# --- the HDRIs Blender ships ------------------------------------------------


def studio_hdris(context=None):
    """[(path, label)] of every world studiolight Blender knows about.

    Blender's own Material Preview list, read from preferences rather than
    typed here, so a studiolight the user has installed appears too.
    """
    context = context or bpy.context
    prefs = getattr(context, "preferences", None)
    lights = getattr(prefs, "studio_lights", None)
    if lights is None:
        return []

    out = []
    for light in lights:
        if getattr(light, "type", None) != 'WORLD':
            continue
        path = getattr(light, "path", "")
        if not path:
            continue
        out.append((path, _pretty(light.name)))
    return sorted(out, key=lambda item: item[1].lower())


def _pretty(name):
    """`forest.exr` -> `Forest`."""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    return stem.replace("_", " ").title() or name


def hdri_label(path):
    """What the panel writes beside "HDRI" for `path`."""
    if not path:
        return "None"
    return _pretty(path)


def current_studio_hdri(space):
    """The HDRI the viewport is lighting with right now, or "".

    This is what seeds the world the first time: "the map already in use" is
    the one the user is looking at, not whichever one happens to sort first.
    """
    shading = getattr(space, "shading", None)
    light = getattr(shading, "selected_studio_light", None)
    path = getattr(light, "path", "") if light is not None else ""
    if path and getattr(light, "type", None) != 'WORLD':
        # A matcap or a solid-mode studio light is not an environment map.
        return ""
    return path


# --- the image --------------------------------------------------------------


def load_hdri(path):
    """The Image for `path`, loaded once and reused. None when it will not open."""
    if not path:
        return None

    absolute = bpy.path.abspath(path)
    if not os.path.isfile(absolute):
        return None

    for image in bpy.data.images:
        if image.filepath and bpy.path.abspath(image.filepath) == absolute:
            return image

    try:
        image = bpy.data.images.load(absolute, check_existing=True)
    except RuntimeError:
        return None

    # An HDRI is light, not a picture: it must not come back through a view
    # transform. Blender loads .exr/.hdr as Linear Rec.709 already, so this
    # only matters for someone pointing the field at a .png.
    return image


# --- the world --------------------------------------------------------------


def find_world():
    return bpy.data.worlds.get(WORLD_NAME)


def ensure_world():
    world = find_world()
    if world is None:
        world = bpy.data.worlds.new(WORLD_NAME)
    world.use_nodes = True
    return world


def _node(tree, idname, name, location):
    node = tree.nodes.new(idname)
    node.name = name
    node.label = name
    node.location = location
    return node


def build(world, settings):
    """Rebuild `world`'s node tree from `settings`. Returns the world.

    Cleared and rebuilt every time. This world is ours from the moment it is
    made, so there is nothing in it to preserve, and a rebuild is a dozen
    nodes -- far cheaper than the bookkeeping that patching would need.
    """
    tree = world.node_tree
    tree.nodes.clear()

    output = _node(tree, 'ShaderNodeOutputWorld', "PaintR2 World Output",
                   (600.0, 0.0))

    environment = _node(tree, 'ShaderNodeBackground', "PaintR2 Environment",
                        (360.0, 60.0))
    environment.inputs["Strength"].default_value = float(settings.strength)

    image = load_hdri(settings.hdri_path)
    if image is None:
        # No map, or a path that will not open. A flat grey still lights the
        # model and still dims with Strength, so the controls keep meaning
        # something; only the direction stops mattering, which is honest.
        source = _node(tree, 'ShaderNodeRGB', "PaintR2 Flat", (140.0, 60.0))
        source.outputs["Color"].default_value = (0.5, 0.5, 0.5, 1.0)
        tree.links.new(source.outputs["Color"], environment.inputs["Color"])
    else:
        texture = _node(tree, 'ShaderNodeTexEnvironment',
                        "PaintR2 Environment Texture", (140.0, 60.0))
        texture.image = image

        # Without a Vector the environment is nailed to Z-up and no rotation
        # exists to write. This is the whole reason the world is here.
        mapping = _node(tree, 'ShaderNodeMapping', "PaintR2 Mapping",
                        (-120.0, 60.0))
        mapping.vector_type = 'POINT'
        mapping.inputs["Rotation"].default_value = (
            float(settings.rotation_x),
            float(settings.rotation_y),
            float(settings.rotation_z),
        )

        coordinates = _node(tree, 'ShaderNodeTexCoord', "PaintR2 Coordinates",
                            (-360.0, 60.0))

        tree.links.new(coordinates.outputs["Generated"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
        tree.links.new(texture.outputs["Color"], environment.inputs["Color"])

    if settings.show_background:
        tree.links.new(environment.outputs["Background"], output.inputs["Surface"])
        return world

    # Hidden background: the HDRI still lights the model, the eye sees a flat
    # colour. Is Camera Ray is 1 only along the ray that reaches the eye.
    backdrop = _node(tree, 'ShaderNodeBackground', "PaintR2 Backdrop",
                     (360.0, -160.0))
    colour = tuple(settings.background_color)
    backdrop.inputs["Color"].default_value = colour + (1.0,) * (4 - len(colour))
    backdrop.inputs["Strength"].default_value = 1.0

    light_path = _node(tree, 'ShaderNodeLightPath', "PaintR2 Light Path",
                       (140.0, -320.0))
    mix = _node(tree, 'ShaderNodeMixShader', "PaintR2 Background Mix",
                (460.0, -40.0))

    tree.links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    tree.links.new(environment.outputs["Background"], mix.inputs[1])
    tree.links.new(backdrop.outputs["Background"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
    return world


# --- driving the viewport ----------------------------------------------------


# Saved and put back as a set. `use_scene_world` is what makes the viewport
# look at our world at all; the lights pair is a control of ours, and a control
# we write is a control we owe back.
_SHADING_FLAGS = (
    "use_scene_world",
    "use_scene_world_render",
    "use_scene_lights",
    "use_scene_lights_render",
)


def install(context, space):
    """Point the scene and the viewport at our world. Idempotent.

    What was there before is remembered on the scene, not in memory: a crash
    or a reload must not be able to lose the user's world.
    """
    scene = getattr(context, "scene", None)
    if scene is None:
        return None

    settings = scene.paintr2.display

    if settings.hdri_path == "":
        seeded = current_studio_hdri(space)
        if not seeded:
            presets = studio_hdris(context)
            seeded = presets[0][0] if presets else ""
        settings.hdri_path = seeded

    world = build(ensure_world(), settings)

    if scene.world is not world:
        settings.saved_world = scene.world.name if scene.world else ""
        scene.world = world

    shading = getattr(space, "shading", None)
    if shading is not None:
        if not settings.saved_shading:
            settings.saved_shading = ",".join(
                "1" if getattr(shading, flag, False) else "0"
                for flag in _SHADING_FLAGS
            )
        _write(shading, "use_scene_world", True)
        _write(shading, "use_scene_world_render", True)
        _write(shading, "use_scene_lights", bool(settings.use_scene_lights))
        _write(shading, "use_scene_lights_render", bool(settings.use_scene_lights))

    return world


def restore(context, space):
    """Hand the scene's own world back and undo the shading flags."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return

    settings = scene.paintr2.display

    if scene.world is not None and scene.world.name == WORLD_NAME:
        scene.world = bpy.data.worlds.get(settings.saved_world)
    settings.saved_world = ""

    shading = getattr(space, "shading", None)
    saved = settings.saved_shading
    if shading is not None and saved:
        flags = saved.split(",")
        for flag, value in zip(_SHADING_FLAGS, flags):
            _write(shading, flag, value == "1")
    settings.saved_shading = ""


def refresh(context, space):
    """Re-apply whatever the settings currently say. Cheap enough per edit."""
    scene = getattr(context, "scene", None)
    if scene is None:
        return

    settings = scene.paintr2.display
    if settings.use_lighting:
        install(context, space)
    else:
        restore(context, space)


def _write(holder, key, value):
    try:
        setattr(holder, key, value)
    except (AttributeError, TypeError):
        pass

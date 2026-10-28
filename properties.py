# SPDX-License-Identifier: GPL-3.0-or-later
"""RNA data model. This is the part that *is* saved into the .blend.

Only the persistent side of PaintR2 belongs here, and only the scene-level
half of it: the interface geometry that should survive closing the window, and
the settings the sidebar and the bake/export paths read -- fill, display,
mesh maps, high-poly, ambient occlusion, curvature.

The layer stack itself is not here. A document hangs off its material
(`material.paintr2`) and lives in document.py; the live window is runtime-only
and lives in session.py.
"""

import math

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


class PR2_UIState(PropertyGroup):
    """Persisted geometry of the custom interface, in unscaled pixels."""

    right_panel_width: IntProperty(
        name="Layer Panel Width",
        # Wide enough for a group row to show its blend chip. At 300 the name
        # column on an indented row ran out first and the chip was the thing
        # dropped, so a group looked like it had no blend mode at all.
        default=340,
        min=200,
        max=640,
    )
    show_right_panel: BoolProperty(name="Show Layer Panel", default=True)

    # The Properties section under the layer list. A height the user drags
    # its top edge to, not one fitted to whatever the active layer happens to
    # show: a section that grew and shrank with every click moved the list
    # under the pointer. Longer content scrolls.
    properties_height: IntProperty(
        name="Properties Height",
        default=220,
        min=60,
        max=1200,
    )

    # The Objects / Materials panel above the stack. Its own switch rather
    # than sharing the column's: with one model and one material there is
    # nothing in it to choose between, and the room is better spent on layers.
    show_targets_panel: BoolProperty(
        name="Show Objects / Materials",
        description="The panel above the layer stack: which model is being "
        "painted, and which of its materials",
        default=True,
    )

    # The brush properties, between the viewport and the layer stack. The
    # strip's popovers are shortcuts -- open, change one thing, close; this is
    # the other way of working, left open beside the layers while painting.
    # Same rows either way, so the two can never disagree.
    brush_panel_width: IntProperty(
        name="Brush Panel Width",
        # Wider than the Display panel: its rows are Blender's own brush
        # settings, and those carry the longest labels in the window --
        # "Adjust Strength for Spacing" sets the floor.
        default=300,
        min=240,
        max=560,
    )
    show_brush_panel: BoolProperty(name="Show Brush Properties", default=False)

    # The Display panel, on the other side of the viewport from the layers.
    # Its own width, because it holds property rows rather than a list and
    # wants less room than the stack does.
    left_panel_width: IntProperty(
        name="Display Panel Width",
        default=250,
        min=200,
        max=520,
    )
    show_left_panel: BoolProperty(name="Show Display Panel", default=False)

    # One panel, two pages. They share the column because only one of them is
    # ever in use, and a second panel would take a third of the window to say
    # so. Two header buttons open it; pressing the other one turns the page.
    left_panel_page: EnumProperty(
        name="Left Panel",
        items=(
            ('DISPLAY', "Display", "Lighting and shader mode"),
            ('BAKE', "Bake", "Mesh maps and layer images"),
        ),
        default='DISPLAY',
    )

    # A display setting, not a paint one: which axes mirror lives in Blender's
    # own `image_paint.use_symmetry_*`, this only says whether to draw them.
    show_symmetry_planes: BoolProperty(name="Show Mirror Planes", default=True)


class PR2_FillSettings(PropertyGroup):
    """What the three fill tools do with the faces a click selects.

    Deliberately not a colour: the fill paints with the *brush's* colour, so
    switching between the brush and a fill tool never loses what you picked
    and Blender's X shortcut still swaps both.
    """

    strength: FloatProperty(
        name="Alpha",
        description="How much of the colour a fill lays down",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
    )

    blend: EnumProperty(
        name="Blend",
        description="How the fill combines with what is already there",
        items=(
            ('MIX', "Mix", "Replace, at the fill's alpha"),
            ('MUL', "Multiply", "Darken by the fill colour"),
            ('ADD', "Add", "Add the fill colour"),
            ('SUB', "Subtract", "Subtract the fill colour"),
            ('LIGHTEN', "Lighten", "Keep whichever is brighter"),
            ('DARKEN', "Darken", "Keep whichever is darker"),
        ),
        default='MIX',
    )

    mode: EnumProperty(
        name="Mode",
        description="Whether a click adds the fill or takes it away",
        items=(
            ('ASSIGN', "Fill", "Lay the colour down"),
            ('ERASE', "Erase", "Clear the region back to transparent"),
        ),
        default='ASSIGN',
    )

    samples: EnumProperty(
        name="Antialiasing",
        description="Sub-pixel samples along the edge of the filled region. "
        "A UV edge never lands on the pixel grid, so without this every fill "
        "has a staircase border",
        items=(
            ('1', "Off", "One sample per pixel - hard, aliased edges"),
            ('2', "2 x 2", "Four samples per pixel"),
            ('4', "4 x 4", "Sixteen samples per pixel"),
        ),
        default='2',
    )

    pixel_perfect: BoolProperty(
        name="Pixel Perfect",
        description="Fill every pixel the region touches, whole. A UV island "
        "edge never lands on the pixel grid, so the pixels along it are only "
        "partly inside it and an antialiased fill leaves them half done - "
        "which is the gap along a seam that a bleed is otherwise needed to "
        "hide. This fills them outright, with hard edges and without "
        "spreading past the island. Antialiasing has nothing to do while it "
        "is on",
        default=False,
    )

    bleed: IntProperty(
        name="Bleed",
        description="Pixels the fill spreads past the UV island edge, so "
        "filtering and mipmaps do not reveal the seam. Pixel Perfect closes "
        "the seam without one, so this starts at zero and is there for the "
        "cases that still want padding",
        default=0,
        min=0,
        max=32,
    )

    # How the faces to fill are chosen. A click is one hit; a rectangle or a
    # lasso is a screen-space shape, and every face it takes is expanded by
    # the tool's own scope afterwards -- so a lasso over a Mesh Fill still
    # fills whole parts, exactly as clicking each of them would.
    select_mode: EnumProperty(
        name="Select",
        description="How a fill picks the faces it covers",
        items=(
            ('CLICK', "Click", "One face under the pointer, or a sweep of them"),
            ('RECT', "Box", "Drag a rectangle; everything inside it is filled"),
            ('LASSO', "Lasso", "Draw a shape; everything inside it is filled"),
        ),
        default='CLICK',
    )

    # A Mesh or UV Island fill takes the whole connected part, and half of
    # that part is usually facing away from you. Filling it is right when the
    # fill is a material assignment and wrong when it is something being
    # painted onto what you can see, so it is a switch rather than a rule.
    front_only: BoolProperty(
        name="Front Faces Only",
        description="Fill only the faces turned towards the view. Off fills "
        "the whole region, including the far side of the model",
        default=False,
    )

    # Polygon Fill: one triangle of the face, or the whole face.
    polygon_scope: EnumProperty(
        name="Polygon",
        items=(
            ('FACE', "Polygon", "The whole face under the pointer"),
            ('TRIANGLE', "Triangle", "Only the tessellated triangle that was hit"),
        ),
        default='FACE',
    )

    mesh_scope: EnumProperty(
        name="Mesh",
        items=(
            ('PART', "Part", "Every face connected to the one under the pointer"),
            ('OBJECT', "Object", "Every face of the object"),
        ),
        default='PART',
    )


# True while a rebuild is already running. `lighting.install` seeds an empty
# `hdri_path`, and writing a property with an update hook from inside that
# hook re-enters it -- which terminates on its own, but rebuilds the world
# twice for one edit. One flag is cheaper than reasoning about it every time.
_lighting_busy = False


def _lighting_update(self, context):
    """A lighting control moved: rebuild our world and re-apply the flags.

    Only the live PaintR2 space is touched. The window owns the lighting for
    as long as it is open, and a viewport we never dressed is not ours to
    change.
    """
    global _lighting_busy

    from . import lighting
    from . import session

    if _lighting_busy:
        return

    area = session.find_area()
    space = session.find_space(area) if area is not None else None
    if space is None:
        return

    _lighting_busy = True
    try:
        lighting.refresh(context or bpy.context, space)
    finally:
        _lighting_busy = False
    session.tag_redraw()


def _fade_update(self, context):
    """Rewrite the alphas and rebuild every material in the set.

    The fade is a shader stage, so turning it on or off is a rewire; the
    alphas are what drive it, so they are written in the same breath. Doing
    one without the other shows either an unfaded scene with the nodes in
    place or a faded one with no nodes to read them.

    `sync_fade` first, so it is the one that decides *which* materials are
    faded; the rebuild afterwards is what makes a switch land on all of them
    at once rather than one recompile at a time.
    """
    from . import isolation

    isolation.sync_fade(context)
    isolation.recompile_set()


def _solo_update(self, context):
    from . import isolation, session

    isolation.sync_solo(context)
    session.tag_redraw()


class PR2_HiddenObject(PropertyGroup):
    """One object the session hid, by name.

    Kept on the scene rather than in session memory because the record is the
    only way back: an add-on that hid half a file and then crashed has to
    leave something behind that can put it right. A name rather than a
    pointer -- a pointer would keep a deleted object alive.
    """

    name: StringProperty(name="Object")


class PR2_DisplaySettings(PropertyGroup):
    """How the model is lit and shaded while it is being painted.

    None of this is the document: it is what the *viewport* does with the
    document, which is why it lives on the scene rather than on the material.
    The one exception is the shader mode, which has to be per material because
    it changes that material's node tree -- see PR2_Document.shader_mode.
    """

    # --- lighting -------------------------------------------------------------

    use_lighting: BoolProperty(
        name="PaintR2 Lighting",
        description="Light the model with PaintR2's own world instead of "
        "Blender's studio light. The scene's own world is put back when this "
        "is switched off, and when the window closes",
        default=False,
        update=_lighting_update,
    )

    hdri_path: StringProperty(
        name="HDRI",
        description="The environment map that lights the model. Seeded from "
        "the studio light the viewport was already using",
        subtype='FILE_PATH',
        default="",
        update=_lighting_update,
    )

    strength: FloatProperty(
        name="Strength",
        description="How brightly the environment lights the model. Zero is "
        "no environment light at all",
        default=1.0,
        min=0.0,
        soft_max=4.0,
        precision=2,
        update=_lighting_update,
    )

    # Three properties rather than one three-component one: a vector property
    # would reach the panel as a colour row (props.py reads any 3+ array as a
    # colour), and Blender draws a rotation as three rows anyway.
    rotation_x: FloatProperty(
        name="Rotation X",
        description="Tilt the environment forward and back",
        default=0.0, min=-math.pi * 2, max=math.pi * 2,
        subtype='ANGLE', unit='ROTATION',
        update=_lighting_update,
    )
    rotation_y: FloatProperty(
        name="Rotation Y",
        description="Roll the environment left and right",
        default=0.0, min=-math.pi * 2, max=math.pi * 2,
        subtype='ANGLE', unit='ROTATION',
        update=_lighting_update,
    )
    rotation_z: FloatProperty(
        name="Rotation Z",
        description="Turn the environment around the model. This is the one "
        "rotation Blender's studio light also has",
        default=0.0, min=-math.pi * 2, max=math.pi * 2,
        subtype='ANGLE', unit='ROTATION',
        update=_lighting_update,
    )

    show_background: BoolProperty(
        name="Show Background",
        description="Draw the environment map behind the model. Switched off, "
        "the map still lights the model but the view shows a flat colour "
        "instead - which is what judging a painted colour needs",
        default=False,
        update=_lighting_update,
    )

    background_color: FloatVectorProperty(
        name="Backdrop",
        description="What the view shows when the background is hidden",
        subtype='COLOR',
        size=3,
        min=0.0, max=1.0,
        default=(0.05, 0.05, 0.05),
        update=_lighting_update,
    )

    use_scene_lights: BoolProperty(
        name="Scene Lights",
        description="Let the scene's own lamps light the model as well as the "
        "environment",
        default=False,
        update=_lighting_update,
    )

    # --- what the rest of the set looks like ----------------------------------
    # With several models in the set, the one being worked on has to be
    # findable at a glance. Darkening the others is one shader stage driven
    # by `object.color`'s alpha, and it costs nothing when it is off:
    # nodegen leaves the nodes out of the material entirely rather than
    # building them and setting the factor to one.

    fade_inactive: BoolProperty(
        name="Dim Other Models",
        description="Draw the models that are not selected darker, so the one "
        "being painted reads on its own. They stay solid -- nothing is made "
        "see-through. Switched off, every model in the set is drawn normally",
        default=True,
        update=_fade_update,
    )

    fade_amount: FloatProperty(
        name="Brightness",
        description="How much of the other models' colour is left. Zero is "
        "black, one is no dimming at all",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_fade_update,
    )

    # The stronger answer to the same question: the other models are not
    # drawn at all. Plain viewport hiding, recorded with the isolation
    # so closing the window or a crash cannot leave a model invisible.
    solo_active: BoolProperty(
        name="Show Selected Only",
        description="Hide every other model in the set, so only the one being "
        "painted is drawn. Switch models from the model list; the hidden ones "
        "come back when this is turned off or the window closes",
        default=False,
        update=_solo_update,
    )

    # What was there before we took over. On the scene rather than in memory:
    # a crash must not be able to lose the user's world.
    saved_world: StringProperty(name="Saved World", default="")
    saved_shading: StringProperty(name="Saved Shading", default="")


class PR2_SceneProps(PropertyGroup):
    """Root of everything PaintR2 stores in a .blend, hung off Scene."""

    ui: PointerProperty(type=PR2_UIState)
    fill: PointerProperty(type=PR2_FillSettings)
    display: PointerProperty(type=PR2_DisplaySettings)

    # Every object the session hid to isolate the paint set, so closing the
    # window can put them back -- and so a session that ended without closing
    # still can. See isolation.py.
    hidden_objects: CollectionProperty(type=PR2_HiddenObject)

    # Which tool the left-hand row has selected. Changing it swaps the whole
    # settings strip above the viewport and decides what a click in the
    # viewport does: a brush stroke Blender runs, or a fill we run.
    active_tool: EnumProperty(
        name="Tool",
        items=(
            ('BRUSH', "Brush", "Paint with Blender's brush engine"),
            ('POLY_FILL', "Polygon Fill", "Fill the polygon under the pointer"),
            ('MESH_FILL', "Mesh Fill", "Fill the whole mesh part under the pointer"),
            ('UV_FILL', "UV Island Fill", "Fill the UV island under the pointer"),
        ),
        default='BRUSH',
    )

    # Informational only. The real answer to "is the window open?" is
    # session.state.is_open -- this flag exists so the sidebar can show a
    # sensible label right after a file load, before any window exists.
    session_was_open: BoolProperty(
        name="Session Was Open",
        description="Whether a PaintR2 window was open when this file was saved",
        default=False,
    )

    # Where the window was when PaintR2 took it over. Also on the session, but
    # a file saved mid-session opens on the PaintR2 workspace with no session
    # behind it, and this is how it finds its way back.
    return_workspace: StringProperty(
        name="Return Workspace",
        description="The workspace PaintR2 was opened from",
        default="",
    )
    return_mode: StringProperty(
        name="Return Mode",
        description="The object mode PaintR2 was opened from",
        default="",
    )

    canvas_zoom: FloatProperty(
        name="Canvas Zoom",
        default=1.0,
        min=0.02,
        max=32.0,
    )
    canvas_pan_x: FloatProperty(name="Canvas Pan X", default=0.0)
    canvas_pan_y: FloatProperty(name="Canvas Pan Y", default=0.0)

    # --- settings used by "Set Models" ---------------------------------------
    # Only applied to newly created images. An image that already exists is
    # never resized or reflooded, so painted work is never thrown away.

    image_resolution: EnumProperty(
        name="Resolution",
        description="Size of the texture created for each paint material",
        items=(
            ('1024', "1024", "1024 x 1024"),
            ('2048', "2048", "2048 x 2048"),
            ('4096', "4096", "4096 x 4096"),
            ('8192', "8192", "8192 x 8192"),
        ),
        default='2048',
    )

    image_base_color: FloatVectorProperty(
        name="Base Color",
        description="Fill color of a newly created paint texture",
        subtype='COLOR',
        size=4,
        min=0.0,
        max=1.0,
        default=(1.0, 1.0, 1.0, 1.0),
    )

    # --- bake / export -------------------------------------------------------

    export_resolution: EnumProperty(
        name="Bake Size",
        description="Resolution of the baked texture. Independent of the "
        "layer resolution, so you can paint at 2K and export at 4K",
        items=(
            ('512', "512", "512 x 512"),
            ('1024', "1024", "1024 x 1024"),
            ('2048', "2048", "2048 x 2048"),
            ('4096', "4096", "4096 x 4096"),
            ('8192', "8192", "8192 x 8192"),
        ),
        default='2048',
    )

    export_format: EnumProperty(
        name="Format",
        items=(
            ('PNG', "PNG", "8-bit, sRGB. The usual choice"),
            ('OPEN_EXR', "OpenEXR", "32-bit float, linear. For HDR data"),
            ('TARGA', "Targa", "8-bit, sRGB"),
            ('JPEG', "JPEG", "8-bit, sRGB, lossy and no alpha"),
        ),
        default='PNG',
    )

    export_directory: StringProperty(
        name="Folder",
        description="Where baked textures are written. '//' is the folder of "
        "the current .blend file",
        subtype='DIR_PATH',
        default="//textures/",
    )

    export_active_only: BoolProperty(
        name="Active Model Only",
        description="Export only the model being worked on. Off, every model "
        "in the paint set is exported -- which is what an export usually "
        "means once there is more than one",
        default=False,
    )

    # One tick per channel: which files an export writes is the
    # user's choice, not something read off which channels are used. Scene
    # properties, so the choice is still there next time; all on at first.
    export_base_color: BoolProperty(
        name="Base Color", default=True,
        description="Write a Base Color texture",
    )
    export_roughness: BoolProperty(
        name="Roughness", default=True,
        description="Write a Roughness texture",
    )
    export_metallic: BoolProperty(
        name="Metallic", default=True,
        description="Write a Metallic texture",
    )
    export_normal: BoolProperty(
        name="Normal", default=True,
        description="Write a Normal texture, with the height baked into it",
    )
    export_height: BoolProperty(
        name="Height", default=True,
        description="Write a Height texture: 0.5 is flat, below is a dent, "
        "above a bump",
    )

    # Only the green channel differs.
    export_normal_format: EnumProperty(
        name="Normal Format",
        items=(
            ('OPENGL', "OpenGL", "Green up (Y+), the convention Blender uses"),
            ('DIRECTX', "DirectX", "Green down (Y-)"),
        ),
        default='OPENGL',
    )

    def export_channels(self, document):
        """The channels to write for `document`, in CHANNELS order: the ticked
        ones, whether or not a layer uses them."""
        return [
            c for c in document.active_channels()
            if getattr(self, "export_" + c.lower(), False)
        ]

    # --- mesh maps -----------------------------------------------------------
    # Their own resolution: a curvature or AO map does not need the size a
    # colour texture does, and baking four of them at 4K for nothing is slow.

    mesh_map_resolution: EnumProperty(
        name="Mesh Map Size",
        items=(
            ('512', "512", ""),
            ('1024', "1024", ""),
            ('2048', "2048", ""),
            ('4096', "4096", ""),
        ),
        default='1024',
    )

    bake_map_ao: BoolProperty(name="Ambient Occlusion", default=True)
    bake_map_curvature: BoolProperty(name="Curvature", default=True)
    bake_map_thickness: BoolProperty(name="Thickness", default=True)
    bake_map_position: BoolProperty(name="Position", default=True)
    bake_map_normal: BoolProperty(name="Normal", default=True)
    bake_map_height: BoolProperty(name="Height", default=False)
    bake_map_bent_normal: BoolProperty(name="Bent Normal", default=False)

    mesh_map_backend: EnumProperty(
        name="Baker",
        description="Which baker produces the mesh maps",
        items=(
            ('AUTO', "Auto",
             "The external baker when it is installed, Cycles otherwise. "
             "The external one is several times faster, does not freeze "
             "Blender, and can be cancelled"),
            ('EXTERNAL', "External",
             "Always the external baker. Fails rather than falling back, "
             "which is what you want while checking it works"),
            ('CYCLES', "Cycles",
             "Always Blender's own bake. Slower and blocking, but it is the "
             "reference the external baker is checked against"),
        ),
        default='AUTO',
    )

    # Two separate questions, and they were one hidden answer each before:
    # which models get maps, and which models a map can see.
    bake_models: EnumProperty(
        name="Bake",
        description="Which models get mesh maps",
        items=(
            ('ALL', "All Models",
             "Every model in the paint set, one after another"),
            ('ACTIVE', "Active Model",
             "Only the model being worked on. The others keep the maps they "
             "have"),
        ),
        default='ALL',
    )

    bake_occluders: EnumProperty(
        name="Occlusion",
        description="What may shadow a model's Ambient Occlusion and Bent "
        "Normal. Thickness always reads the model alone, and nothing outside "
        "the paint set ever takes part",
        items=(
            ('SELF', "Self Only",
             "Each model is shadowed only by itself, as if it were alone in "
             "the scene. The map stays right if the models are moved apart "
             "later"),
            ('MODELS', "All Models",
             "The other models in the paint set shadow it too -- where a hand "
             "rests on an arm, the arm darkens. With Highpoly Bake a model's "
             "high-poly stands in for it"),
        ),
        default='SELF',
    )

    mesh_map_samples: IntProperty(
        name="Samples",
        description="Rays per texel for AO and Thickness, and the only thing "
        "that decides how grainy they come out. Position, Normal, Height and "
        "Curvature are exact and finish at any number. Cheap to raise: a bake "
        "costs about 0.8 s before it traces anything, so 512 samples takes "
        "almost exactly as long as 16",
        default=64, min=1, soft_max=1024, max=4096,
    )

    bake_margin_type: EnumProperty(
        name="Margin Type",
        description="How the bleed outside each UV island is filled",
        items=(
            ('ADJACENT_FACES', "Adjacent Faces",
             "Continue each island with the face next to it across the seam. "
             "Correct across a seam, and the one to want"),
            ('EXTEND', "Extend",
             "Smear the island's edge pixels outward. Cheaper, and wrong "
             "across a seam"),
        ),
        default='ADJACENT_FACES',
    )

    # --- high-poly bake -------------------------------------------------------
    # The pairing convention lives in paintset.py; these are the numbers the
    # bake itself runs on.

    use_highpoly_bake: BoolProperty(
        name="Highpoly Bake",
        description="Read every mesh map off a matching high-poly model "
        "instead of off the model itself. The pairing is by name and "
        "collection - see the Highpoly Bake panel",
        default=False,
    )

    highpoly_suffix: StringProperty(
        name="Suffix",
        description="What a high-poly is called: 'suzanne' looks for "
        "'suzanne_high'. The match is exact",
        default="_high",
    )

    cage_extrusion: FloatProperty(
        name="Extrusion",
        description="How far out along the low-poly's normal each ray starts. "
        "It must clear everything the high-poly sticks out past the low-poly, "
        "or that detail is never hit. Too large and rays from opposite sides "
        "of a thin part start crossing into each other",
        default=0.05, min=0.0, soft_max=1.0,
        unit='LENGTH', precision=4,
    )

    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="How far each ray travels inward from where it started. "
        "Zero means no limit, which is right until a second surface behind "
        "the first starts being hit. It has to cover the extrusion as well as "
        "the depth, because the ray begins out at the extruded position",
        default=0.0, min=0.0, soft_max=2.0,
        unit='LENGTH', precision=4,
    )

    cage_safety: FloatProperty(
        name="Safety Factor",
        description="What Measure multiplies its findings by before writing "
        "them into the fields. A vertex is a sample, and the surface between "
        "two of them can bulge further than either - this is the headroom for "
        "that. Raise it if details are still missing after measuring",
        default=1.15, min=1.0, soft_max=3.0,
        precision=2,
    )

    # What the last Measure found, kept so the panel can show it. Not settings
    # -- readings. Seeing them is how the two fields above stop being magic.
    measured_outward: FloatProperty(
        name="Measured Outward", default=-1.0, unit='LENGTH', precision=4,
    )
    measured_inward: FloatProperty(
        name="Measured Inward", default=-1.0, unit='LENGTH', precision=4,
    )

    height_range: FloatProperty(
        name="Height Range",
        description="The distance that reaches black and white in the Height "
        "map. 0.5 is flat, so this is how far a dent has to go to read as "
        "fully black. Anything further is clamped",
        default=0.05, min=0.0001, soft_max=1.0,
        unit='LENGTH', precision=4,
    )

    normal_flip_green: BoolProperty(
        name="Flip Green (DirectX)",
        description="Bake the green channel pointing down instead of up. "
        "OpenGL and Blender read +Y; DirectX reads -Y. The "
        "wrong one lights every dent as a bump",
        default=False,
    )

    # --- ambient occlusion, the way a bake edicisi asks about it ------------

    ao_distance: FloatProperty(
        name="Max Occluder Distance",
        description="How far a ray looks for something to be shadowed by. "
        "Past this, nothing occludes",
        default=1.0,
        min=0.0,
        soft_max=10.0,
    )

    ao_distance_relative: BoolProperty(
        name="Relative to Bounding Box",
        description="Read the distance as a fraction of the model's bounding "
        "box diagonal rather than in metres, so one setting fits models of "
        "any size",
        default=True,
    )

    ao_min_distance: FloatProperty(
        name="Min Occluder Distance",
        description="Nearer than this and an occluder is ignored. Lifts the "
        "contact shadow off geometry that touches itself",
        default=0.0,
        min=0.0,
        soft_max=1.0,
        precision=5,
    )

    ao_spread: FloatProperty(
        name="Spread Angle",
        description="How wide a cone the rays are fired into. 180 is the "
        "whole hemisphere; narrower reads as occlusion from straight above",
        default=180.0,
        min=1.0,
        max=180.0,
    )

    ao_distribution: EnumProperty(
        name="Distribution",
        description="How the rays are spread inside the cone",
        items=(
            ('COSINE', "Cosine", "More rays near the surface normal. The "
             "physically weighted answer, and the softer-looking one"),
            ('UNIFORM', "Uniform", "Rays spread evenly. Harder edges"),
        ),
        default='COSINE',
    )

    ao_attenuation: EnumProperty(
        name="Attenuation",
        description="How an occluder's distance weighs on the result",
        items=(
            ('NONE', "None", "Hit or not. The fastest, and the harshest"),
            ('LINEAR', "Linear", "An occluder fades out evenly as it moves "
             "away"),
            ('SMOOTH', "Smooth", "Eased at both ends. The softest"),
        ),
        default='NONE',
    )

    ao_ignore_backface: BoolProperty(
        name="Ignore Backface",
        description="Do not be shadowed by surfaces turned away from the ray. "
        "For shells and single-sided geometry, whose inside would otherwise "
        "shadow the outside",
        default=False,
    )

    # --- curvature -----------------------------------------------------------

    curvature_radius: FloatProperty(
        name="Sampling Radius",
        description="How far across the surface curvature is measured. Larger "
        "reads the shape, smaller reads the detail",
        default=0.02,
        min=0.0001,
        soft_max=0.5,
        precision=4,
    )

    curvature_radius_relative: BoolProperty(
        name="Relative to Bounding Box",
        description="Read the radius as a fraction of the model's bounding "
        "box diagonal rather than in metres",
        default=True,
    )

    curvature_low: FloatProperty(
        name="Tonemapping Min",
        description="The curvature that maps to black. Below it the map clamps",
        default=-1.0,
        soft_min=-4.0,
        soft_max=0.0,
    )

    curvature_high: FloatProperty(
        name="Tonemapping Max",
        description="The curvature that maps to white. Above it the map clamps",
        default=1.0,
        soft_min=0.0,
        soft_max=4.0,
    )

    curvature_smooth: IntProperty(
        name="Curvature Smoothing",
        description="How many times the per-vertex curvature is averaged with "
        "every neighbour, creases included. This is a blur, not a clean-up - "
        "for noise without losing creases raise Curvature Iterations instead. "
        "Drop it to zero to keep the field exactly as measured",
        default=1, min=0, max=4,
    )

    curvature_iterations: IntProperty(
        name="Curvature Iterations",
        description="Edge-preserving clean-up passes over the curvature "
        "field, on the mesh. Each vertex is averaged only with the "
        "neighbours that agree with it, so tessellation noise flattens and "
        "creases survive. Costs milliseconds - it does not re-bake anything. "
        "This affects Curvature only; AO and Thickness noise is Samples",
        default=1, min=0, soft_max=16, max=64,
    )

    bake_margin: IntProperty(
        name="Margin",
        description="Pixels of bleed painted outside each UV island, so seams "
        "do not show dark edges when the texture is filtered",
        default=16,
        min=0,
        max=64,
    )


CLASSES = (
    PR2_UIState,
    PR2_FillSettings,
    PR2_HiddenObject,
    PR2_DisplaySettings,
    PR2_SceneProps,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.paintr2 = PointerProperty(
        type=PR2_SceneProps,
        name="PaintR2",
    )


def unregister():
    if hasattr(bpy.types.Scene, "paintr2"):
        del bpy.types.Scene.paintr2

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass

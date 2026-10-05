# SPDX-License-Identifier: GPL-3.0-or-later
"""Running a fill: from a point in the region to pixels in a layer.

The three fill tools all end up here. A click gives a point in the viewport;
a ray gives an object and a face; the tool's scope turns that one face into a
set of faces; `fill.py` turns the set's UV triangles into coverage and lays
the colour down.

Held together by `FillStroke`, because a fill is not always one click. Holding
the button and sweeping the pointer across the model fills every face it
crosses, so the image buffer is read
once when the button goes down, added to on every step, and written back as
the stroke goes along.

Where the events come from
--------------------------

Not from a modal operator of its own. `op_modal` already owns every event that
reaches a PaintR2 window, and a second modal underneath it would be fighting
for the same left mouse button. It drives the stroke directly instead.
"""

import bpy
import numpy as np
from bpy_extras import view3d_utils

from .. import fill as fill_mod
from .. import paint
from .. import session
from .. import window as window_mod


def _ray(region, rv3d, x, y):
    """Ray origin and direction in world space for a region-local point."""
    coord = (x, y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    return origin, direction


def hit_test(context, region, rv3d, x, y):
    """(object, face index, local hit point), or None.

    The evaluated object, deliberately: `Scene.ray_cast` reports a face index
    into the geometry it actually hit, and on a modified mesh that is not the
    same numbering as the one in `bpy.data`. Everything downstream -- the
    adjacency walk, the UVs, the triangles -- reads the evaluated mesh too, so
    the indices agree.
    """
    depsgraph = context.evaluated_depsgraph_get()
    origin, direction = _ray(region, rv3d, x, y)

    result, _location, _normal, index, obj, matrix = context.scene.ray_cast(
        depsgraph, origin, direction
    )
    if not result or obj is None or obj.type != 'MESH':
        return None

    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.data
    if mesh is None or index < 0 or index >= len(mesh.polygons):
        return None

    # The hit point in the mesh's own space, for picking one triangle out of a
    # polygon. `matrix` is the object-to-world of whatever was hit, including
    # the instancing case, so its inverse is the right way back.
    local = matrix.inverted_safe() @ _location if _location is not None else None
    return evaluated, index, local


class FillStroke:
    """One press-drag-release of a fill tool.

    Reads the target image once, accumulates into it, and writes back on every
    step so the viewport keeps up with the pointer. A face is only ever filled
    once per stroke: sweeping back over ground already covered must not build
    the colour up, because at less than full alpha that would leave the sweep's
    own path visible in the result.
    """

    def __init__(self, context):
        self.target = None
        self.scope = window_mod.fill_scope(context)
        self.settings = window_mod._fill_settings(context)
        self.done = set()
        self.touched = False

        # Read once, at the start: a stroke mirrors the way it was begun, the
        # same as the brush's does.
        self.mirror_axes = window_mod.mirror_axes(context)

        # Which way the eye is looking, for the front-faces switch. Set when
        # the stroke has a region to read it from; a test calling `add_hit`
        # directly sets it by hand.
        self.view_direction = None

        image = paint.active_paint_image(context)
        if image is None or self.settings is None:
            return

        target = fill_mod.Target(image)
        if not target.valid:
            return
        target.read()
        self.target = target

    @property
    def active(self):
        return self.target is not None

    def _colour(self, context):
        """The brush's colour, or its secondary one while erasing.

        Shared with the brush on purpose. Erase does not use a colour at
        all -- it takes alpha away -- but reading it keeps the call uniform.
        """
        return tuple(paint.brush_get("color", (1.0, 1.0, 1.0), context) or ())[:3]

    def add(self, context, region, rv3d, x, y):
        """Fill whatever is under (x, y). True when the image changed."""
        if not self.active:
            return False

        _origin, direction = _ray(region, rv3d, x, y)
        self.view_direction = tuple(direction)

        hit = hit_test(context, region, rv3d, x, y)
        if hit is None:
            return False
        return self.add_hit(context, hit)

    def add_hit(self, context, hit):
        """The half of `add` that has no region in it, so it can be tested.

        A background Blender has no window and therefore no view matrix, which
        would otherwise put everything below out of reach of the test suite.

        One click can be several face sets -- the mirror planes turn the point
        into up to eight of them -- but it is still *one* fill. They are
        rasterised into a single coverage mask and composited once, because
        two mirrored sets can overlap (a face lying on the mirror plane is in
        both), and compositing that face twice at half alpha would leave it
        visibly darker than its neighbours.
        """
        if not self.active or hit is None:
            return False

        obj, face_index, local = hit
        mesh = obj.data

        faces, triangles = [], []
        for index, point in fill_mod.click_targets(
            obj, face_index, local, self.mirror_axes
        ):
            triangle = None
            if self.scope == 'TRIANGLE' and point is not None:
                triangle = fill_mod.triangle_index_at(mesh, index, point)

            key = (obj.name, self._key(mesh, index, triangle))
            if key in self.done:
                continue
            self.done.add(key)

            if self.scope == 'TRIANGLE' and triangle is not None:
                triangles.append(triangle)
            else:
                faces.append(fill_mod.face_set(mesh, index, self.scope))

        if triangles:
            uv = fill_mod.uv_triangles(mesh, triangles=triangles)
        elif faces:
            chosen = np.unique(np.concatenate(faces))
            # A connected part is half turned away from you. Filling that half
            # is right for a material assignment and wrong for something being
            # painted onto what you can see, so it is a switch.
            if (getattr(self.settings, "front_only", False)
                    and self.view_direction is not None
                    and self.scope not in ('TRIANGLE', 'FACE')):
                chosen = fill_mod.front_facing(obj, chosen, self.view_direction)
                if len(chosen) == 0:
                    return False
            uv = fill_mod.uv_triangles(mesh, chosen)
        else:
            return False

        if len(uv) == 0:
            return False

        settings = self.settings
        erasing = settings.mode == 'ERASE'

        changed = fill_mod.fill(
            self.target,
            uv,
            self._colour(context),
            strength=float(settings.strength),
            mode='ERASE_ALPHA' if erasing else settings.blend,
            samples=int(settings.samples),
            bleed=int(settings.bleed),
            conservative=bool(settings.pixel_perfect),
        )
        if changed:
            self.target.write()
            self.touched = True
        return changed

    def add_shape(self, context, region, rv3d, shape):
        """Fill everything a screen-space shape covers. One fill, one step.

        The shape picks *faces*; the tool's own scope then expands each of
        them exactly as a click would, so a lasso over Mesh Fill still fills
        whole parts. That is what keeps the four tools four tools rather than
        eight.
        """
        if not self.active or region is None or rv3d is None or len(shape) < 3:
            return False

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return False

        depsgraph = context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.data
        if mesh is None or not len(mesh.polygons):
            return False

        # Only asked for when it is going to be used: the direction comes
        # from the region, and a shape fill that does not care about facing
        # has no reason to need one.
        front_only = bool(getattr(self.settings, "front_only", False))
        direction = None
        if front_only:
            _origin, view = _ray(
                region, rv3d, region.width * 0.5, region.height * 0.5
            )
            direction = tuple(view)

        hit = fill_mod.faces_in_shape(
            evaluated, shape, rv3d.perspective_matrix,
            region.width, region.height,
            front_only=front_only,
            view_direction=direction,
        )
        if len(hit) == 0:
            return False

        # Mirroring is a click-time thing: the shape is already on screen and
        # the user drew it over what they meant. Expanding the scope is not --
        # a lasso touching one polygon of a part means that part.
        faces = []
        for index in hit:
            index = int(index)
            key = (obj.name, self._key(mesh, index, None))
            if key in self.done:
                continue
            self.done.add(key)
            faces.append(fill_mod.face_set(mesh, index, self.scope))

        if not faces:
            return False

        uv = fill_mod.uv_triangles(mesh, np.unique(np.concatenate(faces)))
        if len(uv) == 0:
            return False

        settings = self.settings
        erasing = settings.mode == 'ERASE'
        changed = fill_mod.fill(
            self.target,
            uv,
            self._colour(context),
            strength=float(settings.strength),
            mode='ERASE_ALPHA' if erasing else settings.blend,
            samples=int(settings.samples),
            bleed=int(settings.bleed),
            conservative=bool(settings.pixel_perfect),
        )
        if changed:
            self.target.write()
            self.touched = True
        return changed

    def _key(self, mesh, face_index, triangle):
        """What makes two hits the same fill, so a sweep does not repeat one.

        Repeating matters at less than full alpha: passing back over ground
        already covered would build the colour up and leave the path of the
        sweep visible in the result.
        """
        if self.scope == 'TRIANGLE':
            return ('TRIANGLE', triangle if triangle is not None else face_index)
        if self.scope == 'FACE':
            return ('FACE', face_index)
        return self._label(mesh, face_index)

    def _label(self, mesh, face_index):
        """The connectivity label of `face_index`, so a swept-over set is
        recognised as one already done rather than rasterised again."""
        if self.scope == 'OBJECT':
            return ('OBJECT', 0)

        topology = fill_mod.topology(mesh)
        source = (topology.island_of if self.scope == 'ISLAND'
                  else topology.part_of)
        if face_index >= len(source):
            return (self.scope, face_index)
        return (self.scope, int(source[face_index]))

    def finish(self):
        """Close the stroke off. One undo step for the whole sweep.

        The step goes into PaintR2's own ring, not Blender's. `ed.undo_push`
        used to be here and was worse than nothing: a memfile undo step does
        not carry image pixels, so pressing Ctrl+Z after a fill stepped past
        the fill without undoing it. See history.py.
        """
        if not self.touched:
            return

        if self.target is not None:
            self.target.commit_undo()
        session.tag_redraw()


class PAINTR2_OT_fill(bpy.types.Operator):
    """Fill the region under the pointer with the brush colour"""

    bl_idname = "paintr2.fill"
    bl_label = "PaintR2 Fill"
    bl_options = {'INTERNAL'}

    # Region-local, because that is what the interface hands out everywhere
    # else and converting once at the edge beats converting at every call.
    mouse_x: bpy.props.IntProperty()
    mouse_y: bpy.props.IntProperty()

    @classmethod
    def poll(cls, context):
        return window_mod.is_fill_tool(context)

    def execute(self, context):
        region = session.find_region(session.find_area())
        space = session.find_space()
        rv3d = getattr(space, "region_3d", None) if space is not None else None
        if region is None or rv3d is None:
            return {'CANCELLED'}

        stroke = FillStroke(context)
        if not stroke.active:
            self.report({'WARNING'}, "Nothing to fill into - pick a paint layer")
            return {'CANCELLED'}

        if not stroke.add(context, region, rv3d, self.mouse_x, self.mouse_y):
            return {'CANCELLED'}

        stroke.finish()
        return {'FINISHED'}


class PAINTR2_OT_fill_undo(bpy.types.Operator):
    """Undo the last fill"""

    bl_idname = "paintr2.fill_undo"
    bl_label = "Undo Fill"
    bl_options = {'INTERNAL'}

    redo: bpy.props.BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        from .. import history

        undo_steps, redo_steps = history.depth()
        return bool(undo_steps or redo_steps)

    def execute(self, context):
        from .. import history

        done = history.redo() if self.redo else history.undo()
        if not done:
            return {'CANCELLED'}

        session.tag_redraw()
        return {'FINISHED'}


CLASSES = (PAINTR2_OT_fill, PAINTR2_OT_fill_undo)

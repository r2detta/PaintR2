# SPDX-License-Identifier: GPL-3.0-or-later
"""Baking mesh maps, and setting a scene up to bake them off a high-poly.

Two operators, in the order they are used:

    paintr2.measure_cage          measure the two surfaces and write the
                                  extrusion and ray distance they need
    paintr2.bake_mesh_maps        bake, one pass per model in the paint set

There used to be a third -- `setup_highpoly_bake`, which made the collection
pair. "Set Models" makes `Paint` and `Paint_high` now, so a second button
that made a second kind of pair was a second convention to learn for
nothing. Dropping the high-polys into `Paint_high` is the whole of the setup.

The pairing rules they both obey live in `paintset.py`, and the bake itself is
`meshmaps.py`. What is here is the reporting -- which is most of the value,
because every failure mode of a high-poly bake is silent otherwise.
"""

import shutil
import tempfile
import time

import bpy

from .. import paintset
from .. import external
from .. import isolation
from .. import meshmaps


def _selected_meshes(context):
    """The mesh objects to work on.

    The paint set first, because that is what the session is: a mesh map is
    baked for every model being painted, and with the models isolated the
    "selection" is whatever the last click in the viewport left behind.
    Outside a paint set the old rule stands -- the selection, or the active
    object -- so this still works on a file nobody has run Set Models on.
    """
    models = paintset.paint_objects()
    if models:
        return models

    chosen = [
        obj for obj in context.selected_objects
        if obj is not None and obj.type == 'MESH'
    ]
    if chosen:
        return chosen

    active = context.active_object
    if active is not None and active.type == 'MESH':
        return [active]
    return []


def bake_targets(context):
    """The models a bake produces maps for: all of them, or the active one.

    Same rule as the export's "Active Model Only": the active object when it
    is one of the models, otherwise the first -- a bake that was asked for one
    model makes one, rather than none because the click last landed on a lamp.
    """
    models = _selected_meshes(context)
    props = context.scene.paintr2
    if props.bake_models != 'ACTIVE' or not models:
        return models

    active = context.active_object
    return [active] if active in models else models[:1]


def occluders(context, obj):
    """The other models that may shadow `obj`'s AO. Empty for Self Only.

    Drawn from every model, not from `bake_targets`: baking only the active
    model is a choice about which maps to *make*, and has nothing to say
    about what those maps see.
    """
    props = context.scene.paintr2
    if props.bake_occluders != 'MODELS':
        return []

    high_of = None
    if props.use_highpoly_bake:
        def high_of(model):
            partner = paintset.find_high(model, props.highpoly_suffix)
            return partner.object if partner else None

    return meshmaps.occluders_for(obj, _selected_meshes(context), high_of)


def _dropped_note(names):
    """The front of a report for models that came out of a bake with no map.

    A bake where one of two models produced nothing still opens with
    "Baked N map(s)", and the reason lands at the end of the line behind a
    semicolon, where it reads as a footnote to a success. Naming those models
    first, in warning colour, is the difference between noticing and not.
    """
    return f"No map for {', '.join(names)}. "


class PAINTR2_OT_measure_cage(bpy.types.Operator):
    """Measure the selected pairs and fill in Extrusion and Max Ray Distance

    The two numbers do not have to be guessed. For every vertex of a low-poly
    the nearest point on its high-poly is found, and which side it is on is
    read off the low-poly's normal: outward is what the extrusion has to
    clear, inward is what the ray has to reach.

    What the fields get is the measured maximum times the safety factor,
    because a vertex is a sample and the surface between two of them can bulge
    further than either.
    """

    bl_idname = "paintr2.measure_cage"
    bl_label = "Measure"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    def execute(self, context):
        props = context.scene.paintr2

        outward = 0.0
        inward = 0.0
        measured = 0
        missing = []

        for obj in _selected_meshes(context):
            partner = paintset.find_high(obj, props.highpoly_suffix)
            if not partner:
                missing.append(partner.reason)
                continue

            reading = meshmaps.measure_cage(obj, partner.object)
            if not reading.ok:
                missing.append(f"{obj.name}: {reading.error}")
                continue

            outward = max(outward, reading.outward)
            inward = max(inward, reading.inward)
            measured += 1

        if measured == 0:
            self.report(
                {'ERROR'},
                "Nothing to measure. " + (missing[0] if missing else ""),
            )
            return {'CANCELLED'}

        safety = props.cage_safety
        props.measured_outward = outward
        props.measured_inward = inward
        props.cage_extrusion = outward * safety
        props.max_ray_distance = (outward + inward) * safety * 2.0
        # A dent in the high-poly that the low-poly cuts across is exactly the
        # depth the height map has to be able to show.
        props.height_range = max(outward, inward, 1e-4) * safety

        note = f"Measured {measured} pair(s): out {outward:.4f}, in {inward:.4f}"
        if missing:
            note += f"  ({len(missing)} skipped)"
        self.report({'INFO'}, note)
        return {'FINISHED'}


class PAINTR2_OT_bake_mesh_maps_external(bpy.types.Operator):
    """Bake the ticked mesh maps with the external baker

    Modal, and that is the whole point: the bake runs in another process, so
    Blender keeps drawing, the progress bar is real, and Escape actually stops
    it. Cycles' bake blocks the main thread instead -- the window stops
    answering and the desktop offers to kill it.

    One run produces every ticked map. The BVH is built once and the UVs are
    rasterized once, where Cycles pays a scene sync and a BVH build per map.
    """

    bl_idname = "paintr2.bake_mesh_maps_external"
    bl_label = "Bake Mesh Maps"
    bl_options = {'REGISTER'}

    _timer = None
    _job = None
    _queue = None
    _done = 0
    _maps = 0

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context)) and external.available() is not None

    def _start_next(self, context):
        """Kick off the next object, or return False when there are none."""
        from .. import external as ext

        while self._queue:
            obj, high = self._queue.pop(0)
            props = context.scene.paintr2
            options = meshmaps.BakeOptions.from_scene(context.scene)

            wanted = 0
            for identifier in meshmaps.MAP_IDS:
                if not getattr(props, "bake_map_" + identifier.lower(), False):
                    continue
                if meshmaps.needs_high(identifier) and high is None:
                    self._skipped.append(
                        f"{obj.name}: {meshmaps.map_label(identifier)} needs a "
                        "high-poly")
                    continue
                wanted |= ext.MAP_BITS[identifier]

            if not wanted:
                continue

            try:
                path = ext.prepare(
                    obj, high, self._directory, int(props.mesh_map_resolution),
                    props.mesh_map_samples, wanted, options,
                    occluders=occluders(context, obj))
            except Exception as error:  # noqa: BLE001
                self._skipped.append(f"{obj.name}: {error}")
                self._dropped.append(obj.name)
                continue

            self._current = obj
            self._job = ext.Job(ext.available(), path, path[:-5] + ".pr2m")
            return True

        return False

    def execute(self, context):
        # Called when something runs the operator without a window -- a script,
        # or a headless session. There is no modal loop to hang off there, so
        # it waits instead. Same work, no progress bar.
        return self._run_blocking(context)

    def invoke(self, context, event):
        from .. import external as ext

        props = context.scene.paintr2
        self._skipped = []
        self._dropped = []
        self._done = 0
        self._maps = 0
        self._current = None
        self._directory = tempfile.mkdtemp(prefix="paintr2_bake_")

        self._queue = []
        for obj in bake_targets(context):
            high = None
            if props.use_highpoly_bake:
                partner = paintset.find_high(obj, props.highpoly_suffix)
                if not partner:
                    self._skipped.append(partner.reason)
                    self._dropped.append(obj.name)
                    continue
                high = partner.object
            self._queue.append((obj, high))

        self._total = len(self._queue)
        if not self._queue or not self._start_next(context):
            self.report({'ERROR'}, "Nothing to bake. " +
                        (self._skipped[0] if self._skipped else ""))
            self._finish(context)
            return {'CANCELLED'}

        window_manager = context.window_manager
        window_manager.progress_begin(0.0, 1.0)
        self._timer = window_manager.event_timer_add(0.1, window=context.window)
        window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            self.report({'INFO'}, "Bake cancelled")
            self._finish(context)
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if not self._job.poll():
            fraction = (self._done + self._job.progress) / max(1, self._total)
            context.window_manager.progress_update(fraction)
            context.area.tag_redraw() if context.area else None
            return {'RUNNING_MODAL'}

        if self._job.error:
            self.report({'ERROR'},
                        f"{self._current.name}: {self._job.error}")
            self._finish(context)
            return {'CANCELLED'}

        self._collect(context)
        self._done += 1

        if self._start_next(context):
            return {'RUNNING_MODAL'}

        self._report(context)
        self._finish(context)
        return {'FINISHED'}

    def _collect(self, context):
        from .. import external as ext

        produced = ext.read_output(self._job.output_path)
        images = ext.apply_maps(self._current, produced)
        self._maps += len(images)
        self._job.cleanup()

    def _run_blocking(self, context):
        from .. import external as ext

        self._skipped = []
        self._dropped = []
        self._done = 0
        self._maps = 0
        self._current = None
        self._directory = tempfile.mkdtemp(prefix="paintr2_bake_")
        props = context.scene.paintr2

        self._queue = []
        for obj in bake_targets(context):
            high = None
            if props.use_highpoly_bake:
                partner = paintset.find_high(obj, props.highpoly_suffix)
                if not partner:
                    self._skipped.append(partner.reason)
                    self._dropped.append(obj.name)
                    continue
                high = partner.object
            self._queue.append((obj, high))
        self._total = len(self._queue)

        while self._start_next(context):
            while not self._job.poll():
                time.sleep(0.02)
            if self._job.error:
                self.report({'ERROR'}, f"{self._current.name}: {self._job.error}")
                self._finish(context)
                return {'CANCELLED'}
            self._collect(context)
            self._done += 1

        if self._done == 0:
            self.report({'ERROR'}, "Nothing baked. " +
                        (self._skipped[0] if self._skipped else ""))
            self._finish(context)
            return {'CANCELLED'}

        self._report(context)
        self._finish(context)
        return {'FINISHED'}

    def _report(self, context):
        where = self._job.device if self._job else ""
        note = f"Baked {self._maps} map(s) for {self._done} model(s)"
        if where:
            note += f" on {where}"
        if self._skipped:
            note += f"; skipped {len(self._skipped)} - {self._skipped[0]}"

        if self._dropped:
            self.report({'WARNING'}, _dropped_note(self._dropped) + note)
            return
        self.report({'INFO'}, note)

    def _finish(self, context):
        if self._job is not None:
            self._job.cancel()
            self._job.cleanup()
            self._job = None
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            context.window_manager.progress_end()
            self._timer = None
        if getattr(self, "_directory", None):
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None


class PAINTR2_OT_bake_mesh_maps(bpy.types.Operator):
    """Bake the ticked mesh maps for every selected model

    Off the model itself, or -- with Highpoly Bake on -- off the matching
    high-poly. A model with no partner is skipped and named in the report; it
    is never baked off itself instead, because a map that silently came from
    the wrong place is worse than no map.
    """

    bl_idname = "paintr2.bake_mesh_maps"
    bl_label = "Bake Mesh Maps"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_selected_meshes(context))

    def execute(self, context):
        props = context.scene.paintr2
        objects = bake_targets(context)

        wanted = [
            identifier for identifier in meshmaps.MAP_IDS
            if getattr(props, "bake_map_" + identifier.lower(), True)
        ]
        if not wanted:
            self.report({'ERROR'}, "No mesh map is selected")
            return {'CANCELLED'}

        use_high = props.use_highpoly_bake
        options = meshmaps.BakeOptions.from_scene(context.scene)

        baked = 0
        maps = 0
        skipped = []
        dropped = []

        # A hidden object is not in the depsgraph, and a ray that lands on
        # nothing bakes a blank map without saying so. The high-polys are in
        # `Paint_high` and the session hides that collection, so every bake
        # goes through here.
        with isolation.suspended(context):
            for obj in objects:
                high = None
                if use_high:
                    partner = paintset.find_high(obj, props.highpoly_suffix)
                    if not partner:
                        skipped.append(partner.reason)
                        dropped.append(obj.name)
                        continue
                    high = partner.object

                result = meshmaps.bake_maps(
                    obj,
                    wanted,
                    size=int(props.mesh_map_resolution),
                    margin=props.bake_margin,
                    smooth=props.curvature_smooth,
                    high=high,
                    options=options,
                    occluders=occluders(context, obj),
                )

                if result.error is not None:
                    self.report(
                        {'ERROR'},
                        f"{obj.name}: mesh map bake failed: {result.error}",
                    )
                    return {'CANCELLED'}

                if result.images:
                    baked += 1
                    maps += len(result.images)
                else:
                    dropped.append(obj.name)
                for identifier, reason in result.skipped:
                    skipped.append(
                        f"{obj.name}: {meshmaps.map_label(identifier)} {reason}"
                    )

        if baked == 0:
            self.report(
                {'ERROR'},
                "Nothing baked. " + (skipped[0] if skipped else ""),
            )
            return {'CANCELLED'}

        note = f"Baked {maps} map(s) for {baked} model(s)"
        if skipped:
            note += f"; skipped {len(skipped)} - {skipped[0]}"

        if dropped:
            self.report({'WARNING'}, _dropped_note(dropped) + note)
            return {'FINISHED'}
        self.report({'INFO'}, note)
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_measure_cage,
    PAINTR2_OT_bake_mesh_maps_external,
    PAINTR2_OT_bake_mesh_maps,
)

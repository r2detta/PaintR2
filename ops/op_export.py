# SPDX-License-Identifier: GPL-3.0-or-later
"""Baking and exporting the stack to texture files."""

import contextlib

import bpy

from .. import bake


class PAINTR2_OT_bake_texture(bpy.types.Operator):
    """Bake every switched-on channel of every model in the paint set

    One file per channel per material, each in the colour space and bit depth
    that channel needs -- 16-bit Non-Color for Normal and Height, 8-bit sRGB
    for Base Color. The material's own name is the texture set's name, so a
    material called Body exports as Body_BaseColor.
    """

    bl_idname = "paintr2.bake_texture"
    bl_label = "Bake & Export"
    bl_options = {'REGISTER', 'UNDO'}

    save_files: bpy.props.BoolProperty(
        name="Save to Disk",
        description="Write the baked images to the export folder. When off, "
        "the result stays in the .blend as an image datablock",
        default=True,
    )

    active_only: bpy.props.BoolProperty(
        name="Active Model Only",
        description="Bake only the model being worked on. Off, every model "
        "in the Paint collection is baked -- which is what an export usually "
        "means once there is more than one",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return bool(_targets(context))

    def execute(self, context):
        # Every selected model, one after another. A set of props sharing one
        # export folder is the ordinary case, and running the command once per
        # object was the whole of the work.
        targets = _targets(context)
        if not targets:
            self.report({'ERROR'}, "Select at least one mesh object")
            return {'CANCELLED'}

        if self.active_only:
            active = context.active_object
            targets = [active] if active in targets else targets[:1]

        total_saved, total_baked, total_channels = 0, 0, 0
        failures = []

        from .. import isolation

        # Three things wrap the loop, and each answers a different half of
        # what made this look broken.
        #
        # `_progress` is the only chance to say anything at all: the bake
        # blocks the main thread from the first channel to the last, so a
        # window that says nothing here says nothing for ten seconds and is
        # indistinguishable from a crash.
        #
        # `bake_settings` takes the Cycles switch once for the whole export
        # rather than once per model per channel. In a background Blender that
        # is free; in the window every switch makes the viewport showing this
        # very material drop and rebuild its draw state, on the thread that is
        # supposed to be answering the mouse.
        #
        # `isolation.suspended` is the old one: a bake reads geometry through
        # the view layer, and a hidden model is not in the depsgraph.
        props = context.scene.paintr2
        with _progress(context, self._planned_bakes(context, targets)) as step, \
                bake.bake_settings(context.scene, props.bake_margin) as held, \
                isolation.suspended(context):
            for obj in targets:
                report = self._bake_one(context, obj, step, held)
                if isinstance(report, str):
                    failures.append(f"{obj.name}: {report}")
                    continue
                saved, baked, channels = report
                total_saved += saved
                total_baked += baked
                total_channels = max(total_channels, channels)

        if failures and not total_baked:
            self.report({'ERROR'}, failures[0])
            return {'CANCELLED'}

        # A failure on one model out of five does not throw away the other
        # four; it is reported and the rest are kept.
        if failures:
            first = failures[0]
            more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
            self.report({'WARNING'}, f"Skipped {first}{more}")

        if not self.save_files:
            self.report(
                {'INFO'},
                f"Baked {total_baked} image(s) across {total_channels} "
                f"channel(s) from {len(targets)} model(s), kept in the .blend",
            )
            return {'FINISHED'}

        folder = bpy.path.abspath(context.scene.paintr2.export_directory)
        self.report(
            {'INFO'},
            f"Baked and saved {total_saved} texture(s) from "
            f"{len(targets)} model(s) to {folder}",
        )
        return {'FINISHED'}

    def _planned_bakes(self, context, targets):
        """How many (model, channel) bakes this run is about to do.

        Counted up front because a progress bar needs to know the whole before
        it can report a part, and the channel list is per model: two models do
        not have to agree on what they carry.
        """
        props = context.scene.paintr2
        return sum(len(_channels_for(props, obj)) for obj in targets)

    def _bake_one(self, context, obj, step=None, settings=None):
        """Bake one object. `(saved, baked, channels)`, or a message string."""
        props = context.scene.paintr2

        materials = bake.paint_materials(obj) if obj is not None else []
        if not materials:
            return "no PaintR2 material on it"

        channels = _channels_for(props, obj)
        if not channels:
            return "no channel is switched on for export"

        from .. import document as doc

        def announce(channel, _index):
            if step is not None:
                step(f"{obj.name} - {doc.channel_label(channel)}")

        results = bake.bake_channels(
            obj,
            channels,
            size=int(props.export_resolution),
            margin=props.bake_margin,
            float_buffer=props.export_format == 'OPEN_EXR',
            on_channel=announce,
            settings=settings,
        )

        for channel, result in results.items():
            if result.error is not None:
                return f"{doc.channel_label(channel)} bake failed: {result.error}"

        baked = sum(len(result.images) for result in results.values())

        if not self.save_files:
            return 0, baked, len(channels)

        saved = 0
        for _channel, result in results.items():
            for _material, image in result.images:
                try:
                    bake.save_image(
                        image, props.export_directory, props.export_format
                    )
                except (OSError, RuntimeError) as error:
                    return f"could not save {image.name}: {error}"
                saved += 1

        return saved, baked, len(channels)


class PAINTR2_OT_export_browse(bpy.types.Operator):
    """Choose the folder baked textures are written to"""

    bl_idname = "paintr2.export_browse"
    bl_label = "Export Folder"
    bl_options = {'REGISTER', 'INTERNAL'}

    # `directory` is what Blender fills in when the browser is opened in
    # folder mode -- `filepath` would be the file the user happened to click
    # on inside it, which is not what is being asked for here.
    directory: bpy.props.StringProperty(subtype='DIR_PATH', options={'HIDDEN'})

    def invoke(self, context, _event):
        # The browser opens where the setting already points, so "change this
        # a little" is not "find it again from root".
        current = context.scene.paintr2.export_directory
        if current:
            self.directory = bpy.path.abspath(current)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            return {'CANCELLED'}

        from .. import session

        # Relative to the .blend when it can be: a texture folder beside the
        # file is the folder that survives the project being moved, and it is
        # what the default ("//textures/") already means.
        path = self.directory
        if bpy.data.filepath:
            with contextlib.suppress(ValueError):
                path = bpy.path.relpath(path)

        context.scene.paintr2.export_directory = path
        session.tag_redraw()
        return {'FINISHED'}


def _channels_for(props, obj):
    """The channels to write for `obj`, in CHANNELS order.

    Every channel the object's materials *agree* on: baking a channel one
    material has and another has not would leave half the slots black.
    """
    from .. import document as doc

    materials = bake.paint_materials(obj) if obj is not None else []
    if not materials:
        return []

    wanted = None
    for material in materials:
        offered = set(props.export_channels(material.paintr2))
        wanted = offered if wanted is None else (wanted & offered)

    return [c for c in doc.CHANNEL_IDS if c in (wanted or set())]


@contextlib.contextmanager
def _progress(context, total):
    """Blender's own progress bar and a wait cursor, for the duration.

    Yields `step(label)`, called once before each bake. Both halves matter and
    they say different things: the cursor says "this window is busy, not
    broken" the instant the click lands, and the bar in the status area says
    how much of it is left.

    Everything here is guarded. `window_manager.progress_begin` needs a real
    window manager and the cursor needs a window, and a headless bake -- every
    test, every scripted export -- has neither. A progress report is not worth
    failing an export over, so when it cannot be given it simply is not.
    """
    window_manager = getattr(context, "window_manager", None)
    window = getattr(context, "window", None)
    total = max(1, int(total))
    done = 0

    if window is not None:
        with contextlib.suppress(AttributeError, RuntimeError, TypeError):
            window.cursor_set('WAIT')

    if window_manager is not None:
        with contextlib.suppress(AttributeError, RuntimeError, TypeError):
            window_manager.progress_begin(0, total)

    def step(_label=""):
        nonlocal done
        done += 1
        if window_manager is not None:
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                window_manager.progress_update(done)

    try:
        yield step
    finally:
        if window_manager is not None:
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                window_manager.progress_end()
        if window is not None:
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                window.cursor_set('DEFAULT')


def _targets(context):
    """Every model in the paint set, or the selection when there is no set.

    The paint set is what an export is *for*: a session with four models in
    it produces four models' worth of texture sets, and having to remember to
    select all four first is a way to ship three. Outside a paint set the old
    rule stands, so this still works on a file nobody has prepared.

    One texture set per material, not per object: two objects sharing a
    material share the texture, which is what sharing a material means. The
    de-duplication happens naturally -- `bake.paint_materials` returns the
    same material for both, and the second bake writes the same filename.
    """
    from .. import paintset

    models = paintset.paint_objects()
    if models:
        return models

    selected = [obj for obj in context.selected_objects if obj.type == 'MESH']
    if selected:
        return selected

    active = context.active_object
    return [active] if active is not None and active.type == 'MESH' else []


CLASSES = (PAINTR2_OT_bake_texture, PAINTR2_OT_export_browse)

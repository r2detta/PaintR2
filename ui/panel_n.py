# SPDX-License-Identifier: GPL-3.0-or-later
"""3D Viewport sidebar entry point.

This is the *only* native Blender UI PaintR2 has, and it stays deliberately
small: preparing a model, opening the window, and exporting. Everything about
working on a stack -- layers, masks, painting -- belongs in the PaintR2
window, drawn by gui/.
"""

import bpy

from .. import session


class PAINTR2_PT_sidebar(bpy.types.Panel):
    bl_label = "PaintR2"
    bl_idname = "PAINTR2_PT_sidebar"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"

    def draw(self, context):
        layout = self.layout
        is_open = session.state.is_open
        props = context.scene.paintr2

        # Preparation comes first: a model needs a paint material before the
        # window is of any use.
        box = layout.box()
        box.label(text="Prepare", icon='MATERIAL')
        box.prop(props, "image_resolution")
        box.prop(props, "image_base_color", text="Base Color")

        row = box.row()
        row.scale_y = 1.3
        row.operator("paintr2.set_models", text="Set Models", icon='CHECKMARK')

        targets = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not targets and context.active_object is not None:
            if context.active_object.type == 'MESH':
                targets = [context.active_object]
        box.label(text=f"{len(targets)} mesh selected", icon='INFO')

        # Said before it is pressed, not reported after: this rewrites the
        # material the artist already has, and the name being kept is exactly
        # what makes that easy to miss.
        warning = box.column(align=True)
        warning.active = False
        warning.label(text="Existing materials are reset in place;")
        warning.label(text="their names are kept.")

        # What the set currently holds. The set, not the selection: it is what
        # the window shows and what an export walks, and it stops being
        # obvious the moment the selection has moved on.
        from .. import paintset

        models = paintset.paint_objects()
        set_box = layout.box()
        set_box.label(
            text=f"{paintset.PAINT}: {len(models)} model(s)",
            icon='OUTLINER_COLLECTION',
        )
        row = set_box.row()
        row.enabled = bpy.ops.paintr2.unset_models.poll()
        row.operator(
            "paintr2.unset_models", text="Remove from Set", icon='LOOP_BACK'
        )

        from .. import isolation

        if isolation.is_isolated(context):
            hidden = len(props.hidden_objects)
            row = set_box.row()
            row.operator(
                "paintr2.show_all",
                text=f"Show {hidden} Hidden Object(s)",
                icon='HIDE_OFF',
            )

        layout.separator()

        column = layout.column()
        column.scale_y = 1.6
        if is_open:
            column.operator("paintr2.close", text="Close PaintR2", icon='WINDOW')
        else:
            column.operator("paintr2.open", text="Open PaintR2", icon='BRUSH_DATA')

        if not is_open:
            layout.label(text="Layers and painting live", icon='INFO')
            layout.label(text="in the PaintR2 window")


def _active_document(context):
    """The stack of the active object's active material slot, or None."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    material = obj.active_material
    if material is None or not hasattr(material, "paintr2"):
        return None
    return material.paintr2


class PAINTR2_PT_channels(bpy.types.Panel):
    """Which channels this material carries.

    A document-level setting, and the one PaintR2 setting that belongs out
    here rather than in the window: it is decided once when a material
    is set up and then rarely touched, and the window's panel is for the
    stack, not for the shape of the material.
    """

    bl_label = "Channels"
    bl_idname = "PAINTR2_PT_channels"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"

    @classmethod
    def poll(cls, context):
        return _active_document(context) is not None

    def draw(self, context):
        from .. import document as doc

        layout = self.layout
        document = _active_document(context)

        column = layout.column(align=True)
        for identifier in doc.CHANNEL_IDS:
            column.prop(document, doc.channel_use_attr(identifier))

        # The two scales only mean anything when the channels that use them
        # are on, so they only appear then.
        if document.uses('NORMAL') or document.uses('HEIGHT'):
            layout.separator()
            scales = layout.column(align=True)
            if document.uses('NORMAL'):
                scales.prop(document, "normal_scale")
            if document.uses('HEIGHT'):
                scales.prop(document, "height_scale")

        if not document.uses('BASE_COLOR'):
            layout.label(text="Base Color off - nothing to paint", icon='INFO')


class PAINTR2_PT_meshmaps(bpy.types.Panel):
    """Baking what the model itself is: AO, curvature, thickness, position.

    With Highpoly Bake on, all of it -- plus Normal and Height -- is read off
    a matching high-poly model instead. The pairing has a panel of its own
    below this one.

    Nothing in the layer stack consumes these yet; they are baked, written
    beside the .blend, and available to a Custom pattern in the shader graph.
    """

    bl_label = "Mesh Maps"
    bl_idname = "PAINTR2_PT_meshmaps"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from .. import meshmaps

        layout = self.layout
        props = context.scene.paintr2
        obj = context.active_object

        from .. import external

        binary = external.available()
        use_external = props.mesh_map_backend != 'CYCLES' and binary is not None

        layout.prop(props, "mesh_map_resolution")
        layout.prop(props, "mesh_map_backend", text="")

        column = layout.column(align=True)
        for identifier in meshmaps.MAP_IDS:
            row = column.row(align=True)
            # A map that cannot be produced with the current settings is drawn
            # dim, so the reason it will not appear is visible before the bake
            # rather than in the report afterwards.
            available = True
            if meshmaps.needs_high(identifier) and not props.use_highpoly_bake:
                available = False
            if meshmaps.needs_external(identifier) and not use_external:
                available = False
            row.active = available
            row.prop(props, "bake_map_" + identifier.lower())
            # A tick beside the ones already on disk, so a second bake is a
            # decision rather than a guess.
            baked = meshmaps.stored(obj, identifier) if obj is not None else None
            row.label(text="", icon='CHECKMARK' if baked else 'BLANK1')

        # Per-map settings. Only the external baker reads most of them --
        # Cycles has a bake operator with two knobs and no way to be told any
        # of this -- so the block says so instead of quietly doing nothing.
        if props.bake_map_ao:
            box = layout.box()
            box.label(text="Ambient Occlusion", icon='SHADING_RENDERED')
            column = box.column(align=True)
            column.enabled = use_external
            if not use_external:
                box.label(text="External baker only", icon='INFO')
            row = column.row(align=True)
            row.prop(props, "ao_distance", text="Max Distance")
            row.prop(props, "ao_distance_relative", text="", icon='CON_SIZELIMIT')
            column.prop(props, "ao_min_distance", text="Min Distance")
            column.prop(props, "ao_spread", text="Spread")
            column.prop(props, "ao_distribution", text="")
            column.prop(props, "ao_attenuation", text="")
            column.prop(props, "ao_ignore_backface")

        if props.bake_map_curvature:
            box = layout.box()
            box.label(text="Curvature", icon='MOD_BEVEL')
            column = box.column(align=True)
            column.enabled = use_external
            row = column.row(align=True)
            row.prop(props, "curvature_radius", text="Radius")
            row.prop(props, "curvature_radius_relative", text="",
                     icon='CON_SIZELIMIT')
            column.prop(props, "curvature_low", text="Range Min")
            column.prop(props, "curvature_high", text="Range Max")

            cycles_only = box.column(align=True)
            cycles_only.enabled = not use_external
            cycles_only.prop(props, "curvature_iterations")
            cycles_only.prop(props, "curvature_smooth")

        quality = layout.column(align=True)
        quality.prop(props, "mesh_map_samples")
        quality.prop(props, "bake_margin")
        quality.prop(props, "bake_margin_type", text="")

        if obj is None or obj.type != 'MESH':
            layout.label(text="Select a mesh object", icon='ERROR')
        elif not obj.data.uv_layers:
            layout.label(text="No UV map - unwrap first", icon='ERROR')

        count = len([
            o for o in context.selected_objects
            if o is not None and o.type == 'MESH'
        ])
        if props.mesh_map_backend == 'EXTERNAL' and binary is None:
            layout.label(text="External baker not installed", icon='ERROR')

        row = layout.row()
        row.scale_y = 1.4
        row.operator(
            "paintr2.bake_mesh_maps_external" if use_external
            else "paintr2.bake_mesh_maps",
            icon='TEXTURE',
            text="Bake Mesh Maps" if count <= 1 else f"Bake {count} Models",
        )


class PAINTR2_PT_highpoly(bpy.types.Panel):
    """Where a high-poly bake is set up, and where it says what it found.

    The pairing is deliberately strict -- name match inside collection match,
    nothing fuzzy -- so the panel's real job is to show, for the active model,
    which side of which pair it is on and what it resolved to. A high-poly
    bake that quietly does nothing is the failure this readout exists to
    prevent.
    """

    bl_label = "Highpoly Bake"
    bl_idname = "PAINTR2_PT_highpoly"
    bl_parent_id = "PAINTR2_PT_meshmaps"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"

    def draw_header(self, context):
        self.layout.prop(context.scene.paintr2, "use_highpoly_bake", text="")

    def draw(self, context):
        from .. import paintset

        layout = self.layout
        props = context.scene.paintr2
        layout.active = props.use_highpoly_bake

        # --- setting the pair up ---
        # No button: "Set Models" makes `Paint_high` beside `Paint`, and the
        # rest of the setup is dropping the high-polys into it under the right
        # name. A second button that made a second kind of pair was a second
        # convention to learn for nothing.
        box = layout.box()
        box.prop(props, "highpoly_suffix")
        note = box.column(align=True)
        note.active = False
        note.label(text=f"Put high-polys in {paintset.HIGH},")
        note.label(text=f"named <model>{props.highpoly_suffix}")

        # --- what the active object resolves to ---
        obj = context.active_object
        if obj is not None and obj.type == 'MESH':
            where, note = paintset.status(obj, props.highpoly_suffix)
            column = layout.column(align=True)
            column.label(text=obj.name, icon='OBJECT_DATA')
            if where:
                column.label(text=where, icon='OUTLINER_COLLECTION')
            column.label(
                text=note,
                icon='CHECKMARK' if note.startswith("->") else 'ERROR',
            )

        # --- the two numbers the bake stands on ---
        box = layout.box()
        box.label(text="Cage", icon='MOD_MESHDEFORM')
        box.prop(props, "cage_extrusion")
        box.prop(props, "max_ray_distance")

        row = box.row(align=True)
        row.prop(props, "cage_safety")
        row.operator("paintr2.measure_cage", icon='DRIVER_DISTANCE')

        if props.measured_outward >= 0.0:
            # The readings, not the settings. Showing them is what turns the
            # two fields above from magic numbers into a decision.
            readings = box.column(align=True)
            readings.active = False
            readings.label(text=f"Measured out  {props.measured_outward:.4f} m")
            readings.label(text=f"Measured in   {props.measured_inward:.4f} m")

        # --- per-map settings that only exist with a high-poly ---
        if props.bake_map_normal:
            box = layout.box()
            box.label(text="Normal", icon='NORMALS_FACE')
            box.prop(props, "normal_flip_green")

        if props.bake_map_height:
            box = layout.box()
            box.label(text="Height", icon='MOD_DISPLACE')
            box.prop(props, "height_range")


class PAINTR2_PT_export(bpy.types.Panel):
    bl_label = "Export"
    bl_idname = "PAINTR2_PT_export"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PaintR2"

    def draw(self, context):
        from .. import bake as bake_module
        from .. import storage

        layout = self.layout
        props = context.scene.paintr2

        column = layout.column(align=True)
        column.prop(props, "export_resolution")
        column.prop(props, "export_format")
        column.prop(props, "bake_margin")

        layout.prop(props, "export_directory")

        # One file per channel, so which channels is part of the export
        # settings. Only the ones this material actually carries are offered;
        # the rest are not choices, they are absences.
        document = _active_document(context)
        if document is not None:
            from .. import document as doc

            box = layout.box()
            box.label(text="Channels to write", icon='FILE_IMAGE')
            grid = box.column(align=True)
            for identifier in document.active_channels():
                grid.prop(props, "export_" + identifier.lower())
            if not document.active_channels():
                box.label(text="No channel is switched on", icon='ERROR')

        models = [
            o for o in context.selected_objects if o is not None and o.type == 'MESH'
        ]
        obj = context.active_object
        if not models and obj is not None and obj.type == 'MESH':
            models = [obj]

        if obj is not None and obj.type == 'MESH':
            count = sum(len(bake_module.paint_materials(o)) for o in models)
            if count:
                layout.label(
                    text=f"{count} material to bake on {len(models)} model(s)",
                    icon='INFO',
                )
            else:
                layout.label(text="No PaintR2 material here", icon='ERROR')

            missing = [o.name for o in models if not o.data.uv_layers]
            if missing:
                layout.label(
                    text=f"No UV map on {missing[0]} - unwrap first", icon='ERROR'
                )

        row = layout.row()
        row.scale_y = 1.4
        row.operator(
            "paintr2.bake_texture",
            icon='RENDER_STILL',
            text="Bake & Export" if len(models) <= 1
            else f"Bake & Export {len(models)} Models",
        )

        layout.separator()

        # Where the painted pixels are, in one line. Without it, pressing Pack
        # and seeing nothing change is indistinguishable from Pack not working
        # -- which is exactly how it was reported.
        images = storage.layer_images()
        packed = storage.packed_images()
        unsaved = storage.unsaved_images()
        folder = storage.external_directory()

        if not images:
            layout.label(text="No layer images yet", icon='INFO')
        elif len(packed) == len(images):
            layout.label(
                text=f"{len(packed)} image packed in the .blend", icon='PACKAGE'
            )
        elif packed:
            layout.label(
                text=f"{len(packed)} of {len(images)} packed", icon='PACKAGE'
            )
        elif folder is None:
            layout.label(text="Only in memory - save or pack", icon='ERROR')
        elif unsaved:
            layout.label(text=f"{len(unsaved)} image not on disk", icon='ERROR')
        else:
            layout.label(text=f"{len(images)} image beside the .blend",
                         icon='FILE_TICK')

        row = layout.row(align=True)
        save = row.row(align=True)
        save.enabled = folder is not None
        save.operator("paintr2.save_layers", text="Save Layers", icon='FILE_TICK')

        # Pack and Unpack are one button that swaps: with everything already
        # packed, "Pack" is a button that can only say what it already says.
        if images and len(packed) == len(images):
            unpack = row.row(align=True)
            unpack.enabled = folder is not None
            unpack.operator(
                "paintr2.unpack_layers", text="Unpack", icon='UGLYPACKAGE'
            )
        else:
            sub = row.row(align=True)
            sub.enabled = bool(images)
            sub.operator("paintr2.pack_layers", text="Pack", icon='PACKAGE')


CLASSES = (
    PAINTR2_PT_sidebar,
    PAINTR2_PT_channels,
    PAINTR2_PT_meshmaps,
    PAINTR2_PT_highpoly,
    PAINTR2_PT_export,
)

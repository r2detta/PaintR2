# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer stack operators: add, remove, reorder.

Every one of these mutates the document and then leans on document.py's
update hooks (or an explicit recompile) to bring the node tree back in sync.
"""

import bpy

from .. import adjustment
from .. import effects
from .. import nodegen
from .. import prepare
from .. import procedural


def active_material(context):
    """The material the layer panel is currently pointed at.

    Blender's own two answers, unchanged: the active object, and that
    object's active material slot. The Objects/Materials panel writes exactly
    those two, so pointing it somewhere else is what moves every operator in
    this file with it.
    """
    obj = context.active_object
    if obj is None:
        return None
    return obj.active_material


def _document(context):
    material = active_material(context)
    return material.paintr2 if material is not None else None


_BASE_NAMES = {
    'PAINT': "Paint",
    'FILL': "Fill",
    'PROCEDURAL': "Pattern",
    'GROUP': "Group",
    'ADJUSTMENT': "Adjustment",
}


def _unique_layer_name(document, base, skip=-1):
    """`base`, or `base 2`, `base 3`... if the stack already uses it.

    `skip` is the index of a layer that does not count as a collision, so
    renaming a layer to what it is already called is not turned into "Fill 2".
    """
    existing = {
        layer.name for index, layer in enumerate(document.layers) if index != skip
    }
    if base not in existing:
        return base

    index = 2
    while f"{base} {index}" in existing:
        index += 1
    return f"{base} {index}"


class PAINTR2_OT_layer_add(bpy.types.Operator):
    """Add a layer to the active material's stack"""

    bl_idname = "paintr2.layer_add"
    bl_label = "Add Layer"
    bl_options = {'REGISTER', 'UNDO'}

    layer_type: bpy.props.EnumProperty(
        name="Type",
        items=(
            ('PAINT', "Paint", "An image you paint into"),
            ('FILL', "Fill", "A flat color covering the whole surface"),
            ('PROCEDURAL', "Procedural", "A generated pattern, editable forever"),
            ('GROUP', "Group", "A folder holding the layers under it"),
            ('ADJUSTMENT', "Adjustment",
             "Changes everything under it, within its group"),
        ),
        default='PAINT',
    )

    kind: bpy.props.EnumProperty(
        name="Pattern",
        description="Which pattern a procedural layer starts as",
        items=procedural.KINDS,
        default='GRADIENT',
    )

    adjustment_kind: bpy.props.EnumProperty(
        name="Adjustment",
        description="Which adjustment the layer starts as",
        items=adjustment.KINDS,
        default='HSV',
    )

    @classmethod
    def poll(cls, context):
        return active_material(context) is not None

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2

        base_name = _BASE_NAMES.get(self.layer_type, "Layer")
        if self.layer_type == 'PROCEDURAL':
            base_name = procedural.kind_label(self.kind)
        elif self.layer_type == 'ADJUSTMENT':
            base_name = adjustment.kind_label(self.adjustment_kind)

        layer = document.add_layer(
            _unique_layer_name(document, base_name), self.layer_type
        )

        if self.layer_type == 'ADJUSTMENT':
            layer.adjustment.kind = self.adjustment_kind

        if self.layer_type == 'PROCEDURAL':
            layer.procedural.kind = self.kind
            layer.procedural.enabled = True
            if procedural.edits_in_graph(self.kind):
                procedural.ensure_custom_tree(
                    layer.procedural, material, layer.name
                )

        if self.layer_type == 'PAINT':
            props = context.scene.paintr2
            image, _ = prepare.ensure_image(
                prepare.unique_image_name(f"{material.name}_{layer.name}"),
                int(props.image_resolution),
                # A fresh paint layer starts fully transparent so the layers
                # below it stay visible until something is actually painted.
                (0.0, 0.0, 0.0, 0.0),
            )
            layer.image = image

        # A new layer arrives with its content half selected -- not because
        # anything requires it, but because the alternative is a layer nobody
        # has pointed at yet, and the first thing done to a new layer is
        # almost never "click its thumbnail". A group has no content half.
        if self.layer_type != 'GROUP':
            document.set_active_part('COLOR')

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_procedural_add(bpy.types.Operator):
    """Add a generated pattern as a new layer

    Always a new layer. It used to land on the active layer's mask instead
    whenever the mask chip happened to be the selected half, which meant one
    button did two different things depending on a border round a 20-pixel
    square -- and the pattern it made never appeared in the list, so from the
    outside the click did nothing at all.

    A pattern *on* a mask is a Procedural effect now (Effect menu ->
    Procedural...), which is where every other pattern-on-something lives and
    which shows up as a row under the layer it belongs to.
    """

    bl_idname = "paintr2.procedural_add"
    bl_label = "Add Procedural"
    bl_options = {'REGISTER', 'UNDO'}

    kind: bpy.props.EnumProperty(
        name="Pattern",
        items=procedural.KINDS,
        default='GRADIENT',
    )

    @classmethod
    def poll(cls, context):
        return active_material(context) is not None

    def execute(self, context):
        return bpy.ops.paintr2.layer_add(
            layer_type='PROCEDURAL', kind=self.kind
        )


class PAINTR2_OT_layer_select_none(bpy.types.Operator):
    """Deselect every layer"""

    bl_idname = "paintr2.layer_select_none"
    bl_label = "Deselect Layers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and document.has_selection

    def execute(self, context):
        _document(context).select_none()
        return {'FINISHED'}


class PAINTR2_OT_layer_solo(bpy.types.Operator):
    """Show one layer, or one mask, instead of the whole stack"""

    bl_idname = "paintr2.layer_solo"
    bl_label = "Solo Layer"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('LAYER', "Layer", "Show only this layer"),
            ('MASK', "Mask", "Show this layer's mask, as grey"),
            ('NONE', "Off", "Show the whole stack again"),
        ),
        default='LAYER',
    )

    index: bpy.props.IntProperty(
        name="Layer",
        description="Which layer, or -1 for the active one",
        default=-1,
    )

    @classmethod
    def poll(cls, context):
        return _document(context) is not None

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2

        if self.mode == 'NONE':
            document.solo_clear()
            nodegen.compile_material(material)
            return {'FINISHED'}

        index = self.index if self.index >= 0 else document.active_index
        if not (0 <= index < len(document.layers)):
            return {'CANCELLED'}

        if self.mode == 'MASK' and not document.layers[index].has_mask:
            self.report({'WARNING'}, "That layer has no mask to show")
            return {'CANCELLED'}

        # The same command on the same layer turns it off again: a solo that
        # can only be switched on needs a second control to get out of, and
        # every application that has one binds both to the same key.
        mode, current = document.solo_target()
        if mode == self.mode and current == index:
            document.solo_clear()
        else:
            document.solo_index = index
            document.solo_mode = self.mode

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_layer_tag(bpy.types.Operator):
    """Put a colour tag on the selected layers"""

    bl_idname = "paintr2.layer_tag"
    bl_label = "Color Tag"
    bl_options = {'REGISTER', 'UNDO'}

    color: bpy.props.StringProperty(
        name="Color",
        description="A tag identifier, or NONE to clear it",
        default='NONE',
    )

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 0

    def execute(self, context):
        document = _document(context)

        # Every selected layer, not only the active one: tagging is how a
        # stack is sorted out, and sorting one layer at a time is not.
        indices = document.selected_indices()
        if not indices:
            return {'CANCELLED'}

        for index in indices:
            try:
                document.layers[index].tag_color = self.color
            except TypeError:
                self.report({'WARNING'}, f"Unknown color tag: {self.color}")
                return {'CANCELLED'}

        # No recompile: a tag is a label on the row and changes nothing about
        # what the material looks like.
        return {'FINISHED'}


class PAINTR2_OT_effect_add(bpy.types.Operator):
    """Add a filter or a generated pattern to the selected half of the layer

    Where it lands is not an argument and not a menu entry: it is whichever
    half of the layer is selected. That was two questions -- "add a filter"
    then "to the layer or to the mask" -- and the second one was already
    answered on screen by the chip with the border round it.

    With neither half selected there is no answer, so nothing is added. That
    is the whole of "a filter belongs to a layer": no layer, or no half of
    one, means nowhere to put it.
    """

    bl_idname = "paintr2.effect_add"
    bl_label = "Add Effect"
    bl_options = {'REGISTER', 'UNDO'}

    effect_type: bpy.props.EnumProperty(
        name="Type",
        items=effects.TYPES,
        default='FILTER',
    )

    kind: bpy.props.StringProperty(
        name="Kind",
        description="Which filter, or which pattern",
        default="",
    )

    @classmethod
    def poll(cls, context):
        document = _document(context)
        if document is None or document.active_layer is None:
            return False
        if len(document.selected_indices()) > 1:
            return False
        return document.active_part != 'NONE'

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2
        layer = document.active_layer
        if layer is None:
            self.report({'WARNING'}, "Add a layer first")
            return {'CANCELLED'}

        if len(document.selected_indices()) > 1:
            # Which of the five would it go on? An ambiguous edit is not
            # made at all.
            self.report({'WARNING'}, "Select one layer")
            return {'CANCELLED'}

        part = layer.active_part
        if part == 'NONE':
            self.report(
                {'WARNING'},
                "Select the layer's Color or Mask first",
            )
            return {'CANCELLED'}

        on_mask = part == 'MASK'
        if on_mask and not layer.has_mask:
            # The chip is only on the row while there is a mask, so this is
            # the stale case rather than the ordinary one -- but a filter with
            # nothing to filter is still not something to add quietly.
            self.report({'WARNING'}, "That layer has no mask to filter")
            return {'CANCELLED'}

        # An empty kind is the ordinary case for a filter: it starts at its
        # type's default and *which* filter it is gets chosen in the
        # properties section, beside the parameters that choice changes. A
        # pattern is picked up front, because a pattern is a family the way a
        # procedural layer is one.
        kinds = [item[0] for item in effects.kinds_for(self.effect_type)]
        kind = self.kind if self.kind in kinds else ""

        effect = effects.add(
            layer, self.effect_type, kind, on_mask,
            channel=None if on_mask else document.view_channel,
        )

        if self.effect_type == 'PATTERN' and \
                procedural.edits_in_graph(effect.pattern.kind):
            procedural.ensure_custom_tree(
                effect.pattern, material, effect.label()
            )

        if self.effect_type == 'PAINT':
            # Transparent, so the effect does nothing until it is painted --
            # the alpha is what it blends by. Non-Color on a mask for the same
            # reason the mask image itself is: a mask is data, and 0.5 written
            # into an sRGB image reads back as 0.74.
            image, _made = prepare.ensure_image(
                prepare.unique_image_name(
                    f"{material.name}_{layer.name}"
                    f"_{'mask' if on_mask else 'color'}_paint"
                ),
                int(context.scene.paintr2.image_resolution),
                (0.0, 0.0, 0.0, 0.0),
                colorspace='Non-Color' if on_mask else 'sRGB',
            )
            effect.image = image

        # The new effect is what the properties section edits: adding one and
        # then having to find it is not adding one.
        document.active_effect = 0

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_effect_remove(bpy.types.Operator):
    """Remove an effect from the active layer"""

    bl_idname = "paintr2.effect_remove"
    bl_label = "Remove Effect"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)

    @classmethod
    def poll(cls, context):
        document = _document(context)
        layer = document.active_layer if document is not None else None
        if layer is None or document.active_part == 'NONE':
            return False
        return bool(len(layer.effect_stack(document.active_on_mask)))

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2
        layer = document.active_layer
        if layer is None or document.active_part == 'NONE':
            return {'CANCELLED'}

        on_mask = document.active_on_mask
        index = self.index if self.index >= 0 else document.active_effect
        if not effects.remove(layer, index, on_mask):
            return {'CANCELLED'}

        stack = layer.effect_stack(on_mask)
        document.active_effect = min(index, len(stack) - 1)

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_effect_move(bpy.types.Operator):
    """Move an effect up or down its stack"""

    bl_idname = "paintr2.effect_move"
    bl_label = "Move Effect"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(default=-1)
    direction: bpy.props.EnumProperty(
        items=(('UP', "Up", ""), ('DOWN', "Down", "")),
        default='UP',
    )

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2
        layer = document.active_layer
        if layer is None or document.active_part == 'NONE':
            return {'CANCELLED'}

        on_mask = document.active_on_mask
        index = self.index if self.index >= 0 else document.active_effect
        landed = effects.move(layer, index, self.direction, on_mask)
        if landed == index:
            return {'CANCELLED'}

        document.active_effect = landed

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_effect_select(bpy.types.Operator):
    """Make an effect the one the properties section edits"""

    bl_idname = "paintr2.effect_select"
    bl_label = "Select Effect"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: bpy.props.IntProperty(default=-1)
    index: bpy.props.IntProperty(default=-1)
    on_mask: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        document = _document(context)
        if document is None:
            return {'CANCELLED'}

        if 0 <= self.layer_index < len(document.layers):
            document.select_only(self.layer_index)

        # Selecting a mask filter selects the mask. The row is only on screen
        # because that half is selected, so this is normally a no-op -- but it
        # is the half the effect belongs to that decides, not the other way
        # round, and saying so here is what keeps the two from ever drifting.
        document.set_active_part('MASK' if self.on_mask else 'COLOR')
        document.active_effect = self.index
        return {'FINISHED'}


class PAINTR2_OT_effect_toggle(bpy.types.Operator):
    """Switch an effect off without losing its settings"""

    bl_idname = "paintr2.effect_toggle"
    bl_label = "Toggle Effect"
    bl_options = {'REGISTER', 'UNDO'}

    layer_index: bpy.props.IntProperty(default=-1)
    index: bpy.props.IntProperty(default=0)
    on_mask: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2 if material is not None else None
        if document is None:
            return {'CANCELLED'}

        index = self.layer_index
        if not (0 <= index < len(document.layers)):
            index = document.active_index
        if not (0 <= index < len(document.layers)):
            return {'CANCELLED'}

        stack = document.layers[index].effect_stack(self.on_mask)
        if not (0 <= self.index < len(stack)):
            return {'CANCELLED'}

        stack[self.index].enabled = not stack[self.index].enabled
        return {'FINISHED'}


def _selected_blocks(document):
    """The selected blocks, and the parent they all sit in.

    Returns `(blocks, parent)`, or `(blocks, None)` when they do not share
    one. Moving or grouping layers that live in different groups has no
    answer that is not a guess -- do they all come out, or do they each move
    inside their own group? -- so the operators refuse that case by name
    rather than picking one.
    """
    blocks = document.top_blocks(document.selected_indices())
    if not blocks:
        return [], None

    parents = {document.parent_index(index) for index in blocks}
    if len(parents) != 1:
        return blocks, None
    return blocks, parents.pop()


class PAINTR2_OT_layer_remove(bpy.types.Operator):
    """Remove the active layer"""

    bl_idname = "paintr2.layer_remove"
    bl_label = "Remove Layer"
    bl_options = {'REGISTER', 'UNDO'}

    contents: bpy.props.EnumProperty(
        name="Contents",
        description="What happens to the layers inside a group",
        items=(
            ('KEEP', "Keep", "Move them out of the group, one level up"),
            ('DELETE', "Delete", "Delete them along with the group"),
        ),
        default='KEEP',
    )

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 0

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2

        # Everything selected goes, not only the active one: selecting five
        # layers and pressing delete four more times is not a workflow. The
        # blocks are removed bottom-up so the indices above them stay valid.
        blocks = document.top_blocks(document.selected_indices())
        if not blocks:
            return {'CANCELLED'}

        first = blocks[0]
        for index in reversed(blocks):
            end = document.subtree_end(index)

            # The image datablocks are left alone: they may be shared, and
            # Blender drops unused images on reload anyway.
            if self.contents == 'DELETE':
                for victim in range(end - 1, index - 1, -1):
                    document.layers.remove(victim)
            else:
                # The folder goes, its contents stay and come up a level.
                for child in range(index + 1, end):
                    document.layers[child].depth = max(
                        0, document.layers[child].depth - 1
                    )
                document.layers.remove(index)

        document.active_index = min(first, max(0, len(document.layers) - 1))
        if document.layers:
            document.select_only(document.active_index)
        else:
            document.active_index = -1
        document.normalize()

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_layer_copy(bpy.types.Operator):
    """Copy the selected layers, ready to paste into any material"""

    bl_idname = "paintr2.layer_copy"
    bl_label = "Copy Layers"
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 0

    def execute(self, context):
        from .. import clipboard

        document = _document(context)
        indices = document.selected_indices() or [document.active_index]
        copied = clipboard.copy_layers(document, indices)

        if not copied:
            self.report({'WARNING'}, "Nothing to copy")
            return {'CANCELLED'}

        self.report({'INFO'}, f"{copied} layer(s) copied")
        return {'FINISHED'}


class PAINTR2_OT_layer_paste(bpy.types.Operator):
    """Paste the copied layers into this material as independent copies"""

    bl_idname = "paintr2.layer_paste"
    bl_label = "Paste Layers"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from .. import clipboard

        return active_material(context) is not None and clipboard.has_content()

    def execute(self, context):
        from .. import clipboard

        material = active_material(context)
        document = material.paintr2

        pasted = clipboard.paste_layers(document, unique_name=_unique_layer_name)
        if not pasted:
            self.report({'WARNING'}, "Nothing on the clipboard")
            return {'CANCELLED'}

        nodegen.compile_material(material)

        from .. import paint

        paint.sync_canvas(context)
        self.report(
            {'INFO'},
            f"{len(pasted)} layer(s) pasted into {material.name}",
        )
        return {'FINISHED'}


class PAINTR2_OT_layer_move(bpy.types.Operator):
    """Move the active layer up or down the stack"""

    bl_idname = "paintr2.layer_move"
    bl_label = "Move Layer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=(
            ('UP', "Up", "Move towards the top of the stack"),
            ('DOWN', "Down", "Move towards the bottom of the stack"),
        ),
    )

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 1

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2

        blocks, parent = _selected_blocks(document)
        if not blocks:
            return {'CANCELLED'}
        if parent is None:
            self.report(
                {'WARNING'},
                "Selected layers are in different groups; move them one group "
                "at a time",
            )
            return {'CANCELLED'}

        # A layer moves over its *neighbour*, and a neighbour that is a group
        # is the whole group -- otherwise "move up" would walk into it one row
        # at a time and the two would swap places halfway through. A neighbour
        # that is itself selected is not one either: the selection travels as
        # a block, so it steps over whatever is outside it.
        siblings = document.child_indices(parent)
        if not siblings:
            siblings = document.root_indices()
        chosen = set(blocks)
        outside = [index for index in siblings if index not in chosen]
        if not all(index in siblings for index in blocks) or not outside:
            return {'CANCELLED'}

        # Index 0 is the top of the stack, so "up" means a lower index.
        if self.direction == 'UP':
            above = [index for index in outside if index < blocks[0]]
            if not above:
                return {'CANCELLED'}
            other = above[-1]
            gap, depth = other, document.layers[other].depth
        else:
            below = [index for index in outside if index > blocks[-1]]
            if not below:
                return {'CANCELLED'}
            other = below[0]
            gap, depth = document.subtree_end(other), document.layers[other].depth

        landed = document.move_blocks(blocks, gap, depth)
        if landed < 0:
            return {'CANCELLED'}

        document.active_index = landed
        document.normalize()

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_layer_group_selected(bpy.types.Operator):
    """Put the selected layers into a new group"""

    bl_idname = "paintr2.layer_group_selected"
    bl_label = "Group Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 0

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2

        blocks, parent = _selected_blocks(document)
        if not blocks:
            return {'CANCELLED'}
        if parent is None:
            self.report(
                {'WARNING'},
                "Selected layers are in different groups; group them one "
                "group at a time",
            )
            return {'CANCELLED'}

        # The folder is created where the topmost selected block sits, at that
        # block's depth, and then the blocks move into it. Doing it the other
        # way round -- folder first, blocks after -- is the same two steps the
        # user would otherwise do by hand.
        top = blocks[0]
        depth = document.layers[top].depth

        document.active_index = top
        group = document.add_layer(
            _unique_layer_name(document, _BASE_NAMES.get('GROUP', "Group")),
            'GROUP',
        )
        group.expanded = True
        group.depth = depth

        # `add_layer` inserted the folder at `top`, so every block that was at
        # or below it moved down one.
        group_index = document.active_index
        moving = [index + 1 if index >= group_index else index for index in blocks]

        landed = document.move_blocks(moving, group_index + 1, depth + 1)
        document.normalize()

        if landed >= 0:
            document.select_only(group_index)

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_layer_reorder(bpy.types.Operator):
    """Move a layer to a new position in the stack"""

    bl_idname = "paintr2.layer_reorder"
    bl_label = "Reorder Layer"
    bl_options = {'REGISTER', 'UNDO'}

    from_index: bpy.props.IntProperty(name="From", default=-1)

    # Several layers dragged at once, as a comma-separated list of indices.
    # Empty means "just `from_index`". A string because operator properties
    # have no variable-length integer list, and this one has to survive being
    # replayed by undo like every other operator argument.
    sources: bpy.props.StringProperty(name="Sources", default="")

    # A *gap* index in 0..len(layers): where the layer should be inserted,
    # which is what a drag-and-drop drop point naturally produces.
    to_index: bpy.props.IntProperty(name="To", default=-1)

    # What nesting the drop meant. -1 leaves it to the gap's neighbours, which
    # is what every caller that knows nothing about groups wants.
    to_depth: bpy.props.IntProperty(name="Depth", default=-1)

    @classmethod
    def poll(cls, context):
        document = _document(context)
        return document is not None and len(document.layers) > 1

    def _sources(self, count):
        """The blocks being dragged: the list if there is one, else the row."""
        wanted = []
        for piece in self.sources.split(","):
            piece = piece.strip()
            if piece.lstrip("-").isdigit():
                index = int(piece)
                if 0 <= index < count:
                    wanted.append(index)
        return wanted or [self.from_index]

    def execute(self, context):
        material = active_material(context)
        document = material.paintr2
        count = len(document.layers)

        if not (0 <= self.from_index < count) or not (0 <= self.to_index <= count):
            return {'CANCELLED'}

        starts = document.top_blocks(self._sources(count))
        if not starts:
            return {'CANCELLED'}

        depth = self.to_depth if self.to_depth >= 0 else \
            document.layers[starts[0]].depth

        # Nothing to do: one block, already there, at the depth it is at. Being
        # at the same place but one level in is *not* nothing -- that is how a
        # layer gets adopted by the group above it.
        if len(starts) == 1:
            start = starts[0]
            end = document.subtree_end(start)
            if start <= self.to_index <= end and depth == document.layers[start].depth:
                return {'CANCELLED'}

        landed = document.move_blocks(starts, self.to_index, depth)
        if landed < 0:
            return {'CANCELLED'}

        document.active_index = landed
        document.normalize()

        # Never drop something out of sight: every group the block landed in
        # is opened, so the move is visible the moment it happens.
        parent = document.parent_index(landed)
        while parent >= 0:
            document.layers[parent].expanded = True
            parent = document.parent_index(parent)

        nodegen.compile_material(material)
        return {'FINISHED'}


class PAINTR2_OT_group_toggle(bpy.types.Operator):
    """Open or close a group, or a layer's effect stack"""

    bl_idname = "paintr2.group_toggle"
    bl_label = "Toggle Group"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(name="Index", default=-1)

    @classmethod
    def poll(cls, context):
        return _document(context) is not None

    def execute(self, context):
        document = _document(context)
        if not (0 <= self.index < len(document.layers)):
            return {'CANCELLED'}

        layer = document.layers[self.index]
        # One flag, two meanings that never overlap: a group's children, and
        # an ordinary layer's effects. A group with effects shows both, which
        # is the only case where they meet and is what the user asked for.
        if layer.layer_type != 'GROUP' and not (
            len(layer.effects) or len(layer.mask_effects)
        ):
            return {'CANCELLED'}

        # Nothing to recompile: which rows are drawn is not what is rendered.
        layer.expanded = not layer.expanded
        return {'FINISHED'}


class PAINTR2_OT_layer_rename(bpy.types.Operator):
    """Rename a layer"""

    bl_idname = "paintr2.layer_rename"
    bl_label = "Rename Layer"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(name="Index", default=-1)
    name: bpy.props.StringProperty(name="Name", default="")

    @classmethod
    def poll(cls, context):
        return _document(context) is not None

    def execute(self, context):
        document = _document(context)
        if not (0 <= self.index < len(document.layers)):
            return {'CANCELLED'}

        # An empty name would leave a row with nothing to click on, so it means
        # "no change" rather than an error.
        wanted = self.name.strip()
        if not wanted:
            return {'CANCELLED'}

        layer = document.layers[self.index]
        unique = _unique_layer_name(document, wanted, skip=self.index)
        if layer.name == unique:
            return {'CANCELLED'}

        # Names are labels only -- nothing in the compiler looks them up -- so
        # there is nothing to rebuild here.
        layer.name = unique
        return {'FINISHED'}


class PAINTR2_OT_recompile(bpy.types.Operator):
    """Rebuild the node tree from the layer stack"""

    bl_idname = "paintr2.recompile"
    bl_label = "Rebuild Nodes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_material(context) is not None

    def execute(self, context):
        material = active_material(context)
        if nodegen.compile_material(material):
            self.report({'INFO'}, f"Rebuilt {nodegen.group_name(material)}")
        else:
            self.report({'WARNING'}, "Nothing to compile: the stack is empty")
        return {'FINISHED'}


CLASSES = (
    PAINTR2_OT_layer_add,
    PAINTR2_OT_procedural_add,
    PAINTR2_OT_layer_select_none,
    PAINTR2_OT_layer_solo,
    PAINTR2_OT_layer_tag,
    PAINTR2_OT_effect_add,
    PAINTR2_OT_effect_remove,
    PAINTR2_OT_effect_move,
    PAINTR2_OT_effect_select,
    PAINTR2_OT_effect_toggle,
    PAINTR2_OT_layer_remove,
    PAINTR2_OT_layer_copy,
    PAINTR2_OT_layer_paste,
    PAINTR2_OT_layer_move,
    PAINTR2_OT_layer_group_selected,
    PAINTR2_OT_layer_reorder,
    PAINTR2_OT_layer_rename,
    PAINTR2_OT_group_toggle,
    PAINTR2_OT_recompile,
)

# SPDX-License-Identifier: GPL-3.0-or-later
"""Copying layers from one material's stack into another's.

With a texture set per material, the thing an artist wants to do second is
take the layer they just built and put it on the next set. `Ctrl C` /
`Ctrl V` do that, and the paste is a **copy, not a link**: the pasted
layer gets its own images, and painting into one of the two afterwards does
not touch the other.

That distinction is the whole of why this module exists rather than a call to
`document.layers.add()` in an operator. Two things make an honest copy hard:

* **A layer is a tree of PropertyGroups**, not a record: channels, a
  procedural, an adjustment, two effect stacks each with their own filter and
  pattern. Naming the fields here would mean a new property silently failing
  to copy, so the copy walks RNA instead and copies whatever is there.

* **`image.copy()` does not copy a GENERATED image's pixels** (measured, and
  it is what made an exported mask come out flat). It re-derives them
  from `generated_color`, so a copied paint layer would come back the colour
  it was created in with every stroke gone. Pixels are moved by hand.

A group is copied with everything under it: what is copied is a *subtree*,
because a group without its children is not the thing that was on screen.

The clipboard is module state and dies with the Blender session. It holds
plain data rather than RNA wrappers -- a wrapper into a collection that has
since been reordered points at a different layer.
"""

import bpy


# What was copied, as plain dicts. See `_read_group`.
_layers = []
_source = ""


class _Skip:
    """Properties never worth copying, by identifier."""

    ALWAYS = frozenset({"rna_type"})


def _inherits_property_group(fixed):
    """True when a fixed_type is one of our own PropertyGroups.

    `bpy.types.PropertyGroup` sits in the RNA base chain of every group we
    define, and `base` walks that chain. An ID type -- Image, NodeTree --
    never reaches it, which is exactly the distinction that matters: an ID is
    assigned by pointer, a group has to be recursed into.
    """
    current = fixed
    while current is not None:
        if getattr(current, "identifier", "") == "PropertyGroup":
            return True
        current = getattr(current, "base", None)
    return False


def _read_group(owner):
    """One PropertyGroup as plain data, recursing into everything under it."""
    out = {}
    for prop in owner.bl_rna.properties:
        key = prop.identifier
        if key in _Skip.ALWAYS:
            continue

        if prop.type == 'POINTER':
            value = getattr(owner, key)
            if _inherits_property_group(prop.fixed_type):
                out[key] = ('group', _read_group(value))
            else:
                # An ID: kept by reference. Images are replaced with copies on
                # paste; a node group is a built asset and is shared, the way
                # two materials share one.
                out[key] = ('id', value)
            continue

        if prop.type == 'COLLECTION':
            out[key] = ('collection', [
                _read_group(item) for item in getattr(owner, key)
            ])
            continue

        if prop.is_readonly:
            continue

        value = getattr(owner, key)
        if prop.type in ('FLOAT', 'INT', 'BOOLEAN') and getattr(prop, "array_length", 0):
            value = tuple(value)
        out[key] = ('value', value)

    return out


def _write_group(owner, data, images):
    """Put `_read_group`'s output back onto a fresh PropertyGroup."""
    for key, (kind, value) in data.items():
        if kind == 'group':
            _write_group(getattr(owner, key), value, images)
        elif kind == 'collection':
            target = getattr(owner, key)
            target.clear()
            for item in value:
                _write_group(target.add(), item, images)
        elif kind == 'id':
            if isinstance(value, bpy.types.Image):
                value = images(value)
            try:
                setattr(owner, key, value)
            except (AttributeError, TypeError):
                # A pointer whose poll refuses this datablock. Better a layer
                # missing one reference than no layer at all.
                pass
        else:
            try:
                setattr(owner, key, value)
            except (AttributeError, TypeError, ValueError):
                pass


# --- images -------------------------------------------------------------------


def duplicate_image(image):
    """An independent image with the same pixels. Never shares a buffer.

    `image.copy()` is used for everything but the pixels, so colour space,
    alpha mode and the rest come across without being listed here. The pixels
    are then written across by hand, because a GENERATED image's buffer is
    not part of what `copy()` copies -- it re-derives it from
    `generated_color`, and the result is a flat square with the painting gone.
    """
    if image is None:
        return None

    clone = image.copy()
    clone.name = f"{image.name}_copy"

    width, height = image.size
    channels = image.channels
    expected = width * height * channels
    if expected and len(image.pixels) == expected:
        buffer = [0.0] * expected
        image.pixels.foreach_get(buffer)
        # A GENERATED clone has to become the size it claims before its
        # buffer will accept anything; setting the source is what allocates.
        if clone.size[0] != width or clone.size[1] != height:
            clone.scale(width, height)
        clone.pixels.foreach_set(buffer)
        clone.update()

    if image.packed_file:
        clone.pack()

    from . import storage
    storage.adopt_new_image(clone)
    return clone


class _ImageCopies:
    """One copy per source image, so a layer and its mask stay distinct.

    Also so a layer copied twice in one paste -- a group holding two layers
    that share an image -- ends up sharing one copy rather than two, which is
    what it was before it was copied.
    """

    def __init__(self):
        self._made = {}

    def __call__(self, image):
        if image is None:
            return None
        key = image.as_pointer()
        if key not in self._made:
            self._made[key] = duplicate_image(image)
        return self._made[key]


# --- the clipboard ------------------------------------------------------------


def copy_layers(document, indices):
    """Put the subtrees starting at `indices` on the clipboard. Returns count.

    Whole subtrees: copying a group without its children would paste an empty
    folder. Indices already inside another copied group are dropped, so
    selecting a group *and* one of its children copies each layer once.
    """
    if document is None:
        return 0

    wanted = sorted(set(indices))
    covered = set()
    blocks = []
    for index in wanted:
        if index in covered:
            continue
        span = list(document.subtree(index))
        covered.update(span)
        blocks.append(span)

    if not blocks:
        return 0

    # Flattened back into stack order, so depth still describes the tree.
    ordered = sorted(position for block in blocks for position in block)

    # Depth is relative to the shallowest thing copied: a layer taken out of a
    # group pastes at the level it is dropped into, not at depth 2 in a stack
    # that has no group to hold it.
    base_depth = min(document.layers[position].depth for position in ordered)

    global _layers, _source
    _layers = []
    for position in ordered:
        layer = document.layers[position]
        record = _read_group(layer)
        record['depth'] = ('value', layer.depth - base_depth)
        _layers.append(record)

    _source = document.id_data.name if document.id_data else ""
    return len(_layers)


def has_content():
    return bool(_layers)


def count():
    return len(_layers)


def source_name():
    return _source


def clear():
    global _layers, _source
    _layers = []
    _source = ""


def paste_layers(document, unique_name=None):
    """Paste the clipboard above the active layer. Returns the new layers.

    Everything lands at the active layer's depth, so pasting while a layer
    inside a group is selected pastes into that group -- which is where the
    click that selected it was pointing.
    """
    if document is None or not _layers:
        return []

    images = _ImageCopies()

    at = min(max(document.active_index, 0), max(0, len(document.layers) - 1))
    depth = document.layers[at].depth if len(document.layers) else 0

    made = []
    for offset, record in enumerate(_layers):
        layer = document.layers.add()
        _write_group(layer, record, images)
        layer.depth = depth + record['depth'][1]
        if unique_name is not None:
            layer.name = unique_name(document, layer.name)
        document.layers.move(len(document.layers) - 1, at + offset)
        made.append(at + offset)

    document.active_index = at
    document.normalize()
    document.select_only(at)
    return [document.layers[i] for i in made if i < len(document.layers)]

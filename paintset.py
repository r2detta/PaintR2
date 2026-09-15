# SPDX-License-Identifier: GPL-3.0-or-later
"""The paint set: which models this session is painting, and their high-polys.

Two collections, made by "Set Models" and named without ceremony:

    Paint        the models being painted
    Paint_high   the high-polys they bake detail from

Everything the window shows comes from the first one. The object list, the
material list, which objects stay visible while the window is open, what an
export walks -- all of it is "what is in `Paint`", which is why this is a
collection and not a selection: a selection changes under you every time you
click on the model.

High-poly pairing is a **name match inside the high collection**, and it is
deliberately strict:

    Paint / suzanne          <->  Paint_high / suzanne_high

* **The name matches.** `suzanne` looks for `suzanne_high` and for nothing
  else. No fuzzy matching, no "closest name", no case folding.

  One asymmetry, and it is deliberate: the *low* side may carry the naming
  convention it was exported with. `suzanne_low`, `suzanne_lowpoly`,
  `suzanne_lp` and Blender's own `suzanne_low.001` all look for
  `suzanne_high`, because the suffix is a label on the low-poly and not part
  of its identity. The high side stays exact -- that is where a wrong guess
  costs an hour.

Sub-collections inside either one are searched, so grouping inside
`Paint_high` is allowed.

This replaced the old `Bake_low` / `Bake2_low` / ... indexed pairs.
The index existed so several unrelated bake sets could sit in one file; the
paint set is the session, and a session has one. What the index bought was a
second collection convention to learn beside this one.

Nothing here bakes and nothing here touches a material. It answers "who is in
the set" and "which high-poly, if any".
"""

import re

import bpy


PAINT = "Paint"
HIGH = "Paint_high"

# Suffixes a low-poly is allowed to carry and still be looked up by its base
# name. Longest first, so `_lowpoly` is not read as `_low` with `poly` left
# over. Matched without regard to case; what is built from the base is not.
LOW_SUFFIXES = ("_lowpoly", "_low_poly", "_low", "_lp")

# Blender's duplicate marker: `suzanne_low.001`. Stripped as well, so a
# duplicated low-poly still finds the high-poly it was duplicated from.
_DUPLICATE = re.compile(r"\.\d{3}$")


# --- the two collections ------------------------------------------------------


def paint_collection():
    """The `Paint` collection, or None when the set has not been made yet."""
    return bpy.data.collections.get(PAINT)


def high_collection():
    """The `Paint_high` collection, or None."""
    return bpy.data.collections.get(HIGH)


def _ensure(scene, name):
    """Get or make a collection linked under the scene's root collection."""
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)

    if not _linked_anywhere(scene, collection):
        scene.collection.children.link(collection)
    return collection


def _linked_anywhere(scene, collection):
    """True when `collection` is already somewhere under this scene.

    Not only directly under the root: someone who dragged `Paint` inside
    another collection has said where they want it, and linking a second copy
    under the root would give them two.
    """
    for candidate in descendants(scene.collection):
        if collection.name in candidate.children:
            return True
    return False


def ensure_pair(context=None):
    """Make `Paint` and `Paint_high` if they are not there. Returns both.

    `Paint_high` is made alongside `Paint` rather than on demand: it is where
    the high-polys go, and an empty collection sitting in the outliner is the
    only thing that says so.
    """
    context = context or bpy.context
    scene = context.scene
    return _ensure(scene, PAINT), _ensure(scene, HIGH)


def move_into(objects, collection):
    """Unlink each object from everywhere it is, and link it here.

    A move, not a copy: an object linked into two collections would be found
    twice, and "which collection is this in" would stop having an answer --
    which is the whole basis of the pairing.
    """
    moved = []
    for obj in objects:
        if obj is None or collection is None:
            continue
        if obj.name in collection.objects:
            continue
        for parent in list(obj.users_collection):
            parent.objects.unlink(obj)
        collection.objects.link(obj)
        moved.append(obj)
    return moved


def move_out(objects, context=None):
    """Take objects out of the paint set, back under the scene root.

    The way out of a paint session. Nothing about the material is undone --
    the stack, its images and the compiled shader all stay -- so this is
    "stop showing me this model", not "throw the painting away".
    """
    context = context or bpy.context
    root = context.scene.collection

    moved = []
    for obj in objects:
        if obj is None or not in_set(obj):
            continue
        for parent in list(obj.users_collection):
            parent.objects.unlink(obj)
        root.objects.link(obj)
        moved.append(obj)
    return moved


# --- reading the set ----------------------------------------------------------


def descendants(collection):
    """`collection` and every collection nested under it."""
    seen = []
    stack = [collection]
    while stack:
        current = stack.pop()
        if current is None or current in seen:
            continue
        seen.append(current)
        stack.extend(current.children)
    return seen


def objects_in(collection):
    """Every object under `collection`, sub-collections included."""
    if collection is None:
        return []
    out = []
    for child in descendants(collection):
        out.extend(child.objects)
    return out


def _meshes(collection):
    """Mesh objects under `collection`, in a stable order.

    Sorted by name rather than left in collection order: the object list in
    the window is read down, and a list whose order depends on the order
    things were linked is a list that reorders itself when a model is taken
    out and put back.
    """
    seen = {}
    for obj in objects_in(collection):
        if obj.type == 'MESH':
            seen[obj.name] = obj
    return [seen[name] for name in sorted(seen)]


def paint_objects():
    """The models being painted. This is the set the whole window works on."""
    return _meshes(paint_collection())


def high_objects():
    """The high-polys."""
    return _meshes(high_collection())


def _under(obj, collection):
    if collection is None:
        return False
    names = {child.name for child in descendants(collection)}
    return any(parent.name in names for parent in obj.users_collection)


def in_set(obj):
    """True when `obj` is one of the models being painted."""
    return obj is not None and _under(obj, paint_collection())


def is_high(obj):
    """True when `obj` is a high-poly rather than a model being painted."""
    return obj is not None and _under(obj, high_collection())


def in_session(obj):
    """True for anything the paint session owns: a model or its high-poly."""
    return in_set(obj) or is_high(obj)


# --- pairing ------------------------------------------------------------------


def base_names(name):
    """Every name `name` may be looked up under, most exact first.

    The object's own name is always first, so a low-poly literally called
    `crate` still finds `crate_high` and nothing has changed for anyone who
    was not using a suffix. After that come the conventional low-poly
    suffixes and Blender's duplicate marker, stripped in both orders.

    Duplicates are removed but the order is kept: the first match wins, and
    "first" has to mean "closest to what was typed".
    """
    name = name or ""
    candidates = [name]

    plain = _DUPLICATE.sub("", name)
    if plain != name:
        candidates.append(plain)

    for candidate in list(candidates):
        lowered = candidate.lower()
        for tail in LOW_SUFFIXES:
            if lowered.endswith(tail) and len(candidate) > len(tail):
                candidates.append(candidate[:-len(tail)])
                break

    seen = set()
    out = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


class Partner:
    """The high-poly found for a low-poly, or the reason there is none."""

    __slots__ = ("object", "reason")

    def __init__(self, obj=None, reason=""):
        self.object = obj
        self.reason = reason

    def __bool__(self):
        return self.object is not None


def find_high(obj, suffix="_high"):
    """The high-poly `obj` bakes from, as a Partner."""
    if obj is None:
        return Partner(reason="no object")

    if is_high(obj):
        return Partner(reason=f"{obj.name} is in {HIGH}, not {PAINT}")

    if not in_set(obj):
        return Partner(reason=f"{obj.name} is not in the {PAINT} collection")

    collection = high_collection()
    if collection is None:
        return Partner(reason=f"{HIGH} does not exist")

    meshes = {candidate.name: candidate for candidate in _meshes(collection)}

    wanted = [base + suffix for base in base_names(obj.name)]
    for name in wanted:
        found = meshes.get(name)
        if found is not None:
            return Partner(found)

    tried = wanted[0] if len(wanted) == 1 else f"{wanted[0]} (or {len(wanted) - 1} more)"
    return Partner(reason=f"no mesh named {tried} in {HIGH}")


def status(obj, suffix="_high"):
    """One line for the sidebar: where this object stands. Never raises."""
    if obj is None or obj.type != 'MESH':
        return "", ""

    if is_high(obj):
        return HIGH, "This is a high-poly"

    if not in_set(obj):
        return "", f"Not in the {PAINT} collection"

    partner = find_high(obj, suffix)
    if partner:
        return PAINT, f"-> {partner.object.name}"
    return PAINT, partner.reason

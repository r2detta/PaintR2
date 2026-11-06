# SPDX-License-Identifier: GPL-3.0-or-later
"""Pixel edits made from Python, as steps on Blender's own undo stack.

A brush stroke goes through Blender's image undo. A fill, a speck clean-up or
a mask invert writes `image.pixels` from Python, and Blender's undo knows
nothing of that write. Keeping those edits on a ring of our own gave
two undo stacks with nobody keeping their order: Ctrl+Z took the fills back
first whatever came after them. It also left Blender's image undo holding a
copy of the image from *before* the fill, which the next brush undo restored
over the whole image -- the fill vanished with the stroke after it.

So every such edit becomes a step of Blender's own, made in three moves right
after the pixels are written:

1. **`scene.paintr2.undo_serial` goes up.** The scene is part of Blender's
   memfile undo, so stepping back past this edit brings the old number back.
   That is how the undo handler below knows which edits were taken back.
2. **An ID is made and thrown away.** That is what tells Blender the file
   changed. Before it pushes an image step it then writes a memfile step of
   its own, one it hides from the user, and that step is what holds the new
   serial (WITH_GLOBAL_UNDO_ENSURE_UPDATED in `undo_system.cc`).
3. **`image.resize` to the size the image already is.** It changes no pixel
   (`IMB_scale` returns early) but it pushes an image undo step whose *after*
   is the image as it is now. The next brush stroke starts from that, so its
   undo no longer wipes the fill.

What that step holds as *before* is Blender's last record of the image, and
that is right whenever Blender has one: when the step before it touched the
same image and tile. When it did not, Blender takes the image as it is now
for *before* as well, and undoing it would change nothing. For that case the
ring keeps the rectangles as they were before the edit, and `on_undo` puts
them back.

`on_undo` only writes when the pixels are still the edit's own -- a hash of
them, taken after the write. Where Blender did restore them, or went back
further than this edit in one jump (the undo history), what is there is
already right and is left alone.

Redo is Blender's: the step's *after* is exact. `on_redo` only marks steps as
done again.

Measured in a real window with simulated keys: plain image with and without a
stroke before the fill, and a UDIM fill on a tile the stroke before it did not
touch. Undo and redo come back through every state in order.

In the background there is no undo stack, so nothing is pushed there;
`undo()` and `redo()` do directly what the handlers do, for the tests.
"""

import ctypes
import hashlib

import bpy
import numpy as np
from bpy.app.handlers import persistent


# At least as many as Blender keeps by default; `_limit` reads the preference.
MAX_STEPS = 32
# What the before-rectangles may hold between them. Past it the oldest go,
# which only costs the case where Blender has no record of its own.
MAX_BYTES = 512 * 1024 * 1024


class Step:
    """One edit: where it was made, and its rectangles from before.

    `parts` is a list of (tile, bbox, pixels, digest); `tile` is None on a plain
    image, `digest` hashes the rectangle as the edit left it. `after` is only
    filled in by an undo, for `redo()` where Blender is not there to do it.
    """

    __slots__ = ("serial", "scene_name", "image_name", "parts", "applied", "after")

    def __init__(self, serial, scene_name, image_name, parts):
        self.serial = serial
        self.scene_name = scene_name
        self.image_name = image_name
        self.parts = parts
        self.applied = True
        self.after = None

    @property
    def nbytes(self):
        return sum(part[2].nbytes for part in self.parts)


_steps = []
_serial = 0


def clear():
    """Forget everything. A new file has a new undo stack."""
    _steps.clear()


def depth():
    """(steps that are done, steps that were taken back). For the tests."""
    done = sum(1 for step in _steps if step.applied)
    return done, len(_steps) - done


def _limit():
    try:
        steps = bpy.context.preferences.edit.undo_steps
    except AttributeError:
        steps = 0
    return max(MAX_STEPS, int(steps))


def _trim():
    while len(_steps) > _limit():
        _steps.pop(0)

    total = sum(step.nbytes for step in _steps)
    while len(_steps) > 1 and total > MAX_BYTES:
        total -= _steps[0].nbytes
        _steps.pop(0)


def _digest(pixels):
    return hashlib.blake2b(np.ascontiguousarray(pixels).tobytes(), digest_size=16).digest()


def _read_box(image, tile, bbox):
    from . import udim

    x0, y0, x1, y1 = bbox
    return np.array(udim.read(image, tile)[y0:y1, x0:x1], copy=True)


def _scene_serial(name):
    scene = bpy.data.scenes.get(name) if name else None
    props = getattr(scene, "paintr2", None)
    return None if props is None else int(props.undo_serial)


# --- the guard: painted pixels survive a memfile undo ------------------------
#
# Blender 5.2.3, plain, no add-on: paint on a new image, add an object, Ctrl+Z
# -- the stroke is gone, although the stroke's step is still the one that is
# done. Measured with simulated keys, every order of strokes and memfile steps.
#
# Why: a stroke tags its image with flags 0, which includes ID_RECALC_SOURCE.
# Going back to a memfile written while the image's tags were different reads
# the image again, and `blo_cache_storage_entry_restore_in_new` (readfile.cc)
# throws away the pixel cache of any ID read with ID_RECALC_SOURCE -- a
# work-around there for images whose file changed. For a painted image it
# throws away the painting.
#
# So the bit is cleared from PaintR2's images before a memfile is written:
# from `recalc`, `recalc_up_to_undo_push` and `recalc_after_undo_push`, the
# three uints before `session_uid` in `ID` (DNA_ID.h). The layout is checked
# against `session_uid` on every image before anything is written; where it
# does not match, nothing is touched. Clearing it costs nothing a painted
# image needs: the source of a painted image did not change.

_RECALC_SOURCE = 1 << 23
_SESSION_UID_OFFSET = 324
_RECALC_OFFSETS = (312, 316, 320)
_layout_reported = False


def _guard_image(image):
    global _layout_reported

    try:
        pointer = image.as_pointer()
        uid = image.session_uid
    except (ReferenceError, AttributeError):
        return
    if not pointer or ctypes.c_uint32.from_address(pointer + _SESSION_UID_OFFSET).value != uid:
        if not _layout_reported:
            _layout_reported = True
            print("[PaintR2] this Blender's ID layout is not the one the undo guard "
                  "knows; painted layers may lose strokes on Ctrl+Z")
        return
    for offset in _RECALC_OFFSETS:
        cell = ctypes.c_uint32.from_address(pointer + offset)
        if cell.value & _RECALC_SOURCE:
            cell.value &= ~_RECALC_SOURCE


def guard_images(images=None):
    """Clear ID_RECALC_SOURCE from `images`, or every image that could lose
    paint: PaintR2's own, and any other with unsaved pixels."""
    if images is None:
        from . import storage

        seen = {image.name: image for image in storage.layer_images()}
        for image in bpy.data.images:
            if image.name not in seen and image.is_dirty:
                seen[image.name] = image
        images = seen.values()
    for image in images:
        if image is not None:
            _guard_image(image)


def push_memfile(message):
    """`ed.undo_push`, with the guard run first. What the window uses for the
    steps it makes itself."""
    guard_images()
    try:
        bpy.ops.ed.undo_push(message=message)
    except RuntimeError as error:
        print(f"[PaintR2] could not push an undo step for {message}: {error}")


# --- making a step ------------------------------------------------------------


def capture(image, tiles=None):
    """Every tile of `image` (or `tiles`) as it is now, as parts for `record`.

    For the edits that rewrite a whole tile -- a mask invert, a bake into the
    mask -- where there is no smaller rectangle to keep.
    """
    from . import udim

    numbers = udim.tiles_of(image) if tiles is None else list(tiles)
    parts = []
    for number in numbers:
        size = udim.tile_size(image, number)
        if size is None:
            continue
        parts.append((number, (0, 0, size[0], size[1]), udim.read(image, number)))
    return parts


def push(image, bbox, pixels, tile=None):
    """`record` for one rectangle."""
    record(image, [(tile, bbox, pixels)])


def push_parts(image, parts):
    """The name the fill tools have always called this by."""
    record(image, parts)


def record(image, parts):
    """Make the pixels just written into `image` one step of Blender's undo.

    `parts` is a list of (tile, bbox, pixels) with each rectangle as it was
    *before* the edit; the image must already hold the edit itself.
    """
    global _serial

    if image is None:
        return
    kept = []
    for tile, bbox, pixels in parts:
        if pixels is None or not pixels.size:
            continue
        bbox = tuple(int(v) for v in bbox)
        kept.append((tile, bbox, np.array(pixels, copy=True),
                     _digest(_read_box(image, tile, bbox))))
    if not kept:
        return

    # A new edit ends every future the redo side had, the same way it does on
    # Blender's stack, which this one is about to drop too.
    _steps[:] = [step for step in _steps if step.applied]

    scene = bpy.context.scene
    props = getattr(scene, "paintr2", None)
    previous = None if props is None else int(props.undo_serial)
    _serial = max(_serial, previous or 0) + 1

    if props is not None and not bpy.app.background:
        props.undo_serial = _serial
        if not _push(image, [part[0] for part in kept]):
            # No step: the number must not change, or undoing some other step
            # later would read as undoing this one.
            props.undo_serial = previous
            return

    _steps.append(Step(_serial, scene.name if scene else "", image.name, kept))
    _trim()


def _touch_main():
    """Tell Blender the file changed, so its next image step is preceded by
    a hidden memfile step holding the serial. Only adding, removing or
    renaming an ID does that; changing a property does not."""
    token = bpy.data.texts.new(".PaintR2 undo")
    bpy.data.texts.remove(token)


def _push(image, tiles):
    """One image undo step of `image` as it is now. True when it was pushed."""
    from . import udim

    _touch_main()
    # The hidden memfile step is written inside the resize below.
    guard_images()

    tiled = image.source == 'TILED'
    numbers = sorted({tile for tile in tiles if tile is not None})
    previous_active = image.tiles.active_index if tiled else 0

    try:
        if not tiled:
            return _resize(image, tuple(image.size), False)

        sizes = {tuple(tile.size) for tile in image.tiles}
        if len(numbers) == 1 or len(sizes) != 1:
            # One tile: a step of that tile only. All of them is a full copy
            # of every tile per edit. Mixed sizes cannot share one resize.
            pushed = False
            for number in numbers:
                index = [t.number for t in image.tiles].index(number)
                image.tiles.active_index = index
                pushed |= _resize(image, tuple(udim.tile_size(image, number)), False)
            return pushed
        return _resize(image, sizes.pop(), True)
    finally:
        if tiled:
            image.tiles.active_index = previous_active


def _resize(image, size, all_udims):
    try:
        with bpy.context.temp_override(edit_image=image):
            result = bpy.ops.image.resize(size=size, all_udims=all_udims)
    except RuntimeError as error:
        print(f"[PaintR2] could not make an undo step for {image.name}: {error}")
        return False
    return result == {'FINISHED'}


# --- taking it back -----------------------------------------------------------


def _take_back(step, force=False):
    """Put `step`'s rectangles back where they are still the edit's own.

    True when the step could be checked at all. Returns False when the image
    is gone or no longer fits -- then the step is dropped, never pasted at the
    wrong place.
    """
    from . import udim

    image = bpy.data.images.get(step.image_name)
    if image is None:
        return False

    for tile, bbox, pixels, _digest_after in step.parts:
        size = udim.tile_size(image, tile)
        if size is None:
            return False
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]):
            return False
        if pixels.shape != (y1 - y0, x1 - x0, 4):
            return False

    after = []
    for tile, (x0, y0, x1, y1), pixels, digest_after in step.parts:
        frame = udim.read(image, tile)
        current = frame[y0:y1, x0:x1]
        after.append((tile, (x0, y0, x1, y1), np.array(current, copy=True)))
        if not force and _digest(current) != digest_after:
            continue  # Blender put it back already, or went back further
        frame[y0:y1, x0:x1] = pixels
        udim.write(image, tile, frame)

    step.after = after
    _guard_image(image)
    return True


def _put_forward(step):
    """`redo()` without Blender: write what the undo found there."""
    from . import udim

    image = bpy.data.images.get(step.image_name)
    if image is None or not step.after:
        return False
    for tile, (x0, y0, x1, y1), pixels in step.after:
        frame = udim.read(image, tile)
        frame[y0:y1, x0:x1] = pixels
        udim.write(image, tile, frame)
    return True


@persistent
def on_undo(*_args):
    """Blender just went back: take back the edits whose serial it undid."""
    for step in reversed(list(_steps)):
        serial = _scene_serial(step.scene_name)
        if step.applied and serial is not None and step.serial > serial:
            step.applied = False
            if not _take_back(step):
                _steps.remove(step)


@persistent
def on_redo(*_args):
    """Blender just went forward. Its step put the pixels back itself."""
    for step in _steps:
        serial = _scene_serial(step.scene_name)
        if not step.applied and serial is not None and step.serial <= serial:
            step.applied = True
            step.after = None


def undo():
    """The newest edit back, as `on_undo` would. For where there is no stack."""
    for step in reversed(list(_steps)):
        if step.applied:
            step.applied = False
            if _take_back(step, force=True):
                return True
            _steps.remove(step)
            return False
    return False


def redo():
    """The oldest taken-back edit forward again, as Blender's redo would."""
    for step in list(_steps):
        if not step.applied:
            if _put_forward(step):
                step.applied = True
                step.after = None
                return True
            _steps.remove(step)
            return False
    return False


_HANDLERS = (
    (bpy.app.handlers.undo_post, on_undo),
    (bpy.app.handlers.redo_post, on_redo),
)


def register():
    for handler_list, function in _HANDLERS:
        if function not in handler_list:
            handler_list.append(function)


def unregister():
    for handler_list, function in _HANDLERS:
        while function in handler_list:
            handler_list.remove(function)
    clear()

# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo for the fill tools.

Blender's own undo does not carry these edits. A brush stroke goes through
Blender's image paint undo and is safe; a fill writes `image.pixels` directly
from Python, and nothing in a memfile undo step contains those bytes -- so
`ed.undo_push` after a fill produced a step that restored everything about the
file except the thing that had just changed.

So the fill tools keep their own ring, here. Two rules make it affordable:

* **A step is a rectangle, not an image.** A polygon fill touches a fraction
  of a 4K texture; storing the whole thing per click would cost 268 MB a step.
  The rectangle is the union of what the stroke actually covered.
* **The ring is capped in bytes as well as in steps.** A stroke that covers
  a whole 4K texture is allowed, but it pushes the older steps out rather
  than sitting alongside thirty more of its own size.

Redo is the same mechanism run backwards: undoing captures what it is about
to overwrite, which is exactly the redo step.

The ring is per-session: it is cleared when the PaintR2 window closes, because
its steps refer to pixels that anything else in Blender may have changed in
the meantime and there is no way to notice that from here.
"""

import numpy as np


# How many steps, and how much memory the lot of them may hold. 256 MB is a
# single 4K RGBA float snapshot and change; past that the oldest steps go.
MAX_STEPS = 32
MAX_BYTES = 256 * 1024 * 1024


class Step:
    """One undoable edit: a rectangle of an image, as it was before."""

    __slots__ = ("image_name", "bbox", "pixels")

    def __init__(self, image_name, bbox, pixels):
        self.image_name = image_name
        self.bbox = bbox
        self.pixels = pixels

    @property
    def nbytes(self):
        return self.pixels.nbytes


_undo = []
_redo = []


def clear():
    """Forget everything. Called when the window closes."""
    _undo.clear()
    _redo.clear()


def depth():
    """(undo steps, redo steps). For tests and for the interface."""
    return len(_undo), len(_redo)


def _trim(ring):
    while len(ring) > MAX_STEPS:
        ring.pop(0)

    total = sum(step.nbytes for step in ring)
    while len(ring) > 1 and total > MAX_BYTES:
        total -= ring[0].nbytes
        ring.pop(0)


def push(image, bbox, pixels):
    """Record `pixels` as the contents of `bbox` before the edit.

    `pixels` is copied. A new edit invalidates the redo ring, the same way it
    does in every editor: there is no longer one future to go back to.
    """
    if image is None or pixels is None or pixels.size == 0:
        return

    _undo.append(Step(image.name, tuple(bbox), np.array(pixels, copy=True)))
    _redo.clear()
    _trim(_undo)


def _apply(step, into):
    """Put a step back, recording what it replaced in `into`. True on success.

    Returns False when the image is gone or has been resized under us -- a
    step that no longer fits is dropped rather than pasted at the wrong place.
    """
    import bpy

    image = bpy.data.images.get(step.image_name)
    if image is None:
        return False

    width, height = image.size
    x0, y0, x1, y1 = step.bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return False
    if step.pixels.shape != (y1 - y0, x1 - x0, 4):
        return False

    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    frame = buffer.reshape(height, width, 4)

    into.append(Step(step.image_name, step.bbox,
                     np.array(frame[y0:y1, x0:x1], copy=True)))
    _trim(into)

    frame[y0:y1, x0:x1] = step.pixels
    image.pixels.foreach_set(frame.reshape(-1))
    image.update()
    return True


def undo():
    """Undo the last fill. True when something was undone."""
    while _undo:
        step = _undo.pop()
        if _apply(step, _redo):
            return True
    return False


def redo():
    """Redo the last undone fill. True when something was redone."""
    while _redo:
        step = _redo.pop()
        if _apply(step, _undo):
            return True
    return False

# SPDX-License-Identifier: GPL-3.0-or-later
"""Putting back the texels Blender's occlusion test drops.

**Measured, in the user's own file.** A single stroke on a smooth
1536-face cube at 2048 left 34 texels unpainted inside a solid painted area.
Every one of them sat on a diagonal `x - y = k` with `k` a multiple of 32 --
the triangulation diagonal of the mesh's quads, in texels. Turning
`use_occlude` off took the count to 0 and changed the painted total by 35
texels, which is to say occlusion did nothing else at all on that model.

So these are not a trade-off Blender is making. They are texels on the visible
surface that the per-texel occlusion raycast rejects because the point it
starts from lies exactly on the edge shared by two triangles, and the ray
finds the neighbouring triangle in front of it. The pattern the user sees --
"patternli bir nokta kümesi" -- is that diagonal, repeated across the mesh.

Blender does not expose the bias that would fix it, and `seam_bleed` cannot:
it widens faces at UV island borders, and these are interior edges. Measured:
bleed 2, 8 and 16 all leave the same 34.

What is left is to put them back, which is safe because of what they are: a
texel that disagrees with four neighbours that agree with each other. On the
same file this found 668 of them across a whole painting session, **all 668**
on a 32-multiple diagonal and **all 668** dark specks on light ground -- not
one legitimate mark among them.

Cost, measured on 2048x2048: 68 ms all in, of which 40 ms is the comparison
and the rest is reading and writing the image.

**This runs when asked, and never on its own.** The first version ran it after
every stroke, from the modal's heartbeat -- which is a direct `image.pixels`
write sitting immediately after Blender's own image-paint undo step, and the
cost of that is known: a memfile undo step does not contain those
bytes, so the undo stack and the image stop agreeing about what happened. The
user's Ctrl+Z went to the beginning of the session.

So it goes through `history.py`, the ring the fill tools keep for exactly this
reason. One press of the button is one step of PaintR2's own undo.
"""

import numpy


# How close four neighbours have to be before they count as agreeing. Two
# texels of an 8-bit channel: below that they are the same colour that has
# been dithered, not an edge.
AGREEMENT = 0.02

# How far the middle one has to be from them to count as wrong. A dropped
# texel is the whole difference between painted and unpainted, so this is
# nowhere near it -- the number is here to keep a soft gradient's own
# quantisation out.
DISAGREEMENT = 0.25


def clean(image, agreement=AGREEMENT, disagreement=DISAGREEMENT, record=True):
    """Fill the isolated texels in `image`. Returns how many there were.

    A texel is filled only when all four of its neighbours agree with each
    other and it does not agree with any of them -- so an edge, a gradient and
    a deliberately thin mark are all left alone. What is left over is a speck,
    and it is replaced by what its neighbours say it should have been.

    `record` puts the image back on PaintR2's own undo ring before writing.
    Blender's undo cannot carry a direct pixel write, so this is the
    only Ctrl+Z that will bring the specks back -- and the specks are why
    somebody might want to.

    Reads and writes the whole image in one pass each; `image.pixels` is a
    round trip through Blender either way and slicing it would be slower, not
    faster.
    """
    width, height = image.size
    if width < 3 or height < 3:
        return 0

    pixels = numpy.empty(len(image.pixels), dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
    rgba = pixels.reshape(height, width, 4)

    # One channel decides. A dropped texel is dropped in all of them -- it
    # never took the stroke at all -- and comparing three costs three times
    # the memory to answer the same question.
    channel = rgba[:, :, 0]
    up, down = channel[2:, 1:-1], channel[:-2, 1:-1]
    left, right = channel[1:-1, :-2], channel[1:-1, 2:]
    middle = channel[1:-1, 1:-1]

    low = numpy.minimum(numpy.minimum(up, down), numpy.minimum(left, right))
    high = numpy.maximum(numpy.maximum(up, down), numpy.maximum(left, right))
    speck = (high - low < agreement) & (numpy.abs(middle - low) > disagreement)

    rows, columns = numpy.nonzero(speck)
    count = int(rows.size)
    if count == 0:
        return 0

    # Back to the whole image's coordinates: the comparison ran on the inside.
    rows = rows + 1
    columns = columns + 1

    if record:
        # The rectangle the specks sit in, as it was. A step is a rectangle
        # rather than the whole image for the reason history.py gives: a 4K
        # snapshot per step is 268 MB.
        from . import history

        top, bottom = int(rows.min()), int(rows.max()) + 1
        first, last = int(columns.min()), int(columns.max()) + 1
        history.push(image, (first, top, last, bottom),
                     rgba[top:bottom, first:last].copy())

    for index in range(4):
        plane = rgba[:, :, index]
        plane[rows, columns] = 0.25 * (
            plane[rows + 1, columns] + plane[rows - 1, columns]
            + plane[rows, columns + 1] + plane[rows, columns - 1]
        )

    image.pixels.foreach_set(pixels)
    image.update()
    return count


def count(image, agreement=AGREEMENT, disagreement=DISAGREEMENT):
    """How many specks `image` holds, without touching it."""
    width, height = image.size
    if width < 3 or height < 3:
        return 0

    pixels = numpy.empty(len(image.pixels), dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
    channel = pixels.reshape(height, width, 4)[:, :, 0]

    up, down = channel[2:, 1:-1], channel[:-2, 1:-1]
    left, right = channel[1:-1, :-2], channel[1:-1, 2:]
    middle = channel[1:-1, 1:-1]

    low = numpy.minimum(numpy.minimum(up, down), numpy.minimum(left, right))
    high = numpy.maximum(numpy.maximum(up, down), numpy.maximum(left, right))
    return int(
        ((high - low < agreement) & (numpy.abs(middle - low) > disagreement)).sum()
    )

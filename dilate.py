# SPDX-License-Identifier: GPL-3.0-or-later
"""Filling a baked texture out past its UV islands.

A bake writes nothing outside the islands it rendered, and "nothing" is black
with zero alpha. Sampled with any filtering -- bilinear at the edge of an
island, or any mip level below the top -- that black bleeds back *inward*
across every seam, which is the dark line down a seam that makes an otherwise
correct map unusable.

Blender's own bake margin covers the first `margin` pixels and covers them
*correctly*: Adjacent Faces continues each island with the face across the
seam, so those pixels are real values rather than a smear. Then it stops, and
everything past it is black again. This module carries the rest of the way:
every pixel the bake did not write takes the value of the written pixels
nearest it, so the image is filled corner to corner and there is no black left
anywhere to bleed in.

**The margin is still needed and is still the part that has to be right.** The
fill here is a smear -- it is what a texel far from any island should hold,
not a substitute for a correct value next to one.

## Why a pyramid rather than repeated dilation

The obvious fill is to grow the written region by one pixel at a time until
nothing is left. That costs one pass over the whole image per pixel of reach,
and the reach is the distance to the furthest empty corner: four thousand
passes over sixteen million pixels for a 4K map with its islands in a huddle.
Not slow -- unusable.

The pyramid does it in one walk up and one walk down. Halving the image
twelve times gets from 4096 to 1, and at each step only the pixels that were
actually written contribute (they are carried as premultiplied colour and a
coverage weight, so the average is over the written ones alone). Walking back
down, every hole takes the value from the level above it -- which, for a hole
beside an island, is the average of that island's nearest edge texels.

Cost is `O(n)` in pixels regardless of reach, and the walk is a handful of
whole-array numpy operations per level.
"""

import numpy as np


def fill(image, coverage=None, opaque=True):
    """Spread `image`'s written pixels over everything it left empty.

    `coverage` is a boolean mask of the pixels that were actually written,
    flat or (height, width). Without one it is read off the alpha channel,
    which is where a Cycles bake puts it: exactly 1 inside an island and its
    margin, exactly 0 outside.

    Pass one explicitly whenever the caller has already overwritten alpha --
    a map that computes its own pixels knows its coverage, and the alpha it
    wrote is no longer a record of it.

    With `opaque`, alpha comes out 1 everywhere afterwards. That is the point
    of the exercise: a map that is fully filled but still transparent outside
    its islands will be premultiplied back to black by the next thing that
    touches it.

    Returns True if anything was written. False means there was nothing to
    fill, or nothing to fill *from*.
    """
    width, height = image.size
    if width < 1 or height < 1:
        return False

    flat = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(flat)
    pixels = flat.reshape(height, width, 4)

    if coverage is None:
        mask = pixels[:, :, 3] > 0.5
    else:
        mask = np.asarray(coverage, dtype=bool).reshape(height, width)

    if not mask.any():
        # Nothing was written, so there is no value to spread. Filling with a
        # guess would hide the failure; leaving it black leaves it visible.
        return False

    if not mask.all():
        filled = _pyramid(pixels[:, :, :3], mask.astype(np.float32))
        np.copyto(pixels[:, :, :3], filled, where=~mask[:, :, None])

    if opaque:
        if mask.all() and (pixels[:, :, 3] >= 1.0).all():
            return False
        pixels[:, :, 3] = 1.0
    elif mask.all():
        return False

    image.pixels.foreach_set(pixels.reshape(-1))
    image.update()
    return True


def _pyramid(rgb, weight):
    """The nearest written colour for every pixel, as an (h, w, 3) array.

    Built from level 1 up rather than level 0: level 0's own normalisation is
    the input image itself where it was written, and the caller keeps those
    pixels untouched anyway. Not holding a premultiplied copy of the full-size
    image saves a third of the peak memory, which at 4K is a couple of hundred
    megabytes.
    """
    premuls = []
    weights = []

    premul, weight = _halve(rgb * weight[:, :, None], weight)
    while True:
        premuls.append(premul)
        weights.append(weight)
        if weight.shape[0] == 1 and weight.shape[1] == 1:
            break
        premul, weight = _halve(premul, weight)

    # The coarsest level is one pixel and it is written by construction: the
    # caller checked that something was, and every level sums everything below
    # it. So there is no fallback to hand it.
    filled = _normalise(premuls[-1], weights[-1], None)

    for level in range(len(weights) - 2, -1, -1):
        filled = _normalise(
            premuls[level], weights[level], _grow(filled, weights[level].shape)
        )
        premuls[level] = None
        weights[level] = None

    return _grow(filled, rgb.shape[:2])


def _halve(premul, weight):
    """One level up: 2x2 blocks averaged, counting only what was written.

    Both halves are divided by four rather than summed, so the weight stays a
    coverage fraction in 0..1 instead of climbing to `4 ** level`. The ratio
    the normalisation wants is unchanged, and a 4K image's coverage count
    would otherwise reach sixteen million -- the point where float32 stops
    being able to add one to it.
    """
    height, width = weight.shape

    if height % 2:
        premul = np.concatenate(
            (premul, np.zeros((1, width, 3), premul.dtype)), axis=0)
        weight = np.concatenate(
            (weight, np.zeros((1, width), weight.dtype)), axis=0)
        height += 1
    if width % 2:
        premul = np.concatenate(
            (premul, np.zeros((height, 1, 3), premul.dtype)), axis=1)
        weight = np.concatenate(
            (weight, np.zeros((height, 1), weight.dtype)), axis=1)
        width += 1

    coarse_premul = premul.reshape(
        height // 2, 2, width // 2, 2, 3).sum(axis=(1, 3)) * 0.25
    coarse_weight = weight.reshape(
        height // 2, 2, width // 2, 2).sum(axis=(1, 3)) * 0.25
    return coarse_premul, coarse_weight


def _normalise(premul, weight, fallback):
    """Premultiplied colour back to plain colour; holes take `fallback`."""
    known = weight > 0.0
    value = premul / np.where(known, weight, 1.0)[:, :, None]
    if fallback is None:
        return value
    return np.where(known[:, :, None], value, fallback)


def _grow(image, shape):
    """One level down: each pixel repeated 2x2, cropped to `shape`.

    Cropping is what handles an odd size, which `_halve` padded on the way up.
    """
    grown = np.repeat(np.repeat(image, 2, axis=0), 2, axis=1)
    return grown[:shape[0], :shape[1]]


def blank(image):
    """Wipe the image to (0, 0, 0, 0) before a bake writes into it.

    This is the whole reason a caller switches `bake.use_clear` off, and it is
    worth the paragraph because the difference is one line of alpha.

    **Measured on 5.2: Blender's own clear blanks to (0, 0, 0, 1) -- opaque
    black.** A texel the bake never touches therefore comes back claiming
    alpha 1, and there is afterwards no way to tell it from one that was
    written and happens to be black. On a plane whose UVs cover 40% of the
    map, `use_clear` left 4096 opaque texels out of 4096; blanking to zero
    alpha here and leaving `use_clear` off left 779 -- the island and its
    margin, which is the real answer.

    That mask is what `fill` spreads from. With every texel claiming to have
    been written there is nothing to spread and nowhere to spread it.

    Note the narrower case this does *not* change, because it is easy to
    conclude the opposite: a selected-to-active bake writes `(0, 0, 0, 0)`
    itself where the ray found nothing, over the top of whatever the clear
    put there. Measured on a low-poly half covered by its high-poly: 1024
    written texels out of 4096 either way. So `meshmaps._bake_height`'s
    "no ray landed here" test was reading a real mask before this and still
    is. What was never a mask is the region outside the UV islands, which no
    bake writes at all.

    Blanking costs one array write per bake, which against a Cycles bake is
    not a cost at all.
    """
    width, height = image.size
    image.pixels.foreach_set(np.zeros(width * height * 4, dtype=np.float32))

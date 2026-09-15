# SPDX-License-Identifier: GPL-3.0-or-later
"""Vector icons, drawn from primitives, in Blender's own icon language.

Blender's icon atlas is not reachable from a custom GPU interface, and
shipping a bitmap sheet would mean managing scale variants. These are drawn
from strokes and polygons instead: they stay crisp at every UI scale and cost
nothing to add to.

What makes an icon read as Blender's rather than as some other toolkit's is
not the subject, it is the treatment:

* a 16x16 grid, with the mark inset a unit or two from the edge;
* thin, even strokes (about 1.3 units) with round ends, not filled slabs;
* outlines with at most one solid accent -- a pupil, a drip, a mask half;
* solid triangles for direction, with no stem (Blender's TRIA_UP / TRIA_DOWN).

Every icon takes the bottom-left corner and a square size, so a caller can
place them without knowing what is inside.
"""

import functools
import math

from . import draw


# Stroke weight and corner radius, in grid units of the 16x16 box.
STROKE = 1.3
CORNER = 2.0


# --- artwork from a file -----------------------------------------------------
#
# An icon drawn here is a fallback, not the last word. A PNG named after the
# function replaces it, so the set can be redrawn by hand one icon at a time --
# a missing file is not an error, it is just an icon that still uses the code
# below it.
#
# The art is a white silhouette with alpha and the colour is multiplied in at
# draw time, which is what keeps hover, dimmed and highlighted working: the
# signature `icon(x, y, size, colour)` is the same either way, so no call site
# knows or cares which of the two it got.
#
# Loading is not this module's job. `gui/` does not open datablocks -- window.py
# reads the folder and pushes GPUTextures in through `set_textures`, the same
# way it pushes layer thumbnails.

_TEXTURES = {}

# Every icon the interface can draw, filled in by the decorator below. window.py
# reads it to know which filenames are worth looking for.
_NAMES = []


def set_textures(textures):
    """Hand over {name: GPUTexture}. Replaces whatever was there before."""
    global _TEXTURES
    _TEXTURES = textures or {}


def names():
    """Every icon name, which is also every filename that would be read."""
    return tuple(_NAMES)


def has_texture(name):
    return name in _TEXTURES


def _icon(function):
    """Register `function` as an icon, and let a file stand in front of it."""
    name = function.__name__
    _NAMES.append(name)

    @functools.wraps(function)
    def draw_icon(x, y, size, color):
        texture = _TEXTURES.get(name)
        if texture is None:
            function(x, y, size, color)
            return
        draw.image_tinted(x, y, size, size, texture, color)

    draw_icon.vector = function
    return draw_icon


def _grid(x, y, size):
    """A mapper from 16x16 grid coordinates to region pixels."""
    unit = size / 16.0

    def point(gx, gy):
        return (x + gx * unit, y + gy * unit)

    return point, unit


def _stroke(points, color, unit, weight=STROKE, closed=False):
    draw.polyline(points, color, max(1.0, unit * weight), closed=closed)


def _curve(start, control, end, steps=10):
    """Quadratic bezier, sampled. Curves are what keep an icon from looking
    like it was built out of rectangles."""
    points = []
    for step in range(steps + 1):
        t = step / steps
        inverse = 1.0 - t
        points.append(
            (
                inverse * inverse * start[0] + 2 * inverse * t * control[0] + t * t * end[0],
                inverse * inverse * start[1] + 2 * inverse * t * control[1] + t * t * end[1],
            )
        )
    return points


# --- layer creation ----------------------------------------------------------


@_icon
def fill_bucket(x, y, size, color):
    """A paint bucket with a drip -- 'add a fill layer'."""
    point, unit = _grid(x, y, size)

    # Handle, behind the body so the body's outline cuts it off cleanly.
    _stroke(
        _curve(point(5.2, 11.5), point(6.4, 15.2), point(9.6, 12.6)),
        color,
        unit,
        STROKE * 0.9,
    )

    body = [
        point(3.4, 11.5), point(12.0, 11.5), point(10.4, 3.4), point(5.0, 3.4),
    ]
    _stroke(body, color, unit, closed=True)

    # The paint inside: one solid accent, which is what tells the outline what
    # it is holding.
    draw.polygon(
        [point(4.2, 7.4), point(11.2, 7.4), point(10.4, 3.9), point(5.0, 3.9)],
        color,
    )

    # Drip, running off the rim.
    draw.polygon(
        [point(12.6, 7.4), point(14.6, 7.4), point(13.6, 3.6)], color
    )


@_icon
def brush(x, y, size, color):
    """A brush on the diagonal -- 'add a paint layer'."""
    point, unit = _grid(x, y, size)

    # Handle.
    _stroke([point(6.6, 6.6), point(13.0, 13.0)], color, unit, STROKE * 1.5)
    # Ferrule: the band where the bristles are crimped on.
    _stroke([point(5.0, 7.6), point(8.4, 11.0)], color, unit, STROKE * 2.2)
    # Bristles, tapering to the point.
    draw.polygon(
        [point(3.0, 6.0), point(7.2, 4.6), point(9.4, 6.8), point(8.0, 11.0)],
        color,
    )
    draw.polygon(
        [point(2.2, 2.4), point(3.0, 6.0), point(6.6, 6.8)], color
    )


@_icon
def mask_square(x, y, size, color):
    """A half-filled rounded square -- 'mask options'."""
    point, unit = _grid(x, y, size)

    left, bottom = point(3.0, 3.0)
    side = unit * 10.0
    radius = unit * CORNER

    draw.rounded_rect(
        left, bottom, side * 0.5, side, radius, color, corners=draw.CORNER_LEFT
    )
    draw.rounded_rect_outline(
        left, bottom, side, side, radius, color, max(1.0, unit * STROKE)
    )


# --- stack order and removal -------------------------------------------------


@_icon
def arrow_up(x, y, size, color):
    """Blender's TRIA_UP: a solid triangle, no stem."""
    point, _unit = _grid(x, y, size)
    draw.polygon([point(3.5, 5.5), point(12.5, 5.5), point(8.0, 11.5)], color)


@_icon
def arrow_down(x, y, size, color):
    """Blender's TRIA_DOWN."""
    point, _unit = _grid(x, y, size)
    draw.polygon([point(3.5, 10.5), point(12.5, 10.5), point(8.0, 4.5)], color)


@_icon
def chevron_down(x, y, size, color):
    """The small triangle Blender puts on anything that opens a list."""
    point, _unit = _grid(x, y, size)
    draw.polygon([point(4.0, 10.0), point(12.0, 10.0), point(8.0, 5.5)], color)


@_icon
def trash(x, y, size, color):
    """A waste bin -- 'delete'."""
    point, unit = _grid(x, y, size)

    # Lid, and the little handle above it.
    _stroke([point(2.6, 12.2), point(13.4, 12.2)], color, unit)
    _stroke(
        [point(6.2, 13.9), point(9.8, 13.9)], color, unit, STROKE * 0.9
    )
    _stroke([point(6.2, 13.9), point(6.2, 12.2)], color, unit, STROKE * 0.9)
    _stroke([point(9.8, 13.9), point(9.8, 12.2)], color, unit, STROKE * 0.9)

    # Body: tapered, open at the top where the lid already closes it.
    _stroke(
        [point(3.9, 12.2), point(4.7, 2.6), point(11.3, 2.6), point(12.1, 12.2)],
        color,
        unit,
    )

    for gx in (6.6, 9.4):
        _stroke(
            [point(gx, 10.2), point(gx, 4.6)], color, unit, STROKE * 0.8
        )


# --- row controls ------------------------------------------------------------


@_icon
def eye_open(x, y, size, color):
    """Blender's HIDE_OFF: the layer is showing."""
    point, unit = _grid(x, y, size)

    _stroke(_curve(point(2.0, 8.0), point(8.0, 14.6), point(14.0, 8.0)), color, unit)
    _stroke(_curve(point(2.0, 8.0), point(8.0, 1.4), point(14.0, 8.0)), color, unit)
    draw.circle(*point(8.0, 8.0), unit * 2.1, color)


@_icon
def eye_closed(x, y, size, color):
    """Blender's HIDE_ON: the layer is hidden."""
    point, unit = _grid(x, y, size)

    # MEASURED BUG, fixed: `_curve` interpolates whatever it is given, so the
    # three control points have to be mapped through `point` first. Passing
    # the raw grid tuples drew the lid at the region's own origin -- a stray
    # arc in the bottom-left corner of the window, nowhere near the eye.
    lid = ((2.0, 9.8), (8.0, 4.2), (14.0, 9.8))
    _stroke(_curve(*(point(*corner) for corner in lid)), color, unit)

    # Three lashes hanging off the closed lid, splayed the way the lid runs.
    # Rooting them on the curve itself is what keeps them from reading as
    # loose marks next to the eye.
    for t, lean in ((0.22, -1.1), (0.5, 0.0), (0.78, 1.1)):
        inverse = 1.0 - t
        gx = (inverse * inverse * lid[0][0] + 2 * inverse * t * lid[1][0]
              + t * t * lid[2][0])
        gy = (inverse * inverse * lid[0][1] + 2 * inverse * t * lid[1][1]
              + t * t * lid[2][1])
        _stroke(
            [point(gx, gy), point(gx + lean, gy - 2.4)],
            color,
            unit,
            STROKE * 0.85,
        )


@_icon
def mirror(x, y, size, color):
    """Blender's MOD_MIRROR: a shape and its reflection, split by a dashed axis.

    Used for "show the mirror plane in the viewport" -- the same mark Blender
    puts on its Mirror modifier, so it needs no label.
    """
    point, unit = _grid(x, y, size)

    # The axis, dashed, and a pair of arrows leaving it in opposite
    # directions. Two mirrored silhouettes would say the same thing but turn
    # into a blob at sixteen pixels -- this stays readable.
    for low, high in ((1.8, 4.6), (6.2, 9.8), (11.4, 14.2)):
        _stroke([point(8.0, low), point(8.0, high)], color, unit, STROKE * 0.85)

    for direction in (-1.0, 1.0):
        root = 8.0 + direction * 1.8
        tip = 8.0 + direction * 6.4
        _stroke([point(root, 8.0), point(tip - direction * 1.6, 8.0)],
                color, unit, STROKE * 0.85)
        draw.polygon(
            [
                point(tip, 8.0),
                point(tip - direction * 2.4, 10.0),
                point(tip - direction * 2.4, 6.0),
            ],
            color,
        )


@_icon
def plus(x, y, size, color):
    """Blender's ADD: a bare plus, evenly weighted."""
    point, unit = _grid(x, y, size)
    _stroke([point(8.0, 3.5), point(8.0, 12.5)], color, unit, STROKE * 1.2)
    _stroke([point(3.5, 8.0), point(12.5, 8.0)], color, unit, STROKE * 1.2)


@_icon
def cross(x, y, size, color):
    """A bare X. Drawn over a mask chip that has been switched off.

    Heavier than `plus` turned forty-five degrees: this one sits on top of a
    thumbnail rather than on a panel, so it needs to survive whatever is
    underneath it.
    """
    point, unit = _grid(x, y, size)
    _stroke([point(4.0, 4.0), point(12.0, 12.0)], color, unit, STROKE * 1.4)
    _stroke([point(4.0, 12.0), point(12.0, 4.0)], color, unit, STROKE * 1.4)


@_icon
def swap(x, y, size, color):
    """Blender's FILE_REFRESH, as used on 'flip the two brush colours'.

    Two arcs chasing each other, each with a head. Anything simpler -- two
    straight arrows -- reads as "move", not "exchange".
    """
    point, unit = _grid(x, y, size)
    cx, cy = 8.0, 8.0
    span = 4.6

    for direction in (1.0, -1.0):
        arc = []
        for step in range(13):
            angle = math.radians(200.0 + step * 10.0)
            arc.append(
                point(
                    cx + direction * span * math.cos(angle),
                    cy + direction * span * math.sin(angle),
                )
            )
        _stroke(arc, color, unit, STROKE * 0.85)

        # Head at the open end of the arc, pointing along it.
        tip_angle = math.radians(200.0)
        tx = cx + direction * span * math.cos(tip_angle)
        ty = cy + direction * span * math.sin(tip_angle)
        draw.polygon(
            [
                point(tx, ty + direction * 2.2),
                point(tx - 2.0, ty - direction * 0.6),
                point(tx + 2.0, ty - direction * 0.6),
            ],
            color,
        )


@_icon
def pressure(x, y, size, color):
    """Blender's STYLUS_PRESSURE: a nib with a wedge under it.

    The wedge is the point -- it says the value ramps with how hard you press,
    which a plain pen outline does not.
    """
    point, unit = _grid(x, y, size)

    # The nib, on the diagonal, tapering to its tip.
    _stroke([point(6.4, 9.4), point(12.4, 14.0)], color, unit, STROKE * 1.4)
    draw.polygon(
        [point(3.0, 6.6), point(5.2, 5.2), point(7.4, 8.2), point(5.0, 9.8)],
        color,
    )
    draw.polygon(
        [point(3.0, 6.6), point(5.0, 9.8), point(2.2, 10.6)], color
    )

    # The ramp: thin on the left, thick on the right.
    draw.polygon(
        [point(2.4, 2.4), point(13.6, 2.4), point(13.6, 4.4)], color
    )


@_icon
def chevron_right(x, y, size, color):
    """The closed state of a disclosure triangle. Blender's RIGHTARROW."""
    point, unit = _grid(x, y, size)
    draw.polygon(
        [point(5.5, 3.0), point(11.5, 8.0), point(5.5, 13.0)], color
    )


@_icon
def chevron_left(x, y, size, color):
    """The step-down arrow on a number field."""
    point, unit = _grid(x, y, size)
    draw.polygon(
        [point(10.5, 3.0), point(4.5, 8.0), point(10.5, 13.0)], color
    )


@_icon
def check(x, y, size, color):
    """The tick inside a checkbox. Two strokes, the long one twice as long."""
    point, unit = _grid(x, y, size)
    _stroke(
        [point(3.2, 8.4), point(6.6, 4.6), point(12.8, 12.0)],
        color, unit, STROKE * 1.25,
    )


@_icon
def stroke(x, y, size, color):
    """Blender's GP_SELECT_STROKES: a squiggle, for 'randomise per stroke'.

    A drawn line rather than a shape, because what it stands for is the stroke
    itself -- one continuous thing from press to release.
    """
    point, unit = _grid(x, y, size)
    curve = _curve(point(2.5, 5.0), point(6.0, 15.0), point(8.5, 8.0), steps=8)
    curve += _curve(point(8.5, 8.0), point(11.0, 1.0), point(13.5, 10.5), steps=8)[1:]
    draw.polyline(curve, color, max(1.0, unit * STROKE))


@_icon
def wireframe(x, y, size, color):
    """A triangulated patch, strokes only -- 'show the mesh wireframe'.

    Deliberately unfilled: the whole point of the overlay is edges, and it also
    keeps this apart from `uv_island_fill`, whose grid is the nearest thing to
    it on the strip.
    """
    point, unit = _grid(x, y, size)

    corners = [point(2.6, 2.6), point(13.4, 2.6),
               point(13.4, 13.4), point(2.6, 13.4)]
    _stroke(corners, color, unit, closed=True)

    weight = STROKE * 0.85
    # A centre split and a diagonal in each half: four triangles, which reads
    # as a mesh. One diagonal alone reads as a cross through a box.
    _stroke([point(8.0, 2.6), point(8.0, 13.4)], color, unit, weight)
    _stroke([corners[0], point(8.0, 13.4)], color, unit, weight)
    _stroke([point(8.0, 2.6), corners[2]], color, unit, weight)


# --- the tool row ------------------------------------------------------------
#
# Four tools, and the icon has to say which one without a label: a brush, one
# face, a whole mesh, one UV island. `brush` above is the first of them.


@_icon
def polygon_fill(x, y, size, color):
    """One irregular quad, filled -- 'fill the polygon under the pointer'."""
    point, unit = _grid(x, y, size)

    outline = [
        point(2.6, 5.2), point(5.8, 13.2), point(13.4, 11.2), point(12.2, 3.2),
    ]
    _stroke(outline, color, unit, closed=True)

    # The same quad, pulled in by a stroke's width: outline plus one solid
    # accent is the house style, and here the accent is what "fill" means.
    draw.polygon(
        [point(5.0, 6.8), point(6.9, 11.2), point(11.4, 10.0),
         point(10.6, 5.4)],
        color,
    )


@_icon
def mesh_fill(x, y, size, color):
    """A cube on its corner -- 'fill the whole mesh part'.

    Blender's own object icons are drawn this way: the silhouette is a
    hexagon and the three edges meeting at the near corner are what turns it
    from a hexagon into a cube.
    """
    point, unit = _grid(x, y, size)

    corners = []
    for step in range(6):
        angle = math.radians(90.0 - step * 60.0)
        corners.append(point(8.0 + 6.2 * math.cos(angle),
                             8.0 + 6.2 * math.sin(angle)))
    _stroke(corners, color, unit, closed=True)

    centre = point(8.0, 8.0)
    for index in (1, 3, 5):  # upper-right, lower, upper-left
        _stroke([centre, corners[index]], color, unit, STROKE * 0.85)


@_icon
def uv_island_fill(x, y, size, color):
    """A four-cell UV grid with one cell solid -- 'fill the UV island'."""
    point, unit = _grid(x, y, size)

    left, bottom = point(2.6, 2.6)
    side = unit * 10.8
    radius = unit * (CORNER * 0.6)

    # The filled cell is the top-left one: an island picked out of a layout.
    draw.rounded_rect(
        left, bottom + side * 0.5, side * 0.5, side * 0.5, radius, color,
        corners=draw.CORNER_TOP_LEFT,
    )
    draw.rounded_rect_outline(
        left, bottom, side, side, radius, color, max(1.0, unit * STROKE)
    )

    weight = max(1.0, unit * STROKE * 0.85)
    draw.polyline(
        [(left + side * 0.5, bottom), (left + side * 0.5, bottom + side)],
        color, weight,
    )
    draw.polyline(
        [(left, bottom + side * 0.5), (left + side, bottom + side * 0.5)],
        color, weight,
    )


@_icon
def select_box(x, y, size, color):
    """A dashed rectangle -- 'drag a box over the model'.

    Dashed, not solid: a solid rectangle is a shape, a dashed one is a
    selection, and that is the distinction the button has to make against the
    fill tools sitting beside it.
    """
    point, unit = _grid(x, y, size)
    weight = max(1.0, unit * STROKE)

    left, bottom = point(2.4, 3.4)
    right, top = point(13.6, 12.6)

    # Four sides, each broken in the middle. Two segments a side is enough to
    # read as a marching-ants border at 16 pixels and does not turn into a
    # grey smear at 3x.
    for x0, y0, x1, y1 in (
        (left, bottom, right, bottom),
        (left, top, right, top),
        (left, bottom, left, top),
        (right, bottom, right, top),
    ):
        for start, end in ((0.0, 0.38), (0.62, 1.0)):
            _stroke(
                [(x0 + (x1 - x0) * start, y0 + (y1 - y0) * start),
                 (x0 + (x1 - x0) * end, y0 + (y1 - y0) * end)],
                color, unit, STROKE,
            )

    # A corner mark, so the icon says which gesture starts it.
    draw.rect(left - weight * 0.5, bottom - weight * 0.5, weight * 2.4,
              weight * 2.4, color)


@_icon
def select_lasso(x, y, size, color):
    """A loop drawn by hand -- 'draw a shape over the model'."""
    point, unit = _grid(x, y, size)

    # A closed, deliberately uneven loop: a lasso is not a circle, and drawing
    # one would make this the same icon as every other round mark in the set.
    loop = [
        point(4.0, 4.4), point(2.6, 8.2), point(4.6, 11.8), point(8.6, 13.2),
        point(12.4, 12.0), point(13.6, 8.6), point(12.0, 5.2), point(8.4, 3.4),
        point(5.6, 4.0),
    ]
    _stroke(loop, color, unit, STROKE)

    # The tail, where the two ends do not quite meet -- what makes it read as
    # drawn rather than as a shape.
    _stroke([point(5.6, 4.0), point(6.6, 2.2)], color, unit, STROKE)


@_icon
def gradient(x, y, size, color):
    """A square with a ramp banded across it -- a generated, not painted, layer.

    Banded rather than smoothly faded: at 16px a real gradient turns into a
    grey smudge, and a solid half-square reads as the mask icon instead. Bars
    thinning upward from a solid base say "more here, less there", which is
    what the ramp is, and they stay in the house style -- an outline with one
    solid accent.
    """
    point, unit = _grid(x, y, size)

    left, bottom = point(2.4, 2.4)
    side = unit * 11.2
    radius = unit * (CORNER * 0.6)

    # The dense end: a solid band across the bottom third.
    draw.rounded_rect(
        left, bottom, side, side * 0.30, radius, color,
        corners=draw.CORNER_BOTTOM,
    )

    # Three bars above it, each thinner and further apart: the ramp falling off.
    for offset, weight in ((0.44, 0.85), (0.62, 0.6), (0.80, 0.4)):
        thickness = max(1.0, unit * STROKE * weight)
        draw.rect(left, bottom + side * offset, side, thickness, color)

    draw.rounded_rect_outline(
        left, bottom, side, side, radius, color, max(1.0, unit * STROKE)
    )


@_icon
def nodes(x, y, size, color):
    """Two nodes and the wire between them -- a pattern built by hand.

    Deliberately not the gradient icon with something added: a Custom layer is
    not a gradient with extra settings, it is a different thing entirely, and
    the row has to say which of the two it is at a glance. Two boxes and a
    curve is the shape every node editor in existence uses for itself.
    """
    point, unit = _grid(x, y, size)

    box = unit * 4.6
    radius = unit * (CORNER * 0.5)
    weight = max(1.0, unit * STROKE)

    left, bottom = point(1.8, 8.2)
    draw.rounded_rect_outline(left, bottom, box, box * 0.8, radius, color, weight)

    right, top = point(9.6, 3.0)
    draw.rounded_rect_outline(right, top, box, box * 0.8, radius, color, weight)

    # The wire: out of the first box's right edge, into the second one's left.
    _stroke(
        _curve(
            (left + box, bottom + box * 0.4),
            (right, bottom + box * 0.4),
            (right, top + box * 0.4),
        ),
        color, unit,
    )


@_icon
def adjustment(x, y, size, color):
    """A circle half filled -- the contrast mark every editor uses for this.

    Not a slider or a curve: those say "a value", and an adjustment layer is
    not a value, it is a change applied to everything under it. A disc split
    down the middle is the one shape that reads as "before and after".
    """
    point, unit = _grid(x, y, size)

    centre = point(8.0, 8.0)
    radius = unit * 5.6
    weight = max(1.0, unit * STROKE)

    # The filled half, as a fan of triangles from the centre.
    steps = 14
    left = [centre]
    for step in range(steps + 1):
        angle = math.pi * 0.5 + math.pi * step / steps
        left.append((centre[0] + math.cos(angle) * radius,
                     centre[1] + math.sin(angle) * radius))
    draw.polygon(left, color)

    draw.ring(centre[0], centre[1], radius, color, weight)


@_icon
def effects(x, y, size, color):
    """Three stacked bars with a spark -- 'add a filter or a pattern'.

    Stacked, because that is what an effect stack is; the spark is what tells
    it apart from the folder sitting beside it in the same strip.
    """
    point, unit = _grid(x, y, size)
    weight = max(1.0, unit * STROKE)

    # The stack: three bars, the top one shorter, so it reads as a list.
    for row, right in ((4.2, 11.6), (7.2, 12.6), (10.2, 9.4)):
        left_x, bottom = point(2.6, row)
        end_x, _ = point(right, row)
        draw.rect(left_x, bottom, end_x - left_x, weight * 1.6, color)

    # The spark, top right: two crossed strokes, the vertical longer.
    cx, cy = point(12.4, 12.2)
    arm = unit * 2.2
    draw.rect(cx - weight * 0.5, cy - arm, weight, arm * 2.0, color)
    draw.rect(cx - arm, cy - weight * 0.5, arm * 2.0, weight, color)


@_icon
def cube(x, y, size, color):
    """An isometric cube -- the models in the paint set.

    Blender's own object icon is a cube seen from a corner, and the three
    edges meeting at the middle are what make it read as a solid rather than
    a hexagon. Drawn as an outline plus that Y, which is the fewest strokes
    that still say "object" at 16px.
    """
    point, unit = _grid(x, y, size)

    # The silhouette: a hexagon. Wider than tall, the way a cube tipped
    # towards the viewer is.
    outline = [
        point(8.0, 14.2), point(14.0, 10.8), point(14.0, 5.2),
        point(8.0, 1.8), point(2.0, 5.2), point(2.0, 10.8),
    ]
    _stroke(outline, color, unit, closed=True)

    # The three edges that meet at the near corner. Without them it is a
    # hexagon; with them it is a box.
    _stroke([point(2.0, 10.8), point(8.0, 7.4)], color, unit)
    _stroke([point(14.0, 10.8), point(8.0, 7.4)], color, unit)
    _stroke([point(8.0, 1.8), point(8.0, 7.4)], color, unit)


@_icon
def folder(x, y, size, color):
    """A folder -- a group of layers.

    Blender's own collection icon: a box with a tab on its top left. Drawn as
    one outline so it reads at 16px, where a lid and a body as separate shapes
    turn into a grey blob.
    """
    point, unit = _grid(x, y, size)

    body = [
        point(2.2, 3.0), point(13.8, 3.0), point(13.8, 11.6),
        point(2.2, 11.6),
    ]
    _stroke(body, color, unit, closed=True)

    # The tab: a step up on the left of the top edge, which is what says
    # "folder" rather than "rectangle".
    _stroke(
        [point(2.2, 11.6), point(2.2, 13.0), point(6.6, 13.0), point(7.8, 11.6)],
        color, unit,
    )

# SPDX-License-Identifier: GPL-3.0-or-later
"""Low-level GPU drawing primitives for the custom interface.

Blender 5.x constraints that shape this module:

* ``bgl`` is gone -- all GPU state goes through ``gpu.state.*_set()``.
* ``GPUShader(vertexcode=..., fragcode=...)`` is gone. Custom shaders use
  ``GPUShaderCreateInfo``.
* The ``'TRI_FAN'`` batch type was removed for Metal compatibility, so rounded
  shapes are emitted as explicit ``'TRIS'`` index lists.
* ``blf.size(font_id, size)`` takes no DPI argument anymore.

All coordinates are region-local pixels with the origin at the bottom-left,
which is what a ``'POST_PIXEL'`` draw handler gives us.
"""

import math

import blf
import gpu
from gpu_extras.batch import batch_for_shader

from . import theme


_shader = None
_smooth_shader = None
_image_shader = None
_image_color_shader = None
_polyline_shader = None
_linear_gradient_shader = {}

# Unit-circle corner offsets, built once per radius/segment combination.
_arc_cache = {}

FONT_ID = 0

# Which corners of a rounded rectangle are actually rounded. Blender squares
# off the sides where buttons are joined into a strip, and so do we.
CORNER_BOTTOM_RIGHT = 1
CORNER_TOP_RIGHT = 2
CORNER_TOP_LEFT = 4
CORNER_BOTTOM_LEFT = 8
CORNER_ALL = 15
CORNER_NONE = 0
CORNER_LEFT = CORNER_TOP_LEFT | CORNER_BOTTOM_LEFT
CORNER_RIGHT = CORNER_TOP_RIGHT | CORNER_BOTTOM_RIGHT
CORNER_TOP = CORNER_TOP_LEFT | CORNER_TOP_RIGHT
CORNER_BOTTOM = CORNER_BOTTOM_LEFT | CORNER_BOTTOM_RIGHT

# Blender resolves a corner with nine segments regardless of size; matching it
# keeps our arcs from looking coarser than the ones next to them.
MAX_CORNER_SEGMENTS = 9


def shader():
    """Lazily fetch the builtin flat-color shader.

    Fetching at import time would fail in background mode, where there is no
    GPU context.
    """
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _shader


def smooth_shader():
    """Per-vertex colour shader, used for gradients."""
    global _smooth_shader
    if _smooth_shader is None:
        _smooth_shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    return _smooth_shader


def image_shader():
    """Textured-quad shader, used for thumbnails."""
    global _image_shader
    if _image_shader is None:
        _image_shader = gpu.shader.from_builtin('IMAGE')
    return _image_shader


def image_color_shader():
    """Textured quad multiplied by a colour. Used for icons drawn from a file.

    The multiply is what keeps a file-based icon behaving like a drawn one: the
    art is a white silhouette with alpha, and hover, dimmed and highlighted are
    the same file times a different colour.
    """
    global _image_color_shader
    if _image_color_shader is None:
        _image_color_shader = gpu.shader.from_builtin('IMAGE_COLOR')
    return _image_color_shader


def polyline_shader():
    """Anti-aliased line shader.

    The only builtin that gives smooth edges without writing a shader of our
    own -- measured: a diagonal drawn with it lands 60-odd distinct luminances
    on a black field, where 'LINES' gives two. Every rounded corner in the
    interface is softened by stroking its outline with this.
    """
    global _polyline_shader
    if _polyline_shader is None:
        _polyline_shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    return _polyline_shader


def reset_gpu_cache():
    """Drop cached GPU objects. Called on unregister."""
    global _shader, _smooth_shader, _image_shader, _polyline_shader
    global _image_color_shader, _linear_gradient_shader
    _shader = None
    _smooth_shader = None
    _image_shader = None
    _image_color_shader = None
    _polyline_shader = None
    _linear_gradient_shader = {}
    _arc_cache.clear()


# --- State helpers ----------------------------------------------------------


class blend_alpha:
    """Context manager enabling alpha blending for a block of draw calls."""

    def __enter__(self):
        self._previous = gpu.state.blend_get()
        gpu.state.blend_set('ALPHA')
        return self

    def __exit__(self, *exc):
        gpu.state.blend_set(self._previous)
        return False


class clip:
    """Context manager restricting drawing to a rectangle (scissor test).

    Takes region-local coordinates like the rest of this module, but the
    scissor box is a framebuffer rectangle -- so the region's viewport offset
    has to be added, otherwise clipping lands in the wrong place whenever the
    region is not at the window origin.
    """

    def __init__(self, x, y, width, height):
        self._x = int(x)
        self._y = int(y)
        self._width = max(0, int(width))
        self._height = max(0, int(height))

    def __enter__(self):
        self._previous = gpu.state.scissor_get()

        vx, vy, vw, vh = gpu.state.viewport_get()
        x = vx + self._x
        y = vy + self._y
        # Clamp to the viewport; a scissor box reaching outside it is not an
        # error but does make the result driver-dependent.
        x2 = min(x + self._width, vx + vw)
        y2 = min(y + self._height, vy + vh)
        x = max(x, vx)
        y = max(y, vy)

        gpu.state.scissor_test_set(True)
        gpu.state.scissor_set(x, y, max(0, x2 - x), max(0, y2 - y))
        return self

    def __exit__(self, *exc):
        gpu.state.scissor_set(*self._previous)
        gpu.state.scissor_test_set(False)
        return False


# --- Shapes -----------------------------------------------------------------


def _draw_tris(vertices, indices, color):
    sh = shader()
    batch = batch_for_shader(sh, 'TRIS', {"pos": vertices}, indices=indices)
    sh.bind()
    sh.uniform_float("color", color)
    batch.draw(sh)


def read_pixel(x, y):
    """The colour already on screen at region-local `(x, y)`, or None.

    Only meaningful from inside the draw pass -- there is no framebuffer to
    read at any other moment, and asking for one outside it is what makes this
    return None rather than raise.

    The coordinates are region-local, the way every other coordinate in this
    package is, and the framebuffer's are not: it is the whole window. What
    bridges the two is the viewport rectangle the region set before calling
    us, which `gpu.state.viewport_get()` hands back -- so this needs nothing
    from bpy and stays as testable as the rest of the toolkit.

    What comes back is display-referred: it is the pixel as shown, after
    Blender's view transform, not a scene-linear colour. Undoing that is the
    caller's problem, and `colorpicker` is the one caller that has it.
    """
    try:
        view_x, view_y, view_w, view_h = gpu.state.viewport_get()
    except (RuntimeError, SystemError, AttributeError):
        return None

    x, y = int(x), int(y)
    if not (0 <= x < view_w and 0 <= y < view_h):
        return None

    try:
        buffer = gpu.state.active_framebuffer_get().read_color(
            view_x + x, view_y + y, 1, 1, 3, 0, 'FLOAT'
        )
        buffer.dimensions = 3
        return tuple(float(v) for v in buffer)
    except (RuntimeError, SystemError, ValueError):
        return None


def rect(x, y, width, height, color):
    """Filled axis-aligned rectangle."""
    x2, y2 = x + width, y + height
    vertices = ((x, y), (x2, y), (x2, y2), (x, y2))
    _draw_tris(vertices, ((0, 1, 2), (0, 2, 3)), color)


def _corner_arc(radius, segments):
    key = (round(radius, 2), segments)
    points = _arc_cache.get(key)
    if points is None:
        step = (math.pi * 0.5) / segments
        points = [
            (math.cos(step * i) * radius, math.sin(step * i) * radius)
            for i in range(segments + 1)
        ]
        _arc_cache[key] = points
    return points


def _segments_for(radius):
    """Enough segments that a corner reads as a curve, never more."""
    return max(2, min(MAX_CORNER_SEGMENTS, int(radius)))


def round_path(x, y, width, height, radius, corners=CORNER_ALL, segments=None):
    """The outline of a rounded rectangle, counter-clockwise from bottom-right.

    Shared by the fill, the outline stroke and the emboss line so all three
    trace exactly the same curve -- an outline half a pixel off its own fill is
    the tell that a widget was not drawn as one shape.
    """
    radius = max(0.0, min(radius, width * 0.5, height * 0.5))
    x2, y2 = x + width, y + height

    if radius <= 0.5 or corners == CORNER_NONE:
        return [(x2, y), (x2, y2), (x, y2), (x, y)]

    arc = _corner_arc(radius, segments or _segments_for(radius))
    points = []

    if corners & CORNER_BOTTOM_RIGHT:
        cx, cy = x2 - radius, y + radius
        points += [(cx + ox, cy - oy) for ox, oy in reversed(arc)]
    else:
        points.append((x2, y))

    if corners & CORNER_TOP_RIGHT:
        cx, cy = x2 - radius, y2 - radius
        points += [(cx + ox, cy + oy) for ox, oy in arc]
    else:
        points.append((x2, y2))

    if corners & CORNER_TOP_LEFT:
        cx, cy = x + radius, y2 - radius
        points += [(cx - ox, cy + oy) for ox, oy in reversed(arc)]
    else:
        points.append((x, y2))

    if corners & CORNER_BOTTOM_LEFT:
        cx, cy = x + radius, y + radius
        points += [(cx - ox, cy - oy) for ox, oy in arc]
    else:
        points.append((x, y))

    return points


def polyline(points, color, thickness=1.0, closed=False):
    """An anti-aliased polyline. Needs alpha blending, so it sets it itself."""
    coords = list(points)
    if len(coords) < 2:
        return
    if closed:
        coords.append(coords[0])

    _, _, viewport_width, viewport_height = gpu.state.viewport_get()

    sh = polyline_shader()
    batch = batch_for_shader(sh, 'LINE_STRIP', {"pos": coords})
    sh.bind()
    sh.uniform_float("viewportSize", (viewport_width, viewport_height))
    sh.uniform_float("lineWidth", float(thickness))
    sh.uniform_float("color", color)

    with blend_alpha():
        batch.draw(sh)


def polygon(points, color, smooth=True):
    """A filled convex polygon, softened along its edge.

    The softening is a one-pixel stroke of the same colour over the boundary.
    It is skipped for translucent fills, where drawing the edge twice would
    leave a visible rim instead of an anti-aliased one.
    """
    vertices = tuple(points)
    if len(vertices) < 3:
        return

    cx = sum(point[0] for point in vertices) / len(vertices)
    cy = sum(point[1] for point in vertices) / len(vertices)

    fan = ((cx, cy),) + vertices
    indices = [(0, i, i + 1) for i in range(1, len(fan) - 1)]
    indices.append((0, len(fan) - 1, 1))
    _draw_tris(fan, indices, color)

    if smooth and (len(color) < 4 or color[3] > 0.99):
        polyline(vertices, color, 1.0, closed=True)


def rounded_rect(x, y, width, height, radius, color, corners=CORNER_ALL,
                 segments=None):
    """Filled rectangle with rounded, anti-aliased corners."""
    if radius <= 0.5 or corners == CORNER_NONE:
        rect(x, y, width, height, color)
        return
    polygon(round_path(x, y, width, height, radius, corners, segments), color)


def rounded_rect_outline(x, y, width, height, radius, color, thickness=1.0,
                         corners=CORNER_ALL, segments=None):
    """The border of a rounded rectangle, anti-aliased."""
    polyline(
        round_path(x, y, width, height, radius, corners, segments),
        color,
        thickness,
        closed=True,
    )


def _shaded(color, shade):
    """Blender's integer shading: -100..100 added in 0-255 space."""
    if not shade:
        return color
    offset = shade / 255.0
    return tuple(min(1.0, max(0.0, c + offset)) for c in color[:3]) + (color[3],)


def widget(x, y, width, height, radius, inner, outline=None,
           corners=CORNER_ALL, shade_top=0, shade_down=0, emboss=None,
           segments=None):
    """One Blender-style widget face: shaded inner, outline, emboss line.

    This is the shape every button, field and chip is made of. Keeping it in
    one place is what makes them look like a set rather than like a pile of
    rectangles that happen to share a colour.
    """
    points = round_path(x, y, width, height, radius, corners, segments)

    if shade_top or shade_down:
        top = _shaded(inner, shade_top)
        bottom = _shaded(inner, shade_down)
        _fill_shaded(points, y, height, top, bottom)
    else:
        polygon(points, inner)

    if emboss is not None:
        # Below the widget, following its own bottom curve, so it reads as the
        # widget standing slightly proud of the panel rather than as a line.
        lowest = min(point[1] for point in points)
        skirt = _bottom_run(points, lowest + height * 0.5)
        polyline([(px_, py - 1.0) for px_, py in skirt], emboss, 1.0)

    if outline is not None:
        polyline(points, outline, 1.0, closed=True)


def _bottom_run(points, threshold):
    """The stretch of a closed path that lies below `threshold`, in path order.

    A rounded rectangle's path *starts* along its bottom edge, so the bottom
    stretch is split across the seam between the last point and the first.
    Filtering the list would join those two halves with a straight line across
    the middle of the widget -- which is exactly what the emboss line used to
    draw. Walking the path as a cycle keeps the run in one piece.
    """
    count = len(points)
    below = [index for index, point in enumerate(points) if point[1] <= threshold]
    if not below or len(below) == count:
        return list(points)

    members = set(below)
    start = next(index for index in below if (index - 1) % count not in members)

    run = []
    index = start
    while index in members and len(run) < count:
        run.append(points[index])
        index = (index + 1) % count
    return run


def _fill_shaded(points, y, height, top, bottom):
    """Fill a path with a vertical gradient between two colours."""
    cx = sum(point[0] for point in points) / len(points)
    cy = sum(point[1] for point in points) / len(points)

    def colour_at(py):
        ratio = 0.0 if height <= 0 else min(1.0, max(0.0, (py - y) / height))
        return tuple(
            bottom[index] + (top[index] - bottom[index]) * ratio
            for index in range(4)
        )

    fan = [(cx, cy)] + list(points)
    colors = [colour_at(point[1]) for point in fan]
    indices = [(0, i, i + 1) for i in range(1, len(fan) - 1)]
    indices.append((0, len(fan) - 1, 1))

    sh = smooth_shader()
    batch = batch_for_shader(
        sh, 'TRIS', {"pos": fan, "color": colors}, indices=indices
    )
    sh.bind()
    batch.draw(sh)


def shadow(x, y, width, height, radius, strength, spread):
    """The soft drop shadow Blender puts under a menu.

    Drawn as concentric translucent rings rather than a blurred texture: at
    six pixels of spread the difference is not visible and there is no
    framebuffer round trip.
    """
    spread = max(1, int(spread))
    for step in range(spread, 0, -1):
        alpha = strength * (1.0 - (step - 1) / spread) / spread
        rounded_rect(
            x - step,
            y - step * 1.5,
            width + step * 2,
            height + step * 2,
            radius + step,
            (0.0, 0.0, 0.0, alpha),
        )


def circle(cx, cy, radius, color, segments=None):
    """A filled disc. Icons are built from these and polygons."""
    count = max(8, segments or int(radius * 3))
    step = math.tau / count
    polygon(
        [
            (cx + math.cos(step * i) * radius, cy + math.sin(step * i) * radius)
            for i in range(count)
        ],
        color,
    )


def ring(cx, cy, radius, color, thickness=1.0, segments=None):
    """A circle outline."""
    count = max(8, segments or int(radius * 3))
    step = math.tau / count
    polyline(
        [
            (cx + math.cos(step * i) * radius, cy + math.sin(step * i) * radius)
            for i in range(count)
        ],
        color,
        thickness,
        closed=True,
    )


def rect_outline(x, y, width, height, color, thickness=1):
    """Rectangle border drawn as four filled bars (line width is unreliable)."""
    t = max(1, int(thickness))
    rect(x, y, width, t, color)
    rect(x, y + height - t, width, t, color)
    rect(x, y + t, t, height - 2 * t, color)
    rect(x + width - t, y + t, t, height - 2 * t, color)


def line(x1, y1, x2, y2, color, thickness=1):
    """Straight line drawn as a quad, so thickness is honoured on every GPU."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return

    half = max(0.5, thickness * 0.5)
    nx, ny = -dy / length * half, dx / length * half
    vertices = (
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    )
    _draw_tris(vertices, ((0, 1, 2), (0, 2, 3)), color)


def gradient_rect(x, y, width, height, bottom_left, bottom_right, top_right, top_left):
    """Rectangle with a colour at each corner, interpolated between them.

    Interpolation is per triangle; use linear_gradient_rect for a bilinear
    colour field, where a diagonal seam would be visible.
    """
    sh = smooth_shader()
    x2, y2 = x + width, y + height

    batch = batch_for_shader(
        sh,
        'TRIS',
        {
            "pos": ((x, y), (x2, y), (x2, y2), (x, y2)),
            "color": (bottom_left, bottom_right, top_right, top_left),
        },
        indices=((0, 1, 2), (0, 2, 3)),
    )
    sh.bind()
    batch.draw(sh)


def linear_gradient_rect(x, y, width, height, bottom_left, bottom_right, top_right,
                         top_left, encode=True):
    """Bilinear RGBA field.

    With `encode` the corners are linear and every pixel is encoded to sRGB
    for display; without it the corners already are display values and are
    blended as they are -- what the colour picker draws, since it works in
    sRGB.
    """
    sh = _linear_gradient_shader.get(bool(encode))
    if sh is None:
        interface = gpu.types.GPUStageInterfaceInfo('paintr2_gradient_interface')
        interface.smooth('VEC2', 'uv')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'ModelViewProjectionMatrix')
        for name in ('bottomLeft', 'bottomRight', 'topRight', 'topLeft'):
            info.push_constant('VEC4', name)
        info.vertex_in(0, 'VEC2', 'pos')
        info.vertex_in(1, 'VEC2', 'texCoord')
        info.vertex_out(interface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source('''
            void main() {
                uv = texCoord;
                gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
            }
        ''')
        info.fragment_source('''
            void main() {
                vec4 color = mix(mix(bottomLeft, bottomRight, uv.x),
                                 mix(topLeft, topRight, uv.x), uv.y);
                vec3 c = clamp(color.rgb, 0.0, 1.0);
        ''' + ('''
                c = mix(1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055,
                        12.92 * c, lessThanEqual(c, vec3(0.0031308)));
        ''' if encode else "") + '''
                fragColor = vec4(c, color.a);
            }
        ''')
        sh = gpu.shader.create_from_info(info)
        _linear_gradient_shader[bool(encode)] = sh
    batch = batch_for_shader(sh, 'TRIS', {
        'pos': ((x, y), (x + width, y), (x + width, y + height), (x, y + height)),
        'texCoord': ((0, 0), (1, 0), (1, 1), (0, 1)),
    }, indices=((0, 1, 2), (0, 2, 3)))
    sh.bind()
    for name, color in zip(('bottomLeft', 'bottomRight', 'topRight', 'topLeft'),
                           (bottom_left, bottom_right, top_right, top_left)):
        sh.uniform_float(name, color)
    batch.draw(sh)


def vertical_gradient(x, y, width, height, stops):
    """A stack of colour stops blended top to bottom.

    `stops` runs from the TOP down, which is how a hue strip reads on screen.
    """
    if len(stops) < 2:
        return

    sh = smooth_shader()
    bands = len(stops) - 1
    band_height = height / bands

    positions = []
    colors = []
    indices = []

    for index in range(bands):
        top = y + height - index * band_height
        bottom = top - band_height
        upper = stops[index]
        lower = stops[index + 1]

        base = len(positions)
        positions += [(x, bottom), (x + width, bottom), (x + width, top), (x, top)]
        colors += [lower, lower, upper, upper]
        indices += [(base, base + 1, base + 2), (base, base + 2, base + 3)]

    batch = batch_for_shader(
        sh, 'TRIS', {"pos": positions, "color": colors}, indices=indices
    )
    sh.bind()
    batch.draw(sh)


def _image_batch(sh, x, y, width, height):
    """A textured quad, upright.

    Blender normalises every image it loads to bottom-up, and this module's y
    grows upward too, so the texture coordinates run the same way as the
    rectangle and nothing has to be flipped -- for a PNG read off disk exactly
    as for a paint canvas.
    """
    x2, y2 = x + width, y + height
    return batch_for_shader(
        sh,
        'TRIS',
        {
            "pos": ((x, y), (x2, y), (x2, y2), (x, y2)),
            "texCoord": ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        },
        indices=((0, 1, 2), (0, 2, 3)),
    )


def image_rect(x, y, width, height, texture):
    """Draw a GPUTexture into a rectangle. Used for layer thumbnails."""
    sh = image_shader()
    batch = _image_batch(sh, x, y, width, height)
    sh.bind()
    sh.uniform_sampler("image", texture)
    batch.draw(sh)


def image_tinted(x, y, width, height, texture, color):
    """Draw a GPUTexture multiplied by `color`. Used for icons from a file."""
    sh = image_color_shader()
    batch = _image_batch(sh, x, y, width, height)
    sh.bind()
    sh.uniform_sampler("image", texture)
    sh.uniform_float("color", tuple(color))
    batch.draw(sh)


def checkerboard(x, y, width, height, size, light, dark):
    """The classic transparency backdrop, drawn behind thumbnails."""
    rect(x, y, width, height, dark)

    size = max(2, int(size))
    row = 0
    cy = y
    while cy < y + height:
        cx = x + (size if row % 2 else 0)
        cell_h = min(size, y + height - cy)
        while cx < x + width:
            cell_w = min(size, x + width - cx)
            rect(cx, cy, cell_w, cell_h, light)
            cx += size * 2
        cy += size
        row += 1


# --- Text -------------------------------------------------------------------


def text_dimensions(value, size):
    """Return (width, height) of `value` at `size`, in pixels."""
    blf.size(FONT_ID, size)
    return blf.dimensions(FONT_ID, value)


def _apply_shadow():
    """Put Blender's own text shadow under the next glyphs.

    Blender draws every interface string through `UI_fontstyle_draw`, which
    switches the style's shadow on first. Leaving it off is why our labels
    read as thinner than Blender's on the same grey: the shadow is what holds
    a glyph apart from the fill behind it.
    """
    level = int(theme.FONT_SHADOW)
    if level <= 0:
        blf.disable(FONT_ID, blf.SHADOW)
        return

    blf.enable(FONT_ID, blf.SHADOW)
    blf.shadow(FONT_ID, level, *theme.FONT_SHADOW_COLOR)
    blf.shadow_offset(FONT_ID, *theme.FONT_SHADOW_OFFSET)


def text(x, y, value, size, color):
    """Draw `value` with its baseline-left corner at (x, y).

    Positions are snapped to whole pixels. blf will happily rasterise a glyph
    at x.5, and the result is the soft, slightly smeared text that made the
    interface look unlike Blender's next to it -- Blender lays its own
    interface text out in integers for exactly this reason.

    blf manages its own blend state and leaves it switched off, so anything
    drawn with alpha after a label would come out opaque -- measured: a 22%
    black veil over a layer row rendered as solid black once the row above it
    had drawn its name. Restoring the state here keeps that trap in one place
    rather than in every caller.
    """
    previous = gpu.state.blend_get()

    blf.size(FONT_ID, size)
    _apply_shadow()
    blf.color(FONT_ID, *color)
    blf.position(FONT_ID, int(round(x)), int(round(y)), 0.0)
    blf.draw(FONT_ID, value)
    blf.disable(FONT_ID, blf.SHADOW)

    gpu.state.blend_set(previous)


def text_centered(cx, cy, value, size, color):
    """Draw `value` centered on (cx, cy)."""
    width, height = text_dimensions(value, size)
    text(cx - width * 0.5, cy - height * 0.5, value, size, color)


def text_elided(x, y, value, size, color, max_width, keep='HEAD'):
    """Draw `value`, cutting it with an ellipsis if it exceeds `max_width`.

    `keep='TAIL'` cuts the front instead. A label is identified by how it
    starts and a file path by how it ends: `/home/user/Works/Dimitek/Cripp...`
    tells you nothing about where a texture went, `.../Miyop/textures/` tells
    you everything.
    """
    width, _ = text_dimensions(value, size)
    if width <= max_width:
        text(x, y, value, size, color)
        return

    ellipsis = "..."
    ellipsis_width, _ = text_dimensions(ellipsis, size)
    budget = max_width - ellipsis_width
    if budget <= 0:
        return

    trimmed = value
    if keep == 'TAIL':
        while trimmed and text_dimensions(trimmed, size)[0] > budget:
            trimmed = trimmed[1:]
        text(x, y, ellipsis + trimmed, size, color)
        return

    while trimmed and text_dimensions(trimmed, size)[0] > budget:
        trimmed = trimmed[:-1]

    text(x, y, trimmed + ellipsis, size, color)

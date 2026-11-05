"""Small GPU/BLF drawing helpers shared by the HUD and the target renderer."""
import math

import blf
import gpu
from gpu_extras.batch import batch_for_shader

FONT = 0
_shaders = {}


def builtin(*names):
    """First available builtin shader (names changed between Blender 3.x and 4.x)."""
    if names in _shaders:
        return _shaders[names]
    for name in names:
        try:
            shader = gpu.shader.from_builtin(name)
        except (ValueError, TypeError, SystemError):
            continue
        _shaders[names] = shader
        return shader
    raise RuntimeError(f'No builtin shader among {names}')


def uniform_shader():
    return builtin('UNIFORM_COLOR', '3D_UNIFORM_COLOR')


def polyline_shader():
    return builtin('POLYLINE_UNIFORM_COLOR', '3D_POLYLINE_UNIFORM_COLOR')


def point_shader():
    return builtin('POINT_UNIFORM_COLOR', 'UNIFORM_COLOR', '3D_UNIFORM_COLOR')


def pad3(points):
    return [(p[0], p[1], 0.0) if len(p) == 2 else tuple(p) for p in points]


# ------------------------------------------------------------------ batches

def tri_batch(points, indices=None):
    return batch_for_shader(uniform_shader(), 'TRIS', {'pos': pad3(points)}, indices=indices)


def line_batch(points, strip=False):
    kind = 'LINE_STRIP' if strip else 'LINES'
    return batch_for_shader(polyline_shader(), kind, {'pos': pad3(points)})


def point_batch(points):
    return batch_for_shader(point_shader(), 'POINTS', {'pos': pad3(points)})


def draw_tris(batch, color):
    shader = uniform_shader()
    # Text drawing and some shaders reset blending; set it for every draw.
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float('color', color)
    batch.draw(shader)


def draw_lines(batch, color, width=1.0):
    shader = polyline_shader()
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float('color', color)
    try:
        shader.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
        shader.uniform_float('lineWidth', width)
    except ValueError:
        gpu.state.line_width_set(width)
    batch.draw(shader)


def draw_points(batch, color, size):
    shader = point_shader()
    gpu.state.point_size_set(size)
    gpu.state.blend_set('ALPHA')
    shader.bind()
    shader.uniform_float('color', color)
    batch.draw(shader)


# ------------------------------------------------------------- immediate 2D

def tris(points, color):
    if points:
        draw_tris(tri_batch(points), color)


def lines(points, color, width=1.0, strip=False):
    if len(points) >= 2:
        draw_lines(line_batch(points, strip), color, width)


def points(pts, color, size):
    if len(pts):
        draw_points(point_batch(pts), color, size)


def _rounded_outline(x, y, w, h, r, segments=6):
    r = max(0.0, min(r, w / 2, h / 2))
    corners = ((x + w - r, y + h - r, 0), (x + r, y + h - r, 90),
               (x + r, y + r, 180), (x + w - r, y + r, 270))
    outline = []
    for cx, cy, start in corners:
        for k in range(segments + 1):
            angle = math.radians(start + 90 * k / segments)
            outline.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return outline


def rounded_rect(x, y, w, h, r, color, segments=6):
    outline = _rounded_outline(x, y, w, h, r, segments)
    centre = (x + w / 2, y + h / 2)
    fan = []
    for i, p in enumerate(outline):
        fan += [centre, p, outline[(i + 1) % len(outline)]]
    tris(fan, color)


def rounded_outline(x, y, w, h, r, color, width=1.0, segments=6):
    outline = _rounded_outline(x, y, w, h, r, segments)
    lines(outline + outline[:1], color, width, strip=True)


def circle(x, y, radius, color, width=1.0, segments=48, fill=None):
    ring = [(x + radius * math.cos(2 * math.pi * k / segments),
             y + radius * math.sin(2 * math.pi * k / segments)) for k in range(segments + 1)]
    if fill is not None:
        fan = []
        for i in range(segments):
            fan += [(x, y), ring[i], ring[i + 1]]
        tris(fan, fill)
    lines(ring, color, width, strip=True)


# --------------------------------------------------------------------- text

def _size(size):
    try:
        blf.size(FONT, size)
    except TypeError:  # Blender < 4.0 signature
        blf.size(FONT, int(size), 72)


def text_width(value, size):
    try:
        _size(size)
        return blf.dimensions(FONT, value)[0]
    except Exception:
        return len(value) * size * 0.55


def text(x, y, value, size, color):
    _size(size)
    blf.position(FONT, x, y, 0)
    blf.color(FONT, *color)
    blf.draw(FONT, value)


def fit(value, size, width):
    """Truncate ``value`` with an ellipsis to fit ``width`` pixels."""
    if text_width(value, size) <= width:
        return value
    while value and text_width(value + '…', size) > width:
        value = value[:-1]
    return value + '…'

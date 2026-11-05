"""In-viewport HUD: tool palette, tool options, hints, toasts and help.

``layout`` is pure (apart from text measuring) and returns a ``Frame`` used for
both drawing and hit testing, so what you click is exactly what is drawn.
"""
from dataclasses import dataclass, field

from . import gfx

TOOLS = ('Contours', 'PolyStrips', 'Strokes', 'Patches', 'PolyPen',
         'Knife', 'Loops', 'Tweak', 'Relax', 'Select')

DESCRIPTIONS = {
    'Contours': 'Drag across a form to add a ring',
    'PolyStrips': 'Paint a strip of quads along a stroke',
    'Strokes': 'Draw edge chains; bridges a selected boundary',
    'Patches': 'Click four corners, or F to fill a loop',
    'PolyPen': 'Click to place vertices, edges and faces',
    'Knife': 'Drag a line across faces to cut them',
    'Loops': 'Click an edge to insert an edge loop',
    'Tweak': 'Drag to move nearby vertices along the surface',
    'Relax': 'Brush to even out vertex spacing',
    'Select': 'Click or drag a box to select vertices',
}

# option id -> (scene property, label, kind, step)
OPTIONS = {
    'count': ('rf_rs_count', 'Segments', 'int', 1),
    'columns': ('rf_rs_count', 'Columns', 'int', 1),
    'rows': ('rf_rs_rows', 'Rows', 'int', 1),
    'closed': ('rf_rs_closed', 'Closed loop', 'bool', None),
    'strip_px': ('rf_rs_strip_px', 'Width (px)', 'int', 4),
    'radius': ('rf_rs_brush_radius', 'Radius (px)', 'int', 5),
    'strength': ('rf_rs_strength', 'Strength', 'float', 0.05),
    'brush_selected': ('rf_rs_brush_selected', 'Selected only', 'bool', None),
    'pen_quads': ('rf_rs_pen_quads', 'Tri → Quad', 'bool', None),
    'loop_center': ('rf_rs_loop_center', 'Snap to center', 'bool', None),
}
TOOL_OPTIONS = {
    'Contours': ('count',), 'PolyStrips': ('count', 'strip_px'),
    'Strokes': ('count', 'closed'), 'Patches': ('columns', 'rows'),
    'PolyPen': ('pen_quads',), 'Knife': (), 'Loops': ('loop_center',),
    'Tweak': ('radius', 'brush_selected'), 'Relax': ('radius', 'strength', 'brush_selected'),
    'Select': (),
}
# Ctrl+wheel in the viewport adjusts this option.
PRIMARY_OPTION = {'Contours': 'count', 'PolyStrips': 'strip_px', 'Strokes': 'count',
                  'Patches': 'columns', 'Tweak': 'radius', 'Relax': 'radius'}

HELP = (
    ('1 … 0', 'Switch tool'),
    ('Ctrl Z / Ctrl Shift Z', 'Undo / redo'),
    ('Shift click', 'Toggle-select vertex or edge (any tool)'),
    ('A  /  Alt A', 'Select all / none'),
    ('Ctrl I  /  Ctrl L', 'Invert / select linked'),
    ('M', 'Merge selected vertices'),
    ('F', 'Fill selected boundary loop with a grid'),
    ('X  /  Delete', 'Delete selected vertices'),
    ('Ctrl wheel', 'Adjust count / width / radius'),
    ('Ctrl click', 'PolyPen: start a new chain'),
    ('Backspace', 'Patches: remove last corner'),
    ('Middle mouse, wheel, numpad', 'Navigate as usual'),
    ('Esc / right click', 'Cancel the current gesture'),
    ('Tab / Enter / Esc', 'Finish the session (keeps work)'),
    ('Shift Esc', 'Discard the whole session'),
    ('F1', 'Toggle this help'),
)

THEME = {
    'panel': (0.075, 0.08, 0.095, 0.92),
    'panel_edge': (1.0, 1.0, 1.0, 0.07),
    'row_hover': (1.0, 1.0, 1.0, 0.06),
    'accent': (0.30, 0.68, 1.0, 1.0),
    'accent_soft': (0.30, 0.68, 1.0, 0.20),
    'text': (0.93, 0.94, 0.96, 1.0),
    'dim': (0.60, 0.64, 0.70, 1.0),
    'faint': (0.45, 0.48, 0.53, 1.0),
    'badge': (1.0, 1.0, 1.0, 0.07),
    'button': (1.0, 1.0, 1.0, 0.06),
    'button_hover': (1.0, 1.0, 1.0, 0.13),
    'disabled': (1.0, 1.0, 1.0, 0.25),
    'switch_off': (1.0, 1.0, 1.0, 0.16),
    'error': (0.80, 0.24, 0.24, 0.96),
    'warning': (0.86, 0.58, 0.16, 0.96),
    'info': (0.16, 0.18, 0.22, 0.96),
}


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def contains(self, px, py):
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


@dataclass
class Widget:
    kind: str          # tool | step | toggle | action | option
    rect: Rect
    value: object
    enabled: bool = True


@dataclass
class View:
    """Everything the HUD needs from the session (plain data)."""
    width: float
    height: float
    insets: tuple = (0, 0, 0, 0)    # left, right, top, bottom
    scale: float = 1.0
    tool: str = 'Select'
    options: dict = field(default_factory=dict)   # option id -> value
    stats: str = ''
    target: str = ''
    hint: str = ''
    can_undo: bool = False
    can_redo: bool = False
    toasts: tuple = ()               # (text, kind, alpha)
    show_help: bool = False


@dataclass
class Frame:
    view: View
    panels: list = field(default_factory=list)
    widgets: list = field(default_factory=list)
    palette: Rect = None
    options: Rect = None
    hint: Rect = None
    help: Rect = None

    def widget_at(self, x, y):
        for widget in reversed(self.widgets):
            if widget.rect.contains(x, y):
                return widget
        return None

    def over_hud(self, x, y):
        return any(panel.contains(x, y) for panel in self.panels)


def format_value(kind, value):
    if kind == 'float':
        return f'{value:.2f}'
    return str(value)


def layout(v):
    s = v.scale
    left, right, top, bottom = v.insets
    frame = Frame(v)
    margin, pad, row = 12 * s, 8 * s, 26 * s

    # Tool palette (top left).
    header, button, footer = 38 * s, 26 * s, 24 * s
    width = 184 * s
    height = header + len(TOOLS) * row + pad + 2 * button + 4 * s + pad + footer
    x = left + margin
    y = v.height - top - margin - height
    frame.palette = Rect(x, y, width, height)
    frame.panels.append(frame.palette)
    cursor = y + height - header
    for tool in TOOLS:
        cursor -= row
        frame.widgets.append(Widget('tool', Rect(x + 4 * s, cursor + 1 * s, width - 8 * s, row - 2 * s), tool))
    cursor -= pad + button
    third = (width - 2 * pad - 8 * s) / 3
    for k, (action, enabled) in enumerate((('undo', v.can_undo), ('redo', v.can_redo), ('help', True))):
        frame.widgets.append(Widget('action', Rect(x + pad + k * (third + 4 * s), cursor, third, button - 2 * s),
                                    action, enabled))
    cursor -= button + 4 * s
    half = (width - 2 * pad - 4 * s) / 2
    frame.widgets.append(Widget('action', Rect(x + pad, cursor, half, button - 2 * s), 'finish'))
    frame.widgets.append(Widget('action', Rect(x + pad + half + 4 * s, cursor, half, button - 2 * s), 'cancel'))

    # Tool options (top right).
    names = TOOL_OPTIONS.get(v.tool, ())
    width = 248 * s
    option_row = 30 * s
    title = 50 * s
    height = title + len(names) * option_row + (pad if names else 0)
    x = v.width - right - margin - width
    y = v.height - top - margin - height
    frame.options = Rect(x, y, width, height)
    frame.panels.append(frame.options)
    cursor = y + height - title
    for name in names:
        cursor -= option_row
        _, _, kind, _ = OPTIONS[name]
        rect = Rect(x + 4 * s, cursor, width - 8 * s, option_row)
        frame.widgets.append(Widget('option', rect, name))
        size = 22 * s
        cy = cursor + (option_row - size) / 2
        if kind == 'bool':
            frame.widgets.append(Widget('toggle', Rect(x + width - pad - 40 * s, cy, 40 * s, size), name))
        else:
            frame.widgets.append(Widget('step', Rect(x + width - pad - size, cy, size, size), (name, 1)))
            frame.widgets.append(Widget('step', Rect(x + width - pad - 3 * size - 6 * s, cy, size, size), (name, -1)))

    # Hint bar (bottom centre).
    if v.hint:
        size = 13 * s
        text_w = min(gfx.text_width(v.hint, size), v.width - left - right - 2 * margin - 32 * s)
        w = text_w + 32 * s
        frame.hint = Rect((v.width - w) / 2, bottom + margin, w, 32 * s)
        frame.panels.append(frame.hint)

    if v.show_help:
        keys = max(gfx.text_width(k, 12 * s) for k, _ in HELP)
        actions = max(gfx.text_width(a, 12 * s) for _, a in HELP)
        w, h = keys + actions + 64 * s, (len(HELP) * 22 + 64) * s
        frame.help = Rect((v.width - w) / 2, (v.height - h) / 2, w, h)
        frame.panels.append(frame.help)
    return frame


# ------------------------------------------------------------------- drawing

def _panel(rect, s):
    gfx.rounded_rect(rect.x, rect.y, rect.w, rect.h, 8 * s, THEME['panel'])
    gfx.rounded_outline(rect.x, rect.y, rect.w, rect.h, 8 * s, THEME['panel_edge'], 1.0)


def _centred(rect, value, size, color):
    w = gfx.text_width(value, size)
    gfx.text(rect.x + (rect.w - w) / 2, rect.y + (rect.h - size * 0.72) / 2, value, size, color)


def draw(frame, hover=None):
    v = frame.view
    s = v.scale
    t = THEME
    hovered = hover if isinstance(hover, Widget) else None

    # Palette.
    p = frame.palette
    _panel(p, s)
    gfx.text(p.x + 14 * s, p.y + p.h - 24 * s, 'RETOPO RS', 13 * s, t['text'])
    gfx.text(p.x + 98 * s, p.y + p.h - 24 * s, gfx.fit(v.target, 11 * s, p.w - 108 * s), 11 * s, t['faint'])
    for w in frame.widgets:
        r = w.rect
        if w.kind == 'tool':
            active = w.value == v.tool
            if active:
                gfx.rounded_rect(r.x, r.y, r.w, r.h, 5 * s, t['accent_soft'])
                gfx.rounded_rect(r.x, r.y + 4 * s, 3 * s, r.h - 8 * s, 1.5 * s, t['accent'])
            elif hovered is w:
                gfx.rounded_rect(r.x, r.y, r.w, r.h, 5 * s, t['row_hover'])
            key = str((TOOLS.index(w.value) + 1) % 10)
            badge = Rect(r.x + 10 * s, r.y + (r.h - 18 * s) / 2, 18 * s, 18 * s)
            gfx.rounded_rect(badge.x, badge.y, badge.w, badge.h, 4 * s, t['badge'])
            _centred(badge, key, 11 * s, t['accent'] if active else t['dim'])
            gfx.text(r.x + 38 * s, r.y + (r.h - 13 * s * 0.72) / 2, w.value, 13 * s,
                     t['text'] if active or hovered is w else (0.80, 0.82, 0.86, 1.0))
        elif w.kind == 'action':
            labels = {'undo': 'Undo', 'redo': 'Redo', 'help': 'Help', 'finish': 'Finish', 'cancel': 'Discard'}
            if w.value == 'finish':
                fill = t['accent'] if hovered is w else (0.22, 0.52, 0.85, 1.0)
            else:
                fill = t['button_hover'] if hovered is w and w.enabled else t['button']
            gfx.rounded_rect(r.x, r.y, r.w, r.h, 5 * s, fill)
            _centred(r, labels[w.value], 12 * s, t['text'] if w.enabled else t['disabled'])
    gfx.text(p.x + 14 * s, p.y + 9 * s, gfx.fit(v.stats, 11 * s, p.w - 28 * s), 11 * s, t['dim'])

    # Options.
    o = frame.options
    _panel(o, s)
    gfx.text(o.x + 14 * s, o.y + o.h - 22 * s, v.tool, 14 * s, t['text'])
    gfx.text(o.x + 14 * s, o.y + o.h - 40 * s,
             gfx.fit(DESCRIPTIONS.get(v.tool, ''), 11 * s, o.w - 28 * s), 11 * s, t['dim'])
    for w in frame.widgets:
        r = w.rect
        if w.kind == 'option':
            _, label, kind, _ = OPTIONS[w.value]
            row_hover = hovered is not None and (hovered is w or (
                hovered.kind in ('step', 'toggle') and
                r.contains(hovered.rect.x + hovered.rect.w / 2, hovered.rect.y + hovered.rect.h / 2)))
            if row_hover:
                gfx.rounded_rect(r.x, r.y + 2 * s, r.w, r.h - 4 * s, 5 * s, t['row_hover'])
            gfx.text(r.x + 10 * s, r.y + (r.h - 12 * s * 0.72) / 2, label, 12 * s, (0.82, 0.84, 0.88, 1.0))
            if kind != 'bool':
                value = format_value(kind, v.options.get(w.value, ''))
                size = 22 * s
                slot = Rect(r.x + r.w - 4 * s - 2 * size - 6 * s, r.y, size + 6 * s, r.h)
                _centred(slot, value, 12 * s, t['text'])
        elif w.kind == 'step':
            fill = t['button_hover'] if hovered is w else t['button']
            gfx.rounded_rect(r.x, r.y, r.w, r.h, 4 * s, fill)
            _centred(r, '+' if w.value[1] > 0 else '−', 13 * s, t['text'])
        elif w.kind == 'toggle':
            on = bool(v.options.get(w.value))
            gfx.rounded_rect(r.x, r.y + 2 * s, r.w, r.h - 4 * s, (r.h - 4 * s) / 2,
                             t['accent'] if on else t['switch_off'])
            knob = r.h - 8 * s
            kx = r.x + r.w - knob - 4 * s if on else r.x + 4 * s
            gfx.rounded_rect(kx, r.y + 4 * s, knob, knob, knob / 2, (1, 1, 1, 0.95 if hovered is w else 0.85))

    # Hint bar.
    if frame.hint is not None:
        h = frame.hint
        gfx.rounded_rect(h.x, h.y, h.w, h.h, h.h / 2, t['panel'])
        hint = gfx.fit(v.hint, 13 * s, h.w - 32 * s)
        gfx.text(h.x + 16 * s, h.y + (h.h - 13 * s * 0.72) / 2, hint, 13 * s, t['text'])

    # Toasts (top centre, newest first).
    left, right, top, bottom = v.insets
    y = v.height - top - 14 * s
    for message, kind, alpha in v.toasts:
        size = 13 * s
        w = min(gfx.text_width(message, size) + 32 * s, v.width - 2 * 220 * s)
        h = 30 * s
        y -= h
        fill = list(t.get(kind, t['info']))
        fill[3] *= alpha
        gfx.rounded_rect((v.width - w) / 2, y, w, h, h / 2, tuple(fill))
        gfx.text((v.width - w) / 2 + 16 * s, y + (h - size * 0.72) / 2, gfx.fit(message, size, w - 32 * s), size,
                 (1, 1, 1, alpha))
        y -= 6 * s

    # Help overlay.
    if frame.help is not None:
        r = frame.help
        gfx.rounded_rect(r.x, r.y, r.w, r.h, 8 * s, (0.06, 0.065, 0.08, 0.97))
        gfx.rounded_outline(r.x, r.y, r.w, r.h, 8 * s, t['panel_edge'], 1.0)
        gfx.text(r.x + 20 * s, r.y + r.h - 32 * s, 'Retopo RS shortcuts', 15 * s, t['text'])
        y = r.y + r.h - 62 * s
        column = max(gfx.text_width(k, 12 * s) for k, _ in HELP)
        for keys, action in HELP:
            gfx.text(r.x + 20 * s, y, keys, 12 * s, t['accent'])
            gfx.text(r.x + 44 * s + column, y, action, 12 * s, t['text'])
            y -= 22 * s

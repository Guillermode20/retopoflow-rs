"""Real-Blender session tests driven through Session.modal with synthetic events.

Run after building: blender --background --factory-startup --python-exit-code 1 --python tests/blender_session.py
Background Blender never recomputes view matrices, so the tests use the
default perspective view and project every coordinate through it: picking,
occlusion and brushes run on real projection math. GPU drawing is not exercised headless (see blender_gui.py).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender'))
import retopoflow_rs as addon
from retopoflow_rs import engine, session

OWNED = []


def new(name, vertices=(), edges=(), faces=()):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, edges, faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    OWNED.append(obj)
    return obj


def activate(obj):
    for other in bpy.context.view_layer.objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()


def select(obj, indices):
    engine.set_selection(obj.data, indices)
    engine.remember_selection(obj, list(indices))


class Driver:
    """Delivers synthetic events in region pixel coordinates."""

    def __init__(self, area, region):
        self.area, self.region = area, region

    def centre(self):
        return self.region.width / 2, self.region.height / 2

    def screen(self, point):
        rv3d = self.area.spaces.active.region_3d
        return tuple(view3d_utils.location_3d_to_region_2d(self.region, rv3d, Vector(point)))

    def event(self, kind, value='PRESS', xy=None, ctrl=False, shift=False, alt=False):
        x, y = xy if xy is not None else self.centre()
        return SimpleNamespace(type=kind, value=value, ctrl=ctrl, shift=shift, alt=alt, oskey=False,
                               mouse_x=int(round(self.region.x + x)), mouse_y=int(round(self.region.y + y)))

    def send(self, s, kind, value='PRESS', xy=None, **mods):
        return s.modal(bpy.context, self.event(kind, value, xy, **mods))

    def click(self, s, xy, **mods):
        self.send(s, 'MOUSEMOVE', 'NOTHING', xy, **mods)
        self.send(s, 'LEFTMOUSE', 'PRESS', xy, **mods)
        return self.send(s, 'LEFTMOUSE', 'RELEASE', xy, **mods)

    def drag(self, s, path, **mods):
        self.send(s, 'MOUSEMOVE', 'NOTHING', path[0], **mods)
        self.send(s, 'LEFTMOUSE', 'PRESS', path[0], **mods)
        for xy in path[1:]:
            self.send(s, 'MOUSEMOVE', 'NOTHING', xy, **mods)
        return self.send(s, 'LEFTMOUSE', 'RELEASE', path[-1], **mods)


def around(points, pad=20):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs) - pad, min(ys) - pad), (max(xs) + pad, max(ys) + pad)


def off_surface(d, s):
    """A viewport pixel, clear of the HUD, whose ray misses the source."""
    frame = s.frame()
    for y in range(40, int(d.region.height) - 40, 25):
        for x in range(100, int(d.region.width) - 40, 25):
            event = d.event('MOUSEMOVE', 'NOTHING', (x, y))
            if s._in_viewport(event) and not frame.over_hud(x, y) and s._hit((x, y)) is None:
                return x, y
    raise AssertionError('no off-surface pixel in view')


def lerp(a, b, t):
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def faces_of(obj):
    return sorted(len(p.vertices) for p in obj.data.polygons)


def test_lifecycle(d, source, target):
    space = d.area.spaces.active
    saved = (space.show_gizmo, space.overlay.show_text, space.show_region_ui, target.hide_get())
    activate(target)
    bpy.ops.object.mode_set(mode='EDIT')
    s = session.Session(bpy.context, 'Select')
    assert session.active_session() is s and bpy.context.mode == 'OBJECT'
    assert s.target == target and s.sources == (source,), s.sources
    assert target.rf_rs_is_target and bpy.context.scene.rf_rs_target == target
    assert target.hide_get(), 'target is drawn by the overlay renderer'
    assert not space.show_gizmo and not space.overlay.show_text and not space.show_region_ui

    s.mutate(bpy.context, 'add_vertex', [(0, 0, 1)])
    s.mutate(bpy.context, 'add_vertex', [(1, 0, 1)])
    assert len(target.data.vertices) == 2 and len(s.history.undo_stack) == 2
    try:
        s.mutate(bpy.context, 'knife', extra={'normal': [0, 0, 0]})
        raise AssertionError('invalid knife succeeded')
    except RuntimeError:
        pass
    assert len(target.data.vertices) == 2 and s.history.pending is None and len(s.history.undo_stack) == 2

    # Undo / redo through the keyboard.
    assert d.send(s, 'Z', ctrl=True) == {'RUNNING_MODAL'} and len(target.data.vertices) == 1
    d.send(s, 'Z', ctrl=True)
    assert len(target.data.vertices) == 0
    d.send(s, 'Z', ctrl=True, shift=True)
    d.send(s, 'Y', ctrl=True)
    assert len(target.data.vertices) == 2

    # Esc cancels a gesture without ending the session; right-click idle is a no-op.
    s.gesture = SimpleNamespace(kind='line', screen=[(0, 0), (5, 5)])
    assert d.send(s, 'ESC') == {'RUNNING_MODAL'} and s.gesture is None and not s.finished
    assert d.send(s, 'RIGHTMOUSE') == {'RUNNING_MODAL'} and not s.finished

    # Number keys switch tools; numpad and middle mouse pass through to navigation.
    assert d.send(s, 'FIVE') == {'RUNNING_MODAL'} and s.tool == 'PolyPen'
    assert d.send(s, 'NUMPAD_7') == {'PASS_THROUGH'}
    assert d.send(s, 'MIDDLEMOUSE') == {'PASS_THROUGH'}
    assert d.send(s, 'S', ctrl=True) == {'RUNNING_MODAL'}  # saving mid-session is blocked
    assert d.send(s, 'F1') == {'RUNNING_MODAL'} and s.show_help
    assert d.send(s, 'ESC') == {'RUNNING_MODAL'} and not s.show_help and not s.finished

    handle = s.source_handle
    assert d.send(s, 'TAB') == {'RUNNING_MODAL'} or s.finished
    assert s.finished and session.active_session() is None and not s.handles
    assert bpy.context.mode == 'EDIT_MESH'
    assert (space.show_gizmo, space.overlay.show_text, space.show_region_ui, target.hide_get()) == saved
    assert bpy.context.scene.rf_rs_tool == 'PolyPen'
    try:
        engine.call({'mesh': {'vertices': []}, 'operation': 'project'}, handle)
        raise AssertionError('source handle leaked')
    except RuntimeError as error:
        assert 'Unknown source handle' in str(error)
    bpy.ops.object.mode_set(mode='OBJECT')
    assert len(target.data.vertices) == 2

    # Shift-Esc discards the whole session, restoring geometry.
    activate(target)
    s = session.Session(bpy.context, 'Select')
    s.mutate(bpy.context, 'add_vertex', [(2, 0, 1)])
    assert len(target.data.vertices) == 3
    assert d.send(s, 'ESC', shift=True) == {'CANCELLED'}
    assert len(target.data.vertices) == 2 and session.active_session() is None

    # Blender-initiated cancel (operator.cancel) keeps work.
    activate(target)
    s = session.Session(bpy.context, 'Select')
    s.mutate(bpy.context, 'add_vertex', [(3, 0, 1)])
    operator = SimpleNamespace(_session=s)
    session.RF_RS_OT_session.cancel(operator, bpy.context)
    assert len(target.data.vertices) == 3 and s.finished
    print('  lifecycle ok')


def test_targets(d, source):
    # A lone source creates a target and remembers it for the next session.
    bpy.context.scene.rf_rs_target = None
    activate(source)
    s = session.Session(bpy.context)
    created = s.target
    OWNED.append(created)
    assert created != source and created.rf_rs_is_target and s.created is created
    s.mutate(bpy.context, 'add_vertex', [(0, 0, 0)])
    s.finish(bpy.context)
    assert bpy.context.active_object == created and bpy.context.mode == 'EDIT_MESH'
    bpy.ops.object.mode_set(mode='OBJECT')

    activate(source)
    s = session.Session(bpy.context)
    assert s.target == created and s.created is None, 'remembered target reused'
    assert source in s.sources and created not in s.sources
    s.finish(bpy.context)
    assert bpy.context.active_object == source

    # Discarding a session that created its target removes it.
    activate(source)
    s = session.Session(bpy.context, new_target=True)
    name = s.target.name
    s.finish(bpy.context, cancel=True)
    assert name not in bpy.data.objects and bpy.context.active_object == source

    # A failed start restores mode and presentation and clears the singleton.
    space = d.area.spaces.active
    saved = (space.show_gizmo, space.overlay.show_text)
    lonely = new('Lonely', [(0, 0, 5)], [], [])
    hidden = [o for o in bpy.context.view_layer.objects if o.visible_get() and o != lonely]
    for obj in hidden:
        obj.hide_set(True)
    bpy.context.scene.rf_rs_source = None
    activate(lonely)
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        session.Session(bpy.context)
        raise AssertionError('started without a source')
    except session.SessionError as error:
        assert 'No source mesh' in str(error)
    assert bpy.context.mode == 'EDIT_MESH' and session.active_session() is None
    assert (space.show_gizmo, space.overlay.show_text) == saved
    bpy.ops.object.mode_set(mode='OBJECT')
    for obj in hidden:
        obj.hide_set(False)
    print('  targets ok')


def test_picking_and_select(d, target):
    activate(target)
    s = session.Session(bpy.context, 'Select')
    try:
        quad = [(-2, -2, 0), (2, -2, 0), (2, 2, 0), (-2, 2, 0)]
        s.mutate(bpy.context, 'patch', quad, {'count': 2, 'rows': 2})
        assert faces_of(target) == [4, 4, 4, 4]
        cache = s.cache.ensure()
        corner = int(min(range(cache.count), key=lambda i: tuple(cache.world[i][:2])))
        d.click(s, d.screen(cache.world[corner]))
        assert engine.selected(target) == [corner], engine.selected(target)
        # Shift toggles another vertex on, and off again.
        other = int(max(range(cache.count), key=lambda i: tuple(cache.world[i][:2])))
        d.click(s, d.screen(cache.world[other]), shift=True)
        assert engine.selected(target) == [corner, other]
        d.click(s, d.screen(cache.world[other]), shift=True)
        assert engine.selected(target) == [corner]
        # Box select everything, Ctrl-box removes the left half.
        screens = [d.screen(p) for p in cache.world]
        a, b = around(screens)
        d.drag(s, [a, lerp(a, b, 0.5), b])
        assert len(engine.selected(target)) == 9
        a, b = around([screens[corner]], 6)
        d.drag(s, [a, b], ctrl=True)
        assert len(engine.selected(target)) == 8
        # Keyboard selection: none, all, invert, linked.
        d.send(s, 'A', alt=True)
        assert engine.selected(target) == []
        d.send(s, 'A')
        assert len(engine.selected(target)) == 9
        d.send(s, 'I', ctrl=True)
        assert engine.selected(target) == []
        select(target, [corner])
        s.cache.invalidate()
        d.send(s, 'L', ctrl=True)
        assert len(engine.selected(target)) == 9
        # Hover reports the vertex under the cursor.
        d.send(s, 'MOUSEMOVE', 'NOTHING', d.screen(cache.world[corner]))
        assert s.hover == {'kind': 'vertex', 'vertex': corner}
        # Each selection change is one undo step.
        steps = len(s.history.undo_stack)
        d.send(s, 'Z', ctrl=True)
        assert len(s.history.undo_stack) == steps - 1
    finally:
        s.finish(bpy.context)
    print('  picking ok')


def test_polypen(d, target):
    activate(target)
    s = session.Session(bpy.context, 'PolyPen')
    try:
        # Click-click builds a chain; each click is undoable.
        points = [(-3, 0, 0), (-3, 1, 0)]
        for p in points:
            d.click(s, d.screen(p))
        assert len(target.data.vertices) == 2 and len(target.data.edges) == 1
        assert len(s.history.undo_stack) == 2
        # With an edge selected: a triangle, then the next click completes a quad.
        select(target, [0, 1])
        s.cache.invalidate()
        d.click(s, d.screen((-2, 1, 0)))
        assert faces_of(target) == [3]
        assert s.pen_triangle is not None
        d.click(s, d.screen((-2, 0, 0)))
        assert faces_of(target) == [4], faces_of(target)
        # Keep going: a strip of quads.
        d.click(s, d.screen((-1, 1, 0)))
        d.click(s, d.screen((-1, 0, 0)))
        assert faces_of(target) == [4, 4], faces_of(target)
        # Clicking an existing vertex snaps to it instead of adding a new one.
        count = len(target.data.vertices)
        d.click(s, d.screen((-3, 0, 0)), ctrl=True)
        assert len(target.data.vertices) == count
        assert engine.selected(target) == [0]
        # Clicking an edge splits it (in one undo step).
        steps = len(s.history.undo_stack)
        d.click(s, d.screen((-2.5, 1, 0)), ctrl=True)
        assert len(target.data.vertices) == count + 1
        assert len(s.history.undo_stack) == steps + 1
        # Undo restores the strip.
        d.send(s, 'Z', ctrl=True)
        assert len(target.data.vertices) == count
        # Clicking off the surface is rejected with a toast, not an exception.
        d.click(s, off_surface(d, s))
        assert s.toasts and not s.finished
    finally:
        s.finish(bpy.context)
    print('  polypen ok')


def test_drawing_tools(d, target):
    activate(target)
    s = session.Session(bpy.context, 'Strokes')
    try:
        bpy.context.scene.rf_rs_count = 5
        path = [d.screen((x / 4 - 3, -3, 0)) for x in range(25)]
        d.drag(s, path)
        assert len(target.data.vertices) == 5 and len(target.data.edges) == 4
        # A second stroke beside the selected chain bridges it, even drawn backwards.
        assert s.cache.ensure().selected_boundary() == (5, False)
        d.drag(s, [d.screen((3 - x / 4, -2, 0)) for x in range(25)])
        assert faces_of(target) == [4] * 4, faces_of(target)
        normals = [p.normal.z for p in target.data.polygons]
        assert all(n > 0.9 for n in normals), normals  # oriented to face the viewer

        # PolyStrips: width in pixels converted to world units.
        d.send(s, 'TWO')
        before = len(target.data.polygons)
        d.drag(s, [d.screen((x / 4 - 3, 2, 0)) for x in range(25)])
        assert len(target.data.polygons) > before

        # Contours on a flat source: an open section across the surface.
        d.send(s, 'ONE')
        before = len(target.data.vertices)
        d.drag(s, [d.screen((0, -6, 0)), d.screen((0, 0, 0)), d.screen((0, 6, 0))])
        assert len(target.data.vertices) == before + 5, (before, len(target.data.vertices), s.toasts)

        # Knife across only the first quad strip.
        d.send(s, 'SIX')
        faces = len(target.data.polygons)
        d.drag(s, [d.screen((-2.4, -3.5, 0)), d.screen((-2.4, -1.5, 0))])
        assert len(target.data.polygons) == faces + 1, (faces, len(target.data.polygons))
        assert not target.data.validate()
    finally:
        s.finish(bpy.context)
    print('  drawing tools ok')


def test_patches_fill_loops(d, target):
    activate(target)
    s = session.Session(bpy.context, 'Patches')
    try:
        bpy.context.scene.rf_rs_count = 3
        bpy.context.scene.rf_rs_rows = 2
        corners = [(-2, -2, 0), (2, -2, 0), (2, 2, 0), (-2, 2, 0)]
        d.click(s, d.screen(corners[0]))
        d.click(s, d.screen(corners[1]))
        d.send(s, 'BACK_SPACE')
        assert len(s.corners) == 1
        for c in corners[1:]:
            d.click(s, d.screen(c))
        assert faces_of(target) == [4] * 6 and not s.corners

        # Fill: delete the interior, select the boundary loop, press F.
        d.send(s, 'A', alt=True)
        cache = s.cache.ensure()
        inner = [i for i in range(cache.count) if not cache.boundary[i]]
        select(target, inner)
        s.cache.invalidate()
        d.send(s, 'X')
        assert len(target.data.polygons) == 0
        cache = s.cache.ensure()
        select(target, range(cache.count))
        s.cache.invalidate()
        assert s.cache.ensure().selected_boundary() == (10, True)
        d.send(s, 'F')
        assert faces_of(target) == [4] * 6, faces_of(target)

        # Loops: hover an edge, preview the ring, click to insert.
        d.send(s, 'SEVEN')
        cache = s.cache.ensure()
        edge = next(i for i, (a, b) in enumerate(cache.edges.tolist())
                    if abs(cache.world[a][1] - cache.world[b][1]) > 1 and abs(cache.world[a][0] + 2) < 0.1)
        a, b = cache.edges[edge].tolist()
        xy = lerp(d.screen(cache.world[a]), d.screen(cache.world[b]), 0.5)
        d.send(s, 'MOUSEMOVE', 'NOTHING', xy)
        assert s.hover and s.hover['kind'] == 'loop' and len(s.hover['ring']) == 3
        d.click(s, xy)
        assert faces_of(target) == [4] * 9, faces_of(target)
    finally:
        s.finish(bpy.context)
    print('  patches/fill/loops ok')


def test_brushes(d, target):
    activate(target)
    s = session.Session(bpy.context, 'Tweak')
    try:
        quad = [(-2, -2, 0), (2, -2, 0), (2, 2, 0), (-2, 2, 0)]
        s.mutate(bpy.context, 'patch', quad, {'count': 4, 'rows': 4})
        cache = s.cache.ensure()
        centre = int(min(range(cache.count), key=lambda i: Vector(cache.world[i]).length))
        steps = len(s.history.undo_stack)
        start = d.screen(cache.world[centre])
        bpy.context.scene.rf_rs_brush_radius = 40
        d.drag(s, [start, (start[0] + 10, start[1]), (start[0] + 20, start[1])])
        moved = Vector(target.matrix_world @ target.data.vertices[centre].co)
        assert moved.length > 0.3 and abs(moved.z) < 1e-5, moved  # dragged and kept on the surface
        assert (Vector(d.screen(moved)) - Vector((start[0] + 20, start[1]))).length < 3
        assert len(s.history.undo_stack) == steps + 1
        d.send(s, 'Z', ctrl=True)
        assert Vector(target.data.vertices[centre].co).length < 1e-5

        # Relax pulls a displaced vertex back toward its neighbours; boundary stays.
        target.data.vertices[centre].co.x += 0.6
        s.cache.invalidate()
        boundary = [tuple(target.data.vertices[i].co) for i in range(len(target.data.vertices))
                    if s.cache.ensure().boundary[i]]
        d.send(s, 'NINE')
        bpy.context.scene.rf_rs_strength = 1.0
        xy = d.screen(s.cache.ensure().world[centre])
        d.drag(s, [xy] + [(xy[0] + 0.5 * k, xy[1]) for k in range(10)])
        assert abs(target.data.vertices[centre].co.x) < 0.45, target.data.vertices[centre].co
        after = [tuple(target.data.vertices[i].co) for i in range(len(target.data.vertices))
                 if s.cache.ensure().boundary[i]]
        assert after == boundary
    finally:
        s.finish(bpy.context)
    print('  brushes ok')


def test_hud(d, target):
    activate(target)
    s = session.Session(bpy.context, 'Strokes')
    try:
        frame = s.frame()
        left, right, top, bottom = s.insets()
        assert frame.palette.y + frame.palette.h <= d.region.height - top
        # Clicking a tool row in the palette switches tool and is not a viewport click.
        tool = next(w for w in frame.widgets if w.kind == 'tool' and w.value == 'Relax')
        xy = (tool.rect.x + tool.rect.w / 2, tool.rect.y + tool.rect.h / 2)
        vertices = len(target.data.vertices)
        d.click(s, xy)
        assert s.tool == 'Relax' and len(target.data.vertices) == vertices
        # Steppers adjust options; wheel over an option row too.
        radius = bpy.context.scene.rf_rs_brush_radius
        frame = s.frame()
        plus = next(w for w in frame.widgets if w.kind == 'step' and w.value == ('radius', 1))
        d.click(s, (plus.rect.x + plus.rect.w / 2, plus.rect.y + plus.rect.h / 2))
        assert bpy.context.scene.rf_rs_brush_radius == radius + 5
        d.send(s, 'WHEELDOWNMOUSE', xy=(plus.rect.x - 60, plus.rect.y + plus.rect.h / 2))
        assert bpy.context.scene.rf_rs_brush_radius == radius
        # Ctrl+wheel in the viewport adjusts the primary option.
        d.send(s, 'WHEELUPMOUSE', ctrl=True)
        assert bpy.context.scene.rf_rs_brush_radius == radius + 5
        # Discard needs a confirming second click.
        frame = s.frame()
        cancel = next(w for w in frame.widgets if w.kind == 'action' and w.value == 'cancel')
        xy = (cancel.rect.x + cancel.rect.w / 2, cancel.rect.y + cancel.rect.h / 2)
        d.click(s, xy)
        assert not s.finished
        d.send(s, 'LEFTMOUSE', 'PRESS', xy)
        assert s.finished
    finally:
        s.finish(bpy.context)
    print('  hud ok')


def main():
    addon.register()
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    try:
        with bpy.context.temp_override(area=area, region=region):
            d = Driver(area, region)
            source = new('Session source', [(-6, -6, 0), (6, -6, 0), (6, 6, 0), (-6, 6, 0)], [], [(0, 1, 2, 3)])
            test_lifecycle(d, source, new('Lifecycle target'))
            test_targets(d, source)
            test_picking_and_select(d, new('Picking target'))
            test_polypen(d, new('Pen target'))
            test_drawing_tools(d, new('Drawing target'))
            test_patches_fill_loops(d, new('Patch target'))
            test_brushes(d, new('Brush target'))
            test_hud(d, new('HUD target'))
        print('Rust Blender viewport session tests passed')
    finally:
        if session.active_session():
            session.shutdown(bpy.context)
        if bpy.context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in OWNED:
            try:
                mesh = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            except ReferenceError:
                pass
        addon.unregister()


if __name__ == '__main__':
    main()

"""Visual check of the live session (needs a display, not run in CI).

    blender --factory-startup --python tests/blender_gui.py -- OUTPUT_DIR

Opens a window, starts a session on a subdivided Suzanne, builds some topology
through the session API, saves screenshots of several states, then quits.
"""
import sys
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'blender'))
import retopoflow_rs as addon
from retopoflow_rs import session

OUT = Path(sys.argv[sys.argv.index('--') + 1] if '--' in sys.argv else 'gui-shots').resolve()
OUT.mkdir(parents=True, exist_ok=True)


def view():
    window = bpy.context.window_manager.windows[0]
    area = max((a for a in window.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return window, area, region


def override():
    window, area, region = view()
    return bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region)


def shot(name):
    with override():
        bpy.ops.screen.screenshot(filepath=str(OUT / f'{name}.png'))
    print('screenshot', OUT / f'{name}.png')


def setup():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.mesh.primitive_monkey_add(size=2)
    source = bpy.context.active_object
    source.name = 'Suzanne'
    modifier = source.modifiers.new('Subdivision', 'SUBSURF')
    modifier.levels = 2
    bpy.ops.object.shade_smooth()
    with override():
        bpy.ops.view3d.view_axis(type='FRONT')
        bpy.ops.view3d.view_selected()
    return source


STEPS = []


def step(fn):
    STEPS.append(fn)
    return fn


@step
def start():
    setup()
    with override():
        bpy.ops.mesh.rf_rs_session('INVOKE_DEFAULT', tool='PolyPen')
    assert session.active_session() is not None


@step
def build():
    s = session.active_session()
    window, area, region = view()
    w, h = region.width, region.height
    with override():
        ctx = bpy.context
        corners = [s._hit((w * x, h * y)) for x, y in ((0.43, 0.50), (0.57, 0.50), (0.57, 0.66), (0.43, 0.66))]
        if all(c is not None for c in corners):
            ctx.scene.rf_rs_count, ctx.scene.rf_rs_rows = 4, 4
            s.mutate(ctx, 'patch', corners, {'count': 4, 'rows': 4})
        cache = s.cache.ensure()
        right = sorted(range(cache.count), key=lambda i: -cache.world[i][0])[:2]
        s.apply_selection(right)
        s.mouse = (w * 0.62, h * 0.58)
        s.hover = s.compute_hover()
        s.toast('Visual check: PolyPen with a selected edge')
    area.tag_redraw()


@step
def polypen():
    shot('1-polypen')


@step
def relax():
    s = session.active_session()
    window, area, region = view()
    s.set_tool('Relax')
    s.mouse = (region.width * 0.5, region.height * 0.5)
    s.hover_widget = None
    area.tag_redraw()


@step
def relax_shot():
    shot('2-relax')


@step
def loops():
    s = session.active_session()
    window, area, region = view()
    s.set_tool('Loops')
    cache = s.cache.ensure()
    from bpy_extras import view3d_utils
    rv3d = area.spaces.active.region_3d
    for a, b in cache.edges.tolist():
        mid = (Vector(cache.world[a]) * 0.7 + Vector(cache.world[b]) * 0.3)
        s.mouse = tuple(view3d_utils.location_3d_to_region_2d(region, rv3d, mid))
        s.hover = s.compute_hover()
        if s.hover is not None and len(s.hover['ring']) > 2:
            break
    area.tag_redraw()


@step
def loops_shot():
    shot('3-loops')


@step
def help_on():
    s = session.active_session()
    s.show_help = True
    view()[1].tag_redraw()


@step
def help_shot():
    shot('4-help')


@step
def finish():
    s = session.active_session()
    s.show_help = False
    with override():
        s.finish(bpy.context)
    view()[1].tag_redraw()


@step
def after():
    shot('5-after')


@step
def quit_blender():
    addon.unregister()
    bpy.ops.wm.quit_blender()


def runner():
    if not STEPS:
        return None
    fn = STEPS.pop(0)
    try:
        fn()
    except Exception:
        import traceback
        traceback.print_exc()
        STEPS[:] = [quit_blender]
    return 0.8


bpy.context.preferences.view.show_splash = False
addon.register()
bpy.app.timers.register(runner, first_interval=1.5)

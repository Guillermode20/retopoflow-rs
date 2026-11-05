"""Persistent viewport retopology session.

One modal operator owns the viewport while you retopologize: a HUD with tools
and options, a custom target renderer that respects source occlusion, live
brushes, per-action undo and the native Rust engine for topology changes.
``Session`` is a plain class (the operator only forwards to it) so the whole
state machine can be driven deterministically from headless tests.
"""
import time
from contextlib import contextmanager
from types import SimpleNamespace

import bmesh
import bpy
import gpu
import numpy as np
from bpy.props import BoolProperty, EnumProperty
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import engine, gfx, hud
from .hud import TOOLS
from .target import TargetCache

_SESSION = None
KEYS = dict(zip(('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN',
                 'EIGHT', 'NINE', 'ZERO'), TOOLS))
WHEEL = frozenset({'WHEELUPMOUSE', 'WHEELDOWNMOUSE'})
NAVIGATION = frozenset({'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN',
                        'TRACKPADZOOM', 'NDOF_MOTION', 'HOME', 'ACCENT_GRAVE'})
MOVES = frozenset({'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'})
CURSORS = {'Select': 'DEFAULT', 'Knife': 'KNIFE', 'Tweak': 'HAND', 'Relax': 'PAINT_BRUSH'}
COLORS = {
    'face': (0.30, 0.68, 1.0, 0.16),
    'edge': (0.62, 0.85, 1.0, 0.95),
    'vertex': (0.88, 0.94, 1.0, 1.0),
    'selected': (1.0, 0.60, 0.16, 1.0),
    'hover': (1.0, 0.92, 0.30, 1.0),
    'preview': (0.30, 0.78, 1.0, 1.0),
    'preview_soft': (0.30, 0.78, 1.0, 0.35),
    'box': (1.0, 1.0, 1.0, 0.85),
}
TOAST_SECONDS = 4.0


class SessionError(RuntimeError):
    """The session cannot start or must end."""


class UserError(ValueError):
    """A gesture was rejected; shown as a toast while the session continues."""


def active_session():
    return _SESSION


def shutdown(context):
    if _SESSION is not None:
        _SESSION.finish(context, cancel=False)


def falloff(x):
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    return (1.0 - x * x) ** 2


class SessionHistory:
    """Per-action undo with bmesh snapshots (no Blender datablocks are created).

    begin/commit/rollback make failed native operations transactional.
    """

    def __init__(self, target, limit=64):
        self.target = target
        self.limit = limit
        self.undo_stack = []
        self.redo_stack = []
        self.pending = None
        self.entry = self.capture()

    def capture(self):
        snapshot = bmesh.new()
        snapshot.from_mesh(self.target.data)
        return snapshot, tuple(engine.selected(self.target))

    def restore(self, state):
        snapshot, order = state
        mesh = self.target.data
        if mesh.users > 1:
            mesh = self.target.data = mesh.copy()
        snapshot.to_mesh(mesh)
        mesh.update()
        engine.remember_selection(self.target, order)

    @staticmethod
    def _free(state):
        state[0].free()

    def begin(self):
        if self.pending is not None:
            raise RuntimeError('A gesture is already pending')
        self.pending = self.capture()

    def commit(self):
        if self.pending is None:
            return
        self.undo_stack.append(self.pending)
        self.pending = None
        self.clear_stack(self.redo_stack)
        while len(self.undo_stack) > self.limit:
            self._free(self.undo_stack.pop(0))

    def discard(self):
        """Drop the pending snapshot when nothing changed."""
        if self.pending is not None:
            self._free(self.pending)
            self.pending = None

    def rollback(self):
        if self.pending is not None:
            state, self.pending = self.pending, None
            try:
                self.restore(state)
            finally:
                self._free(state)

    def step(self, redo=False):
        self.rollback()
        source, destination = ((self.redo_stack, self.undo_stack) if redo else
                               (self.undo_stack, self.redo_stack))
        if not source:
            return False
        current = self.capture()
        try:
            self.restore(source[-1])
        except Exception:
            self._free(current)
            raise
        self._free(source.pop())
        destination.append(current)
        return True

    def clear_stack(self, stack):
        for state in stack:
            self._free(state)
        stack.clear()

    def close(self, cancel=False):
        try:
            if cancel:
                self.restore(self.entry)
        finally:
            if self.pending is not None:
                self._free(self.pending)
                self.pending = None
            self.clear_stack(self.undo_stack)
            self.clear_stack(self.redo_stack)
            self._free(self.entry)


def _is_target(obj):
    return bool(getattr(obj, 'rf_rs_is_target', False))


def _has_surface(obj):
    return obj.type == 'MESH' and (len(obj.data.polygons) or len(obj.modifiers))


class Session:
    def __init__(self, context, tool='Select', new_target=False, report=None):
        global _SESSION
        if _SESSION is not None:
            raise SessionError('A Retopo RS session is already active')
        self.report = report or (lambda kind, message: None)
        self.tool = tool if tool in TOOLS else 'Select'
        self.finished = False
        self.created = None
        self.target = None
        self.sources = ()
        self.history = None
        self.cache = None
        self.source_handle = None
        self.handles = []
        self.timer = None
        self.restorers = []
        self.gesture = None
        self.corners = []
        self.hover = None
        self.hover_widget = None
        self.mouse = (0.0, 0.0)
        self.toasts = []
        self.show_help = False
        self.pen_triangle = None
        self.discard_armed = 0.0
        self.cursor = None
        self.render_failed = False
        self.batch_key = None
        self.batches = {}
        _SESSION = self
        try:
            self._start(context, new_target)
        except Exception:
            self.finish(context, cancel=True)
            raise

    # ------------------------------------------------------------ lifecycle
    def _start(self, context, new_target):
        if context.area is None or context.area.type != 'VIEW_3D':
            raise SessionError('Start in a 3D viewport')
        obj = context.active_object
        if obj is None or obj.type != 'MESH' or context.mode not in {'OBJECT', 'EDIT_MESH'}:
            raise SessionError('Start with an active mesh in Object or Edit Mode')
        if obj.library or obj.data.library:
            raise SessionError('Target must be editable')
        self.area = context.area
        self.space = context.space_data
        self.region = next((r for r in self.area.regions if r.type == 'WINDOW'), None)
        if self.region is None:
            raise SessionError('Viewport has no window region')
        self.rv3d = self.space.region_3d
        self.window = context.window
        self.workspace = context.workspace
        self.scene = context.scene
        self.view_layer = context.view_layer
        self.original_active = obj
        self.original_selected = list(context.selected_objects)
        self.original_mode = context.mode
        if context.mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='OBJECT')

        explicit = self.scene.rf_rs_source
        remembered = self.scene.rf_rs_target
        if remembered is not None and (remembered.type != 'MESH' or remembered.name not in self.view_layer.objects
                                       or remembered.library or remembered.data.library):
            remembered = None
        others = [o for o in context.visible_objects if o != obj and _has_surface(o) and not _is_target(o)]
        active_is_source = (obj == explicit or (not _is_target(obj) and self.original_mode == 'OBJECT'
                                                and len(obj.data.polygons) and not others and explicit is None))
        if new_target:
            self.target = self._create_target(context)
        elif active_is_source:
            # Continue the target last used for this scene instead of piling up new ones.
            self.target = remembered if remembered is not None and remembered != obj else self._create_target(context)
        else:
            self.target = obj

        if explicit is not None and explicit != self.target and explicit.type == 'MESH':
            candidates = [explicit]
        else:
            candidates = [o for o in context.visible_objects
                          if o != self.target and _has_surface(o) and not _is_target(o)]
            if obj not in candidates and obj != self.target and _has_surface(obj) and not _is_target(obj):
                candidates.append(obj)
        if not candidates:
            raise SessionError('No source mesh found. Pick the high-poly object in Source, '
                               'or select it in Object Mode and press Retopo RS.')
        vertices, triangles, usable, warnings, skipped = [], [], [], [], 0
        for source in candidates:
            try:
                data = engine.source_geometry(context, source)
            except RuntimeError as exc:
                if source == explicit:
                    raise
                warnings.append(str(exc))
                continue
            usable.append(source)
            skipped += data['skipped_triangles']
            offset = len(vertices)
            vertices.extend(data['vertices'])
            triangles.extend([[i + offset for i in tri] for tri in data['triangles']])
        if not triangles:
            raise SessionError('No usable source surfaces. ' + '; '.join(warnings))
        self.sources = tuple(usable)
        self.source = {'vertices': vertices, 'triangles': triangles}
        self.bvh = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
        self.source_handle = engine.cache_source(self.source)
        self.history = SessionHistory(self.target)
        self.cache = TargetCache(self.target).refresh()
        if skipped or warnings:
            parts = []
            if skipped:
                parts.append(f'skipped {skipped} zero-area triangles')
            if warnings:
                parts.append(f'ignored {len(warnings)} unusable meshes')
            self.toast('Source cleanup: ' + ', '.join(parts), 'warning')

        # Viewport state changes happen only once everything above succeeded.
        self.target.rf_rs_is_target = True
        self.scene.rf_rs_target = self.target
        for selected in context.selected_objects:
            selected.select_set(False)
        self.target.select_set(True)
        self.view_layer.objects.active = self.target
        hidden = self.target.hide_get()
        self.target.hide_set(True)   # drawn by our renderer instead
        self.restorers.append(lambda: self.target.hide_set(hidden))
        self._set(self.space, 'show_gizmo', False)
        self._set(self.space.overlay, 'show_text', False)
        self._set(self.space.overlay, 'show_stats', False)
        for name in ('show_region_ui', 'show_region_toolbar', 'show_region_tool_header'):
            self._set(self.space, name, False)
        self.handles.append(bpy.types.SpaceView3D.draw_handler_add(self.draw_3d, (), 'WINDOW', 'POST_VIEW'))
        self.handles.append(bpy.types.SpaceView3D.draw_handler_add(self.draw_2d, (), 'WINDOW', 'POST_PIXEL'))
        self.workspace.status_text_set('Retopo RS  ·  1–0 tools  ·  F1 help  ·  Tab finish  ·  Shift Esc discard')
        manager = getattr(context, 'window_manager', None)
        if manager is not None and hasattr(manager, 'event_timer_add') and self.window is not None:
            self.timer = manager.event_timer_add(1 / 30, window=self.window)
        self._set_cursor()
        self.area.tag_redraw()

    def _create_target(self, context):
        mesh = bpy.data.meshes.new('Retopo Target')
        self.created = bpy.data.objects.new('Retopo Target', mesh)
        context.collection.objects.link(self.created)
        self.created.rf_rs_is_target = True
        # Keeps the retopology readable over the source after the session too.
        self.created.show_in_front = True
        return self.created

    def _set(self, owner, name, value):
        if not hasattr(owner, name):
            return
        previous = getattr(owner, name)
        # Region visibility setters can toggle even when assigned their
        # current value; avoid redundant writes on entry and restoration.
        if previous == value:
            return
        setattr(owner, name, value)

        def restore(owner=owner, name=name, previous=previous):
            if getattr(owner, name) != previous:
                setattr(owner, name, previous)
        self.restorers.append(restore)

    def finish(self, context, cancel=False):
        global _SESSION
        if self.finished:
            return
        self.finished = True
        self.gesture = None
        try:
            if self.history is not None:
                try:
                    self.history.close(cancel)
                except (ReferenceError, RuntimeError):
                    pass
            if cancel and self.created is not None:
                try:
                    mesh = self.created.data
                    bpy.data.objects.remove(self.created, do_unlink=True)
                    if mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
                except ReferenceError:
                    pass
        finally:
            for handle in self.handles:
                try:
                    bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
                except (ValueError, ReferenceError):
                    pass
            self.handles.clear()
            if self.source_handle:
                try:
                    engine.free_source(self.source_handle)
                except Exception:
                    pass
                self.source_handle = None
            if self.timer is not None:
                try:
                    context.window_manager.event_timer_remove(self.timer)
                except Exception:
                    pass
                self.timer = None
            if self.cursor is not None:
                try:
                    self.window.cursor_modal_restore()
                except Exception:
                    pass
                self.cursor = None
            for restore in reversed(self.restorers):
                try:
                    restore()
                except (ReferenceError, RuntimeError, AttributeError):
                    pass
            self.restorers.clear()
            self._restore_selection(context, cancel)
            try:
                self.scene.rf_rs_tool = self.tool
            except Exception:
                pass
            try:
                self.workspace.status_text_set(None)
                self.area.tag_redraw()
            except (ReferenceError, AttributeError):
                pass
            if _SESSION is self:
                _SESSION = None

    def _restore_selection(self, context, cancel):
        view_layer = getattr(self, 'view_layer', None)
        if view_layer is None:
            return
        try:
            for obj in view_layer.objects:
                obj.select_set(False)
            if not cancel and self.created is not None:
                # New-target sessions finish on the retopology, not the source.
                self.target.select_set(True)
                view_layer.objects.active = self.target
                return_to_edit = True
            else:
                for obj in self.original_selected:
                    try:
                        obj.select_set(True)
                    except (ReferenceError, RuntimeError):
                        pass
                view_layer.objects.active = self.original_active
                return_to_edit = self.original_mode == 'EDIT_MESH'
            if return_to_edit:
                with context.temp_override(window=self.window, area=self.area):
                    if context.mode == 'OBJECT':
                        bpy.ops.object.mode_set(mode='EDIT')
        except (ReferenceError, RuntimeError, TypeError):
            pass

    # ---------------------------------------------------------------- utils
    @property
    def scale(self):
        try:
            # 0.0 in background mode, where there is no display.
            return bpy.context.preferences.system.ui_scale or 1.0
        except AttributeError:
            return 1.0

    def toast(self, message, kind='info'):
        self.toasts = [t for t in self.toasts if t[0] != message][-2:]
        self.toasts.append((message, kind, time.monotonic() + TOAST_SECONDS))
        if kind in ('error', 'warning'):
            self.report({'WARNING'}, message)
        self._redraw()

    def _redraw(self):
        try:
            self.area.tag_redraw()
        except (ReferenceError, AttributeError):
            pass

    def _coordinate(self, event):
        return float(event.mouse_x - self.region.x), float(event.mouse_y - self.region.y)

    def _overlapping_regions(self):
        win = self.region
        for region in self.area.regions:
            if region.type == 'WINDOW' or region.width <= 1 or region.height <= 1:
                continue
            if (region.x >= win.x + win.width or region.x + region.width <= win.x or
                    region.y >= win.y + win.height or region.y + region.height <= win.y):
                continue
            yield region

    def insets(self):
        """Pixels of the window region covered by overlapping header/toolbar/sidebar.

        Floating regions (the operator redo panel) and hidden ones do not count.
        """
        win = self.region
        left = right = top = bottom = 0
        for region in self._overlapping_regions():
            align = getattr(region, 'alignment', 'NONE')
            if align == 'TOP':
                top = max(top, win.y + win.height - region.y)
            elif align == 'BOTTOM':
                bottom = max(bottom, region.y + region.height - win.y)
            elif align == 'LEFT':
                left = max(left, region.x + region.width - win.x)
            elif align == 'RIGHT':
                right = max(right, win.x + win.width - region.x)
        return left, right, top, bottom

    def _in_viewport(self, event):
        x, y = event.mouse_x, event.mouse_y
        win = self.region
        if not (win.x <= x < win.x + win.width and win.y <= y < win.y + win.height):
            return False
        return not any(r.x <= x < r.x + r.width and r.y <= y < r.y + r.height
                       for r in self._overlapping_regions())

    def _hit(self, xy):
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, Vector(xy))
        direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, Vector(xy))
        return self.bvh.ray_cast(origin, direction)[0]

    def _pixels_to_world(self, pixels, point):
        screen = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, point)
        if screen is None:
            raise UserError('Stroke starts behind the view')
        other = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, screen + Vector((pixels, 0)), point)
        return max((other - point).length, 1e-6)

    def _set_cursor(self, over_hud=False):
        name = 'DEFAULT' if over_hud else CURSORS.get(self.tool, 'CROSSHAIR')
        if name == self.cursor or self.window is None:
            return
        try:
            self.window.cursor_modal_set(name)
            self.cursor = name
        except Exception:
            pass

    def _context_ok(self, context):
        try:
            return (context.area == self.area and self.area.type == 'VIEW_3D' and context.mode == 'OBJECT'
                    and context.view_layer.objects.active == self.target
                    and self.target.name in context.view_layer.objects)
        except ReferenceError:
            return False

    # ------------------------------------------------------------- mutation
    @contextmanager
    def transaction(self):
        self.history.begin()
        try:
            yield
        except BaseException:
            self.history.rollback()
            raise
        else:
            self.history.commit()
        finally:
            self.cache.invalidate()
            self._redraw()

    def run(self, context, operation, points=(), extra=None, selection=None):
        """One native call inside an open transaction."""
        return engine.execute(context, operation, points, self.target, self.source, extra,
                              self.source_handle, selection)

    def mutate(self, context, operation, points=(), extra=None, selection=None):
        with self.transaction():
            return self.run(context, operation, points, extra, selection)

    def apply_selection(self, order):
        order = [int(i) for i in order]
        with self.transaction():
            if self.target.data.users > 1:
                self.target.data = self.target.data.copy()
            engine.set_selection(self.target.data, order)
            engine.remember_selection(self.target, order)

    def set_tool(self, tool):
        if tool == self.tool:
            return
        self.cancel_gesture()
        self.tool = tool
        self.hover = None
        self._set_cursor()
        self._redraw()

    def cancel_gesture(self):
        gesture, self.gesture = self.gesture, None
        self.corners = []
        if gesture is not None and getattr(gesture, 'transaction', False):
            self.history.rollback()
            self.cache.invalidate()
        self._redraw()

    def undo(self, redo=False):
        self.cancel_gesture()
        if self.history.step(redo):
            self.cache.invalidate()
            self.pen_triangle = None
            self.toast('Redo' if redo else 'Undo')
        else:
            self.toast('Nothing to redo' if redo else 'Nothing to undo')

    def adjust(self, name, direction):
        prop, _, kind, step = hud.OPTIONS[name]
        value = getattr(self.scene, prop)
        if kind == 'bool':
            setattr(self.scene, prop, not value)
        else:
            setattr(self.scene, prop, value + direction * step)
        self._redraw()

    # -------------------------------------------------------------- the HUD
    def frame(self):
        cache = self.cache.ensure()
        names = hud.TOOL_OPTIONS.get(self.tool, ())
        now = time.monotonic()
        self.toasts = [t for t in self.toasts if t[2] > now]
        toasts = tuple((message, kind, max(0.0, min(1.0, (expires - now) / 0.4)))
                       for message, kind, expires in reversed(self.toasts))
        view = hud.View(
            width=self.region.width, height=self.region.height, insets=self.insets(),
            scale=self.scale, tool=self.tool,
            options={n: getattr(self.scene, hud.OPTIONS[n][0]) for n in names},
            stats=(f'{cache.count} v  ·  {len(cache.edges)} e  ·  {len(cache.faces)} f'
                   f'  ·  {int(cache.selected.sum())} sel'),
            target=self.target.name, hint=self.hint(),
            can_undo=bool(self.history.undo_stack), can_redo=bool(self.history.redo_stack),
            toasts=toasts, show_help=self.show_help)
        return hud.layout(view)

    def hint(self):
        tool, cache = self.tool, self.cache.ensure()
        selection = cache.order
        boundary = cache.selected_boundary()
        if tool == 'Select':
            return 'Click select  ·  Shift toggle  ·  Drag box  ·  Ctrl-drag deselect  ·  Ctrl L linked'
        if tool == 'PolyPen':
            if self.pen_triangle is not None and self.pen_triangle == self._pen_key(selection):
                return 'Click to complete the quad  ·  Ctrl-click starts a new chain'
            if len(selection) == 2 and cache.has_edge(*selection):
                return 'Click to add a triangle off the selected edge  ·  Ctrl-click new chain'
            if len(selection) == 1:
                return 'Click to extend the chain  ·  click a vertex to connect  ·  Ctrl-click new chain'
            return 'Click to place a vertex  ·  click an edge to split it  ·  Shift-click selects'
        if tool in ('Strokes', 'Contours'):
            if boundary is not None:
                kind = 'loop' if boundary[1] else 'chain'
                verb = 'Draw beside' if tool == 'Strokes' else 'Drag across the form near'
                return f'{verb} the selected {kind} ({boundary[0]} verts) to bridge it  ·  Ctrl-drag: separate'
            if tool == 'Strokes':
                return 'Drag a stroke on the surface  ·  end near the start to close it'
            return 'Drag a line across the form to add a ring  ·  Ctrl-wheel: segments'
        if tool == 'PolyStrips':
            return 'Drag along the surface to paint a strip  ·  Ctrl-wheel: width'
        if tool == 'Patches':
            if self.corners:
                return f'Corner {len(self.corners)}/4  ·  click the next corner  ·  Backspace removes one'
            if boundary is not None and boundary[1]:
                return f'Press F to fill the selected loop ({boundary[0]} verts)  ·  or click four corners'
            return 'Click four corners in order  ·  F fills a selected boundary loop'
        if tool == 'Knife':
            return 'Drag a line across faces to cut them'
        if tool == 'Loops':
            return 'Hover an edge and click to insert a loop' + (
                ' at the center' if self.scene.rf_rs_loop_center else ' where you click')
        if tool == 'Tweak':
            return 'Drag to move vertices under the brush  ·  Ctrl-wheel: radius'
        if tool == 'Relax':
            return 'Hold and brush to relax vertices  ·  boundaries stay fixed  ·  Ctrl-wheel: radius'
        return ''

    def activate(self, context, widget):
        kind, value = widget.kind, widget.value
        if kind == 'tool':
            self.set_tool(value)
        elif kind == 'step':
            self.adjust(*value)
        elif kind in ('toggle', 'option'):
            if hud.OPTIONS[value][2] == 'bool':
                self.adjust(value, 1)
        elif kind == 'action' and widget.enabled:
            if value in ('undo', 'redo'):
                self.undo(redo=value == 'redo')
            elif value == 'help':
                self.show_help = not self.show_help
            elif value == 'finish':
                self.finish(context)
                return {'FINISHED'}
            elif value == 'cancel':
                if time.monotonic() < self.discard_armed:
                    self.finish(context, cancel=True)
                    return {'CANCELLED'}
                self.discard_armed = time.monotonic() + 3.0
                self.toast('Click Discard again to throw away this session', 'warning')
        self._redraw()
        return None

    # ------------------------------------------------------------ the modal
    def modal(self, context, event):
        if self.finished:
            return {'FINISHED'}
        try:
            return self._modal(context, event)
        except UserError as exc:
            self.cancel_gesture()
            self.toast(str(exc), 'warning')
            return {'RUNNING_MODAL'}
        except (engine.EngineError, RuntimeError) as exc:
            if isinstance(exc, SessionError) or not self._context_ok(context):
                self.report({'WARNING'}, str(exc))
                self.finish(context)
                return {'FINISHED'}
            self.cancel_gesture()
            self.toast(str(exc), 'error')
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self.report({'ERROR'}, f'Retopo RS: {exc}')
            self.finish(context)
            return {'FINISHED'}

    def _modal(self, context, event):
        if not self._context_ok(context):
            raise SessionError('Session context changed; ending session')
        kind, value = event.type, event.value
        press = value == 'PRESS'
        ctrl = event.ctrl or getattr(event, 'oskey', False)
        if kind == 'TIMER':
            self.tick(context)
            return {'PASS_THROUGH'}
        if ctrl and kind == 'S':
            self.toast('Finish the session (Tab) before saving', 'warning')
            return {'RUNNING_MODAL'}
        if press and kind == 'ESC' and event.shift:
            self.finish(context, cancel=True)
            return {'CANCELLED'}
        if press and kind in {'ESC', 'RIGHTMOUSE'}:
            if self.gesture is not None or self.corners:
                self.cancel_gesture()
            elif self.show_help:
                self.show_help = False
                self._redraw()
            elif kind == 'ESC':
                self.finish(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}
        if press and kind == 'F1':
            self.show_help = not self.show_help
            self._redraw()
            return {'RUNNING_MODAL'}
        if ctrl and kind in {'Z', 'Y'}:
            if press:
                self.undo(redo=kind == 'Y' or event.shift)
            return {'RUNNING_MODAL'}

        xy = self._coordinate(event)
        if self.gesture is not None:
            # Drags keep tracking past the viewport edge and always finish.
            if kind in MOVES:
                self.mouse = xy
                self.gesture_move(context)
                self._redraw()
            elif kind == 'LEFTMOUSE' and value == 'RELEASE':
                self.mouse = xy
                self.gesture_release(context)
            return {'RUNNING_MODAL'}
        if not self._in_viewport(event):
            if self.hover_widget is not None or self.hover is not None:
                self.hover_widget = self.hover = None
                self._redraw()
            self._set_cursor(over_hud=True)
            return {'PASS_THROUGH'}

        self.mouse = xy
        frame = self.frame()
        widget = frame.widget_at(*xy)
        over_hud = frame.over_hud(*xy)
        if kind in MOVES:
            self.hover_widget = widget
            self.hover = None if over_hud else self.compute_hover()
            self._set_cursor(over_hud)
            self._redraw()
            return {'RUNNING_MODAL'}
        if over_hud:
            if widget is not None and press and kind == 'LEFTMOUSE':
                return self.activate(context, widget) or {'RUNNING_MODAL'}
            if widget is not None and kind in WHEEL and widget.kind in ('option', 'step', 'toggle'):
                name = widget.value[0] if widget.kind == 'step' else widget.value
                self.adjust(name, 1 if kind == 'WHEELUPMOUSE' else -1)
            return {'RUNNING_MODAL'}
        if press and self.shortcut(context, event):
            return {'RUNNING_MODAL'}
        if ctrl and kind in WHEEL and self.tool in hud.PRIMARY_OPTION:
            self.adjust(hud.PRIMARY_OPTION[self.tool], 1 if kind == 'WHEELUPMOUSE' else -1)
            return {'RUNNING_MODAL'}
        if self._is_navigation(event):
            return {'PASS_THROUGH'}
        if kind == 'LEFTMOUSE' and press:
            self.press(context, event)
        return {'RUNNING_MODAL'}

    @staticmethod
    def _is_navigation(event):
        kind = event.type
        return (kind in NAVIGATION or kind.startswith('NDOF_') or
                (kind.startswith('NUMPAD_') and kind != 'NUMPAD_ENTER') or
                (event.alt and kind == 'LEFTMOUSE'))

    def shortcut(self, context, event):
        kind = event.type
        ctrl = event.ctrl or getattr(event, 'oskey', False)
        if kind in KEYS and not ctrl and not event.alt:
            self.set_tool(KEYS[kind])
            return True
        if kind == 'W' and not ctrl:
            self.set_tool('Select')
            return True
        if kind in {'RET', 'NUMPAD_ENTER', 'TAB'}:
            self.finish(context)
            return True
        if kind == 'BACK_SPACE' and self.corners:
            self.corners.pop()
            self._redraw()
            return True
        cache = self.cache.ensure()
        if kind == 'A' and not ctrl:
            self.apply_selection([] if event.alt else range(cache.count))
            return True
        if kind == 'I' and ctrl:
            self.apply_selection(np.flatnonzero(~cache.selected).tolist())
            return True
        if kind == 'L' and ctrl:
            if not cache.order:
                raise UserError('Select a vertex first')
            self.apply_selection(cache.linked(cache.order))
            return True
        if kind == 'M' and not ctrl:
            self.mutate(context, 'merge')
            return True
        if kind == 'F' and not ctrl:
            self.fill(context)
            return True
        if kind in {'X', 'DEL'} and not ctrl:
            if not cache.order:
                raise UserError('Nothing selected to delete')
            self.mutate(context, 'delete')
            return True
        return False

    def fill(self, context):
        cache = self.cache.ensure()
        boundary = cache.selected_boundary()
        if boundary is None or not boundary[1]:
            raise UserError('Select a closed boundary loop to fill (every vertex of it)')
        self.mutate(context, 'fill_auto')
        self.toast(f'Filled a {boundary[0]}-vertex loop')

    def tick(self, context):
        now = time.monotonic()
        if any(t[2] - now < 0.5 for t in self.toasts):
            self._redraw()
        gesture = self.gesture
        if gesture is not None and gesture.kind == 'relax' and now - gesture.last >= 1 / 30:
            self.relax_step()

    # ----------------------------------------------------------- hovering
    def compute_hover(self):
        cache = self.cache.ensure()
        s = self.scale
        tool = self.tool
        if tool == 'PolyPen':
            return self.pen_target(self.mouse)
        if tool == 'Select':
            vertex = cache.nearest_vertex(self.mouse, 14 * s, self.region, self.rv3d, self.bvh)
            if vertex is not None:
                return {'kind': 'vertex', 'vertex': vertex}
            edge = cache.nearest_edge(self.mouse, 8 * s, self.region, self.rv3d, self.bvh)
            return {'kind': 'edge', 'edge': edge[0]} if edge else None
        if tool == 'Loops':
            edge = cache.nearest_edge(self.mouse, 16 * s, self.region, self.rv3d, self.bvh)
            if edge is None:
                return None
            factor = 0.5 if self.scene.rf_rs_loop_center else min(max(edge[1], 0.05), 0.95)
            return {'kind': 'loop', 'edge': edge[0], 'factor': factor,
                    'ring': cache.loop_ring(edge[0], factor)}
        if tool in ('Patches', 'Strokes', 'PolyStrips'):
            hit = self._hit(self.mouse)
            return {'kind': 'surface', 'point': hit} if hit is not None else None
        return None

    def pen_target(self, xy):
        cache = self.cache.ensure()
        s = self.scale
        vertex = cache.nearest_vertex(xy, 12 * s, self.region, self.rv3d, self.bvh)
        if vertex is not None:
            return {'kind': 'vertex', 'vertex': vertex, 'point': Vector(cache.world[vertex])}
        edge = cache.nearest_edge(xy, 6 * s, self.region, self.rv3d, self.bvh)
        if edge is not None:
            a, b = cache.edges[edge[0]].tolist()
            point = Vector(cache.world[a] + (cache.world[b] - cache.world[a]) * edge[1])
            return {'kind': 'edge', 'edge': edge[0], 'point': point}
        hit = self._hit(xy)
        return {'kind': 'surface', 'point': hit} if hit is not None else None

    # ------------------------------------------------------------ gestures
    def press(self, context, event):
        tool = self.tool
        ctrl = event.ctrl or getattr(event, 'oskey', False)
        xy = self.mouse
        if event.shift and tool not in ('Select',):
            self.click_select(xy, 'toggle')
            return
        if tool == 'Select':
            mode = 'remove' if ctrl else ('toggle' if event.shift else 'set')
            self.gesture = SimpleNamespace(kind='select', start=xy, mode=mode)
        elif tool == 'PolyPen':
            self.pen_click(context, new_chain=ctrl)
        elif tool == 'Patches':
            self.patch_corner(context)
        elif tool == 'Loops':
            self.loop_click(context)
        elif tool in ('Strokes', 'PolyStrips'):
            hit = self._hit(xy)
            if hit is None:
                raise UserError('Start the stroke on the source surface')
            self.gesture = SimpleNamespace(kind='stroke', points=[hit], screen=[xy], separate=ctrl)
        elif tool in ('Contours', 'Knife'):
            self.gesture = SimpleNamespace(kind='line', screen=[xy, xy], separate=ctrl)
        elif tool in ('Tweak', 'Relax'):
            self.brush_begin()

    def gesture_move(self, context):
        g, xy = self.gesture, self.mouse
        if g.kind == 'stroke':
            if (Vector(xy) - Vector(g.screen[-1])).length >= 3 * self.scale:
                hit = self._hit(xy)
                if hit is not None and (hit - g.points[-1]).length > 1e-9:
                    g.points.append(hit)
                    g.screen.append(xy)
        elif g.kind == 'line':
            g.screen[1] = xy
        elif g.kind == 'tweak':
            self.tweak_move()
        elif g.kind == 'relax':
            self.relax_step()

    def gesture_release(self, context):
        g = self.gesture
        if g.kind == 'select':
            self.gesture = None
            if (Vector(self.mouse) - Vector(g.start)).length < 4 * self.scale:
                self.click_select(self.mouse, g.mode)
            else:
                self.box_select(g.start, self.mouse, g.mode)
        elif g.kind == 'stroke':
            self.gesture = None
            self.commit_stroke(context, g)
        elif g.kind == 'line':
            self.gesture = None
            self.commit_line(context, g)
        elif g.kind in ('tweak', 'relax'):
            self.gesture = None
            if g.moved:
                self.history.commit()
            else:
                self.history.discard()
            self.cache.invalidate()
        self._redraw()

    # ------------------------------------------------------------ selection
    def click_select(self, xy, mode):
        cache = self.cache.ensure()
        s = self.scale
        vertex = cache.nearest_vertex(xy, 14 * s, self.region, self.rv3d, self.bvh)
        picked = [vertex] if vertex is not None else []
        if not picked:
            edge = cache.nearest_edge(xy, 8 * s, self.region, self.rv3d, self.bvh)
            if edge is not None:
                picked = cache.edges[edge[0]].tolist()
        order = list(cache.order)
        if mode == 'set':
            order = picked
        elif mode == 'remove':
            order = [i for i in order if i not in picked]
        elif picked and all(p in order for p in picked):
            order = [i for i in order if i not in picked]
        else:
            order += [p for p in picked if p not in order]
        if order != list(cache.order):
            self.apply_selection(order)

    def box_select(self, a, b, mode):
        cache = self.cache.ensure()
        inside = cache.in_rect(np.asarray(a), np.asarray(b), self.region, self.rv3d, self.bvh)
        order = list(cache.order)
        if mode == 'set':
            order = inside
        elif mode == 'remove':
            drop = set(inside)
            order = [i for i in order if i not in drop]
        else:
            order += [i for i in inside if i not in set(order)]
        if order != list(cache.order):
            self.apply_selection(order)

    # --------------------------------------------------------------- tools
    def _pen_key(self, order):
        cache = self.cache.ensure()
        return cache.count, len(cache.faces), tuple(order)

    def pen_click(self, context, new_chain=False):
        cache = self.cache.ensure()
        order = [] if new_chain else list(cache.order)
        note = None
        if len(order) > 2 or (len(order) == 2 and not cache.has_edge(*order)):
            order = []
            note = 'Started a new chain: PolyPen extends one vertex or one edge'
        target = self.pen_target(self.mouse)
        if target is None:
            raise UserError('Click on the source surface')
        quad = bool(self.scene.rf_rs_pen_quads and len(order) == 2 and
                    self.pen_triangle == self._pen_key(order))
        faces_before = len(cache.faces)
        point = target['point']
        with self.transaction():
            if target['kind'] == 'edge':
                a, b = cache.edges[target['edge']].tolist()
                result = self.run(context, 'polypen', [point], {'edge': [a, b], 'width': 0.0}, order)
                if order:
                    split = result['selected'][0]
                    self.run(context, 'polypen', [point], {'vertex': split, 'width': 0.0}, order)
            else:
                extra = {'width': 0.0, 'quad': quad}
                if target['kind'] == 'vertex':
                    extra['vertex'] = target['vertex']
                self.run(context, 'polypen', [point], extra, order)
        cache = self.cache.ensure()
        made_triangle = (not quad and len(cache.faces) == faces_before + 1 and len(cache.order) == 2
                         and len(cache.faces[-1]) == 3)
        self.pen_triangle = self._pen_key(cache.order) if made_triangle else None
        self.hover = self.compute_hover()
        if note:
            self.toast(note)

    def patch_corner(self, context):
        cache = self.cache.ensure()
        vertex = cache.nearest_vertex(self.mouse, 12 * self.scale, self.region, self.rv3d, self.bvh)
        point = Vector(cache.world[vertex]) if vertex is not None else self._hit(self.mouse)
        if point is None:
            raise UserError('Click corners on the source surface')
        self.corners.append(point)
        if len(self.corners) == 4:
            corners, self.corners = self.corners, []
            self.mutate(context, 'patch', corners)
        self._redraw()

    def loop_click(self, context):
        hover = self.compute_hover()
        if hover is None:
            raise UserError('Click on an edge of a quad strip or ring')
        a, b = self.cache.edges[hover['edge']].tolist()
        self.mutate(context, 'loops', extra={'edge': [a, b], 'factor': hover['factor']})
        self.hover = self.compute_hover()

    def commit_stroke(self, context, g):
        if len(g.points) < 2:
            raise UserError('Draw a longer stroke on the surface')
        cache = self.cache.ensure()
        if self.tool == 'PolyStrips':
            width = self._pixels_to_world(self.scene.rf_rs_strip_px, g.points[0])
            self.mutate(context, 'polystrip', g.points, {'width': width})
            return
        boundary = None if g.separate else cache.selected_boundary()
        if boundary is not None:
            self.mutate(context, 'bridge', g.points, {'count': boundary[0], 'closed': boundary[1]})
            return
        screen = [Vector(p) for p in g.screen]
        length = sum((b - a).length for a, b in zip(screen, screen[1:]))
        closed = self.scene.rf_rs_closed or (
            len(screen) > 6 and (screen[0] - screen[-1]).length < 24 * self.scale
            and length > 120 * self.scale)
        count = max(self.scene.rf_rs_count, 3 if closed else 2)
        self.mutate(context, 'stroke', g.points, {'count': count, 'closed': closed}, selection=[])

    def _line_anchor(self, a, b):
        best = None
        for k in range(33):
            t = k / 32
            hit = self._hit((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            if hit is not None and (best is None or abs(t - 0.5) < best[0]):
                best = (abs(t - 0.5), hit)
        return None if best is None else best[1]

    def commit_line(self, context, g):
        a, b = Vector(g.screen[0]), Vector(g.screen[1])
        if (b - a).length < 6 * self.scale:
            raise UserError('Drag a longer line')
        anchor = self._line_anchor(a, b)
        if anchor is None:
            raise UserError('The line must cross the source surface')
        start = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, a, anchor)
        end = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, b, anchor)
        ray = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, (a + b) / 2)
        normal = (end - start).cross(ray)
        if normal.length < 1e-12:
            raise UserError('Drag a longer line')
        normal.normalize()
        plane = {'origin': list(anchor), 'normal': list(normal)}
        cache = self.cache.ensure()
        if self.tool == 'Contours':
            boundary = None if g.separate else cache.selected_boundary()
            if boundary is not None:
                try:
                    self.mutate(context, 'contour_bridge', [anchor], dict(plane, count=boundary[0]))
                    return
                except engine.EngineError as exc:
                    if 'match' not in str(exc):
                        raise
                    # e.g. a closed selected loop and an open section: still add the ring.
                    self.toast('Section does not match the selected boundary; added a separate ring')
            self.mutate(context, 'contour', [anchor], dict(plane, count=self.scene.rf_rs_count))
            return
        faces = cache.faces_crossing(np.asarray(a), np.asarray(b), self.region, self.rv3d, self.bvh)
        if not faces:
            raise UserError('The knife line must cross visible target faces')
        self.mutate(context, 'knife', extra=dict(plane, faces=faces))

    # -------------------------------------------------------------- brushes
    def brush_begin(self):
        if self.target.data.shape_keys is not None:
            raise UserError('Brushes do not support shape-key meshes')
        cache = self.cache.ensure()
        radius = self.scene.rf_rs_brush_radius * self.scale
        mask = cache.selected if self.scene.rf_rs_brush_selected else None
        self.history.begin()
        g = SimpleNamespace(kind=self.tool.lower(), moved=False, transaction=True, last=0.0,
                            start=self.mouse, radius=radius, mask=mask)
        self.gesture = g
        if g.kind == 'tweak':
            indices, distance = cache.in_circle(self.mouse, radius, self.region, self.rv3d, self.bvh, mask)
            g.indices = indices
            g.weights = falloff(distance / radius)
            g.origin = cache.world[indices].copy()
            g.anchor = Vector(cache.world[indices[np.argmin(distance)]]) if len(indices) else None
        else:
            self.relax_step()

    def _project(self, points):
        result = np.empty_like(points)
        for i, p in enumerate(points):
            nearest = self.bvh.find_nearest(Vector(p))[0]
            result[i] = nearest if nearest is not None else p
        return result

    def tweak_move(self):
        g = self.gesture
        if g.anchor is None or not len(g.indices):
            return
        start = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, Vector(g.start), g.anchor)
        now = view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, Vector(self.mouse), g.anchor)
        delta = np.asarray(now - start)
        moved = g.origin + g.weights[:, None] * delta
        self.cache.set_world(g.indices, self._project(moved))
        g.moved = True

    def relax_step(self):
        g = self.gesture
        g.last = time.monotonic()
        cache = self.cache
        indices, distance = cache.in_circle(self.mouse, g.radius, self.region, self.rv3d, self.bvh, g.mask)
        keep = ~cache.boundary[indices] if len(indices) else np.zeros(0, bool)
        indices, distance = indices[keep], distance[keep]
        if not len(indices):
            return
        weights = falloff(distance / g.radius) * self.scene.rf_rs_strength
        adjacency = cache.adjacency()
        world = cache.world
        moved = world[indices].copy()
        for k, i in enumerate(indices.tolist()):
            neighbours = adjacency[i]
            if len(neighbours):
                moved[k] += (world[neighbours].mean(axis=0) - world[i]) * weights[k]
        cache.set_world(indices, self._project(moved))
        g.moved = True
        self._redraw()

    # ------------------------------------------------------------- drawing
    def _draw_here(self):
        return (not self.finished and bpy.context.area == self.area and
                bpy.context.region == self.region)

    def _target_batches(self, cache):
        rv3d = self.rv3d
        key = (cache.version, tuple(tuple(r) for r in rv3d.view_matrix), rv3d.is_perspective)
        if key == self.batch_key:
            return self.batches
        self.batch_key = key
        world = cache.world
        inverse = rv3d.view_matrix.inverted()
        # Nudge toward the viewer so faces lying on the source win the depth test.
        if rv3d.is_perspective:
            offset = (np.asarray(inverse.translation) - world) * 0.002
        else:
            offset = np.asarray(inverse.col[2][:3]) * rv3d.view_distance * 0.002
        pos = (world + offset).tolist() if len(world) else []
        batches = {}
        if len(cache.tris):
            batches['faces'] = gfx.tri_batch(pos, indices=cache.tris.tolist())
        if len(cache.edges):
            both = cache.selected[cache.edges].all(axis=1)
            for name, mask in (('edges', ~both), ('edges_selected', both)):
                pairs = cache.edges[mask].ravel().tolist()
                if pairs:
                    batches[name] = gfx.line_batch([pos[i] for i in pairs])
        visible = ~cache.hidden
        for name, mask in (('verts', visible & ~cache.selected), ('verts_selected', visible & cache.selected)):
            ids = np.flatnonzero(mask).tolist()
            if ids:
                batches[name] = gfx.point_batch([pos[i] for i in ids])
        self.batches = batches
        return batches

    def draw_3d(self):
        if not self._draw_here() or self.render_failed:
            return
        try:
            cache = self.cache.ensure()
            px = bpy.context.preferences.system.pixel_size
            batches = self._target_batches(cache)
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_mask_set(False)
            # X-ray ghost first: flat faces cut through curved sources, so parts
            # of the retopology sit just under the surface. Keep them readable.
            gpu.state.depth_test_set('NONE')
            self._draw_target(batches, px, ghost=True)
            gpu.state.depth_test_set('LESS_EQUAL')
            self._draw_target(batches, px, ghost=False)
            gpu.state.depth_test_set('NONE')
            self._draw_previews(cache, px)
        except Exception as exc:
            # Never break Blender's draw loop; fall back to Blender's display.
            self.render_failed = True
            try:
                self.target.hide_set(False)
                self.target.show_in_front = True
            except Exception:
                pass
            self.toast(f'Overlay renderer unavailable ({exc}); using Blender display', 'error')
        finally:
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(True)
            gpu.state.blend_set('NONE')
            gpu.state.point_size_set(1.0)

    @staticmethod
    def _draw_target(batches, px, ghost):
        def tone(name, factor=0.3):
            r, g, b, a = COLORS[name]
            return (r, g, b, a * factor) if ghost else (r, g, b, a)
        if 'faces' in batches:
            gfx.draw_tris(batches['faces'], tone('face', 0.35))
        if 'edges' in batches:
            gfx.draw_lines(batches['edges'], tone('edge'), 1.5 * px)
        if 'edges_selected' in batches:
            gfx.draw_lines(batches['edges_selected'], tone('selected'), 2.5 * px)
        if 'verts' in batches:
            gfx.draw_points(batches['verts'], tone('vertex'), 6 * px)
        if 'verts_selected' in batches:
            gfx.draw_points(batches['verts_selected'], tone('selected'), 8 * px)

    def _draw_previews(self, cache, px):
        hover = self.hover
        world = cache.world
        if self.tool == 'PolyPen' and hover is not None and self.gesture is None:
            point = tuple(hover['point'])
            order = cache.order
            if len(order) <= 2:
                lines = []
                for i in order:
                    lines += [tuple(world[i]), point]
                if lines:
                    gfx.lines(lines, COLORS['preview'], 2 * px)
            if hover['kind'] == 'edge':
                a, b = cache.edges[hover['edge']].tolist()
                gfx.lines([tuple(world[a]), tuple(world[b])], COLORS['hover'], 3 * px)
            gfx.points([point], COLORS['hover'] if hover['kind'] == 'vertex' else COLORS['preview'],
                       (11 if hover['kind'] == 'vertex' else 8) * px)
        elif self.tool == 'Select' and hover is not None:
            if hover['kind'] == 'vertex':
                gfx.points([tuple(world[hover['vertex']])], COLORS['hover'], 10 * px)
            else:
                a, b = cache.edges[hover['edge']].tolist()
                gfx.lines([tuple(world[a]), tuple(world[b])], COLORS['hover'], 3 * px)
        elif self.tool == 'Loops' and hover is not None:
            a, b = cache.edges[hover['edge']].tolist()
            gfx.lines([tuple(world[a]), tuple(world[b])], COLORS['hover'], 3 * px)
            segments = [tuple(p) for pair in hover['ring'] for p in pair]
            gfx.lines(segments, COLORS['preview'], 2.5 * px)
        if self.corners:
            corners = [tuple(p) for p in self.corners]
            tail = [tuple(hover['point'])] if hover is not None and hover.get('point') is not None else []
            path = corners + tail
            if len(corners) == 3 and tail:
                path = path + corners[:1]
            gfx.lines(path, COLORS['preview'], 2 * px, strip=True)
            gfx.points(corners, COLORS['preview'], 9 * px)
        elif self.tool in ('Patches', 'Strokes', 'PolyStrips') and hover is not None and self.gesture is None:
            gfx.points([tuple(hover['point'])], COLORS['preview_soft'], 8 * px)

    def draw_2d(self):
        if not self._draw_here():
            return
        try:
            gpu.state.blend_set('ALPHA')
            s = self.scale
            g = self.gesture
            if g is not None and g.kind == 'stroke':
                gfx.lines(g.screen, COLORS['preview'], 2.5 * s, strip=True)
            elif g is not None and g.kind == 'line':
                gfx.lines(g.screen, COLORS['preview'], 2 * s)
                gfx.points(g.screen, COLORS['preview'], 7 * s)
            elif g is not None and g.kind == 'select':
                (x0, y0), (x1, y1) = g.start, self.mouse
                gfx.tris([(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)], (1, 1, 1, 0.06))
                gfx.lines([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], COLORS['box'], 1.0, strip=True)
            if self.tool in ('Tweak', 'Relax') and (self.hover_widget is None or g is not None):
                radius = self.scene.rf_rs_brush_radius * s
                x, y = self.mouse
                active = g is not None
                gfx.circle(x, y, radius, (1, 1, 1, 0.9 if active else 0.6), 1.5 * s,
                           fill=(0.3, 0.68, 1.0, 0.10 if active else 0.04))
            hud.draw(self.frame(), self.hover_widget)
        except Exception:
            pass   # the HUD must never break Blender's drawing
        finally:
            gpu.state.blend_set('NONE')


class RF_RS_OT_session(bpy.types.Operator):
    """Retopologize the active (or a new) mesh on top of the source in this viewport"""
    bl_idname = 'mesh.rf_rs_session'
    bl_label = 'Retopo RS Session'
    bl_options = {'UNDO', 'BLOCKING'}

    tool: EnumProperty(name='Tool', items=[(t, t, '') for t in TOOLS], default='Select')
    new_target: BoolProperty(name='Create New Target', default=False)

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        tool = self.tool if self.properties.is_property_set('tool') else context.scene.rf_rs_tool
        try:
            self._session = Session(context, tool, self.new_target, self.report)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        return self._session.modal(context, event)

    def cancel(self, context):
        # Blender-initiated cancellation (file load, window closed): keep work.
        self._session.finish(context, cancel=False)


CLASSES = (RF_RS_OT_session,)

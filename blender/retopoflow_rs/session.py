"""One viewport session around the native tools, not a replacement RF renderer.

The target is Blender's wire/in-front object. Preview lines are input gestures,
not reconstructed geometry. Selection currently picks vertices, not edges/brushes.
"""
import importlib

import bpy
import blf
import gpu
from bpy.props import BoolProperty, EnumProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.bvhtree import BVHTree

_SESSION = None
TOOLS = ('Contours', 'PolyStrips', 'Strokes', 'Patches', 'PolyPen',
         'Knife', 'Loops', 'Tweak', 'Relax', 'Select')
KEYS = dict(zip(('ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN',
                 'EIGHT', 'NINE', 'ZERO'), TOOLS))
OPERATIONS = {'PolyPen': 'polypen', 'Strokes': 'stroke', 'Contours': 'contour',
              'PolyStrips': 'polystrip', 'Patches': 'patch', 'Loops': 'loops',
              'Knife': 'knife', 'Relax': 'relax', 'Tweak': 'tweak'}
OPTIONS = {
    'Select': (), 'PolyPen': ('width',),
    'Strokes': ('count', 'closed'), 'Contours': ('count',),
    'PolyStrips': ('count', 'width'), 'Patches': ('count', 'rows'),
    'Loops': ('factor',), 'Knife': (),
    'Relax': ('strength', 'iterations'), 'Tweak': (),
}

HELP = {
    'Select': 'Click vertex; Shift-click toggles | A all / Alt-A none | Ctrl-I invert | Ctrl-L linked',
    'PolyPen': 'Click to insert/snap; near an edge splits it | Shift-click selects | each click has its own undo',
    'Strokes': 'Drag a stroke; release to commit | matching selected boundary extrudes quads',
    'Contours': 'Drag a section plane; release to commit | matching selected boundary bridges',
    'PolyStrips': 'Drag a centerline; release creates a ribbon | Width controls rail separation',
    'Patches': 'Click four corners in perimeter order | F fills a matching selected boundary',
    'Loops': 'Click target edge to insert a quad ring | Factor controls the split position',
    'Knife': 'Drag a line; release cuts by an infinite view-perpendicular plane',
    'Relax': 'Shift-click to select vertices; click to smooth selected interior vertices',
    'Tweak': 'Shift-click to select vertices; drag to translate and project selection',
}


def adapter():
    return importlib.import_module(__package__)


def active_session():
    return _SESSION


def shutdown(context):
    session = active_session()
    if session is not None:
        session._finish(context, False)


def _discard(mesh):
    if mesh is not None:
        try:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except ReferenceError:
            pass


class SessionHistory:
    """Owned mesh snapshots and ordered selection; no Blender global undo calls.

    begin/commit/rollback also make failed native operations transactional.
    Snapshots are never installed directly: history retains ownership of them.
    """
    def __init__(self, target, limit=32):
        self.target = target
        self.limit = limit
        self.undo_stack = []
        self.redo_stack = []
        self.pending = None
        self.entry = self.capture()

    def capture(self):
        return self.target.data.copy(), list(adapter()._selected(self.target))

    def restore(self, state):
        mesh, selected = state
        old = self.target.data
        self.target.data = mesh.copy()
        adapter()._set_selection(self.target.data, selected)
        adapter()._remember_selection(self.target, selected)
        _discard(old)

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
            _discard(self.undo_stack.pop(0)[0])

    def rollback(self):
        if self.pending is not None:
            state, self.pending = self.pending, None
            try:
                self.restore(state)
            finally:
                _discard(state[0])

    def step(self, redo=False):
        self.rollback()
        source, destination = ((self.redo_stack, self.undo_stack) if redo else
                               (self.undo_stack, self.redo_stack))
        if not source:
            return False
        current = self.capture()
        state = source[-1]
        try:
            self.restore(state)
        except Exception:
            _discard(current[0])
            raise
        source.pop()
        destination.append(current)
        _discard(state[0])
        return True

    @staticmethod
    def clear_stack(stack):
        for mesh, _ in stack:
            _discard(mesh)
        stack.clear()

    def close(self, cancel=False):
        try:
            if cancel:
                self.restore(self.entry)
        finally:
            if self.pending is not None:
                _discard(self.pending[0])
                self.pending = None
            self.clear_stack(self.undo_stack)
            self.clear_stack(self.redo_stack)
            _discard(self.entry[0])


class RF_RS_OT_session(bpy.types.Operator):
    bl_idname = 'mesh.rf_rs_session'
    bl_label = 'Retopo RS Session'
    bl_options = {'UNDO', 'BLOCKING'}

    tool: EnumProperty(name='Tool', items=[(t, t, '') for t in TOOLS], default='Select')
    new_target: BoolProperty(name='Create New Target', default=False)

    def _snapshot(self, owner, name, value):
        if hasattr(owner, name):
            previous = getattr(owner, name)
            self._settings.append((owner, name, previous))
            # Region visibility setters can toggle even when assigned their
            # current value; avoid redundant writes on entry and restoration.
            if previous != value:
                setattr(owner, name, value)

    def _restore_settings(self):
        for owner, name, value in reversed(self._settings):
            try:
                if getattr(owner, name) != value:
                    setattr(owner, name, value)
            except (ReferenceError, RuntimeError, AttributeError):
                pass
        self._settings.clear()

    def invoke(self, context, event):
        global _SESSION
        if active_session() is not None:
            self.report({'WARNING'}, 'A Retopo RS session is already active')
            return {'CANCELLED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Start in a 3D viewport')
            return {'CANCELLED'}
        obj = context.active_object
        if obj is None or obj.type != 'MESH' or context.mode not in {'OBJECT', 'EDIT_MESH'}:
            self.report({'ERROR'}, 'Start with an active mesh in Object or Edit Mode')
            return {'CANCELLED'}
        self._settings = []
        self._handles = []
        self._source_handle = None
        self._history = None
        self._finished = False
        self._created = None
        self._target = None
        self._area = context.area
        self._space = context.space_data
        self._workspace = context.workspace
        self._window = context.window
        self._scene = context.scene
        self._view_layer = context.view_layer
        self._original_active = obj
        self._original_selected = list(context.selected_objects)
        self._original_mode = context.mode
        self._original_source = context.scene.rf_rs_source
        self._points, self._screen = [], []
        self._hover = (0, 0)
        self._drawing = False
        self._region = next((r for r in self._area.regions if r.type == 'WINDOW'), None)
        self._rv3d = self._space.region_3d
        self._message = ''
        _SESSION = self
        try:
            if obj.library or obj.data.library:
                raise RuntimeError('Target must be editable')
            if self._region is None:
                raise RuntimeError('Viewport has no window region')
            if context.mode == 'EDIT_MESH':
                bpy.ops.object.mode_set(mode='OBJECT')
            visible_sources = [o for o in context.visible_objects
                               if o != obj and o.type == 'MESH' and (len(o.data.polygons) or o.modifiers)]
            explicit_source = self._original_source
            # The Source picker defines a source, never the target. A lone
            # polygon mesh launched in Object Mode is also a new-target entry.
            create_target = (self.new_target or obj == explicit_source or
                             (self._original_mode == 'OBJECT' and len(obj.data.polygons)
                              and not visible_sources and explicit_source is None))
            if create_target:
                mesh = bpy.data.meshes.new('Retopo Target')
                self._created = bpy.data.objects.new('Retopo Target', mesh)
                context.collection.objects.link(self._created)
                self._target = self._created
            else:
                self._target = obj
            sources = [o for o in context.visible_objects
                       if o != self._target and o.type == 'MESH' and (len(o.data.polygons) or o.modifiers)]
            pointer = self._original_source
            if pointer is not None and pointer != self._target and pointer.type == 'MESH' and pointer not in sources:
                sources.append(pointer)  # An explicitly chosen source may be hidden.
            if not sources:
                raise RuntimeError('No source mesh for the active target. Select the high-poly object '
                                   'in the Source picker, or select it in Object Mode and use + to create a target.')
            vertices, triangles, usable, warnings = [], [], [], []
            skipped = 0
            for source in sources:
                try:
                    data = adapter()._source_geometry(context, source)
                except RuntimeError as exc:
                    if source == pointer:
                        raise
                    warnings.append(str(exc))
                    continue
                usable.append(source)
                skipped += data.get('skipped_triangles', 0)
                offset = len(vertices)
                vertices.extend(data['vertices'])
                triangles.extend([[i + offset for i in tri] for tri in data['triangles']])
            if not triangles:
                raise RuntimeError('No usable source surfaces. ' + '; '.join(warnings))
            self._sources = tuple(usable)
            if skipped or warnings:
                self._message = f'Source cleanup: skipped {skipped} zero-area triangles; excluded {len(warnings)} unusable meshes'
                self.report({'WARNING'}, self._message)
            sources = usable
            self._source = {'vertices': vertices, 'triangles': triangles}
            self._bvh = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
            self._source_handle = adapter()._cache_source(self._source)
            self._history = SessionHistory(self._target)
            # Only alter viewport state once all sources and the native cache exist.
            self._scene.rf_rs_source = pointer if pointer in sources else sources[0]
            for selected in context.selected_objects:
                selected.select_set(False)
            self._target.select_set(True)
            context.view_layer.objects.active = self._target
            self._snapshot(self._target, 'display_type', 'WIRE')
            self._snapshot(self._target, 'show_in_front', True)
            self._snapshot(self._space.overlay, 'show_overlays', True)
            self._snapshot(self._space, 'show_gizmo', False)
            for name in ('show_region_ui', 'show_region_toolbar', 'show_region_tool_header'):
                self._snapshot(self._space, name, False)
            self._handles.append(bpy.types.SpaceView3D.draw_handler_add(
                self._draw, (), 'WINDOW', 'POST_PIXEL'))
            self._handles.append(bpy.types.SpaceView3D.draw_handler_add(
                self._draw_vertices, (), 'WINDOW', 'POST_VIEW'))
            self._workspace.status_text_set(
                'Retopo RS | 1–0 tools | Esc finish | Shift Esc discard session | exit before saving')
            context.window_manager.modal_handler_add(self)
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            self._finish(context, True)
            return {'CANCELLED'}

    def _finish(self, context, cancel=False):
        global _SESSION
        if getattr(self, '_finished', True):
            return
        self._finished = True
        try:
            if self._history is not None:
                try:
                    self._history.close(cancel)
                except (ReferenceError, RuntimeError):
                    pass
            if cancel and self._created is not None:
                try:
                    mesh = self._created.data
                    bpy.data.objects.remove(self._created, do_unlink=True)
                    _discard(mesh)
                except ReferenceError:
                    pass
        finally:
            for handle in self._handles:
                try:
                    bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
                except (ValueError, ReferenceError):
                    pass
            self._handles.clear()
            if self._source_handle:
                try:
                    adapter()._load_library().rf_source_free(self._source_handle)
                except Exception:
                    pass
                finally:
                    self._source_handle = None
            self._restore_settings()
            try:
                self._scene.rf_rs_source = self._original_source
            except (ReferenceError, RuntimeError):
                pass
            try:
                for obj in self._view_layer.objects:
                    obj.select_set(False)
                if not cancel and self._created is not None:
                    # New-target sessions finish on the retopology, not the source.
                    self._target.select_set(True)
                    self._view_layer.objects.active = self._target
                    return_to_edit = True
                else:
                    for obj in self._original_selected:
                        try:
                            obj.select_set(True)
                        except (ReferenceError, RuntimeError):
                            pass
                    self._view_layer.objects.active = self._original_active
                    return_to_edit = self._original_mode == 'EDIT_MESH' and self._original_active is not None
                if return_to_edit:
                    with context.temp_override(window=self._window, area=self._area):
                        if context.mode == 'OBJECT':
                            bpy.ops.object.mode_set(mode='EDIT')
            except (ReferenceError, RuntimeError, TypeError):
                pass
            try:
                self._workspace.status_text_set(None)
                self._area.tag_redraw()
            except ReferenceError:
                pass
            if _SESSION is self:
                _SESSION = None

    def cancel(self, context):
        self._finish(context, True)

    def _inside(self, event):
        return (self._region.x <= event.mouse_x < self._region.x + self._region.width and
                self._region.y <= event.mouse_y < self._region.y + self._region.height)

    def _coordinate(self, event):
        return event.mouse_x - self._region.x, event.mouse_y - self._region.y

    def _hit(self, context, event):
        return adapter().RF_RS_OT_draw._hit(self, context, event)

    def _view_point(self, coordinate):
        return adapter().RF_RS_OT_draw._view_point(self, coordinate)

    def _edge(self, event):
        return adapter().RF_RS_OT_draw._edge(self, event)

    def _reset_gesture(self):
        self._drawing = False
        self._points.clear()
        self._screen.clear()

    def _abort_gesture(self):
        self._history.rollback()
        self._reset_gesture()

    def _mutate(self, context, operation, points=(), extra=None):
        self._history.begin()
        try:
            adapter()._execute(context, operation, points, self._target,
                               self._source, extra, self._source_handle)
        except Exception:
            self._history.rollback()
            raise
        else:
            self._history.commit()
        finally:
            self._reset_gesture()

    def _select(self, context, event):
        cursor = Vector(self._coordinate(event))
        best = None
        for vertex in self._target.data.vertices:
            if vertex.hide:
                continue
            screen = view3d_utils.location_3d_to_region_2d(
                self._region, self._rv3d, self._target.matrix_world @ vertex.co)
            if screen is not None:
                candidate = ((screen-cursor).length, vertex.index)
                if candidate[0] <= 16 and (best is None or candidate < best):
                    best = candidate
        selected = adapter()._selected(self._target) if event.shift else []
        if best:
            if best[1] in selected:
                selected.remove(best[1])
            else:
                selected.append(best[1])
        self._history.begin()
        try:
            if self._target.data.users > 1:
                self._target.data = self._target.data.copy()
            adapter()._set_selection(self._target.data, selected)
            adapter()._remember_selection(self._target, selected)
            self._history.commit()
        except Exception:
            self._history.rollback()
            raise
        context.view_layer.update()

    def _boundary(self, closed):
        selected = set(adapter()._selected(self._target))
        if not selected:
            return False
        uses = {}
        for face in self._target.data.polygons:
            indices = list(face.vertices)
            for a, b in zip(indices, indices[1:] + indices[:1]):
                key = tuple(sorted((a, b)))
                uses[key] = uses.get(key, 0) + 1
        edges = [tuple(e.vertices) for e in self._target.data.edges
                 if all(i in selected for i in e.vertices) and
                 uses.get(tuple(sorted(e.vertices)), 0) <= 1]
        degrees = {i: [] for i in selected}
        for a, b in edges:
            degrees[a].append(b)
            degrees[b].append(a)
        # Native count is vertices/samples for both open chains and loops.
        expected = self._scene.rf_rs_count
        if len(selected) != expected or not edges:
            return False
        if closed:
            valid = all(len(n) == 2 for n in degrees.values())
        else:
            valid = (sum(len(n) == 1 for n in degrees.values()) == 2 and
                     all(len(n) in (1, 2) for n in degrees.values()))
        seen, todo = set(), [next(iter(selected))]
        while todo:
            i = todo.pop()
            if i not in seen:
                seen.add(i)
                todo.extend(degrees[i])
        return valid and seen == selected

    def _option_rows(self):
        return [(name, self._region.width - 250, self._region.height - 65 - i*27)
                for i, name in enumerate(OPTIONS[self.tool])]

    def _ui(self, event):
        x, y = self._coordinate(event)
        press = event.type == 'LEFTMOUSE' and event.value == 'PRESS'
        wheel = event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}
        if self._points or self._drawing:
            return False
        if 12 <= x <= 180 and self._region.height-52-10*28 <= y <= self._region.height-48:
            if press:
                index = int((self._region.height-48-y)//28)
                if 0 <= index < len(TOOLS):
                    self.tool = TOOLS[index]
            return True
        for name, left, baseline in self._option_rows():
            if left <= x <= self._region.width-12 and baseline-6 <= y <= baseline+20:
                if press or wheel:
                    prop = 'rf_rs_' + name
                    value = getattr(self._scene, prop)
                    direction = (1 if event.type == 'WHEELUPMOUSE' else -1) if wheel else (
                        1 if x > self._region.width-45 else -1)
                    step = .05 if name in {'strength', 'factor'} else (.01 if name == 'width' else 1)
                    setattr(self._scene, prop, not value if name == 'closed' else value + direction*step)
                return True
        return False

    def modal(self, context, event):
        if self._finished:
            return {'FINISHED'}
        try:
            if (context.area != self._area or self._area.type != 'VIEW_3D' or
                    context.active_object != self._target or context.mode != 'OBJECT'):
                raise RuntimeError('Session context changed; ending session')
            # Block global saves and Blender undo even when the pointer is outside.
            if event.ctrl and event.type == 'S':
                self._message = 'Exit the session before saving (temporary viewport state).'
                self._area.tag_redraw()
                return {'RUNNING_MODAL'}
            press = event.value == 'PRESS'
            if press and event.type == 'ESC' and event.shift:
                self._finish(context, True)
                return {'CANCELLED'}
            if press and event.type in {'ESC', 'RIGHTMOUSE'}:
                if self._points or self._drawing:
                    self._abort_gesture()
                else:
                    self._finish(context)
                    return {'FINISHED'}
                return {'RUNNING_MODAL'}
            if event.ctrl and event.type in {'Z', 'Y'}:
                if press:
                    self._abort_gesture()
                    self._history.step(event.type == 'Y' or event.shift)
                    context.view_layer.update()
                    self._area.tag_redraw()
                return {'RUNNING_MODAL'}
            if not self._inside(event):
                if self._drawing and event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    self._abort_gesture()
                return {'PASS_THROUGH'}
            self._hover = self._coordinate(event)
            self._area.tag_redraw()
            if self._ui(event):
                return {'RUNNING_MODAL'}
            idle = not self._points and not self._drawing
            if idle and press:
                if event.type in KEYS:
                    self.tool = KEYS[event.type]
                    return {'RUNNING_MODAL'}
                if event.type == 'W':
                    self.tool = 'Select'
                    return {'RUNNING_MODAL'}
                if event.type == 'Q':
                    self.tool = TOOLS[(TOOLS.index(self.tool)+1) % len(TOOLS)]
                    return {'RUNNING_MODAL'}
                if event.type in {'RET', 'NUMPAD_ENTER', 'TAB'}:
                    self._finish(context)
                    return {'FINISHED'}
                if event.type == 'A':
                    self._mutate(context, 'select_none' if event.alt else 'select_all')
                    return {'RUNNING_MODAL'}
                shortcuts = {'I': 'select_invert', 'L': 'select_connected'}
                operation = shortcuts.get(event.type) if event.ctrl else {
                    'M': 'merge', 'F': 'fill_patch'}.get(event.type)
                if operation:
                    self._mutate(context, operation)
                    return {'RUNNING_MODAL'}
                if event.type in {'X', 'DEL'}:
                    self._mutate(context, 'delete')
                    return {'RUNNING_MODAL'}
            if (event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                               'TRACKPADPAN', 'TRACKPADZOOM', 'NDOF_MOTION'} or event.alt):
                return {'PASS_THROUGH'} if idle else {'RUNNING_MODAL'}
            left_press = event.type == 'LEFTMOUSE' and press
            release = event.type == 'LEFTMOUSE' and event.value == 'RELEASE'
            if self.tool == 'Select' or (idle and left_press and event.shift):
                if left_press:
                    self._select(context, event)
                return {'RUNNING_MODAL'}
            operation = OPERATIONS[self.tool]
            if left_press:
                if operation == 'loops':
                    self._mutate(context, operation, extra={'edge': self._edge(event)})
                    return {'RUNNING_MODAL'}
                if operation == 'relax':
                    if not adapter()._selected(self._target):
                        raise ValueError('Select vertices before relaxing')
                    self._mutate(context, operation)
                    return {'RUNNING_MODAL'}
                hit = self._hit(context, event)
                if hit is None:
                    raise ValueError('Start on the source surface')
                self._anchor = hit.copy()
                self._points.append(hit.copy())
                self._screen.append(self._coordinate(event))
                if operation == 'polypen':
                    extra = {}
                    try:
                        extra['edge'] = self._edge(event)
                    except RuntimeError:
                        pass
                    self._mutate(context, operation, list(self._points), extra)
                elif operation == 'patch':
                    if len(self._points) == 4:
                        self._mutate(context, operation, list(self._points))
                else:
                    self._drawing = True
            elif self._drawing and event.type in {'MOUSEMOVE', 'LEFTMOUSE'}:
                extra = {}
                if operation in {'stroke', 'polystrip'}:
                    hit = self._hit(context, event)
                    if hit is not None and (hit-self._points[-1]).length > 1e-6:
                        self._points.append(hit.copy())
                        self._screen.append(self._coordinate(event))
                else:
                    self._screen = [self._screen[0], self._coordinate(event)]
                if release:
                    points = list(self._points)
                    if operation in {'knife', 'contour', 'tweak'}:
                        start, end = (self._view_point(p) for p in (self._screen[0], self._screen[-1]))
                        delta = end-start
                        if delta.length < 1e-6:
                            raise ValueError('Draw a nonzero segment')
                        if operation == 'tweak':
                            if not adapter()._selected(self._target):
                                raise ValueError('Select vertices before tweaking')
                            points = [delta]
                        else:
                            view = view3d_utils.region_2d_to_vector_3d(
                                self._region, self._rv3d, self._screen[0])
                            extra = {'origin': list(start), 'normal': list(delta.cross(view).normalized())}
                    elif len(points) < 2:
                        raise ValueError('Draw at least two surface points')
                    closed = self._scene.rf_rs_closed
                    if operation == 'stroke' and self._boundary(closed):
                        operation = 'bridge'
                    elif operation == 'contour' and (self._boundary(True) or self._boundary(False)):
                        operation = 'contour_bridge'
                    self._mutate(context, operation, points, extra)
            return {'RUNNING_MODAL'}
        except (ValueError, RuntimeError) as exc:
            self._message = str(exc)
            self.report({'WARNING'}, str(exc))
            try:
                self._abort_gesture()
                if (context.active_object == self._target and context.area == self._area
                        and self._area.type == 'VIEW_3D' and context.mode == 'OBJECT'):
                    return {'RUNNING_MODAL'}
            except (ReferenceError, RuntimeError):
                pass
            self._finish(context)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            self._finish(context)
            return {'FINISHED'}

    def _draw_here(self):
        return (not self._finished and bpy.context.area == self._area and
                bpy.context.region == self._region)

    @staticmethod
    def _text(x, y, text, color=(.9, .93, .96, 1), size=14):
        try:
            blf.size(0, size)
        except TypeError:
            blf.size(0, size, 72)
        blf.position(0, x, y, 0)
        blf.color(0, *color)
        blf.draw(0, text)

    @staticmethod
    def _batch(kind, points, color):
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        except ValueError:
            shader = gpu.shader.from_builtin(
                '2D_UNIFORM_COLOR' if len(points[0]) == 2 else '3D_UNIFORM_COLOR')
        batch = batch_for_shader(shader, kind, {'pos': points})
        shader.bind()
        shader.uniform_float('color', color)
        batch.draw(shader)

    def _draw(self):
        if not self._draw_here():
            return
        try:
            h, w = self._region.height, self._region.width
            gpu.state.blend_set('ALPHA')
            self._batch('TRIS', [(8, h-340), (184, h-340), (184, h-16),
                                  (8, h-340), (184, h-16), (8, h-16)], (.025, .035, .045, .88))
            self._text(18, h-35, 'RETOPO RS', size=16)
            for i, tool in enumerate(TOOLS):
                y = h-68-i*28
                self._text(20, y, f'{(i+1)%10}  {tool}',
                           (.2, .85, 1, 1) if tool == self.tool else (.8, .82, .85, 1))
            self._text(w-250, h-35, self.tool + ' options')
            for name, x, y in self._option_rows():
                value = getattr(self._scene, 'rf_rs_' + name)
                formatted = f'{value:.3g}' if isinstance(value, float) else str(value)
                self._text(x, y, f'{name.title()}: {formatted}   − / +')
            mesh = self._target.data
            self._text(18, 65, f'Verts {len(mesh.vertices)}   Edges {len(mesh.edges)}   Faces {len(mesh.polygons)}'
                       f'   Selected {len(adapter()._selected(self._target))}')
            self._text(18, 87, HELP[self.tool], size=12)
            self._text(18, 44, 'Q cycles (no pie) | Ctrl-Z / Shift-Ctrl-Z undo/redo | M merge | X delete', size=12)
            self._text(18, 26, 'Esc: cancel gesture / finish idle | Shift-Esc: discard session | Tab/Enter idle: finish | exit before save', size=12)
            self._text(195, h-24, self._message or f'Target: {self._target.name} | Sources: {len(self._sources)} | input-stroke preview', size=12)
            points = list(self._screen)
            if self.tool == 'Patches' and points:
                points += [self._hover, points[0]]
            if len(points) > 1:
                self._batch('LINE_STRIP', points, (.2, .85, 1, 1))
        except Exception:
            # Draw callbacks must not break Blender's drawing loop.
            self._message = 'Overlay unavailable; Esc exits safely'
        finally:
            gpu.state.blend_set('NONE')

    def _draw_vertices(self):
        if not self._draw_here():
            return
        try:
            selected = adapter()._selected(self._target)
            points = [self._target.matrix_world @ self._target.data.vertices[i].co for i in selected]
            if points:
                gpu.state.blend_set('ALPHA')
                gpu.state.point_size_set(6)
                self._batch('POINTS', points, (1, .55, .12, 1))
        except Exception:
            pass
        finally:
            gpu.state.point_size_set(1)
            gpu.state.blend_set('NONE')


CLASSES = (RF_RS_OT_session,)

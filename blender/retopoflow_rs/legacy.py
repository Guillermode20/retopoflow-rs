"""Standalone one-shot operators (pre-session workflow), kept for scripting.

The viewport session in ``session.py`` is the primary interface; these remain
for batch use from Python and are hidden in the sidebar behind a toggle.
"""
import bpy
import gpu
from bpy.props import BoolProperty, EnumProperty
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_point_line

from . import engine

_draw_sessions = set()


def shutdown(context):
    for legacy_session in tuple(_draw_sessions):
        legacy_session._finish(context)


class RF_RS_OT_new_target(bpy.types.Operator):
    """Create an empty retopology target mesh and make it active"""
    bl_idname = 'mesh.rf_rs_new_target'
    bl_label = 'New Retopo Target'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, 'Switch to Object Mode first')
            return {'CANCELLED'}
        source = context.scene.rf_rs_source
        if source is None:
            source = context.active_object
            if source is None or source.type != 'MESH':
                self.report({'ERROR'}, 'Select a source mesh first')
                return {'CANCELLED'}
            context.scene.rf_rs_source = source
        mesh = bpy.data.meshes.new('Retopo Target')
        target = bpy.data.objects.new('Retopo Target', mesh)
        context.collection.objects.link(target)
        target.show_in_front = True
        target.display_type = 'WIRE'
        target.rf_rs_is_target = True
        context.scene.rf_rs_target = target
        for obj in context.selected_objects:
            obj.select_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
        return {'FINISHED'}


class RF_RS_OT_select(bpy.types.Operator):
    """Click a target vertex to select it (Shift toggles)"""
    bl_idname = 'mesh.rf_rs_select'
    bl_label = 'Select Target Vertex'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        try:
            self._target, source = engine.objects(context)
            self._bvh = None
            if not self._target.show_in_front:
                data = engine.source_geometry(context, source)
                self._bvh = BVHTree.FromPolygons(data['vertices'], data['triangles'], all_triangles=True)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self._area = context.area
        self._region = next(r for r in context.area.regions if r.type == 'WINDOW')
        self._rv3d = context.space_data.region_3d
        context.workspace.status_text_set('Click vertex (16 px). Shift: toggle. Esc/right-click: cancel')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
            return {'PASS_THROUGH'}
        if not (self._region.x <= event.mouse_x < self._region.x + self._region.width and
                self._region.y <= event.mouse_y < self._region.y + self._region.height):
            return {'RUNNING_MODAL'}
        try:
            if context.active_object != self._target or context.mode != 'OBJECT':
                raise RuntimeError('Active target changed')
            cursor = Vector((event.mouse_x - self._region.x, event.mouse_y - self._region.y))
            best = None
            for vertex in self._target.data.vertices:
                if vertex.hide:
                    continue
                world = self._target.matrix_world @ vertex.co
                screen = view3d_utils.location_3d_to_region_2d(self._region, self._rv3d, world)
                if screen is None or (screen - cursor).length > 16:
                    continue
                if self._bvh is not None:
                    origin = view3d_utils.region_2d_to_origin_3d(self._region, self._rv3d, screen)
                    delta = world - origin
                    if delta.length > 1e-8:
                        hit = self._bvh.ray_cast(origin, delta.normalized(), max(0.0, delta.length - 1e-4))[0]
                        if hit is not None:
                            continue
                candidate = ((screen - cursor).length, vertex.index)
                if best is None or candidate < best:
                    best = candidate
            selected = engine.selected(self._target) if event.shift else []
            if best is not None:
                index = best[1]
                if index in selected:
                    selected.remove(index)
                else:
                    selected.append(index)
            if self._target.data.users > 1:
                self._target.data = self._target.data.copy()
            engine.set_selection(self._target.data, selected)
            engine.remember_selection(self._target, selected)
            context.view_layer.update()
            self._area.tag_redraw()
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            self.cancel(context)
            return {'CANCELLED'}
        self.cancel(context)
        return {'FINISHED'}

    def cancel(self, context):
        context.workspace.status_text_set(None)


class RF_RS_OT_execute(bpy.types.Operator):
    """Run one Rust retopology operation on the active target"""
    bl_idname = 'mesh.rf_rs_execute'
    bl_label = 'Rust Retopology Operation'
    bl_options = {'REGISTER', 'UNDO'}
    operation: EnumProperty(items=[(name, name.replace('_', ' ').title(), '') for name in
                                   ('project', 'relax', 'delete', 'face', 'merge', 'fill_patch', 'fill_auto',
                                    'select_all', 'select_none', 'select_invert', 'select_connected',
                                    'select_path')])

    def execute(self, context):
        try:
            engine.execute(context, self.operation)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class RF_RS_OT_draw(bpy.types.Operator):
    """One-shot drawing gesture on the source (legacy; prefer the session)"""
    bl_idname = 'mesh.rf_rs_draw'
    bl_label = 'Draw on Source'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    operation: EnumProperty(items=[(name, label, '') for name, label in (
        ('stroke', 'Stroke'), ('bridge', 'Bridge boundary with stroke'),
        ('polystrip', 'Ribbon strip'), ('patch', 'Four-corner patch'),
        ('contour', 'Plane contour'), ('contour_bridge', 'Contour from boundary'),
        ('loops', 'Insert edge loop'), ('knife', 'Plane cut'),
        ('tweak', 'Translate selected and project'), ('polypen', 'PolyPen point'),
        ('add_vertex', 'Add vertex'))])

    persistent: BoolProperty(name='Repeat PolyPen', default=False)

    def _finish(self, context):
        source_handle = getattr(self, '_source_handle', None)
        if source_handle:
            engine.free_source(source_handle)
            self._source_handle = None
        handle = getattr(self, '_handle', None)
        if handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            self._handle = None
        _draw_sessions.discard(self)
        if getattr(self, '_cursor', False):
            context.window.cursor_modal_restore()
            self._cursor = False
        context.workspace.status_text_set(None)
        if getattr(self, '_area', None):
            self._area.tag_redraw()

    def _inside(self, event):
        return (self._region.x <= event.mouse_x < self._region.x + self._region.width and
                self._region.y <= event.mouse_y < self._region.y + self._region.height)

    def _coordinate(self, event):
        return (event.mouse_x - self._region.x, event.mouse_y - self._region.y)

    def _preview(self):
        if bpy.context.area != self._area or bpy.context.region != self._region:
            return
        points = self._screen[:]
        if self.operation == 'patch' and points:
            points += [self._hover, points[0]]
        if len(points) < 2:
            return
        try:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        except ValueError:  # Blender 3.6 used dimension-prefixed shader names.
            shader = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': points})
        gpu.state.blend_set('ALPHA')
        try:
            shader.bind()
            shader.uniform_float('color', (0.2, 0.85, 1.0, 0.9))
            batch.draw(shader)
        finally:
            gpu.state.blend_set('NONE')

    def _view_point(self, coordinate):
        return view3d_utils.region_2d_to_location_3d(self._region, self._rv3d, coordinate, self._anchor)

    def _edge(self, event):
        cursor = Vector(self._coordinate(event))
        best = None
        for edge in self._target.data.edges:
            ends = [view3d_utils.location_3d_to_region_2d(
                self._region, self._rv3d, self._target.matrix_world @ self._target.data.vertices[i].co)
                for i in edge.vertices]
            if any(p is None for p in ends):
                continue
            a, b = ends
            if (a - b).length_squared < 1e-12:
                continue
            _, factor = intersect_point_line(cursor, a, b)
            distance = (cursor - a.lerp(b, max(0.0, min(1.0, factor)))).length
            if best is None or distance < best[0]:
                best = (distance, list(edge.vertices))
        if best is None or best[0] > 24:
            raise RuntimeError('Click within 24 pixels of a target edge')
        return best[1]

    def _hit(self, context, event):
        coordinate = self._coordinate(event)
        origin = view3d_utils.region_2d_to_origin_3d(self._region, self._rv3d, coordinate)
        direction = view3d_utils.region_2d_to_vector_3d(self._region, self._rv3d, coordinate)
        return self._bvh.ray_cast(origin, direction)[0]

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Run in a 3D viewport')
            return {'CANCELLED'}
        self._area = context.area
        self._region = next(r for r in context.area.regions if r.type == 'WINDOW')
        self._rv3d = context.space_data.region_3d
        try:
            self._target, source = engine.objects(context)
            self._source = engine.source_geometry(context, source)
            self._bvh = BVHTree.FromPolygons(self._source['vertices'], self._source['triangles'],
                                             all_triangles=True)
            self._source_handle = engine.cache_source(self._source)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self._commits = 0
        self._points = []
        self._screen = []
        self._hover = (0, 0)
        self._drawing = False
        self._handle = None
        self._cursor = False
        try:
            context.window.cursor_modal_set('CROSSHAIR')
            self._cursor = True
            self._handle = bpy.types.SpaceView3D.draw_handler_add(self._preview, (), 'WINDOW', 'POST_PIXEL')
            _draw_sessions.add(self)
            context.workspace.status_text_set(
                'PolyPen: click repeatedly. Enter/Esc/right-click: finish; one session undo'
                if self.persistent and self.operation == 'polypen' else
                'Patch: four corner clicks. Other tools: click/drag on source. Esc/right-click: cancel')
            context.window_manager.modal_handler_add(self)
        except Exception as exc:
            self._finish(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} or (event.type in {'RET', 'NUMPAD_ENTER'} and self.persistent):
            self._finish(context)
            return {'FINISHED'} if getattr(self, '_commits', 0) else {'CANCELLED'}
        try:
            if context.area != self._area:
                raise RuntimeError('Viewport changed during drawing')
            if not self._inside(event):
                if self._drawing and event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    raise RuntimeError('Drag ended outside viewport; cancelled')
                return {'RUNNING_MODAL'} if self._drawing else {'PASS_THROUGH'}
            if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN',
                              'TRACKPADZOOM', 'NDOF_MOTION'} or event.alt:
                return {'RUNNING_MODAL'} if self._drawing else {'PASS_THROUGH'}
            self._hover = self._coordinate(event)
            self._area.tag_redraw()
            press = event.type == 'LEFTMOUSE' and event.value == 'PRESS'
            release = event.type == 'LEFTMOUSE' and event.value == 'RELEASE'
            extra = {}
            commit = False
            if press:
                if self.operation == 'loops':
                    extra['edge'] = self._edge(event)
                    commit = True
                else:
                    hit = self._hit(context, event)
                    if hit is None:
                        raise RuntimeError('Start on the source surface')
                    self._points.append(hit.copy())
                    self._screen.append(self._coordinate(event))
                    if self.operation == 'polypen':
                        try:
                            extra['edge'] = self._edge(event)
                        except RuntimeError:
                            pass  # No nearby target edge: native add/snap behavior.
                    self._anchor = hit.copy()
                    self._drawing = self.operation not in {'patch', 'add_vertex', 'polypen'}
                    commit = (self.operation in {'add_vertex', 'polypen'} or
                              (self.operation == 'patch' and len(self._points) == 4))
            if self._drawing and event.type in {'MOUSEMOVE', 'LEFTMOUSE'} and not press:
                if self.operation in {'stroke', 'bridge', 'polystrip'}:
                    hit = self._hit(context, event)
                    if hit is not None and (hit - self._points[-1]).length > 1e-6:
                        self._points.append(hit.copy())
                        self._screen.append(self._coordinate(event))
                else:
                    self._screen = [self._screen[0], self._coordinate(event)]
                if release:
                    if self.operation in {'knife', 'contour', 'contour_bridge', 'tweak'}:
                        start = self._view_point(self._screen[0])
                        end = self._view_point(self._screen[-1])
                        delta = end - start
                        if delta.length < 1e-6:
                            raise RuntimeError('Draw a nonzero segment')
                        if self.operation == 'tweak':
                            self._points = [delta]
                        else:
                            view = view3d_utils.region_2d_to_vector_3d(self._region, self._rv3d, self._screen[0])
                            normal = delta.cross(view).normalized()
                            extra.update(origin=list(start), normal=list(normal))
                            self._points = [self._anchor]
                    elif len(self._points) < 2:
                        raise RuntimeError('Draw at least two surface points')
                    commit = True
            if commit:
                engine.execute(context, self.operation, self._points, self._target, self._source, extra,
                               self._source_handle)
                self._commits += 1
                if self.persistent and self.operation == 'polypen':
                    self._points = []
                    self._screen = []
                    self._drawing = False
                    return {'RUNNING_MODAL'}
                self._finish(context)
                return {'FINISHED'}
        except Exception as exc:
            self._finish(context)
            self.report({'ERROR'}, str(exc))
            return {'FINISHED'} if getattr(self, '_commits', 0) else {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._finish(context)


def draw_panel(layout, context):
    scene = context.scene
    layout.operator('mesh.rf_rs_new_target')
    layout.operator('mesh.rf_rs_select')
    pen = layout.operator('mesh.rf_rs_draw', text='PolyPen (Repeated Clicks)')
    pen.operation = 'polypen'
    pen.persistent = True
    col = layout.column(align=True)
    for name in ('rf_rs_strength', 'rf_rs_iterations', 'rf_rs_count', 'rf_rs_rows',
                 'rf_rs_width', 'rf_rs_factor', 'rf_rs_closed'):
        col.prop(scene, name)
    grid = layout.grid_flow(columns=2, align=True)
    for operation in ('select_all', 'select_none', 'select_invert', 'select_connected', 'select_path',
                      'project', 'relax', 'delete', 'face', 'merge', 'fill_auto'):
        grid.operator('mesh.rf_rs_execute', text=operation.replace('_', ' ').title()).operation = operation
    grid = layout.grid_flow(columns=2, align=True)
    for operation, label in (
            ('stroke', 'Stroke'), ('bridge', 'Bridge'), ('polystrip', 'Strip'), ('patch', 'Patch'),
            ('contour', 'Contour'), ('contour_bridge', 'Contour Bridge'), ('loops', 'Loop'),
            ('knife', 'Plane Cut'), ('tweak', 'Translate'), ('add_vertex', 'Add Vertex')):
        grid.operator('mesh.rf_rs_draw', text=label).operation = operation


CLASSES = (RF_RS_OT_new_target, RF_RS_OT_select, RF_RS_OT_execute, RF_RS_OT_draw)

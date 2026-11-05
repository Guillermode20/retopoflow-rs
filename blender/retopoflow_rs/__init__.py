"""Thin Blender adapter for the Rust geometry engine (world-space JSON API)."""
bl_info = {
    "name": "Retopoflow RS", "author": "Retopoflow RS contributors",
    "version": (0, 2, 2), "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Retopo RS", "category": "Mesh",
    "description": "Rust-backed surface retopology tools",
}

import ctypes
import json
import math
import pathlib
import sys

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree

_library = None
_draw_sessions = set()
_selection_order = {}


def _selection_stamp(target):
    mesh = target.data
    return (mesh.as_pointer(), tuple(tuple(v.co) for v in mesh.vertices),
            tuple(tuple(e.vertices) for e in mesh.edges),
            tuple(tuple(p.vertices) for p in mesh.polygons))


def _selected(target):
    actual = [v.index for v in target.data.vertices if v.select]
    cached = _selection_order.get(target.as_pointer())
    if cached and cached[0] == _selection_stamp(target):
        order = cached[1]
        if len(order) == len(actual) and set(order) == set(actual):
            return list(order)
    _selection_order.pop(target.as_pointer(), None)
    return actual


def _remember_selection(target, order):
    _selection_order[target.as_pointer()] = (_selection_stamp(target), tuple(order))


def _set_selection(mesh, selected):
    selected = set(selected)
    for vertex in mesh.vertices:
        vertex.select = vertex.index in selected
    for edge in mesh.edges:
        edge.select = all(i in selected for i in edge.vertices)
    for face in mesh.polygons:
        face.select = all(i in selected for i in face.vertices)
    mesh.update()


def _load_library():
    global _library
    if _library is None:
        filename = ("retopoflow_core.dll" if sys.platform == "win32" else
                    "libretopoflow_core.dylib" if sys.platform == "darwin" else
                    "libretopoflow_core.so")
        path = pathlib.Path(__file__).parent / "native" / filename
        try:
            from .native_loader import load_runtime_copy
            library = load_runtime_copy(path)
            library.rf_execute.argtypes = [ctypes.c_char_p]
            # Preserve the allocation address; c_char_p would convert to bytes.
            library.rf_execute.restype = ctypes.c_void_p
            library.rf_free.argtypes = [ctypes.c_void_p]
            library.rf_free.restype = None
            library.rf_source_create.argtypes = [ctypes.c_char_p]
            library.rf_source_create.restype = ctypes.c_uint64
            library.rf_source_free.argtypes = [ctypes.c_uint64]
            library.rf_source_free.restype = None
            library.rf_execute_cached.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
            library.rf_execute_cached.restype = ctypes.c_void_p
        except (OSError, AttributeError) as exc:
            raise RuntimeError(f"Cannot load Rust engine at {path}: {exc}") from exc
        _library = library
    return _library


def _cache_source(source):
    handle = _load_library().rf_source_create(json.dumps(source, allow_nan=False).encode('utf8'))
    if not handle:
        # The handle-only API cannot carry an error string. Ask the normal
        # validated API for its precise diagnostic instead of hiding it.
        _engine({'source': source, 'mesh': {'vertices': []}, 'operation': 'project'})
        raise RuntimeError('Native source cache creation failed despite valid geometry')
    return handle


def _engine(request, source_handle=None):
    library = _load_library()
    payload = json.dumps(request, allow_nan=False).encode('utf8')
    pointer = (library.rf_execute_cached(payload, source_handle) if source_handle
               else library.rf_execute(payload))
    if not pointer:
        raise RuntimeError("Rust engine returned a null response")
    try:
        response = json.loads(ctypes.string_at(pointer).decode("utf8"))
    finally:
        library.rf_free(pointer)
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise RuntimeError(response.get("error", "Invalid engine response")
                           if isinstance(response, dict) else "Invalid engine response")
    if not isinstance(response.get("mesh"), dict):
        raise RuntimeError("Engine response has no mesh")
    return response["mesh"]


def _source_poll(self, obj):
    return obj.type == 'MESH'


def _objects(context):
    target = context.active_object
    source = context.scene.rf_rs_source
    if context.mode != 'OBJECT' or target is None or target.type != 'MESH':
        raise RuntimeError("Select a target mesh in Object Mode")
    if source is None or source.type != 'MESH' or source == target:
        raise RuntimeError("Choose a separate source mesh")
    if target.library or target.data.library:
        raise RuntimeError("Target mesh must be editable")
    return target, source


def _source_geometry(context, source):
    evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        vertices = [list(evaluated.matrix_world @ vertex.co) for vertex in mesh.vertices]
        if any(not math.isfinite(c) for p in vertices for c in p):
            raise RuntimeError(f'Source {source.name!r} contains non-finite world coordinates')
        triangles = []
        skipped = 0
        for triangle in mesh.loop_triangles:
            ids = list(triangle.vertices)
            a, b, c = (vertices[i] for i in ids)
            u = [b[i]-a[i] for i in range(3)]
            v = [c[i]-a[i] for i in range(3)]
            scale = max(abs(x) for x in u + v)
            if scale == 0:
                skipped += 1
                continue
            u, v = ([x/scale for x in e] for e in (u, v))
            cross = [u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]]
            if sum(x*x for x in cross) <= 1e-28:
                skipped += 1
                continue
            triangles.append(ids)
    finally:
        evaluated.to_mesh_clear()
    if not triangles:
        raise RuntimeError(f'Source {source.name!r} has no usable evaluated surface triangles '
                           f'({skipped} zero-area triangles skipped); check its geometry and object scale')
    return {"vertices": vertices, "triangles": triangles, "skipped_triangles": skipped}


def _execute(context, operation, points=(), target=None, source_data=None, extra=None, source_handle=None):
    current, source = _objects(context)
    if target is not None and current != target:
        raise RuntimeError("Active target changed during drawing")
    target = current
    mesh = target.data
    coordinate_only = operation in {'project', 'relax', 'tweak'}
    selection_only = operation in {'select_all', 'select_none', 'select_invert', 'select_connected', 'select_path'}
    if mesh.shape_keys is not None and not selection_only:
        raise RuntimeError("Edits on shape-key meshes are not supported; make a key-free copy")
    if not coordinate_only and not selection_only:
        # Blender creates sharp_face for ordinary flat-shaded from_pydata meshes.
        # All-flat faces and all-unmarked edges are default data, not user attributes.
        def default_sharp(attribute):
            if attribute.name == 'sharp_face' and attribute.domain == 'FACE' and attribute.data_type == 'BOOLEAN':
                return all(item.value for item in attribute.data)
            if attribute.name == 'sharp_edge' and attribute.domain == 'EDGE' and attribute.data_type == 'BOOLEAN':
                return not any(item.value for item in attribute.data)
            return False

        custom = [a for a in mesh.attributes if not a.is_internal and
                  a.name not in {'position', '.edge_verts', '.corner_vert', '.corner_edge'}
                  and not default_sharp(a)]
        if mesh.uv_layers or mesh.color_attributes or custom or target.vertex_groups or getattr(mesh, 'has_custom_normals', False):
            raise RuntimeError("Topology edits require a mesh without UVs, colors, custom attributes/normals or vertex groups")
    request = {
        "mesh": {
            "vertices": [list(target.matrix_world @ v.co) for v in mesh.vertices],
            "edges": [list(e.vertices) for e in mesh.edges],
            "faces": [list(p.vertices) for p in mesh.polygons],
            # Only trust tracked order while mesh and selection still match.
            "selected": _selected(target),
        },
        "source": None if source_handle else (source_data if source_data is not None else _source_geometry(context, source)),
        "operation": operation, "points": [list(p) for p in points],
        "strength": context.scene.rf_rs_strength,
        "iterations": context.scene.rf_rs_iterations,
        "count": context.scene.rf_rs_count,
        "rows": context.scene.rf_rs_rows,
        "width": context.scene.rf_rs_width,
        "factor": context.scene.rf_rs_factor,
        "closed": context.scene.rf_rs_closed if operation in {'stroke', 'bridge'} else False,
    }
    if extra:
        request.update(extra)
    result = _engine(request, source_handle)
    if selection_only:
        if mesh.users > 1:
            mesh = mesh.copy()
            target.data = mesh
        selected = set(result.get('selected', []))
        for vertex in mesh.vertices:
            vertex.select = vertex.index in selected
        for edge in mesh.edges:
            edge.select = all(i in selected for i in edge.vertices)
        for face in mesh.polygons:
            face.select = all(i in selected for i in face.vertices)
        mesh.update()
        context.view_layer.update()
        return
    inverse = target.matrix_world.inverted()
    if coordinate_only:
        # Coordinate-only edits preserve UVs, groups and attributes. Keys are rejected above.
        if len(result['vertices']) != len(mesh.vertices):
            raise RuntimeError("Coordinate operation unexpectedly changed vertex count")
        if mesh.users > 1:
            mesh = mesh.copy()
            target.data = mesh
        for vertex, point in zip(mesh.vertices, result['vertices']):
            vertex.co = inverse @ Vector(point)
        mesh.update()
        context.view_layer.update()
        return
    if mesh.shape_keys is not None:
        raise RuntimeError("Topology changes on shape-key meshes are not supported")
    replacement = bpy.data.meshes.new(mesh.name + " RS")
    try:
        replacement.from_pydata([inverse @ Vector(p) for p in result["vertices"]],
                                result["edges"], result["faces"])
        for material in mesh.materials:
            replacement.materials.append(material)
        _set_selection(replacement, result.get('selected', []))
    except Exception:
        bpy.data.meshes.remove(replacement)
        raise
    # Replace only after successful construction; shared mesh users stay unchanged.
    target.data = replacement
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    context.view_layer.update()
    if operation == 'polypen':
        _remember_selection(target, result.get('selected', []))
    else:
        _selection_order.pop(target.as_pointer(), None)


class RF_RS_OT_new_target(bpy.types.Operator):
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
        for obj in context.selected_objects:
            obj.select_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
        return {'FINISHED'}


class RF_RS_OT_select(bpy.types.Operator):
    bl_idname = 'mesh.rf_rs_select'
    bl_label = 'Select Target Vertex'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            return {'CANCELLED'}
        try:
            self._target, source = _objects(context)
            self._bvh = None
            if not self._target.show_in_front:
                data = _source_geometry(context, source)
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
            cursor = Vector((event.mouse_x-self._region.x, event.mouse_y-self._region.y))
            best = None
            for vertex in self._target.data.vertices:
                if vertex.hide:
                    continue
                world = self._target.matrix_world @ vertex.co
                screen = view3d_utils.location_3d_to_region_2d(self._region, self._rv3d, world)
                if screen is None or (screen-cursor).length > 16:
                    continue
                if self._bvh is not None:
                    origin = view3d_utils.region_2d_to_origin_3d(self._region, self._rv3d, screen)
                    delta = world-origin
                    if delta.length > 1e-8:
                        hit = self._bvh.ray_cast(origin, delta.normalized(), max(0.0, delta.length-1e-4))[0]
                        if hit is not None:
                            continue
                candidate = ((screen-cursor).length, vertex.index)
                if best is None or candidate < best:
                    best = candidate
            selected = _selected(self._target) if event.shift else []
            if best is not None:
                index = best[1]
                if index in selected:
                    selected.remove(index)
                else:
                    selected.append(index)
            if self._target.data.users > 1:
                self._target.data = self._target.data.copy()
            _set_selection(self._target.data, selected)
            _remember_selection(self._target, selected)
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
    bl_idname = "mesh.rf_rs_execute"
    bl_label = "Rust Retopology Operation"
    bl_options = {'REGISTER', 'UNDO'}
    operation: EnumProperty(items=[(name, name.title(), "") for name in
                                  ('project', 'relax', 'delete', 'face', 'merge', 'fill_patch',
                                   'select_all', 'select_none', 'select_invert', 'select_connected', 'select_path')])

    def execute(self, context):
        try:
            _execute(context, self.operation)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if self.operation == 'face':
            self.report({'INFO'}, "Face uses tracked selection order when valid; otherwise vertex indices")
        return {'FINISHED'}


class RF_RS_OT_draw(bpy.types.Operator):
    bl_idname = "mesh.rf_rs_draw"
    bl_label = "Draw on Source"
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
            _load_library().rf_source_free(source_handle)
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
        return view3d_utils.region_2d_to_location_3d(
            self._region, self._rv3d, coordinate, self._anchor)

    def _edge(self, event):
        # Rank target edges in screen space, not source triangle edges.
        from mathutils.geometry import intersect_point_line
        cursor = Vector(self._coordinate(event))
        best = None
        for edge in self._target.data.edges:
            ends = [view3d_utils.location_3d_to_region_2d(
                self._region, self._rv3d, self._target.matrix_world @ self._target.data.vertices[i].co)
                for i in edge.vertices]
            if any(p is None for p in ends):
                continue
            a, b = ends
            if (a-b).length_squared < 1e-12:
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
            self.report({'ERROR'}, "Run in a 3D viewport")
            return {'CANCELLED'}
        self._area = context.area
        self._region = next(r for r in context.area.regions if r.type == 'WINDOW')
        self._rv3d = context.space_data.region_3d
        try:
            self._target, source = _objects(context)
            self._source = _source_geometry(context, source)
            self._bvh = BVHTree.FromPolygons(self._source['vertices'], self._source['triangles'], all_triangles=True)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        try:
            self._source_handle = _cache_source(self._source)
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
            if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM', 'NDOF_MOTION'} or event.alt:
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
                    commit = self.operation in {'add_vertex', 'polypen'} or (self.operation == 'patch' and len(self._points) == 4)
            if self._drawing and event.type in {'MOUSEMOVE', 'LEFTMOUSE'} and not press:
                if self.operation in {'stroke', 'bridge', 'polystrip'}:
                    hit = self._hit(context, event)
                    if hit is not None and (hit-self._points[-1]).length > 1e-6:
                        self._points.append(hit.copy())
                        self._screen.append(self._coordinate(event))
                else:
                    self._screen = [self._screen[0], self._coordinate(event)]
                if release:
                    if self.operation in {'knife', 'contour', 'contour_bridge', 'tweak'}:
                        start = self._view_point(self._screen[0])
                        end = self._view_point(self._screen[-1])
                        delta = end-start
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
                _execute(context, self.operation, self._points, self._target, self._source, extra, self._source_handle)
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


class RF_RS_PT_tools(bpy.types.Panel):
    bl_label = "Rust Retopology"
    bl_idname = "RF_RS_PT_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Retopo RS"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator('mesh.rf_rs_session', text='Enter Retopology Session')
        layout.operator('mesh.rf_rs_session', text='New Target + Start Session').new_target = True
        layout.label(text='Tools and settings appear in the viewport')
        layout.prop(context.scene, 'rf_rs_source')
        layout.prop(context.scene, 'rf_rs_advanced', text='Legacy standalone commands')
        if not context.scene.rf_rs_advanced:
            return
        layout.operator('mesh.rf_rs_new_target')
        layout.operator('mesh.rf_rs_select')
        layout.label(text='Select: click / Shift-click toggle')
        pen = layout.operator('mesh.rf_rs_draw', text='PolyPen Session (Repeated Clicks)')
        pen.operation = 'polypen'
        pen.persistent = True
        layout.label(text='Pen: Esc / Enter finish; undo whole session')
        layout.label(text="Target: active mesh, Object Mode")
        layout.prop(context.scene, 'rf_rs_strength')
        layout.prop(context.scene, 'rf_rs_iterations')
        layout.prop(context.scene, 'rf_rs_count')
        layout.prop(context.scene, 'rf_rs_rows')
        layout.prop(context.scene, 'rf_rs_width')
        layout.prop(context.scene, 'rf_rs_factor')
        layout.prop(context.scene, 'rf_rs_closed')
        for operation in ('select_all', 'select_none', 'select_invert', 'select_connected', 'select_path'):
            layout.operator('mesh.rf_rs_execute', text=operation.replace('_', ' ').title()).operation = operation
        layout.label(text='Path: select exactly two endpoints')
        for operation in ('project', 'relax', 'delete', 'face', 'merge', 'fill_patch'):
            layout.operator('mesh.rf_rs_execute', text=operation.title()).operation = operation
        layout.label(text="Face: tracked picks, otherwise index order", icon='INFO')
        layout.label(text="Fill: closed boundary, 2*(columns+rows) edges")
        for operation, label in (
                ('stroke', 'Draw Stroke'), ('bridge', 'Draw Boundary Bridge'),
                ('polystrip', 'Draw Ribbon Strip'), ('patch', 'Four-click Patch'),
                ('contour', 'Drag Contour Plane'), ('contour_bridge', 'Contour Boundary Bridge'),
                ('loops', 'Click Target Edge: Insert Loop'), ('knife', 'Drag Cut Plane'),
                ('tweak', 'Drag Selected: Translate / Project'),
                ('polypen', 'PolyPen: Add / Snap Point'), ('add_vertex', 'Add Vertex')):
            layout.operator('mesh.rf_rs_draw', text=label).operation = operation
        layout.label(text='Bridge: select boundary first', icon='INFO')
        layout.label(text='Topology edits reject attributed meshes', icon='INFO')
        layout.label(text='Shape keys: coordinate edits unsupported', icon='INFO')


from . import session


def _header_session(self, context):
    row = self.layout.row(align=True)
    row.operator_context = 'INVOKE_DEFAULT'
    row.operator('mesh.rf_rs_session', text='Retopo RS', icon='MOD_SHRINKWRAP')
    row.operator('mesh.rf_rs_session', text='', icon='ADD').new_target = True


_CLASSES = (RF_RS_OT_new_target, RF_RS_OT_select, RF_RS_OT_execute, RF_RS_OT_draw,
            *session.CLASSES, RF_RS_PT_tools)
_PROPERTIES = ('rf_rs_source', 'rf_rs_strength', 'rf_rs_iterations', 'rf_rs_count',
               'rf_rs_rows', 'rf_rs_width', 'rf_rs_factor', 'rf_rs_closed', 'rf_rs_advanced')


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rf_rs_source = PointerProperty(name="Source", type=bpy.types.Object, poll=_source_poll)
    bpy.types.Scene.rf_rs_strength = FloatProperty(name="Strength", default=0.5, min=0.0, max=1.0)
    bpy.types.Scene.rf_rs_iterations = IntProperty(name="Iterations", default=1, min=1, max=100)
    bpy.types.Scene.rf_rs_count = IntProperty(name="Count / Patch Columns", default=8, min=2, max=4096)
    bpy.types.Scene.rf_rs_rows = IntProperty(name="Patch Rows", default=4, min=1, max=256)
    bpy.types.Scene.rf_rs_width = FloatProperty(name="Strip Width / PolyPen Snap", default=0.1, min=0.000001)
    bpy.types.Scene.rf_rs_factor = FloatProperty(name="Loop Factor", default=0.5, min=0.001, max=0.999)
    bpy.types.Scene.rf_rs_closed = BoolProperty(name="Closed Stroke / Bridge", default=False)
    bpy.types.Scene.rf_rs_advanced = BoolProperty(name='Legacy standalone commands', default=False)
    bpy.types.VIEW3D_HT_header.append(_header_session)


def unregister():
    session.shutdown(bpy.context)
    bpy.types.VIEW3D_HT_header.remove(_header_session)
    _selection_order.clear()
    for legacy_session in tuple(_draw_sessions):
        legacy_session._finish(bpy.context)
    for name in _PROPERTIES:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    _selection_order.clear()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()

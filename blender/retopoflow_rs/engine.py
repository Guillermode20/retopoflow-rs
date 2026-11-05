"""Bridge between Blender meshes and the native Rust engine (world-space JSON).

Mesh data is moved with ``foreach_get``/``foreach_set`` and numpy, so the cost
of an operation is dominated by the native call rather than per-vertex Python.
"""
import ctypes
import json
import pathlib
import sys

import numpy as np

_library = None
# target object pointer -> (stamp, ordered selection). The order matters for
# PolyPen extrusion and face creation; Blender itself only stores flags.
_selection_order = {}

SELECTION_OPS = frozenset({'select_all', 'select_none', 'select_invert',
                           'select_connected', 'select_path'})
COORDINATE_OPS = frozenset({'project', 'relax', 'tweak'})


class EngineError(RuntimeError):
    """A native operation was rejected; the target mesh is unchanged."""


# ---------------------------------------------------------------- native library

def library_filename():
    if sys.platform == 'win32':
        return 'retopoflow_core.dll'
    if sys.platform == 'darwin':
        return 'libretopoflow_core.dylib'
    return 'libretopoflow_core.so'


def load_library():
    global _library
    if _library is None:
        path = pathlib.Path(__file__).parent / 'native' / library_filename()
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
            raise RuntimeError(f'Cannot load Rust engine at {path}: {exc}. '
                               'Rebuild the add-on for this platform.') from exc
        _library = library
    return _library


def call(request, source_handle=None):
    """Run one request; returns the result mesh dict or raises EngineError."""
    library = load_library()
    payload = json.dumps(request, allow_nan=False).encode('utf8')
    pointer = (library.rf_execute_cached(payload, source_handle) if source_handle
               else library.rf_execute(payload))
    if not pointer:
        raise EngineError('Rust engine returned a null response')
    try:
        response = json.loads(ctypes.string_at(pointer).decode('utf8'))
    finally:
        library.rf_free(pointer)
    if not isinstance(response, dict) or response.get('ok') is not True:
        raise EngineError(response.get('error', 'Invalid engine response')
                          if isinstance(response, dict) else 'Invalid engine response')
    if not isinstance(response.get('mesh'), dict):
        raise EngineError('Engine response has no mesh')
    return response['mesh']


def cache_source(source):
    handle = load_library().rf_source_create(json.dumps(source, allow_nan=False).encode('utf8'))
    if not handle:
        # The handle-only API cannot carry an error string. Ask the normal
        # validated API for its precise diagnostic instead of hiding it.
        call({'source': source, 'mesh': {'vertices': []}, 'operation': 'project'})
        raise EngineError('Native source cache creation failed despite valid geometry')
    return handle


def free_source(handle):
    if handle:
        load_library().rf_source_free(handle)


# ------------------------------------------------------------------ mesh arrays

def matrix_array(matrix):
    return np.array([list(row) for row in matrix], dtype=np.float64)


def transform(points, matrix):
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    m = matrix_array(matrix) if not isinstance(matrix, np.ndarray) else matrix
    return points @ m[:3, :3].T + m[:3, 3]


def vertex_coordinates(mesh):
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get('co', co)
    return co.reshape(-1, 3)


def edge_array(mesh):
    edges = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get('vertices', edges)
    return edges.reshape(-1, 2)


def face_arrays(mesh):
    """Return (loop vertex indices, polygon loop starts, polygon sizes)."""
    loops = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get('vertex_index', loops)
    starts = np.empty(len(mesh.polygons), dtype=np.int64)
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    return loops, starts, totals


def face_lists(mesh):
    loops, starts, totals = face_arrays(mesh)
    flat = loops.tolist()
    return [flat[s:s + t] for s, t in zip(starts.tolist(), totals.tolist())]


def selected_flags(mesh):
    flags = np.zeros(len(mesh.vertices), dtype=bool)
    mesh.vertices.foreach_get('select', flags)
    return flags


# ------------------------------------------------------------------- selection

def _stamp(mesh):
    return mesh.as_pointer(), len(mesh.vertices), len(mesh.edges), len(mesh.polygons)


def selected(target):
    """Selected vertex indices, in click order while that order is still valid."""
    actual = np.flatnonzero(selected_flags(target.data)).tolist()
    cached = _selection_order.get(target.as_pointer())
    if cached and cached[0] == _stamp(target.data):
        order = cached[1]
        if len(order) == len(actual) and set(order) == set(actual):
            return list(order)
    _selection_order.pop(target.as_pointer(), None)
    return actual


def remember_selection(target, order):
    _selection_order[target.as_pointer()] = (_stamp(target.data), tuple(order))


def forget_selection(target=None):
    if target is None:
        _selection_order.clear()
    else:
        _selection_order.pop(target.as_pointer(), None)


def set_selection(mesh, indices):
    """Select exactly ``indices`` (vertex flags plus derived edge/face flags)."""
    flags = np.zeros(len(mesh.vertices), dtype=bool)
    indices = np.asarray(list(indices), dtype=np.int64)
    if indices.size:
        flags[indices] = True
    mesh.vertices.foreach_set('select', flags)
    if len(mesh.edges):
        edges = edge_array(mesh)
        mesh.edges.foreach_set('select', flags[edges].all(axis=1))
    if len(mesh.polygons):
        loops, starts, _ = face_arrays(mesh)
        mesh.polygons.foreach_set('select', np.minimum.reduceat(flags[loops], starts))
    mesh.update()


# --------------------------------------------------------------------- sources

def source_geometry(context, source):
    """Evaluated world-space triangles of ``source``; zero-area ones skipped."""
    evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        vertices = transform(vertex_coordinates(mesh), evaluated.matrix_world)
        triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
        mesh.loop_triangles.foreach_get('vertices', triangles)
        triangles = triangles.reshape(-1, 3)
    finally:
        evaluated.to_mesh_clear()
    if not np.isfinite(vertices).all():
        raise RuntimeError(f'Source {source.name!r} contains non-finite world coordinates')
    total = len(triangles)
    if total:
        a, b, c = (vertices[triangles[:, k]] for k in range(3))
        u, v = b - a, c - a
        scale = np.maximum(np.abs(u).max(axis=1), np.abs(v).max(axis=1))
        safe = np.where(scale > 0, scale, 1.0)[:, None]
        cross = np.cross(u / safe, v / safe)
        keep = (scale > 0) & ((cross * cross).sum(axis=1) > 1e-28)
        triangles = triangles[keep]
    skipped = total - len(triangles)
    if not len(triangles):
        raise RuntimeError(f'Source {source.name!r} has no usable evaluated surface triangles '
                           f'({skipped} zero-area triangles skipped); check its geometry and object scale')
    return {'vertices': vertices.tolist(), 'triangles': triangles.tolist(), 'skipped_triangles': skipped}


def objects(context):
    target = context.active_object
    source = context.scene.rf_rs_source
    if context.mode != 'OBJECT' or target is None or target.type != 'MESH':
        raise RuntimeError('Select a target mesh in Object Mode')
    if source is None or source.type != 'MESH' or source == target:
        raise RuntimeError('Choose a separate source mesh')
    if target.library or target.data.library:
        raise RuntimeError('Target mesh must be editable')
    return target, source


# ------------------------------------------------------------------- execution

def _default_sharp(attribute):
    # Blender creates sharp_face for flat-shaded from_pydata meshes. All-flat
    # faces and all-unmarked edges are default data, not user attributes.
    if attribute.name == 'sharp_face' and attribute.domain == 'FACE' and attribute.data_type == 'BOOLEAN':
        return all(item.value for item in attribute.data)
    if attribute.name == 'sharp_edge' and attribute.domain == 'EDGE' and attribute.data_type == 'BOOLEAN':
        return not any(item.value for item in attribute.data)
    return False


def check_topology_editable(target):
    mesh = target.data
    if mesh.shape_keys is not None:
        raise RuntimeError('Edits on shape-key meshes are not supported; make a key-free copy')
    custom = [a for a in mesh.attributes if not a.is_internal and
              a.name not in {'position', '.edge_verts', '.corner_vert', '.corner_edge'}
              and not _default_sharp(a)]
    if (mesh.uv_layers or mesh.color_attributes or custom or target.vertex_groups
            or getattr(mesh, 'has_custom_normals', False)):
        raise RuntimeError('Topology edits require a mesh without UVs, colors, custom '
                           'attributes/normals or vertex groups')


def mesh_request(target, selection=None):
    mesh = target.data
    world = transform(vertex_coordinates(mesh), target.matrix_world)
    return {
        'vertices': world.tolist(),
        'edges': edge_array(mesh).tolist(),
        'faces': face_lists(mesh),
        'selected': list(selection) if selection is not None else selected(target),
    }


def _own_mesh(target):
    """Copy-on-write: never mutate a mesh shared with other objects."""
    if target.data.users > 1:
        target.data = target.data.copy()
    return target.data


def apply_result(target, result, operation):
    mesh = target.data
    if operation in SELECTION_OPS:
        mesh = _own_mesh(target)
        set_selection(mesh, result.get('selected', []))
        return
    inverse = matrix_array(target.matrix_world.inverted())
    local = transform(result['vertices'], inverse) if result['vertices'] else np.empty((0, 3))
    if operation in COORDINATE_OPS:
        # Coordinate-only edits preserve UVs, groups and attributes.
        if len(local) != len(mesh.vertices):
            raise EngineError('Coordinate operation unexpectedly changed vertex count')
        mesh = _own_mesh(target)
        mesh.vertices.foreach_set('co', local.ravel())
        mesh.update()
        return
    if mesh.shape_keys is not None:
        raise EngineError('Topology changes on shape-key meshes are not supported')
    mesh = _own_mesh(target)
    # Rebuild in place: keeps the datablock, its name and material slots.
    mesh.clear_geometry()
    mesh.from_pydata(local.tolist(), result['edges'], result['faces'])
    set_selection(mesh, result.get('selected', []))
    if operation == 'polypen':
        remember_selection(target, result.get('selected', []))
    else:
        forget_selection(target)


def execute(context, operation, points=(), target=None, source_data=None, extra=None,
            source_handle=None, selection=None):
    """Run ``operation`` on ``target`` (default: active object + scene Source)."""
    source = None
    if target is None or (source_handle is None and source_data is None):
        current, source = objects(context)
        if target is not None and current != target:
            raise RuntimeError('Active target changed during drawing')
        target = current
    if operation not in SELECTION_OPS:
        if operation in COORDINATE_OPS:
            if target.data.shape_keys is not None:
                raise RuntimeError('Edits on shape-key meshes are not supported; make a key-free copy')
        else:
            check_topology_editable(target)
    scene = context.scene
    request = {
        'mesh': mesh_request(target, selection),
        'source': None if source_handle else (
            source_data if source_data is not None else source_geometry(context, source)),
        'operation': operation,
        'points': [list(p) for p in points],
        'strength': scene.rf_rs_strength,
        'iterations': scene.rf_rs_iterations,
        'count': scene.rf_rs_count,
        'rows': scene.rf_rs_rows,
        'width': scene.rf_rs_width,
        'factor': scene.rf_rs_factor,
        'closed': scene.rf_rs_closed if operation in {'stroke', 'bridge'} else False,
    }
    if extra:
        request.update(extra)
    result = call(request, source_handle)
    apply_result(target, result, operation)
    context.view_layer.update()
    return result

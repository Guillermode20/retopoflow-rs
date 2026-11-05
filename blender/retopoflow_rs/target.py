"""Cached, numpy-backed view of the retopology target for drawing and picking.

Everything here is rebuilt only when the mesh changes (``refresh``) or the view
moves (screen projection / occlusion caches), never per mouse event.
"""
import numpy as np
from mathutils import Vector

from . import engine


def _segment_distance(points, a, b):
    """Distances from 2D ``points`` (N,2) to segments a[i]-b[i]; also the param t."""
    ab = b - a
    length2 = (ab * ab).sum(axis=1)
    safe = np.where(length2 > 1e-12, length2, 1.0)
    t = np.clip(((points - a) * ab).sum(axis=1) / safe, 0.0, 1.0)
    closest = a + ab * t[:, None]
    return np.linalg.norm(points - closest, axis=1), t


def _segments_intersect(p, q, a, b):
    """Vectorised 2D test: does segment p-q intersect each segment a[i]-b[i]?"""
    def orient(u, v, w):
        return (v[..., 0] - u[..., 0]) * (w[..., 1] - u[..., 1]) - (v[..., 1] - u[..., 1]) * (w[..., 0] - u[..., 0])
    p = np.broadcast_to(p, a.shape)
    q = np.broadcast_to(q, a.shape)
    d1, d2 = orient(a, b, p), orient(a, b, q)
    d3, d4 = orient(p, q, a), orient(p, q, b)
    return (d1 * d2 <= 0) & (d3 * d4 <= 0)


class TargetCache:
    def __init__(self, obj):
        self.obj = obj
        self.version = 0
        self.dirty = True
        self._view_key = None
        self._screen = None
        self._valid = None
        self._occluded = {}
        self._adjacency = None
        self._edge_faces = None
        self._boundary_info = None

    # ------------------------------------------------------------- mesh data
    def invalidate(self):
        self.dirty = True

    def ensure(self):
        if self.dirty:
            self.refresh()
        return self

    def refresh(self):
        mesh = self.obj.data
        self.matrix = engine.matrix_array(self.obj.matrix_world)
        self.local = engine.vertex_coordinates(mesh)
        self.world = engine.transform(self.local, self.matrix)
        self.selected = engine.selected_flags(mesh)
        hidden = np.zeros(len(mesh.vertices), dtype=bool)
        mesh.vertices.foreach_get('hide', hidden)
        self.hidden = hidden
        self.edges = engine.edge_array(mesh)
        loops, starts, totals = engine.face_arrays(mesh)
        self.loops, self.starts, self.totals = loops, starts, totals
        flat = loops.tolist()
        self.faces = [tuple(flat[s:s + t]) for s, t in zip(starts.tolist(), totals.tolist())]
        mesh.calc_loop_triangles()
        tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
        mesh.loop_triangles.foreach_get('vertices', tris)
        self.tris = tris.reshape(-1, 3)
        edge_loops = np.empty(len(mesh.loops), dtype=np.int64)
        mesh.loops.foreach_get('edge_index', edge_loops)
        self.edge_face_count = (np.bincount(edge_loops, minlength=len(mesh.edges)) if len(mesh.edges)
                                else np.zeros(0, int))
        boundary = np.zeros(len(mesh.vertices), dtype=bool)
        if len(self.edges):
            boundary[self.edges[self.edge_face_count <= 1].ravel()] = True
        self.boundary = boundary
        self.order = engine.selected(self.obj)
        self.version += 1
        self.dirty = False
        self._view_key = None
        self._adjacency = None
        self._edge_faces = None
        self._boundary_info = None
        return self

    def set_world(self, indices, positions):
        """Move vertices (world space) without a topology refresh."""
        indices = np.asarray(indices, dtype=np.int64)
        if not indices.size:
            return
        self.world[indices] = positions
        inverse = np.linalg.inv(self.matrix)
        self.local[indices] = engine.transform(positions, inverse)
        mesh = self.obj.data
        mesh.vertices.foreach_set('co', self.local.ravel())
        mesh.update()
        self.version += 1
        self._view_key = None

    @property
    def count(self):
        return len(self.world)

    @property
    def selected_indices(self):
        return np.flatnonzero(self.selected)

    def has_edge(self, a, b):
        if not len(self.edges):
            return False
        e = self.edges
        return bool((((e[:, 0] == a) & (e[:, 1] == b)) | ((e[:, 0] == b) & (e[:, 1] == a))).any())

    def adjacency(self):
        if self._adjacency is None:
            neighbours = [[] for _ in range(self.count)]
            for a, b in self.edges.tolist():
                neighbours[a].append(b)
                neighbours[b].append(a)
            self._adjacency = [np.array(n, dtype=np.int64) for n in neighbours]
        return self._adjacency

    def linked(self, seeds):
        adjacency = self.adjacency()
        seen = set(int(s) for s in seeds)
        todo = list(seen)
        while todo:
            for n in adjacency[todo.pop()].tolist():
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return sorted(seen)

    def edge_faces(self):
        if self._edge_faces is None:
            mapping = {}
            for index, face in enumerate(self.faces):
                for i in range(len(face)):
                    a, b = face[i], face[(i + 1) % len(face)]
                    mapping.setdefault((a, b) if a < b else (b, a), []).append(index)
            self._edge_faces = mapping
        return self._edge_faces

    def selected_boundary(self):
        """(vertex count, closed) when the selection is one unbranched boundary chain."""
        if self._boundary_info is not None:
            return self._boundary_info or None
        info = ()
        chosen = set(self.selected_indices.tolist())
        if len(chosen) >= 2 and len(self.edges):
            mask = self.selected[self.edges].all(axis=1) & (self.edge_face_count <= 1)
            degree = {i: 0 for i in chosen}
            links = {i: [] for i in chosen}
            for a, b in self.edges[mask].tolist():
                degree[a] += 1
                degree[b] += 1
                links[a].append(b)
                links[b].append(a)
            values = list(degree.values())
            closed = all(d == 2 for d in values)
            open_chain = values.count(1) == 2 and all(d in (1, 2) for d in values)
            if closed or open_chain:
                start = next(iter(chosen))
                seen, todo = set(), [start]
                while todo:
                    i = todo.pop()
                    if i not in seen:
                        seen.add(i)
                        todo.extend(links[i])
                if seen == chosen and (not closed or len(chosen) >= 3):
                    info = (len(chosen), closed)
        self._boundary_info = info
        return info or None

    def loop_ring(self, edge_index, factor):
        """World-space segments of the edge loop Loops would insert (preview)."""
        a, b = self.edges[edge_index].tolist()
        edge_faces = self.edge_faces()
        key = (a, b) if a < b else (b, a)
        oriented = {key: (a, b)}
        queue = [key]
        segments, visited = [], set()
        world = self.world
        while queue:
            e = queue.pop()
            u, v = oriented[e]
            for fi in edge_faces.get(e, ()):
                face = self.faces[fi]
                if fi in visited or len(face) != 4:
                    continue
                visited.add(fi)
                i = next(i for i in range(4) if {face[i], face[(i + 1) % 4]} == {u, v})
                if (face[i], face[(i + 1) % 4]) == (u, v):
                    directed = (face[(i + 3) % 4], face[(i + 2) % 4])
                else:
                    directed = (face[(i + 2) % 4], face[(i + 3) % 4])
                p = world[u] + (world[v] - world[u]) * factor
                q = world[directed[0]] + (world[directed[1]] - world[directed[0]]) * factor
                segments.append((p, q))
                opposite = tuple(sorted(directed))
                if opposite not in oriented:
                    oriented[opposite] = directed
                    queue.append(opposite)
        if not segments:
            p = world[a] + (world[b] - world[a]) * factor
            segments.append((p, p))
        return segments

    # --------------------------------------------------------- view & picking
    def project(self, region, rv3d):
        key = (self.version, region.width, region.height,
               tuple(tuple(row) for row in rv3d.perspective_matrix))
        if key != self._view_key:
            self._view_key = key
            self._occluded = {}
            if self.count:
                matrix = engine.matrix_array(rv3d.perspective_matrix)
                homogeneous = np.c_[self.world, np.ones(self.count)] @ matrix.T
                w = homogeneous[:, 3]
                valid = (w > 1e-9) & ~self.hidden
                safe = np.where(valid, w, 1.0)
                screen = np.empty((self.count, 2))
                screen[:, 0] = (homogeneous[:, 0] / safe + 1.0) * 0.5 * region.width
                screen[:, 1] = (homogeneous[:, 1] / safe + 1.0) * 0.5 * region.height
                self._screen, self._valid = screen, valid
            else:
                self._screen, self._valid = np.empty((0, 2)), np.zeros(0, bool)
        return self._screen, self._valid

    def visible(self, index, region, rv3d, bvh):
        """True unless the source surface hides this vertex from the viewer."""
        cached = self._occluded.get(index)
        if cached is not None:
            return not cached
        occluded = self._point_occluded(Vector(self.world[index]), self._screen[index], region, rv3d, bvh)
        self._occluded[index] = occluded
        return not occluded

    @staticmethod
    def _point_occluded(point, screen, region, rv3d, bvh):
        from bpy_extras import view3d_utils
        if bvh is None:
            return False
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, Vector(screen))
        delta = point - origin
        distance = delta.length
        if distance < 1e-9:
            return False
        tolerance = max(distance * 4e-3, 1e-6) if rv3d.is_perspective else max(rv3d.view_distance * 4e-3, 1e-6)
        hit = bvh.ray_cast(origin, delta / distance, max(0.0, distance - tolerance))
        return hit[0] is not None

    def nearest_vertex(self, xy, radius, region, rv3d, bvh, mask=None):
        screen, valid = self.project(region, rv3d)
        if not self.count:
            return None
        distance = np.linalg.norm(screen - np.asarray(xy, dtype=np.float64), axis=1)
        candidates = valid & (distance <= radius)
        if mask is not None:
            candidates &= mask
        for index in np.flatnonzero(candidates)[np.argsort(distance[candidates])].tolist():
            if self.visible(index, region, rv3d, bvh):
                return index
        return None

    def nearest_edge(self, xy, radius, region, rv3d, bvh):
        """(edge index, t along edge from its first vertex) or None."""
        screen, valid = self.project(region, rv3d)
        if not len(self.edges):
            return None
        e = self.edges
        ok = valid[e[:, 0]] & valid[e[:, 1]]
        if not ok.any():
            return None
        point = np.broadcast_to(np.asarray(xy, dtype=np.float64), (len(e), 2))
        distance, t = _segment_distance(point, screen[e[:, 0]], screen[e[:, 1]])
        candidates = ok & (distance <= radius)
        for index in np.flatnonzero(candidates)[np.argsort(distance[candidates])].tolist():
            # Edge midpoints of a curved surface dip below it; test endpoints.
            a, b = e[index].tolist()
            if self.visible(a, region, rv3d, bvh) or self.visible(b, region, rv3d, bvh):
                return index, float(t[index])
        return None

    def in_circle(self, xy, radius, region, rv3d, bvh, mask=None):
        """Visible vertex indices within ``radius`` px and their pixel distances."""
        screen, valid = self.project(region, rv3d)
        if not self.count:
            return np.zeros(0, np.int64), np.zeros(0)
        distance = np.linalg.norm(screen - np.asarray(xy, dtype=np.float64), axis=1)
        candidates = valid & (distance <= radius)
        if mask is not None:
            candidates &= mask
        indices = [i for i in np.flatnonzero(candidates).tolist() if self.visible(i, region, rv3d, bvh)]
        indices = np.asarray(indices, dtype=np.int64)
        return indices, distance[indices]

    def in_rect(self, a, b, region, rv3d, bvh):
        screen, valid = self.project(region, rv3d)
        if not self.count:
            return []
        lo = np.minimum(a, b)
        hi = np.maximum(a, b)
        inside = valid & (screen >= lo).all(axis=1) & (screen <= hi).all(axis=1)
        return [i for i in np.flatnonzero(inside).tolist() if self.visible(i, region, rv3d, bvh)]

    def faces_crossing(self, a, b, region, rv3d, bvh):
        """Faces whose screen outline crosses segment a-b and that face the viewer."""
        screen, valid = self.project(region, rv3d)
        if not len(self.faces):
            return []
        loops, starts = self.loops, self.starts
        nxt = np.empty_like(loops)
        ends = starts + self.totals
        nxt[:-1] = loops[1:]
        nxt[ends - 1] = loops[starts]
        hit = _segments_intersect(np.asarray(a, float), np.asarray(b, float), screen[loops], screen[nxt])
        hit &= valid[loops] & valid[nxt]
        crossing = np.logical_or.reduceat(hit, starts)
        result = []
        for index in np.flatnonzero(crossing).tolist():
            if any(self.visible(v, region, rv3d, bvh) for v in self.faces[index]):
                result.append(index)
        return result

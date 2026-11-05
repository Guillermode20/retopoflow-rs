use glam::DVec3 as V;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

// Test nonadjacent edges in the face's dominant-plane projection. This also
// rejects folded quads whose signed triangle areas happen to have nonzero sum.
fn simple_polygon(points: &[V], normal: V) -> bool {
    let n = normal.abs();
    let axis = if n.x >= n.y && n.x >= n.z {
        0
    } else if n.y >= n.z {
        1
    } else {
        2
    };
    let project = |p: V| match axis {
        0 => glam::DVec2::new(p.y, p.z),
        1 => glam::DVec2::new(p.x, p.z),
        _ => glam::DVec2::new(p.x, p.y),
    };
    let points: Vec<_> = points.iter().map(|&p| project(p)).collect();
    let scale = points
        .iter()
        .map(|p| p.distance_squared(points[0]))
        .fold(0.0, f64::max)
        .max(1e-20);
    let eps = scale * 1e-12;
    let orient = |a: glam::DVec2, b: glam::DVec2, c: glam::DVec2| (b - a).perp_dot(c - a);
    for i in 0..points.len() {
        for j in i + 1..points.len() {
            if j == i + 1 || (i == 0 && j == points.len() - 1) {
                continue;
            }
            let (a, b, c, d) = (
                points[i],
                points[(i + 1) % points.len()],
                points[j],
                points[(j + 1) % points.len()],
            );
            let (ab_c, ab_d, cd_a, cd_b) = (
                orient(a, b, c),
                orient(a, b, d),
                orient(c, d, a),
                orient(c, d, b),
            );
            let opposite = |x: f64, y: f64| (x > eps && y < -eps) || (x < -eps && y > eps);
            if opposite(ab_c, ab_d) && opposite(cd_a, cd_b) {
                return false;
            }
            let on = |p: glam::DVec2, u: glam::DVec2, v: glam::DVec2, o: f64| {
                o.abs() <= eps && (p - u).dot(p - v) <= eps
            };
            if on(c, a, b, ab_c) || on(d, a, b, ab_d) || on(a, c, d, cd_a) || on(b, c, d, cd_b) {
                return false;
            }
        }
    }
    true
}

pub type Edge = [usize; 2];
pub fn edge(a: usize, b: usize) -> Edge {
    if a < b { [a, b] } else { [b, a] }
}
/// Undirected edges around a polygon, in winding order.
pub fn face_edges(face: &[usize]) -> impl Iterator<Item = Edge> + '_ {
    (0..face.len()).map(move |i| edge(face[i], face[(i + 1) % face.len()]))
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Mesh {
    pub vertices: Vec<V>,
    #[serde(default)]
    pub edges: Vec<Edge>,
    #[serde(default)]
    pub faces: Vec<Vec<usize>>,
    #[serde(default)]
    pub selected: Vec<usize>,
}
impl Mesh {
    pub fn validate(&self) -> Result<(), String> {
        if self.vertices.iter().any(|v| !v.is_finite()) {
            return Err("Mesh has non-finite coordinates".into());
        }
        for e in &self.edges {
            if e[0] == e[1] || e.iter().any(|&v| v >= self.vertices.len()) {
                return Err("Invalid edge".into());
            }
        }
        let mut faces = BTreeSet::new();
        for f in &self.faces {
            if f.len() < 3
                || f.iter().any(|&v| v >= self.vertices.len())
                || f.iter().copied().collect::<BTreeSet<_>>().len() != f.len()
            {
                return Err("Invalid face indices".into());
            }
            let mut key = f.clone();
            key.sort_unstable();
            if !faces.insert(key) {
                return Err("Duplicate face".into());
            }
            let a = self.vertices[f[0]];
            let normal = (1..f.len() - 1)
                .map(|i| (self.vertices[f[i]] - a).cross(self.vertices[f[i + 1]] - a))
                .sum::<V>();
            if !normal.is_finite() || normal.length() < 1e-14 {
                return Err("Operation would create a zero-area or overflowing face".into());
            }
            if f.len() > 3
                && !simple_polygon(
                    &f.iter().map(|&i| self.vertices[i]).collect::<Vec<_>>(),
                    normal,
                )
            {
                return Err("Operation would create a self-intersecting polygon".into());
            }
        }
        if self.incidence().values().any(|&n| n > 2) {
            return Err("Operation would create a non-manifold edge".into());
        }
        if self.selected.iter().any(|&i| i >= self.vertices.len()) {
            return Err("Invalid selection".into());
        }
        Ok(())
    }
    pub fn incidence(&self) -> BTreeMap<Edge, usize> {
        let mut result = BTreeMap::new();
        for &e in &self.edges {
            result.entry(edge(e[0], e[1])).or_insert(0);
        }
        for face in &self.faces {
            for i in 0..face.len() {
                *result
                    .entry(edge(face[i], face[(i + 1) % face.len()]))
                    .or_insert(0) += 1;
            }
        }
        result
    }
    pub fn neighbors(&self) -> Vec<BTreeSet<usize>> {
        let mut result = vec![BTreeSet::new(); self.vertices.len()];
        for [a, b] in self.incidence().keys() {
            result[*a].insert(*b);
            result[*b].insert(*a);
        }
        result
    }
    pub fn add_vertex(&mut self, p: V) -> usize {
        let id = self.vertices.len();
        self.vertices.push(p);
        id
    }
    pub fn add_face(&mut self, face: Vec<usize>) {
        self.faces.push(face);
    }
    pub fn add_chain(&mut self, points: &[V], closed: bool) -> Vec<usize> {
        let ids: Vec<_> = points.iter().map(|&p| self.add_vertex(p)).collect();
        for pair in ids.windows(2) {
            self.edges.push(edge(pair[0], pair[1]));
        }
        if closed {
            self.edges.push(edge(*ids.last().unwrap(), ids[0]));
        }
        ids
    }
    /// Orient a selected boundary chain; rejects branches and interior edges.
    pub fn boundary_chain(&self, selected: &[usize]) -> Result<(Vec<usize>, bool), String> {
        let selected: BTreeSet<_> = selected.iter().copied().collect();
        let mut adjacency: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for ([a, b], n) in self.incidence() {
            if n <= 1 && selected.contains(&a) && selected.contains(&b) {
                adjacency.entry(a).or_default().push(b);
                adjacency.entry(b).or_default().push(a);
            }
        }
        if adjacency.is_empty() || adjacency.values().any(|n| n.len() > 2) {
            return Err("Select one unbranched boundary chain or loop".into());
        }
        let start = adjacency
            .iter()
            .find(|(_, n)| n.len() == 1)
            .or_else(|| adjacency.first_key_value())
            .map(|(&v, _)| v)
            .unwrap();
        let closed = adjacency.values().all(|n| n.len() == 2);
        let mut chain = vec![start];
        let mut previous = usize::MAX;
        let mut current = start;
        loop {
            let next = adjacency[&current].iter().copied().find(|&n| n != previous);
            let Some(next) = next else {
                break;
            };
            if next == start {
                break;
            }
            if chain.contains(&next) {
                return Err("Invalid boundary cycle".into());
            }
            chain.push(next);
            previous = current;
            current = next;
        }
        if chain.len() != adjacency.len() {
            return Err("Selection contains disconnected boundaries".into());
        }
        // Choose orientation opposite to an existing face edge, so the new bridge shares winding.
        if chain.len() >= 2
            && self
                .faces
                .iter()
                .any(|f| (0..f.len()).any(|i| f[i] == chain[0] && f[(i + 1) % f.len()] == chain[1]))
        {
            chain.reverse();
        }
        Ok((chain, closed))
    }
    pub fn delete_selected(&mut self) {
        let selected: BTreeSet<_> = self.selected.iter().copied().collect();
        self.faces
            .retain(|f| f.iter().all(|i| !selected.contains(i)));
        self.edges
            .retain(|e| e.iter().all(|i| !selected.contains(i)));
        let mut map = vec![0; self.vertices.len()];
        let mut vertices = Vec::new();
        for (i, &p) in self.vertices.iter().enumerate() {
            if !selected.contains(&i) {
                map[i] = vertices.len();
                vertices.push(p);
            }
        }
        for f in &mut self.faces {
            for i in f {
                *i = map[*i];
            }
        }
        for e in &mut self.edges {
            for i in e {
                *i = map[*i];
            }
        }
        self.vertices = vertices;
        self.selected.clear();
    }
    pub fn shortest_path(&self, start: usize, end: usize) -> Result<Vec<usize>, String> {
        if start >= self.vertices.len() || end >= self.vertices.len() {
            return Err("Path vertex out of bounds".into());
        }
        let adjacency = self.neighbors();
        let mut previous = vec![usize::MAX; self.vertices.len()];
        let mut queue = VecDeque::from([start]);
        previous[start] = start;
        while let Some(v) = queue.pop_front() {
            if v == end {
                break;
            }
            for &n in &adjacency[v] {
                if previous[n] == usize::MAX {
                    previous[n] = v;
                    queue.push_back(n);
                }
            }
        }
        if previous[end] == usize::MAX {
            return Err("No connected path".into());
        }
        let mut path = vec![end];
        while *path.last().unwrap() != start {
            path.push(previous[*path.last().unwrap()]);
        }
        path.reverse();
        Ok(path)
    }
    pub fn split_edge(&mut self, a: usize, b: usize, p: V) -> Result<usize, String> {
        if !self.incidence().contains_key(&edge(a, b)) {
            return Err("Edge does not exist".into());
        }
        let id = self.add_vertex(p);
        self.edges.retain(|e| edge(e[0], e[1]) != edge(a, b));
        self.edges.extend([edge(a, id), edge(id, b)]);
        for face in &mut self.faces {
            if let Some(i) =
                (0..face.len()).find(|&i| edge(face[i], face[(i + 1) % face.len()]) == edge(a, b))
            {
                face.insert(i + 1, id);
            }
        }
        Ok(id)
    }
}

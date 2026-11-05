use crate::math::{closest_triangle, ray_triangle};
use glam::DVec3 as V;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug)]
struct Bounds {
    min: V,
    max: V,
}
impl Bounds {
    fn empty() -> Self {
        Self {
            min: V::splat(f64::INFINITY),
            max: V::splat(f64::NEG_INFINITY),
        }
    }
    fn include(&mut self, p: V) {
        self.min = self.min.min(p);
        self.max = self.max.max(p);
    }
    fn distance2(self, p: V) -> f64 {
        p.distance_squared(p.clamp(self.min, self.max))
    }
    fn ray(self, o: V, d: V, maximum: f64) -> bool {
        let mut near: f64 = 0.0;
        let mut far = maximum;
        for axis in 0..3 {
            if d[axis].abs() < 1e-15 {
                if o[axis] < self.min[axis] || o[axis] > self.max[axis] {
                    return false;
                }
            } else {
                let a = (self.min[axis] - o[axis]) / d[axis];
                let b = (self.max[axis] - o[axis]) / d[axis];
                near = near.max(a.min(b));
                far = far.min(a.max(b));
                if near > far {
                    return false;
                }
            }
        }
        true
    }
}

#[derive(Debug)]
enum Node {
    Leaf(Bounds, Vec<usize>),
    Branch(Bounds, Box<Node>, Box<Node>),
}
impl Node {
    fn bounds(&self) -> Bounds {
        match self {
            Self::Leaf(b, _) | Self::Branch(b, _, _) => *b,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Hit {
    pub point: V,
    pub normal: V,
    pub triangle: usize,
    pub distance: f64,
}

#[derive(Debug)]
pub struct Section {
    pub points: Vec<V>,
    pub closed: bool,
}

#[derive(Debug)]
pub struct Surface {
    vertices: Vec<V>,
    triangles: Vec<[usize; 3]>,
    root: Node,
}
impl Surface {
    pub fn new(vertices: Vec<V>, triangles: Vec<[usize; 3]>) -> Result<Self, String> {
        if vertices.iter().any(|p| !p.is_finite()) {
            return Err("Source coordinates must be finite".into());
        }
        if triangles.is_empty() {
            return Err("Source needs triangles".into());
        }
        for t in &triangles {
            if t.iter().any(|&i| i >= vertices.len()) {
                return Err("Source triangle index out of bounds".into());
            }
            let u = vertices[t[1]] - vertices[t[0]];
            let v = vertices[t[2]] - vertices[t[0]];
            let scale = u.abs().max(v.abs()).max_element();
            if !scale.is_finite() {
                return Err("Source triangle coordinates overflow".into());
            }
            if scale == 0.0 || (u / scale).cross(v / scale).length_squared() <= 1e-28 {
                return Err("Source contains a zero-area triangle".into());
            }
        }
        fn build(vertices: &[V], triangles: &[[usize; 3]], mut ids: Vec<usize>) -> Node {
            let mut bounds = Bounds::empty();
            for &i in &ids {
                for &v in &triangles[i] {
                    bounds.include(vertices[v]);
                }
            }
            if ids.len() <= 8 {
                return Node::Leaf(bounds, ids);
            }
            let size = bounds.max - bounds.min;
            let axis = if size.x > size.y && size.x > size.z {
                0
            } else if size.y > size.z {
                1
            } else {
                2
            };
            let center = |i: usize| triangles[i].iter().map(|&v| vertices[v][axis]).sum::<f64>();
            ids.sort_unstable_by(|&a, &b| center(a).total_cmp(&center(b)));
            let right = ids.split_off(ids.len() / 2);
            Node::Branch(
                bounds,
                Box::new(build(vertices, triangles, ids)),
                Box::new(build(vertices, triangles, right)),
            )
        }
        let root = build(&vertices, &triangles, (0..triangles.len()).collect());
        Ok(Self {
            vertices,
            triangles,
            root,
        })
    }
    fn triangle(&self, id: usize) -> [V; 3] {
        self.triangles[id].map(|v| self.vertices[v])
    }
    fn hit(&self, id: usize, point: V, distance: f64) -> Hit {
        let [a, b, c] = self.triangle(id);
        Hit {
            point,
            normal: (b - a).cross(c - a).normalize(),
            triangle: id,
            distance,
        }
    }
    pub fn closest(&self, point: V) -> Hit {
        fn visit(s: &Surface, node: &Node, p: V, best: &mut (f64, usize, V)) {
            if node.bounds().distance2(p) > best.0 {
                return;
            }
            match node {
                Node::Leaf(_, ids) => {
                    for &i in ids {
                        let [a, b, c] = s.triangle(i);
                        let q = closest_triangle(p, a, b, c);
                        let d = p.distance_squared(q);
                        if d < best.0 {
                            *best = (d, i, q);
                        }
                    }
                }
                Node::Branch(_, a, b) => {
                    let (a, b) = if a.bounds().distance2(p) < b.bounds().distance2(p) {
                        (a, b)
                    } else {
                        (b, a)
                    };
                    visit(s, a, p, best);
                    visit(s, b, p, best);
                }
            }
        }
        let mut best = (f64::INFINITY, 0, V::ZERO);
        visit(self, &self.root, point, &mut best);
        self.hit(best.1, best.2, best.0.sqrt())
    }
    pub fn raycast(&self, origin: V, direction: V) -> Option<Hit> {
        if !origin.is_finite() || !direction.is_finite() || direction.length_squared() < 1e-24 {
            return None;
        }
        let direction = direction.normalize();
        fn visit(s: &Surface, node: &Node, o: V, d: V, best: &mut (f64, usize)) {
            if !node.bounds().ray(o, d, best.0) {
                return;
            }
            match node {
                Node::Leaf(_, ids) => {
                    for &i in ids {
                        let [a, b, c] = s.triangle(i);
                        if let Some(t) = ray_triangle(o, d, a, b, c)
                            && t < best.0
                        {
                            *best = (t, i);
                        }
                    }
                }
                Node::Branch(_, a, b) => {
                    visit(s, a, o, d, best);
                    visit(s, b, o, d, best);
                }
            }
        }
        let mut best = (f64::INFINITY, 0);
        visit(self, &self.root, origin, direction, &mut best);
        best.0
            .is_finite()
            .then(|| self.hit(best.1, origin + direction * best.0, best.0))
    }
    /// Intersect the full source with a plane and stitch connected segments into loops/chains.
    pub fn contours(&self, origin: V, normal: V) -> Result<Vec<Vec<V>>, String> {
        Ok(self
            .sections(origin, normal)?
            .into_iter()
            .map(|s| s.points)
            .collect())
    }
    /// Plane sections with exact closure information from segment stitching.
    pub fn sections(&self, origin: V, normal: V) -> Result<Vec<Section>, String> {
        if !origin.is_finite() || !normal.is_finite() || normal.length_squared() < 1e-20 {
            return Err("Contour plane needs a normal".into());
        }
        let n = normal.normalize();
        let tolerance = (self.root.bounds().max - self.root.bounds().min)
            .length()
            .max(1.0)
            * 1e-7;
        let mut segments = Vec::new();
        for i in 0..self.triangles.len() {
            let p = self.triangle(i);
            let d = p.map(|v| (v - origin).dot(n));
            if d.iter().all(|x| x.abs() < tolerance * 0.01) {
                continue;
            }
            let mut cuts: Vec<V> = Vec::new();
            for j in 0..3 {
                let k = (j + 1) % 3;
                if d[j].abs() < tolerance * 0.01 {
                    cuts.push(p[j]);
                }
                if d[j] * d[k] < 0.0 {
                    cuts.push(p[j].lerp(p[k], d[j] / (d[j] - d[k])));
                }
            }
            let mut unique = Vec::<V>::new();
            for p in cuts {
                if unique.iter().all(|q| p.distance(*q) > tolerance) {
                    unique.push(p);
                }
            }
            if unique.len() == 2
                && !segments.iter().any(|s: &[V; 2]| {
                    (s[0].distance(unique[0]) < tolerance && s[1].distance(unique[1]) < tolerance)
                        || (s[0].distance(unique[1]) < tolerance
                            && s[1].distance(unique[0]) < tolerance)
                })
            {
                segments.push([unique[0], unique[1]]);
            }
        }
        let mut chains = Vec::new();
        while let Some([a, b]) = segments.pop() {
            let mut chain = vec![a, b];
            loop {
                let end = *chain.last().unwrap();
                if let Some((i, flip)) = segments.iter().enumerate().find_map(|(i, s)| {
                    if end.distance(s[0]) < tolerance {
                        Some((i, false))
                    } else if end.distance(s[1]) < tolerance {
                        Some((i, true))
                    } else {
                        None
                    }
                }) {
                    let s = segments.swap_remove(i);
                    chain.push(s[usize::from(!flip)]);
                } else {
                    break;
                }
            }
            chain.reverse();
            loop {
                let end = *chain.last().unwrap();
                if let Some((i, flip)) = segments.iter().enumerate().find_map(|(i, s)| {
                    if end.distance(s[0]) < tolerance {
                        Some((i, false))
                    } else if end.distance(s[1]) < tolerance {
                        Some((i, true))
                    } else {
                        None
                    }
                }) {
                    let s = segments.swap_remove(i);
                    chain.push(s[usize::from(!flip)]);
                } else {
                    break;
                }
            }
            let closed = chain.len() >= 4 && chain[0].distance(*chain.last().unwrap()) < tolerance;
            if closed {
                chain.pop();
            }
            chains.push(Section {
                points: chain,
                closed,
            });
        }
        Ok(chains)
    }
}

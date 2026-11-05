//! Transactional topology primitives; these are not interactive RetopoFlow tools.
use crate::mesh::{Edge, Mesh, edge};
use glam::DVec3;
use std::collections::{BTreeMap, BTreeSet, VecDeque};

/// Insert a quad-strip loop. `factor` is measured from `a` toward `b` and
/// transported across each quad (rather than along sorted vertex indices).
/// Any failure leaves the input mesh unchanged.
pub fn insert_loop(mesh: &mut Mesh, a: usize, b: usize, factor: f64) -> Result<(), String> {
    mesh.validate()?;
    if !factor.is_finite() || factor <= 0.0 || factor >= 1.0 {
        return Err("Loop factor must be finite and strictly between zero and one".into());
    }
    let mut incident: BTreeMap<Edge, Vec<usize>> = BTreeMap::new();
    for (fi, f) in mesh.faces.iter().enumerate() {
        for i in 0..f.len() {
            incident
                .entry(edge(f[i], f[(i + 1) % f.len()]))
                .or_default()
                .push(fi);
        }
    }
    if a == b || !incident.contains_key(&edge(a, b)) {
        return Err("Choose an edge belonging to a quad".into());
    }
    let mut oriented = BTreeMap::from([(edge(a, b), [a, b])]);
    let mut queue = VecDeque::from([edge(a, b)]);
    let mut cuts: BTreeMap<usize, (Edge, Edge)> = BTreeMap::new();
    while let Some(e) = queue.pop_front() {
        let [u, v] = oriented[&e];
        for &fi in &incident[&e] {
            let f = &mesh.faces[fi];
            if f.len() != 4 {
                return Err("Loop path encounters a non-quad face".into());
            }
            let i = (0..4).find(|&i| edge(f[i], f[(i + 1) % 4]) == e).unwrap();
            let directed = if f[i] == u && f[(i + 1) % 4] == v {
                [f[(i + 3) % 4], f[(i + 2) % 4]]
            } else {
                [f[(i + 2) % 4], f[(i + 3) % 4]]
            };
            let opposite = edge(directed[0], directed[1]);
            if let Some(&(x, y)) = cuts.get(&fi) {
                if !((x == e && y == opposite) || (y == e && x == opposite)) {
                    return Err("Loop crosses itself in a face".into());
                }
            } else {
                cuts.insert(fi, (e, opposite));
            }
            if let Some(previous) = oriented.get(&opposite) {
                if *previous != directed {
                    return Err("Ambiguous or twisted quad ring".into());
                }
            } else {
                oriented.insert(opposite, directed);
                queue.push_back(opposite);
            }
        }
    }
    let mut result = mesh.clone();
    let mut ids = BTreeMap::new();
    for (&e, &[u, v]) in &oriented {
        let id = result.add_vertex(mesh.vertices[u].lerp(mesh.vertices[v], factor));
        ids.insert(e, id);
    }
    result.faces.clear();
    for (fi, f) in mesh.faces.iter().enumerate() {
        if let Some(&(e, opposite)) = cuts.get(&fi) {
            let i = (0..4).find(|&i| edge(f[i], f[(i + 1) % 4]) == e).unwrap();
            let p = ids[&e];
            let q = ids[&opposite];
            result.faces.push(vec![f[i], p, q, f[(i + 3) % 4]]);
            result
                .faces
                .push(vec![p, f[(i + 1) % 4], f[(i + 2) % 4], q]);
        } else {
            result.faces.push(f.clone());
        }
    }
    split_explicit_edges(&mut result, &ids);
    result.selected = ids.values().copied().collect();
    result.validate()?;
    *mesh = result;
    Ok(())
}

fn split_explicit_edges(mesh: &mut Mesh, ids: &BTreeMap<Edge, usize>) {
    mesh.edges = mesh
        .edges
        .iter()
        .flat_map(|&[a, b]| match ids.get(&edge(a, b)) {
            Some(&p) => vec![edge(a, p), edge(p, b)],
            None => vec![edge(a, b)],
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
}

/// Cut polygon faces by a plane, sharing intersections across incident faces.
/// Faces with more than two crossing events (or crossing coplanar edge runs)
/// are explicitly unsupported. No caps are generated. Failures are atomic.
pub fn knife(mesh: &mut Mesh, origin: DVec3, normal: DVec3) -> Result<(), String> {
    knife_faces(mesh, origin, normal, None)
}

/// Like [`knife`], but only faces listed in `allowed` are split (a finite,
/// screen-space knife). Uncut neighbours receive the new edge vertices in their
/// boundary so no cracks or T-junctions are created.
pub fn knife_faces(
    mesh: &mut Mesh,
    origin: DVec3,
    normal: DVec3,
    allowed: Option<&BTreeSet<usize>>,
) -> Result<(), String> {
    mesh.validate()?;
    if let Some(allowed) = allowed
        && allowed.iter().any(|&f| f >= mesh.faces.len())
    {
        return Err("Knife face index out of bounds".into());
    }
    if !origin.is_finite()
        || !normal.is_finite()
        || normal.length() == 0.0
        || !normal.length().is_finite()
    {
        return Err("Plane must have a finite origin and nonzero finite normal".into());
    }
    let normal = normal.normalize();
    let distances: Vec<_> = mesh
        .vertices
        .iter()
        .map(|p| (*p - origin).dot(normal))
        .collect();
    if distances.iter().any(|d| !d.is_finite()) {
        return Err("Plane distance overflow".into());
    }
    // Absolute tolerance in mesh units; avoids duplicate intersections at vertices.
    let sign = |d: f64| {
        if d.abs() <= 1e-10 {
            0i8
        } else if d > 0.0 {
            1
        } else {
            -1
        }
    };
    let signs: Vec<_> = distances.iter().map(|&d| sign(d)).collect();
    let mut result = mesh.clone();
    let mut ids = BTreeMap::new();
    let mut faces: Vec<Vec<Vec<usize>>> = Vec::with_capacity(mesh.faces.len());
    let mut deferred = Vec::new();
    let mut selection = BTreeSet::new();
    for (index, f) in mesh.faces.iter().enumerate() {
        if !f.iter().any(|&v| signs[v] > 0) || !f.iter().any(|&v| signs[v] < 0) {
            faces.push(vec![f.clone()]);
            continue;
        }
        if allowed.is_some_and(|a| !a.contains(&index)) {
            deferred.push(index);
            faces.push(Vec::new());
            continue;
        }
        let mut pieces = Vec::new();
        let mut crossings = 0;
        for i in 0..f.len() {
            let prev = f[(i + f.len() - 1) % f.len()];
            let a = f[i];
            let b = f[(i + 1) % f.len()];
            if signs[a] == 0 {
                if signs[b] == 0 {
                    return Err("Unsupported crossing face with a coplanar edge".into());
                }
                if signs[prev] * signs[b] < 0 {
                    crossings += 1;
                } else {
                    return Err("Unsupported concave face tangent to the cutting plane".into());
                }
            } else if signs[a] * signs[b] < 0 {
                crossings += 1;
            }
        }
        if crossings != 2 {
            return Err("Unsupported concave face with multiple plane crossings".into());
        }
        let mut augmented = Vec::new();
        for i in 0..f.len() {
            let a = f[i];
            let b = f[(i + 1) % f.len()];
            augmented.push((a, signs[a]));
            if signs[a] == 0 {
                selection.insert(a);
            }
            if signs[a] * signs[b] < 0 {
                let e = edge(a, b);
                let id = *ids.entry(e).or_insert_with(|| {
                    let t = distances[a] / (distances[a] - distances[b]);
                    result.add_vertex(mesh.vertices[a].lerp(mesh.vertices[b], t))
                });
                augmented.push((id, 0));
                selection.insert(id);
            }
        }
        for side in [-1, 1] {
            let polygon: Vec<_> = augmented
                .iter()
                .filter(|(_, s)| *s == 0 || *s == side)
                .map(|&(v, _)| v)
                .collect();
            if polygon.len() < 3 {
                return Err("Plane cut would create a degenerate polygon".into());
            }
            pieces.push(polygon);
        }
        faces.push(pieces);
    }
    // Uncut faces adjacent to a cut edge gain the shared intersection vertex.
    for index in deferred {
        let f = &mesh.faces[index];
        let mut polygon = Vec::with_capacity(f.len() + 2);
        for i in 0..f.len() {
            polygon.push(f[i]);
            if let Some(&id) = ids.get(&edge(f[i], f[(i + 1) % f.len()])) {
                polygon.push(id);
            }
        }
        faces[index] = vec![polygon];
    }
    result.faces = faces.into_iter().flatten().collect();
    if result.vertices.len() == mesh.vertices.len() && allowed.is_some() {
        return Err("Knife line does not cross any faces".into());
    }
    split_explicit_edges(&mut result, &ids);
    result.selected = selection.into_iter().collect();
    result.validate()?;
    *mesh = result;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn grid(w: usize, h: usize) -> Mesh {
        let mut m = Mesh::default();
        for y in 0..=h {
            for x in 0..=w {
                m.vertices.push(DVec3::new(x as f64, y as f64, 0.0));
            }
        }
        for y in 0..h {
            for x in 0..w {
                let a = y * (w + 1) + x;
                m.faces.push(vec![a, a + 1, a + w + 2, a + w + 1]);
            }
        }
        m
    }
    fn winding(m: &Mesh) {
        for f in &m.faces {
            let area: DVec3 = (1..f.len() - 1)
                .map(|i| {
                    (m.vertices[f[i]] - m.vertices[f[0]])
                        .cross(m.vertices[f[i + 1]] - m.vertices[f[0]])
                })
                .sum();
            assert!(area.z > 0.0);
        }
        m.validate().unwrap();
    }
    #[test]
    fn open_grid_loop() {
        let mut m = grid(3, 3);
        insert_loop(&mut m, 5, 6, 0.25).unwrap();
        assert_eq!(m.faces.len(), 12);
        assert_eq!(m.vertices.len(), 20);
        assert_eq!(m.selected.len(), 4);
        for &id in &m.selected {
            assert_eq!(m.vertices[id].x, 1.25);
        }
        winding(&m);
    }
    #[test]
    fn boundary_start_and_reverse_factor() {
        let mut m = grid(1, 3);
        insert_loop(&mut m, 1, 0, 0.25).unwrap();
        assert_eq!(m.faces.len(), 6);
        for &id in &m.selected {
            assert_eq!(m.vertices[id].x, 0.75);
        }
        winding(&m);
    }
    #[test]
    fn closed_ring() {
        let mut m = Mesh::default();
        for r in [1.0, 2.0] {
            for i in 0..4 {
                let theta = i as f64 * std::f64::consts::FRAC_PI_2;
                m.vertices
                    .push(DVec3::new(r * theta.cos(), r * theta.sin(), 0.0));
            }
        }
        for i in 0..4 {
            let j = (i + 1) % 4;
            m.faces.push(vec![i, i + 4, j + 4, j]);
        }
        insert_loop(&mut m, 0, 4, 0.3).unwrap();
        assert_eq!(m.vertices.len(), 12);
        assert_eq!(m.faces.len(), 8);
        winding(&m);
    }
    #[test]
    fn invalid_factors_and_edges_are_atomic() {
        for t in [0.0, 1.0, -0.5, 2.0, f64::NAN, f64::INFINITY] {
            let mut m = grid(1, 1);
            let before = format!("{m:?}");
            assert!(insert_loop(&mut m, 0, 1, t).is_err());
            assert_eq!(format!("{m:?}"), before);
        }
        let mut m = grid(1, 1);
        assert!(insert_loop(&mut m, 0, 3, 0.5).is_err());
        assert!(insert_loop(&mut m, 0, 999, 0.5).is_err());
    }
    #[test]
    fn nonquad_path_is_atomic() {
        let mut m = grid(1, 2);
        m.faces[1].remove(2);
        let before = format!("{m:?}");
        assert!(insert_loop(&mut m, 0, 1, 0.5).is_err());
        assert_eq!(format!("{m:?}"), before);
    }
    #[test]
    fn nonmanifold_loop_is_atomic() {
        let mut m = grid(1, 1);
        m.vertices
            .extend([DVec3::new(0.5, -1.0, 0.0), DVec3::new(0.5, 0.0, 1.0)]);
        m.faces.extend([vec![1, 0, 4], vec![0, 1, 5]]);
        let before = format!("{m:?}");
        assert!(insert_loop(&mut m, 0, 1, 0.5).is_err());
        assert_eq!(format!("{m:?}"), before);
    }
    #[test]
    fn knife_shares_intersections_and_splits_explicit_edges() {
        let mut m = grid(1, 2);
        m.edges.push(edge(2, 3));
        knife(&mut m, DVec3::new(0.5, 0.0, 0.0), DVec3::X).unwrap();
        assert_eq!(m.vertices.len(), 9);
        assert_eq!(m.faces.len(), 4);
        assert_eq!(m.selected.len(), 3);
        let shared = *m
            .selected
            .iter()
            .find(|&&i| m.vertices[i].y == 1.0)
            .unwrap();
        assert_eq!(m.faces.iter().filter(|f| f.contains(&shared)).count(), 4);
        assert!(m.edges.contains(&edge(2, shared)));
        assert!(m.edges.contains(&edge(shared, 3)));
        winding(&m);
    }
    #[test]
    fn limited_knife_cuts_only_allowed_faces_without_cracks() {
        // 1x2 grid: faces 0 (y 0..1) and 1 (y 1..2); cut along x=0.5 in face 0 only.
        let mut m = grid(1, 2);
        let allowed = BTreeSet::from([0]);
        knife_faces(&mut m, DVec3::new(0.5, 0.0, 0.0), DVec3::X, Some(&allowed)).unwrap();
        assert_eq!(m.faces.len(), 3);
        // The shared edge (y=1) vertex is also inserted into the uncut face.
        assert!(m.faces.iter().any(|f| f.len() == 5));
        assert_eq!(m.selected.len(), 2);
        winding(&m);
        let mut untouched = grid(1, 2);
        let before = format!("{untouched:?}");
        let none = BTreeSet::new();
        assert!(
            knife_faces(
                &mut untouched,
                DVec3::new(0.5, 0.0, 0.0),
                DVec3::X,
                Some(&none)
            )
            .is_err()
        );
        assert_eq!(format!("{untouched:?}"), before);
    }
    #[test]
    fn knife_through_existing_vertices() {
        let mut m = grid(1, 1);
        knife(&mut m, DVec3::ZERO, DVec3::new(1.0, -1.0, 0.0)).unwrap();
        assert_eq!(m.vertices.len(), 4);
        assert_eq!(m.faces.len(), 2);
        assert_eq!(m.selected, vec![0, 3]);
        winding(&m);
    }
    #[test]
    fn knife_preserves_one_sided_and_coplanar_faces() {
        for (origin, normal) in [
            (DVec3::ZERO, DVec3::Z),
            (DVec3::ZERO, DVec3::X),
            (DVec3::new(3.0, 0.0, 0.0), DVec3::X),
        ] {
            let mut m = grid(2, 1);
            let faces = m.faces.clone();
            knife(&mut m, origin, normal).unwrap();
            assert_eq!(m.faces, faces);
            assert_eq!(m.vertices.len(), 6);
        }
    }
    #[test]
    fn knife_rejects_multiple_crossings_atomically() {
        let mut m = Mesh::default();
        for (x, y) in [
            (0., 0.),
            (3., 0.),
            (3., 3.),
            (2., 3.),
            (2., 1.),
            (1., 1.),
            (1., 3.),
            (0., 3.),
        ] {
            m.vertices.push(DVec3::new(x, y, 0.));
        }
        m.faces.push((0..8).collect());
        let before = format!("{m:?}");
        assert!(knife(&mut m, DVec3::new(0., 2., 0.), DVec3::Y).is_err());
        assert_eq!(format!("{m:?}"), before);
        assert!(knife(&mut m, DVec3::ZERO, DVec3::ZERO).is_err());
        assert!(knife(&mut m, DVec3::ZERO, DVec3::splat(f64::NAN)).is_err());
    }
}

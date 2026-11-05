//! Transactional native pen operations (not a full PolyPen implementation).
use crate::{
    mesh::{Mesh, edge},
    surface::Surface,
};
use glam::DVec3;
use std::collections::{BTreeMap, BTreeSet};

fn dedup_edges(mesh: &mut Mesh) {
    mesh.edges = mesh
        .edges
        .iter()
        .filter(|e| e[0] != e[1])
        .map(|e| edge(e[0], e[1]))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
}

// Mesh::validate checks edge incidence; also reject inconsistent winding and
// disconnected face fans at vertices (a bow-tie weld).
fn validate(mesh: &Mesh) -> Result<(), String> {
    mesh.validate()?;
    let mut directed = BTreeSet::new();
    let mut fans: BTreeMap<usize, Vec<[usize; 2]>> = BTreeMap::new();
    for f in &mesh.faces {
        for i in 0..f.len() {
            let v = f[i];
            let next = f[(i + 1) % f.len()];
            if !directed.insert([v, next]) {
                return Err("Inconsistent face winding".into());
            }
            fans.entry(v)
                .or_default()
                .push([f[(i + f.len() - 1) % f.len()], next]);
        }
    }
    for links in fans.values() {
        let mut reached = BTreeSet::from([links[0][0]]);
        loop {
            let before = reached.len();
            for &[a, b] in links {
                if reached.contains(&a) || reached.contains(&b) {
                    reached.extend([a, b]);
                }
            }
            if reached.len() == before {
                break;
            }
        }
        if links.iter().flatten().any(|v| !reached.contains(v)) {
            return Err("Operation would create a disconnected vertex fan".into());
        }
    }
    Ok(())
}

/// PolyPen insertion parameters beyond the clicked point.
#[derive(Clone, Debug, Default)]
pub struct PenOptions {
    /// Split this existing edge at the click instead of extending the selection.
    pub edge: Option<[usize; 2]>,
    /// Reuse an existing vertex within this distance (mesh units).
    pub snap_distance: f64,
    /// Reuse exactly this vertex (screen-space snapping done by the caller).
    pub vertex: Option<usize>,
    /// Turn the triangle on the selected edge into a quad (Tri/Quad mode).
    pub quad: bool,
}

/// Project a click onto the source, then insert/reuse a vertex.
/// Zero/one selected vertices create a loose chain, selecting the clicked vertex.
/// Two selected vertices must define a boundary/loose edge and create a triangle;
/// selection becomes its last boundary edge `[old_second, clicked]`, ordered for
/// the next extrusion. Larger selections are rejected rather than guessed.
/// An explicit edge only splits that edge, selecting the split/reused vertex.
/// All failures leave the mesh unchanged. Distances are in mesh coordinates.
pub fn insert(
    mesh: &mut Mesh,
    surface: &Surface,
    point: DVec3,
    edge_hint: Option<[usize; 2]>,
    snap_distance: f64,
) -> Result<(), String> {
    insert_with(
        mesh,
        surface,
        point,
        &PenOptions {
            edge: edge_hint,
            snap_distance,
            ..PenOptions::default()
        },
    )
}

fn polygon_normal(mesh: &Mesh, face: &[usize]) -> DVec3 {
    let origin = mesh.vertices[face[0]];
    (1..face.len() - 1)
        .map(|i| (mesh.vertices[face[i]] - origin).cross(mesh.vertices[face[i + 1]] - origin))
        .sum()
}

/// Tri/Quad: with the selected edge `[a, b]` bounding exactly one triangle,
/// insert the new vertex into that triangle, making a quad. Returns `None` when
/// the selection is not such an edge (the caller then inserts a triangle).
fn quad_from_triangle(next: &Mesh, id: usize) -> Option<Result<Mesh, String>> {
    let &[a, b] = next.selected.as_slice() else {
        return None;
    };
    let owners: Vec<_> = next
        .faces
        .iter()
        .enumerate()
        .filter(|(_, f)| (0..f.len()).any(|i| edge(f[i], f[(i + 1) % f.len()]) == edge(a, b)))
        .map(|(i, _)| i)
        .collect();
    let &[owner] = owners.as_slice() else {
        return None;
    };
    let triangle = next.faces[owner].clone();
    if triangle.len() != 3 {
        return None;
    }
    if triangle.contains(&id) {
        return Some(Err("Click away from the triangle to make a quad".into()));
    }
    let apex = *triangle.iter().find(|&&v| v != a && v != b)?;
    // Prefer inserting between the selected pair; fall back to the edge between
    // the newest vertex `b` and the apex if the first quad would self-intersect.
    let mut last_error = String::new();
    for (u, v) in [(a, b), (b, apex)] {
        let shared = next.faces.iter().enumerate().any(|(fi, f)| {
            fi != owner && (0..f.len()).any(|i| edge(f[i], f[(i + 1) % f.len()]) == edge(u, v))
        });
        if shared {
            continue;
        }
        let mut candidate = next.clone();
        let face = &mut candidate.faces[owner];
        let i = (0..3).find(|&i| edge(face[i], face[(i + 1) % 3]) == edge(u, v))?;
        face.insert(i + 1, id);
        candidate.edges.retain(|e| edge(e[0], e[1]) != edge(u, v));
        candidate.selected = vec![b, id];
        dedup_edges(&mut candidate);
        match validate(&candidate) {
            Ok(()) => return Some(Ok(candidate)),
            Err(error) => last_error = error,
        }
    }
    Some(Err(if last_error.is_empty() {
        "Cannot extend that triangle into a quad".into()
    } else {
        format!("Quad rejected: {last_error}")
    }))
}

/// [`insert`] with screen-space vertex snapping and Tri/Quad support.
pub fn insert_with(
    mesh: &mut Mesh,
    surface: &Surface,
    point: DVec3,
    options: &PenOptions,
) -> Result<(), String> {
    mesh.validate()?;
    let snap_distance = options.snap_distance;
    if !point.is_finite() || !snap_distance.is_finite() || snap_distance < 0.0 {
        return Err("Pen requires a finite point and nonnegative finite snap distance".into());
    }
    if options.vertex.is_some_and(|v| v >= mesh.vertices.len()) {
        return Err("Pen snap vertex does not exist".into());
    }
    let mut next = mesh.clone();
    let projected = surface.closest(point).point;
    if let Some([a, b]) = options.edge {
        if !next.incidence().contains_key(&edge(a, b)) {
            return Err("Pen split edge does not exist".into());
        }
        let start = next.vertices[a];
        let delta = next.vertices[b] - start;
        let length2 = delta.length_squared();
        if length2 <= 0.0 {
            return Err("Cannot split a zero-length edge".into());
        }
        let t = ((projected - start).dot(delta) / length2).clamp(0.0, 1.0);
        let closest = start + t * delta;
        let endpoint = [a, b]
            .into_iter()
            .min_by(|&i, &j| {
                next.vertices[i]
                    .distance(closest)
                    .total_cmp(&next.vertices[j].distance(closest))
            })
            .unwrap();
        let id = if closest.distance(next.vertices[endpoint]) <= snap_distance {
            endpoint
        } else {
            next.split_edge(a, b, surface.closest(closest).point)?
        };
        next.selected = vec![id];
    } else {
        if next.selected.len() > 2
            || next.selected.iter().collect::<BTreeSet<_>>().len() != next.selected.len()
        {
            return Err("Pen requires zero, one, or two distinct selected vertices; polygons are not inferred".into());
        }
        if next.selected.len() == 2
            && !next
                .incidence()
                .contains_key(&edge(next.selected[0], next.selected[1]))
        {
            return Err("Select an existing edge for triangle insertion".into());
        }
        let nearest = options.vertex.or_else(|| {
            next.vertices
                .iter()
                .enumerate()
                .map(|(i, p)| (i, p.distance(projected)))
                .filter(|(_, d)| *d <= snap_distance)
                .min_by(|a, b| a.1.total_cmp(&b.1))
                .map(|(i, _)| i)
        });
        let id = nearest.unwrap_or_else(|| next.add_vertex(projected));
        if options.quad
            && let Some(result) = quad_from_triangle(&next, id)
        {
            *mesh = result?;
            return Ok(());
        }
        match next.selected.as_slice() {
            [] => next.selected = vec![id],
            &[a] => {
                if a != id {
                    next.edges.push(edge(a, id));
                }
                next.selected = vec![id];
            }
            &[a, b] => {
                if a == id || b == id {
                    next.selected = vec![id];
                } else {
                    let mut face = vec![a, b, id];
                    // Evaluate both orientations against every shared edge, not
                    // just the selected edge (the click may reuse a vertex).
                    let fits = |f: &[usize]| {
                        (0..3).all(|i| {
                            let (u, v) = (f[i], f[(i + 1) % 3]);
                            next.faces.iter().all(|old| {
                                !(0..old.len())
                                    .any(|j| old[j] == u && old[(j + 1) % old.len()] == v)
                            })
                        })
                    };
                    let reversed: Vec<_> = face.iter().rev().copied().collect();
                    if fits(&face) && fits(&reversed) {
                        // Unconstrained by neighbours: face the source normal.
                        let centroid = face.iter().map(|&i| next.vertices[i]).sum::<DVec3>() / 3.0;
                        if polygon_normal(&next, &face).dot(surface.closest(centroid).normal) < 0.0
                        {
                            face = reversed;
                        }
                    } else if !fits(&face) {
                        face = reversed;
                    }
                    if !fits(&face) {
                        return Err("No consistent triangle winding".into());
                    }
                    let new_normal = polygon_normal(&next, &face);
                    for old in &next.faces {
                        if (0..old.len())
                            .any(|i| edge(old[i], old[(i + 1) % old.len()]) == edge(a, b))
                            && new_normal.dot(polygon_normal(&next, old)) <= 0.0
                        {
                            return Err("Pen extrusion overlaps or folds back across the selected face; click outside its boundary".into());
                        }
                    }
                    next.faces.push(face);
                    next.selected = vec![b, id];
                }
            }
            _ => unreachable!(),
        }
    }
    dedup_edges(&mut next);
    validate(&next)?;
    *mesh = next;
    Ok(())
}

/// Weld the distinct selected vertices at their source-projected center.
/// Unselected vertices retain their order and positions. Collapsed and duplicate
/// faces are removed; any remaining invalid topology rejects the entire weld.
/// The single welded vertex remains selected for a subsequent pen chain.
pub fn merge_selected(mesh: &mut Mesh, surface: &Surface) -> Result<(), String> {
    mesh.validate()?;
    let selected: BTreeSet<_> = mesh.selected.iter().copied().collect();
    if selected.len() < 2 {
        return Err("Select at least two vertices to merge".into());
    }
    let center = selected
        .iter()
        .map(|&i| mesh.vertices[i] / selected.len() as f64)
        .sum();
    let center = surface.closest(center).point;
    let survivor = *selected.first().unwrap();
    let mut next = Mesh::default();
    let mut remap = vec![0; mesh.vertices.len()];
    for (i, &p) in mesh.vertices.iter().enumerate() {
        if i == survivor || !selected.contains(&i) {
            remap[i] = next.add_vertex(if i == survivor { center } else { p });
        }
    }
    let welded = remap[survivor];
    for &i in &selected {
        remap[i] = welded;
    }
    let mut seen = BTreeSet::new();
    for face in &mesh.faces {
        let mut f: Vec<_> = face.iter().map(|&i| remap[i]).collect();
        f.dedup();
        if f.len() > 1 && f.first() == f.last() {
            f.pop();
        }
        let unique: BTreeSet<_> = f.iter().copied().collect();
        if unique.len() < 3 {
            continue;
        }
        if unique.len() != f.len() {
            return Err("Weld would create a pinched polygon".into());
        }
        let origin = next.vertices[f[0]];
        let area = (1..f.len() - 1)
            .map(|i| (next.vertices[f[i]] - origin).cross(next.vertices[f[i + 1]] - origin))
            .sum::<DVec3>()
            .length();
        if area < 1e-14 {
            continue;
        }
        if seen.insert(unique) {
            next.faces.push(f);
        }
    }
    next.edges = mesh
        .edges
        .iter()
        .map(|e| edge(remap[e[0]], remap[e[1]]))
        .collect();
    dedup_edges(&mut next);
    next.selected = vec![welded];
    validate(&next)?;
    *mesh = next;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn surface() -> Surface {
        Surface::new(
            vec![
                DVec3::new(-10., -10., 0.),
                DVec3::new(10., -10., 0.),
                DVec3::new(0., 10., 0.),
            ],
            vec![[0, 1, 2]],
        )
        .unwrap()
    }
    #[test]
    fn triangle_winding_and_chain() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X, DVec3::Y],
            faces: vec![vec![0, 1, 2]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        insert(&mut m, &surface(), DVec3::new(0.5, -1., 2.), None, 0.01).unwrap();
        assert_eq!(m.faces[1], vec![3, 1, 0]);
        assert_eq!(m.selected, vec![1, 3]);
        assert_eq!(m.vertices[3].z, 0.);
        insert(&mut m, &surface(), DVec3::new(2., -1., 1.), None, 0.01).unwrap();
        assert_eq!(m.faces.len(), 3);
    }
    #[test]
    fn tri_quad_builds_a_quad_strip() {
        // Loose edge 0-1 along X; build a strip upward in +Y.
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X],
            edges: vec![[0, 1]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        let quad = |quad| PenOptions {
            snap_distance: 0.01,
            quad,
            ..PenOptions::default()
        };
        insert_with(&mut m, &surface(), DVec3::new(1., 1., 0.), &quad(true)).unwrap();
        assert_eq!(m.faces.len(), 1);
        assert_eq!(m.faces[0].len(), 3);
        // Unconstrained triangle faces the +Z source normal.
        assert!(polygon_normal(&m, &m.faces[0]).z > 0.0);
        insert_with(&mut m, &surface(), DVec3::new(0., 1., 0.), &quad(true)).unwrap();
        assert_eq!(m.faces.len(), 1);
        assert_eq!(m.faces[0].len(), 4, "{:?}", m.faces);
        assert!(polygon_normal(&m, &m.faces[0]).z > 0.0);
        let far: BTreeSet<_> = m.selected.iter().copied().collect();
        assert_eq!(far, BTreeSet::from([2, 3]));
        // Next pair of clicks extends the strip with a second quad.
        insert_with(&mut m, &surface(), DVec3::new(1., 2., 0.), &quad(true)).unwrap();
        insert_with(&mut m, &surface(), DVec3::new(0., 2., 0.), &quad(true)).unwrap();
        assert_eq!(m.faces.len(), 2);
        assert!(m.faces.iter().all(|f| f.len() == 4));
        assert!(m.faces.iter().all(|f| polygon_normal(&m, f).z > 0.0));
        m.validate().unwrap();
    }
    #[test]
    fn tri_quad_accepts_either_click_order() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X],
            edges: vec![[0, 1]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        let options = PenOptions {
            snap_distance: 0.01,
            quad: true,
            ..PenOptions::default()
        };
        // First click above vertex 0, then above vertex 1 (the reverse of the strip test).
        insert_with(&mut m, &surface(), DVec3::new(0., 1., 0.), &options).unwrap();
        insert_with(&mut m, &surface(), DVec3::new(1., 1., 0.), &options).unwrap();
        assert_eq!(m.faces.len(), 1);
        assert_eq!(m.faces[0].len(), 4);
        m.validate().unwrap();
    }
    #[test]
    fn explicit_vertex_snap_closes_chain() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X, DVec3::Y],
            edges: vec![[0, 1], [1, 2]],
            selected: vec![2],
            ..Mesh::default()
        };
        let options = PenOptions {
            vertex: Some(0),
            ..PenOptions::default()
        };
        insert_with(&mut m, &surface(), DVec3::new(5., 5., 5.), &options).unwrap();
        assert_eq!(m.vertices.len(), 3);
        assert!(m.edges.contains(&[0, 2]));
        assert_eq!(m.selected, vec![0]);
        let bad = PenOptions {
            vertex: Some(99),
            ..PenOptions::default()
        };
        assert!(insert_with(&mut m, &surface(), DVec3::ZERO, &bad).is_err());
    }
    #[test]
    fn split_and_endpoint_snap() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X, DVec3::Y],
            faces: vec![vec![0, 1, 2]],
            ..Mesh::default()
        };
        insert(
            &mut m,
            &surface(),
            DVec3::new(0.5, 0., 3.),
            Some([0, 1]),
            0.01,
        )
        .unwrap();
        assert_eq!(m.faces[0], vec![0, 3, 1, 2]);
        assert!(m.vertices[3].distance(DVec3::X * 0.5) < 1e-12);
        insert(&mut m, &surface(), DVec3::ZERO, Some([0, 3]), 0.01).unwrap();
        assert_eq!(m.selected, vec![0]);
        assert_eq!(m.vertices.len(), 4);
    }
    #[test]
    fn reuse_and_merge_remap() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X, DVec3::Y, DVec3::new(1., 1., 0.)],
            edges: vec![[0, 2], [1, 2], [0, 1]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        merge_selected(&mut m, &surface()).unwrap();
        assert_eq!(m.vertices.len(), 3);
        assert_eq!(m.vertices[1], DVec3::Y);
        assert_eq!(m.edges, vec![[0, 1]]);
        assert_eq!(m.selected, vec![0]);
        insert(&mut m, &surface(), DVec3::Y + DVec3::Z, None, 0.01).unwrap();
        assert_eq!(m.vertices.len(), 3);
        assert_eq!(m.selected, vec![1]);
        assert_eq!(m.edges.len(), 1);
    }
    #[test]
    fn merge_collapses_faces_and_rejects_bowtie_transactionally() {
        let mut m = Mesh {
            vertices: vec![DVec3::ZERO, DVec3::X, DVec3::Y],
            faces: vec![vec![0, 1, 2]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        merge_selected(&mut m, &surface()).unwrap();
        assert!(m.faces.is_empty());
        let mut m = Mesh {
            vertices: vec![
                DVec3::ZERO,
                DVec3::X,
                DVec3::Y,
                -DVec3::X,
                -DVec3::Y,
                DVec3::new(-1., -1., 0.),
            ],
            faces: vec![vec![0, 1, 2], vec![3, 4, 5]],
            selected: vec![0, 3],
            ..Mesh::default()
        };
        let before = serde_json::to_string(&m).unwrap();
        assert!(merge_selected(&mut m, &surface()).is_err());
        assert_eq!(serde_json::to_string(&m).unwrap(), before);
    }
}

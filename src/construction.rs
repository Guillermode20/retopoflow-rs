//! Surface-projected construction tools. Operations commit only after validation.
use crate::{
    math::resample,
    mesh::{Mesh, face_edges},
    surface::Surface,
};
use glam::DVec3;
use std::collections::BTreeSet;

const MAX_GRID: usize = 65_536;
fn finite(points: &[DVec3]) -> Result<(), String> {
    if points.iter().any(|p| !p.is_finite()) {
        Err("Coordinates must be finite".into())
    } else {
        Ok(())
    }
}
/// Faces appended after `first_new` that share no edge with older faces are
/// free to choose their winding. Orient them so their normals agree with the
/// source surface, so Blender shading and modifiers see outward-facing quads.
pub fn orient_new_faces(mesh: &mut Mesh, first_new: usize, surface: &Surface) {
    if first_new >= mesh.faces.len() {
        return;
    }
    let old: BTreeSet<_> = mesh.faces[..first_new]
        .iter()
        .flat_map(|f| face_edges(f))
        .collect();
    if mesh.faces[first_new..]
        .iter()
        .flat_map(|f| face_edges(f))
        .any(|e| old.contains(&e))
    {
        return;
    }
    let agreement: f64 = mesh.faces[first_new..]
        .iter()
        .map(|f| {
            let points: Vec<_> = f.iter().map(|&i| mesh.vertices[i]).collect();
            let normal = (1..points.len() - 1)
                .map(|i| (points[i] - points[0]).cross(points[i + 1] - points[0]))
                .sum::<DVec3>();
            let centroid = points.iter().sum::<DVec3>() / points.len() as f64;
            normal.dot(surface.closest(centroid).normal)
        })
        .sum();
    if agreement < 0.0 {
        for face in &mut mesh.faces[first_new..] {
            face.reverse();
        }
    }
}

/// Reorder `points` (reversing, and cyclically shifting closed loops) so they
/// best match `boundary` index-for-index. Prevents twisted bridges when a stroke
/// or contour is drawn in the opposite direction to the selected boundary.
pub fn align(boundary: &[DVec3], points: &[DVec3], closed: bool) -> Vec<DVec3> {
    let count = points.len();
    let mut best = (f64::INFINITY, points.to_vec());
    if count == 0 || boundary.len() != count {
        return best.1;
    }
    for reverse in [false, true] {
        for shift in 0..if closed { count } else { 1 } {
            let candidate: Vec<_> = (0..count)
                .map(|i| {
                    points[if reverse {
                        (shift + count - 1 - i) % count
                    } else {
                        (shift + i) % count
                    }]
                })
                .collect();
            let cost = boundary
                .iter()
                .zip(&candidate)
                .map(|(a, b)| a.distance_squared(*b))
                .sum::<f64>();
            if cost < best.0 {
                best = (cost, candidate);
            }
        }
    }
    best.1
}

fn commit(mesh: &mut Mesh, mut next: Mesh, surface: &Surface) -> Result<(), String> {
    orient_new_faces(&mut next, mesh.faces.len(), surface);
    next.validate()?;
    for face in next.faces.iter().skip(mesh.faces.len()) {
        if face.len() == 4 {
            let [a, b, c, d] = [0, 1, 2, 3].map(|i| next.vertices[face[i]]);
            let first = (b - a).cross(c - a);
            let second = (c - a).cross(d - a);
            if !first.is_finite()
                || !second.is_finite()
                || first.length_squared() < 1e-28
                || second.length_squared() < 1e-28
                || first.dot(second) <= 0.0
            {
                return Err("Construction would create a folded or degenerate quad".into());
            }
        }
    }
    *mesh = next;
    Ok(())
}
/// Corners follow the perimeter; columns and rows are numbers of quad cells.
pub fn patch(
    mesh: &mut Mesh,
    surface: &Surface,
    corners: &[DVec3],
    columns: usize,
    rows: usize,
) -> Result<(), String> {
    finite(corners)?;
    if corners.len() != 4
        || columns == 0
        || rows == 0
        || columns > 256
        || rows > 256
        || (columns + 1) * (rows + 1) > MAX_GRID
    {
        return Err("Patch needs four corners and a bounded, nonzero grid".into());
    }
    let mut next = mesh.clone();
    let base = next.vertices.len();
    for y in 0..=rows {
        let v = y as f64 / rows as f64;
        for x in 0..=columns {
            let u = x as f64 / columns as f64;
            let p = corners[0]
                .lerp(corners[1], u)
                .lerp(corners[3].lerp(corners[2], u), v);
            next.add_vertex(surface.closest(p).point);
        }
    }
    for y in 0..rows {
        for x in 0..columns {
            let a = base + y * (columns + 1) + x;
            next.add_face(vec![a, a + 1, a + columns + 2, a + columns + 1]);
        }
    }
    next.selected = (base..next.vertices.len()).collect();
    commit(mesh, next, surface)
}
/// `count` is the number of centerline samples; `width` is the full rail separation.
pub fn polystrip(
    mesh: &mut Mesh,
    surface: &Surface,
    points: &[DVec3],
    count: usize,
    width: f64,
) -> Result<(), String> {
    finite(points)?;
    if !width.is_finite() || width <= 1e-10 {
        return Err("Strip width must be positive and finite".into());
    }
    let centers = resample(points, count, false)?;
    finite(&centers)?;
    let centers: Vec<_> = centers.iter().map(|&p| surface.closest(p)).collect();
    let mut next = mesh.clone();
    let base = next.vertices.len();
    for i in 0..count {
        let tangent = centers[(i + 1).min(count - 1)].point - centers[i.saturating_sub(1)].point;
        let side = tangent.cross(centers[i].normal);
        if !side.is_finite() || side.length_squared() < 1e-20 {
            return Err("Degenerate projected stroke tangent".into());
        }
        let side = side.normalize() * (width * 0.5);
        for sign in [-1.0, 1.0] {
            let p = centers[i].point + side * sign;
            if !p.is_finite() {
                return Err("Strip width overflow".into());
            }
            next.add_vertex(surface.closest(p).point);
        }
    }
    for i in 0..count - 1 {
        let a = base + 2 * i;
        next.add_face(vec![a, a + 1, a + 3, a + 2]);
    }
    next.selected = (base..next.vertices.len()).collect();
    commit(mesh, next, surface)
}

/// Add a resampled plane section, optionally bridging an equally sized selected boundary.
pub fn contour(
    mesh: &mut Mesh,
    surface: &Surface,
    origin: DVec3,
    normal: DVec3,
    count: usize,
    seed: DVec3,
    bridge: bool,
) -> Result<(), String> {
    finite(&[origin, normal, seed])?;
    if !(2..=4096).contains(&count) || !normal.length_squared().is_finite() {
        return Err("Invalid contour sample count or normal".into());
    }
    let paths = surface.sections(origin, normal)?;
    let distance = |section: &crate::surface::Section| {
        let p = &section.points;
        let mut best = f64::INFINITY;
        let closing = [*p.last().unwrap(), p[0]];
        let last = section.closed.then_some(closing.as_slice());
        for w in p.windows(2).chain(last) {
            let d = w[1] - w[0];
            let t = ((seed - w[0]).dot(d) / d.length_squared().max(1e-30)).clamp(0.0, 1.0);
            best = best.min(seed.distance_squared(w[0] + t * d));
        }
        best
    };
    let path = paths
        .iter()
        .filter(|s| s.points.len() >= 2)
        .min_by(|a, b| distance(a).total_cmp(&distance(b)))
        .ok_or("Plane does not intersect source")?;
    let closed = path.closed;
    if closed && count < 3 {
        return Err("Closed contours need at least three samples".into());
    }
    let mut points = resample(&path.points, count, closed)?;
    // Chords across a faceted source are snapped back to the source.
    for p in &mut points {
        *p = surface.closest(*p).point;
    }
    let mut next = mesh.clone();
    if bridge {
        let (boundary, looped) = mesh.boundary_chain(&mesh.selected)?;
        if boundary.len() != count || looped != closed {
            return Err("Selected boundary must match contour size and closure".into());
        }
        let boundary_points: Vec<_> = boundary.iter().map(|&i| mesh.vertices[i]).collect();
        let aligned = align(&boundary_points, &points, closed);
        let ids: Vec<_> = aligned.into_iter().map(|p| next.add_vertex(p)).collect();
        for i in 0..if closed { count } else { count - 1 } {
            let j = (i + 1) % count;
            next.add_face(vec![boundary[i], boundary[j], ids[j], ids[i]]);
        }
        next.selected = ids;
    } else {
        next.selected = next.add_chain(&points, closed);
    }
    commit(mesh, next, surface)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn plane() -> Surface {
        Surface::new(
            vec![
                DVec3::new(-10., -10., 0.),
                DVec3::new(10., -10., 0.),
                DVec3::new(10., 10., 0.),
                DVec3::new(-10., 10., 0.),
            ],
            vec![[0, 1, 2], [0, 2, 3]],
        )
        .unwrap()
    }
    #[test]
    fn grid_and_winding() {
        let mut m = Mesh::default();
        patch(
            &mut m,
            &plane(),
            &[DVec3::ZERO, DVec3::X, DVec3::X + DVec3::Y, DVec3::Y],
            3,
            2,
        )
        .unwrap();
        assert_eq!((m.vertices.len(), m.faces.len()), (12, 6));
        assert!(m.faces.iter().all(|f| {
            (m.vertices[f[1]] - m.vertices[f[0]])
                .cross(m.vertices[f[3]] - m.vertices[f[0]])
                .z
                > 0.
        }));
        m.validate().unwrap();
    }
    #[test]
    fn clockwise_patch_faces_the_source() {
        let mut m = Mesh::default();
        // Clockwise corners would naively produce -Z normals.
        patch(
            &mut m,
            &plane(),
            &[DVec3::ZERO, DVec3::Y, DVec3::X + DVec3::Y, DVec3::X],
            2,
            2,
        )
        .unwrap();
        for f in &m.faces {
            let [a, b, c] = [0, 1, 2].map(|i| m.vertices[f[i]]);
            assert!((b - a).cross(c - a).z > 0.0);
        }
    }
    #[test]
    fn align_reverses_and_rotates() {
        let boundary = [DVec3::ZERO, DVec3::X, DVec3::Y];
        let reversed = [DVec3::Y, DVec3::X, DVec3::ZERO];
        assert_eq!(align(&boundary, &reversed, false), boundary.to_vec());
        let rotated = [DVec3::X, DVec3::Y, DVec3::ZERO];
        assert_eq!(align(&boundary, &rotated, true), boundary.to_vec());
    }
    #[test]
    fn strip_width_and_rejections() {
        let mut m = Mesh::default();
        polystrip(&mut m, &plane(), &[DVec3::ZERO, DVec3::X * 3.], 4, 0.4).unwrap();
        assert_eq!(m.faces.len(), 3);
        for f in &m.faces {
            assert!(
                (m.vertices[f[1]] - m.vertices[f[0]])
                    .cross(m.vertices[f[2]] - m.vertices[f[0]])
                    .z
                    > 0.0
            );
        }
        assert!((m.vertices[0].distance(m.vertices[1]) - 0.4).abs() < 1e-9);
        let before = m.vertices.clone();
        assert!(polystrip(&mut m, &plane(), &[DVec3::ZERO; 2], 4, 1.).is_err());
        assert!(polystrip(&mut m, &plane(), &[DVec3::ZERO, DVec3::X], 4, f64::NAN).is_err());
        assert_eq!(before, m.vertices);
    }
    #[test]
    fn patch_rejections_are_atomic() {
        let mut m = Mesh::default();
        for (c, r) in [(0, 1), (usize::MAX, 1), (256, 256)] {
            assert!(patch(&mut m, &plane(), &[DVec3::ZERO; 4], c, r).is_err());
        }
        assert!(patch(&mut m, &plane(), &[DVec3::NAN; 4], 1, 1).is_err());
        assert!(patch(&mut m, &plane(), &[DVec3::ZERO; 4], 1, 1).is_err());
        assert!(m.vertices.is_empty());
    }
    fn cube() -> Surface {
        let v = vec![
            DVec3::new(-1., -1., -1.),
            DVec3::new(1., -1., -1.),
            DVec3::new(1., 1., -1.),
            DVec3::new(-1., 1., -1.),
            DVec3::new(-1., -1., 1.),
            DVec3::new(1., -1., 1.),
            DVec3::new(1., 1., 1.),
            DVec3::new(-1., 1., 1.),
        ];
        Surface::new(
            v,
            vec![
                [0, 2, 1],
                [0, 3, 2],
                [4, 5, 6],
                [4, 6, 7],
                [0, 1, 5],
                [0, 5, 4],
                [1, 2, 6],
                [1, 6, 5],
                [2, 3, 7],
                [2, 7, 6],
                [3, 0, 4],
                [3, 4, 7],
            ],
        )
        .unwrap()
    }
    #[test]
    fn closed_cube_and_bridge() {
        let s = cube();
        let mut m = Mesh::default();
        contour(&mut m, &s, DVec3::ZERO, DVec3::Z, 8, DVec3::X, false).unwrap();
        assert_eq!(m.edges.len(), 8);
        assert!(m.neighbors().iter().all(|n| n.len() == 2));
        contour(&mut m, &s, DVec3::Z * 0.5, DVec3::Z, 8, -DVec3::X, true).unwrap();
        assert_eq!(m.faces.len(), 8);
        m.validate().unwrap();
        for p in &m.vertices {
            assert!(s.closest(*p).distance < 1e-8);
        }
    }
    #[test]
    fn open_contour_and_invalid_inputs() {
        let mut m = Mesh::default();
        let s = plane();
        contour(&mut m, &s, DVec3::ZERO, DVec3::X, 5, DVec3::ZERO, false).unwrap();
        assert_eq!(m.edges.len(), 4);
        assert!(contour(&mut m, &s, DVec3::ZERO, DVec3::ZERO, 5, DVec3::ZERO, false).is_err());
        assert!(contour(&mut m, &s, DVec3::Z, DVec3::Z, 5, DVec3::ZERO, false).is_err());
        assert!(contour(&mut m, &s, DVec3::ZERO, DVec3::X, 6, DVec3::ZERO, true).is_err());
    }
}

//! Boundary-preserving rectangular Coons patch fill.
use crate::{mesh::Mesh, surface::Surface};
use glam::DVec3 as V;

/// Fill a closed boundary with `2*(columns+rows)` edges, reusing all perimeter
/// vertices. The first ordered boundary vertex is the first grid corner.
pub fn fill_patch(
    mesh: &mut Mesh,
    surface: &Surface,
    columns: usize,
    rows: usize,
) -> Result<(), String> {
    mesh.validate()?;
    let (boundary, closed) = mesh.boundary_chain(&mesh.selected)?;
    if !closed || boundary.len() != 2 * (columns + rows) {
        return Err("Select one closed boundary with exactly 2*(columns + rows) edges".into());
    }
    fill_chain(mesh, surface, &boundary, columns, rows)
}

/// Turning angle (radians) at every vertex of a closed polyline.
fn turns(points: &[V]) -> Vec<f64> {
    let n = points.len();
    (0..n)
        .map(|i| {
            let u = points[i] - points[(i + n - 1) % n];
            let v = points[(i + 1) % n] - points[i];
            let denominator = u.length() * v.length();
            if denominator <= 1e-30 {
                0.0
            } else {
                (u.dot(v) / denominator).clamp(-1.0, 1.0).acos()
            }
        })
        .collect()
}

/// Choose the four grid corners of an even closed boundary automatically.
/// Opposite sides must have equal edge counts; among those layouts the corners
/// with the sharpest turns win, with a slight preference for square cells.
/// Returns `(start, columns, rows)`.
pub fn auto_corners(points: &[V]) -> Result<(usize, usize, usize), String> {
    let n = points.len();
    if n < 4 || !n.is_multiple_of(2) {
        return Err(format!(
            "Fill needs a closed boundary with an even number of edges (selected loop has {n})"
        ));
    }
    let half = n / 2;
    let turn = turns(points);
    let perimeter: f64 = (0..n)
        .map(|i| points[i].distance(points[(i + 1) % n]))
        .sum();
    let mut best: Option<(f64, usize, usize)> = None;
    for start in 0..n {
        for columns in 1..half {
            let rows = half - columns;
            if columns > MAX_CELLS || rows > MAX_CELLS {
                continue;
            }
            let corners = [
                start,
                start + columns,
                start + columns + rows,
                start + 2 * columns + rows,
            ];
            let sharpness: f64 = corners.iter().map(|&c| turn[c % n]).sum();
            // Compare side lengths with cell counts so cells stay near square.
            let side = |from: usize, count: usize| -> f64 {
                (0..count)
                    .map(|k| points[(from + k) % n].distance(points[(from + k + 1) % n]))
                    .sum()
            };
            let width = (side(corners[0], columns) + side(corners[2], columns)) / 2.0;
            let height = (side(corners[1], rows) + side(corners[3], rows)) / 2.0;
            let aspect = ((width / columns as f64) / (height / rows as f64).max(1e-30))
                .ln()
                .abs();
            let score = sharpness - 0.05 * aspect - 1e-9 * perimeter;
            if best.is_none_or(|(b, _, _)| score > b + 1e-9) {
                best = Some((score, start, columns));
            }
        }
    }
    let (_, start, columns) = best.ok_or("Boundary is too large to fill as one grid")?;
    Ok((start, columns, half - columns))
}

/// Fill any even closed boundary, detecting grid corners automatically.
pub fn fill_auto(mesh: &mut Mesh, surface: &Surface) -> Result<(usize, usize), String> {
    mesh.validate()?;
    let (boundary, closed) = mesh.boundary_chain(&mesh.selected)?;
    if !closed {
        return Err("Fill needs a closed boundary loop; select all of its vertices".into());
    }
    let points: Vec<_> = boundary.iter().map(|&i| mesh.vertices[i]).collect();
    let (start, columns, rows) = auto_corners(&points)?;
    let mut rotated = boundary.clone();
    rotated.rotate_left(start);
    fill_chain(mesh, surface, &rotated, columns, rows)?;
    Ok((columns, rows))
}

const MAX_CELLS: usize = 128;

fn fill_chain(
    mesh: &mut Mesh,
    surface: &Surface,
    boundary: &[usize],
    columns: usize,
    rows: usize,
) -> Result<(), String> {
    if columns == 0 || rows == 0 || columns > MAX_CELLS || rows > MAX_CELLS {
        return Err("Fill dimensions must be 1–128 cells".into());
    }
    if boundary.len() != 2 * (columns + rows) {
        return Err("Boundary size does not match the fill dimensions".into());
    }
    let boundary = boundary.to_vec();
    let mut next = mesh.clone();
    let stride = columns + 1;
    let mut grid = vec![usize::MAX; stride * (rows + 1)];
    let mut cursor = 0;
    for cell in grid.iter_mut().take(columns) {
        *cell = boundary[cursor];
        cursor += 1;
    }
    for y in 0..rows {
        grid[y * stride + columns] = boundary[cursor];
        cursor += 1;
    }
    for x in (1..=columns).rev() {
        grid[rows * stride + x] = boundary[cursor];
        cursor += 1;
    }
    for y in (1..=rows).rev() {
        grid[y * stride] = boundary[cursor];
        cursor += 1;
    }
    let p00 = next.vertices[grid[0]];
    let p10 = next.vertices[grid[columns]];
    let p01 = next.vertices[grid[rows * stride]];
    let p11 = next.vertices[grid[rows * stride + columns]];
    let mut added = Vec::new();
    for y in 1..rows {
        for x in 1..columns {
            let u = x as f64 / columns as f64;
            let v = y as f64 / rows as f64;
            let bottom = next.vertices[grid[x]];
            let top = next.vertices[grid[rows * stride + x]];
            let left = next.vertices[grid[y * stride]];
            let right = next.vertices[grid[y * stride + columns]];
            let bilinear = p00.lerp(p10, u).lerp(p01.lerp(p11, u), v);
            let p = bottom.lerp(top, v) + left.lerp(right, u) - bilinear;
            let id = next.add_vertex(surface.closest(p).point);
            grid[y * stride + x] = id;
            added.push(id);
        }
    }
    for y in 0..rows {
        for x in 0..columns {
            let ids = [
                grid[y * stride + x],
                grid[y * stride + x + 1],
                grid[(y + 1) * stride + x + 1],
                grid[(y + 1) * stride + x],
            ];
            let [a, b, c, d] = ids.map(|i| next.vertices[i]);
            let n1 = (b - a).cross(c - a);
            let n2 = (c - a).cross(d - a);
            if n1.length_squared() < 1e-24 || n2.length_squared() < 1e-24 || n1.dot(n2) <= 0.0 {
                return Err("Boundary corners create folded or degenerate fill; change dimensions or corner ordering".into());
            }
            next.add_face(ids.to_vec());
        }
    }
    crate::construction::orient_new_faces(&mut next, mesh.faces.len(), surface);
    next.selected = if added.is_empty() { boundary } else { added };
    next.validate()?;
    *mesh = next;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn plane() -> Surface {
        Surface::new(
            vec![
                V::new(-10., -10., 0.),
                V::new(10., -10., 0.),
                V::new(0., 10., 0.),
            ],
            vec![[0, 1, 2]],
        )
        .unwrap()
    }
    fn boundary() -> Mesh {
        let vertices = vec![
            V::ZERO,
            V::X,
            V::X * 2.,
            V::new(2., 1., 0.),
            V::new(2., 2., 0.),
            V::new(1., 2., 0.),
            V::Y * 2.,
            V::Y,
        ];
        Mesh {
            vertices,
            edges: (0..8).map(|i| [i, (i + 1) % 8]).collect(),
            selected: (0..8).collect(),
            ..Mesh::default()
        }
    }
    #[test]
    fn fills_reusing_perimeter() {
        let mut m = boundary();
        fill_patch(&mut m, &plane(), 2, 2).unwrap();
        assert_eq!(m.vertices.len(), 9);
        assert_eq!(m.faces.len(), 4);
        assert_eq!(m.vertices[8], V::new(1., 1., 0.));
    }
    #[test]
    fn auto_fill_detects_rectangle_corners() {
        // 3x1 rectangle boundary starting mid-side: 8 edges, corners not at index 0.
        let ring = [
            (1., 0.),
            (2., 0.),
            (3., 0.),
            (3., 1.),
            (2., 1.),
            (1., 1.),
            (0., 1.),
            (0., 0.),
        ];
        let mut m = Mesh {
            vertices: ring.iter().map(|&(x, y)| V::new(x, y, 0.)).collect(),
            edges: (0..8)
                .map(|i| [i, (i + 1) % 8])
                .map(|[a, b]| crate::mesh::edge(a, b))
                .collect(),
            selected: (0..8).collect(),
            ..Mesh::default()
        };
        let (columns, rows) = fill_auto(&mut m, &plane()).unwrap();
        assert_eq!((columns.max(rows), columns.min(rows)), (3, 1));
        assert_eq!(m.faces.len(), 3);
        assert_eq!(m.vertices.len(), 8);
        // Loose boundaries are oriented to the source normal (+Z).
        for f in &m.faces {
            let [a, b, c] = [0, 1, 2].map(|i| m.vertices[f[i]]);
            assert!((b - a).cross(c - a).z > 0.0);
        }
    }
    #[test]
    fn auto_fill_rejects_odd_and_open_boundaries() {
        let mut m = boundary();
        m.edges.pop();
        let before = format!("{m:?}");
        assert!(fill_auto(&mut m, &plane()).is_err());
        assert_eq!(format!("{m:?}"), before);
        let triangle = [V::ZERO, V::X, V::Y];
        assert!(auto_corners(&triangle).is_err());
    }
    #[test]
    fn invalid_boundary_atomic() {
        let mut m = boundary();
        let original = m.vertices.clone();
        assert!(fill_patch(&mut m, &plane(), 3, 2).is_err());
        assert_eq!(m.vertices, original);
        assert!(m.faces.is_empty());
    }
}

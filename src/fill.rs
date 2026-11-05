//! Boundary-preserving rectangular Coons patch fill.
use crate::{mesh::Mesh, surface::Surface};
#[cfg(test)]
use glam::DVec3 as V;

/// Fill a closed boundary with `2*(columns+rows)` edges, reusing all perimeter
/// vertices. The first ordered boundary vertex is the first grid corner.
pub fn fill_patch(
    mesh: &mut Mesh,
    surface: &Surface,
    columns: usize,
    rows: usize,
) -> Result<(), String> {
    if columns == 0 || rows == 0 || columns > 128 || rows > 128 {
        return Err("Fill dimensions must be 1–128 cells".into());
    }
    mesh.validate()?;
    let (boundary, closed) = mesh.boundary_chain(&mesh.selected)?;
    if !closed || boundary.len() != 2 * (columns + rows) {
        return Err("Select one closed boundary with exactly 2*(columns + rows) edges".into());
    }
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
    fn invalid_boundary_atomic() {
        let mut m = boundary();
        let original = m.vertices.clone();
        assert!(fill_patch(&mut m, &plane(), 3, 2).is_err());
        assert_eq!(m.vertices, original);
        assert!(m.faces.is_empty());
    }
}

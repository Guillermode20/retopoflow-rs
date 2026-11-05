use glam::DVec3 as V;
use retopoflow_core::surface::Surface;
fn cube() -> Surface {
    Surface::new(
        vec![
            V::new(-2., -1., -1.),
            V::new(2., -1., -1.),
            V::new(2., 1., -1.),
            V::new(-2., 1., -1.),
            V::new(-2., -1., 1.),
            V::new(2., -1., 1.),
            V::new(2., 1., 1.),
            V::new(-2., 1., 1.),
        ],
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
fn cube_sections_have_exact_closure() {
    for z in [0., 1., -1.] {
        let paths = cube().sections(V::new(0., 0., z), V::Z).unwrap();
        assert_eq!(paths.len(), 1, "z={z}");
        assert!(paths[0].closed, "z={z}");
        assert!(paths[0].points.len() >= 4);
    }
}
#[test]
fn open_section_not_guessed_closed() {
    let source = Surface::new(
        vec![V::ZERO, V::X, V::Y, V::X + V::Y],
        vec![[0, 1, 2], [1, 3, 2]],
    )
    .unwrap();
    let paths = source.sections(V::X * 0.5, V::X).unwrap();
    assert_eq!(paths.len(), 1);
    assert!(!paths[0].closed);
    assert!(source.sections(V::ZERO, V::NAN).is_err());
    assert!(source.sections(V::NAN, V::Z).is_err());
}

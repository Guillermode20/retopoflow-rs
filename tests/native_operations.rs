use retopoflow_core::{Request, execute};
use serde_json::{Value, json};
fn run(
    operation: &str,
    mesh: Value,
    options: Value,
) -> Result<retopoflow_core::mesh::Mesh, String> {
    let mut value = json!({"operation":operation,"mesh":mesh,
        "source":{"vertices":[[-10,-10,0],[10,-10,0],[10,10,0],[-10,10,0]],"triangles":[[0,1,2],[0,2,3]]}});
    for (key, value_option) in options.as_object().unwrap() {
        value[key] = value_option.clone();
    }
    let request: Request = serde_json::from_value(value).unwrap();
    execute(request)
}
fn square() -> Value {
    json!({"vertices":[[0,0,0],[1,0,0],[1,1,0],[0,1,0]],"faces":[[0,1,2,3]],"selected":[0,1]})
}
#[test]
fn selection_operations() {
    let result = run("select_path", square(), json!({})).unwrap();
    assert_eq!(result.selected, vec![0, 1]);
    let result = run("select_connected", square(), json!({})).unwrap();
    assert_eq!(result.selected.len(), 4);
    let result = run("select_invert", square(), json!({})).unwrap();
    assert_eq!(result.selected, vec![2, 3]);
}
#[test]
fn tweak_projects_and_preserves_topology() {
    let mesh = run("tweak", square(), json!({"points":[[0,0,2]]})).unwrap();
    assert_eq!(mesh.faces, vec![vec![0, 1, 2, 3]]);
    assert!(mesh.vertices.iter().all(|p| p.z == 0.));
}
#[test]
fn patch_api() {
    let mesh = run(
        "patch",
        json!({"vertices":[]}),
        json!({"points":[[0,0,1],[2,0,1],[2,2,1],[0,2,1]],"count":3,"rows":2}),
    )
    .unwrap();
    mesh.validate().unwrap();
    assert!(!mesh.faces.is_empty());
    assert!(mesh.faces.iter().all(|f| f.len() == 4));
    assert!(mesh.vertices.iter().all(|p| p.z.abs() < 1e-9));
}
#[test]
fn strip_api() {
    let mesh = run(
        "polystrip",
        json!({"vertices":[]}),
        json!({"points":[[0,0,1],[2,0,1]],"count":5,"width":0.5}),
    )
    .unwrap();
    mesh.validate().unwrap();
    assert!(!mesh.faces.is_empty());
    assert!(mesh.faces.iter().all(|f| f.len() == 4));
}
#[test]
fn loop_api() {
    let mesh = run("loops", square(), json!({"edge":[0,1],"factor":0.3})).unwrap();
    assert_eq!(mesh.faces.len(), 2);
    assert_eq!(mesh.vertices.len(), 6);
    assert!(mesh.faces.iter().all(|f| f.len() == 4));
}
#[test]
fn knife_api() {
    let mesh = run(
        "knife",
        square(),
        json!({"origin":[0.5,0,0],"normal":[1,0,0]}),
    )
    .unwrap();
    assert_eq!(mesh.faces.len(), 2);
    mesh.validate().unwrap();
}
#[test]
fn rejects_overlapping_pen_and_self_intersecting_weld() {
    let mesh = json!({"vertices":[[0,0,0],[2,0,0],[0,2,0]],"faces":[[0,1,2]],"selected":[0,1]});
    assert!(run("polypen", mesh, json!({"points":[[0.5,0.5,0]],"width":0})).is_err());
    let mesh = json!({"vertices":[[0,0,0],[1,0,0],[1,1,0],[0,1,0],[4,1,0]],"faces":[[0,1,2,3]],"selected":[0,4]});
    assert!(run("merge", mesh, json!({})).is_err());
}
#[test]
fn malformed_options_rejected() {
    assert!(run("loops", square(), json!({"edge":[0,1],"factor":1.0})).is_err());
    assert!(run("knife", square(), json!({"normal":[0,0,0]})).is_err());
    assert!(run("patch", square(), json!({"points":[[0,0,0]],"count":4})).is_err());
    assert!(
        run(
            "polystrip",
            square(),
            json!({"points":[[0,0,0],[1,0,0]],"width":-1})
        )
        .is_err()
    );
}

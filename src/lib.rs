//! Native geometry operations. The C ABI uses UTF-8 JSON and explicit ownership.
pub mod construction;
pub mod fill;
pub mod math;
pub mod mesh;
pub mod pen;
pub mod surface;
pub mod topology;
use glam::DVec3 as V;
use mesh::Mesh;
use serde::Deserialize;
use std::ffi::{CStr, CString, c_char};
use std::sync::{
    Arc, Mutex, OnceLock,
    atomic::{AtomicU64, Ordering},
};
use surface::Surface;

#[derive(Deserialize)]
pub struct Source {
    pub vertices: Vec<V>,
    pub triangles: Vec<[usize; 3]>,
}
#[derive(Deserialize)]
pub struct Request {
    pub mesh: Mesh,
    #[serde(default)]
    pub source: Option<Source>,
    pub operation: String,
    #[serde(default)]
    pub points: Vec<V>,
    #[serde(default = "default_strength")]
    pub strength: f64,
    #[serde(default = "default_iterations")]
    pub iterations: usize,
    #[serde(default = "default_count")]
    pub count: usize,
    #[serde(default)]
    pub closed: bool,
    #[serde(default)]
    pub origin: V,
    #[serde(default = "default_normal")]
    pub normal: V,
    #[serde(default = "default_width")]
    pub width: f64,
    #[serde(default = "default_rows")]
    pub rows: usize,
    #[serde(default = "default_strength")]
    pub factor: f64,
    #[serde(default)]
    pub edge: Option<[usize; 2]>,
    /// PolyPen: reuse this existing vertex instead of projecting a new one.
    #[serde(default)]
    pub vertex: Option<usize>,
    /// PolyPen: convert the triangle on the selected edge into a quad.
    #[serde(default)]
    pub quad: bool,
    /// Knife: restrict the cut to these face indices (finite knife).
    #[serde(default)]
    pub faces: Option<Vec<usize>>,
}
fn default_normal() -> V {
    V::Z
}
fn default_width() -> f64 {
    0.1
}
fn default_rows() -> usize {
    4
}
fn default_strength() -> f64 {
    0.5
}
fn default_iterations() -> usize {
    1
}
fn default_count() -> usize {
    8
}

/// Work on an owned mesh: failed operations never expose partial mutations.
pub fn execute(mut request: Request) -> Result<Mesh, String> {
    let source = request
        .source
        .take()
        .ok_or("A source surface is required")?;
    let surface = Surface::new(source.vertices, source.triangles)?;
    execute_on_surface(request, &surface)
}

fn execute_on_surface(request: Request, surface: &Surface) -> Result<Mesh, String> {
    let Request {
        mut mesh,
        source: _,
        operation,
        points,
        strength,
        iterations,
        count,
        closed,
        origin,
        normal,
        width,
        rows,
        factor,
        edge,
        vertex,
        quad,
        faces,
    } = request;
    mesh.validate()?;
    if points.iter().any(|p| !p.is_finite()) {
        return Err("Points must be finite".into());
    }
    match operation.as_str() {
        "project" => {
            for &i in &mesh.selected {
                mesh.vertices[i] = surface.closest(mesh.vertices[i]).point;
            }
        }
        "relax" => {
            if !strength.is_finite()
                || !(0.0..=1.0).contains(&strength)
                || !(1..=100).contains(&iterations)
            {
                return Err("Relax needs strength 0–1 and 1–100 iterations".into());
            }
            let neighbors = mesh.neighbors();
            let boundary: std::collections::BTreeSet<_> = mesh
                .incidence()
                .into_iter()
                .filter(|(_, n)| *n <= 1)
                .flat_map(|(e, _)| e)
                .collect();
            for _ in 0..iterations {
                let previous = mesh.vertices.clone();
                for &i in &mesh.selected {
                    if neighbors[i].is_empty() || boundary.contains(&i) {
                        continue;
                    }
                    let average = neighbors[i].iter().map(|&n| previous[n]).sum::<V>()
                        / neighbors[i].len() as f64;
                    mesh.vertices[i] = surface.closest(previous[i].lerp(average, strength)).point;
                }
            }
        }
        "tweak" => {
            let delta = *points.first().ok_or("Tweak needs a translation vector")?;
            for &i in &mesh.selected {
                mesh.vertices[i] = surface.closest(mesh.vertices[i] + delta).point;
            }
        }
        "select_all" => mesh.selected = (0..mesh.vertices.len()).collect(),
        "select_none" => mesh.selected.clear(),
        "select_invert" => {
            let selected: std::collections::BTreeSet<_> = mesh.selected.iter().copied().collect();
            mesh.selected = (0..mesh.vertices.len())
                .filter(|i| !selected.contains(i))
                .collect();
        }
        "select_connected" => {
            let neighbors = mesh.neighbors();
            let mut selected: std::collections::BTreeSet<_> =
                mesh.selected.iter().copied().collect();
            let mut pending = mesh.selected.clone();
            while let Some(i) = pending.pop() {
                for &n in &neighbors[i] {
                    if selected.insert(n) {
                        pending.push(n);
                    }
                }
            }
            mesh.selected = selected.into_iter().collect();
        }
        "select_path" => {
            if mesh.selected.len() != 2 {
                return Err("Select exactly two path endpoints".into());
            }
            mesh.selected = mesh.shortest_path(mesh.selected[0], mesh.selected[1])?;
        }
        "patch" => construction::patch(&mut mesh, surface, &points, count, rows)?,
        "fill_patch" => fill::fill_patch(&mut mesh, surface, count, rows)?,
        "fill_auto" => {
            fill::fill_auto(&mut mesh, surface)?;
        }
        "polypen" => pen::insert_with(
            &mut mesh,
            surface,
            *points.first().ok_or("PolyPen needs a point")?,
            &pen::PenOptions {
                edge,
                snap_distance: width,
                vertex,
                quad,
            },
        )?,
        "merge" => pen::merge_selected(&mut mesh, surface)?,
        "polystrip" => construction::polystrip(&mut mesh, surface, &points, count, width)?,
        "contour" | "contour_bridge" => construction::contour(
            &mut mesh,
            surface,
            origin,
            normal,
            count,
            points.first().copied().unwrap_or(origin),
            operation == "contour_bridge",
        )?,
        "loops" => {
            let [a, b] = edge.ok_or("Choose an edge for loop insertion")?;
            topology::insert_loop(&mut mesh, a, b, factor)?;
            for &i in &mesh.selected {
                mesh.vertices[i] = surface.closest(mesh.vertices[i]).point;
            }
        }
        "knife" => {
            let allowed = faces.map(|f| f.into_iter().collect());
            topology::knife_faces(&mut mesh, origin, normal, allowed.as_ref())?
        }
        "delete" => mesh.delete_selected(),
        "face" => {
            if mesh.selected.len() < 3 {
                return Err("Select at least three vertices in winding order".into());
            }
            mesh.add_face(mesh.selected.clone());
        }
        "add_vertex" => {
            let p = *points.first().ok_or("A point is required")?;
            let id = mesh.add_vertex(surface.closest(p).point);
            mesh.selected = vec![id];
        }
        "stroke" | "bridge" => {
            if closed && count < 3 {
                return Err("Closed strokes need at least three samples".into());
            }
            let sampled = math::resample(&points, count, closed)?;
            let sampled: Vec<_> = sampled
                .into_iter()
                .map(|p| surface.closest(p).point)
                .collect();
            let boundary = if operation == "bridge" {
                let (ids, looped) = mesh.boundary_chain(&mesh.selected)?;
                if ids.len() != count || looped != closed {
                    return Err(
                        "Boundary and stroke must have matching sample count and closure".into(),
                    );
                }
                Some(ids)
            } else {
                None
            };
            let sampled = match &boundary {
                Some(ids) => {
                    let positions: Vec<_> = ids.iter().map(|&i| mesh.vertices[i]).collect();
                    construction::align(&positions, &sampled, closed)
                }
                None => sampled,
            };
            let first_new = mesh.faces.len();
            let ids = mesh.add_chain(&sampled, closed);
            if let Some(boundary) = boundary {
                for i in 0..if closed { count } else { count - 1 } {
                    let j = (i + 1) % count;
                    mesh.add_face(vec![boundary[i], boundary[j], ids[j], ids[i]]);
                }
                construction::orient_new_faces(&mut mesh, first_new, surface);
            }
            mesh.selected = ids;
        }
        _ => return Err(format!("Unknown operation: {operation}")),
    }
    mesh.validate()?;
    Ok(mesh)
}

type Sources = std::collections::BTreeMap<u64, Arc<Surface>>;
static SOURCES: OnceLock<Mutex<Sources>> = OnceLock::new();
static NEXT_SOURCE: AtomicU64 = AtomicU64::new(1);
fn sources() -> &'static Mutex<Sources> {
    SOURCES.get_or_init(|| Mutex::new(Sources::new()))
}

/// Build an immutable source once for repeated native operations. Zero means
/// invalid JSON/geometry. Handles are process-local and never reused.
/// # Safety
/// `input` must be null or point to valid NUL-terminated Source JSON.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rf_source_create(input: *const c_char) -> u64 {
    std::panic::catch_unwind(|| -> Option<u64> {
        if input.is_null() {
            return None;
        }
        let text = unsafe { CStr::from_ptr(input) }.to_str().ok()?;
        let source: Source = serde_json::from_str(text).ok()?;
        let surface = Surface::new(source.vertices, source.triangles).ok()?;
        let mut id = NEXT_SOURCE.load(Ordering::Relaxed);
        loop {
            let next = id.checked_add(1)?;
            match NEXT_SOURCE.compare_exchange_weak(id, next, Ordering::Relaxed, Ordering::Relaxed)
            {
                Ok(_) => break,
                Err(current) => id = current,
            }
        }
        sources().lock().ok()?.insert(id, Arc::new(surface));
        Some(id)
    })
    .ok()
    .flatten()
    .unwrap_or(0)
}
/// Release a cached source. Unknown/already released handles are harmless.
#[unsafe(no_mangle)]
pub extern "C" fn rf_source_free(handle: u64) {
    if let Ok(mut cache) = sources().lock() {
        cache.remove(&handle);
    }
}
/// Execute with a cached source; requests can omit `source` entirely.
/// # Safety
/// `input` must be null or point to a valid NUL-terminated request string.
/// Release the returned JSON using `rf_free`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rf_execute_cached(input: *const c_char, handle: u64) -> *mut c_char {
    let result = std::panic::catch_unwind(|| -> Result<Mesh, String> {
        if input.is_null() {
            return Err("Null request".into());
        }
        let surface = sources()
            .lock()
            .map_err(|_| "Source cache unavailable")?
            .get(&handle)
            .cloned()
            .ok_or("Unknown source handle")?;
        let text = unsafe { CStr::from_ptr(input) }
            .to_str()
            .map_err(|e| e.to_string())?;
        let request = serde_json::from_str(text).map_err(|e| format!("Invalid request: {e}"))?;
        execute_on_surface(request, &surface)
    });
    response(result)
}
fn response(result: std::thread::Result<Result<Mesh, String>>) -> *mut c_char {
    let response = match result {
        Ok(Ok(mesh)) => serde_json::json!({"ok":true,"mesh":mesh}),
        Ok(Err(error)) => serde_json::json!({"ok":false,"error":error}),
        Err(_) => serde_json::json!({"ok":false,"error":"Native operation panicked"}),
    };
    CString::new(response.to_string())
        .expect("JSON has no raw NUL")
        .into_raw()
}

/// # Safety
/// `input` must be null or point to a valid NUL-terminated string for this call.
/// Free the returned allocation exactly once using `rf_free`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rf_execute(input: *const c_char) -> *mut c_char {
    let result = std::panic::catch_unwind(|| -> Result<Mesh, String> {
        if input.is_null() {
            return Err("Null request".into());
        }
        let text = unsafe { CStr::from_ptr(input) }
            .to_str()
            .map_err(|e| e.to_string())?;
        let request = serde_json::from_str(text).map_err(|e| format!("Invalid request: {e}"))?;
        execute(request)
    });
    response(result)
}
/// # Safety
/// `pointer` must be null or an unfreed pointer returned by `rf_execute`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rf_free(pointer: *mut c_char) {
    if !pointer.is_null() {
        drop(unsafe { CString::from_raw(pointer) });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(operation: &str) -> Request {
        serde_json::from_value(serde_json::json!({
            "mesh":{"vertices":[[0.2,0.2,1.0]],"selected":[0]},
            "source":{"vertices":[[0,0,0],[10,0,0],[0,10,0]],"triangles":[[0,1,2]]},
            "operation":operation
        }))
        .unwrap()
    }
    #[test]
    fn projects() {
        assert_eq!(execute(request("project")).unwrap().vertices[0].z, 0.0);
    }
    #[test]
    fn rejects_invalid_selection() {
        let mut r = request("project");
        r.mesh.selected = vec![2];
        assert!(execute(r).is_err());
    }
    #[test]
    fn stroke_and_delete() {
        let mut r = request("stroke");
        r.points = vec![V::new(0., 0., 1.), V::new(1., 0., 1.)];
        r.count = 4;
        let mesh = execute(r).unwrap();
        assert_eq!(mesh.vertices.len(), 5);
        assert_eq!(mesh.edges.len(), 3);
        let mut r = request("delete");
        r.mesh = mesh;
        assert_eq!(execute(r).unwrap().vertices.len(), 1);
    }
    #[test]
    fn bridges_boundary() {
        let mut r = request("bridge");
        r.mesh = Mesh {
            vertices: vec![V::ZERO, V::X],
            edges: vec![[0, 1]],
            selected: vec![0, 1],
            ..Mesh::default()
        };
        r.points = vec![V::Y, V::X + V::Y];
        r.count = 2;
        let result = execute(r).unwrap();
        assert_eq!(result.faces.len(), 1);
        assert_eq!(result.vertices.len(), 4);
    }
    #[test]
    fn bridge_aligns_reversed_stroke() {
        let mut r = request("bridge");
        r.mesh = Mesh {
            vertices: vec![V::ZERO, V::X, V::X * 2.0],
            edges: vec![[0, 1], [1, 2]],
            selected: vec![0, 1, 2],
            ..Mesh::default()
        };
        // Drawn right-to-left, opposite the boundary order.
        r.points = vec![V::new(2., 1., 1.), V::new(0., 1., 1.)];
        r.count = 3;
        let result = execute(r).unwrap();
        assert_eq!(result.faces.len(), 2);
        for f in &result.faces {
            let [a, b, c] = [0, 1, 2].map(|i| result.vertices[f[i]]);
            // Untwisted and oriented with the +Z source normal.
            assert!((b - a).cross(c - a).z > 0.0, "{f:?}");
        }
    }
    #[test]
    fn rejects_nonfinite_and_bad_closed_strokes() {
        let mut r = request("project");
        r.mesh.vertices[0].x = f64::NAN;
        assert!(execute(r).is_err());
        let mut r = request("stroke");
        r.closed = true;
        r.count = 2;
        r.points = vec![V::ZERO, V::X];
        assert!(execute(r).is_err());
    }
    #[test]
    fn raycast() {
        let s = Surface::new(vec![V::ZERO, V::X, V::Y], vec![[0, 1, 2]]).unwrap();
        assert!(s.raycast(V::new(0.2, 0.2, 1.), -V::Z).is_some());
        assert!(s.raycast(V::new(2., 2., 1.), -V::Z).is_none());
    }
    #[test]
    fn cached_source_lifecycle() {
        unsafe {
            let source =
                CString::new(r#"{"vertices":[[0,0,0],[2,0,0],[0,2,0]],"triangles":[[0,1,2]]}"#)
                    .unwrap();
            let handle = rf_source_create(source.as_ptr());
            assert_ne!(handle, 0);
            let request = CString::new(
                r#"{"operation":"project","mesh":{"vertices":[[0.2,0.2,1]],"selected":[0]}}"#,
            )
            .unwrap();
            let result = rf_execute_cached(request.as_ptr(), handle);
            let value: serde_json::Value =
                serde_json::from_slice(CStr::from_ptr(result).to_bytes()).unwrap();
            assert_eq!(value["ok"], true);
            assert_eq!(value["mesh"]["vertices"][0][2], 0.0);
            rf_free(result);
            rf_source_free(handle);
            rf_source_free(handle);
            let result = rf_execute_cached(request.as_ptr(), handle);
            let value: serde_json::Value =
                serde_json::from_slice(CStr::from_ptr(result).to_bytes()).unwrap();
            assert_eq!(value["ok"], false);
            rf_free(result);
            assert_eq!(rf_source_create(std::ptr::null()), 0);
        }
    }
    #[test]
    fn ffi_invalid_json() {
        unsafe {
            let input = CString::new("bad").unwrap();
            let p = rf_execute(input.as_ptr());
            let value: serde_json::Value =
                serde_json::from_slice(CStr::from_ptr(p).to_bytes()).unwrap();
            assert_eq!(value["ok"], false);
            rf_free(p);
        }
    }
}

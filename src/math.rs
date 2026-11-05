use glam::DVec3 as V;

pub fn closest_triangle(p: V, a: V, b: V, c: V) -> V {
    let ab = b - a;
    let ac = c - a;
    let ap = p - a;
    let d1 = ab.dot(ap);
    let d2 = ac.dot(ap);
    if d1 <= 0.0 && d2 <= 0.0 {
        return a;
    }
    let bp = p - b;
    let d3 = ab.dot(bp);
    let d4 = ac.dot(bp);
    if d3 >= 0.0 && d4 <= d3 {
        return b;
    }
    let vc = d1 * d4 - d3 * d2;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        return a + ab * (d1 / (d1 - d3));
    }
    let cp = p - c;
    let d5 = ab.dot(cp);
    let d6 = ac.dot(cp);
    if d6 >= 0.0 && d5 <= d6 {
        return c;
    }
    let vb = d5 * d2 - d1 * d6;
    if vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0 {
        return a + ac * (d2 / (d2 - d6));
    }
    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && d4 - d3 >= 0.0 && d5 - d6 >= 0.0 {
        return b + (c - b) * ((d4 - d3) / ((d4 - d3) + (d5 - d6)));
    }
    let den = 1.0 / (va + vb + vc);
    a + ab * (vb * den) + ac * (vc * den)
}

pub fn ray_triangle(origin: V, direction: V, a: V, b: V, c: V) -> Option<f64> {
    let e1 = b - a;
    let e2 = c - a;
    let h = direction.cross(e2);
    let det = e1.dot(h);
    if det.abs() < 1e-14 {
        return None;
    }
    let inv = det.recip();
    let s = origin - a;
    let u = inv * s.dot(h);
    if !(0.0..=1.0).contains(&u) {
        return None;
    }
    let q = s.cross(e1);
    let v = inv * direction.dot(q);
    if v < 0.0 || u + v > 1.0 {
        return None;
    }
    let t = inv * e2.dot(q);
    (t >= 0.0).then_some(t)
}

pub fn resample(points: &[V], count: usize, closed: bool) -> Result<Vec<V>, String> {
    if points.len() < 2 || !(2..=4096).contains(&count) {
        return Err("A stroke needs two points and 2–4096 samples".into());
    }
    let mut path = points.to_vec();
    if closed {
        path.push(points[0]);
    }
    let mut distances = vec![0.0];
    for pair in path.windows(2) {
        distances.push(distances.last().unwrap() + pair[0].distance(pair[1]));
    }
    let total = *distances.last().unwrap();
    if total < 1e-10 {
        return Err("Stroke has zero length".into());
    }
    let mut segment = 0;
    Ok((0..count)
        .map(|i| {
            let distance = total * i as f64
                / if closed {
                    count as f64
                } else {
                    (count - 1) as f64
                };
            while segment + 2 < distances.len() && distances[segment + 1] < distance {
                segment += 1;
            }
            let length = distances[segment + 1] - distances[segment];
            path[segment].lerp(
                path[segment + 1],
                if length > 1e-15 {
                    (distance - distances[segment]) / length
                } else {
                    0.0
                },
            )
        })
        .collect())
}

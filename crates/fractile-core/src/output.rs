//! Monitor (output) model, deterministic ordering and topology remapping.

use crate::geometry::Rect;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Transform {
    #[default]
    Normal,
    Rotate90,
    Rotate180,
    Rotate270,
    Flipped,
    Flipped90,
    Flipped180,
    Flipped270,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Output {
    /// Stable identifier: EDID-derived hash when available, otherwise a
    /// fallback derived from connector name + size (documented as weaker).
    pub stable_id: String,
    /// True when `stable_id` came from EDID data.
    #[serde(default)]
    pub stable_id_from_edid: bool,
    /// Connector name (e.g. "DP-1"). Can change between boots.
    pub name: String,
    /// Human readable make/model if known.
    #[serde(default)]
    pub description: Option<String>,
    /// Logical rectangle in the global compositor space.
    pub rect: Rect,
    /// Area usable for windows (excludes panels, docks, struts).
    pub work_area: Rect,
    #[serde(default = "one")]
    pub scale: f64,
    #[serde(default)]
    pub transform: Transform,
    #[serde(default)]
    pub primary: bool,
    /// Physical size in millimetres when known.
    #[serde(default)]
    pub physical_mm: Option<(u32, u32)>,
}

fn one() -> f64 {
    1.0
}

/// Deterministic monitor order used for cycling and indexing:
/// left-to-right, then top-to-bottom, then stable id.
pub fn ordered(outputs: &[Output]) -> Vec<Output> {
    let mut v = outputs.to_vec();
    v.sort_by(|a, b| {
        (a.rect.x, a.rect.y, &a.stable_id).cmp(&(b.rect.x, b.rect.y, &b.stable_id))
    });
    v
}

/// Output containing the largest part of `r` (ties: nearest center).
pub fn output_for_rect<'a>(outputs: &'a [Output], r: &Rect) -> Option<&'a Output> {
    outputs
        .iter()
        .max_by(|a, b| {
            let ia = a.rect.intersection_area(r);
            let ib = b.rect.intersection_area(r);
            ia.cmp(&ib).then_with(|| b.rect.center_distance_sq(r).cmp(&a.rect.center_distance_sq(r)))
        })
}

/// Next output after `current` in deterministic order (wraps).
pub fn next_output<'a>(outputs: &'a [Output], current: &str, step: i32) -> Option<Output> {
    let ord = ordered(outputs);
    if ord.is_empty() {
        return None;
    }
    let idx = ord.iter().position(|o| o.stable_id == current).unwrap_or(0) as i32;
    let n = ord.len() as i32;
    let next = ((idx + step) % n + n) % n;
    ord.get(next as usize).cloned()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MappingMethod {
    StableId,
    ConnectorAndSize,
    ConnectorName,
    GeometryOverlap,
    NearestFallback,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutputMapping {
    pub recorded_id: String,
    pub present_id: String,
    pub method: MappingMethod,
    /// False when the recorded monitor is absent and we fell back.
    pub exact: bool,
}

/// Map recorded outputs to the currently present ones.
/// Priority: stable (EDID) identity, then connector+size, then connector,
/// then geometry overlap, then nearest/primary fallback. Each present output
/// is preferred for at most one recorded output in the exact passes.
pub fn map_outputs(recorded: &[Output], present: &[Output]) -> Vec<OutputMapping> {
    let mut out = Vec::new();
    if present.is_empty() {
        return out;
    }
    let mut used: Vec<bool> = vec![false; present.len()];
    let mut pending: Vec<&Output> = Vec::new();
    for r in recorded {
        if let Some(i) = present.iter().position(|p| p.stable_id == r.stable_id) {
            used[i] = true;
            out.push(OutputMapping { recorded_id: r.stable_id.clone(), present_id: present[i].stable_id.clone(), method: MappingMethod::StableId, exact: true });
        } else {
            pending.push(r);
        }
    }
    let mut still = Vec::new();
    for r in pending {
        let cand = present.iter().enumerate().find(|(i, p)| {
            !used[*i] && p.name == r.name && p.rect.w == r.rect.w && p.rect.h == r.rect.h
        });
        if let Some((i, p)) = cand {
            used[i] = true;
            out.push(OutputMapping { recorded_id: r.stable_id.clone(), present_id: p.stable_id.clone(), method: MappingMethod::ConnectorAndSize, exact: true });
        } else {
            still.push(r);
        }
    }
    let mut rest = Vec::new();
    for r in still {
        if let Some((i, p)) = present.iter().enumerate().find(|(i, p)| !used[*i] && p.name == r.name) {
            used[i] = true;
            out.push(OutputMapping { recorded_id: r.stable_id.clone(), present_id: p.stable_id.clone(), method: MappingMethod::ConnectorName, exact: false });
        } else {
            rest.push(r);
        }
    }
    for r in rest {
        // Overlap with any present output (even if already used): windows from a
        // disconnected monitor collapse onto the nearest suitable surviving one.
        let best = present
            .iter()
            .max_by_key(|p| p.rect.intersection_area(&r.rect))
            .filter(|p| p.rect.intersection_area(&r.rect) > 0);
        let (p, method) = match best {
            Some(p) => (p, MappingMethod::GeometryOverlap),
            None => {
                let near = present
                    .iter()
                    .min_by_key(|p| p.rect.center_distance_sq(&r.rect))
                    .unwrap();
                (near, MappingMethod::NearestFallback)
            }
        };
        out.push(OutputMapping { recorded_id: r.stable_id.clone(), present_id: p.stable_id.clone(), method, exact: false });
    }
    out
}

/// Short fingerprint of a monitor topology (for per-profile matching:
/// laptop vs docking station vs home monitors).
pub fn topology_signature(outputs: &[Output]) -> String {
    let mut ids: Vec<String> = ordered(outputs)
        .iter()
        .map(|o| format!("{}@{}x{}", o.stable_id, o.rect.w, o.rect.h))
        .collect();
    ids.sort();
    ids.join("+")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn o(id: &str, name: &str, x: i32, w: i32) -> Output {
        Output {
            stable_id: id.into(),
            stable_id_from_edid: true,
            name: name.into(),
            description: None,
            rect: Rect::new(x, 0, w, 1440),
            work_area: Rect::new(x, 32, w, 1408),
            scale: 1.0,
            transform: Transform::Normal,
            primary: x == 0,
            physical_mm: None,
        }
    }

    #[test]
    fn deterministic_order_and_cycle() {
        let outs = vec![o("b", "DP-1", 2560, 3440), o("a", "HDMI-0", 0, 2560), o("c", "DP-2", -1920, 1920)];
        let ord: Vec<_> = ordered(&outs).into_iter().map(|o| o.stable_id).collect();
        assert_eq!(ord, vec!["c", "a", "b"]);
        assert_eq!(next_output(&outs, "b", 1).unwrap().stable_id, "c");
        assert_eq!(next_output(&outs, "c", -1).unwrap().stable_id, "b");
    }

    #[test]
    fn mapping_prefers_stable_id_then_falls_back() {
        let recorded = vec![o("a", "HDMI-0", 0, 2560), o("gone", "DP-9", 2560, 3440)];
        let present = vec![o("a", "HDMI-1", 0, 2560)];
        let m = map_outputs(&recorded, &present);
        assert_eq!(m[0].method, MappingMethod::StableId);
        assert!(m[0].exact);
        assert_eq!(m[1].present_id, "a");
        assert!(!m[1].exact);
        assert_eq!(m[1].method, MappingMethod::NearestFallback);
    }

    #[test]
    fn mapping_by_connector_and_size_when_edid_changed() {
        let recorded = vec![o("old-edid", "DP-1", 0, 2560)];
        let present = vec![o("new-edid", "DP-1", 0, 2560)];
        let m = map_outputs(&recorded, &present);
        assert_eq!(m[0].method, MappingMethod::ConnectorAndSize);
    }

    #[test]
    fn output_for_rect_uses_largest_overlap() {
        let outs = vec![o("a", "A", 0, 2560), o("b", "B", 2560, 3440)];
        let r = Rect::new(2000, 0, 1000, 500);
        assert_eq!(output_for_rect(&outs, &r).unwrap().stable_id, "a");
        let r = Rect::new(2400, 0, 1000, 500);
        assert_eq!(output_for_rect(&outs, &r).unwrap().stable_id, "b");
    }
}

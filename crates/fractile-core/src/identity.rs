//! Window identity scoring and deterministic target↔window assignment.
//! Deterministic identifiers (desktop id, app id, WM_CLASS, role) dominate;
//! titles are only a tie-breaker and never sufficient for an exact match.

use crate::snapshot::WindowRecord;
use crate::window::WindowInfo;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

/// Minimum score for a window to be considered the same application.
pub const APP_MATCH_THRESHOLD: u32 = 40;

fn eq_ci(a: &Option<String>, b: &Option<String>) -> bool {
    match (a, b) {
        (Some(x), Some(y)) => !x.is_empty() && x.eq_ignore_ascii_case(y),
        _ => false,
    }
}

fn norm_desktop(id: &Option<String>) -> Option<String> {
    id.as_ref().map(|d| d.trim_end_matches(".desktop").to_lowercase())
}

/// Score how well a live window matches a recorded target, 0..=100, and
/// whether the match is "deterministic" (not reliant on titles).
pub fn score(target: &WindowRecord, w: &WindowInfo) -> (u32, Vec<&'static str>) {
    let mut s = 0u32;
    let mut why = vec![];
    let td = norm_desktop(&target.desktop_file_id);
    let wd = norm_desktop(&w.desktop_file_id);
    if td.is_some() && td == wd {
        s += 50;
        why.push("desktop_id");
    }
    if eq_ci(&target.app_id, &w.app_id) {
        s += 25;
        why.push("app_id");
    }
    if eq_ci(&target.wm_class, &w.wm_class) {
        s += 20;
        why.push("wm_class");
    }
    if eq_ci(&target.wm_class_instance, &w.wm_class_instance) {
        s += 5;
        why.push("wm_class_instance");
    }
    // Role distinguishes multiple windows of one app (e.g. "browser" vs "devtools").
    match (&target.role, &w.role) {
        (Some(a), Some(b)) if a == b => {
            s += 10;
            why.push("role");
        }
        (Some(_), Some(_)) => s = s.saturating_sub(15),
        _ => {}
    }
    (s.min(100), why)
}

/// Title similarity 0..=100 (token Jaccard), used only among windows that
/// already match deterministically.
pub fn title_similarity(a: &str, b: &str) -> u32 {
    let ta: HashSet<String> = a.split(|c: char| !c.is_alphanumeric()).filter(|t| !t.is_empty()).map(|t| t.to_lowercase()).collect();
    let tb: HashSet<String> = b.split(|c: char| !c.is_alphanumeric()).filter(|t| !t.is_empty()).map(|t| t.to_lowercase()).collect();
    if ta.is_empty() || tb.is_empty() {
        return 0;
    }
    let inter = ta.intersection(&tb).count() as f64;
    let uni = ta.union(&tb).count() as f64;
    ((inter / uni) * 100.0).round() as u32
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Assignment {
    pub record_id: String,
    pub window_id: Option<String>,
    pub score: u32,
    pub title_score: u32,
    /// True when the window is a confident unique match.
    pub confident: bool,
    /// True when several candidate windows were indistinguishable and the
    /// choice was made by ordinal (harmless swap) - reported, not hidden.
    pub ambiguous: bool,
    pub reasons: Vec<String>,
}

/// Assign recorded targets to live windows. Each window is used at most
/// once. `already_claimed` windows are excluded (e.g. placed earlier in the
/// same restore transaction).
pub fn assign(targets: &[WindowRecord], windows: &[WindowInfo], already_claimed: &HashSet<String>) -> Vec<Assignment> {
    // candidate edges (score>=threshold)
    #[derive(Clone)]
    struct Edge {
        t: usize,
        w: usize,
        score: u32,
        title: u32,
        ord_penalty: u32,
    }
    // per-app ordinal of live windows (by stacking, then id) for tie-breaking
    let mut app_windows: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, w) in windows.iter().enumerate() {
        app_windows.entry(w.app_key()).or_default().push(i);
    }
    let mut live_ordinal = vec![0u32; windows.len()];
    for idxs in app_windows.values_mut() {
        idxs.sort_by_key(|&i| (windows[i].stacking.unwrap_or(u32::MAX), windows[i].id.clone()));
        for (o, &i) in idxs.iter().enumerate() {
            live_ordinal[i] = o as u32;
        }
    }
    let mut edges = vec![];
    for (ti, t) in targets.iter().enumerate() {
        for (wi, w) in windows.iter().enumerate() {
            if already_claimed.contains(&w.id) || !w.is_placeable() {
                continue;
            }
            let (sc, _) = score(t, w);
            if sc < APP_MATCH_THRESHOLD {
                continue;
            }
            let title = match &t.title_hint {
                Some(h) if h != "[redacted]" => title_similarity(h, &w.title),
                _ => 0,
            };
            let ord_penalty = (t.instance_ordinal as i64 - live_ordinal[wi] as i64).unsigned_abs() as u32;
            edges.push(Edge { t: ti, w: wi, score: sc, title, ord_penalty });
        }
    }
    // Greedy by (score desc, title desc, ordinal distance asc): deterministic.
    edges.sort_by(|a, b| {
        b.score
            .cmp(&a.score)
            .then(b.title.cmp(&a.title))
            .then(a.ord_penalty.cmp(&b.ord_penalty))
            .then(a.t.cmp(&b.t))
            .then(a.w.cmp(&b.w))
    });
    let mut t_done = vec![false; targets.len()];
    let mut w_done = vec![false; windows.len()];
    let mut result: Vec<Option<Assignment>> = vec![None; targets.len()];
    for e in &edges {
        if t_done[e.t] || w_done[e.w] {
            continue;
        }
        // ambiguity: another free window with identical score+title for this target
        let rivals = edges
            .iter()
            .filter(|o| o.t == e.t && o.w != e.w && !w_done[o.w] && o.score == e.score && o.title == e.title)
            .count();
        t_done[e.t] = true;
        w_done[e.w] = true;
        let (_, why) = score(&targets[e.t], &windows[e.w]);
        let mut reasons: Vec<String> = why.iter().map(|s| s.to_string()).collect();
        if e.title > 0 {
            reasons.push(format!("title~{}", e.title));
        }
        if rivals > 0 {
            reasons.push(format!("{rivals} indistinguishable candidate(s); chose by instance ordinal"));
        }
        result[e.t] = Some(Assignment {
            record_id: targets[e.t].record_id.clone(),
            window_id: Some(windows[e.w].id.clone()),
            score: e.score,
            title_score: e.title,
            confident: e.score >= 50 && rivals == 0,
            ambiguous: rivals > 0,
            reasons,
        });
    }
    targets
        .iter()
        .enumerate()
        .map(|(i, t)| {
            result[i].take().unwrap_or(Assignment {
                record_id: t.record_id.clone(),
                window_id: None,
                score: 0,
                title_score: 0,
                confident: false,
                ambiguous: false,
                reasons: vec!["no matching window".into()],
            })
        })
        .collect()
}

/// Compute instance ordinals for a set of windows (per app, by stacking order).
pub fn instance_ordinals(windows: &[WindowInfo]) -> BTreeMap<String, u32> {
    let mut by_app: BTreeMap<String, Vec<&WindowInfo>> = BTreeMap::new();
    for w in windows {
        by_app.entry(w.app_key()).or_default().push(w);
    }
    let mut out = BTreeMap::new();
    for v in by_app.values_mut() {
        v.sort_by_key(|w| (w.stacking.unwrap_or(u32::MAX), w.id.clone()));
        for (i, w) in v.iter().enumerate() {
            out.insert(w.id.clone(), i as u32);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::Rect;

    fn win(id: &str, desk: &str, title: &str, stack: u32) -> WindowInfo {
        WindowInfo {
            id: id.into(),
            title: title.into(),
            desktop_file_id: Some(desk.into()),
            wm_class: Some(desk.trim_end_matches(".desktop").into()),
            stacking: Some(stack),
            frame: Rect::new(0, 0, 10, 10),
            ..Default::default()
        }
    }
    fn rec(id: &str, desk: &str, title: &str, ord: u32) -> WindowRecord {
        WindowRecord {
            record_id: id.into(),
            desktop_file_id: Some(desk.into()),
            wm_class: Some(desk.trim_end_matches(".desktop").into()),
            title_hint: Some(title.into()),
            instance_ordinal: ord,
            normalized_geometry: [0.0, 0.0, 1.0, 1.0],
            absolute_geometry: Rect::new(0, 0, 10, 10),
            ..Default::default()
        }
    }

    #[test]
    fn two_windows_same_app_distinguished_by_title() {
        let targets = vec![rec("a", "code.desktop", "main.rs - Fractile - Visual Studio Code", 0), rec("b", "code.desktop", "notes.md - Notes - Visual Studio Code", 1)];
        // live stacking order is reversed relative to recorded ordinals
        let windows = vec![win("w1", "code.desktop", "notes.md - Notes - Visual Studio Code", 0), win("w2", "code.desktop", "main.rs - Fractile - Visual Studio Code", 1)];
        let a = assign(&targets, &windows, &HashSet::new());
        assert_eq!(a[0].window_id.as_deref(), Some("w2"));
        assert_eq!(a[1].window_id.as_deref(), Some("w1"));
        assert!(a.iter().all(|x| !x.ambiguous));
    }

    #[test]
    fn indistinguishable_windows_use_ordinal_and_flag() {
        let targets = vec![rec("a", "xterm.desktop", "xterm", 0), rec("b", "xterm.desktop", "xterm", 1)];
        let windows = vec![win("w1", "xterm.desktop", "xterm", 0), win("w2", "xterm.desktop", "xterm", 1)];
        let a = assign(&targets, &windows, &HashSet::new());
        assert_eq!(a[0].window_id.as_deref(), Some("w1"));
        assert_eq!(a[1].window_id.as_deref(), Some("w2"));
        assert!(a[0].ambiguous);
    }

    #[test]
    fn titles_alone_never_match() {
        let targets = vec![rec("a", "code.desktop", "same title", 0)];
        let mut w = win("w1", "other.desktop", "same title", 0);
        w.wm_class = Some("Other".into());
        let a = assign(&targets, &[w], &HashSet::new());
        assert!(a[0].window_id.is_none());
    }

    #[test]
    fn claimed_windows_not_reused() {
        let targets = vec![rec("a", "x.desktop", "", 0)];
        let windows = vec![win("w1", "x.desktop", "", 0)];
        let claimed: HashSet<String> = ["w1".to_string()].into_iter().collect();
        assert!(assign(&targets, &windows, &claimed)[0].window_id.is_none());
    }
}

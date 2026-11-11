//! Build snapshots from live desktop state, applying privacy rules.

use fractile_core::adapter::{Capabilities, Workspace};
use fractile_core::config::Config;
use fractile_core::geometry::rect_to_norm;
use fractile_core::identity::instance_ordinals;
use fractile_core::output::{output_for_rect, Output};
use fractile_core::snapshot::{LaunchPolicy, RestoreContent, Snapshot, SnapshotMode, SnapshotWorkspace, WindowRecord};
use fractile_core::window::WindowInfo;

pub fn identity_confidence(source: Option<&str>) -> u8 {
    match source {
        Some("systemd-scope") | Some("launch-env") => 90,
        Some("app-id") => 75,
        Some("startup-wm-class") => 65,
        Some("wm-class-name") => 55,
        Some("exec-name") => 35,
        _ => 20,
    }
}

pub fn is_private(title: &str, patterns: &[regex::Regex]) -> bool {
    patterns.iter().any(|p| p.is_match(title))
}

pub fn compile_private(cfg: &Config) -> Vec<regex::Regex> {
    cfg.session.private_title_patterns.iter().filter_map(|p| regex::RegexBuilder::new(p).size_limit(1 << 16).build().ok()).collect()
}

pub struct CaptureInput<'a> {
    pub windows: &'a [WindowInfo],
    pub outputs: &'a [Output],
    pub workspaces: &'a [Workspace],
    pub caps: &'a Capabilities,
    pub desktop: &'a str,
}

/// Capture eligible windows. Private windows (by title pattern) are excluded
/// entirely; titles are stored only as hints when enabled.
pub fn capture(input: &CaptureInput, cfg: &Config, mode: SnapshotMode, name: Option<String>) -> Snapshot {
    let mut s = Snapshot::new_empty(mode);
    s.name = name;
    s.desktop = input.desktop.to_string();
    s.compositor = input.caps.compositor.clone();
    s.session_type = input.caps.session_type.clone();
    s.outputs = input.outputs.to_vec();
    s.workspaces = input.workspaces.iter().map(|w| SnapshotWorkspace { id: format!("ws-{}", w.index), name: w.name.clone(), ordinal: w.index }).collect();
    s.active_workspace = input.workspaces.iter().find(|w| w.active).map(|w| format!("ws-{}", w.index));
    let private = compile_private(cfg);
    let eligible: Vec<WindowInfo> = input.windows.iter().filter(|w| w.is_placeable()).cloned().collect();
    let ords = instance_ordinals(&eligible);
    let mut excluded_private = 0;
    for w in &eligible {
        if cfg.session.ignore_private_windows && is_private(&w.title, &private) {
            excluded_private += 1;
            continue;
        }
        if let Some(d) = &w.desktop_file_id {
            if cfg.session.do_not_restore.iter().any(|x| x == d) {
                continue;
            }
        }
        let out = w.output_id.as_ref().and_then(|id| input.outputs.iter().find(|o| &o.stable_id == id)).or_else(|| output_for_rect(input.outputs, &w.frame));
        let wa = out.map(|o| o.work_area).unwrap_or(w.frame);
        let norm = rect_to_norm(wa, w.frame);
        let app_pol = w.desktop_file_id.as_ref().and_then(|d| cfg.apps.get(d));
        let mut diags = vec![];
        if let Some(src) = &w.desktop_file_source {
            diags.push(format!("desktop id via {src}"));
        } else {
            diags.push("no desktop file resolved: cannot relaunch".into());
        }
        let norm_arr = norm.as_array().map(|v| (v * 1e6).round() / 1e6);
        s.windows.push(WindowRecord {
            record_id: uuid::Uuid::new_v4().to_string(),
            desktop_file_id: w.desktop_file_id.clone(),
            app_id: w.app_id.clone(),
            wm_class: w.wm_class.clone(),
            wm_class_instance: w.wm_class_instance.clone(),
            role: w.role.clone(),
            instance_ordinal: ords.get(&w.id).copied().unwrap_or(0),
            title_hint: if cfg.session.store_titles { Some(w.title.chars().take(200).collect()) } else { None },
            executable: w.executable.clone(),
            sandbox: w.sandbox.clone(),
            window_group: None,
            output_stable_id: out.map(|o| o.stable_id.clone()),
            workspace_id: w.workspace.map(|i| format!("ws-{i}")),
            normalized_geometry: norm_arr,
            absolute_geometry: w.frame,
            state: w.state,
            stacking: w.stacking,
            focused: w.focused,
            launch_policy: app_pol.map(|p| p.launch).unwrap_or(LaunchPolicy::Ask),
            restore_content: if app_pol.map(|p| p.app_manages_session).unwrap_or(false) { RestoreContent::ApplicationManaged } else { RestoreContent::FrameOnly },
            placement_rule: None,
            identity_confidence: identity_confidence(w.desktop_file_source.as_deref()),
            diagnostics: diags,
        });
    }
    if excluded_private > 0 {
        s.diagnostics.push(format!("{excluded_private} private window(s) excluded"));
    }
    s
}

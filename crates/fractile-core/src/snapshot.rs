//! Versioned snapshot schema. Snapshots are compact, human-exportable and
//! never contain process environments, document contents, clipboard data or
//! anything executable without explicit approval.

use crate::geometry::{NormRect, Rect};
use crate::output::Output;
use crate::window::WindowState;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const SNAPSHOT_SCHEMA_VERSION: u32 = 1;
/// Hard limits that protect against hostile imports.
pub const MAX_WINDOWS: usize = 500;
pub const MAX_OUTPUTS: usize = 32;
pub const MAX_WORKSPACES: usize = 64;
pub const MAX_STRING: usize = 512;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum LaunchPolicy {
    /// Always launch a new instance (rarely what users want).
    Always,
    /// Launch only when no matching window exists (default for trusted).
    IfMissing,
    /// Never launch; only position existing windows.
    #[default]
    Never,
    /// Ask the user before launching (default for imports).
    Ask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RestoreContent {
    /// The application restores its own documents/tabs (e.g. browser session).
    ApplicationManaged,
    /// Only the window frame (position/size/state) is restored.
    #[default]
    FrameOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SnapshotMode {
    #[default]
    Manual,
    Periodic,
    Logout,
    Shutdown,
    PreRestore,
    Layout,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SnapshotWorkspace {
    pub id: String,
    pub name: String,
    pub ordinal: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct WindowRecord {
    pub record_id: String,
    #[serde(default)]
    pub desktop_file_id: Option<String>,
    #[serde(default)]
    pub app_id: Option<String>,
    #[serde(default)]
    pub wm_class: Option<String>,
    #[serde(default)]
    pub wm_class_instance: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
    /// Ordinal among windows of the same app, by stacking/creation order.
    #[serde(default)]
    pub instance_ordinal: u32,
    /// Title hint for disambiguation only. Redacted when private.
    #[serde(default)]
    pub title_hint: Option<String>,
    /// Informational only, never executed.
    #[serde(default)]
    pub executable: Option<String>,
    #[serde(default)]
    pub sandbox: Option<String>,
    #[serde(default)]
    pub window_group: Option<String>,
    #[serde(default)]
    pub output_stable_id: Option<String>,
    #[serde(default)]
    pub workspace_id: Option<String>,
    pub normalized_geometry: [f64; 4],
    pub absolute_geometry: Rect,
    #[serde(default)]
    pub state: WindowState,
    #[serde(default)]
    pub stacking: Option<u32>,
    #[serde(default)]
    pub focused: bool,
    #[serde(default)]
    pub launch_policy: LaunchPolicy,
    #[serde(default)]
    pub restore_content: RestoreContent,
    #[serde(default)]
    pub placement_rule: Option<String>,
    /// Confidence (0..100) of the identity at capture time.
    #[serde(default)]
    pub identity_confidence: u8,
    #[serde(default)]
    pub diagnostics: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub schema_version: u32,
    pub snapshot_id: String,
    pub created_at: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub mode: SnapshotMode,
    #[serde(default)]
    pub desktop: String,
    #[serde(default)]
    pub compositor: String,
    #[serde(default)]
    pub session_type: String,
    pub outputs: Vec<Output>,
    #[serde(default)]
    pub workspaces: Vec<SnapshotWorkspace>,
    #[serde(default)]
    pub active_workspace: Option<String>,
    pub windows: Vec<WindowRecord>,
    #[serde(default)]
    pub diagnostics: Vec<String>,
}

impl Snapshot {
    pub fn new_empty(mode: SnapshotMode) -> Snapshot {
        Snapshot {
            schema_version: SNAPSHOT_SCHEMA_VERSION,
            snapshot_id: uuid::Uuid::new_v4().to_string(),
            created_at: chrono::Local::now().to_rfc3339(),
            name: None,
            mode,
            desktop: String::new(),
            compositor: String::new(),
            session_type: String::new(),
            outputs: vec![],
            workspaces: vec![],
            active_workspace: None,
            windows: vec![],
            diagnostics: vec![],
        }
    }

    /// Canonical JSON used for checksums (serde_json preserves struct order).
    pub fn canonical_json(&self) -> String {
        serde_json::to_string(self).expect("snapshot serializes")
    }

    pub fn checksum(&self) -> String {
        hex::encode(Sha256::digest(self.canonical_json().as_bytes()))
    }

    /// Content fingerprint ignoring id/timestamp/name, to detect "changed"
    /// for periodic capture.
    pub fn content_fingerprint(&self) -> String {
        let mut c = self.clone();
        c.snapshot_id = String::new();
        c.created_at = String::new();
        c.name = None;
        c.mode = SnapshotMode::Manual;
        c.diagnostics.clear();
        for w in &mut c.windows {
            w.record_id = String::new();
            w.diagnostics.clear();
        }
        hex::encode(Sha256::digest(c.canonical_json().as_bytes()))
    }

    /// Strict validation for anything read from disk or imported.
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version == 0 || self.schema_version > SNAPSHOT_SCHEMA_VERSION {
            return Err(format!("unsupported snapshot schema_version {}", self.schema_version));
        }
        if self.windows.len() > MAX_WINDOWS {
            return Err(format!("too many windows ({})", self.windows.len()));
        }
        if self.outputs.len() > MAX_OUTPUTS {
            return Err("too many outputs".into());
        }
        if self.workspaces.len() > MAX_WORKSPACES {
            return Err("too many workspaces".into());
        }
        chrono::DateTime::parse_from_rfc3339(&self.created_at).map_err(|e| format!("bad created_at: {e}"))?;
        let check = |label: &str, s: &Option<String>| -> Result<(), String> {
            if let Some(v) = s {
                if v.len() > MAX_STRING {
                    return Err(format!("{label} too long"));
                }
                if v.chars().any(|c| c.is_control() && c != '\t') {
                    return Err(format!("{label} contains control characters"));
                }
            }
            Ok(())
        };
        check("name", &self.name)?;
        for (label, v) in [("snapshot_id", &self.snapshot_id), ("desktop", &self.desktop), ("compositor", &self.compositor), ("session_type", &self.session_type)] {
            check(label, &Some(v.clone()))?;
        }
        check("active_workspace", &self.active_workspace)?;
        if self.diagnostics.len() > 100 || self.diagnostics.iter().any(|d| d.len() > MAX_STRING) {
            return Err("diagnostics too large".into());
        }
        for ws in &self.workspaces {
            check("workspace id", &Some(ws.id.clone()))?;
            check("workspace name", &Some(ws.name.clone()))?;
        }
        for o in &self.outputs {
            if o.stable_id.is_empty() {
                return Err("invalid output id".into());
            }
            check("output id", &Some(o.stable_id.clone()))?;
            check("output name", &Some(o.name.clone()))?;
            check("output description", &o.description)?;
            if o.rect.w <= 0 || o.rect.h <= 0 || o.rect.w > 100_000 || o.rect.h > 100_000 {
                return Err(format!("invalid output geometry for {}", o.stable_id));
            }
        }
        for w in &self.windows {
            check("desktop_file_id", &w.desktop_file_id)?;
            check("app_id", &w.app_id)?;
            check("wm_class", &w.wm_class)?;
            check("role", &w.role)?;
            check("title_hint", &w.title_hint)?;
            check("executable", &w.executable)?;
            check("record_id", &Some(w.record_id.clone()))?;
            check("wm_class_instance", &w.wm_class_instance)?;
            check("sandbox", &w.sandbox)?;
            check("window_group", &w.window_group)?;
            check("output_stable_id", &w.output_stable_id)?;
            check("workspace_id", &w.workspace_id)?;
            check("placement_rule", &w.placement_rule)?;
            if w.diagnostics.len() > 32 || w.diagnostics.iter().any(|d| d.len() > MAX_STRING || d.chars().any(|c| c.is_control())) {
                return Err(format!("diagnostics too large in record {}", w.record_id));
            }
            if let Some(d) = &w.desktop_file_id {
                if !is_safe_desktop_id(d) {
                    return Err(format!("unsafe desktop_file_id '{d}'"));
                }
            }
            if !NormRect::from_array(w.normalized_geometry).is_sane() {
                return Err(format!("invalid normalized geometry in record {}", w.record_id));
            }
            if w.absolute_geometry.w <= 0 || w.absolute_geometry.h <= 0 || w.absolute_geometry.w > 100_000 || w.absolute_geometry.h > 100_000 {
                return Err(format!("invalid absolute geometry in record {}", w.record_id));
            }
        }
        Ok(())
    }

    /// Apply import safety: every launch policy becomes `Ask` unless the user
    /// confirmed trusted provenance; executables are dropped.
    pub fn sanitize_for_import(&mut self, trusted: bool) {
        for w in &mut self.windows {
            if !trusted {
                w.launch_policy = match w.launch_policy {
                    LaunchPolicy::Never => LaunchPolicy::Never,
                    _ => LaunchPolicy::Ask,
                };
            }
            w.executable = None;
        }
    }

    /// Export form with titles optionally redacted.
    pub fn redacted(&self, redact_titles: bool) -> Snapshot {
        let mut c = self.clone();
        if redact_titles {
            for w in &mut c.windows {
                if w.title_hint.is_some() {
                    w.title_hint = Some("[redacted]".into());
                }
            }
        }
        for w in &mut c.windows {
            w.executable = w.executable.as_ref().map(|e| {
                std::path::Path::new(e).file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default()
            });
        }
        c
    }
}

/// Desktop ids must be plain file names: no path separators, no traversal.
pub fn is_safe_desktop_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 255
        && id.ends_with(".desktop")
        && !id.contains('/')
        && !id.contains("..")
        && !id.starts_with('-')
        && id.chars().all(|c| c.is_alphanumeric() || matches!(c, '.' | '-' | '_' | '+' | ' ' | '@'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec() -> WindowRecord {
        WindowRecord {
            record_id: "r".into(),
            desktop_file_id: Some("org.gnome.Terminal.desktop".into()),
            normalized_geometry: [0.0, 0.0, 0.5, 1.0],
            absolute_geometry: Rect::new(0, 0, 100, 100),
            launch_policy: LaunchPolicy::Always,
            executable: Some("/usr/bin/gnome-terminal".into()),
            title_hint: Some("secret.txt".into()),
            ..Default::default()
        }
    }

    #[test]
    fn checksum_detects_change_and_fingerprint_ignores_ids() {
        let mut s = Snapshot::new_empty(SnapshotMode::Manual);
        s.windows.push(rec());
        let c1 = s.checksum();
        let f1 = s.content_fingerprint();
        let mut t = s.clone();
        t.snapshot_id = "other".into();
        assert_ne!(c1, t.checksum());
        assert_eq!(f1, t.content_fingerprint());
        t.windows[0].normalized_geometry[2] = 0.6;
        assert_ne!(f1, t.content_fingerprint());
    }

    #[test]
    fn validation_rejects_hostile_fields() {
        let mut s = Snapshot::new_empty(SnapshotMode::Manual);
        let mut r = rec();
        r.desktop_file_id = Some("../../bin/sh.desktop".into());
        s.windows.push(r);
        assert!(s.validate().is_err());
        s.windows[0].desktop_file_id = Some("ok.desktop".into());
        s.windows[0].normalized_geometry = [f64::NAN, 0.0, 1.0, 1.0];
        assert!(s.validate().is_err());
        s.windows[0].normalized_geometry = [0.0, 0.0, 1.0, 1.0];
        assert!(s.validate().is_ok());
        s.schema_version = 99;
        assert!(s.validate().is_err());
    }

    #[test]
    fn import_sanitizes_launch_policy() {
        let mut s = Snapshot::new_empty(SnapshotMode::Manual);
        s.windows.push(rec());
        s.sanitize_for_import(false);
        assert_eq!(s.windows[0].launch_policy, LaunchPolicy::Ask);
        assert!(s.windows[0].executable.is_none());
    }

    #[test]
    fn redaction() {
        let mut s = Snapshot::new_empty(SnapshotMode::Manual);
        s.windows.push(rec());
        let r = s.redacted(true);
        assert_eq!(r.windows[0].title_hint.as_deref(), Some("[redacted]"));
        assert_eq!(r.windows[0].executable.as_deref(), Some("gnome-terminal"));
    }

    #[test]
    fn desktop_id_safety() {
        assert!(is_safe_desktop_id("org.gnome.Terminal.desktop"));
        assert!(is_safe_desktop_id("brave-fnhjhbiiigefikegflikiekgmcmlmlln-Default.desktop"));
        assert!(!is_safe_desktop_id("a/b.desktop"));
        assert!(!is_safe_desktop_id("x.sh"));
        assert!(!is_safe_desktop_id("-x.desktop"));
    }
}

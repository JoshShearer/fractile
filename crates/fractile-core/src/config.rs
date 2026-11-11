//! Typed, versioned configuration (TOML on disk). Every section of the
//! settings UI maps to a struct here. Unknown keys are tolerated for forward
//! compatibility; invalid values are rejected by `validate`.

use crate::accel::Accelerator;
use crate::geometry::{ConstraintPolicy, Gaps, Grid, Length, Margins, Selection, SnapAction, Spacing, MAX_MARGIN_PX};
use crate::rules::{OutputSel, Rule};
use crate::snapshot::{is_safe_desktop_id, LaunchPolicy};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};

pub const CONFIG_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct GeneralConfig {
    /// Start the Fractile service at login (independent of restore policy).
    pub autostart: bool,
    pub tray_icon: bool,
    /// Global show/hide panel shortcut (DV-20). None = not assigned.
    pub panel_shortcut: Option<String>,
    /// Show a panel on every monitor (DV-12).
    pub panel_on_all_monitors: bool,
    /// Dismiss panel after a placement (DV-10). Shift inverts (DV-11).
    pub dismiss_after_apply: bool,
    /// Repeating a global placement shortcut cycles monitors (DV-17).
    pub cycle_monitors_on_repeat: bool,
    pub cycle_timeout_ms: u64,
    pub first_run_complete: bool,
    pub show_help: bool,
    /// Floating-only mode: never place windows managed by an auto-tiler.
    pub respect_tiling_manager: bool,
}

impl Default for GeneralConfig {
    fn default() -> Self {
        GeneralConfig {
            autostart: true,
            tray_icon: true,
            panel_shortcut: None,
            panel_on_all_monitors: true,
            dismiss_after_apply: true,
            cycle_monitors_on_repeat: true,
            cycle_timeout_ms: 2000,
            first_run_complete: false,
            show_help: true,
            respect_tiling_manager: true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Theme {
    #[default]
    System,
    Light,
    Dark,
    HighContrast,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct AppearanceConfig {
    pub default_grid: Grid,
    /// DV-24: reset to default grid on each panel open (else keep last).
    pub reset_grid_on_open: bool,
    pub subdivision_enabled: bool,
    pub spacing: Spacing,
    pub preview_enabled: bool,
    pub theme: Theme,
    pub font_scale: f64,
    /// Panel cell size in logical px.
    pub cell_size: u32,
}

impl Default for AppearanceConfig {
    fn default() -> Self {
        AppearanceConfig {
            default_grid: Grid::new(6, 6),
            reset_grid_on_open: false,
            subdivision_enabled: true,
            spacing: Spacing {
                margins_enabled: false,
                margins: Margins::uniform_px(10),
                gaps_enabled: false,
                gaps: Gaps { horizontal: Length::Px(10), vertical: Length::Px(10) },
            },
            preview_enabled: true,
            theme: Theme::System,
            font_scale: 1.0,
            cell_size: 40,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct PlacementConfig {
    pub constraint_policy: ConstraintPolicy,
    /// Max per-edge error accepted as "placed" (window-manager tolerance).
    pub tolerance_px: i32,
    /// Re-apply window: if an app overwrites geometry within this period.
    pub stabilization_ms: u64,
    pub max_reapply: u32,
    pub undo_depth: usize,
}

impl Default for PlacementConfig {
    fn default() -> Self {
        PlacementConfig { constraint_policy: ConstraintPolicy::Fit, tolerance_px: 2, stabilization_ms: 1500, max_reapply: 3, undo_depth: 20 }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ShortcutScope {
    /// Only while the panel is visible (DV-15).
    #[default]
    Local,
    /// System-wide via the compositor (DV-16).
    Global,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ShortcutAction {
    Place { grid: Grid, selection: Selection, #[serde(default)] output: OutputSel },
    Snap { action: SnapAction },
    MoveToNextOutput,
    MoveToPrevOutput,
    MoveToWorkspace { index: u32 },
    Undo,
    SaveWorkspace,
    RestoreLatest,
    ApplyLayout { name: String },
    TogglePanel,
    CommandPalette,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NamedShortcut {
    pub id: String,
    pub name: String,
    pub action: ShortcutAction,
    #[serde(default)]
    pub accelerator: Option<String>,
    #[serde(default)]
    pub scope: ShortcutScope,
    /// For Place: repeated activation cycles across monitors (DV-17).
    #[serde(default)]
    pub cycle_monitors: bool,
    #[serde(default)]
    pub favorite: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RestorePolicy {
    /// Never save or restore windows.
    Off,
    /// Only explicit Save / Restore actions (first-run default).
    #[default]
    Manual,
    /// Autosave; restore the last cleanly-ended session at next login.
    LastClean,
    /// Restore the last durable snapshot even after a crash/power loss.
    LastKnown,
    /// Apply a chosen named layout at login.
    Named,
    /// Ask at each login with a list of recent snapshots.
    Ask,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum PowerLossPolicy {
    #[default]
    Ask,
    Restore,
    Skip,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SessionConfig {
    pub policy: RestorePolicy,
    pub named_layout: Option<String>,
    pub autosave_enabled: bool,
    pub autosave_interval_min: u64,
    pub max_recent: usize,
    pub daily_history_days: u32,
    pub confirm_before_restore: bool,
    pub restore_once_per_login: bool,
    pub restore_on_monitor_reconnect: bool,
    pub ignore_private_windows: bool,
    pub private_title_patterns: Vec<String>,
    /// Never move windows already open before restore started unless matched.
    pub preserve_open_windows: bool,
    pub max_concurrent_launches: usize,
    pub per_app_timeout_s: u64,
    pub total_timeout_s: u64,
    pub power_loss_policy: PowerLossPolicy,
    pub do_not_restore: Vec<String>,
    /// Disable automatic restore after this many consecutive failed runs.
    pub max_consecutive_failures: u32,
    /// Max seconds to wait for monitor/window topology to settle at login.
    pub readiness_timeout_s: u64,
    /// Store window titles (as disambiguation hints) in snapshots.
    pub store_titles: bool,
    /// Pause capture while the session is locked.
    pub skip_capture_when_locked: bool,
}

impl Default for SessionConfig {
    fn default() -> Self {
        SessionConfig {
            policy: RestorePolicy::Manual,
            named_layout: None,
            autosave_enabled: true,
            autosave_interval_min: 10,
            max_recent: 30,
            daily_history_days: 7,
            confirm_before_restore: true,
            restore_once_per_login: true,
            restore_on_monitor_reconnect: false,
            ignore_private_windows: true,
            private_title_patterns: vec![
                "(?i)private browsing".into(),
                "(?i)incognito".into(),
                "(?i)inprivate".into(),
                "(?i)\\bprivate window\\b".into(),
            ],
            preserve_open_windows: true,
            max_concurrent_launches: 3,
            per_app_timeout_s: 30,
            total_timeout_s: 180,
            power_loss_policy: PowerLossPolicy::Ask,
            do_not_restore: vec![],
            max_consecutive_failures: 3,
            readiness_timeout_s: 60,
            store_titles: true,
            skip_capture_when_locked: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct AppPolicy {
    #[serde(default)]
    pub launch: LaunchPolicy,
    /// Extra argv entries the user explicitly approved (never shell-parsed).
    #[serde(default)]
    pub extra_args: Vec<String>,
    /// The application restores its own documents/tabs; Fractile only
    /// launches it once and places the windows it opens. `None` means the
    /// user has not chosen, and the built-in list of well-known
    /// session-restoring apps decides (see [`builtin_manages_session`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub app_manages_session: Option<bool>,
}

/// Browsers and editors that reopen their own windows and tabs when started.
/// Launching them once per recorded window would open extra windows, so a
/// restore starts them once and places what they open.
pub fn builtin_manages_session(desktop_id: &str) -> bool {
    let id = desktop_id.strip_suffix(".desktop").unwrap_or(desktop_id).to_ascii_lowercase();
    const EXACT: &[&str] = &[
        "firefox",
        "firefox-esr",
        "org.mozilla.firefox",
        "firefoxdeveloperedition",
        "librewolf",
        "io.gitlab.librewolf-community",
        "google-chrome",
        "google-chrome-beta",
        "google-chrome-unstable",
        "com.google.chrome",
        "chromium",
        "chromium-browser",
        "org.chromium.chromium",
        "brave-browser",
        "com.brave.browser",
        "microsoft-edge",
        "com.microsoft.edge",
        "vivaldi-stable",
        "com.vivaldi.vivaldi",
        "org.gnome.epiphany",
        "code",
        "code-insiders",
        "com.microsoft.vscode",
        "com.microsoft.vscodeinsiders",
        "code-oss",
        "com.visualstudio.code",
        "codium",
        "com.vscodium.codium",
        "dev.zed.zed",
    ];
    EXACT.contains(&id.as_str()) || id.starts_with("jetbrains-") || id.starts_with("com.jetbrains.")
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Profile {
    pub name: String,
    /// Auto-select when the monitor topology signature matches.
    #[serde(default)]
    pub topology: Option<String>,
    #[serde(default)]
    pub default_grid: Option<Grid>,
    #[serde(default)]
    pub spacing: Option<Spacing>,
    #[serde(default)]
    pub login_layout: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct DiagnosticsConfig {
    pub log_level: String,
    pub max_log_kb: u64,
}

impl Default for DiagnosticsConfig {
    fn default() -> Self {
        DiagnosticsConfig { log_level: "info".into(), max_log_kb: 2048 }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub schema_version: u32,
    pub general: GeneralConfig,
    pub appearance: AppearanceConfig,
    pub placement: PlacementConfig,
    pub shortcuts: Vec<NamedShortcut>,
    pub rules_enabled: bool,
    /// Opt-in: allow rules/layouts to match on window title regex.
    pub allow_title_regex: bool,
    pub rules: Vec<Rule>,
    pub session: SessionConfig,
    pub apps: BTreeMap<String, AppPolicy>,
    pub profiles: Vec<Profile>,
    pub auto_switch_profiles: bool,
    pub active_profile: Option<String>,
    pub diagnostics: DiagnosticsConfig,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            schema_version: CONFIG_SCHEMA_VERSION,
            general: GeneralConfig::default(),
            appearance: AppearanceConfig::default(),
            placement: PlacementConfig::default(),
            shortcuts: default_shortcuts(),
            rules_enabled: true,
            allow_title_regex: false,
            rules: vec![],
            session: SessionConfig::default(),
            apps: BTreeMap::new(),
            profiles: vec![],
            auto_switch_profiles: false,
            active_profile: None,
            diagnostics: DiagnosticsConfig::default(),
        }
    }
}

/// Default named shortcuts. Global accelerators are left unassigned: the
/// first-run wizard assigns them only after probing for collisions.
pub fn default_shortcuts() -> Vec<NamedShortcut> {
    let mut v = vec![];
    let mut add = |id: &str, name: &str, action: ShortcutAction, accel: Option<&str>, scope: ShortcutScope| {
        v.push(NamedShortcut { id: id.into(), name: name.into(), action, accelerator: accel.map(|s| s.into()), scope, cycle_monitors: false, favorite: false });
    };
    // Panel-local shortcuts (only active while the panel is visible).
    add("local-left", "Left half", ShortcutAction::Snap { action: SnapAction::LeftHalf }, Some("Ctrl+Left"), ShortcutScope::Local);
    add("local-right", "Right half", ShortcutAction::Snap { action: SnapAction::RightHalf }, Some("Ctrl+Right"), ShortcutScope::Local);
    add("local-up", "Top half", ShortcutAction::Snap { action: SnapAction::TopHalf }, Some("Ctrl+Up"), ShortcutScope::Local);
    add("local-down", "Bottom half", ShortcutAction::Snap { action: SnapAction::BottomHalf }, Some("Ctrl+Down"), ShortcutScope::Local);
    add("local-max", "Maximize (fill work area)", ShortcutAction::Snap { action: SnapAction::Maximize }, Some("M"), ShortcutScope::Local);
    add("local-center", "Center", ShortcutAction::Snap { action: SnapAction::Center }, Some("C"), ShortcutScope::Local);
    // Global actions: unassigned until the user or wizard picks a free key.
    add("global-panel", "Show/hide panel", ShortcutAction::TogglePanel, None, ShortcutScope::Global);
    add("global-undo", "Undo last placement", ShortcutAction::Undo, None, ShortcutScope::Global);
    add("global-next-output", "Move window to next monitor", ShortcutAction::MoveToNextOutput, None, ShortcutScope::Global);
    add("global-save", "Save current workspace", ShortcutAction::SaveWorkspace, None, ShortcutScope::Global);
    add("global-restore", "Restore latest snapshot", ShortcutAction::RestoreLatest, None, ShortcutScope::Global);
    add("global-palette", "Command palette", ShortcutAction::CommandPalette, None, ShortcutScope::Global);
    v
}

/// Keys the panel itself uses (grid keyboard navigation, apply, cancel,
/// grid resize). Local shortcuts may not use them, so typing in the panel
/// never collides (DV-07, DV-09, DV-15).
pub const PANEL_RESERVED_KEYS: [&str; 22] = [
    "Left", "Right", "Up", "Down", "Shift+Left", "Shift+Right", "Shift+Up", "Shift+Down", "Return", "KP_Enter", "Space", "Escape", "Tab", "Shift+Tab",
    "Alt+plus", "Alt+minus", "Alt+Shift+plus", "Alt+Shift+minus", "Alt+equal", "Alt+Shift+equal", "Alt+KP_Add", "Alt+KP_Subtract",
];

pub fn is_panel_reserved(a: &Accelerator) -> bool {
    PANEL_RESERVED_KEYS.iter().any(|k| Accelerator::parse(k).map(|r| r.same_as(a)).unwrap_or(false))
}

/// Accelerator candidates for the panel toggle, tried in order by the
/// first-run wizard; the first one that registers without conflict wins.
pub const PANEL_SHORTCUT_CANDIDATES: [&str; 5] = ["Super+Alt+G", "Ctrl+Alt+G", "Super+Alt+Space", "Ctrl+Alt+Shift+G", "Super+Ctrl+G"];

impl Config {
    /// Whether `desktop_id` restores its own windows: the user's per-app
    /// setting if there is one, else the built-in list.
    pub fn manages_session(&self, desktop_id: &str) -> bool {
        self.apps.get(desktop_id).and_then(|a| a.app_manages_session).unwrap_or_else(|| builtin_manages_session(desktop_id))
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version > CONFIG_SCHEMA_VERSION {
            return Err(format!("config schema {} is newer than supported {}", self.schema_version, CONFIG_SCHEMA_VERSION));
        }
        if !self.appearance.default_grid.is_valid_base() {
            return Err("default grid must be between 1x1 and 10x10".into());
        }
        if !(0.5..=3.0).contains(&self.appearance.font_scale) {
            return Err("font scale must be 0.5..3.0".into());
        }
        if !(16..=120).contains(&self.appearance.cell_size) {
            return Err("cell size must be 16..120 px".into());
        }
        let sp = &self.appearance.spacing;
        for (label, l) in [("left margin", sp.margins.left), ("right margin", sp.margins.right), ("top margin", sp.margins.top), ("bottom margin", sp.margins.bottom), ("horizontal gap", sp.gaps.horizontal), ("vertical gap", sp.gaps.vertical)] {
            match l {
                Length::Px(p) if p > MAX_MARGIN_PX => return Err(format!("{label} must be 0..{MAX_MARGIN_PX} px")),
                Length::Percent(p) if !(0.0..=45.0).contains(&p) || !p.is_finite() => return Err(format!("{label} percent must be 0..45")),
                _ => {}
            }
        }
        if self.placement.tolerance_px < 0 || self.placement.tolerance_px > 64 {
            return Err("tolerance must be 0..64 px".into());
        }
        if let Some(p) = &self.general.panel_shortcut {
            Accelerator::parse(p).map_err(|e| format!("panel shortcut: {e}"))?;
        }
        let mut seen: Vec<(ShortcutScope, Accelerator, String)> = vec![];
        let mut ids = std::collections::HashSet::new();
        for s in &self.shortcuts {
            if !ids.insert(s.id.clone()) {
                return Err(format!("duplicate shortcut id '{}'", s.id));
            }
            if s.name.trim().is_empty() {
                return Err("shortcut name required".into());
            }
            if let ShortcutAction::Place { grid, selection, .. } = &s.action {
                if !grid.is_valid_effective() || !selection.is_valid_for(*grid) {
                    return Err(format!("shortcut '{}' has an invalid grid selection", s.name));
                }
            }
            if let Some(a) = &s.accelerator {
                let acc = Accelerator::parse(a).map_err(|e| format!("shortcut '{}': {e}", s.name))?;
                if s.scope == ShortcutScope::Local && is_panel_reserved(&acc) {
                    return Err(format!("local shortcut '{}' uses {a}, which the panel needs for keyboard navigation", s.name));
                }
                if s.scope == ShortcutScope::Global && !acc.has_modifier() {
                    return Err(format!("global shortcut '{}' needs at least one modifier", s.name));
                }
                if let Some((_, _, other)) = seen.iter().find(|(sc, ac, _)| *sc == s.scope && ac.same_as(&acc)) {
                    return Err(format!("shortcut '{}' uses the same keys as '{}'", s.name, other));
                }
                seen.push((s.scope, acc, s.name.clone()));
            }
        }
        if let Some(p) = &self.general.panel_shortcut {
            let acc = Accelerator::parse(p)?;
            if let Some((_, _, other)) = seen.iter().find(|(sc, ac, _)| *sc == ShortcutScope::Global && ac.same_as(&acc)) {
                return Err(format!("panel shortcut collides with '{other}'"));
            }
        }
        for r in &self.rules {
            r.validate()?;
        }
        let s = &self.session;
        if s.autosave_interval_min == 0 || s.autosave_interval_min > 24 * 60 {
            return Err("autosave interval must be 1..1440 minutes".into());
        }
        if s.max_recent == 0 || s.max_recent > 500 {
            return Err("max recent snapshots must be 1..500".into());
        }
        if s.max_concurrent_launches == 0 || s.max_concurrent_launches > 16 {
            return Err("max concurrent launches must be 1..16".into());
        }
        if s.per_app_timeout_s == 0 || s.per_app_timeout_s > 600 || s.total_timeout_s == 0 || s.total_timeout_s > 3600 {
            return Err("restore timeouts out of range".into());
        }
        for p in &s.private_title_patterns {
            regex::RegexBuilder::new(p).size_limit(1 << 16).build().map_err(|e| format!("private pattern '{p}': {e}"))?;
        }
        for (id, ap) in &self.apps {
            if !is_safe_desktop_id(id) {
                return Err(format!("unsafe app id '{id}'"));
            }
            if ap.extra_args.len() > 32 || ap.extra_args.iter().any(|a| a.len() > 1024 || a.contains('\0')) {
                return Err(format!("extra args for '{id}' invalid"));
            }
        }
        if s.policy == RestorePolicy::Named && s.named_layout.as_deref().map(str::is_empty).unwrap_or(true) {
            return Err("restore policy 'named' requires a layout name".into());
        }
        Ok(())
    }

    /// Effective spacing/grid after applying the active profile.
    pub fn effective_spacing(&self) -> Spacing {
        self.active_profile_ref().and_then(|p| p.spacing).unwrap_or(self.appearance.spacing)
    }
    pub fn effective_default_grid(&self) -> Grid {
        self.active_profile_ref().and_then(|p| p.default_grid).unwrap_or(self.appearance.default_grid)
    }
    pub fn active_profile_ref(&self) -> Option<&Profile> {
        self.active_profile.as_ref().and_then(|n| self.profiles.iter().find(|p| &p.name == n))
    }

    /// Migrate older schemas in place. v0 (pre-release) had no version field.
    pub fn migrate(mut self) -> Config {
        if self.schema_version == 0 {
            self.schema_version = 1;
        }
        // Ensure default shortcut ids exist (new actions added in later versions).
        for d in default_shortcuts() {
            if !self.shortcuts.iter().any(|s| s.id == d.id) && d.scope == ShortcutScope::Global {
                self.shortcuts.push(d);
            }
        }
        self
    }

    pub fn from_toml(s: &str) -> Result<Config, String> {
        let c: Config = toml::from_str(s).map_err(|e| format!("config parse error: {e}"))?;
        Ok(c.migrate())
    }

    pub fn to_toml(&self) -> String {
        toml::to_string_pretty(self).expect("config serializes")
    }

    pub fn load(path: &Path) -> Result<Config, String> {
        match std::fs::read_to_string(path) {
            Ok(s) => {
                let c = Config::from_toml(&s)?;
                c.validate()?;
                Ok(c)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config::default()),
            Err(e) => Err(format!("cannot read {}: {e}", path.display())),
        }
    }

    /// Atomic save: write temp file, fsync, rename, fsync dir. Keeps a .bak.
    pub fn save(&self, path: &Path) -> Result<(), String> {
        self.validate()?;
        atomic_write(path, self.to_toml().as_bytes(), true)
    }
}

/// Crash-consistent file replacement (temp + fsync + rename + dir fsync).
pub fn atomic_write(path: &Path, data: &[u8], keep_backup: bool) -> Result<(), String> {
    let dir = path.parent().ok_or("path has no parent")?;
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let tmp: PathBuf = dir.join(format!(".{}.tmp-{}", path.file_name().unwrap().to_string_lossy(), std::process::id()));
    {
        let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).open(&tmp).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = f.set_permissions(std::fs::Permissions::from_mode(0o600));
        }
        f.write_all(data).map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
    }
    if keep_backup && path.exists() {
        let _ = std::fs::copy(path, path.with_extension("toml.bak"));
    }
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    if let Ok(d) = std::fs::File::open(dir) {
        let _ = d.sync_all();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn well_known_browsers_and_editors_restore_their_own_windows() {
        for id in ["firefox.desktop", "org.mozilla.firefox.desktop", "google-chrome.desktop", "code.desktop", "com.visualstudio.code.desktop", "com.microsoft.VSCode.desktop", "jetbrains-idea.desktop", "Chromium.desktop"] {
            assert!(builtin_manages_session(id), "{id}");
        }
        for id in ["org.gnome.TextEditor.desktop", "xterm.desktop", "firefoxish.desktop", "codex.desktop"] {
            assert!(!builtin_manages_session(id), "{id}");
        }
    }

    #[test]
    fn the_users_choice_overrides_the_builtin_list() {
        let mut c = Config::default();
        assert!(c.manages_session("firefox.desktop"));
        assert!(!c.manages_session("gedit.desktop"));
        c.apps.insert("firefox.desktop".into(), AppPolicy { app_manages_session: Some(false), ..Default::default() });
        c.apps.insert("gedit.desktop".into(), AppPolicy { app_manages_session: Some(true), ..Default::default() });
        assert!(!c.manages_session("firefox.desktop"));
        assert!(c.manages_session("gedit.desktop"));
        // A policy that only sets the launch mode keeps the built-in default.
        c.apps.insert("code.desktop".into(), AppPolicy { launch: LaunchPolicy::Always, ..Default::default() });
        assert!(c.manages_session("code.desktop"));
        // Old configs wrote a plain bool; it still reads as the user's choice.
        let p: AppPolicy = serde_json::from_str(r#"{"launch":"ask","app_manages_session":false}"#).unwrap();
        assert_eq!(p.app_manages_session, Some(false));
        let p: AppPolicy = serde_json::from_str(r#"{"launch":"ask"}"#).unwrap();
        assert_eq!(p.app_manages_session, None);
        assert!(!serde_json::to_string(&p).unwrap().contains("app_manages_session"));
    }

    #[test]
    fn panel_reserved_keys_parse_and_block_local_shortcuts() {
        for k in PANEL_RESERVED_KEYS {
            assert!(Accelerator::parse(k).is_ok(), "reserved key {k} must parse");
        }
        let mut c = Config::default();
        c.shortcuts[0].accelerator = Some("Left".into());
        assert!(c.validate().unwrap_err().contains("keyboard navigation"));
        c.shortcuts[0].accelerator = Some("Ctrl+Left".into());
        assert!(c.validate().is_ok());
    }

    #[test]
    fn default_round_trips_and_validates() {
        let c = Config::default();
        c.validate().unwrap();
        let s = c.to_toml();
        let d = Config::from_toml(&s).unwrap();
        assert_eq!(c, d);
    }

    #[test]
    fn rejects_bad_values() {
        let mut c = Config::default();
        c.appearance.default_grid = Grid::new(11, 6);
        assert!(c.validate().is_err());
        let mut c = Config::default();
        c.appearance.spacing.margins.left = Length::Px(301);
        assert!(c.validate().is_err());
        let mut c = Config::default();
        c.shortcuts[6].accelerator = Some("G".into()); // global without modifier
        assert!(c.validate().is_err());
        let mut c = Config::default();
        c.shortcuts[6].accelerator = Some("Ctrl+Alt+G".into());
        c.shortcuts[7].accelerator = Some("<Control><Alt>g".into());
        assert!(c.validate().unwrap_err().contains("same keys"));
    }

    #[test]
    fn tolerant_of_unknown_and_missing_keys() {
        let c = Config::from_toml("unknown_key = 3\n[general]\ntray_icon = false\n").unwrap();
        assert!(!c.general.tray_icon);
        assert_eq!(c.appearance.default_grid, Grid::new(6, 6));
        c.validate().unwrap();
    }

    #[test]
    fn atomic_write_replaces() {
        let d = std::env::temp_dir().join(format!("fractile-test-{}", uuid::Uuid::new_v4()));
        let p = d.join("c.toml");
        atomic_write(&p, b"a", true).unwrap();
        atomic_write(&p, b"b", true).unwrap();
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "b");
        assert_eq!(std::fs::read_to_string(p.with_extension("toml.bak")).unwrap(), "a");
        std::fs::remove_dir_all(d).unwrap();
    }
}

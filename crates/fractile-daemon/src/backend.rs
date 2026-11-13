//! Desktop/session detection and adapter selection. Truthful capability
//! reporting for desktops where window control is not possible.

use fractile_core::adapter::*;
use fractile_core::geometry::Rect;
use fractile_core::output::Output;
use fractile_core::window::{WindowInfo, WindowState};
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::mpsc::{channel, Receiver};

#[derive(Debug, Clone, Serialize)]
pub struct Environment {
    pub session_type: String,
    pub current_desktop: String,
    pub desktop_session: String,
    pub wayland_display: Option<String>,
    pub x_display: Option<String>,
    pub os_pretty_name: String,
    pub gnome_shell_version: Option<String>,
    pub cosmic_comp_running: bool,
    /// kwin_wayland runs for this user. (kwin_x11 says nothing about a Wayland session.)
    pub kwin_running: bool,
    /// HYPRLAND_INSTANCE_SIGNATURE from the environment, when set.
    pub hyprland_signature: Option<String>,
    /// A Hyprland compositor runs for this user.
    pub hyprland_running: bool,
    /// SWAYSOCK from the environment, when set.
    pub sway_socket: Option<String>,
    /// A Sway compositor runs for this user.
    pub sway_running: bool,
}

fn proc_running(name: &str) -> bool {
    let Ok(rd) = std::fs::read_dir("/proc") else { return false };
    let uid = fractile_ipc::current_uid();
    for e in rd.flatten() {
        let p = e.path();
        if !p.file_name().map(|f| f.to_string_lossy().chars().all(|c| c.is_ascii_digit())).unwrap_or(false) {
            continue;
        }
        use std::os::unix::fs::MetadataExt;
        if std::fs::metadata(&p).map(|m| m.uid() != uid).unwrap_or(true) {
            continue;
        }
        if let Ok(c) = std::fs::read_to_string(p.join("comm")) {
            if c.trim() == name {
                return true;
            }
        }
    }
    false
}

pub fn detect() -> Environment {
    let os = std::fs::read_to_string("/etc/os-release")
        .ok()
        .and_then(|s| s.lines().find(|l| l.starts_with("PRETTY_NAME=")).map(|l| l.trim_start_matches("PRETTY_NAME=").trim_matches('"').to_string()))
        .unwrap_or_else(|| "unknown".into());
    let gsv = std::process::Command::new("gnome-shell").arg("--version").output().ok().filter(|o| o.status.success()).map(|o| String::from_utf8_lossy(&o.stdout).trim().trim_start_matches("GNOME Shell ").to_string());
    Environment {
        session_type: std::env::var("XDG_SESSION_TYPE").unwrap_or_default(),
        current_desktop: std::env::var("XDG_CURRENT_DESKTOP").unwrap_or_default(),
        desktop_session: std::env::var("DESKTOP_SESSION").unwrap_or_default(),
        wayland_display: std::env::var("WAYLAND_DISPLAY").ok().filter(|v| !v.is_empty()),
        x_display: std::env::var("DISPLAY").ok().filter(|v| !v.is_empty()),
        os_pretty_name: os,
        gnome_shell_version: gsv,
        cosmic_comp_running: proc_running("cosmic-comp"),
        kwin_running: proc_running("kwin_wayland"),
        hyprland_signature: std::env::var("HYPRLAND_INSTANCE_SIGNATURE").ok().filter(|v| !v.is_empty()),
        hyprland_running: proc_running("Hyprland"),
        sway_socket: std::env::var("SWAYSOCK").ok().filter(|v| !v.is_empty()),
        sway_running: proc_running("sway"),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Backend {
    X11,
    GnomeShell,
    KWin,
    Hyprland,
    Sway,
    Cosmic,
    Unsupported,
}

impl Environment {
    pub fn is_wayland(&self) -> bool {
        self.session_type == "wayland" || (self.wayland_display.is_some() && self.session_type != "x11")
    }
    pub fn desktop_has(&self, s: &str) -> bool {
        self.current_desktop.split(':').any(|d| d.eq_ignore_ascii_case(s))
    }
    pub fn choose(&self, forced: Option<&str>) -> Backend {
        match forced {
            Some("x11") => return Backend::X11,
            Some("gnome") => return Backend::GnomeShell,
            Some("kwin") => return Backend::KWin,
            Some("hyprland") => return Backend::Hyprland,
            Some("sway") => return Backend::Sway,
            Some("cosmic") => return Backend::Cosmic,
            _ => {}
        }
        if !self.is_wayland() {
            return if self.x_display.is_some() { Backend::X11 } else { Backend::Unsupported };
        }
        // Hyprland first: a Hyprland session nested in (or next to) KDE still
        // names itself in XDG_CURRENT_DESKTOP and the instance signature.
        if self.desktop_has("Hyprland") || self.hyprland_signature.is_some() {
            return Backend::Hyprland;
        }
        if self.desktop_has("sway") || self.sway_socket.is_some() {
            return Backend::Sway;
        }
        if self.desktop_has("GNOME") || self.desktop_has("ubuntu") && self.gnome_shell_version.is_some() {
            Backend::GnomeShell
        } else if self.desktop_has("KDE") || self.kwin_running {
            Backend::KWin
        } else if self.desktop_has("COSMIC") || self.cosmic_comp_running {
            Backend::Cosmic
        } else if self.hyprland_running {
            Backend::Hyprland
        } else if self.sway_running {
            Backend::Sway
        } else {
            Backend::Unsupported
        }
    }
}

/// Adapter used when no window control exists. Every operation reports the
/// real reason instead of silently succeeding.
pub struct StubAdapter {
    pub name: String,
    pub session_type: String,
    pub compositor: String,
    pub reason: String,
    pub notes: Vec<String>,
}

impl StubAdapter {
    pub fn cosmic() -> StubAdapter {
        StubAdapter {
            name: "cosmic".into(),
            session_type: "wayland".into(),
            compositor: "cosmic-comp".into(),
            reason: "Fractile has no COSMIC adapter: COSMIC has no client API to move or resize other applications' windows (its toplevel management protocol covers activate, close, minimize, maximize, fullscreen, sticky and workspace moves only), so window control is not offered on COSMIC.".into(),
            notes: vec![
                "BLOCKED BY PLATFORM: window geometry control needs a compositor-side hook; see docs/compatibility/cosmic.md for the upstream proposal.".into(),
                "COSMIC auto-tiling is never modified by Fractile.".into(),
            ],
        }
    }
    /// Hyprland was detected but its IPC could not be used.
    pub fn hyprland(reason: String) -> StubAdapter {
        StubAdapter {
            name: "hyprland".into(),
            session_type: "wayland".into(),
            compositor: "Hyprland".into(),
            reason: format!("no window control on Hyprland: {reason}"),
            notes: vec!["Fractile reaches Hyprland through its IPC socket in $XDG_RUNTIME_DIR/hypr. See docs/compatibility/hyprland.md.".into()],
        }
    }
    /// Sway was detected but its IPC could not be used.
    pub fn sway(reason: String) -> StubAdapter {
        StubAdapter {
            name: "sway".into(),
            session_type: "wayland".into(),
            compositor: "Sway".into(),
            reason: format!("no window control on Sway: {reason}"),
            notes: vec!["Fractile reaches Sway through its IPC socket ($SWAYSOCK, or sway-ipc.*.sock in $XDG_RUNTIME_DIR). See docs/compatibility/sway.md.".into()],
        }
    }
    pub fn unsupported(env: &Environment) -> StubAdapter {
        StubAdapter {
            name: "none".into(),
            session_type: env.session_type.clone(),
            compositor: env.current_desktop.clone(),
            reason: if env.wayland_display.is_none() && env.x_display.is_none() {
                "no graphical session: neither WAYLAND_DISPLAY nor DISPLAY is set (started outside a desktop login?)".into()
            } else {
                format!("no Fractile adapter for session '{}' desktop '{}'", env.session_type, env.current_desktop)
            },
            notes: if env.wayland_display.is_some() {
                vec!["Wayland clients cannot position other applications' windows without a compositor-specific integration.".into()]
            } else {
                vec![]
            },
        }
    }
    fn err<T>(&self) -> AResult<T> {
        Err(AdapterError::Unsupported(self.reason.clone()))
    }
}

impl CompositorAdapter for StubAdapter {
    fn capabilities(&self) -> Capabilities {
        let mut ops = BTreeMap::new();
        for op in Operation::ALL {
            ops.insert(op, SupportLevel::Unsupported(self.reason.clone()));
        }
        Capabilities { adapter: self.name.clone(), adapter_version: env!("CARGO_PKG_VERSION").into(), api_version: ADAPTER_API_VERSION, session_type: self.session_type.clone(), compositor: self.compositor.clone(), compositor_version: None, operations: ops, tiling_manager: None, notes: self.notes.clone() }
    }
    fn list_windows(&self) -> AResult<Vec<WindowInfo>> {
        self.err()
    }
    fn get_active_window(&self) -> AResult<Option<String>> {
        self.err()
    }
    fn get_outputs(&self) -> AResult<Vec<Output>> {
        self.err()
    }
    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        self.err()
    }
    fn move_resize(&self, _id: &str, _r: Rect) -> AResult<PlacementOutcome> {
        self.err()
    }
    fn set_state(&self, _id: &str, _s: &WindowState) -> AResult<()> {
        self.err()
    }
    fn move_to_workspace(&self, _id: &str, _w: u32) -> AResult<()> {
        self.err()
    }
    fn activate(&self, _id: &str, _t: Option<u32>) -> AResult<()> {
        self.err()
    }
    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        let (_tx, rx) = channel();
        std::mem::forget(_tx);
        Ok(rx)
    }
    fn register_hotkey(&self, _b: &str, _a: &str) -> AResult<()> {
        self.err()
    }
    fn unregister_hotkey(&self, _b: &str) -> AResult<()> {
        Ok(())
    }
    fn set_own_windows(&self, _ids: &[String]) {}
}

/// Detect automatic tiling managers that would fight floating placement.
pub fn detect_tiling(env: &Environment) -> Option<String> {
    match env.choose(None) {
        // Tiling compositors: windows Fractile places become floating.
        Backend::Hyprland => return Some("hyprland".into()),
        Backend::Sway => return Some("sway".into()),
        _ => {}
    }
    if env.desktop_has("COSMIC") || env.cosmic_comp_running {
        let home = std::env::var("HOME").unwrap_or_default();
        let p = format!("{home}/.config/cosmic/com.system76.CosmicComp/v1/autotile");
        if std::fs::read_to_string(&p).map(|s| s.trim() == "true").unwrap_or(false) {
            return Some("cosmic-autotile".into());
        }
    }
    if env.gnome_shell_version.is_some() {
        let out = std::process::Command::new("gsettings").args(["get", "org.gnome.shell", "enabled-extensions"]).output().ok()?;
        let s = String::from_utf8_lossy(&out.stdout);
        for (ext, name) in [("pop-shell@system76.com", "pop-shell"), ("tilingshell@ferrarodomenico.com", "tiling-shell"), ("forge@jmmaranan.com", "forge")] {
            if s.contains(ext) {
                if name == "pop-shell" {
                    let t = std::process::Command::new("gsettings").args(["get", "org.gnome.shell.extensions.pop-shell", "tile-by-default"]).output().ok();
                    if t.map(|o| String::from_utf8_lossy(&o.stdout).trim() == "true").unwrap_or(false) {
                        return Some("pop-shell (auto-tile on)".into());
                    }
                    continue;
                }
                return Some(name.into());
            }
        }
    }
    None
}

//! Window model shared by adapters, the daemon and session restore.

use crate::geometry::{Rect, SizeConstraints};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum WindowType {
    #[default]
    Normal,
    Dialog,
    Utility,
    Toolbar,
    Menu,
    Splash,
    Dock,
    Desktop,
    Notification,
    Tooltip,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct WindowState {
    #[serde(default)]
    pub maximized: bool,
    #[serde(default)]
    pub fullscreen: bool,
    #[serde(default)]
    pub minimized: bool,
    #[serde(default)]
    pub above: bool,
    #[serde(default)]
    pub sticky: bool,
    #[serde(default)]
    pub modal: bool,
    #[serde(default)]
    pub skip_taskbar: bool,
    /// True when an automatic tiler currently manages the window.
    #[serde(default)]
    pub tiled: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct WindowInfo {
    /// Adapter-scoped id, e.g. "x11:0x3a00007" or "gnome:1234567".
    /// Valid only for the current session; never persisted as identity.
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub app_id: Option<String>,
    #[serde(default)]
    pub wm_class_instance: Option<String>,
    #[serde(default)]
    pub wm_class: Option<String>,
    #[serde(default)]
    pub role: Option<String>,
    #[serde(default)]
    pub desktop_file_id: Option<String>,
    /// How desktop_file_id was determined (e.g. "systemd-scope", "gio-env",
    /// "app-id", "startup-wm-class", "wm-class-name", "exec-name").
    #[serde(default)]
    pub desktop_file_source: Option<String>,
    #[serde(default)]
    pub pid: Option<u32>,
    /// Informational only: never executed directly.
    #[serde(default)]
    pub executable: Option<String>,
    #[serde(default)]
    pub sandbox: Option<String>,
    #[serde(default)]
    pub window_type: WindowType,
    #[serde(default)]
    pub transient_for: Option<String>,
    /// Window group leader (X11 WM_HINTS group / client leader).
    #[serde(default)]
    pub group: Option<String>,
    /// Visible frame rectangle in logical coordinates.
    pub frame: Rect,
    /// Client (content) rectangle if different from frame.
    #[serde(default)]
    pub client: Option<Rect>,
    #[serde(default)]
    pub constraints: SizeConstraints,
    #[serde(default)]
    pub state: WindowState,
    /// Workspace index (None = all workspaces / sticky / unknown).
    #[serde(default)]
    pub workspace: Option<u32>,
    #[serde(default)]
    pub output_id: Option<String>,
    /// Stacking position, 0 = bottom.
    #[serde(default)]
    pub stacking: Option<u32>,
    #[serde(default)]
    pub focused: bool,
    /// Window belongs to Fractile itself (panels, settings, overlays).
    #[serde(default)]
    pub is_own: bool,
}

/// Why a window is not eligible for automatic placement / snapshotting.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Ineligible {
    OwnWindow,
    NonNormalType(WindowType),
    Transient,
    Modal,
    SkipTaskbar,
    Fullscreen,
}

impl WindowInfo {
    /// Best key for grouping windows of the same application.
    pub fn app_key(&self) -> String {
        self.desktop_file_id
            .clone()
            .map(|d| d.trim_end_matches(".desktop").to_lowercase())
            .or_else(|| self.app_id.clone().map(|a| a.to_lowercase()))
            .or_else(|| self.wm_class.clone().map(|c| c.to_lowercase()))
            .or_else(|| self.wm_class_instance.clone().map(|c| c.to_lowercase()))
            .unwrap_or_else(|| format!("unknown:{}", self.executable.clone().unwrap_or_default()))
    }

    /// Placement/snapshot eligibility. Dialogs, tool windows, panels, system
    /// windows and Fractile's own windows are excluded by default.
    pub fn placement_eligibility(&self) -> Result<(), Ineligible> {
        if self.is_own {
            return Err(Ineligible::OwnWindow);
        }
        match self.window_type {
            WindowType::Normal => {}
            WindowType::Dialog if self.transient_for.is_none() && !self.state.modal => {}
            t => return Err(Ineligible::NonNormalType(t)),
        }
        if self.state.modal {
            return Err(Ineligible::Modal);
        }
        if self.transient_for.is_some() {
            return Err(Ineligible::Transient);
        }
        if self.state.skip_taskbar {
            return Err(Ineligible::SkipTaskbar);
        }
        Ok(())
    }

    pub fn is_placeable(&self) -> bool {
        self.placement_eligibility().is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dialogs_and_own_windows_excluded() {
        let mut w = WindowInfo { id: "x".into(), title: "t".into(), ..Default::default() };
        assert!(w.is_placeable());
        w.transient_for = Some("y".into());
        assert_eq!(w.placement_eligibility(), Err(Ineligible::Transient));
        w.transient_for = None;
        w.window_type = WindowType::Dock;
        assert!(!w.is_placeable());
        w.window_type = WindowType::Normal;
        w.is_own = true;
        assert_eq!(w.placement_eligibility(), Err(Ineligible::OwnWindow));
    }

    #[test]
    fn app_key_prefers_desktop_id() {
        let w = WindowInfo {
            desktop_file_id: Some("org.gnome.Terminal.desktop".into()),
            wm_class: Some("Gnome-terminal".into()),
            ..Default::default()
        };
        assert_eq!(w.app_key(), "org.gnome.terminal");
    }
}

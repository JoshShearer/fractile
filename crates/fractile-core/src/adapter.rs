//! The `CompositorAdapter` contract. Adapters are the only layer that may
//! manipulate compositor state. Every operation declares a support level;
//! core logic must check capabilities instead of assuming operations exist.

use crate::geometry::Rect;
use crate::output::Output;
use crate::window::{WindowInfo, WindowState};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::sync::mpsc::Receiver;

/// Adapter API version. Bump when the trait or semantics change; bridges
/// (GNOME extension, KWin script) negotiate against this.
pub const ADAPTER_API_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    ListWindows,
    GetActiveWindow,
    GetOutputs,
    GetWorkspaces,
    GetGeometry,
    MoveResize,
    SetState,
    MoveToOutput,
    MoveToWorkspace,
    Activate,
    SubscribeEvents,
    RegisterHotkey,
    ShowOverlay,
    GetPermissions,
}

impl Operation {
    pub const ALL: [Operation; 14] = [
        Operation::ListWindows,
        Operation::GetActiveWindow,
        Operation::GetOutputs,
        Operation::GetWorkspaces,
        Operation::GetGeometry,
        Operation::MoveResize,
        Operation::SetState,
        Operation::MoveToOutput,
        Operation::MoveToWorkspace,
        Operation::Activate,
        Operation::SubscribeEvents,
        Operation::RegisterHotkey,
        Operation::ShowOverlay,
        Operation::GetPermissions,
    ];
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "level", content = "note", rename_all = "snake_case")]
pub enum SupportLevel {
    /// Works as specified.
    Full,
    /// Works with documented caveats.
    Partial(String),
    /// Not possible with this adapter/compositor.
    Unsupported(String),
}

impl SupportLevel {
    pub fn usable(&self) -> bool {
        !matches!(self, SupportLevel::Unsupported(_))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Capabilities {
    pub adapter: String,
    pub adapter_version: String,
    pub api_version: u32,
    pub session_type: String,
    pub compositor: String,
    #[serde(default)]
    pub compositor_version: Option<String>,
    pub operations: BTreeMap<Operation, SupportLevel>,
    /// Automatic tiling manager detected (e.g. "pop-shell", "cosmic-autotile").
    #[serde(default)]
    pub tiling_manager: Option<String>,
    #[serde(default)]
    pub notes: Vec<String>,
}

impl Capabilities {
    pub fn supports(&self, op: Operation) -> bool {
        self.operations.get(&op).map(|s| s.usable()).unwrap_or(false)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "detail", rename_all = "snake_case")]
pub enum AdapterError {
    Unsupported(String),
    NoSuchWindow(String),
    Rejected(String),
    PermissionDenied(String),
    Disconnected(String),
    Timeout(String),
    Conflict(String),
    Other(String),
}

impl std::fmt::Display for AdapterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AdapterError::Unsupported(s) => write!(f, "unsupported: {s}"),
            AdapterError::NoSuchWindow(s) => write!(f, "no such window: {s}"),
            AdapterError::Rejected(s) => write!(f, "rejected by compositor: {s}"),
            AdapterError::PermissionDenied(s) => write!(f, "permission denied: {s}"),
            AdapterError::Disconnected(s) => write!(f, "adapter disconnected: {s}"),
            AdapterError::Timeout(s) => write!(f, "timed out: {s}"),
            AdapterError::Conflict(s) => write!(f, "conflict: {s}"),
            AdapterError::Other(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for AdapterError {}

pub type AResult<T> = Result<T, AdapterError>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Workspace {
    pub index: u32,
    pub name: String,
    pub active: bool,
}

/// Result of applying geometry: what we asked for vs what the compositor did.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlacementOutcome {
    pub window_id: String,
    pub requested: Rect,
    pub observed: Option<Rect>,
    pub max_error_px: Option<i32>,
    pub within_tolerance: bool,
    pub attempts: u32,
    #[serde(default)]
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AdapterEvent {
    WindowCreated { id: String },
    WindowClosed { id: String },
    WindowChanged { id: String },
    ActiveChanged { id: Option<String> },
    OutputsChanged,
    WorkspacesChanged,
    Hotkey { binding_id: String, timestamp: u32 },
    Disconnected { reason: String },
    /// The display server itself is gone (the X server closed the
    /// connection): the desktop session this service belongs to has ended.
    DisplayClosed { reason: String },
}

/// The compositor adapter contract.
pub trait CompositorAdapter: Send + Sync {
    fn capabilities(&self) -> Capabilities;
    fn list_windows(&self) -> AResult<Vec<WindowInfo>>;
    fn get_window(&self, id: &str) -> AResult<WindowInfo> {
        self.list_windows()?
            .into_iter()
            .find(|w| w.id == id)
            .ok_or_else(|| AdapterError::NoSuchWindow(id.to_string()))
    }
    fn get_active_window(&self) -> AResult<Option<String>>;
    fn get_outputs(&self) -> AResult<Vec<Output>>;
    fn get_workspaces(&self) -> AResult<Vec<Workspace>>;
    /// Move/resize the visible frame of a window to `frame` (logical coords).
    /// Implementations unmaximize first when needed and verify the result.
    fn move_resize(&self, id: &str, frame: Rect) -> AResult<PlacementOutcome>;
    fn set_state(&self, id: &str, state: &WindowState) -> AResult<()>;
    fn move_to_workspace(&self, id: &str, workspace: u32) -> AResult<()>;
    fn activate(&self, id: &str, timestamp: Option<u32>) -> AResult<()>;
    /// Start event delivery. May only be called once.
    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>>;
    /// Register a global hotkey. Returns Err(Conflict) when already taken.
    fn register_hotkey(&self, binding_id: &str, accelerator: &str) -> AResult<()>;
    fn unregister_hotkey(&self, binding_id: &str) -> AResult<()>;
    /// Optional: hold or release every desktop global shortcut (not only
    /// Fractile's) while a shortcut editor listens, the way the desktop's own
    /// shortcut editor does, so a combination the desktop owns reaches the
    /// editor and can be reported instead of running its desktop action.
    /// Returns true when the desktop honoured it.
    fn block_desktop_shortcuts(&self, _on: bool) -> bool {
        false
    }
    /// Declare windows that belong to Fractile (never targeted, never captured).
    fn set_own_windows(&self, ids: &[String]);
    /// Optional: a session-lock state, when the adapter can observe it.
    fn session_locked(&self) -> Option<bool> {
        None
    }
    /// Optional: draw the placement preview inside the compositor (Wayland,
    /// where a client overlay window would take focus and cannot position
    /// itself). `below_pids`: keep the preview under these processes' windows.
    fn show_overlay(&self, _rect: Rect, _label: &str, _below_pids: &[u32]) -> AResult<()> {
        Err(AdapterError::Unsupported("the preview is drawn by the Fractile UI on this desktop".into()))
    }
    fn hide_overlay(&self) -> AResult<()> {
        Ok(())
    }
}

//! Compositor bridge adapter for Wayland desktops whose window management is
//! only reachable from inside the compositor (GNOME Shell extension, KWin
//! script).
//!
//! GNOME: the extension exports no D-Bus API (which any process on the
//! session bus could call). It connects *to* Fractile's private 0600 socket
//! like any other client, says `hello`, then `bridge_attach`. The daemon sends
//! requests down that connection as `bridge_call` events and the integration
//! answers with `bridge_result`; it pushes window/output/hotkey changes with
//! `bridge_event`.
//!
//! KWin: scripts can only make outgoing D-Bus calls, so the daemon serves
//! `io.fractile.Bridge` (see kwin_dbus.rs) and the script long-polls it for
//! calls. Every D-Bus method authenticates the caller by its bus-verified pid.
//!
//! Either way the peer must be the compositor process itself: its executable
//! (not its settable comm name) must be a root-owned compositor binary.

use fractile_core::adapter::{AResult, AdapterError, AdapterEvent, Capabilities, CompositorAdapter, Operation, PlacementOutcome, SupportLevel, Workspace, ADAPTER_API_VERSION};
use fractile_core::geometry::Rect;
use fractile_core::output::Output;
use fractile_core::window::{WindowInfo, WindowState};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

static BRIDGE: OnceLock<Arc<BridgeAdapter>> = OnceLock::new();

/// The bridge adapter of this daemon, if the session uses one.
pub fn get() -> Option<Arc<BridgeAdapter>> {
    BRIDGE.get().cloned()
}

pub fn install(b: Arc<BridgeAdapter>) -> Arc<BridgeAdapter> {
    let _ = BRIDGE.set(b.clone());
    b
}

/// How calls reach the integration.
#[derive(Clone)]
pub enum Transport {
    /// GNOME: JSON lines on the integration's own socket connection.
    Socket(Arc<Mutex<UnixStream>>),
    /// KWin: queued for the script's next long-poll.
    Queue(async_channel::Sender<Value>),
}

struct Attached {
    conn_id: u64,
    peer_pid: i32,
    transport: Transport,
    caps: Capabilities,
    info: Value,
}

/// Global shortcuts handled outside the integration (KWin: KGlobalAccel).
pub trait HotkeyService: Send + Sync {
    fn register(&self, binding_id: &str, accel: &fractile_core::accel::Accelerator) -> AResult<()>;
    fn unregister(&self, binding_id: &str) -> AResult<()>;
}

/// The peer must be running a root-owned, not group/world-writable
/// compositor binary. /proc/<pid>/comm can be set by any same-uid process
/// (PR_SET_NAME); the executable cannot be faked without running the real
/// binary.
///
/// KWin is installed with the cap_sys_nice file capability, which makes it
/// non-dumpable: its /proc/<pid>/exe cannot be read even by the same user.
/// For such a peer the check is instead: process name is an allowed
/// compositor *and* it holds cap_sys_nice in its permitted set. An ordinary
/// same-uid process can rename itself but cannot acquire that capability
/// without executing a binary that grants it.
pub fn verify_compositor_peer(pid: i32, allowed: &[&str]) -> Result<String, String> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let exe = match std::fs::read_link(format!("/proc/{pid}/exe")) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => return verify_privileged_peer(pid, allowed),
        Err(e) => return Err(format!("cannot inspect peer process {pid}: {e}")),
    };
    let shown = exe.to_string_lossy().to_string();
    let path = shown.trim_end_matches(" (deleted)");
    let name = std::path::Path::new(path).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    if !allowed.contains(&name.as_str()) {
        return Err(format!("only the compositor ({}) may attach; peer runs {path}", allowed.join(" or ")));
    }
    // Stat through /proc so a binary replaced by an upgrade still resolves.
    let md = std::fs::metadata(format!("/proc/{pid}/exe")).map_err(|e| format!("cannot stat peer executable: {e}"))?;
    if md.uid() != 0 || md.permissions().mode() & 0o022 != 0 {
        return Err(format!("peer executable {path} is not a root-owned system binary"));
    }
    Ok(path.to_string())
}

const CAP_SYS_NICE: u32 = 23;

fn verify_privileged_peer(pid: i32, allowed: &[&str]) -> Result<String, String> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).map_err(|e| format!("cannot inspect peer process {pid}: {e}"))?;
    let field = |k: &str| status.lines().find_map(|l| l.strip_prefix(k).map(|v| v.trim().to_string()));
    let name = field("Name:").unwrap_or_default();
    if !allowed.contains(&name.as_str()) {
        return Err(format!("only the compositor ({}) may attach; peer is '{name}'", allowed.join(" or ")));
    }
    let prm = field("CapPrm:").and_then(|v| u64::from_str_radix(&v, 16).ok()).unwrap_or(0);
    if prm & (1 << CAP_SYS_NICE) == 0 {
        return Err(format!("peer '{name}' is not the system compositor (its executable is unreadable and it holds no compositor capability)"));
    }
    Ok(format!("{name} (privileged, cap_sys_nice)"))
}

pub struct BridgeAdapter {
    /// "gnome" or "kwin".
    pub kind: String,
    /// Process names allowed to attach (compositor processes).
    allowed_peers: Vec<&'static str>,
    session_type: String,
    compositor: String,
    /// What to tell the user while the integration is not connected.
    hint: String,
    attached: Mutex<Option<Attached>>,
    /// Why the most recent attach attempt was refused (cleared on success),
    /// so status/diagnose can say "update the integration" instead of the
    /// generic "enable it" hint.
    rejected: Mutex<Option<String>>,
    pending: Mutex<HashMap<u64, Sender<AResult<Value>>>>,
    next: AtomicU64,
    events: Mutex<Option<Sender<AdapterEvent>>>,
    own: Mutex<Vec<String>>,
    timeout: Duration,
    hotkeys: Mutex<Option<Arc<dyn HotkeyService>>>,
}

#[derive(Deserialize)]
struct WireError {
    #[serde(default)]
    kind: String,
    #[serde(default)]
    detail: String,
}

impl BridgeAdapter {
    pub fn gnome() -> BridgeAdapter {
        BridgeAdapter::new(
            "gnome",
            vec!["gnome-shell"],
            "GNOME Shell",
            "The Fractile GNOME Shell extension is not connected. Enable it in the Extensions app or run: gnome-extensions enable fractile@fractile.io (on Wayland, log out and in once after installing).",
        )
    }

    pub fn kwin() -> BridgeAdapter {
        BridgeAdapter::new(
            "kwin",
            vec!["kwin_wayland", "kwin_x11"],
            "KWin",
            "The Fractile KWin script is not connected. Enable \"Fractile\" in System Settings > Window Management > KWin Scripts.",
        )
    }

    fn new(kind: &str, peers: Vec<&'static str>, compositor: &str, hint: &str) -> BridgeAdapter {
        BridgeAdapter {
            kind: kind.into(),
            allowed_peers: peers,
            session_type: "wayland".into(),
            compositor: compositor.into(),
            hint: hint.into(),
            attached: Mutex::new(None),
            rejected: Mutex::new(None),
            pending: Mutex::new(HashMap::new()),
            next: AtomicU64::new(1),
            events: Mutex::new(None),
            own: Mutex::new(vec![]),
            timeout: Duration::from_secs(5),
            hotkeys: Mutex::new(None),
        }
    }

    /// Route global shortcuts to `svc` instead of the integration.
    pub fn set_hotkey_service(&self, svc: Arc<dyn HotkeyService>) {
        *self.hotkeys.lock().unwrap() = Some(svc);
    }

    pub fn allowed_peers(&self) -> &[&'static str] {
        &self.allowed_peers
    }

    /// Emit an event that did not come from the integration (KGlobalAccel).
    pub fn inject_event(&self, ev: AdapterEvent) {
        self.emit(ev);
    }


    pub fn is_attached(&self) -> bool {
        self.attached.lock().unwrap().is_some()
    }

    pub fn status(&self) -> Value {
        match &*self.attached.lock().unwrap() {
            Some(a) => json!({"kind": self.kind, "connected": true, "peer_pid": a.peer_pid, "info": a.info}),
            None => match &*self.rejected.lock().unwrap() {
                Some(r) => json!({"kind": self.kind, "connected": false, "rejected": r, "hint": format!("The {} integration tried to connect but was refused: {r}", self.compositor)}),
                None => json!({"kind": self.kind, "connected": false, "hint": self.hint}),
            },
        }
    }

    /// Validate and accept an integration connection. A refusal is kept for
    /// status/diagnose until the next successful attach.
    pub fn attach(&self, conn_id: u64, peer_pid: i32, transport: Transport, p: &Value) -> Result<(), String> {
        let r = self.attach_checked(conn_id, peer_pid, transport, p);
        *self.rejected.lock().unwrap() = r.as_ref().err().cloned();
        r
    }

    fn attach_checked(&self, conn_id: u64, peer_pid: i32, transport: Transport, p: &Value) -> Result<(), String> {
        let kind = p.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        if kind != self.kind {
            return Err(format!("this session expects a '{}' integration, not '{kind}'", self.kind));
        }
        let api = p.get("api").and_then(|v| v.as_u64()).unwrap_or(0);
        if api != ADAPTER_API_VERSION as u64 {
            let fix = if self.kind == "gnome" {
                "reinstall the Fractile package so the shipped extension matches, then log out and back in"
            } else {
                "reinstall the Fractile package so the shipped KWin script matches, then turn the script off and on in System Settings > Window Management > KWin Scripts"
            };
            return Err(format!("version mismatch: the integration speaks adapter API {api}, this Fractile needs {ADAPTER_API_VERSION}. To fix: {fix}"));
        }
        verify_compositor_peer(peer_pid, &self.allowed_peers)?;
        let mut ops = BTreeMap::new();
        for op in Operation::ALL {
            ops.insert(op, SupportLevel::Unsupported("not offered by this integration version".into()));
        }
        if let Some(m) = p.get("operations").and_then(|v| v.as_object()) {
            for (k, v) in m {
                if let (Ok(op), Ok(level)) = (serde_json::from_value::<Operation>(json!(k)), serde_json::from_value::<SupportLevel>(v.clone())) {
                    ops.insert(op, level);
                }
            }
        }
        let mut notes: Vec<String> = p.get("notes").and_then(|v| serde_json::from_value(v.clone()).ok()).unwrap_or_default();
        notes.push(format!("Window control runs inside {} via the Fractile integration; same-user processes that can use Fractile's socket can therefore move windows while it is enabled.", self.compositor));
        let caps = Capabilities {
            adapter: self.kind.clone(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: p.get("session_type").and_then(|v| v.as_str()).unwrap_or(&self.session_type).to_string(),
            compositor: self.compositor.clone(),
            compositor_version: p.get("compositor_version").and_then(|v| v.as_str()).map(String::from),
            operations: ops,
            tiling_manager: p.get("tiling_manager").and_then(|v| v.as_str()).map(String::from),
            notes,
        };
        let mut a = self.attached.lock().unwrap();
        if let Some(old) = a.as_ref() {
            // A compositor restart reconnects from a new process; the old
            // connection is dead. A second live integration is refused.
            if std::path::Path::new(&format!("/proc/{}", old.peer_pid)).exists() && old.peer_pid != peer_pid {
                return Err("another integration is already attached".into());
            }
        }
        *a = Some(Attached { conn_id, peer_pid, transport, caps, info: p.clone() });
        Ok(())
    }

    /// Connection closed. Fails all calls in flight.
    pub fn detach(&self, conn_id: u64) -> bool {
        let mut a = self.attached.lock().unwrap();
        if a.as_ref().map(|x| x.conn_id) != Some(conn_id) {
            return false;
        }
        *a = None;
        drop(a);
        for (_, tx) in self.pending.lock().unwrap().drain() {
            let _ = tx.send(Err(AdapterError::Disconnected("integration disconnected".into())));
        }
        self.emit(AdapterEvent::Disconnected { reason: format!("{} integration disconnected", self.compositor) });
        true
    }

    pub fn is_bridge_conn(&self, conn_id: u64) -> bool {
        self.attached.lock().unwrap().as_ref().map(|x| x.conn_id) == Some(conn_id)
    }

    pub fn deliver_result(&self, p: &Value) {
        let Some(call) = p.get("call").and_then(|v| v.as_u64()) else { return };
        let Some(tx) = self.pending.lock().unwrap().remove(&call) else { return };
        let res = if p.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
            Ok(p.get("result").cloned().unwrap_or(Value::Null))
        } else {
            let e: WireError = p.get("error").and_then(|v| serde_json::from_value(v.clone()).ok()).unwrap_or(WireError { kind: "other".into(), detail: "integration error".into() });
            Err(match e.kind.as_str() {
                "no_such_window" => AdapterError::NoSuchWindow(e.detail),
                "conflict" => AdapterError::Conflict(e.detail),
                "unsupported" => AdapterError::Unsupported(e.detail),
                "rejected" => AdapterError::Rejected(e.detail),
                _ => AdapterError::Other(e.detail),
            })
        };
        let _ = tx.send(res);
    }

    pub fn deliver_event(&self, p: &Value) -> Result<(), String> {
        let ev: AdapterEvent = serde_json::from_value(p.clone()).map_err(|e| format!("bad bridge event: {e}"))?;
        self.emit(ev);
        Ok(())
    }

    fn emit(&self, ev: AdapterEvent) {
        if let Some(tx) = self.events.lock().unwrap().as_ref() {
            let _ = tx.send(ev);
        }
    }

    fn call(&self, op: &str, args: Value) -> AResult<Value> {
        self.call_t(op, args, self.timeout)
    }

    fn call_t(&self, op: &str, args: Value, timeout: Duration) -> AResult<Value> {
        let transport = match &*self.attached.lock().unwrap() {
            Some(a) => a.transport.clone(),
            None => return Err(AdapterError::Unsupported(self.hint.clone())),
        };
        let id = self.next.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = channel();
        self.pending.lock().unwrap().insert(id, tx);
        let data = json!({"call": id, "op": op, "args": args});
        let sent = match transport {
            Transport::Socket(writer) => {
                let mut line = json!({"event": "bridge_call", "data": data}).to_string();
                line.push('\n');
                let mut w = writer.lock().unwrap();
                let _ = w.set_write_timeout(Some(Duration::from_secs(2)));
                w.write_all(line.as_bytes())
            }
            Transport::Queue(q) => q.try_send(data).map_err(|e| std::io::Error::new(std::io::ErrorKind::BrokenPipe, e.to_string())),
        };
        if let Err(e) = sent {
            self.pending.lock().unwrap().remove(&id);
            return Err(AdapterError::Disconnected(e.to_string()));
        }
        match rx.recv_timeout(timeout) {
            Ok(r) => r,
            Err(_) => {
                self.pending.lock().unwrap().remove(&id);
                Err(AdapterError::Timeout(format!("{} integration did not answer '{op}'", self.compositor)))
            }
        }
    }

    fn parse<T: serde::de::DeserializeOwned>(v: Value, what: &str) -> AResult<T> {
        serde_json::from_value(v).map_err(|e| AdapterError::Other(format!("integration sent a malformed {what}: {e}")))
    }
}

impl CompositorAdapter for BridgeAdapter {
    fn capabilities(&self) -> Capabilities {
        if let Some(a) = &*self.attached.lock().unwrap() {
            let mut caps = a.caps.clone();
            // Shortcuts registered by the daemon itself (KGlobalAccel on
            // KDE) do not go through the integration.
            if self.hotkeys.lock().unwrap().is_some() {
                caps.operations.insert(Operation::RegisterHotkey, SupportLevel::Full);
            }
            return caps;
        }
        let mut ops = BTreeMap::new();
        for op in Operation::ALL {
            ops.insert(op, SupportLevel::Unsupported(self.hint.clone()));
        }
        Capabilities {
            adapter: self.kind.clone(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: self.session_type.clone(),
            compositor: self.compositor.clone(),
            compositor_version: None,
            operations: ops,
            tiling_manager: None,
            notes: vec![self.hint.clone()],
        }
    }

    fn list_windows(&self) -> AResult<Vec<WindowInfo>> {
        let mut ws: Vec<WindowInfo> = Self::parse(self.call("list_windows", json!({}))?, "window list")?;
        let own = self.own.lock().unwrap().clone();
        let own_pids: Vec<u32> = own.iter().filter_map(|s| s.strip_prefix("pid:")).filter_map(|p| p.parse().ok()).collect();
        for w in &mut ws {
            if own.contains(&w.id) || w.pid.map(|p| own_pids.contains(&p)).unwrap_or(false) {
                w.is_own = true;
            }
        }
        Ok(ws)
    }

    fn get_active_window(&self) -> AResult<Option<String>> {
        Self::parse(self.call("get_active_window", json!({}))?, "active window")
    }

    fn get_outputs(&self) -> AResult<Vec<Output>> {
        Self::parse(self.call("get_outputs", json!({}))?, "output list")
    }

    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        Self::parse(self.call("get_workspaces", json!({}))?, "workspace list")
    }

    fn move_resize(&self, id: &str, frame: Rect) -> AResult<PlacementOutcome> {
        if frame.w <= 0 || frame.h <= 0 {
            return Err(AdapterError::Rejected("empty target rectangle".into()));
        }
        // The integration applies the request, waits for the compositor to
        // settle and reports the frame it observed.
        let v = self.call_t("move_resize", json!({"id": id, "rect": frame}), Duration::from_secs(4))?;
        let observed: Option<Rect> = v.get("observed").and_then(|o| serde_json::from_value(o.clone()).ok());
        let attempts = v.get("attempts").and_then(|a| a.as_u64()).unwrap_or(1) as u32;
        let tol = v.get("tolerance").and_then(|a| a.as_i64()).unwrap_or(2) as i32;
        let err = observed.map(|o| o.max_edge_delta(&frame));
        Ok(PlacementOutcome {
            window_id: id.into(),
            requested: frame,
            observed,
            max_error_px: err,
            within_tolerance: err.map(|e| e <= tol).unwrap_or(false),
            attempts,
            note: v.get("note").and_then(|n| n.as_str()).map(String::from),
        })
    }

    fn set_state(&self, id: &str, state: &WindowState) -> AResult<()> {
        self.call("set_state", json!({"id": id, "state": state})).map(|_| ())
    }

    fn move_to_workspace(&self, id: &str, workspace: u32) -> AResult<()> {
        self.call("move_to_workspace", json!({"id": id, "workspace": workspace})).map(|_| ())
    }

    fn activate(&self, id: &str, timestamp: Option<u32>) -> AResult<()> {
        self.call("activate", json!({"id": id, "timestamp": timestamp})).map(|_| ())
    }

    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        let (tx, rx) = channel();
        let mut e = self.events.lock().unwrap();
        if e.is_some() {
            return Err(AdapterError::Other("events already subscribed".into()));
        }
        *e = Some(tx);
        Ok(rx)
    }

    fn register_hotkey(&self, binding_id: &str, accelerator: &str) -> AResult<()> {
        let a = fractile_core::accel::Accelerator::parse(accelerator).map_err(AdapterError::Rejected)?;
        if let Some(h) = self.hotkeys.lock().unwrap().clone() {
            return h.register(binding_id, &a);
        }
        self.call("grab_accelerator", json!({"binding_id": binding_id, "accelerator": a.to_gtk()})).map(|_| ())
    }

    fn unregister_hotkey(&self, binding_id: &str) -> AResult<()> {
        if let Some(h) = self.hotkeys.lock().unwrap().clone() {
            return h.unregister(binding_id);
        }
        if !self.is_attached() {
            return Ok(());
        }
        self.call("ungrab_accelerator", json!({"binding_id": binding_id})).map(|_| ())
    }

    fn set_own_windows(&self, ids: &[String]) {
        *self.own.lock().unwrap() = ids.to_vec();
    }

    fn show_overlay(&self, rect: Rect, label: &str, below_pids: &[u32]) -> AResult<()> {
        self.call_t("show_overlay", json!({"rect": rect, "label": label, "below_pids": below_pids}), Duration::from_secs(1)).map(|_| ())
    }

    fn hide_overlay(&self) -> AResult<()> {
        if !self.is_attached() {
            return Ok(());
        }
        self.call_t("hide_overlay", json!({}), Duration::from_secs(1)).map(|_| ())
    }

    fn session_locked(&self) -> Option<bool> {
        self.call("session_locked", json!({})).ok().and_then(|v| v.as_bool())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q() -> Transport {
        Transport::Queue(async_channel::unbounded().0)
    }

    /// SRS T19: after a shell/KWin or Fractile upgrade the integration may
    /// speak another adapter API. It must be refused (fail safe), and the
    /// refusal must reach status with a concrete fix, not the generic hint.
    #[test]
    fn adapter_api_mismatch_is_refused_and_reported_with_a_fix() {
        for b in [BridgeAdapter::gnome(), BridgeAdapter::kwin()] {
            let me = std::process::id() as i32;
            let e = b.attach(1, me, q(), &json!({"kind": b.kind, "api": ADAPTER_API_VERSION + 1})).unwrap_err();
            assert!(e.contains("version mismatch") && e.contains("To fix:"), "{e}");
            assert!(!b.is_attached());
            let st = b.status();
            assert_eq!(st["connected"], false);
            assert_eq!(st["rejected"].as_str(), Some(e.as_str()));
            assert!(st["hint"].as_str().unwrap().contains("refused"));
            // A missing api field is treated as a mismatch too.
            assert!(b.attach(2, me, q(), &json!({"kind": b.kind})).unwrap_err().contains("version mismatch"));
            // The wrong kind is refused before anything else.
            assert!(b.attach(3, me, q(), &json!({"kind": "other", "api": ADAPTER_API_VERSION})).is_err());
            // A correct-version attach from a process that is not the
            // compositor (this test binary) is still refused by peer checks.
            let e = b.attach(4, me, q(), &json!({"kind": b.kind, "api": ADAPTER_API_VERSION})).unwrap_err();
            assert!(!e.contains("version mismatch"), "{e}");
            assert!(!b.is_attached());
        }
    }
}

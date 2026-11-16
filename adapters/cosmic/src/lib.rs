//! COSMIC adapter.
//!
//! COSMIC (cosmic-comp) gives ordinary clients a list of toplevels with their
//! title, app id, state, outputs, workspaces and (read-only) geometry
//! (`zcosmic_toplevel_info_v1` over `ext_foreign_toplevel_list_v1`), and lets
//! them activate, maximize, minimize, fullscreen, stick and move toplevels to
//! another workspace (`zcosmic_toplevel_manager_v1`). It has no request that
//! sets a toplevel's position or size, so grid placement is unsupported here;
//! everything else this adapter offers uses those protocols as COSMIC
//! documents them. Workspaces come from `ext_workspace_manager_v1`, outputs
//! from `wl_output` and `zxdg_output_manager_v1`.
//!
//! One thread dispatches the Wayland connection. Requests are sent from the
//! caller's thread (wayland-client proxies are thread safe) and their effect
//! is read back from the state that thread keeps.

use cosmic_protocols::toplevel_info::v1::client::{zcosmic_toplevel_handle_v1 as cth, zcosmic_toplevel_info_v1 as cti};
use cosmic_protocols::toplevel_management::v1::client::zcosmic_toplevel_manager_v1 as ctm;
use fractile_core::adapter::{AResult, AdapterError, AdapterEvent, Capabilities, CompositorAdapter, Operation, PlacementOutcome, SupportLevel, Workspace, ADAPTER_API_VERSION};
use fractile_core::geometry::Rect;
use fractile_core::output::{Output, Transform};
use fractile_core::window::{WindowInfo, WindowState, WindowType};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use wayland_client::backend::ObjectId;
use wayland_client::protocol::{wl_output, wl_registry, wl_seat};
use wayland_client::{event_created_child, Connection, Dispatch, EventQueue, Proxy, QueueHandle, WEnum};
use wayland_protocols::ext::foreign_toplevel_list::v1::client::{ext_foreign_toplevel_handle_v1 as efh, ext_foreign_toplevel_list_v1 as efl};
use wayland_protocols::ext::workspace::v1::client::{ext_workspace_group_handle_v1 as ewg, ext_workspace_handle_v1 as ewh, ext_workspace_manager_v1 as ewm};
use wayland_protocols::xdg::xdg_output::zv1::client::{zxdg_output_manager_v1 as xom, zxdg_output_v1 as xo};

/// Why grid placement is not offered (shown by status, diagnose, the panel).
pub const NO_GEOMETRY: &str = "COSMIC has no client API to move or resize other applications' windows (its toplevel management protocol covers activate, close, minimize, maximize, fullscreen, sticky and workspace moves only), so Fractile cannot place windows on COSMIC.";

const MAXIMIZED: u32 = 0;
const MINIMIZED: u32 = 1;
const ACTIVATED: u32 = 2;
const FULLSCREEN: u32 = 3;
const STICKY: u32 = 4;

#[derive(Default)]
struct Top {
    ext: Option<efh::ExtForeignToplevelHandleV1>,
    cosmic: Option<cth::ZcosmicToplevelHandleV1>,
    identifier: String,
    title: String,
    app_id: Option<String>,
    states: Vec<u32>,
    /// Geometry relative to each output it is on (output object id).
    geometry: Vec<(ObjectId, Rect)>,
    outputs: Vec<ObjectId>,
    workspaces: Vec<ObjectId>,
    /// The first `done` of the foreign handle has arrived.
    announced: bool,
    /// Activation order, for a stacking estimate (COSMIC reports none).
    last_active: u64,
}

#[derive(Default, Clone)]
struct Out {
    wl: Option<wl_output::WlOutput>,
    global: u32,
    name: String,
    description: String,
    make: String,
    model: String,
    scale: i32,
    mode: (i32, i32),
    transform: i32,
    physical_mm: (i32, i32),
    logical_pos: Option<(i32, i32)>,
    logical_size: Option<(i32, i32)>,
}

#[derive(Default)]
struct Group {
    handle: Option<ewg::ExtWorkspaceGroupHandleV1>,
    outputs: Vec<ObjectId>,
    workspaces: Vec<ObjectId>,
}

#[derive(Default)]
struct Ws {
    handle: Option<ewh::ExtWorkspaceHandleV1>,
    name: String,
    coordinates: Vec<u32>,
    active: bool,
}

#[derive(Default)]
struct Shared {
    tops: Vec<Top>,
    outputs: Vec<Out>,
    groups: Vec<Group>,
    workspaces: HashMap<ObjectId, Ws>,
    seat: Option<wl_seat::WlSeat>,
    info: Option<cti::ZcosmicToplevelInfoV1>,
    info_version: u32,
    manager: Option<ctm::ZcosmicToplevelManagerV1>,
    manager_version: u32,
    manager_caps: Vec<u32>,
    xdg_mgr: Option<xom::ZxdgOutputManagerV1>,
    list: bool,
    workspace_mgr: bool,
    activation_seq: u64,
    own: HashSet<String>,
    events: Option<Sender<AdapterEvent>>,
}

impl Shared {
    fn send(&self, ev: AdapterEvent) {
        if let Some(tx) = &self.events {
            let _ = tx.send(ev);
        }
    }
    fn top_by_ext(&mut self, id: &ObjectId) -> Option<&mut Top> {
        self.tops.iter_mut().find(|t| t.ext.as_ref().map(|h| &h.id() == id).unwrap_or(false))
    }
    fn top_by_cosmic(&mut self, id: &ObjectId) -> Option<&mut Top> {
        self.tops.iter_mut().find(|t| t.cosmic.as_ref().map(|h| &h.id() == id).unwrap_or(false))
    }
    fn output(&self, id: &ObjectId) -> Option<&Out> {
        self.outputs.iter().find(|o| o.wl.as_ref().map(|w| &w.id() == id).unwrap_or(false))
    }
    /// The workspaces of the group an output belongs to, in COSMIC's order
    /// (by coordinates, then as announced).
    fn group_workspaces(&self, output: Option<&ObjectId>) -> Vec<ObjectId> {
        let g = output
            .and_then(|o| self.groups.iter().find(|g| g.outputs.contains(o)))
            .or_else(|| self.primary_output().and_then(|p| self.groups.iter().find(|g| g.outputs.contains(&p))))
            .or_else(|| self.groups.first());
        let mut v: Vec<(Vec<u32>, usize, ObjectId)> = g
            .map(|g| g.workspaces.iter().enumerate().map(|(k, w)| (self.workspaces.get(w).map(|x| x.coordinates.clone()).unwrap_or_default(), k, w.clone())).collect())
            .unwrap_or_default();
        v.sort_by(|a, b| (&a.0, a.1).cmp(&(&b.0, b.1)));
        v.into_iter().map(|(_, _, w)| w).collect()
    }
    fn primary_output(&self) -> Option<ObjectId> {
        let outs = self.public_outputs();
        outs.iter().find(|o| o.primary).and_then(|p| self.outputs.iter().find(|o| o.name == p.name)).and_then(|o| o.wl.as_ref().map(|w| w.id()))
    }
    fn public_outputs(&self) -> Vec<Output> {
        let mut v: Vec<Output> = self
            .outputs
            .iter()
            .filter(|o| o.logical_size.map(|(w, h)| w > 0 && h > 0).unwrap_or(o.mode.0 > 0))
            .map(|o| {
                let scale = if o.scale > 0 { o.scale as f64 } else { 1.0 };
                let (w, h) = o.logical_size.unwrap_or(((o.mode.0 as f64 / scale) as i32, (o.mode.1 as f64 / scale) as i32));
                let (x, y) = o.logical_pos.unwrap_or((0, 0));
                // Fractional scale: the logical size against the mode.
                let scale = if o.mode.0 > 0 && w > 0 { o.mode.0 as f64 / w as f64 } else { scale };
                let rect = Rect::new(x, y, w, h);
                let known = |s: &str| !s.is_empty() && s != "Unknown" && s != "unknown";
                let from_edid = known(&o.make) && known(&o.model) && !o.description.is_empty();
                let stable_id = if from_edid {
                    let h = Sha256::digest(format!("{}|{}|{}", o.make, o.model, o.description).as_bytes());
                    format!("edid-{}", &hex::encode(h)[..10])
                } else {
                    format!("conn-{}-{}x{}", o.name, w, h)
                };
                let desc = [o.make.as_str(), o.model.as_str()].iter().filter(|s| known(s)).cloned().collect::<Vec<_>>().join(" ");
                Output {
                    stable_id,
                    stable_id_from_edid: from_edid,
                    name: o.name.clone(),
                    description: if desc.is_empty() { None } else { Some(desc) },
                    rect,
                    // COSMIC reports no panel or dock areas to clients.
                    work_area: rect,
                    scale,
                    transform: transform_of(o.transform),
                    primary: false,
                    physical_mm: if o.physical_mm.0 > 0 { Some((o.physical_mm.0 as u32, o.physical_mm.1 as u32)) } else { None },
                }
            })
            .collect();
        // COSMIC names no primary output: the leftmost (then topmost).
        if let Some(p) = v.iter_mut().min_by_key(|o| (o.rect.x, o.rect.y)) {
            p.primary = true;
        }
        v
    }
}

fn transform_of(t: i32) -> Transform {
    match t {
        1 => Transform::Rotate90,
        2 => Transform::Rotate180,
        3 => Transform::Rotate270,
        4 => Transform::Flipped,
        5 => Transform::Flipped90,
        6 => Transform::Flipped180,
        7 => Transform::Flipped270,
        _ => Transform::Normal,
    }
}

fn states_of(raw: &[u8]) -> Vec<u32> {
    raw.chunks_exact(4).map(|c| u32::from_ne_bytes([c[0], c[1], c[2], c[3]])).collect()
}

fn window_id(identifier: &str) -> String {
    format!("cosmic:{identifier}")
}

/// Fractile's own windows (panel, settings, dialogs): Qt sets the app id
/// from the desktop file name.
fn own_app(app_id: Option<&str>) -> bool {
    app_id.map(|a| a == "io.fractile.Fractile" || a.starts_with("io.fractile.") || a == "fractile-ui").unwrap_or(false)
}

struct App {
    shared: Arc<Mutex<Shared>>,
}

pub struct CosmicAdapter {
    conn: Connection,
    shared: Arc<Mutex<Shared>>,
    stop: Arc<AtomicBool>,
    subscribed: Mutex<bool>,
    version: String,
}

impl CosmicAdapter {
    /// Connect to the COSMIC compositor named by WAYLAND_DISPLAY.
    pub fn connect() -> Result<CosmicAdapter, String> {
        let conn = Connection::connect_to_env().map_err(|e| format!("cannot connect to the Wayland display: {e}"))?;
        let shared = Arc::new(Mutex::new(Shared::default()));
        let mut queue: EventQueue<App> = conn.new_event_queue();
        let qh = queue.handle();
        let _registry = conn.display().get_registry(&qh, ());
        let mut app = App { shared: shared.clone() };
        // Globals, then the objects they announce (toplevels, outputs,
        // workspaces), then those objects' details.
        for _ in 0..4 {
            queue.roundtrip(&mut app).map_err(|e| format!("Wayland roundtrip: {e}"))?;
        }
        {
            let s = shared.lock().unwrap();
            if s.info.is_none() || !s.list {
                return Err("the compositor does not offer zcosmic_toplevel_info_v1 and ext_foreign_toplevel_list_v1 to Fractile".into());
            }
            if s.manager.is_none() {
                return Err("the compositor does not offer zcosmic_toplevel_manager_v1 to Fractile".into());
            }
        }
        let version = std::process::Command::new("cosmic-comp")
            .arg("--version")
            .output()
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_default();
        let stop = Arc::new(AtomicBool::new(false));
        let (sh, st) = (shared.clone(), stop.clone());
        std::thread::Builder::new()
            .name("cosmic-wayland".into())
            .spawn(move || {
                let mut app = App { shared: sh.clone() };
                let reason = loop {
                    if let Err(e) = queue.blocking_dispatch(&mut app) {
                        break format!("the COSMIC Wayland connection ended: {e}");
                    }
                    if st.load(Ordering::SeqCst) {
                        return;
                    }
                };
                if !st.load(Ordering::SeqCst) {
                    sh.lock().unwrap().send(AdapterEvent::DisplayClosed { reason });
                }
            })
            .map_err(|e| e.to_string())?;
        Ok(CosmicAdapter { conn, shared, stop, subscribed: Mutex::new(false), version })
    }

    pub fn version(&self) -> &str {
        &self.version
    }

    fn flush(&self) -> AResult<()> {
        self.conn.flush().map_err(|e| AdapterError::Disconnected(format!("COSMIC Wayland connection: {e}")))
    }

    fn handle(&self, id: &str) -> AResult<cth::ZcosmicToplevelHandleV1> {
        let ident = id.strip_prefix("cosmic:").ok_or_else(|| AdapterError::NoSuchWindow(id.into()))?;
        let s = self.shared.lock().unwrap();
        s.tops.iter().find(|t| t.identifier == ident).and_then(|t| t.cosmic.clone()).ok_or_else(|| AdapterError::NoSuchWindow(id.into()))
    }

    /// Wait (bounded) until the window satisfies `ok`.
    fn wait_for(&self, id: &str, timeout: Duration, ok: impl Fn(&WindowInfo) -> bool) -> bool {
        let end = Instant::now() + timeout;
        while Instant::now() < end {
            if self.get_window(id).map(|w| ok(&w)).unwrap_or(false) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(30));
        }
        false
    }

    fn infos(&self) -> Vec<WindowInfo> {
        let s = self.shared.lock().unwrap();
        let outs = s.public_outputs();
        let mut order: Vec<(u64, usize)> = s.tops.iter().enumerate().map(|(k, t)| (t.last_active, k)).collect();
        order.sort();
        let mut stacking = vec![0u32; s.tops.len()];
        for (pos, (_, k)) in order.into_iter().enumerate() {
            stacking[k] = pos as u32;
        }
        s.tops
            .iter()
            .enumerate()
            .filter(|(_, t)| t.announced && t.cosmic.is_some() && !t.identifier.is_empty())
            .map(|(k, t)| {
                let id = window_id(&t.identifier);
                let first_out = t.outputs.first().cloned().or_else(|| t.geometry.first().map(|g| g.0.clone()));
                let frame = t
                    .geometry
                    .iter()
                    .find(|(o, _)| Some(o) == first_out.as_ref())
                    .or(t.geometry.first())
                    .map(|(o, r)| {
                        let (ox, oy) = s.output(o).and_then(|o| o.logical_pos).unwrap_or((0, 0));
                        Rect::new(ox + r.x, oy + r.y, r.w, r.h)
                    })
                    .unwrap_or_default();
                let output = first_out.as_ref().and_then(|o| s.output(o)).and_then(|o| outs.iter().find(|p| p.name == o.name)).map(|p| p.stable_id.clone());
                let wss = s.group_workspaces(first_out.as_ref());
                let workspace = if t.states.contains(&STICKY) { None } else { t.workspaces.iter().find_map(|w| wss.iter().position(|x| x == w)).map(|p| p as u32) };
                WindowInfo {
                    id: id.clone(),
                    title: t.title.clone(),
                    app_id: t.app_id.clone(),
                    wm_class: t.app_id.clone(),
                    window_type: WindowType::Normal,
                    frame,
                    state: WindowState {
                        maximized: t.states.contains(&MAXIMIZED),
                        minimized: t.states.contains(&MINIMIZED),
                        fullscreen: t.states.contains(&FULLSCREEN),
                        sticky: t.states.contains(&STICKY),
                        ..Default::default()
                    },
                    workspace,
                    output_id: output,
                    stacking: Some(stacking[k]),
                    focused: t.states.contains(&ACTIVATED),
                    is_own: own_app(t.app_id.as_deref()) || s.own.contains(&id),
                    ..Default::default()
                }
            })
            .collect()
    }
}

impl CompositorAdapter for CosmicAdapter {
    fn capabilities(&self) -> Capabilities {
        use Operation::*;
        use SupportLevel::*;
        let s = self.shared.lock().unwrap();
        let mut ops = BTreeMap::new();
        ops.insert(ListWindows, Full);
        ops.insert(GetActiveWindow, Full);
        ops.insert(GetOutputs, Partial("the work area is the whole monitor: COSMIC does not tell clients where its panel and dock are".into()));
        ops.insert(
            GetWorkspaces,
            if s.workspace_mgr { Partial("the workspaces of the leftmost monitor (COSMIC keeps a set per monitor by default)".into()) } else { Unsupported("the compositor offers no ext_workspace_manager_v1".into()) },
        );
        ops.insert(GetGeometry, Partial("read only: COSMIC reports each window's position and size but offers no way to set them".into()));
        ops.insert(MoveResize, Unsupported(NO_GEOMETRY.into()));
        ops.insert(
            SetState,
            Partial(if s.manager_version >= 3 { "maximize, minimize, fullscreen and sticky; COSMIC has no keep-above request" } else { "maximize, minimize and fullscreen; this cosmic-comp has no sticky or keep-above request" }.into()),
        );
        ops.insert(MoveToOutput, Unsupported(NO_GEOMETRY.into()));
        ops.insert(
            MoveToWorkspace,
            if s.manager_version >= 4 && s.workspace_mgr { Full } else { Unsupported("this cosmic-comp has no move_to_ext_workspace request (zcosmic_toplevel_manager_v1 version 4)".into()) },
        );
        ops.insert(Activate, Full);
        ops.insert(SubscribeEvents, Full);
        ops.insert(RegisterHotkey, Unsupported("COSMIC offers applications no way to register global shortcuts".into()));
        ops.insert(ShowOverlay, Unsupported("there is nothing to preview: windows cannot be placed on COSMIC".into()));
        ops.insert(GetPermissions, Full);
        Capabilities {
            adapter: "cosmic".into(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: "wayland".into(),
            compositor: "cosmic-comp".into(),
            compositor_version: if self.version.is_empty() { None } else { Some(self.version.clone()) },
            operations: ops,
            tiling_manager: None,
            notes: vec![
                "PARTIAL: Fractile lists COSMIC's windows, saves sessions (apps, workspaces, maximized/minimized/fullscreen) and restores them by reopening approved apps and putting their windows back on their workspaces and in their state. It cannot set position or size (BLOCKED BY PLATFORM: see docs/compatibility/cosmic.md for the upstream proposal), so the grid panel and placement shortcuts do not work.".into(),
                "COSMIC auto-tiling is never modified by Fractile.".into(),
            ],
        }
    }

    fn list_windows(&self) -> AResult<Vec<WindowInfo>> {
        Ok(self.infos())
    }

    fn get_active_window(&self) -> AResult<Option<String>> {
        Ok(self.infos().into_iter().find(|w| w.focused).map(|w| w.id))
    }

    fn get_outputs(&self) -> AResult<Vec<Output>> {
        Ok(self.shared.lock().unwrap().public_outputs())
    }

    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        let s = self.shared.lock().unwrap();
        if !s.workspace_mgr {
            return Err(AdapterError::Unsupported("the compositor offers no ext_workspace_manager_v1".into()));
        }
        let p = s.primary_output();
        Ok(s
            .group_workspaces(p.as_ref())
            .iter()
            .enumerate()
            .map(|(k, w)| {
                let ws = s.workspaces.get(w);
                Workspace { index: k as u32, name: ws.map(|x| x.name.clone()).unwrap_or_default(), active: ws.map(|x| x.active).unwrap_or(false) }
            })
            .collect())
    }

    fn move_resize(&self, _id: &str, _frame: Rect) -> AResult<PlacementOutcome> {
        Err(AdapterError::Unsupported(NO_GEOMETRY.into()))
    }

    fn set_state(&self, id: &str, want: &WindowState) -> AResult<()> {
        let h = self.handle(id)?;
        let w = *want;
        let ver = self.shared.lock().unwrap().manager_version;
        let cur = self.get_window(id)?;
        if w.above && !cur.state.above {
            return Err(AdapterError::Unsupported("COSMIC has no keep-above request".into()));
        }
        if w.sticky != cur.state.sticky && ver < 3 {
            return Err(AdapterError::Unsupported("this cosmic-comp has no sticky request".into()));
        }
        let done = |i: &WindowInfo| i.state.maximized == w.maximized && i.state.minimized == w.minimized && i.state.fullscreen == w.fullscreen && (ver < 3 || i.state.sticky == w.sticky);
        // A request that arrives while cosmic-comp settles a workspace move
        // can be dropped, so what is still missing is sent once more. For a
        // window on a workspace that is not in view cosmic-comp applies the
        // change (the window takes the maximized size at once) but reports
        // the new state only 11-38 s later (VM, llvmpipe); that is not
        // waited for here. A restore checks such states again at its end.
        for wait_ms in [1500u64, 3000] {
            let cur = self.get_window(id)?;
            if done(&cur) {
                return Ok(());
            }
            let (mgr, out) = {
                let s = self.shared.lock().unwrap();
                let out = s.tops.iter().find(|t| t.cosmic.as_ref() == Some(&h)).and_then(|t| t.outputs.first().cloned()).and_then(|o| s.output(&o).and_then(|o| o.wl.clone()));
                (s.manager.clone().ok_or_else(|| AdapterError::Disconnected("no toplevel manager".into()))?, out)
            };
            if w.sticky != cur.state.sticky {
                if w.sticky {
                    mgr.set_sticky(&h);
                } else {
                    mgr.unset_sticky(&h);
                }
            }
            if w.fullscreen != cur.state.fullscreen {
                if w.fullscreen {
                    mgr.set_fullscreen(&h, out.as_ref());
                } else {
                    mgr.unset_fullscreen(&h);
                }
            }
            if w.maximized != cur.state.maximized {
                if w.maximized {
                    mgr.set_maximized(&h);
                } else {
                    mgr.unset_maximized(&h);
                }
            }
            if w.minimized != cur.state.minimized {
                if w.minimized {
                    mgr.set_minimized(&h);
                } else {
                    mgr.unset_minimized(&h);
                }
            }
            self.flush()?;
            if self.wait_for(id, Duration::from_millis(wait_ms), done) {
                return Ok(());
            }
        }
        let now = self.get_window(id).map(|i| i.state).unwrap_or_default();
        let hidden = {
            let s = self.shared.lock().unwrap();
            s.tops.iter().find(|t| t.cosmic.as_ref() == Some(&h)).map(|t| !t.workspaces.is_empty() && !t.workspaces.iter().any(|k| s.workspaces.get(k).map(|x| x.active).unwrap_or(false))).unwrap_or(false)
        };
        let why = if hidden {
            "COSMIC has not confirmed it yet; for a window on a workspace that is not in view it reports state changes late"
        } else {
            "COSMIC did not apply it"
        };
        Err(AdapterError::Rejected(format!("{why} (wanted maximized={} minimized={} fullscreen={} sticky={}, now maximized={} minimized={} fullscreen={} sticky={})", w.maximized, w.minimized, w.fullscreen, w.sticky, now.maximized, now.minimized, now.fullscreen, now.sticky)))
    }

    fn move_to_workspace(&self, id: &str, workspace: u32) -> AResult<()> {
        let h = self.handle(id)?;
        let (mgr, ws, out) = {
            let s = self.shared.lock().unwrap();
            if s.manager_version < 4 || !s.workspace_mgr {
                return Err(AdapterError::Unsupported("this cosmic-comp has no move_to_ext_workspace request".into()));
            }
            let t = s.tops.iter().find(|t| t.cosmic.as_ref() == Some(&h)).ok_or_else(|| AdapterError::NoSuchWindow(id.into()))?;
            let out_id = t.outputs.first().cloned().or_else(|| s.primary_output());
            let wss = s.group_workspaces(out_id.as_ref());
            let wid = wss.get(workspace as usize).ok_or_else(|| AdapterError::Rejected(format!("there is no workspace {} on this monitor ({} exist)", workspace + 1, wss.len())))?;
            let ws = s.workspaces.get(wid).and_then(|w| w.handle.clone()).ok_or_else(|| AdapterError::Rejected("the workspace is gone".into()))?;
            let out = out_id.and_then(|o| s.output(&o).and_then(|o| o.wl.clone())).ok_or_else(|| AdapterError::Rejected("no output for the window".into()))?;
            (s.manager.clone().ok_or_else(|| AdapterError::Disconnected("no toplevel manager".into()))?, ws, out)
        };
        mgr.move_to_ext_workspace(&h, &ws, &out);
        self.flush()?;
        if self.wait_for(id, Duration::from_millis(1500), |i| i.workspace == Some(workspace)) {
            Ok(())
        } else {
            Err(AdapterError::Rejected(format!("COSMIC did not move the window to workspace {}", workspace + 1)))
        }
    }

    fn activate(&self, id: &str, _timestamp: Option<u32>) -> AResult<()> {
        let h = self.handle(id)?;
        let (mgr, seat) = {
            let s = self.shared.lock().unwrap();
            (s.manager.clone().ok_or_else(|| AdapterError::Disconnected("no toplevel manager".into()))?, s.seat.clone().ok_or_else(|| AdapterError::Rejected("no seat".into()))?)
        };
        mgr.activate(&h, &seat);
        self.flush()
    }

    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        let mut sub = self.subscribed.lock().unwrap();
        if *sub {
            return Err(AdapterError::Other("already subscribed".into()));
        }
        *sub = true;
        let (tx, rx) = channel();
        self.shared.lock().unwrap().events = Some(tx);
        Ok(rx)
    }

    fn register_hotkey(&self, _binding_id: &str, _accelerator: &str) -> AResult<()> {
        Err(AdapterError::Unsupported("COSMIC offers applications no way to register global shortcuts".into()))
    }

    fn unregister_hotkey(&self, _binding_id: &str) -> AResult<()> {
        Ok(())
    }

    fn set_own_windows(&self, ids: &[String]) {
        self.shared.lock().unwrap().own = ids.iter().cloned().collect();
    }
}

impl Drop for CosmicAdapter {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

// ---------------------------------------------------------------- dispatch

impl Dispatch<wl_registry::WlRegistry, ()> for App {
    fn event(app: &mut Self, registry: &wl_registry::WlRegistry, event: wl_registry::Event, _: &(), _: &Connection, qh: &QueueHandle<Self>) {
        match event {
            wl_registry::Event::Global { name, interface, version } => {
                let mut s = app.shared.lock().unwrap();
                match interface.as_str() {
                    "wl_output" => {
                        let o = registry.bind::<wl_output::WlOutput, _, _>(name, version.min(4), qh, ());
                        if let Some(m) = &s.xdg_mgr {
                            m.get_xdg_output(&o, qh, o.id());
                        }
                        s.outputs.push(Out { wl: Some(o), global: name, ..Default::default() });
                    }
                    "zxdg_output_manager_v1" => {
                        let m = registry.bind::<xom::ZxdgOutputManagerV1, _, _>(name, version.min(3), qh, ());
                        for o in s.outputs.iter().filter_map(|o| o.wl.clone()) {
                            m.get_xdg_output(&o, qh, o.id());
                        }
                        s.xdg_mgr = Some(m);
                    }
                    "wl_seat" if s.seat.is_none() => {
                        s.seat = Some(registry.bind::<wl_seat::WlSeat, _, _>(name, version.min(1), qh, ()));
                    }
                    "ext_foreign_toplevel_list_v1" => {
                        registry.bind::<efl::ExtForeignToplevelListV1, _, _>(name, 1, qh, ());
                        s.list = true;
                    }
                    "zcosmic_toplevel_info_v1" if version >= 2 => {
                        let v = version.min(3);
                        s.info = Some(registry.bind::<cti::ZcosmicToplevelInfoV1, _, _>(name, v, qh, ()));
                        s.info_version = v;
                        // Toplevels announced before the info global was bound.
                        let info = s.info.clone().unwrap();
                        for t in s.tops.iter_mut().filter(|t| t.cosmic.is_none()) {
                            if let Some(e) = &t.ext {
                                t.cosmic = Some(info.get_cosmic_toplevel(e, qh, ()));
                            }
                        }
                    }
                    "zcosmic_toplevel_manager_v1" => {
                        let v = version.min(4);
                        s.manager = Some(registry.bind::<ctm::ZcosmicToplevelManagerV1, _, _>(name, v, qh, ()));
                        s.manager_version = v;
                    }
                    "ext_workspace_manager_v1" => {
                        registry.bind::<ewm::ExtWorkspaceManagerV1, _, _>(name, 1, qh, ());
                        s.workspace_mgr = true;
                    }
                    _ => {}
                }
            }
            wl_registry::Event::GlobalRemove { name } => {
                let mut s = app.shared.lock().unwrap();
                let before = s.outputs.len();
                s.outputs.retain(|o| o.global != name);
                if s.outputs.len() != before {
                    s.send(AdapterEvent::OutputsChanged);
                }
            }
            _ => {}
        }
    }
}

impl Dispatch<wl_output::WlOutput, ()> for App {
    fn event(app: &mut Self, output: &wl_output::WlOutput, event: wl_output::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        let id = output.id();
        let done = matches!(event, wl_output::Event::Done);
        if let Some(o) = s.outputs.iter_mut().find(|o| o.wl.as_ref().map(|w| w.id() == id).unwrap_or(false)) {
            match event {
                wl_output::Event::Geometry { make, model, transform, physical_width, physical_height, .. } => {
                    o.make = make;
                    o.model = model;
                    o.physical_mm = (physical_width, physical_height);
                    o.transform = match transform {
                        WEnum::Value(t) => t as i32,
                        WEnum::Unknown(t) => t as i32,
                    };
                }
                wl_output::Event::Mode { flags, width, height, .. } => {
                    let current = match flags {
                        WEnum::Value(f) => f.contains(wl_output::Mode::Current),
                        WEnum::Unknown(_) => true,
                    };
                    if current {
                        o.mode = (width, height);
                    }
                }
                wl_output::Event::Scale { factor } => o.scale = factor,
                wl_output::Event::Name { name } => o.name = name,
                wl_output::Event::Description { description } => o.description = description,
                _ => {}
            }
        }
        if done {
            s.send(AdapterEvent::OutputsChanged);
        }
    }
}

impl Dispatch<xo::ZxdgOutputV1, ObjectId> for App {
    fn event(app: &mut Self, _: &xo::ZxdgOutputV1, event: xo::Event, wl: &ObjectId, _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        if let Some(o) = s.outputs.iter_mut().find(|o| o.wl.as_ref().map(|w| &w.id() == wl).unwrap_or(false)) {
            match event {
                xo::Event::LogicalPosition { x, y } => o.logical_pos = Some((x, y)),
                xo::Event::LogicalSize { width, height } => o.logical_size = Some((width, height)),
                xo::Event::Name { name } if o.name.is_empty() => o.name = name,
                _ => {}
            }
        }
    }
}

impl Dispatch<xom::ZxdgOutputManagerV1, ()> for App {
    fn event(_: &mut Self, _: &xom::ZxdgOutputManagerV1, _: xom::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<wl_seat::WlSeat, ()> for App {
    fn event(_: &mut Self, _: &wl_seat::WlSeat, _: wl_seat::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {}
}

impl Dispatch<efl::ExtForeignToplevelListV1, ()> for App {
    fn event(app: &mut Self, _: &efl::ExtForeignToplevelListV1, event: efl::Event, _: &(), _: &Connection, qh: &QueueHandle<Self>) {
        if let efl::Event::Toplevel { toplevel } = event {
            let mut s = app.shared.lock().unwrap();
            let cosmic = s.info.as_ref().map(|i| i.get_cosmic_toplevel(&toplevel, qh, ()));
            s.tops.push(Top { ext: Some(toplevel), cosmic, ..Default::default() });
        }
    }
    event_created_child!(App, efl::ExtForeignToplevelListV1, [efl::EVT_TOPLEVEL_OPCODE => (efh::ExtForeignToplevelHandleV1, ())]);
}

impl Dispatch<efh::ExtForeignToplevelHandleV1, ()> for App {
    fn event(app: &mut Self, handle: &efh::ExtForeignToplevelHandleV1, event: efh::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        let id = handle.id();
        match event {
            efh::Event::Closed => {
                let gone = s.tops.iter().position(|t| t.ext.as_ref().map(|h| h.id() == id).unwrap_or(false));
                if let Some(k) = gone {
                    let t = s.tops.remove(k);
                    if let Some(c) = &t.cosmic {
                        c.destroy();
                    }
                    handle.destroy();
                    if t.announced {
                        s.send(AdapterEvent::WindowClosed { id: window_id(&t.identifier) });
                    }
                }
            }
            efh::Event::Done => {
                let ev = s.top_by_ext(&id).map(|t| {
                    let first = !t.announced;
                    t.announced = true;
                    (first, window_id(&t.identifier))
                });
                match ev {
                    Some((true, w)) => s.send(AdapterEvent::WindowCreated { id: w }),
                    Some((false, w)) => s.send(AdapterEvent::WindowChanged { id: w }),
                    None => {}
                }
            }
            efh::Event::Title { title } => {
                if let Some(t) = s.top_by_ext(&id) {
                    t.title = title;
                }
            }
            efh::Event::AppId { app_id } => {
                if let Some(t) = s.top_by_ext(&id) {
                    t.app_id = Some(app_id).filter(|a| !a.is_empty());
                }
            }
            efh::Event::Identifier { identifier } => {
                if let Some(t) = s.top_by_ext(&id) {
                    t.identifier = identifier;
                }
            }
            _ => {}
        }
    }
}

impl Dispatch<cti::ZcosmicToplevelInfoV1, ()> for App {
    fn event(_: &mut Self, _: &cti::ZcosmicToplevelInfoV1, _: cti::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {}
    // Only sent to version 1 clients (this one binds 2 or later), but a
    // created object must be declared.
    event_created_child!(App, cti::ZcosmicToplevelInfoV1, [cti::EVT_TOPLEVEL_OPCODE => (cth::ZcosmicToplevelHandleV1, ())]);
}

impl Dispatch<cth::ZcosmicToplevelHandleV1, ()> for App {
    fn event(app: &mut Self, handle: &cth::ZcosmicToplevelHandleV1, event: cth::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        let id = handle.id();
        let mut changed: Option<(String, bool)> = None;
        let seq = s.activation_seq + 1;
        let mut bumped = false;
        if let Some(t) = s.top_by_cosmic(&id) {
            match event {
                cth::Event::State { state } => {
                    let new = states_of(&state);
                    let was_active = t.states.contains(&ACTIVATED);
                    let active = new.contains(&ACTIVATED);
                    if active && !was_active {
                        t.last_active = seq;
                        bumped = true;
                    }
                    t.states = new;
                    changed = Some((window_id(&t.identifier), active != was_active));
                }
                cth::Event::Geometry { output, x, y, width, height } => {
                    let o = output.id();
                    t.geometry.retain(|(k, _)| k != &o);
                    t.geometry.push((o, Rect::new(x, y, width, height)));
                    changed = Some((window_id(&t.identifier), false));
                }
                cth::Event::OutputEnter { output } => {
                    let o = output.id();
                    if !t.outputs.contains(&o) {
                        t.outputs.push(o);
                    }
                }
                cth::Event::OutputLeave { output } => {
                    let o = output.id();
                    t.outputs.retain(|k| k != &o);
                    t.geometry.retain(|(k, _)| k != &o);
                }
                cth::Event::ExtWorkspaceEnter { workspace } => {
                    let w = workspace.id();
                    if !t.workspaces.contains(&w) {
                        t.workspaces.push(w);
                    }
                    changed = Some((window_id(&t.identifier), false));
                }
                cth::Event::ExtWorkspaceLeave { workspace } => {
                    let w = workspace.id();
                    t.workspaces.retain(|k| k != &w);
                    changed = Some((window_id(&t.identifier), false));
                }
                _ => {}
            }
            if !t.announced {
                changed = None;
            }
        }
        if bumped {
            s.activation_seq = seq;
        }
        if let Some((w, active_changed)) = changed {
            s.send(AdapterEvent::WindowChanged { id: w.clone() });
            if active_changed {
                let active = s.tops.iter().find(|t| t.states.contains(&ACTIVATED)).map(|t| window_id(&t.identifier));
                s.send(AdapterEvent::ActiveChanged { id: active });
            }
        }
    }
}

impl Dispatch<ctm::ZcosmicToplevelManagerV1, ()> for App {
    fn event(app: &mut Self, _: &ctm::ZcosmicToplevelManagerV1, event: ctm::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        if let ctm::Event::Capabilities { capabilities } = event {
            app.shared.lock().unwrap().manager_caps = states_of(&capabilities);
        }
    }
}

impl Dispatch<ewm::ExtWorkspaceManagerV1, ()> for App {
    fn event(app: &mut Self, _: &ewm::ExtWorkspaceManagerV1, event: ewm::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        match event {
            ewm::Event::WorkspaceGroup { workspace_group } => s.groups.push(Group { handle: Some(workspace_group), ..Default::default() }),
            ewm::Event::Workspace { workspace } => {
                let id = workspace.id();
                s.workspaces.insert(id, Ws { handle: Some(workspace), ..Default::default() });
            }
            ewm::Event::Done => s.send(AdapterEvent::WorkspacesChanged),
            _ => {}
        }
    }
    event_created_child!(App, ewm::ExtWorkspaceManagerV1, [
        ewm::EVT_WORKSPACE_GROUP_OPCODE => (ewg::ExtWorkspaceGroupHandleV1, ()),
        ewm::EVT_WORKSPACE_OPCODE => (ewh::ExtWorkspaceHandleV1, ()),
    ]);
}

impl Dispatch<ewg::ExtWorkspaceGroupHandleV1, ()> for App {
    fn event(app: &mut Self, group: &ewg::ExtWorkspaceGroupHandleV1, event: ewg::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        let gid = group.id();
        if matches!(event, ewg::Event::Removed) {
            s.groups.retain(|g| g.handle.as_ref().map(|h| h.id() != gid).unwrap_or(true));
            return;
        }
        let Some(g) = s.groups.iter_mut().find(|g| g.handle.as_ref().map(|h| h.id() == gid).unwrap_or(false)) else { return };
        match event {
            ewg::Event::OutputEnter { output } => g.outputs.push(output.id()),
            ewg::Event::OutputLeave { output } => {
                let o = output.id();
                g.outputs.retain(|k| k != &o);
            }
            ewg::Event::WorkspaceEnter { workspace } => {
                let w = workspace.id();
                if !g.workspaces.contains(&w) {
                    g.workspaces.push(w);
                }
            }
            ewg::Event::WorkspaceLeave { workspace } => {
                let w = workspace.id();
                g.workspaces.retain(|k| k != &w);
            }
            _ => {}
        }
    }
}

impl Dispatch<ewh::ExtWorkspaceHandleV1, ()> for App {
    fn event(app: &mut Self, ws: &ewh::ExtWorkspaceHandleV1, event: ewh::Event, _: &(), _: &Connection, _: &QueueHandle<Self>) {
        let mut s = app.shared.lock().unwrap();
        let id = ws.id();
        if matches!(event, ewh::Event::Removed) {
            s.workspaces.remove(&id);
            for g in s.groups.iter_mut() {
                g.workspaces.retain(|k| k != &id);
            }
            return;
        }
        let Some(w) = s.workspaces.get_mut(&id) else { return };
        match event {
            ewh::Event::Name { name } => w.name = name,
            ewh::Event::Coordinates { coordinates } => w.coordinates = states_of(&coordinates),
            ewh::Event::State { state } => {
                w.active = match state {
                    WEnum::Value(st) => st.contains(ewh::State::Active),
                    WEnum::Unknown(b) => b & 1 != 0,
                };
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn states_decode_native_endian_u32_array() {
        let mut raw = vec![];
        for v in [MAXIMIZED, ACTIVATED, STICKY] {
            raw.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(states_of(&raw), vec![0, 2, 4]);
        assert!(states_of(&[1, 2, 3]).is_empty());
    }

    #[test]
    fn ids_and_own_windows() {
        assert_eq!(window_id("abc"), "cosmic:abc");
        assert!(own_app(Some("io.fractile.Fractile")));
        assert!(!own_app(Some("org.gnome.TextEditor")));
        assert!(!own_app(None));
    }

    #[test]
    fn outputs_use_logical_size_and_mark_leftmost_primary() {
        let mut s = Shared::default();
        s.outputs.push(Out { name: "Virtual-2".into(), mode: (1920, 1080), scale: 1, logical_pos: Some((1600, 0)), logical_size: Some((1920, 1080)), ..Default::default() });
        s.outputs.push(Out { name: "Virtual-1".into(), mode: (2400, 1800), scale: 2, logical_pos: Some((0, 0)), logical_size: Some((1600, 1200)), ..Default::default() });
        let o = s.public_outputs();
        let v1 = o.iter().find(|o| o.name == "Virtual-1").unwrap();
        assert_eq!(v1.rect, Rect::new(0, 0, 1600, 1200));
        assert!((v1.scale - 1.5).abs() < 1e-9);
        assert!(v1.primary);
        assert_eq!(v1.stable_id, "conn-Virtual-1-1600x1200");
        assert!(!o.iter().find(|o| o.name == "Virtual-2").unwrap().primary);
    }

    #[test]
    fn capabilities_never_offer_geometry() {
        // The reason text is what status, diagnose and the panel show.
        assert!(NO_GEOMETRY.contains("no client API to move or resize"));
    }
}

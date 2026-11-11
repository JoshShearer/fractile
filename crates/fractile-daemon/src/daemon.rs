//! Daemon state and the serialized action executor.

use crate::backend::Environment;
use crate::logging::Logger;
use fractile_core::adapter::{AdapterEvent, Capabilities, CompositorAdapter, Operation, PlacementOutcome};
use fractile_core::config::{Config, ShortcutAction, ShortcutScope};
use fractile_core::geometry::{apply_constraints, remap_rect, selection_rect, Grid, Rect, Selection, SnapAction};
use fractile_core::output::{next_output, output_for_rect, Output};
use fractile_core::rules::{first_match, OutputSel, RuleTrigger};
use fractile_core::window::{WindowInfo, WindowState};
use fractile_session::apps::AppIndex;
use fractile_session::store::Store;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

pub struct Subscriber {
    pub id: u64,
    pub kind: String,
    pub pid: i32,
    pub writer: Arc<Mutex<UnixStream>>,
}

#[derive(Clone, Debug)]
pub struct UndoEntry {
    pub window_id: String,
    pub frame: Rect,
    pub state: WindowState,
}

#[derive(Clone, Debug)]
struct CycleState {
    shortcut: String,
    window: String,
    output: String,
    at: Instant,
}

#[derive(Default)]
pub struct PanelState {
    pub visible: bool,
    pub target: Option<String>,
    pub last_target: Option<String>,
    pub pending_show: Option<Value>,
    pub last_timestamp: Option<u32>,
    pub requested_at: Option<Instant>,
    pub ui_spawned_at: Option<Instant>,
}

pub struct RestoreJob {
    pub run_id: String,
    pub cancel: Arc<AtomicBool>,
    pub new_windows: Sender<String>,
    pub progress: Value,
}

pub struct Daemon {
    pub cfg: RwLock<Config>,
    pub cfg_path: PathBuf,
    pub cfg_error: Mutex<Option<String>>,
    pub store: Mutex<Store>,
    pub adapter: RwLock<Arc<dyn CompositorAdapter>>,
    pub apps: Mutex<AppIndex>,
    pub exec_lock: Mutex<()>,
    pub undo: Mutex<Vec<UndoEntry>>,
    cycle: Mutex<Option<CycleState>>,
    pub subscribers: Mutex<Vec<Subscriber>>,
    pub next_sub: AtomicU64,
    pub own_windows: Mutex<Vec<String>>,
    pub own_pids: Mutex<Vec<u32>>,
    pub panel: Mutex<PanelState>,
    pub restore: Mutex<Option<RestoreJob>>,
    pub last_report: Mutex<Option<Value>>,
    pub hotkey_errors: Mutex<BTreeMap<String, String>>,
    pub hotkey_actions: Mutex<HashMap<String, (String, ShortcutAction, bool)>>,
    pub session_key: String,
    pub new_login: AtomicBool,
    pub env: Environment,
    pub log: Arc<Logger>,
    pub shutdown: AtomicBool,
    pub locked: AtomicBool,
    pub tiling: Option<String>,
    pub started: Instant,
    pub last_place_ms: Mutex<Vec<u64>>,
}


pub fn err(code: &str, msg: impl Into<String>) -> fractile_ipc::RpcError {
    fractile_ipc::RpcError::new(code, msg)
}

pub type R<T> = Result<T, fractile_ipc::RpcError>;

#[allow(clippy::too_many_arguments)]
impl Daemon {
    pub fn new(cfg: Config, cfg_path: PathBuf, cfg_error: Option<String>, store: Store, adapter: Arc<dyn CompositorAdapter>, env: Environment, log: Arc<Logger>, session_key: String, new_login: bool, tiling: Option<String>) -> Daemon {
        Daemon {
            cfg: RwLock::new(cfg),
            cfg_path,
            cfg_error: Mutex::new(cfg_error),
            store: Mutex::new(store),
            adapter: RwLock::new(adapter),
            apps: Mutex::new(AppIndex::system()),
            exec_lock: Mutex::new(()),
            undo: Mutex::new(vec![]),
            cycle: Mutex::new(None),
            subscribers: Mutex::new(vec![]),
            next_sub: AtomicU64::new(1),
            own_windows: Mutex::new(vec![]),
            own_pids: Mutex::new(vec![]),
            panel: Mutex::new(PanelState::default()),
            restore: Mutex::new(None),
            last_report: Mutex::new(None),
            hotkey_errors: Mutex::new(BTreeMap::new()),
            hotkey_actions: Mutex::new(HashMap::new()),
            session_key,
            new_login: AtomicBool::new(new_login),
            env,
            log,
            shutdown: AtomicBool::new(false),
            locked: AtomicBool::new(false),
            tiling,
            started: Instant::now(),
            last_place_ms: Mutex::new(vec![]),
        }
    }

    pub fn adapter(&self) -> Arc<dyn CompositorAdapter> {
        self.adapter.read().unwrap().clone()
    }

    pub fn caps(&self) -> Capabilities {
        let mut c = self.adapter().capabilities();
        c.tiling_manager = self.tiling.clone();
        c
    }

    pub fn config(&self) -> Config {
        self.cfg.read().unwrap().clone()
    }

    pub fn broadcast(&self, event: &str, data: Value) {
        let msg = fractile_ipc::EventMsg { event: event.into(), data };
        let mut line = serde_json::to_string(&msg).unwrap_or_default();
        line.push('\n');
        let mut subs = self.subscribers.lock().unwrap();
        subs.retain(|s| {
            let mut w = s.writer.lock().unwrap();
            let _ = w.set_write_timeout(Some(Duration::from_millis(500)));
            w.write_all(line.as_bytes()).is_ok()
        });
    }

    pub fn ui_attached(&self) -> bool {
        self.subscribers.lock().unwrap().iter().any(|s| s.kind == "ui")
    }

    fn sync_own(&self) {
        let mut ids = self.own_windows.lock().unwrap().clone();
        ids.extend(self.own_pids.lock().unwrap().iter().map(|p| format!("pid:{p}")));
        ids.push(format!("pid:{}", std::process::id()));
        self.adapter().set_own_windows(&ids);
    }

    pub fn register_ui(&self, windows: Vec<String>, pid: Option<u32>) {
        *self.own_windows.lock().unwrap() = windows;
        if let Some(p) = pid {
            let mut pids = self.own_pids.lock().unwrap();
            if !pids.contains(&p) {
                pids.push(p);
            }
        }
        self.sync_own();
    }

    pub fn enrich(&self, mut w: WindowInfo) -> WindowInfo {
        self.apps.lock().unwrap().enrich(&mut w);
        if let Some(p) = w.pid {
            if self.own_pids.lock().unwrap().contains(&p) || p == std::process::id() {
                w.is_own = true;
            }
        }
        w
    }

    pub fn windows(&self) -> R<Vec<WindowInfo>> {
        {
            let mut a = self.apps.lock().unwrap();
            a.refresh_if_stale(Duration::from_secs(300));
        }
        let ws = self.adapter().list_windows().map_err(|e| err("adapter", e.to_string()))?;
        Ok(ws.into_iter().map(|w| self.enrich(w)).collect())
    }

    pub fn window(&self, id: &str) -> R<WindowInfo> {
        self.adapter().get_window(id).map(|w| self.enrich(w)).map_err(|e| err("no_window", e.to_string()))
    }

    pub fn outputs(&self) -> R<Vec<Output>> {
        self.adapter().get_outputs().map_err(|e| err("adapter", e.to_string()))
    }

    pub fn require(&self, op: Operation) -> R<()> {
        let c = self.adapter().capabilities();
        if c.supports(op) {
            Ok(())
        } else {
            let why = c.operations.get(&op).map(|s| format!("{s:?}")).unwrap_or_default();
            Err(err("unsupported", format!("{op:?} is not available with adapter '{}' on this desktop: {why}", c.adapter)))
        }
    }

    /// Low-level placement used by every feature (panel, shortcuts, rules,
    /// layouts, restore). Serialized; records undo; applies constraints.
    pub fn place_rect(&self, wid: &str, rect: Rect, record_undo: bool) -> R<PlacementOutcome> {
        self.require(Operation::MoveResize)?;
        let _g = self.exec_lock.lock().unwrap();
        let t0 = Instant::now();
        let w = self.window(wid)?;
        if let Err(e) = w.placement_eligibility() {
            return Err(err("ineligible", format!("window {wid} is not placeable: {e:?}")));
        }
        let cfg = self.config();
        let c = apply_constraints(rect, &w.constraints, cfg.placement.constraint_policy).map_err(|e| err("constraints", e))?;
        if record_undo {
            let mut u = self.undo.lock().unwrap();
            u.push(UndoEntry { window_id: wid.into(), frame: w.frame, state: w.state });
            let depth = cfg.placement.undo_depth.max(1);
            if u.len() > depth {
                let n = u.len() - depth;
                u.drain(0..n);
            }
        }
        let mut out = self.adapter().move_resize(wid, c.rect).map_err(|e| err("adapter", e.to_string()))?;
        if c.adjusted {
            out.note = Some(format!("{}; constrained: {}", out.note.clone().unwrap_or_default(), c.reason.clone().unwrap_or_default()));
            out.requested = rect;
        }
        let ms = t0.elapsed().as_millis() as u64;
        {
            let mut v = self.last_place_ms.lock().unwrap();
            v.push(ms);
            if v.len() > 200 {
                v.remove(0);
            }
        }
        info!(self.log, "place {wid} app={} requested={:?} observed={:?} err={:?} {}ms", w.app_key(), out.requested, out.observed, out.max_error_px, ms);
        Ok(out)
    }

    pub fn resolve_window(&self, wid: Option<&str>) -> R<String> {
        if let Some(w) = wid {
            return Ok(w.to_string());
        }
        let a = self.adapter().get_active_window().map_err(|e| err("adapter", e.to_string()))?;
        let a = a.ok_or_else(|| err("no_window", "no active window"))?;
        let w = self.window(&a)?;
        if w.is_own {
            return self.panel.lock().unwrap().last_target.clone().ok_or_else(|| err("no_window", "active window belongs to Fractile"));
        }
        Ok(a)
    }

    pub fn output_by_id(&self, id: Option<&str>, w: &WindowInfo) -> R<Output> {
        let outs = self.outputs()?;
        if let Some(id) = id {
            return outs.into_iter().find(|o| o.stable_id == id).ok_or_else(|| err("no_output", format!("no output '{id}'")));
        }
        output_for_rect(&outs, &w.frame).cloned().ok_or_else(|| err("no_output", "no outputs"))
    }

    pub fn place_selection(&self, wid: &str, output_id: Option<&str>, grid: Grid, sel: Selection) -> R<PlacementOutcome> {
        if !grid.is_valid_effective() || !sel.is_valid_for(grid) {
            return Err(err("invalid", "invalid grid selection"));
        }
        let w = self.window(wid)?;
        let o = self.output_by_id(output_id, &w)?;
        let rect = selection_rect(o.work_area, grid, sel, &self.config().effective_spacing());
        self.place_rect(wid, rect, true)
    }

    pub fn snap(&self, wid: &str, action: SnapAction, output: &OutputSel) -> R<PlacementOutcome> {
        let w = self.window(wid)?;
        let outs = self.outputs()?;
        let o = output.resolve(&outs, Some(&w.frame)).cloned().ok_or_else(|| err("no_output", "no outputs"))?;
        let (g, s) = action.grid_selection();
        let rect = selection_rect(o.work_area, g, s, &self.config().effective_spacing());
        self.place_rect(wid, rect, true)
    }

    /// Move a window to the next/previous monitor keeping its relative geometry.
    pub fn move_to_output_step(&self, wid: &str, step: i32) -> R<PlacementOutcome> {
        let w = self.window(wid)?;
        let outs = self.outputs()?;
        let cur = output_for_rect(&outs, &w.frame).ok_or_else(|| err("no_output", "no outputs"))?;
        let next = next_output(&outs, &cur.stable_id, step).ok_or_else(|| err("no_output", "no outputs"))?;
        let rect = remap_rect(w.frame, cur.work_area, next.work_area);
        let was_max = w.state.maximized;
        let out = self.place_rect(wid, rect, true)?;
        if was_max {
            let _ = self.adapter().set_state(wid, &WindowState { maximized: true, ..w.state });
        }
        Ok(out)
    }

    pub fn undo_last(&self) -> R<Value> {
        let e = self.undo.lock().unwrap().pop().ok_or_else(|| err("empty", "nothing to undo"))?;
        let out = self.place_rect(&e.window_id, e.frame, false)?;
        if e.state.maximized || e.state.fullscreen {
            let _ = self.adapter().set_state(&e.window_id, &e.state);
        }
        Ok(json!({ "undone": e.window_id, "outcome": out }))
    }

    /// Divvy shortcut semantics, including monitor cycling on repeat (DV-17).
    pub fn run_place_shortcut(&self, shortcut_id: &str, grid: Grid, sel: Selection, output: &OutputSel, cycle: bool, wid: &str) -> R<PlacementOutcome> {
        let cfg = self.config();
        let w = self.window(wid)?;
        let outs = self.outputs()?;
        let mut target = output.resolve(&outs, Some(&w.frame)).cloned().ok_or_else(|| err("no_output", "no outputs"))?;
        if cycle && cfg.general.cycle_monitors_on_repeat {
            let mut c = self.cycle.lock().unwrap();
            if let Some(prev) = c.as_ref() {
                if prev.shortcut == shortcut_id && prev.window == wid && prev.at.elapsed() < Duration::from_millis(cfg.general.cycle_timeout_ms) {
                    if let Some(n) = next_output(&outs, &prev.output, 1) {
                        target = n;
                    }
                }
            }
            *c = Some(CycleState { shortcut: shortcut_id.into(), window: wid.into(), output: target.stable_id.clone(), at: Instant::now() });
        } else {
            *self.cycle.lock().unwrap() = None;
        }
        let rect = selection_rect(target.work_area, grid, sel, &cfg.effective_spacing());
        self.place_rect(wid, rect, true)
    }

    pub fn reset_cycle(&self) {
        *self.cycle.lock().unwrap() = None;
    }

    // ---------- hotkeys ----------
    pub fn register_hotkeys(&self) {
        let cfg = self.config();
        let adapter = self.adapter();
        let mut actions = self.hotkey_actions.lock().unwrap();
        for id in actions.keys() {
            let _ = adapter.unregister_hotkey(id);
        }
        actions.clear();
        let mut errors = BTreeMap::new();
        if !adapter.capabilities().supports(Operation::RegisterHotkey) {
            errors.insert("*".into(), "global shortcuts are not available with this adapter".into());
            *self.hotkey_errors.lock().unwrap() = errors;
            return;
        }
        let mut wanted: Vec<(String, String, String, ShortcutAction, bool)> = vec![];
        if let Some(p) = &cfg.general.panel_shortcut {
            wanted.push(("panel".into(), "Show/hide panel".into(), p.clone(), ShortcutAction::TogglePanel, false));
        }
        for s in cfg.shortcuts.iter().filter(|s| s.scope == ShortcutScope::Global) {
            if let Some(a) = &s.accelerator {
                wanted.push((s.id.clone(), s.name.clone(), a.clone(), s.action.clone(), s.cycle_monitors));
            }
        }
        for (id, name, accel, action, cyc) in wanted {
            match adapter.register_hotkey(&id, &accel) {
                Ok(()) => {
                    actions.insert(id, (name, action, cyc));
                }
                Err(e) => {
                    warn!(self.log, "hotkey '{name}' ({accel}) not registered: {e}");
                    errors.insert(id, format!("{accel}: {e}"));
                }
            }
        }
        *self.hotkey_errors.lock().unwrap() = errors;
    }

    /// Probe whether an accelerator is free by registering it temporarily.
    /// Check whether `accel` can be used as a global shortcut. `own_id` is
    /// the shortcut being edited ("panel" or a shortcut id): its current
    /// binding is not a conflict with itself.
    pub fn probe_accelerator(&self, accel: &str, own_id: Option<&str>) -> Value {
        let a = self.adapter();
        let probe_id = format!("probe-{}", uuid::Uuid::new_v4());
        let parsed = fractile_core::accel::Accelerator::parse(accel);
        if let Err(e) = parsed {
            return json!({"free": false, "verified": false, "reason": e});
        }
        // known Fractile bindings
        let cfg = self.config();
        let p = parsed.unwrap();
        let same = |x: &str| fractile_core::accel::Accelerator::parse(x).map(|q| q.same_as(&p)).unwrap_or(false);
        let mut ours: Vec<(String, String)> = vec![];
        if let Some(x) = &cfg.general.panel_shortcut {
            ours.push(("panel".into(), x.clone()));
        }
        for s in cfg.shortcuts.iter().filter(|s| s.scope == ShortcutScope::Global) {
            if let Some(x) = &s.accelerator {
                ours.push((s.id.clone(), x.clone()));
            }
        }
        for (id, x) in &ours {
            if !same(x) {
                continue;
            }
            if Some(id.as_str()) == own_id {
                let active = !self.hotkey_errors.lock().unwrap().contains_key(id);
                return if active {
                    json!({"free": true, "verified": true, "reason": "current binding (active)"})
                } else {
                    json!({"free": false, "verified": true, "reason": self.hotkey_errors.lock().unwrap().get(id).cloned().unwrap_or_default()})
                };
            }
            let name = if id == "panel" { "Show/hide panel".to_string() } else { cfg.shortcuts.iter().find(|s| &s.id == id).map(|s| s.name.clone()).unwrap_or_default() };
            return json!({"free": false, "verified": true, "reason": format!("already used by Fractile shortcut '{name}'")});
        }
        let desktop = desktop_binding_conflicts(&p);
        match a.register_hotkey(&probe_id, accel) {
            Ok(()) => {
                let _ = a.unregister_hotkey(&probe_id);
                if let Some(d) = desktop {
                    json!({"free": false, "verified": true, "reason": format!("assigned in desktop settings: {d}")})
                } else {
                    json!({"free": true, "verified": true, "reason": "registered successfully (desktop bindings checked where readable)"})
                }
            }
            Err(e) => json!({"free": false, "verified": true, "reason": e.to_string()}),
        }
    }

    pub fn handle_hotkey(self: &Arc<Self>, binding: &str, ts: u32) {
        let entry = self.hotkey_actions.lock().unwrap().get(binding).cloned();
        let Some((name, action, cyc)) = entry else { return };
        info!(self.log, "hotkey {binding} ({name})");
        let d = self.clone();
        let b = binding.to_string();
        std::thread::spawn(move || {
            if let Err(e) = d.run_action(&b, &action, cyc, Some(ts)) {
                warn!(d.log, "shortcut '{name}' failed: {e}");
                d.broadcast("notice", json!({"level": "warning", "message": format!("{name}: {}", e.message)}));
            }
        });
    }

    pub fn run_action(self: &Arc<Self>, binding: &str, action: &ShortcutAction, cycle: bool, ts: Option<u32>) -> R<Value> {
        match action {
            ShortcutAction::TogglePanel => Ok(self.toggle_panel(ts)),
            ShortcutAction::CommandPalette => {
                self.request_ui("show_palette", json!({}));
                Ok(json!({"ok": true}))
            }
            ShortcutAction::Place { grid, selection, output } => {
                let w = self.resolve_window(None)?;
                Ok(json!(self.run_place_shortcut(binding, *grid, *selection, output, cycle, &w)?))
            }
            ShortcutAction::Snap { action } => {
                let w = self.resolve_window(None)?;
                Ok(json!(self.snap(&w, *action, &OutputSel::Current)?))
            }
            ShortcutAction::MoveToNextOutput => {
                let w = self.resolve_window(None)?;
                Ok(json!(self.move_to_output_step(&w, 1)?))
            }
            ShortcutAction::MoveToPrevOutput => {
                let w = self.resolve_window(None)?;
                Ok(json!(self.move_to_output_step(&w, -1)?))
            }
            ShortcutAction::MoveToWorkspace { index } => {
                let w = self.resolve_window(None)?;
                self.require(Operation::MoveToWorkspace)?;
                self.adapter().move_to_workspace(&w, *index).map_err(|e| err("adapter", e.to_string()))?;
                Ok(json!({"ok": true}))
            }
            ShortcutAction::Undo => self.undo_last(),
            ShortcutAction::SaveWorkspace => crate::session::save_snapshot(self, None, fractile_core::snapshot::SnapshotMode::Manual).map(|s| json!({"snapshot_id": s})),
            ShortcutAction::RestoreLatest => crate::session::restore_latest_interactive(self),
            ShortcutAction::ApplyLayout { name } => crate::session::start_layout(self, name, fractile_core::layout::ApplyMode::LaunchMissing, &Default::default(), false),
        }
    }

    // ---------- panel ----------
    pub fn panel_payload(&self, ts: Option<u32>) -> Value {
        let target = self.adapter().get_active_window().ok().flatten();
        let mut tw = target.and_then(|t| self.window(&t).ok());
        if tw.as_ref().map(|w| w.is_own || !w.is_placeable()).unwrap_or(false) {
            let last = self.panel.lock().unwrap().last_target.clone();
            tw = last.and_then(|t| self.window(&t).ok());
        }
        let outs = self.outputs().unwrap_or_default();
        let cfg = self.config();
        {
            let mut p = self.panel.lock().unwrap();
            p.target = tw.as_ref().map(|w| w.id.clone());
            if let Some(w) = &tw {
                p.last_target = Some(w.id.clone());
            }
            p.last_timestamp = ts;
            p.requested_at = Some(Instant::now());
        }
        let icon = tw.as_ref().and_then(|w| w.desktop_file_id.as_ref()).and_then(|d| self.apps.lock().unwrap().get(d).and_then(|e| e.icon.clone()));
        let app_name = tw.as_ref().and_then(|w| w.desktop_file_id.as_ref()).and_then(|d| self.apps.lock().unwrap().get(d).map(|e| e.name.clone()));
        json!({
            "target": tw,
            "target_icon": icon,
            "target_app_name": app_name,
            "outputs": outs,
            "grid": cfg.effective_default_grid(),
            "timestamp": ts,
            "caps": self.caps(),
        })
    }

    pub fn toggle_panel(self: &Arc<Self>, ts: Option<u32>) -> Value {
        let visible = self.panel.lock().unwrap().visible;
        if visible && self.ui_attached() {
            self.panel.lock().unwrap().visible = false;
            self.broadcast("hide_panel", json!({}));
            return json!({"visible": false});
        }
        self.show_panel(ts)
    }

    pub fn show_panel(self: &Arc<Self>, ts: Option<u32>) -> Value {
        let payload = self.panel_payload(ts);
        self.panel.lock().unwrap().visible = true;
        self.request_ui("show_panel", payload.clone());
        json!({"visible": true, "target": payload["target"]["id"]})
    }

    /// Deliver a UI request, spawning the UI process if none is attached.
    pub fn request_ui(self: &Arc<Self>, event: &str, data: Value) {
        if self.ui_attached() {
            self.broadcast(event, data);
            return;
        }
        let mut p = self.panel.lock().unwrap();
        p.pending_show = Some(json!({"event": event, "data": data}));
        let recently = p.ui_spawned_at.map(|t| t.elapsed() < Duration::from_secs(10)).unwrap_or(false);
        if !recently {
            p.ui_spawned_at = Some(Instant::now());
            drop(p);
            spawn_ui(self, &[]);
        }
    }

    pub fn flush_pending_ui(&self) {
        let pending = self.panel.lock().unwrap().pending_show.take();
        if let Some(v) = pending {
            self.broadcast(v["event"].as_str().unwrap_or("show_panel"), v["data"].clone());
        }
    }

    /// Activate a Fractile UI window with the triggering timestamp so the
    /// window manager's focus-stealing prevention lets it take focus.
    pub fn focus_own_window(&self, wid: &str) {
        let ts = self.panel.lock().unwrap().last_timestamp;
        let _ = self.adapter().activate(wid, ts);
    }

    // ---------- config ----------
    pub fn apply_config(self: &Arc<Self>, new: Config) -> R<()> {
        new.validate().map_err(|e| err("invalid_config", e))?;
        new.save(&self.cfg_path).map_err(|e| err("io", e))?;
        *self.cfg.write().unwrap() = new;
        *self.cfg_error.lock().unwrap() = None;
        self.register_hotkeys();
        crate::autostart::reconcile(self);
        self.broadcast("config_changed", json!({}));
        Ok(())
    }

    // ---------- events ----------
    pub fn handle_event(self: &Arc<Self>, ev: AdapterEvent) {
        match &ev {
            AdapterEvent::Hotkey { binding_id, timestamp } => self.handle_hotkey(binding_id, *timestamp),
            AdapterEvent::WindowCreated { id } => {
                if let Some(job) = self.restore.lock().unwrap().as_ref() {
                    let _ = job.new_windows.send(id.clone());
                    return;
                }
                let cfg = self.config();
                if cfg.rules_enabled && cfg.rules.iter().any(|r| r.enabled && r.trigger == RuleTrigger::OnCreate) {
                    let d = self.clone();
                    let id = id.clone();
                    std::thread::spawn(move || d.apply_rules_to_new(&id));
                }
                self.broadcast("windows_changed", json!({}));
            }
            AdapterEvent::WindowClosed { id } => {
                self.undo.lock().unwrap().retain(|u| &u.window_id != id);
                self.broadcast("windows_changed", json!({}));
            }
            AdapterEvent::ActiveChanged { id } => {
                if let Some(id) = id {
                    if let Ok(w) = self.window(id) {
                        if !w.is_own && w.is_placeable() {
                            self.panel.lock().unwrap().last_target = Some(id.clone());
                            self.reset_cycle_if_other(id);
                        }
                    }
                }
            }
            AdapterEvent::OutputsChanged => {
                self.reset_cycle();
                crate::session::on_outputs_changed(self);
                self.broadcast("outputs_changed", json!({}));
            }
            AdapterEvent::WorkspacesChanged => self.broadcast("workspaces_changed", json!({})),
            AdapterEvent::WindowChanged { .. } => {}
            AdapterEvent::Disconnected { reason } => {
                error!(self.log, "adapter disconnected: {reason}");
                self.broadcast("notice", json!({"level": "error", "message": format!("desktop connection lost: {reason}")}));
            }
        }
    }

    fn reset_cycle_if_other(&self, id: &str) {
        let mut c = self.cycle.lock().unwrap();
        if c.as_ref().map(|s| s.window != id).unwrap_or(false) {
            *c = None;
        }
    }

    fn apply_rules_to_new(&self, id: &str) {
        // Wait for first stable geometry (bounded) before placing.
        let mut prev = None;
        for _ in 0..10 {
            std::thread::sleep(Duration::from_millis(80));
            let cur = self.window(id).ok().map(|w| w.frame);
            if cur.is_some() && cur == prev {
                break;
            }
            prev = cur;
        }
        let Ok(w) = self.window(id) else { return };
        if !w.is_placeable() {
            return;
        }
        let cfg = self.config();
        let all = self.windows().unwrap_or_default();
        let ords = fractile_core::identity::instance_ordinals(&all);
        let ord = ords.get(id).copied().unwrap_or(0);
        if let Some((rule, _)) = first_match(&cfg.rules, &w, ord, cfg.allow_title_regex, Some(RuleTrigger::OnCreate)) {
            info!(self.log, "rule '{}' matched new window {id}", rule.name);
            let _ = self.apply_rule(rule, &w);
        }
    }

    pub fn apply_rule(&self, rule: &fractile_core::rules::Rule, w: &WindowInfo) -> R<Value> {
        let outs = self.outputs()?;
        let o = rule.output.resolve(&outs, Some(&w.frame)).cloned().ok_or_else(|| err("no_output", "no outputs"))?;
        if let Some(ws) = rule.workspace {
            let _ = self.adapter().move_to_workspace(&w.id, ws);
        }
        let mut res = json!({"rule": rule.name, "window": w.id});
        if let Some(p) = &rule.placement {
            let rect = p.to_rect(o.work_area, &self.config().effective_spacing());
            res["outcome"] = json!(self.place_rect(&w.id, rect, true)?);
        }
        if let Some(st) = &rule.state {
            let _ = self.adapter().set_state(&w.id, st);
        }
        Ok(res)
    }
}

/// Look up accelerators bound in GNOME settings (best effort, read-only).
pub fn desktop_binding_conflicts(a: &fractile_core::accel::Accelerator) -> Option<String> {
    let schemas = ["org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings", "org.gnome.settings-daemon.plugins.media-keys", "org.gnome.mutter.wayland.keybindings"];
    for s in schemas {
        let Ok(out) = std::process::Command::new("gsettings").args(["list-recursively", s]).output() else { return None };
        if !out.status.success() {
            continue;
        }
        for line in String::from_utf8_lossy(&out.stdout).lines() {
            let mut parts = line.splitn(3, ' ');
            let (_schema, key, val) = (parts.next(), parts.next().unwrap_or(""), parts.next().unwrap_or(""));
            for acc in val.trim_matches(|c| c == '[' || c == ']' || c == '@' || c == 'a' || c == 's' || c == ' ').split(',') {
                let acc = acc.trim().trim_matches('\'');
                if acc.is_empty() {
                    continue;
                }
                if let Ok(b) = fractile_core::accel::Accelerator::parse(acc) {
                    if b.same_as(a) {
                        return Some(format!("{s} {key}"));
                    }
                }
            }
        }
    }
    None
}

/// Locate a sibling binary (installed layout) or fall back to PATH.
pub fn find_binary(name: &str) -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join(name);
            if p.exists() {
                return p;
            }
        }
    }
    PathBuf::from(name)
}

pub fn spawn_ui(d: &Daemon, args: &[&str]) {
    let ui = std::env::var_os("FRACTILE_UI").map(PathBuf::from).unwrap_or_else(|| find_binary("fractile-ui"));
    let mut argv: Vec<String> = vec![];
    if fractile_session::launch::systemd_user_available() && std::env::var_os("FRACTILE_NO_SCOPE").is_none() {
        argv.extend(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit"].iter().map(|s| s.to_string()));
        argv.push(format!("app-fractile-ui-{}.scope", std::process::id() as u64 * 1000 + (chrono::Utc::now().timestamp_subsec_millis() as u64)));
        argv.push("--".into());
    }
    argv.push(ui.to_string_lossy().to_string());
    argv.extend(args.iter().map(|s| s.to_string()));
    let plan = fractile_session::launch::LaunchPlan { desktop_id: "fractile-ui".into(), argv, method: "ui" };
    match fractile_session::launch::spawn(&plan) {
        Ok(pid) => info!(d.log, "spawned UI pid {pid}"),
        Err(e) => error!(d.log, "cannot start UI: {e}"),
    }
}

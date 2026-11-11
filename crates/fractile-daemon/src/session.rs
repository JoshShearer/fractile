//! Session service: snapshots, restore jobs over the live adapter, autosave,
//! login-time restore policy, monitor reconnect handling.

use crate::daemon::{err, Daemon, RestoreJob, R};
use fractile_core::adapter::{Capabilities, Operation, PlacementOutcome};
use fractile_core::config::{PowerLossPolicy, RestorePolicy};
use fractile_core::geometry::{rect_to_norm, Rect};
use fractile_core::layout::{ApplyMode, Layout, LayoutEntry, LayoutVariant};
use fractile_core::output::{output_for_rect, Output};
use fractile_core::rules::{Matcher, OutputSel, Placement};
use fractile_core::snapshot::{LaunchPolicy, Snapshot, SnapshotMode};
use fractile_core::window::{WindowInfo, WindowState};
use fractile_session::capture::{capture, CaptureInput};
use fractile_session::launch::{plan_launch, spawn, systemd_user_available, which};
use fractile_session::restore::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub fn capture_now(d: &Daemon, mode: SnapshotMode, name: Option<String>) -> R<Snapshot> {
    // An empty window list because the desktop can't report windows (no WM
    // yet, unsupported compositor) must never become a "latest" snapshot.
    d.require(Operation::ListWindows)?;
    let windows = d.windows()?;
    let outputs = d.outputs()?;
    let workspaces = d.adapter().get_workspaces().unwrap_or_default();
    let caps = d.caps();
    let cfg = d.config();
    let input = CaptureInput { windows: &windows, outputs: &outputs, workspaces: &workspaces, caps: &caps, desktop: &d.env.current_desktop };
    Ok(capture(&input, &cfg, mode, name))
}

pub fn save_snapshot(d: &Daemon, name: Option<String>, mode: SnapshotMode) -> R<String> {
    if d.config().session.policy == RestorePolicy::Off && mode != SnapshotMode::Manual {
        return Err(err("disabled", "session snapshots are turned off"));
    }
    if d.is_locked() && d.config().session.skip_capture_when_locked && mode != SnapshotMode::Manual {
        return Err(err("locked", "session is locked; capture skipped"));
    }
    let s = capture_now(d, mode, name.clone())?;
    let pinned = name.is_some();
    d.store.lock().unwrap().save_snapshot(&s, pinned).map_err(|e| err("store", e))?;
    info!(d.log, "snapshot {} saved mode={:?} windows={}", s.snapshot_id, mode, s.windows.len());
    d.broadcast("snapshots_changed", json!({"id": s.snapshot_id}));
    Ok(s.snapshot_id)
}

/// RestoreHost over the daemon's live adapter.
pub struct DaemonHost {
    pub d: Arc<Daemon>,
    pub new_windows: Mutex<Receiver<String>>,
}

impl RestoreHost for DaemonHost {
    fn capabilities(&self) -> Capabilities {
        self.d.caps()
    }
    fn windows(&self) -> Result<Vec<WindowInfo>, String> {
        self.d.windows().map_err(|e| e.message)
    }
    fn window(&self, id: &str) -> Result<WindowInfo, String> {
        self.d.window(id).map_err(|e| e.message)
    }
    fn outputs(&self) -> Result<Vec<Output>, String> {
        self.d.outputs().map_err(|e| e.message)
    }
    fn place(&self, id: &str, rect: Rect) -> Result<PlacementOutcome, String> {
        self.d.place_rect(id, rect, false).map_err(|e| e.message)
    }
    fn set_state(&self, id: &str, s: &WindowState) -> Result<(), String> {
        self.d.adapter().set_state(id, s).map_err(|e| e.to_string())
    }
    fn move_to_workspace(&self, id: &str, ws: u32) -> Result<(), String> {
        self.d.adapter().move_to_workspace(id, ws).map_err(|e| e.to_string())
    }
    fn activate(&self, id: &str) -> Result<(), String> {
        self.d.adapter().activate(id, None).map_err(|e| e.to_string())
    }
    fn launch(&self, desktop_id: &str, extra: &[String]) -> Result<u32, String> {
        let entry = self.d.apps.lock().unwrap().get(desktop_id).cloned().ok_or_else(|| format!("{desktop_id} is not installed"))?;
        let plan = plan_launch(&entry, extra, systemd_user_available(), which("gio"))?;
        info!(self.d.log, "launch {desktop_id} via {} argv={:?}", plan.method, plan.argv);
        spawn(&plan)
    }
    fn can_launch(&self, desktop_id: &str) -> Result<String, String> {
        let entry = self.d.apps.lock().unwrap().get(desktop_id).cloned().ok_or_else(|| format!("{desktop_id} is not installed"))?;
        plan_launch(&entry, &[], systemd_user_available(), which("gio")).map(|p| p.method.to_string())
    }
    fn next_new_window(&self, timeout: Duration) -> Option<String> {
        self.new_windows.lock().unwrap().recv_timeout(timeout).ok()
    }
    fn recent_launches(&self, desktop_id: &str, seconds: i64) -> u32 {
        self.d.store.lock().unwrap().launches_since(desktop_id, seconds).unwrap_or(0)
    }
    fn record_launch(&self, desktop_id: &str, run_id: &str, pid: u32) {
        let _ = self.d.store.lock().unwrap().record_launch(desktop_id, Some(run_id), Some(pid));
    }
    fn persist(&self, run_id: &str, item: &ItemResult) {
        let _ = self.d.store.lock().unwrap().item_upsert(run_id, &item.record_id, &item.label, item.status.as_str(), item.window_id.as_deref(), item.launched, item.message.as_deref());
    }
    fn progress(&self, p: &Progress) {
        let v = json!(p);
        if let Some(j) = self.d.restore.lock().unwrap().as_mut() {
            j.progress = v.clone();
        }
        self.d.broadcast("restore_progress", v);
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RestoreRequest {
    #[serde(default)]
    pub approved: Vec<String>,
    #[serde(default)]
    pub approve_all: bool,
    #[serde(default)]
    pub selected: Option<Vec<String>>,
    #[serde(default = "yes")]
    pub launch: bool,
    #[serde(default)]
    pub run_id: Option<String>,
}

fn yes() -> bool {
    true
}

impl Default for RestoreRequest {
    fn default() -> Self {
        RestoreRequest { approved: vec![], approve_all: false, selected: None, launch: true, run_id: None }
    }
}

enum Source {
    Snapshot(Snapshot),
    Layout(Layout, ApplyMode),
}

fn build(d: &Arc<Daemon>, src: &Source, req: &RestoreRequest, run_id: &str) -> R<(Vec<Target>, Vec<Output>, RunOptions, DaemonHost)> {
    let cfg = d.config();
    let mut opts = RunOptions::from_config(&cfg, run_id);
    opts.approved = req.approved.iter().cloned().collect();
    opts.approve_all = req.approve_all;
    opts.launch_enabled = req.launch;
    opts.selected = req.selected.as_ref().map(|v| v.iter().cloned().collect());
    // previous attempts of this run (idempotent retry)
    for (rid, status, wid, launched, _, _) in d.store.lock().unwrap().items(run_id).unwrap_or_default() {
        opts.previous.insert(rid, (ItemStatus::parse(&status), wid, launched.is_some()));
    }
    let outputs = d.outputs()?;
    let (targets, recorded) = match src {
        Source::Snapshot(s) => (targets_from_snapshot(s, &cfg), s.outputs.clone()),
        Source::Layout(l, mode) => (targets_from_layout(l, *mode, &outputs, &cfg).map_err(|e| err("invalid", e))?, outputs.clone()),
    };
    let (_tx, rx) = channel();
    Ok((targets, recorded, opts, DaemonHost { d: d.clone(), new_windows: Mutex::new(rx) }))
}

fn plan_json(p: &Plan) -> Value {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for i in &p.items {
        *counts.entry(serde_json::to_value(i.action).unwrap().as_str().unwrap().to_string()).or_insert(0) += 1;
    }
    json!({"items": p.items, "warnings": p.warnings, "output_mapping": p.output_mapping, "counts": counts})
}

pub fn dry_run_snapshot(d: &Arc<Daemon>, snapshot_ref: &str, req: &RestoreRequest) -> R<Value> {
    let id = d.store.lock().unwrap().resolve_snapshot_ref(snapshot_ref).map_err(|e| err("not_found", e))?;
    let s = d.store.lock().unwrap().get_snapshot(&id).map_err(|e| err("corrupt", e))?;
    let (targets, recorded, opts, host) = build(d, &Source::Snapshot(s.clone()), req, "dry-run")?;
    let p = plan(targets, &recorded, &host, &opts, &d.config()).map_err(|e| err("plan", e))?;
    let mut v = plan_json(&p);
    v["snapshot"] = json!({"id": s.snapshot_id, "created_at": s.created_at, "name": s.name, "mode": s.mode, "windows": s.windows.len()});
    v["limitations"] = json!(LIMITATIONS);
    Ok(v)
}

pub const LIMITATIONS: &str = "Fractile restores application windows (position, size, monitor, workspace, state). It cannot restore unsaved document contents, in-memory state, or tabs unless the application's own session restore provides them.";

pub fn dry_run_layout(d: &Arc<Daemon>, name: &str, mode: ApplyMode, req: &RestoreRequest) -> R<Value> {
    let l = d.store.lock().unwrap().get_layout(name).map_err(|e| err("not_found", e))?;
    let (targets, recorded, opts, host) = build(d, &Source::Layout(l, mode), req, "dry-run")?;
    let p = plan(targets, &recorded, &host, &opts, &d.config()).map_err(|e| err("plan", e))?;
    Ok(plan_json(&p))
}

fn start_job(d: &Arc<Daemon>, src: Source, req: &RestoreRequest, source_label: &str, source_id: Option<String>, wait: bool) -> R<Value> {
    if d.restore.lock().unwrap().is_some() {
        return Err(err("busy", "a restore is already running"));
    }
    if d.is_locked() {
        return Err(err("locked", "the session is locked; restore runs only while it is unlocked"));
    }
    let run_id = req.run_id.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let (targets, recorded, opts, mut host) = build(d, &src, req, &run_id)?;
    // Remember the user's approvals so a resumed run (after a crash) keeps them.
    let _ = d.store.lock().unwrap().meta_set(&format!("run_req:{run_id}"), &serde_json::to_string(req).unwrap_or_default());
    // Baseline before touching anything (for "undo restore"); a retry keeps
    // the original run's baseline.
    let has_baseline = d.store.lock().unwrap().meta_get(&format!("baseline:{run_id}")).ok().flatten().is_some();
    if !has_baseline && !matches!(src, Source::Snapshot(ref s) if s.mode == SnapshotMode::PreRestore) {
        if let Ok(mut base) = capture_now(d, SnapshotMode::PreRestore, None) {
            base.diagnostics.push(format!("baseline for restore run {run_id}"));
            let _ = d.store.lock().unwrap().save_snapshot(&base, false);
            let _ = d.store.lock().unwrap().meta_set(&format!("baseline:{run_id}"), &base.snapshot_id);
            let _ = d.store.lock().unwrap().meta_set("last_baseline", &base.snapshot_id);
        }
    }
    d.store.lock().unwrap().run_begin(&run_id, source_label, source_id.as_deref()).map_err(|e| err("store", e))?;
    let (tx, rx) = channel();
    host.new_windows = Mutex::new(rx);
    let cancel = Arc::new(AtomicBool::new(false));
    *d.restore.lock().unwrap() = Some(RestoreJob { run_id: run_id.clone(), cancel: cancel.clone(), new_windows: tx, progress: json!({}) });
    let cfg = d.config();
    let p = match plan(targets, &recorded, &host, &opts, &cfg) {
        Ok(p) => p,
        Err(e) => {
            *d.restore.lock().unwrap() = None;
            let _ = d.store.lock().unwrap().run_finish(&run_id, "failed", &e);
            return Err(err("plan", e));
        }
    };
    d.broadcast("restore_started", json!({"run_id": run_id, "plan": plan_json(&p)}));
    info!(d.log, "restore {run_id} started source={source_label} targets={}", p.targets.len());
    let d2 = d.clone();
    let rid = run_id.clone();
    let worker = std::thread::spawn(move || {
        let rep = execute(&p, &host, &opts, &cancel);
        let state = if rep.cancelled {
            "cancelled"
        } else if rep.all_success() {
            "completed"
        } else if rep.items.iter().any(|i| i.status.is_success()) {
            "partial"
        } else if rep.items.is_empty() {
            "completed"
        } else {
            "failed"
        };
        let summary = rep.summary();
        let _ = d2.store.lock().unwrap().run_finish(&rid, state, &summary);
        info!(d2.log, "restore {rid} finished state={state} {summary} launches={} {}ms", rep.launches, rep.duration_ms);
        let v = json!({"run_id": rid, "state": state, "report": rep});
        *d2.last_report.lock().unwrap() = Some(v.clone());
        *d2.restore.lock().unwrap() = None;
        d2.broadcast("restore_finished", v.clone());
        v
    });
    if wait {
        return worker.join().map_err(|_| err("internal", "restore worker panicked"));
    }
    Ok(json!({"run_id": run_id, "started": true}))
}

pub fn start_snapshot(d: &Arc<Daemon>, snapshot_ref: &str, req: &RestoreRequest, source: &str, wait: bool) -> R<Value> {
    let id = d.store.lock().unwrap().resolve_snapshot_ref(snapshot_ref).map_err(|e| err("not_found", e))?;
    let s = d.store.lock().unwrap().get_snapshot(&id).map_err(|e| err("corrupt", e))?;
    start_job(d, Source::Snapshot(s), req, source, Some(id), wait)
}

pub fn start_layout(d: &Arc<Daemon>, name: &str, mode: ApplyMode, req: &RestoreRequest, wait: bool) -> R<Value> {
    let l = d.store.lock().unwrap().get_layout(name).map_err(|e| err("not_found", e))?;
    start_job(d, Source::Layout(l, mode), req, "layout", Some(name.to_string()), wait)
}

/// Retry only incomplete items of an earlier run (same run id → idempotent).
pub fn retry_run(d: &Arc<Daemon>, run_id: &str, req: &RestoreRequest, wait: bool) -> R<Value> {
    let (source, source_id, _state, _) = d.store.lock().unwrap().run_get(run_id).map_err(|e| err("store", e))?.ok_or_else(|| err("not_found", "no such run"))?;
    let mut r = req.clone();
    r.run_id = Some(run_id.to_string());
    let sid = source_id.ok_or_else(|| err("invalid", "run has no source"))?;
    if source == "layout" {
        let l = d.store.lock().unwrap().get_layout(&sid).map_err(|e| err("not_found", e))?;
        start_job(d, Source::Layout(l, ApplyMode::LaunchMissing), &r, &source, Some(sid), wait)
    } else {
        let s = d.store.lock().unwrap().get_snapshot(&sid).map_err(|e| err("corrupt", e))?;
        start_job(d, Source::Snapshot(s), &r, &source, Some(sid), wait)
    }
}

/// Put windows back where they were before the last restore (no launching).
pub fn undo_restore(d: &Arc<Daemon>, wait: bool) -> R<Value> {
    let id = d.store.lock().unwrap().meta_get("last_baseline").map_err(|e| err("store", e))?.ok_or_else(|| err("empty", "no pre-restore baseline"))?;
    let s = d.store.lock().unwrap().get_snapshot(&id).map_err(|e| err("corrupt", e))?;
    let req = RestoreRequest { launch: false, ..Default::default() };
    start_job(d, Source::Snapshot(s), &req, "undo", Some(id), wait)
}

pub fn restore_latest_interactive(d: &Arc<Daemon>) -> R<Value> {
    let s = d.store.lock().unwrap().latest_good(Some(&[SnapshotMode::Manual, SnapshotMode::Periodic, SnapshotMode::Logout, SnapshotMode::Shutdown])).ok_or_else(|| err("empty", "no valid snapshot"))?;
    let cfg = d.config();
    if cfg.session.confirm_before_restore {
        d.request_ui("show_recovery", json!({"snapshot_id": s.snapshot_id, "reason": "shortcut"}));
        return Ok(json!({"prompted": true}));
    }
    start_snapshot(d, &s.snapshot_id, &RestoreRequest::default(), "manual", false)
}

pub fn cancel(d: &Daemon) -> R<Value> {
    match d.restore.lock().unwrap().as_ref() {
        Some(j) => {
            j.cancel.store(true, Ordering::SeqCst);
            Ok(json!({"cancelling": j.run_id}))
        }
        None => Err(err("idle", "no restore running")),
    }
}

/// Convert current windows into an editable named layout.
pub fn layout_from_windows(d: &Daemon, name: &str, launch: bool) -> R<Layout> {
    let windows = d.windows()?;
    let outputs = d.outputs()?;
    let ords = fractile_core::identity::instance_ordinals(&windows);
    let mut entries = vec![];
    for w in windows.iter().filter(|w| w.is_placeable()) {
        let o = output_for_rect(&outputs, &w.frame);
        let wa = o.map(|o| o.work_area).unwrap_or(w.frame);
        let n = rect_to_norm(wa, w.frame);
        let desk = w.desktop_file_id.clone();
        let label = desk.clone().map(|x| x.trim_end_matches(".desktop").to_string()).or(w.wm_class.clone()).unwrap_or_else(|| "window".into());
        let matcher = Matcher {
            desktop_id: desk.clone(),
            wm_class: if desk.is_none() { w.wm_class.clone() } else { None },
            app_id: if desk.is_none() { w.app_id.clone() } else { None },
            role: w.role.clone(),
            instance_ordinal: Some(ords.get(&w.id).copied().unwrap_or(0)),
            ..Default::default()
        };
        if matcher.is_empty() {
            continue;
        }
        entries.push(LayoutEntry {
            id: format!("e{}", entries.len() + 1),
            label,
            matcher,
            placement: Placement::Normalized { rect: fractile_core::geometry::NormRect::from_array(n.as_array().map(|v| (v * 1e4).round() / 1e4)) },
            output: o.map(|o| OutputSel::Stable(o.stable_id.clone())).unwrap_or_default(),
            workspace: w.workspace,
            state: if w.state.maximized { Some(w.state) } else { None },
            launch: if launch { desk.map(|d| fractile_core::layout::LaunchSpec { desktop_id: d, policy: LaunchPolicy::IfMissing, delay_ms: 0, depends_on: vec![], group: None }) } else { None },
            instances: 1,
            excluded: false,
        });
    }
    let now = chrono::Local::now().to_rfc3339();
    Ok(Layout {
        schema_version: 1,
        name: name.into(),
        description: format!("Saved from {} windows on {}", entries.len(), chrono::Local::now().format("%Y-%m-%d %H:%M")),
        favorite: false,
        variants: vec![LayoutVariant { when_topology: None, when_output_count: Some(outputs.len() as u32), entries }],
        created_at: now.clone(),
        updated_at: now,
    })
}

/// Preview a layout as concrete rectangles on present outputs (wireframe data).
pub fn layout_preview(d: &Daemon, l: &Layout) -> R<Value> {
    let outputs = d.outputs()?;
    let windows = d.windows()?;
    let cfg = d.config();
    let v = l.select_variant(&outputs).ok_or_else(|| err("invalid", "no variant"))?;
    let mut items = vec![];
    let ords = fractile_core::identity::instance_ordinals(&windows);
    for e in &v.entries {
        let o = e.output.resolve(&outputs, None);
        let rect = o.map(|o| e.placement.to_rect(o.work_area, &cfg.effective_spacing()));
        let matched: Vec<String> = windows
            .iter()
            .filter(|w| w.is_placeable() && e.matcher.explain(w, ords.get(&w.id).copied().unwrap_or(0), cfg.allow_title_regex).matched)
            .map(|w| w.id.clone())
            .collect();
        let icon = e.matcher.desktop_id.as_ref().and_then(|dd| d.apps.lock().unwrap().get(dd).and_then(|x| x.icon.clone()));
        items.push(json!({"entry": e, "output": o.map(|o| o.stable_id.clone()), "rect": rect, "matched_windows": matched, "icon": icon}));
    }
    let matched_ids: HashSet<String> = items.iter().flat_map(|i| i["matched_windows"].as_array().cloned().unwrap_or_default()).filter_map(|v| v.as_str().map(|s| s.to_string())).collect();
    let extra: Vec<Value> = windows.iter().filter(|w| w.is_placeable() && !matched_ids.contains(&w.id)).map(|w| json!({"id": w.id, "app": w.app_key()})).collect();
    let missing = items.iter().filter(|i| i["matched_windows"].as_array().map(|a| a.is_empty()).unwrap_or(true)).count();
    Ok(json!({"layout": l.name, "outputs": outputs, "items": items, "diff": {"missing_entries": missing, "unmatched_open_windows": extra}}))
}

// ---------------- background services ----------------

pub fn boot_id() -> String {
    std::fs::read_to_string("/proc/sys/kernel/random/boot_id").map(|s| s.trim().to_string()).unwrap_or_else(|_| "unknown-boot".into())
}

pub fn session_key() -> String {
    format!("{}:{}", boot_id(), std::env::var("XDG_SESSION_ID").unwrap_or_else(|_| "nosession".into()))
}

/// Periodic autosave (only for automatic policies), retention and daily DB backup.
pub fn autosave_loop(d: Arc<Daemon>) {
    let mut last_capture = Instant::now();
    let mut last_backup_day = String::new();
    loop {
        for _ in 0..30 {
            if d.shutdown.load(Ordering::SeqCst) {
                return;
            }
            std::thread::sleep(Duration::from_secs(1));
        }
        let cfg = d.config();
        let auto = matches!(cfg.session.policy, RestorePolicy::LastClean | RestorePolicy::LastKnown | RestorePolicy::Ask);
        if !auto || !cfg.session.autosave_enabled || d.restore.lock().unwrap().is_some() {
            continue;
        }
        if last_capture.elapsed() < Duration::from_secs(cfg.session.autosave_interval_min * 60) {
            continue;
        }
        last_capture = Instant::now();
        if d.is_locked() && cfg.session.skip_capture_when_locked {
            continue;
        }
        match capture_now(&d, SnapshotMode::Periodic, None) {
            Ok(s) if s.windows.is_empty() => {}
            Ok(s) => {
                let prev = d.store.lock().unwrap().latest_fingerprint();
                if prev.as_deref() != Some(s.content_fingerprint().as_str()) {
                    if let Err(e) = d.store.lock().unwrap().save_snapshot(&s, false) {
                        error!(d.log, "autosave failed: {e}");
                    } else {
                        info!(d.log, "autosave {} ({} windows)", s.snapshot_id, s.windows.len());
                    }
                }
                let _ = d.store.lock().unwrap().prune(cfg.session.max_recent, cfg.session.daily_history_days);
            }
            Err(e) => warn!(d.log, "autosave capture failed: {}", e.message),
        }
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        if today != last_backup_day {
            last_backup_day = today;
            let dest = fractile_core::paths::data_dir().join("fractile.db.bak");
            if let Err(e) = d.store.lock().unwrap().backup_to(&dest) {
                warn!(d.log, "daily backup failed: {e}");
            }
        }
    }
}

/// Wait until monitors and the window list stop changing (no fixed sleep):
/// three consecutive identical polls 500 ms apart, bounded by timeout.
pub fn wait_for_readiness(d: &Daemon, timeout: Duration) -> bool {
    let start = Instant::now();
    let mut last: Option<(String, usize)> = None;
    let mut stable = 0;
    while start.elapsed() < timeout {
        if d.shutdown.load(Ordering::SeqCst) {
            return false;
        }
        // Restoring behind a lock screen would move and activate windows
        // nobody can see; wait for the unlock.
        if d.is_locked() {
            stable = 0;
            std::thread::sleep(Duration::from_millis(500));
            continue;
        }
        let outs = d.outputs().ok();
        let n = d.windows().map(|w| w.len()).ok();
        if let (Some(o), Some(n)) = (outs, n) {
            if !o.is_empty() {
                let key = (fractile_core::output::topology_signature(&o), n);
                if last.as_ref() == Some(&key) {
                    stable += 1;
                    if stable >= 3 {
                        return true;
                    }
                } else {
                    stable = 0;
                    last = Some(key);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

/// Login flow: decide whether/how to restore according to policy.
pub fn login_flow(d: Arc<Daemon>) {
    let cfg = d.config();
    let key = d.session_key.clone();
    let store_prev = d.store.lock().unwrap().previous_session(&key).ok().flatten();
    // Interrupted restore in this same session (daemon crashed/restarted) → resume idempotently.
    if !d.new_login.load(Ordering::SeqCst) {
        let runs = d.store.lock().unwrap().interrupted_runs().unwrap_or_default();
        if let Some(run) = runs.first() {
            info!(d.log, "resuming interrupted restore {run}");
            if wait_for_readiness(&d, Duration::from_secs(cfg.session.readiness_timeout_s)) {
                let saved = d.store.lock().unwrap().meta_get(&format!("run_req:{run}")).ok().flatten();
                let req: RestoreRequest = saved.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
                if let Err(e) = retry_run(&d, run, &req, false) {
                    warn!(d.log, "resume failed: {}", e.message);
                }
            }
        }
        return;
    }
    let policy = cfg.session.policy;
    if matches!(policy, RestorePolicy::Off | RestorePolicy::Manual) {
        return;
    }
    if d.store.lock().unwrap().consecutive_failed_auto_runs() >= cfg.session.max_consecutive_failures {
        warn!(d.log, "automatic restore disabled after repeated failures");
        d.request_ui("notice", json!({"level": "warning", "message": "Automatic restore was paused after repeated failures. Open Session Restore settings to retry."}));
        return;
    }
    if !wait_for_readiness(&d, Duration::from_secs(cfg.session.readiness_timeout_s)) {
        warn!(d.log, "desktop did not settle within {}s; restoring anyway", cfg.session.readiness_timeout_s);
    }
    let unclean = store_prev.as_ref().map(|p| !p.clean).unwrap_or(false);
    let since = store_prev.as_ref().map(|p| p.started_at.clone());
    let pick = |modes: &[SnapshotMode]| -> Option<Snapshot> {
        let s = d.store.lock().unwrap().latest_good(Some(modes))?;
        match &since {
            Some(t) if s.created_at.as_str() < t.as_str() => None,
            _ => Some(s),
        }
    };
    let ask = |reason: &str, snap: Option<&Snapshot>| {
        d.request_ui("show_recovery", json!({"snapshot_id": snap.map(|s| s.snapshot_id.clone()), "reason": reason, "unclean": unclean}));
    };
    match policy {
        RestorePolicy::Named => {
            if let Some(name) = cfg.session.named_layout.clone() {
                if cfg.session.confirm_before_restore {
                    d.request_ui("show_recovery", json!({"layout": name, "reason": "named_layout"}));
                } else if let Err(e) = start_layout(&d, &name, ApplyMode::LaunchMissing, &RestoreRequest::default(), false) {
                    warn!(d.log, "login layout '{name}' failed: {}", e.message);
                }
            }
        }
        RestorePolicy::Ask => ask("ask_each_login", pick(&[SnapshotMode::Logout, SnapshotMode::Shutdown, SnapshotMode::Periodic]).as_ref()),
        RestorePolicy::LastClean | RestorePolicy::LastKnown => {
            let snap = if policy == RestorePolicy::LastClean && !unclean {
                pick(&[SnapshotMode::Logout, SnapshotMode::Shutdown]).or_else(|| pick(&[SnapshotMode::Periodic]))
            } else {
                pick(&[SnapshotMode::Logout, SnapshotMode::Shutdown, SnapshotMode::Periodic])
            };
            let Some(snap) = snap else {
                info!(d.log, "no snapshot from the previous session to restore");
                return;
            };
            let decision = if unclean {
                match (policy, cfg.session.power_loss_policy) {
                    (_, PowerLossPolicy::Skip) => "skip",
                    (RestorePolicy::LastClean, _) | (_, PowerLossPolicy::Ask) => "ask",
                    (_, PowerLossPolicy::Restore) => "restore",
                }
            } else if cfg.session.confirm_before_restore {
                "ask"
            } else {
                "restore"
            };
            info!(d.log, "login restore decision={decision} unclean={unclean} snapshot={}", snap.snapshot_id);
            match decision {
                "restore" => {
                    let _ = d.store.lock().unwrap().session_mark_restored(&key);
                    if let Err(e) = start_snapshot(&d, &snap.snapshot_id, &RestoreRequest::default(), "login", false) {
                        warn!(d.log, "login restore failed to start: {}", e.message);
                    }
                }
                "ask" => ask(if unclean { "unclean_shutdown" } else { "confirm" }, Some(&snap)),
                _ => {}
            }
        }
        _ => {}
    }
}

/// Capture at orderly end of session (SIGTERM / PrepareForShutdown).
pub fn capture_at_exit(d: &Daemon, mode: SnapshotMode) {
    let cfg = d.config();
    let auto = matches!(cfg.session.policy, RestorePolicy::LastClean | RestorePolicy::LastKnown | RestorePolicy::Ask);
    if auto && d.restore.lock().unwrap().is_none() && !(d.is_locked() && cfg.session.skip_capture_when_locked) {
        match capture_now(d, mode, None) {
            Ok(s) if !s.windows.is_empty() => match d.store.lock().unwrap().save_snapshot(&s, false) {
                Ok(()) => info!(d.log, "{mode:?} snapshot {} saved ({} windows)", s.snapshot_id, s.windows.len()),
                Err(e) => error!(d.log, "exit snapshot failed: {e}"),
            },
            Ok(_) => {}
            Err(e) => warn!(d.log, "exit capture failed: {}", e.message),
        }
    }
    let _ = d.store.lock().unwrap().session_end(&d.session_key, true);
}

/// Monitor hot-plug: optional restore of the last snapshot's layout for the
/// reconnected monitor (disabled by default) and profile auto-switch.
pub fn on_outputs_changed(d: &Arc<Daemon>) {
    let cfg = d.config();
    let outs = match d.outputs() {
        Ok(o) => o,
        Err(_) => return,
    };
    if cfg.auto_switch_profiles {
        let sig = fractile_core::output::topology_signature(&outs);
        if let Some(p) = cfg.profiles.iter().find(|p| p.topology.as_deref() == Some(sig.as_str())) {
            if cfg.active_profile.as_deref() != Some(p.name.as_str()) {
                let mut c = cfg.clone();
                c.active_profile = Some(p.name.clone());
                info!(d.log, "monitor topology matched profile '{}'", p.name);
                let _ = d.apply_config(c);
                if let Some(l) = p.login_layout.clone() {
                    let _ = start_layout(d, &l, ApplyMode::PositionExisting, &RestoreRequest { launch: false, ..Default::default() }, false);
                }
            }
        }
    }
    if cfg.session.restore_on_monitor_reconnect && d.restore.lock().unwrap().is_none() {
        let snap = d.store.lock().unwrap().latest_good(None);
        if let Some(s) = snap {
            let present: HashSet<&str> = outs.iter().map(|o| o.stable_id.as_str()).collect();
            let recorded: HashSet<&str> = s.outputs.iter().map(|o| o.stable_id.as_str()).collect();
            if recorded.is_subset(&present) && recorded.len() > 1 {
                info!(d.log, "all monitors of snapshot {} present again; repositioning windows", s.snapshot_id);
                let req = RestoreRequest { launch: false, ..Default::default() };
                let _ = start_snapshot(d, &s.snapshot_id, &req, "reconnect", false);
            }
        }
    }
}

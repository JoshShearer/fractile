//! Local JSON-lines RPC server on a private 0600 socket. Every connection is
//! checked with SO_PEERCRED (same uid only) and must start with `hello`.

use crate::daemon::{err, Daemon, Subscriber, R};
use crate::session::{self, RestoreRequest};
use fractile_core::config::Config;
use fractile_core::geometry::{selection_rect, Grid, Selection, SnapAction};
use fractile_core::layout::{ApplyMode, Layout};
use fractile_core::rules::{OutputSel, Rule};
use fractile_core::snapshot::{Snapshot, SnapshotMode};
use fractile_ipc::{peer_cred, read_line, write_msg, Request, Response, PROTOCOL_VERSION};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use std::io::BufReader;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::Duration;

struct Conn {
    id: u64,
    pid: i32,
    writer: Arc<Mutex<UnixStream>>,
    sub_id: Option<u64>,
    client: String,
}

pub fn serve(d: Arc<Daemon>, listener: UnixListener) {
    for stream in listener.incoming() {
        if d.shutdown.load(Ordering::SeqCst) {
            break;
        }
        let Ok(s) = stream else { continue };
        let cred = match peer_cred(&s) {
            Ok(c) => c,
            Err(_) => continue,
        };
        if cred.uid != fractile_ipc::current_uid() {
            warn!(d.log, "rejected connection from uid {} pid {}", cred.uid, cred.pid);
            continue;
        }
        let d = d.clone();
        std::thread::Builder::new()
            .name("ipc-conn".into())
            .spawn(move || handle(d, s, cred.pid))
            .ok();
    }
}

fn handle(d: Arc<Daemon>, s: UnixStream, pid: i32) {
    let _ = s.set_read_timeout(None);
    let Ok(w) = s.try_clone() else { return };
    static NEXT_CONN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    let mut conn = Conn { id: NEXT_CONN.fetch_add(1, Ordering::SeqCst), pid, writer: Arc::new(Mutex::new(w)), sub_id: None, client: String::new() };
    let mut reader = BufReader::new(s);
    let mut greeted = false;
    loop {
        let line = match read_line(&mut reader) {
            Ok(Some(l)) => l,
            Ok(None) => break,
            Err(e) => {
                let _ = send(&conn, Response { id: 0, result: None, error: Some(err("protocol", e.to_string())) });
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let req: Request = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                let _ = send(&conn, Response { id: 0, result: None, error: Some(err("protocol", format!("bad request: {e}"))) });
                continue;
            }
        };
        if !greeted && req.method != "hello" {
            let _ = send(&conn, Response { id: req.id, result: None, error: Some(err("protocol", "first message must be hello")) });
            break;
        }
        let res = if req.method == "hello" {
            greeted = true;
            hello(&d, &mut conn, &req.params)
        } else {
            dispatch(&d, &mut conn, &req.method, &req.params)
        };
        let resp = match res {
            Ok(v) => Response { id: req.id, result: Some(v), error: None },
            Err(e) => {
                crate::debug!(d.log, "request {} from {} failed: {} {}", req.method, conn.client, e.code, e.message);
                Response { id: req.id, result: None, error: Some(e) }
            }
        };
        if send(&conn, resp).is_err() {
            break;
        }
        if d.shutdown.load(Ordering::SeqCst) {
            break;
        }
    }
    if let Some(b) = crate::bridge::get() {
        if b.detach(conn.id) {
            warn!(d.log, "{} integration disconnected", b.kind);
            d.broadcast("capabilities_changed", json!({}));
        }
    }
    // An editor that exits while listening must not leave shortcuts off.
    if d.resume_hotkeys(Some(conn.id)) {
        warn!(d.log, "shortcut editor connection closed during a pause; global shortcuts restored");
    }
    if let Some(id) = conn.sub_id {
        d.subscribers.lock().unwrap().retain(|s| s.id != id);
        if conn.client == "ui" {
            // A UI that exits or crashes must not leave a compositor-drawn
            // preview behind.
            let _ = d.adapter().hide_overlay();
            d.panel.lock().unwrap().visible = false;
            // The last UI left (a tray-only UI with no tray, one that
            // yielded to another, a crash): the next request must start a
            // UI, not wait out the guard against starting two at once.
            if !d.ui_attached() {
                d.panel.lock().unwrap().ui_spawned_at = None;
            }
        }
    }
}

fn send(c: &Conn, r: Response) -> std::io::Result<()> {
    let mut w = c.writer.lock().unwrap();
    write_msg(&mut *w, &r)
}

fn hello(d: &Daemon, c: &mut Conn, p: &Value) -> R<Value> {
    let proto = p.get("protocol").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
    if proto != PROTOCOL_VERSION {
        return Err(err("protocol", format!("protocol {proto} not supported (daemon speaks {PROTOCOL_VERSION})")));
    }
    c.client = p.get("client").and_then(|v| v.as_str()).unwrap_or("unknown").chars().take(32).collect();
    let caps = d.caps();
    Ok(json!({"product": fractile_core::PRODUCT_NAME, "version": fractile_core::VERSION, "protocol": PROTOCOL_VERSION, "adapter": caps.adapter, "pid": std::process::id()}))
}

/// A window of the calling (registered) Fractile UI process, by title.
fn own_window_by_title(d: &Daemon, c: &Conn, title: &str) -> R<fractile_core::window::WindowInfo> {
    let pid = c.pid as u32;
    if !d.own_pids.lock().unwrap().contains(&pid) {
        return Err(err("denied", "only a registered Fractile UI may manage its windows"));
    }
    // A freshly mapped surface can take a moment to reach the compositor.
    for _ in 0..40 {
        let ws = d.adapter().list_windows().map_err(|e| err("adapter", e.to_string()))?;
        if let Some(w) = ws.into_iter().find(|w| w.pid == Some(pid) && w.title == title) {
            return Ok(w);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    Err(err("no_window", "own window not found"))
}

fn s<'a>(p: &'a Value, k: &str) -> Option<&'a str> {
    p.get(k).and_then(|v| v.as_str())
}

fn req_s<'a>(p: &'a Value, k: &str) -> R<&'a str> {
    s(p, k).ok_or_else(|| err("invalid", format!("missing parameter '{k}'")))
}

fn b(p: &Value, k: &str, default: bool) -> bool {
    p.get(k).and_then(|v| v.as_bool()).unwrap_or(default)
}

fn parse<T: DeserializeOwned>(p: &Value, k: &str) -> R<T> {
    let v = p.get(k).cloned().ok_or_else(|| err("invalid", format!("missing parameter '{k}'")))?;
    serde_json::from_value(v).map_err(|e| err("invalid", format!("bad '{k}': {e}")))
}

fn opt<T: DeserializeOwned>(p: &Value, k: &str) -> R<Option<T>> {
    match p.get(k) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => serde_json::from_value(v.clone()).map(Some).map_err(|e| err("invalid", format!("bad '{k}': {e}"))),
    }
}

fn restore_req(p: &Value) -> R<RestoreRequest> {
    Ok(opt::<RestoreRequest>(p, "options")?.unwrap_or_default())
}

fn merge(base: &mut Value, patch: &Value) {
    match (base, patch) {
        (Value::Object(b), Value::Object(p)) => {
            for (k, v) in p {
                merge(b.entry(k.clone()).or_insert(Value::Null), v);
            }
        }
        (b, p) => *b = p.clone(),
    }
}

fn mode_param(p: &Value) -> R<ApplyMode> {
    Ok(opt::<ApplyMode>(p, "mode")?.unwrap_or(ApplyMode::LaunchMissing))
}

fn percentile(v: &[u64], q: f64) -> Option<u64> {
    if v.is_empty() {
        return None;
    }
    let mut s = v.to_vec();
    s.sort_unstable();
    Some(s[((s.len() - 1) as f64 * q).round() as usize])
}

pub fn status(d: &Daemon) -> Value {
    let caps = d.caps();
    let cfg = d.config();
    let restore = d.restore.lock().unwrap().as_ref().map(|j| json!({"run_id": j.run_id, "progress": j.progress}));
    json!({
        "running": true,
        "version": fractile_core::VERSION,
        "pid": std::process::id(),
        "uptime_s": d.started.elapsed().as_secs(),
        "session_type": d.env.session_type,
        "desktop": d.env.current_desktop,
        "adapter": caps.adapter,
        "window_control": caps.supports(fractile_core::adapter::Operation::MoveResize),
        "global_shortcuts": caps.supports(fractile_core::adapter::Operation::RegisterHotkey),
        "tiling_manager": d.tiling,
        "restore_policy": cfg.session.policy,
        "restore": restore,
        "ui_attached": d.ui_attached(),
        "uis": d.ui_list(),
        "locked": d.is_locked(),
        "config_error": d.cfg_error.lock().unwrap().clone(),
        "hotkey_errors": d.hotkey_errors.lock().unwrap().clone(),
        "hotkeys_paused": d.hotkeys_paused(),
        "integration": crate::bridge::get().map(|b| b.status()).unwrap_or(Value::Null),
    })
}

pub fn diagnose(d: &Daemon) -> Value {
    let caps = d.caps();
    let outputs = d.outputs();
    let windows = d.windows();
    let store = d.store.lock().unwrap();
    let times = d.last_place_ms.lock().unwrap().clone();
    let mut warnings: Vec<String> = vec![];
    if !caps.supports(fractile_core::adapter::Operation::MoveResize) {
        warnings.push(format!("Window placement is unavailable on this desktop ({}).", caps.adapter));
    }
    if let Some(r) = crate::bridge::get().and_then(|b| b.status().get("rejected").and_then(|v| v.as_str()).map(String::from)) {
        warnings.push(format!("The compositor integration was refused: {r}"));
    }
    if let Some(t) = &d.tiling {
        warnings.push(format!("An automatic tiling manager is active ({t}); it may override floating placement. Fractile does not change its settings."));
    }
    if let Some(e) = d.cfg_error.lock().unwrap().as_ref() {
        warnings.push(format!("Config could not be loaded, defaults are in use: {e}"));
    }
    for (k, v) in d.hotkey_errors.lock().unwrap().iter() {
        warnings.push(format!("Shortcut {k} not active: {v}"));
    }
    json!({
        "product": fractile_core::PRODUCT_NAME,
        "version": fractile_core::VERSION,
        "environment": d.env,
        "capabilities": caps,
        "outputs": outputs.as_ref().ok(),
        "outputs_error": outputs.as_ref().err().map(|e| e.message.clone()),
        "topology_signature": outputs.as_ref().ok().filter(|o| !o.is_empty()).map(|o| fractile_core::output::topology_signature(o)),
        "window_count": windows.as_ref().map(|w| w.len()).ok(),
        "placeable_windows": windows.as_ref().map(|w| w.iter().filter(|w| w.is_placeable()).count()).ok(),
        "store": {"path": store.path(), "snapshots": store.list_snapshots(1000).map(|v| v.len()).unwrap_or(0), "layouts": store.list_layouts().map(|v| v.len()).unwrap_or(0), "interrupted_runs": store.interrupted_runs().unwrap_or_default()},
        "session": {"key": d.session_key, "new_login": d.new_login.load(Ordering::SeqCst), "previous": store.previous_session(&d.session_key).ok().flatten()},
        "placement_latency_ms": {"samples": times.len(), "p50": percentile(&times, 0.5), "p95": percentile(&times, 0.95)},
        "autostart": crate::autostart::status(),
        "paths": {"config": d.cfg_path, "socket": fractile_core::paths::socket_path(), "log": fractile_core::paths::log_file()},
        "warnings": warnings,
        "telemetry": "none: Fractile sends no data anywhere",
    })
}

fn dispatch(d: &Arc<Daemon>, c: &mut Conn, method: &str, p: &Value) -> R<Value> {
    match method {
        "status" => Ok(status(d)),
        "capabilities" => Ok(json!(d.caps())),
        "diagnose" => Ok(diagnose(d)),
        "list_windows" => {
            let all = b(p, "all", false);
            let ws = d.windows()?;
            Ok(json!(ws.into_iter().filter(|w| all || (w.is_placeable() && !w.is_own)).collect::<Vec<_>>()))
        }
        "list_outputs" => Ok(json!(d.outputs()?)),
        "list_workspaces" => Ok(json!(d.adapter().get_workspaces().map_err(|e| err("adapter", e.to_string()))?)),
        "active_window" => Ok(json!(d.adapter().get_active_window().map_err(|e| err("adapter", e.to_string()))?)),

        // ---- panel / UI ----
        "toggle_panel" => Ok(d.toggle_panel(p.get("timestamp").and_then(|v| v.as_u64()).map(|v| v as u32))),
        "show_panel" => Ok(d.show_panel(p.get("timestamp").and_then(|v| v.as_u64()).map(|v| v as u32))),
        "hide_panel" => {
            d.panel.lock().unwrap().visible = false;
            d.broadcast("hide_panel", json!({}));
            Ok(json!({"visible": false}))
        }
        "panel_state" => Ok(d.panel_payload(None)),
        "panel_visible" => {
            d.panel.lock().unwrap().visible = b(p, "visible", false);
            Ok(json!({}))
        }
        "ui_register" => {
            let windows: Vec<String> = opt(p, "windows")?.unwrap_or_default();
            d.register_ui(windows, Some(c.pid as u32));
            Ok(json!({}))
        }
        "activate_window" => {
            // Return focus to the placement target after the panel closes.
            let wid = req_s(p, "window")?;
            let ts = d.panel.lock().unwrap().last_timestamp;
            d.adapter().activate(wid, ts).map_err(|e| err("adapter", e.to_string()))?;
            Ok(json!({}))
        }
        "place_own_window" => {
            // Wayland clients cannot position their own surfaces; the UI asks
            // the compositor integration. Only windows of a registered UI
            // process are eligible.
            let title = req_s(p, "title")?;
            let rect: fractile_core::geometry::Rect = parse(p, "rect")?;
            let w = own_window_by_title(d, c, title)?;
            Ok(json!(d.adapter().move_resize(&w.id, rect).map_err(|e| err("adapter", e.to_string()))?))
        }
        "focus_own_window" => {
            if let Some(wid) = s(p, "window") {
                d.focus_own_window(wid);
                return Ok(json!({}));
            }
            // Wayland: the UI knows only its window title.
            let w = own_window_by_title(d, c, req_s(p, "title")?)?;
            d.focus_own_window(&w.id);
            Ok(json!({"window": w.id}))
        }
        "overlay_show" | "overlay_hide" => {
            if !d.own_pids.lock().unwrap().contains(&(c.pid as u32)) {
                return Err(err("denied", "only a registered Fractile UI may draw the preview"));
            }
            let a = d.adapter();
            let r = if method == "overlay_show" {
                let rect: fractile_core::geometry::Rect = parse(p, "rect")?;
                let pids = d.own_pids.lock().unwrap().clone();
                a.show_overlay(rect, s(p, "label").unwrap_or(""), &pids)
            } else {
                a.hide_overlay()
            };
            r.map_err(|e| err("adapter", e.to_string()))?;
            Ok(json!({}))
        }
        "show_settings" | "show_dashboard" | "show_palette" | "show_recovery" => {
            d.request_ui(method, p.clone());
            Ok(json!({}))
        }
        "bridge_attach" => {
            let b = crate::bridge::get().ok_or_else(|| err("unsupported", "this session does not use a compositor integration"))?;
            b.attach(c.id, c.pid, crate::bridge::Transport::Socket(c.writer.clone()), p).map_err(|e| err("denied", e))?;
            info!(d.log, "{} integration attached from pid {}", b.kind, c.pid);
            // Hotkey grabs are calls over this same connection, so they must
            // not run on this reader thread.
            let d2 = d.clone();
            std::thread::spawn(move || {
                d2.register_hotkeys();
                d2.broadcast("capabilities_changed", json!({}));
            });
            Ok(json!({"api": fractile_core::adapter::ADAPTER_API_VERSION}))
        }
        "bridge_result" | "bridge_event" => {
            let b = crate::bridge::get().filter(|b| b.is_bridge_conn(c.id)).ok_or_else(|| err("denied", "not the attached integration"))?;
            if method == "bridge_result" {
                b.deliver_result(p);
            } else {
                b.deliver_event(p).map_err(|e| err("invalid", e))?;
            }
            Ok(json!({}))
        }
        "bridge_status" => Ok(crate::bridge::get().map(|b| b.status()).unwrap_or(Value::Null)),
        "subscribe" => {
            let kind: String = s(p, "kind").unwrap_or("client").chars().take(16).collect();
            if c.sub_id.is_none() {
                let id = d.next_sub.fetch_add(1, Ordering::SeqCst);
                d.subscribers.lock().unwrap().push(Subscriber { id, kind: kind.clone(), pid: c.pid, background: kind == "ui" && p.get("background").and_then(|b| b.as_bool()).unwrap_or(false), writer: c.writer.clone() });
                c.sub_id = Some(id);
                c.client = kind.clone();
            }
            if kind == "ui" {
                // UIs already attached learn of this one: an idle UI started
                // only for the tray then exits, so one UI answers requests.
                let background = p.get("background").and_then(|b| b.as_bool()).unwrap_or(false);
                d.broadcast("ui_joined", json!({"pid": c.pid, "background": background}));
                // A freshly attached UI shows nothing until the pending
                // request (if any) is delivered, so the panel flag must
                // reflect that or the next toggle would "hide" a panel
                // that never appeared.
                {
                    let mut pn = d.panel.lock().unwrap();
                    if pn.pending_show.as_ref().map(|v| v["event"] != "show_panel").unwrap_or(true) {
                        pn.visible = false;
                    }
                }
                let d2 = d.clone();
                // Deliver after this response is written.
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(50));
                    d2.flush_pending_ui();
                });
            }
            Ok(json!({"subscribed": true}))
        }

        // ---- placement ----
        "compute_rect" => {
            let grid: Grid = parse(p, "grid")?;
            let sel: Selection = parse(p, "selection")?;
            let outs = d.outputs()?;
            let o = match s(p, "output") {
                Some(id) => outs.iter().find(|o| o.stable_id == id).cloned(),
                None => outs.first().cloned(),
            }
            .ok_or_else(|| err("no_output", "no such output"))?;
            if !grid.is_valid_effective() || !sel.is_valid_for(grid) {
                return Err(err("invalid", "invalid grid selection"));
            }
            Ok(json!(selection_rect(o.work_area, grid, sel, &d.config().effective_spacing())))
        }
        "place" => {
            let wid = d.resolve_window(s(p, "window"))?;
            if let Some(r) = opt::<fractile_core::geometry::Rect>(p, "rect")? {
                return Ok(json!(d.place_rect(&wid, r, true)?));
            }
            let grid: Grid = parse(p, "grid")?;
            let sel: Selection = parse(p, "selection")?;
            let out = d.place_selection(&wid, s(p, "output"), grid, sel)?;
            if b(p, "from_panel", false) {
                d.broadcast("placed", json!({"window": wid}));
            }
            Ok(json!(out))
        }
        "snap" => {
            let wid = d.resolve_window(s(p, "window"))?;
            let a: SnapAction = parse(p, "action")?;
            let o: OutputSel = opt(p, "output")?.unwrap_or_default();
            Ok(json!(d.snap(&wid, a, &o)?))
        }
        "move_to_output" => {
            let wid = d.resolve_window(s(p, "window"))?;
            let step = p.get("step").and_then(|v| v.as_i64()).unwrap_or(1) as i32;
            Ok(json!(d.move_to_output_step(&wid, step)?))
        }
        "move_to_workspace" => {
            let wid = d.resolve_window(s(p, "window"))?;
            let ws = p.get("workspace").and_then(|v| v.as_u64()).ok_or_else(|| err("invalid", "missing workspace"))? as u32;
            d.adapter().move_to_workspace(&wid, ws).map_err(|e| err("adapter", e.to_string()))?;
            Ok(json!({"ok": true}))
        }
        "set_state" => {
            let wid = d.resolve_window(s(p, "window"))?;
            let st = parse(p, "state")?;
            d.adapter().set_state(&wid, &st).map_err(|e| err("adapter", e.to_string()))?;
            Ok(json!({"ok": true}))
        }
        "undo" => d.undo_last(),
        "run_shortcut" => {
            let id = req_s(p, "id")?;
            let cfg = d.config();
            let sc = cfg.shortcuts.iter().find(|x| x.id == id).ok_or_else(|| err("not_found", format!("no shortcut '{id}'")))?;
            d.run_action(&sc.id, &sc.action, sc.cycle_monitors, None)
        }

        // ---- config ----
        "get_config" => Ok(json!({"config": d.config(), "path": d.cfg_path, "error": d.cfg_error.lock().unwrap().clone()})),
        "set_config" => {
            let new: Config = if let Some(full) = p.get("config") {
                serde_json::from_value(full.clone()).map_err(|e| err("invalid_config", e.to_string()))?
            } else {
                let mut cur = serde_json::to_value(d.config()).unwrap();
                merge(&mut cur, p.get("patch").ok_or_else(|| err("invalid", "need config or patch"))?);
                serde_json::from_value(cur).map_err(|e| err("invalid_config", e.to_string()))?
            };
            d.apply_config(new)?;
            Ok(json!({"ok": true, "hotkey_errors": d.hotkey_errors.lock().unwrap().clone()}))
        }
        "reset_config" => {
            let section = s(p, "section");
            let mut cfg = d.config();
            let def = Config::default();
            match section {
                None | Some("all") => cfg = def,
                Some("general") => cfg.general = def.general,
                Some("appearance") => cfg.appearance = def.appearance,
                Some("placement") => cfg.placement = def.placement,
                Some("shortcuts") => cfg.shortcuts = def.shortcuts,
                Some("session") => cfg.session = def.session,
                Some("rules") => cfg.rules = def.rules,
                Some(x) => return Err(err("invalid", format!("unknown section '{x}'"))),
            }
            d.apply_config(cfg)?;
            Ok(json!({"ok": true}))
        }
        // A shortcut editor is listening: pause global shortcuts so the
        // compositor passes the keys to it (Wayland has no client grab).
        "hotkeys_pause" => {
            let secs = p.get("seconds").and_then(|v| v.as_u64()).unwrap_or(60).clamp(1, 300);
            let (_, desktop) = d.pause_hotkeys(c.id, secs);
            Ok(json!({"paused": true, "seconds": secs, "desktop_held": desktop}))
        }
        "hotkeys_resume" => Ok(json!({"resumed": d.resume_hotkeys(Some(c.id))})),
        "shortcut_check" => Ok(d.probe_accelerator(req_s(p, "accelerator")?, s(p, "own_id"))),
        "autostart_status" => Ok(crate::autostart::status()),
        "autostart_set" => {
            let en = b(p, "enabled", true);
            let mut cfg = d.config();
            cfg.general.autostart = en;
            d.apply_config(cfg)?;
            Ok(crate::autostart::status())
        }

        // ---- snapshots ----
        "snapshot_save" => {
            let name = s(p, "name").map(|x| x.chars().take(128).collect());
            let id = session::save_snapshot(d, name, SnapshotMode::Manual)?;
            Ok(json!({"snapshot_id": id}))
        }
        "snapshot_list" => {
            let limit = p.get("limit").and_then(|v| v.as_u64()).unwrap_or(50) as usize;
            Ok(json!(d.store.lock().unwrap().list_snapshots(limit).map_err(|e| err("store", e))?))
        }
        "snapshot_get" => {
            let st = d.store.lock().unwrap();
            let id = st.resolve_snapshot_ref(req_s(p, "id")?).map_err(|e| err("not_found", e))?;
            let snap = st.get_snapshot(&id).map_err(|e| err("corrupt", e))?;
            Ok(json!(if b(p, "redacted", false) { snap.redacted(true) } else { snap }))
        }
        "snapshot_delete" => {
            let st = d.store.lock().unwrap();
            let id = st.resolve_snapshot_ref(req_s(p, "id")?).map_err(|e| err("not_found", e))?;
            Ok(json!({"deleted": st.delete_snapshot(&id).map_err(|e| err("store", e))?}))
        }
        "snapshot_pin" => {
            let st = d.store.lock().unwrap();
            let id = st.resolve_snapshot_ref(req_s(p, "id")?).map_err(|e| err("not_found", e))?;
            st.set_pinned(&id, b(p, "pinned", true)).map_err(|e| err("store", e))?;
            Ok(json!({"ok": true}))
        }
        "snapshot_import" => {
            let mut snap: Snapshot = parse(p, "snapshot")?;
            // Imports are never trusted: launch policies become Ask.
            snap.sanitize_for_import(false);
            snap.validate().map_err(|e| err("invalid", e))?;
            let mut st = d.store.lock().unwrap();
            if st.get_snapshot(&snap.snapshot_id).is_ok() {
                snap.snapshot_id = uuid::Uuid::new_v4().to_string();
            }
            st.save_snapshot(&snap, true).map_err(|e| err("store", e))?;
            info!(d.log, "imported snapshot {} ({} windows)", snap.snapshot_id, snap.windows.len());
            Ok(json!({"snapshot_id": snap.snapshot_id}))
        }

        // ---- restore ----
        "restore_plan" => {
            let r = restore_req(p)?;
            match (s(p, "snapshot"), s(p, "layout")) {
                (_, Some(l)) => session::dry_run_layout(d, l, mode_param(p)?, &r),
                (sn, None) => session::dry_run_snapshot(d, sn.unwrap_or("latest"), &r),
            }
        }
        "restore_start" => {
            let r = restore_req(p)?;
            let wait = b(p, "wait", false);
            match (s(p, "snapshot"), s(p, "layout")) {
                (_, Some(l)) => session::start_layout(d, l, mode_param(p)?, &r, wait),
                (sn, None) => session::start_snapshot(d, sn.unwrap_or("latest"), &r, "manual", wait),
            }
        }
        "restore_status" => {
            let cur = d.restore.lock().unwrap().as_ref().map(|j| json!({"run_id": j.run_id, "progress": j.progress}));
            let runs = d.store.lock().unwrap().recent_runs(10).unwrap_or_default();
            Ok(json!({"running": cur, "last_report": d.last_report.lock().unwrap().clone(), "recent_runs": runs}))
        }
        "restore_run" => {
            let id = req_s(p, "run_id")?;
            let st = d.store.lock().unwrap();
            let run = st.run_get(id).map_err(|e| err("store", e))?.ok_or_else(|| err("not_found", "no such run"))?;
            let items: Vec<Value> = st
                .items(id)
                .unwrap_or_default()
                .into_iter()
                .map(|(rid, status, wid, launched, label, msg)| json!({"record_id": rid, "status": status, "window_id": wid, "launched_at": launched, "label": label, "message": msg}))
                .collect();
            Ok(json!({"source": run.0, "source_id": run.1, "state": run.2, "summary": run.3, "items": items}))
        }
        "restore_cancel" => session::cancel(d),
        "restore_retry" => session::retry_run(d, req_s(p, "run_id")?, &restore_req(p)?, b(p, "wait", false)),
        "restore_undo" => session::undo_restore(d, b(p, "wait", false)),

        // ---- layouts ----
        "layout_list" => Ok(json!(d.store.lock().unwrap().list_layouts().map_err(|e| err("store", e))?)),
        "layout_get" => Ok(json!(d.store.lock().unwrap().get_layout(req_s(p, "name")?).map_err(|e| err("not_found", e))?)),
        "layout_save" => {
            let mut l: Layout = parse(p, "layout")?;
            if b(p, "import", false) {
                l.sanitize_for_import(false);
            }
            l.updated_at = chrono::Local::now().to_rfc3339();
            if l.created_at.is_empty() {
                l.created_at = l.updated_at.clone();
            }
            l.validate().map_err(|e| err("invalid", e))?;
            d.store.lock().unwrap().save_layout(&l).map_err(|e| err("store", e))?;
            d.broadcast("layouts_changed", json!({}));
            Ok(json!({"ok": true, "name": l.name}))
        }
        "layout_from_windows" => {
            let name = req_s(p, "name")?;
            let l = session::layout_from_windows(d, name, b(p, "launch", true))?;
            if b(p, "save", true) {
                l.validate().map_err(|e| err("invalid", e))?;
                d.store.lock().unwrap().save_layout(&l).map_err(|e| err("store", e))?;
                d.broadcast("layouts_changed", json!({}));
            }
            Ok(json!(l))
        }
        "layout_delete" => {
            let r = d.store.lock().unwrap().delete_layout(req_s(p, "name")?).map_err(|e| err("store", e))?;
            d.broadcast("layouts_changed", json!({}));
            Ok(json!({"deleted": r}))
        }
        "layout_preview" => {
            let l: Layout = match p.get("layout") {
                Some(v) => serde_json::from_value(v.clone()).map_err(|e| err("invalid", e.to_string()))?,
                None => d.store.lock().unwrap().get_layout(req_s(p, "name")?).map_err(|e| err("not_found", e))?,
            };
            session::layout_preview(d, &l)
        }
        "layout_apply" => session::start_layout(d, req_s(p, "name")?, mode_param(p)?, &restore_req(p)?, b(p, "wait", false)),

        // ---- rules ----
        "rules_test" => {
            let rule: Rule = parse(p, "rule")?;
            rule.validate().map_err(|e| err("invalid", e))?;
            let cfg = d.config();
            let ws = d.windows()?;
            let ords = fractile_core::identity::instance_ordinals(&ws);
            let res: Vec<Value> = ws
                .iter()
                .filter(|w| w.is_placeable() && !w.is_own)
                .map(|w| {
                    let e = rule.matcher.explain(w, ords.get(&w.id).copied().unwrap_or(0), cfg.allow_title_regex);
                    json!({"window": w.id, "app": w.app_key(), "explanation": e})
                })
                .collect();
            Ok(json!(res))
        }
        "rules_apply_now" => {
            let cfg = d.config();
            let ws = d.windows()?;
            let ords = fractile_core::identity::instance_ordinals(&ws);
            let only: Option<String> = s(p, "rule").map(|x| x.to_string());
            let mut out = vec![];
            for w in ws.iter().filter(|w| w.is_placeable() && !w.is_own) {
                let ord = ords.get(&w.id).copied().unwrap_or(0);
                let rules: Vec<Rule> = cfg.rules.iter().filter(|r| only.as_deref().map(|o| o == r.id).unwrap_or(true)).cloned().collect();
                if let Some((rule, _)) = fractile_core::rules::first_match(&rules, w, ord, cfg.allow_title_regex, None) {
                    out.push(match d.apply_rule(rule, w) {
                        Ok(v) => v,
                        Err(e) => json!({"rule": rule.name, "window": w.id, "error": e.message}),
                    });
                }
            }
            Ok(json!(out))
        }

        // ---- apps ----
        "apps_list" => {
            let apps = d.apps.lock().unwrap();
            let cfg = d.config();
            let mut v: Vec<Value> = apps
                .entries
                .values()
                .filter(|e| !e.no_display && !e.hidden && e.kind == "Application")
                .map(|e| {
                    json!({
                        "id": e.id, "name": e.name, "icon": e.icon, "terminal": e.terminal,
                        "policy": cfg.apps.get(&e.id),
                        // effective value (the user's choice, else the built-in list)
                        "manages_session": cfg.manages_session(&e.id),
                        "manages_session_builtin": fractile_core::config::builtin_manages_session(&e.id),
                    })
                })
                .collect();
            v.sort_by(|a, b| a["name"].as_str().unwrap_or("").to_lowercase().cmp(&b["name"].as_str().unwrap_or("").to_lowercase()));
            Ok(json!(v))
        }
        "app_policy_set" => {
            let id = req_s(p, "desktop_id")?;
            if !fractile_core::snapshot::is_safe_desktop_id(id) {
                return Err(err("invalid", "bad desktop id"));
            }
            let mut cfg = d.config();
            match opt::<fractile_core::config::AppPolicy>(p, "policy")? {
                Some(pol) => {
                    cfg.apps.insert(id.to_string(), pol);
                }
                None => {
                    cfg.apps.remove(id);
                }
            }
            d.apply_config(cfg)?;
            Ok(json!({"ok": true}))
        }

        // ---- misc ----
        "logs" => {
            let n = p.get("lines").and_then(|v| v.as_u64()).unwrap_or(200).min(5000) as usize;
            Ok(json!(d.log.recent(n, b(p, "redacted", true))))
        }
        "quit" => {
            info!(d.log, "quit requested over IPC by pid {}", c.pid);
            crate::request_shutdown(d);
            Ok(json!({"ok": true}))
        }
        _ => Err(err("unknown_method", format!("unknown method '{method}'"))),
    }
}

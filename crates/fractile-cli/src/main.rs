//! `fractile`: command-line control of the Fractile service.
//!
//! Exit codes:
//!   0 success
//!   1 operation failed
//!   2 usage error
//!   3 service not running
//!   4 not supported on this desktop
//!   5 not found (window, output, snapshot, layout)
//!   6 restore finished incomplete (some items not restored)
//!   7 invalid input or configuration

use fractile_ipc::{Client, RpcError};
use serde_json::{json, Value};
use std::process::exit;

const USAGE: &str = "fractile - grid window placement and session restore

USAGE: fractile [--json] <command> [options]

Service
  status                          service, desktop and capability summary
  diagnose                        full diagnostic report (paste into bug reports)
  logs [--redacted] [--lines N]   recent service log lines (--redacted hides window titles)
  quit                            stop the service

Windows
  list-windows [--all]            windows Fractile can place (--all: every window)
  list-outputs                    monitors with stable ids and work areas
  place --grid CxR --cells C0,R0,C1,R1 [--window ID] [--output ID]
                                  place a window on a grid selection (0-based, inclusive)
  snap ACTION [--window ID]       left-half, right-half, top-half, bottom-half,
                                  left-third, center-third, right-third, left-two-thirds,
                                  right-two-thirds, top-left-quarter, top-right-quarter,
                                  bottom-left-quarter, bottom-right-quarter, center, maximize
  move-output next|prev [--window ID]
  undo                            undo the last placement
  panel [toggle|show|hide]        the placement panel
  settings | dashboard | palette  open UI pages

Session
  save [--name NAME]              snapshot the current windows
  restore --dry-run [--snapshot ID|latest] [--layout NAME]
                                  show what a restore would do, change nothing
  restore [--snapshot ID|latest] [--layout NAME] [--approve-all] [--approve RECORD]...
          [--no-launch] [--no-wait]
                                  restore windows; missing apps are only launched when approved
  restore-status | cancel | retry RUN_ID | undo-restore
  list-snapshots [--limit N]
  snapshot export ID [FILE] [--redacted] | snapshot import FILE
  snapshot delete ID | snapshot pin ID | snapshot unpin ID

Layouts
  list-layouts
  layout save NAME [--no-launch]  save current windows as a named layout
  layout apply NAME [--mode position_existing|launch_missing|exact_n] [--dry-run] [--approve-all]
  layout show NAME | layout delete NAME
  layout export NAME [FILE] | layout import FILE

Configuration
  config path | config get [KEY.PATH] | config set KEY.PATH VALUE | config reset [SECTION]
  shortcut-check ACCELERATOR      e.g. \"Ctrl+Alt+G\"
  autostart [on|off|status]
  list-apps                       installed applications and launch policies
  version

Fractile restores window placement. It cannot restore unsaved document
contents unless the application itself restores them.";

struct Args {
    v: Vec<String>,
    json: bool,
}

impl Args {
    fn flag(&mut self, f: &str) -> bool {
        if let Some(i) = self.v.iter().position(|a| a == f) {
            self.v.remove(i);
            true
        } else {
            false
        }
    }
    fn opt(&mut self, f: &str) -> Option<String> {
        let i = self.v.iter().position(|a| a == f)?;
        if i + 1 >= self.v.len() {
            usage_err(&format!("{f} needs a value"));
        }
        let val = self.v.remove(i + 1);
        self.v.remove(i);
        Some(val)
    }
    fn opts(&mut self, f: &str) -> Vec<String> {
        let mut out = vec![];
        while let Some(v) = self.opt(f) {
            out.push(v);
        }
        out
    }
    fn pos(&mut self) -> Option<String> {
        if self.v.is_empty() {
            None
        } else {
            Some(self.v.remove(0))
        }
    }
    fn finish(&self) {
        if let Some(x) = self.v.first() {
            usage_err(&format!("unexpected argument '{x}'"));
        }
    }
}

fn usage_err(m: &str) -> ! {
    eprintln!("fractile: {m}\nRun 'fractile help' for usage.");
    exit(2)
}

fn code_for(e: &RpcError) -> i32 {
    match e.code.as_str() {
        "not_running" => 3,
        "unsupported" => 4,
        "not_found" | "no_window" | "no_output" | "empty" => 5,
        c if c.starts_with("invalid") || c == "ineligible" || c == "constraints" || c == "protocol" => 7,
        _ => 1,
    }
}

fn die(e: RpcError) -> ! {
    eprintln!("fractile: {}", e.message);
    if e.code == "not_running" {
        eprintln!("Start it with: systemctl --user start fractile.service   (or run: fractiled &)");
    }
    exit(code_for(&e))
}

fn connect() -> Client {
    Client::connect(&fractile_core::paths::socket_path(), "cli").unwrap_or_else(|e| die(e))
}

fn call(c: &mut Client, m: &str, p: Value) -> Value {
    c.call(m, p).unwrap_or_else(|e| die(e))
}

fn pretty(v: &Value) {
    println!("{}", serde_json::to_string_pretty(v).unwrap());
}

fn sv(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "-".into(),
        other => other.to_string(),
    }
}

fn rect(v: &Value) -> String {
    if v.is_null() {
        return "-".into();
    }
    format!("{}x{}+{}+{}", sv(&v["w"]), sv(&v["h"]), sv(&v["x"]), sv(&v["y"]))
}

fn parse_grid(s: &str) -> Value {
    let (c, r) = s.split_once(['x', 'X']).unwrap_or_else(|| usage_err("--grid must look like 6x4"));
    let c: u32 = c.parse().unwrap_or_else(|_| usage_err("bad grid columns"));
    let r: u32 = r.parse().unwrap_or_else(|_| usage_err("bad grid rows"));
    json!({"cols": c, "rows": r})
}

fn parse_cells(s: &str) -> Value {
    let n: Vec<u32> = s.split(',').map(|x| x.trim().parse().unwrap_or_else(|_| usage_err("--cells must be C0,R0,C1,R1"))).collect();
    if n.len() != 4 {
        usage_err("--cells must be C0,R0,C1,R1");
    }
    json!({"col0": n[0].min(n[2]), "row0": n[1].min(n[3]), "col1": n[0].max(n[2]), "row1": n[1].max(n[3])})
}

fn read_file(p: &str) -> Value {
    let s = if p == "-" {
        let mut b = String::new();
        std::io::Read::read_to_string(&mut std::io::stdin(), &mut b).unwrap_or_else(|e| die(RpcError::new("io", e.to_string())));
        b
    } else {
        std::fs::read_to_string(p).unwrap_or_else(|e| die(RpcError::new("not_found", format!("{p}: {e}"))))
    };
    if s.len() > (fractile_ipc::MAX_LINE - 4096) {
        die(RpcError::new("invalid", "file too large"));
    }
    serde_json::from_str(&s).unwrap_or_else(|e| die(RpcError::new("invalid", format!("{p} is not valid JSON: {e}"))))
}

fn write_out(file: Option<String>, v: &Value) {
    let s = serde_json::to_string_pretty(v).unwrap() + "\n";
    match file {
        Some(f) if f != "-" => {
            std::fs::write(&f, s).unwrap_or_else(|e| die(RpcError::new("io", format!("{f}: {e}"))));
            eprintln!("wrote {f}");
        }
        _ => print!("{s}"),
    }
}

fn print_outcome(v: &Value) {
    let ok = v["within_tolerance"].as_bool().unwrap_or(false);
    println!(
        "{} {} requested {} observed {} (error {}px, {} attempt(s)){}",
        if ok { "placed" } else { "placed approximately" },
        sv(&v["window_id"]),
        rect(&v["requested"]),
        rect(&v["observed"]),
        sv(&v["max_error_px"]),
        sv(&v["attempts"]),
        v["note"].as_str().filter(|s| !s.is_empty()).map(|n| format!(" note: {n}")).unwrap_or_default()
    );
}

fn print_plan(v: &Value) {
    if let Some(s) = v.get("snapshot") {
        println!("Snapshot {} ({}, {} windows, {})", sv(&s["id"]), sv(&s["mode"]), sv(&s["windows"]), sv(&s["created_at"]));
    }
    println!("{:<18} {:<28} {:<14} {}", "ACTION", "WINDOW", "CONTENT", "DETAIL");
    for i in v["items"].as_array().cloned().unwrap_or_default() {
        let content = if i["restore_content"] == "application_managed" { "app-managed" } else { "frame only" };
        println!("{:<18} {:<28} {:<14} {}", sv(&i["action"]), sv(&i["label"]).chars().take(28).collect::<String>(), content, sv(&i["reason"]));
    }
    for w in v["warnings"].as_array().cloned().unwrap_or_default() {
        println!("warning: {}", sv(&w));
    }
    if let Some(m) = v["output_mapping"].as_object() {
        for (k, val) in m {
            println!("monitor {k} -> {}", sv(val));
        }
    }
    println!("Counts: {}", v["counts"]);
    if let Some(l) = v["limitations"].as_str() {
        println!("Note: {l}");
    }
    println!("Dry run: nothing was changed.");
}

fn print_report(v: &Value) -> i32 {
    let state = sv(&v["state"]);
    let rep = &v["report"];
    println!("Restore {} {state}", sv(&v["run_id"]));
    for i in rep["items"].as_array().cloned().unwrap_or_default() {
        println!("  {:<20} {:<28} {}", sv(&i["status"]), sv(&i["label"]).chars().take(28).collect::<String>(), i["message"].as_str().unwrap_or(""));
    }
    for w in rep["warnings"].as_array().cloned().unwrap_or_default() {
        println!("warning: {}", sv(&w));
    }
    println!("{} launch(es), {} ms", sv(&rep["launches"]), sv(&rep["duration_ms"]));
    match state.as_str() {
        "completed" => 0,
        "partial" | "cancelled" => 6,
        _ => 1,
    }
}

fn set_path(root: &mut Value, path: &str, val: Value) {
    let mut cur = root;
    let parts: Vec<&str> = path.split('.').collect();
    for (i, p) in parts.iter().enumerate() {
        if i == parts.len() - 1 {
            cur[*p] = val;
            return;
        }
        if !cur[*p].is_object() {
            cur[*p] = json!({});
        }
        cur = &mut cur[*p];
    }
}

fn restore_options(a: &mut Args) -> Value {
    let approved = a.opts("--approve");
    json!({"approve_all": a.flag("--approve-all"), "approved": approved, "launch": !a.flag("--no-launch")})
}

fn main() {
    let mut a = Args { v: std::env::args().skip(1).collect(), json: false };
    a.json = a.flag("--json");
    let cmd = a.pos().unwrap_or_else(|| {
        println!("{USAGE}");
        exit(2)
    });
    let json_out = a.json;
    let code = match cmd.as_str() {
        "help" | "--help" | "-h" => {
            println!("{USAGE}");
            0
        }
        "version" | "--version" | "-V" => {
            println!("fractile {}", fractile_core::VERSION);
            0
        }
        "status" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "status", json!({}));
            if json_out {
                pretty(&v);
            } else {
                println!("Fractile {} running (pid {}, up {}s)", sv(&v["version"]), sv(&v["pid"]), sv(&v["uptime_s"]));
                println!("Session:          {} / {}", sv(&v["session_type"]), sv(&v["desktop"]));
                println!("Adapter:          {}", sv(&v["adapter"]));
                println!("Window control:   {}", if v["window_control"].as_bool() == Some(true) { "available" } else { "NOT AVAILABLE on this desktop (run 'fractile diagnose')" });
                println!("Global shortcuts: {}", if v["global_shortcuts"].as_bool() == Some(true) { "available" } else { "not available" });
                if !v["tiling_manager"].is_null() {
                    println!("Tiling manager:   {} (may override placement)", sv(&v["tiling_manager"]));
                }
                println!("Restore policy:   {}", sv(&v["restore_policy"]));
                if !v["restore"].is_null() {
                    println!("Restore running:  {}", sv(&v["restore"]["run_id"]));
                }
                if let Some(e) = v["config_error"].as_str() {
                    println!("Config error:     {e}");
                }
                if let Some(m) = v["hotkey_errors"].as_object() {
                    for (k, e) in m {
                        println!("Shortcut issue:   {k}: {}", sv(e));
                    }
                }
            }
            0
        }
        "diagnose" => {
            a.finish();
            let mut c = match Client::connect(&fractile_core::paths::socket_path(), "cli") {
                Ok(c) => c,
                Err(e) => {
                    // Still useful without a running service.
                    eprintln!("fractile: service not running ({}); local detection only:", e.message);
                    let st = std::process::Command::new(std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.join("fractiled"))).filter(|p| p.exists()).unwrap_or_else(|| "fractiled".into())).arg("--check").status();
                    exit(if st.map(|s| s.success()).unwrap_or(false) { 3 } else { 1 });
                }
            };
            let v = call(&mut c, "diagnose", json!({}));
            if json_out {
                pretty(&v);
            } else {
                println!("Fractile {} diagnostics", sv(&v["version"]));
                let e = &v["environment"];
                println!("OS: {}\nSession: {} desktop={} gnome-shell={}", sv(&e["os_pretty_name"]), sv(&e["session_type"]), sv(&e["current_desktop"]), sv(&e["gnome_shell_version"]));
                let caps = &v["capabilities"];
                println!("Adapter: {} {} (compositor {})", sv(&caps["adapter"]), sv(&caps["adapter_version"]), sv(&caps["compositor"]));
                if let Some(ops) = caps["operations"].as_object() {
                    for (op, lvl) in ops {
                        println!("  {:<22} {}", op, if lvl.is_string() { sv(lvl) } else { lvl.to_string() });
                    }
                }
                for n in caps["notes"].as_array().cloned().unwrap_or_default() {
                    println!("  note: {}", sv(&n));
                }
                for o in v["outputs"].as_array().cloned().unwrap_or_default() {
                    println!("Monitor {} {} {} work area {} scale {}{}", sv(&o["stable_id"]), sv(&o["name"]), rect(&o["rect"]), rect(&o["work_area"]), sv(&o["scale"]), if o["primary"] == true { " primary" } else { "" });
                }
                println!("Windows: {} total, {} placeable", sv(&v["window_count"]), sv(&v["placeable_windows"]));
                println!("Store: {} ({} snapshots, {} layouts)", sv(&v["store"]["path"]), sv(&v["store"]["snapshots"]), sv(&v["store"]["layouts"]));
                println!("Placement latency: p50 {} ms, p95 {} ms over {} samples", sv(&v["placement_latency_ms"]["p50"]), sv(&v["placement_latency_ms"]["p95"]), sv(&v["placement_latency_ms"]["samples"]));
                println!("Autostart: {} via {}", sv(&v["autostart"]["enabled"]), sv(&v["autostart"]["mode"]));
                println!("Telemetry: {}", sv(&v["telemetry"]));
                for w in v["warnings"].as_array().cloned().unwrap_or_default() {
                    println!("WARNING: {}", sv(&w));
                }
            }
            0
        }
        "logs" => {
            let red = a.flag("--redacted");
            let n: u64 = a.opt("--lines").map(|s| s.parse().unwrap_or_else(|_| usage_err("bad --lines"))).unwrap_or(200);
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "logs", json!({"lines": n, "redacted": red}));
            if json_out {
                pretty(&v);
            } else {
                for l in v.as_array().cloned().unwrap_or_default() {
                    println!("{}", sv(&l));
                }
            }
            0
        }
        "quit" => {
            a.finish();
            let mut c = connect();
            call(&mut c, "quit", json!({}));
            println!("Fractile service stopping.");
            0
        }
        "list-windows" => {
            let all = a.flag("--all");
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "list_windows", json!({"all": all}));
            if json_out {
                pretty(&v);
            } else {
                println!("{:<14} {:<28} {:<22} {:<5} {}", "ID", "APP", "GEOMETRY", "WS", "TITLE");
                for w in v.as_array().cloned().unwrap_or_default() {
                    let app = w["desktop_file_id"].as_str().or(w["wm_class"].as_str()).or(w["app_id"].as_str()).unwrap_or("?");
                    let mut flags = String::new();
                    if w["state"]["maximized"] == true {
                        flags.push_str(" [max]");
                    }
                    if w["state"]["minimized"] == true {
                        flags.push_str(" [min]");
                    }
                    if w["focused"] == true {
                        flags.push_str(" [active]");
                    }
                    println!("{:<14} {:<28} {:<22} {:<5} {}{}", sv(&w["id"]), app.chars().take(28).collect::<String>(), rect(&w["frame"]), sv(&w["workspace"]), sv(&w["title"]).chars().take(60).collect::<String>(), flags);
                }
            }
            0
        }
        "list-outputs" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "list_outputs", json!({}));
            if json_out {
                pretty(&v);
            } else {
                for o in v.as_array().cloned().unwrap_or_default() {
                    println!("{:<30} {:<10} {:<22} work area {:<22} scale {}{}", sv(&o["stable_id"]), sv(&o["name"]), rect(&o["rect"]), rect(&o["work_area"]), sv(&o["scale"]), if o["primary"] == true { " primary" } else { "" });
                }
            }
            0
        }
        "place" => {
            let grid = parse_grid(&a.opt("--grid").unwrap_or_else(|| usage_err("place needs --grid")));
            let cells = parse_cells(&a.opt("--cells").unwrap_or_else(|| usage_err("place needs --cells")));
            let w = a.opt("--window");
            let o = a.opt("--output");
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "place", json!({"window": w, "output": o, "grid": grid, "selection": cells}));
            if json_out {
                pretty(&v)
            } else {
                print_outcome(&v)
            }
            0
        }
        "snap" => {
            let act = a.pos().unwrap_or_else(|| usage_err("snap needs an action"));
            let w = a.opt("--window");
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "snap", json!({"window": w, "action": act}));
            if json_out {
                pretty(&v)
            } else {
                print_outcome(&v)
            }
            0
        }
        "move-output" => {
            let dir = a.pos().unwrap_or_else(|| "next".into());
            let step = match dir.as_str() {
                "next" => 1,
                "prev" | "previous" => -1,
                _ => usage_err("move-output next|prev"),
            };
            let w = a.opt("--window");
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "move_to_output", json!({"window": w, "step": step}));
            if json_out {
                pretty(&v)
            } else {
                print_outcome(&v)
            }
            0
        }
        "undo" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "undo", json!({}));
            if json_out {
                pretty(&v)
            } else {
                println!("undid placement of {}", sv(&v["undone"]));
            }
            0
        }
        "panel" => {
            let m = match a.pos().as_deref() {
                None | Some("toggle") => "toggle_panel",
                Some("show") => "show_panel",
                Some("hide") => "hide_panel",
                _ => usage_err("panel [toggle|show|hide]"),
            };
            a.finish();
            let mut c = connect();
            let v = call(&mut c, m, json!({}));
            if json_out {
                pretty(&v)
            }
            0
        }
        "settings" | "dashboard" | "palette" => {
            let page = a.pos();
            a.finish();
            let mut c = connect();
            call(&mut c, &format!("show_{cmd}"), json!({"page": page}));
            0
        }
        "save" => {
            let name = a.opt("--name");
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "snapshot_save", json!({"name": name}));
            if json_out {
                pretty(&v)
            } else {
                println!("saved snapshot {}", sv(&v["snapshot_id"]));
            }
            0
        }
        "restore" => {
            let dry = a.flag("--dry-run");
            let snap = a.opt("--snapshot");
            let layout = a.opt("--layout");
            let mode = a.opt("--mode");
            let no_wait = a.flag("--no-wait");
            let opts = restore_options(&mut a);
            a.finish();
            let mut c = connect();
            c.set_timeout(Some(std::time::Duration::from_secs(900)));
            let mut p = json!({"snapshot": snap.clone().unwrap_or_else(|| "latest".into()), "layout": layout, "options": opts, "wait": !no_wait});
            if let Some(m) = mode {
                p["mode"] = json!(m);
            }
            if dry {
                let v = call(&mut c, "restore_plan", p);
                if json_out {
                    pretty(&v)
                } else {
                    print_plan(&v)
                }
                0
            } else {
                let v = call(&mut c, "restore_start", p);
                if json_out {
                    pretty(&v);
                    if no_wait {
                        0
                    } else {
                        match v["state"].as_str() {
                            Some("completed") => 0,
                            Some("partial") | Some("cancelled") => 6,
                            _ => 1,
                        }
                    }
                } else if no_wait {
                    println!("restore {} started", sv(&v["run_id"]));
                    0
                } else {
                    print_report(&v)
                }
            }
        }
        "restore-status" => {
            a.finish();
            let mut c = connect();
            pretty(&call(&mut c, "restore_status", json!({})));
            0
        }
        "cancel" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "restore_cancel", json!({}));
            println!("cancelling {}", sv(&v["cancelling"]));
            0
        }
        "retry" => {
            let run = a.pos().unwrap_or_else(|| usage_err("retry RUN_ID"));
            let opts = restore_options(&mut a);
            a.finish();
            let mut c = connect();
            c.set_timeout(Some(std::time::Duration::from_secs(900)));
            let v = call(&mut c, "restore_retry", json!({"run_id": run, "options": opts, "wait": true}));
            if json_out {
                pretty(&v);
                0
            } else {
                print_report(&v)
            }
        }
        "undo-restore" => {
            a.finish();
            let mut c = connect();
            c.set_timeout(Some(std::time::Duration::from_secs(900)));
            let v = call(&mut c, "restore_undo", json!({"wait": true}));
            if json_out {
                pretty(&v);
                0
            } else {
                print_report(&v)
            }
        }
        "list-snapshots" => {
            let limit: u64 = a.opt("--limit").map(|s| s.parse().unwrap_or_else(|_| usage_err("bad --limit"))).unwrap_or(30);
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "snapshot_list", json!({"limit": limit}));
            if json_out {
                pretty(&v)
            } else {
                println!("{:<38} {:<26} {:<11} {:>4} {:>4}  {}", "ID", "CREATED", "MODE", "WIN", "APPS", "NAME");
                for s in v.as_array().cloned().unwrap_or_default() {
                    let flags = format!("{}{}", if s["pinned"] == true { "[pinned] " } else { "" }, if s["checksum_ok"] == false { "[CORRUPT] " } else { "" });
                    println!("{:<38} {:<26} {:<11} {:>4} {:>4}  {}{}", sv(&s["id"]), sv(&s["created_at"]).chars().take(25).collect::<String>(), sv(&s["mode"]), sv(&s["window_count"]), sv(&s["app_count"]), flags, s["name"].as_str().unwrap_or(""));
                }
            }
            0
        }
        "snapshot" => {
            let sub = a.pos().unwrap_or_else(|| usage_err("snapshot export|import|delete|pin|unpin"));
            let mut c = connect();
            match sub.as_str() {
                "export" => {
                    let id = a.pos().unwrap_or_else(|| usage_err("snapshot export ID [FILE]"));
                    let red = a.flag("--redacted");
                    let f = a.pos();
                    a.finish();
                    let v = call(&mut c, "snapshot_get", json!({"id": id, "redacted": red}));
                    write_out(f, &v);
                }
                "import" => {
                    let f = a.pos().unwrap_or_else(|| usage_err("snapshot import FILE"));
                    a.finish();
                    let v = call(&mut c, "snapshot_import", json!({"snapshot": read_file(&f)}));
                    println!("imported as {} (launch actions require approval)", sv(&v["snapshot_id"]));
                }
                "delete" => {
                    let id = a.pos().unwrap_or_else(|| usage_err("snapshot delete ID"));
                    a.finish();
                    let v = call(&mut c, "snapshot_delete", json!({"id": id}));
                    if v["deleted"] != true {
                        die(RpcError::new("not_found", "snapshot not found"));
                    }
                    println!("deleted");
                }
                "pin" | "unpin" => {
                    let id = a.pos().unwrap_or_else(|| usage_err("snapshot pin ID"));
                    a.finish();
                    call(&mut c, "snapshot_pin", json!({"id": id, "pinned": sub == "pin"}));
                    println!("ok");
                }
                _ => usage_err("snapshot export|import|delete|pin|unpin"),
            }
            0
        }
        "list-layouts" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "layout_list", json!({}));
            if json_out {
                pretty(&v)
            } else {
                for l in v.as_array().cloned().unwrap_or_default() {
                    let n: usize = l["variants"].as_array().map(|vs| vs.iter().map(|x| x["entries"].as_array().map(|e| e.len()).unwrap_or(0)).sum()).unwrap_or(0);
                    println!("{}{:<30} {:>3} entries  {}", if l["favorite"] == true { "* " } else { "  " }, sv(&l["name"]), n, l["description"].as_str().unwrap_or(""));
                }
            }
            0
        }
        "layout" => {
            let sub = a.pos().unwrap_or_else(|| usage_err("layout save|apply|show|delete|export|import"));
            let mut c = connect();
            c.set_timeout(Some(std::time::Duration::from_secs(900)));
            match sub.as_str() {
                "save" => {
                    let name = a.pos().unwrap_or_else(|| usage_err("layout save NAME"));
                    let launch = !a.flag("--no-launch");
                    a.finish();
                    let v = call(&mut c, "layout_from_windows", json!({"name": name, "launch": launch, "save": true}));
                    println!("saved layout '{}' ({} entries)", sv(&v["name"]), v["variants"][0]["entries"].as_array().map(|e| e.len()).unwrap_or(0));
                    0
                }
                "apply" => {
                    let name = a.pos().unwrap_or_else(|| usage_err("layout apply NAME"));
                    let dry = a.flag("--dry-run");
                    let mode = a.opt("--mode").unwrap_or_else(|| "launch_missing".into());
                    let opts = restore_options(&mut a);
                    a.finish();
                    if dry {
                        let v = call(&mut c, "restore_plan", json!({"layout": name, "mode": mode, "options": opts}));
                        if json_out {
                            pretty(&v)
                        } else {
                            print_plan(&v)
                        }
                        0
                    } else {
                        let v = call(&mut c, "layout_apply", json!({"name": name, "mode": mode, "options": opts, "wait": true}));
                        if json_out {
                            pretty(&v);
                            0
                        } else {
                            print_report(&v)
                        }
                    }
                }
                "show" => {
                    let name = a.pos().unwrap_or_else(|| usage_err("layout show NAME"));
                    a.finish();
                    pretty(&call(&mut c, "layout_preview", json!({"name": name})));
                    0
                }
                "delete" => {
                    let name = a.pos().unwrap_or_else(|| usage_err("layout delete NAME"));
                    a.finish();
                    let v = call(&mut c, "layout_delete", json!({"name": name}));
                    if v["deleted"] != true {
                        die(RpcError::new("not_found", "layout not found"));
                    }
                    println!("deleted");
                    0
                }
                "export" => {
                    let name = a.pos().unwrap_or_else(|| usage_err("layout export NAME [FILE]"));
                    let f = a.pos();
                    a.finish();
                    write_out(f, &call(&mut c, "layout_get", json!({"name": name})));
                    0
                }
                "import" => {
                    let f = a.pos().unwrap_or_else(|| usage_err("layout import FILE"));
                    a.finish();
                    let v = call(&mut c, "layout_save", json!({"layout": read_file(&f), "import": true}));
                    println!("imported layout '{}' (launch actions require approval)", sv(&v["name"]));
                    0
                }
                _ => usage_err("layout save|apply|show|delete|export|import"),
            }
        }
        "config" => {
            let sub = a.pos().unwrap_or_else(|| "get".into());
            let mut c = connect();
            match sub.as_str() {
                "path" => {
                    a.finish();
                    println!("{}", sv(&call(&mut c, "get_config", json!({}))["path"]));
                }
                "get" => {
                    let key = a.pos();
                    a.finish();
                    let v = call(&mut c, "get_config", json!({}));
                    let mut cur = &v["config"];
                    if let Some(k) = &key {
                        for p in k.split('.') {
                            cur = &cur[p];
                        }
                        if cur.is_null() {
                            die(RpcError::new("not_found", format!("no config key '{k}'")));
                        }
                    }
                    pretty(cur);
                }
                "set" => {
                    let key = a.pos().unwrap_or_else(|| usage_err("config set KEY.PATH VALUE"));
                    let val = a.pos().unwrap_or_else(|| usage_err("config set KEY.PATH VALUE"));
                    a.finish();
                    let jv = serde_json::from_str(&val).unwrap_or(Value::String(val));
                    let mut patch = json!({});
                    set_path(&mut patch, &key, jv);
                    let v = call(&mut c, "set_config", json!({"patch": patch}));
                    println!("ok");
                    if let Some(m) = v["hotkey_errors"].as_object() {
                        for (k, e) in m {
                            eprintln!("shortcut {k}: {}", sv(e));
                        }
                    }
                }
                "reset" => {
                    let section = a.pos();
                    a.finish();
                    call(&mut c, "reset_config", json!({"section": section}));
                    println!("reset {}", section.unwrap_or_else(|| "all settings".into()));
                }
                _ => usage_err("config path|get|set|reset"),
            }
            0
        }
        "shortcut-check" => {
            let acc = a.pos().unwrap_or_else(|| usage_err("shortcut-check ACCELERATOR"));
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "shortcut_check", json!({"accelerator": acc}));
            if json_out {
                pretty(&v);
            } else {
                println!("{}: {}", if v["free"] == true { "free" } else { "unavailable" }, sv(&v["reason"]));
            }
            if v["free"] == true {
                0
            } else {
                1
            }
        }
        "autostart" => {
            let sub = a.pos().unwrap_or_else(|| "status".into());
            a.finish();
            let mut c = connect();
            let v = match sub.as_str() {
                "on" | "enable" => call(&mut c, "autostart_set", json!({"enabled": true})),
                "off" | "disable" => call(&mut c, "autostart_set", json!({"enabled": false})),
                "status" => call(&mut c, "autostart_status", json!({})),
                _ => usage_err("autostart on|off|status"),
            };
            if json_out {
                pretty(&v);
            } else {
                println!("autostart {} (via {})", if v["enabled"] == true { "enabled" } else { "disabled" }, sv(&v["mode"]));
            }
            0
        }
        "list-apps" => {
            a.finish();
            let mut c = connect();
            let v = call(&mut c, "apps_list", json!({}));
            if json_out {
                pretty(&v)
            } else {
                for x in v.as_array().cloned().unwrap_or_default() {
                    println!("{:<50} {:<30} {}", sv(&x["id"]), sv(&x["name"]).chars().take(30).collect::<String>(), if x["policy"].is_null() { "default".to_string() } else { x["policy"]["launch"].to_string() });
                }
            }
            0
        }
        other => usage_err(&format!("unknown command '{other}'")),
    };
    exit(code)
}

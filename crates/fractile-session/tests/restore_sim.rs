//! Restore engine scenarios against a simulated desktop. These cover the
//! logic of T09-T16 (duplicates, reuse, same-app windows, monitor loss,
//! app geometry fights, crash + retry idempotency). Real-desktop E2E tests
//! live in tests/integration.

use fractile_core::adapter::*;
use fractile_core::config::{AppPolicy, Config};
use fractile_core::geometry::Rect;
use fractile_core::output::{Output, Transform};
use fractile_core::snapshot::{LaunchPolicy, Snapshot, SnapshotMode, WindowRecord};
use fractile_core::window::{WindowInfo, WindowState};
use fractile_session::restore::*;
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::atomic::AtomicBool;
use std::time::{Duration, Instant};

#[derive(Clone)]
struct AppSim {
    /// windows opened per launch (session-managing apps open several)
    windows_per_launch: u32,
    delay: Duration,
    /// app re-asserts its own size once after being placed
    fights: bool,
    titles: Vec<String>,
}

struct Sim {
    windows: RefCell<Vec<WindowInfo>>,
    outputs: Vec<Output>,
    apps: HashMap<String, AppSim>,
    pending: RefCell<Vec<(Instant, String, u32)>>,
    new_events: RefCell<VecDeque<String>>,
    launches: RefCell<Vec<String>>,
    another_window: RefCell<Vec<bool>>,
    ledger: RefCell<Vec<(String, Instant)>>,
    persisted: RefCell<BTreeMap<String, ItemResult>>,
    fought: RefCell<std::collections::HashSet<String>>,
    next_id: RefCell<u32>,
    can_move: bool,
}

fn out(id: &str, x: i32, w: i32) -> Output {
    Output { stable_id: id.into(), stable_id_from_edid: true, name: id.into(), description: None, rect: Rect::new(x, 0, w, 1440), work_area: Rect::new(x, 32, w, 1408), scale: 1.0, transform: Transform::Normal, primary: x == 0, physical_mm: None }
}

impl Sim {
    fn new(outputs: Vec<Output>) -> Sim {
        Sim {
            windows: RefCell::new(vec![]),
            outputs,
            apps: HashMap::new(),
            pending: RefCell::new(vec![]),
            new_events: RefCell::new(VecDeque::new()),
            launches: RefCell::new(vec![]),
            another_window: RefCell::new(vec![]),
            ledger: RefCell::new(vec![]),
            persisted: RefCell::new(BTreeMap::new()),
            fought: RefCell::new(Default::default()),
            next_id: RefCell::new(1),
            can_move: true,
        }
    }
    fn app(mut self, desktop: &str, wpl: u32, delay_ms: u64, fights: bool, titles: &[&str]) -> Sim {
        self.apps.insert(desktop.into(), AppSim { windows_per_launch: wpl, delay: Duration::from_millis(delay_ms), fights, titles: titles.iter().map(|s| s.to_string()).collect() });
        self
    }
    fn open(&self, desktop: &str, title: &str) -> String {
        let mut n = self.next_id.borrow_mut();
        let id = format!("sim:{}", *n);
        *n += 1;
        let stacking = self.windows.borrow().len() as u32;
        self.windows.borrow_mut().push(WindowInfo {
            id: id.clone(),
            title: title.into(),
            desktop_file_id: Some(desktop.into()),
            wm_class: Some(desktop.trim_end_matches(".desktop").into()),
            frame: Rect::new(100, 100, 800, 600),
            stacking: Some(stacking),
            ..Default::default()
        });
        id
    }
    fn tick(&self) {
        let now = Instant::now();
        let due: Vec<(Instant, String, u32)> = self.pending.borrow().iter().filter(|p| p.0 <= now).cloned().collect();
        self.pending.borrow_mut().retain(|p| p.0 > now);
        for (_, d, _k) in due {
            let app = &self.apps[&d];
            let k = self.count(&d);
            let title = app.titles.get(k).cloned().unwrap_or_else(|| format!("{d} window"));
            let id = self.open(&d, &title);
            self.new_events.borrow_mut().push_back(id);
        }
    }
    fn count(&self, desktop: &str) -> usize {
        self.windows.borrow().iter().filter(|w| w.desktop_file_id.as_deref() == Some(desktop)).count()
    }
}

impl RestoreHost for Sim {
    fn capabilities(&self) -> Capabilities {
        let mut ops = BTreeMap::new();
        for op in Operation::ALL {
            ops.insert(op, SupportLevel::Full);
        }
        if !self.can_move {
            ops.insert(Operation::MoveResize, SupportLevel::Unsupported("sim".into()));
        }
        Capabilities { adapter: "sim".into(), adapter_version: "0".into(), api_version: 1, session_type: "sim".into(), compositor: "sim".into(), compositor_version: None, operations: ops, tiling_manager: None, notes: vec![] }
    }
    fn windows(&self) -> Result<Vec<WindowInfo>, String> {
        self.tick();
        Ok(self.windows.borrow().clone())
    }
    fn window(&self, id: &str) -> Result<WindowInfo, String> {
        self.tick();
        // geometry fight: the first time a fighting app's window is read after
        // placement, it resets its size
        let fights = {
            let ws = self.windows.borrow();
            let w = ws.iter().find(|w| w.id == id).ok_or("gone")?;
            let d = w.desktop_file_id.clone().unwrap_or_default();
            self.apps.get(&d).map(|a| a.fights).unwrap_or(false) && w.frame != Rect::new(100, 100, 800, 600) && !self.fought.borrow().contains(id)
        };
        if fights {
            self.fought.borrow_mut().insert(id.into());
            let mut ws = self.windows.borrow_mut();
            let w = ws.iter_mut().find(|w| w.id == id).unwrap();
            w.frame.w = 800;
            w.frame.h = 600;
        }
        self.windows.borrow().iter().find(|w| w.id == id).cloned().ok_or_else(|| "gone".into())
    }
    fn outputs(&self) -> Result<Vec<Output>, String> {
        Ok(self.outputs.clone())
    }
    fn place(&self, id: &str, rect: Rect) -> Result<PlacementOutcome, String> {
        let mut ws = self.windows.borrow_mut();
        let w = ws.iter_mut().find(|w| w.id == id).ok_or("gone")?;
        w.frame = rect;
        Ok(PlacementOutcome { window_id: id.into(), requested: rect, observed: Some(rect), max_error_px: Some(0), within_tolerance: true, attempts: 1, note: None })
    }
    fn set_state(&self, id: &str, s: &WindowState) -> Result<(), String> {
        let mut ws = self.windows.borrow_mut();
        let w = ws.iter_mut().find(|w| w.id == id).ok_or("gone")?;
        w.state = *s;
        Ok(())
    }
    fn move_to_workspace(&self, id: &str, n: u32) -> Result<(), String> {
        let mut ws = self.windows.borrow_mut();
        ws.iter_mut().find(|w| w.id == id).ok_or("gone")?.workspace = Some(n);
        Ok(())
    }
    fn activate(&self, id: &str) -> Result<(), String> {
        for w in self.windows.borrow_mut().iter_mut() {
            w.focused = w.id == id;
        }
        Ok(())
    }
    fn launch(&self, d: &str, _a: &[String], another: bool) -> Result<u32, String> {
        let app = self.apps.get(d).ok_or("not installed")?;
        self.another_window.borrow_mut().push(another);
        self.launches.borrow_mut().push(d.into());
        let already = self.launches.borrow().iter().filter(|x| *x == d).count() as u32 - 1;
        for k in 0..app.windows_per_launch {
            self.pending.borrow_mut().push((Instant::now() + app.delay + Duration::from_millis(k as u64 * 50), d.into(), already * app.windows_per_launch + k));
        }
        Ok(1000)
    }
    fn can_launch(&self, d: &str) -> Result<String, String> {
        if self.apps.contains_key(d) {
            Ok("sim".into())
        } else {
            Err("not installed".into())
        }
    }
    fn next_new_window(&self, timeout: Duration) -> Option<String> {
        let end = Instant::now() + timeout;
        loop {
            self.tick();
            if let Some(id) = self.new_events.borrow_mut().pop_front() {
                return Some(id);
            }
            if Instant::now() >= end {
                return None;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    fn recent_launches(&self, d: &str, secs: i64) -> u32 {
        self.ledger.borrow().iter().filter(|(x, t)| x == d && t.elapsed().as_secs() as i64 <= secs).count() as u32
    }
    fn record_launch(&self, d: &str, _r: &str, _p: u32) {
        self.ledger.borrow_mut().push((d.into(), Instant::now()));
    }
    fn persist(&self, _run: &str, item: &ItemResult) {
        self.persisted.borrow_mut().insert(item.record_id.clone(), item.clone());
    }
    fn progress(&self, _p: &Progress) {}
}

fn rec(id: &str, desktop: &str, title: &str, ord: u32, out: &str, abs: Rect, wa: Rect) -> WindowRecord {
    WindowRecord {
        record_id: id.into(),
        desktop_file_id: Some(desktop.into()),
        wm_class: Some(desktop.trim_end_matches(".desktop").into()),
        title_hint: Some(title.into()),
        instance_ordinal: ord,
        output_stable_id: Some(out.into()),
        normalized_geometry: fractile_core::geometry::rect_to_norm(wa, abs).as_array(),
        absolute_geometry: abs,
        launch_policy: LaunchPolicy::IfMissing,
        ..Default::default()
    }
}

fn opts(cfg: &Config) -> RunOptions {
    let mut o = RunOptions::from_config(cfg, "run-test");
    o.per_app_timeout = Duration::from_secs(3);
    o.total_timeout = Duration::from_secs(10);
    o.stabilization = Duration::from_millis(400);
    o.instance_settle = Duration::from_millis(200);
    o
}

fn snapshot(outputs: Vec<Output>, recs: Vec<WindowRecord>) -> Snapshot {
    let mut s = Snapshot::new_empty(SnapshotMode::Manual);
    s.outputs = outputs;
    s.windows = recs;
    s
}

const WA_A: Rect = Rect::new(0, 32, 2560, 1408);
const WA_B: Rect = Rect::new(2560, 32, 3440, 1408);

/// T10/T11/T12: 10 apps / 15 windows, some already open, zero duplicates.
#[test]
fn restore_many_apps_without_duplicates() {
    let outs = vec![out("A", 0, 2560), out("B", 2560, 3440)];
    let mut sim = Sim::new(outs.clone());
    let mut recs = vec![];
    let mut k = 0;
    for a in 0..10 {
        let d = format!("app{a}.desktop");
        let n = if a < 5 { 2 } else { 1 };
        let titles: Vec<String> = (0..n).map(|i| format!("doc{i} - app{a}")).collect();
        let tref: Vec<&str> = titles.iter().map(|s| s.as_str()).collect();
        sim = sim.app(&d, 1, 50, a == 3, &tref);
        for i in 0..n {
            let (o, wa) = if k % 2 == 0 { ("A", WA_A) } else { ("B", WA_B) };
            let abs = Rect::new(wa.x + 10 * k, wa.y + 5 * k, 900, 700);
            recs.push(rec(&format!("r{k}"), &d, &titles[i], i as u32, o, abs, wa));
            k += 1;
        }
    }
    assert_eq!(recs.len(), 15);
    // app0 and app7 already running (one window each)
    sim.open("app0.desktop", "doc0 - app0");
    sim.open("app7.desktop", "doc0 - app7");
    let cfg = Config::default();
    let s = snapshot(outs.clone(), recs);
    let targets = targets_from_snapshot(&s, &cfg);
    let o = opts(&cfg);
    let p = plan(targets, &s.outputs, &sim, &o, &cfg).unwrap();
    assert_eq!(p.items.iter().filter(|i| i.action == PlannedAction::AlreadyOpen).count(), 2);
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(rep.all_success(), "{}\n{:#?}", rep.summary(), rep.items);
    // zero duplicates: exactly the recorded number of windows per app
    for a in 0..10 {
        let d = format!("app{a}.desktop");
        let want = if a < 5 { 2 } else { 1 };
        assert_eq!(sim.count(&d), want, "{d} windows");
    }
    // app7 was open → reused, not relaunched
    assert!(!sim.launches.borrow().contains(&"app7.desktop".to_string()));
    // geometry exact for every window (fighting app3 got re-applied)
    for it in &rep.items {
        let w = sim.window(it.window_id.as_ref().unwrap()).unwrap();
        assert_eq!(Some(w.frame), it.requested, "{}", it.label);
    }
    // same-app windows matched by title, no swaps
    let r1 = rep.items.iter().find(|i| i.record_id == "r1").unwrap();
    assert!(sim.window(r1.window_id.as_ref().unwrap()).unwrap().title.contains("doc1"));
}

/// Session-managing app (e.g. a browser) opens all its windows on one launch:
/// Fractile launches it once and never a second instance.
#[test]
fn session_managing_app_launched_once() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("browser.desktop", 3, 100, false, &["w1", "w2", "w3"]);
    let recs = (0..3).map(|i| rec(&format!("r{i}"), "browser.desktop", &format!("w{}", i + 1), i, "A", Rect::new(i as i32 * 500, 32, 500, 500), WA_A)).collect();
    let mut cfg = Config::default();
    cfg.apps.insert("browser.desktop".into(), AppPolicy { launch: LaunchPolicy::IfMissing, extra_args: vec![], app_manages_session: Some(true) });
    let s = snapshot(outs, recs);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(rep.all_success(), "{:#?}", rep.items);
    assert_eq!(sim.launches.borrow().len(), 1);
    assert_eq!(sim.count("browser.desktop"), 3);
}

/// A well-known browser with no per-app setting restores its own windows by
/// default: one launch, no extra windows, and the window it did not reopen is
/// reported as such instead of being opened blank.
#[test]
fn known_browser_without_policy_is_launched_once_and_missing_window_reported() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("firefox.desktop", 2, 100, false, &["w1", "w2"]);
    let recs = (0..3).map(|i| rec(&format!("r{i}"), "firefox.desktop", &format!("w{}", i + 1), i, "A", Rect::new(i as i32 * 500, 32, 500, 500), WA_A)).collect();
    let cfg = Config::default();
    let s = snapshot(outs, recs);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert_eq!(sim.launches.borrow().len(), 1, "{:#?}", rep.items);
    assert_eq!(sim.count("firefox.desktop"), 2);
    assert_eq!(rep.items.iter().filter(|i| i.status.is_success()).count(), 2, "{:#?}", rep.items);
    let missing = rep.items.iter().find(|i| !i.status.is_success()).unwrap();
    assert_eq!(missing.status, ItemStatus::TimedOut);
    assert!(missing.message.as_deref().unwrap_or("").contains("restores its own windows"), "{:?}", missing.message);
}

/// The same browser with the user's setting turned off is launched per window.
#[test]
fn users_off_switch_beats_the_builtin_list() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("firefox.desktop", 1, 50, false, &["w1", "w2"]);
    let recs = (0..2).map(|i| rec(&format!("r{i}"), "firefox.desktop", &format!("w{}", i + 1), i, "A", Rect::new(i as i32 * 500, 32, 500, 500), WA_A)).collect();
    let mut cfg = Config::default();
    cfg.apps.insert("firefox.desktop".into(), AppPolicy { launch: LaunchPolicy::IfMissing, extra_args: vec![], app_manages_session: Some(false) });
    let s = snapshot(outs, recs);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(rep.all_success(), "{:#?}", rep.items);
    assert_eq!(sim.launches.borrow().len(), 2);
}

/// Non-session app with two recorded windows: launched twice, sequentially.
#[test]
fn per_instance_launch_for_plain_apps() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("term.desktop", 1, 50, false, &["t1", "t2"]);
    let recs = (0..2).map(|i| rec(&format!("r{i}"), "term.desktop", &format!("t{}", i + 1), i, "A", Rect::new(i as i32 * 500, 32, 500, 500), WA_A)).collect();
    let cfg = Config::default();
    let s = snapshot(outs, recs);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(rep.all_success(), "{:#?}", rep.items);
    assert_eq!(sim.launches.borrow().len(), 2);
    assert_eq!(sim.count("term.desktop"), 2);
    // the second launch asks for another window (the app's new-window action)
    assert_eq!(*sim.another_window.borrow(), vec![false, true]);
}

/// The desktop's own session restore (Plasma) reopened one window of a
/// single-instance app before Fractile restores: plain activation would only
/// raise that window, so both launches ask for another window.
#[test]
fn launches_ask_for_another_window_when_the_app_already_has_one() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("files.desktop", 1, 50, false, &["f1", "f2", "f3"]);
    sim.open("files.desktop", "f1");
    let recs = (0..3).map(|i| rec(&format!("r{i}"), "files.desktop", &format!("f{}", i + 1), i, "A", Rect::new(i as i32 * 500, 32, 500, 500), WA_A)).collect();
    let cfg = Config::default();
    let s = snapshot(outs, recs);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(rep.all_success(), "{:#?}", rep.items);
    assert_eq!(sim.count("files.desktop"), 3);
    assert_eq!(*sim.another_window.borrow(), vec![true, true]);
}

/// T13: recorded monitor B missing → windows remapped onto A, inside its work area.
#[test]
fn missing_monitor_remaps() {
    let recorded = vec![out("A", 0, 2560), out("B", 2560, 3440)];
    let sim = Sim::new(vec![out("A", 0, 2560)]);
    sim.open("x.desktop", "x");
    let r = rec("r0", "x.desktop", "x", 0, "B", Rect::new(2560 + 1720, 32, 1720, 1408), WA_B);
    let cfg = Config::default();
    let s = snapshot(recorded, vec![r]);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    assert!(p.warnings.iter().any(|w| w.contains("not present")));
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    let w = sim.window(rep.items[0].window_id.as_ref().unwrap()).unwrap();
    assert_eq!(w.frame, Rect::new(1280, 32, 1280, 1408), "right half of B → right half of A");
}

/// Launch policies: ASK without approval and NEVER don't launch; dry run is side-effect free.
#[test]
fn approval_gating_and_dry_run() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("ask.desktop", 1, 10, false, &[]).app("never.desktop", 1, 10, false, &[]);
    let mut r1 = rec("r0", "ask.desktop", "a", 0, "A", Rect::new(0, 32, 100, 100), WA_A);
    r1.launch_policy = LaunchPolicy::Ask;
    let mut r2 = rec("r1", "never.desktop", "n", 0, "A", Rect::new(0, 32, 100, 100), WA_A);
    r2.launch_policy = LaunchPolicy::Never;
    let r3 = rec("r2", "missing.desktop", "m", 0, "A", Rect::new(0, 32, 100, 100), WA_A);
    let cfg = Config::default();
    let s = snapshot(outs, vec![r1, r2, r3]);
    let mut o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let acts: Vec<PlannedAction> = p.items.iter().map(|i| i.action).collect();
    assert_eq!(acts, vec![PlannedAction::NeedsApproval, PlannedAction::NotFound, PlannedAction::NotFound]);
    assert!(sim.launches.borrow().is_empty(), "planning launched something");
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert!(sim.launches.borrow().is_empty());
    assert!(rep.items.iter().all(|i| i.status == ItemStatus::Skipped));
    // approve once
    o.approved.insert("ask.desktop".into());
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    assert_eq!(p.items[0].action, PlannedAction::WillReopen);
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert_eq!(rep.items[0].status, ItemStatus::LaunchedAndPlaced);
}

/// T16: crash during restore, then retry with the persisted transaction log:
/// completed items are not redone and launched-but-unmatched apps are not relaunched.
#[test]
fn crash_and_retry_is_idempotent() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("slow.desktop", 1, 400, false, &["s"]).app("fast.desktop", 1, 10, false, &["f"]);
    let recs = vec![rec("r0", "fast.desktop", "f", 0, "A", Rect::new(0, 32, 600, 600), WA_A), rec("r1", "slow.desktop", "s", 0, "A", Rect::new(600, 32, 600, 600), WA_A)];
    let cfg = Config::default();
    let s = snapshot(outs, recs);
    let mut o = opts(&cfg);
    o.per_app_timeout = Duration::from_millis(250); // "crash" before slow app's window appears
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let rep1 = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert_eq!(rep1.items[0].status, ItemStatus::LaunchedAndPlaced);
    assert_eq!(rep1.items[1].status, ItemStatus::TimedOut);
    assert_eq!(sim.launches.borrow().len(), 2);
    // retry using persisted log
    let mut o2 = opts(&cfg);
    for (id, it) in sim.persisted.borrow().iter() {
        o2.previous.insert(id.clone(), (it.status, it.window_id.clone(), it.launched));
    }
    let p2 = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o2, &cfg).unwrap();
    assert_eq!(p2.items[0].action, PlannedAction::AlreadyDone);
    let rep2 = execute(&p2, &sim, &o2, &AtomicBool::new(false));
    assert!(rep2.all_success(), "{:#?}", rep2.items);
    assert_eq!(sim.launches.borrow().len(), 2, "no relaunch on retry");
    assert_eq!(sim.count("slow.desktop"), 1);
    assert_eq!(sim.count("fast.desktop"), 1);
}

/// T17: adapter without geometry control: honest UNSUPPORTED, nothing launched.
#[test]
fn unsupported_adapter_reports_honestly() {
    let outs = vec![out("A", 0, 2560)];
    let mut sim = Sim::new(outs.clone()).app("x.desktop", 1, 10, false, &[]);
    sim.can_move = false;
    let cfg = Config::default();
    let s = snapshot(outs, vec![rec("r0", "x.desktop", "x", 0, "A", Rect::new(0, 32, 10, 10), WA_A)]);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    assert_eq!(p.items[0].action, PlannedAction::CannotRestoreGeometry);
    let rep = execute(&p, &sim, &o, &AtomicBool::new(false));
    assert_eq!(rep.items[0].status, ItemStatus::Unsupported);
    assert!(sim.launches.borrow().is_empty());
}

/// Cancellation stops waiting and reports CANCELLED.
#[test]
fn cancellation() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone()).app("slow.desktop", 1, 5000, false, &[]);
    let cfg = Config::default();
    let s = snapshot(outs, vec![rec("r0", "slow.desktop", "x", 0, "A", Rect::new(0, 32, 10, 10), WA_A)]);
    let o = opts(&cfg);
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    let cancel = std::sync::Arc::new(AtomicBool::new(false));
    let c2 = cancel.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(300));
        c2.store(true, std::sync::atomic::Ordering::SeqCst);
    });
    let t = Instant::now();
    let rep = execute(&p, &sim, &o, &cancel);
    assert!(t.elapsed() < Duration::from_secs(2));
    assert_eq!(rep.items[0].status, ItemStatus::Cancelled);
}

/// Session ends during a restore: finished windows keep their live geometry
/// (the user's later change survives), unfinished ones keep their target,
/// a record with no window is kept, and no app is recorded twice.
#[test]
fn interrupted_restore_overlay() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone());
    let w_done = sim.open("done.desktop", "d");
    let w_pend = sim.open("pend.desktop", "p");
    let w_unreached = sim.open("unreached.desktop", "u");
    let t_done = Rect::new(0, 32, 600, 600);
    let t_pend = Rect::new(600, 32, 700, 500);
    let t_unr = Rect::new(1300, 32, 500, 400);
    let t_missing = Rect::new(1800, 32, 400, 400);
    let recs = vec![
        rec("r0", "done.desktop", "d", 0, "A", t_done, WA_A),
        rec("r1", "pend.desktop", "p", 0, "A", t_pend, WA_A),
        rec("r2", "unreached.desktop", "u", 0, "A", t_unr, WA_A),
        rec("r3", "missing.desktop", "m", 0, "A", t_missing, WA_A),
    ];
    let s = snapshot(outs, recs);
    // r0 was placed, then the user moved it; r1 was matched but cancelled
    // before placing; r2 and r3 were never reached.
    let user_rect = Rect::new(50, 60, 900, 700);
    let journal = vec![("r0".to_string(), ItemStatus::AlreadyPresent, Some(w_done.clone())), ("r1".to_string(), ItemStatus::Cancelled, Some(w_pend.clone()))];
    let mut windows = sim.windows.borrow().clone();
    windows.iter_mut().find(|w| w.id == w_done).unwrap().frame = user_rect;
    let left = interrupted_overlay(&s, &Config::default(), &journal, &mut windows);
    let f = |id: &str| windows.iter().find(|w| w.id == id).unwrap().frame;
    assert_eq!(f(&w_done), user_rect, "a finished window keeps the user's change");
    assert_eq!(f(&w_pend), t_pend, "a cancelled window keeps its target");
    assert_eq!(f(&w_unreached), t_unr, "an unreached but open window keeps its target");
    assert_eq!(left.len(), 1, "only the app with no window is appended: {left:?}");
    assert_eq!(left[0].record_id, "r3");
    assert_eq!(windows.len(), 3);
}

/// A restore cancelled before it reached an open window reports that window
/// CANCELLED (not finished), even for a window with no desktop id.
#[test]
fn cancel_before_placing_reports_cancelled() {
    let outs = vec![out("A", 0, 2560)];
    let sim = Sim::new(outs.clone());
    sim.open("x.desktop", "x");
    let w = sim.windows.borrow()[0].clone();
    let mut r = rec("r0", "x.desktop", "x", 0, "A", Rect::new(0, 32, 600, 600), WA_A);
    r.desktop_file_id = None;
    r.app_id = w.app_id.clone();
    let cfg = Config::default();
    let s = snapshot(outs, vec![r]);
    let o = opts(&cfg);
    sim.windows.borrow_mut()[0].desktop_file_id = None;
    let p = plan(targets_from_snapshot(&s, &cfg), &s.outputs, &sim, &o, &cfg).unwrap();
    assert_eq!(p.items[0].action, PlannedAction::AlreadyOpen);
    let rep = execute(&p, &sim, &o, &AtomicBool::new(true));
    assert!(rep.cancelled);
    assert_eq!(rep.items[0].status, ItemStatus::Cancelled, "{:?}", rep.items[0]);
    assert_eq!(sim.persisted.borrow().get("r0").map(|i| i.status), Some(ItemStatus::Cancelled));
}

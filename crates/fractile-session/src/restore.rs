//! Restore planning and execution (SRS 6.2). The engine is host-agnostic:
//! the daemon implements `RestoreHost` over a real compositor adapter, tests
//! implement it over a simulated desktop.

use fractile_core::adapter::{Capabilities, Operation, PlacementOutcome};
use fractile_core::config::Config;
use fractile_core::geometry::{norm_rect_to_rect, NormRect, Rect, Spacing};
use fractile_core::identity::{assign, score, APP_MATCH_THRESHOLD};
use fractile_core::layout::{ApplyMode, Layout};
use fractile_core::output::{map_outputs, Output, OutputMapping};
use fractile_core::rules::{OutputSel, Placement};
use fractile_core::snapshot::{LaunchPolicy, RestoreContent, Snapshot, WindowRecord};
use fractile_core::window::{WindowInfo, WindowState};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ItemStatus {
    Pending,
    Launching,
    AlreadyPresent,
    LaunchedAndPlaced,
    PlacedOnly,
    LaunchedUnplaced,
    Skipped,
    TimedOut,
    Unsupported,
    Ambiguous,
    Failed,
    Cancelled,
}

impl ItemStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ItemStatus::Pending => "PENDING",
            ItemStatus::Launching => "LAUNCHING",
            ItemStatus::AlreadyPresent => "ALREADY_PRESENT",
            ItemStatus::LaunchedAndPlaced => "LAUNCHED_AND_PLACED",
            ItemStatus::PlacedOnly => "PLACED_ONLY",
            ItemStatus::LaunchedUnplaced => "LAUNCHED_UNPLACED",
            ItemStatus::Skipped => "SKIPPED",
            ItemStatus::TimedOut => "TIMED_OUT",
            ItemStatus::Unsupported => "UNSUPPORTED",
            ItemStatus::Ambiguous => "AMBIGUOUS",
            ItemStatus::Failed => "FAILED",
            ItemStatus::Cancelled => "CANCELLED",
        }
    }
    pub fn parse(s: &str) -> ItemStatus {
        serde_json::from_value(serde_json::Value::String(s.into())).unwrap_or(ItemStatus::Pending)
    }
    /// Done successfully: never re-launched or re-placed on retry.
    pub fn is_success(&self) -> bool {
        matches!(self, ItemStatus::AlreadyPresent | ItemStatus::LaunchedAndPlaced | ItemStatus::PlacedOnly | ItemStatus::Ambiguous)
    }
    pub fn is_terminal(&self) -> bool {
        !matches!(self, ItemStatus::Pending | ItemStatus::Launching)
    }
}

/// Dry-run classification shown before anything happens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlannedAction {
    /// Window already open: will be positioned.
    AlreadyOpen,
    /// App not running: will be launched (approved), then positioned.
    WillReopen,
    /// App not running and launch requires user approval.
    NeedsApproval,
    /// No window and launch not permitted / not possible.
    NotFound,
    /// The adapter cannot set geometry on this desktop.
    CannotRestoreGeometry,
    /// Excluded by do-not-restore list or user deselection.
    Excluded,
    /// Already completed in an earlier attempt of this run.
    AlreadyDone,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum TargetGeometry {
    /// Snapshot geometry: recorded output + normalized + absolute.
    Recorded { output: Option<String>, norm: [f64; 4], abs: Rect },
    /// Layout placement on a selected output.
    Layout { output: OutputSel, placement: Placement, spacing: Spacing },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Target {
    pub record: WindowRecord,
    pub label: String,
    pub geometry: TargetGeometry,
    #[serde(default)]
    pub title_regex: Option<String>,
    pub launch_desktop_id: Option<String>,
    pub launch_policy: LaunchPolicy,
    #[serde(default)]
    pub extra_args: Vec<String>,
    #[serde(default)]
    pub delay_ms: u64,
    #[serde(default)]
    pub depends_on: Vec<String>,
    pub workspace: Option<u32>,
    pub state: Option<WindowState>,
    pub app_manages_session: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlanItem {
    pub record_id: String,
    pub label: String,
    pub app: String,
    pub action: PlannedAction,
    pub window_id: Option<String>,
    pub reason: String,
    pub launch_method: Option<String>,
    pub restore_content: RestoreContent,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Plan {
    pub targets: Vec<Target>,
    pub items: Vec<PlanItem>,
    pub output_mapping: Vec<OutputMapping>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ItemResult {
    pub record_id: String,
    pub label: String,
    pub status: ItemStatus,
    pub window_id: Option<String>,
    pub message: Option<String>,
    pub requested: Option<Rect>,
    pub observed: Option<Rect>,
    pub launched: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunReport {
    pub run_id: String,
    pub items: Vec<ItemResult>,
    pub cancelled: bool,
    pub timed_out: bool,
    pub launches: u32,
    pub duration_ms: u64,
    pub warnings: Vec<String>,
}

impl RunReport {
    pub fn counts(&self) -> BTreeMap<String, usize> {
        let mut m = BTreeMap::new();
        for i in &self.items {
            *m.entry(i.status.as_str().to_string()).or_insert(0) += 1;
        }
        m
    }
    pub fn summary(&self) -> String {
        let c = self.counts();
        c.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(" ")
    }
    pub fn all_success(&self) -> bool {
        self.items.iter().all(|i| i.status.is_success() || i.status == ItemStatus::Skipped)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Progress {
    pub run_id: String,
    pub done: usize,
    pub total: usize,
    pub item: Option<ItemResult>,
    pub phase: String,
}

/// Abstract desktop the engine drives.
pub trait RestoreHost {
    fn capabilities(&self) -> Capabilities;
    fn windows(&self) -> Result<Vec<WindowInfo>, String>;
    fn window(&self, id: &str) -> Result<WindowInfo, String>;
    fn outputs(&self) -> Result<Vec<Output>, String>;
    fn place(&self, id: &str, rect: Rect) -> Result<PlacementOutcome, String>;
    fn set_state(&self, id: &str, state: &WindowState) -> Result<(), String>;
    fn move_to_workspace(&self, id: &str, ws: u32) -> Result<(), String>;
    fn activate(&self, id: &str) -> Result<(), String>;
    /// Launch via desktop id (safe argv). Returns pid.
    fn launch(&self, desktop_id: &str, extra_args: &[String]) -> Result<u32, String>;
    fn can_launch(&self, desktop_id: &str) -> Result<String, String>;
    /// Next created window id, waiting up to `timeout`.
    fn next_new_window(&self, timeout: Duration) -> Option<String>;
    /// Launches of this desktop id in the last `seconds` (any run).
    fn recent_launches(&self, desktop_id: &str, seconds: i64) -> u32;
    fn record_launch(&self, desktop_id: &str, run_id: &str, pid: u32);
    fn persist(&self, run_id: &str, item: &ItemResult);
    fn progress(&self, p: &Progress);
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunOptions {
    pub run_id: String,
    /// One-time approvals for launching these desktop ids.
    pub approved: HashSet<String>,
    pub approve_all: bool,
    /// Master switch: when false nothing is launched.
    pub launch_enabled: bool,
    /// Only these record ids (None = all).
    pub selected: Option<HashSet<String>>,
    pub max_concurrent: usize,
    pub per_app_timeout: Duration,
    pub total_timeout: Duration,
    pub stabilization: Duration,
    pub max_reapply: u32,
    pub tolerance_px: i32,
    pub restore_focus: bool,
    /// Statuses from earlier attempts of this run (idempotent retry).
    pub previous: HashMap<String, (ItemStatus, Option<String>, bool)>,
    /// Gap after an app's last new window before launching another instance.
    pub instance_settle: Duration,
}

impl RunOptions {
    pub fn from_config(cfg: &Config, run_id: &str) -> RunOptions {
        RunOptions {
            run_id: run_id.into(),
            approved: HashSet::new(),
            approve_all: false,
            launch_enabled: true,
            selected: None,
            max_concurrent: cfg.session.max_concurrent_launches,
            per_app_timeout: Duration::from_secs(cfg.session.per_app_timeout_s),
            total_timeout: Duration::from_secs(cfg.session.total_timeout_s),
            stabilization: Duration::from_millis(cfg.placement.stabilization_ms),
            max_reapply: cfg.placement.max_reapply,
            tolerance_px: cfg.placement.tolerance_px,
            restore_focus: true,
            previous: HashMap::new(),
            instance_settle: Duration::from_millis(1500),
        }
    }
}

/// Targets from a snapshot. Per-app config policy overrides the recorded one.
pub fn targets_from_snapshot(s: &Snapshot, cfg: &Config) -> Vec<Target> {
    let ws_index = |id: &Option<String>| id.as_ref().and_then(|w| s.workspaces.iter().find(|x| &x.id == w).map(|x| x.ordinal).or_else(|| w.strip_prefix("ws-").and_then(|n| n.parse().ok())));
    s.windows
        .iter()
        .map(|r| {
            let app = r.desktop_file_id.as_ref().and_then(|d| cfg.apps.get(d));
            let label = r
                .desktop_file_id
                .clone()
                .map(|d| d.trim_end_matches(".desktop").to_string())
                .or(r.app_id.clone())
                .or(r.wm_class.clone())
                .unwrap_or_else(|| "unknown".into());
            Target {
                record: r.clone(),
                label: if r.instance_ordinal > 0 { format!("{label} #{}", r.instance_ordinal + 1) } else { label },
                geometry: TargetGeometry::Recorded { output: r.output_stable_id.clone(), norm: r.normalized_geometry, abs: r.absolute_geometry },
                title_regex: None,
                launch_desktop_id: r.desktop_file_id.clone(),
                launch_policy: app.map(|a| a.launch).unwrap_or(r.launch_policy),
                extra_args: app.map(|a| a.extra_args.clone()).unwrap_or_default(),
                delay_ms: 0,
                depends_on: vec![],
                workspace: ws_index(&r.workspace_id),
                state: Some(r.state),
                app_manages_session: app.map(|a| a.app_manages_session).unwrap_or(r.restore_content == RestoreContent::ApplicationManaged),
            }
        })
        .collect()
}

/// Targets from a named layout for the present monitors.
pub fn targets_from_layout(l: &Layout, mode: ApplyMode, outputs: &[Output], cfg: &Config) -> Result<Vec<Target>, String> {
    let v = l.select_variant(outputs).ok_or("layout has no variants")?;
    let order = fractile_core::layout::launch_order(&v.entries)?;
    let spacing = cfg.effective_spacing();
    let mut out = vec![];
    for id in order {
        let e = v.entries.iter().find(|e| e.id == id).unwrap();
        if e.excluded {
            continue;
        }
        let n = if mode == ApplyMode::ExactN { e.instances } else { 1 };
        for k in 0..n {
            let desk = e.matcher.desktop_id.clone().map(|d| if d.ends_with(".desktop") { d } else { format!("{d}.desktop") });
            let rec = WindowRecord {
                record_id: if n > 1 { format!("{}#{k}", e.id) } else { e.id.clone() },
                desktop_file_id: desk.clone(),
                app_id: e.matcher.app_id.clone(),
                wm_class: e.matcher.wm_class.clone(),
                role: e.matcher.role.clone(),
                instance_ordinal: e.matcher.instance_ordinal.unwrap_or(k),
                normalized_geometry: [0.0, 0.0, 1.0, 1.0],
                absolute_geometry: Rect::new(0, 0, 1, 1),
                ..Default::default()
            };
            let launch = e.launch.as_ref();
            let app = launch.and_then(|l| cfg.apps.get(&l.desktop_id));
            let policy = match mode {
                ApplyMode::PositionExisting => LaunchPolicy::Never,
                _ => launch.map(|l| app.map(|a| a.launch).filter(|p| *p != LaunchPolicy::Never).unwrap_or(l.policy)).unwrap_or(LaunchPolicy::Never),
            };
            out.push(Target {
                record: rec,
                label: if e.label.is_empty() { e.id.clone() } else { e.label.clone() },
                geometry: TargetGeometry::Layout { output: e.output.clone(), placement: e.placement.clone(), spacing },
                title_regex: if cfg.allow_title_regex { e.matcher.title_regex.clone() } else { None },
                launch_desktop_id: launch.map(|l| l.desktop_id.clone()).or(desk),
                launch_policy: policy,
                extra_args: app.map(|a| a.extra_args.clone()).unwrap_or_default(),
                delay_ms: launch.map(|l| l.delay_ms).unwrap_or(0),
                depends_on: launch.map(|l| l.depends_on.clone()).unwrap_or_default(),
                workspace: e.workspace,
                state: e.state,
                app_manages_session: app.map(|a| a.app_manages_session).unwrap_or(false),
            });
        }
    }
    Ok(out)
}

fn matches_regex(t: &Target, w: &WindowInfo) -> bool {
    match &t.title_regex {
        Some(r) => regex::RegexBuilder::new(r).size_limit(1 << 16).build().map(|re| re.is_match(&w.title)).unwrap_or(false),
        None => true,
    }
}

fn assign_targets(targets: &[&Target], windows: &[WindowInfo], claimed: &HashSet<String>) -> Vec<(String, Option<String>, bool)> {
    let recs: Vec<WindowRecord> = targets.iter().map(|t| t.record.clone()).collect();
    let a = assign(&recs, windows, claimed);
    a.into_iter()
        .zip(targets.iter())
        .map(|(asg, t)| {
            let ok = asg.window_id.as_ref().and_then(|id| windows.iter().find(|w| &w.id == id)).map(|w| matches_regex(t, w)).unwrap_or(false);
            (asg.record_id, if ok { asg.window_id } else { None }, asg.ambiguous && !asg.confident)
        })
        .collect()
}

/// Build the plan (dry run). No side effects.
pub fn plan(targets: Vec<Target>, recorded_outputs: &[Output], host: &dyn RestoreHost, opts: &RunOptions, cfg: &Config) -> Result<Plan, String> {
    let caps = host.capabilities();
    let outputs = host.outputs()?;
    let windows = host.windows()?;
    let mut warnings = vec![];
    if !caps.supports(Operation::MoveResize) {
        warnings.push(format!("adapter '{}' cannot move/resize windows on this desktop", caps.adapter));
    }
    let mapping = map_outputs(recorded_outputs, &outputs);
    for m in &mapping {
        if !m.exact {
            warnings.push(format!("monitor {} not present; its windows go to {} ({:?})", m.recorded_id, m.present_id, m.method));
        }
    }
    let refs: Vec<&Target> = targets.iter().collect();
    let asg = assign_targets(&refs, &windows, &HashSet::new());
    let mut items = vec![];
    for (t, (_, win, _amb)) in targets.iter().zip(asg) {
        let excluded = t.launch_desktop_id.as_ref().map(|d| cfg.session.do_not_restore.contains(d)).unwrap_or(false) || opts.selected.as_ref().map(|s| !s.contains(&t.record.record_id)).unwrap_or(false);
        let prev = opts.previous.get(&t.record.record_id);
        let (action, reason, method) = if excluded {
            (PlannedAction::Excluded, "excluded by user".to_string(), None)
        } else if prev.map(|p| p.0.is_success()).unwrap_or(false) {
            (PlannedAction::AlreadyDone, "completed in an earlier attempt".into(), None)
        } else if !caps.supports(Operation::MoveResize) {
            (PlannedAction::CannotRestoreGeometry, "compositor adapter cannot set window geometry".into(), None)
        } else if win.is_some() {
            (PlannedAction::AlreadyOpen, "matching window is open".into(), None)
        } else {
            launch_decision(t, &windows, host, opts)
        };
        items.push(PlanItem {
            record_id: t.record.record_id.clone(),
            label: t.label.clone(),
            app: t.launch_desktop_id.clone().unwrap_or_else(|| t.record.wm_class.clone().unwrap_or_default()),
            action,
            window_id: win,
            reason,
            launch_method: method,
            restore_content: if t.app_manages_session { RestoreContent::ApplicationManaged } else { RestoreContent::FrameOnly },
        });
    }
    Ok(Plan { targets, items, output_mapping: mapping, warnings })
}

fn launch_decision(t: &Target, windows: &[WindowInfo], host: &dyn RestoreHost, opts: &RunOptions) -> (PlannedAction, String, Option<String>) {
    let Some(d) = &t.launch_desktop_id else {
        return (PlannedAction::NotFound, "no desktop file identified at capture; cannot relaunch".into(), None);
    };
    let method = match host.can_launch(d) {
        Ok(m) => m,
        Err(e) => return (PlannedAction::NotFound, format!("cannot launch: {e}"), None),
    };
    // Weak partial matches (e.g. same WM_CLASS, different desktop id) require
    // confirmation rather than risking a duplicate process.
    let weak = windows.iter().any(|w| {
        let (s, _) = score(&t.record, w);
        s > 0 && s < APP_MATCH_THRESHOLD && w.is_placeable()
    });
    let approved = opts.approve_all || opts.approved.contains(d);
    if !opts.launch_enabled {
        return (PlannedAction::NotFound, "launching disabled for this restore".into(), Some(method));
    }
    match t.launch_policy {
        LaunchPolicy::Never => (PlannedAction::NotFound, "app not running; launch policy NEVER".into(), Some(method)),
        LaunchPolicy::Ask if !approved => (PlannedAction::NeedsApproval, "app not running; launch needs approval".into(), Some(method)),
        _ if weak && !approved => (PlannedAction::NeedsApproval, "a similar window exists; confirm to avoid a duplicate".into(), Some(method)),
        _ => (PlannedAction::WillReopen, "app not running; approved to launch".into(), Some(method)),
    }
}

struct Live {
    status: ItemStatus,
    window: Option<String>,
    launched: bool,
    message: Option<String>,
    requested: Option<Rect>,
    observed: Option<Rect>,
    reapplied: u32,
    placed_at: Option<Instant>,
}

fn target_rect(t: &Target, outputs: &[Output], mapping: &[OutputMapping], w: &WindowInfo) -> Option<Rect> {
    match &t.geometry {
        TargetGeometry::Recorded { output, norm, abs } => {
            let mapped = output.as_ref().and_then(|o| mapping.iter().find(|m| &m.recorded_id == o));
            let present = mapped.and_then(|m| outputs.iter().find(|o| o.stable_id == m.present_id)).or_else(|| outputs.iter().find(|o| o.primary)).or(outputs.first())?;
            // Exact topology: restore absolute pixels (no rounding at all).
            if mapped.map(|m| m.exact).unwrap_or(false) && present.work_area.intersection(abs).map(|i| i == *abs).unwrap_or(false) {
                return Some(*abs);
            }
            Some(norm_rect_to_rect(present.work_area, NormRect::from_array(*norm), &Spacing::default()))
        }
        TargetGeometry::Layout { output, placement, spacing } => {
            let o = output.resolve(outputs, Some(&w.frame))?;
            Some(placement.to_rect(o.work_area, spacing))
        }
    }
}

/// Execute a plan. Blocking; honours `cancel`; bounded by timeouts.
pub fn execute(p: &Plan, host: &dyn RestoreHost, opts: &RunOptions, cancel: &AtomicBool) -> RunReport {
    let start = Instant::now();
    let caps = host.capabilities();
    let can_place = caps.supports(Operation::MoveResize);
    let mut warnings = p.warnings.clone();
    let mut live: Vec<Live> = p
        .targets
        .iter()
        .map(|t| {
            let prev = opts.previous.get(&t.record.record_id);
            Live {
                status: prev.map(|x| if x.0.is_success() { x.0 } else { ItemStatus::Pending }).unwrap_or(ItemStatus::Pending),
                window: prev.and_then(|x| x.1.clone()),
                launched: prev.map(|x| x.2).unwrap_or(false),
                message: None,
                requested: None,
                observed: None,
                reapplied: 0,
                placed_at: None,
            }
        })
        .collect();
    let total = p.targets.len();
    let mut claimed: HashSet<String> = live.iter().filter(|l| l.status.is_success()).filter_map(|l| l.window.clone()).collect();
    let emit = |live: &Vec<Live>, i: usize, phase: &str| {
        let t = &p.targets[i];
        let l = &live[i];
        let r = ItemResult { record_id: t.record.record_id.clone(), label: t.label.clone(), status: l.status, window_id: l.window.clone(), message: l.message.clone(), requested: l.requested, observed: l.observed, launched: l.launched };
        host.persist(&opts.run_id, &r);
        let done = live.iter().filter(|x| x.status.is_terminal()).count();
        host.progress(&Progress { run_id: opts.run_id.clone(), done, total, item: Some(r), phase: phase.into() });
    };

    // Items excluded / not actionable → terminal right away.
    for (i, item) in p.items.iter().enumerate() {
        if live[i].status.is_success() {
            continue;
        }
        let st = match item.action {
            PlannedAction::Excluded => Some((ItemStatus::Skipped, item.reason.clone())),
            PlannedAction::NotFound => Some((ItemStatus::Skipped, item.reason.clone())),
            PlannedAction::NeedsApproval => Some((ItemStatus::Skipped, "launch not approved".to_string())),
            PlannedAction::CannotRestoreGeometry => Some((ItemStatus::Unsupported, item.reason.clone())),
            _ => None,
        };
        if let Some((s, m)) = st {
            // A NotFound item may still be satisfied by an already-open window
            // found later; only mark skipped when it has no window now.
            live[i].status = s;
            live[i].message = Some(m);
            emit(&live, i, "plan");
        }
    }

    let outputs = host.outputs().unwrap_or_default();
    let mapping = p.output_mapping.clone();

    // Place one target onto one window (workspace → geometry → state).
    let place = |t: &Target, wid: &str, l: &mut Live| -> bool {
        let w = match host.window(wid) {
            Ok(w) => w,
            Err(e) => {
                l.message = Some(format!("window vanished: {e}"));
                return false;
            }
        };
        if !can_place {
            l.message = Some("adapter cannot move/resize".into());
            return false;
        }
        if let Some(ws) = t.workspace {
            if w.workspace != Some(ws) {
                let _ = host.move_to_workspace(wid, ws);
            }
        }
        let Some(rect) = target_rect(t, &outputs, &mapping, &w) else {
            l.message = Some("no output available".into());
            return false;
        };
        l.requested = Some(rect);
        let res = host.place(wid, rect);
        let ok = match res {
            Ok(o) => {
                l.observed = o.observed;
                if !o.within_tolerance {
                    l.message = Some(format!("requested {:?}, observed {:?} (error {:?}px)", o.requested, o.observed, o.max_error_px));
                }
                o.within_tolerance || o.max_error_px.map(|e| e <= opts.tolerance_px.max(16)).unwrap_or(false)
            }
            Err(e) => {
                l.message = Some(e);
                false
            }
        };
        if let Some(st) = &t.state {
            if st.maximized || st.fullscreen || st.above || st.sticky {
                let s = WindowState { minimized: false, ..*st };
                let _ = host.set_state(wid, &s);
            }
        }
        l.placed_at = Some(Instant::now());
        ok
    };

    // Phase A: windows already present.
    if let Ok(windows) = host.windows() {
        let pending: Vec<usize> = (0..total).filter(|&i| live[i].status == ItemStatus::Pending).collect();
        let refs: Vec<&Target> = pending.iter().map(|&i| &p.targets[i]).collect();
        let asg = assign_targets(&refs, &windows, &claimed);
        for (k, (_, win, amb)) in asg.into_iter().enumerate() {
            if cancel.load(Ordering::SeqCst) {
                break;
            }
            let i = pending[k];
            if let Some(wid) = win {
                claimed.insert(wid.clone());
                let mut l = std::mem::replace(&mut live[i], Live { status: ItemStatus::Pending, window: None, launched: false, message: None, requested: None, observed: None, reapplied: 0, placed_at: None });
                l.window = Some(wid.clone());
                let ok = place(&p.targets[i], &wid, &mut l);
                l.status = if !ok {
                    ItemStatus::Failed
                } else if amb {
                    l.message = Some("indistinguishable windows; assigned by instance order".into());
                    ItemStatus::Ambiguous
                } else if l.launched {
                    ItemStatus::LaunchedAndPlaced
                } else {
                    ItemStatus::AlreadyPresent
                };
                live[i] = l;
                emit(&live, i, "place-existing");
            }
        }
    }

    // Phase B: launch missing apps (approved only) and catch their windows.
    struct Group {
        desktop_id: String,
        members: Vec<usize>,
        launches: u32,
        first_launch: Option<Instant>,
        last_window: Option<Instant>,
        deps_ready_at: Option<Instant>,
        timed_out: bool,
        manages_session: bool,
        extra_args: Vec<String>,
    }
    let mut groups: Vec<Group> = vec![];
    for (i, item) in p.items.iter().enumerate() {
        if live[i].status != ItemStatus::Pending {
            continue;
        }
        match item.action {
            PlannedAction::WillReopen | PlannedAction::AlreadyOpen | PlannedAction::AlreadyDone => {}
            _ => continue,
        }
        let t = &p.targets[i];
        let Some(d) = t.launch_desktop_id.clone() else {
            live[i].status = ItemStatus::Skipped;
            live[i].message = Some("no desktop id".into());
            emit(&live, i, "launch");
            continue;
        };
        if item.action != PlannedAction::WillReopen {
            // was open at plan time but vanished; don't launch without approval
            live[i].status = ItemStatus::Failed;
            live[i].message = Some("window closed before it could be placed".into());
            emit(&live, i, "launch");
            continue;
        }
        if let Some(g) = groups.iter_mut().find(|g| g.desktop_id == d) {
            g.members.push(i);
        } else {
            groups.push(Group { desktop_id: d, members: vec![i], launches: 0, first_launch: None, last_window: None, deps_ready_at: None, timed_out: false, manages_session: t.app_manages_session, extra_args: t.extra_args.clone() });
        }
    }
    let mut launches_total = 0u32;
    let mut last_rescan = Instant::now();
    let mut timed_out = false;
    let record_index: HashMap<String, usize> = p.targets.iter().enumerate().map(|(i, t)| (t.record.record_id.clone(), i)).collect();
    loop {
        let waiting: Vec<usize> = groups.iter().filter(|g| !g.timed_out).flat_map(|g| g.members.iter().copied()).filter(|&i| matches!(live[i].status, ItemStatus::Pending | ItemStatus::Launching)).collect();
        if waiting.is_empty() {
            break;
        }
        if cancel.load(Ordering::SeqCst) {
            for &i in &waiting {
                live[i].status = ItemStatus::Cancelled;
                emit(&live, i, "cancelled");
            }
            break;
        }
        if start.elapsed() > opts.total_timeout {
            timed_out = true;
            for &i in &waiting {
                live[i].status = if live[i].launched { ItemStatus::TimedOut } else { ItemStatus::Skipped };
                live[i].message = Some("restore total timeout reached".into());
                emit(&live, i, "timeout");
            }
            break;
        }
        // Start launches.
        let running = groups.iter().filter(|g| g.first_launch.is_some() && !g.timed_out && g.members.iter().any(|&i| matches!(live[i].status, ItemStatus::Pending | ItemStatus::Launching))).count();
        let mut slots = opts.max_concurrent.saturating_sub(running);
        for g in groups.iter_mut() {
            let pending_members: Vec<usize> = g.members.iter().copied().filter(|&i| matches!(live[i].status, ItemStatus::Pending | ItemStatus::Launching)).collect();
            if pending_members.is_empty() || g.timed_out {
                continue;
            }
            // dependencies: all depends_on targets terminal
            let deps_ok = pending_members.iter().all(|&i| p.targets[i].depends_on.iter().all(|d| record_index.get(d).map(|&j| live[j].status.is_terminal()).unwrap_or(true)));
            if !deps_ok {
                continue;
            }
            let ready = *g.deps_ready_at.get_or_insert_with(Instant::now);
            let delay = Duration::from_millis(p.targets[pending_members[0]].delay_ms);
            if ready.elapsed() < delay {
                continue;
            }
            let need_more = if g.first_launch.is_none() {
                true
            } else {
                // another instance only if the app finished opening windows and
                // it does not manage its own session (which would reopen all).
                // Every launch so far produced a window, the app has been quiet
                // for `instance_settle`, and members are still missing.
                let placed = (g.members.len() - pending_members.len()) as u32;
                !g.manages_session
                    && g.launches < g.members.len() as u32
                    && placed >= g.launches
                    && g.last_window.map(|t| t.elapsed() > opts.instance_settle).unwrap_or(false)
            };
            if !need_more {
                continue;
            }
            if g.first_launch.is_none() && slots == 0 {
                continue;
            }
            // Duplicate guard: a launch of this app recently (e.g. an interrupted
            // earlier attempt) → wait for its window instead of launching again.
            let recent = host.recent_launches(&g.desktop_id, opts.per_app_timeout.as_secs() as i64);
            let prior_attempt = pending_members.iter().any(|&i| live[i].launched);
            if g.first_launch.is_none() && recent > 0 && prior_attempt {
                g.first_launch = Some(Instant::now());
                g.launches = recent;
                for &i in &pending_members {
                    live[i].status = ItemStatus::Launching;
                    live[i].message = Some("waiting for window from an earlier launch".into());
                }
                warnings.push(format!("{}: launched recently by an earlier attempt; not relaunching", g.desktop_id));
                continue;
            }
            match host.launch(&g.desktop_id, &g.extra_args) {
                Ok(pid) => {
                    host.record_launch(&g.desktop_id, &opts.run_id, pid);
                    launches_total += 1;
                    if g.first_launch.is_none() {
                        slots = slots.saturating_sub(1);
                        g.first_launch = Some(Instant::now());
                    }
                    g.launches += 1;
                    for &i in &pending_members {
                        if live[i].status == ItemStatus::Pending {
                            live[i].status = ItemStatus::Launching;
                            live[i].launched = true;
                            emit(&live, i, "launch");
                        }
                    }
                }
                Err(e) => {
                    for &i in &pending_members {
                        live[i].status = ItemStatus::Failed;
                        live[i].message = Some(format!("launch failed: {e}"));
                        emit(&live, i, "launch");
                    }
                }
            }
        }
        // Collect new windows (event-driven; periodic rescan catches missed events).
        let mut candidates: Vec<String> = vec![];
        if let Some(id) = host.next_new_window(Duration::from_millis(100)) {
            candidates.push(id);
            while let Some(id) = host.next_new_window(Duration::from_millis(0)) {
                candidates.push(id);
            }
        }
        let rescan = last_rescan.elapsed() > Duration::from_millis(1000);
        let mut new_windows: Vec<WindowInfo> = vec![];
        if rescan || !candidates.is_empty() {
            last_rescan = Instant::now();
            if let Ok(ws) = host.windows() {
                new_windows = ws.into_iter().filter(|w| !claimed.contains(&w.id)).collect();
            }
        }
        if !new_windows.is_empty() {
            let waiting: Vec<usize> = groups.iter().flat_map(|g| g.members.iter().copied()).filter(|&i| matches!(live[i].status, ItemStatus::Pending | ItemStatus::Launching)).collect();
            let refs: Vec<&Target> = waiting.iter().map(|&i| &p.targets[i]).collect();
            let asg = assign_targets(&refs, &new_windows, &claimed);
            for (k, (_, win, amb)) in asg.into_iter().enumerate() {
                let i = waiting[k];
                let Some(wid) = win else { continue };
                // Wait for first stable geometry after map (bounded).
                let mut prev = host.window(&wid).ok().map(|w| w.frame);
                for _ in 0..8 {
                    std::thread::sleep(Duration::from_millis(60));
                    let cur = host.window(&wid).ok().map(|w| w.frame);
                    if cur == prev {
                        break;
                    }
                    prev = cur;
                }
                claimed.insert(wid.clone());
                let launched = live[i].launched;
                let mut l = std::mem::replace(&mut live[i], Live { status: ItemStatus::Pending, window: None, launched, message: None, requested: None, observed: None, reapplied: 0, placed_at: None });
                l.window = Some(wid.clone());
                let ok = place(&p.targets[i], &wid, &mut l);
                l.status = match (ok, launched, amb) {
                    (true, _, true) => ItemStatus::Ambiguous,
                    (true, true, _) => ItemStatus::LaunchedAndPlaced,
                    (true, false, _) => ItemStatus::PlacedOnly,
                    (false, true, _) => ItemStatus::LaunchedUnplaced,
                    (false, false, _) => ItemStatus::Failed,
                };
                live[i] = l;
                if let Some(g) = groups.iter_mut().find(|g| g.members.contains(&i)) {
                    g.last_window = Some(Instant::now());
                }
                emit(&live, i, "place-launched");
            }
        }
        // Per-app timeouts.
        for g in groups.iter_mut() {
            if g.timed_out {
                continue;
            }
            if let Some(t0) = g.first_launch {
                if t0.elapsed() > opts.per_app_timeout {
                    g.timed_out = true;
                    for &i in &g.members {
                        if matches!(live[i].status, ItemStatus::Pending | ItemStatus::Launching) {
                            live[i].status = ItemStatus::TimedOut;
                            live[i].message = Some(format!("no matching window within {}s", opts.per_app_timeout.as_secs()));
                            emit(&live, i, "timeout");
                        }
                    }
                }
            }
        }
    }

    // Phase C: stabilization. Apps that overwrite their geometry after map
    // get re-placed a bounded number of times.
    let deadline = Instant::now() + opts.stabilization;
    while Instant::now() < deadline && !cancel.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(150));
        for i in 0..total {
            let l = &live[i];
            if !l.status.is_success() || l.requested.is_none() || l.reapplied >= opts.max_reapply {
                continue;
            }
            let Some(wid) = l.window.clone() else { continue };
            let Ok(w) = host.window(&wid) else { continue };
            if w.state.maximized || w.state.fullscreen {
                continue;
            }
            let req = l.requested.unwrap();
            let tol = opts.tolerance_px.max(w.constraints.inc_w.unwrap_or(0)).max(w.constraints.inc_h.unwrap_or(0));
            if w.frame.max_edge_delta(&req) > tol {
                live[i].reapplied += 1;
                if let Ok(o) = host.place(&wid, req) {
                    live[i].observed = o.observed;
                    live[i].message = Some(format!("re-applied geometry after app changed it (attempt {})", live[i].reapplied));
                    emit(&live, i, "stabilize");
                }
            }
        }
    }

    // Phase D: minimized state last (so geometry applied first), then focus.
    for i in 0..total {
        if !live[i].status.is_success() {
            continue;
        }
        if let (Some(st), Some(wid)) = (&p.targets[i].state, &live[i].window) {
            if st.minimized {
                let _ = host.set_state(wid, st);
            }
        }
    }
    if opts.restore_focus {
        if let Some(i) = (0..total).find(|&i| p.targets[i].record.focused && live[i].status.is_success()) {
            if let Some(w) = &live[i].window {
                let _ = host.activate(w);
            }
        }
    }

    let items = (0..total)
        .map(|i| {
            let t = &p.targets[i];
            let l = &live[i];
            ItemResult { record_id: t.record.record_id.clone(), label: t.label.clone(), status: if l.status == ItemStatus::Pending { ItemStatus::Skipped } else { l.status }, window_id: l.window.clone(), message: l.message.clone(), requested: l.requested, observed: l.observed, launched: l.launched }
        })
        .collect();
    RunReport { run_id: opts.run_id.clone(), items, cancelled: cancel.load(Ordering::SeqCst), timed_out, launches: launches_total, duration_ms: start.elapsed().as_millis() as u64, warnings }
}

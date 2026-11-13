//! Hyprland adapter. Hyprland has its own IPC: a request socket
//! (`.socket.sock`: one request per connection, JSON queries with the `j/`
//! flag, `dispatch` for actions) and an event socket (`.socket2.sock`: lines
//! of `EVENT>>DATA`). Both live in `$XDG_RUNTIME_DIR/hypr/<instance>/`, a
//! directory only this user can open.
//!
//! Hyprland 0.57 and later read a Lua configuration and evaluate every
//! `dispatch` argument as a Lua expression (`return hl.dispatch(<expr>)`).
//! The classic `dispatch movewindowpixel ...` and `keyword ...` forms are
//! refused there. This adapter speaks the Lua form only; older Hyprland
//! versions are reported as unsupported with that reason.
//!
//! Every value that goes into a Lua expression is either a number or a
//! string literal built by `lua_str`, which passes only a small set of
//! characters through and writes everything else as a decimal escape, so no
//! input can end the literal.
//!
//! Hyprland tiles new windows. A grid placement makes the window floating
//! (that is the only way to give a window an exact rectangle in Hyprland);
//! the capability report says so, and the layout setting itself is never
//! changed.

use fractile_core::accel::Accelerator;
use fractile_core::adapter::*;
use fractile_core::geometry::Rect;
use fractile_core::output::{Output, Transform};
use fractile_core::window::{WindowInfo, WindowState, WindowType};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Fractile's Wayland app id (the UI sets it as the desktop file name).
const OWN_APP_ID: &str = "io.fractile.Fractile";
/// Fractile windows that should float from the moment they map: the panel,
/// palette, preview and the small dialogs. Settings and the dashboard are
/// ordinary windows and are tiled like any other.
const OWN_FLOAT_TITLES: &str = "^(Fractile panel .*|Fractile command palette|Fractile preview|Welcome to Fractile|Restore windows)$";
const RULE_NAME: &str = "fractile-own-windows";
/// Description given to every bind Fractile adds, so its binds can be told
/// apart from the user's in `hyprctl binds`.
const BIND_PREFIX: &str = "Fractile: ";

const SHIFT: u64 = 1;
const CTRL: u64 = 4;
const ALT: u64 = 8;
const SUPER: u64 = 64;

pub struct HyprlandAdapter {
    inner: Arc<Inner>,
    subscribed: Mutex<bool>,
}

struct Inner {
    dir: PathBuf,
    version: String,
    /// Per-instance random part of the hotkey event names: a bind left
    /// behind by an earlier service (killed before it could unbind) cannot
    /// trigger an action in this one.
    nonce: String,
    state: Mutex<State>,
    stop: AtomicBool,
    events: Mutex<Option<UnixStream>>,
}

#[derive(Default)]
struct State {
    own: HashSet<String>,
    hotkeys: BTreeMap<String, Hotkey>,
    next_token: u64,
    border: i32,
    last_active: Option<String>,
    /// Hotkeys not added back after the last config reload because the
    /// user's configuration now binds the same keys (shown in the
    /// capability notes, so `fractile diagnose` says so).
    lost: std::collections::BTreeSet<String>,
}

#[derive(Clone)]
struct Hotkey {
    combo: String,
    mask: u64,
    key: String,
    token: String,
    /// False when the keys are now bound by the user's configuration (seen
    /// after a reload); the bind is not ours to remove then.
    active: bool,
}

fn uid() -> u32 {
    // SAFETY: getuid has no preconditions and cannot fail.
    unsafe { libc::getuid() }
}

fn trunc(s: &str) -> String {
    let t = s.trim();
    if t.chars().count() > 300 {
        format!("{}...", t.chars().take(300).collect::<String>())
    } else {
        t.to_string()
    }
}

/// A Lua string literal. Letters, digits and a few punctuation characters
/// are kept; every other byte (quotes, backslashes, brackets, newlines,
/// non-ASCII) becomes a `\ddd` escape.
fn lua_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for b in s.bytes() {
        match b {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b' ' | b'_' | b'-' | b'+' | b':' | b'.' | b',' => o.push(b as char),
            _ => o.push_str(&format!("\\{b:03}")),
        }
    }
    o.push('"');
    o
}

/// "hypr:0x55d0c0a1b2c0" -> "0x55d0c0a1b2c0".
fn parse_id(id: &str) -> AResult<String> {
    let a = id.strip_prefix("hypr:").ok_or_else(|| AdapterError::NoSuchWindow(id.to_string()))?;
    let hex = a.strip_prefix("0x").ok_or_else(|| AdapterError::NoSuchWindow(id.to_string()))?;
    if hex.is_empty() || hex.len() > 16 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(AdapterError::NoSuchWindow(id.to_string()));
    }
    Ok(a.to_ascii_lowercase())
}

/// Event payloads carry addresses without the 0x prefix.
fn event_id(addr: &str) -> Option<String> {
    let a = addr.trim();
    let a = a.strip_prefix("0x").unwrap_or(a);
    if a.is_empty() || !a.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    Some(format!("hypr:0x{}", a.to_ascii_lowercase()))
}

fn i(v: &Value) -> i64 {
    v.as_i64().or_else(|| v.as_f64().map(|f| f.round() as i64)).unwrap_or(0)
}

fn s(v: &Value) -> String {
    v.as_str().unwrap_or_default().to_string()
}

fn box_of(c: &Value) -> Rect {
    Rect::new(i(&c["at"][0]) as i32, i(&c["at"][1]) as i32, i(&c["size"][0]) as i32, i(&c["size"][1]) as i32)
}

/// The visible frame: Hyprland draws the border outside the window box.
/// Fullscreen windows have none.
fn frame_of(c: &Value, border: i32) -> Rect {
    let b = box_of(c);
    if i(&c["fullscreen"]) >= 2 || border <= 0 {
        return b;
    }
    Rect::new(b.x - border, b.y - border, b.w + 2 * border, b.h + 2 * border)
}

/// Bind modifier mask and the `hl.bind` key string, e.g. "SUPER + ALT + g".
fn combo_for(acc: &Accelerator) -> (u64, String) {
    let mut mask = 0;
    let mut parts: Vec<&str> = Vec::new();
    if acc.mods.super_ {
        mask |= SUPER;
        parts.push("SUPER");
    }
    if acc.mods.ctrl {
        mask |= CTRL;
        parts.push("CTRL");
    }
    if acc.mods.alt {
        mask |= ALT;
        parts.push("ALT");
    }
    if acc.mods.shift {
        mask |= SHIFT;
        parts.push("SHIFT");
    }
    parts.push(&acc.key);
    (mask, parts.join(" + "))
}

/// Two key names for the same key (xkb accepts several spellings).
fn same_key(a: &str, b: &str) -> bool {
    let canon = |k: &str| -> String {
        let l = k.to_ascii_lowercase();
        match l.as_str() {
            "page_up" => "prior".into(),
            "page_down" => "next".into(),
            "enter" => "return".into(),
            _ => l,
        }
    };
    canon(a) == canon(b)
}

fn transform_of(t: i64) -> Transform {
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

/// Monitors in Fractile's model. Hyprland reports the mode in physical
/// pixels and the position in the logical layout; the logical size is the
/// mode divided by the scale (rotated for 90/270 transforms). `reserved`
/// (left, top, right, bottom) is the space bars and layer surfaces claim.
fn outputs_from(mons: &Value) -> Vec<(i64, Output)> {
    let Some(arr) = mons.as_array() else { return vec![] };
    let min_id = arr.iter().map(|m| i(&m["id"])).min().unwrap_or(0);
    arr.iter()
        .map(|m| {
            let scale = m["scale"].as_f64().filter(|v| *v > 0.0).unwrap_or(1.0);
            let t = i(&m["transform"]);
            let (pw, ph) = (i(&m["width"]) as f64, i(&m["height"]) as f64);
            let (pw, ph) = if t % 2 == 1 { (ph, pw) } else { (pw, ph) };
            let (lw, lh) = ((pw / scale).round() as i32, (ph / scale).round() as i32);
            let rect = Rect::new(i(&m["x"]) as i32, i(&m["y"]) as i32, lw, lh);
            let r = &m["reserved"];
            let (l, tp, rt, bt) = (i(&r[0]) as i32, i(&r[1]) as i32, i(&r[2]) as i32, i(&r[3]) as i32);
            let work = Rect::new(rect.x + l, rect.y + tp, (lw - l - rt).max(1), (lh - tp - bt).max(1));
            let name = s(&m["name"]);
            let (make, model, serial) = (s(&m["make"]), s(&m["model"]), s(&m["serial"]));
            // make/model/serial come from the monitor's EDID.
            let edid = !serial.is_empty() || (!make.is_empty() && !model.is_empty());
            let stable_id = if edid {
                format!("edid-{}", hex::encode(&Sha256::digest(format!("{make}|{model}|{serial}").as_bytes())[..10]))
            } else {
                format!("conn-{name}-{}x{}", pw as i64, ph as i64)
            };
            let desc = s(&m["description"]);
            let (mw, mh) = (i(&m["physicalWidth"]), i(&m["physicalHeight"]));
            (
                i(&m["id"]),
                Output {
                    stable_id,
                    stable_id_from_edid: edid,
                    name,
                    description: if desc.is_empty() { None } else { Some(desc) },
                    rect,
                    work_area: work,
                    scale,
                    transform: transform_of(t),
                    primary: i(&m["id"]) == min_id,
                    physical_mm: if mw > 0 && mh > 0 { Some((mw as u32, mh as u32)) } else { None },
                },
            )
        })
        .collect()
}

fn runtime_dir() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", uid())))
}

/// The instance directory, owned by this user, with a request socket.
fn usable_instance(dir: &Path) -> bool {
    let sock = dir.join(".socket.sock");
    let owned = |p: &Path| std::fs::symlink_metadata(p).map(|m| m.uid() == uid()).unwrap_or(false);
    owned(dir) && owned(&sock) && UnixStream::connect(&sock).is_ok()
}

/// Finds the running Hyprland instance. HYPRLAND_INSTANCE_SIGNATURE names it
/// when the service inherited the session environment; a systemd user
/// service may not have it, and then the newest live instance of this user
/// is used.
fn find_instance() -> Result<PathBuf, String> {
    let bases = [runtime_dir().join("hypr"), PathBuf::from("/tmp/hypr")];
    if let Some(sig) = std::env::var("HYPRLAND_INSTANCE_SIGNATURE").ok().filter(|v| !v.is_empty()) {
        if sig.contains('/') || sig.starts_with('.') {
            return Err(format!("HYPRLAND_INSTANCE_SIGNATURE is not a valid instance name: {sig:?}"));
        }
        for b in &bases {
            let d = b.join(&sig);
            if usable_instance(&d) {
                return Ok(d);
            }
        }
        return Err(format!("HYPRLAND_INSTANCE_SIGNATURE={sig}, but no Hyprland socket of this user answers at {}", bases[0].join(&sig).join(".socket.sock").display()));
    }
    let mut found: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
    for b in &bases {
        let Ok(rd) = std::fs::read_dir(b) else { continue };
        for e in rd.flatten() {
            let d = e.path();
            if usable_instance(&d) {
                let t = std::fs::metadata(d.join(".socket.sock")).and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH);
                found.push((t, d));
            }
        }
    }
    found.sort();
    found.pop().map(|(_, d)| d).ok_or_else(|| format!("no running Hyprland instance of this user (looked in {})", bases[0].display()))
}

fn random_hex() -> String {
    let mut b = [0u8; 6];
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        let _ = f.read_exact(&mut b);
    }
    if b == [0; 6] {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        b.copy_from_slice(&((t as u64) ^ ((std::process::id() as u64) << 32)).to_le_bytes()[..6]);
    }
    hex::encode(b)
}

impl Inner {
    fn request(&self, cmd: &str) -> AResult<String> {
        let mut st = UnixStream::connect(self.dir.join(".socket.sock")).map_err(|e| AdapterError::Disconnected(format!("Hyprland IPC socket: {e}")))?;
        st.set_read_timeout(Some(Duration::from_secs(3))).ok();
        st.set_write_timeout(Some(Duration::from_secs(3))).ok();
        st.write_all(cmd.as_bytes()).map_err(|e| AdapterError::Disconnected(format!("Hyprland IPC write: {e}")))?;
        let mut out = Vec::new();
        st.read_to_end(&mut out).map_err(|e| match e.kind() {
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => AdapterError::Timeout("Hyprland did not answer within 3 s".into()),
            _ => AdapterError::Disconnected(format!("Hyprland IPC read: {e}")),
        })?;
        Ok(String::from_utf8_lossy(&out).into_owned())
    }

    fn json(&self, what: &str) -> AResult<Value> {
        let r = self.request(&format!("j/{what}"))?;
        serde_json::from_str(&r).map_err(|e| AdapterError::Other(format!("Hyprland '{what}' reply is not JSON ({e}): {}", trunc(&r))))
    }

    /// Runs one Lua dispatch expression; Hyprland answers "ok" or an error.
    fn dispatch(&self, lua: &str) -> AResult<()> {
        let r = self.request(&format!("/dispatch {lua}"))?;
        if r.trim() == "ok" {
            Ok(())
        } else {
            Err(AdapterError::Rejected(format!("Hyprland: {}", trunc(&r))))
        }
    }

    fn clients(&self) -> AResult<Vec<Value>> {
        match self.json("clients")? {
            Value::Array(a) => Ok(a.into_iter().filter(|c| c["mapped"].as_bool().unwrap_or(true)).collect()),
            v => Err(AdapterError::Other(format!("Hyprland clients: unexpected reply {}", trunc(&v.to_string())))),
        }
    }

    fn client(&self, addr: &str) -> AResult<Value> {
        self.clients()?
            .into_iter()
            .find(|c| s(&c["address"]).eq_ignore_ascii_case(addr))
            .ok_or_else(|| AdapterError::NoSuchWindow(format!("hypr:{addr}")))
    }

    fn active(&self) -> AResult<Option<String>> {
        let v = self.json("activewindow")?;
        Ok(v["address"].as_str().filter(|a| !a.is_empty()).map(|a| format!("hypr:{}", a.to_ascii_lowercase())))
    }

    fn border(&self) -> i32 {
        self.json("getoption general:border_size").map(|v| i(&v["int"]) as i32).unwrap_or(0).clamp(0, 50)
    }

    /// Fractile's own transient windows float when they map, so opening
    /// the panel does not re-tile the user's windows. This is a runtime rule
    /// that matches only Fractile's app id; Hyprland drops it on a config
    /// reload and it is added again then.
    fn install_own_rule(&self) -> AResult<()> {
        let class = format!("^({})$", OWN_APP_ID.replace('.', "\\."));
        self.dispatch(&format!(
            "(function() hl.window_rule({{ name = {}, match = {{ class = {}, title = {} }}, float = true }}) end)",
            lua_str(RULE_NAME),
            lua_str(&class),
            lua_str(OWN_FLOAT_TITLES)
        ))
    }

    /// Binds in the active (default) submap that use these keys: `Ok(None)`
    /// when there are none, `Ok(Some((true, _)))` when all are Fractile's,
    /// otherwise the user's bind (its description or dispatcher).
    fn existing_bind(&self, mask: u64, key: &str) -> AResult<Option<(bool, String)>> {
        let binds = self.json("binds")?;
        let mut found = None;
        for b in binds.as_array().into_iter().flatten() {
            if b["mouse"].as_bool().unwrap_or(false) || !s(&b["submap"]).is_empty() {
                continue;
            }
            if b["modmask"].as_u64() != Some(mask) || !same_key(&s(&b["key"]), key) {
                continue;
            }
            let desc = s(&b["description"]);
            if !desc.starts_with(BIND_PREFIX) {
                return Ok(Some((false, if desc.is_empty() { s(&b["dispatcher"]) } else { desc })));
            }
            found = Some((true, desc));
        }
        Ok(found)
    }

    /// Adds the bind and keeps Hyprland's handle for it in a Lua table, so
    /// that removing it later removes exactly this bind and never a user's
    /// bind on the same keys.
    fn bind(&self, binding_id: &str, hk: &Hotkey) -> AResult<()> {
        self.dispatch(&format!(
            "(function() _G.fractile_binds = _G.fractile_binds or {{}}; _G.fractile_binds[{t}] = hl.bind({}, hl.dsp.event({t}), {{ description = {} }}) end)",
            lua_str(&hk.combo),
            lua_str(&format!("{BIND_PREFIX}{binding_id}")),
            t = lua_str(&hk.token),
        ))
    }

    fn unbind(&self, hk: &Hotkey) -> AResult<()> {
        self.dispatch(&format!(
            "(function() local t = _G.fractile_binds; local b = t and t[{k}]; if b then b:remove(); t[{k}] = nil end end)",
            k = lua_str(&hk.token)
        ))
    }

    /// Binds an earlier Fractile service added and could not remove (it was
    /// killed): those still in the Lua table are removed through their
    /// handles. One whose handle is gone (only its description is left) is
    /// removed by its keys, which `existing_bind` has checked hold no user
    /// bind.
    fn remove_stale_binds(&self) -> AResult<()> {
        self.dispatch("(function() local t = _G.fractile_binds; if t then for _, b in pairs(t) do b:remove() end end; _G.fractile_binds = {} end)")
    }

    fn unbind_keys(&self, combo: &str) -> AResult<()> {
        self.dispatch(&format!("(function() hl.unbind({}) end)", lua_str(combo)))
    }

    /// After Hyprland reloads its configuration, runtime binds and rules are
    /// gone: add them again (unless the user's configuration now uses the
    /// same keys).
    fn reapply(&self) {
        let b = self.border();
        let hotkeys: Vec<(String, Hotkey)> = {
            let mut st = self.state.lock().unwrap();
            st.border = b;
            st.lost.clear();
            st.hotkeys.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
        };
        let _ = self.install_own_rule();
        for (id, hk) in hotkeys {
            let free = match self.existing_bind(hk.mask, &hk.key) {
                Ok(None) => true,
                Ok(Some((true, _))) => {
                    let _ = self.unbind(&hk);
                    true
                }
                _ => false,
            };
            let ok = free && self.bind(&id, &hk).is_ok();
            let mut st = self.state.lock().unwrap();
            if let Some(h) = st.hotkeys.get_mut(&id) {
                h.active = ok;
            }
            if !ok {
                st.lost.insert(id);
            }
        }
    }

    fn handle_event(&self, line: &str, tx: &Sender<AdapterEvent>) -> bool {
        let Some((name, data)) = line.split_once(">>") else { return true };
        let first = data.split(',').next().unwrap_or("");
        let ev = match name {
            "openwindow" => event_id(first).map(|id| AdapterEvent::WindowCreated { id }),
            "closewindow" => event_id(first).map(|id| AdapterEvent::WindowClosed { id }),
            "movewindowv2" | "windowtitlev2" | "changefloatingmode" | "pin" | "minimized" => event_id(first).map(|id| AdapterEvent::WindowChanged { id }),
            "fullscreen" => self.state.lock().unwrap().last_active.clone().map(|id| AdapterEvent::WindowChanged { id }),
            "activewindowv2" => {
                let id = event_id(first);
                self.state.lock().unwrap().last_active = id.clone();
                Some(AdapterEvent::ActiveChanged { id })
            }
            "monitoraddedv2" | "monitorremovedv2" => Some(AdapterEvent::OutputsChanged),
            "workspacev2" | "createworkspacev2" | "destroyworkspacev2" | "moveworkspacev2" | "renameworkspace" => Some(AdapterEvent::WorkspacesChanged),
            "configreloaded" => {
                self.reapply();
                Some(AdapterEvent::OutputsChanged)
            }
            "custom" => {
                let st = self.state.lock().unwrap();
                st.hotkeys
                    .iter()
                    .find(|(_, h)| h.active && h.token == data.trim())
                    .map(|(id, _)| AdapterEvent::Hotkey { binding_id: id.clone(), timestamp: 0 })
            }
            _ => None,
        };
        match ev {
            Some(e) => tx.send(e).is_ok(),
            None => true,
        }
    }

    /// Polls the window until its frame reaches `target` or stops changing.
    fn settle(&self, addr: &str, target: Rect, tol: i32) -> AResult<Rect> {
        let start = Instant::now();
        let mut last: Option<Rect> = None;
        let mut same = 0;
        loop {
            let border = self.state.lock().unwrap().border;
            let r = frame_of(&self.client(addr)?, border);
            if r.max_edge_delta(&target) <= tol {
                return Ok(r);
            }
            if last == Some(r) {
                same += 1;
            } else {
                same = 0;
            }
            last = Some(r);
            if (same >= 3 && start.elapsed() > Duration::from_millis(200)) || start.elapsed() > Duration::from_millis(1500) {
                return Ok(r);
            }
            std::thread::sleep(Duration::from_millis(40));
        }
    }

    fn wait_until(&self, timeout: Duration, mut f: impl FnMut() -> bool) -> bool {
        let end = Instant::now() + timeout;
        while Instant::now() < end {
            if f() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(30));
        }
        f()
    }
}

impl HyprlandAdapter {
    /// Connects to the running Hyprland instance and checks that it takes
    /// the Lua dispatch form this adapter uses.
    pub fn connect() -> Result<HyprlandAdapter, String> {
        let dir = find_instance()?;
        let mut inner = Inner {
            dir,
            version: String::new(),
            nonce: random_hex(),
            state: Mutex::new(State::default()),
            stop: AtomicBool::new(false),
            events: Mutex::new(None),
        };
        let v = inner.json("version").map_err(|e| format!("Hyprland did not answer a version query: {e}"))?;
        inner.version = v["tag"].as_str().map(|t| t.trim_start_matches('v').to_string()).filter(|t| !t.is_empty()).unwrap_or_else(|| s(&v["version"]));
        if let Err(e) = inner.dispatch("(function() end)") {
            return Err(format!(
                "Hyprland {} does not take Lua dispatch expressions over IPC ({e}). Fractile's Hyprland adapter needs Hyprland 0.57 or later.",
                inner.version
            ));
        }
        let b = inner.border();
        inner.state.lock().unwrap().border = b;
        let _ = inner.remove_stale_binds();
        inner.install_own_rule().map_err(|e| format!("Hyprland refused Fractile's window rule for its own windows: {e}"))?;
        Ok(HyprlandAdapter { inner: Arc::new(inner), subscribed: Mutex::new(false) })
    }

    pub fn version(&self) -> &str {
        &self.inner.version
    }

    pub fn instance_dir(&self) -> &Path {
        &self.inner.dir
    }

    fn window_infos(&self) -> AResult<Vec<WindowInfo>> {
        let clients = self.inner.clients()?;
        let outs = outputs_from(&self.inner.json("monitors")?);
        let active = self.inner.active()?;
        let (own, border) = {
            let st = self.inner.state.lock().unwrap();
            (st.own.clone(), st.border)
        };
        // Hyprland has no global stacking list: floating windows are above
        // tiled ones, and within each the most recently focused is on top.
        let mut order: Vec<usize> = (0..clients.len()).collect();
        order.sort_by_key(|&k| (clients[k]["floating"].as_bool().unwrap_or(false), -i(&clients[k]["focusHistoryID"])));
        let mut stacking = vec![0u32; clients.len()];
        for (pos, k) in order.into_iter().enumerate() {
            stacking[k] = pos as u32;
        }
        Ok(clients
            .iter()
            .enumerate()
            .map(|(k, c)| {
                let addr = s(&c["address"]).to_ascii_lowercase();
                let id = format!("hypr:{addr}");
                let pid = Some(i(&c["pid"])).filter(|p| *p > 0).map(|p| p as u32);
                let class = s(&c["class"]);
                let xwayland = c["xwayland"].as_bool().unwrap_or(false);
                let fs = i(&c["fullscreen"]);
                let floating = c["floating"].as_bool().unwrap_or(false);
                let ws = i(&c["workspace"]["id"]);
                let frame = frame_of(c, border);
                let mon = i(&c["monitor"]);
                let is_own = own.contains(&id) || pid.map(|p| own.contains(&format!("pid:{p}"))).unwrap_or(false);
                WindowInfo {
                    id: id.clone(),
                    title: s(&c["title"]),
                    app_id: if !xwayland && !class.is_empty() { Some(class.clone()) } else { None },
                    wm_class_instance: None,
                    wm_class: if class.is_empty() { None } else { Some(class) },
                    pid,
                    window_type: WindowType::Normal,
                    frame,
                    client: Some(box_of(c)),
                    state: WindowState {
                        maximized: fs == 1,
                        fullscreen: fs >= 2,
                        // Hyprland has no minimize; a window on a special
                        // (scratchpad) workspace is out of sight in the same way.
                        minimized: ws < 0,
                        sticky: c["pinned"].as_bool().unwrap_or(false),
                        tiled: !floating && fs == 0,
                        ..Default::default()
                    },
                    workspace: if ws > 0 { Some((ws - 1) as u32) } else { None },
                    output_id: outs.iter().find(|(mid, _)| *mid == mon).map(|(_, o)| o.stable_id.clone()),
                    stacking: Some(stacking[k]),
                    focused: active.as_deref() == Some(id.as_str()),
                    is_own,
                    ..Default::default()
                }
            })
            .collect())
    }
}

impl CompositorAdapter for HyprlandAdapter {
    fn capabilities(&self) -> Capabilities {
        use Operation::*;
        use SupportLevel::*;
        let mut ops = BTreeMap::new();
        ops.insert(ListWindows, Full);
        ops.insert(GetActiveWindow, Full);
        ops.insert(GetOutputs, Full);
        ops.insert(GetWorkspaces, Partial("Hyprland creates workspaces on demand: listed are the ones that exist now (index = workspace id - 1); special workspaces are not listed".into()));
        ops.insert(GetGeometry, Full);
        ops.insert(MoveResize, Partial("a tiled window is made floating so it can take the exact rectangle; the result is verified".into()));
        ops.insert(SetState, Partial("maximize, fullscreen and pin (sticky); Hyprland has no minimize or keep-above".into()));
        ops.insert(MoveToOutput, Partial("by placing the window inside the other monitor (it is made floating)".into()));
        ops.insert(MoveToWorkspace, Full);
        ops.insert(Activate, Full);
        ops.insert(SubscribeEvents, Full);
        ops.insert(RegisterHotkey, Partial("runtime binds through Hyprland's IPC, added again after a config reload; keys the Hyprland configuration already binds are reported as a conflict".into()));
        ops.insert(ShowOverlay, Unsupported("Hyprland has no IPC to draw a preview; the panel shows the selection only".into()));
        ops.insert(GetPermissions, Full);
        let mut notes = vec![
            "Hyprland tiles new windows. Placing a window with Fractile makes that window floating; Hyprland's layout settings are not changed.".into(),
            format!("Fractile's panel and dialogs float through a runtime window rule ({RULE_NAME}) that matches only Fractile's own app id."),
        ];
        let lost = self.inner.state.lock().unwrap().lost.iter().cloned().collect::<Vec<_>>();
        if !lost.is_empty() {
            notes.push(format!(
                "after the last Hyprland config reload these shortcuts are not bound, because the configuration now binds the same keys: {}",
                lost.join(", ")
            ));
        }
        Capabilities {
            adapter: "hyprland".into(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: "wayland".into(),
            compositor: "Hyprland".into(),
            compositor_version: Some(self.inner.version.clone()),
            operations: ops,
            tiling_manager: None,
            notes,
        }
    }

    fn list_windows(&self) -> AResult<Vec<WindowInfo>> {
        self.window_infos()
    }

    fn get_active_window(&self) -> AResult<Option<String>> {
        self.inner.active()
    }

    fn get_outputs(&self) -> AResult<Vec<Output>> {
        Ok(outputs_from(&self.inner.json("monitors")?).into_iter().map(|(_, o)| o).collect())
    }

    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        let wss = self.inner.json("workspaces")?;
        let mons = self.inner.json("monitors")?;
        let focused_ws = mons
            .as_array()
            .into_iter()
            .flatten()
            .find(|m| m["focused"].as_bool().unwrap_or(false))
            .map(|m| i(&m["activeWorkspace"]["id"]));
        let mut v: Vec<(i64, String)> = wss.as_array().into_iter().flatten().map(|w| (i(&w["id"]), s(&w["name"]))).filter(|(id, _)| *id > 0).collect();
        v.sort();
        Ok(v.into_iter().map(|(id, name)| Workspace { index: (id - 1) as u32, name, active: Some(id) == focused_ws }).collect())
    }

    fn move_resize(&self, id: &str, target: Rect) -> AResult<PlacementOutcome> {
        let addr = parse_id(id)?;
        if target.w <= 0 || target.h <= 0 {
            return Err(AdapterError::Rejected("empty target rectangle".into()));
        }
        let sel = lua_str(&format!("address:{addr}"));
        let c = self.inner.client(&addr)?;
        if i(&c["fullscreen"]) != 0 {
            self.inner.dispatch(&format!("hl.dsp.window.fullscreen_state({{ internal = 0, client = 0, window = {sel} }})"))?;
            self.inner.wait_until(Duration::from_millis(600), || self.inner.client(&addr).map(|c| i(&c["fullscreen"]) == 0).unwrap_or(true));
        }
        let was_tiled = !c["floating"].as_bool().unwrap_or(false);
        // A floating window moved by coordinates stays on its monitor's
        // workspace in Hyprland, even when it is drawn on another monitor.
        // When the target is on another monitor, the window first goes to
        // the workspace that monitor shows.
        let mons = self.inner.json("monitors")?;
        let outs = outputs_from(&mons);
        let dest = outs.iter().max_by_key(|(_, o)| o.rect.intersection_area(&target)).map(|(id, _)| *id);
        let to_ws = match dest {
            Some(m) if m != i(&c["monitor"]) && i(&c["workspace"]["id"]) > 0 => mons
                .as_array()
                .into_iter()
                .flatten()
                .find(|x| i(&x["id"]) == m)
                .map(|x| i(&x["activeWorkspace"]["id"]))
                .filter(|w| *w > 0),
            _ => None,
        };
        if let Some(ws) = to_ws {
            self.inner.dispatch(&format!("hl.dsp.window.move({{ workspace = {ws}, follow = false, window = {sel} }})"))?;
        }
        let tol = 2;
        let mut corr = [0i32; 4];
        let mut observed = None;
        let mut attempts = 0;
        for attempt in 1..=3 {
            attempts = attempt;
            let b = self.inner.state.lock().unwrap().border;
            let (cx, cy) = (target.x + b + corr[0], target.y + b + corr[1]);
            let (cw, ch) = ((target.w - 2 * b + corr[2]).max(1), (target.h - 2 * b + corr[3]).max(1));
            // One expression, so the window is floated, sized and moved in
            // one step. Resize keeps the window's centre, so it goes first.
            self.inner.dispatch(&format!(
                "(function() hl.dispatch(hl.dsp.window.float({{ action = \"enable\", window = {sel} }})) \
                 hl.dispatch(hl.dsp.window.resize({{ x = {cw}, y = {ch}, window = {sel} }})) \
                 hl.dispatch(hl.dsp.window.move({{ x = {cx}, y = {cy}, window = {sel} }})) end)"
            ))?;
            let obs = self.inner.settle(&addr, target, tol)?;
            observed = Some(obs);
            if obs.max_edge_delta(&target) <= tol {
                break;
            }
            corr[0] += target.x - obs.x;
            corr[1] += target.y - obs.y;
            corr[2] += target.w - obs.w;
            corr[3] += target.h - obs.h;
        }
        let err = observed.map(|o| o.max_edge_delta(&target));
        Ok(PlacementOutcome {
            window_id: id.into(),
            requested: target,
            observed,
            max_error_px: err,
            within_tolerance: err.map(|e| e <= tol).unwrap_or(false),
            attempts,
            note: if was_tiled { Some("the window was tiled; Hyprland now floats it at the placed size".into()) } else { None },
        })
    }

    fn set_state(&self, id: &str, want: &WindowState) -> AResult<()> {
        let addr = parse_id(id)?;
        let sel = lua_str(&format!("address:{addr}"));
        let c = self.inner.client(&addr)?;
        let cur = i(&c["fullscreen"]);
        let mode = if want.fullscreen { 2 } else if want.maximized { 1 } else { 0 };
        if mode != cur {
            let client = if mode == 2 { 2 } else { 0 };
            self.inner.dispatch(&format!("hl.dsp.window.fullscreen_state({{ internal = {mode}, client = {client}, window = {sel} }})"))?;
        }
        let pinned = c["pinned"].as_bool().unwrap_or(false);
        if want.sticky != pinned {
            // Hyprland pins floating windows only.
            if want.sticky && !c["floating"].as_bool().unwrap_or(false) {
                self.inner.dispatch(&format!("hl.dsp.window.float({{ action = \"enable\", window = {sel} }})"))?;
            }
            let action = if want.sticky { "enable" } else { "disable" };
            self.inner.dispatch(&format!("hl.dsp.window.pin({{ action = \"{action}\", window = {sel} }})"))?;
        }
        Ok(())
    }

    fn move_to_workspace(&self, id: &str, workspace: u32) -> AResult<()> {
        let addr = parse_id(id)?;
        self.inner.client(&addr)?;
        let ws = workspace as i64 + 1;
        self.inner.dispatch(&format!("hl.dsp.window.move({{ workspace = {ws}, follow = false, window = {} }})", lua_str(&format!("address:{addr}"))))
    }

    fn activate(&self, id: &str, _timestamp: Option<u32>) -> AResult<()> {
        let addr = parse_id(id)?;
        self.inner.client(&addr)?;
        self.inner.dispatch(&format!("hl.dsp.focus({{ window = {} }})", lua_str(&format!("address:{addr}"))))
    }

    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        {
            let mut sub = self.subscribed.lock().unwrap();
            if *sub {
                return Err(AdapterError::Other("already subscribed".into()));
            }
            *sub = true;
        }
        let sock = UnixStream::connect(self.inner.dir.join(".socket2.sock")).map_err(|e| AdapterError::Disconnected(format!("Hyprland event socket: {e}")))?;
        *self.inner.events.lock().unwrap() = sock.try_clone().ok();
        self.inner.state.lock().unwrap().last_active = self.inner.active().ok().flatten();
        let (tx, rx) = channel();
        let inner = self.inner.clone();
        std::thread::Builder::new()
            .name("hyprland-events".into())
            .spawn(move || {
                let mut reason = "Hyprland closed its event socket (the compositor exited)".to_string();
                for line in BufReader::new(sock).lines() {
                    match line {
                        Ok(l) => {
                            if !inner.handle_event(&l, &tx) {
                                return;
                            }
                        }
                        Err(e) => {
                            reason = format!("Hyprland event socket: {e}");
                            break;
                        }
                    }
                }
                if !inner.stop.load(Ordering::SeqCst) {
                    let _ = tx.send(AdapterEvent::DisplayClosed { reason });
                }
            })
            .map_err(|e| AdapterError::Other(e.to_string()))?;
        Ok(rx)
    }

    fn register_hotkey(&self, binding_id: &str, accelerator: &str) -> AResult<()> {
        let acc = Accelerator::parse(accelerator).map_err(AdapterError::Rejected)?;
        let (mask, combo) = combo_for(&acc);
        let _ = self.unregister_hotkey(binding_id);
        {
            let st = self.inner.state.lock().unwrap();
            if let Some((other, _)) = st.hotkeys.iter().find(|(_, h)| h.mask == mask && same_key(&h.key, &acc.key)) {
                return Err(AdapterError::Conflict(format!("already bound to '{other}'")));
            }
        }
        match self.inner.existing_bind(mask, &acc.key)? {
            None => {}
            // Left behind by an earlier Fractile service that did not exit
            // cleanly: it is Fractile's, so it is replaced.
            Some((true, _)) => self.inner.unbind_keys(&combo)?,
            Some((false, what)) => {
                return Err(AdapterError::Conflict(format!("Hyprland already binds {} ({what})", acc.to_display())));
            }
        }
        let token = {
            let mut st = self.inner.state.lock().unwrap();
            st.next_token += 1;
            format!("fractile:{}:{}", self.inner.nonce, st.next_token)
        };
        let hk = Hotkey { combo, mask, key: acc.key.clone(), token, active: true };
        self.inner.bind(binding_id, &hk)?;
        let mut st = self.inner.state.lock().unwrap();
        st.lost.remove(binding_id);
        st.hotkeys.insert(binding_id.to_string(), hk);
        Ok(())
    }

    fn unregister_hotkey(&self, binding_id: &str) -> AResult<()> {
        let hk = self.inner.state.lock().unwrap().hotkeys.remove(binding_id);
        match hk {
            Some(h) if h.active => self.inner.unbind(&h),
            _ => Ok(()),
        }
    }

    fn set_own_windows(&self, ids: &[String]) {
        self.inner.state.lock().unwrap().own = ids.iter().cloned().collect();
    }
}

impl Drop for HyprlandAdapter {
    fn drop(&mut self) {
        self.inner.stop.store(true, Ordering::SeqCst);
        let hotkeys: Vec<Hotkey> = std::mem::take(&mut self.inner.state.lock().unwrap().hotkeys).into_values().collect();
        for h in hotkeys.into_iter().filter(|h| h.active) {
            let _ = self.inner.unbind(&h);
        }
        if let Some(s) = self.inner.events.lock().unwrap().take() {
            let _ = s.shutdown(std::net::Shutdown::Both);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn lua_strings_cannot_be_escaped() {
        assert_eq!(lua_str("address:0x5c3b"), "\"address:0x5c3b\"");
        let evil = "a\" ) os.execute(\"x\") --\n\\";
        let l = lua_str(evil);
        assert!(l.starts_with('"') && l.ends_with('"'));
        let body = &l[1..l.len() - 1];
        assert!(!body.contains('"') && !body.contains('\n') && !body.contains('(') && !body.contains(')'));
        assert!(body.contains("\\034") && body.contains("\\010") && body.contains("\\092"));
        assert_eq!(lua_str("é"), "\"\\195\\169\"");
    }

    #[test]
    fn ids() {
        assert_eq!(parse_id("hypr:0x5C3BB4E54E60").unwrap(), "0x5c3bb4e54e60");
        assert!(parse_id("hypr:0x").is_err());
        assert!(parse_id("hypr:0x12\"").is_err());
        assert!(parse_id("x11:0x12").is_err());
        assert_eq!(event_id("5c3bb4e54e60").as_deref(), Some("hypr:0x5c3bb4e54e60"));
        assert_eq!(event_id(""), None);
    }

    #[test]
    fn combos() {
        let a = Accelerator::parse("Ctrl+Alt+G").unwrap();
        assert_eq!(combo_for(&a), (CTRL | ALT, "CTRL + ALT + g".to_string()));
        let a = Accelerator::parse("Super+Shift+Left").unwrap();
        assert_eq!(combo_for(&a), (SUPER | SHIFT, "SUPER + SHIFT + Left".to_string()));
        assert!(same_key("left", "Left") && same_key("Page_Up", "Prior") && !same_key("g", "h"));
    }

    #[test]
    fn outputs_scale_reserved_transform() {
        let m = json!([{"id": 0, "name": "DP-1", "make": "Dell", "model": "U2720Q", "serial": "ABC", "description": "Dell U2720Q",
            "width": 3840, "height": 2160, "scale": 2.0, "x": 0, "y": 0, "reserved": [0, 30, 0, 0], "transform": 0},
            {"id": 1, "name": "HDMI-A-1", "make": "", "model": "", "serial": "", "width": 1920, "height": 1080, "scale": 1.0,
            "x": 1920, "y": 0, "reserved": [0, 0, 0, 0], "transform": 1}]);
        let o = outputs_from(&m);
        assert_eq!(o[0].1.rect, Rect::new(0, 0, 1920, 1080));
        assert_eq!(o[0].1.work_area, Rect::new(0, 30, 1920, 1050));
        assert!(o[0].1.stable_id.starts_with("edid-") && o[0].1.stable_id_from_edid && o[0].1.primary);
        assert_eq!(o[1].1.rect, Rect::new(1920, 0, 1080, 1920));
        assert_eq!(o[1].1.stable_id, "conn-HDMI-A-1-1080x1920");
        assert!(!o[1].1.primary);
    }

    #[test]
    fn frame_includes_border_except_fullscreen() {
        let c = json!({"at": [100, 50], "size": [400, 300], "fullscreen": 0});
        assert_eq!(frame_of(&c, 2), Rect::new(98, 48, 404, 304));
        let f = json!({"at": [0, 0], "size": [1600, 1200], "fullscreen": 2});
        assert_eq!(frame_of(&f, 2), Rect::new(0, 0, 1600, 1200));
    }
}

//! Sway adapter. Sway speaks the i3 IPC protocol on a Unix socket
//! (`$SWAYSOCK`, normally `$XDG_RUNTIME_DIR/sway-ipc.<uid>.<pid>.sock`):
//! framed messages (`i3-ipc`, length, type, JSON payload) for the tree,
//! outputs, workspaces and commands, and a subscription stream for events.
//!
//! Commands are Sway command strings. Everything Fractile puts into one is
//! either a number (container ids, pixel sizes), a fixed string of this
//! file, a key name checked to be letters, digits and `_`, or a hotkey token
//! made of hex digits; no window title, app id or user text is ever placed
//! in a command.
//!
//! Sway tiles new windows. A grid placement makes the window floating (the
//! only way to give a window an exact rectangle in Sway); the capability
//! report says so, and the layout itself is never changed.
//!
//! Sway's IPC cannot list key bindings, and a runtime `bindsym` on keys the
//! configuration already binds silently replaces that binding. So before
//! binding, the adapter reads the configuration (the main file Sway reports,
//! and the files it includes) and refuses keys bound there.

use fractile_core::accel::Accelerator;
use fractile_core::adapter::*;
use fractile_core::geometry::Rect;
use fractile_core::output::{Output, Transform};
use fractile_core::window::{WindowInfo, WindowState, WindowType};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::io::{Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const MAGIC: &[u8; 6] = b"i3-ipc";
const RUN_COMMAND: u32 = 0;
const GET_WORKSPACES: u32 = 1;
const SUBSCRIBE: u32 = 2;
const GET_OUTPUTS: u32 = 3;
const GET_TREE: u32 = 4;
const GET_VERSION: u32 = 7;
const GET_CONFIG: u32 = 9;
const EV_WORKSPACE: u32 = 0x8000_0000;
const EV_OUTPUT: u32 = 0x8000_0001;
const EV_WINDOW: u32 = 0x8000_0003;
const EV_BINDING: u32 = 0x8000_0005;
const EV_SHUTDOWN: u32 = 0x8000_0006;
/// Largest reply accepted (a big tree is a few hundred KiB).
const MAX_REPLY: usize = 32 << 20;

/// Fractile's Wayland app id (the UI sets it as the desktop file name).
const OWN_APP_ID_RE: &str = "^io\\.fractile\\.Fractile$";
/// Fractile windows that float from the moment they map: the panel,
/// palette, preview and the small dialogs.
const OWN_FLOAT_TITLES: &str = "^(Fractile panel .*|Fractile command palette|Fractile preview|Welcome to Fractile|Restore windows)$";

pub struct SwayAdapter {
    inner: Arc<Inner>,
    subscribed: Mutex<bool>,
}

struct Inner {
    sock: PathBuf,
    version: String,
    /// Per-service random part of the hotkey tokens: a binding left behind
    /// by an earlier service (killed before it could unbind) cannot trigger
    /// an action in this one.
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
    /// Hotkeys not added back after the last config reload because the
    /// configuration now binds the same keys.
    lost: std::collections::BTreeSet<String>,
}

#[derive(Clone)]
struct Hotkey {
    mods: Mods,
    key: String,
    combo: String,
    token: String,
    /// False when the configuration binds these keys now (seen after a
    /// reload); the binding is not ours to remove then.
    active: bool,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
struct Mods {
    ctrl: bool,
    alt: bool,
    shift: bool,
    super_: bool,
    /// Mod2, Mod3, Mod5 or Lock: never produced by Fractile's shortcuts.
    other: bool,
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

fn i(v: &Value) -> i64 {
    v.as_i64().or_else(|| v.as_f64().map(|f| f.round() as i64)).unwrap_or(0)
}

fn s(v: &Value) -> String {
    v.as_str().unwrap_or_default().to_string()
}

fn rect_of(v: &Value) -> Rect {
    Rect::new(i(&v["x"]) as i32, i(&v["y"]) as i32, i(&v["width"]) as i32, i(&v["height"]) as i32)
}

/// "sway:42" -> 42.
fn parse_id(id: &str) -> AResult<i64> {
    let n = id.strip_prefix("sway:").ok_or_else(|| AdapterError::NoSuchWindow(id.to_string()))?;
    if n.is_empty() || n.len() > 18 || !n.bytes().all(|b| b.is_ascii_digit()) {
        return Err(AdapterError::NoSuchWindow(id.to_string()));
    }
    n.parse().map_err(|_| AdapterError::NoSuchWindow(id.to_string()))
}

/// The key part of a Sway key combination for one of Fractile's
/// accelerators, e.g. "Mod4+Ctrl+Mod1+g". The key name comes from
/// Fractile's accelerator parser (an xkb keysym name); anything but
/// letters, digits and `_` is refused, so it cannot carry Sway syntax.
fn combo_for(acc: &Accelerator) -> AResult<(Mods, String)> {
    if acc.key.is_empty() || !acc.key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        return Err(AdapterError::Rejected(format!("key name {:?} cannot be bound in Sway", acc.key)));
    }
    let m = Mods { ctrl: acc.mods.ctrl, alt: acc.mods.alt, shift: acc.mods.shift, super_: acc.mods.super_, other: false };
    let mut parts: Vec<&str> = Vec::new();
    if m.super_ {
        parts.push("Mod4");
    }
    if m.ctrl {
        parts.push("Ctrl");
    }
    if m.alt {
        parts.push("Mod1");
    }
    if m.shift {
        parts.push("Shift");
    }
    parts.push(&acc.key);
    Ok((m, parts.join("+")))
}

/// Two key names for the same key (xkb accepts several spellings; Sway
/// matches keysym names without regard to case for letters).
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

/// Splits a Sway key combination ("$mod+Shift+Return", variables already
/// replaced) into modifiers and key. None for a combination with no key.
fn parse_combo(c: &str) -> Option<(Mods, String)> {
    let mut m = Mods::default();
    let mut key = None;
    for p in c.split('+').filter(|p| !p.is_empty()) {
        match p.to_ascii_lowercase().as_str() {
            "mod4" | "super" | "logo" => m.super_ = true,
            "mod1" | "alt" => m.alt = true,
            "ctrl" | "control" => m.ctrl = true,
            "shift" => m.shift = true,
            "mod2" | "mod3" | "mod5" | "lock" => m.other = true,
            _ => key = Some(p.to_string()),
        }
    }
    key.map(|k| (m, k))
}

/// A `bindsym` of the configuration in the default mode.
#[derive(Debug, Clone, PartialEq)]
struct ConfigBind {
    mods: Mods,
    key: String,
    command: String,
}

/// Default-mode `bindsym` lines of a Sway configuration text, with `set
/// $var value` variables replaced. Handles comments, `\` line continuation,
/// flags (`--release`, `--locked`, ...), `bindsym { ... }` blocks, and skips
/// `mode "..." { ... }` blocks. `include` lines are returned separately so
/// the caller can read those files too. `bindcode` lines are not matched
/// (key codes need the keymap) and are counted.
fn parse_config(text: &str, vars: &mut Vec<(String, String)>) -> (Vec<ConfigBind>, Vec<String>, usize) {
    let mut binds = vec![];
    let mut includes = vec![];
    let mut bindcodes = 0;
    // Join continued lines, drop comments.
    let mut lines: Vec<String> = vec![];
    let mut cur = String::new();
    for raw in text.lines() {
        let l = raw.trim_end();
        if let Some(stripped) = l.strip_suffix('\\') {
            cur.push_str(stripped);
            cur.push(' ');
            continue;
        }
        cur.push_str(l);
        let t = cur.trim().to_string();
        cur.clear();
        if t.is_empty() || t.starts_with('#') {
            continue;
        }
        lines.push(t);
    }
    let subst = |s: &str, vars: &Vec<(String, String)>| -> String {
        let mut out = s.to_string();
        // Longest names first, so $mod does not eat $modifier.
        let mut v: Vec<&(String, String)> = vars.iter().collect();
        v.sort_by_key(|(k, _)| std::cmp::Reverse(k.len()));
        for (k, val) in v {
            out = out.replace(k.as_str(), val);
        }
        out
    };
    let mut depth_mode = 0usize; // inside mode "x" { ... }
    let mut in_bind_block: Option<&'static str> = None; // inside bindsym { ... }
    for l in lines {
        let words: Vec<&str> = l.split_whitespace().collect();
        if depth_mode > 0 {
            if l.ends_with('{') {
                depth_mode += 1;
            }
            if l == "}" || l.ends_with('}') && !l.contains('{') {
                depth_mode -= 1;
            }
            continue;
        }
        if let Some(kind) = in_bind_block {
            if l == "}" {
                in_bind_block = None;
                continue;
            }
            let line = subst(&l, vars);
            let w: Vec<&str> = line.split_whitespace().filter(|w| !w.starts_with("--")).collect();
            if kind == "sym" {
                if let Some((m, k)) = w.first().and_then(|c| parse_combo(c)) {
                    binds.push(ConfigBind { mods: m, key: k, command: w[1..].join(" ") });
                }
            } else {
                bindcodes += 1;
            }
            continue;
        }
        match words.first().copied() {
            Some("set") if words.len() >= 3 && words[1].starts_with('$') => {
                let val = subst(&words[2..].join(" "), vars);
                vars.retain(|(k, _)| k != words[1]);
                vars.push((words[1].to_string(), val));
            }
            Some("mode") if l.ends_with('{') => depth_mode = 1,
            Some("include") if words.len() >= 2 => includes.push(subst(&words[1..].join(" "), vars)),
            Some("bindsym") | Some("bindcode") => {
                let kind = if words[0] == "bindsym" { "sym" } else { "code" };
                if l.ends_with('{') {
                    in_bind_block = Some(kind);
                    continue;
                }
                if kind == "code" {
                    bindcodes += 1;
                    continue;
                }
                let line = subst(&l, vars);
                let w: Vec<&str> = line.split_whitespace().skip(1).filter(|w| !w.starts_with("--")).collect();
                if let Some((m, k)) = w.first().and_then(|c| parse_combo(c)) {
                    binds.push(ConfigBind { mods: m, key: k, command: w[1..].join(" ") });
                }
            }
            _ => {}
        }
    }
    (binds, includes, bindcodes)
}

/// Paths an `include` argument names: `~` and `$HOME` expanded, relative
/// to the including file's directory, and a `*` or `?` in the last
/// component matched against that directory's entries.
fn include_paths(arg: &str, base: &Path) -> Vec<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut a = arg.trim().trim_matches('"').trim_matches('\'').to_string();
    if let Some(r) = a.strip_prefix("~/") {
        a = format!("{home}/{r}");
    }
    a = a.replace("$HOME", &home);
    if let Ok(x) = std::env::var("XDG_CONFIG_HOME") {
        a = a.replace("$XDG_CONFIG_HOME", &x);
    }
    let p = if Path::new(&a).is_absolute() { PathBuf::from(&a) } else { base.join(&a) };
    let Some(name) = p.file_name().map(|n| n.to_string_lossy().into_owned()) else { return vec![] };
    if !name.contains('*') && !name.contains('?') {
        return vec![p];
    }
    let dir = p.parent().map(Path::to_path_buf).unwrap_or_default();
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| wildcard(&name, &e.file_name().to_string_lossy()))
        .map(|e| e.path())
        .collect();
    out.sort();
    out
}

fn wildcard(pat: &str, s: &str) -> bool {
    fn m(p: &[u8], s: &[u8]) -> bool {
        match (p.first(), s.first()) {
            (None, None) => true,
            (Some(b'*'), _) => m(&p[1..], s) || (!s.is_empty() && m(p, &s[1..])),
            (Some(b'?'), Some(_)) => m(&p[1..], &s[1..]),
            (Some(a), Some(b)) if a == b => m(&p[1..], &s[1..]),
            _ => false,
        }
    }
    m(pat.as_bytes(), s.as_bytes())
}

/// Connects to a Sway socket and checks, through SO_PEERCRED, that the
/// process listening on it runs as this user. Checked on every connection.
fn connect_checked(path: &Path) -> std::io::Result<UnixStream> {
    let s = UnixStream::connect(path)?;
    let mut cred = libc::ucred { pid: 0, uid: u32::MAX, gid: 0 };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: getsockopt writes at most `len` bytes into `cred`, a ucred on
    // the stack, for a socket fd that stays open for the call.
    let rc = unsafe { libc::getsockopt(s.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED, &mut cred as *mut _ as *mut libc::c_void, &mut len) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error());
    }
    if cred.uid != uid() {
        return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, format!("the process on {} runs as uid {}, not this user", path.display(), cred.uid)));
    }
    Ok(s)
}

fn runtime_dir() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).filter(|p| p.is_absolute()).unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", uid())))
}

/// A real directory (not a symlink) owned by this user and writable by no
/// one else.
fn private_dir(p: &Path) -> bool {
    std::fs::symlink_metadata(p).map(|m| m.file_type().is_dir() && m.uid() == uid() && m.mode() & 0o022 == 0).unwrap_or(false)
}

/// A socket of this user, in a directory private to this user, that a
/// process of this user answers.
fn usable_socket(p: &Path) -> bool {
    let ok_file = std::fs::symlink_metadata(p).map(|m| m.file_type().is_socket() && m.uid() == uid()).unwrap_or(false);
    ok_file && p.parent().map(private_dir).unwrap_or(false) && connect_checked(p).is_ok()
}

/// Finds Sway's IPC socket. SWAYSOCK names it when the service inherited the
/// session environment; a systemd user service may not have it, and then
/// the newest live `sway-ipc.<uid>.*.sock` in the runtime directory is used.
fn find_socket() -> Result<PathBuf, String> {
    if let Some(p) = std::env::var_os("SWAYSOCK").map(PathBuf::from).filter(|p| !p.as_os_str().is_empty()) {
        if !p.is_absolute() {
            return Err(format!("SWAYSOCK is not an absolute path: {}", p.display()));
        }
        if usable_socket(&p) {
            return Ok(p);
        }
        return Err(format!("SWAYSOCK={}, but no Sway socket of this user answers there (it must be in a directory private to this user)", p.display()));
    }
    let rt = runtime_dir();
    if !private_dir(&rt) {
        return Err(format!("the runtime directory {} is not private to this user", rt.display()));
    }
    let prefix = format!("sway-ipc.{}.", uid());
    let mut found: Vec<(std::time::SystemTime, PathBuf)> = std::fs::read_dir(&rt)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| {
            let n = e.file_name().to_string_lossy().into_owned();
            n.starts_with(&prefix) && n.ends_with(".sock")
        })
        .map(|e| e.path())
        .filter(|p| usable_socket(p))
        .map(|p| (std::fs::symlink_metadata(&p).and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH), p))
        .collect();
    found.sort();
    found.pop().map(|(_, p)| p).ok_or_else(|| format!("no running Sway of this user (no answering sway-ipc.{}.*.sock in {})", uid(), rt.display()))
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

fn write_msg(st: &mut UnixStream, kind: u32, payload: &[u8]) -> std::io::Result<()> {
    let mut m = Vec::with_capacity(14 + payload.len());
    m.extend_from_slice(MAGIC);
    m.extend_from_slice(&(payload.len() as u32).to_ne_bytes());
    m.extend_from_slice(&kind.to_ne_bytes());
    m.extend_from_slice(payload);
    st.write_all(&m)
}

fn read_msg(st: &mut UnixStream) -> std::io::Result<(u32, Vec<u8>)> {
    let mut h = [0u8; 14];
    st.read_exact(&mut h)?;
    if &h[..6] != MAGIC {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "not an i3-ipc message"));
    }
    let len = u32::from_ne_bytes([h[6], h[7], h[8], h[9]]) as usize;
    let kind = u32::from_ne_bytes([h[10], h[11], h[12], h[13]]);
    if len > MAX_REPLY {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, format!("reply of {len} bytes is too large")));
    }
    let mut body = vec![0u8; len];
    st.read_exact(&mut body)?;
    Ok((kind, body))
}

/// One window of the tree with where it sits.
struct Leaf<'a> {
    node: &'a Value,
    output: String,
    workspace: Option<i64>,
    scratchpad: bool,
    floating: bool,
}

fn leaves(tree: &Value) -> Vec<Leaf<'_>> {
    fn walk<'a>(n: &'a Value, output: &str, ws: Option<i64>, scratch: bool, floating: bool, out: &mut Vec<Leaf<'a>>) {
        let t = s(&n["type"]);
        let (mut output, mut ws, mut scratch) = (output.to_string(), ws, scratch);
        match t.as_str() {
            "output" => output = s(&n["name"]),
            "workspace" => {
                let name = s(&n["name"]);
                scratch = name == "__i3_scratch";
                let num = i(&n["num"]);
                ws = if num >= 1 { Some(num) } else { None };
            }
            _ => {}
        }
        let kids = n["nodes"].as_array().map(|a| a.as_slice()).unwrap_or(&[]);
        let fkids = n["floating_nodes"].as_array().map(|a| a.as_slice()).unwrap_or(&[]);
        if (t == "con" || t == "floating_con") && kids.is_empty() && fkids.is_empty() && n["pid"].as_i64().is_some() {
            out.push(Leaf { node: n, output: output.clone(), workspace: ws, scratchpad: scratch, floating: floating || t == "floating_con" });
            return;
        }
        for k in kids {
            walk(k, &output, ws, scratch, floating, out);
        }
        for k in fkids {
            walk(k, &output, ws, scratch, true, out);
        }
    }
    let mut out = vec![];
    walk(tree, "", None, false, false, &mut out);
    out
}

fn focused_id(tree: &Value) -> Option<i64> {
    leaves(tree).into_iter().find(|l| l.node["focused"].as_bool().unwrap_or(false)).map(|l| i(&l.node["id"]))
}

fn transform_of(t: &str) -> Transform {
    match t {
        "90" => Transform::Rotate90,
        "180" => Transform::Rotate180,
        "270" => Transform::Rotate270,
        "flipped" => Transform::Flipped,
        "flipped-90" => Transform::Flipped90,
        "flipped-180" => Transform::Flipped180,
        "flipped-270" => Transform::Flipped270,
        _ => Transform::Normal,
    }
}

/// Outputs in Fractile's model. Sway reports positions and sizes in the
/// logical layout already (scale and transform applied). The work area is
/// the rectangle of the workspace the output shows (bars and other
/// exclusive layer surfaces taken out).
fn outputs_from(outs: &Value, wss: &Value) -> Vec<Output> {
    let mut v: Vec<Output> = outs
        .as_array()
        .into_iter()
        .flatten()
        .filter(|o| o["active"].as_bool().unwrap_or(true) && i(&o["rect"]["width"]) > 0)
        .map(|o| {
            let name = s(&o["name"]);
            let rect = rect_of(&o["rect"]);
            let cur_ws = s(&o["current_workspace"]);
            let work = wss
                .as_array()
                .into_iter()
                .flatten()
                .find(|w| s(&w["output"]) == name && (s(&w["name"]) == cur_ws || w["visible"].as_bool().unwrap_or(false)))
                .map(|w| rect_of(&w["rect"]))
                .filter(|r| r.w > 0 && r.h > 0)
                .unwrap_or(rect);
            let (make, model, serial) = (s(&o["make"]), s(&o["model"]), s(&o["serial"]));
            let known = |x: &str| !x.is_empty() && x != "Unknown";
            let from_edid = known(&make) && known(&model) && known(&serial);
            let stable_id = if from_edid {
                let h = Sha256::digest(format!("{make}|{model}|{serial}").as_bytes());
                format!("edid-{}", &hex::encode(h)[..10])
            } else {
                format!("conn-{name}-{}x{}", rect.w, rect.h)
            };
            let desc = [make.as_str(), model.as_str()].iter().filter(|x| known(x)).cloned().collect::<Vec<_>>().join(" ");
            Output {
                stable_id,
                stable_id_from_edid: from_edid,
                name,
                description: if desc.is_empty() { None } else { Some(desc) },
                rect,
                work_area: work,
                scale: o["scale"].as_f64().filter(|v| *v > 0.0).unwrap_or(1.0),
                transform: transform_of(&s(&o["transform"])),
                primary: false,
                physical_mm: None,
            }
        })
        .collect();
    // Sway has no primary output: the leftmost (then topmost) is used.
    if let Some(p) = v.iter_mut().min_by_key(|o| (o.rect.x, o.rect.y)) {
        p.primary = true;
    }
    v
}

impl Inner {
    fn request(&self, kind: u32, payload: &str) -> AResult<Value> {
        let mut st = connect_checked(&self.sock).map_err(|e| AdapterError::Disconnected(format!("Sway IPC socket: {e}")))?;
        st.set_read_timeout(Some(Duration::from_secs(3))).ok();
        st.set_write_timeout(Some(Duration::from_secs(3))).ok();
        write_msg(&mut st, kind, payload.as_bytes()).map_err(|e| AdapterError::Disconnected(format!("Sway IPC write: {e}")))?;
        let (_k, body) = read_msg(&mut st).map_err(|e| match e.kind() {
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => AdapterError::Timeout("Sway did not answer within 3 s".into()),
            _ => AdapterError::Disconnected(format!("Sway IPC read: {e}")),
        })?;
        serde_json::from_slice(&body).map_err(|e| AdapterError::Other(format!("Sway reply is not JSON ({e}): {}", trunc(&String::from_utf8_lossy(&body)))))
    }

    /// Runs one Sway command string; every part must succeed.
    fn command(&self, cmd: &str) -> AResult<()> {
        let r = self.request(RUN_COMMAND, cmd)?;
        let fails: Vec<String> = r.as_array().into_iter().flatten().filter(|x| !x["success"].as_bool().unwrap_or(false)).map(|x| s(&x["error"])).collect();
        if fails.is_empty() {
            Ok(())
        } else {
            Err(AdapterError::Rejected(format!("Sway: {}", trunc(&fails.join("; ")))))
        }
    }

    fn tree(&self) -> AResult<Value> {
        self.request(GET_TREE, "")
    }

    fn node(&self, id: i64) -> AResult<Value> {
        let t = self.tree()?;
        leaves(&t).into_iter().find(|l| i(&l.node["id"]) == id).map(|l| l.node.clone()).ok_or_else(|| AdapterError::NoSuchWindow(format!("sway:{id}")))
    }

    fn outputs(&self) -> AResult<Vec<Output>> {
        Ok(outputs_from(&self.request(GET_OUTPUTS, "")?, &self.request(GET_WORKSPACES, "")?))
    }

    /// Fractile's own transient windows float when they map. Sway has no
    /// way to remove a `for_window` rule at runtime; the rule matches only
    /// Fractile's own app id and titles, and a config reload drops it (it is
    /// added again then).
    fn install_own_rule(&self) -> AResult<()> {
        self.command(&format!("for_window [app_id=\"{OWN_APP_ID_RE}\" title=\"{OWN_FLOAT_TITLES}\"] floating enable"))
    }

    /// The configuration's default-mode key bindings: the main file Sway
    /// loaded (its text as Sway has it) and the files it includes, to a
    /// depth of 4. The count of `bindcode` lines is returned too.
    fn config_binds(&self) -> (Vec<ConfigBind>, usize) {
        let main = self.request(GET_CONFIG, "").ok().map(|v| s(&v["config"])).unwrap_or_default();
        let main_path = self.request(GET_VERSION, "").ok().map(|v| s(&v["loaded_config_file_name"])).unwrap_or_default();
        let mut vars = vec![];
        let mut seen: HashSet<PathBuf> = HashSet::new();
        let (mut binds, includes, mut codes) = parse_config(&main, &mut vars);
        let base = Path::new(&main_path).parent().map(Path::to_path_buf).unwrap_or_default();
        let mut todo: Vec<(PathBuf, usize)> = includes.iter().flat_map(|a| include_paths(a, &base)).map(|p| (p, 1)).collect();
        while let Some((p, depth)) = todo.pop() {
            if depth > 4 || !seen.insert(p.clone()) {
                continue;
            }
            let Ok(meta) = std::fs::metadata(&p) else { continue };
            if !meta.is_file() || meta.len() > 1 << 20 {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&p) else { continue };
            let (b, inc, c) = parse_config(&text, &mut vars);
            binds.extend(b);
            codes += c;
            let dir = p.parent().map(Path::to_path_buf).unwrap_or_default();
            todo.extend(inc.iter().flat_map(|a| include_paths(a, &dir)).map(|q| (q, depth + 1)));
        }
        (binds, codes)
    }

    fn config_conflict(&self, mods: Mods, key: &str) -> Option<String> {
        let (binds, _) = self.config_binds();
        binds.into_iter().find(|b| b.mods == mods && same_key(&b.key, key)).map(|b| b.command)
    }

    fn bind(&self, hk: &Hotkey) -> AResult<()> {
        self.command(&format!("bindsym --no-repeat {} nop {}", hk.combo, hk.token))
    }

    fn unbind(&self, hk: &Hotkey) -> AResult<()> {
        self.command(&format!("unbindsym --no-repeat {}", hk.combo))
    }

    /// After `swaymsg reload` the runtime bindings and the window rule are
    /// gone: add them again, unless the configuration now binds the keys.
    fn reapply(&self) {
        let hotkeys: Vec<(String, Hotkey)> = {
            let mut st = self.state.lock().unwrap();
            st.lost.clear();
            st.hotkeys.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
        };
        let _ = self.install_own_rule();
        let (binds, _) = self.config_binds();
        for (id, hk) in hotkeys {
            let taken = binds.iter().any(|b| b.mods == hk.mods && same_key(&b.key, &hk.key));
            let ok = !taken && self.bind(&hk).is_ok();
            let mut st = self.state.lock().unwrap();
            if let Some(h) = st.hotkeys.get_mut(&id) {
                h.active = ok;
            }
            if !ok {
                st.lost.insert(id);
            }
        }
    }

    fn handle_event(&self, kind: u32, body: &[u8], tx: &Sender<AdapterEvent>) -> bool {
        let Ok(v) = serde_json::from_slice::<Value>(body) else { return true };
        let change = s(&v["change"]);
        let ev = match kind {
            EV_WINDOW => {
                let id = format!("sway:{}", i(&v["container"]["id"]));
                match change.as_str() {
                    "new" => Some(AdapterEvent::WindowCreated { id }),
                    "close" => Some(AdapterEvent::WindowClosed { id }),
                    "focus" => Some(AdapterEvent::ActiveChanged { id: Some(id) }),
                    _ => Some(AdapterEvent::WindowChanged { id }),
                }
            }
            EV_OUTPUT => Some(AdapterEvent::OutputsChanged),
            EV_WORKSPACE => {
                if change == "reload" {
                    self.reapply();
                    Some(AdapterEvent::OutputsChanged)
                } else {
                    Some(AdapterEvent::WorkspacesChanged)
                }
            }
            EV_BINDING if change == "run" => {
                let cmd = s(&v["binding"]["command"]);
                let tok = cmd.trim().strip_prefix("nop ").map(str::trim).unwrap_or("");
                let st = self.state.lock().unwrap();
                st.hotkeys.iter().find(|(_, h)| h.active && h.token == tok).map(|(id, _)| AdapterEvent::Hotkey { binding_id: id.clone(), timestamp: 0 })
            }
            EV_SHUTDOWN => {
                if !self.stop.load(Ordering::SeqCst) {
                    let _ = tx.send(AdapterEvent::DisplayClosed { reason: format!("Sway is shutting down ({change})") });
                }
                return false;
            }
            _ => None,
        };
        match ev {
            Some(e) => tx.send(e).is_ok(),
            None => true,
        }
    }

    /// Polls the window until its frame has held `target` for 300 ms, or
    /// stops changing. Sway reports the size it asked the client for at
    /// once; a client that then commits an older size (it acked an earlier
    /// configure) makes a floating container take that size a moment later.
    /// So reaching the target once is not enough: the second value is true
    /// when the frame reached the target and then left it.
    fn settle(&self, id: i64, target: Rect, tol: i32) -> AResult<(Rect, bool)> {
        let start = Instant::now();
        let mut last: Option<Rect> = None;
        let mut same = 0;
        let mut reached: Option<Instant> = None;
        loop {
            let r = rect_of(&self.node(id)?["rect"]);
            if r.max_edge_delta(&target) <= tol {
                let t = *reached.get_or_insert_with(Instant::now);
                if t.elapsed() >= Duration::from_millis(300) {
                    return Ok((r, false));
                }
            } else if reached.is_some() {
                return Ok((r, true));
            }
            if last == Some(r) {
                same += 1;
            } else {
                same = 0;
            }
            last = Some(r);
            if reached.is_none() && ((same >= 3 && start.elapsed() > Duration::from_millis(200)) || start.elapsed() > Duration::from_millis(1500)) {
                return Ok((r, false));
            }
            if start.elapsed() > Duration::from_millis(2500) {
                return Ok((r, false));
            }
            std::thread::sleep(Duration::from_millis(40));
        }
    }
}

impl SwayAdapter {
    pub fn connect() -> Result<SwayAdapter, String> {
        let sock = find_socket()?;
        let mut inner = Inner { sock, version: String::new(), nonce: random_hex(), state: Mutex::new(State::default()), stop: AtomicBool::new(false), events: Mutex::new(None) };
        let v = inner.request(GET_VERSION, "").map_err(|e| format!("Sway did not answer a version query: {e}"))?;
        if s(&v["variant"]) != "sway" && v.get("variant").is_some() {
            return Err(format!("the IPC socket belongs to {}, not Sway", s(&v["variant"])));
        }
        inner.version = s(&v["human_readable"]);
        let (maj, min) = (i(&v["major"]), i(&v["minor"]));
        if (maj, min) < (1, 8) {
            return Err(format!("Sway {} is too old: Fractile's Sway adapter needs Sway 1.8 or later", inner.version));
        }
        inner.install_own_rule().map_err(|e| format!("Sway refused Fractile's window rule for its own windows: {e}"))?;
        Ok(SwayAdapter { inner: Arc::new(inner), subscribed: Mutex::new(false) })
    }

    pub fn version(&self) -> &str {
        &self.inner.version
    }

    pub fn socket(&self) -> &Path {
        &self.inner.sock
    }

    fn window_infos(&self) -> AResult<Vec<WindowInfo>> {
        let tree = self.inner.tree()?;
        let outs = self.inner.outputs()?;
        let own = self.inner.state.lock().unwrap().own.clone();
        let ls = leaves(&tree);
        // Sway has no global stacking list: floating windows are above
        // tiled ones, and the focused window is on top of its kind.
        let mut order: Vec<usize> = (0..ls.len()).collect();
        order.sort_by_key(|&k| (ls[k].floating, ls[k].node["focused"].as_bool().unwrap_or(false), k));
        let mut stacking = vec![0u32; ls.len()];
        for (pos, k) in order.into_iter().enumerate() {
            stacking[k] = pos as u32;
        }
        Ok(ls
            .iter()
            .enumerate()
            .map(|(k, l)| {
                let n = l.node;
                let id = format!("sway:{}", i(&n["id"]));
                let pid = Some(i(&n["pid"])).filter(|p| *p > 0).map(|p| p as u32);
                let app_id = n["app_id"].as_str().filter(|a| !a.is_empty()).map(String::from);
                let wp = &n["window_properties"];
                let class = wp["class"].as_str().filter(|a| !a.is_empty()).map(String::from);
                let instance = wp["instance"].as_str().filter(|a| !a.is_empty()).map(String::from);
                let fs = i(&n["fullscreen_mode"]);
                let frame = rect_of(&n["rect"]);
                let wr = rect_of(&n["window_rect"]);
                let is_own = own.contains(&id) || pid.map(|p| own.contains(&format!("pid:{p}"))).unwrap_or(false);
                WindowInfo {
                    id: id.clone(),
                    title: s(&n["name"]),
                    wm_class: class.clone().or_else(|| app_id.clone()),
                    wm_class_instance: instance,
                    app_id,
                    pid,
                    window_type: WindowType::Normal,
                    frame,
                    client: Some(Rect::new(frame.x + wr.x, frame.y + wr.y, wr.w, wr.h)),
                    state: WindowState {
                        fullscreen: fs >= 1,
                        // Sway has no minimize; a window in the scratchpad is
                        // out of sight in the same way.
                        minimized: l.scratchpad,
                        sticky: n["sticky"].as_bool().unwrap_or(false),
                        tiled: !l.floating && fs == 0,
                        ..Default::default()
                    },
                    workspace: if l.scratchpad { None } else { l.workspace.map(|w| (w - 1) as u32) },
                    output_id: outs.iter().find(|o| o.name == l.output).map(|o| o.stable_id.clone()),
                    stacking: Some(stacking[k]),
                    focused: n["focused"].as_bool().unwrap_or(false),
                    is_own,
                    ..Default::default()
                }
            })
            .collect())
    }
}

impl CompositorAdapter for SwayAdapter {
    fn capabilities(&self) -> Capabilities {
        use Operation::*;
        use SupportLevel::*;
        let mut ops = BTreeMap::new();
        ops.insert(ListWindows, Full);
        ops.insert(GetActiveWindow, Full);
        ops.insert(GetOutputs, Full);
        ops.insert(GetWorkspaces, Partial("numbered workspaces that exist now (index = number - 1); named workspaces are not listed".into()));
        ops.insert(GetGeometry, Full);
        ops.insert(MoveResize, Partial("a tiled window is made floating so it can take the exact rectangle; the result is verified".into()));
        ops.insert(SetState, Partial("fullscreen, sticky, and minimize through the scratchpad; Sway has no maximize or keep-above".into()));
        ops.insert(MoveToOutput, Partial("by placing the window inside the other output (it is made floating)".into()));
        ops.insert(MoveToWorkspace, Full);
        ops.insert(Activate, Full);
        ops.insert(SubscribeEvents, Full);
        ops.insert(RegisterHotkey, Partial("runtime bindsym through Sway's IPC, added again after a config reload; keys the Sway configuration binds (main file and includes) are reported as a conflict; bindcode lines are not checked".into()));
        ops.insert(ShowOverlay, Unsupported("Sway has no IPC to draw a preview; the panel shows the selection only".into()));
        ops.insert(GetPermissions, Full);
        let mut notes = vec![
            "Sway tiles new windows. Placing a window with Fractile makes that window floating; Sway's layout is not changed otherwise.".into(),
            "Fractile's panel and dialogs float through a runtime for_window rule that matches only Fractile's own app id.".into(),
        ];
        let (_, codes) = self.inner.config_binds();
        if codes > 0 {
            notes.push(format!("the Sway configuration has {codes} bindcode binding(s); Fractile cannot tell which keys they use, so a shortcut on the same keys would replace them"));
        }
        let lost = self.inner.state.lock().unwrap().lost.iter().cloned().collect::<Vec<_>>();
        if !lost.is_empty() {
            notes.push(format!("after the last Sway config reload these shortcuts are not bound, because the configuration now binds the same keys: {}", lost.join(", ")));
        }
        Capabilities {
            adapter: "sway".into(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: "wayland".into(),
            compositor: "Sway".into(),
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
        Ok(focused_id(&self.inner.tree()?).map(|n| format!("sway:{n}")))
    }

    fn get_outputs(&self) -> AResult<Vec<Output>> {
        self.inner.outputs()
    }

    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        let wss = self.inner.request(GET_WORKSPACES, "")?;
        let mut v: Vec<(i64, String, bool)> = wss
            .as_array()
            .into_iter()
            .flatten()
            .map(|w| (i(&w["num"]), s(&w["name"]), w["focused"].as_bool().unwrap_or(false)))
            .filter(|(n, _, _)| *n >= 1)
            .collect();
        v.sort();
        Ok(v.into_iter().map(|(n, name, f)| Workspace { index: (n - 1) as u32, name, active: f }).collect())
    }

    fn move_resize(&self, id: &str, target: Rect) -> AResult<PlacementOutcome> {
        let con = parse_id(id)?;
        if target.w <= 0 || target.h <= 0 {
            return Err(AdapterError::Rejected("empty target rectangle".into()));
        }
        let n = self.inner.node(con)?;
        if i(&n["fullscreen_mode"]) != 0 {
            self.inner.command(&format!("[con_id={con}] fullscreen disable"))?;
        }
        let was_tiled = s(&n["type"]) == "con";
        let tol = 2;
        let mut corr = [0i32; 4];
        let mut observed = None;
        let mut attempts = 0;
        for attempt in 1..=4 {
            attempts = attempt;
            let (x, y) = (target.x + corr[0], target.y + corr[1]);
            let (w, h) = ((target.w + corr[2]).max(1), (target.h + corr[3]).max(1));
            // One command list: float, size, then position (the container
            // rectangle, border and title bar included, is what Sway sizes
            // and moves here).
            self.inner.command(&format!("[con_id={con}] floating enable, resize set width {w} px height {h} px, move absolute position {x} px {y} px"))?;
            let (obs, drifted) = self.inner.settle(con, target, tol)?;
            observed = Some(obs);
            if obs.max_edge_delta(&target) <= tol {
                break;
            }
            if drifted {
                // The client committed an older size after Sway's: ask again
                // with the same numbers.
                continue;
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
            note: if was_tiled { Some("the window was tiled; Sway now floats it at the placed size".into()) } else { None },
        })
    }

    fn set_state(&self, id: &str, want: &WindowState) -> AResult<()> {
        let con = parse_id(id)?;
        let t = self.inner.tree()?;
        let l = leaves(&t).into_iter().find(|l| i(&l.node["id"]) == con).ok_or_else(|| AdapterError::NoSuchWindow(id.into()))?;
        let (fs, sticky, scratch, floating) = (i(&l.node["fullscreen_mode"]) != 0, l.node["sticky"].as_bool().unwrap_or(false), l.scratchpad, l.floating);
        if want.minimized != scratch {
            if want.minimized {
                self.inner.command(&format!("[con_id={con}] move scratchpad"))?;
                return Ok(());
            }
            // Out of the scratchpad onto the current workspace (as floating).
            self.inner.command(&format!("[con_id={con}] scratchpad show"))?;
        }
        if want.fullscreen != fs {
            self.inner.command(&format!("[con_id={con}] fullscreen {}", if want.fullscreen { "enable" } else { "disable" }))?;
        }
        if want.sticky != sticky {
            // Sway makes floating windows sticky only.
            if want.sticky && !floating {
                self.inner.command(&format!("[con_id={con}] floating enable"))?;
            }
            self.inner.command(&format!("[con_id={con}] sticky {}", if want.sticky { "enable" } else { "disable" }))?;
        }
        Ok(())
    }

    fn move_to_workspace(&self, id: &str, workspace: u32) -> AResult<()> {
        let con = parse_id(id)?;
        self.inner.node(con)?;
        self.inner.command(&format!("[con_id={con}] move container to workspace number {}", workspace as u64 + 1))
    }

    fn activate(&self, id: &str, _timestamp: Option<u32>) -> AResult<()> {
        let con = parse_id(id)?;
        self.inner.node(con)?;
        self.inner.command(&format!("[con_id={con}] focus"))
    }

    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        {
            let mut sub = self.subscribed.lock().unwrap();
            if *sub {
                return Err(AdapterError::Other("already subscribed".into()));
            }
            *sub = true;
        }
        let mut sock = connect_checked(&self.inner.sock).map_err(|e| AdapterError::Disconnected(format!("Sway IPC socket: {e}")))?;
        write_msg(&mut sock, SUBSCRIBE, br#"["window","workspace","output","binding","shutdown"]"#).map_err(|e| AdapterError::Disconnected(format!("Sway subscribe: {e}")))?;
        sock.set_read_timeout(Some(Duration::from_secs(3))).ok();
        let (_, body) = read_msg(&mut sock).map_err(|e| AdapterError::Disconnected(format!("Sway subscribe reply: {e}")))?;
        let ok: Value = serde_json::from_slice(&body).unwrap_or_default();
        if !ok["success"].as_bool().unwrap_or(false) {
            return Err(AdapterError::Rejected(format!("Sway refused the event subscription: {}", trunc(&String::from_utf8_lossy(&body)))));
        }
        sock.set_read_timeout(None).ok();
        *self.inner.events.lock().unwrap() = sock.try_clone().ok();
        let (tx, rx) = channel();
        let inner = self.inner.clone();
        std::thread::Builder::new()
            .name("sway-events".into())
            .spawn(move || {
                let reason = loop {
                    match read_msg(&mut sock) {
                        Ok((kind, body)) => {
                            if !inner.handle_event(kind, &body, &tx) {
                                return;
                            }
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => break "Sway closed its IPC socket (the compositor exited)".to_string(),
                        Err(e) => break format!("Sway IPC event stream: {e}"),
                    }
                };
                if !inner.stop.load(Ordering::SeqCst) {
                    let _ = tx.send(AdapterEvent::DisplayClosed { reason });
                }
            })
            .map_err(|e| AdapterError::Other(e.to_string()))?;
        Ok(rx)
    }

    fn register_hotkey(&self, binding_id: &str, accelerator: &str) -> AResult<()> {
        let acc = Accelerator::parse(accelerator).map_err(AdapterError::Rejected)?;
        let (mods, combo) = combo_for(&acc)?;
        let _ = self.unregister_hotkey(binding_id);
        {
            let st = self.inner.state.lock().unwrap();
            if let Some((other, _)) = st.hotkeys.iter().find(|(_, h)| h.mods == mods && same_key(&h.key, &acc.key)) {
                return Err(AdapterError::Conflict(format!("already bound to '{other}'")));
            }
        }
        if let Some(cmd) = self.inner.config_conflict(mods, &acc.key) {
            return Err(AdapterError::Conflict(format!("the Sway configuration already binds {} ({})", acc.to_display(), trunc(&cmd))));
        }
        let token = {
            let mut st = self.inner.state.lock().unwrap();
            st.next_token += 1;
            format!("fractile:{}:{}", self.inner.nonce, st.next_token)
        };
        let hk = Hotkey { mods, key: acc.key.clone(), combo, token, active: true };
        self.inner.bind(&hk)?;
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

impl Drop for SwayAdapter {
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
    fn ids_are_numbers_only() {
        assert_eq!(parse_id("sway:42").unwrap(), 42);
        for bad in ["sway:", "sway:4 2", "sway:1]; exec x", "hypr:0x1", "sway:-1", "sway:12345678901234567890"] {
            assert!(parse_id(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn combos_refuse_sway_syntax_in_key_names() {
        let a = Accelerator::parse("Super+Ctrl+Alt+G").unwrap();
        let (m, c) = combo_for(&a).unwrap();
        assert_eq!(c, "Mod4+Ctrl+Mod1+g");
        assert!(m.super_ && m.ctrl && m.alt && !m.shift);
        let mut evil = a.clone();
        evil.key = "g nop x; exec rm".into();
        assert!(combo_for(&evil).is_err());
        evil.key = "g,exec".into();
        assert!(combo_for(&evil).is_err());
    }

    #[test]
    fn config_parsing_finds_default_mode_bindsyms() {
        let cfg = r#"
# comment
set $mod Mod4
set $left h
set $term foot
bindsym $mod+Return exec $term
bindsym --release $mod+Shift+$left move left
bindsym --to-code Ctrl+Mod1+Delete \
    exec swaynag
bindcode 133+24 kill
bindsym {
    $mod+q kill
    Mod1+Tab focus next
}
mode "resize" {
    bindsym Ctrl+Mod1+g resize grow width 10px
    bindsym {
        Escape mode default
    }
}
include ~/.config/sway/config.d/*
include extra.conf
"#;
        let mut vars = vec![];
        let (b, inc, codes) = parse_config(cfg, &mut vars);
        let has = |m: Mods, k: &str| b.iter().any(|x| x.mods == m && same_key(&x.key, k));
        let sup = Mods { super_: true, ..Default::default() };
        assert!(has(sup, "Return"));
        assert_eq!(b.iter().find(|x| x.key == "Return").unwrap().command, "exec foot");
        assert!(has(Mods { super_: true, shift: true, ..Default::default() }, "h"));
        assert!(has(Mods { ctrl: true, alt: true, ..Default::default() }, "Delete"), "continued line: {b:?}");
        assert!(has(sup, "q"));
        assert!(has(Mods { alt: true, ..Default::default() }, "Tab"));
        assert!(!has(Mods { ctrl: true, alt: true, ..Default::default() }, "g"), "a binding in another mode is not a conflict");
        assert!(!b.iter().any(|x| x.key == "Escape"));
        assert_eq!(codes, 1);
        assert_eq!(inc, vec!["~/.config/sway/config.d/*".to_string(), "extra.conf".to_string()]);
        assert!(same_key("Q", "q") && same_key("Page_Up", "Prior"));
    }

    #[test]
    fn includes_expand_globs_relative_to_the_file() {
        let d = std::env::temp_dir().join(format!("fractile-sway-inc-{}", std::process::id()));
        std::fs::create_dir_all(d.join("config.d")).unwrap();
        for f in ["10-keys", "20-more", "README"] {
            std::fs::write(d.join("config.d").join(f), "").unwrap();
        }
        let got = include_paths("config.d/*-*", &d);
        assert_eq!(got, vec![d.join("config.d/10-keys"), d.join("config.d/20-more")]);
        assert_eq!(include_paths("extra.conf", &d), vec![d.join("extra.conf")]);
        assert_eq!(include_paths("/etc/sway/config.d/x", &d), vec![PathBuf::from("/etc/sway/config.d/x")]);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn tree_leaves_outputs_workspaces_scratchpad() {
        let tree = json!({"id": 1, "type": "root", "nodes": [
            {"id": 2, "type": "output", "name": "__i3", "nodes": [
                {"id": 3, "type": "workspace", "name": "__i3_scratch", "num": -1, "nodes": [], "floating_nodes": [
                    {"id": 30, "type": "floating_con", "pid": 9, "app_id": "org.s", "name": "S", "nodes": [], "floating_nodes": [], "rect": {"x":0,"y":0,"width":10,"height":10}}]}]},
            {"id": 4, "type": "output", "name": "DP-1", "nodes": [
                {"id": 5, "type": "workspace", "name": "2", "num": 2, "nodes": [
                    {"id": 6, "type": "con", "nodes": [
                        {"id": 7, "type": "con", "pid": 11, "app_id": "org.a", "name": "A", "nodes": [], "floating_nodes": [], "focused": true}]}],
                 "floating_nodes": [{"id": 8, "type": "floating_con", "pid": 12, "app_id": null, "window_properties": {"class": "XTerm"}, "name": "x", "nodes": [], "floating_nodes": []}]}]}]});
        let ls = leaves(&tree);
        let ids: Vec<i64> = ls.iter().map(|l| i(&l.node["id"])).collect();
        assert_eq!(ids, vec![30, 7, 8]);
        assert!(ls[0].scratchpad && ls[0].floating);
        assert_eq!((ls[1].output.as_str(), ls[1].workspace, ls[1].floating), ("DP-1", Some(2), false));
        assert!(ls[2].floating);
        assert_eq!(focused_id(&tree), Some(7));
    }

    #[test]
    fn outputs_use_workspace_rect_as_work_area() {
        let outs = json!([
            {"name": "DP-1", "make": "Dell", "model": "U2720Q", "serial": "ABC", "active": true, "rect": {"x": 1920, "y": 0, "width": 1920, "height": 1080}, "scale": 2.0, "transform": "normal", "current_workspace": "2"},
            {"name": "HEADLESS-1", "make": "Unknown", "model": "Unknown", "serial": "Unknown", "active": true, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "scale": 1.0, "transform": "90", "current_workspace": "1"},
            {"name": "OFF-1", "active": false, "rect": {"x": 0, "y": 0, "width": 0, "height": 0}}]);
        let wss = json!([
            {"name": "1", "output": "HEADLESS-1", "visible": true, "rect": {"x": 0, "y": 30, "width": 1920, "height": 1050}},
            {"name": "2", "output": "DP-1", "visible": true, "rect": {"x": 1920, "y": 0, "width": 1920, "height": 1080}}]);
        let o = outputs_from(&outs, &wss);
        assert_eq!(o.len(), 2);
        assert!(o[0].stable_id.starts_with("edid-") && o[0].stable_id_from_edid);
        assert_eq!(o[1].stable_id, "conn-HEADLESS-1-1920x1080");
        assert_eq!(o[1].work_area, Rect::new(0, 30, 1920, 1050));
        assert!(o[1].primary && !o[0].primary);
        assert_eq!(o[1].transform, Transform::Rotate90);
    }

    #[test]
    fn framing_round_trip_and_limits() {
        let (mut a, mut b) = UnixStream::pair().unwrap();
        write_msg(&mut a, GET_TREE, b"{}").unwrap();
        let (k, body) = read_msg(&mut b).unwrap();
        assert_eq!((k, body.as_slice()), (GET_TREE, &b"{}"[..]));
        let mut bad = Vec::new();
        bad.extend_from_slice(MAGIC);
        bad.extend_from_slice(&(u32::MAX).to_ne_bytes());
        bad.extend_from_slice(&0u32.to_ne_bytes());
        a.write_all(&bad).unwrap();
        assert!(read_msg(&mut b).is_err(), "an oversized length is refused");
    }
}

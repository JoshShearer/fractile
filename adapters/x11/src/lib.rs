//! X11 adapter: EWMH window management, RandR outputs (EDID identities),
//! mutter `_GTK_WORKAREAS_D<n>` per-monitor work areas, passive key grabs
//! for global shortcuts, and verified move/resize.
//!
//! Works with any EWMH-compliant window manager (validated on GNOME Shell /
//! mutter X11). It cannot control native Wayland windows; under a Wayland
//! session it would only see XWayland clients, so the daemon never selects
//! it there.

mod keysyms;

use fractile_core::accel::Accelerator;
use fractile_core::adapter::*;
use fractile_core::geometry::{Rect, SizeConstraints};
use fractile_core::output::{output_for_rect, Output, Transform};
use fractile_core::window::{WindowInfo, WindowState, WindowType};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use x11rb::connection::Connection;
use x11rb::protocol::randr::{self, ConnectionExt as _};
use x11rb::protocol::xproto::*;
use x11rb::protocol::Event;
use x11rb::rust_connection::RustConnection;

x11rb::atom_manager! {
    pub Atoms: AtomsCookie {
        _NET_SUPPORTED,
        _NET_SUPPORTING_WM_CHECK,
        _NET_CLIENT_LIST,
        _NET_CLIENT_LIST_STACKING,
        _NET_ACTIVE_WINDOW,
        _NET_WM_NAME,
        _NET_WM_PID,
        _NET_WM_DESKTOP,
        _NET_WM_STATE,
        _NET_WM_STATE_MAXIMIZED_VERT,
        _NET_WM_STATE_MAXIMIZED_HORZ,
        _NET_WM_STATE_FULLSCREEN,
        _NET_WM_STATE_HIDDEN,
        _NET_WM_STATE_ABOVE,
        _NET_WM_STATE_STICKY,
        _NET_WM_STATE_MODAL,
        _NET_WM_STATE_SKIP_TASKBAR,
        _NET_WM_WINDOW_TYPE,
        _NET_WM_WINDOW_TYPE_NORMAL,
        _NET_WM_WINDOW_TYPE_DIALOG,
        _NET_WM_WINDOW_TYPE_UTILITY,
        _NET_WM_WINDOW_TYPE_TOOLBAR,
        _NET_WM_WINDOW_TYPE_MENU,
        _NET_WM_WINDOW_TYPE_SPLASH,
        _NET_WM_WINDOW_TYPE_DOCK,
        _NET_WM_WINDOW_TYPE_DESKTOP,
        _NET_WM_WINDOW_TYPE_NOTIFICATION,
        _NET_WM_WINDOW_TYPE_TOOLTIP,
        _NET_WM_WINDOW_TYPE_POPUP_MENU,
        _NET_WM_WINDOW_TYPE_DROPDOWN_MENU,
        _NET_FRAME_EXTENTS,
        _GTK_FRAME_EXTENTS,
        _GTK_APPLICATION_ID,
        _NET_MOVERESIZE_WINDOW,
        _NET_NUMBER_OF_DESKTOPS,
        _NET_CURRENT_DESKTOP,
        _NET_DESKTOP_NAMES,
        _NET_WORKAREA,
        WM_CHANGE_STATE,
        WM_WINDOW_ROLE,
        WM_CLIENT_LEADER,
        UTF8_STRING,
        EDID,
    }
}

#[derive(Clone)]
struct Binding {
    keycode: u8,
    mods: u16,
}

struct Shared {
    own: HashSet<String>,
    bindings: HashMap<String, Binding>,
    known_clients: HashSet<u32>,
}

pub struct X11Adapter {
    conn: Arc<RustConnection>,
    root: u32,
    atoms: Atoms,
    shared: Arc<Mutex<Shared>>,
    subscribed: Mutex<bool>,
    wm_name: String,
    numlock_mask: u16,
    keymap: Mutex<(u8, u8, Vec<u32>)>,
    randr_ok: bool,
}

fn ae<E: std::fmt::Display>(e: E) -> AdapterError {
    AdapterError::Other(format!("X11: {e}"))
}

pub fn parse_id(id: &str) -> AResult<u32> {
    let hex = id.strip_prefix("x11:0x").ok_or_else(|| AdapterError::NoSuchWindow(id.into()))?;
    u32::from_str_radix(hex, 16).map_err(|_| AdapterError::NoSuchWindow(id.into()))
}

pub fn fmt_id(w: u32) -> String {
    format!("x11:0x{w:x}")
}

impl X11Adapter {
    /// Connect to `$DISPLAY` (or the given display name).
    pub fn connect(display: Option<&str>) -> AResult<X11Adapter> {
        let (conn, screen) = x11rb::connect(display).map_err(|e| AdapterError::Disconnected(format!("cannot open X display: {e}")))?;
        let root = conn.setup().roots[screen].root;
        let atoms = Atoms::new(&conn).map_err(ae)?.reply().map_err(ae)?;
        let randr_ok = conn.randr_query_version(1, 5).ok().and_then(|c| c.reply().ok()).map(|v| v.major_version >= 1 && v.minor_version >= 5).unwrap_or(false);
        let mut a = X11Adapter {
            conn: Arc::new(conn),
            root,
            atoms,
            shared: Arc::new(Mutex::new(Shared { own: HashSet::new(), bindings: HashMap::new(), known_clients: HashSet::new() })),
            subscribed: Mutex::new(false),
            wm_name: String::new(),
            numlock_mask: 0,
            keymap: Mutex::new((0, 0, vec![])),
            randr_ok,
        };
        a.wm_name = a.detect_wm_name().unwrap_or_else(|| "unknown".into());
        a.numlock_mask = a.find_numlock_mask();
        a.refresh_keymap();
        Ok(a)
    }

    pub fn wm_name(&self) -> &str {
        &self.wm_name
    }

    fn prop(&self, win: u32, atom: u32, ty: impl Into<u32>, len: u32) -> Option<GetPropertyReply> {
        self.conn.get_property(false, win, atom, ty.into(), 0, len).ok()?.reply().ok().filter(|r| r.type_ != 0)
    }
    fn prop32(&self, win: u32, atom: u32, ty: impl Into<u32>) -> Vec<u32> {
        self.prop(win, atom, ty, 4096).and_then(|r| r.value32().map(|v| v.collect())).unwrap_or_default()
    }
    fn prop_string(&self, win: u32, atom: u32) -> Option<String> {
        self.prop(win, atom, AtomEnum::ANY, 4096).map(|r| String::from_utf8_lossy(&r.value).trim_end_matches('\0').to_string())
    }

    fn detect_wm_name(&self) -> Option<String> {
        let check = *self.prop32(self.root, self.atoms._NET_SUPPORTING_WM_CHECK, AtomEnum::WINDOW).first()?;
        self.prop_string(check, self.atoms._NET_WM_NAME)
    }

    fn find_numlock_mask(&self) -> u16 {
        let setup = self.conn.setup();
        let (min, max) = (setup.min_keycode, setup.max_keycode);
        let Ok(km) = self.conn.get_keyboard_mapping(min, max - min + 1).map_err(ae).and_then(|c| c.reply().map_err(ae)) else { return 0 };
        let per = km.keysyms_per_keycode as usize;
        let numlock_codes: Vec<u8> = (0..=(max - min) as usize)
            .filter(|i| km.keysyms[i * per..(i + 1) * per].contains(&keysyms::XK_NUM_LOCK))
            .map(|i| min + i as u8)
            .collect();
        let Ok(mm) = self.conn.get_modifier_mapping().map_err(ae).and_then(|c| c.reply().map_err(ae)) else { return 0 };
        let per_mod = mm.keycodes_per_modifier() as usize;
        for m in 0..8 {
            for k in &mm.keycodes[m * per_mod..(m + 1) * per_mod] {
                if *k != 0 && numlock_codes.contains(k) {
                    return 1 << m;
                }
            }
        }
        u16::from(ModMask::M2)
    }

    fn refresh_keymap(&self) {
        let setup = self.conn.setup();
        let (min, max) = (setup.min_keycode, setup.max_keycode);
        if let Ok(km) = self.conn.get_keyboard_mapping(min, max - min + 1).map_err(ae).and_then(|c| c.reply().map_err(ae)) {
            *self.keymap.lock().unwrap() = (min, km.keysyms_per_keycode, km.keysyms);
        }
    }

    fn keycode_for(&self, keysym: u32) -> Option<u8> {
        let km = self.keymap.lock().unwrap();
        let (min, per, syms) = (&km.0, km.1 as usize, &km.2);
        if per == 0 {
            return None;
        }
        // prefer unshifted column
        for col in 0..per {
            for (i, chunk) in syms.chunks(per).enumerate() {
                if chunk.get(col) == Some(&keysym) {
                    return Some(*min + i as u8);
                }
            }
        }
        None
    }

    fn client_list(&self) -> Vec<u32> {
        self.prop32(self.root, self.atoms._NET_CLIENT_LIST_STACKING, AtomEnum::WINDOW)
    }

    fn window_type(&self, win: u32) -> WindowType {
        let a = &self.atoms;
        let types = self.prop32(win, a._NET_WM_WINDOW_TYPE, AtomEnum::ATOM);
        for t in types {
            let wt = match t {
                x if x == a._NET_WM_WINDOW_TYPE_NORMAL => WindowType::Normal,
                x if x == a._NET_WM_WINDOW_TYPE_DIALOG => WindowType::Dialog,
                x if x == a._NET_WM_WINDOW_TYPE_UTILITY => WindowType::Utility,
                x if x == a._NET_WM_WINDOW_TYPE_TOOLBAR => WindowType::Toolbar,
                x if x == a._NET_WM_WINDOW_TYPE_MENU || x == a._NET_WM_WINDOW_TYPE_POPUP_MENU || x == a._NET_WM_WINDOW_TYPE_DROPDOWN_MENU => WindowType::Menu,
                x if x == a._NET_WM_WINDOW_TYPE_SPLASH => WindowType::Splash,
                x if x == a._NET_WM_WINDOW_TYPE_DOCK => WindowType::Dock,
                x if x == a._NET_WM_WINDOW_TYPE_DESKTOP => WindowType::Desktop,
                x if x == a._NET_WM_WINDOW_TYPE_NOTIFICATION => WindowType::Notification,
                x if x == a._NET_WM_WINDOW_TYPE_TOOLTIP => WindowType::Tooltip,
                _ => continue, // unknown/vendor type: try next in preference list
            };
            return wt;
        }
        WindowType::Normal
    }

    fn state(&self, win: u32) -> WindowState {
        let a = &self.atoms;
        let st = self.prop32(win, a._NET_WM_STATE, AtomEnum::ATOM);
        let has = |x: u32| st.contains(&x);
        WindowState {
            maximized: has(a._NET_WM_STATE_MAXIMIZED_HORZ) && has(a._NET_WM_STATE_MAXIMIZED_VERT),
            fullscreen: has(a._NET_WM_STATE_FULLSCREEN),
            minimized: has(a._NET_WM_STATE_HIDDEN),
            above: has(a._NET_WM_STATE_ABOVE),
            sticky: has(a._NET_WM_STATE_STICKY),
            modal: has(a._NET_WM_STATE_MODAL),
            skip_taskbar: has(a._NET_WM_STATE_SKIP_TASKBAR),
            tiled: false,
        }
    }

    fn partially_maximized(&self, win: u32) -> bool {
        let a = &self.atoms;
        let st = self.prop32(win, a._NET_WM_STATE, AtomEnum::ATOM);
        st.contains(&a._NET_WM_STATE_MAXIMIZED_HORZ) || st.contains(&a._NET_WM_STATE_MAXIMIZED_VERT) || st.contains(&a._NET_WM_STATE_FULLSCREEN)
    }

    /// (client rect in root coords, NET frame extents l,r,t,b, GTK extents l,r,t,b)
    fn geometry_parts(&self, win: u32) -> AResult<(Rect, [i32; 4], [i32; 4])> {
        let g = self.conn.get_geometry(win).map_err(ae)?.reply().map_err(|_| AdapterError::NoSuchWindow(fmt_id(win)))?;
        let t = self.conn.translate_coordinates(win, self.root, 0, 0).map_err(ae)?.reply().map_err(|_| AdapterError::NoSuchWindow(fmt_id(win)))?;
        let client = Rect::new(t.dst_x as i32, t.dst_y as i32, g.width as i32, g.height as i32);
        let ext = |atom| {
            let v = self.prop32(win, atom, AtomEnum::CARDINAL);
            if v.len() == 4 {
                [v[0] as i32, v[1] as i32, v[2] as i32, v[3] as i32]
            } else {
                [0; 4]
            }
        };
        Ok((client, ext(self.atoms._NET_FRAME_EXTENTS), ext(self.atoms._GTK_FRAME_EXTENTS)))
    }

    /// Visible frame: client + server-side decorations − client-side shadows.
    fn visible_frame(&self, win: u32) -> AResult<(Rect, Rect)> {
        let (c, n, g) = self.geometry_parts(win)?;
        let f = Rect::new(c.x - n[0] + g[0], c.y - n[2] + g[2], c.w + n[0] + n[1] - g[0] - g[1], c.h + n[2] + n[3] - g[2] - g[3]);
        Ok((f, c))
    }

    fn size_hints(&self, win: u32) -> SizeConstraints {
        let v = self.prop32(win, u32::from(AtomEnum::WM_NORMAL_HINTS), AtomEnum::WM_SIZE_HINTS);
        let mut c = SizeConstraints::default();
        if v.len() >= 11 {
            let flags = v[0];
            if flags & 16 != 0 {
                c.min_w = Some(v[5] as i32);
                c.min_h = Some(v[6] as i32);
            }
            if flags & 32 != 0 {
                c.max_w = Some(v[7] as i32).filter(|x| *x > 0 && *x < 32767);
                c.max_h = Some(v[8] as i32).filter(|x| *x > 0 && *x < 32767);
            }
            if flags & 64 != 0 {
                c.inc_w = Some(v[9] as i32).filter(|x| *x > 1);
                c.inc_h = Some(v[10] as i32).filter(|x| *x > 1);
            }
        }
        c
    }

    fn read_window(&self, win: u32, stacking: u32, active: Option<u32>, outputs: &[Output], own: &HashSet<String>) -> AResult<WindowInfo> {
        let a = &self.atoms;
        let (frame, client) = self.visible_frame(win)?;
        let title = self
            .prop(win, a._NET_WM_NAME, a.UTF8_STRING, 4096)
            .map(|r| String::from_utf8_lossy(&r.value).to_string())
            .or_else(|| self.prop_string(win, u32::from(AtomEnum::WM_NAME)))
            .unwrap_or_default();
        let (instance, class) = match self.prop(win, u32::from(AtomEnum::WM_CLASS), AtomEnum::STRING, 1024) {
            Some(r) => {
                let parts: Vec<String> = r.value.split(|b| *b == 0).map(|p| String::from_utf8_lossy(p).to_string()).collect();
                (parts.first().cloned().filter(|s| !s.is_empty()), parts.get(1).cloned().filter(|s| !s.is_empty()))
            }
            None => (None, None),
        };
        let pid = self.prop32(win, a._NET_WM_PID, AtomEnum::CARDINAL).first().copied();
        let desktop = self.prop32(win, a._NET_WM_DESKTOP, AtomEnum::CARDINAL).first().copied();
        let transient = self.prop32(win, u32::from(AtomEnum::WM_TRANSIENT_FOR), AtomEnum::WINDOW).first().copied().filter(|w| *w != 0 && *w != self.root);
        let leader = self.prop32(win, a.WM_CLIENT_LEADER, AtomEnum::WINDOW).first().copied();
        let mut state = self.state(win);
        if desktop == Some(0xFFFF_FFFF) {
            state.sticky = true;
        }
        let id = fmt_id(win);
        let is_own = own.contains(&id) || pid.map(|p| own.contains(&format!("pid:{p}"))).unwrap_or(false);
        Ok(WindowInfo {
            id,
            title,
            app_id: self.prop_string(win, a._GTK_APPLICATION_ID).filter(|s| !s.is_empty()),
            wm_class_instance: instance,
            wm_class: class,
            role: self.prop_string(win, a.WM_WINDOW_ROLE).filter(|s| !s.is_empty()),
            desktop_file_id: None,
            desktop_file_source: None,
            pid,
            executable: None,
            sandbox: None,
            window_type: self.window_type(win),
            transient_for: transient.map(fmt_id),
            group: leader.map(fmt_id),
            frame,
            client: Some(client),
            constraints: self.size_hints(win),
            state,
            workspace: desktop.filter(|d| *d != 0xFFFF_FFFF),
            output_id: output_for_rect(outputs, &frame).map(|o| o.stable_id.clone()),
            stacking: Some(stacking),
            focused: active == Some(win),
            is_own,
        })
    }

    fn active(&self) -> Option<u32> {
        self.prop32(self.root, self.atoms._NET_ACTIVE_WINDOW, AtomEnum::WINDOW).first().copied().filter(|w| *w != 0)
    }

    fn send_client_message(&self, win: u32, ty: u32, data: [u32; 5]) -> AResult<()> {
        let ev = ClientMessageEvent::new(32, win, ty, data);
        self.conn
            .send_event(false, self.root, EventMask::SUBSTRUCTURE_REDIRECT | EventMask::SUBSTRUCTURE_NOTIFY, ev)
            .map_err(ae)?;
        self.conn.flush().map_err(ae)?;
        Ok(())
    }

    fn set_wm_state(&self, win: u32, add: bool, p1: u32, p2: u32) -> AResult<()> {
        self.send_client_message(win, self.atoms._NET_WM_STATE, [if add { 1 } else { 0 }, p1, p2, 2, 0])
    }

    fn wait_until<F: Fn() -> bool>(&self, timeout: Duration, f: F) -> bool {
        let start = Instant::now();
        while start.elapsed() < timeout {
            if f() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(15));
        }
        f()
    }

    /// Poll until geometry stops changing (two equal reads) or timeout.
    fn settle(&self, win: u32, timeout: Duration) -> AResult<Rect> {
        let start = Instant::now();
        let mut last = self.visible_frame(win)?.0;
        let mut stable = 0;
        while start.elapsed() < timeout {
            std::thread::sleep(Duration::from_millis(25));
            let cur = self.visible_frame(win)?.0;
            if cur == last {
                stable += 1;
                if stable >= 2 {
                    return Ok(cur);
                }
            } else {
                stable = 0;
                last = cur;
            }
        }
        Ok(last)
    }

    /// Wait for the window manager to act on a move/resize request.
    /// "No change yet" is not "settled": a busy WM (or a client slow to
    /// acknowledge the configure) can take hundreds of milliseconds, and
    /// treating the old geometry as the result makes the correction loop
    /// overshoot. Returns the observed frame and whether it changed from
    /// `before` (or already matches `target`).
    fn settle_after(&self, win: u32, before: Rect, target: Rect, tol: i32, timeout: Duration) -> AResult<(Rect, bool)> {
        let start = Instant::now();
        let mut last = self.visible_frame(win)?.0;
        let mut responded = last != before || last.max_edge_delta(&target) <= tol;
        let mut stable = 0;
        while start.elapsed() < timeout {
            std::thread::sleep(Duration::from_millis(20));
            let cur = self.visible_frame(win)?.0;
            if cur != before || cur.max_edge_delta(&target) <= tol {
                responded = true;
            }
            if cur == last {
                stable += 1;
                // Once the WM has responded, a short quiet period means done
                // (some WMs and CSD clients configure in two steps).
                if responded && stable >= 4 {
                    return Ok((cur, true));
                }
            } else {
                stable = 0;
                last = cur;
            }
        }
        Ok((last, responded))
    }

    fn output_list(&self) -> AResult<Vec<Output>> {
        let a = &self.atoms;
        let current_desktop = self.prop32(self.root, a._NET_CURRENT_DESKTOP, AtomEnum::CARDINAL).first().copied().unwrap_or(0);
        let mut outs = vec![];
        if self.randr_ok {
            let mons = self.conn.randr_get_monitors(self.root, true).map_err(ae)?.reply().map_err(ae)?;
            for m in mons.monitors {
                let name = self.conn.get_atom_name(m.name).ok().and_then(|c| c.reply().ok()).map(|r| String::from_utf8_lossy(&r.name).to_string()).unwrap_or_else(|| "unknown".into());
                let mut edid_hash = None;
                let mut transform = Transform::Normal;
                if let Some(&out) = m.outputs.first() {
                    if let Ok(Ok(p)) = self.conn.randr_get_output_property(out, a.EDID, AtomEnum::ANY, 0, 256, false, false).map(|c| c.reply()) {
                        if p.data.len() >= 128 {
                            edid_hash = Some(hex::encode(&Sha256::digest(&p.data[..128])[..10]));
                        }
                    }
                    if let Ok(Ok(info)) = self.conn.randr_get_output_info(out, 0).map(|c| c.reply()) {
                        if info.crtc != 0 {
                            if let Ok(Ok(ci)) = self.conn.randr_get_crtc_info(info.crtc, 0).map(|c| c.reply()) {
                                let r = u16::from(ci.rotation);
                                transform = match r & 0xf {
                                    2 => Transform::Rotate90,
                                    4 => Transform::Rotate180,
                                    8 => Transform::Rotate270,
                                    _ => Transform::Normal,
                                };
                            }
                        }
                    }
                }
                let rect = Rect::new(m.x as i32, m.y as i32, m.width as i32, m.height as i32);
                let (stable_id, from_edid) = match edid_hash {
                    Some(h) => (format!("edid-{h}"), true),
                    None => (format!("conn-{name}-{}x{}", m.width, m.height), false),
                };
                outs.push(Output {
                    stable_id,
                    stable_id_from_edid: from_edid,
                    name,
                    description: None,
                    rect,
                    work_area: rect,
                    scale: 1.0,
                    transform,
                    primary: m.primary,
                    physical_mm: Some((m.width_in_millimeters, m.height_in_millimeters)),
                });
            }
        }
        if outs.is_empty() {
            let s = &self.conn.setup().roots[0];
            let rect = Rect::new(0, 0, s.width_in_pixels as i32, s.height_in_pixels as i32);
            outs.push(Output { stable_id: "screen-0".into(), stable_id_from_edid: false, name: "screen".into(), description: None, rect, work_area: rect, scale: 1.0, transform: Transform::Normal, primary: true, physical_mm: None });
        }
        // Per-monitor work areas: mutter publishes _GTK_WORKAREAS_D<n>.
        let gtk_atom = self.conn.intern_atom(false, format!("_GTK_WORKAREAS_D{current_desktop}").as_bytes()).ok().and_then(|c| c.reply().ok()).map(|r| r.atom);
        let gtk_areas: Vec<Rect> = gtk_atom
            .map(|at| self.prop32(self.root, at, AtomEnum::CARDINAL))
            .unwrap_or_default()
            .chunks(4)
            .filter(|c| c.len() == 4)
            .map(|c| Rect::new(c[0] as i32, c[1] as i32, c[2] as i32, c[3] as i32))
            .collect();
        let net_wa = self.prop32(self.root, a._NET_WORKAREA, AtomEnum::CARDINAL);
        let idx = (current_desktop as usize) * 4;
        let global = if net_wa.len() >= idx + 4 { Some(Rect::new(net_wa[idx] as i32, net_wa[idx + 1] as i32, net_wa[idx + 2] as i32, net_wa[idx + 3] as i32)) } else { None };
        for o in &mut outs {
            let per = gtk_areas.iter().filter_map(|r| r.intersection(&o.rect)).max_by_key(|r| r.area());
            o.work_area = per.or_else(|| global.and_then(|g| g.intersection(&o.rect))).unwrap_or(o.rect);
        }
        Ok(outs)
    }

    fn select_client_events(&self, win: u32) {
        let _ = self.conn.change_window_attributes(win, &ChangeWindowAttributesAux::new().event_mask(EventMask::PROPERTY_CHANGE | EventMask::STRUCTURE_NOTIFY));
    }

    fn grab_variants(&self, keycode: u8, mods: u16, grab: bool) -> AResult<()> {
        let lock = u16::from(ModMask::LOCK);
        let num = self.numlock_mask;
        let variants = [0, lock, num, lock | num];
        for v in variants {
            let m = ModMask::from(mods | v);
            if grab {
                let r = self.conn.grab_key(true, self.root, m, keycode, GrabMode::ASYNC, GrabMode::ASYNC).map_err(ae)?.check();
                if let Err(e) = r {
                    // undo partial grabs
                    for u in variants {
                        let _ = self.conn.ungrab_key(keycode, self.root, ModMask::from(mods | u));
                    }
                    let _ = self.conn.flush();
                    // Shown to the user in the shortcut editor: say what it means.
                    let why = match &e {
                        x11rb::errors::ReplyError::X11Error(x) if x.error_kind == x11rb::protocol::ErrorKind::Access => "another application already uses this key combination".to_string(),
                        x11rb::errors::ReplyError::X11Error(x) => format!("the X server refused the key grab ({:?})", x.error_kind),
                        other => format!("the X server refused the key grab ({other})"),
                    };
                    return Err(AdapterError::Conflict(why));
                }
            } else {
                let _ = self.conn.ungrab_key(keycode, self.root, m);
            }
        }
        self.conn.flush().map_err(ae)?;
        Ok(())
    }
}

fn accel_mods(a: &Accelerator) -> u16 {
    let mut m = 0u16;
    if a.mods.shift {
        m |= u16::from(ModMask::SHIFT);
    }
    if a.mods.ctrl {
        m |= u16::from(ModMask::CONTROL);
    }
    if a.mods.alt {
        m |= u16::from(ModMask::M1);
    }
    if a.mods.super_ {
        m |= u16::from(ModMask::M4);
    }
    m
}

impl CompositorAdapter for X11Adapter {
    fn capabilities(&self) -> Capabilities {
        use Operation::*;
        use SupportLevel::*;
        // Everything except outputs, key grabs and our own overlay needs an
        // EWMH window manager. Checked live: the WM may start after us.
        let has_wm = !self.prop32(self.root, self.atoms._NET_SUPPORTING_WM_CHECK, AtomEnum::WINDOW).is_empty();
        if !has_wm {
            let why = "no EWMH window manager is running on this X display (_NET_SUPPORTING_WM_CHECK is missing)".to_string();
            let mut ops = BTreeMap::new();
            for op in Operation::ALL {
                ops.insert(op, Unsupported(why.clone()));
            }
            ops.insert(GetOutputs, if self.randr_ok { Full } else { Partial("RandR 1.5 unavailable: single screen only".into()) });
            ops.insert(RegisterHotkey, Partial("passive X key grab; nothing to place without a window manager".into()));
            ops.insert(ShowOverlay, Full);
            ops.insert(GetPermissions, Full);
            return Capabilities {
                adapter: "x11".into(),
                adapter_version: env!("CARGO_PKG_VERSION").into(),
                api_version: ADAPTER_API_VERSION,
                session_type: "x11".into(),
                compositor: "none".into(),
                compositor_version: None,
                operations: ops,
                tiling_manager: None,
                notes: vec![why],
            };
        }
        let mut ops = BTreeMap::new();
        ops.insert(ListWindows, Full);
        ops.insert(GetActiveWindow, Full);
        ops.insert(GetOutputs, if self.randr_ok { Full } else { Partial("RandR 1.5 unavailable: single screen only".into()) });
        ops.insert(GetWorkspaces, Full);
        ops.insert(GetGeometry, Full);
        ops.insert(MoveResize, Partial("EWMH request; window manager and size hints may adjust; result is verified".into()));
        ops.insert(SetState, Full);
        ops.insert(MoveToOutput, Full);
        ops.insert(MoveToWorkspace, Full);
        ops.insert(Activate, Partial("subject to window-manager focus-stealing prevention".into()));
        ops.insert(SubscribeEvents, Full);
        ops.insert(RegisterHotkey, Partial("passive X key grab; conflicts detected via BadAccess; grabs inactive while another client holds an active keyboard grab".into()));
        ops.insert(ShowOverlay, Full);
        ops.insert(GetPermissions, Full);
        Capabilities {
            adapter: "x11".into(),
            adapter_version: env!("CARGO_PKG_VERSION").into(),
            api_version: ADAPTER_API_VERSION,
            session_type: "x11".into(),
            compositor: self.wm_name.clone(),
            compositor_version: None,
            operations: ops,
            tiling_manager: None,
            notes: vec!["X11 has no client isolation: any X client of this user can also move windows; Fractile adds no extra exposure.".into()],
        }
    }

    fn list_windows(&self) -> AResult<Vec<WindowInfo>> {
        let outputs = self.output_list()?;
        let active = self.active();
        let own = self.shared.lock().unwrap().own.clone();
        let mut out = vec![];
        for (i, w) in self.client_list().into_iter().enumerate() {
            if let Ok(info) = self.read_window(w, i as u32, active, &outputs, &own) {
                out.push(info);
            }
        }
        Ok(out)
    }

    fn get_window(&self, id: &str) -> AResult<WindowInfo> {
        let w = parse_id(id)?;
        let list = self.client_list();
        let pos = list.iter().position(|x| *x == w).ok_or_else(|| AdapterError::NoSuchWindow(id.into()))?;
        let outputs = self.output_list()?;
        let own = self.shared.lock().unwrap().own.clone();
        self.read_window(w, pos as u32, self.active(), &outputs, &own)
    }

    fn get_active_window(&self) -> AResult<Option<String>> {
        Ok(self.active().map(fmt_id))
    }

    fn get_outputs(&self) -> AResult<Vec<Output>> {
        self.output_list()
    }

    fn get_workspaces(&self) -> AResult<Vec<Workspace>> {
        let a = &self.atoms;
        let n = self.prop32(self.root, a._NET_NUMBER_OF_DESKTOPS, AtomEnum::CARDINAL).first().copied().unwrap_or(1);
        let cur = self.prop32(self.root, a._NET_CURRENT_DESKTOP, AtomEnum::CARDINAL).first().copied().unwrap_or(0);
        let names: Vec<String> = self
            .prop(self.root, a._NET_DESKTOP_NAMES, a.UTF8_STRING, 4096)
            .map(|r| r.value.split(|b| *b == 0).map(|p| String::from_utf8_lossy(p).to_string()).collect())
            .unwrap_or_default();
        Ok((0..n.min(64))
            .map(|i| Workspace { index: i, name: names.get(i as usize).filter(|s| !s.is_empty()).cloned().unwrap_or_else(|| format!("Workspace {}", i + 1)), active: i == cur })
            .collect())
    }

    fn move_resize(&self, id: &str, target: Rect) -> AResult<PlacementOutcome> {
        let win = parse_id(id)?;
        if target.w <= 0 || target.h <= 0 {
            return Err(AdapterError::Rejected("empty target rectangle".into()));
        }
        let a = &self.atoms;
        // Leave maximized/fullscreen first, and wait for the state change so the
        // restored "normal" rectangle is not overwritten mid-transition.
        if self.partially_maximized(win) {
            self.set_wm_state(win, false, a._NET_WM_STATE_FULLSCREEN, 0)?;
            self.set_wm_state(win, false, a._NET_WM_STATE_MAXIMIZED_HORZ, a._NET_WM_STATE_MAXIMIZED_VERT)?;
            self.wait_until(Duration::from_millis(500), || !self.partially_maximized(win));
            let _ = self.settle(win, Duration::from_millis(300));
        }
        let hints = self.size_hints(win);
        let tol = 2.max(hints.inc_w.unwrap_or(0)).max(hints.inc_h.unwrap_or(0));
        let mut corr = [0i32; 4]; // dx, dy, dw, dh
        let mut observed = None;
        let mut attempts = 0;
        for attempt in 1..=3 {
            attempts = attempt;
            let (_c, n, g) = self.geometry_parts(win)?;
            // Outer reference point (NorthWest gravity) and client size.
            let rx = target.x - g[0] + corr[0];
            let ry = target.y - g[2] + corr[1];
            let rw = (target.w - n[0] - n[1] + g[0] + g[1] + corr[2]).max(1);
            let rh = (target.h - n[2] - n[3] + g[2] + g[3] + corr[3]).max(1);
            let flags: u32 = 1 /*NorthWest*/ | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | (2 << 12) /*pager*/;
            let before = self.visible_frame(win)?.0;
            self.send_client_message(win, a._NET_MOVERESIZE_WINDOW, [flags, rx as u32, ry as u32, rw as u32, rh as u32])?;
            let (obs, responded) = self.settle_after(win, before, target, tol, Duration::from_millis(1500))?;
            observed = Some(obs);
            let err = obs.max_edge_delta(&target);
            if err <= tol {
                break;
            }
            if !responded {
                // Nothing happened yet: resend the same request rather than
                // "correcting" against a stale geometry.
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
            note: if tol > 2 { Some(format!("window uses resize increments; tolerance {tol}px")) } else { None },
        })
    }

    fn set_state(&self, id: &str, s: &WindowState) -> AResult<()> {
        let win = parse_id(id)?;
        let a = &self.atoms;
        let cur = self.state(win);
        if cur.maximized != s.maximized {
            self.set_wm_state(win, s.maximized, a._NET_WM_STATE_MAXIMIZED_HORZ, a._NET_WM_STATE_MAXIMIZED_VERT)?;
        }
        if cur.fullscreen != s.fullscreen {
            self.set_wm_state(win, s.fullscreen, a._NET_WM_STATE_FULLSCREEN, 0)?;
        }
        if cur.above != s.above {
            self.set_wm_state(win, s.above, a._NET_WM_STATE_ABOVE, 0)?;
        }
        if cur.sticky != s.sticky {
            self.set_wm_state(win, s.sticky, a._NET_WM_STATE_STICKY, 0)?;
        }
        if s.minimized && !cur.minimized {
            self.send_client_message(win, a.WM_CHANGE_STATE, [3, 0, 0, 0, 0])?;
        } else if !s.minimized && cur.minimized {
            self.activate(id, None)?;
        }
        Ok(())
    }

    fn move_to_workspace(&self, id: &str, ws: u32) -> AResult<()> {
        let win = parse_id(id)?;
        self.send_client_message(win, self.atoms._NET_WM_DESKTOP, [ws, 2, 0, 0, 0])
    }

    fn activate(&self, id: &str, ts: Option<u32>) -> AResult<()> {
        let win = parse_id(id)?;
        let cur = self.active().unwrap_or(0);
        self.send_client_message(win, self.atoms._NET_ACTIVE_WINDOW, [2, ts.unwrap_or(0), cur, 0, 0])
    }

    fn subscribe_events(&self) -> AResult<Receiver<AdapterEvent>> {
        {
            let mut s = self.subscribed.lock().unwrap();
            if *s {
                return Err(AdapterError::Other("already subscribed".into()));
            }
            *s = true;
        }
        let (tx, rx): (Sender<AdapterEvent>, Receiver<AdapterEvent>) = channel();
        self.conn
            .change_window_attributes(self.root, &ChangeWindowAttributesAux::new().event_mask(EventMask::PROPERTY_CHANGE | EventMask::STRUCTURE_NOTIFY))
            .map_err(ae)?;
        if self.randr_ok {
            let _ = self.conn.randr_select_input(self.root, randr::NotifyMask::SCREEN_CHANGE | randr::NotifyMask::OUTPUT_CHANGE | randr::NotifyMask::CRTC_CHANGE);
        }
        let initial = self.client_list();
        for w in &initial {
            self.select_client_events(*w);
        }
        self.shared.lock().unwrap().known_clients = initial.into_iter().collect();
        self.conn.flush().map_err(ae)?;
        let conn = self.conn.clone();
        let shared = self.shared.clone();
        let atoms = self.atoms;
        let root = self.root;
        let relevant_mods = u16::from(ModMask::SHIFT | ModMask::CONTROL | ModMask::M1 | ModMask::M4);
        std::thread::Builder::new()
            .name("x11-events".into())
            .spawn(move || loop {
                let ev = match conn.wait_for_event() {
                    Ok(e) => e,
                    Err(e) => {
                        let _ = tx.send(AdapterEvent::Disconnected { reason: e.to_string() });
                        return;
                    }
                };
                let out: Vec<AdapterEvent> = match ev {
                    Event::KeyPress(k) => {
                        let sh = shared.lock().unwrap();
                        let st = u16::from(k.state) & relevant_mods;
                        sh.bindings
                            .iter()
                            .filter(|(_, b)| b.keycode == k.detail && b.mods == st)
                            .map(|(id, _)| AdapterEvent::Hotkey { binding_id: id.clone(), timestamp: k.time })
                            .collect()
                    }
                    Event::PropertyNotify(p) if p.window == root => {
                        if p.atom == atoms._NET_CLIENT_LIST || p.atom == atoms._NET_CLIENT_LIST_STACKING {
                            let list: Vec<u32> = conn
                                .get_property(false, root, atoms._NET_CLIENT_LIST, AtomEnum::WINDOW, 0, 4096)
                                .ok()
                                .and_then(|c| c.reply().ok())
                                .and_then(|r| r.value32().map(|v| v.collect()))
                                .unwrap_or_default();
                            let now: HashSet<u32> = list.into_iter().collect();
                            let mut sh = shared.lock().unwrap();
                            let mut evs = vec![];
                            for w in now.difference(&sh.known_clients) {
                                let _ = conn.change_window_attributes(*w, &ChangeWindowAttributesAux::new().event_mask(EventMask::PROPERTY_CHANGE | EventMask::STRUCTURE_NOTIFY));
                                evs.push(AdapterEvent::WindowCreated { id: fmt_id(*w) });
                            }
                            for w in sh.known_clients.difference(&now) {
                                evs.push(AdapterEvent::WindowClosed { id: fmt_id(*w) });
                            }
                            let _ = conn.flush();
                            sh.known_clients = now;
                            evs
                        } else if p.atom == atoms._NET_ACTIVE_WINDOW {
                            let a = conn
                                .get_property(false, root, atoms._NET_ACTIVE_WINDOW, AtomEnum::WINDOW, 0, 1)
                                .ok()
                                .and_then(|c| c.reply().ok())
                                .and_then(|r| r.value32().and_then(|mut v| v.next()))
                                .filter(|w| *w != 0);
                            vec![AdapterEvent::ActiveChanged { id: a.map(fmt_id) }]
                        } else if p.atom == atoms._NET_CURRENT_DESKTOP || p.atom == atoms._NET_NUMBER_OF_DESKTOPS || p.atom == atoms._NET_DESKTOP_NAMES {
                            vec![AdapterEvent::WorkspacesChanged]
                        } else if p.atom == atoms._NET_WORKAREA {
                            vec![AdapterEvent::OutputsChanged]
                        } else {
                            vec![]
                        }
                    }
                    Event::PropertyNotify(p) => {
                        if p.atom == atoms._NET_WM_STATE || p.atom == atoms._NET_WM_NAME || p.atom == atoms._NET_WM_DESKTOP || p.atom == u32::from(AtomEnum::WM_NAME) {
                            vec![AdapterEvent::WindowChanged { id: fmt_id(p.window) }]
                        } else {
                            vec![]
                        }
                    }
                    Event::ConfigureNotify(c) if c.window != root => vec![AdapterEvent::WindowChanged { id: fmt_id(c.window) }],
                    Event::RandrScreenChangeNotify(_) | Event::RandrNotify(_) => vec![AdapterEvent::OutputsChanged],
                    Event::ConfigureNotify(_) => vec![AdapterEvent::OutputsChanged],
                    Event::MappingNotify(_) => vec![],
                    _ => vec![],
                };
                for e in out {
                    if tx.send(e).is_err() {
                        return;
                    }
                }
            })
            .map_err(ae)?;
        Ok(rx)
    }

    fn register_hotkey(&self, binding_id: &str, accelerator: &str) -> AResult<()> {
        let acc = Accelerator::parse(accelerator).map_err(AdapterError::Rejected)?;
        let ks = keysyms::keysym_for(&acc.key).ok_or_else(|| AdapterError::Rejected(format!("no keysym for {}", acc.key)))?;
        let kc = self.keycode_for(ks).ok_or_else(|| AdapterError::Rejected(format!("key '{}' not on current keyboard layout", acc.key)))?;
        let mods = accel_mods(&acc);
        let _ = self.unregister_hotkey(binding_id);
        {
            let sh = self.shared.lock().unwrap();
            if let Some((other, _)) = sh.bindings.iter().find(|(_, b)| b.keycode == kc && b.mods == mods) {
                return Err(AdapterError::Conflict(format!("already bound to '{other}'")));
            }
        }
        self.grab_variants(kc, mods, true)?;
        self.shared.lock().unwrap().bindings.insert(binding_id.to_string(), Binding { keycode: kc, mods });
        Ok(())
    }

    fn unregister_hotkey(&self, binding_id: &str) -> AResult<()> {
        let b = self.shared.lock().unwrap().bindings.remove(binding_id);
        if let Some(b) = b {
            self.grab_variants(b.keycode, b.mods, false)?;
        }
        Ok(())
    }

    fn set_own_windows(&self, ids: &[String]) {
        self.shared.lock().unwrap().own = ids.iter().cloned().collect();
    }
}

impl Drop for X11Adapter {
    fn drop(&mut self) {
        let ids: Vec<String> = self.shared.lock().unwrap().bindings.keys().cloned().collect();
        for id in ids {
            let _ = self.unregister_hotkey(&id);
        }
        let _ = self.conn.flush();
    }
}

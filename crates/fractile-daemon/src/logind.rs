//! logind integration: a delay inhibitor so a snapshot is written before
//! shutdown/reboot, and LockedHint tracking for the lock-screen capture rule.
//! Everything is best effort: without logind Fractile still works, it just
//! relies on SIGTERM at logout and periodic autosave.

use crate::daemon::Daemon;
use crate::session::capture_at_exit;
use fractile_core::snapshot::SnapshotMode;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use zbus::blocking::{Connection, Proxy};
use zbus::zvariant::{OwnedFd, OwnedObjectPath};

const DEST: &str = "org.freedesktop.login1";
const MGR_PATH: &str = "/org/freedesktop/login1";
const MGR: &str = "org.freedesktop.login1.Manager";

fn inhibit(mgr: &Proxy) -> zbus::Result<OwnedFd> {
    mgr.call("Inhibit", &("shutdown", "Fractile", "Saving the window layout for session restore", "delay"))
}

/// The system bus, or for tests only the bus named by
/// FRACTILE_TEST_LOGIND_BUS (tests/tools/fake_logind.py serves logind
/// there). Same-user processes can set the daemon's environment anyway, so
/// this grants nothing new.
fn connect() -> zbus::Result<Connection> {
    match std::env::var("FRACTILE_TEST_LOGIND_BUS") {
        Ok(addr) if !addr.is_empty() => zbus::blocking::connection::Builder::address(addr.as_str())?.build(),
        _ => Connection::system(),
    }
}

/// The logind session this service belongs to, and how it was found.
pub struct LoginSession {
    pub id: String,
    pub path: Option<OwnedObjectPath>,
    pub how: &'static str,
}

/// Finds the graphical session the service works for. Started by the
/// systemd user unit, the service is not inside any session and the user
/// manager's environment usually has no XDG_SESSION_ID, so the last step
/// asks logind for the user's graphical session (User.Display), the one
/// GNOME and Plasma run in. Without that, every login of a boot looked like
/// the same session and no login restore ran after a log-out.
/// With `use_bus` false (FRACTILE_NO_LOGIND) only XDG_SESSION_ID counts.
pub fn find_session(use_bus: bool) -> Option<LoginSession> {
    let env_id = std::env::var("XDG_SESSION_ID").ok().filter(|s| !s.is_empty());
    let conn = if use_bus { connect().ok() } else { None };
    let mgr = conn.as_ref().and_then(|c| Proxy::new(c, DEST, MGR_PATH, MGR).ok());
    let id_of = |path: &OwnedObjectPath| -> Option<String> {
        let c = conn.as_ref()?;
        Proxy::new(c, DEST, path.as_str().to_owned(), "org.freedesktop.login1.Session").ok()?.get_property::<String>("Id").ok()
    };
    if let Some(id) = env_id {
        let path = mgr.as_ref().and_then(|m| m.call::<_, _, OwnedObjectPath>("GetSession", &(id.as_str(),)).ok());
        return Some(LoginSession { id, path, how: "XDG_SESSION_ID" });
    }
    let mgr = mgr?;
    if let Ok(path) = mgr.call::<_, _, OwnedObjectPath>("GetSessionByPID", &(std::process::id(),)) {
        if let Some(id) = id_of(&path) {
            return Some(LoginSession { id, path: Some(path), how: "the session this process runs in" });
        }
    }
    // SAFETY: getuid has no preconditions and cannot fail.
    let uid = unsafe { libc::getuid() };
    let user: OwnedObjectPath = mgr.call("GetUser", &(uid,)).ok()?;
    let c = conn.as_ref()?;
    let (id, path) = Proxy::new(c, DEST, user.as_str().to_owned(), "org.freedesktop.login1.User").ok()?.get_property::<(String, OwnedObjectPath)>("Display").ok()?;
    if id.is_empty() {
        return None;
    }
    Some(LoginSession { id, path: Some(path), how: "the user's graphical session (logind User.Display)" })
}

pub fn start(d: Arc<Daemon>, session: Option<OwnedObjectPath>) {
    let conn = match connect() {
        Ok(c) => c,
        Err(e) => {
            warn!(d.log, "logind unavailable (no system bus): {e}");
            return;
        }
    };
    // Shutdown watcher.
    {
        let d = d.clone();
        let conn = conn.clone();
        std::thread::Builder::new().name("logind-shutdown".into()).spawn(move || {
            let mgr = match Proxy::new(&conn, DEST, MGR_PATH, MGR) {
                Ok(p) => p,
                Err(e) => return warn!(d.log, "logind manager proxy: {e}"),
            };
            let mut fd = inhibit(&mgr).map_err(|e| warn!(d.log, "logind inhibitor refused: {e}")).ok();
            let signals = match mgr.receive_signal("PrepareForShutdown") {
                Ok(s) => s,
                Err(e) => return warn!(d.log, "cannot watch PrepareForShutdown: {e}"),
            };
            for msg in signals {
                let Ok(starting) = msg.body().deserialize::<bool>() else { continue };
                if starting {
                    info!(d.log, "PrepareForShutdown: capturing session");
                    capture_at_exit(&d, SnapshotMode::Shutdown);
                    fd = None; // release the delay lock
                } else if fd.is_none() {
                    fd = inhibit(&mgr).ok(); // shutdown was cancelled
                }
                let _ = &fd;
            }
        }).ok();
    }
    // Lock watcher.
    std::thread::Builder::new().name("logind-lock".into()).spawn(move || {
        let Some(path) = session else { return warn!(d.log, "no logind session found for this service; lock state unknown") };
        let Ok(sess) = Proxy::new(&conn, DEST, path.as_str().to_owned(), "org.freedesktop.login1.Session") else { return };
        if let Ok(v) = sess.get_property::<bool>("LockedHint") {
            d.locked.store(v, Ordering::SeqCst);
        }
        for ch in sess.receive_property_changed::<bool>("LockedHint") {
            if let Ok(v) = ch.get() {
                d.locked.store(v, Ordering::SeqCst);
                info!(d.log, "session locked={v}");
                d.broadcast("lock_changed", serde_json::json!({"locked": v}));
            }
            if d.shutdown.load(Ordering::SeqCst) {
                break;
            }
        }
    }).ok();
}

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

pub fn start(d: Arc<Daemon>) {
    let conn = match Connection::system() {
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
        let Ok(mgr) = Proxy::new(&conn, DEST, MGR_PATH, MGR) else { return };
        let path: zbus::Result<OwnedObjectPath> = match std::env::var("XDG_SESSION_ID") {
            Ok(id) => mgr.call("GetSession", &(id,)),
            Err(_) => mgr.call("GetSessionByPID", &(std::process::id(),)),
        };
        let Ok(path) = path else { return warn!(d.log, "no logind session for this process; lock state unknown") };
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

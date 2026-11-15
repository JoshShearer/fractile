//! GNOME session-manager client: the logout snapshot is taken when
//! gnome-session announces the end of the session, while GNOME Shell (and so
//! the Fractile extension) is still running.
//!
//! At logout gnome-session stops GNOME Shell before systemd stops
//! fractile.service, so a capture at SIGTERM finds no extension and saves
//! nothing. gnome-session tells its registered clients first (QueryEndSession,
//! EndSession) and waits for each one's EndSessionResponse before it stops
//! the shell. Fractile answers QueryEndSession at once (it never holds a
//! logout back), captures on EndSession and then answers.

use crate::daemon::Daemon;
use crate::session;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use zbus::blocking::{Connection, Proxy};
use zbus::zvariant::OwnedObjectPath;

const DEST: &str = "org.gnome.SessionManager";
const PATH: &str = "/org/gnome/SessionManager";
const CLIENT: &str = "org.gnome.SessionManager.ClientPrivate";
pub const APP_ID: &str = "io.fractile.Fractile";

/// Registers with gnome-session if it runs on this session bus; does nothing
/// on other desktops.
pub fn start(d: Arc<Daemon>) {
    std::thread::Builder::new()
        .name("gnome-session".into())
        .spawn(move || {
            if let Err(e) = run(&d) {
                info!(d.log, "not registered with gnome-session ({e}); the logout snapshot is taken at SIGTERM");
            }
        })
        .ok();
}

fn run(d: &Arc<Daemon>) -> zbus::Result<()> {
    let conn = Connection::session()?;
    let mgr = Proxy::new(&conn, DEST, PATH, DEST)?;
    // A client started by gnome-session's autostart passes its startup id;
    // under the systemd unit there is none, and gnome-session accepts "".
    let startup = std::env::var("DESKTOP_AUTOSTART_ID").unwrap_or_default();
    let client: OwnedObjectPath = mgr.call("RegisterClient", &(APP_ID, startup.as_str()))?;
    info!(d.log, "registered with gnome-session as {}", client.as_str());
    let proxy = Proxy::new(&conn, DEST, client.as_str().to_owned(), CLIENT)?;
    let respond = |what: &str| {
        if let Err(e) = proxy.call_method("EndSessionResponse", &(true, "")) {
            warn!(d.log, "gnome-session {what} response failed: {e}");
        }
    };
    for msg in proxy.receive_all_signals()? {
        let member = msg.header().member().map(|m| m.to_string()).unwrap_or_default();
        match member.as_str() {
            "QueryEndSession" => respond("QueryEndSession"),
            "EndSession" => {
                info!(d.log, "gnome-session: the session is ending; taking the logout snapshot now");
                {
                    let _one_at_a_time = d.exit_capture.lock().unwrap_or_else(|e| e.into_inner());
                    session::capture_before_exit(d, fractile_core::snapshot::SnapshotMode::Logout);
                }
                respond("EndSession");
            }
            "CancelEndSession" => {
                d.exit_captured.store(false, Ordering::SeqCst);
                info!(d.log, "gnome-session: logout cancelled");
            }
            "Stop" => break,
            _ => {}
        }
        if d.shutdown.load(Ordering::SeqCst) {
            break;
        }
    }
    Ok(())
}

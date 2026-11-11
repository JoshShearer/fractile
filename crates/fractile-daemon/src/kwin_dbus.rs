//! D-Bus endpoint for the KWin script.
//!
//! KWin scripts cannot open sockets; their only IPC is outgoing `callDBus`.
//! So, on KWin sessions only, the daemon owns `io.fractile.Bridge` and the
//! script long-polls it for calls (`Poll`), answers with `Result` and pushes
//! changes with `Event`.
//!
//! A bus name is not access control: any session process can call these
//! methods. Every call is therefore authenticated by the caller's pid as
//! reported by the bus daemon, and that process must be running a root-owned
//! KWin binary (bridge::verify_compositor_peer). `Poll`, `Result` and `Event`
//! are additionally accepted only from the connection that attached.
//!
//! Liveness: a script's `callDBus` goes out on KWin's own bus connection,
//! so unloading the script does not drop a connection. The long-poll is the
//! heartbeat instead: when no `Poll` is in flight and none arrived for
//! `HEARTBEAT_GRACE`, the integration is detached. A KWin exit is still
//! noticed at once through NameOwnerChanged.

use crate::bridge::{self, BridgeAdapter, Transport};
use crate::daemon::Daemon;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use zbus::fdo;
use zbus::message::Header;
use zbus::names::BusName;

pub const BUS_NAME: &str = "io.fractile.Bridge";
pub const PATH: &str = "/io/fractile/Bridge";
/// Longest gap between two polls of a live script (it re-polls at once).
const HEARTBEAT_GRACE: Duration = Duration::from_secs(3);

struct Service {
    d: Arc<Daemon>,
    b: Arc<BridgeAdapter>,
    tx: async_channel::Sender<Value>,
    rx: async_channel::Receiver<Value>,
    /// Bus unique name -> connection id used by the bridge.
    conns: Mutex<HashMap<String, u64>>,
    next: AtomicU64,
    beat: Arc<Heartbeat>,
}

#[derive(Default)]
struct Heartbeat {
    /// (polls in flight, end of the last poll)
    state: Mutex<(u32, Option<Instant>)>,
}

impl Heartbeat {
    fn reset(&self) {
        *self.state.lock().unwrap() = (0, Some(Instant::now()));
    }
    fn begin(&self) {
        self.state.lock().unwrap().0 += 1;
    }
    fn end(&self) {
        let mut s = self.state.lock().unwrap();
        s.0 = s.0.saturating_sub(1);
        s.1 = Some(Instant::now());
    }
    fn stale(&self) -> bool {
        let s = self.state.lock().unwrap();
        s.0 == 0 && s.1.map(|t| t.elapsed() > HEARTBEAT_GRACE).unwrap_or(false)
    }
}

/// Ends the poll even when the future is dropped.
struct PollGuard<'a>(&'a Heartbeat);
impl Drop for PollGuard<'_> {
    fn drop(&mut self) {
        self.0.end();
    }
}

fn denied(e: impl std::fmt::Display) -> fdo::Error {
    fdo::Error::AccessDenied(e.to_string())
}

impl Service {
    async fn caller(&self, hdr: &Header<'_>, conn: &zbus::Connection) -> fdo::Result<(String, i32)> {
        let sender = hdr.sender().ok_or_else(|| denied("no sender"))?.to_string();
        let dbus = fdo::DBusProxy::new(conn).await?;
        let name = BusName::try_from(sender.as_str()).map_err(denied)?;
        let pid = dbus.get_connection_unix_process_id(name).await? as i32;
        if let Err(e) = bridge::verify_compositor_peer(pid, self.b.allowed_peers()) {
            warn!(self.d.log, "refused {} call from pid {pid}: {e}", hdr.member().map(|m| m.to_string()).unwrap_or_default());
            return Err(denied(e));
        }
        Ok((sender, pid))
    }

    /// The caller must be the attached integration connection.
    async fn attached_caller(&self, hdr: &Header<'_>, conn: &zbus::Connection) -> fdo::Result<()> {
        let (sender, _) = self.caller(hdr, conn).await?;
        let id = self.conns.lock().unwrap().get(&sender).copied();
        match id {
            Some(id) if self.b.is_bridge_conn(id) => Ok(()),
            _ => Err(denied("not the attached integration")),
        }
    }
}

#[zbus::interface(name = "io.fractile.Bridge1")]
impl Service {
    /// Attach as the KWin integration. `info` is the same JSON object the
    /// GNOME extension sends with `bridge_attach`.
    async fn attach(&self, #[zbus(header)] hdr: Header<'_>, #[zbus(connection)] conn: &zbus::Connection, info: String) -> fdo::Result<String> {
        let (sender, pid) = self.caller(&hdr, conn).await?;
        let p: Value = serde_json::from_str(&info).map_err(|e| fdo::Error::InvalidArgs(e.to_string()))?;
        let id = self.next.fetch_add(1, Ordering::SeqCst);
        // Calls queued for a previous (dead) script instance are void.
        while self.rx.try_recv().is_ok() {}
        self.b.attach(id, pid, Transport::Queue(self.tx.clone()), &p).map_err(denied)?;
        self.conns.lock().unwrap().insert(sender.clone(), id);
        info!(self.d.log, "kwin integration attached from pid {pid}");
        self.beat.reset();
        watch_owner(self.d.clone(), self.b.clone(), sender, id);
        watch_heartbeat(self.d.clone(), self.b.clone(), self.beat.clone(), id);
        let d = self.d.clone();
        std::thread::spawn(move || {
            d.register_hotkeys();
            d.broadcast("capabilities_changed", json!({}));
        });
        Ok(json!({"api": fractile_core::adapter::ADAPTER_API_VERSION}).to_string())
    }

    /// Wait up to `timeout_ms` for calls; returns a JSON array (possibly
    /// empty). `i32` because KWin's callDBus marshals JS numbers as int32.
    async fn poll(&self, #[zbus(header)] hdr: Header<'_>, #[zbus(connection)] conn: &zbus::Connection, timeout_ms: i32) -> fdo::Result<String> {
        self.attached_caller(&hdr, conn).await?;
        self.beat.begin();
        let _g = PollGuard(&self.beat);
        let wait = Duration::from_millis(timeout_ms.clamp(10, 20_000) as u64);
        let first = futures_lite::future::or(async { self.rx.recv().await.ok() }, async {
            async_io::Timer::after(wait).await;
            None
        })
        .await;
        let mut out = Vec::new();
        if let Some(v) = first {
            out.push(v);
            while let Ok(v) = self.rx.try_recv() {
                out.push(v);
            }
        }
        Ok(Value::Array(out).to_string())
    }

    async fn result(&self, #[zbus(header)] hdr: Header<'_>, #[zbus(connection)] conn: &zbus::Connection, payload: String) -> fdo::Result<()> {
        self.attached_caller(&hdr, conn).await?;
        let p: Value = serde_json::from_str(&payload).map_err(|e| fdo::Error::InvalidArgs(e.to_string()))?;
        self.b.deliver_result(&p);
        Ok(())
    }

    async fn event(&self, #[zbus(header)] hdr: Header<'_>, #[zbus(connection)] conn: &zbus::Connection, payload: String) -> fdo::Result<()> {
        self.attached_caller(&hdr, conn).await?;
        let p: Value = serde_json::from_str(&payload).map_err(|e| fdo::Error::InvalidArgs(e.to_string()))?;
        self.b.deliver_event(&p).map_err(fdo::Error::InvalidArgs)
    }
}

/// Detach when the script's bus connection goes away (KWin restart, script
/// disabled).
fn watch_owner(d: Arc<Daemon>, b: Arc<BridgeAdapter>, unique: String, id: u64) {
    std::thread::Builder::new()
        .name("kwin-owner".into())
        .spawn(move || {
            let run = || -> zbus::Result<()> {
                let c = zbus::blocking::Connection::session()?;
                let p = zbus::blocking::fdo::DBusProxy::new(&c)?;
                let stream = p.receive_name_owner_changed_with_args(&[(0, unique.as_str())])?;
                // The name may already be gone.
                if !p.name_has_owner(BusName::try_from(unique.as_str())?)? {
                    return Ok(());
                }
                for sig in stream {
                    if let Ok(a) = sig.args() {
                        if a.new_owner().is_none() {
                            break;
                        }
                    }
                }
                Ok(())
            };
            let _ = run();
            if b.detach(id) {
                warn!(d.log, "kwin integration disconnected");
                d.broadcast("capabilities_changed", json!({}));
            }
        })
        .ok();
}

/// Detach a script that stopped polling (unloaded or disabled).
fn watch_heartbeat(d: Arc<Daemon>, b: Arc<BridgeAdapter>, beat: Arc<Heartbeat>, id: u64) {
    std::thread::Builder::new()
        .name("kwin-heartbeat".into())
        .spawn(move || loop {
            std::thread::sleep(Duration::from_millis(500));
            if !b.is_bridge_conn(id) {
                return;
            }
            if beat.stale() {
                if b.detach(id) {
                    warn!(d.log, "kwin integration stopped polling; detached");
                    d.broadcast("capabilities_changed", json!({}));
                }
                return;
            }
        })
        .ok();
}

/// Serve `io.fractile.Bridge` for the KWin script. The connection lives for
/// the daemon's lifetime.
pub fn start(d: Arc<Daemon>, b: Arc<BridgeAdapter>) -> zbus::Result<()> {
    let (tx, rx) = async_channel::bounded(256);
    let svc = Service { d: d.clone(), b, tx, rx, conns: Mutex::new(HashMap::new()), next: AtomicU64::new(1 << 32), beat: Arc::new(Heartbeat::default()) };
    let conn = zbus::blocking::connection::Builder::session()?.name(BUS_NAME)?.serve_at(PATH, svc)?.build()?;
    info!(d.log, "serving {BUS_NAME} for the KWin script");
    std::mem::forget(conn);
    Ok(())
}

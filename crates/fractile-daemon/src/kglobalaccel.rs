//! Global shortcuts on KDE Plasma through KGlobalAccel (org.kde.kglobalaccel),
//! the same service every KDE application uses. No input is read or grabbed
//! by Fractile; KDE reports activations as D-Bus signals.

use crate::bridge::{BridgeAdapter, HotkeyService};
use fractile_core::accel::Accelerator;
use fractile_core::adapter::{AResult, AdapterError, AdapterEvent};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use zbus::blocking::{Connection, Proxy};

const COMPONENT: &str = "fractile";
const COMPONENT_NAME: &str = "Fractile";
/// KGlobalAccel::SetPresent | NoAutoloading: use exactly these keys.
const FLAGS: u32 = 0x2 | 0x4;

pub struct KGlobalAccel {
    conn: Connection,
    bound: Mutex<HashMap<String, i32>>,
}

fn other(e: impl std::fmt::Display) -> AdapterError {
    AdapterError::Other(format!("KGlobalAccel: {e}"))
}

impl KGlobalAccel {
    pub fn connect(bridge: Arc<BridgeAdapter>) -> zbus::Result<Arc<KGlobalAccel>> {
        let conn = Connection::session()?;
        // Fail early when there is no KGlobalAccel on this bus.
        let dbus = zbus::blocking::fdo::DBusProxy::new(&conn)?;
        if !dbus.name_has_owner("org.kde.kglobalaccel".try_into()?)? {
            return Err(zbus::Error::Failure("org.kde.kglobalaccel is not running".into()));
        }
        let k = Arc::new(KGlobalAccel { conn: conn.clone(), bound: Mutex::new(HashMap::new()) });
        // A previous service that died while an editor listened left every
        // KDE shortcut held (KGlobalAccel's block is not tied to the caller).
        if block_marker().exists() {
            k.block_all(false);
        }
        listen(conn, bridge);
        Ok(k)
    }

    /// The KDE shortcut that owns `key`, as "“Show Logout Screen” (Session
    /// Management)", when KGlobalAccel names one.
    fn owner(&self, key: i32) -> Option<String> {
        type Info = (String, String, String, String, String, String, Vec<i32>, Vec<i32>);
        let found: Vec<Info> = self.proxy().ok()?.call("getGlobalShortcutsByKey", &(key,)).ok()?;
        found.into_iter().find(|i| i.2 != COMPONENT).map(|i| {
            let name = if i.1.is_empty() { i.0 } else { i.1 };
            if i.3.is_empty() { format!("“{name}”") } else { format!("“{name}” ({})", i.3) }
        })
    }

    fn proxy(&self) -> AResult<Proxy<'_>> {
        Proxy::new(&self.conn, "org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel").map_err(other)
    }

    fn action_id(binding: &str) -> Vec<String> {
        vec![COMPONENT.into(), binding.into(), COMPONENT_NAME.into(), binding.into()]
    }
}

impl HotkeyService for KGlobalAccel {
    fn register(&self, binding_id: &str, accel: &Accelerator) -> AResult<()> {
        let key = accel.to_qt_key().ok_or_else(|| AdapterError::Rejected(format!("{} cannot be expressed as a KDE shortcut", accel.to_display())))?;
        let p = self.proxy()?;
        let id = Self::action_id(binding_id);
        p.call_method("doRegister", &(id.clone(),)).map_err(other)?;
        let got: Vec<i32> = p.call("setShortcut", &(id, vec![key], FLAGS)).map_err(other)?;
        if got.first().copied() != Some(key) {
            // KDE keeps a key that another component already owns.
            let _ = p.call_method("unRegister", &(Self::action_id(binding_id),));
            let msg = match self.owner(key) {
                Some(o) => format!("{} is already used by the KDE shortcut {o}, set in System Settings > Keyboard > Shortcuts", accel.to_display()),
                None => format!("{} is already used by another KDE shortcut", accel.to_display()),
            };
            return Err(AdapterError::Conflict(msg));
        }
        self.bound.lock().unwrap().insert(binding_id.to_string(), key);
        Ok(())
    }

    fn unregister(&self, binding_id: &str) -> AResult<()> {
        if self.bound.lock().unwrap().remove(binding_id).is_none() {
            return Ok(());
        }
        let p = self.proxy()?;
        p.call_method("unRegister", &(Self::action_id(binding_id),)).map_err(other)?;
        Ok(())
    }

    /// KGlobalAccel's blockGlobalShortcuts, which KDE's own shortcut editor
    /// uses while it records: KWin then passes every combination to the
    /// focused window. A marker file records the hold so a service that dies
    /// mid-hold releases it on its next start.
    fn block_all(&self, on: bool) -> bool {
        let ok = self.proxy().and_then(|p| p.call_method("blockGlobalShortcuts", &(on,)).map_err(other)).is_ok();
        let m = block_marker();
        if on && ok {
            let _ = std::fs::write(&m, b"");
        } else if !on {
            let _ = std::fs::remove_file(&m);
        }
        ok
    }
}

fn block_marker() -> std::path::PathBuf {
    fractile_core::paths::runtime_dir().join("kde-shortcuts-held")
}

/// Forward `globalShortcutPressed` for our component as hotkey events.
fn listen(conn: Connection, bridge: Arc<BridgeAdapter>) {
    std::thread::Builder::new()
        .name("kglobalaccel".into())
        .spawn(move || {
            let rule = match zbus::MatchRule::builder()
                .msg_type(zbus::message::Type::Signal)
                .interface("org.kde.kglobalaccel.Component")
                .and_then(|b| b.member("globalShortcutPressed"))
                .and_then(|b| b.path(format!("/component/{COMPONENT}")))
            {
                Ok(b) => b.build(),
                Err(_) => return,
            };
            let Ok(it) = zbus::blocking::MessageIterator::for_match_rule(rule, &conn, Some(64)) else { return };
            for m in it.flatten() {
                if let Ok((component, action, ts)) = m.body().deserialize::<(String, String, i64)>() {
                    if component == COMPONENT {
                        bridge.inject_event(AdapterEvent::Hotkey { binding_id: action, timestamp: ts as u32 });
                    }
                }
            }
        })
        .ok();
}

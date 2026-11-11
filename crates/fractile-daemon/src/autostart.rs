//! Autostart reconciliation. Preferred: the packaged systemd user unit
//! (`fractile.service`, PartOf=graphical-session.target). Fallback: a user
//! XDG autostart entry. Only the user's own toggle changes either one.
//! FRACTILE_AUTOSTART_MODE=systemd|xdg|none overrides detection (tests).

use crate::daemon::Daemon;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Command;

pub const UNIT: &str = "fractile.service";
pub const XDG_FILE: &str = "io.fractile.Fractile.desktop";

fn mode() -> String {
    if let Ok(m) = std::env::var("FRACTILE_AUTOSTART_MODE") {
        return m;
    }
    if unit_installed() && fractile_session::launch::systemd_user_available() {
        "systemd".into()
    } else {
        "xdg".into()
    }
}

fn config_home() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".config"))
}

pub fn xdg_path() -> PathBuf {
    config_home().join("autostart").join(XDG_FILE)
}

fn unit_installed() -> bool {
    let mut dirs = vec![config_home().join("systemd/user")];
    dirs.extend(["/usr/lib/systemd/user", "/usr/local/lib/systemd/user", "/etc/systemd/user"].iter().map(PathBuf::from));
    dirs.iter().any(|d| d.join(UNIT).exists())
}

fn systemd_enabled() -> bool {
    Command::new("systemctl").args(["--user", "is-enabled", UNIT]).output().map(|o| String::from_utf8_lossy(&o.stdout).trim() == "enabled").unwrap_or(false)
}

fn xdg_enabled() -> bool {
    std::fs::read_to_string(xdg_path()).map(|s| !s.lines().any(|l| l.trim() == "Hidden=true" || l.trim() == "X-GNOME-Autostart-enabled=false")).unwrap_or(false)
}

pub fn status() -> Value {
    let m = mode();
    let enabled = match m.as_str() {
        "systemd" => systemd_enabled(),
        "xdg" => xdg_enabled(),
        _ => false,
    };
    json!({"mode": m, "enabled": enabled, "unit_installed": unit_installed(), "xdg_path": xdg_path()})
}

fn xdg_entry() -> String {
    let exe = std::env::current_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| "fractiled".into());
    // Exec has no field codes and no shell; the path is quoted per the spec.
    let quoted = if exe.chars().any(|c| c.is_whitespace() || "\"`$\\".contains(c)) { format!("\"{}\"", exe.replace('\\', "\\\\").replace('"', "\\\"").replace('`', "\\`").replace('$', "\\$")) } else { exe };
    format!(
        "[Desktop Entry]\nType=Application\nName=Fractile\nComment=Window grid placement and session restore\nExec={quoted} --autostart\nIcon=io.fractile.Fractile\nTerminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Phase=Applications\nX-KDE-autostart-phase=2\n"
    )
}

pub fn set(enabled: bool) -> Result<Value, String> {
    match mode().as_str() {
        "systemd" => {
            let verb = if enabled { "enable" } else { "disable" };
            let out = Command::new("systemctl").args(["--user", verb, UNIT]).output().map_err(|e| e.to_string())?;
            if !out.status.success() {
                return Err(format!("systemctl --user {verb} {UNIT} failed: {}", String::from_utf8_lossy(&out.stderr).trim()));
            }
            // Never leave a duplicate XDG entry behind.
            let _ = std::fs::remove_file(xdg_path());
        }
        "xdg" => {
            let p = xdg_path();
            if enabled {
                std::fs::create_dir_all(p.parent().unwrap()).map_err(|e| e.to_string())?;
                fractile_core::config::atomic_write(&p, xdg_entry().as_bytes(), false)?;
            } else if p.exists() {
                std::fs::remove_file(&p).map_err(|e| e.to_string())?;
            }
        }
        _ => {}
    }
    Ok(status())
}

/// Make the real autostart state follow the config, only when they differ.
pub fn reconcile(d: &Daemon) {
    let want = d.config().general.autostart;
    let st = status();
    if st["mode"] == "none" || st["enabled"].as_bool() == Some(want) {
        return;
    }
    match set(want) {
        Ok(v) => info!(d.log, "autostart set to {want} via {}", v["mode"]),
        Err(e) => warn!(d.log, "autostart change failed: {e}"),
    }
}

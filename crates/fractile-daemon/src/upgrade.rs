//! Restart onto a new binary after a package upgrade.
//!
//! dpkg and pacman replace /usr/bin/fractiled by writing a new file and
//! renaming it over the old one. The running service keeps executing the
//! old, now unlinked, file until something restarts it, and a package
//! cannot reliably restart a per-user service for every logged-in user.
//! So the service watches for that itself: when /proc/self/exe reads
//! "<path> (deleted)" and a new executable is at <path>, it re-executes
//! itself in place once it is idle.
//!
//! In place (exec, same pid) so systemd keeps tracking the unit. This is
//! not the end of the login session: no logout snapshot and no
//! session_end, so the new process carries on the same session (same
//! session key and boot, so it does not count as a new login).

use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::daemon::Daemon;


/// How often to look, and how long after start before the first look (the
/// login flow and any restore it starts happen first).
const CHECK_EVERY: Duration = Duration::from_secs(20);
const FIRST_CHECK_AFTER: Duration = Duration::from_secs(60);

/// The path the running binary was started from, if that file has since
/// been replaced by another executable. `exe_link` is what
/// /proc/self/exe reads.
pub fn replaced_binary(exe_link: &Path) -> Option<PathBuf> {
    let s = exe_link.to_str()?;
    let path = PathBuf::from(s.strip_suffix(" (deleted)")?);
    let meta = std::fs::metadata(&path).ok()?;
    (meta.is_file() && meta.permissions().mode() & 0o111 != 0).then_some(path)
}

fn idle(d: &Daemon) -> bool {
    !d.panel.lock().unwrap().visible && d.restore.lock().unwrap().is_none() && !d.shutdown.load(Ordering::SeqCst)
}

pub fn start(d: Arc<Daemon>) {
    if std::env::var_os("FRACTILE_NO_SELF_RESTART").is_some() {
        return;
    }
    let started = Instant::now();
    std::thread::Builder::new()
        .name("upgrade-watch".into())
        .spawn(move || {
            // The new file must look the same on two checks in a row: an
            // upgrade still unpacking is left to finish.
            let mut seen: Option<(PathBuf, std::time::SystemTime, u64)> = None;
            loop {
                std::thread::sleep(CHECK_EVERY);
                if d.shutdown.load(Ordering::SeqCst) {
                    return;
                }
                if started.elapsed() < FIRST_CHECK_AFTER {
                    continue;
                }
                let Ok(link) = std::fs::read_link("/proc/self/exe") else { continue };
                let Some(path) = replaced_binary(&link) else {
                    seen = None;
                    continue;
                };
                let Ok(meta) = std::fs::metadata(&path) else { continue };
                let id = (path.clone(), meta.modified().unwrap_or(std::time::UNIX_EPOCH), meta.len());
                if seen.as_ref() != Some(&id) {
                    info!(d.log, "the installed service changed ({}); will restart when idle", path.display());
                    seen = Some(id);
                    continue;
                }
                if !idle(&d) {
                    continue;
                }
                restart(&d, &path);
            }
        })
        .unwrap();
}

fn restart(d: &Daemon, path: &Path) {
    info!(d.log, "restarting onto the new service binary {}", path.display());
    d.release_hotkeys_for_exit();
    d.broadcast("daemon_exit", serde_json::json!({"restarting": true}));
    // Hold the store so no write is cut off by the exec. Every other
    // descriptor (listening socket, lock file, X11 and D-Bus connections)
    // is close-on-exec, so the new process finds the socket stale and the
    // lock free, as after any restart.
    let store = d.store.lock().unwrap();
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    let err = std::process::Command::new(path).args(&args).exec();
    // Only reached when exec failed: keep serving with the old binary.
    drop(store);
    warn!(d.log, "could not restart onto {}: {err}; the running service carries on", path.display());
    d.register_hotkeys();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_deleted_exe_with_a_new_executable_counts() {
        let dir = std::env::temp_dir().join(format!("fractile-upgrade-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let bin = dir.join("fractiled");
        std::fs::write(&bin, b"#!/bin/sh\n").unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        let deleted = PathBuf::from(format!("{} (deleted)", bin.display()));
        assert_eq!(replaced_binary(&deleted), Some(bin.clone()));
        // Still the original file: nothing to do.
        assert_eq!(replaced_binary(&bin), None);
        // Not executable (half-written, or not a program): wait.
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(replaced_binary(&deleted), None);
        // Removed (package uninstalled): keep running.
        std::fs::remove_file(&bin).unwrap();
        assert_eq!(replaced_binary(&deleted), None);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

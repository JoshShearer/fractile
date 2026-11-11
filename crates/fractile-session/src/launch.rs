//! Safe application launching. Never uses a shell. Each launch is placed in
//! its own systemd user scope (`app-fractile-<id>-<n>.scope`) so apps survive
//! a restart of the Fractile service and remain identifiable; falls back to
//! a detached double-fork with `setsid` when systemd is unavailable.

use fractile_core::desktop::{exec_to_argv, DesktopEntry};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};

#[derive(Debug, Clone, PartialEq)]
pub struct LaunchPlan {
    pub desktop_id: String,
    pub argv: Vec<String>,
    pub method: &'static str,
}

/// Build the argv that would be executed (for dry-run display and tests).
pub fn plan_launch(entry: &DesktopEntry, extra_args: &[String], use_systemd: bool, gio_available: bool) -> Result<LaunchPlan, String> {
    if entry.terminal {
        return Err(format!("{} requires a terminal; refusing to guess a terminal emulator", entry.id));
    }
    let id_stem = entry.id.trim_end_matches(".desktop");
    // systemd-escape style: keep [A-Za-z0-9:_.], encode everything else as \xNN
    let escaped: String = id_stem
        .bytes()
        .map(|b| if b.is_ascii_alphanumeric() || b == b':' || b == b'_' || b == b'.' { (b as char).to_string() } else { format!("\\x{b:02x}") })
        .collect();
    let unit = format!("app-fractile-{}-{}.scope", escaped, std::process::id() as u64 * 1000 + (rand_suffix() % 1000));
    let (inner, method): (Vec<String>, &'static str) = if extra_args.is_empty() && gio_available && (entry.dbus_activatable || entry.exec.is_some()) {
        // gio handles DBusActivatable, field codes and startup notification
        // correctly; the desktop file path is the only argument.
        (vec!["gio".into(), "launch".into(), entry.path.to_string_lossy().to_string()], "gio-launch")
    } else {
        let exec = entry.exec.as_deref().ok_or_else(|| format!("{} has no Exec line", entry.id))?;
        let mut argv = exec_to_argv(exec, &entry.name, entry.icon.as_deref())?;
        argv.extend(extra_args.iter().cloned());
        (argv, "exec")
    };
    if use_systemd {
        let mut argv = vec!["systemd-run".into(), "--user".into(), "--scope".into(), "--quiet".into(), "--collect".into(), format!("--unit={unit}"), "--".into()];
        argv.extend(inner);
        Ok(LaunchPlan { desktop_id: entry.id.clone(), argv, method: if method == "exec" { "systemd-scope+exec" } else { "systemd-scope+gio" } })
    } else {
        Ok(LaunchPlan { desktop_id: entry.id.clone(), argv: inner, method })
    }
}

fn rand_suffix() -> u64 {
    let n = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() as u64).unwrap_or(0);
    n ^ (n >> 7)
}

pub fn systemd_user_available() -> bool {
    std::env::var_os("XDG_RUNTIME_DIR").map(|d| Path::new(&d).join("systemd/private").exists()).unwrap_or(false) && which("systemd-run")
}

pub fn which(prog: &str) -> bool {
    std::env::var_os("PATH").map(|p| std::env::split_paths(&p).any(|d| d.join(prog).is_file())).unwrap_or(false)
}

/// Spawn detached. Returns the pid of the immediate child (systemd-run or
/// the program itself); the process is reaped in a background thread.
pub fn spawn(plan: &LaunchPlan) -> Result<u32, String> {
    let (prog, args) = plan.argv.split_first().ok_or("empty argv")?;
    let mut cmd = Command::new(prog);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    // Don't leak Fractile's own environment markers into apps. Fractile's
    // own UI is the exception: it must reach the same service instance.
    if plan.method != "ui" {
        cmd.env_remove("FRACTILE_RUNTIME_DIR").env_remove("FRACTILE_DATA_DIR").env_remove("FRACTILE_CONFIG").env_remove("FRACTILE_STATE_DIR");
    }
    cmd.env_remove("DESKTOP_STARTUP_ID");
    // SAFETY: setsid is async-signal-safe and has no preconditions.
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let mut child = cmd.spawn().map_err(|e| format!("failed to start {}: {e}", prog))?;
    let pid = child.id();
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(pid)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn entry(exec: &str) -> DesktopEntry {
        DesktopEntry { id: "org.x.App.desktop".into(), path: PathBuf::from("/usr/share/applications/org.x.App.desktop"), name: "App".into(), exec: Some(exec.into()), kind: "Application".into(), ..Default::default() }
    }

    #[test]
    fn plans_are_argv_never_shell() {
        let p = plan_launch(&entry("app --x %U"), &["--restore".into()], true, true).unwrap();
        assert_eq!(&p.argv[0], "systemd-run");
        let dashdash = p.argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(&p.argv[dashdash + 1..], &["app", "--x", "--restore"]);
        assert!(p.argv.iter().any(|a| a.starts_with("--unit=app-fractile-org.x.App-")));
        let p = plan_launch(&entry("app"), &[], false, true).unwrap();
        assert_eq!(p.argv, vec!["gio", "launch", "/usr/share/applications/org.x.App.desktop"]);
        let mut t = entry("vim");
        t.terminal = true;
        assert!(plan_launch(&t, &[], true, true).is_err());
    }
}

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
///
/// A launch that fails at once is reported, not counted as a launch: the
/// child is watched for `EARLY_EXIT` and a non-zero exit in that time is an
/// error with the program's own message. If the failing program is the
/// systemd-run wrapper (no user manager reachable on this session bus, for
/// example), the app is started without the scope instead.
pub fn spawn(plan: &LaunchPlan) -> Result<u32, String> {
    match spawn_checked(&plan.argv, plan.method) {
        Err(e) if plan.argv.first().map(|p| Path::new(p).file_name().map(|f| f == "systemd-run").unwrap_or(false)).unwrap_or(false) => {
            let inner: Vec<String> = plan.argv.iter().skip_while(|a| *a != "--").skip(1).cloned().collect();
            if inner.is_empty() {
                return Err(e);
            }
            spawn_checked(&inner, plan.method)
                .map_err(|e2| format!("{e}; without the systemd scope: {e2}"))
                .inspect(|_| eprintln!("fractile: {e}; started {} without a systemd scope", plan.desktop_id))
        }
        r => r,
    }
}

const EARLY_EXIT: std::time::Duration = std::time::Duration::from_millis(500);

fn spawn_checked(argv: &[String], method: &str) -> Result<u32, String> {
    use std::io::Read;
    let (prog, args) = argv.split_first().ok_or("empty argv")?;
    let mut cmd = Command::new(prog);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
    // Don't leak Fractile's own environment markers into apps. Fractile's
    // own UI is the exception: it must reach the same service instance.
    if method != "ui" {
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
    let mut err_pipe = child.stderr.take();
    let start = std::time::Instant::now();
    while start.elapsed() < EARLY_EXIT {
        match child.try_wait() {
            Ok(Some(st)) if !st.success() => {
                let mut msg = String::new();
                if let Some(p) = err_pipe.as_mut() {
                    let _ = p.take(4096).read_to_string(&mut msg);
                }
                let msg = msg.trim().lines().last().unwrap_or("").to_string();
                return Err(format!("{prog} exited with {st}{}", if msg.is_empty() { String::new() } else { format!(": {msg}") }));
            }
            Ok(Some(_)) => break, // a launcher (gio) that handed over and exited: fine
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(20)),
            Err(_) => break,
        }
    }
    std::thread::spawn(move || {
        // Keep draining the app's stderr so it never blocks or gets SIGPIPE.
        if let Some(mut p) = err_pipe {
            let _ = std::io::copy(&mut p, &mut std::io::sink());
        }
        let _ = child.wait();
    });
    Ok(pid)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn a_launch_that_fails_at_once_is_an_error_with_its_message() {
        let plan = LaunchPlan { desktop_id: "x.desktop".into(), argv: vec!["sh".into(), "-c".into(), "echo no display >&2; exit 3".into()], method: "exec" };
        let e = spawn(&plan).unwrap_err();
        assert!(e.contains("exit status: 3") && e.contains("no display"), "{e}");
        // a launcher that hands over and exits 0 is a launch
        let ok = LaunchPlan { desktop_id: "x.desktop".into(), argv: vec!["true".into()], method: "exec" };
        assert!(spawn(&ok).is_ok());
    }

    #[test]
    fn a_failing_systemd_run_wrapper_falls_back_to_a_direct_start() {
        let d = std::env::temp_dir().join(format!("fractile-launch-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let fake = d.join("systemd-run");
        std::fs::write(&fake, "#!/bin/sh\necho 'Failed to start transient scope unit: no user manager' >&2\nexit 1\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        let mark = d.join("started");
        let plan = LaunchPlan {
            desktop_id: "x.desktop".into(),
            argv: vec![fake.to_string_lossy().into(), "--user".into(), "--scope".into(), "--".into(), "touch".into(), mark.to_string_lossy().into()],
            method: "systemd-scope+exec",
        };
        assert!(spawn(&plan).is_ok());
        for _ in 0..50 {
            if mark.exists() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(mark.exists(), "the app was not started without the scope");
        std::fs::remove_dir_all(d).unwrap();
    }

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

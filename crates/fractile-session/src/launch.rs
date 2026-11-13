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
/// `another_window`: the app already opened a window in this run and one
/// more is wanted. An app whose desktop entry declares a "new window" action
/// is then asked through that action, because activating a running app only
/// raises its existing window (Files 50 does that).
pub fn plan_launch(entry: &DesktopEntry, extra_args: &[String], use_systemd: bool, gio_available: bool, gdbus_available: bool, another_window: bool) -> Result<LaunchPlan, String> {
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
    let nw = entry.new_window_action.as_ref().filter(|_| another_window && extra_args.is_empty());
    let dbus_ok = entry.dbus_activatable && gdbus_available && is_bus_name(id_stem);
    let object_path = format!("/{}", id_stem.replace('.', "/").replace('-', "_"));
    let (inner, method): (Vec<String>, &'static str) = if let Some(a) = nw.filter(|a| dbus_ok && a.id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')) {
        // D-Bus activatable: the spec's ActivateAction with the action id.
        let name = format!("'{}'", a.id);
        let argv = ["gdbus", "call", "--session", "--dest", id_stem, "--object-path", &object_path, "--method", "org.freedesktop.Application.ActivateAction", &name, "[]", "{}"];
        (argv.iter().map(|s| s.to_string()).collect(), "dbus-activate")
    } else if let Some(exec) = nw.and_then(|a| a.exec.as_deref()).filter(|_| !entry.dbus_activatable || !dbus_ok) {
        (exec_to_argv(exec, &entry.name, entry.icon.as_deref())?, "exec")
    } else if extra_args.is_empty() && dbus_ok {
        // D-Bus activation (the Desktop Entry spec's DBusActivatable): call
        // Activate on the app's well-known name and wait for the reply. Not
        // `gio launch`: it sends the call and exits at once, and dbus-daemon
        // drops a message still waiting for its service to start when the
        // sender has gone, so the app starts without a window and exits.
        let argv = ["gdbus", "call", "--session", "--dest", id_stem, "--object-path", &object_path, "--method", "org.freedesktop.Application.Activate", "{}"];
        (argv.iter().map(|s| s.to_string()).collect(), "dbus-activate")
    } else if extra_args.is_empty() && gio_available && (entry.exec.is_none() || !entry.dbus_activatable) && (entry.dbus_activatable || entry.exec.is_some()) {
        // gio handles field codes and startup notification correctly; the
        // desktop file path is the only argument. (A D-Bus activatable entry
        // comes here only without an Exec line: gio is the launcher left.)
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
        Ok(LaunchPlan { desktop_id: entry.id.clone(), argv, method: match method { "exec" => "systemd-scope+exec", "dbus-activate" => "systemd-scope+dbus", _ => "systemd-scope+gio" } })
    } else {
        Ok(LaunchPlan { desktop_id: entry.id.clone(), argv: inner, method })
    }
}

/// A well-known D-Bus name, as the D-Bus Activation spec requires of a
/// DBusActivatable desktop id: two or more dot-separated elements of
/// [A-Za-z0-9_-], none empty or starting with a digit, at most 255 bytes.
fn is_bus_name(n: &str) -> bool {
    let parts: Vec<&str> = n.split('.').collect();
    n.len() <= 255 && parts.len() >= 2 && parts.iter().all(|p| !p.is_empty() && !p.as_bytes()[0].is_ascii_digit() && p.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'))
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
        let p = plan_launch(&entry("app --x %U"), &["--restore".into()], true, true, true, false).unwrap();
        assert_eq!(&p.argv[0], "systemd-run");
        let dashdash = p.argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(&p.argv[dashdash + 1..], &["app", "--x", "--restore"]);
        assert!(p.argv.iter().any(|a| a.starts_with("--unit=app-fractile-org.x.App-")));
        let p = plan_launch(&entry("app"), &[], false, true, true, false).unwrap();
        assert_eq!(p.argv, vec!["gio", "launch", "/usr/share/applications/org.x.App.desktop"]);
        let mut t = entry("vim");
        t.terminal = true;
        assert!(plan_launch(&t, &[], true, true, true, false).is_err());
    }

    /// `gio launch` sends org.freedesktop.Application.Activate and exits at
    /// once; dbus-daemon drops a message still waiting for its service to
    /// start when the sender has gone, so the app starts and never opens a
    /// window (reproduced on Ubuntu 26.04, GLib 2.88, Plasma 6.6 Wayland).
    /// A D-Bus activatable app is activated by a call that waits for the
    /// reply instead.
    #[test]
    fn dbus_activatable_apps_are_activated_by_a_call_that_waits_for_the_reply() {
        let mut e = entry("app %U");
        e.dbus_activatable = true;
        let p = plan_launch(&e, &[], true, true, true, false).unwrap();
        let dashdash = p.argv.iter().position(|a| a == "--").unwrap();
        let want = ["gdbus", "call", "--session", "--dest", "org.x.App", "--object-path", "/org/x/App", "--method", "org.freedesktop.Application.Activate", "{}"];
        assert_eq!(&p.argv[dashdash + 1..], &want);
        assert_eq!(p.method, "systemd-scope+dbus");
        // dashes become underscores in the object path (D-Bus Activation spec)
        let mut d = e.clone();
        d.id = "org.gnome.font-viewer.desktop".into();
        let p = plan_launch(&d, &[], false, true, true, false).unwrap();
        assert_eq!(p.argv[4..7], ["org.gnome.font-viewer", "--object-path", "/org/gnome/font_viewer"]);
        // not a valid bus name, or no gdbus: the Exec line, never gio's activation
        for (id, gdbus) in [("app.desktop", true), ("org.9x.App.desktop", true), ("org.x.App.desktop", false)] {
            let mut d = e.clone();
            d.id = id.into();
            let p = plan_launch(&d, &[], false, true, gdbus, false).unwrap();
            assert_eq!(p.argv, vec!["app"], "{id} gdbus={gdbus}");
        }
        // extra arguments need the Exec line
        let p = plan_launch(&e, &["--x".into()], false, true, true, false).unwrap();
        assert_eq!(p.argv, vec!["app", "--x"]);
        // no Exec line and no gdbus: gio is the only launcher left
        let mut n = e.clone();
        n.exec = None;
        let p = plan_launch(&n, &[], false, true, false, false).unwrap();
        assert_eq!(p.argv[0], "gio");
    }

    /// Activating a running app only raises its window; the next window of a
    /// restore uses the entry's "new window" action (Files 50, Plasma 6.6).
    #[test]
    fn another_window_uses_the_new_window_action() {
        let mut e = entry("nautilus --new-window %U");
        e.dbus_activatable = true;
        e.new_window_action = Some(fractile_core::desktop::DesktopAction { id: "new-window".into(), exec: Some("nautilus --new-window".into()) });
        // first window: plain activation
        let p = plan_launch(&e, &[], false, true, true, false).unwrap();
        assert_eq!(p.argv[8], "org.freedesktop.Application.Activate");
        // another: ActivateAction('new-window')
        let p = plan_launch(&e, &[], false, true, true, true).unwrap();
        assert_eq!(p.argv[8..], ["org.freedesktop.Application.ActivateAction", "'new-window'", "[]", "{}"]);
        // not D-Bus activatable (gnome-terminal): the action's Exec line
        let mut t = entry("gnome-terminal");
        t.new_window_action = Some(fractile_core::desktop::DesktopAction { id: "new-window".into(), exec: Some("gnome-terminal --window".into()) });
        assert_eq!(plan_launch(&t, &[], true, true, true, true).unwrap().argv.iter().skip_while(|a| *a != "--").skip(1).cloned().collect::<Vec<_>>(), ["gnome-terminal", "--window"]);
        // no gdbus: the action's Exec line
        assert_eq!(plan_launch(&e, &[], false, true, false, true).unwrap().argv, ["nautilus", "--new-window"]);
        // without the action, another window is a plain launch
        let p = plan_launch(&entry("app"), &[], false, true, true, true).unwrap();
        assert_eq!(p.argv, ["gio", "launch", "/usr/share/applications/org.x.App.desktop"]);
    }
}

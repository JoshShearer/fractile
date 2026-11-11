//! fractiled: the Fractile user-session service. Runs unprivileged, owns the
//! compositor adapter, the session store and the private IPC socket.

#[macro_use]
mod logging;
mod autostart;
mod backend;
mod bridge;
mod kglobalaccel;
mod kwin_dbus;
mod daemon;
mod ipc;
mod logind;
mod session;

use daemon::Daemon;
use fractile_core::adapter::CompositorAdapter;
use fractile_core::config::Config;
use fractile_core::paths;
use fractile_core::snapshot::SnapshotMode;
use logging::Logger;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

const USAGE: &str = "fractiled - Fractile session service

USAGE: fractiled [--autostart] [--replace] [--backend x11|gnome|kwin|cosmic] [--verbose]
       fractiled --check     print environment and capability detection, then exit
       fractiled --version

Most users never run this directly: it is started by the systemd user unit
(fractile.service) or the XDG autostart entry, and controlled with `fractile`.";

pub fn request_shutdown(d: &Daemon) {
    d.shutdown.store(true, Ordering::SeqCst);
    // Wake the accept loop.
    let _ = std::os::unix::net::UnixStream::connect(paths::socket_path());
}

fn take_lock() -> std::io::Result<Option<std::fs::File>> {
    let p = paths::lock_path();
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d)?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700))?;
    }
    let f = std::fs::OpenOptions::new().create(true).truncate(false).write(true).open(&p)?;
    // SAFETY: valid fd.
    let r = unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if r != 0 {
        return Ok(None);
    }
    Ok(Some(f))
}

fn make_adapter(env: &backend::Environment, forced: Option<&str>, log: &Logger) -> Arc<dyn CompositorAdapter> {
    let choice = env.choose(forced);
    log.log(logging::Level::Info, &format!("session={} desktop={} backend={choice:?}", env.session_type, env.current_desktop));
    match choice {
        backend::Backend::X11 => match fractile_adapter_x11::X11Adapter::connect(None) {
            Ok(a) => {
                log.log(logging::Level::Info, &format!("X11 adapter connected, window manager '{}'", a.wm_name()));
                Arc::new(a)
            }
            Err(e) => {
                log.log(logging::Level::Error, &format!("X11 adapter failed: {e}"));
                Arc::new(backend::StubAdapter::unsupported(env))
            }
        },
        backend::Backend::GnomeShell => bridge::install(Arc::new(bridge::BridgeAdapter::gnome())),
        backend::Backend::KWin => bridge::install(Arc::new(bridge::BridgeAdapter::kwin())),
        backend::Backend::Cosmic => Arc::new(backend::StubAdapter::cosmic()),
        _ => Arc::new(backend::StubAdapter::unsupported(env)),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let has = |f: &str| args.iter().any(|a| a == f);
    if has("--version") || has("-V") {
        println!("fractiled {}", fractile_core::VERSION);
        return;
    }
    if has("--help") || has("-h") {
        println!("{USAGE}");
        return;
    }
    // SAFETY: no preconditions.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("fractiled: refusing to run as root. Fractile is a per-user service.");
        std::process::exit(2);
    }
    let forced = args.iter().position(|a| a == "--backend").and_then(|i| args.get(i + 1)).map(|s| s.as_str());
    let verbose = has("--verbose") || has("-v");

    let env = backend::detect();
    if has("--check") {
        // Output is often piped into `head`; exit quietly on a closed pipe.
        // SAFETY: restoring the default disposition of SIGPIPE has no preconditions.
        unsafe {
            libc::signal(libc::SIGPIPE, libc::SIG_DFL);
        }
        // Per-user log (never a fixed path in a shared temp dir).
        let log = Logger::new(paths::log_file(), 64, "info", false);
        let a = make_adapter(&env, forced, &log);
        let mut caps = a.capabilities();
        caps.tiling_manager = backend::detect_tiling(&env);
        let v = serde_json::json!({"environment": env, "backend": env.choose(forced), "capabilities": caps, "outputs": a.get_outputs().ok()});
        println!("{}", serde_json::to_string_pretty(&v).unwrap());
        return;
    }

    let lock = match take_lock() {
        Ok(Some(f)) => f,
        Ok(None) => {
            if has("--replace") {
                if let Ok(mut c) = fractile_ipc::Client::connect(&paths::socket_path(), "fractiled-replace") {
                    let _ = c.call("quit", serde_json::json!({}));
                }
                let mut got = None;
                for _ in 0..50 {
                    std::thread::sleep(Duration::from_millis(100));
                    if let Ok(Some(f)) = take_lock() {
                        got = Some(f);
                        break;
                    }
                }
                match got {
                    Some(f) => f,
                    None => {
                        eprintln!("fractiled: the running instance did not exit");
                        std::process::exit(1);
                    }
                }
            } else {
                eprintln!("fractiled: already running");
                return;
            }
        }
        Err(e) => {
            eprintln!("fractiled: cannot create runtime lock {}: {e}", paths::lock_path().display());
            std::process::exit(1);
        }
    };

    let cfg_path = paths::config_file();
    let (cfg, cfg_error) = if cfg_path.exists() {
        match Config::load(&cfg_path) {
            Ok(c) => (c, None),
            // Keep the user's broken file untouched; run on defaults and say so.
            Err(e) => (Config::default(), Some(e)),
        }
    } else {
        (Config::default(), None)
    };
    let log = Arc::new(Logger::new(paths::log_file(), cfg.diagnostics.max_log_kb, &cfg.diagnostics.log_level, verbose));
    info!(log, "fractiled {} starting pid={} autostart={}", fractile_core::VERSION, std::process::id(), has("--autostart"));
    if let Some(e) = &cfg_error {
        error!(log, "config error, using defaults: {e}");
    }
    let store = match fractile_session::store::Store::open(&paths::db_file()) {
        Ok(s) => s,
        Err(e) => {
            error!(log, "cannot open store {}: {e}", paths::db_file().display());
            eprintln!("fractiled: cannot open database: {e}");
            std::process::exit(1);
        }
    };
    let key = session::session_key();
    let new_login = store.session_start(&key, &session::boot_id()).unwrap_or(true);
    let adapter = make_adapter(&env, forced, &log);
    let tiling = backend::detect_tiling(&env);
    if let Some(t) = &tiling {
        warn!(log, "tiling manager detected: {t}");
    }
    let listener = match fractile_ipc::secure_bind(&paths::socket_path()) {
        Ok(l) => l,
        Err(e) => {
            error!(log, "cannot bind socket: {e}");
            eprintln!("fractiled: cannot bind {}: {e}", paths::socket_path().display());
            std::process::exit(1);
        }
    };
    let d = Arc::new(Daemon::new(cfg, cfg_path, cfg_error, store, adapter, env, log.clone(), key, new_login, tiling));
    d.register_ui(vec![], None);
    if let Some(b) = bridge::get().filter(|b| b.kind == "kwin") {
        // KDE global shortcuts go through KGlobalAccel, the script only
        // handles window management.
        match kglobalaccel::KGlobalAccel::connect(b.clone()) {
            Ok(k) => b.set_hotkey_service(k),
            Err(e) => warn!(log, "KGlobalAccel unavailable, no global shortcuts: {e}"),
        }
        if let Err(e) = kwin_dbus::start(d.clone(), b) {
            error!(log, "cannot serve {} for the KWin script: {e}", kwin_dbus::BUS_NAME);
        }
    }
    d.register_hotkeys();

    // IPC
    {
        let d = d.clone();
        std::thread::Builder::new().name("ipc".into()).spawn(move || ipc::serve(d, listener)).unwrap();
    }
    // Adapter events
    match d.adapter().subscribe_events() {
        Ok(rx) => {
            let d = d.clone();
            std::thread::Builder::new()
                .name("events".into())
                .spawn(move || {
                    while let Ok(ev) = rx.recv() {
                        if d.shutdown.load(Ordering::SeqCst) {
                            break;
                        }
                        d.handle_event(ev);
                    }
                })
                .unwrap();
        }
        Err(e) => warn!(d.log, "no adapter events: {e}"),
    }
    {
        let d = d.clone();
        std::thread::Builder::new().name("autosave".into()).spawn(move || session::autosave_loop(d)).unwrap();
    }
    if std::env::var_os("FRACTILE_NO_LOGIND").is_none() {
        logind::start(d.clone());
    }
    {
        let d = d.clone();
        std::thread::Builder::new().name("login".into()).spawn(move || session::login_flow(d)).unwrap();
    }

    // Signals: TERM/INT = orderly end of session, HUP = reload config.
    let mut signals = signal_hook::iterator::Signals::new([signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT, signal_hook::consts::SIGHUP]).expect("signals");
    {
        let d = d.clone();
        std::thread::Builder::new()
            .name("signals".into())
            .spawn(move || {
                for sig in signals.forever() {
                    if sig == signal_hook::consts::SIGHUP {
                        match Config::load(&d.cfg_path) {
                            Ok(c) => {
                                *d.cfg.write().unwrap() = c;
                                *d.cfg_error.lock().unwrap() = None;
                                d.register_hotkeys();
                                d.broadcast("config_changed", serde_json::json!({}));
                                info!(d.log, "config reloaded");
                            }
                            Err(e) => {
                                error!(d.log, "config reload failed, keeping current settings: {e}");
                                *d.cfg_error.lock().unwrap() = Some(e);
                            }
                        }
                        continue;
                    }
                    info!(d.log, "signal {sig}: ending session");
                    session::capture_at_exit(&d, SnapshotMode::Logout);
                    request_shutdown(&d);
                    break;
                }
            })
            .unwrap();
    }

    while !d.shutdown.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(200));
    }
    if let Some(j) = d.restore.lock().unwrap().as_ref() {
        j.cancel.store(true, Ordering::SeqCst);
    }
    d.broadcast("daemon_exit", serde_json::json!({}));
    let _ = d.store.lock().unwrap().session_end(&d.session_key, true);
    let _ = std::fs::remove_file(paths::socket_path());
    info!(d.log, "fractiled stopped");
    drop(lock);
}

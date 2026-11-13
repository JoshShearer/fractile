//! Application registry: indexes installed `.desktop` entries and resolves
//! live windows to desktop ids using deterministic evidence first.

use fractile_core::desktop::{app_ids_from_systemd_unit, desktop_id_from_relative, parse_desktop_entry, DesktopEntry};
use fractile_core::window::WindowInfo;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub fn application_dirs() -> Vec<PathBuf> {
    let mut dirs = vec![];
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    let data_home = std::env::var_os("XDG_DATA_HOME").filter(|v| !v.is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(".local/share"));
    dirs.push(data_home.join("applications"));
    dirs.push(data_home.join("flatpak/exports/share/applications"));
    let data_dirs = std::env::var("XDG_DATA_DIRS").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "/usr/local/share:/usr/share".into());
    for d in data_dirs.split(':').filter(|d| !d.is_empty()) {
        dirs.push(PathBuf::from(d).join("applications"));
    }
    dirs.push(PathBuf::from("/var/lib/flatpak/exports/share/applications"));
    dirs.push(PathBuf::from("/var/lib/snapd/desktop/applications"));
    let mut seen = std::collections::HashSet::new();
    dirs.retain(|d| seen.insert(d.clone()));
    dirs
}

#[derive(Default)]
pub struct AppIndex {
    pub entries: HashMap<String, DesktopEntry>,
    by_wm_class: HashMap<String, String>,
    by_exec_name: HashMap<String, String>,
    built_at: Option<Instant>,
    dirs: Vec<PathBuf>,
    /// Modification time of each application dir at build time (None:
    /// missing). Installing, removing or updating an app renames files in
    /// one of them, which changes that dir's mtime.
    dir_mtimes: Vec<Option<std::time::SystemTime>>,
}

fn dir_mtimes(dirs: &[PathBuf]) -> Vec<Option<std::time::SystemTime>> {
    dirs.iter().map(|d| std::fs::metadata(d).and_then(|m| m.modified()).ok()).collect()
}

impl AppIndex {
    pub fn build(dirs: &[PathBuf]) -> AppIndex {
        let mut idx = AppIndex { dirs: dirs.to_vec(), dir_mtimes: dir_mtimes(dirs), ..Default::default() };
        // Earlier dirs take precedence (XDG order): an id found in an earlier
        // dir shadows the same id later on.
        let mut rank: HashMap<String, usize> = HashMap::new();
        for (n, d) in dirs.iter().enumerate() {
            scan(d, d, &mut idx.entries, 0);
            for id in idx.entries.keys() {
                rank.entry(id.clone()).or_insert(n);
            }
        }
        idx.entries.retain(|_, e| e.kind == "Application" && !e.hidden);
        // Several entries can claim the same WM class or program (a browser
        // that ships both google-chrome.desktop and com.google.Chrome.desktop,
        // or a user's own copy of a system entry). The pick must not depend
        // on hash order: the entry from the user's own (earlier) dir first,
        // then the one whose id is the class itself, then a visible one, then
        // the lowest id.
        let mut ids: Vec<&String> = idx.entries.keys().collect();
        ids.sort();
        let key_for = |id: &String, class: &str| {
            let e = &idx.entries[id];
            let stem = id.strip_suffix(".desktop").unwrap_or(id).to_lowercase();
            (rank.get(id).copied().unwrap_or(usize::MAX), stem != class, e.no_display)
        };
        let mut wm: HashMap<String, (String, (usize, bool, bool))> = HashMap::new();
        let mut ex: HashMap<String, (String, (usize, bool, bool))> = HashMap::new();
        for id in ids {
            let e = &idx.entries[id];
            if let Some(c) = &e.startup_wm_class {
                let c = c.to_lowercase();
                let k = key_for(id, &c);
                // ids are visited in order, so on a tie the lowest id stays
                if wm.get(&c).map(|(_, old)| k < *old).unwrap_or(true) {
                    wm.insert(c, (id.clone(), k));
                }
            }
            if let Some(exec) = &e.exec {
                if let Ok(argv) = fractile_core::desktop::exec_to_argv(exec, &e.name, None) {
                    let prog = argv.iter().find(|a| !a.contains('=') && *a != "env").cloned().unwrap_or_else(|| argv[0].clone());
                    if let Some(name) = Path::new(&prog).file_name() {
                        let name = name.to_string_lossy().to_lowercase();
                        let k = key_for(id, &name);
                        if ex.get(&name).map(|(_, old)| k < *old).unwrap_or(true) {
                            ex.insert(name, (id.clone(), k));
                        }
                    }
                }
            }
        }
        idx.by_wm_class = wm.into_iter().map(|(c, (id, _))| (c, id)).collect();
        idx.by_exec_name = ex.into_iter().map(|(c, (id, _))| (c, id)).collect();
        idx.built_at = Some(Instant::now());
        idx
    }

    pub fn system() -> AppIndex {
        AppIndex::build(&application_dirs())
    }

    /// Rebuild when an application dir changed (an app was installed,
    /// removed or updated) or the index is older than `max_age`.
    pub fn refresh_if_stale(&mut self, max_age: Duration) {
        let changed = !self.dirs.is_empty() && dir_mtimes(&self.dirs) != self.dir_mtimes;
        if changed || self.built_at.map(|t| t.elapsed() > max_age).unwrap_or(true) {
            let dirs = if self.dirs.is_empty() { application_dirs() } else { self.dirs.clone() };
            *self = AppIndex::build(&dirs);
        }
    }

    pub fn get(&self, id: &str) -> Option<&DesktopEntry> {
        self.entries.get(id).or_else(|| self.entries.get(&format!("{id}.desktop")))
    }

    /// Program name of an entry's Exec line (lowercased basename).
    pub fn exec_name_of(&self, id: &str) -> Option<String> {
        let e = self.entries.get(id)?;
        let argv = fractile_core::desktop::exec_to_argv(e.exec.as_deref()?, &e.name, None).ok()?;
        let prog = argv.iter().find(|a| !a.contains('=') && *a != "env").cloned().or_else(|| argv.first().cloned())?;
        Path::new(&prog).file_name().map(|n| n.to_string_lossy().to_lowercase())
    }

    fn find_ci(&self, stem: &str) -> Option<String> {
        let want = format!("{}.desktop", stem.to_lowercase());
        if self.entries.contains_key(&format!("{stem}.desktop")) {
            return Some(format!("{stem}.desktop"));
        }
        self.entries.keys().find(|k| k.to_lowercase() == want).cloned()
    }

    /// Fill desktop_file_id / executable / sandbox on a window.
    /// Evidence order (most to least specific to *this* window):
    /// 1. the window's own claims: app id (_GTK_APPLICATION_ID / Wayland
    ///    app_id) == desktop id, StartupWMClass == WM_CLASS, desktop id == WM_CLASS
    /// 2. systemd scope of the process (app-<launcher>-<id>-N.scope)
    /// 3. GIO_LAUNCHED_DESKTOP_FILE, only when GIO_LAUNCHED_DESKTOP_FILE_PID is
    ///    this very process (the variable is inherited by children)
    /// 4. Exec program name == executable name, never for interpreters or
    ///    generic launchers (python3, sh, env, java, ...)
    ///
    /// Process-derived evidence (2-4) can be inherited from a parent such as a
    /// terminal, which is why the window's own claims win.
    pub fn enrich(&self, w: &mut WindowInfo) {
        let proc_dir = w.pid.map(|pid| PathBuf::from(format!("/proc/{pid}")));
        let mut unit: Option<String> = None;
        if let Some(pd) = &proc_dir {
            if w.executable.is_none() {
                w.executable = std::fs::read_link(pd.join("exe")).ok().map(|p| p.to_string_lossy().to_string());
            }
            if let Ok(cg) = std::fs::read_to_string(pd.join("cgroup")) {
                unit = cg.lines().filter_map(|l| l.rsplit('/').next()).find(|u| u.starts_with("app-") || u.starts_with("snap.")).map(|s| s.to_string());
                if let Some(u) = &unit {
                    if u.starts_with("app-flatpak-") {
                        w.sandbox = Some("flatpak".into());
                    } else if u.starts_with("snap.") || u.contains("-snap.") {
                        w.sandbox = Some("snap".into());
                    }
                }
            }
            if w.sandbox.is_none() && pd.join("root/.flatpak-info").exists() {
                w.sandbox = Some("flatpak".into());
            }
        }
        // An adapter's own desktop id is kept when it names an installed
        // entry. KWin reports the Wayland app id plus ".desktop" even when no
        // such file exists; then the evidence below decides, and the reported
        // id stays only if it finds nothing better.
        let reported = match w.desktop_file_id.take() {
            Some(id) if self.entries.is_empty() || self.entries.contains_key(&id) => {
                w.desktop_file_id = Some(id);
                return;
            }
            other => other,
        };
        let mut found: Option<(String, &str)> = None;
        let mut try_set = |id: Option<String>, src: &'static str| {
            if found.is_none() {
                if let Some(id) = id {
                    found = Some((id, src));
                }
            }
        };
        try_set(w.app_id.as_deref().and_then(|a| self.find_ci(a)), "app-id");
        try_set(w.wm_class.as_deref().and_then(|c| self.by_wm_class.get(&c.to_lowercase()).cloned()), "startup-wm-class");
        try_set(w.wm_class_instance.as_deref().and_then(|c| self.by_wm_class.get(&c.to_lowercase()).cloned()), "startup-wm-class");
        try_set(w.wm_class.as_deref().and_then(|c| self.find_ci(c)), "wm-class-name");
        try_set(w.wm_class_instance.as_deref().and_then(|c| self.find_ci(c)), "wm-class-name");
        if let Some(u) = &unit {
            // A scope is inherited by every child (e.g. apps started from a
            // terminal). Accept it only when the process plausibly *is* the
            // scope's app: sandboxed, same program, or the window has no
            // class of its own.
            let exe = w.executable.as_deref().and_then(|e| Path::new(e).file_name()).map(|n| n.to_string_lossy().to_lowercase());
            let cand = app_ids_from_systemd_unit(u).iter().find_map(|c| self.find_ci(c));
            let consistent = |id: &String| {
                w.sandbox.is_some()
                    || (w.wm_class.is_none() && w.app_id.is_none())
                    || match (self.exec_name_of(id), &exe) {
                        (Some(prog), Some(exe)) => &prog == exe || is_generic_program(&prog),
                        _ => true,
                    }
            };
            try_set(cand.filter(consistent), "systemd-scope");
        }
        if let (Some(pd), Some(pid)) = (&proc_dir, w.pid) {
            try_set(launched_desktop_file(pd, pid).filter(|id| self.entries.contains_key(id)), "launch-env");
        }
        let exe_name = w.executable.as_deref().and_then(|e| Path::new(e).file_name()).map(|n| n.to_string_lossy().to_lowercase());
        if let Some(n) = exe_name.filter(|n| !is_generic_program(n)) {
            try_set(self.by_exec_name.get(&n).cloned(), "exec-name");
        }
        if let Some((id, src)) = found {
            w.desktop_file_id = Some(id);
            w.desktop_file_source = Some(src.into());
        } else {
            w.desktop_file_id = reported;
        }
    }
}

/// GIO_LAUNCHED_DESKTOP_FILE, trusted only for the process GIO launched.
fn launched_desktop_file(proc_dir: &Path, pid: u32) -> Option<String> {
    let env = std::fs::read(proc_dir.join("environ")).ok()?;
    let mut file = None;
    let mut fpid = None;
    for kv in env.split(|b| *b == 0) {
        let s = String::from_utf8_lossy(kv);
        if let Some(v) = s.strip_prefix("GIO_LAUNCHED_DESKTOP_FILE=") {
            file = Path::new(v).file_name().map(|n| n.to_string_lossy().to_string());
        } else if let Some(v) = s.strip_prefix("GIO_LAUNCHED_DESKTOP_FILE_PID=") {
            fpid = v.trim().parse::<u32>().ok();
        }
    }
    if fpid == Some(pid) {
        file
    } else {
        None
    }
}

/// Programs that run many unrelated applications; their name says nothing
/// about which app a window belongs to.
pub fn is_generic_program(name: &str) -> bool {
    let base = name.trim_end_matches(|c: char| c.is_ascii_digit() || c == '.');
    matches!(base, "python" | "python3" | "pypy" | "pypy3" | "perl" | "ruby" | "node" | "nodejs" | "deno" | "bun" | "java" | "gjs" | "lua" | "luajit" | "php" | "sh" | "bash" | "dash" | "zsh" | "fish" | "env" | "mono" | "dotnet" | "wine" | "wine64" | "wine-preloader" | "wine64-preloader" | "electron" | "flatpak" | "bwrap" | "snap" | "steam" | "qemu-system-x86_64")
}

fn scan(base: &Path, dir: &Path, out: &mut HashMap<String, DesktopEntry>, depth: u32) {
    if depth > 3 {
        return;
    }
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            scan(base, &p, out, depth + 1);
            continue;
        }
        if p.extension().map(|x| x == "desktop").unwrap_or(false) {
            let rel = p.strip_prefix(base).unwrap_or(&p);
            let id = desktop_id_from_relative(rel);
            if out.contains_key(&id) {
                continue;
            }
            if let Ok(meta) = std::fs::metadata(&p) {
                if meta.len() > 256 * 1024 {
                    continue;
                }
            }
            if let Ok(content) = std::fs::read_to_string(&p) {
                if let Some(entry) = parse_desktop_entry(&id, &p, &content) {
                    out.insert(id, entry);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// KWin names a window's desktop file after its Wayland app id even when
    /// no such file exists: GNOME Disks (app id `gnome-disks`) is reported as
    /// `gnome-disks.desktop`, its entry is `org.gnome.DiskUtility.desktop`
    /// (Exec=gnome-disks, no StartupWMClass). Seen on Plasma 6.6 Wayland.
    #[test]
    fn a_reported_desktop_id_that_is_not_installed_is_resolved_from_other_evidence() {
        let d = std::env::temp_dir().join(format!("fractile-apps-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("org.gnome.DiskUtility.desktop"), "[Desktop Entry]\nType=Application\nName=Disks\nExec=gnome-disks\nDBusActivatable=true\n").unwrap();
        std::fs::write(d.join("org.x.Real.desktop"), "[Desktop Entry]\nType=Application\nName=Real\nExec=real\n").unwrap();
        let idx = AppIndex::build(std::slice::from_ref(&d));
        let mut w = WindowInfo {
            app_id: Some("gnome-disks".into()),
            wm_class: Some("gnome-disks".into()),
            executable: Some("/usr/bin/gnome-disks".into()),
            desktop_file_id: Some("gnome-disks.desktop".into()),
            desktop_file_source: Some("kwin-desktop-file-name".into()),
            ..Default::default()
        };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id.as_deref(), Some("org.gnome.DiskUtility.desktop"));
        assert_eq!(w.desktop_file_source.as_deref(), Some("exec-name"));
        // an installed reported id is kept as it is
        let mut w = WindowInfo { executable: Some("/usr/bin/gnome-disks".into()), desktop_file_id: Some("org.x.Real.desktop".into()), desktop_file_source: Some("kwin-desktop-file-name".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id.as_deref(), Some("org.x.Real.desktop"));
        assert_eq!(w.desktop_file_source.as_deref(), Some("kwin-desktop-file-name"));
        // nothing better found: the reported id stays
        let mut w = WindowInfo { app_id: Some("other".into()), desktop_file_id: Some("other.desktop".into()), desktop_file_source: Some("kwin-desktop-file-name".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id.as_deref(), Some("other.desktop"));
        assert_eq!(w.desktop_file_source.as_deref(), Some("kwin-desktop-file-name"));
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn resolves_by_startup_wm_class_and_name() {
        let d = std::env::temp_dir().join(format!("fractile-apps-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(d.join("sub")).unwrap();
        std::fs::write(d.join("org.example.Editor.desktop"), "[Desktop Entry]\nType=Application\nName=Editor\nExec=example-editor %F\nStartupWMClass=ExEdit\n").unwrap();
        std::fs::write(d.join("sub/tool.desktop"), "[Desktop Entry]\nType=Application\nName=Tool\nExec=/opt/tool/bin/tool\n").unwrap();
        std::fs::write(d.join("hidden.desktop"), "[Desktop Entry]\nType=Application\nName=H\nExec=h\nHidden=true\n").unwrap();
        let idx = AppIndex::build(&[d.clone()]);
        assert!(idx.get("sub-tool.desktop").is_some());
        assert!(idx.get("hidden.desktop").is_none());
        let mut w = WindowInfo { wm_class: Some("ExEdit".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id.as_deref(), Some("org.example.Editor.desktop"));
        assert_eq!(w.desktop_file_source.as_deref(), Some("startup-wm-class"));
        let mut w = WindowInfo { executable: Some("/opt/tool/bin/tool".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id.as_deref(), Some("sub-tool.desktop"));
        let mut w = WindowInfo { app_id: Some("org.example.editor".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_source.as_deref(), Some("app-id"));
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn app_installed_after_build_is_found_without_waiting() {
        let d = std::env::temp_dir().join(format!("fractile-apps-{}", uuid::Uuid::new_v4()));
        let late = d.join("late");
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("a.desktop"), "[Desktop Entry]\nType=Application\nName=A\nExec=a\n").unwrap();
        let mut idx = AppIndex::build(&[late.clone(), d.clone()]);
        assert!(idx.get("b.desktop").is_none());
        // a new entry in an existing dir, and a dir that did not exist yet
        std::thread::sleep(Duration::from_millis(20));
        std::fs::write(d.join("b.desktop"), "[Desktop Entry]\nType=Application\nName=B\nExec=b\n").unwrap();
        idx.refresh_if_stale(Duration::from_secs(3600));
        assert!(idx.get("b.desktop").is_some());
        std::fs::create_dir_all(&late).unwrap();
        std::fs::write(late.join("a.desktop"), "[Desktop Entry]\nType=Application\nName=A (user)\nExec=a --user\n").unwrap();
        idx.refresh_if_stale(Duration::from_secs(3600));
        assert_eq!(idx.get("a.desktop").map(|e| e.name.as_str()), Some("A (user)"));
        // nothing changed: no rebuild needed, same result
        idx.refresh_if_stale(Duration::from_secs(3600));
        assert_eq!(idx.get("a.desktop").map(|e| e.name.as_str()), Some("A (user)"));
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn entries_sharing_a_wm_class_resolve_the_same_way_every_time() {
        let d = std::env::temp_dir().join(format!("fractile-apps-{}", uuid::Uuid::new_v4()));
        let (user, sys) = (d.join("user"), d.join("sys"));
        std::fs::create_dir_all(&user).unwrap();
        std::fs::create_dir_all(&sys).unwrap();
        let entry = |name: &str, exec: &str| format!("[Desktop Entry]\nType=Application\nName={name}\nExec={exec}\nStartupWMClass=google-chrome\n");
        // Both names Chrome ships, in the system dir; several decoys so hash
        // order varies between builds.
        std::fs::write(sys.join("com.google.Chrome.desktop"), entry("Chrome", "/usr/bin/google-chrome-stable %U")).unwrap();
        std::fs::write(sys.join("google-chrome.desktop"), entry("Chrome", "/usr/bin/google-chrome-stable %U")).unwrap();
        for i in 0..20 {
            std::fs::write(sys.join(format!("decoy{i}.desktop")), format!("[Desktop Entry]\nType=Application\nName=D{i}\nExec=d{i}\n")).unwrap();
        }
        let w = || WindowInfo { wm_class: Some("Google-chrome".into()), wm_class_instance: Some("google-chrome".into()), ..Default::default() };
        for _ in 0..30 {
            let idx = AppIndex::build(&[user.clone(), sys.clone()]);
            let mut x = w();
            idx.enrich(&mut x);
            assert_eq!(x.desktop_file_id.as_deref(), Some("google-chrome.desktop"), "the id that is the class itself wins");
        }
        // The user's own entry (another id, same class) beats both system ones.
        std::fs::write(user.join("chrome-work.desktop"), entry("Chrome (work)", "/usr/bin/google-chrome-stable --profile-directory=Work")).unwrap();
        for _ in 0..30 {
            let idx = AppIndex::build(&[user.clone(), sys.clone()]);
            let mut x = w();
            idx.enrich(&mut x);
            assert_eq!(x.desktop_file_id.as_deref(), Some("chrome-work.desktop"));
        }
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn interpreters_never_identify_an_app() {
        let d = std::env::temp_dir().join(format!("fractile-apps-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("python3.12.desktop"), "[Desktop Entry]\nType=Application\nName=Python\nExec=/usr/bin/python3.12\nTerminal=true\n").unwrap();
        let idx = AppIndex::build(&[d.clone()]);
        let mut w = WindowInfo { app_id: Some("org.example.Unknown".into()), wm_class: Some("org.example.Unknown".into()), executable: Some("/usr/bin/python3.12".into()), ..Default::default() };
        idx.enrich(&mut w);
        assert_eq!(w.desktop_file_id, None, "python must not claim a python GUI app's window");
        assert!(is_generic_program("python3.12") && is_generic_program("bash") && !is_generic_program("firefox"));
        std::fs::remove_dir_all(d).unwrap();
    }
}

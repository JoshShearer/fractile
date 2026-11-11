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
}

impl AppIndex {
    pub fn build(dirs: &[PathBuf]) -> AppIndex {
        let mut idx = AppIndex { dirs: dirs.to_vec(), ..Default::default() };
        // Earlier dirs take precedence (XDG order).
        for d in dirs {
            scan(d, d, &mut idx.entries, 0);
        }
        idx.entries.retain(|_, e| e.kind == "Application" && !e.hidden);
        for (id, e) in &idx.entries {
            if let Some(c) = &e.startup_wm_class {
                idx.by_wm_class.entry(c.to_lowercase()).or_insert_with(|| id.clone());
            }
            if let Some(exec) = &e.exec {
                if let Ok(argv) = fractile_core::desktop::exec_to_argv(exec, &e.name, None) {
                    let prog = argv.iter().find(|a| !a.contains('=') && *a != "env").cloned().unwrap_or_else(|| argv[0].clone());
                    if let Some(name) = Path::new(&prog).file_name() {
                        idx.by_exec_name.entry(name.to_string_lossy().to_lowercase()).or_insert_with(|| id.clone());
                    }
                }
            }
        }
        idx.built_at = Some(Instant::now());
        idx
    }

    pub fn system() -> AppIndex {
        AppIndex::build(&application_dirs())
    }

    /// Rebuild if older than `max_age` (new apps installed).
    pub fn refresh_if_stale(&mut self, max_age: Duration) {
        if self.built_at.map(|t| t.elapsed() > max_age).unwrap_or(true) {
            let dirs = if self.dirs.is_empty() { application_dirs() } else { self.dirs.clone() };
            *self = AppIndex::build(&dirs);
        }
    }

    pub fn get(&self, id: &str) -> Option<&DesktopEntry> {
        self.entries.get(id).or_else(|| self.entries.get(&format!("{id}.desktop")))
    }

    fn find_ci(&self, stem: &str) -> Option<String> {
        let want = format!("{}.desktop", stem.to_lowercase());
        if self.entries.contains_key(&format!("{stem}.desktop")) {
            return Some(format!("{stem}.desktop"));
        }
        self.entries.keys().find(|k| k.to_lowercase() == want).cloned()
    }

    /// Fill desktop_file_id / executable / sandbox on a window.
    /// Evidence order (most to least deterministic):
    /// 1. systemd scope of the process (app-<launcher>-<id>-N.scope)
    /// 2. GIO_LAUNCHED_DESKTOP_FILE / BAMF_DESKTOP_FILE_HINT in the process env
    /// 3. flatpak / snap sandbox app id
    /// 4. app id (_GTK_APPLICATION_ID / Wayland app_id) == desktop id
    /// 5. StartupWMClass == WM_CLASS
    /// 6. desktop id == WM_CLASS (class or instance)
    /// 7. Exec program name == executable name
    pub fn enrich(&self, w: &mut WindowInfo) {
        if let Some(pid) = w.pid {
            let proc_dir = PathBuf::from(format!("/proc/{pid}"));
            if w.executable.is_none() {
                w.executable = std::fs::read_link(proc_dir.join("exe")).ok().map(|p| p.to_string_lossy().to_string());
            }
            if let Ok(cg) = std::fs::read_to_string(proc_dir.join("cgroup")) {
                let unit = cg.lines().filter_map(|l| l.rsplit('/').next()).find(|u| u.starts_with("app-")).map(|s| s.to_string());
                if let Some(unit) = unit {
                    if unit.starts_with("app-flatpak-") {
                        w.sandbox = Some("flatpak".into());
                    } else if unit.starts_with("snap.") || unit.contains("snap-") {
                        w.sandbox = Some("snap".into());
                    }
                    if w.desktop_file_id.is_none() {
                        for cand in app_ids_from_systemd_unit(&unit) {
                            if let Some(id) = self.find_ci(&cand) {
                                w.desktop_file_id = Some(id);
                                w.desktop_file_source = Some("systemd-scope".into());
                                break;
                            }
                        }
                    }
                }
            }
            if w.desktop_file_id.is_none() {
                if let Ok(env) = std::fs::read(proc_dir.join("environ")) {
                    for kv in env.split(|b| *b == 0) {
                        let s = String::from_utf8_lossy(kv);
                        for key in ["GIO_LAUNCHED_DESKTOP_FILE=", "BAMF_DESKTOP_FILE_HINT="] {
                            if let Some(path) = s.strip_prefix(key) {
                                let p = Path::new(path);
                                if let Some(name) = p.file_name() {
                                    let id = name.to_string_lossy().to_string();
                                    if self.entries.contains_key(&id) {
                                        w.desktop_file_id = Some(id);
                                        w.desktop_file_source = Some("launch-env".into());
                                    }
                                }
                            }
                        }
                        if w.desktop_file_id.is_some() {
                            break;
                        }
                    }
                }
            }
            if w.sandbox.is_none() && proc_dir.join("root/.flatpak-info").exists() {
                w.sandbox = Some("flatpak".into());
            }
        }
        if w.desktop_file_id.is_some() {
            return;
        }
        let mut set = |id: Option<String>, src: &str| {
            if w.desktop_file_id.is_none() {
                if let Some(id) = id {
                    w.desktop_file_id = Some(id);
                    w.desktop_file_source = Some(src.into());
                }
            }
        };
        set(w.app_id.as_deref().and_then(|a| self.find_ci(a)), "app-id");
        if let Some(c) = w.wm_class.clone() {
            set(self.by_wm_class.get(&c.to_lowercase()).cloned(), "startup-wm-class");
        }
        if let Some(c) = w.wm_class_instance.clone() {
            set(self.by_wm_class.get(&c.to_lowercase()).cloned(), "startup-wm-class");
        }
        set(w.wm_class.as_deref().and_then(|c| self.find_ci(c)), "wm-class-name");
        set(w.wm_class_instance.as_deref().and_then(|c| self.find_ci(c)), "wm-class-name");
        let exe_name = w.executable.as_deref().and_then(|e| Path::new(e).file_name()).map(|n| n.to_string_lossy().to_lowercase());
        set(exe_name.and_then(|n| self.by_exec_name.get(&n).cloned()), "exec-name");
    }
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
}

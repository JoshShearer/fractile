//! Small file logger with rotation and an in-memory ring buffer.
//! Convention: window titles are always logged as `title="..."` so that
//! `logs --redacted` can scrub them reliably.

use std::collections::VecDeque;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Debug,
    Info,
    Warn,
    Error,
}

pub struct Logger {
    path: PathBuf,
    max_bytes: u64,
    level: Level,
    ring: Mutex<VecDeque<String>>,
    file: Mutex<Option<std::fs::File>>,
    pub also_stderr: bool,
}

impl Logger {
    pub fn new(path: PathBuf, max_kb: u64, level: &str, also_stderr: bool) -> Logger {
        if let Some(d) = path.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let level = match level {
            "debug" => Level::Debug,
            "warn" => Level::Warn,
            "error" => Level::Error,
            _ => Level::Info,
        };
        let file = std::fs::OpenOptions::new().create(true).append(true).open(&path).ok();
        Logger { path, max_bytes: max_kb * 1024, level, ring: Mutex::new(VecDeque::with_capacity(2000)), file: Mutex::new(file), also_stderr }
    }

    pub fn log(&self, level: Level, msg: &str) {
        if level < self.level {
            return;
        }
        let line = format!("{} {:?} {}", chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%.3f"), level, msg);
        if self.also_stderr {
            eprintln!("{line}");
        }
        {
            let mut r = self.ring.lock().unwrap();
            if r.len() >= 2000 {
                r.pop_front();
            }
            r.push_back(line.clone());
        }
        let mut f = self.file.lock().unwrap();
        if let Some(file) = f.as_mut() {
            let _ = writeln!(file, "{line}");
            if file.metadata().map(|m| m.len() > self.max_bytes).unwrap_or(false) {
                let _ = std::fs::rename(&self.path, self.path.with_extension("log.1"));
                *f = std::fs::OpenOptions::new().create(true).append(true).open(&self.path).ok();
            }
        }
    }

    pub fn recent(&self, n: usize, redacted: bool) -> Vec<String> {
        let r = self.ring.lock().unwrap();
        let re = regex::Regex::new(r#"title="(?:[^"\\]|\\.)*""#).unwrap();
        r.iter().rev().take(n).rev().map(|l| if redacted { re.replace_all(l, "title=\"[redacted]\"").to_string() } else { l.clone() }).collect()
    }
}

#[macro_export]
macro_rules! info { ($l:expr, $($t:tt)*) => { $l.log($crate::logging::Level::Info, &format!($($t)*)) } }
#[macro_export]
macro_rules! warn { ($l:expr, $($t:tt)*) => { $l.log($crate::logging::Level::Warn, &format!($($t)*)) } }
#[macro_export]
macro_rules! error { ($l:expr, $($t:tt)*) => { $l.log($crate::logging::Level::Error, &format!($($t)*)) } }
#[macro_export]
macro_rules! debug { ($l:expr, $($t:tt)*) => { $l.log($crate::logging::Level::Debug, &format!($($t)*)) } }

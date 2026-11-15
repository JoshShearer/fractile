//! Fractile core: platform-independent logic. No I/O beyond config files.

pub mod accel;
pub mod adapter;
pub mod config;
pub mod desktop;
pub mod geometry;
pub mod identity;
pub mod layout;
pub mod lockers;
pub mod output;
pub mod rules;
pub mod snapshot;
pub mod window;

pub const PRODUCT_NAME: &str = "Fractile";
pub const APP_ID: &str = "io.fractile.Fractile";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Standard XDG paths for Fractile.
pub mod paths {
    use std::path::PathBuf;

    fn home() -> PathBuf {
        std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
    }
    fn xdg(var: &str, fallback: &str) -> PathBuf {
        std::env::var_os(var).filter(|v| !v.is_empty()).map(PathBuf::from).unwrap_or_else(|| home().join(fallback))
    }
    pub fn config_dir() -> PathBuf {
        xdg("XDG_CONFIG_HOME", ".config").join("fractile")
    }
    pub fn config_file() -> PathBuf {
        std::env::var_os("FRACTILE_CONFIG").map(PathBuf::from).unwrap_or_else(|| config_dir().join("config.toml"))
    }
    pub fn data_dir() -> PathBuf {
        std::env::var_os("FRACTILE_DATA_DIR").map(PathBuf::from).unwrap_or_else(|| xdg("XDG_DATA_HOME", ".local/share").join("fractile"))
    }
    pub fn db_file() -> PathBuf {
        data_dir().join("fractile.db")
    }
    pub fn state_dir() -> PathBuf {
        std::env::var_os("FRACTILE_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| xdg("XDG_STATE_HOME", ".local/state").join("fractile"))
    }
    pub fn log_file() -> PathBuf {
        state_dir().join("fractile.log")
    }
    /// Private runtime dir (0700) holding the IPC sockets.
    pub fn runtime_dir() -> PathBuf {
        if let Some(v) = std::env::var_os("FRACTILE_RUNTIME_DIR") {
            return PathBuf::from(v);
        }
        match std::env::var_os("XDG_RUNTIME_DIR") {
            Some(v) if !v.is_empty() => PathBuf::from(v).join("fractile"),
            _ => std::env::temp_dir().join(format!("fractile-{}", unsafe_uid())),
        }
    }
    pub fn socket_path() -> PathBuf {
        runtime_dir().join("fractile.sock")
    }
    pub fn bridge_socket_path() -> PathBuf {
        runtime_dir().join("bridge.sock")
    }
    pub fn lock_path() -> PathBuf {
        runtime_dir().join("daemon.lock")
    }
    fn unsafe_uid() -> u32 {
        // getuid via /proc to avoid a libc dependency in core
        std::fs::read_to_string("/proc/self/status")
            .ok()
            .and_then(|s| s.lines().find(|l| l.starts_with("Uid:")).and_then(|l| l.split_whitespace().nth(1)).and_then(|v| v.parse().ok()))
            .unwrap_or(0)
    }
}

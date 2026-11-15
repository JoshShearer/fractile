//! Screen lockers of compositors that tell logind nothing about a lock.
//!
//! swaylock, gtklock, waylock and hyprlock lock a wlroots or Hyprland session
//! through the ext-session-lock protocol; no Wayland client can ask the
//! compositor whether a lock is active, and they do not set logind's
//! LockedHint (seen on Ubuntu 26.04 with Sway 1.11: `loginctl lock-session`
//! starts swaylock through swayidle, LockedHint stays "no"). Each one runs
//! exactly while the screen is locked and exits on unlock, so a running locker
//! of this user is the lock state.

use std::os::unix::fs::MetadataExt;
use std::path::Path;

pub const WLROOTS_LOCKERS: &[&str] = &["swaylock", "gtklock", "waylock"];
pub const HYPRLAND_LOCKERS: &[&str] = &["hyprlock", "swaylock", "gtklock", "waylock"];

/// A process of `uid` whose name (`/proc/<pid>/comm`) is one of `names`.
pub fn locker_running(proc_root: &Path, uid: u32, names: &[&str]) -> bool {
    let Ok(rd) = std::fs::read_dir(proc_root) else { return false };
    rd.flatten().any(|e| {
        let name = e.file_name();
        let Some(s) = name.to_str() else { return false };
        if !s.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        if e.metadata().map(|m| m.uid() != uid).unwrap_or(true) {
            return false;
        }
        std::fs::read_to_string(e.path().join("comm")).map(|c| names.contains(&c.trim_end())).unwrap_or(false)
    })
}

/// The current user's session is locked by one of `names`.
pub fn locked_by(names: &[&str]) -> bool {
    // SAFETY: getuid has no preconditions and cannot fail.
    let uid = unsafe { libc_getuid() };
    locker_running(Path::new("/proc"), uid, names)
}

extern "C" {
    #[link_name = "getuid"]
    fn libc_getuid() -> u32;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_only_named_processes_of_the_user() {
        let d = std::env::temp_dir().join(format!("fractile-lockers-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        for (pid, comm) in [("100", "swaybar\n"), ("101", "foot\n"), ("self-not-a-pid", "swaylock\n")] {
            std::fs::create_dir_all(d.join(pid)).unwrap();
            std::fs::write(d.join(pid).join("comm"), comm).unwrap();
        }
        let uid = std::fs::metadata(&d).unwrap().uid();
        assert!(!locker_running(&d, uid, WLROOTS_LOCKERS));
        std::fs::create_dir_all(d.join("102")).unwrap();
        std::fs::write(d.join("102").join("comm"), "swaylock\n").unwrap();
        assert!(locker_running(&d, uid, WLROOTS_LOCKERS));
        assert!(!locker_running(&d, uid.wrapping_add(1), WLROOTS_LOCKERS), "another user's locker does not count");
        assert!(!locker_running(&d, uid, &["hyprlock"]));
        let _ = std::fs::remove_dir_all(&d);
    }
}

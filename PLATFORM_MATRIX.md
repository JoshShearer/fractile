# Platform matrix

Fractile 0.9.0, adapter API 1, IPC protocol 1. Verified 2026-11-11. The tester is
the automated suites in `tests/`, run by the Claude Code agent on the
maintainer's workstation. Logs and JSON evidence are in `tests/evidence/`.

Statuses: VERIFIED / PARTIAL / BLOCKED BY PLATFORM / NOT IMPLEMENTED / NOT
TESTED. Per-requirement detail is in [REQUIREMENTS.md](REQUIREMENTS.md).

## Tested environments

| # | OS (host or image) | Desktop / compositor | Session | How it ran | Adapter |
|---|---|---|---|---|---|
| E1 | Pop!_OS 24.04 LTS host, kernel 7.2.6, NVIDIA RTX 3080 driver 595.104.02 | GNOME Shell 46.0 / Mutter | X11 | nested in Xephyr 1600x1200, real Mutter as WM, isolated D-Bus | x11 |
| E2 | Pop!_OS 24.04 LTS host | GNOME Shell 46.0 / Mutter | Wayland | `gnome-shell --headless --wayland --no-x11`, 1 virtual monitor 1600x1200 | gnome (extension) |
| E3 | Pop!_OS 24.04 LTS host | GNOME Shell 46.0 / Mutter | Wayland | headless, 2 virtual monitors 1600x1200 + 1280x1024 (scale 1) | gnome (extension) |
| E4 | archlinux:latest container on the host kernel, host GPU render node | KWin 6.8.2 (Qt 6.12.0) | Wayland | `kwin_wayland --virtual`, no Xwayland, no plasmashell | kwin (script + KGlobalAccel) |
| E5 | Ubuntu 24.04.5 LTS container | Openbox on Xvfb | X11 | package install/remove smoke | x11 |
| E6 | archlinux:latest container | Openbox on Xvfb | X11 | makepkg + install/remove smoke | x11 |
| E7 | Ubuntu 24.04 container | kwin_x11 4:5.27.11-0ubuntu3 (Plasma 5.27, as in Kubuntu 24.04) | X11 | Xvfb, kwin_x11 as WM, no plasmashell | x11 |
| E8 | Fedora 39 / 41 / 42 containers | GNOME Shell 45.10 / 47.10 / 48.8 | Wayland | headless, same suites as E2/E3 plus display changes, Settings and tray | gnome (extension) |
| E9 | Pop!_OS 24.04 host | GNOME Shell 46.0 | Wayland | headless, monitors changed at runtime through Mutter DisplayConfig: scale 2 + scale 1, disable/enable | gnome (extension) |
| E10 | Ubuntu 24.04.5 LTS desktop (ubuntu-desktop-minimal) in a QEMU/KVM VM, kernel 6.8.0-149 | GNOME Shell 46.0 / Mutter | Wayland | real GDM logins, real log-out and reboot, `apt install` of the .deb, keys typed on the VM's virtual keyboard (`tests/vm/vm-e2e.sh wayland`, `x11`, `plasma-x11`); GNOME Wayland and Xorg sessions, and a Plasma 5.27 X11 session (kde-plasma-desktop) | gnome (extension) / x11 |
| E11 | archlinux:latest container on the host kernel, host GPU render node | Hyprland 0.57.2 | Wayland | nested inside `kwin_wayland --virtual` (KWin is only the parent display), native Wayland GTK4/Qt clients, keys pressed with wtype; no login, no bar, no uwsm | hyprland (IPC) |

Host UI stack: Python 3.12.3, Qt 6.4.2 / PyQt6 6.6.1 (Ubuntu packages). Rust 1.99.1.

## Capability by desktop

| Capability | GNOME X11 (E1/E10) | GNOME Wayland (E2/E3/E8/E9/E10) | KDE Plasma Wayland (E4) | KDE Plasma X11 (E7/E10) | COSMIC (Pop!_OS 24.04) | Hyprland 0.57 (E11) | Other Wayland (Sway, ...) |
|---|---|---|---|---|---|---|---|
| Window list / active window | VERIFIED | VERIFIED | VERIFIED | VERIFIED | BLOCKED BY PLATFORM | VERIFIED | NOT IMPLEMENTED (reported unsupported) |
| Move / resize to grid | VERIFIED | VERIFIED | VERIFIED | VERIFIED | BLOCKED BY PLATFORM | VERIFIED (the placed window becomes floating) | NOT IMPLEMENTED |
| Global shortcuts | VERIFIED (XGrabKey) | VERIFIED (extension grab) | VERIFIED (KGlobalAccel) | VERIFIED (XGrabKey) | BLOCKED BY PLATFORM | VERIFIED (runtime Hyprland binds; re-added after a config reload) | NOT IMPLEMENTED |
| Panel UI + keyboard/mouse | VERIFIED | VERIFIED (keyboard; drag not run) | PARTIAL (shown and focused; placement from the panel not run) | NOT TESTED | BLOCKED BY PLATFORM | VERIFIED (keyboard; drag not run) | NOT IMPLEMENTED |
| Live preview overlay | VERIFIED | NOT TESTED | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED (Hyprland has no IPC to draw one) | NOT IMPLEMENTED |
| Multi-monitor | NOT TESTED | VERIFIED (sizes; mixed DPI scale 2 + 1; disconnect and reconnect through DisplayConfig, E9/E8) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | PARTIAL (a second headless output added at runtime; a window moved onto it; scale 1 only) | NOT IMPLEMENTED |
| Workspaces | NOT TESTED (EWMH desktops implemented) | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM | VERIFIED | NOT IMPLEMENTED |
| Snapshot save | VERIFIED | VERIFIED | VERIFIED | VERIFIED | BLOCKED BY PLATFORM (no geometry to read back for restore) | VERIFIED | NOT IMPLEMENTED |
| Snapshot restore (same session) | VERIFIED | VERIFIED | VERIFIED | VERIFIED | BLOCKED BY PLATFORM | VERIFIED | NOT IMPLEMENTED |
| Restore after relogin | VERIFIED (E10 X11 session: real log-out and log-in, 15 windows of 10 apps, V06) | VERIFIED (E10: real log-out and log-in, 15 windows of 10 apps restored automatically, V06) | NOT TESTED | VERIFIED (E10 Plasma X11 session: real log-out and log-in, 15 windows of 10 apps, V06) | BLOCKED BY PLATFORM | NOT TESTED | NOT IMPLEMENTED |
| Restore after reboot | VERIFIED (E10 X11 session: real reboot, 15 windows of 10 apps, V05) | VERIFIED (E10: real reboot, the prompt answered with Enter, 15 windows of 10 apps restored, V05) | NOT TESTED | VERIFIED (E10 Plasma X11 session: real reboot, 15 windows of 10 apps, V05) | BLOCKED BY PLATFORM | NOT TESTED | NOT IMPLEMENTED |
| App launch on restore (approved) | VERIFIED | VERIFIED (E10: 10 apps reopened at login, V05/V06) | NOT TESTED | VERIFIED | BLOCKED BY PLATFORM | NOT TESTED | NOT IMPLEMENTED |
| Session lock handling | NOT TESTED | VERIFIED (logind LockedHint) | NOT TESTED | NOT TESTED | n/a | NOT TESTED | n/a |
| Integration missing / mismatched reported | n/a | VERIFIED (W16, W23) | VERIFIED (K00, K14) | n/a | VERIFIED (`--check` reports unsupported with the reason) | VERIFIED (H23) | VERIFIED (X24) |
| Tray icon | NOT TESTED | VERIFIED (with the AppIndicator extension; stock GNOME has no tray) | NOT TESTED | NOT TESTED | n/a | NOT TESTED | n/a |

**Divvy-equivalent (SRS 16):** not claimed on any platform. GNOME X11 is the
closest; its open items are DV-12/13/17 multi-monitor rows (tested on GNOME
Wayland only), the tray (DV-19), and the PARTIAL rows in REQUIREMENTS.md.

**Session restore supported (SRS 16):** not claimed on any platform. GNOME
on Ubuntu 24.04 (Wayland and X11) and Plasma 5.27 X11 have passed a real reboot
and a real log-out (E10). An
unexpected interruption (power loss) and monitor remapping have not been run
for real. Simulated relogins and simulated crash-resume pass on GNOME X11.

## Packages

| Package | Built on | Clean install | Remove | Reinstall keeps data | purge-data | Evidence |
|---|---|---|---|---|---|---|
| `fractile_0.9.0-1_amd64.deb` | Pop!_OS 24.04 host | VERIFIED (Ubuntu 24.04.5 container) | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-deb-ubuntu24.04.log` |
| `fractile-0.9.0-1-x86_64.pkg.tar.zst` | archlinux:latest container (makepkg) | VERIFIED | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-arch.log` |
| Pop!_OS 24.04 host install | n/a | NOT TESTED (no root on the test host) | | | | |

## Integration versions

| Integration | Declares | Tested |
|---|---|---|
| GNOME Shell extension `fractile@fractile.io` 0.9.0 | shell-version 45, 46, 47, 48 | 45.10, 46.0, 47.10, 48.8 |
| KWin script `fractile` 0.9.0 | X-Plasma-API-Minimum-Version 6.0 | KWin 6.8.2 only |
| Hyprland adapter (built in, Hyprland's IPC) | Hyprland 0.57 or later (Lua dispatch) | Hyprland 0.57.2 only |

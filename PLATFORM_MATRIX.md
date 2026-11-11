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

Host UI stack: Python 3.12.3, Qt 6.4.2 / PyQt6 6.6.1 (Ubuntu packages). Rust 1.99.1.

## Capability by desktop

| Capability | GNOME X11 (E1) | GNOME Wayland (E2/E3) | KDE Plasma Wayland (E4) | KDE Plasma X11 | COSMIC (Pop!_OS 24.04) | Other Wayland (Sway, Hyprland, ...) |
|---|---|---|---|---|---|---|
| Window list / active window | VERIFIED | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED (reported unsupported) |
| Move / resize to grid | VERIFIED | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Global shortcuts | VERIFIED (XGrabKey) | VERIFIED (extension grab) | VERIFIED (KGlobalAccel) | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Panel UI + keyboard/mouse | VERIFIED | VERIFIED (keyboard; drag not run) | PARTIAL (shown and focused; placement from the panel not run) | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Live preview overlay | VERIFIED | NOT TESTED | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Multi-monitor | NOT TESTED | VERIFIED (sizes; mixed DPI NOT TESTED) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Workspaces | NOT TESTED (EWMH desktops implemented) | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Snapshot save | VERIFIED | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM (no geometry to read back for restore) | NOT IMPLEMENTED |
| Snapshot restore (same session) | VERIFIED | VERIFIED | VERIFIED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Restore after relogin | PARTIAL (simulated relogins, X27/X28/P08) | NOT TESTED | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Restore after reboot | NOT TESTED | NOT TESTED | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| App launch on restore (approved) | VERIFIED | NOT TESTED | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM | NOT IMPLEMENTED |
| Session lock handling | NOT TESTED | VERIFIED (logind LockedHint) | NOT TESTED | NOT TESTED | n/a | n/a |
| Integration missing / mismatched reported | n/a | VERIFIED (W16, W23) | VERIFIED (K00, K14) | NOT TESTED | VERIFIED (`--check` reports unsupported with the reason) | VERIFIED (X24) |
| Tray icon | NOT TESTED | NOT TESTED | NOT TESTED | NOT TESTED | n/a | n/a |

**Divvy-equivalent (SRS 16):** not claimed on any platform. GNOME X11 is the
closest; its open items are DV-12/13/17 multi-monitor rows (tested on GNOME
Wayland only), the tray (DV-19), and the PARTIAL rows in REQUIREMENTS.md.

**Session restore supported (SRS 16):** not claimed on any platform. That needs
a clean reboot, an unexpected interruption and monitor remapping, run for real.
Simulated relogins and simulated crash-resume pass on GNOME X11.

## Packages

| Package | Built on | Clean install | Remove | Reinstall keeps data | purge-data | Evidence |
|---|---|---|---|---|---|---|
| `fractile_0.9.0-1_amd64.deb` | Pop!_OS 24.04 host | VERIFIED (Ubuntu 24.04.5 container) | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-deb-ubuntu24.04.log` |
| `fractile-0.9.0-1-x86_64.pkg.tar.zst` | archlinux:latest container (makepkg) | VERIFIED | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-arch.log` |
| Pop!_OS 24.04 host install | n/a | NOT TESTED (no root on the test host) | | | | |

## Integration versions

| Integration | Declares | Tested |
|---|---|---|
| GNOME Shell extension `fractile@fractile.io` 0.9.0 | shell-version 45, 46, 47, 48 | 46.0 only |
| KWin script `fractile` 0.9.0 | X-Plasma-API-Minimum-Version 6.0 | KWin 6.8.2 only |

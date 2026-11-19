# Platform matrix

Fractile 0.9.0, adapter API 1, IPC protocol 1. Verified 2026-11-11, updated 2026-11-15. The tester is
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
| E12 | archlinux:latest container on the host kernel | Sway 1.12 | Wayland | nested inside `kwin_wayland --virtual` (wlroots Wayland backend, pixman renderer), native Wayland GTK4/Qt clients, keys pressed with wtype; no login, no bar | sway (IPC) |
| E13 | Ubuntu 26.04.1 LTS desktop with kde-plasma-desktop in a QEMU/KVM VM, kernel 7.0.0-43 | Plasma 6.6.6 / KWin 6.6.6 | Wayland | real GDM logins into the Plasma Wayland session, real log-out (Plasma's) and reboot, `apt install` of the .deb, keys typed on the VM's virtual keyboard (`tests/vm/vm-e2e.sh plasma-wayland`) | kwin (script) |
| E15 | Arch Linux (official cloud image, `pacman -Syu` at provisioning, kernel 7.3.4-arch1-2) in a QEMU/KVM VM, llvmpipe rendering | Hyprland 0.57.2 (waybar, hyprlock through hypridle) | Wayland | real GDM 51 timed logins into Arch's Hyprland session (no uwsm; it reaches graphical-session.target), real log-out (`hl.dsp.exit()`) and reboot, `pacman -U` of the Arch package, keys typed on the VM's virtual keyboard (`tests/vm/vm-e2e.sh hyprland`) | hyprland (IPC) |
| E16 | Arch Linux (official cloud image, `pacman -Syu` at provisioning, kernel 7.3.4-arch1-2) in a QEMU/KVM VM, llvmpipe rendering | COSMIC 1.11.0 (cosmic-session, cosmic-comp 1:1.11.0-1), auto-tiling on | Wayland | real GDM 51 timed logins into the COSMIC session (graphical-session.target active), real reboot, `pacman -U` of the Arch package (`tests/vm/vm-e2e.sh cosmic`) | cosmic (toplevel info/manager and ext-workspace; no geometry) |
| E14 | Ubuntu 26.04.1 LTS desktop with sway, swayidle and swaylock in a QEMU/KVM VM, kernel 7.0.0-43 | Sway 1.11 (swaybar) | Wayland | real GDM logins into the Sway session, real log-out (`swaymsg exit`) and reboot, `apt install` of the .deb, keys typed on the VM's virtual keyboard (`tests/vm/vm-e2e.sh sway`) | sway (IPC) |

Host UI stack: Python 3.12.3, Qt 6.4.2 / PyQt6 6.6.1 (Ubuntu packages). Rust 1.99.1.

## Capability by desktop

| Capability | GNOME X11 (E1/E10) | GNOME Wayland (E2/E3/E8/E9/E10) | KDE Plasma Wayland (E4/E13) | KDE Plasma X11 (E7/E10) | COSMIC (Pop!_OS 24.04; Arch cosmic-comp 1.11.0, E16) | Hyprland 0.57 (E11, E15) | Sway 1.12 (E12), 1.11 (E14) | Other Wayland (river, Wayfire, ...) |
|---|---|---|---|---|---|---|---|---|
| Window list / active window | VERIFIED | VERIFIED | VERIFIED | VERIFIED | VERIFIED (E16: the COSMIC adapter lists windows with app, workspace, state and the position COSMIC reports; C03) | VERIFIED | VERIFIED (S02, S08) | NOT IMPLEMENTED (reported unsupported) |
| Move / resize to grid | VERIFIED | VERIFIED | VERIFIED | VERIFIED | BLOCKED BY PLATFORM (no client API sets a window's position or size; refused with the reason, nothing moved: C04) | VERIFIED (the placed window becomes floating) | VERIFIED (the placed window becomes floating; S04-S07) | NOT IMPLEMENTED |
| Global shortcuts | VERIFIED (XGrabKey) | VERIFIED (extension grab) | VERIFIED (KGlobalAccel) | VERIFIED (XGrabKey) | BLOCKED BY PLATFORM (no client API for global shortcuts; reported unsupported) | VERIFIED (runtime Hyprland binds; re-added after a config reload) | VERIFIED (runtime `bindsym`; re-added after a config reload; keys the Sway configuration binds are reported as conflicts; S10, S11, S16) | NOT IMPLEMENTED |
| Panel UI + keyboard/mouse | VERIFIED | VERIFIED (keyboard and drag, Esc, grid keys, Shift, Alt; PW01-PW09 on 45/47/48; real GNOME 46 login in E10) | VERIFIED (real Plasma 6.6 login in E13: shortcut, drag, keys, tray, P suite 38/38) | VERIFIED (real Plasma 5.27 X11 login in E10: P suite 38/38) | BLOCKED BY PLATFORM (opening the panel shows that windows cannot be placed here and why: C08 in E16) | VERIFIED (keyboard; drag not run) | VERIFIED (keyboard; drag not run; S14, S22) | NOT IMPLEMENTED |
| Live preview overlay | VERIFIED | VERIFIED (the extension draws it at the exact target, takes no input, gone after the drop; PW02, PW02b) | VERIFIED (the UI draws it, the KWin script places it on the exact target; P07 in E13, KM04 on two outputs) | VERIFIED (override-redirect window at the exact target; P07 in E10) | BLOCKED BY PLATFORM | NOT IMPLEMENTED (Hyprland has no IPC to draw one) | NOT IMPLEMENTED (Sway has no IPC to draw one) | NOT IMPLEMENTED |
| Multi-monitor | VERIFIED (GNOME Shell 46 X11 with two RandR monitors: G22, G23, G25; and by hand on two physical monitors) | VERIFIED (sizes; mixed DPI scale 2 + 1; disconnect and reconnect through DisplayConfig, E9/E8; a panel per monitor and a drag on the second, PM02-PM04) | VERIFIED (two virtual outputs: a panel per output, exact drag and preview on the second, shortcut cycles a, b, a; KM00-KM06; and in a real Plasma 6.6 login in E13 with a 1600x1200 monitor at scale 1 beside a 5120x2160 one at scale 2.7: a panel on each with the keyboard on the window's monitor, exact drag and preview on the scaled one, shortcut cycling, margins and gaps, and the scaled monitor turned to portrait; P05b, P05c, P20-P24) | VERIFIED (kwin_x11 5.27, two RandR monitors: G22, G23, G25) | NOT TESTED (one virtual monitor in E16; placement on any monitor is blocked) | VERIFIED nested (a second headless output added at runtime, a window moved onto it, H17; that output at scale 1.5 next to one at scale 1, listed at its logical 1280x800 and a grid cell on it placed with 0 px error, H25); physical monitors not tested | VERIFIED nested (a second output added at runtime, a window moved onto it, S17; that output at scale 1.5 next to one at scale 1, listed at its logical size and a grid cell on it placed with 0 px error, S25); physical monitors not tested | NOT IMPLEMENTED |
| Workspaces | VERIFIED (EWMH desktops, X37 under GNOME Shell 46 X11) | VERIFIED | VERIFIED | VERIFIED (X37 under kwin_x11 5.27) | VERIFIED (E16: listed, and a window moved to the second workspace on request; C03, C09) | VERIFIED | VERIFIED (S12) | NOT IMPLEMENTED |
| Snapshot save | VERIFIED | VERIFIED | VERIFIED | VERIFIED | PARTIAL (E16: app, workspace and state recorded; positions recorded but cannot be restored; C09) | VERIFIED | VERIFIED (S13, S21) | NOT IMPLEMENTED |
| Snapshot restore (same session) | VERIFIED | VERIFIED | VERIFIED | VERIFIED | PARTIAL (E16: same snapshot restored again, nothing reopened twice, reported RESTORED_WITHOUT_GEOMETRY; C11) | VERIFIED | VERIFIED (S13) | NOT IMPLEMENTED |
| Restore after relogin | VERIFIED (E10 X11 session: real log-out and log-in, 15 windows of 10 apps, V06) | VERIFIED (E10: real log-out and log-in, 15 windows of 10 apps restored automatically, V06) | VERIFIED (E13: real Plasma log-out and log-in, 15 windows of 10 apps restored automatically; Plasma reopened 9, Fractile launched 6; V06) | VERIFIED (E10 Plasma X11 session: real log-out and log-in, 15 windows of 10 apps, V06) | NOT TESTED (the reboot path runs the same login restore) | VERIFIED (E15: real log-out and GDM log-in, 15 windows of 10 apps restored automatically, all launched by Fractile, one Logout snapshot; V06) | VERIFIED (E14: real log-out and GDM log-in, 15 windows of 10 apps restored automatically, all launched by Fractile; V06) | NOT IMPLEMENTED |
| Restore after reboot | VERIFIED (E10 X11 session: real reboot, 15 windows of 10 apps, V05) | VERIFIED (E10: real reboot, the prompt answered with Enter, 15 windows of 10 apps restored, V05) | VERIFIED (E13: real reboot, the prompt answered with Enter, 15 windows of 10 apps restored, V05) | VERIFIED (E10 Plasma X11 session: real reboot, 15 windows of 10 apps, V05) | PARTIAL (E16: real reboot; the approved editor reopened once, on its workspace, maximized; position and size not restored and reported so; C10) | VERIFIED (E15: real reboot, the prompt answered with Enter, 15 windows of 10 apps restored; V05) | VERIFIED (E14: real reboot, the prompt answered with Enter, 15 windows of 10 apps restored; V05) | NOT IMPLEMENTED |
| App launch on restore (approved) | VERIFIED | VERIFIED (E10: 10 apps reopened at login, V05/V06) | VERIFIED (E13: apps reopened at login, V05/V06) | VERIFIED | VERIFIED (E16: the approved editor reopened once at login; C10, C11) | VERIFIED (E15: 10 apps started from their Exec line in their own scopes, without restrictions from the service; V05, V06, V09) | VERIFIED (E14: 10 apps started from their Exec line, V05/V06; gnome-terminal later resizes itself on focus changes, see docs/compatibility/sway.md) | NOT IMPLEMENTED |
| Session lock handling | VERIFIED (E10 GNOME Xorg: real lock screen, V07) | VERIFIED (E10: real lock screen, V07; logind LockedHint, W21) | VERIFIED (E13: real Plasma lock screen, V07) | VERIFIED (E10 Plasma X11: real lock screen, V07) | NOT TESTED (COSMIC's lock screen not run; Fractile relies on logind's LockedHint there) | VERIFIED (E15: hyprlock through hypridle, detected as a running locker since it sets no LockedHint; restore refused while locked; V07) | VERIFIED (E14: swaylock through swayidle, detected as a running locker since it sets no LockedHint; restore refused while locked; V07) | n/a |
| Integration missing / mismatched reported | n/a | VERIFIED (W16, W23) | VERIFIED (K00, K14) | n/a | VERIFIED (E16: status, diagnose, the wizard and the panel say window placement is not available and why; C02, C04, C08) | VERIFIED (H23) | VERIFIED (S23) | VERIFIED (X24) |
| Tray icon | VERIFIED (E10 GNOME Xorg with Ubuntu's AppIndicator extension: P18, P19) | VERIFIED (with the AppIndicator extension, headless Y suite and the E10 real login P18, P19; stock GNOME has no tray) | VERIFIED (Plasma's StatusNotifierWatcher, click shows the panel; P18, P19 in E13) | VERIFIED (P18, P19 in E10, Plasma 5.27 X11) | VERIFIED (E16: COSMIC's status area, Active; C06) | VERIFIED (E15: waybar's StatusNotifierWatcher, Active, a click opens the panel; V08) | VERIFIED (E14: swaybar's StatusNotifierWatcher, Active, a click opens the panel; V08) | n/a |

**Divvy-equivalent (SRS 16):** claimed for one precise platform, **KDE Plasma
6.6.6 / KWin 6.6.6 Wayland on Ubuntu 26.04.1 LTS (E13)**: DV-01..28 each
VERIFIED there in real logins (P and PT suites), placement errors visible and
recoverable, apt install and upgrade checked there (SRS_STATUS.md, section
16). Not claimed for GNOME X11 or GNOME Wayland, whose columns combine several
versions and setups.

**Session restore supported (SRS 16):** claimed on KDE Plasma 6.6.6
Wayland (KWin 6.6.6), Ubuntu 26.04.1 LTS (E13) only, where all five tests SRS
16 names pass with the final build: real reboot and log-out (V05, V06), two
power cuts during snapshot writes (PC00-PC03), duplicate avoidance (V05/V06,
P28, R01d-R03d), monitor remapping with the second monitor turned off and on
(P29-P33) and mixed apps: Qt, GTK 3 and GTK 4 apps across two monitors (P27)
and Firefox 160 (snap), Chrome 157 and VS Code 1.146 (R01-R03). Qualified as
in SRS_STATUS.md section 16 (window frames only; Chrome's windows matched by
order; KWin moves windows back itself on reconnect; virtual monitors). Not
claimed elsewhere:

- GNOME Xorg (E10) has passed four of them: a real reboot and log-out (V05,
  V06), a simulated power loss during snapshot writes (PC01-PC03, then all 15
  windows restored), duplicate avoidance (X19, X21, X27) and mixed real apps
  (A01-A03: Firefox, Chrome and VS Code). The last two ran on nested GNOME 46
  X11 (E1), not in the VM. Its monitor remapping was not run on X11.
- GNOME Wayland has passed monitor remapping (D04-D09), a real reboot and
  log-out, and duplicate avoidance. The power-loss run was on the Xorg
  session only.
- Plasma 5.27 X11, Sway 1.11 and Hyprland 0.57.2 have passed a real reboot
  and a real log-out (E10, E14, E15).
- COSMIC 1.11.0 restores without positions or sizes (apps, workspaces and
  maximized state after a real reboot, E16); it cannot qualify, since
  placement is blocked there.
- A power cut on real hardware and monitor remapping with physical monitors
  have not been run.

## Packages

| Package | Built on | Clean install | Remove | Reinstall keeps data | purge-data | Evidence |
|---|---|---|---|---|---|---|
| `fractile_0.9.0-1_amd64.deb` | Pop!_OS 24.04 host | VERIFIED (Ubuntu 24.04.5 container) | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-deb-ubuntu24.04.log` |
| `fractile-0.9.0-1-x86_64.pkg.tar.zst` | archlinux:latest container (makepkg) | VERIFIED | VERIFIED | VERIFIED | VERIFIED | `tests/evidence/package-arch.log` |
| Pop!_OS 24.04 host install | VERIFIED by hand (`apt install` of the .deb on the developer's Pop!_OS 24.04 desktop, GNOME 46 on Xorg, two physical monitors 2560x1440 + 3440x1440; service, UI, a panel shortcut and placement on both monitors, judged by the user) | NOT TESTED (that desktop runs the Xorg session) | | | | |

## Integration versions

| Integration | Declares | Tested |
|---|---|---|
| GNOME Shell extension `fractile@fractile.io` 0.9.0 | shell-version 45, 46, 47, 48 | 45.10, 46.0, 47.10, 48.8 |
| KWin script `fractile` 0.9.0 | X-Plasma-API-Minimum-Version 6.0 | KWin 6.8.2 only |
| Hyprland adapter (built in, Hyprland's IPC) | Hyprland with the Lua configuration and dispatch (0.56 or later) | Hyprland 0.57.2 (nested, automated; real Arch login in a VM, automated); 0.56.2 on Omarchy (real login, by hand; the v0.9.0 release package installed and checked 2026-11-19) |

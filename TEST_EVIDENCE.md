# Test evidence

What was run, where, and what came out. Every result here comes from a suite
in `tests/` or `packaging/test/`, run by the Claude Code agent on the machine
described below. Machine-readable results are in `tests/evidence/`.

None of this is a real login, a real logout, or a real reboot. The desktops
are nested (Xephyr), headless (`gnome-shell --headless`, `kwin_wayland
--virtual`) or in containers. Input events in the tests are sent by the test
harness (XTEST/xdotool, or the compositor's test hooks). Fractile itself never
injects input. See PLATFORM_MATRIX.md for what each environment can and
cannot show.

## Environments

| ID | Where | Desktop | Used by |
|---|---|---|---|
| E1 | Pop!_OS 24.04 host, kernel 7.2.6, NVIDIA RTX 3080 | GNOME Shell 46.0 on X11, nested in Xephyr 1600x1200, private D-Bus | X11 E2E, GUI, Settings, perf/soak |
| E2 | same host | GNOME Shell 46.0 `--headless --wayland --no-x11`, one virtual 1600x1200 monitor | GNOME Wayland E2E, Settings, tray |
| E3 | same host | as E2 with two virtual monitors (1600x1200 + 1280x1024); scale and enabled state changed at runtime through Mutter DisplayConfig | multi-monitor and display-change E2E |
| E4 | archlinux:latest container on the host kernel | KWin 6.8.2 `--virtual`, Wayland, no Xwayland | KWin E2E |
| E5 | ubuntu:24.04 container | Openbox on Xvfb | .deb install, lifecycle, T22 |
| E6 | archlinux:latest container | Openbox on Xvfb | PKGBUILD build, install, lifecycle |
| E7 | ubuntu:24.04 container | kwin_x11 4:5.27.11 (Plasma X11) on Xvfb, no plasmashell | X11 E2E under KWin |
| E8 | fedora:39 / 41 / 42 containers | GNOME Shell 45.10 / 47.10 / 48.8 headless Wayland | the five GNOME Wayland suites |
| E9 | Ubuntu 24.04.5 desktop in QEMU/KVM (Docker), 4 vCPU, 6 GiB, virtio-vga 1600x1200 | GNOME Shell 46.0 (Wayland and Xorg sessions) and Plasma 5.27 X11 (kde-plasma-desktop), real GDM logins | VM E2E (`tests/vm/`) |
| E10 | archlinux:latest container on the host kernel, host GPU render node | Hyprland 0.57.2 nested in `kwin_wayland --virtual` (KWin is only the parent display); native Wayland GTK4/Qt clients; keys with wtype | Hyprland E2E |
| E11 | archlinux:latest container on the host kernel | Sway 1.12 nested in `kwin_wayland --virtual` (wlroots Wayland backend, pixman renderer); native Wayland GTK4/Qt clients; keys with wtype | Sway E2E |
| E12 | fedora:39 container | GNOME Shell 45.10 `--x11` (real mutter WM) on Xvfb 1600x1200, private D-Bus session, AT-SPI | X11 suites under GNOME without a host display |
| E13 | ubuntu:24.04 container (as on Pop!_OS 24.04) | GNOME Shell 46.0 `--x11` (mutter 46.2) on Xvfb 1600x1200, system bus, AT-SPI, Orca 46.1 with speech-dispatcher (no audio) | X11 suites under GNOME 46 without a host display |
| E14 | fedora:42 container | GNOME Shell 48.8 `--x11` on Xvfb, Qt 6.10.2, Orca from Fedora 42 | Orca suite on a current Qt |
| E15 | Ubuntu 26.04.1 desktop with sway, swayidle and swaylock in QEMU/KVM (Docker), as E9 | Sway 1.11 with swaybar, real GDM timed logins into the Sway session | VM E2E on Sway |
| E16 | Arch Linux cloud image in QEMU/KVM (Docker), 1600x1200, llvmpipe; Hyprland, GDM, waybar, hyprlock, hypridle and the test apps from the Arch repositories | Hyprland 0.57.2, real GDM 51 timed logins into Arch's Hyprland session (no uwsm) | VM E2E on Hyprland |
| E17 | Arch Linux cloud image in QEMU/KVM (Docker), 1600x1200, llvmpipe; cosmic-session (the whole COSMIC desktop), GDM, vulkan-swrast and GNOME Text Editor from the Arch repositories | COSMIC 1.11.0 (cosmic-comp 1:1.11.0-1), real GDM 51 timed logins into the COSMIC session, auto-tiling switched on in COSMIC's own config | VM E2E on COSMIC (partial support) |

## Results

| Suite | Command | Env | Result | Evidence |
|---|---|---|---|---|
| Rust unit, property and restore-simulation tests | `cargo test --workspace` | host | all pass | (console) |
| X11 E2E | `python3 tests/integration/e2e_x11.py` | E1 | 44/44 | `tests/evidence/e2e-x11.json` |
| X11 E2E under Plasma X11 | `bash tests/kwin-x11/e2e-kwin-x11-in-container.sh` | E7 | 51/51 (with X37 workspaces, 2026-11-14; again on the final build e14aa01, 2026-11-15), including RS1-RS6 (restore policies off, last_known, last_clean after a crash, ask, named). RS1-RS6 also pass under GNOME 45 (E12) | `tests/evidence/e2e-x11-kwin.json` |
| Real apps (T14: browsers, editors, terminals) | `python3 tests/integration/e2e_x11_apps.py` | E1 | 13/13, Chrome incognito recorded as a known limitation | `tests/evidence/e2e-x11-apps.json` |
| Real Google Chrome 157 (T14, RS-20) in the GNOME 46 X11 container | `FRACTILE_INSTALL_CHROME=1 FRACTILE_TEST_APPS=chrome tests/gnome46-x11/run.sh tests/integration/e2e_x11_apps.py` | E1 | 5/5; incognito window not detectable (KNOWN A01e), but no Chrome title is stored and the incognito title is absent from the export (A01f) | `tests/evidence/e2e-x11-apps-gnome46-x11.json` |
| Chrome 157 normal vs incognito window, every X11 property (RS-20) | `FRACTILE_INSTALL_CHROME=1 tests/gnome46-x11/run.sh tests/tools/chrome_window_props.py` | Xvfb in the GNOME 46 container | no property marks the incognito window | `tests/evidence/chrome-window-props-gnome46-x11.json` |
| Two monitors under GNOME Shell 46 X11 (DV-12, DV-13, DV-17) | `tests/gnome46-x11/run.sh tests/gui/gui_two_monitors.py` | Xvfb split into two RandR monitors, GNOME Shell 46.0 as WM, Ubuntu 24.04 container | 6/6 | `tests/evidence/gui-two-monitors-gnome46-x11.json` |
| GUI (real PyQt6 UI) | `python3 tests/gui/gui_x11.py` | E1 | 18/18 | `tests/evidence/gui-x11.json`, screenshots `tests/evidence/gui/` |
| GUI (real PyQt6 UI) under Plasma X11 | `bash tests/gui/gui-kwin-x11-in-container.sh` | kwin-x11 test image (Xvfb, kwin_x11 5.27) | 21/21 (again on the final build e14aa01, and on the build with the COSMIC adapter, 2026-11-15), G15 skipped (kwin_x11 takes Alt+click; G18 covers tap-then-drag). G18-G21 also pass under GNOME 45 (E12) | `tests/evidence/gui-x11-kwin.json` |
| Accessibility (UX-06) | `bash tests/gui/a11y-in-container.sh` | kwin-x11 test image (Xvfb, kwin_x11 5.27, at-spi2-core) | 5/5: A1 names over AT-SPI, A2 high contrast, A3 150% font, A4 right to left (ar_EG locale) | `tests/evidence/a11y-kwin.json` |
| X11 suites under GNOME Shell 45 | `bash tests/gnome/x11-gnome-in-container.sh fedora:39 [suite.py ...]` (default: E2E, GUI, Settings; also `tests/gui/a11y_x11.py`) | E12 | X11 E2E 50/50, GUI 22/22 (G15 runs: mutter leaves Alt+click to the app), Settings 22/22, accessibility 5/5 | `tests/evidence/e2e-x11-gnome45-x11.json`, `gui-x11-gnome45-x11.json`, `settings-x11-gnome45-x11.json`, `a11y-x11-gnome45-x11.json` |
| X11 suites under GNOME Shell 46 | `bash tests/gnome46-x11/run.sh [suite.py ...]` (default: E2E, GUI, Settings, accessibility) | E13 | X11 E2E 51/51 (with X37: workspaces through EWMH, 2026-11-14), GUI 22/22, Settings 22/22, accessibility 5/5; all four again on the final build e14aa01 (2026-11-15) | `tests/evidence/e2e-x11-gnome46-x11.json`, `gui-x11-gnome46-x11.json`, `settings-x11-gnome46-x11.json`, `a11y-x11-gnome46-x11.json` |
| Screen reader (Orca) | `bash tests/gnome46-x11/run.sh tests/gui/orca_x11.py`; `bash tests/gnome/x11-gnome-in-container.sh fedora:42 tests/gui/orca_x11.py` | E13 (Qt 6.4), E14 (Qt 6.10) | E14 6/6. E13 5/6: O1b fails because Qt 6.4 sends the grid's name change without the new name, so Orca drops it (known issue) | `tests/evidence/orca-x11-gnome46-x11.json`, `orca-x11-gnome48-x11.json` |
| Panel shortcut just after the tray-only UI left | `bash tests/gui/early-panel-in-container.sh` | kwin-x11 test image | PASS; FAIL before the fix (no panel) | (console) |
| Command palette snaps the focused window, repeated | `bash tests/gui/gui-kwin-x11-in-container.sh palette_loop_x11.py palette-loop-kwin.json` | kwin-x11 test image | 60/60 (and 160/160 by hand); 3 failures in 105 before the fix | `tests/evidence/palette-loop-kwin.json` |
| GUI on two monitors (DV-12, DV-13, DV-17) | `bash tests/gui/gui-two-monitors-in-container.sh` | kwin-x11 test image, Xvfb split into two RandR monitors (1600x1200 + 1280x1024) | 6/6 (G25: a repeated placement shortcut cycles the monitors) | `tests/evidence/gui-two-monitors-kwin.json` |
| Settings and shortcut editor under Plasma X11 | `bash tests/gui/gui-kwin-x11-in-container.sh settings_x11.py settings-x11-kwin.json` | kwin-x11 test image (Xvfb, kwin_x11 5.27) | 22/22, including S18-S21 (Duplicate, Export, Remove, Import) which have not run under GNOME yet | `tests/evidence/settings-x11-kwin.json` |
| Settings and shortcut editor (X11) | `python3 tests/gui/settings_x11.py` | E1 | 18/18 | `tests/evidence/settings-x11.json` |
| One UI after a service restart | `bash tests/gui/ui-restart-in-container.sh` | kwin-x11 test image (Xvfb, kwin_x11) | passed (the unfixed build left 2 UIs) | output of the script |
| UI restarts onto new code after an upgrade (idle, and at the panel shortcut) | `bash tests/gui/ui-upgrade-in-container.sh` | kwin-x11 test image (Xvfb, kwin_x11) | passed (the unfixed build kept the old code) | output of the script |
| Service restarts onto a new binary after an upgrade (not while the panel is open) | `bash tests/gui/service-upgrade-in-container.sh` | kwin-x11 test image (Xvfb, kwin_x11) | passed (without the watcher the new binary is never picked up) | output of the script |
| Recovery dialog keys | `python3 tests/gui/recovery_keys.py` | host (offscreen Qt) | 3/3 | `tests/evidence/recovery-keys.json` |
| GNOME Wayland E2E | `python3 tests/integration/e2e_gnome_wayland.py`; in containers `bash tests/gnome/e2e-gnome-in-container.sh fedora:42 tests/integration/e2e_gnome_wayland.py` (and fedora:41, fedora:39) | E2; GNOME 48.8, 47.10, 45.10 | 27/27 (E2, 2026-11-14, with W30 margins and gaps and W06b 20x20 no drift; GNOME 48 container again on the final build e14aa01, 2026-11-15); 26/26 on 45, 47 and 48 (before W06b) | `tests/evidence/e2e-gnome-wayland.json`, `e2e-gnome-wayland-gnome{45,47,48}.json` |
| GNOME Wayland multi-monitor | `python3 tests/integration/e2e_gnome_wayland_multimon.py` | E3 | 8/8 | `tests/evidence/e2e-gnome-wayland-multimon.json` |
| GNOME Wayland display changes (mixed DPI, disconnect/reconnect) | `python3 tests/integration/e2e_gnome_wayland_displays.py` | E3 | 10/10 | `tests/evidence/e2e-gnome-wayland-displays.json` |
| Settings on GNOME Wayland | `python3 tests/gui/settings_gnome_wayland.py` | E2 | 23/23 (2026-11-14), including S05b: Super+Up, a Mutter keybinding, reaches the editor because the extension holds GNOME's keybindings while it listens; with the hold disabled S05b fails (negative control) | `tests/evidence/settings-gnome-wayland.json` |
| Panel on GNOME Wayland (DV-04, -05, -07, -09, -11, -15, -21, -24) | `bash tests/gnome/e2e-gnome-in-container.sh fedora:42 tests/gui/panel_gnome_wayland.py` (also fedora:41, fedora:39) | headless GNOME Shell 48.8, 47.10, 45.10 `--wayland`, real UI on Wayland; keys and pointer from Mutter's RemoteDesktop virtual devices (test harness) | 12/12 on each | `tests/evidence/panel-gnome-wayland-gnome{45,47,48}.json` |
| Panel in a real Plasma Wayland login (DV-01..05, -07, -09..11, -15, -18, -20, -21, -24, -27) | `FRACTILE_VM_PANEL=1 bash tests/vm/vm-e2e.sh plasma-wayland` | Ubuntu 26.04 VM, Plasma 6.6.6 Wayland, GDM timed login, .deb installed with apt; keys and pointer from QEMU's virtual keyboard and USB tablet | 39/39 (2026-11-14): the panel checks P00-P17, the tray (P18, P19) and the Settings probe in the real session (P-S01..S21 and P-S05b: Super+Up reaches the editor during the KDE hold and is named as KWin's “Quick Tile Window to the Top”, keys from a uinput test keyboard), then the shortcut made in Settings pressed (P-S11). P07 failed before the preview fix (no preview on KWin); P-S05 failed before the KDE shortcut hold (KWin opened its logout screen) | `tests/evidence/vm-ubuntu26.04-plasma-wayland-panel.json`, screenshots `tests/evidence/vm/panel-plasma-wayland-*.png` |
| Panel, tray and Settings in a real Plasma X11 login (DV-01..05, -07, -09..11, -14, -15, -18..21, -24..27) | `FRACTILE_VM_PANEL=1 bash tests/vm/vm-e2e.sh plasma-x11` | Ubuntu 24.04 VM, Plasma 5.27 X11, GDM timed login, .deb installed with apt; keys and pointer from QEMU's virtual keyboard and USB tablet, Settings keys from a uinput test keyboard | 38/38. P13 failed before the panel named its target (no active window right after the panel hid) | `tests/evidence/vm-ubuntu24.04-plasma-x11-panel.json` |
| Panel, tray and Settings in a real GNOME login (DV-01..05, -07, -09..11, -14, -15, -18..21, -24..27) | `FRACTILE_VM_PANEL=1 bash tests/vm/vm-e2e.sh wayland` / `x11` | Ubuntu 24.04 VM, GNOME Shell 46.0 Wayland and Xorg sessions, GDM timed login, .deb installed with apt; QEMU keyboard and USB tablet, Settings keys from a uinput test keyboard; tray from Ubuntu's AppIndicator extension | 38/38 on Wayland, 37/37 on Xorg (2026-11-14). The first run found that Ctrl+Alt+Delete pressed in the shortcut editor ran GNOME's log-out action and never reached the editor (fixed: the extension holds GNOME's keybindings while the editor listens), and a VM helper bug reading the AppIndicator watcher's item names | `tests/evidence/vm-ubuntu24.04-wayland-panel.json`, `vm-ubuntu24.04-x11-panel.json` |
| Two monitors on KWin Wayland (DV-12, DV-13, DV-17) | `bash tests/kwin/e2e-kwin-in-container.sh e2e_kwin_two.py 2 e2e-kwin-wayland-two-monitors.json` | kwin_wayland 6.8 --virtual --output-count 2 in the KWin test image; pointer from KWin's fake-input protocol (test harness), shortcuts through KGlobalAccel invokeShortcut | 7/7 | `tests/evidence/e2e-kwin-wayland-two-monitors.json` |
| Two monitors on GNOME Wayland (DV-12, DV-13) | `bash tests/gnome/e2e-gnome-in-container.sh fedora:42 tests/gui/panel_gnome_wayland_two.py` | headless GNOME Shell 48 with two virtual monitors (1600x1200 + 1280x1024); RemoteDesktop pointer and keys | 7/7 | `tests/evidence/panel-gnome-wayland-two-gnome48.json` |
| Panel in a real GNOME Wayland login (DV-01, -02, -27 and the panel rows) | `FRACTILE_VM_PANEL=1 bash tests/vm/vm-e2e.sh wayland` | Ubuntu 24.04 VM, GNOME Shell 46.0 Wayland, GDM timed login, .deb installed with apt; QEMU keyboard and USB tablet | 17/17 | `tests/evidence/vm-ubuntu24.04-wayland-panel.json` |
| Tray on GNOME Wayland (AppIndicator extension) | `python3 tests/gui/tray_gnome_wayland.py` | E2 | 12/12 | `tests/evidence/tray-gnome-wayland.json` |
| GNOME 45 / 47 / 48 (all five GNOME Wayland suites) | `bash tests/gnome/e2e-gnome-in-container.sh fedora:39` (`:41`, `:42`) | E8 | all pass on each | `tests/evidence/*-gnome45.json`, `*-gnome47.json`, `*-gnome48.json` |
| KWin Wayland E2E | `bash tests/kwin/e2e-kwin-in-container.sh` | E4 | 21/21 (K20: margins and gaps; K06b: 20x20 no drift, 2026-11-14; again on the final build e14aa01, 2026-11-15) | `tests/evidence/e2e-kwin-wayland.json` |
| Hyprland E2E | `bash tests/hyprland/e2e-hyprland-in-container.sh` | E10 | 28/28 (2026-11-15, with H24: a cell smaller than a window's minimum size stays in the work area, H25: a second monitor at scale 1.5, and H26: a window larger for a moment after mapping; H26 fails without either of its two fixes) | `tests/evidence/e2e-hyprland.json` |
| Real Hyprland login VM: start at login (the systemd unit), restore after log-out and reboot (10 apps / 15 windows), hyprlock, waybar tray, launched apps without the service's restrictions | `bash tests/vm/vm-e2e.sh hyprland` | E16 | 10/10 on the last four runs (2026-11-15, the last with the size-limit learning fixes). Earlier runs failed V05/V06: GNOME Calculator placed off-screen (fixed in the adapter), then GNOME Disks aborting when restored (the unit's RestrictSUIDSGID, inherited by launched apps; removed) | `tests/evidence/vm-arch-hyprland.json`, `tests/evidence/vm/hyprland-*.png` |
| Real COSMIC login VM (T17, T18, CA-04): Fractile started from the app menu uses the COSMIC adapter and says placement is not available and why; it lists the monitor, workspaces and windows, refuses placing and snapping with the reason and moves nothing, moves a window to the second workspace and maximizes it, saves it, writes nothing under `~/.config/cosmic`, shows its tray icon and answers a tray click with a window saying why; after a real reboot it reopens the approved app once on its workspace, maximized, reported RESTORED_WITHOUT_GEOMETRY, and a second restore opens nothing | `bash tests/vm/vm-e2e.sh cosmic packaging/build/fractile-0.9.0-1-x86_64.pkg.tar.zst` | E17 | 13/13 in 2 of 2 runs on the final build (2026-11-15), and 3 of 3 on the build before it (which waited 40 s instead of 60 s for late states). Earlier builds found: a tray click that showed nothing (fixed in the UI); COSMIC dropping or confirming late (11-38 s) a maximize for a window on a workspace not in view, which made restore report a false rejection; moving a maximized window to a tiled workspace un-maximizing it while the item said "state restored" (fixed: workspace first, then the state, late states checked at the end of the run, refused states reported as failed); and once the test's own `gio launch` of the editor was lost (the test retries it). The KWin X11 GUI suite was rerun on the final UI: 21/21 | `tests/evidence/vm-arch-cosmic.json`, `tests/evidence/vm/cosmic-*.png` |
| Sway E2E | `bash tests/sway/e2e-sway-in-container.sh` | E11 | 28/28 (2026-11-15, with S24: a cell smaller than a window's minimum size, S25: a second output at scale 1.5, and S26: a window larger for a moment after mapping) | `tests/evidence/e2e-sway.json` |
| Real Sway login VM: start at login, restore after log-out and reboot (10 apps / 15 windows), swaylock, swaybar tray, launched apps without the service's restrictions | `bash tests/vm/vm-e2e.sh sway` | E15 | 10/10 on the last three runs (2026-11-15, with V09 and V06's one-Logout-snapshot check). One run between them failed V05: GNOME Clocks came back 400x320 at y 880 for a 400x295 cell at y 905. The log showed why: at its cold start Clocks committed 320 px for a moment, Fractile learned that as its minimum height and moved it up to keep it on screen. Fixed (a limit is learned only when the window still holds the size 700 ms later; S26/H26); the two runs after the fix passed. 9/9 on the two runs before V09 existed. The run before them failed V05 once: GNOME Characters came back 400x319 for a 400x294 cell; not reproduced in 13 launches by hand, but one cold launch went back to its tiled size after placement (a late commit), so the Sway adapter now holds a just-mapped window at the target for 800 ms. gnome-terminal is compared by position only, with its placement checked in the restore report, because it resizes itself on focus changes | `tests/evidence/vm-ubuntu26.04-sway.json`, `tests/evidence/vm/sway-*.png` |
| Ubuntu desktop VM: install, real logins, log-out, reboot, restore of 10 apps / 15 windows | `bash tests/vm/vm-e2e.sh wayland` / `x11` / `plasma-x11` / `plasma-wayland` | E9 | 9/9 on GNOME Wayland, GNOME Xorg and Plasma 5.27 X11 (Ubuntu 24.04) and on Plasma 6.6.6 Wayland (Ubuntu 26.04), 2026-11-15, including V07 (the real lock screen), V06's one-Logout-snapshot count and V09: all 10 test apps run without no-new-privileges or a seccomp filter and with the user manager's umask, wherever they were started (on Plasma Wayland the desktop's own session restore and D-Bus started most of them); 8/8 on 2026-11-14 before V09 | `tests/evidence/vm-ubuntu24.04-wayland.json`, `vm-ubuntu24.04-x11.json`, `vm-ubuntu24.04-plasma-x11.json`, `vm-ubuntu26.04-plasma-wayland.json`, screenshots `tests/evidence/vm/` |
| Upgrade between builds (PK-06): 0.8.0 -> 0.8.5 -> 0.9.0 with apt over a running session, restore of an old snapshot, reboot | `FRACTILE_VM_UPGRADE_FROM="old-0.8.0.deb old-0.8.5.deb" bash tests/vm/vm-e2e.sh wayland new.deb` | Ubuntu 24.04 VM, GNOME Shell 46.0 Wayland, GDM timed login | 14/14 | `tests/evidence/vm-ubuntu24.04-wayland-upgrade.json` |
| VM power loss (T15, RS-18): QEMU `system_reset` during back-to-back snapshot writes, twice, then the next login | `FRACTILE_VM_POWER_CUT=1 bash tests/vm/vm-e2e.sh x11` | E9 (GNOME Xorg session) | 9/9 | `tests/evidence/vm-ubuntu24.04-x11-power-cut.json`, `tests/evidence/vm/x11-power-cut-log.txt`, `x11-pc1-prompt.png`, `x11-pc2-after.png` |
| Performance and soak | `FRACTILE_BIN_DIR=target/release python3 tests/perf/soak_x11.py` | E1 | see below | `tests/evidence/perf-x11.json` |
| 24-hour soak (T27) | `bash tests/vm/vm-e2e.sh soak 1440` | E9 (Xvfb + GNOME Shell 46.0 X11 in that VM) | 11/11 (second 24 h run, 2026-11-13/14, with the corrected P07 and the UI measured for the full 24 h) | `tests/evidence/perf-x11-24h.json`, `tests/evidence/vm/soak.log`, `soak-progress.log`, `soak-daemon-tail.log` |
| 4-hour soak with memory by process and the UI's RSS (T27) | `bash tests/vm/vm-e2e.sh soak 240` | E9 (as above) | 11/11 | `tests/evidence/perf-x11-240min.json`, `tests/evidence/vm/soak-240min.log`, `soak-240min-progress.log`, `soak-240min-daemon-tail.log` |
| .deb package lifecycle | `bash packaging/debian/build-deb.sh && bash packaging/test/deb-in-container.sh ubuntu:24.04 packaging/build/fractile_0.9.0-1_amd64.deb` | E5 | passed | `tests/evidence/package-deb-ubuntu24.04.log` |
| Arch package lifecycle | `bash packaging/test/arch-in-container.sh` | E6 | passed | `tests/evidence/package-arch.log` |

The check ids (X01, W23, K17, G15, S05, P09, ...) are what REQUIREMENTS.md
cites. Each JSON file lists every check with its name and measured detail.

## Measurements (SRS section 10)

| Target | Measured (release build, E1) | Check |
|---|---|---|
| Panel P95 < 150 ms after the hotkey (warm) | p50 13.1 ms, p95 19.6 ms, max 22.8 ms over 40 presses, 0 missed | P05 PASS |
| Placement P95 < 250 ms, request to geometry read back | service p50 105 ms, p95 114 ms (client round trip p95 115.9 ms), 200/200 within tolerance | P04 PASS |
| Idle service RSS < 75 MiB | 10.4 MiB, 9 threads, 16 fds (the UI process is separate: 65.4 MiB) | P02 PASS |
| Idle CPU median < 0.5% of a core | median 0.0%, max 0.0% over 60 s | P03 PASS |
| Soak: no crash, no leak (T27, shortened) | 20 minutes, 300 cycles (~3000 placements, 300 save+restore); RSS 11.5 to 14.3 MiB (max 14.4), fds 16 and threads 9 flat, 0 errors | P06 PASS |
| Soak, 24 hours (T27; VM, release build, 3 GiB cap) | Second run: 1440 minutes, 21199 cycles (~212000 placements, 21199 save+restore), 0 errors. fractiled RSS 14.2 MiB at 2 min, 17.4 MiB at 4 h, 17.6 MiB at 24 h; fds 19-21 and threads 11-12 flat. UI RSS 57.0 to 57.6 MiB over 24 h (P06u). The first 24 h run (21537 cycles, 17.3 MiB at 24 h) agreed | P06, P06u PASS |
| Whole test memory during the 24 h (cgroup: GNOME Shell, Xvfb, the UI, test windows, page cache) | Second run, by process: 624 MiB at 2 min, 809 MiB at 4 h, 1171 MiB at 24 h. `mutter-x11-frames` (the test desktop's X11 frame helper) 121 to 468 MiB and page cache 33 to 216 MiB account for nearly all of it; gnome-shell 269 to 280, the UI and test windows flat | not a check |
| Soak, 4 hours, memory by process (T27; VM, release build, 3 GiB cap) | 240 minutes, 3602 cycles, 0 errors. Whole test 623 to 787 MiB, as in the first 4 h of the 24 h run. Of the growth: `mutter-x11-frames` (GNOME's X11 window-frame helper, part of the test desktop) 120 to 246 MiB, page cache 34 to 68 MiB; gnome-shell 273 to 274 MiB and the test windows flat. fractiled 14.8 to 17.4 MiB; the Fractile UI 56.9 to 57.5 MiB (median of the first and last tenth of 451 samples, every 30 s) | P06 PASS, P06u PASS (11/11) |
| Automatic snapshots bounded | 21 automatic snapshots after 300 restores with max_recent=20 (20 minutes); 22 after 21199 restores in the second 24 h run (20 recent plus the newest of each of the 2 days it spanned) | P07 PASS (20 min and 24 h). The first 24 h run recorded FAIL on a wrong bound in the check, see below |
| Repeated relogins | 10/10 restored, no ghost processes, no duplicate windows, service ready p50 105 ms | P08 PASS |
| Logouts during a running restore | 6/6 interrupted while running; layout and the user's changes restored; automatic restore never paused | P09 PASS |

Most of the placement time is the deliberate read-back: the service waits for
the window manager to apply the geometry and reads it back before answering.
The RSS rose 2.9 MiB over the 20 minutes, under the 5 MiB bound. The 24-hour
run shows it levels off: 17.0 MiB at 4 hours, 17.3 MiB at 24 hours.

P07 in the 24-hour run: 22 automatic snapshots were left with max_recent=20.
Retention keeps the 20 newest automatic snapshots plus the newest of each
calendar day beyond those (daily history), and the run crossed midnight, so
22 is the designed result. The check allowed 20 + 1. A store test now
reproduces the run's pattern (a pre-restore snapshot every minute across
midnight, prune to 20: exactly 22 left, one extra per day;
`retention_across_midnight_keeps_one_extra_per_day`), and the check now
allows one extra per calendar day the run touches. The 24-hour run was not
repeated with the corrected check; its JSON keeps the FAIL as recorded.

The whole test grew by about 540 MiB over the 24 hours while fractiled stayed
flat. That run did not record which process grew, so a 4-hour run repeated it
with the cgroup's anonymous and page-cache memory, the four largest processes
and the UI's RSS logged every minute (`tests/evidence/perf-x11-240min.json`,
`tests/evidence/vm/soak-240min*.log`). It grew the same way in those 4 hours
(623 to 787 MiB), and the growth is the test desktop: `mutter-x11-frames`
(+126 MiB) and page cache (+34 MiB). Fractile's UI grew 0.6 MiB (56.9 to
57.5 MiB), slowing, and the service 2.6 MiB. The UI's memory was measured
over 4 hours, not 24. In that run P02's single idle sample missed the UI
process (it had not reconnected yet: the service showed 8 threads and 15 fds
instead of 11 and 19); P06u sampled it 451 times out of 451.

Method: `tests/perf/soak_x11.py` header. RSS and CPU are read from
`/proc/<fractiled>`; placement latency is the service's own timing from
request to geometry read back from the window manager; panel latency is from a
real hotkey event to the panel window being viewable (X map state), so it does
not include the compositor's paint.

The soak runs itself in a systemd user scope with a hard memory cap and no
swap (`--memory-max`, default 3G; `none` turns it off), so a leak anywhere in
the test (Xephyr, the nested shell, the UI, the service) is killed at the cap
instead of pushing the desktop into swap. It prints the service's RSS, fds and
threads and the whole test's memory every minute. A 2-minute check of these
changes measured the whole test flat at about 557 MiB (fractiled 13.7 to
13.9 MiB). The five named soak snapshots are reused (each save deletes that
name's previous one); before, every cycle added a pinned snapshot. The test
desktops (`nested-gnome-x11.sh`, `headless-gnome-wayland.sh`) start a
watchdog that stops them when the test that started them exits without
stopping them (verified by killing a test with SIGKILL: the shell, its bus
and its state were gone within seconds). Before, an interrupted GNOME Wayland
run left its headless shell running.

## Bugs found by the suites, and the before/after evidence

| Bug | Found by | Before the fix | After |
|---|---|---|---|
| Logging out during the login restore left no restore for the next login | soak relogins (P08), then reproduced in X11 E2E | X28 FAIL | X28 PASS |
| Logging out while a restore ran lost the session's changes, and three such logouts paused automatic restore ("disabled after repeated failures") | full soak P08 (3/10 restored) | P09 FAIL: breaker tripped, 3 windows not restored (`tests/evidence/perf-x11-p09-before-fix.json`, built from commit 00b2c53) | P09 PASS 6/6 (`tests/evidence/perf-x11-p09-after-fix.json`) |
| A cancelled restore reported unreached windows as SKIPPED | P09 service log | sim test `cancel_before_placing_reports_cancelled` FAIL | PASS |
| Shortcut editor showed a raw X11 error dump | S05 | message `X11Error(X11Error { error_kind: Access, ...` | "another application already uses this key combination" |
| Shortcuts list said the panel shortcut had no keys | Settings screenshot 15 | "Show/hide panel: no keys set" while Ctrl+Alt+G worked | S13 PASS |
| Integration with the wrong adapter API showed only "not connected" | T19 work | no reason shown | W23 PASS |
| KDE kept Fractile's shortcuts after exit | K17 | ghost bindings in kglobalshortcutsrc | K17 PASS |
| Automatic snapshot store grew between periodic ticks | soak P07 | growth with every restore | bounded (P07) |
| Under the systemd user unit every login of a boot had the same session key (no `XDG_SESSION_ID`), so no login restore ran after a log-out, and the lock state was unknown | VM V06 | guest DB: both sessions keyed `<boot>:nosession`; no login flow at the second login | each login keyed by its logind session (User.Display); V06 PASS |
| Plasma 5.27 log-out left the service running without its X display, and the next login started no new one | VM V03 (plasma-x11) | after log-out and log-in: unit active, `window_control` false, log "The X11 server closed the connection" (`tests/evidence/vm/plasma-x11-00-no-control-before-fix.png`) | unit bound to plasma-workspace.target; the service exits when its display closes; V03-V06 PASS |
| On GNOME no logout snapshot: gnome-session stops GNOME Shell before systemd stops the service | VM V06 | "exit capture failed: ... extension is not connected"; "no snapshot from the previous session" | snapshot taken at gnome-session's EndSession (15 windows); V06 PASS |
| Enter in the restore prompt unticked the focused row instead of restoring | VM V05 | Enter toggled the focused row checkbox; nothing was restored | focus on "Restore selected"; R01-R03 and V05 PASS |
| GNOME 45: a frame rescaled while crossing monitors of another scale was learned as the window's minimum | GNOME 45 display suite (D02) | learned minimum 2272 px; later placements too large | re-requested, never learned; D02 PASS 3/3 |
| GNOME 47: a window a pixel or two over the cell was left on the wrong monitor | GNOME 47 display suite (D02) | D02 FAIL | D02 PASS |
| GNOME 47: the first placement of a new window was undone by Mutter | GNOME 47 E2E (W04) | `e2e-gnome-wayland-gnome47-before-fix.json` | W04 PASS |

## Security checks (T22, T26)

- Other users: in the Ubuntu container, root and a second user `v` try to use
  user `u`'s socket. Root is refused by the SO_PEERCRED check (connection
  reset, logged as "rejected connection from uid 0"); `v` cannot open the
  socket at all (0700 directory). The service keeps serving `u`
  (`package-deb-ubuntu24.04.log`, section T22).
- Same user, wrong process: a non-compositor process cannot attach as the
  GNOME or KWin integration or inject events (SEC5, SEC6).
- Requests before `hello` are refused (SEC2). The socket is 0600 in a 0700
  directory (SEC1).
- Hostile imports: command fields are dropped, launch policies forced to
  `ask`, path-like desktop ids rejected (SEC3, SEC4, SEC7).

## What is not covered

- A power-loss interruption on real hardware (the VM power cut is
  covered). Physical monitors were used by hand only: two monitors at scale
  1 on Pop!_OS GNOME Xorg, and a laptop panel at scale 1.5 on Omarchy
  (Hyprland); mixed DPI and disconnect/reconnect ran on virtual monitors
  through DisplayConfig.
- The tray on panels other than GNOME's AppIndicator extension, Plasma,
  swaybar, waybar and COSMIC's status area.
- COSMIC window placement: BLOCKED BY PLATFORM (docs/compatibility/cosmic.md);
  the partial COSMIC support ran in a VM only (llvmpipe, one monitor).
- Pop!_OS host package install: done by hand on the developer's desktop
  (`apt install`, GNOME 46 on Xorg); no automated suite ran there. Recording
  a shortcut in Settings works there with the current build (checked by hand,
  2026-11-14).
- The 24-hour soak ran on Xvfb with GNOME Shell (X11) in the Ubuntu VM, not
  on a GNOME Wayland or KDE session. Earlier attempts on the developer's
  desktop were stopped (SIGTTIN after 2 minutes; then a run without a memory
  cap stopped while the desktop was short of memory); the VM run is the
  evidence. The UI process's memory was measured over 4 hours (see above), not
  24, and the relogins in the soak are simulated (real logins: the VM
  suites).

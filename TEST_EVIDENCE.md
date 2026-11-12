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

## Results

| Suite | Command | Env | Result | Evidence |
|---|---|---|---|---|
| Rust unit, property and restore-simulation tests | `cargo test --workspace` | host | all pass | (console) |
| X11 E2E | `python3 tests/integration/e2e_x11.py` | E1 | 39/39 | `tests/evidence/e2e-x11.json` |
| X11 E2E under Plasma X11 | `bash tests/kwin-x11/e2e-kwin-x11-in-container.sh` | E7 | 39/39 | `tests/evidence/e2e-x11-kwin.json` |
| Real apps (T14: browsers, editors, terminals) | `python3 tests/integration/e2e_x11_apps.py` | E1 | 13/13, Chrome incognito recorded as a known limitation | `tests/evidence/e2e-x11-apps.json` |
| GUI (real PyQt6 UI) | `python3 tests/gui/gui_x11.py` | E1 | 18/18 | `tests/evidence/gui-x11.json`, screenshots `tests/evidence/gui/` |
| Settings and shortcut editor (X11) | `python3 tests/gui/settings_x11.py` | E1 | 18/18 | `tests/evidence/settings-x11.json` |
| Recovery dialog keys | `python3 tests/gui/recovery_keys.py` | host (offscreen Qt) | 3/3 | `tests/evidence/recovery-keys.json` |
| GNOME Wayland E2E | `python3 tests/integration/e2e_gnome_wayland.py` | E2 | 25/25 | `tests/evidence/e2e-gnome-wayland.json` |
| GNOME Wayland multi-monitor | `python3 tests/integration/e2e_gnome_wayland_multimon.py` | E3 | 8/8 | `tests/evidence/e2e-gnome-wayland-multimon.json` |
| GNOME Wayland display changes (mixed DPI, disconnect/reconnect) | `python3 tests/integration/e2e_gnome_wayland_displays.py` | E3 | 10/10 | `tests/evidence/e2e-gnome-wayland-displays.json` |
| Settings on GNOME Wayland | `python3 tests/gui/settings_gnome_wayland.py` | E2 | 18/18 | `tests/evidence/settings-gnome-wayland.json` |
| Tray on GNOME Wayland (AppIndicator extension) | `python3 tests/gui/tray_gnome_wayland.py` | E2 | 12/12 | `tests/evidence/tray-gnome-wayland.json` |
| GNOME 45 / 47 / 48 (all five GNOME Wayland suites) | `bash tests/gnome/e2e-gnome-in-container.sh fedora:39` (`:41`, `:42`) | E8 | all pass on each | `tests/evidence/*-gnome45.json`, `*-gnome47.json`, `*-gnome48.json` |
| KWin Wayland E2E | `bash tests/kwin/e2e-kwin-in-container.sh` | E4 | 19/19 | `tests/evidence/e2e-kwin-wayland.json` |
| Ubuntu desktop VM: install, real logins, log-out, reboot, restore of 10 apps / 15 windows | `bash tests/vm/vm-e2e.sh wayland` / `x11` / `plasma-x11` | E9 | 7/7 on GNOME Wayland, 7/7 on GNOME Xorg, 7/7 on Plasma 5.27 X11 | `tests/evidence/vm-ubuntu24.04-wayland.json`, `vm-ubuntu24.04-x11.json`, `vm-ubuntu24.04-plasma-x11.json`, screenshots `tests/evidence/vm/` |
| Performance and soak | `FRACTILE_BIN_DIR=target/release python3 tests/perf/soak_x11.py` | E1 | see below | `tests/evidence/perf-x11.json` |
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
| Automatic snapshots bounded | 21 automatic snapshots after 300 restores with max_recent=20 | P07 PASS |
| Repeated relogins | 10/10 restored, no ghost processes, no duplicate windows, service ready p50 105 ms | P08 PASS |
| Logouts during a running restore | 6/6 interrupted while running; layout and the user's changes restored; automatic restore never paused | P09 PASS |

Most of the placement time is the deliberate read-back: the service waits for
the window manager to apply the geometry and reads it back before answering.
The RSS rose 2.9 MiB over the 20 minutes, under the 5 MiB bound; a 24-hour run
would show whether it levels off.

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

- A real login on Plasma 6 / KWin Wayland, a power-loss
  interruption, and physical monitors (mixed DPI and disconnect/reconnect ran
  on virtual monitors through DisplayConfig).
- The tray outside GNOME with the AppIndicator extension.
- COSMIC: BLOCKED BY PLATFORM (docs/compatibility/cosmic.md).
- Pop!_OS host package install (no root on the test host): dependency
  resolution was simulated (`tests/evidence/package-deb-popos24.04-simulate.log`);
  the real install ran on Ubuntu 24.04 (container and VM).
- The 24-hour soak (T27) has not been run to the end. The first attempt was
  stopped by the terminal (SIGTTIN) 2 minutes in; the second was started on
  the developer's desktop without a memory cap and was stopped during the
  soak loop while that desktop was short of memory (it logged no memory
  figures before it stopped). The soak evidence is still the 20-minute run.
  The harness now runs in a memory-capped scope and logs the whole test's
  memory every minute (see Method); the 24-hour run is to be repeated that
  way, in the VM or on an otherwise idle machine.

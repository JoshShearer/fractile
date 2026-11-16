# Fractile

Grid window placement for Linux, in the style of Divvy, plus workspace
snapshots and restoring your windows at login.

Press a shortcut and a 6x6 grid appears. Drag across the cells you want, and the
active window moves and resizes to exactly that rectangle. Hold Alt for a finer
grid, Shift to keep the panel open. Named shortcuts can place windows without
the panel. Fractile can also save which apps are open and where their windows
are, and put them back later, including after you log in again. It only
launches apps you approved.

- **Works on:** GNOME on X11 and Wayland (Wayland needs the bundled extension),
  KDE Plasma on Wayland (needs the bundled KWin script), Hyprland with a Lua configuration (0.56 or later)
  and Sway 1.8 or later (through their own IPC; placed windows become
  floating), and X11 window managers.
- **Partly works on:** COSMIC. It offers no API for moving or resizing other
  programs' windows, so the grid panel and snapping do not work there.
  Session restore does, without positions or sizes: apps are reopened and
  windows go back to their workspace, maximized or not. See
  [docs/compatibility/cosmic.md](docs/compatibility/cosmic.md).
- **Does not work on:** other Wayland compositors (river, Wayfire, labwc,
  ...). Fractile says so instead of pretending.
- **Does not restore window contents.** Unsaved documents, tabs and terminal
  sessions come back only if the application restores them itself.

Status: 0.9.0. Tested in nested, headless and container desktops, not yet on
real logins or reboots. Read [PLATFORM_MATRIX.md](PLATFORM_MATRIX.md) before
relying on it.

## Install

```
sudo apt install ./fractile_0.9.0-1_amd64.deb      # Ubuntu / Pop!_OS 24.04
cd packaging/arch && makepkg -si                    # Arch
```

Then, on GNOME Wayland, log out and in once and enable the extension
(`gnome-extensions enable fractile@fractile.io`). On Plasma, enable "Fractile"
under System Settings > Window Management > KWin Scripts. Start Fractile from
the app menu. `fractile status` tells you whether window control works.

Full guide: [docs/user-guide](docs/user-guide/README.md).

## Build and test

Requirements: Rust (stable), Python 3 with PyQt6 and PyGObject, and SQLite
(bundled through rusqlite). For the test suites: Xephyr, gnome-shell, xdotool,
xprop, python3-xlib and Docker.

```
cargo build --release
cargo test --workspace                                  # unit, property and restore-simulation tests
python3 tests/integration/e2e_x11.py                    # nested GNOME X11
python3 tests/gui/gui_x11.py                            # real UI on nested GNOME X11
python3 tests/integration/e2e_gnome_wayland.py          # headless GNOME Wayland + extension
python3 tests/integration/e2e_gnome_wayland_multimon.py # two monitors
bash tests/kwin/e2e-kwin-in-container.sh                # KWin Wayland in a container
bash tests/hyprland/e2e-hyprland-in-container.sh        # Hyprland, nested in a headless KWin
bash tests/sway/e2e-sway-in-container.sh                # Sway, nested in a headless KWin
FRACTILE_BIN_DIR=target/release python3 tests/perf/soak_x11.py   # SLOs + soak
bash packaging/debian/build-deb.sh && bash packaging/test/deb-in-container.sh ubuntu:24.04 packaging/build/fractile_0.9.0-1_amd64.deb
bash packaging/test/arch-in-container.sh
```

Every suite runs isolated (its own display, D-Bus, runtime and data
directories) and does not touch your session. Input events in the tests are
sent by the test harness. Fractile itself never injects input.

## Documents

| | |
|---|---|
| [GOAL.md](GOAL.md) | goals and success thresholds |
| [REQUIREMENTS.md](REQUIREMENTS.md) | SRS traceability with per-platform status |
| [PLATFORM_MATRIX.md](PLATFORM_MATRIX.md) | tested versions and capabilities |
| [ARCHITECTURE.md](ARCHITECTURE.md) | design and decision records ([internals](docs/architecture/README.md)) |
| [TEST_EVIDENCE.md](TEST_EVIDENCE.md) | commands, results, measurements |
| [RISK_REGISTER.md](RISK_REGISTER.md) | open risks |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | phases and next steps |
| [CHANGELOG.md](CHANGELOG.md) | changes and known issues |
| [docs/security](docs/security/README.md) | security model and limits |
| [docs/compatibility](docs/compatibility/README.md) | per-desktop notes |

## License

GPL-3.0-or-later. Fractile is an independent project. It is not affiliated with
Mizage or Divvy, and it uses none of their code or artwork. Divvy is referenced
only as a functional baseline.

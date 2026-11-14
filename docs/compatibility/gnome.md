# GNOME Shell

Fractile controls windows on GNOME in two different ways, depending on the session type.

| Session | Adapter | How |
|---|---|---|
| GNOME on **Wayland** | `gnome` (bridge) | The Fractile GNOME Shell extension (`fractile@fractile.io`) runs inside gnome-shell. It connects to the Fractile service's private socket and carries out window operations with Mutter's API. |
| GNOME on **X11** | `x11` | The Fractile service talks to the X server directly (EWMH, ICCCM size hints, RandR). No extension is needed. |

## Tested

| Environment | Result | Evidence |
|---|---|---|
| GNOME Shell 46.0, X11, nested (Xephyr + `gnome-shell --x11`, real Mutter as WM), Pop!_OS 24.04 host | all functional checks pass | `tests/evidence/e2e-x11.json`, `gui-x11.json`, `perf-x11.json` |
| GNOME Shell 46.0, Wayland, headless (`gnome-shell --headless --wayland --no-x11`, one 1600x1200 virtual monitor) | all checks pass | `tests/evidence/e2e-gnome-wayland.json` |
| Same, two virtual monitors 1600x1200 + 1280x1024 | all checks pass | `tests/evidence/e2e-gnome-wayland-multimon.json` |
| Same, monitors changed at runtime through Mutter DisplayConfig: scale 2 + scale 1 (mixed DPI), monitor disabled and re-enabled | 10/10 | `tests/evidence/e2e-gnome-wayland-displays.json` |
| Same, tray: Ubuntu AppIndicators extension + the PyQt6 UI | 12/12 | `tests/evidence/tray-gnome-wayland.json` |
| GNOME Shell 45.10 (Fedora 39), 47.10 (Fedora 41), 48.8 (Fedora 42), each headless in a container (`tests/gnome/e2e-gnome-in-container.sh`): the functional, two-monitor, display-change (mixed DPI, disconnect/reconnect), Settings and tray suites | all pass on all three | `tests/evidence/*-gnome45.json`, `*-gnome47.json`, `*-gnome48.json` |
| Ubuntu 24.04.5 desktop (GNOME Shell 46.0, Wayland and Xorg sessions) in a QEMU/KVM VM: real GDM login, `apt install` of the .deb, start from the app menu, real log-out and log-in, reboot and restore (`tests/vm/vm-e2e.sh wayland`, `x11`) | 7/7 on each | `tests/evidence/vm-ubuntu24.04-wayland.json`, `vm-ubuntu24.04-x11.json`, screenshots in `tests/evidence/vm/` |

Version differences found by these runs, and handled:

- **GNOME 47** lists a new window before Mutter has placed it. Fractile offers
  new windows only once Mutter has shown them, so its first placement is not
  undone.
- **GNOME 45** (Mutter physical layout mode) briefly rescales a window's frame
  while it moves to a monitor of another scale. Fractile requests the placement
  again and does not take the rescaled frame as the window's minimum size.
- **GNOME 47** (logical layout mode) does not move a window whose minimum is a
  pixel or two over the cell. Fractile learns that minimum when the move was
  skipped, and the next placement fits.

At log-out, gnome-session stops GNOME Shell before systemd stops the
Fractile service, so Fractile registers with gnome-session and saves the
logout snapshot when the session ends, before the shell goes.

Used by hand on a real Pop!_OS 24.04 desktop (GNOME 46 on Xorg, x11
adapter, two physical monitors at scale 1): install, the panel on each
monitor, placement on both. That found a GNOME Xorg behaviour: a frameless
window asking for a position on the second monitor is first mapped at
120,120 on the primary; the UI moves its panels back after the map.

**Not tested:** fractional scaling; a physical monitor hot-plug (the display
changes above are made through DisplayConfig on virtual monitors); a
non-Ubuntu real login.

## Setting it up on Wayland

1. Install the package. The extension is installed to
   `/usr/share/gnome-shell/extensions/fractile@fractile.io/`, **disabled**.
2. Log out and back in once (GNOME on Wayland only discovers new extensions at
   login).
3. Enable it in the Extensions app, or run
   `gnome-extensions enable fractile@fractile.io`.
4. `fractile status` should show `Window control: available`.

Fractile never enables the extension itself. Until it is enabled, `status` and
the Diagnostics page say window control is not available and show these steps.

## How the extension is trusted

The extension connects to `$XDG_RUNTIME_DIR/fractile/fractile.sock`. The service
accepts it as the integration only if the connecting process is running a
root-owned `gnome-shell` binary (checked through `/proc/<pid>/exe`, not the
process name). See [security](../security/README.md).

## Shortcuts

- On Wayland, Fractile's global shortcuts are grabbed by GNOME Shell through the
  extension (`global.display.grab_accelerator`). They work in every application
  (W09).
- On X11, Fractile grabs keys on the root window with `XGrabKey` (X09).
- Conflicts with GNOME's own bindings are detected for the bindings Fractile can
  see (`fractile shortcut-check`, the shortcut editor). Bindings held by other
  programs cannot always be detected. The editor says so.

## Known limitations

- GTK4 windows with client-side decorations have invisible resize borders.
  Fractile places the *visible* frame, using the window's frame extents (W04).
- Windows with a minimum size larger than the target cell are fitted to the cell
  as far as the window allows, and the placement result says so ("constrained by
  the window: ..."). The policy (fit or fail) is configurable (M06).
- Extension API changes between GNOME releases can break the extension. The
  service then reports the integration as missing or refused. It does not report
  false success (W16, W23).

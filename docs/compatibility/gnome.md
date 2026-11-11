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

**Not tested:** a real (non-nested, non-headless) GNOME login; GNOME 45, 47 or 48
(the extension's metadata declares 45-48, but only 46 was exercised); mixed-DPI
monitors (the virtual monitors are all scale 1); fractional scaling; monitor
hot-plug during a session; the tray icon (stock GNOME has no tray without the
AppIndicator extension); real logout and reboot.

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

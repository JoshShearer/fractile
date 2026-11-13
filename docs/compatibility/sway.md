# Sway

| Session | Adapter | How |
|---|---|---|
| Sway 1.8 or later (Wayland) | `sway` | The Fractile service talks to Sway's IPC socket (`$SWAYSOCK`, normally `$XDG_RUNTIME_DIR/sway-ipc.<uid>.<pid>.sock`): the window tree, outputs and workspaces, Sway commands, and the event stream. Nothing has to be installed or enabled in Sway. |
| Sway before 1.8 | none | Not supported; `fractile status` says so with the version it found. |

## Tested

| Environment | Result | Evidence |
|---|---|---|
| Sway 1.12 (Arch package) nested inside `kwin_wayland --virtual` (wlroots' Wayland backend, pixman renderer), in an `archlinux:latest` container; native Wayland GTK4 and Qt windows, the real Fractile UI; keys pressed with `wtype` | 25/25 | `tests/evidence/e2e-sway.json` (`tests/sway/e2e-sway-in-container.sh`) |

The suite covers: detection and connection, and an unreachable socket
reported with the reason; window, output and workspace lists; grid
placements on GTK4 and Qt windows (0 px error); 1x1 filling the work area;
undo; activation; leaving fullscreen to place; global shortcuts as Sway
bindings pressed through Sway's keyboard path; conflicts with bindings of the
user's configuration, in the main file and in a file it includes (reported,
the user's bindings still run); a shortcut id and a window title full of Sway
command syntax staying data; moving windows between workspaces; snapshot
restore; the panel opened by its shortcut, floating, focused and driven with
the keyboard; `swaymsg reload` (Fractile's bindings come back); a second
output and moving a window onto it; bindings removed on exit; a killed
service followed by a new one (found without `SWAYSOCK`); and the service
saving and exiting when Sway exits.

This is a real Sway with real clients, but **not** a real Sway login: it
runs as a window of a headless KWin, with no swaybar, no portal for Sway and
no display manager.

**Not tested:** a real login (from a TTY or a display manager), physical
monitors, a scale other than 1, XWayland applications, tabbed or stacked
containers, the scratchpad as "minimize" beyond unit tests, session restore
across a real log-out and log-in, and Sway versions other than 1.12.

## Setting it up

1. Install the package.
2. The systemd user unit starts with `graphical-session.target`. A Sway
   started by a session manager that reaches that target (uwsm, or a
   distribution's sway session) passes its environment (`WAYLAND_DISPLAY`,
   `SWAYSOCK`) to user services; enable the unit with `fractile autostart on`.
   Without one, start the service from the Sway configuration instead:
   `exec fractiled --autostart` (not run by the test suite).
3. `fractile status` should show `Window control: available` and adapter
   `sway`.

If `SWAYSOCK` is not set, the service uses this user's newest running Sway
socket in `$XDG_RUNTIME_DIR`.

## How Fractile behaves on Sway

- **Tiled windows become floating when placed.** A grid placement runs
  `floating enable, resize set, move absolute position` on that one window;
  the frame Fractile places is the container rectangle, border and title bar
  included. The layout of other windows is not changed. `fractile status`
  lists Sway as the tiling manager.
- **The work area** of an output is the rectangle of the workspace it shows,
  so space taken by bars is left out.
- **Fractile's own windows float.** At start the service adds a runtime
  `for_window` rule matching only Fractile's app id and the titles of its
  panel, palette, preview and small dialogs. Sway cannot remove a
  `for_window` rule at runtime; a config reload drops it and Fractile adds it
  again. Each service start adds one more copy until the next reload (the
  copies are identical).
- **Shortcuts are runtime bindings** (`bindsym --no-repeat <keys> nop
  <token>`). Sway's IPC cannot list bindings, and a runtime `bindsym` on keys
  the configuration already binds would silently replace that binding. So
  before binding, Fractile reads the configuration (the main file Sway
  loaded and the files it `include`s, with `set $var` variables) and reports
  keys bound there in the default mode as a conflict. `bindcode` bindings
  cannot be matched without the keymap; `fractile diagnose` says how many
  there are. Bindings added at runtime by other tools are not visible to
  Fractile. A config reload drops runtime bindings; Fractile adds them again
  unless the reloaded configuration now binds the same keys (`fractile
  diagnose` lists those). On exit they are removed. Bindings of a service
  that was killed stay until a reload; the next service binds the same keys
  over them, and a stale binding on other keys does nothing (its token is not
  the new service's).
- **Workspaces** are Sway's numbered workspaces (index = number - 1). Named
  workspaces without a number are not listed.
- **Minimize** moves a window to the scratchpad; a window in the scratchpad
  counts as minimized. Sway has no maximize or keep-above.
- **Not available:** the drag preview overlay (no IPC to draw one; the panel
  shows the selection).
- **Log-out.** When Sway exits, its IPC socket closes; the service saves the
  layout last seen and exits.

## Security

The service uses `$SWAYSOCK` or a `sway-ipc.<uid>.*.sock` of this user only
when it is a socket owned by the user in a directory private to the user,
and on every connection it checks (`SO_PEERCRED`) that the listening process
runs as the user. Commands sent to Sway contain only numbers, fixed text and
key names limited to letters, digits and `_` (unit tests
`ids_are_numbers_only`, `combos_refuse_sway_syntax_in_key_names`; E2E SEC7
places a window whose title is Sway command syntax). Anything running as the
same user can use the same socket; Fractile adds no access to Sway beyond
what the user already has.

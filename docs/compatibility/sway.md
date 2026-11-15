# Sway

| Session | Adapter | How |
|---|---|---|
| Sway 1.8 or later (Wayland) | `sway` | The Fractile service talks to Sway's IPC socket (`$SWAYSOCK`, normally `$XDG_RUNTIME_DIR/sway-ipc.<uid>.<pid>.sock`): the window tree, outputs and workspaces, Sway commands, and the event stream. Nothing has to be installed or enabled in Sway. |
| Sway before 1.8 | none | Not supported; `fractile status` says so with the version it found. |

## Tested

| Environment | Result | Evidence |
|---|---|---|
| Sway 1.12 (Arch package) nested inside `kwin_wayland --virtual` (wlroots' Wayland backend, pixman renderer), in an `archlinux:latest` container; native Wayland GTK4 and Qt windows, the real Fractile UI; keys pressed with `wtype` | 28/28 | `tests/evidence/e2e-sway.json` (`tests/sway/e2e-sway-in-container.sh`) |
| Ubuntu 26.04.1 desktop with sway 1.11, swaybar, swayidle and swaylock in a QEMU/KVM VM; real GDM logins into the Sway session, `apt install` of the .deb, keys typed on the VM's keyboard | 9/9 | `tests/evidence/vm-ubuntu26.04-sway.json` (`tests/vm/vm-e2e.sh sway`) |

The suite covers: detection and connection, and an unreachable socket
reported with the reason; window, output and workspace lists; grid
placements on GTK4 and Qt windows (0 px error); a cell smaller than a window's
minimum size (the frame is not shrunk under it); 1x1 filling the work area;
undo; activation; leaving fullscreen to place; global shortcuts as Sway
bindings pressed through Sway's keyboard path; conflicts with bindings of the
user's configuration, in the main file and in a file it includes (reported,
the user's bindings still run); a shortcut id and a window title full of Sway
command syntax staying data; moving windows between workspaces; snapshot
restore; the panel opened by its shortcut, floating, focused and driven with
the keyboard; `swaymsg reload` (Fractile's bindings come back); a second
output and moving a window onto it; that output at scale 1.5 next to one at
scale 1 (its logical size listed, a grid cell on it placed exactly); bindings
removed on exit; a killed
service followed by a new one (found without `SWAYSOCK`); and the service
saving and exiting when Sway exits.

The container run is a real Sway with real clients but not a login: it runs
as a window of a headless KWin, with no bar, portal or display manager. The
VM run is a real login: the service starts from the packaged Sway
configuration, 15 windows of 10 apps (GNOME apps, started from their Exec
line) are restored after a real log-out and log-in and after a reboot (the
prompt answered with Enter), swaylock is seen as a lock and a restore is
refused while it runs, and Fractile's icon is in swaybar's tray and opens the
panel. gnome-terminal is checked by position (see below).

**Not tested:** a login from a TTY (`sway` started by hand), physical
monitors, a fractional scale on a real login, XWayland applications, tabbed or stacked
containers, the scratchpad as "minimize" beyond unit tests, lockers other
than swaylock, and Sway versions other than 1.11 and 1.12.

## Setting it up

1. Install the package.
2. Start Fractile once from the app menu (or `fractile-ui`) and finish the
   first-run setup with "Start at login" on.
3. `fractile status` should show `Window control: available` and adapter
   `sway`.

### Start at login

The systemd user unit starts with `graphical-session.target`. Ubuntu's,
Debian's and Arch's plain "Sway" session never reaches that target (their
`/etc/sway/config.d/50-systemd-user.conf` only imports the environment), so
the unit, although enabled, would never run. The package therefore also
installs `/etc/sway/config.d/60-fractile.conf`:

    exec /usr/bin/fractiled --autostart --compositor-start

Sway's default configuration reads it (`include /etc/sway/config.d/*`). With
`--compositor-start` the service starts only when login start is on for this
user (otherwise it exits at once); when a session manager such as uwsm brings
`graphical-session.target` up within 5 s, it leaves the start to the systemd
unit. A configuration of your own that does not include
`/etc/sway/config.d/*` needs that `exec` line.

### Lock screen

swaylock, gtklock and waylock lock the session through Wayland's
ext-session-lock protocol and do not tell logind (`LockedHint` stays `no`).
Fractile on Sway treats the session as locked while one of them runs for
this user, and then moves, captures and restores nothing on its own. Another
locker is not recognised.

### Starting apps

When Fractile reopens an app on Sway it runs the desktop entry's `Exec` line
(in a systemd scope of its own), as Sway's launcher does, also for entries
marked `DBusActivatable`. On Ubuntu 26.04's Sway session the GNOME 50 apps
started by D-Bus activation exit without a window: the activation
environment lacks `XDG_SESSION_TYPE`, and with it set they look for GNOME's
`org.gnome.Mutter.ServiceChannel`. The same apps open normally from `Exec`.

### Known platform issue (not Fractile)

On a session that never reaches `graphical-session.target`, the user services
that are part of it, such as `xdg-desktop-portal`, are not stopped at log-out.
If the user manager is still running at the next login (lingering, an ssh
session, or a log-in within 10 s of the log-out), that portal, its backend
gone with the old display, makes every GTK app wait 25 s per portal call at
start (seen on Ubuntu 26.04). `systemctl --user restart xdg-desktop-portal`
clears it.

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
- **A window whose minimum size is larger than the cell** keeps its
  minimum. Sway 1.11 grows the floating container to the window, and
  Fractile leaves it there. Sway 1.12 keeps the container at the requested
  size while the window draws past it; Sway's IPC shows nothing of that
  (`geometry` is the window's natural size), so Fractile reports the
  placement as exact.
- **gnome-terminal sets its own size again.** On Sway (seen with Sway 1.11
  and Ubuntu 26.04's gnome-terminal) a gnome-terminal window goes back
  to its default 80x24 whenever its focus changes, in or out, after Fractile
  placed it. The position is kept. Other apps tested do not do this, and
  gnome-terminal does not do it on GNOME or KDE.
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

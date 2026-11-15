# Hyprland

| Session | Adapter | How |
|---|---|---|
| Hyprland with a Lua configuration (Wayland; 0.56.2 and 0.57.2 tested) | `hyprland` | The Fractile service talks to Hyprland's own IPC: the request socket `$XDG_RUNTIME_DIR/hypr/<instance>/.socket.sock` (JSON queries, Lua dispatch expressions) and the event socket `.socket2.sock`. Nothing has to be installed or enabled in Hyprland. |
| Hyprland that takes only the classic `hyprctl dispatch` form | none | Not supported. Fractile's adapter speaks the Lua dispatch form only; at start it sends one empty Lua dispatch, and when Hyprland refuses it `fractile status` says so with the version it found. |

Hyprland 0.56 and 0.57 (as packaged by Arch) read a Lua configuration (`~/.config/hypr/hyprland.lua`) and
evaluates every `hyprctl dispatch` argument as a Lua expression. The classic
forms (`dispatch movewindowpixel ...`, `keyword bind ...`) are refused there
("unknown request" or a Lua syntax error), which is why a Hyprland without
the Lua configuration is not covered by this adapter.

## Tested

| Environment | Result | Evidence |
|---|---|---|
| Omarchy 4.5 (Arch), Hyprland 0.56.2 started by uwsm from SDDM: a real login on a laptop, 1920x1200 at scale 1.5, the user's own Omarchy configuration (261 binds); Arch package from `makepkg` installed with `pacman -U`, service started by hand | works (manual): detection, the scaled monitor as 1280x800 with the bar left out of the work area, all 12 windows placeable, the panel shortcut moved to Ctrl+Alt+G because Omarchy binds Super+Alt+G (conflict reported, Omarchy's bind untouched), the UI, and grid placement from the panel (done and judged by the user) | this page; no automated suite on that machine |
| The same Omarchy laptop, 2026-11-15: the rebuilt Arch package (with the minimum-size, launch and service-unit fixes) installed with `pacman -U` over the earlier one, the running service replaced (`fractiled --replace`) in the live Hyprland 0.56.2 session | works: `fractile status` (adapter hyprland, window control and global shortcuts available), eDP-1 listed as 1280x800 at scale 1.5 with the bar's 22 px left out of the work area, a test foot window placed on the top-right cell of a 2x2 grid with 0 px error (it was tiled; Hyprland floats it), the service with umask 0077 and no no-new-privileges or seccomp filter | this page; checked over ssh, no automated suite |
| Hyprland 0.57.2 (Arch package) nested inside `kwin_wayland --virtual`, in an `archlinux:latest` container with the host's NVIDIA render node; native Wayland GTK4 and Qt windows, the real Fractile UI; keys pressed with `wtype` | 27/27 | `tests/evidence/e2e-hyprland.json` (`tests/hyprland/e2e-hyprland-in-container.sh`) |
| A real Hyprland 0.57.2 login on Arch Linux in a QEMU/KVM VM (llvmpipe, 1600x1200): GDM 51 timed logins into Arch's Hyprland session (no uwsm), waybar, hyprlock through hypridle, `pacman -U` of the Arch package; the Hyprland configuration starts waybar, hypridle and `fractiled --autostart --compositor-start` with `hl.on("hyprland.start", ...)` | 10/10 on the last two runs (2026-11-15): install, set-up, start at login, 15 windows of 10 apps placed on a 4x4 grid, restored after a real reboot (prompt answered with Enter) and after a real log-out (`hl.dsp.exit()`) and log-in, hyprlock, the waybar tray and its panel, launched apps without the service's restrictions | `tests/evidence/vm-arch-hyprland.json` (`tests/vm/vm-e2e.sh hyprland`) |

The suite covers: detection and connection, and an unreachable instance
reported with the reason; window, output and workspace
lists; grid placements on GTK4 and Qt windows (0 px error); 1x1 filling the
work area; undo; activation; leaving fullscreen to place; global shortcuts as
Hyprland binds pressed through Hyprland's keyboard path; a conflict with a
bind of the user's own configuration (reported, the user's bind untouched); a
shortcut id full of Lua syntax staying data; moving windows between
workspaces; snapshot restore; the panel opened by its shortcut, floating,
focused and driven with the keyboard; `hyprctl reload` (Fractile's binds come
back); a second monitor and moving a window onto it; that monitor at
1920x1200 scale 1.5 next to one at scale 1 (listed as 1280x800, a grid cell
on it placed exactly); binds removed on exit;
a killed service's binds replaced by the next one (found without
`HYPRLAND_INSTANCE_SIGNATURE`); and the service saving and exiting when
Hyprland exits.

The nested suite is a real Hyprland with real clients, but **not** a real Hyprland login:
it runs as a window of a headless KWin (Hyprland cannot start without a DRM
or Wayland backend), with no bar, no portal for Hyprland, no uwsm and no
display manager.

The harness sets `input.resolve_binds_by_sym = true` in the test
configuration. `wtype` uploads its own keymap with keysyms at arbitrary
keycodes, so Hyprland must match binds by keysym for the harness key presses
to reach them. A physical keyboard needs no such setting.

The Omarchy run found one bug: the UI did not start, because Omarchy puts
mise's shims first on the session's PATH and the UI's `#!/usr/bin/env
python3` took mise's Python, which has no PyQt6. The packaged UI now runs on
`/usr/bin/python3`.

The VM run found three bugs, fixed: GNOME Calculator (whose minimum size is
larger than its cell) was pushed above the screen; GNOME Disks aborted when
Fractile reopened it, because the systemd unit's RestrictSUIDSGID was
inherited by every app Fractile starts; and when Hyprland exited, the display
closing and SIGTERM each saved a Logout snapshot. In Arch's Hyprland session
(started by GDM, without uwsm) `graphical-session.target` became active, so
the systemd unit started the service and the `--compositor-start` line
stepped aside.

**Not tested:** autostart through uwsm, the nested suite's shortcut and
panel checks on a real login (the VM suite covers the tray, not the
shortcuts), multiple physical monitors, XWayland applications, window
groups, special (scratchpad) workspaces, a hardware GPU on a real login
(the VM uses llvmpipe), and Hyprland versions other than 0.56.2 and 0.57.2.

## Setting it up

1. Install the package.
2. The systemd user unit starts with `graphical-session.target`. Hyprland
   started through **uwsm** reaches that target and passes its environment
   (`WAYLAND_DISPLAY`, `HYPRLAND_INSTANCE_SIGNATURE`) to user services; enable
   the unit with `fractile autostart on`.
   Started without uwsm, nothing starts `graphical-session.target`. Start the
   service from the Hyprland configuration instead, for example
   `hl.on("hyprland.start", function() hl.exec_cmd("fractiled --autostart --compositor-start") end)`
   (the form shown in Hyprland's generated `hyprland.lua`; the VM suite's
   configuration uses it). With `--compositor-start` it starts only when login start is on
   for this user, and leaves the start to the systemd unit when uwsm brings
   `graphical-session.target` up (the same flag the Sway package snippet
   uses; see sway.md).
3. `fractile status` should show `Window control: available` and adapter
   `hyprland`.

If `HYPRLAND_INSTANCE_SIGNATURE` is not set, the service uses this user's
newest running Hyprland instance in `$XDG_RUNTIME_DIR/hypr`. The UI needs
`WAYLAND_DISPLAY` from the session.

## How Fractile behaves on Hyprland

- **Tiled windows become floating when placed.** Hyprland tiles new windows,
  and a tiled window's size is decided by the layout. Placing a window on the
  grid makes that window floating at the chosen rectangle (the placement
  result says so). Hyprland's layout settings are never changed, and other
  windows stay tiled. `fractile status` lists Hyprland as the tiling manager.
- **Frames include the border.** Hyprland draws the border outside the
  window. Fractile treats the border as part of the frame, so the visible
  window, border included, fills the grid cell (`general:border_size` is read
  at start and after each config reload).
- **Fractile's own windows float.** At start the service adds a runtime
  window rule, `fractile-own-windows`, that floats only Fractile's panel,
  palette, preview and small dialogs (app id `io.fractile.Fractile`, matched
  by title), so opening the panel does not re-tile the desktop. Settings and
  the dashboard are tiled like other windows. The rule is not written to any
  file; Hyprland drops it on a config reload and Fractile adds it again.
- **Shortcuts are runtime binds.** Each global shortcut is added with
  `hl.bind` and the description `Fractile: <shortcut id>`, so it shows up in
  `hyprctl binds`. A key combination your Hyprland configuration already binds
  is reported as a conflict and left alone. Hyprland drops runtime binds on a
  config reload; Fractile adds them again then, unless the reloaded
  configuration now binds the same keys (`fractile diagnose` then lists those
  shortcuts in the adapter notes). On exit they are removed (each
  through its own handle, so a bind of yours on the same keys is never
  touched). Binds left by a service that was killed are replaced by the next
  one.
- **Workspaces** are Hyprland's numbered workspaces (index = id - 1). Moving a
  window to a workspace that does not exist yet creates it, as Hyprland does.
  Special workspaces are not listed; a window on one counts as minimized.
- **Multiple monitors.** A floating window moved by coordinates stays on its
  monitor's workspace in Hyprland. When a placement targets another monitor,
  Fractile first moves the window to the workspace that monitor shows.
- **Not available:** the drag preview overlay (Hyprland has no IPC to draw
  one; the panel shows the selection), minimize and keep-above (Hyprland has
  neither).
- **Lock screen.** Session lock state comes from logind's `LockedHint` and,
  since hyprlock, swaylock, gtklock and waylock do not set it, from one of
  them running for this user. (Verified for swaylock on a real Sway login
  and hyprlock on a real Hyprland login.)
- **Starting apps.** Apps are reopened from their desktop entry's `Exec`
  line, also when it is marked `DBusActivatable`, as on Sway (see sway.md).
- **Log-out.** When Hyprland exits, its event socket closes; the service saves
  the layout last seen and exits.

## Security

The IPC directory is in the user's runtime directory; the shared `/tmp/hypr`
of Hyprland before 0.40 is never searched. The service uses an instance
directory only when it is a real directory owned by the user and writable by
no one else, and on every connection it checks (`SO_PEERCRED`) that the
process listening on the socket runs as the same user (unit test
`socket_peer_must_be_this_user_and_dir_private`). Values
Fractile sends to Hyprland go into Lua string literals that pass only
letters, digits and a few punctuation characters, and write every other byte
as an escape, so a shortcut id or window address cannot end the literal (unit
test `lua_strings_cannot_be_escaped`, E2E SEC7). Anything running as the same
user can use the same sockets; Fractile adds no access to Hyprland beyond
what the user already has.

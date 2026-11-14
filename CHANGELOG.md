# Changelog

## 0.9.0 (unreleased)

First feature-complete pre-release. Validated on nested, headless and container
desktops only. See PLATFORM_MATRIX.md.

### Added

- **Glass panel.** The placement panel is a translucent, rounded slab with a
  rim light. The cells are frosted panes, and the selection is drawn as one lit
  pane spanning its cells. It fades in over 120 ms on X11. On compositors that
  blur translucent windows (Hyprland by default) the slab is frosted. On
  GNOME/Mutter and KWin it is tinted but not blurred. The high-contrast theme
  stays opaque.
- **Grid placement.** A 6x6 panel with drag and keyboard selection. Alt
  subdivision (up to 20x20), Alt+=/- and Alt+Shift+=/- resize the grid, and
  Shift inverts keep-open. A live preview is shown, with per-monitor panels,
  margins and gaps (px or %), and an undo stack. Every placement is verified by
  reading the geometry back, with bounded re-apply and learned window minimum
  sizes.
- **Shortcuts.** Unlimited named shortcuts, local (panel) or global, with
  conflict detection, monitor cycling, and a command palette. They are
  registered through XGrabKey (X11), the GNOME Shell extension (Wayland) or
  KGlobalAccel (KDE), and released on exit.
- **Session restore.** Manual, periodic, logout and shutdown snapshots in SQLite
  (WAL, checksummed, daily backup, rolling retention). Restore is a planned,
  journaled, idempotent transaction: dry run, per-app launch approval
  (ask/always/if missing/never), cancel, retry, resume after a crash, and
  undo-restore. Login policies: off, manual, last clean session, last known
  state, named layout, ask.
- **Layouts and rules.** Named layouts (position existing, launch missing,
  exact N), per-topology variants, and on-create placement rules with
  explanations.
- **Adapters.** X11 (EWMH, ICCCM, RandR with EDID ids). GNOME Shell extension
  for Wayland (45-48 declared; see PLATFORM_MATRIX.md for the versions run). KWin script for Plasma Wayland (6.x,
  6.8.2 tested). Hyprland with a Lua configuration (0.56 or later) through Hyprland's own
  IPC (nothing to enable; 0.57.2 tested nested, 0.56.2 on Omarchy by hand). Sway 1.8 or later through Sway's IPC
  (1.12 tested, nested only). COSMIC and other Wayland compositors (river,
  Wayfire, ...) are reported as unsupported, with reasons.
- **UI.** A PyQt6 panel, settings (General, Appearance, Shortcuts, Session,
  Rules, Monitors, Diagnostics, About), dashboard, first-run wizard, recovery
  dialog and tray icon.
- **CLI.** `fractile` covers everything the UI does, with stable exit codes.
- **Packaging.** `.deb`, Arch PKGBUILD, systemd user unit, XDG autostart with
  TryExec, desktop entry, AppStream metainfo. The GNOME extension and KWin
  script are shipped disabled.
- `fractile purge-data` removes per-user data. Package removal keeps it.
- Security: same-uid-only socket (0600, SO_PEERCRED, hello), integrations
  authenticated by peer executable (and cap_sys_nice for KWin), imports never
  auto-launch, no shell, no telemetry.
- Session lock handling through logind LockedHint and the compositor.
- logind PrepareForShutdown snapshot with a delay inhibitor.
- Shortcut editor on Wayland: while it listens, Fractile pauses its own
  global shortcuts so the desktop passes the keys to it. The pause ends when
  the editor closes or disconnects, and after at most 300 s (S14-S17).
- Tray icon at login: with the tray icon on, the service starts the UI in the
  background, so the icon appears without opening a window. On a desktop
  without a tray host the background UI exits again; the panel still opens
  on request (Y01-Y11).
- Monitor changes: when a monitor goes away, the layout it had is kept as a
  "monitor setup" snapshot. With "return windows when a monitor comes back"
  (off by default), reconnecting it puts the windows back (D06-D08).
- Browsers and editors that reopen their own windows (Firefox, Chrome,
  Chromium, Brave, Edge, Vivaldi, Epiphany, VS Code, VSCodium, Zed, JetBrains
  IDEs) are started once by a restore, and Fractile places the windows they
  open. A per-app setting overrides the built-in list (T14, A01-A03).

- **Hyprland.** Window list, outputs and workspaces from Hyprland's JSON
  queries; placement through Lua dispatch (a tiled window becomes floating
  at the chosen rectangle, border included in the frame, verified by reading
  it back); shortcuts as runtime Hyprland binds, re-added after a config
  reload, reported as a conflict when the Hyprland configuration binds the
  same keys, removed on exit; Fractile's panel floats through a runtime window
  rule; moving a window onto another monitor moves it to that monitor's
  workspace; the service exits with a saved layout when Hyprland exits. No
  preview overlay on Hyprland. See docs/compatibility/hyprland.md.
- **Sway.** Window tree, outputs and workspaces from Sway's IPC; placement
  with `floating enable, resize set, move absolute position` on the one
  window, verified by reading the frame back and waiting until the client
  stops changing its size; shortcuts as runtime `bindsym ... nop <token>`,
  re-added after a config reload; keys bound in the Sway configuration (and
  the files it includes, with `set $var`) are reported as conflicts instead of
  being silently replaced; minimize is the scratchpad; the service exits with
  a saved layout when Sway exits. No preview overlay. See
  docs/compatibility/sway.md.

### Fixed during validation

- **Closing and reopening the panel quickly crashed the UI (X11).** The
  position re-check added for GNOME Xorg ran up to 1.2 s after the panel
  opened. If the panel was reopened within that time, the old window had been
  deleted, and the check's error aborted the UI (PyQt6 aborts on errors in
  timers). The timers now skip deleted windows. Found by the new G21 run
  under KWin.
- **The Alt finer grid could not be used with a mouse on Plasma 5 X11.**
  kwin_x11 5.27 takes Alt+click for moving windows by default, so a drag with
  Alt held never reached the panel. Tapping Alt (press and release) now keeps
  the finer grid until you tap again or the panel closes. Holding works as
  before. The panel's help line says so.
- **Setup said the panel's own shortcut was "already used".** The wizard
  checked its suggestions without saying they were for the panel shortcut, so
  the panel's current binding was reported as taken by "Show/hide panel".
  The shortcut editor now also takes focus explicitly when it starts
  listening. If another program holds the keyboard, it says so instead of
  waiting silently, and it logs whether the keyboard grab was taken.
  `FRACTILE_DEBUG_KEYS=1` logs each key it receives. This is for a
  recording that timed out on a Pop!_OS desktop reached over VNC: the key
  never arrived, and the cause is not found yet.
- **An upgrade left the old service running.** A package replaces
  `/usr/bin/fractiled`, but the running service kept executing the old binary
  until the next login, and a package cannot reliably restart a per-user
  service. The service now notices that its binary was replaced and restarts
  itself in place (same pid, so systemd keeps tracking it) once it is idle:
  never while the panel is open or a restore is running. It is not treated as
  a logout: there is no logout snapshot, and the login session carries on. Set
  `FRACTILE_NO_SELF_RESTART=1` to turn this off. Regression test:
  `tests/gui/service-upgrade-in-container.sh`.
- **An upgrade left the old UI running.** After reinstalling on Pop!_OS and
  Omarchy, the UI started before the upgrade kept drawing the old panel until
  it was killed, because a service restart does not end the UI. The UI now
  notices when its installed files change and restarts onto the new code. When
  idle it restarts within about 40 s, but never while one of its windows is
  open. If the panel shortcut comes first, it restarts and opens the new panel.
  It waits until the files stop changing, so an upgrade still in progress is
  left alone. Regression test: `tests/gui/ui-upgrade-in-container.sh`.
- **"Follow system" ignored a dark desktop on Pop!_OS.** GNOME was set to
  prefer dark, but Qt 6.4 with qt5ct/qt6ct handed the UI a light palette, so the
  panel and grid were light. The UI now reads the desktop's light/dark setting
  from the XDG desktop portal (`org.freedesktop.appearance color-scheme`) and
  uses its own dark or light palette when Qt disagrees. It also follows the
  switch live. Two related fixes: switching back to "Follow system" now
  restores the system palette, and the text size no longer grows on every
  configuration reload.
- A layout entry matching on app id alone (no desktop file, no WM_CLASS)
  never matched its window, because the score needed was above what an app id
  alone can reach. Such an entry now matches exactly that app id (X32).
- Login restore with "last clean session" or "last known state" could find no
  snapshot when more than 200 newer manual saves existed, because the newest
  200 rows were fetched before filtering by kind. The filter now runs in the
  query (store test, VM power-cut run).
- A layout applied in "position existing" mode with every app closed reported
  the run as failed; it is now reported as partial, with each window skipped.
- Hyprland: the service now checks the socket owner (`SO_PEERCRED`) on every
  connection and no longer falls back to the shared `/tmp/hypr` directory.

- KDE kept Fractile's shortcuts as dead entries after exit. They are now
  unregistered on exit (K17).
- The recovery dialog and wizard could cover the panel (parentless QDialog
  stacking). They are now top-level windows.
- A KWin script unload went unnoticed (the script shares KWin's bus
  connection). A poll heartbeat detects it now.
- Windows with a minimum size larger than the cell were placed far off. Their
  limits are now learned and the constraint policy applied (M06).
- An integration with the wrong adapter API showed only "not connected". The
  refusal and the fix are now reported (T19).
- Automatic snapshot retention ran only after periodic captures. It now runs
  after every automatic snapshot.
- Logging out while the login restore was still running lost the layout: the
  next login restored nothing. A login restore is now treated as in progress
  from service start, no exit snapshot is taken of a half-restored desktop, and
  the next login restores what that session was restoring (X28).
- Logging out while any restore was still running lost what the user did in
  that session, and each such logout left the run marked "running", which the
  failure breaker counts as a crash: after three, automatic restore paused
  itself. Now the restore is cancelled first, the exit snapshot keeps the live
  state of what the restore finished and the restore target of what it had
  not, and the run ends as cancelled (P09; failed before the fix, see
  `tests/evidence/perf-x11-p09-before-fix.json`).
- A restore cancelled before it reached an open window reported that window
  as SKIPPED ("no desktop id") instead of CANCELLED.
- The shortcut editor showed a raw X11 error dump when another application
  held the combination. It now says "another application already uses this
  key combination" (S05).
- The Shortcuts list showed "Show/hide panel: no keys set" while the panel
  shortcut (set under General) was active. The list now shows the panel
  shortcut with its keys and status; Edit opens General (S13).
- GNOME Shell crashed (SIGSEGV) when a window closed while Fractile was
  still placing it. The extension now stops at once for a window being
  unmanaged (W24; before the fix see
  `tests/evidence/e2e-gnome-wayland-w24-before-fix.log`).
- GNOME 47 lists a new window before Mutter has placed it (frame 0x0 at
  0,0), and the first placement was then undone by Mutter's initial
  placement. New windows are now offered once Mutter has shown them (W04 on
  GNOME 47; before the fix see
  `tests/evidence/e2e-gnome-wayland-gnome47-before-fix.json`).
- A window grown to its minimum size near a monitor edge could be moved by
  the compositor onto another monitor. The grown rectangle is now kept inside
  the work area it was meant for (D02, GNOME 47 logical layout mode).
- On GNOME 45 (Mutter physical layout mode), moving a window to a monitor
  with another scale could report a transiently rescaled frame (twice or half
  the requested size, or left on the old monitor). Fractile learned that as
  the window's minimum size, and every later placement of that window came
  out too large. Such frames are now re-requested (twice, the second after a
  short pause) and never learned from, and a learned minimum the window later
  undercuts is dropped (D02 on GNOME 45).
- A window whose minimum was only a pixel or two over the cell, and which the
  compositor therefore did not move (GNOME 47, a 73 px minimum for a 71 px
  cell), stayed on the wrong monitor: the growth was within the size
  tolerance, so no minimum was learned. When the move was skipped, any growth
  is now learned (D02 on GNOME 47).
- A service stopped very early in a login could miss the stop signal and lose
  the exit snapshot. Signal handlers are now installed before the service
  accepts connections.
- A running kwin_x11 (Plasma X11) was taken for KWin Wayland.
- Apps installed after the service started were not found for a restore
  until the next start. The app index is now rebuilt when an application
  directory changes.
- A launch that failed at once (for example a failing `systemd-run`) was
  counted as started, and the restore waited 30 s for a window. The failure
  is now reported with the program's own error, and a failing `systemd-run`
  wrapper falls back to starting the app directly.
- When two desktop entries claimed the same window class (Chrome ships
  `google-chrome.desktop` and `com.google.Chrome.desktop`), which one a window
  resolved to depended on hash order, so a restore could launch a different
  entry. The user's own entries win, then the entry named after the class.
- The shortcut dialog clipped the grid under a two-line message.
- Started by the systemd user unit (the default autostart), the service is
  outside any login session and has no `XDG_SESSION_ID`, so every login of a
  boot had the same session key. After a log-out and log-in nothing was
  restored, and the lock state was unknown. The service now asks logind for
  the user's graphical session (`User.Display`) when the environment does not
  name one. Found in a real log-out and log-in in the Ubuntu VM (V06); the
  simulated relogins set `XDG_SESSION_ID` and could not catch it.
- On GNOME no logout snapshot was saved: gnome-session stops GNOME Shell
  before systemd stops the service, so the capture at SIGTERM found the
  extension gone. Fractile now registers with gnome-session and takes the
  logout snapshot when the session ends (EndSession), before the shell stops;
  it answers at once when asked whether the logout may go ahead. If a capture
  at exit still fails because the compositor is gone, the layout last seen in
  the session (at most about 10 s old) is saved instead, and the log says so
  (V06 in the Ubuntu VM).
- On Plasma 5.27 a log-out does not stop `graphical-session.target`, so
  the service kept running after the X server had gone, and the next login
  started no new one (no window control until a reboot). The unit is now also
  bound to `plasma-workspace.target` (stopped at every Plasma log-out, started
  at every login), autostart adds the new link on upgrade, and the service
  exits, saving the layout last seen, when its X display closes (V03 in the
  Plasma VM).
- In the "Restore your windows from last time?" dialog, focus started on the
  first row's checkbox. In the Ubuntu 24.04 GNOME session, pressing Enter to
  accept toggled that checkbox, and nothing was restored. The dialog now
  starts on "Restore selected", and Enter on a row also restores. Space still
  toggles a row. Found in a real login in the Ubuntu VM.
- Apps with `DBusActivatable=true` were started with `gio launch`, which
  sends the activation and disconnects about 1 ms later. The bus drops a
  pending activation whose sender is gone, so with GLib 2.88 (Ubuntu 26.04)
  the app started without a window and quit about 10 s later. Fractile now
  activates such apps with `gdbus call`, which waits for the reply (Plasma 6
  VM run).
- KWin reports `<app id>.desktop` as a window's desktop file even when no
  such file is installed (GNOME Disks: `gnome-disks.desktop`, installed as
  `org.gnome.DiskUtility.desktop`), so a restore could not launch it. A
  reported id that is not installed is now resolved from the other evidence.
- A restore of several windows of a single-instance app (Nautilus) opened
  only one: activating a running app raises its window. When the app already
  has a window (from this restore, or reopened by the desktop's own session
  restore), Fractile now uses the app's "new window" desktop action.
- On Plasma 6 Wayland no logout snapshot was saved: Plasma closes every
  window before systemd stops the service, and sends it nothing. When windows
  close while KSMServer reports that the session is ending, Fractile saves
  the layout last seen as the logout snapshot. The layout last seen is now
  also refreshed right after a restore finishes (V06 in the Plasma 6 VM).
- Right after a launch, KWin can report a placement before the new window
  has applied the size. Fractile took the unchanged size for the window's
  minimum and kept it at that size (GNOME Text Editor at 700x520 for a
  400x288 cell). When the observed size is still the window's previous size,
  Fractile now asks again after short pauses (up to about 1.75 s) and learns a
  minimum only if the size never moves (V05 in the Plasma 6 VM).
- The KWin script did not keep a reference to its timers. KWin 6.6's
  JavaScript engine can collect such a timer before it fires (reproduced in
  the VM: 0 of 200 fired after an allocation burst), and a placement then
  never answered ("KWin integration did not answer 'move_resize'"). Timers
  are now held until they fire (200 of 200), and an error while a placement
  settles is reported instead of leaving the call open.
- The packaged UI started with `#!/usr/bin/env python3`, so a version
  manager's Python first on the PATH (Omarchy's mise shims) ran it, without
  PyQt6, and the UI did not start. The .deb and the Arch package now run it
  on `/usr/bin/python3` (`FRACTILE_PYTHON` changes that at build time).
- Hyprland 0.56.2 already reads a Lua configuration and takes the Lua
  dispatch form; the docs said 0.57 or later. The adapter checks for the
  Lua form, not a version, and works on 0.56.2 (Omarchy).
- After a service restart two UIs could stay attached: the service started
  one for the tray while the UI opened from the menu was still reconnecting,
  and both answered the panel shortcut (two panels on every monitor; seen on
  Pop!_OS with GNOME on Xorg). A UI started only for the tray now leaves when
  another UI is attached, and of two tray-only UIs the newer one leaves
  (`tests/gui/ui-restart-in-container.sh`).
- On GNOME Shell 46 with Xorg and two monitors (Pop!_OS, seen on a real
  desktop), the panel for the second monitor opened on the first, at 120,120:
  a frameless window asking for a position on another monitor than the
  primary is mapped there, and a move after the map is honoured. Two grids
  showed on the first monitor, and a cell picked in the misplaced one moved
  the window to the other monitor. On X11 the UI now checks its panels and
  the command palette after they are mapped and moves them back.
- Test harnesses: the nested X11 desktop leaked D-Bus-activated host services
  and FUSE mounts, and `xdotool --sync` could hang forever. Both are fixed.

### Known issues and limits

- Real logins, log-out and reboot are validated on Ubuntu 24.04 GNOME
  (Wayland and Xorg sessions), Plasma 5.27 X11 and Ubuntu 26.04 Plasma 6.6.6
  Wayland, in a VM. Sway and Hyprland have no real login test. Mixed DPI and monitor
  disconnect/reconnect are validated on headless GNOME (virtual monitors
  changed through DisplayConfig); physical monitors were used by hand only
  (Pop!_OS GNOME Xorg with two monitors, Omarchy at scale 1.5). The tray is
  validated on GNOME with the AppIndicator extension only.
- On Plasma and other desktops without gnome-session, the logout snapshot is
  taken when systemd stops the service. Apps in KDE's own session management
  may have closed by then (Plasma reopens those itself). On Plasma 6 Wayland
  every window is closed first; the layout last seen is saved instead. If the display is
  already gone, the layout last seen (up to about 10 s old) is saved.
- COSMIC: no window control (platform limitation).
- Hyprland: the automated suite runs nested inside a headless KWin only. A
  real Omarchy login (Hyprland 0.56.2, uwsm, scale 1.5) was checked by hand;
  autostart and the log-out snapshot there are not tested. A Hyprland without
  the Lua configuration is not supported. Without uwsm the systemd unit does not start; start
  `fractiled --autostart` from the Hyprland configuration.
- Sway: tested nested inside a headless KWin only (Sway 1.12), not in a real
  Sway login. `bindcode` bindings in the Sway configuration are not checked
  for conflicts, and bindings added at runtime by other tools are invisible to
  Fractile (Sway's IPC cannot list bindings).
- Google Chrome and Chromium incognito windows cannot be detected (no marker
  in the title or the window properties) and are recorded in snapshots like
  normal windows. Firefox private windows are left out.
- Fractile cannot restore application contents.
- If the service is stopped within milliseconds of starting at a new login,
  before the login flow has picked a snapshot, the next login may find no
  snapshot to restore.
- There is no Settings control for the placement constraint policy (config
  only).
- A window the user moves during a restore, before the restore reaches it, is
  moved to its restore target (the restore does not detect manual moves).
- The "Ask" login dialog offers the most recent snapshot only. Others are
  reachable from the Dashboard.

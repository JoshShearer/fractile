# Changelog

## 0.9.0 (unreleased)

First feature-complete pre-release. Validated on nested, headless and container
desktops only. See PLATFORM_MATRIX.md.

### Added

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
  6.8.2 tested). COSMIC and unknown Wayland compositors are reported as
  unsupported, with reasons.
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

### Fixed during validation

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
- In the "Restore your windows from last time?" dialog, focus started on the
  first row's checkbox. In the Ubuntu 24.04 GNOME session, pressing Enter to
  accept toggled that checkbox, and nothing was restored. The dialog now
  starts on "Restore selected", and Enter on a row also restores. Space still
  toggles a row. Found in a real login in the Ubuntu VM.
- Test harnesses: the nested X11 desktop leaked D-Bus-activated host services
  and FUSE mounts, and `xdotool --sync` could hang forever. Both are fixed.

### Known issues and limits

- Real logins, log-out and reboot are validated on Ubuntu 24.04 GNOME
  Wayland only (VM). X11 and KDE logins are simulated. Mixed DPI and monitor
  disconnect/reconnect are validated on headless GNOME (virtual monitors
  changed through DisplayConfig), not with physical monitors. The tray is
  validated on GNOME with the AppIndicator extension only.
- On Plasma and other desktops without gnome-session, the logout snapshot is
  taken at SIGTERM. If the compositor is already gone by then, the layout
  last seen (up to about 10 s old) is saved. Not tested on a real Plasma
  log-out.
- COSMIC: no window control (platform limitation).
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

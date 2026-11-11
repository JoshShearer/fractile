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
  for Wayland (45-48 declared, 46 tested). KWin script for Plasma Wayland (6.x,
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
- Test harnesses: the nested X11 desktop leaked D-Bus-activated host services
  and FUSE mounts, and `xdotool --sync` could hang forever. Both are fixed.

### Known issues and limits

- Not validated on real logins, real logout/reboot, mixed DPI, monitor hot-plug,
  the tray, Plasma X11, or GNOME 45/47/48.
- COSMIC: no window control (platform limitation).
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

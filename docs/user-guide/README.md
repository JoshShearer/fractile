# Fractile user guide

Fractile puts windows on a grid. You open the panel, drag across the cells you
want, and the window moves and resizes to exactly that rectangle. It also saves
your window layout and can put it back after you log in again.

Fractile 0.9.0. Where your desktop is supported is in
[PLATFORM_MATRIX.md](../../PLATFORM_MATRIX.md). In short: GNOME (X11 and Wayland),
KDE Plasma on Wayland, Hyprland 0.56 or later (with its Lua configuration) and Sway 1.8 or later work. On COSMIC only session restore works, without
positions or sizes, because COSMIC has no API for moving or resizing other
programs' windows (see [compatibility/cosmic.md](../compatibility/cosmic.md)).

## Install

- **Ubuntu / Pop!_OS 24.04:** `sudo apt install ./fractile_0.9.0-1_amd64.deb`
- **Arch:** `makepkg -si` in `packaging/arch/` (the PKGBUILD builds from the
  release tarball)
- **From source:** `packaging/debian/build-deb.sh` builds the .deb, and
  `packaging/arch/make-srctar.sh` makes the tarball for the PKGBUILD.
  `packaging/install.sh <destdir> [prefix]` is the shared file-layout
  installer both use.

Then:

- **GNOME on Wayland:** log out and back in once, then enable the extension in
  the Extensions app (or `gnome-extensions enable fractile@fractile.io`).
- **KDE Plasma:** enable "Fractile" in System Settings > Window Management >
  KWin Scripts.
- **GNOME on X11:** nothing more.
- **Hyprland:** nothing to enable. Fractile uses Hyprland's own IPC. With
  uwsm, `fractile autostart on` starts it at login; without uwsm, start
  `fractiled --autostart` from your Hyprland configuration. Windows you place
  become floating. Details: [compatibility/hyprland.md](../compatibility/hyprland.md).
- **Sway:** nothing to enable. Fractile uses Sway's IPC. If your Sway session
  starts `graphical-session.target` (uwsm or a distribution's sway session),
  `fractile autostart on` starts it at login; otherwise add
  `exec fractiled --autostart` to your Sway configuration. Windows you place
  become floating. A shortcut on keys your Sway configuration already binds is
  reported as a conflict, and your binding keeps working. Details:
  [compatibility/sway.md](../compatibility/sway.md).

Start Fractile from the application menu. The first time, a short setup asks
for a panel shortcut. It only offers keys that are free on your desktop. It also
asks whether to start at login and whether to restore your windows at login.
Both default to the safe choice and can be changed later.

`fractile status` tells you whether window control works and, if it does not,
what to do.

## The panel

Open it with your panel shortcut, the tray icon (if your desktop has a tray),
or by launching Fractile again from the menu.

The tray icon is on by default. Stock GNOME has no tray; Ubuntu and Pop!_OS
add one with the AppIndicators extension, and KDE has one. Where there is a
tray, the icon appears at login without opening a window; its menu has the
panel, Settings, the Dashboard and "Save workspace now". Where there is none,
nothing is shown and the shortcut still works. Turn the icon off in Settings >
General. It shows which window it will
move, with its name and icon. That window is chosen when the panel opens, so the
panel taking focus does not change the target.

| To | Do |
|---|---|
| Place the window | drag across cells and release |
| Place with the keyboard | arrow keys move the cursor, Shift+arrows extend the selection, Enter places |
| Cancel | Esc (nothing moves) |
| Keep the panel open for this placement | hold Shift while placing (inverts "close after placing") |
| Finer grid, temporarily | hold Alt (6x6 becomes 12x12, 10x10 becomes 20x20) |
| More / fewer columns | Alt+= / Alt+- |
| More / fewer rows | Alt+Shift+= / Alt+Shift+- |
| Use a panel-local shortcut | defaults: Ctrl+arrows for halves, M to fill, C to center |

While you drag, a preview shows exactly where the window will go. The window
itself does not move until you release. The grid is 6x6 by default. It goes from
1x1 to 10x10, or to 20x20 while Alt is held. With "panel on all monitors" on,
each monitor gets its own panel. Selecting on another monitor's panel moves the
window there.

If the window cannot take the size (some apps have a minimum size), Fractile
fits it as closely as the app allows and tells you. Undo (the global undo
shortcut or `fractile undo`) puts the last placement back.

## Shortcuts

Settings > Shortcuts lists named shortcuts. There is no limit. Each has a name,
an action (a grid placement, a snap such as "left third", move to the next
monitor, undo, save or restore workspace, the command palette) and a key.

- **Local** shortcuts work only while the panel is open, and keys the panel
  itself uses cannot be assigned to them.
- **Global** shortcuts work everywhere. The editor checks for conflicts with
  your desktop's bindings and with Fractile's own. It tells you when it cannot
  check, which happens for bindings held by other programs.
- While the editor waits for your keys, Fractile's own global shortcuts are
  paused, so pressing one of them reaches the editor instead of running it
  (on Wayland the desktop would otherwise take those keys). They come back
  when the editor closes, or after 5 minutes at most.
- "Cycle monitors": pressing the same placement shortcut again moves the
  window to the same spot on the next monitor.

The command palette (global shortcut, or `fractile palette`) finds any action by
typing.

## Margins and gaps

Settings > Appearance: margins at the screen edges (each side separately) and
gaps between windows (horizontal and vertical separately), in pixels or
percent. Both default to off, with 10 px when turned on (range 0-300 px), as in
Divvy.

## Saving and restoring your workspace

A **snapshot** records which apps are open, where each window is, on which
monitor and workspace, and its state (maximized and so on). It does **not**
record what is inside the windows. Fractile cannot bring back unsaved documents,
browser tabs or terminal contents. Apps that restore their own sessions (most
browsers, many editors) still do that themselves. Fractile then only places
their windows.

For such apps a restore starts the app **once** and places the windows it
reopens, instead of starting it once per window (which would open extra
windows). Firefox, Chrome, Chromium, Brave, Edge, Vivaldi, Web (Epiphany),
VS Code, VSCodium, Zed and JetBrains IDEs are treated this way by default.
Change it per app in Settings > Session Restore > Applications ("Restores its own
documents"). If the app does not reopen a window it had, the restore report
says so; Fractile does not open a blank one in its place.

- Save now: `fractile save --name Work`, the "Save current workspace" shortcut,
  or the Dashboard.
- Restore: the Dashboard, or `fractile restore --snapshot latest`. Try
  `--dry-run` first to see what would happen to each window: already open,
  will be launched, needs approval, or cannot be placed.
- Windows that are already open are moved. They are never closed and never
  opened twice.
- Apps that are not running are launched only with your approval. Each app has
  a policy: ask (the default), always, if missing, or never. Set it in Settings > Session Restore >
  Applications, or approve it in the restore dialog.
- Undo a whole restore with "Undo restore" (`fractile undo-restore`).

### At login

Settings > Session Restore > Restore > "At login":

| Policy | What happens |
|---|---|
| Off | nothing is saved or restored |
| Manual (default) | only your explicit saves; nothing at login |
| Last clean session | autosaves every 10 minutes when something changed and at logout; restores after the next login |
| Last known state | like the above, but also after a crash or power loss (asks first by default) |
| Named layout | applies a chosen layout at every login |
| Ask | shows the most recent snapshot at login with each window listed: "Restore selected", "Not now", or "Don't restore automatically". Other snapshots are on the Dashboard. |

"Start Fractile when I log in" (Settings > General) is a separate switch.

If you log out while a restore is still running, Fractile stops it and saves
what you had: windows the restore had already placed are saved where they are
now (including any you moved since), and windows it had not reached yet are
saved at their restore position. The next login restores that. One limit: if
you move a window by hand before the restore reaches it, the restore moves it
to its saved position.

### When a monitor goes away or comes back

When a monitor is unplugged or turned off, your desktop moves its windows to
the remaining monitors, and Fractile keeps the layout you had as a "monitor
setup" snapshot. Restoring a snapshot from a setup with more monitors puts the
windows of a missing monitor on the remaining ones, at the same relative
position. With Settings > Session Restore > "Move windows back when a monitor is reconnected"
on (it is off by default), reconnecting the monitor puts its windows back
where they were. Monitors are recognized by their EDID identity, not by the
port they are plugged into.

Retention: the newest 30 automatic snapshots, plus one per day for 7 days. Your
named and manual saves are kept until you delete them. While the screen is
locked, nothing is captured and nothing is restored.

## Layouts and rules

A **layout** is a named arrangement you reuse, for example "Coding": editor on
the left two thirds, terminal on the right. Save one from your current windows
(`fractile layout save Coding`) and apply it from the Dashboard, the palette or
`fractile layout apply Coding`. A layout can just position existing windows,
launch missing apps first, or restore exactly N windows per app. It can have
variants for different monitor setups.

**Rules** (Settings > Rules) place new windows automatically when they open,
matched by app id, window class, role, monitor, workspace or instance number.
Title matching is available but off unless you enable it. Rules are applied in
order, and "Test rule" explains which rule matches a window and why.

## Command line

`fractile --help` lists everything. A few examples:

```
fractile status                          # does window control work here?
fractile place --grid 3x1 --cells 0,0,1,0    # left two thirds of the focused window's monitor
fractile snap right-half
fractile move-output next
fractile save --name Work
fractile restore --dry-run --snapshot latest
fractile diagnose > report.txt           # attach to bug reports (titles redacted with logs --redacted)
```

Exit codes: 0 ok, 5 window not found, 6 partial (for example a restore that
needs approvals), 7 invalid input.

## Troubleshooting

- **"Window control: NOT AVAILABLE"**: `fractile status` prints the reason and
  the fix. Usually the GNOME extension or KWin script is not enabled yet. On
  GNOME Wayland, you also need one log out and in after installing.
- **"integration was refused: version mismatch"**: the extension or script
  does not match the installed Fractile, typically after an upgrade. Reinstall
  the package, then log out and in (GNOME) or turn the KWin script off and on.
- **A shortcut does nothing**: Settings > Shortcuts shows shortcuts that could
  not be registered and why. Another program may own the key.
- **A window lands slightly off**: some apps (terminals with character-cell
  sizing, apps with a minimum size) refuse exact sizes. The placement result
  says "constrained by the window". If you prefer a refusal, set
  `fractile config set placement.constraint_policy '"fail"'` (there is no
  Settings control for this in 0.9.0).
- **Automatic tiling is on** (some desktops): Fractile reports it and does not
  change it. A tiler may move windows back.

## Uninstall

Removing the package (`sudo apt remove fractile`, `sudo pacman -R fractile`)
removes the programs, the extension and the KWin script. **Your settings,
snapshots and layouts stay** in your home directory, so a reinstall picks them up.
To remove them too, run this *before* uninstalling:

```
fractile purge-data            # shows what will be removed, then asks
fractile purge-data --dry-run  # only show
```

It stops Fractile, turns off autostart, and deletes `~/.config/fractile`,
`~/.local/share/fractile` and `~/.local/state/fractile`. Exports you saved
elsewhere are not touched. If you already uninstalled, delete those three
directories by hand. A leftover autostart entry is harmless, because it is
skipped when the program is gone.

## Privacy

No telemetry, no network access. Data stays in the three directories above.
See [security](../security/README.md).

### Private windows

Fractile leaves a window out of snapshots when its title matches one of the
patterns in `session.private_title_patterns` (by default "Private Browsing",
"Incognito", "InPrivate" and "Private window", any case). This works for
browsers that put such a marker in the window title, for example Firefox
("... Mozilla Firefox Private Browsing").

Google Chrome and Chromium do not: an incognito window is titled like a normal
one ("Page - Google Chrome") and has no window property that tells it apart
(checked with Chrome 157 on X11). Fractile cannot tell these windows apart,
so it cannot leave them out. Instead it never stores a title for these
browsers: Chrome, Chromium, Brave, Edge and Vivaldi are listed in
`session.titles_never_stored_for`, and their windows are recorded with their
position and size only. An incognito window therefore puts nothing from its
pages into a snapshot. The cost is that Fractile has no title to tell two
windows of the same browser apart on restore, so it matches them by order.
To store titles for one of these browsers anyway, remove it from the list.
Chrome never reopens incognito windows, so a restore reports a recorded
incognito window as not reopened and does not open it.

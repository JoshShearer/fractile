# COSMIC

**Status: PARTIAL.** On COSMIC Fractile lists windows, moves them between
workspaces, maximizes them and restores sessions without positions or
sizes. Window **placement** (the grid panel, snapping, shortcuts, layouts
placed by position) is **BLOCKED BY PLATFORM**: COSMIC offers clients no way
to move or resize another application's window.

## What was checked (2026-11-15)

- **cosmic-comp 1.11.0** (Arch `cosmic-comp 1:1.11.0-1`, `cosmic-session
  1:1.11.0-1`). The Wayland globals an ordinary client of a real COSMIC login
  sees, listed with `wayland-info` from a user service:
  `zcosmic_toplevel_info_v1` v3, `zcosmic_toplevel_manager_v1` v4,
  `ext_foreign_toplevel_list_v1`, `ext_workspace_manager_v1` v1,
  `zxdg_output_manager_v1` v3, `wl_output` v4 and `wl_seat`.
- The protocol definitions (crate `cosmic-protocols` 0.2.0, MIT):
  - `zcosmic_toplevel_info_v1` v3: per toplevel its state (maximized,
    minimized, activated, fullscreen, sticky), outputs, workspaces and a
    `geometry` event (position and size relative to the output). **Read
    only.**
  - `zcosmic_toplevel_manager_v1` v4: `activate`, `close`, set/unset
    maximized, minimized, fullscreen and sticky, `move_to_ext_workspace`,
    and `set_rectangle`. `set_rectangle` is the *minimize animation hint*: it
    tells the compositor where a taskbar icon is. It does not position the
    window.
  - No request in any global sets a toplevel's position or size. COSMIC has
    no scripting or extension mechanism that runs code inside the compositor,
    unlike GNOME Shell extensions and KWin scripts.
- Earlier (2026-11-14) the Pop!_OS 24.04 host's packaged COSMIC
  (`cosmic-comp 0.1~1793802426~24.04~ec674cd`) was checked the same way, from
  the interface names in the binary: the same toplevel protocols, also with
  no geometry request.

## Tested in a real COSMIC login

`tests/vm/vm-e2e.sh cosmic` (E16 in PLATFORM_MATRIX.md): Arch Linux in a
QEMU/KVM VM, llvmpipe, GDM 51 timed logins into the COSMIC session,
`pacman -U` of the Arch package, COSMIC's auto-tiling switched on
(`~/.config/cosmic/com.system76.CosmicComp/v1/autotile`), and a real reboot.
Evidence: `tests/evidence/vm-arch-cosmic.json` and
`tests/evidence/vm/cosmic-*.png`.

| Check | Result |
|---|---|
| C01 | GDM logs in to the COSMIC session; cosmic-comp runs |
| C02 | Started from the app menu, Fractile uses the COSMIC adapter; `fractile status` and `diagnose` say window placement is not available and why, and that session restore covers apps, workspaces and states only; COSMIC auto-tiling is detected |
| C03 | The monitor, both workspaces and the text editor window (app, workspace, and a frame on the monitor) are listed |
| C04 | `fractile place` and `fractile snap` fail with the reason; nothing on screen moved |
| C05 | No file under `~/.config/cosmic` changed while Fractile listed, refused, moved to a workspace, maximized and saved; auto-tiling still on (T18) |
| C06 | The tray icon is registered with COSMIC's status area |
| C08 | A click on the tray icon shows "Fractile can't place windows on this desktop" with COSMIC's reason, not a grid that cannot work |
| C09 | On request the editor is moved to the second workspace and maximized; a saved snapshot records that workspace and state |
| C07 | After a real reboot the systemd unit starts the service at login |
| C10 | After the reboot Fractile reopens the approved editor once, on the second workspace, maximized; the item reports RESTORED_WITHOUT_GEOMETRY with "position and size not restored" |
| C11 | Restoring the same snapshot again opens nothing new and claims no placement |

On the final build the suite passed 13/13 in 2 of 2 runs, and 3 of 3 on
the build before it (which waited 40 s instead of 60 s for late states).
Earlier builds failed it and were fixed: a tray click showed nothing; a
maximize for a window on a workspace not in view was confirmed by COSMIC
only 11 to 38 s later (or dropped right after a workspace move), so restore
reported a false rejection; and setting maximized before the workspace move
let the move un-maximize the window while the item said "state restored".
Once, the editor the test starts with `gio launch` never started; the test
now retries that launch.

**Not tested:** a hardware GPU (the VM uses llvmpipe), Pop!_OS's own COSMIC
packages in a real login (the host's COSMIC was checked from its protocol
list only), multiple monitors on COSMIC, fullscreen and sticky restore,
XWayland windows, and COSMIC with auto-tiling off.

## What Fractile does on COSMIC

- The COSMIC adapter is an ordinary Wayland client of the session. It uses
  `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` for the
  window list, `ext_workspace_manager_v1` for workspaces, and
  `zcosmic_toplevel_manager_v1` for workspace moves, maximize, minimize,
  fullscreen, sticky and activation. Each request is checked against the
  state COSMIC reports back. A workspace move not confirmed within 1.5 s is
  reported as rejected. A state change is sent again once and given 4.5 s.
  For a window on a workspace that is not in view, cosmic-comp applies a
  maximize at once (the window takes the maximized size) but reports the
  new state only 11 to 38 s later (measured in the VM, llvmpipe); the error
  then says "COSMIC has not confirmed it yet" rather than claiming either
  outcome.
- Operations it reports as unsupported, with the reason: move/resize and
  move to another monitor ("COSMIC has no client API to move or resize other
  applications' windows ..."), global shortcuts, and the preview overlay.
  "Keep above" is not offered by COSMIC's protocol. The work area is the
  whole monitor, because COSMIC does not tell clients where its panel and
  dock are.
- `fractile status`, `diagnose`, the Diagnostics page and the first-run
  wizard say what works. Opening the panel (tray, CLI) shows that windows
  cannot be placed here and why, instead of a grid.
- **Session restore without geometry.** Snapshots record each window's app,
  workspace and state (and the position COSMIC reports, for reference). A
  restore reopens approved apps (the same approval and duplicate rules as on
  every desktop), moves each window to its recorded workspace and then sets
  maximized, fullscreen or sticky (in that order: moving a maximized window
  onto a tiled workspace un-maximizes it). States COSMIC has not confirmed
  are checked again at the end of the run, all together, for up to 60 s;
  an item whose state never shows up fails with the reason
  (`FAILED`, or `LAUNCHED_UNPLACED` for an app Fractile started). Each item reports
  `RESTORED_WITHOUT_GEOMETRY` with "position and size not restored (this
  desktop has no way to set them)"; the UI and notifications say so too.
  It is never reported as placed. This status is an addition to the list in
  SRS 6.2 step 11: none of those statuses describes "reopened and on the right
  workspace, but not positioned" without claiming a placement
  (`LAUNCHED_AND_PLACED`, `PLACED_ONLY`) or a failure (`LAUNCHED_UNPLACED`).
- It never reads or changes COSMIC's settings (SRS T18). It reads
  `autotile` only to warn that auto-tiling is on.
- It does not use maximize or workspace moves to *imitate* grid placement.
  Grid placement stays unavailable; the operations COSMIC does offer are used
  only for what they are.

## Upstream proposal

The smallest change that would let Fractile (and gTile-style tools) work on
COSMIC without weakening Wayland's security model:

**Add a privileged `set_geometry` request to `zcosmic_toplevel_manager_v1`**
(new protocol version):

```xml
<request name="set_geometry" since="N">
  <description summary="request a new position and size for a toplevel">
    Ask the compositor to place the toplevel at the given rectangle in the
    global compositor space (logical pixels). The compositor may adjust it
    to the window's size constraints and must then send the updated
    geometry through zcosmic_toplevel_info_v1. If the window is tiled, the
    compositor should either float it (if 'float' is set) or reply with
    an error event; it must not change the user's tiling settings.
  </description>
  <arg name="toplevel" type="object" interface="zcosmic_toplevel_handle_v1"/>
  <arg name="x" type="int"/>
  <arg name="y" type="int"/>
  <arg name="width" type="int"/>
  <arg name="height" type="int"/>
  <arg name="flags" type="uint" enum="geometry_flags" summary="e.g. float"/>
</request>
```

along with a capability bit in the manager's `capabilities` event.

**Security.** `zcosmic_toplevel_manager_v1` can already close, activate and
move windows between workspaces, so it is a privileged global. cosmic-comp
implements `wp_security_context_v1`, which is how a compositor tells sandboxed
(for example Flatpak) clients apart. The new request would inherit whatever
gate cosmic-comp applies to the manager. How exactly cosmic-comp filters
privileged globals per client was not checked here. This matches the
trust GNOME and KDE give to extensions and scripts, without loading code into
the compositor.

**Tiling.** On a tiled workspace the request should fail with a reason, or float
the window when the client explicitly asks. The user's auto-tile setting is
never toggled by a client.

Also useful but not required: a per-toplevel "is tiled / floating" state in
`zcosmic_toplevel_info_v1`. Clients could then explain why a placement was
refused.

If this lands, the COSMIC adapter would send the new request for move/resize
and verify it against the `geometry` events it already reads, as the KWin
adapter does.

Filed upstream on 2026-11-14 as
[pop-os/cosmic-protocols#97](https://github.com/pop-os/cosmic-protocols/issues/97)
(related: #72, a user question about scripting window moves on COSMIC).

## What COSMIC users can use today

- The test host has GNOME sessions installed alongside COSMIC
  (`/usr/share/wayland-sessions/gnome.desktop`, `ubuntu.desktop` and others).
  In a GNOME session, Fractile works through the GNOME adapter (see
  [gnome.md](gnome.md)).
- On COSMIC itself: COSMIC's own tiling and its keyboard shortcuts for halves and
  maximize, and Fractile's session restore without positions or sizes.

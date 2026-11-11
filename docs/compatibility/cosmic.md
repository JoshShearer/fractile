# COSMIC (Pop!_OS 24.04 COSMIC session)

**Status: BLOCKED BY PLATFORM.** Fractile 0.9.0 offers no window control on COSMIC.

## What was checked

- Host: Pop!_OS 24.04 LTS with `cosmic-comp 0.1~1793802426~24.04~ec674cd` and
  `cosmic-session 1.11.0~1792525615~24.04~4b0a0a2` installed. This is the
  packaged COSMIC desktop.
- The Wayland globals compiled into `/usr/bin/cosmic-comp` (from the protocol
  interface names in the binary). The relevant ones:
  - `zcosmic_toplevel_info_v1`, `ext_foreign_toplevel_list_v1`: list toplevels
    with title, app id, state, outputs and workspaces. **Read-only.**
  - `zcosmic_toplevel_manager_v1`: requests `close`, `activate`,
    `set_maximized`/`unset_maximized`, `set_minimized`/`unset_minimized`,
    `set_fullscreen`/`unset_fullscreen`, `set_rectangle`, `move_to_workspace`,
    `move_to_ext_workspace`, `set_sticky`/`unset_sticky`. `set_rectangle` is the
    *minimize animation hint*: it tells the compositor where a taskbar icon is.
    It does not position the window.
  - `zcosmic_workspace_manager_v2` / `ext_workspace_manager_v1`: workspaces.
  - `zwlr_layer_shell_v1`: lets a client draw panels and overlays, so a Fractile
    panel could be drawn.
- No global in the list sets a toplevel's position or size. COSMIC has no
  scripting or extension mechanism that runs code inside the compositor, unlike
  GNOME Shell extensions and KWin scripts.
- The ordinary Wayland client rules apply: a client cannot position other
  clients' windows, and XWayland gives no control over native Wayland windows.

**Not tested:** a real COSMIC login session. The user session on the test host
is GNOME on X11, and the agent cannot log into another session. Detection was
tested by running `fractiled --check` with `XDG_CURRENT_DESKTOP=COSMIC` on a
Wayland session type. It reports adapter `cosmic`, every operation
`unsupported`, and the reason shown below.

## What Fractile does on COSMIC

- It detects COSMIC (from `XDG_CURRENT_DESKTOP` or a running `cosmic-comp`) and
  reports every window operation as unsupported, with this reason:
  "Fractile has no COSMIC adapter: COSMIC has no client API to move or resize
  other applications' windows (its toplevel management protocol covers activate,
  close, minimize, maximize, fullscreen, sticky and workspace moves only), so
  window control is not offered on COSMIC."
- `fractile status`, `diagnose` and the Diagnostics page show this. Placement
  requests fail with an explanation and never report a false success.
- It never reads or changes COSMIC's auto-tiling settings (SRS T18). There is
  nothing to fight, because Fractile does not move windows there.
- It does not use maximize, minimize or workspace moves to *imitate* grid
  placement. Only "maximize" matches a grid cell (the full 1x1 grid), and a
  partial imitation would look like support that does not exist.

### Could a partial COSMIC mode be offered?

A partial mode was considered and rejected for 0.9.0. Using
`zcosmic_toplevel_manager_v1` (activate, maximize, minimize, workspace moves) and
`zcosmic_toplevel_info_v1` (window list), Fractile could offer maximize,
move-to-workspace and the read-only half of session snapshots. That would mean
taking snapshots that cannot be restored to their geometry. It would add a
Wayland client implementation that can only be tested in a real COSMIC session,
which is not available here. It stays NOT IMPLEMENTED until a geometry hook
exists. The adapter interface (`CompositorAdapter`) already supports
per-operation support levels, so such a mode needs no core changes.

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

If this lands, the Fractile COSMIC adapter would mirror the KWin adapter. It
would be an out-of-process Wayland client using the manager for placement and
the info protocol for the window list and events, with the same capability
reporting and placement verification.

This proposal has **not been filed upstream yet**. That is an action for the
maintainers (see [IMPLEMENTATION_PLAN.md](../../IMPLEMENTATION_PLAN.md)).

## What COSMIC users can use today

- The test host has GNOME sessions installed alongside COSMIC
  (`/usr/share/wayland-sessions/gnome.desktop`, `ubuntu.desktop` and others).
  In a GNOME session, Fractile works through the GNOME adapter (see
  [gnome.md](gnome.md)).
- On COSMIC itself: COSMIC's own tiling and its keyboard shortcuts for halves and
  maximize.

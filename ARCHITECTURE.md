# Fractile architecture

Component-level detail (IPC protocol, restore pipeline, data model) is in
[docs/architecture/README.md](docs/architecture/README.md). This file holds the
overview, the adapter model and the decision records.

## Overview

```
 ┌──────────────┐  JSON-lines over      ┌──────────────────────────────────────┐
 │ fractile-ui  │  $XDG_RUNTIME_DIR/    │ fractiled  (Rust, per-user service)  │
 │ PyQt6: panel,│  fractile/fractile.   │  daemon: config, shortcuts, panel    │
 │ settings,    │◄─sock (0600, peer-   ►│   state, placement + verification,   │
 │ dashboard,   │  cred, hello)         │   undo, rules, autostart, logind     │
 │ wizard, tray │                       │  fractile-session: capture, SQLite   │
 └──────────────┘       ▲               │   store, restore plan/execute, apps  │
 ┌──────────────┐       │               │  fractile-core: geometry, outputs,   │
 │ fractile CLI │───────┘               │   identity, snapshot/layout schemas  │
 └──────────────┘                       │  CompositorAdapter (one active):     │
                                        │   x11 │ gnome bridge │ kwin bridge │ │
                                        │   stub (cosmic / unknown Wayland)    │
                                        └────────┬───────────────┬─────────────┘
                     same socket, bridge_attach  │               │ session D-Bus io.fractile.Bridge1
                         ┌───────────────────────▼──┐   ┌────────▼───────────────┐
                         │ GNOME Shell extension    │   │ KWin script (JS)       │
                         │ (GJS, inside gnome-shell)│   │ (inside KWin), long-   │
                         │ Mutter window API, key   │   │ polls; + KGlobalAccel  │
                         │ grabs, preview overlay   │   │ for shortcuts          │
                         └──────────────────────────┘   └────────────────────────┘
```

- **fractiled** owns all state and all decisions. The UI and CLI are thin
  clients, and the compositor integrations are thin executors.
- **One adapter per session**, chosen at start from `XDG_SESSION_TYPE`,
  `XDG_CURRENT_DESKTOP` and the running compositor (`fractiled --check` prints
  the choice).
- Integrations hold no policy. They answer typed calls (`list_windows`,
  `move_resize`, ...) and push events (window created, focus, outputs changed).

## Adapter model

`fractile_core::adapter::CompositorAdapter` defines the operations from SRS 3:
`list_windows`, `get_active_window`, `get_outputs`, `get_workspaces`,
`get_geometry`, `move_resize`, `set_state`, `move_to_output`,
`move_to_workspace`, `activate`, `subscribe_events`, `register_hotkey`,
`show_overlay`, `get_permissions`, plus `session_locked`.

Every adapter returns `Capabilities`: for each operation, `full`,
`partial(note)` or `unsupported(reason)`, and an `api_version`
(`ADAPTER_API_VERSION = 1`). Core code checks capabilities before acting and never
assumes an operation exists. Errors are typed (`unsupported`, `not_found`,
`rejected`, `timeout`, `disconnected`, ...) and reach the user as such.

| Adapter | Where | Status |
|---|---|---|
| `x11` | in-process, `adapters/x11` (x11rb) | GNOME/Mutter X11 VERIFIED (nested); Openbox smoke |
| `gnome` | `BridgeAdapter` in the daemon + `adapters/gnome-shell/fractile@fractile.io` | GNOME 46 Wayland VERIFIED (headless) |
| `kwin` | `BridgeAdapter` + `kwin_dbus.rs` + `adapters/kwin/fractile` + `kglobalaccel.rs` | KWin 6.8.2 Wayland VERIFIED (container, virtual backend) |
| `cosmic` | `StubAdapter`: every operation unsupported, with the reason | BLOCKED BY PLATFORM |
| `none` | `StubAdapter` for unknown Wayland compositors | reports unsupported, never fakes support |

## Decision records

**ADR-1: Rust service, Python/Qt UI, compositor-native integrations.**
The service is long-running and security-relevant, and needs a small idle
footprint (measured 10.6 MiB RSS, 0.0% idle CPU). Rust fits that. The UI needs
rich widgets, accessibility and fast iteration, so PyQt6 (system packages on both
target distros) runs in a separate process that may exit when idle. Window control
on Wayland is only possible inside the compositor, so integrations are written
in each compositor's own extension language (GJS, KWin JS).
*Rejected:* QML/C++ UI (more build surface for no measured gain); a Rust GUI
toolkit (weaker accessibility and IME support today).

**ADR-2: One private socket, authenticated by kernel credentials.**
All clients use one Unix socket in a 0700 runtime directory. Peers are checked
with SO_PEERCRED, and the protocol starts with a `hello`. Integrations are
additionally authenticated by the executable of the peer process. *Rejected:* a
public session-bus API. Bus names are not access control, and every session
process could call it. KWin is the exception: its scripts can only make outgoing
D-Bus calls, so a D-Bus endpoint exists there, and each call is authenticated by
the caller's pid (see [docs/security](docs/security/README.md)).

**ADR-3: Verify every placement.**
Window managers and apps may adjust requested geometry: size hints, minimum
sizes, client-side decoration shadows. After each `move_resize` the service
reads the geometry back, reports the error in pixels, re-applies within a bounded
stabilization window, and learns per-window minimum sizes it cannot exceed.
Placement results are never reported from the request alone.

**ADR-4: Logical-pixel geometry in a pure core.**
Grid math, margins, gaps, normalization and monitor remapping live in
`fractile-core::geometry` / `output`, with no I/O, and are property-tested
(`crates/fractile-core/tests/properties.rs`). Adapters convert to and from
the compositor's coordinate space.

**ADR-5: SQLite (WAL, synchronous=FULL) with checksummed snapshot payloads.**
This makes snapshot writes crash-consistent, with "latest good" skipping
corrupt rows, plus a daily online backup. *Rejected:* JSON files with
rename-on-write. Restore runs need a transaction log, and SQLite gives that.

**ADR-6: Restore is a planned, journaled, idempotent transaction.**
`plan()` matches snapshot targets to live windows: deterministic ids first,
titles only as a tie-breaker, one-to-one assignment. It classifies each target
(already present, launch, needs approval, cannot place). `execute()` records
each item under a run id, so a crash mid-restore resumes without duplicate
launches (X21). Launching is opt-in per app, and imports force `ask`.

**ADR-7: Never imitate unsupported platforms.**
On COSMIC and unknown Wayland compositors, Fractile reports "unsupported" with
the reason. It does not approximate grid placement with maximize or workspace
moves, and it never changes tiling settings.

**ADR-8: Automatic snapshots never record a half-restored desktop as if it were the user's.**
Periodic snapshots are skipped while a restore is pending or running. At exit:
- before the login restore has started (it waits for the desktop to settle),
  no exit snapshot is taken. The session has recorded which snapshot it was
  going to restore, and the next login restores that one (X28);
- while a restore is running, the service cancels it, waits up to 3 s for it
  to stop, and saves a merged exit snapshot: live state for every window the
  restore had finished (so later user changes survive), and the restore
  target for every record it had not finished, matched to live windows with
  the restore matcher so no app is recorded twice
  (`restore::interrupted_overlay`). The run ends as cancelled, which the
  failure breaker does not count (P09).

**ADR-9: Integrations ship disabled.**
Packages install the GNOME extension and KWin script, but the user enables
them. Fractile reports how to enable them and never toggles desktop settings
itself.

## Process lifecycle

- `fractiled` is single-instance per user: a second start finds the live
  socket and exits (X02). It runs from the systemd user unit
  (`PartOf=graphical-session.target`, `KillMode=process`) when a user manager is
  available, otherwise from an XDG autostart entry with `TryExec`.
- The UI is started on demand (panel shortcut, launcher, tray) and is
  single-instance. Launching it again shows the panel (G17).
- Launched apps run in their own transient systemd scopes, so they are not
  part of Fractile's unit and survive a Fractile restart.
- On SIGTERM or logind `PrepareForShutdown` (with a delay inhibitor), the
  service cancels a running restore, takes the exit snapshot (ADR-8), releases
  the inhibitor, unregisters its shortcuts, and exits.

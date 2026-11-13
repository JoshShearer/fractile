# Fractile internals

The overview and the decision records are in [ARCHITECTURE.md](../../ARCHITECTURE.md).
This page covers components, protocols and data.

## Source layout

| Path | What |
|---|---|
| `crates/fractile-core` | Pure logic, no I/O: grid and placement geometry (`geometry.rs`), outputs and remapping (`output.rs`), window model and identity scoring (`window.rs`, `identity.rs`), config schema and validation (`config.rs`), snapshot and layout schemas (`snapshot.rs`, `layout.rs`), rules (`rules.rs`), accelerators (`accel.rs`), `.desktop` parsing (`desktop.rs`), the `CompositorAdapter` trait (`adapter.rs`). Property tests in `tests/properties.rs`. |
| `crates/fractile-ipc` | Socket setup, SO_PEERCRED, JSON-lines framing, a blocking client |
| `crates/fractile-session` | Snapshot capture with privacy rules (`capture.rs`), SQLite store (`store.rs`), app registry and identity evidence (`apps.rs`), safe launching (`launch.rs`), restore planning and execution (`restore.rs`). `tests/restore_sim.rs` runs restores against a simulated desktop. |
| `crates/fractile-daemon` | `fractiled`: the daemon state (`daemon.rs`), the RPC server (`ipc.rs`), adapter selection (`backend.rs`), the GNOME/KWin bridge (`bridge.rs`), KWin's D-Bus endpoint (`kwin_dbus.rs`), KGlobalAccel (`kglobalaccel.rs`), logind lock and shutdown (`logind.rs`), autostart (`autostart.rs`), session flows (`session.rs`), and the log with title tagging (`logging.rs`). |
| `crates/fractile-cli` | `fractile`, the command-line client |
| `adapters/x11` | the X11 adapter (x11rb: EWMH, ICCCM, RandR with EDID, XGrabKey) |
| `adapters/hyprland` | the Hyprland adapter (Hyprland's IPC sockets: JSON queries, Lua dispatch, the event socket; runtime binds for shortcuts) |
| `adapters/sway` | the Sway adapter (Sway's i3-compatible IPC socket: the tree, outputs, workspaces, commands and events; runtime `bindsym` for shortcuts, checked against the user's configuration) |
| `adapters/gnome-shell/fractile@fractile.io` | the GNOME Shell extension (GJS) |
| `adapters/kwin/fractile` | the KWin script (JS) |
| `adapters/cosmic` | empty: see [compatibility/cosmic.md](../compatibility/cosmic.md) |
| `ui/fractile_ui` | the PyQt6 UI: panel and grid, overlay, settings, dashboard, wizard, palette, recovery dialog, tray |
| `packaging/` | `install.sh` (shared layout), `.deb` builder, PKGBUILD, systemd user unit, desktop entry, metainfo, container package tests |
| `tests/` | E2E suites (`integration/`, `gui/`, `kwin/`, `hyprland/`, `sway/`, `perf/`, `vm/`), tools (`tools/`), evidence (`evidence/`) |

## RPC protocol

- Socket: `$XDG_RUNTIME_DIR/fractile/fractile.sock`, mode 0600, in a 0700
  directory that must be owned by the user and have no group/other access.
- Framing: one JSON object per line, at most 1 MiB.
- Request: `{"id": n, "method": "...", "params": {...}}`. Response:
  `{"id": n, "result": ...}` or `{"id": n, "error": {"kind", "message"}}`.
  Events: `{"event": "...", "data": {...}}` to subscribed clients.
- The first request must be `hello` with `protocol: 1`.
- Methods: `status`, `diagnose`, `capabilities`, `list_windows`, `list_outputs`,
  `list_workspaces`, `active_window`, `place`, `snap`, `compute_rect`,
  `move_to_output`, `move_to_workspace`, `set_state`, `activate_window`, `undo`,
  panel control (`toggle_panel`, `show_panel`, `hide_panel`, `panel_state`), UI
  registration and own-window placement, config (`get_config`, `set_config`
  with full config or `patch`, `reset_config`), shortcuts (`run_shortcut`,
  `shortcut_check`), autostart, snapshots (`snapshot_save/list/get/delete/pin/import`),
  restore (`restore_plan/start/status/run/cancel/retry/undo`), layouts
  (`layout_list/get/save/from_windows/delete/preview/apply`), rules
  (`rules_test`, `rules_apply_now`), apps (`apps_list`, `app_policy_set`),
  `logs`, `quit`, and for integrations `bridge_attach`, `bridge_result`,
  `bridge_status`.
- Errors are typed: `invalid`, `not_found`, `unsupported`, `denied`, `locked`,
  `constraints`, `busy`, `store`, ... The CLI maps them to exit codes 5 (not
  found), 6 (partial) and 7 (invalid).

## Bridge protocol (GNOME, KWin)

The daemon side is `BridgeAdapter`. It implements `CompositorAdapter` by sending
`{"call": id, "op": "move_resize", "args": {...}}` to the integration and waiting
up to 5 s for `{"call": id, "ok": true, "value": ...}` or an error. The
integration pushes events (`window_created`, `window_closed`, `focus_changed`,
`outputs_changed`, `hotkey`, ...).

- GNOME: the extension sends `hello` and then `bridge_attach` on the normal
  socket. Calls and results then flow on that connection.
- KWin: the script calls `Attach(json)`, then loops on `Poll()`, which returns
  queued calls (or `[]` after 4 s). It answers with `Result(json)` and pushes
  `Event(json)`. While a `Poll` is outstanding the script is alive; 3 s without
  one detaches it.

`bridge_attach` carries `kind`, `api`, the per-operation support levels, the
compositor version and notes. Attachment is refused for a wrong kind, a wrong
API version (the reason stays visible in status), a peer process that is not the
compositor, or a second live integration.

## Placement pipeline

1. **Target.** The panel captures the target window id when it opens. A placement
   targets that id, not "whatever is focused now". Fractile's own windows are
   never targets.
2. **Rect.** `compute_rect(grid, selection, output)`: work area minus margins,
   split into cells with gaps between them, in logical pixels. Remainders are
   distributed deterministically, so adjacent selections tile without overlap or
   holes.
3. **Constraints.** Known size hints and learned minimum sizes are applied by
   `constraint_policy`: fit (default), center, or fail.
4. **Apply.** `move_resize` through the adapter, after unmaximizing if needed.
   The undo stack (depth 20) records the previous frame.
5. **Verify.** Read the frame back. If it is off by more than
   `tolerance_px` (2), re-apply up to `max_reapply` (3) times within
   `stabilization_ms` (1500). If the window clearly refuses (for example a
   minimum size), learn that limit and re-plan once.
6. **Report.** `requested`, `observed`, `max_error_px`, `within_tolerance`,
   attempts, and a note. The service records the timing (diagnose: p50/p95).

## Restore pipeline

1. **Readiness.** Wait until the window list and output topology are stable
   (three equal samples 500 ms apart), up to `readiness_timeout_s`. There is no
   fixed sleep.
2. **Plan** (`restore::plan`). Remap recorded outputs to current ones: stable
   (EDID) id, then connector plus size, then connector, then geometry overlap,
   then the nearest or primary output. Score every target against every live
   window, using desktop id, app id, WM_CLASS, role and instance ordinal; titles
   only break ties. Solve a one-to-one assignment. Classify each target:
   already open, will reopen, needs approval, not found, cannot restore
   geometry, or excluded. A dry run stops here.
3. **Baseline.** Capture a `pre_restore` snapshot for "undo restore".
4. **Execute.** Place present windows. Launch approved missing apps (at most
   `max_concurrent_launches` at once, each with `per_app_timeout_s`), wait for
   their windows through adapter events, and place them. Every item's result is
   journaled in `restore_items` under the run id.
5. **Converge or report.** Bounded by `total_timeout_s`. The result is a per-item
   status (`ALREADY_PRESENT`, `LAUNCHED_AND_PLACED`, `PLACED_ONLY`,
   `LAUNCHED_UNPLACED`, `SKIPPED`, `TIMED_OUT`, `UNSUPPORTED`, `AMBIGUOUS`,
   `FAILED`, `CANCELLED`) with a reason. It can be cancelled. Failed items can be retried. A
   crash mid-run resumes the same run, using the journal to avoid duplicate
   launches (X21).

## Data model

SQLite at `~/.local/share/fractile/fractile.db` (WAL, `synchronous=FULL`):

| Table | Holds |
|---|---|
| `snapshots` | id, created_at, name, mode (`manual`, `periodic`, `logout`, `shutdown`, `pre_restore`, `layout`), JSON payload, SHA-256 checksum, content fingerprint, counts, pinned |
| `layouts` | named layouts (JSON) |
| `restore_runs`, `restore_items`, `launches` | the restore journal |
| `sessions` | one row per login session: key, boot id, start, end, clean flag, restored flag |
| `meta` | small key/value state (baselines, saved restore requests, the snapshot each login restored) |

The snapshot payload follows SRS section 11: schema version, outputs with stable
ids (EDID hash where available), workspaces, and per-window desktop id, app id,
WM_CLASS, role, instance ordinal, optional title hint, output, workspace,
normalized and absolute geometry, state flags, launch policy and
`restore_content: application_managed`. It holds no process environment, no
command line, and no window contents.

Config: TOML at `~/.config/fractile/config.toml`, schema-validated. An
invalid file is kept untouched, defaults are used, and the error is reported
(X23).

# Divvy for Linux — Grid Window Manager & Workspace Session Restore

**Software Requirements Specification (SRS), Technical Architecture, Delivery Plan, and Claude Code Master Prompt**  
**Version:** 1.0 | **Date:** 2026-11-10 | **Status:** Implementation-ready specification; platform capabilities require validation  
**Target platforms:** Ubuntu, Pop!_OS, Arch Linux; GNOME, COSMIC, KDE Plasma; X11 and Wayland where supported  
**Primary reference:** [Divvy for Windows User Guide](https://mizage.com/downloads/DivvyWinHelp.pdf) (19 pages)  
**Working name:** GridKeeper (replaceable; avoid using the third-party Divvy brand in release branding)

---

## 0. Executive goal and non-negotiable rules

Create a polished, reliable, lightweight, native Linux window-placement utility matching **all relevant Divvy for Windows workflows**, plus substantially improved application launching, named workspace layouts, automatic desktop snapshotting, session restoration after logout/reboot/crash, and robust multi-monitor support. The result must be a distributable product, not an isolated script or demo.

**Goal → Delegate → Verify**: Claude Code shall research the repository and host environment, assign independent workstreams when supported, implement in small verified vertical slices, and prove behavior with automated and real-desktop integration tests. Research and prototyping do not count as production functionality.

**Acceptance policy:** Features must be classified as VERIFIED, PARTIAL, BLOCKED BY PLATFORM, NOT IMPLEMENTED, or NOT TESTED, separately for each tested compositor/OS version. Never state universal Wayland support without compositor-specific evidence. Never claim that an application’s unsaved document buffers can be restored unless its own application API or session system provides it. Never disguise reopening an executable as an exact session restore.

**Security:** Unprivileged user-session app; never run its UI or normal service as root; never use blanket `/dev/input` access or privileged simulated input to evade Wayland security. No telemetry without opt-in. No arbitrary command replay from imported layout files without explicit user approval.

## 1. Source-grounded Divvy compatibility inventory

The baseline is derived from the original Windows manual. During discovery, build a page-by-page traceability table, reproduce every behavior on a Windows reference or document where behavior remains ambiguous, and write executable parity tests.

| ID | Windows Divvy requirement | Linux acceptance criterion |
|---|---|---|
| DV-01 | Background resident utility, one instance | Session service with single-instance UI activation and reliable lifecycle |
| DV-02 | Show panel using tray icon, keyboard shortcut, relaunch | Equivalent entry points when desktop provides tray support; launcher and shortcut always available |
| DV-03 | Panel identifies active application | Active app name/icon displayed; target captured before panel steals focus |
| DV-04 | Default 6×6 selection grid | Default 6 rows × 6 columns with click-and-drag contiguous rectangular selection |
| DV-05 | Grid subdivided by holding Alt | Temporary 2× resolution (e.g., 6×6→12×12), reversible without losing intent |
| DV-06 | 1×1 through 10×10, subdivision up to 20×20 | Exact parity; optional larger grid with bounded UI/performance |
| DV-07 | Keyboard add/remove rows/columns | Defaults Alt+Plus/Minus and Alt+Shift+Plus/Minus, configurable where desktop conflicts |
| DV-08 | Resize + move to selected normalized grid rectangle | Stable pixel-accurate target geometry within compositor/window constraints |
| DV-09 | Escape cancels/dismisses panel | No geometry side effects after cancellation |
| DV-10 | Automatically dismiss after applying | Default dismiss; configurable keep-open mode |
| DV-11 | Shift temporarily flips keep-open behavior | Identical mode toggle for a placement |
| DV-12 | Panel on each monitor | Discover screens; show per-monitor panels where technically supported |
| DV-13 | Move target across monitors via panel selection | Correct destination work area, DPI, offsets, and workspace mapping |
| DV-14 | Unlimited named shortcuts | Persistent named placement commands, import/export and edit/delete |
| DV-15 | Local shortcuts work only while panel visible | Panel-scoped shortcuts with collision-free typing behavior |
| DV-16 | Global shortcuts trigger without opening panel | Desktop-native binding/registration; reject and explain conflicts |
| DV-17 | Repeated shortcut cycles window among monitors | Optional cycle, deterministic monitor order, state reset on context change |
| DV-18 | Start automatically at login | Desktop-compliant user autostart with self-healing status and disable control |
| DV-19 | Optional tray icon | AppIndicator/StatusNotifier where available, fallback launcher/shortcut |
| DV-20 | Optional global show/hide shortcut | System setting with robust focus return |
| DV-21 | Live hover/drag resize preview | Preview selection and final target rectangle, non-interactive overlay if allowed |
| DV-22 | Window-to-window horizontal/vertical gaps | Independently configurable axis gaps, consistent edge accounting |
| DV-23 | Screen-edge left/right/top/bottom margins | Per-side pixels or normalized percentages; original defaults/range supported |
| DV-24 | Default grid size in Appearance | Preserve or reset dynamic grid on next invocation as configured |
| DV-25 | General/Appearance/Shortcuts preference sections | Modern settings tabs containing all corresponding controls |
| DV-26 | Shortcut editor with conflict feedback | Detect known conflicts, warn for unobservable compositor bindings |
| DV-27 | Respects active window and window lifecycle | On panel opening snapshot target ID; graceful closed-window behavior |
| DV-28 | No accidental window modifications during preview | Apply geometry atomically when possible, with undo for last placement |

Original manual facts: default grid 6×6, minimum 1×1, maximum 10×10 with temporary 2× subdivision; per-screen panel; unlimited shortcuts; local/global key scopes; adjustable screen-edge and window gaps; hold Shift to invert panel dismissal; default tray/startup options. The manual’s margin examples use 10px defaults when margins are enabled, and 0–300px adjustment range. Do not copy Divvy brand images/assets or proprietary code.

## 2. New product capabilities beyond Divvy

### 2.1 Session persistence and restore (P0 — core differentiator)

The user can enable **Restore my previous workspace at next login** independently of **Start GridKeeper at login**. Provide separate policies:

1. Off: application never saves or restores windows.
2. Manual snapshot only: explicit **Save Current Workspace** and **Restore Snapshot** actions.
3. Last clean session: autosave periodically and on orderly logout/shutdown where possible, restoring after next login.
4. Last known state: recover a durable snapshot after power loss, crash, or forced reboot, with confirmation policy.
5. Named workspace: launch a chosen saved layout at login, irrespective of previous session.
6. Ask each login: present recent snapshots with preview and **Restore / Skip / Choose another**.

**Snapshot must store:** version/schema, timestamp, desktop environment/compositor, monitor topology and stable EDID-derived identifiers where available, logical/physical dimensions, scaling and transform, work areas, active workspace, ordered workspaces, window identities and match confidence, desktop-file ID, app ID/WM_CLASS, process executable (informational only, not blindly executed), window title/role, window group, instance ordinal, geometry in normalized and absolute logical coordinates, monitor/workspace assignment, stacking order when exposed, focus selection, state flags (maximized/fullscreen/minimized/floating/always-on-top), placement rule identity, launch policy, optional per-app reopen metadata, and restoration diagnostics. Keep snapshots compact and human-exportable with secrets redacted.

**Restoration must:**
- Wait for graphical session and monitor topology readiness without a fixed single sleep.
- Discover already-running applications and windows before launching anything.
- Match deterministic identifiers before fuzzy title matching; never rely on titles alone for exact matching.
- Handle multiple windows from the same application independently with stable signatures and instance counts.
- Launch only applications the user has approved for auto-launch; support per-app policy ALWAYS / IF_MISSING / NEVER / ASK.
- Start using approved `.desktop` IDs/desktop launchers, D-Bus activation, safe argv arrays or user-approved explicitly configured commands; no shell interpolation.
- Prefer native app resume support or recognized app-specific reopen flows for documents/browser windows; otherwise restore only the application/window frame and report limitation.
- Wait asynchronously for window-created events; apply geometry after map/initial placement and re-apply within a bounded stabilization window if the app overwrites it.
- Respect workspace/monitor assignments, window constraints, focus-stealing prevention, and active workspace.
- Handle disconnected monitors using nearest suitable monitor and relative geometry remapping; retain original layout for reconnection.
- Avoid duplicates after service restarts, suspend/resume, session retries, and app-native session restore.
- Support dry run showing **will reopen / already open / not found / cannot restore geometry**.
- Offer cancellation, incremental progress, timeout limits, skip/failure reports, retry failed items and recovery from partially completed operations.
- Persist a transaction log and run ID so retries are idempotent.
- Never close or replace user windows without explicit consent.

### 2.2 Snapshot hygiene and interruption recovery

Snapshots must be journaled/atomic (temporary write + fsync + rename or SQLite WAL transaction), schema-versioned, checksummed, time-limited and backed up. Implement rolling snapshots (defaults: 10-minute periodic capture when changed; max 30 recent, daily history for 7 days, user-configurable). Keep the previous good snapshot if the latest save is incomplete. Capture at logout/shutdown when the desktop exposes a reliable hook, but do not depend on shutdown callbacks: many shutdowns provide little or no time. Include unsaved session/crash flag, login grace period, lock-screen redaction and private-window exclusion. Do not claim in-memory app/document state restoration.

### 2.3 Named layouts, recipes and rules

- Unlimited named layout presets; e.g. Coding, Research, Trading, Communication.
- Window placement rules by app ID, WM_CLASS, title regex (opt-in), role, current workspace, monitor, and window instance ordinal; ordered rule priority and explainable matching.
- Layout recipe can **position existing windows**, **launch missing apps then position**, or **restore exactly N instances**.
- Per-monitor preset variations and responsive mapping when monitor counts/resolutions change.
- Application groups (IDE + terminal + browser) and ordered/delayed dependency launching.
- Layout preview, apply, reset, per-window exclusion; undo last action or full pre-restore backup.
- Workspace/profile selector with favorite presets and optional automatic context switching.
- Floating-window only mode when compositor has tiling manager; do not fight automatic tilers.

### 2.4 Usability extras

- Keyboard-driven selection, mouse drag, high-contrast preview, accessibility labels, keyboard-only operation, configurable grid themes, font scaling, RTL handling, optional on-screen help.
- Snapping keyboard commands (left/right halves, thirds, quadrants, center, maximize), cycle placements, resize increments, move-to-display/workspace.
- Independent profile for laptop, docking station, home monitors.
- Command palette, searchable shortcuts, favorite placements.
- Export/import profiles with validation and merging; safe JSON/YAML where appropriate.
- Optional local CLI and authenticated user-session API for automation.

## 3. Desktop and compositor compatibility strategy

**Fundamental constraint:** On Wayland, ordinary clients cannot enumerate, move, resize or globally intercept arbitrary windows using a standard cross-compositor API. Build a reusable logic engine plus separately shipped *trusted compositor adapters* and expose truthful capability detection. XWayland does not grant control over native Wayland windows.

| Platform / compositor | Required strategy | Caveats |
|---|---|---|
| Ubuntu GNOME Wayland | GNOME Shell extension running inside Shell for window control + restricted user-session IPC service | Extension must track exact GNOME Shell versions; avoid broad public D-Bus surfaces without authorization |
| Ubuntu GNOME X11 (when offered) | EWMH/X11 adapter using libxcb; WM_NORMAL_HINTS and RandR | Windows can reject/snap sizes; focus policies apply |
| Pop!_OS 24.04+ COSMIC Wayland | COSMIC compositor extension/integration or explicitly supported documented IPC/protocol; upstream collaboration if no supported hook | Must research actual compositor APIs; do NOT claim wlr-foreign-toplevel grants geometry control |
| Pop!_OS legacy GNOME/COSMIC extensions (if supported) | GNOME X11/Wayland adapter matched to installed session | Validate legacy/version boundaries |
| Arch GNOME Wayland/X11 | Same GNOME adapter, built/tested against Arch's rolling Shell version | Update compatibility quickly |
| Arch KDE Plasma Wayland | KWin script/plugin exposing policy-limited geometry control | KWin API changes with Plasma versions; package script accordingly |
| Arch KDE Plasma X11 | Prefer KWin script or X11 adapter | Track focus and interaction specifics |
| Arch Sway/Hyprland (optional tier) | Native compositor IPC, separate adapters | Tiling-native layout semantics may preclude exact Divvy floating placement |

Define `CompositorAdapter` capabilities: `list_windows`, `get_active_window`, `get_outputs`, `get_workspaces`, `get_geometry`, `move_resize`, `set_state`, `move_to_output`, `move_to_workspace`, `activate`, `subscribe_events`, `register_hotkey`, `show_overlay`, and `get_permissions`. Every method declares support level and error semantics. Core logic must never assume all operations exist. Maintain adapter API version and compatibility negotiation.

**Wayland security:** GNOME Shell/KWin/COSMIC bridge must enforce same-session access and restrict commands to the owning user, ideally over a private UNIX socket with OS credentials plus an extension-controlled authorization handshake; evaluate D-Bus policy realistically (session-bus names alone are not an access control mechanism). Avoid exposing unguarded global window metadata/control to every session process. Review user-session compromise assumptions; state remaining limitations. Security review is mandatory before release.

**COSMIC-specific warning:** COSMIC auto-tiling and stacking can override requested floating-window geometry. Detect tile/float state and offer explicit policy: prompt to float selected window, pause/avoid auto-tiling for requested windows only if a supported API exists, or refuse with explanation. Never silently disable the user's tiling configuration.

## 4. Architecture

**Recommendation:** Rust core and user-session daemon, Qt 6/QML GUI (or a well-supported native alternative decided by proof-of-concept), small compositor-native bridge extensions in the respective ecosystem languages (GJS for GNOME, JavaScript for KWin where appropriate; Rust for COSMIC if supported). Avoid Electron for a resident window manager unless measurements demonstrate competitive cold-start and idle footprint.

```text
+-------------------------------------------------------------+
| Qt/QML Panel + Settings + Snapshot Viewer + Layout Editor    |
+----------------------------+--------------------------------+
                             | versioned local IPC
+----------------------------v--------------------------------+
| Rust GridKeeper Session Service                               |
| Window Registry | Placement Engine | Layout Rules | Hotkeys    |
| Session Snapshot/Restore | Launch Supervisor | Diagnostics    |
+---------------+---------------------+-----------------------+
                |                     |
       +--------v---------+  +--------v---------------------+
       | SQLite / Config  |  | CompositorAdapter interface  |
       | WAL / snapshots  |  | GNOME / COSMIC / KWin / X11  |
       +------------------+  +------------------------------+
```

Separate concerns:
- **Geometry engine** pure and platform-independent: normalized rectangle math, edge margins, window gaps, minimum size policy, logical coordinate scaling and monitor work area.
- **Window discovery/identity**: event-driven cache, confidence scoring, duplicate instance matching.
- **Action executor**: serializes conflicting placement actions, validates target state, logs outcomes.
- **Restoration engine**: staged plan, launch/match/place/verify, retries with bounded backoff, cancellation and idempotency.
- **Desktop adapters**: only layer that manipulates compositor state.
- **GUI**: no direct privileged operations or process spawning for restoration.
- **Persistence layer**: SQLite for snapshots/events/jobs; typed TOML/JSON config and import/export.
- **Installer**: validated compositor integration, user startup, uninstall and upgrade without losing settings.

Suggested repo:

```text
crates/gridkeeper-core/
crates/gridkeeper-daemon/
crates/gridkeeper-ipc/
crates/gridkeeper-session/
crates/gridkeeper-cli/
ui/qt-qml/
adapters/gnome-shell/
adapters/kwin/
adapters/cosmic/
adapters/x11/
packaging/debian/
packaging/arch/
packaging/aur/
packaging/systemd/
packaging/xdg/
tests/unit/
tests/integration/
tests/gui/
tests/session-reboot/
docs/architecture/
docs/compatibility/
docs/security/
docs/user-guide/
```

## 5. Precision geometry requirements

- Use *logical coordinates* for Wayland GUI-compositor APIs; don't apply DPI scaling twice.
- Target work area excludes dock/panel, system UI, and compositor reservations (X11 `_NET_WORKAREA` as available), not just raw output dimensions.
- Model grid selection as integer inclusive cell bounds and convert to a half-open logical rectangle.
- Use deterministic rounding so adjacent cells remain seamless; distribute remainder pixels without drift.
- Separate outer margins (L/R/T/B) and inter-window gaps; preserve user intent with fractional scaling.
- Define treatment of window decoration, borders and client vs frame geometry; verify observed result after applying.
- On min/max/aspect constraints, report requested vs actual geometry and configurable fit/center/fail policies.
- Handle monitor arrangement with negative coordinates, rotation, mixed scales, portrait screens, docks and changed EDIDs.
- Avoid moving dialogs, modal/tool windows, desktop panels, system critical windows by default.
- Remember pre-placement geometry for undo; do not overwrite a normal window rectangle if restoring fullscreen/maximized until state transition completes.

## 6. Session restore contract and limitations

### 6.1 What the product can promise

**Supported when adapter permits:** Reopen an application and reposition its windows, resize them, assign monitor/workspace, restore state flags, and apply saved layout. Restore app arguments/documents only through explicit safe integration or user-provided commands. Fully restore browser tabs/session only by letting browser's own session recovery do its job, or via a validated browser integration; never infer URLs/private tabs from titles.

**Not generally possible:** Reconstruct arbitrary unsaved in-memory content in apps, exact prior OS process memory, session cookies, app-specific tabs/projects without cooperation, or arbitrary Wayland window placement on unsupported compositors. Display these caveats in UI, diagnostics and release notes.

### 6.2 Matching/launch algorithm

1. Capture pre-restore baseline and create unique restore transaction ID.
2. Validate snapshot schema and reject stale/unsafe commands; enumerate current outputs/windows.
3. Map recorded displays to present outputs by stable identity, topology and fallback geometry.
4. Build stable target records with app ID, WM_CLASS, role, optional title pattern and instance ordinal.
5. Mark currently matched windows; do not relaunch running apps or their extra instances unless required by per-app launch policy.
6. Show dry-run actions for approval if `ASK` mode.
7. Resolve `.desktop` file IDs; launch missing applications using safe APIs, respecting dependencies and rate limits.
8. Subscribe to new-window events before launching; match with scoring and ambiguity handling.
9. Wait for window creation, mapped state and first stable geometry; then place and verify; retry boundedly if overridden.
10. Restore workspaces and visibility safely; focus one user-selected target last; do not move focus continuously as each window starts.
11. Record result per window: `ALREADY_PRESENT`, `LAUNCHED_AND_PLACED`, `PLACED_ONLY`, `LAUNCHED_UNPLACED`, `SKIPPED`, `TIMED_OUT`, `UNSUPPORTED`, `AMBIGUOUS`, or `FAILED`.
12. Persist transaction, update UI and allow retry of only incomplete targets.

**App native restore gotchas:** `code`, `firefox`, `brave`, LibreOffice, terminal emulators, Electron apps, Flatpaks, AppImages and Steam apps vary in multi-instance and document restore behavior. Build per-app strategies only after test evidence; default strategy is safe app activation, not arbitrary `--restore-session` arguments. Launch portal/security boundary behavior must be tested.

### 6.3 Restoration profiles

Provide global and per-profile settings: auto-restore enabled; restore once per new login; restore on monitor reconnect (disabled by default); ignore privacy-sensitive windows; preserve currently open windows; max concurrent app launches; per-app timeout; confirmation requirement; power-loss policy; do-not-restore list; automatic restore delay strategy; disable auto-restart after N failures to prevent loops. A first-run wizard must default to **manual restore** and request opt-in before automatic application launches.

## 7. UI/UX detailed screens

**Quick panel** (Divvy parity): centered on focused screen or independently accessible on all screens; active target icon/title; grid with click-drag, keyboard navigation, temporary subdivision, live rectangle preview, close/settings/pin controls; apply/cancel; tooltip for selection as x/y/w/h percentage and work area; panel never selects itself as target.

**Settings:** General (autostart, tray, hotkey, panel behavior), Appearance (grid default, subdivision, margins, gaps, preview, theme), Shortcuts (global/local with editing and conflict reporting), Monitors (identity/order/layout mapping), Rules (ordered match/action editor), Session Restore (policy and privacy), Applications (relaunch method and permissions), Diagnostics (environment capabilities and trace logs), About/Update.

**Workspace dashboard:** named layouts, Save Current, Preview, Apply to Running Windows, Launch & Apply, Set for Next Login; thumbnail-like wireframe preview showing app icons and grid rectangles, *without window-content screenshots by default*; differences from current session; show monitor/workspace topology.

**Session recovery dialog:** timestamp, detected shutdown mode, count of apps/windows, per-app checkbox, planned launches vs existing windows, warning about app-internal unsaved state, Restore/Skip/Restore Selected, progress timeline and error recovery.

**Rules builder:** drag select rectangle, choose app selector from open windows, match confidence inspection, priority drag ordering, per-display conditional logic, test rule on current windows without saving, revert action.

**First-run compatibility wizard:** detect GNOME/COSMIC/KWin/X11, installed bridge, permissions, global shortcut registration, supported operations and auto-tiling collisions. Offer safe installer actions and explain unsupported features; no green checkmarks without executing a probe.

## 8. Keyboard shortcuts and interoperability

- Assignable panel toggle with default chosen only after collision detection.
- Global and panel-scoped shortcuts in separate registries; globally registered shortcuts must use compositor-approved API/service.
- Required actions: grid placement, monitor cycling, move-to-workspace, save/restore workspace, restore undo, temporary subdivision, search presets.
- Conflict report includes reserved compositor shortcuts and existing app actions when discoverable; unknown collisions are documented as unverified.
- Avoid raw keyboard hooks and blanket evdev grabbing on Wayland. On GNOME/KWin/COSMIC use compositor-specific registration or supported desktop interfaces.
- Headless `gridkeeper status`, `gridkeeper diagnose`, `gridkeeper save --name`, `gridkeeper restore --dry-run`, `gridkeeper restore --snapshot <id>`, `gridkeeper list-windows`, `gridkeeper list-layouts`, `gridkeeper logs --redacted` should remain usable with a graphical session, with clear exit codes.

## 9. Installation, upgrade, and distribution

**Ubuntu/Pop!_OS:** signed `.deb` package and optional apt repository, supported LTS versions explicitly documented, user session service/autostart installation, desktop entry, icons, GNOME Shell extension only on supported versions, and COSMIC adapter where proven. Never replace/reconfigure system window manager during install.

**Arch Linux:** reproducible PKGBUILD (community/AUR route initially), clean build in Arch chroot, explicit dependencies/optdepends for GNOME/KWin/COSMIC, install hooks and user setup; support rolling updates with ABI/capability checks, source package before binary AUR where possible. If offering official pacman repo, sign packages and maintain proper metadata.

**All:** x86_64 first, aarch64 next; debug vs release packages; no root requirement during runtime; optional `systemd --user` lifecycle attached to graphical session, with `XDG autostart` fallback and no double launches; idempotent installer; semver migrations; preserve settings during upgrade; full clean uninstall without deleting user data unless explicitly requested; reliable removal of companion extensions and desktop shortcuts. Do not launch duplicate service instances after reboot.

Installation acceptance:
- Fresh install → first-run wizard → detect adapter → install/enable needed integration with user consent → working hotkey/panel → placement.
- Logout/login → service active once; autostart and restore policies independent.
- Reboot/unexpected termination → previous-good snapshot retained; no duplicated app launch.
- Upgrade across minor versions → settings and snapshots migrated and adapter compatibility checked.
- Uninstall → no leftover daemon, hook or extensions; explicit opt-in to purge user data.

## 10. Test & verification matrix

| ID | Test scenario | Release gate |
|---|---|---|
| T01 | Drag 1×1, half, thirds, quarter, full 6×6 | Request/observed geometry matches within window-manager tolerance |
| T02 | 1×1, 10×10, 20×20 subdivision | Valid selections; no off-by-one/rounding drift |
| T03 | Per-side margins + inter-window gaps | Correct work-area-based coordinates |
| T04 | Toggle panel by hotkey, tray, relaunch | Correct target window preserved |
| T05 | Local and global shortcuts | Consistent activation; no ghost bindings after uninstall |
| T06 | Hold Shift and Alt behavior | Matches Divvy interaction contract |
| T07 | Multi-monitor selection / cycle | Cross-monitor placement; stable ordering |
| T08 | 100 placements on mixed DPI monitors | No runaway geometry drift/crashes |
| T09 | Save named layout; launch missing apps | Identical target count and placement or explicit unsupported flags |
| T10 | Reboot restore of 10 apps/15 windows | All supported targets matched; zero duplicates |
| T11 | App open before restore begins | Reuse, don't relaunch |
| T12 | Two windows of same app | Unique matching; no accidental swaps when distinguishable |
| T13 | Disconnect/reconnect external display | Predictable fallback and return policy |
| T14 | Browser/IDE native session restore | No extra tabs/windows; honest limitation reporting |
| T15 | Crash/power-cut during snapshot write | Previous valid snapshot usable |
| T16 | Crash during restore + retry | Idempotent, no duplicate app launching |
| T17 | Unsupported compositor/extension failure | Clear degraded mode; no silent false success |
| T18 | COSMIC auto-tiling enabled | No fight loops or silent global setting changes |
| T19 | Shell/KWin upgrade | Adapter mismatch detected; fail safe and offer fix |
| T20 | Unicode app names, title changes, transient dialogs | Safe window identity and exclusion |
| T21 | Log out with unsaved document | No false promise of content recovery |
| T22 | Permission boundary/IPC abuse tests | Non-owner cannot control windows; no unintended command execution |
| T23 | Session locked | No automation leaking window titles/activating windows |
| T24 | Arch packaging and clean install | No dependency break, functional user service |
| T25 | Ubuntu/Pop!_OS packaging and clean install | No manual hack required |
| T26 | Profile export/import with malicious fields | Schema validated; no shell execution |
| T27 | 24-hour service soak; repeated relogins | No unbounded memory/resource growth or ghost processes |
| T28 | Fully uninstall/reinstall | Clean lifecycle; explicit config preservation policy |

Produce a **versioned compatibility matrix**: OS version, desktop, compositor version, X11/Wayland, package version, adapter version, window move/resize, hotkeys, multi-monitor, save, restore, verified date, tester, logs. All automated tests should run in CI where feasible; critical Wayland desktop tests need nested/real compositor graphical environments and actual logout/reboot tests on VMs or machines. Containerized tests alone are insufficient to declare desktop parity.

### Quality/SLO targets (targets, not presumed measurements)

- Typical panel display latency: P95 <150 ms after hotkey (tested warm service, reference hardware).
- Normal geometry application: P95 <250 ms from selection commit to compositor acknowledgment where API permits.
- Session daemon idle resident memory target <75 MiB excluding desktop extension/Qt UI; measure and document.
- CPU idle median <0.5% of one core on reference hardware, apart from events.
- Snapshot write must be crash-consistent, with rollback to last valid snapshot.
- Restore transaction should converge or report a per-app actionable error by configured timeout; no indefinite hanging.
- UI responsive throughout restoration; cancellation available.
- Zero duplicate launches in supported tested scenario; any uncertain app identity requires confirmation instead of duplicate process spawning.

## 11. Data model / schema example

Example conceptual JSON (derive validated schemas, migrations and tests; this is illustrative, not final):

```json
{
  "schema_version": 1,
  "snapshot_id": "uuid",
  "created_at": "2026-11-10T20:00:00-06:00",
  "name": "Coding workspace",
  "mode": "manual",
  "outputs": [{"stable_id": "edid-hash-or-fallback", "name": "DP-1", "logical_rect": [0,0,2560,1440], "scale": 1.0}],
  "workspaces": [{"id": "ws-1", "name": "Development", "ordinal": 0}],
  "windows": [{
    "record_id": "uuid",
    "desktop_file_id": "org.gnome.Terminal.desktop",
    "app_id": "org.gnome.Terminal",
    "wm_class": "org.gnome.Terminal",
    "instance_ordinal": 0,
    "title_hint": "Terminal",
    "output_stable_id": "edid-hash-or-fallback",
    "workspace_id": "ws-1",
    "normalized_geometry": [0.0,0.0,0.5,1.0],
    "state": {"maximized": false, "fullscreen": false, "minimized": false},
    "launch_policy": "if_missing",
    "restore_content": "application_managed"
  }]
}
```

Never store plaintext browser contents, passwords, clipboard data, private document contents or full process environment in snapshots. `launch_policy` must be permission-gated; import sets it to `ask` unless the user confirms trusted profile provenance. Use internal numeric/UUID IDs; do not rely on volatile process IDs across reboot.

## 12. Discovery, alternatives and licensing

Audit: GNOME Shell extension APIs, GNOME Window Control/ws-dbus examples (security reviews required); KWin scripting API; COSMIC compositor/window-management capabilities and plugin/IPC support; X11 EWMH/libxcb; `systemd --user`, XDG autostart and `graphical-session.target`; contemporary existing products such as gTile, Tiling Assistant, Rectangle-like GNOME extensions, KDE native tiling and session-restoration tools. Produce a build/buy/extend/replace decision matrix and credit licenses.

**Important:** Do not package somebody else's source/assets without license compliance. Build original branding and UI. Reference Divvy feature behavior as a functional baseline, not as permission to clone copyrighted visual resources verbatim.

**Current source references, consult during implementation:**
- Divvy Windows manual: https://mizage.com/downloads/DivvyWinHelp.pdf
- GNOME Shell window-management example (assess security, do not blindly reuse): https://extensions.gnome.org/extension/10886/window-control/
- KWin scripting API: https://develop.kde.org/docs/plasma/kwin/api/
- System76 COSMIC/Pop guide: https://support.system76.com/support/install-pop/
- COSMIC shortcuts: https://www.system76.com/support/pop-cosmic-keyboard-shortcuts
- GNOME session documentation: https://gnome.pages.gitlab.gnome.org/gnome-session/re01.html
- systemd desktop integration: https://systemd.io/DESKTOP_ENVIRONMENTS/

## 13. Phased delivery roadmap (gated)

**Phase 0 — feasibility and discovery:** Inspect host and official manual; choose license and name; map Divvy parity requirements. Produce compositor capability proof-of-concepts for GNOME Wayland, KWin Wayland, COSMIC Wayland, and X11; document blocked operations and mitigation. *Gate:* actual move/resize + active window retrieval demonstrated on one real supported desktop; honest roadmap for the others.

**Phase 1 — platform-neutral engine:** Implement grid math, monitor transforms, margins/gaps, window matching, config schemas, test harness and IPC. *Gate:* comprehensive geometry property-based tests and schema fuzzing.

**Phase 2 — first complete desktop vertical slice:** Choose the developer's currently running desktop as the first target (do not presume GNOME on Pop!_OS). Build one adapter + GUI panel + hotkey + placement + settings + packaging. *Gate:* reproducible clean install, 10+ manual placement cases, screenshots/video/test log.

**Phase 3 — Divvy parity:** Subdivision, resizable grid, per-screen panels, local/global shortcuts, monitor cycling, previews, keep-open/Shift, profiles and all preference toggles. *Gate:* all DV items for initial platform verified.

**Phase 4 — persistence and restore:** Snapshots, app registry, opt-in launching, idempotent restore, graceful and unexpected reboot, named layouts, detailed UI. *Gate:* T09-T16, T21 success on initial platform.

**Phase 5 — desktop adapters:** GNOME, KWin, COSMIC, X11; integrate only available APIs, create upstream issues/proposals if needed. *Gate:* feature matrix per tested desktop/OS.

**Phase 6 — distribution/hardening:** `.deb`, Arch PKGBUILD/AUR, migration/rollback, security review, accessibility, long-run stress and release CI. *Gate:* fresh install, upgrade, reboot, uninstall all verified on Ubuntu/Pop!_OS/Arch target matrix.

**Phase 7 — release:** Tagged reproducible build, user guide, limitations, support matrix, troubleshooting and changelog. *Gate:* no critical release blockers or misleading parity claims.

## 14. Claude Code operating protocol: Goal → Delegate → Verify

Claude shall execute iteratively and keep these files current:

```text
GOAL.md               Product goals and success thresholds
REQUIREMENTS.md       Full requirements traceability (DV/RS/UI/PK/T IDs)
ARCHITECTURE.md       Decision records and adapter model
PLATFORM_MATRIX.md    Desktop/OS/version/capability status
IMPLEMENTATION_PLAN.md Milestones and owners
TEST_EVIDENCE.md      Commands, tests, results, real-desktop validation
RISK_REGISTER.md      Critical blockers, API risks, security risks
CHANGELOG.md          Delivered changes and known issues
```

**Delegate by clear workstream boundaries:** (A) compositor reconnaissance, (B) placement engine and property tests, (C) Qt/QML GUI, (D) session snapshot/restore and identity, (E) security and IPC review, (F) Debian/Arch packaging and release. Resolve shared API contracts before concurrent implementation; integration owner verifies final artifacts.

**Verify loop, mandatory for each milestone:**
1. Identify exact requirement IDs and proof needed.
2. Inspect actual repository and target platform state; no assumptions about OS/session/compositor.
3. Write tests or concrete manual procedures before implementation.
4. Implement smallest end-to-end slice and run static checks, unit tests, integration tests, packaging checks and a real desktop test when applicable.
5. Save test output, OS versions, compositor version and implementation commit in TEST_EVIDENCE.md.
6. Flag untested capabilities as NOT TESTED; blocked ones as BLOCKED BY PLATFORM.
7. Fix regressions before starting unrelated features.
8. Update user-facing limitations, configuration migration docs and release matrix.

**No shortcuts:** No unreviewed privileged input injection, screen scraping as primary window identity, indefinite sleeps for startup, unbounded retries, publicly writable IPC socket, brittle arbitrary title matching, silent auto-launch of unknown software, or claims of guaranteed restoration of unsaved app state.

## 15. Master Claude Code prompt — copy into a /goal workflow

> **GOAL:** Build and ship GridKeeper, a production-quality Linux desktop utility that matches Divvy for Windows' complete grid-window-placement feature set and surpasses it with opt-in workspace/session snapshots and reboot-time app/window restoration. Read this entire SRS and the linked original Divvy Windows manual before coding. Target Ubuntu, Pop!_OS (including modern COSMIC), and Arch Linux. Support GNOME, COSMIC, KDE Plasma, and X11 as verified per-platform adapters; explicitly report unsupported Wayland behavior.
>
> **DELEGATE:** Split research, core geometry, UI, compositor adapters, session restore, security, testing, and packaging into bounded tasks. First create a verified capabilities matrix from real API/code inspection. Architect a Rust session-service core, a modern low-overhead GUI, and compositor-native extensions with strict capability contracts and restricted IPC. Reuse permitted open-source code selectively and honor licenses.
>
> **DELIVER:** Full project source and build scripts; fast interactive 6×6 Divvy grid, Alt subdivision up to 20×20, customizable row/column count, draggable placement, edge margins and inter-window gaps, live geometry preview, local/global hotkeys, monitor cycling, panel-dismissal options, modern configuration GUI, named layouts and rules, robust session snapshots, per-app controlled relaunch, deduplicated idempotent window reconstruction, optional last-session login restoration, safe crash recovery, diagnostics, `.deb` and Arch packages, clear user docs, testing artifacts and compatibility reports.
>
> **VERIFY:** Treat every requirement as unproven until tested on a real relevant compositor. Never claim a generic Wayland API supports window positioning. Validate first-run install, hotkey, multimonitor geometry, restore after clean reboot and forced reboot, application duplication resistance, GNOME/COSMIC/KWin differences, privacy/security, upgrade, and uninstall. Record exact versions and evidence. Do not declare parity until all Divvy baseline requirements have VERIFIED status on the relevant platform, or clearly state residual limitations.
>
> **START NOW:** Perform Phase 0: inspect existing repo and host OS/session, analyze the original Divvy PDF page by page, investigate available compositor APIs and identify how session restoration can actually work on each target desktop. Produce GOAL.md, REQUIREMENTS.md, ARCHITECTURE.md, PLATFORM_MATRIX.md, IMPLEMENTATION_PLAN.md and a risk register, then implement a minimal working vertical slice on the current host desktop and prove it. Continue phase by phase unless a genuine platform blocker requires a design decision. Favor progress and recorded evidence over speculative completeness.

## 16. Definition of done

A release may be labeled **Divvy-equivalent on Platform X** only when DV-01..DV-28 are verified on that precise platform/version (or an openly documented, explicitly accepted non-applicable desktop feature), placement errors are visible and recoverable, and packaging/install reliability is established. A release may be labeled **session restore supported on Platform X** only after clean reboot, unexpected interruption, duplicate-avoidance, monitor remapping, and mixed-app regression tests pass; qualify unsupported apps and application-internal session data. Product UI and release notes must use measured results, never marketing substitutes for evidence.

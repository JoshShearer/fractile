# Requirements traceability

Source: `Divvy_Linux_Grid_Tiling_Session_Restore_SRS.md` (SRS v1.0, 2026-11-10).
Fractile 0.9.0. Statuses follow SRS section 0: **VERIFIED**, **PARTIAL**,
**BLOCKED BY PLATFORM**, **NOT IMPLEMENTED**, **NOT TESTED**. They are given
separately for each platform where a test ran.

Platform columns:

- **X11**: GNOME Shell 46.0 / Mutter on X11, nested in Xephyr, on a Pop!_OS 24.04 host
- **GW**: GNOME Shell 46.0 Wayland, headless, virtual monitors
- **KW**: KWin 6.8.2 Wayland, virtual backend, in an Arch container
- Plasma X11 (kwin_x11 5.27.11) is covered in CA-06, and other GNOME Shell
  versions in [PLATFORM_MATRIX.md](PLATFORM_MATRIX.md)
- **COSMIC**: cosmic-comp on Pop!_OS 24.04

Test ids (X.., G.., W.., M.., D.., S.., Y.., A.., K.., P.., SEC..) are the checks recorded in
`tests/evidence/*.json`. See [TEST_EVIDENCE.md](TEST_EVIDENCE.md). "NOT TESTED"
on a platform means the code is shared with a platform where the check passed,
but no check ran on this one.

No platform is labeled **Divvy-equivalent** in the SRS section 16 sense: on no
platform are all of DV-01..28 VERIFIED. The gaps are listed per row.

## Divvy parity (SRS 1)

| ID | Requirement | X11 | GW | KW | COSMIC |
|---|---|---|---|---|---|
| DV-01 | Background resident, one instance | VERIFIED (X01, X02, G17) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-02 | Panel via tray, shortcut, relaunch | PARTIAL: shortcut (G01) and relaunch (G17) verified; tray NOT TESTED on X11 | PARTIAL: shortcut (W12) and tray click (Y07, with the AppIndicator extension); relaunch not run on GW | PARTIAL: shortcut (K12) | BLOCKED BY PLATFORM |
| DV-03 | Panel shows the active app; target captured before focus moves | VERIFIED (G03, G04; screenshot 01-panel) | VERIFIED (W12-W14) | PARTIAL: panel shown (K12); placement from the panel not run | BLOCKED BY PLATFORM |
| DV-04 | Default 6x6, drag selection | VERIFIED (G03) | PARTIAL: keyboard selection (W13); drag not run | NOT TESTED | BLOCKED BY PLATFORM |
| DV-05 | Alt subdivides 2x | VERIFIED (G15) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-06 | 1x1..10x10, subdivision to 20x20 | VERIFIED (G15, X04, X06) | PARTIAL: 1x1..10x10 (W06, M06); 20x20 not run | PARTIAL: K04, K06 | BLOCKED BY PLATFORM |
| DV-07 | Alt+=/- and Alt+Shift+=/- change columns/rows | VERIFIED (G08) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-08 | Move and resize to the selection, pixel-accurate | VERIFIED (X04-X06) | VERIFIED (W04-W06, W08, M02, M06) | VERIFIED (K04-K06) | BLOCKED BY PLATFORM |
| DV-09 | Esc cancels with no side effects | VERIFIED (G07) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-10 | Dismiss after applying (configurable) | VERIFIED (G04) | VERIFIED (W14) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-11 | Shift inverts keep-open | VERIFIED (G09) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-12 | Panel on each monitor | NOT TESTED (single monitor) | VERIFIED (M05) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-13 | Move to another monitor through its panel | NOT TESTED | PARTIAL: placement onto the other monitor's grid (M02, via the service); drag on the second panel not run | NOT TESTED | BLOCKED BY PLATFORM |
| DV-14 | Unlimited named shortcuts, edit/delete, import/export | PARTIAL: stored and run (X09, G16); adding one in Settings and pressing it VERIFIED (S09-S11); edit/duplicate/remove/import/export exist in Settings > Shortcuts but are not driven by a test | PARTIAL (W09) | PARTIAL (K09) | BLOCKED BY PLATFORM |
| DV-15 | Local shortcuts only while the panel is open | VERIFIED (G16) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-16 | Global shortcuts; reject and explain conflicts | VERIFIED (X09, X10, X11) | VERIFIED (W09) | VERIFIED (K09) | BLOCKED BY PLATFORM |
| DV-17 | Repeated shortcut cycles monitors | NOT TESTED | VERIFIED (M04) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-18 | Start at login | VERIFIED (Ubuntu 24.04 VM, GNOME on Xorg, real GDM logins: V02, V03) | VERIFIED (Ubuntu 24.04 VM, real GDM logins): the first start from the app menu turns autostart on (systemd unit enabled, V02); after a real log-out and log-in the unit starts the service and it controls windows (V03) | PARTIAL: systemd unit enable and XDG entry verified in containers; no real login | BLOCKED BY PLATFORM |
| DV-19 | Optional tray icon | NOT TESTED | VERIFIED with the Ubuntu AppIndicators extension (the tray Ubuntu and Pop!_OS ship): icon appears at service start with no window (Y01-Y03, screenshot 30-tray-topbar), its menu works (Y04-Y06), click opens the panel (Y07), the setting turns it off and on (Y08). Stock GNOME has no tray: the background UI then exits and the panel still opens (Y10, Y11) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-20 | Show/hide shortcut with focus return | VERIFIED (G01, G04) | VERIFIED (W12, W14) | PARTIAL: shown and focused (K12); focus return not run | BLOCKED BY PLATFORM |
| DV-21 | Live preview, non-interactive | VERIFIED (G14) | NOT TESTED (drawn by the extension) | NOT TESTED (drawn by the UI) | BLOCKED BY PLATFORM |
| DV-22 | Horizontal/vertical gaps | VERIFIED (X25) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-23 | Per-side margins, px or %, 10 px default, 0-300 | VERIFIED (X25; ranges validated in config tests) | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-24 | Default grid size; keep or reset | PARTIAL: size kept (G08); reset-on-next-open not run | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-25 | General/Appearance/Shortcuts settings | VERIFIED: all 8 pages open and render (S01, S02, screenshots 20-27); a General change is held until Apply, then saved by the service (S08); the wizard (G05). Not every control is exercised | VERIFIED: same checks on Wayland (S01, S02, S08) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-26 | Shortcut editor conflict feedback | VERIFIED: real X key events into the editor: no modifier refused (S03), Fractile's own binding named (S04), another X client's grab reported in plain words (S05), free combination (S06), reserved panel key (S07); the panel shortcut listed with its keys (S13); global shortcuts paused while the editor listens and resumed on close, disconnect and timeout (S14-S17) | VERIFIED: real key events through Mutter's RemoteDesktop portal into the editor (S03-S07, S09-S11, S13); while it listens Fractile pauses its own global shortcuts so GNOME passes the keys on (S14-S17); a GNOME-assigned combination is reported with where it is set (S05) | NOT TESTED | BLOCKED BY PLATFORM |
| DV-27 | Target captured on open; closed window handled | PARTIAL: target capture (G03, G17); an unknown window is refused (X08b); closing the target while the panel is open not run | NOT TESTED | NOT TESTED | BLOCKED BY PLATFORM |
| DV-28 | No changes during preview; undo | VERIFIED (G14, X07) | PARTIAL: undo (W07) | PARTIAL: undo (K07) | BLOCKED BY PLATFORM |

## Session restore (SRS 2.1, 2.2, 6)

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| RS-01 | Six restore policies; restore independent of autostart | VERIFIED (X11) for manual and last_clean; others PARTIAL (unit-tested decision logic, no E2E) | X13, X22, X27, X28 |
| RS-02 | Snapshot contents per SRS 2.1/11 | VERIFIED | snapshot schema tests; X13, M07 |
| RS-03 | Wait for readiness, no fixed sleep | VERIFIED (X11) | X27, X28 (readiness wait observed in log) |
| RS-04 | Existing windows found before launching | VERIFIED | X12, X19, T11 below |
| RS-05 | Deterministic ids before titles | VERIFIED (unit) | `titles_alone_never_match`, `two_windows_same_app_distinguished_by_title` |
| RS-06 | Per-app launch policy; approval gating | VERIFIED | X16, X17, X18, `approval_gating_and_dry_run` |
| RS-07 | Launch via `.desktop`/argv, no shell | VERIFIED | SEC4, SEC7, launch unit tests |
| RS-08 | Bounded re-apply after the app moves itself | VERIFIED | placement verification (X04, W04, K04) |
| RS-09 | Missing monitor remap | VERIFIED (GW): monitor removed at runtime, its window moves to the remaining one (D04); a two-monitor snapshot restored onto one monitor keeps relative positions (D05); unit/sim (`missing_monitor_remaps`) | D04, D05 |
| RS-10 | No duplicates after restart/retry | VERIFIED | X19, X21, X27, `crash_and_retry_is_idempotent` |
| RS-11 | Dry run | VERIFIED | X12 |
| RS-12 | Cancel, progress, timeouts, retry, resume | VERIFIED (X11) for resume and retry; cancel unit-tested | X21, `cancellation` |
| RS-13 | Journal and run id | VERIFIED | X21, `transaction_log_and_ledger` |
| RS-14 | Never close or replace windows | VERIFIED (by design; no close call exists in the restore engine) | |
| RS-15 | Atomic, checksummed, backed-up store; corrupt latest falls back | VERIFIED (unit) | `corrupt_latest_falls_back_to_previous_good`, `refuses_newer_schema` |
| RS-16 | Rolling retention (10 min, 30 recent, 7 daily) | VERIFIED | `retention_keeps_recent_daily_and_manual`, P07 |
| RS-17 | Logout/shutdown capture without depending on it | VERIFIED | X22, W22 (logind PrepareForShutdown with a delay inhibitor); on GNOME the logout snapshot is taken at gnome-session's EndSession, before GNOME Shell stops (VM V06; at SIGTERM the extension is already gone), with the layout last seen as the fallback |
| RS-18 | Unclean-session flag and policy | PARTIAL: unit (`sessions_detect_unclean`); no power-cut test | |
| RS-19 | Lock-screen redaction | VERIFIED (GW) | W21 |
| RS-20 | Private windows excluded | PARTIAL: VERIFIED (X11) for Firefox 159: a real private window ("... Mozilla Firefox Private Browsing") is left out of the snapshot while the two normal windows are recorded (A01e). NOT MET for Google Chrome 157: its incognito window has no marker in the title or in any X11 property, so it is recorded like a normal window; recorded as a known limitation (`known_limitations` in the evidence) and documented in the user guide with the workaround (turn off stored titles) | `e2e-x11-apps.json` |
| RS-21 | No snapshot of a half-restored desktop | VERIFIED (X11): logout before the login restore starts (X28) and while a restore runs (P09) | X28, P09, `interrupted_restore_overlay` |

## Layouts, rules, extras (SRS 2.3, 2.4)

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| LR-01 | Unlimited named layouts | VERIFIED (X11) | X26 |
| LR-02 | Rules by app id, WM_CLASS, role, workspace, monitor, ordinal, opt-in title regex; explainable | VERIFIED (GW) for app-id on-create rules; others unit-tested | W20, rules tests |
| LR-03 | Layout modes: position existing / launch missing / exact N | PARTIAL: position existing (X26); others unit-tested | |
| LR-04 | Per-monitor variants | PARTIAL: implemented (layout variants by topology); no E2E | |
| LR-05 | App groups, ordered/delayed launching | PARTIAL: concurrency limit and order; no delay E2E | |
| LR-06 | Undo last action / pre-restore backup | VERIFIED | X07, X14 |
| LR-07 | Profile auto-switch by monitor topology | PARTIAL: implemented; no E2E | |
| LR-08 | Don't fight automatic tilers | VERIFIED (reports only; never changes settings) | diagnose warning; COSMIC never modified |
| UX-01 | Keyboard-only operation | VERIFIED (X11, GW) | G06, W13 |
| UX-02 | Snap commands (halves, thirds, quarters, center, maximize) | VERIFIED | G10, G16, M04 |
| UX-03 | Command palette | VERIFIED (X11) | G10 |
| UX-04 | Export/import with validation | VERIFIED | SEC3, SEC7 |
| UX-05 | Local CLI and authenticated API | VERIFIED | all E2E suites use them; SEC1, SEC2 |
| UX-06 | Accessibility labels, high contrast, font scaling, RTL | PARTIAL: keyboard-only use works (G06); accessible names are set on only a few widgets (grid, shortcut buttons, dashboard and recovery lists); no screen-reader, high-contrast or RTL test | |

## Compositor capability (SRS 3)

| ID | Requirement | Status |
|---|---|---|
| CA-01 | Adapter API with per-operation support levels and version negotiation | VERIFIED (W23, K00, X24, bridge unit test) |
| CA-02 | GNOME Wayland via Shell extension and restricted IPC | VERIFIED (GW) |
| CA-03 | GNOME X11 via EWMH/RandR | VERIFIED (X11) |
| CA-04 | COSMIC | BLOCKED BY PLATFORM: no geometry API ([docs/compatibility/cosmic.md](docs/compatibility/cosmic.md)) |
| CA-05 | KDE Plasma Wayland via KWin script | VERIFIED (KW, virtual backend) |
| CA-06 | KDE Plasma X11 | VERIFIED: the full X11 suite (39/39) with kwin_x11 5.27.11 as the window manager (Ubuntu 24.04 container, Xvfb, no plasmashell). Plasma 6.8 (Arch) ships no X11 session, so 5.27 is the newest Plasma X11 tested |
| CA-07 | Sway/Hyprland (optional) | NOT IMPLEMENTED (reported unsupported, X24) |
| CA-08 | Bridge authorization beyond bus names | VERIFIED (SEC5, SEC6) |

## Packaging (SRS 9)

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| PK-01 | `.deb` for Ubuntu/Pop!_OS | VERIFIED (clean Ubuntu 24.04 container) | `package-deb-ubuntu24.04.log` |
| PK-02 | Arch PKGBUILD | VERIFIED (clean `archlinux:latest`, makepkg as user) | `package-arch.log` |
| PK-03 | systemd user unit (graphical-session.target) | PARTIAL: `systemd-analyze --user verify` passes, enable verified; not run under a real user manager | |
| PK-04 | XDG autostart fallback | VERIFIED (Arch container; TryExec unit test) | |
| PK-05 | Clean uninstall; config preservation policy | VERIFIED | T28 below |
| PK-06 | Upgrade/migration | PARTIAL: config/schema version checks (`refuses_newer_schema`); no package-to-package upgrade run | |

## Test matrix (SRS 10)

| ID | Test | Status | Evidence / gap |
|---|---|---|---|
| T01 | Drag 1x1, half, thirds, quarter, full | VERIFIED (X11, GW, KW) | G03, X04-X06, W04-W06, K04-K06 |
| T02 | 1x1, 10x10, 20x20, no drift | VERIFIED (X11); PARTIAL (GW, KW: 20x20 not run) | G15, M06 |
| T03 | Margins + gaps | VERIFIED (X11) | X25, property tests |
| T04 | Panel by hotkey, tray, relaunch; target preserved | VERIFIED across platforms: hotkey and relaunch (X11), tray (GW, AppIndicator extension); tray on X11 not run | G01, G17, Y07 |
| T05 | Local and global shortcuts; no ghost bindings | VERIFIED: X11 (G16, X09), GW (W09, W16), KW (K09, K17) | |
| T06 | Shift and Alt | VERIFIED (X11) | G09, G15 |
| T07 | Multi-monitor selection and cycle | VERIFIED (GW); NOT TESTED (X11, KW) | M01-M05 |
| T08 | 100 placements on mixed DPI | VERIFIED (GW): 100 placements alternating a scale-2 and a scale-1 monitor, exact, no drift (D02); scale changed at runtime (D01); snapshot restore across both (D03). X11 and KW not run | D01-D03, M06 |
| T09 | Named layout; launch missing apps | PARTIAL: layout (X26) and approved launch on restore (X18); layout launch-missing mode not run | |
| T10 | Reboot restore of 10 apps / 15 windows | VERIFIED (GW and X11, Ubuntu 24.04.5 VM, GNOME 46.0, Wayland and Xorg sessions): 10 apps / 15 windows on a 4x4 grid; after a real reboot and GDM login Fractile asks first and Enter restores all 15 where they were, no duplicates (V05); with asking off, after a real log-out and log-in all 15 come back automatically (V06). KW: simulated relogins only | `vm-ubuntu24.04-wayland.json`, `vm-ubuntu24.04-x11.json`, `tests/evidence/vm/` |
| T11 | App open before restore: reuse | VERIFIED | X13, X19, X27 |
| T12 | Two windows of one app | VERIFIED (X11): two windows of one app are listed separately and each returns to its own place, nothing duplicated (X30a, X30); real browsers and VS Code with two windows each (A01-A03); unit/sim (`two_windows_same_app_distinguished_by_title`, `indistinguishable_windows_use_ordinal_and_flag`) | X30, A01-A03 |
| T13 | Monitor disconnect/reconnect | VERIFIED (GW): disconnect at runtime (D04-D06), reconnect with the same stable id; windows go back when the return option is on (D07) and stay put when it is off, the default (D08). X11 and KW not run | D04-D09 |
| T14 | Browser/IDE native session restore | VERIFIED (X11) with real Firefox 159, Google Chrome 157 and VS Code 1.145 on throwaway profiles: after the app quits, a restore launches it once, it reopens its own two windows, both go back to where they were, no extra windows, nothing claimed about tabs or unsaved content, a second restore launches nothing (A01-A03). Such apps are on a built-in list that starts them once; the per-app setting overrides it. GW and KW not run | `e2e-x11-apps.json` |
| T15 | Crash during snapshot write | PARTIAL: corrupt-row fallback (unit); SQLite WAL with synchronous=FULL; no power-cut test | |
| T16 | Crash during restore + retry | VERIFIED (X11) | X21 |
| T17 | Unsupported compositor / extension failure | VERIFIED | X24, W16, K00, K14 |
| T18 | COSMIC auto-tiling | BLOCKED BY PLATFORM: no window control on COSMIC; Fractile never touches tiling settings | |
| T19 | Shell/KWin upgrade: mismatch detected, fix offered | VERIFIED (GW: W23; both bridges: unit test); KW E2E not run | |
| T20 | Unicode names, title changes, transient dialogs | VERIFIED (X11): a Unicode title is listed exactly, recorded, restored, and left out of redacted exports and logs (X29a, X29); a window retitled after the snapshot keeps its identity (X30); a transient dialog is never offered, recorded or moved (X31); GW: dialogs excluded (W15) | X29-X31, W15, G12 |
| T21 | Logout with unsaved document: no false promise | VERIFIED by inspection: UI, CLI and docs state that contents are not restored; snapshots record `restore_content: application_managed` | |
| T22 | Permission boundary / IPC abuse | VERIFIED: other uid refused (root and a second user, Ubuntu container), non-compositor same-uid refused (SEC5, SEC6), hello required (SEC2), no command execution from imports (SEC3, SEC7) | |
| T23 | Session locked | VERIFIED (GW, through logind LockedHint from a test logind) | W21; real lock screen not run |
| T24 | Arch packaging, clean install | VERIFIED (container) | `package-arch.log` |
| T25 | Ubuntu/Pop!_OS packaging, clean install | VERIFIED (Ubuntu 24.04 container; and `apt install` on a real Ubuntu 24.04.5 desktop VM, dependencies from the Ubuntu archive, V01); Pop!_OS install NOT TESTED (no root on the test host) | `package-deb-ubuntu24.04.log` |
| T26 | Malicious import | VERIFIED | SEC3, SEC4, SEC7 |
| T27 | 24 h soak; repeated relogins | PARTIAL: 20-minute soak, 10 simulated relogins and 6 logouts during a restore all pass (P06, P08, P09); not 24 h, not real logins | `perf-x11.json` |
| T28 | Uninstall/reinstall, config policy | VERIFIED (Ubuntu and Arch containers) | package logs |

## Quality targets (SRS 10)

Measured on the X11 nested desktop (see `tests/evidence/perf-x11.json`):

| Target | Result |
|---|---|
| Panel P95 < 150 ms after hotkey | met: p95 19.6 ms (P05) |
| Placement P95 < 250 ms | met: p95 114 ms (P04) |
| Idle RSS < 75 MiB (service) | met: 10.4 MiB (P02) |
| Idle CPU median < 0.5% | met: 0.0% (P03) |
| Crash-consistent snapshots | WAL + checksums; corrupt-row fallback unit-tested |
| Restore converges or reports by timeout | per-item and total timeouts; X17 (partial reported) |
| UI responsive during restore; cancellation | restore runs in the service; UI stays live (G11); cancel unit-tested |
| Zero duplicate launches | X19, X21, X27, P08 |

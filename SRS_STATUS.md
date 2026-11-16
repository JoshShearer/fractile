# SRS status

Every section of `Divvy_Linux_Grid_Tiling_Session_Restore_SRS.md` (v1.0,
2026-11-10) checked against Fractile 0.9.0 as built on 2026-11-15. Statuses
follow SRS section 0 (VERIFIED, PARTIAL, BLOCKED BY PLATFORM, NOT
IMPLEMENTED, NOT TESTED). The numbered requirements (DV, RS, LR, UX, CA, PK,
T01-T28 and the quality targets) are traced one by one, per platform, in
[REQUIREMENTS.md](REQUIREMENTS.md); this file covers every SRS section,
points to those rows, and lists each item that is not VERIFIED with the
reason it is accepted. Test ids refer to `tests/evidence/*.json`
([TEST_EVIDENCE.md](TEST_EVIDENCE.md)); environments E1-E16 are in
[PLATFORM_MATRIX.md](PLATFORM_MATRIX.md).

## Summary

| SRS section | Status | Where |
|---|---|---|
| 0 Rules (classification, honesty, security) | VERIFIED | below |
| 1 Divvy parity DV-01..28 | VERIFIED on GNOME X11, GNOME Wayland, KWin Wayland; BLOCKED BY PLATFORM on COSMIC | REQUIREMENTS.md, Divvy parity |
| 2.1-2.2 Session persistence, restore, hygiene RS-01..21 | VERIFIED | REQUIREMENTS.md, Session restore |
| 2.3-2.4 Layouts, rules, extras LR-01..08, UX-01..06 | VERIFIED | REQUIREMENTS.md, Layouts |
| 3 Compositor strategy CA-01..08 | VERIFIED except CA-04 COSMIC: PARTIAL (placement BLOCKED BY PLATFORM) | REQUIREMENTS.md, Compositor capability |
| 4 Architecture | VERIFIED (one recommendation replaced, see below) | ARCHITECTURE.md |
| 5 Precision geometry | VERIFIED, with rotation NOT TESTED | below |
| 6 Restore contract | VERIFIED; one status added | below |
| 7 UI screens | VERIFIED | below |
| 8 Shortcuts and CLI | VERIFIED; COSMIC shortcuts BLOCKED BY PLATFORM | below |
| 9 Installation and distribution | VERIFIED for x86_64 packages; signing, repositories, aarch64 and debug packages not done | below |
| 10 Test matrix T01-T28, quality targets | T01-T28 VERIFIED; latency targets measured on X11 only | REQUIREMENTS.md, Test matrix |
| 11 Data model | VERIFIED | below |
| 12 Discovery, licensing | VERIFIED | below |
| 13 Phases 0-7 | gates 0-6 met; phase 7 (tag and publish) left to the maintainer | below |
| 14-15 Operating protocol, master prompt deliverables | VERIFIED | below |
| 16 Definition of done | neither label claimed; reasons below | below |

## 0. Rules

- Classification per compositor and version: REQUIREMENTS.md and
  PLATFORM_MATRIX.md give a status per platform and name the environment
  and test behind each VERIFIED.
- No universal Wayland claim: Wayland support is listed per compositor
  (GNOME Shell 45-48, KWin 6.6/6.8, Hyprland 0.56/0.57, Sway 1.11/1.12,
  COSMIC 1.11 partial); other compositors are reported unsupported (X24).
- No unsaved-content promise: T21 VERIFIED; the UI, CLI help, recovery
  dialog and docs say content is restored only by the app itself.
- Reopening is never shown as exact restore: per-item statuses
  (`LAUNCHED_AND_PLACED`, `PLACED_ONLY`, `LAUNCHED_UNPLACED`, and on COSMIC
  `RESTORED_WITHOUT_GEOMETRY` with "position and size not restored").
- Security: unprivileged service and UI, no `/dev/input`, no input
  injection by the product, no telemetry, imports never launch apps without
  approval, 0600 socket with peer-credential checks (T22, T26, SEC suites;
  [docs/security](docs/security/README.md)). The VM suites inject keys
  through QEMU's virtual keyboard as a test harness; the product does not.

## 1-3. Numbered requirements

All rows are in REQUIREMENTS.md. Cells that are not VERIFIED:

| Item | Status | Accepted because |
|---|---|---|
| DV-01..28 on COSMIC | BLOCKED BY PLATFORM | Every DV item places windows or depends on placement. cosmic-comp 1.11.0 (and the Pop!_OS host's cosmic-comp) has no request that sets another client's position or size, and no in-compositor extension mechanism (docs/compatibility/cosmic.md). The SRS foresees this: Wayland "where supported" (line 5); for COSMIC "upstream collaboration if no supported hook" (line 122); "arbitrary Wayland window placement on unsupported compositors" is "not generally possible" (line 213); phase 5 says "integrate only available APIs, create upstream issues/proposals if needed" (line 379). The proposal is filed: pop-os/cosmic-protocols#97. |
| CA-04 COSMIC | PARTIAL | The available APIs are integrated (window list, workspaces, states, restore without geometry; C01-C11 in a real COSMIC 1.11.0 login); placement is BLOCKED BY PLATFORM for the reason above. |
| CA-07 other wlroots compositors (river, Wayfire, labwc) | NOT IMPLEMENTED | The SRS lists only Sway and Hyprland, as an optional tier (section 3). Others are detected and reported unsupported with the reason (X24). |

## 4. Architecture

- Rust core and user-session service, versioned local IPC, SQLite with WAL,
  CompositorAdapter with per-operation support levels and version
  negotiation (CA-01), adapters as the only layer that changes compositor
  state, GUI without privileged operations (restores run in the service).
  Repository layout as suggested: `crates/fractile-{core,daemon,ipc,session,cli}`,
  `adapters/{gnome-shell,kwin,x11,cosmic,sway,hyprland}`,
  `packaging/{debian,arch,systemd,xdg}`, `tests/`, `docs/{architecture,compatibility,security,user-guide}`.
- Replaced recommendation: the GUI is PyQt6 widgets, not Qt/QML. The SRS
  allows "a well-supported native alternative decided by proof-of-concept";
  the reasons are in ARCHITECTURE.md (ADRs). Idle footprint of the service
  10.4 MiB, CPU 0.0% (P02, P03).
- The COSMIC adapter is Rust, as the SRS suggests; it is an ordinary Wayland
  client of the session, since COSMIC has no extension mechanism.
- `packaging/aur/` does not exist: the PKGBUILD in `packaging/arch/` is the
  AUR candidate (it says how to switch it to a release tarball).

## 5. Precision geometry

| Item | Status | Evidence |
|---|---|---|
| Logical coordinates, no double scaling | VERIFIED | mixed scale 2 + 1 (D01-D03, GNOME Wayland); scale 1.5 next to 1 (S25 Sway, H25 Hyprland); Omarchy scale 1.5 by hand |
| Work area excludes panels and docks | VERIFIED where the desktop reports it (X11 `_NET_WORKAREA`, GNOME, KWin, Sway, Hyprland); on COSMIC the work area is the whole monitor because COSMIC does not report it (placement is blocked there anyway) | X04-X06, W04-W06, K04-K06 |
| Inclusive cells to half-open rectangles; deterministic rounding, no drift | VERIFIED | `halves_are_seamless`, `thirds_on_odd_width_have_no_drift`, `portrait_monitor_at_negative_origin_tiles_exactly`, proptests; T02 |
| Margins and gaps separate | VERIFIED | X25, `gaps_are_exact_between_neighbours`, `margins_shrink_from_each_side`; DV-22, DV-23 |
| Frame vs client geometry; observed result verified | VERIFIED | placement verification on every adapter; `frame_includes_border_except_fullscreen` (Hyprland) |
| Min/max/aspect constraints reported; fit/center/fail policy | VERIFIED | constraint policy tests; learned limits (`learned_minimum_sizes`), V09 on every VM desktop |
| Negative coordinates, portrait monitors, mixed scales, changed EDIDs | VERIFIED (engine and mapping tests; mixed scales on real compositors) | `thirds_on_odd_width_have_no_drift` (x = -1000), `portrait_monitor_at_negative_origin_tiles_exactly`, `mapping_by_connector_and_size_when_edid_changed` |
| Rotated monitors on a real compositor | NOT TESTED | Outputs are taken in the compositor's logical (already rotated) size; no suite rotated a monitor. |
| Dialogs, panels and system windows not moved by default | VERIFIED | `dialogs_and_own_windows_excluded`, T20 (X29-X31, W15) |
| Pre-placement geometry kept for undo; maximized/fullscreen transitions | VERIFIED | X07, X14, LR-06 |

## 6. Restore contract

- 6.1: what can and cannot be promised is shown in the UI (recovery dialog
  warning), CLI help, diagnostics and the release notes (CHANGELOG known
  issues). VERIFIED (T21, T14).
- 6.2 steps 1-12: RS-01..21 and T09-T16 VERIFIED, including real reboots and
  log-outs in VMs on GNOME (Xorg, Wayland), Plasma (6.6 Wayland, 5.27 X11),
  Sway, Hyprland and COSMIC (without geometry), and a VM power cut. Step 11's
  status list is extended by `CANCELLED`, the in-progress `PENDING` and
  `LAUNCHING`, and `RESTORED_WITHOUT_GEOMETRY` (REQUIREMENTS.md, after
  RS-21): none of the nine listed statuses describes these cases without
  claiming a placement or a failure.
- 6.3 profiles: global and per-profile settings, manual restore as the
  first-run default with opt-in before automatic launches, loop protection
  after repeated failures. VERIFIED (RS-01, RS-06, RS-18; wizard tests).

## 7. UI screens

Quick panel, Settings (General, Appearance, Shortcuts, Session, Rules,
Monitors, Diagnostics, About), workspace dashboard, session recovery dialog,
rules builder and first-run compatibility wizard exist and are covered by
DV-02..28, LR-01..08 and UX-01..06 (GUI suites on X11 and KWin X11, PW suite
on GNOME 45/47/48 Wayland, P suites in real logins). The wizard runs real
probes (no green check without one) and, on a desktop that cannot place
windows (COSMIC), says so in bold and what still works.

## 8. Shortcuts and interoperability

VERIFIED: separate global and panel registries, compositor-approved
registration (Mutter grab through the extension, KGlobalAccel, XGrabKey,
Sway/Hyprland runtime binds), conflict reporting (DV-16, DV-26), no raw
keyboard hooks or evdev. All required actions exist (grid placement, monitor
cycling, move to workspace, save/restore, restore undo, subdivision, preset
search through the palette). The headless commands the SRS names
(`status`, `diagnose`, `save --name`, `restore --dry-run`,
`restore --snapshot`, `list-windows`, `list-layouts`, `logs --redacted`) are
all in `fractile` with stable exit codes (UX-05). On COSMIC global shortcuts
are BLOCKED BY PLATFORM: COSMIC offers clients no shortcut registration, and
the SRS forbids raw keyboard hooks.

## 9. Installation and distribution

| Item | Status | Accepted because / evidence |
|---|---|---|
| `.deb` for Ubuntu/Pop!_OS, desktop entry, icons, systemd user unit, XDG autostart fallback, no double launch | VERIFIED | PK-01, PK-03, PK-04; apt install in real Ubuntu 24.04 and 26.04 logins (VMs); Pop!_OS host by hand |
| Arch PKGBUILD, clean build, dependencies | VERIFIED | PK-02; makepkg in a clean container; pacman -U in real Arch logins (Hyprland, COSMIC VMs) and on Omarchy |
| Upgrade keeps settings and snapshots; uninstall leaves nothing running; purge only on request | VERIFIED | PK-05, PK-06, T28 |
| Installation acceptance (wizard, login once, reboot, upgrade, uninstall) | VERIFIED | VM suites V00-V09, U suite |
| Signed `.deb`, apt repository, signed pacman repository, AUR submission | NOT IMPLEMENTED | Signing and publishing need the maintainer's keys and accounts and are outward-facing; this build does not publish anything (the SRS makes the apt repository optional and the pacman repository "if offering"). |
| aarch64 | NOT TESTED | The SRS orders "x86_64 first, aarch64 next". The PKGBUILD declares aarch64, but nothing was built or run on aarch64. |
| Separate debug packages | NOT IMPLEMENTED | Release builds only; the `.deb` is stripped. No debug package is built or tested. |

## 10. Test matrix and quality targets

T01-T28: VERIFIED (REQUIREMENTS.md, Test matrix), with the gaps each row
states (for example T08 and T13 ran on GNOME Wayland only, T19's KWin
mismatch E2E was not run). Quality targets: idle RSS 10.4 MiB and CPU 0.0%
(P02, P03), panel p95 19.6 ms (P05) and placement p95 114 ms (P04), all
measured on X11; the latency targets were not measured on Wayland. Crash
consistency (PC01-PC03), restore convergence and cancellation (X17, X21),
zero duplicate launches (X19, X21, X27, C11). The versioned compatibility
matrix is PLATFORM_MATRIX.md.

Multiple monitors: automated runs used virtual or headless outputs (RandR
monitors on Xvfb, GNOME virtual monitors, KWin virtual outputs, headless
Sway/Hyprland outputs). Physical monitors were checked by hand only: two
physical monitors (2560x1440 and 3440x1440) on the Pop!_OS GNOME Xorg host,
and a scale-1.5 laptop panel on Omarchy.

## 11. Data model

Snapshot schema with the SRS 11 fields (schema version, outputs with stable
ids, workspaces, windows with identity, normalized and absolute geometry,
state, launch policy, restore content, diagnostics), validated on load and
import (RS-02, T26, SEC3).

## 12. Discovery and licensing

GPL-3.0-or-later, original name (Fractile, not the Divvy brand), original
icons and UI. Dependencies' licences are compatible (the COSMIC protocol
bindings are MIT). The audit of GNOME, KWin, COSMIC, X11 and systemd
interfaces is in docs/compatibility and ARCHITECTURE.md.

## 13. Phases

| Phase | Gate | Status |
|---|---|---|
| 0 Feasibility | move/resize and active window on one real desktop; honest roadmap | met (X11 on GNOME/Mutter) |
| 1 Engine | property tests, schema fuzzing | met |
| 2 First vertical slice | adapter, panel, hotkey, placement, settings, packaging | met (GNOME X11) |
| 3 Divvy parity | all DV items on the initial platform | met (X11; also GW and KW) |
| 4 Persistence and restore | T09-T16, T21 | met |
| 5 Desktop adapters | feature matrix per desktop | met: GNOME, KWin, X11, Sway, Hyprland; COSMIC partial with the upstream proposal filed |
| 6 Distribution and hardening | install, upgrade, reboot, uninstall on Ubuntu/Pop!_OS/Arch | met (VMs and containers; Pop!_OS host by hand) |
| 7 Release | tagged build, guide, limitations, matrix, changelog | docs done; tagging and publishing are left to the maintainer |

## 14-15. Protocol and deliverables

GOAL.md, REQUIREMENTS.md, ARCHITECTURE.md, PLATFORM_MATRIX.md,
IMPLEMENTATION_PLAN.md, RISK_REGISTER.md, TEST_EVIDENCE.md, user guide,
security review, compatibility reports, `.deb` and Arch packages, test
artifacts in `tests/evidence/`. Every claim names its environment and test.

## 16. Definition of done

- **Divvy-equivalent on Platform X:** not claimed. DV-01..28 are VERIFIED on
  GNOME X11, GNOME Wayland and KWin Wayland, but each of these combines
  several versions and setups (nested, headless, container and real VM
  logins), not one precise platform/version as SRS 16 requires.
- **Session restore supported on Platform X:** not claimed. No single
  platform has passed all five tests SRS 16 names (clean reboot, unexpected
  interruption, duplicate avoidance, monitor remapping, mixed apps); the
  closest, GNOME Xorg, lacks monitor remapping on X11 (PLATFORM_MATRIX.md).
- Product UI and release notes report measured results only.

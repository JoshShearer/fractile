# Risk register

Severity: H (blocks a release claim or harms users), M (limits a feature), L (cosmetic or unlikely).
Status as of 0.9.0.

## Platform and API risks

| ID | Risk | Sev | Likelihood | Mitigation | Status |
|---|---|---|---|---|---|
| R1 | COSMIC has no API to move or resize other clients' windows | H | certain | Report placement BLOCKED BY PLATFORM with the reason; use what COSMIC offers (window list, workspaces, states) and say per restore item that position and size were not restored; upstream proposal in docs/compatibility/cosmic.md; never imitate | open: needs upstream; proposal filed as pop-os/cosmic-protocols#97 (2026-11-14) |
| R2 | GNOME Shell extension API changes between releases (45-48 declared, 46 tested) | H | likely per release | adapter API version negotiation; refused integrations reported with a fix (W23); test on each new GNOME before raising shell-version | open: 45, 47, 48 not tested |
| R3 | KWin scripting API changes across Plasma releases | M | likely | same negotiation; heartbeat liveness; script kept minimal | open: only 6.8.2 tested |
| R4 | Apps refuse exact geometry (minimum sizes, size increments, CSD shadows) | M | certain | read-back verification, bounded re-apply, learned minimum sizes, explicit "constrained" note | mitigated (M06) |
| R5 | Automatic tilers override placement | M | on tiling desktops | detect and report; never change tiler settings | mitigated (reporting); no tiler E2E |
| R6 | Mixed-DPI and fractional scaling coordinate errors | H | possible | all geometry in logical pixels; adapters own conversion | open: NOT TESTED |
| R7 | Monitor hot-plug during a session | M | common on laptops | remap logic unit-tested; on_outputs_changed handler | open: runtime hot-plug NOT TESTED |
| R8 | Real logout/reboot timing differs from simulation (logind delay, session teardown order) | H | possible | logind delay inhibitor, exit snapshot, periodic autosave, no dependence on shutdown hooks | open: no real reboot test |
| R9 | Headless/virtual compositors differ from real sessions (input, focus, lock screen) | M | possible | real Mutter/KWin binaries, real clients and real key events; documented as such | open: needs real-login runs |
| R10 | Tray unavailable on stock GNOME | L | certain | launcher relaunch and shortcut always available | accepted |

## Security risks

| ID | Risk | Sev | Mitigation | Status |
|---|---|---|---|---|
| S1 | Same-uid process drives Fractile (moves windows, launches `always` apps) | M | outside the trust boundary; no capability beyond what same-uid already has; launch defaults to `ask` | accepted, documented |
| S2 | Impostor integration attaches | H | peer exe check (root-owned compositor binary), cap_sys_nice rule for KWin, connection pinning | mitigated (SEC5, SEC6) |
| S3 | Socket or D-Bus name squatting by a same-uid process | M | second-instance check; documented | accepted, documented |
| S4 | Hostile snapshot/layout import runs commands | H | command fields dropped, policies forced to `ask`, desktop-id validation | mitigated (SEC3, SEC4, SEC7) |
| S5 | Window titles leak via logs/exports/lock screen | M | tagged titles, redacted export, private-window exclusion, lock handling | mitigated (X20, W21) |
| S6 | Other users reach the socket | H | 0700 dir, 0600 socket, SO_PEERCRED uid check | mitigated (SEC1; T22 container check) |
| S7 | No seccomp/landlock sandbox for the service | L | systemd unit hardening; small attack surface (local socket only) | open |

## Delivery risks

| ID | Risk | Sev | Mitigation | Status |
|---|---|---|---|---|
| D1 | Evidence comes from nested/headless/container runs, not real logins (SRS 10: containers alone are insufficient for parity) | H | statuses say so; no parity claims | open: real-desktop pass needed on GNOME Wayland, Plasma, and a reboot |
| D2 | Pop!_OS host install not tested (no root on the test host) | M | Ubuntu 24.04 container install; same packaging | open |
| D3 | Soak far shorter than 24 h | M | 24 h VM soak: service RSS flat (17.0 to 17.3 MiB from hour 4), 0 errors; 10 relogins + 6 mid-restore logouts. Whole-test memory grew ~540 MiB unattributed (harness now logs per-process RSS) | partly mitigated |
| D4 | Test flakiness under load (timing-sensitive GUI checks) | L | waits on observable state, not sleeps; bounded subprocess calls | mitigated; one G17 flake fixed (now waits for panel focus) |

# Goal

Build **Fractile**, a Linux desktop utility that matches Divvy for Windows'
grid window placement (DV-01..DV-28 in the SRS) and goes beyond it with opt-in
workspace snapshots, named layouts and rules, and restoring windows after
logout, reboot or crash. It should be distributable as a `.deb` and an Arch
package, run unprivileged in the user's session, and be honest per desktop
about what works.

Specification: `Divvy_Linux_Grid_Tiling_Session_Restore_SRS.md`. Traceability:
[REQUIREMENTS.md](REQUIREMENTS.md).

## Success thresholds

| Area | Threshold | Where it stands (0.9.0) |
|---|---|---|
| Placement accuracy | requested vs. observed frame within 2 px, or the window's own constraint reported | met on GNOME X11, GNOME Wayland and KWin Wayland (X04-X06, W04-W06, M06, K04-K06) |
| Divvy parity | every DV item VERIFIED on a platform before calling it "Divvy-equivalent" there | not met on any platform; see REQUIREMENTS.md |
| Session restore | clean reboot, unexpected interruption, duplicate avoidance, monitor remap and mixed apps pass before calling it "supported" | duplicate avoidance and crash-resume met (X19, X21); relogin simulated (X27, X28, P08, P09); real reboot not run |
| Panel latency | P95 < 150 ms after the hotkey, warm | met on X11: p95 19.6 ms (P05); not measured on Wayland |
| Placement latency | P95 < 250 ms, request to geometry read back | met on X11: p95 114 ms (P04); not measured on Wayland |
| Idle footprint | service < 75 MiB RSS, < 0.5% of a core | met on X11: 10.4 MiB, 0.0% (P02, P03) |
| Duplicates | zero duplicate launches in tested scenarios | met (X19, X21, X27, P08, P09) |
| Security | unprivileged, no `/dev/input`, no input injection, 0600 socket with peer credentials, no telemetry, imports never auto-launch, no shell | met; see docs/security |
| Honesty | no claim of generic Wayland support, of content restore, or of silent tiling changes | met; COSMIC reported BLOCKED BY PLATFORM |
| Packaging | clean install, remove, reinstall on Ubuntu 24.04 and Arch | met in containers |

## Non-goals (0.9.0)

- Restoring application contents (documents, tabs, terminals) beyond what the
  application restores itself.
- COSMIC window control (no platform API; see docs/compatibility/cosmic.md).
- Sway, Hyprland and other wlroots compositors.
- Telemetry or any network access.

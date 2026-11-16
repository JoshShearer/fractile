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
| Divvy parity | every DV item VERIFIED on a platform before calling it "Divvy-equivalent" there | DV-01..28 VERIFIED on GNOME X11, GNOME Wayland and KWin Wayland; all of them on one precise platform, KDE Plasma 6.6.6 Wayland on Ubuntu 26.04 (real logins, one and two monitors), so "Divvy-equivalent" is claimed there and only there; COSMIC: BLOCKED BY PLATFORM; see SRS_STATUS.md section 16 |
| Session restore | clean reboot, unexpected interruption, duplicate avoidance, monitor remap and mixed apps pass before calling it "supported" | real reboot and log-out pass on GNOME Xorg and Wayland, Plasma 6.6 Wayland and 5.27 X11, Sway 1.11 and Hyprland 0.57.2 (V05, V06 in VMs); VM power cut (PC01-PC03), duplicate avoidance (X19, X21, X27), monitor remap (D04-D09, GNOME Wayland) and mixed apps (A01-A03); no single platform has all five yet, so "supported" is not claimed (PLATFORM_MATRIX.md). COSMIC: restore without positions or sizes after a real reboot (C10, C11) |
| Panel latency | P95 < 150 ms after the hotkey, warm | met on X11: p95 19.6 ms (P05); not measured on Wayland |
| Placement latency | P95 < 250 ms, request to geometry read back | met on X11: p95 114 ms (P04); not measured on Wayland |
| Idle footprint | service < 75 MiB RSS, < 0.5% of a core | met on X11: 10.4 MiB, 0.0% (P02, P03) |
| Duplicates | zero duplicate launches in tested scenarios | met (X19, X21, X27, P08, P09) |
| Security | unprivileged, no `/dev/input`, no input injection, 0600 socket with peer credentials, no telemetry, imports never auto-launch, no shell | met; see docs/security |
| Honesty | no claim of generic Wayland support, of content restore, or of silent tiling changes | met; on COSMIC placement is reported BLOCKED BY PLATFORM and restore items say position and size were not restored |
| Packaging | clean install, remove, reinstall on Ubuntu 24.04 and Arch | met in containers; installed with apt in real Ubuntu 24.04 and 26.04 desktops (VMs) and with pacman in real Arch logins (VMs) and on Omarchy; upgrade with settings kept (PK-06) |

## Non-goals (0.9.0)

- Restoring application contents (documents, tabs, terminals) beyond what the
  application restores itself.
- Window placement on COSMIC (no platform API; see docs/compatibility/cosmic.md).
  The rest of COSMIC support is PARTIAL.
- wlroots compositors other than Sway and Hyprland (river, Wayfire, ...).
- Telemetry or any network access.

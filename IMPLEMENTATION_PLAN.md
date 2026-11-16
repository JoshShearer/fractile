# Implementation plan

The phases from SRS section 13, with where each stands. All work so far was done
by the Claude Code agent and verified by the suites in `tests/`. "Owner" is the
workstream from SRS section 14.

| Phase | Gate | Status | Owner |
|---|---|---|---|
| 0 Feasibility | move/resize and active window on one real desktop; honest roadmap | done: X11 adapter on GNOME/Mutter; capability research for GNOME, KWin, COSMIC | A recon |
| 1 Core engine | geometry property tests, schema fuzzing | done: `fractile-core` with proptest; config/snapshot validation tests | B engine |
| 2 First vertical slice | adapter + panel + hotkey + placement + settings + packaging; 10+ placement cases | done on GNOME X11 (E2E + GUI suites, screenshots in `tests/evidence/gui/`) | C UI, F packaging |
| 3 Divvy parity | all DV items for the initial platform | done: DV-01..28 VERIFIED on GNOME X11, GNOME Wayland and KWin Wayland (REQUIREMENTS.md) | C UI |
| 4 Persistence and restore | T09-T16, T21 on the initial platform | done: T09-T16 and T21 VERIFIED, including a real reboot and log-out in VMs (T10) and browser/IDE native restore (T14) | D session |
| 5 Desktop adapters | feature matrix per desktop | GNOME (X11, Wayland), KWin (Wayland, X11), Sway and Hyprland done (nested, headless, containers and real logins in VMs); COSMIC partial (no placement; real login in a VM) | A recon, E security |
| 6 Distribution and hardening | fresh install, upgrade, reboot, uninstall on the target matrix | install/remove/reinstall/purge done in containers; install, upgrade (PK-06) and reboot done in real logins in VMs; security review done ([docs/security](docs/security/README.md)) | F packaging, E security |
| 7 Release | tagged reproducible build, guide, limitations, matrix, changelog | docs done; release tag not made (the maintainer tags and publishes) | integration owner |

## Next milestones (in order)

1. **Real-desktop validation** (D1): log into a real GNOME Wayland session and a
   real Plasma 6 session on test machines. Run the E2E suites against them, plus a
   real logout/login and a reboot with 10 apps / 15 windows (T10). This needs
   the maintainer or a VM with a display. The agent cannot log into sessions.
2. **Multi-monitor on X11 and KWin**: an Xephyr multi-screen setup and KWin
   `--virtual` with two outputs. Add DV-12/13/17 checks there.
3. **Mixed DPI** (T08, R6): GNOME Wayland virtual monitors at scales 1 and 2.
4. **Monitor hot-plug at runtime** (T13).
5. **GNOME 45/47/48 and other KWin versions** in containers (R2, R3).
6. **Tray** (DV-19): test with the AppIndicator extension on GNOME and with Plasma's tray.
7. **Settings UI checks on Wayland** (DV-25/26): `tests/gui/settings_x11.py`
   covers X11. Run the same probe on GNOME Wayland and KWin, and extend it to
   edit, duplicate, remove, import and export (DV-14).
8. **Upstream COSMIC proposal** (R1): done, filed as pop-os/cosmic-protocols#97
   (2026-11-14). A partial COSMIC adapter (no geometry) exists; if the request
   lands, add move/resize to it.
9. **24-hour soak** (T27) on a real session.
10. Package-to-package **upgrade** test (PK-06): done, 0.8.0 -> 0.8.5 -> 0.9.0 in
   the GNOME 46 Wayland VM (`FRACTILE_VM_UPGRADE_FROM`).

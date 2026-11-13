# KDE Plasma (KWin)

| Session | Adapter | How |
|---|---|---|
| Plasma on **Wayland** | `kwin` (bridge) | The Fractile KWin script runs inside KWin and long-polls the Fractile service over the session bus (`io.fractile.Bridge1`). Global shortcuts go through KDE's KGlobalAccel. |
| Plasma on **X11** | `x11` | The Fractile service talks to the X server directly (EWMH, ICCCM, RandR). Tested with Plasma 5.27 (kwin_x11), the Plasma that has an X11 session. |

## Tested

| Environment | Result | Evidence |
|---|---|---|
| KWin 6.8.2, `kwin_wayland --virtual` (no Xwayland), in an `archlinux:latest` container with the host's NVIDIA render node passed in | all checks pass | `tests/evidence/e2e-kwin-wayland.json` (`tests/kwin/e2e-kwin-in-container.sh`) |
| kwin_x11 5.27.11 on Xvfb (no plasmashell), Ubuntu 24.04 container: the X11 suite | 44/44 | `tests/evidence/e2e-x11-kwin.json` (`tests/kwin-x11/e2e-kwin-x11-in-container.sh`) |
| Ubuntu 24.04.5 with kde-plasma-desktop (plasma-workspace 5.27.12, kwin-x11 5.27.11) in a QEMU/KVM VM, the Plasma X11 session started by GDM: `apt install`, start from the app menu, real log-out (Plasma's own) and log-in, real reboot, restore of 10 apps / 15 windows | 7/7 | `tests/evidence/vm-ubuntu24.04-plasma-x11.json` (`tests/vm/vm-e2e.sh plasma-x11`) |

This is a real KWin running real native Wayland GTK4 and Qt clients, the real
Fractile UI and real KGlobalAccel. It is **not** a full Plasma desktop: there is no
plasmashell, no Plasma panel and no System Settings, and there was no login.

Plasma 5.27 leaves `graphical-session.target` active across a log-out and
stops `plasma-workspace.target` instead, so `fractile.service` is bound to
both. At a Plasma log-out systemd stops the service while KWin and the X
server are still up, and the logout snapshot is taken then. Apps that take
part in KDE's own session management (most KDE apps) may already have closed
by then; Plasma's own session restore reopens those, and Fractile places what
is already open instead of launching it twice (seen in the VM run: 5 windows
were reopened by Plasma, 10 by Fractile).

**Not tested:** a real Plasma 6 login (Plasma 6 has no X11 session, and the
Wayland session needs the KWin script; only the headless KWin run above
covers it); KWin versions other than 6.8.2 and 5.27.11; multiple monitors on
KWin; System Settings' KWin Scripts page (the instructions below are from
KDE's documentation, not from a run).

## Setting it up

1. Install the package. The script is installed to
   `/usr/share/kwin/scripts/fractile/`, **disabled** (`EnabledByDefault: false`).
2. Enable "Fractile" in System Settings > Window Management > KWin Scripts.
3. `fractile status` should show `Window control: available`.

## Notes specific to KWin

- **Liveness.** A KWin script's D-Bus calls go out on KWin's own bus connection,
  so unloading the script closes nothing. The script keeps a long-poll open (one
  at a time, re-issued every 4 s at most). If no poll is in flight for 3 s, the
  service treats the script as gone and reports window control as unavailable
  (K14). If KWin exits, this is noticed at once through `NameOwnerChanged`.
- **Trust.** Calls to `io.fractile.Bridge1` are authenticated by the caller's
  pid. KWin is installed with the `cap_sys_nice` file capability, which hides its
  `/proc/<pid>/exe`. In that case the service requires the process name
  `kwin_wayland` *and* `cap_sys_nice` in its permitted set. See
  [security](../security/README.md).
- **Shortcuts** are registered with KGlobalAccel under the component
  `fractile`. They are unregistered when Fractile exits, so no dead entries
  remain in KDE's shortcut settings (K17).
- **Preview overlay.** KWin scripts cannot draw. On KWin the drag preview is
  drawn by the Fractile UI itself.
- **Session lock.** The script cannot see the lock screen, so Fractile relies on
  logind's `LockedHint` alone on KWin.

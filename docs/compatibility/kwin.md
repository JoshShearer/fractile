# KDE Plasma (KWin)

| Session | Adapter | How |
|---|---|---|
| Plasma on **Wayland** | `kwin` (bridge) | The Fractile KWin script runs inside KWin and long-polls the Fractile service over the session bus (`io.fractile.Bridge1`). Global shortcuts go through KDE's KGlobalAccel. |
| Plasma on **X11** | `kwin` bridge if the script is enabled, otherwise `x11` | **NOT TESTED** |

## Tested

| Environment | Result | Evidence |
|---|---|---|
| KWin 6.8.2, `kwin_wayland --virtual` (no Xwayland), in an `archlinux:latest` container with the host's NVIDIA render node passed in | all checks pass | `tests/evidence/e2e-kwin-wayland.json` (`tests/kwin/e2e-kwin-in-container.sh`) |

This is a real KWin running real native Wayland GTK4 and Qt clients, the real
Fractile UI and real KGlobalAccel. It is **not** a full Plasma desktop: there is no
plasmashell, no Plasma panel and no System Settings, and there was no login.

**Not tested:** a real Plasma login; Plasma on X11; KWin versions other than 6.8.2;
multiple monitors on KWin; System Settings' KWin Scripts page (the instructions
below are from KDE's documentation, not from a run); logout and reboot.

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

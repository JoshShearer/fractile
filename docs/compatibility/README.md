# Compatibility

| Desktop | Page | Summary |
|---|---|---|
| GNOME (X11 and Wayland) | [gnome.md](gnome.md) | works; Wayland needs the bundled extension enabled |
| KDE Plasma (Wayland) | [kwin.md](kwin.md) | works; needs the bundled KWin script enabled; Plasma X11 not tested |
| Any X11 window manager | [x11.md](x11.md) | works with EWMH window managers; tested with Mutter and Openbox |
| Hyprland 0.57+ | [hyprland.md](hyprland.md) | works through Hyprland's own IPC (nothing to enable); placed windows become floating; tested nested only, no real login |
| COSMIC | [cosmic.md](cosmic.md) | BLOCKED BY PLATFORM: no API to place other apps' windows |
| Other Wayland compositors (Sway, ...) | n/a | not supported; Fractile reports this instead of pretending |

The full matrix with versions is in [PLATFORM_MATRIX.md](../../PLATFORM_MATRIX.md).

Why Wayland needs an integration: Wayland gives ordinary programs no way to
list, move or resize other programs' windows. On Wayland, only code running
inside the compositor can do that, so Fractile ships a GNOME Shell extension and a
KWin script. Hyprland is the exception: its own IPC socket lets a program of
the same user list, move and resize windows, and Fractile uses that. XWayland does not help: it only reaches X11 apps, and only as an X
client.

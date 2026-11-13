# Hyprland test image: the KWin image (archlinux, Qt6/GTK4, Rust, Python)
# plus Hyprland, which runs nested inside kwin_wayland --virtual, and wtype
# (test-harness key presses through Hyprland's virtual-keyboard protocol).
FROM fractile-kwin-test
RUN pacman -Syu --noconfirm --needed hyprland jq wtype >/tmp/pacman.log 2>&1 || (tail -30 /tmp/pacman.log; exit 1)

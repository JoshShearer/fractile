# Sway test image: the KWin image (archlinux, Qt6/GTK4, Rust, Python) plus
# Sway, which runs nested inside kwin_wayland --virtual (wlroots' Wayland
# backend), and wtype (test-harness key presses through Sway's
# virtual-keyboard protocol).
FROM fractile-kwin-test
RUN pacman -Syu --noconfirm --needed sway jq wtype >/tmp/pacman.log 2>&1 || (tail -30 /tmp/pacman.log; exit 1)

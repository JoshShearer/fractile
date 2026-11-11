# Test bed for the KWin integration: a headless kwin_wayland (virtual
# backend) on Arch Linux. Used only by tests/kwin/e2e-kwin-in-container.sh.
FROM archlinux:latest
RUN pacman -Syu --noconfirm --needed kwin qt6-wayland python-pyqt6 python-gobject gtk4 \
      dbus rust base-devel sqlite qt6-tools >/tmp/pacman.log 2>&1 || (tail -30 /tmp/pacman.log; exit 1)
RUN useradd -m tester

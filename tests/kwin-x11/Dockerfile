# Test bed for Plasma X11: kwin_x11 (Plasma 5.27, as in Kubuntu 24.04) on
# Xvfb. Plasma 6.8 no longer has an X11 session, so this is the X11 KWin
# users have. Used only by tests/kwin-x11/e2e-kwin-x11-in-container.sh.
FROM ubuntu:24.04
RUN apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      kwin-x11 xvfb x11-utils x11-xserver-utils xdotool dbus dbus-x11 python3 python3-xlib python3-gi gir1.2-gtk-4.0 \
      python3-pyqt6 libglib2.0-bin procps sqlite3 >/tmp/apt.log 2>&1 || (tail -30 /tmp/apt.log; exit 1)
RUN useradd -m tester

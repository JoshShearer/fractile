# GNOME Shell 46 (as on Ubuntu 24.04 and Pop!_OS 24.04) for the X11 suites:
# gnome-shell --x11 with its real mutter WM on Xvfb, so nothing opens on the
# host display. Uses the host's debug build (same glibc 2.39).
# Used by tests/gnome46-x11/run.sh.
FROM ubuntu:24.04
RUN apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      gnome-shell xvfb x11-utils x11-xserver-utils xdotool dbus dbus-x11 python3 python3-xlib python3-gi \
      gir1.2-gtk-4.0 python3-pyqt6 at-spi2-core gir1.2-atspi-2.0 libglib2.0-bin procps sqlite3 \
      >/tmp/apt.log 2>&1 || (tail -30 /tmp/apt.log; exit 1)

#!/usr/bin/env bash
# Accessibility suite (UX-06, tests/gui/a11y_x11.py) under kwin_x11 on Xvfb
# in the kwin-x11 test image, as an unprivileged user. Needs the debug build
# (cargo build). Evidence: tests/evidence/a11y-kwin.json.
# Under GNOME: tests/gnome/x11-gnome-in-container.sh fedora:39 tests/gui/a11y_x11.py
set -euo pipefail
exec bash "$(dirname "$0")/gui-kwin-x11-in-container.sh" a11y_x11.py a11y-kwin.json

#!/usr/bin/env python3
"""Save a screenshot of the whole X display ($DISPLAY) to a PNG. Test helper."""
import sys

from PyQt6.QtGui import QGuiApplication

app = QGuiApplication(sys.argv[:1] + ["-platform", "xcb"])
scr = app.primaryScreen()
virt = scr.virtualGeometry()
pm = scr.grabWindow(0, virt.x(), virt.y(), virt.width(), virt.height())
out = sys.argv[1] if len(sys.argv) > 1 else "shot.png"
if not pm.save(out):
    sys.exit("could not save " + out)
print(out, pm.width(), "x", pm.height())

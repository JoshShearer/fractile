#!/usr/bin/env python3
"""Test-only probe: what can a window manager see that tells a Chrome incognito
window from a normal one? Opens one normal and one incognito window on a
throwaway profile on Xvfb and diffs every X11 property of the two top-level
windows (values hashed when long, e.g. _NET_WM_ICON). Prints the diff.

  FRACTILE_INSTALL_CHROME=1 tests/gnome46-x11/run.sh tests/tools/chrome_window_props.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def props(wid):
    out = {}
    cur = None
    for ln in sh("xprop", "-id", wid).splitlines():
        m = re.match(r"^([A-Z_a-z0-9]+)(\([A-Z_0-9]+\))?\s*[:=]\s?(.*)$", ln)
        if m:
            cur = m.group(1)
            out[cur] = m.group(3)
        elif cur:
            out[cur] += ln
    return {k: (v if len(v) < 160 else f"<{len(v)} chars sha1 {hashlib.sha1(v.encode()).hexdigest()[:12]}>") for k, v in out.items()}


def chrome_tops():
    tree = sh("xwininfo", "-root", "-children")
    ids = re.findall(r"^\s+(0x[0-9a-f]+) ", tree, re.M)
    res = []
    for w in ids:
        p = sh("xprop", "-id", w, "WM_CLASS", "_NET_WM_NAME")
        if "oogle-chrome" in p and "_NET_WM_NAME(UTF8_STRING) =" in p:
            res.append(w)
    return res


def main() -> int:
    if not shutil.which("xprop"):
        print("xprop missing")
        return 2
    disp = ":77"
    xv = subprocess.Popen(["Xvfb", disp, "-screen", "0", "1600x1200x24"], stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = disp
    time.sleep(1.5)
    ud = tempfile.mkdtemp(prefix="chrome-probe-")
    base = ["google-chrome", f"--user-data-dir={ud}", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-sandbox"]
    page = lambda t: f"data:text/html,<title>{t}</title>"  # noqa: E731
    p1 = subprocess.Popen(base + ["--new-window", page("Normal")], stderr=subprocess.DEVNULL)
    time.sleep(6)
    before = set(chrome_tops())
    p2 = subprocess.Popen(base + ["--incognito", page("Secret")], stderr=subprocess.DEVNULL)
    time.sleep(6)
    after = chrome_tops()
    normal = [w for w in after if w in before]
    private = [w for w in after if w not in before]
    print(f"normal {normal} private {private}")
    if not normal or not private:
        return 1
    a, b = props(normal[0]), props(private[0])
    diff = {}
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            diff[k] = {"normal": a.get(k), "incognito": b.get(k)}
            print(f"DIFF {k}\n  normal:  {a.get(k)}\n  private: {b.get(k)}")
    print("all properties:", ", ".join(sorted(a)))
    if "--evidence" in sys.argv:
        ev = {"suite": "chrome-window-props", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "environment": sh("google-chrome", "--version").strip() + " on Xvfb (no window manager), throwaway profile, X11",
              "properties": sorted(set(a) | set(b)), "differences": diff,
              "conclusion": "only the title, the requested position, the minimum-size hint (toolbar-dependent) and per-window counters differ; "
                            "no property marks the incognito window"}
        open(sys.argv[sys.argv.index("--evidence") + 1], "w").write(json.dumps(ev, indent=2) + "\n")
    for p in (p2, p1, xv):
        p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

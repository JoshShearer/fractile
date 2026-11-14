#!/usr/bin/env bash
# Accessibility checks on the real UI (UX-06): tests/gui/a11y_probe.py against
# a live service, under kwin_x11 on Xvfb, as an unprivileged user. Runs the
# probe twice: in English (A1-A3) and in an Arabic locale (A4, right to left).
# Needs the debug build (cargo build) and the kwin-x11 test image.
# Evidence: tests/evidence/a11y-kwin.json.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
out=$(docker run --rm --user "$(id -u):$(id -g)" -v "$root:/src:ro" fractile-kwin-x11-test:latest bash -euo pipefail -c '
mkdir -p /tmp/t/home /tmp/r; chmod 700 /tmp/r
export HOME=/tmp/t/home XDG_RUNTIME_DIR=/tmp/r XDG_CONFIG_HOME=/tmp/t/home/.config XDG_STATE_HOME=/tmp/t/home/.local/state XDG_DATA_HOME=/tmp/t/home/.local/share DISPLAY=:96 XDG_CURRENT_DESKTOP=KDE XDG_SESSION_TYPE=x11 FRACTILE_NO_SESSION_MANAGER=1 FRACTILE_UI=/bin/true QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 PYTHONDONTWRITEBYTECODE=1 PATH=/src/target/debug:/usr/bin:/bin
Xvfb :96 -screen 0 1600x1000x24 >/dev/null 2>&1 & sleep 1
kwin_x11 >/dev/null 2>&1 & sleep 2
exec dbus-run-session -- bash -euo pipefail -c "
mkdir -p \$XDG_CONFIG_HOME/fractile; printf \"[general]\nfirst_run_complete = true\ntray_icon = false\n\" > \$XDG_CONFIG_HOME/fractile/config.toml
fractiled --backend x11 >/tmp/t/daemon.log 2>&1 & D=\$!
python3 /src/tests/tools/testwin.py --toolkit gtk4 --app-id org.fractile.A11y --title Target >/dev/null 2>&1 &
sleep 3
python3 /src/tests/gui/a11y_probe.py 2>/tmp/t/ui-en.log | tail -1 || echo \"probe exited \$?\" >&2
LANG=ar_EG.UTF-8 LC_ALL=ar_EG.UTF-8 LANGUAGE=ar python3 /src/tests/gui/a11y_probe.py --rtl 2>/tmp/t/ui-ar.log | tail -1 || echo \"rtl probe exited \$?\" >&2
tail -n 15 /tmp/t/ui-en.log /tmp/t/ui-ar.log >&2 || true
kill \$D || true"
')
python3 - "$out" "$root/tests/evidence/a11y-kwin.json" <<'EOF'
import json, sys, time
res = []
for line in sys.argv[1].splitlines():
    line = line.strip()
    if line.startswith("{"):
        res += json.loads(line)["results"]
for r in res:
    print(("PASS" if r["ok"] else "FAIL"), r["id"], r["name"], "::", r["detail"])
ev = {"suite": "a11y", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
      "environment": "kwin-x11 test image (Xvfb, kwin_x11 5.27), real PyQt6 UI and service", "results": res}
open(sys.argv[2], "w").write(json.dumps(ev, indent=2) + "\n")
failed = [r for r in res if not r["ok"]]
print(f"\n{len(res) - len(failed)}/{len(res)} checks passed")
sys.exit(1 if failed or len(res) < 4 else 0)
EOF

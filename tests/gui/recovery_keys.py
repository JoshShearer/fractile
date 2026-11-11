#!/usr/bin/env python3
"""R01-R03: keyboard use of the "Restore your windows from last time?" dialog.

Found in the Ubuntu 24.04 VM (tests/vm, V05): the dialog opened with focus on
the first row's checkbox, and in that session's Qt style Enter toggled the
checkbox, so a user pressing Enter to accept the restore unticked a window
and nothing was restored. Runs the real dialog on Qt's offscreen platform
with a stand-in for the service; the style-specific part is covered by the VM
run, this guards the dialog's own key handling everywhere.

  tests/gui/recovery_keys.py [--evidence FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from fractile_ui.recovery import RecoveryDialog  # noqa: E402

results: list[dict] = []


def check(cid: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {cid} {name} :: {detail}")
    return ok


PLAN = {"snapshot": {"created_at": "2026-11-11T14:37:00", "windows": 2},
        "items": [{"label": "org.gnome.Terminal", "action": "will_reopen", "record_id": "r1", "app": "org.gnome.Terminal.desktop"},
                  {"label": "org.gnome.Calculator", "action": "will_reopen", "record_id": "r2", "app": "org.gnome.Calculator.desktop"}]}


class Bridge:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call_async(self, method, params, ok, err=None):
        self.calls.append((method, params))
        if method == "restore_plan":
            ok(PLAN)


class App:
    def __init__(self):
        self.bridge = Bridge()


def dialog():
    app = App()
    d = RecoveryDialog(app, {"snapshot_id": "s1", "reason": "confirm"})
    d.show()
    QTest.qWaitForWindowExposed(d)
    d.activateWindow()
    QTest.qWait(100)
    return d, app


def restores(app) -> list[dict]:
    return [p for m, p in app.bridge.calls if m == "restore_start"]


def ticks(d) -> list[bool]:
    return [cb.isChecked() for cb, _ in d.checks]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=str(ROOT / "tests/evidence/recovery-keys.json"))
    a = ap.parse_args()
    qa = QApplication(sys.argv[:1])

    d, app = dialog()
    fw = qa.focusWidget()
    QTest.keyClick(fw or d, Qt.Key.Key_Return)
    QTest.qWait(100)
    r = restores(app)
    check("R01", "the dialog opens on Restore selected; Enter restores everything that is ticked",
          fw is d.go and len(r) == 1 and r[0]["options"].get("selected") == ["r1", "r2"],
          f"focus {type(fw).__name__ if fw else None}; restore calls {len(r)}; ticks {ticks(d)}; params {json.dumps(r[0]) if r else None}")
    d.close()

    d, app = dialog()
    cb0 = d.checks[0][0]
    cb0.setFocus()
    QTest.qWait(50)
    QTest.keyClick(cb0, Qt.Key.Key_Return)
    QTest.qWait(100)
    r = restores(app)
    check("R02", "Enter on a row's checkbox restores too; it does not untick that row",
          len(r) == 1 and r[0]["options"].get("selected") == ["r1", "r2"],
          f"restore calls {len(r)}; selected {r[0]['options'].get('selected') if r else None}")
    d.close()

    d, app = dialog()
    cb0 = d.checks[0][0]
    cb0.setFocus()
    QTest.qWait(50)
    QTest.keyClick(cb0, Qt.Key.Key_Space)
    QTest.qWait(50)
    QTest.keyClick(d.go, Qt.Key.Key_Return)
    QTest.qWait(100)
    r = restores(app)
    sel = r[0]["options"].get("selected") if r else None
    check("R03", "Space still unticks a row, and the restore then leaves that window out",
          ticks(d) == [False, True] and len(r) == 1 and sel == ["r2"],
          f"ticks {ticks(d)}; restore calls {len(r)}; selected {sel}")
    d.close()

    Path(a.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(a.evidence).write_text(json.dumps({"suite": "recovery-keys", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                            "environment": f"Qt {qa.platformName()} platform, PyQt6", "results": results}, indent=2) + "\n")
    failed = [x for x in results if not x["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

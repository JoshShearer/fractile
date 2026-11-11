"""S16/S17: the shortcut pause cannot outlive its editor (shared by the
settings harnesses). A raw client stands in for an editor that crashes or
hangs."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
from fractile_ui.ipc import Client  # noqa: E402


def run(sock, status, check) -> None:
    c = Client("pause-probe", sock)
    c.call("hotkeys_pause", {"seconds": 60})
    during = status().get("hotkeys_paused")
    c.close()
    end = time.time() + 5
    while time.time() < end and status().get("hotkeys_paused"):
        time.sleep(0.1)
    check("S16", "an editor that disconnects while listening does not leave shortcuts paused",
          during is True and status().get("hotkeys_paused") is False, f"during={during} after_close={status().get('hotkeys_paused')}")
    c = Client("pause-probe", sock)
    try:
        c.call("hotkeys_pause", {"seconds": 1})
        time.sleep(2.5)
        after = status().get("hotkeys_paused")
    finally:
        c.close()
    check("S17", "a pause that is never ended times out and shortcuts come back", after is False and "panel" not in (status().get("hotkey_errors") or {}),
          f"after_timeout={after}")

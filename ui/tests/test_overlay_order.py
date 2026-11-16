"""The KWin preview window must end on the newest target even when the
compositor answers placement calls late (call_async runs on a thread pool,
so unserialized calls could be applied out of order)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fractile_ui.overlay import CompositorOverlay  # noqa: E402


class FakeClient:
    def windowTitle(self):
        return "Fractile preview"


class FakeBridge:
    def __init__(self):
        self.calls = []  # (rect, ok, fail), unanswered

    def call_async(self, method, params=None, ok=None, fail=None):
        assert method == "place_own_window"
        self.calls.append((params["rect"], ok, fail))


def r(x, w):
    return {"x": x, "y": 0, "w": w, "h": 100}


class OverlayOrder(unittest.TestCase):
    def setUp(self):
        self.b = FakeBridge()
        self.refocused = 0
        self.o = CompositorOverlay(self.b, refocus=self._refocus)
        self.o._client = FakeClient()
        self.o._visible = True

    def _refocus(self):
        self.refocused += 1

    def move(self, rect, first=False):
        key = (rect["x"], rect["y"], rect["w"], rect["h"], "")
        self.o._last = key
        self.o._place(rect, key, first, tries=10)

    def test_one_call_in_flight_and_newest_wins(self):
        self.move(r(0, 100), first=True)
        self.move(r(0, 200))
        self.move(r(0, 300))
        self.assertEqual(len(self.b.calls), 1, "only one placement may be in flight")
        rect, ok, _ = self.b.calls.pop(0)
        ok({})
        self.assertEqual(self.refocused, 1)
        self.assertEqual([c[0] for c in self.b.calls], [r(0, 300)], "the skipped 200 must not be sent")
        self.b.calls.pop(0)[1]({})
        self.assertEqual(self.b.calls, [])

    def test_hide_drops_pending(self):
        self.move(r(0, 100))
        self.move(r(0, 200))
        self.o.hide = CompositorOverlay.hide.__get__(self.o)
        self.o._client = type("C", (), {"hide": lambda s: None, "windowTitle": lambda s: "t"})()
        self.o.hide()
        self.b.calls.pop(0)[1]({})
        self.assertEqual(self.b.calls, [])


if __name__ == "__main__":
    unittest.main()

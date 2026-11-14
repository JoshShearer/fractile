"""What the shortcut editor says while it listens (no display needed)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fractile_ui.widgets import ShortcutEdit  # noqa: E402


class ListeningText(unittest.TestCase):
    def test_grab_taken(self):
        self.assertEqual(ShortcutEdit.listening_text(True, False), "Listening. Press the combination you want.")

    def test_desktop_held(self):
        # KDE holds its own shortcuts: no warning even though Wayland refused the grab
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertEqual(ShortcutEdit.listening_text(False, True), "Listening. Press the combination you want.")

    def test_wayland_without_hold_warns_about_desktop_keys(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            t = ShortcutEdit.listening_text(False, False)
        self.assertTrue(t.startswith("Listening."))
        self.assertIn("desktop uses itself", t)
        self.assertNotIn("held by another program", t)

    def test_x11_refused_grab_names_the_holder(self):
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIn("held by another program", ShortcutEdit.listening_text(False, False))


if __name__ == "__main__":
    unittest.main()

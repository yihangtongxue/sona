import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sona.window_lifecycle import ServiceShutdown
from sona.window_settings import WindowSettings
from sona.windows_lifecycle import WindowsLifecycle


class WindowSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_preference_and_notice_survive_restart(self):
        settings = WindowSettings(self.root)
        self.assertEqual(settings.get_close_action(), "tray")
        self.assertTrue(settings.notice_required())
        settings.set_close_action("quit")
        settings.acknowledge_notice()
        restored = WindowSettings(self.root)
        self.assertEqual(restored.get_close_action(), "quit")
        self.assertFalse(restored.notice_required())

    def test_invalid_file_uses_defaults(self):
        for content in ("invalid", "null", json.dumps({"close_action": [], "background_notice_seen": 1})):
            (self.root / "window.json").write_text(content)
            settings = WindowSettings(self.root)
            self.assertEqual(settings.get_close_action(), "tray")
            self.assertTrue(settings.notice_required())

    def test_failed_save_keeps_memory_and_file_consistent(self):
        settings = WindowSettings(self.root)
        settings.set_close_action("quit")
        with patch("sona.window_settings.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "无法保存"):
                settings.set_close_action("tray")
        self.assertEqual(settings.get_close_action(), "quit")
        self.assertEqual(WindowSettings(self.root).get_close_action(), "quit")
        self.assertEqual([path.name for path in self.root.iterdir()], ["window.json"])

    def test_invalid_action_never_writes(self):
        settings = WindowSettings(self.root)
        for action in (None, True, [], {}, "hide"):
            with self.assertRaises(ValueError):
                settings.set_close_action(action)
        self.assertFalse(settings.path.exists())


class ShutdownTests(unittest.TestCase):
    def test_one_service_failure_does_not_skip_others_or_repeat_shutdown(self):
        failing, remaining = Mock(), Mock()
        failing.close.side_effect = RuntimeError("worker failed")
        shutdown = ServiceShutdown(failing, remaining)
        with self.assertLogs("sona.window_lifecycle", level="ERROR"):
            shutdown()
        shutdown()
        failing.close.assert_called_once_with()
        remaining.close.assert_called_once_with()


class WindowsCloseTests(unittest.TestCase):
    """Exercise close reasons without loading WinForms or starting a GUI."""

    def setUp(self):
        forms = SimpleNamespace(CloseReason=SimpleNamespace(UserClosing="user"))
        self.modules = patch.dict("sys.modules", {"System.Windows.Forms": forms})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.controller = WindowsLifecycle.__new__(WindowsLifecycle)
        self.controller.quitting = False
        self.controller.ready = True
        self.controller.tray = SimpleNamespace(Visible=True)
        self.controller.settings = Mock()
        self.controller.settings.get_close_action.return_value = "tray"
        self.controller.native = Mock()
        self.controller.show_background_notice = Mock()

    def close(self, reason="user"):
        args = SimpleNamespace(CloseReason=reason, Cancel=False)
        self.controller.on_closing(None, args)
        return args

    def test_user_close_hides_without_forwarding_to_backend(self):
        self.assertTrue(self.close().Cancel)
        self.controller.native.Hide.assert_called_once_with()
        self.controller.native.on_closing.assert_not_called()
        self.assertFalse(self.controller.quitting)

    def test_shutdown_bypasses_tray(self):
        self.assertFalse(self.close("shutdown").Cancel)
        self.controller.native.Hide.assert_not_called()
        self.controller.native.on_closing.assert_called_once()
        self.assertTrue(self.controller.quitting)

    def test_explicit_quit_bypasses_tray(self):
        self.controller.quitting = True
        self.assertFalse(self.close().Cancel)
        self.controller.native.Hide.assert_not_called()

    def test_exit_preference_bypasses_tray(self):
        self.controller.settings.get_close_action.return_value = "quit"
        self.assertFalse(self.close().Cancel)
        self.controller.native.Hide.assert_not_called()

    def test_missing_tray_never_hides_window(self):
        self.controller.ready = False
        self.controller.tray = None
        self.assertFalse(self.close().Cancel)
        self.controller.native.Hide.assert_not_called()

    def test_failed_hide_falls_back_to_exit(self):
        self.controller.native.Hide.side_effect = RuntimeError("window failed")
        with self.assertLogs("sona.windows_lifecycle", level="ERROR"):
            self.assertFalse(self.close().Cancel)
        self.controller.native.on_closing.assert_called_once()


if __name__ == "__main__":
    unittest.main()

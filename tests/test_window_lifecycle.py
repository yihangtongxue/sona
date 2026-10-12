import unittest
from unittest.mock import Mock

from sona.window_lifecycle import ServiceShutdown


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


if __name__ == "__main__":
    unittest.main()

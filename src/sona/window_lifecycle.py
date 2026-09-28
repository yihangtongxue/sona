"""Separate closing the UI from shutting down its background services."""

import logging
import sys
from threading import Lock


logger = logging.getLogger(__name__)


class ServiceShutdown:
    """Shared by the native quit callback and the GUI loop's finally block."""

    def __init__(self, *services):
        self._services = services
        self._lock = Lock()
        self._finished = False

    def __call__(self):
        with self._lock:
            if self._finished:
                return
            logger.info("应用关闭，正在停止后台任务")
            for service in self._services:
                try:
                    service.close()
                except Exception:
                    # Still release the other workers if one service fails.
                    logger.exception("后台服务关闭失败 service=%s", type(service).__name__)
            self._finished = True
            logger.info("后台任务停止请求已发送，应用退出")


class WindowLifecycle:
    def __init__(self, window, settings, shutdown):
        self.window = window
        self.settings = settings
        self.shutdown = shutdown
        self.native = None
        window.events.before_show += self.attach

    def attach(self):
        try:
            if sys.platform == "darwin":
                from .macos_lifecycle import MacOSLifecycle

                self.native = MacOSLifecycle(self.window, self.shutdown)
            elif sys.platform == "win32":
                from .windows_lifecycle import WindowsLifecycle

                self.native = WindowsLifecycle(self.window, self.settings)
        except Exception:
            # A window must never become hidden without a working restore path.
            logger.exception("无法启用后台窗口模式，保留关闭窗口退出行为")

    def request_quit(self):
        if self.native is not None:
            self.native.request_quit()
        else:
            self.window.destroy()

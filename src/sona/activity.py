"""Prevent a restart from racing with a bridge operation or a queued job."""

import threading
from contextlib import contextmanager


class ActivityGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._frozen = False

    @contextmanager
    def operation(self, *, background=False):
        with self._lock:
            allowed = not self._frozen
            if not allowed and not background:
                raise ValueError("正在准备更新，请稍候。")
            if allowed:
                self._active += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._lock:
                    self._active -= 1

    def freeze(self):
        with self._lock:
            if self._active or self._frozen:
                raise ValueError("有任务正在处理，请完成后再更新。")
            self._frozen = True

    def thaw(self):
        with self._lock:
            self._frozen = False

"""Persist Windows close behavior and the one-time background notice."""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock


class WindowSettings:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "window.json"
        self._lock = Lock()
        self._record = {"close_action": "tray", "background_notice_seen": False}
        try:
            with self.path.open(encoding="utf-8") as stream:
                record = json.loads(stream.read(4096))
            if isinstance(record, dict):
                if record.get("close_action") in ("tray", "quit"):
                    self._record["close_action"] = record["close_action"]
                self._record["background_notice_seen"] = record.get("background_notice_seen") is True
        except (OSError, ValueError):
            pass

    def get_close_action(self):
        with self._lock:
            return self._record["close_action"]

    def set_close_action(self, action):
        if action not in ("tray", "quit"):
            raise ValueError("请选择进入系统托盘或退出应用。")
        self._save(close_action=action)
        return action

    def notice_required(self):
        with self._lock:
            return not self._record["background_notice_seen"]

    def acknowledge_notice(self):
        self._save(background_notice_seen=True)

    def _save(self, **changes):
        with self._lock:
            record = {**self._record, **changes}
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".window-", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
                self._record = record
            except OSError:
                raise ValueError("暂时无法保存窗口设置，请重试。") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

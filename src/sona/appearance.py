"""Persist the user's appearance preference independently of WebView storage."""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock


THEMES = ("system", "light", "dark")


class AppearanceSettings:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "appearance.json"
        self._lock = Lock()
        self.on_change = None
        self._theme = "system"
        try:
            with self.path.open(encoding="utf-8") as stream:
                record = json.loads(stream.read(4096))
            if isinstance(record, dict) and record.get("theme") in THEMES:
                self._theme = record["theme"]
        except (OSError, ValueError):
            pass

    def get_theme(self) -> str:
        with self._lock:
            return self._theme

    def set_theme(self, theme: str) -> str:
        if not isinstance(theme, str) or theme not in THEMES:
            raise ValueError("请选择跟随系统、浅色或深色。")
        with self._lock:
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".appearance-", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"theme": theme}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
                self._theme = theme
            except OSError:
                raise ValueError("暂时无法保存外观设置，请重试。") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        if self.on_change is not None:
            self.on_change()
        return theme

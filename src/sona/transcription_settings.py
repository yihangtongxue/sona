"""Persist the default Whisper recognition language."""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock

from .languages import AUTO, normalize_language


class TranscriptionSettings:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "transcription.json"
        self._lock = Lock()
        self._language = AUTO
        try:
            with self.path.open(encoding="utf-8") as stream:
                record = json.loads(stream.read(4096))
            if isinstance(record, dict):
                self._language = normalize_language(record.get("language"))
        except (OSError, ValueError):
            pass

    def get_language(self) -> str:
        with self._lock:
            return self._language

    def set_language(self, language) -> str:
        language = normalize_language(language)
        with self._lock:
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".transcription-", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"language": language}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
                temporary = None
                self._language = language
            except OSError:
                raise ValueError("暂时无法保存识别语言，请重试。") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return language

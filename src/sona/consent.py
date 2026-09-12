"""Versioned, local acknowledgement for sending text to the chosen AI endpoint."""

import json
import os
import tempfile
from pathlib import Path


class AIUsageConsent:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "ai-usage-consent.json"

    def required(self) -> bool:
        try:
            with self.path.open(encoding="utf-8") as stream:
                record = json.loads(stream.read(1024))
            return not (isinstance(record, dict) and type(record.get("version")) is int
                        and record["version"] == 1 and record.get("accepted") is True)
        except (OSError, ValueError):
            return True

    def require(self, confirmed: bool = False) -> None:
        if type(confirmed) is not bool:
            raise ValueError("请确认 AI 使用提示后再继续。")
        if not self.required():
            return
        if not confirmed:
            raise ValueError("首次使用 AI 整理前，请确认文字发送与费用提示。")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".ai-consent-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "accepted": True}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except OSError:
            raise ValueError("暂时无法保存确认，请稍后重试。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

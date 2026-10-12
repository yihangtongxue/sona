"""Transport-independent implementation of the MCP tools.

Kept free of MCP SDK imports so it can be unit tested and reused by the menu bar.
"""

from __future__ import annotations

import re
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .languages import AUTO, normalize_language
from .manuscript_export import short_clock
from .search import search as search_index

FETCHING = {"waiting_fetch", "resolving", "downloading", "importing"}
STATE_MAP = {
    "waiting_fetch": "fetching", "resolving": "fetching", "downloading": "fetching", "importing": "fetching",
    "interrupted": "failed", "cancelling": "cancelling",
}
DONE = {"completed", "failed", "cancelled"}
STATE_LABELS = {
    "fetching": "正在获取链接内容", "queued": "排队等待转录", "waiting_model": "等待转录模型",
    "transcribing": "正在转录", "cancelling": "正在取消", "completed": "已完成",
    "failed": "失败", "cancelled": "已取消",
}
MAX_WAIT_SECONDS = 50
DEFAULT_PAGE_CHARS = 60000
MAX_PAGE_CHARS = 200000


class SonaTools:
    def __init__(self, paths, audio_library, podcasts, transcription, activity=None, *, clock=time.monotonic,
                 sleep=time.sleep):
        self._paths = paths
        self._library = audio_library
        self._podcasts = podcasts
        self._transcription = transcription
        self._activity = activity
        self._clock = clock
        self._sleep = sleep

    def _operation(self):
        return self._activity.operation() if self._activity is not None else nullcontext()

    # ---- write -------------------------------------------------------------
    def transcribe(self, source: str, language: str | None = None, prefer_subtitles: bool = True) -> dict:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("请提供链接或本地文件的绝对路径。")
        source = source.strip()
        language = normalize_language(language)
        with self._operation():
            if re.match(r"(?i)^https?://", source):
                strategy = "subtitle_first" if prefer_subtitles else "transcribe"
                created = self._podcasts.create(source, strategy)
                identifier = created["id"]
                if not created.get("existing") and language != AUTO:
                    self._transcription.repository.set_language(identifier, language)
                return {"task_id": identifier, "kind": "link", "existing": bool(created.get("existing")),
                        "language": language,
                        "message": ("该链接已在 Sona 中，返回已有任务。" if created.get("existing")
                                    else "已开始获取并转录，请用 get_status 查询进度。")}
            path = self._local_path(source)

            def remember(identifier):
                if language != AUTO:
                    self._transcription.repository.set_language(identifier, language)

            imported = self._library.import_local_file(path, before_commit=remember)
            return {"task_id": imported["id"], "kind": "file", "existing": False, "language": language,
                    "message": "文件已复制到 Sona 并开始排队转录（原文件不变），请用 get_status 查询进度。"}

    @staticmethod
    def _local_path(source: str) -> Path:
        if source.lower().startswith("file://"):
            parts = urlsplit(source)
            if parts.netloc not in ("", "localhost"):
                raise ValueError("只支持本机文件。")
            source = unquote(parts.path)
        path = Path(source).expanduser()
        if not path.is_absolute():
            raise ValueError("本地文件需要使用绝对路径，例如 /Users/me/Downloads/meeting.m4a。")
        return path

    # ---- read --------------------------------------------------------------
    def _records(self) -> list[dict]:
        with self._operation():
            return self._library.list_files()

    def _find(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("请提供 task_id。")
        for record in self._records():
            if record["id"] == task_id.strip():
                return record
        raise ValueError(f"找不到任务 {task_id}，它可能已被删除。")

    @staticmethod
    def _summary(record: dict) -> dict:
        raw = record.get("transcription_status") or "queued"
        state = STATE_MAP.get(raw, raw)
        summary = {
            "task_id": record["id"], "name": record.get("name") or "", "state": state,
            "state_label": STATE_LABELS.get(state, state), "done": state in DONE,
            "has_transcript": bool(record.get("has_result")),
            "detail": record.get("transcription_detail") or "", "imported_at": record.get("imported_at"),
        }
        if record.get("transcription_error"):
            summary["error"] = record["transcription_error"]
        if record.get("source_url"):
            summary["source_url"] = record["source_url"]
            summary["platform"] = record.get("platform")
        if record.get("is_podcast_import") and record.get("total_bytes"):
            summary["downloaded_bytes"] = record.get("downloaded_bytes")
            summary["total_bytes"] = record.get("total_bytes")
        return summary

    def get_status(self, task_id: str, wait_seconds: float = 0) -> dict:
        try:
            wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
        except (TypeError, ValueError):
            raise ValueError("wait_seconds 必须是 0 到 50 之间的数字。") from None
        deadline = self._clock() + wait
        while True:
            summary = self._summary(self._find(task_id))
            if summary["done"] or self._clock() >= deadline:
                return summary
            self._sleep(min(2.0, max(0.1, deadline - self._clock())))

    def get_transcript(self, task_id: str, timestamps: bool = False, offset: int = 0,
                       max_chars: int = DEFAULT_PAGE_CHARS) -> dict:
        try:
            offset = max(0, int(offset or 0))
            max_chars = max(1000, min(int(max_chars or DEFAULT_PAGE_CHARS), MAX_PAGE_CHARS))
        except (TypeError, ValueError):
            raise ValueError("offset 和 max_chars 必须是整数。") from None
        with self._operation():
            try:
                result = self._transcription.repository.result(task_id)
            except ValueError:
                summary = self._summary(self._find(task_id))
                raise ValueError(f"转录尚未完成（{summary['state_label']}）。"
                                 "请稍后用 get_status 查询，完成后再获取全文。") from None
        segments = result.get("segments") or []
        if timestamps and segments:
            text = "\n".join(f"[{short_clock(segment['start'])}] {str(segment['text']).strip()}"
                             for segment in segments if str(segment.get("text", "")).strip())
        else:
            text = result.get("text") or ""
        page = text[offset:offset + max_chars]
        next_offset = offset + len(page) if offset + len(page) < len(text) else None
        response = {"task_id": task_id, "name": result.get("name") or "", "language": result.get("language"),
                    "duration_seconds": result.get("duration"), "timestamps": bool(timestamps and segments),
                    "source": result.get("source_kind") or "transcription", "total_chars": len(text),
                    "offset": offset, "next_offset": next_offset, "text": page}
        if result.get("source_url"):
            response["source_url"] = result["source_url"]
        if not text.strip():
            response["note"] = "未识别到语音。"
        return response

    def list_history(self, limit: int = 20, offset: int = 0) -> dict:
        try:
            limit = max(1, min(int(limit or 20), 100))
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            raise ValueError("limit 和 offset 必须是整数。") from None
        records = self._records()
        items = [self._summary(record) for record in records[offset:offset + limit]]
        return {"total": len(records), "offset": offset, "items": items,
                "next_offset": offset + limit if offset + limit < len(records) else None}

    def search(self, query: str, limit: int = 10) -> dict:
        try:
            limit = max(1, min(int(limit or 10), 50))
        except (TypeError, ValueError):
            raise ValueError("limit 必须是整数。") from None
        with self._operation():
            results = search_index(self._paths.database, query, limit)
        for item in results:
            if item["kind"] in ("transcription", "subtitle"):
                item["task_id"] = item["id"]
            else:
                item["manuscript_id"] = item["id"]
        return {"query": query, "results": results}

    def active_tasks(self, limit: int = 5) -> list[dict]:
        """Unfinished tasks for the menu bar."""
        active = [self._summary(record) for record in self._records()]
        return [item for item in active if not item["done"]][:limit]

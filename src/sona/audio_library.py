from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .file_lock import FileLocked, exclusive_file_lock


CHUNK_SIZE = 256 * 1024
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".wma"}
logger = logging.getLogger(__name__)


class AudioLibrary:
    """Local audio copies and committed history, isolated by the app data directory."""

    def __init__(self, database: Path, directory: Path) -> None:
        self._database = database
        self._directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._session: dict | None = None
        self._guard = None
        self._closed = False
        try:
            with self._file_lock():
                self._recover()
        except FileLocked:
            pass  # Another instance is importing; it owns the pending files.

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _file_lock(self):
        return exclusive_file_lock(self._directory / ".library.lock")

    def _path(self, record: dict) -> Path:
        # Paths are generated from validated IDs/extensions, never from display names.
        identifier = str(uuid.UUID(record["id"]))
        if record["suffix"] not in AUDIO_SUFFIXES:
            raise ValueError("音频格式不受支持。")
        return self._directory / f"{identifier}{record['suffix']}"

    def _insert(self, record: dict) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO audio_files (id, name, suffix, size_bytes, imported_at) "
                "VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
                (record["id"], record["name"], record["suffix"], record["size_bytes"], record.get("imported_at")),
            )

    def _recover(self) -> None:
        # The lock proves no live importer owns these files. Recover a completed
        # rename before cleaning an interrupted copy; never list partial files.
        marker = self._directory / ".pending.json"
        partial = self._directory / ".pending.audio"
        if marker.is_file():
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
                target = self._path(record)
                valid = isinstance(record["name"], str) and isinstance(record["size_bytes"], int) and record["size_bytes"] > 0
            except (ValueError, TypeError, KeyError, AttributeError):
                valid = False
            if valid and target.is_file() and target.stat().st_size == record["size_bytes"]:
                self._insert(record)
            partial.unlink(missing_ok=True)
            marker.unlink()
        else:
            partial.unlink(missing_ok=True)
        (self._directory / ".pending.json.tmp").unlink(missing_ok=True)
        # Deletions are first renamed to a tombstone. Restore on DB rollback,
        # or remove the tombstone after a committed deletion.
        for tombstone in self._directory.glob("*.deleted"):
            identifier = tombstone.stem
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM audio_files WHERE id=?", (identifier,)).fetchone()
            if row:
                os.replace(tombstone, self._path(dict(row)))
            else:
                tombstone.unlink()

    def list_files(self) -> list[dict]:
        with self._connection() as connection:
            records = [dict(row) for row in connection.execute(
                """SELECT a.*, t.status AS transcription_status, t.detail AS transcription_detail,
                    t.error AS transcription_error, t.model_id, t.device,
                    EXISTS(SELECT 1 FROM transcription_results r WHERE r.audio_id=a.id) AS has_result
                    FROM audio_files a LEFT JOIN transcription_tasks t ON t.audio_id=a.id
                    ORDER BY a.imported_at DESC, a.id DESC""",
            ).fetchall()]
        return [{**record, "available": self._path(record).is_file()} for record in records]

    def cleanup_completed(self) -> None:
        """Remove only managed copies whose completed transcripts are committed.

        Called by the queue owner between tasks, after worker processes exit.
        Completed rows themselves form a durable retry set: deletion is idempotent,
        and interrupted/failed cleanup is retried without deleting history/results.
        """
        with self._mutex:
            if self._closed or self._session:
                return
            try:
                with self._file_lock():
                    self._recover()
                    with self._connection() as connection:
                        records = connection.execute(
                            """SELECT a.id, a.suffix FROM audio_files a
                               JOIN transcription_tasks t ON t.audio_id=a.id
                               JOIN transcription_results r ON r.audio_id=a.id
                               WHERE t.status='completed'""",
                        ).fetchall()
                    for record in records:
                        try:
                            # _path validates the UUID and extension; display names
                            # and original user paths never participate in deletion.
                            self._path(dict(record)).unlink()
                        except FileNotFoundError:
                            continue
                        except (OSError, ValueError):
                            logger.warning('已完成音频副本清理失败，将稍后重试',
                                           extra={'task_id': record['id']})
                        else:
                            logger.info('已清理完成转录的音频副本，保留记录与文字结果',
                                        extra={'task_id': record['id']})
            except FileLocked:
                pass  # Another instance is importing or deleting; retry later.

    def begin_import(self, name: str, size: int) -> str:
        if not isinstance(name, str) or not name or len(name) > 255 or any(c in name for c in "\\/\0"):
            raise ValueError("文件名无效。")
        suffix = Path(name).suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            raise ValueError("请选择 MP3、WAV、M4A 等支持的音频文件。")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("音频文件不能为空。")
        with self._mutex:
            if self._closed:
                raise RuntimeError("应用正在关闭。")
            if self._session:
                raise RuntimeError("请等待当前音频导入完成。")
            guard = self._file_lock()
            try:
                guard.__enter__()
            except FileLocked as error:
                raise RuntimeError("另一个窗口正在管理音频，请稍后重试。") from error
            try:
                self._recover()
                identifier = str(uuid.uuid4())
                record = {"id": identifier, "name": name, "suffix": suffix, "size_bytes": size}
                marker = self._directory / ".pending.json.tmp"
                marker.write_text(json.dumps(record), encoding="utf-8")
                os.replace(marker, self._directory / ".pending.json")
                (self._directory / ".pending.audio").write_bytes(b"")
                self._session = {**record, "offset": 0}
                self._guard = guard
                return identifier
            except Exception:
                guard.__exit__(None, None, None)
                raise

    def _require_session(self, identifier: str) -> dict:
        if not self._session or self._session["id"] != identifier:
            raise ValueError("导入任务已结束，请重新选择文件。")
        return self._session

    def append_chunk(self, identifier: str, offset: int, encoded: str) -> None:
        if not isinstance(encoded, str) or len(encoded) > ((CHUNK_SIZE + 2) // 3) * 4:
            raise ValueError("音频数据块过大。")
        data = base64.b64decode(encoded, validate=True)
        with self._mutex:
            session = self._require_session(identifier)
            if offset != session["offset"] or not data or len(data) > CHUNK_SIZE:
                raise ValueError("音频数据顺序不正确，请重新导入。")
            if offset + len(data) > session["size_bytes"]:
                raise ValueError("音频大小与导入记录不一致。")
            with (self._directory / ".pending.audio").open("ab") as stream:
                stream.write(data)
            session["offset"] += len(data)

    def finish_import(self, identifier: str) -> dict:
        with self._mutex:
            session = self._require_session(identifier)
            if session["offset"] != session["size_bytes"]:
                raise ValueError("音频尚未传输完整。")
            pending = self._directory / ".pending.audio"
            if pending.stat().st_size != session["size_bytes"]:
                raise ValueError("保存的音频大小不正确。")
            os.replace(pending, self._path(session))
            try:
                self._insert(session)
            except Exception:
                os.replace(self._path(session), pending)
                raise
            try:
                (self._directory / ".pending.json").unlink(missing_ok=True)
            except OSError:
                # Data and history are already committed. Recovery can remove
                # this marker on the next operation without duplicating the row.
                pass
            finally:
                self._release_session()
            return {"id": identifier, "name": session["name"]}

    def _release_session(self) -> None:
        self._session = None
        guard, self._guard = self._guard, None
        if guard:
            guard.__exit__(None, None, None)

    def abort_import(self, identifier: str) -> None:
        with self._mutex:
            if self._session and self._session["id"] == identifier:
                try:
                    (self._directory / ".pending.audio").unlink(missing_ok=True)
                    (self._directory / ".pending.json").unlink(missing_ok=True)
                finally:
                    self._release_session()

    def _get(self, identifier: str) -> dict:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM audio_files WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("音频不存在，列表可能已更新。")
        return dict(row)

    def delete_file(self, identifier: str) -> None:
        with self._mutex:
            if self._session:
                raise RuntimeError("请等待音频导入完成后再删除。")
            try:
                with self._file_lock():
                    self._recover()
                    record = self._get(identifier)
                    path = self._path(record)
                    tombstone = self._directory / f"{record['id']}.deleted"
                    try:
                        with self._connection() as connection:
                            # Serialize task claiming with deletion. Running files
                            # must be cancelled and their process stopped first.
                            connection.execute("BEGIN IMMEDIATE")
                            task = connection.execute(
                                "SELECT status FROM transcription_tasks WHERE audio_id=?", (identifier,),
                            ).fetchone()
                            if task and task["status"] in {"transcribing", "cancelling"}:
                                raise RuntimeError("请先取消转录，等待结束后再删除音频。")
                            if path.exists():
                                os.replace(path, tombstone)
                            connection.execute("DELETE FROM audio_files WHERE id=?", (identifier,))
                    except Exception:
                        if tombstone.exists():
                            os.replace(tombstone, path)
                        raise
                    try:
                        tombstone.unlink(missing_ok=True)
                    except OSError:
                        # The DB deletion (including results) has committed. Do
                        # not recreate a record without its transcript. Recovery
                        # removes this tombstone the next time it holds the lock.
                        pass
            except FileLocked as error:
                raise RuntimeError("另一个窗口正在管理音频，请稍后重试。") from error

    def close(self) -> None:
        with self._mutex:
            self._closed = True
            if self._session:
                self.abort_import(self._session["id"])

    def is_importing(self) -> bool:
        with self._mutex:
            return self._session is not None

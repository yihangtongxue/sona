from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..models import ModelDefinition, ModelEvent


MANIFEST_NAME = "manifest.json"
LOCK_SUFFIX = ".lock"
CHUNK_SIZE = 1024 * 1024
IO_TIMEOUT = 10


class WhisperProvider:
    """Manage official Whisper checkpoints without loading or executing them."""

    def __init__(self, models_dir: Path, downloads_dir: Path) -> None:
        self._models_dir = models_dir
        self._downloads_dir = downloads_dir
        self._closed = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancellations: dict[str, threading.Event] = {}

    def run(
        self, action: str, model: ModelDefinition, emit: Callable[[ModelEvent], None],
    ) -> ModelEvent:
        if not model.artifact_url or not model.artifact_filename or not model.artifact_sha256:
            return ModelEvent("unsupported", "该模型没有可下载的资源描述。")
        if action == "status":
            return self._status(model)
        if action == "download":
            with self._cancel_lock:
                cancelled = self._cancellations.setdefault(model.id, threading.Event())
            try:
                return self._download(model, emit, cancelled)
            finally:
                with self._cancel_lock:
                    self._cancellations.pop(model.id, None)
        if action == "delete":
            return self._delete(model)
        raise ValueError("不支持的 Whisper 模型操作。")

    def close(self) -> None:
        self._closed.set()

    def cancel(self, model_id: str) -> None:
        with self._cancel_lock:
            cancelled = self._cancellations.get(model_id)
            if cancelled is not None:
                cancelled.set()

    def _check_cancelled(self, cancelled: threading.Event) -> None:
        if self._closed.is_set() or cancelled.is_set():
            raise _DownloadCancelled()

    def _has_files(self, model: ModelDefinition) -> bool:
        return self._target_dir(model).exists() or self._partial_dir(model).exists()

    def _status(self, model: ModelDefinition) -> ModelEvent:
        target = self._target_dir(model)
        manifest = _read_manifest(target / MANIFEST_NAME)
        artifact = target / model.artifact_filename
        if _is_installed(model, manifest, artifact):
            size = artifact.stat().st_size
            return ModelEvent(
                "installed", "模型文件已安装。", resource_path=str(target),
                downloaded_bytes=size, total_bytes=size, artifact_version=model.artifact_version,
                has_files=True,
            )
        partial = self._partial_file(model)
        if partial.is_file() and partial.stat().st_size:
            return ModelEvent(
                "supported", "发现未完成下载；点击下载可继续。",
                downloaded_bytes=partial.stat().st_size, artifact_version=model.artifact_version,
                has_files=True,
            )
        has_files = self._has_files(model)
        return ModelEvent(
            "supported", "发现不完整的模型资源，可重新下载或清理文件。" if has_files else "模型可供下载。",
            downloaded_bytes=0, total_bytes=0, artifact_version=model.artifact_version,
            has_files=has_files,
        )

    def _download(
        self, model: ModelDefinition, emit: Callable[[ModelEvent], None], cancelled: threading.Event,
    ) -> ModelEvent:
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._model_lock(model):
                self._check_cancelled(cancelled)
                # Recheck inside the lock: another instance may have just installed it.
                current = self._status(model)
                if current.status == "installed":
                    return current
                emit(ModelEvent("preparing", "正在准备模型下载。", artifact_version=model.artifact_version))
                partial_dir = self._partial_dir(model)
                partial_dir.mkdir(parents=True, exist_ok=True)
                partial = self._partial_file(model)
                try:
                    downloaded, total = self._transfer(model, partial, emit, cancelled)
                except urllib.error.HTTPError as error:
                    # A completed partial file can receive HTTP 416 after an app
                    # closes between receiving the last byte and writing its manifest.
                    if error.code != 416 or not partial.is_file():
                        raise
                    if _sha256(partial, lambda: self._check_cancelled(cancelled)) == model.artifact_sha256:
                        downloaded = partial.stat().st_size
                        total = downloaded
                    else:
                        partial.unlink()
                        downloaded, total = self._transfer(model, partial, emit, cancelled)
                self._check_cancelled(cancelled)
                emit(ModelEvent(
                    "verifying", "正在校验下载的模型文件。", downloaded_bytes=downloaded,
                    total_bytes=total, artifact_version=model.artifact_version,
                    has_files=True,
                ))
                checksum = _sha256(partial, lambda: self._check_cancelled(cancelled))
                if checksum != model.artifact_sha256:
                    partial.unlink(missing_ok=True)
                    return ModelEvent(
                        "failed", "模型文件校验失败，已移除损坏的下载。",
                        error=f"SHA-256 不匹配：期望 {model.artifact_sha256}，实际 {checksum}",
                        artifact_version=model.artifact_version,
                        downloaded_bytes=0, total_bytes=0, has_files=True,
                    )
                self._check_cancelled(cancelled)
                manifest = {
                    "schema_version": 1,
                    "model_id": model.id,
                    "artifact_version": model.artifact_version,
                    "source_url": model.artifact_url,
                    "filename": model.artifact_filename,
                    "sha256": model.artifact_sha256,
                    "size_bytes": downloaded,
                }
                (partial_dir / MANIFEST_NAME).write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                target = self._target_dir(model)
                if target.exists():
                    shutil.rmtree(target)
                os.replace(partial_dir, target)
                return ModelEvent(
                    "installed", "模型已下载并完成校验。", resource_path=str(target),
                    downloaded_bytes=downloaded, total_bytes=downloaded,
                    artifact_version=model.artifact_version,
                    has_files=True,
                )
        except _ModelLocked:
            return ModelEvent("locked", "另一个 Sona 实例正在管理这个模型，请稍后刷新。")
        except _DownloadCancelled:
            return self._paused(model)
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as error:
            if self._closed.is_set() or cancelled.is_set():
                return self._paused(model)
            return ModelEvent(
                "failed", "模型下载失败，可稍后重试。", error=f"{type(error).__name__}: {error}",
                artifact_version=model.artifact_version, has_files=self._has_files(model),
                downloaded_bytes=self._partial_size(model),
            )

    def _partial_size(self, model: ModelDefinition) -> int:
        try:
            return self._partial_file(model).stat().st_size
        except FileNotFoundError:
            return 0

    def _paused(self, model: ModelDefinition) -> ModelEvent:
        return ModelEvent(
            "paused", "下载已暂停，可继续下载或清理文件。",
            downloaded_bytes=self._partial_size(model), artifact_version=model.artifact_version,
            has_files=self._has_files(model),
        )

    def _transfer(
        self, model: ModelDefinition, partial: Path, emit: Callable[[ModelEvent], None],
        cancelled: threading.Event,
    ) -> tuple[int, int | None]:
        existing = partial.stat().st_size if partial.is_file() else 0
        headers = {"User-Agent": "Sona model manager/0.1"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(model.artifact_url, headers=headers)
        self._check_cancelled(cancelled)
        with urllib.request.urlopen(request, timeout=IO_TIMEOUT) as response:
            status = response.getcode()
            resumed = status == 206 and existing > 0
            if not resumed:
                existing = 0
            content_length = response.headers.get("Content-Length")
            remaining = int(content_length) if content_length and content_length.isdigit() else None
            total = existing + remaining if remaining is not None else None
            if status == 206:
                content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
                if not content_range:
                    raise ValueError("下载源未返回有效的续传范围，已保留原下载文件。")
                start, end = int(content_range[1]), int(content_range[2])
                if start != existing or end < start or (remaining is not None and remaining != end - start + 1):
                    raise ValueError("下载源返回的续传范围不匹配，已保留原下载文件。")
                if content_range[3] != "*":
                    total = int(content_range[3])
                    if end >= total:
                        raise ValueError("下载源返回的文件大小不正确。")
            mode = "ab" if resumed else "wb"
            downloaded = existing
            last_emit = 0.0
            emit(ModelEvent(
                "downloading", "正在下载模型文件。", downloaded_bytes=downloaded,
                total_bytes=total if total is not None else 0,
                artifact_version=model.artifact_version, has_files=True,
            ))
            # HTTPResponse.read1 returns available data without filling the whole MiB.
            read = getattr(response, "read1", response.read)
            self._check_cancelled(cancelled)
            with partial.open(mode) as output:
                while True:
                    self._check_cancelled(cancelled)
                    chunk = read(CHUNK_SIZE)
                    self._check_cancelled(cancelled)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.2:
                        emit(ModelEvent(
                            "downloading", "正在下载模型文件。",
                            progress=downloaded / total if total else None,
                            downloaded_bytes=downloaded, total_bytes=total,
                            artifact_version=model.artifact_version,
                            has_files=True,
                        ))
                        last_emit = now
            if total is not None and downloaded != total:
                raise OSError("下载连接提前结束，已保留文件，可继续下载。")
        emit(ModelEvent(
            "downloading", "模型文件下载完成，正在校验。", progress=1,
            downloaded_bytes=downloaded, total_bytes=total, artifact_version=model.artifact_version,
            has_files=True,
        ))
        return downloaded, total

    def _delete(self, model: ModelDefinition) -> ModelEvent:
        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._model_lock(model):
                _remove_directory(self._target_dir(model))
                _remove_directory(self._partial_dir(model))
                return ModelEvent(
                    "supported", "模型已从本机移除。", downloaded_bytes=0, total_bytes=0,
                    artifact_version=model.artifact_version,
                    has_files=False,
                )
        except _ModelLocked:
            return ModelEvent("locked", "另一个 Sona 实例正在管理这个模型，请稍后刷新。")
        except OSError as error:
            return ModelEvent(
                "failed", "未能完整删除模型文件，请检查文件占用和目录权限后重试。",
                error=f"{type(error).__name__}: {error}", has_files=self._has_files(model),
                downloaded_bytes=self._partial_size(model),
            )

    def _target_dir(self, model: ModelDefinition) -> Path:
        return self._models_dir / model.id

    def _partial_dir(self, model: ModelDefinition) -> Path:
        return self._downloads_dir / f"{model.id}.partial"

    def _partial_file(self, model: ModelDefinition) -> Path:
        return self._partial_dir(model) / model.artifact_filename

    @contextmanager
    def _model_lock(self, model: ModelDefinition) -> Iterator[None]:
        lock = self._downloads_dir / f"{model.id}{LOCK_SUFFIX}"
        # Never unlink this file: waiters must all lock the same inode. The OS
        # releases the lock on close or process death, including forced exits.
        with lock.open("a+b") as output:
            if os.fstat(output.fileno()).st_size == 0:
                output.write(b"\0")
                output.flush()
            output.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(output.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise _ModelLocked() from error
                    raise
                try:
                    yield
                finally:
                    output.seek(0)
                    msvcrt.locking(output.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise _ModelLocked() from error
                try:
                    yield
                finally:
                    fcntl.flock(output.fileno(), fcntl.LOCK_UN)


class _ModelLocked(Exception):
    pass


class _DownloadCancelled(Exception):
    pass


def _remove_directory(path: Path) -> None:
    # Ignore only an already absent root, never permission errors or a partial removal.
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        if path.exists():
            raise
    if path.exists():
        raise OSError(f"文件夹仍然存在：{path}")


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_installed(
    model: ModelDefinition, manifest: dict[str, object] | None, artifact: Path,
) -> bool:
    if not manifest or not artifact.is_file():
        return False
    return (
        manifest.get("schema_version") == 1
        and manifest.get("model_id") == model.id
        and manifest.get("artifact_version") == model.artifact_version
        and manifest.get("filename") == model.artifact_filename
        and manifest.get("sha256") == model.artifact_sha256
        and manifest.get("size_bytes") == artifact.stat().st_size
    )


def _sha256(path: Path, check_cancelled: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            if check_cancelled is not None:
                check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()

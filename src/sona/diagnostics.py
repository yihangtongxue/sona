"""Bounded local diagnostics: allowlisted metadata, never raw log messages."""

import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import re
import tempfile
import time
import zipfile

from .file_lock import FileLocked, exclusive_file_lock
from .version import VERSION

MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_FILES = ("sona.log", "sona.log.1", "sona.log.2")
SAFE_VALUES = frozenset({
    "-", "acceleration", "darwin", "win32", "linux", "arm64", "x86_64", "AMD64",
    "production", "development", "test", "CPU", "cpu", "cuda", "Apple-GPU",
    "apple-speech", "mlx-whisper", "faster-whisper", "float16", "float32", "int8",
    "installed", "not_installed", "downloading", "paused", "unsupported", "unknown",
    "waiting_model", "queued", "transcribing", "cancelling", "completed", "failed",
    "cancelled", "ready", "untested", "optimizing", "status", "download", "select",
    "delete", "pause", "enable", "disable", "check", "stop", "length", "content_filter",
    "sensitive", "network_error", "model_context_window_exceeded", "tool_calls",
    "update_tls_certificate", "update_tls_connection", "update_timeout", "update_dns",
    "update_manifest_json", "update_connection", "update_unknown",
    "resolving", "processing", "importing",
    "media_unknown", "media_invalid_url", "media_unsupported_port", "media_private_address",
    "media_dns_failed", "media_response_too_large",
    "media_empty_response", "EmptyMediaResponseError", "ContentTooShortError",
    "MediaRequestError", "MediaImportError", "RequestError", "HTTPError", "TransportError",
    "DownloadError", "ExtractorError", "NoSupportingHandlers", "UnsupportedRequest",
    "SSLError", "SSLCertVerificationError", "CertificateVerifyError", "ProxyError",
    "TimeoutError", "ConnectionError", "ConnectionResetError", "gaierror",
    "ValueError", "KeyError", "TypeError", "AttributeError", "OSError", "PermissionError",
    "FileNotFoundError", "ModuleNotFoundError",
})
UUID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


def safe_value(value):
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str and (value in SAFE_VALUES or UUID_PATTERN.fullmatch(value)):
        return value
    return "[omitted]"


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        # Raw messages, exception messages, traceback source lines and local
        # variables may contain user text or secrets. Do not serialize them.
        arguments = record.args if isinstance(record.args, tuple) else ()
        data = {
            "version": VERSION,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname, "module": record.name,
            "function": record.funcName, "line": record.lineno, "pid": record.process,
            "task": safe_value(getattr(record, "task_id", "-")),
            "values": [safe_value(value) for value in arguments[:16]],
        }
        if record.exc_info and record.exc_info[0]:
            data["exception"] = record.exc_info[0].__name__
            if isinstance(record.exc_info[1], OSError):
                data["errno"] = safe_value(record.exc_info[1].errno)
            frames = []
            trace = record.exc_info[2]
            while trace is not None and len(frames) < 20:
                frames.append({"function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
                trace = trace.tb_next
            data["frames"] = frames
        return json.dumps(data, ensure_ascii=False)


class DiagnosticHandler(logging.Handler):
    def __init__(self, directory: Path):
        super().__init__(logging.INFO)
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.setFormatter(DiagnosticFormatter())
        self._sona_diagnostics = True

    def emit(self, record):
        try:
            # Each process opens the file only while holding the same lock, so
            # rotation never leaves a worker writing to an old file descriptor.
            with exclusive_file_lock(self.directory / ".write.lock"):
                path = self.directory / LOG_FILES[0]
                if path.is_symlink():
                    return
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.close(descriptor)
                handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=2,
                                              encoding="utf-8", delay=True)
                try:
                    # Write the already-safe line; never let logging's error
                    # handler print the original record when disk writes fail.
                    line = self.format(record)
                    if path.stat().st_size + len(line.encode("utf-8")) + 1 > MAX_LOG_BYTES:
                        handler.doRollover()
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                        stream.write(line + "\n")
                finally:
                    handler.close()
        except (OSError, FileLocked, ValueError, TypeError):
            # Diagnostics are best-effort: full disks/lock contention must not
            # stop transcription or turn an otherwise successful action into an error.
            pass


def export_bundle(directory: Path, destination: Path) -> None:
    snapshots = {}
    try:
        with exclusive_file_lock(directory / ".write.lock"):
            for name in LOG_FILES:
                path = directory / name
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as stream:
                        snapshots[name] = stream.read(MAX_LOG_BYTES)
    except (OSError, FileLocked):
        raise ValueError("暂时无法读取日志，请稍后重试。") from None
    if not snapshots:
        raise ValueError("暂无可导出的日志，请重新打开应用后再试。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".sona-logs-", delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in snapshots.items():
                archive.writestr(name, content)
            archive.writestr("info.json", json.dumps({"version": VERSION, "system": platform.system(),
                "architecture": platform.machine(), "python": platform.python_version()}, indent=2))
        temporary.replace(destination)
    except OSError:
        raise ValueError("日志保存失败，请选择可写的位置并检查剩余空间。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

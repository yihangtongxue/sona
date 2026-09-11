from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from ..models import ModelDefinition, ModelEvent


HELPER_SOURCE = Path(__file__).resolve().parents[1] / "native" / "speech_asset_manager.swift"
EVENT_STATUSES = {
    "checking", "preparing", "downloading", "verifying", "installed",
    "waiting", "supported", "unsupported", "failed", "unknown",
}
FINAL_STATUSES = {"installed", "waiting", "supported", "unsupported", "failed", "unknown"}


class AppleSpeechProvider:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()
        self._closed = False

    def run(
        self, action: str, model: ModelDefinition, emit: Callable[[ModelEvent], None],
    ) -> ModelEvent:
        if action not in {"status", "download"}:
            raise ValueError("不支持的 Apple Speech 操作。")
        if platform.system() != "Darwin":
            return ModelEvent("unsupported", "Apple Speech 仅支持 macOS。")
        version = platform.mac_ver()[0]
        if not version or int(version.split(".")[0]) < 26:
            return ModelEvent("unsupported", "此模型需要 macOS 26 或更高版本。")
        command = _helper_command()
        if command is None:
            return ModelEvent(
                "unknown", "无法启动语音资源工具。",
                error="开发环境需要 Xcode Command Line Tools，或通过 SONA_SPEECH_HELPER 指定原生工具。",
            )

        # stderr is separate from newline-delimited JSON to avoid blocking the child.
        with tempfile.TemporaryFile(mode="w+b") as errors:
            with self._lock:
                if self._closed:
                    return ModelEvent("unknown", "应用正在关闭。")
                process = subprocess.Popen(
                    [*command, action, model.locale], stdout=subprocess.PIPE,
                    stderr=errors, text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                self._processes.add(process)

            timed_out = threading.Event()

            def stop_after_timeout() -> None:
                if process.poll() is None:
                    timed_out.set()
                    _terminate(process)

            timer = threading.Timer(120 if action == "status" else 3600, stop_after_timeout)
            timer.daemon = True
            last_event: ModelEvent | None = None
            try:
                timer.start()
                assert process.stdout is not None
                for line in process.stdout:
                    event = _parse_event(line)
                    if event is not None:
                        last_event = event
                        emit(event)
                returncode = process.wait()
            finally:
                timer.cancel()
                _terminate(process)
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                with self._lock:
                    self._processes.discard(process)

            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 16000))
            diagnostics = errors.read().decode("utf-8", errors="replace").strip()

        if timed_out.is_set():
            return ModelEvent(
                "unknown", "系统响应超时，请稍后重试。",
                error=diagnostics or "本次请求已停止等待；已提交的系统下载可能仍在继续。",
            )
        if returncode != 0:
            detail = "语音资源工具运行失败。" if last_event else "语音资源工具未能启动。"
            return ModelEvent("unknown", detail, error=diagnostics or f"工具退出码：{returncode}")
        if last_event is None or last_event.status not in FINAL_STATUSES:
            return ModelEvent("unknown", "无法确认模型资源状态。", error=diagnostics or "工具未返回最终状态。")
        return last_event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for process in self._processes:
                _terminate(process)

    def cancel(self, model_id: str) -> None:
        raise ValueError("Apple 资源由系统管理，无法在应用内暂停。")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _parse_event(line: str) -> ModelEvent | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if not isinstance(status, str) or status not in EVENT_STATUSES:
        return None
    if status == "installed" and payload.get("protocol_version") != 2:
        return ModelEvent("unknown", "原生语音工具版本过旧，请更新后重试。",
                          error="请更新 SONA_SPEECH_HELPER 或应用附带的 speech_asset_manager，当前工具尚不支持转录。")
    progress = payload.get("progress")
    if (isinstance(progress, bool) or not isinstance(progress, (int, float))
            or not math.isfinite(progress)):
        progress = None
    return ModelEvent(
        status=status, detail=str(payload.get("detail", "")),
        progress=None if progress is None else max(0, min(1, progress)),
        error=str(payload.get("error") or ""),
    )


def _helper_command() -> list[str] | None:
    configured_helper = os.environ.get("SONA_SPEECH_HELPER")
    if configured_helper:
        return [configured_helper]
    bundled_helper = HELPER_SOURCE.with_suffix("")
    if bundled_helper.is_file() and os.access(bundled_helper, os.X_OK):
        return [str(bundled_helper)]
    xcrun_path = shutil.which("xcrun")
    if xcrun_path and HELPER_SOURCE.is_file():
        return [xcrun_path, "swift", str(HELPER_SOURCE)]
    return None

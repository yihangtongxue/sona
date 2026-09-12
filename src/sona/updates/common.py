"""Platform-neutral update job metadata and non-destructive process checks."""

import json
import os
from pathlib import Path

from .protocol import UpdateError
from .signatures import verify_archive


def process_running(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    # os.kill(pid, 0) is not a Windows liveness probe: it can terminate a process.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        raise OSError("无法检查应用进程状态。")
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise OSError("无法等待应用进程退出。")
        return result == 258  # WAIT_TIMEOUT
    finally:
        kernel.CloseHandle(handle)


def read_job_json(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise UpdateError("更新任务元数据过大。")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise UpdateError("更新任务元数据无效。")
    return record


def verify_job_archive(directory, version, *, platform, architecture):
    record = read_job_json(directory / "release.json")
    asset = record.get("asset", {})
    if (record.get("version") != version or not isinstance(asset, dict)
            or asset.get("platform") != platform or asset.get("architecture") != architecture
            or asset.get("packageType") != "zip"):
        raise UpdateError("更新任务版本或目标平台不一致。")
    verify_archive(directory / "update.zip", version, asset)


def write_state(directory, **state):
    state["helper_pid"] = os.getpid()
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / "result.json")

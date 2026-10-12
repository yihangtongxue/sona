"""Platform-neutral update job metadata and non-destructive process checks."""

import json
import os
from pathlib import Path

from .protocol import UpdateError
from .signatures import verify_archive


def process_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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

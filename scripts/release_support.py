"""Shared release identity checks; importing this module never builds anything."""

import importlib.metadata
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sona.updates.protocol import version_tuple
from sona.updates.signatures import read_public_key
from sona.version import RELEASE_REPOSITORY, VERSION

PUBLIC_KEY = ROOT / "src/sona/updates/update-public-key.json"
PYINSTALLER_VERSION = "6.22.2"


def release_requested():
    event = os.environ.get("GITHUB_EVENT_NAME")
    return event == "push" or (event == "workflow_dispatch" and os.environ.get("SONA_PUBLISH_RELEASE") == "true")


def check_release_context():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not release_requested()
            or os.environ.get("GITHUB_REF") != f"refs/tags/v{VERSION}"
            or os.environ.get("GITHUB_REPOSITORY") != RELEASE_REPOSITORY.removeprefix("https://github.com/")):
        raise ValueError("签名发布只允许在正式仓库的对应版本标签下执行。")


def check_version(version):
    version_tuple(version)
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(package for package in lock["package"] if package["name"] == "sona")
    if version != VERSION or metadata["project"]["version"] != VERSION or locked["version"] != VERSION:
        raise ValueError("版本标签、源码、项目配置和锁文件版本必须一致。")


def check_build(version, public_path):
    check_version(version)
    if importlib.metadata.version("pyinstaller") != PYINSTALLER_VERSION:
        raise ValueError(f"请使用 PyInstaller {PYINSTALLER_VERSION}。")
    public = read_public_key(public_path)
    if public != read_public_key(PUBLIC_KEY):
        raise ValueError("打包公钥必须与仓库中固定的发布公钥一致。")
    record = json.loads(public_path.read_text(encoding="utf-8"))
    if set(record) != {"algorithm", "keyId", "publicKey"}:
        raise ValueError("公钥文件包含多余字段。")
    return record

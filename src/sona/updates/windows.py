"""Replace a per-user Windows installation from a signed ZIP using the old app."""

import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from ..file_lock import exclusive_file_lock
from ..paths import get_app_paths
from ..version import BUNDLE_ID, RELEASE_REPOSITORY, UPDATE_MANIFEST_URL, VERSION
from .common import process_running, read_job_json, verify_job_archive, write_state
from .protocol import UpdateError, version_tuple
from .signatures import SignatureError, read_public_key

MAX_EXPANDED_BYTES = 10 * 1024**3
RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def installation_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs/Sona"


def check_target(target):
    expected = installation_root() / "app"
    if target != expected or any(p.is_symlink() or p.is_junction() for p in (target, *target.parents)):
        raise UpdateError("请使用 Windows 安装程序安装到默认目录后再更新。")
    if not target.is_dir() or not os.access(target, os.W_OK) or not os.access(target.parent, os.W_OK):
        raise UpdateError("应用安装目录不可写，请重新运行当前用户安装程序。")


def installed_bundle():
    if (sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}
            or not getattr(sys, "frozen", False)):
        raise UpdateError("自动安装仅在 Windows x64 正式安装版中可用。")
    if os.environ.get("SONA_DATA_DIR") or os.environ.get("SONA_ENV", "production") != "production":
        raise UpdateError("自定义数据环境不执行自动安装，请使用正式环境。")
    executable = Path(sys.executable).absolute()
    target = executable.parent
    check_target(target)
    if executable.name != "Sona.exe":
        raise UpdateError("当前程序不是 Sona 正式安装版。")
    return target


def app_info(app):
    try:
        info = read_job_json(app / "release-info.json")
        if (info.get("appId") != BUNDLE_ID or info.get("platform") != "windows"
                or info.get("architecture") != "x64" or info.get("executable") != "Sona.exe"
                or info.get("releaseRepository") != RELEASE_REPOSITORY
                or info.get("updateManifestURL") != UPDATE_MANIFEST_URL):
            raise ValueError()
        version_tuple(info.get("version"))
        return info
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError("安装包不是有效的 Windows x64 Sona 应用。") from None


def validate_app(app, requirement, expected_version):
    info = app_info(app)
    public = read_public_key(app / "_internal/sona/updates/update-public-key.json")
    if info["version"] != expected_version or public != requirement:
        raise UpdateError("更新包内部版本或发布公钥不一致。")
    # Check the executable's PE machine field without executing downloaded code.
    try:
        with (app / "Sona.exe").open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                raise ValueError()
            offset = struct.unpack_from("<I", header, 60)[0]
            if not 64 <= offset <= 1024 * 1024:
                raise ValueError()
            stream.seek(offset)
            if stream.read(6) != b"PE\0\0\x64\x86":
                raise ValueError()
    except (OSError, ValueError):
        raise UpdateError("更新包缺少有效的 Windows x64 主程序。") from None


def signing_requirement(app):
    public = read_public_key()
    validate_app(app, public, VERSION)
    return public


def extract_app(archive: Path, directory: Path):
    directory.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        expanded = sum(entry.file_size for entry in entries)
        if len(entries) > 100000 or expanded > MAX_EXPANDED_BYTES:
            raise UpdateError("更新包解压大小超出限制。")
        if shutil.disk_usage(directory).free < expanded + 512 * 1024**2:
            raise UpdateError("磁盘空间不足，无法解压更新包。")
        names = set()
        # Validate the entire directory before writing any archive content.
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            mode = entry.external_attr >> 16
            if (not parts or parts[0] != "Sona" or "\\" in entry.filename
                    or entry.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or any(part in {".", ".."} or part.endswith((".", " "))
                           or re.search(r'[\x00-\x1f<>:"|?*]', part) or RESERVED_NAME.match(part)
                           for part in parts)
                    or entry.filename.rstrip("/") != "/".join(parts)):
                raise UpdateError("Windows ZIP 必须只包含顶层 Sona，且不能包含链接或不安全路径。")
            name = "/".join(parts).casefold()
            if name in names:
                raise UpdateError("更新包包含重复路径。")
            names.add(name)
        for entry in entries:
            path = directory.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 256 * 1024)
    app = directory / "Sona"
    if not app.is_dir():
        raise UpdateError("更新包中没有 Sona 应用。")
    return app


def tree_size(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def launch(executable, arguments=(), *, log=None):
    return subprocess.Popen([str(executable), *arguments], cwd=str(executable.parent),
                            stdin=subprocess.DEVNULL, stdout=log or subprocess.DEVNULL,
                            stderr=log or subprocess.DEVNULL, close_fds=True,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})


def prepare_runner(directory: Path, current: Path, staged: Path, version: str):
    check_target(current)
    verify_job_archive(directory, version, platform="windows", architecture="x64")
    requirement = signing_requirement(current)
    validate_app(staged, requirement, version)
    if shutil.disk_usage(directory).free < tree_size(current) + 512 * 1024**2:
        raise UpdateError("磁盘空间不足，无法准备独立更新程序。")
    runner = directory / "runner/Sona"
    shutil.copytree(current, runner)
    validate_app(runner, requirement, VERSION)
    (directory / "install.json").write_text(json.dumps({
        "target": str(current), "version": version, "parent_pid": os.getpid(),
    }), encoding="utf-8")
    with (directory / "helper.log").open("wb") as log:
        return launch(runner / "Sona.exe", ["--sona-update-helper", str(directory)], log=log)


def move_directory(source, destination):
    # Anti-virus scans and exiting workers can briefly retain DLL handles.
    for attempt in range(30):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(1)


def apply_update(directory_name: str):
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise UpdateError("更新程序只能在正式 Windows 应用中运行。")
    root = (get_app_paths().data_dir / "updates").resolve()
    original = Path(directory_name)
    directory = original.resolve()
    if (original.is_symlink() or original.is_junction() or directory.parent != root
            or str(uuid.UUID(directory.name)) != directory.name):
        raise UpdateError("无效的更新任务目录。")
    runner = directory / "runner/Sona"
    if Path(sys.executable).resolve() != (runner / "Sona.exe").resolve():
        raise UpdateError("更新程序必须从独立副本启动。")
    request = read_job_json(directory / "install.json")
    target = Path(request["target"])
    check_target(target)
    if version_tuple(request["version"]) <= version_tuple(VERSION):
        raise UpdateError("不能安装相同版本或降级版本。")
    pid = request["parent_pid"]
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        raise UpdateError("无效的更新进程。")
    backup, replaced = None, False
    try:
        write_state(directory, state="waiting")
        for _ in range(120):
            if (directory / "cancel").exists():
                raise UpdateError("更新已取消。")
            if not process_running(pid):
                break
            time.sleep(1)
        else:
            raise UpdateError("等待应用退出超时，未替换原应用。")
        with exclusive_file_lock(root.parent / ".application.lock"):
            check_target(target)
            requirement = signing_requirement(target)
            verify_job_archive(directory, request["version"], platform="windows", architecture="x64")
            staged = extract_app(directory / "update.zip", directory / "helper-unpacked")
            validate_app(staged, requirement, request["version"])
            if shutil.disk_usage(target.parent).free < tree_size(staged) + 512 * 1024**2:
                raise UpdateError("安装目录磁盘空间不足。")
            sibling = Path(tempfile.mkdtemp(prefix=".sona-update-", dir=target.parent))
            replacement, backup = sibling / "app", sibling / "previous"
            shutil.copytree(staged, replacement)
            validate_app(replacement, requirement, request["version"])
            if (directory / "cancel").exists():
                raise UpdateError("更新已取消。")
            write_state(directory, state="installing", backup=str(backup))
            move_directory(target, backup)
            try:
                move_directory(replacement, target)
                replaced = True
                validate_app(target, requirement, request["version"])
            except Exception:
                if target.exists():
                    move_directory(target, sibling / "failed")
                move_directory(backup, target)
                replaced = False
                raise
            write_state(directory, state="installed", version=request["version"], backup=str(backup))
        launch(target / "Sona.exe")
    except Exception as error:
        message = str(error) if isinstance(error, (UpdateError, SignatureError)) else "更新未完成，请重新打开应用后重试。"
        write_state(directory, state="failed", message=message, backup=str(backup) if backup else "", replaced=replaced)
        if (target / "Sona.exe").is_file() and not process_running(pid):
            launch(target / "Sona.exe")

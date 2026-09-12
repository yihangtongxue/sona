"""User-owned, signed .app replacement. Never elevate or execute a downloaded installer."""

import json
import logging
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from ..file_lock import exclusive_file_lock
from ..paths import get_app_paths
from ..version import BUNDLE_ID, VERSION
from .protocol import UpdateError, version_tuple
from .signatures import SignatureError, verify_archive

MAX_EXPANDED_BYTES = 10 * 1024**3
logger = logging.getLogger(__name__)


def installed_bundle():
    if sys.platform != "darwin" or platform.machine().lower() != "arm64" or not getattr(sys, "frozen", False):
        raise UpdateError("自动安装仅在 Apple 芯片 Mac 的正式安装版中可用。")
    if os.environ.get("SONA_DATA_DIR") or os.environ.get("SONA_ENV", "production") != "production":
        raise UpdateError("自定义数据环境不执行自动安装，请使用正式环境。")
    executable = Path(sys.executable).resolve()
    app = executable.parent.parent.parent
    allowed = {Path("/Applications").resolve(), (Path.home() / "Applications").resolve()}
    if app.name != "Sona.app" or executable.parent.name != "MacOS" or app.parent not in allowed:
        raise UpdateError("请先将 Sona.app 安装到“应用程序”目录，再使用自动更新。")
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        raise UpdateError("当前安装目录不可写，请将应用安装到个人“应用程序”目录。")
    return app


def run_tool(arguments, timeout=120, *, action="应用操作"):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        # Exported diagnostics keep the exception type and call locations, never
        # the tool's stderr, command arguments or user paths.
        logger.warning('更新工具调用失败 returncode=%s', getattr(error, 'returncode', None), exc_info=True)
        raise UpdateError(f"{action}失败，请检查安装包完整性、权限及系统兼容性。") from None


def app_info(app):
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            result = plistlib.load(stream)
        executable = result["CFBundleExecutable"]
        if (result.get("CFBundleIdentifier") != BUNDLE_ID or not isinstance(executable, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", executable)):
            raise ValueError()
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError("安装包不是有效的 Sona 应用。") from None


def signing_requirement(app):
    info = app_info(app)
    if info.get("CFBundleShortVersionString") != VERSION:
        raise UpdateError("当前程序版本与打包版本不一致，不能自动更新。")
    # Ad-hoc signing seals Mach-O resources but does NOT identify the publisher.
    # Publisher authentication comes from the pinned Ed25519 key and signed ZIP.
    requirement = f'identifier "{BUNDLE_ID}"'
    # codesign treats -R's argument as a filename unless inline text starts with '='.
    run_tool(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", '=' + requirement, str(app)],
             action="当前应用签名校验")
    return requirement


def validate_app(app, requirement, expected_version):
    run_tool(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", '=' + requirement, str(app)],
             action="更新包应用签名校验")
    info = app_info(app)
    if (info.get("CFBundleShortVersionString") != expected_version
            or info.get("CFBundleVersion") != expected_version):
        raise UpdateError("更新包内部版本与发布清单不一致。")
    minimum = info.get("LSMinimumSystemVersion")
    if minimum:
        try:
            required = tuple(map(int, str(minimum).split(".")))
            current = tuple(map(int, platform.mac_ver()[0].split(".")))
        except ValueError:
            raise UpdateError("安装包的系统版本要求无效。") from None
        if (required + (0, 0))[:3] > (current + (0, 0))[:3]:
            raise UpdateError(f"此版本需要 macOS {minimum} 或更高版本。")
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    if "arm64" not in run_tool(["/usr/bin/lipo", "-archs", str(executable)], action="更新包架构检查").stdout.split():
        raise UpdateError("更新包不支持 Apple 芯片。")
    # Do not run Gatekeeper as our publisher-authentication check: membership-free
    # releases are not notarized. Never disable Gatekeeper or remove quarantine.


def verify_job_archive(directory, version):
    with (directory / "release.json").open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise UpdateError("更新任务元数据过大。")
    record = json.loads(raw)
    if record.get("version") != version:
        raise UpdateError("更新任务版本不一致。")
    verify_archive(directory / "update.zip", version, record["asset"])


def extract_app(archive: Path, directory: Path):
    """Write files before links so an archive can never redirect extraction."""
    directory.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 100000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise UpdateError("更新包解压大小超出限制。")
        if shutil.disk_usage(directory).free < sum(entry.file_size for entry in entries) + 512 * 1024**2:
            raise UpdateError("磁盘空间不足，无法解压更新包。")
        names, links = set(), []
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if parts and parts[0] == "__MACOSX":
                continue
            if (not parts or parts[0] != "Sona.app" or ".." in parts or "\\" in entry.filename
                    or "\0" in entry.filename or entry.flag_bits & 1):
                raise UpdateError("ZIP 必须只包含顶层 Sona.app，且不能包含不安全路径。")
            normalized = "/".join(parts).casefold()
            if normalized in names:
                raise UpdateError("更新包包含重复路径。")
            names.add(normalized)
            path = directory.joinpath(*parts)
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                raise UpdateError("更新包包含不支持的特殊文件。")
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise UpdateError("更新包的链接无效。")
                target = source.read(entry).decode("utf-8")
                if not target or target.startswith("/") or "\0" in target or "\\" in target:
                    raise UpdateError("更新包包含不安全的链接。")
                links.append((path, target))
            elif entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 256 * 1024)
                path.chmod(0o755 if mode & 0o111 else 0o644)
        # All real files are now written. No file can be created through a link.
        link_paths = {path for path, _ in links}
        app_root = directory / "Sona.app"
        for path, target in links:
            if any(parent in link_paths for parent in path.parents):
                raise UpdateError("更新包包含不安全的嵌套链接。")
            if not Path(os.path.normpath(path.parent / target)).is_relative_to(app_root):
                raise UpdateError("更新包的链接越出了应用目录。")
        for path, target in links:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        app = (directory / "Sona.app").resolve()
        for path, _ in links:
            try:
                if not path.resolve().is_relative_to(app):
                    raise UpdateError("更新包的链接越出了应用目录。")
            except (OSError, RuntimeError):
                raise UpdateError("更新包包含循环或无效链接。") from None
        if not app.is_dir():
            raise UpdateError("更新包中没有 Sona.app。")
        return app


def write_state(directory, **state):
    state["helper_pid"] = os.getpid()
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / "result.json")


def prepare_runner(directory: Path, current: Path, staged: Path, version: str):
    verify_job_archive(directory, version)
    requirement = signing_requirement(current)
    validate_app(staged, requirement, version)
    size = sum(p.stat().st_size for p in current.rglob("*") if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(directory).free < size + 512 * 1024**2:
        raise UpdateError("磁盘空间不足，无法准备独立更新程序。")
    # Run the currently trusted program from a private copy, not from the bundle
    # being replaced, and never run code from the new archive before validation.
    runner = directory / "runner" / "Sona.app"
    shutil.copytree(current, runner, symlinks=True)
    run_tool(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", '=' + requirement, str(runner)],
             action="独立更新程序签名校验")
    (directory / "install.json").write_text(json.dumps({
        "target": str(current), "version": version, "parent_pid": os.getpid(),
    }), encoding="utf-8")
    executable = runner / "Contents/MacOS" / app_info(runner)["CFBundleExecutable"]
    with (directory / "helper.log").open("wb") as log:
        process = subprocess.Popen([str(executable), "--sona-update-helper", str(directory)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, cwd=str(directory),
                                   env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    return process


def apply_update(directory_name: str):
    """Private helper mode: wait for the old process and lock out other instances."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        raise UpdateError("更新程序只能在正式 Mac 应用中运行。")
    root = (get_app_paths().data_dir / "updates").resolve()
    directory = Path(directory_name).resolve()
    if directory.parent != root or str(uuid.UUID(directory.name)) != directory.name:
        raise UpdateError("无效的更新任务目录。")
    runner = directory / "runner/Sona.app"
    expected_executable = runner / "Contents/MacOS" / app_info(runner)["CFBundleExecutable"]
    if Path(sys.executable).resolve() != expected_executable.resolve():
        raise UpdateError("更新程序必须从独立副本启动。")
    request = json.loads((directory / "install.json").read_text(encoding="utf-8"))
    target = Path(request["target"])
    allowed = {Path("/Applications").resolve(), (Path.home() / "Applications").resolve()}
    if target.name != "Sona.app" or target.is_symlink() or target.resolve().parent not in allowed:
        raise UpdateError("无效的应用安装目录。")
    if version_tuple(request["version"]) <= version_tuple(VERSION):
        raise UpdateError("不能安装相同版本或降级版本。")
    pid = request["parent_pid"]
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        raise UpdateError("无效的更新进程。")
    backup = None
    replaced = False
    try:
        write_state(directory, state="waiting")
        for _ in range(120):
            if (directory / "cancel").exists():
                raise UpdateError("更新已取消。")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(1)
        else:
            raise UpdateError("等待应用退出超时，未替换原应用。")
        with exclusive_file_lock(root.parent / ".application.lock"):
            requirement = signing_requirement(target)
            # Reauthenticate the original ZIP using the OLD app's embedded key,
            # then extract afresh. An ad-hoc signature on staged files is not trust.
            verify_job_archive(directory, request["version"])
            staged = extract_app(directory / "update.zip", directory / "helper-unpacked")
            validate_app(staged, requirement, request["version"])
            if shutil.disk_usage(target.parent).free < sum(
                p.stat().st_size for p in staged.rglob("*") if p.is_file() and not p.is_symlink()
            ) + 512 * 1024**2:
                raise UpdateError("安装目录磁盘空间不足。")
            # Sibling staging guarantees renames are on the same filesystem.
            sibling = Path(tempfile.mkdtemp(prefix=".sona-update-", dir=target.parent))
            replacement, backup = sibling / "Sona.app", sibling / "previous.app"
            shutil.copytree(staged, replacement, symlinks=True)
            validate_app(replacement, requirement, request["version"])
            write_state(directory, state="installing", backup=str(backup))
            os.replace(target, backup)
            try:
                os.replace(replacement, target)
                replaced = True
                validate_app(target, requirement, request["version"])
            except Exception:
                if target.exists():
                    os.replace(target, sibling / "failed.app")
                os.replace(backup, target)
                replaced = False
                raise
            write_state(directory, state="installed", version=request["version"], backup=str(backup))
        # Launch only after releasing the instance lock. Keep previous.app for recovery.
        run_tool(["/usr/bin/open", "-n", str(target)], timeout=30, action="新版应用启动")
    except Exception as error:
        message = str(error) if isinstance(error, (UpdateError, SignatureError)) else "更新未完成，请重新打开应用后重试。"
        write_state(directory, state="failed", message=message, backup=str(backup) if backup else "", replaced=replaced)
        # This helper never removes a backup or modifies the user's database/models.
        if target.exists():
            subprocess.run(["/usr/bin/open", str(target)], check=False, capture_output=True, timeout=30)

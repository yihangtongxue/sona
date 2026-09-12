"""Non-blocking update UI state; installation requires an explicit user action."""

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..version import RELEASE_REPOSITORY, VERSION
from .backend import APP_DIRECTORY, extract_app, installed_bundle, prepare_runner, signing_requirement, validate_app
from .common import process_running
from .protocol import UpdateError, current_target, download, fetch_manifest, parse_manifest, version_tuple
from .signatures import SignatureError, read_public_key, verify_signature

logger = logging.getLogger(__name__)


class UpdateService:
    def __init__(self, paths, activity, other_busy=lambda: False):
        self._paths = paths
        self._activity = activity
        self._other_busy = other_busy
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread = None
        self._release = None
        self._directory = None
        self._quit = None
        self._last_check = 0.0
        try:
            self._app = installed_bundle()
            read_public_key()
            unavailable = ""
        except (UpdateError, SignatureError) as error:
            self._app = None
            unavailable = str(error)
        self._state = {
            "version": VERSION, "repository": RELEASE_REPOSITORY, "state": "idle", "message": "",
            "latest_version": "", "notes": "", "received": 0, "total": 0,
            "can_install": self._app is not None, "install_hint": unavailable,
        }
        self._read_previous_result()

    def _cleanup_payload_later(self, directory, pid):
        if type(pid) is not int or pid <= 1 or pid == os.getpid():
            return

        def cleanup():
            for _ in range(12):
                if self._stop.wait(5):
                    return
                try:
                    if process_running(pid):
                        continue
                    try:
                        (directory / "update.zip").unlink(missing_ok=True)
                        for name in ("runner", "unpacked", "helper-unpacked"):
                            path = directory / name
                            if path.is_dir() and not path.is_symlink():
                                shutil.rmtree(path)
                    except OSError:
                        logger.warning("更新临时文件清理未完成。")
                    return
                except OSError:
                    return
        threading.Thread(target=cleanup, name="update-cache-cleanup", daemon=True).start()

    def _read_previous_result(self):
        pointer = self._paths.data_dir / "updates/last-job"
        try:
            identifier = str(uuid.UUID(pointer.read_text(encoding="utf-8").strip()))
            directory = pointer.parent / identifier
            if directory.is_symlink() or directory.resolve().parent != pointer.parent.resolve():
                return
            result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                return
            if result.get("state") == "installed" and result.get("version") == VERSION:
                self._state["message"] = f"已更新到 {VERSION}。"
                self._cleanup_payload_later(pointer.parent / identifier, result.get("helper_pid"))
            elif result.get("state") in ("failed", "waiting", "installing"):
                self._state.update(state="error", message="上次更新未完成，请检查更新后重试。")
        except (OSError, ValueError, TypeError):
            pass

    def bind_window(self, quit_callback):
        self._quit = quit_callback

    def status(self):
        with self._mutex:
            return dict(self._state)

    def _set(self, **values):
        with self._mutex:
            self._state.update(values)

    def _start(self, state, work):
        with self._mutex:
            if self._stop.is_set():
                raise UpdateError("应用正在关闭。")
            if self._thread is not None and self._thread.is_alive():
                raise UpdateError("正在处理更新，请稍候。")
            if self._state["state"] == "restarting":
                raise UpdateError("正在重启更新。")
            self._cancel.clear()
            self._state.update(state=state, message="")
            self._thread = threading.Thread(target=self._run, args=(work,), name="software-update", daemon=True)
            self._thread.start()
        return self.status()

    def _run(self, work):
        try:
            work()
        except Exception as error:
            message = str(error) if isinstance(error, (UpdateError, SignatureError)) else "更新操作失败，请检查网络、权限或磁盘空间后重试。"
            self._set(state="error", message=message)
            logger.warning("软件更新未完成 type=%s", type(error).__name__)

    def check(self, *, automatic=False):
        with self._mutex:
            if automatic and ((self._last_check and time.monotonic() - self._last_check < 6 * 3600)
                              or self._state["state"] in {"downloading", "verifying", "ready", "preparing", "restarting"}
                              or (self._thread and self._thread.is_alive())):
                return self.status()
        return self._start("checking", self._check)

    def _check(self):
        self._last_check = time.monotonic()
        self._release = None
        self._set(latest_version="", notes="", received=0, total=0)
        data = fetch_manifest()
        if data is None:
            self._set(state="unpublished", message="尚未发布版本。")
            return
        platform, architecture = current_target()
        version, release = parse_manifest(data, architecture, platform=platform)
        if version_tuple(version) <= version_tuple(VERSION):
            self._set(state="current", message="已是最新版本。")
        elif release is None:
            self._set(state="unavailable", latest_version=version, message="新版本暂无适用于当前系统和架构的更新包。")
        else:
            self._release = release
            self._set(state="available", latest_version=version, notes=release.notes,
                      total=release.size, message=f"发现新版本 {version}。")

    def download(self):
        with self._mutex:
            if self._release is None:
                raise UpdateError("请先检查更新。")
            if self._app is None:
                raise UpdateError(self._state["install_hint"])
        return self._start("downloading", self._download)

    def _download(self):
        requirement = signing_requirement(self._app)
        release = self._release
        verify_signature(release.version, release.signed_asset())
        root = self._paths.data_dir / "updates"
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < release.size + 512 * 1024**2:
            raise UpdateError("磁盘空间不足，无法下载更新。")
        directory = root / str(uuid.uuid4())
        directory.mkdir(mode=0o700)
        self._directory = directory
        try:
            (directory / "release.json").write_text(json.dumps({
                "version": release.version, "asset": release.signed_asset(),
            }), encoding="utf-8")
            download(release, directory / "update.zip", lambda: self._cancel.is_set() or self._stop.is_set(),
                     lambda received, total: self._set(received=received, total=total))
            if self._stop.is_set() or self._cancel.is_set():
                raise UpdateError("下载已取消。")
            self._set(state="verifying", message="正在校验应用签名。")
            app = extract_app(directory / "update.zip", directory / "unpacked")
            validate_app(app, requirement, release.version)
            if self._stop.is_set() or self._cancel.is_set():
                raise UpdateError("更新已取消。")
            self._set(state="ready", message="更新已就绪。")
        except Exception:
            # Only this freshly allocated, not-yet-executed job directory is removed.
            shutil.rmtree(directory)
            self._directory = None
            raise

    def cancel(self):
        if self.status()["state"] not in {"downloading", "verifying"}:
            raise UpdateError("当前没有可取消的下载。")
        self._cancel.set()
        self._set(message="正在取消…")
        return self.status()

    def install(self):
        with self._mutex:
            if self._state["state"] != "ready" or self._directory is None or self._quit is None:
                raise UpdateError("请先下载并校验更新包。")
        self._activity.freeze()
        try:
            if self._other_busy():
                raise UpdateError("有导入或模型任务正在处理，请完成后再更新。")
            return self._start("preparing", self._install)
        except Exception:
            self._activity.thaw()
            raise

    def _install(self):
        directory = self._directory
        try:
            # The gate has paused queue claims and blocked bridge writes. SQLite's
            # backup API includes WAL data; never copy just the live .sqlite3 file.
            source = sqlite3.connect(self._paths.database, timeout=10)
            backup = sqlite3.connect(directory / "before-update.sqlite3", timeout=10)
            try:
                source.backup(backup)
            finally:
                backup.close()
                source.close()
            process = prepare_runner(directory, self._app, directory / "unpacked" / APP_DIRECTORY, self._release.version)
            # Confirm the trusted helper started before asking the main window to close.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._stop.is_set() or process.poll() is not None:
                    raise UpdateError("更新程序未能启动，原应用未修改。")
                if (directory / "result.json").is_file():
                    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
                    if result.get("state") == "waiting":
                        break
                    raise UpdateError("更新程序未能就绪。")
                self._stop.wait(0.2)
            else:
                raise UpdateError("启动更新程序超时，请重试。")
            (directory.parent / "last-job").write_text(directory.name, encoding="utf-8")
            self._set(state="restarting", message="正在重启更新。")
            self._quit()
        except Exception:
            (directory / "cancel").touch()
            self._activity.thaw()
            raise

    def start_automatic_checks(self):
        if self._app is None:
            return

        def loop():
            while not self._stop.wait(30 if not self._last_check else 6 * 3600):
                try:
                    self.check(automatic=True)
                except UpdateError:
                    pass
        threading.Thread(target=loop, name="update-checks", daemon=True).start()

    def close(self):
        self._stop.set()
        self._cancel.set()
        # An explicit restart has a detached helper; other exits must cancel it.
        if self._directory and self.status()["state"] == "preparing":
            (self._directory / "cancel").touch()

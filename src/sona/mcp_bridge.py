"""``Sona mcp``: stdio MCP endpoint for Codex that forwards to the running app.

stdout carries protocol messages only. The access token is read from the app
data directory for every connection attempt and never appears in client config.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .mcp_server import MCP_PATH, read_connection

logger = logging.getLogger(__name__)

LAUNCH_TIMEOUT = 45
REQUEST_TIMEOUT = 180
ACCEPT = "application/json, text/event-stream"


class BridgeError(Exception):
    pass


def app_bundle_path(executable: str | None = None) -> Path | None:
    """Return the .app bundle containing a frozen executable, if any."""
    executable = Path(executable or sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    return None


def launch_app(run=subprocess.run) -> str | None:
    """Start Sona; return an error description or None on success."""
    attempts = [["open", "-a", "Sona"]]
    bundle = app_bundle_path() if getattr(sys, "frozen", False) else None
    if bundle is not None:
        attempts.append(["open", str(bundle)])
    errors = []
    for command in attempts:
        try:
            completed = run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{' '.join(command)}：{error}")
            continue
        if completed.returncode == 0:
            return None
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        errors.append(f"{' '.join(command)} 退出码 {completed.returncode}{'：' + detail if detail else ''}")
    return "；".join(errors) or "无法执行 open 命令"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Bridge:
    def __init__(self, data_dir: Path, *, launcher=launch_app, launch_timeout=LAUNCH_TIMEOUT,
                 opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic):
        self._data_dir = data_dir
        self._launcher = launcher
        self._launch_timeout = launch_timeout
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._launch_lock = threading.Lock()

    def _connection(self) -> dict | None:
        record = read_connection(self._data_dir)
        if record and _port_open(record["port"]):
            return record
        return None

    def ensure_running(self) -> dict:
        record = self._connection()
        if record:
            return record
        with self._launch_lock:
            record = self._connection()
            if record:
                return record
            logger.info("Sona 未运行，尝试自动启动")
            failure = self._launcher()
            if failure:
                raise BridgeError(f"Sona 未运行，自动启动失败（{failure}）。请确认 Sona 已安装在“应用程序”中并手动打开一次。")
            deadline = self._clock() + self._launch_timeout
            while self._clock() < deadline:
                record = self._connection()
                if record:
                    return record
                self._sleep(0.5)
        raise BridgeError(f"已尝试启动 Sona，但 {self._launch_timeout} 秒内本地 MCP 服务未就绪。"
                          "请打开 Sona，在菜单栏或“设置 → AI 工具连接”查看 MCP 状态后重试。")

    def _post(self, record: dict, body: bytes):
        request = urllib.request.Request(
            f"http://127.0.0.1:{record['port']}{record.get('path') or MCP_PATH}", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": ACCEPT,
                     "Authorization": f"Bearer {record['token']}"})
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", "") if error.headers else "", error.read()

    def forward(self, body: bytes) -> list[bytes]:
        """Send one JSON-RPC payload; return zero or more JSON lines for stdout."""
        status = None
        for attempt in range(2):
            record = self.ensure_running()
            try:
                status, content_type, payload = self._post(record, body)
            except (urllib.error.URLError, OSError) as error:
                if attempt == 0:
                    continue  # The app may have just quit; relaunch once.
                raise BridgeError(f"无法连接 Sona 本地 MCP 服务：{error}") from None
            if status == 401 and attempt == 0:
                continue  # Token rotated by an app restart; reread mcp.json.
            break
        if status in (202, 204):
            return []
        if status == 401:
            raise BridgeError("Sona 拒绝了访问令牌，请重新启动 Sona 后重试。")
        if status != 200:
            raise BridgeError(f"Sona MCP 服务返回 HTTP {status}：{payload[:300].decode('utf-8', 'replace')}")
        if "text/event-stream" in content_type:
            lines = []
            for raw in payload.decode("utf-8").splitlines():
                if raw.startswith("data:") and raw[5:].strip():
                    lines.append(json.dumps(json.loads(raw[5:]), ensure_ascii=False).encode())
            return lines
        if not payload.strip():
            return []
        return [json.dumps(json.loads(payload), ensure_ascii=False).encode()]


def _error(identifier, message: str, code: int = -32000) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}},
                      ensure_ascii=False).encode()


def serve(bridge: Bridge, stdin, stdout) -> None:
    lock = threading.Lock()

    def write(lines):
        with lock:
            for line in lines:
                stdout.write(line + b"\n")
            stdout.flush()

    def handle(raw: bytes):
        try:
            message = json.loads(raw)
        except ValueError:
            write([_error(None, "无法解析的 JSON-RPC 消息。", -32700)])
            return
        identifiers = ([item.get("id") for item in message if isinstance(item, dict)]
                       if isinstance(message, list) else [message.get("id")] if isinstance(message, dict) else [None])
        try:
            write(bridge.forward(raw))
        except BridgeError as error:
            logger.warning("MCP 转发失败：%s", error)
            write([_error(identifier, str(error)) for identifier in identifiers if identifier is not None])
        except Exception as error:  # Never crash the stdio session on one request.
            logger.exception("MCP 转发异常")
            write([_error(identifier, f"Sona MCP 桥接异常：{type(error).__name__}")
                   for identifier in identifiers if identifier is not None])

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-bridge") as pool:
        for raw in stdin:
            raw = raw.strip()
            if raw:
                pool.submit(handle, raw)


def main() -> int:
    from .paths import get_app_paths

    # Logging must never touch stdout, which carries the protocol.
    logging.basicConfig(level=os.environ.get("SONA_LOG_LEVEL", "WARNING"), stream=sys.stderr,
                        format="sona-mcp %(levelname)s %(message)s")
    paths = get_app_paths()
    serve(Bridge(paths.data_dir), sys.stdin.buffer, sys.stdout.buffer)
    return 0

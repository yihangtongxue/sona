"""Local MCP server inside the Sona app: 127.0.0.1 only, random bearer token."""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

from .version import VERSION

logger = logging.getLogger(__name__)

CONNECTION_FILE = "mcp.json"
MCP_PATH = "/mcp"
INSTRUCTIONS = """Sona 是用户 Mac 上的本地转录引擎（音频不离开本机）。
推荐流程：1) transcribe 传入链接（小宇宙、Apple Podcasts、B站、YouTube）或本地文件绝对路径，立即得到 task_id；
2) get_status(task_id, wait_seconds=45) 反复调用直到 done=true；3) state=completed 时用 get_transcript 取全文
（长文用 next_offset 分页，需要时间轴时 timestamps=true），再由你完成总结。
YouTube 默认优先使用平台字幕，prefer_subtitles=false 强制本地转录。language 只影响 Whisper（默认自动识别）。
list_history 和 search 用于查找以前的转录。"""

READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True, "open_world_hint": False}
WRITE = {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": False, "open_world_hint": True}


def connection_path(data_dir: Path) -> Path:
    return data_dir / CONNECTION_FILE


def read_connection(data_dir: Path) -> dict | None:
    try:
        with connection_path(data_dir).open(encoding="utf-8") as stream:
            record = json.loads(stream.read(4096))
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("token"), str)
            or not isinstance(record.get("port"), int)):
        return None
    return record


def _write_private_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mcp-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class TokenMiddleware:
    """Reject every HTTP request without the exact bearer token (constant-time compare)."""

    def __init__(self, app, token: str, on_request=None):
        self.app = app
        self._expected = f"Bearer {token}".encode()
        self._on_request = on_request

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        provided = b""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                provided = value
                break
        if not provided or not hmac.compare_digest(provided, self._expected):
            body = json.dumps({"error": "unauthorized", "message": "缺少或错误的 Sona 访问令牌。"},
                              ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        if self._on_request is not None:
            self._on_request()
        return await self.app(scope, receive, send)


def build_server(tools):
    """Create the MCPServer with Sona tools. ``tools`` is a SonaTools-like object."""
    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(name="sona", title="Sona 本地转录", version=VERSION, instructions=INSTRUCTIONS)

    from mcp.server.mcpserver.exceptions import ToolError

    async def call(function, *args):
        try:
            return await anyio.to_thread.run_sync(function, *args)
        except (ValueError, LookupError, OSError) as error:
            # Expected user-facing failures; their Chinese message is shown to the client.
            raise ToolError(str(error)) from error

    @server.tool(title="转录链接或本地文件", annotations=ToolAnnotations(title="转录", **WRITE))
    async def transcribe(source: str, language: str | None = None, prefer_subtitles: bool = True) -> dict:
        """开始转录。source 为小宇宙/Apple Podcasts/B站/YouTube 单集链接，或本机音视频文件的绝对路径。
        language 为 Whisper 语言代码（如 zh、en、ja），省略或 auto 为自动识别。立即返回 task_id，不等待转录完成。"""
        return await call(tools.transcribe, source, language, prefer_subtitles)

    @server.tool(title="查询任务状态", annotations=ToolAnnotations(title="查询状态", **READ_ONLY))
    async def get_status(task_id: str, wait_seconds: float = 0) -> dict:
        """查询任务进度。wait_seconds（0–50）在任务完成前最多等待这么久再返回，适合轮询。"""
        return await call(tools.get_status, task_id, wait_seconds)

    @server.tool(title="获取转录全文", annotations=ToolAnnotations(title="获取转录", **READ_ONLY))
    async def get_transcript(task_id: str, timestamps: bool = False, offset: int = 0, max_chars: int = 60000) -> dict:
        """获取已完成任务的全文。timestamps=true 时每段带 [hh:mm:ss]。文本过长时用返回的 next_offset 继续读取。"""
        return await call(tools.get_transcript, task_id, timestamps, offset, max_chars)

    @server.tool(title="历史记录", annotations=ToolAnnotations(title="历史记录", **READ_ONLY))
    async def list_history(limit: int = 20, offset: int = 0) -> dict:
        """按时间倒序列出 Sona 中的音频、链接和转录状态。"""
        return await call(tools.list_history, limit, offset)

    @server.tool(title="全文搜索", annotations=ToolAnnotations(title="搜索", **READ_ONLY))
    async def search(query: str, limit: int = 10) -> dict:
        """在所有转录、字幕和整理后的文稿中全文搜索，返回匹配片段及 task_id / manuscript_id。"""
        return await call(tools.search, query, limit)

    return server


def build_app(tools, token: str, on_request=None):
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_server(tools)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=["127.0.0.1:*", "localhost:*"],
                                         allowed_origins=[])
    app = server.streamable_http_app(streamable_http_path=MCP_PATH, json_response=True, stateless_http=True,
                                     transport_security=security, host="127.0.0.1")
    return TokenMiddleware(app, token, on_request)


class MCPService:
    """Runs the MCP HTTP server on a background thread for the app's lifetime."""

    def __init__(self, data_dir: Path, tools, *, port: int = 0):
        self._data_dir = data_dir
        self._tools = tools
        self._requested_port = port
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self.error = ""
        self.last_request_at = None
        self.request_count = 0

    def _touch(self):
        self.last_request_at = time.time()
        self.request_count += 1

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            try:
                import uvicorn

                app = build_app(self._tools, self.token, self._touch)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", self._requested_port))
                self.port = sock.getsockname()[1]
                config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on",
                                        log_config=None)
                self._server = uvicorn.Server(config)
                self._thread = threading.Thread(target=self._serve, args=(sock,), name="mcp-server", daemon=True)
                self._thread.start()
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
                logger.exception("MCP 服务启动失败")
                return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._server.started and self._thread.is_alive():
            time.sleep(0.05)
        if not self._server.started:
            self.error = self.error or "MCP 服务未能在 10 秒内启动"
            logger.error("MCP 服务启动超时")
            return
        _write_private_json(connection_path(self._data_dir),
                            {"port": self.port, "token": self.token, "pid": os.getpid(), "version": VERSION,
                             "path": MCP_PATH})
        logger.info("MCP 服务已启动 port=%d", self.port)

    def _serve(self, sock):
        try:
            self._server.run(sockets=[sock])
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            logger.exception("MCP 服务异常退出")

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.started and self._thread and self._thread.is_alive())

    def status(self) -> dict:
        return {"running": self.running, "port": self.port if self.running else None, "error": self.error,
                "last_request_at": self.last_request_at, "request_count": self.request_count}

    def close(self) -> None:
        record = read_connection(self._data_dir)
        if record and record.get("pid") == os.getpid():
            connection_path(self._data_dir).unlink(missing_ok=True)
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

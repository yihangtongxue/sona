"""Generate the Codex ``config.toml`` snippet that starts ``Sona mcp`` over stdio."""

from __future__ import annotations

import json
import os
import sys

STARTUP_TIMEOUT = 60
TOOL_TIMEOUT = 120


def _toml_string(value: str) -> str:
    # JSON string escaping is valid TOML basic-string escaping for these values.
    return json.dumps(value, ensure_ascii=False)


def codex_config(executable: str | None = None, *, frozen: bool | None = None, environ=None) -> dict:
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    environ = os.environ if environ is None else environ
    if frozen:
        args = ["mcp"]
    else:
        args = ["-m", "sona", "mcp"]
    lines = ["[mcp_servers.sona]", f"command = {_toml_string(executable)}",
             f"args = [{', '.join(_toml_string(arg) for arg in args)}]",
             f"startup_timeout_sec = {STARTUP_TIMEOUT}", f"tool_timeout_sec = {TOOL_TIMEOUT}"]
    env = {name: environ[name] for name in ("SONA_ENV", "SONA_DATA_DIR") if environ.get(name)}
    if env:
        lines += ["", "[mcp_servers.sona.env]", *(f"{name} = {_toml_string(value)}" for name, value in env.items())]
    warning = ""
    if "/AppTranslocation/" in executable or executable.startswith("/Volumes/"):
        warning = "Sona 当前从下载位置或磁盘映像运行。请先把 Sona 拖到“应用程序”并从那里打开，再复制配置。"
    elif not frozen:
        warning = "当前为源码运行，配置指向开发环境的 Python。"
    return {"snippet": "\n".join(lines) + "\n", "command": executable, "args": args,
            "config_path": "~/.codex/config.toml", "warning": warning}

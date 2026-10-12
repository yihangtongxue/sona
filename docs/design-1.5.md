# Sona 1.5 技术设计

依据：`PRD-1.5.md`。目标：让 Codex 桌面版等 AI 工具通过 MCP 直接调用 Sona 的本地转录能力。

## 1. 总体结构

```
Codex ──stdio──▶ Sona.app/Contents/MacOS/Sona mcp   (桥接进程，无界面)
                     │  读取 <数据目录>/mcp.json（端口 + 令牌，权限 600）
                     │  HTTP POST + Authorization: Bearer <令牌>
                     ▼
            Sona 主进程内的 MCP 服务（127.0.0.1:随机端口/mcp）
                     │  官方 Python `mcp` SDK（MCPServer，Streamable HTTP，无状态 + JSON 响应）
                     ▼
            现有服务：AudioLibrary / PodcastService / TranscriptionService / 搜索索引
```

- **MCP 服务**（`src/sona/mcp_server.py`）：应用启动后在后台线程以 uvicorn 运行，只绑定 `127.0.0.1`，端口由系统分配。每次启动生成 32 字节随机令牌，与端口、PID 一起原子写入 `mcp.json`（权限 600），退出时删除。外层 ASGI 中间件对每个请求做常量时间令牌比较，缺失或错误一律返回 401；SDK 的 DNS rebinding 保护只允许 `127.0.0.1:*`、`localhost:*` 主机头。
- **工具层**（`src/sona/mcp_tools.py`）：与 MCP 解耦的纯 Python 类 `SonaTools`，便于在 Linux 上单元测试。
- **stdio 桥接**（`src/sona/mcp_bridge.py`）：`Sona mcp` 子命令（`sona/__init__.py` 在初始化界面前分派）。逐行读取 stdin 的 JSON-RPC 消息并转发到本机 HTTP 服务，把 JSON 响应原样写回 stdout；通知（无 id）返回 202 时不输出。连接失败时执行 `open -a Sona`（打包版失败后再尝试打开自身 .app 路径），最多等待 45 秒等待 `mcp.json` 出现且端口可连；仍失败则对该请求返回 JSON-RPC 错误：“Sona 未运行，自动启动失败：…”。令牌只在本机文件中，Codex 配置不含令牌。stdout 只写协议数据，日志写入 stderr/日志文件。

## 2. MCP 工具

| 工具 | 只读 | 说明 |
| --- | --- | --- |
| `transcribe(source, language?, prefer_subtitles?)` | 否（`readOnlyHint=false`，`destructiveHint=false`） | `source` 为链接（小宇宙/Apple Podcasts/B站/YouTube）或本机绝对路径。立即返回 `task_id`。本地文件复制进应用后排队；链接走现有导入队列。重复链接返回已有任务。 |
| `get_status(task_id, wait_seconds=0)` | 是 | 返回状态、进度说明、是否有结果；`wait_seconds`（≤50）在任务结束前长轮询，减少调用次数。 |
| `get_transcript(task_id, timestamps=false, offset=0, max_chars=60000)` | 是 | 返回全文；`timestamps=true` 时每段前加 `[hh:mm:ss]`。超长文本分页，返回 `next_offset`。 |
| `list_history(limit=20, offset=0)` | 是 | 最近的音频/链接记录及状态。 |
| `search(query, limit=10)` | 是 | 全文搜索转录、字幕和文稿，返回片段。 |

服务器 `instructions` 说明推荐流程：transcribe → get_status(wait_seconds) → get_transcript → 由客户端模型总结。

## 3. 全文搜索

- 新表 `search_index`（FTS5，`trigram` 分词，支持中文子串）列：`kind`、`ref_id`（不索引）、`title`、`body`。触发器在 `transcription_results`、`subtitle_results`、`manuscripts`（完成后）增删改时同步；数据库版本 10 → 11，升级时一次性回填。
- 查询 ≥3 个字符走 `MATCH`（按短语转义）；1–2 个字符的查询用 `LIKE` 扫描索引表（trigram 限制）。当前 SQLite 不支持 trigram 时退化为普通表 + LIKE。
- 界面：「我的音频」顶部搜索框，结果可直接打开转录或文稿。

## 4. Whisper 语言

- 设置页新增「识别语言」（默认自动识别），保存在 `transcription.json`。
- 新表 `media_languages(id, language)` 保存单条任务的语言覆盖（MCP `transcribe` 的 `language`）；链接导入的任务 ID 与最终音频 ID 相同，因此同表通用。
- 转录时：单条覆盖 > 全局设置 > 自动；传给 `mlx_whisper.transcribe(language=...)`。Apple Speech 固定中文，忽略该设置。
- 支持语言为常用的 Whisper 语言代码白名单（`sona/languages.py`）。

## 5. 导出

`manuscript_export.py` 新增 SRT（使用现有分段时间戳，无分段时整段输出）和 Markdown（标题 + 来源信息 + 正文；转录可带时间戳列表）。界面：转录页「导出 TXT / SRT / Markdown」，文稿页「导出 TXT / Markdown」。API 增加 `format` 参数，默认 `txt` 保持兼容。

## 6. 菜单栏常驻

`src/sona/menu_bar.py`：Cocoa `NSStatusItem`（SF Symbol `waveform`）。菜单在每次打开前重建（`menuNeedsUpdate:`），显示 MCP 状态（运行中/未启动、最近一次客户端请求时间）、进行中的任务（最多 5 条，含进度说明）、「打开 Sona」「复制 Codex 配置」「退出 Sona」。关闭主窗口后仍常驻；退出走现有终止流程。

## 7. Codex 配置

依据 Codex 官方文档（developers.openai.com/codex/mcp）：ChatGPT/Codex 桌面版、CLI、IDE 扩展共享 `~/.codex/config.toml`。设置页与菜单栏「复制 Codex 配置」生成：

```toml
[mcp_servers.sona]
command = "/Applications/Sona.app/Contents/MacOS/Sona"
args = ["mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

路径取当前运行的可执行文件；源码运行时生成 `python -m sona mcp` 及对应环境变量。不包含令牌。

## 8. 打包

`Sona.spec` 收集 `mcp`（排除需要可选依赖的 `mcp.cli`）、`uvicorn`、`starlette`、`sse_starlette`、`anyio`、`pydantic` 子模块和元数据。`build_macos.py` 构建后运行 `Sona --sona-self-test`，在打包产物中导入 MCP 服务与桥接模块并启动一次本机服务完成自检，失败即终止发布。

## 9. 测试

Linux 上覆盖：令牌校验（无令牌/错误令牌 401，正确令牌可列出工具与调用）、工具逻辑、桥接转发与自动启动失败提示、搜索索引与迁移、语言选择传递、SRT/Markdown 导出、Codex 配置生成。菜单栏、真实 MLX 转录、Codex 实际连接需在 Apple 芯片 Mac 上验收。

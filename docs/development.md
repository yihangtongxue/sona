# 开发说明

本文面向维护者。普通使用者请从 [README](../README.md) 开始。

## 本地运行

需要 Python 3.13+ 和 uv，由开发者手动执行：

```sh
uv sync --locked
uv run sona
```

Mac 使用原生 arm64 Python。当前锁定的 MLX/Metal 依赖和 Mac 打包目标为 macOS 26+，不能只修改系统版本声明来支持旧系统。Apple Speech 开发运行还需要支持 macOS 26 API 的 Command Line Tools；安装包携带编译后的辅助程序。

Windows x64 源码使用 faster-whisper，默认 CPU。NVIDIA 加速由用户在应用内下载和启用，仍需兼容的显卡驱动；不要求用户安装 CUDA SDK 或修改 PATH。Windows 安装版使用 PyInstaller + Inno Setup，按当前用户安装；完整更新包不包含用户模型及数据。

## 代码分工

| 位置 | 职责 |
| --- | --- |
| `src/sona/app.py`、`api.py` | 生命周期、服务组装、桌面桥接 |
| `audio_library.py`、`transcription/` | 音频导入、持久队列、本地识别及恢复 |
| `model_service.py`、`providers/`、`acceleration/` | 模型资源、Apple Speech、Whisper 与 Windows 加速 |
| `ai_model_service.py`、`manuscripts.py` | AI 配置、连接测试、独立文稿及整理队列 |
| `consent.py`、`diagnostics.py`、`logging_config.py` | 首次 AI 确认、本地诊断与导出 |
| `updates/`、`version.py` | 签名验证、更新安装、发行标识 |
| `web/` | HTML、CSS、JavaScript 界面 |
| `tests/` | 临时数据和模拟请求的回归用例 |
| `scripts/`、`packaging/`、`.github/workflows/release.yml` | 双平台打包、签名和 GitHub 自动发布 |

## 数据和恢复

- 源码默认使用 `.data/development/`；Mac 安装版使用 `~/Library/Application Support/Sona/`；Windows 使用 `%LOCALAPPDATA%/Sona/`。开发数据不会自动出现在安装版中。
- `SONA_ENV` 可选 `development / test / production`。设置 `SONA_DATA_DIR` 后，实际目录为 `<SONA_DATA_DIR>/<SONA_ENV>/`。
- SQLite 使用 WAL，数据库版本目前为 6；拒绝打开更高版本数据库。后续表结构变化必须设计显式迁移，不通过清空数据库解决。在线备份使用 SQLite backup API，不能只复制正在运行的主数据库文件。
- 音频分块导入，完整导入和转录入队同事务。队列通过跨进程锁保证单调度者，识别在 spawn 子进程中运行；取消、重试通过执行次数隔离迟到结果。
- 只在结果入库且识别进程结束后清理应用内副本。清理失败可以稍后重试；失败、取消任务保留副本，原文件不修改。转录中断后从整条音频重新开始。
- 文稿保存输入快照及非密钥配置，完成后清空快照与内部模型引用；不对音频记录建立外键。删除原转录不影响独立文稿。
- AI 正文每段最多 1800 字符，并按模型预算进一步缩小，尽量在标点或换行处分开。每段是独立请求，最多带前文 200 字符，不累积对话；全部完成后在本地合并，不将整篇结果再发给 AI。正文失败需重新整理，仅标题失败则重试标题。处理中退出后标记中断，不自动重发中断的请求。
- 首次 AI 提示记录在 `ai-usage-consent.json`，按数据目录隔离，损坏或版本变化时重新确认。取消不会创建任务；桥接的创建和重试也会检查确认。

## 模型与请求边界

- 本地模型清单固定仓库提交、文件大小及校验值；显式点击后才下载。Whisper 下载支持 Range 续传，成功校验后才安装。Apple 语言资源由系统管理。
- Apple 当前为简体中文离线转录；Whisper 引擎按平台选择。音频解码使用 PyAV，不调用外部 FFmpeg。根据实际解码采样数自动分段，每段最多 30 分钟，不依赖容器时长估计，不设录音总时长上限。
- 分段按原顺序解码和识别，不整段加载长录音、不提前缓存所有分段。Whisper 复用单个最多约 110 MiB 的 float32 PCM 缓冲区；Apple 使用最多约 55 MiB 的 int16 PCM 缓冲区和一个循环复用的临时 WAV。这些数值仅指 PCM，不包含模型、特征计算和累计文字结果的内存。
- 同条录音的 Whisper 分段在同一子进程中执行：faster-whisper 缓存模型实例，MLX Whisper 使用依赖自身的同路径模型缓存。Apple 每段调用原生工具，结束后删除该段 WAV。模型锁和取消时终止进程的机制保持不变。
- 各段时间戳按累计采样数换算到整条录音，保留静音占用的时间，仅全部分段成功后保存一份结果。没有重叠音频或文本去重；30 分钟交界处的连续说话识别效果需要实机验收。中途失败、取消或退出均不保存部分结果为完成；重试及 GPU 故障后的 CPU 回退从整条录音重新开始。
- 长转录的时间戳段落每批最多创建 200 行，批次之间让出界面线程；复制全文和 AI 整理始终使用完整正文。切换页面后取消旧结果的后续渲染。
- AI 首个模型不自动测试或设为默认。测试通过后手动选默认，默认模型禁止编辑和删除；模型连接配置变化会重置测试状态。
- API Key 在数据目录的 `credentials/` 中使用 AES-256-GCM 加密，SQLite 只保存引用。每次写入生成随机 nonce，以完整凭据引用为附加认证数据，防止密文在不同引用之间互换。`master.key` 保存随机 256 位主密钥；macOS/Linux 目录权限为 700、文件为 600，Windows 主密钥由当前用户 DPAPI 封装且禁止交互。主密钥不硬编码、不从机器标识推导；它和密文位于同一设备，因此不抵御同一用户权限的恶意代码，也不保证包含两者的 POSIX 备份泄露后仍安全。
- 凭据写入采用临时文件、fsync 和原子替换，并通过进程内及跨进程锁串行化。解密失败或主密钥丢失时拒绝读取；已有密文时绝不自动生成新主密钥覆盖旧密钥。备份需保存完整 `credentials/` 目录，Windows 的 DPAPI 备份仍依赖原用户账户。该目录已加入 Git 忽略规则，也不进入诊断日志导出。
- 新凭据引用以 `local-v1:` 开头。旧 UUID 引用在首次读取时从原系统凭据存储迁移，保留引用和验证状态，兼容排队任务。迁移成功后的读取只访问本地文件；迁移失败不会在同一服务实例内反复请求授权。保存新 Key 不读取旧钥匙串，列表和清理也不访问钥匙串。为避免额外授权与迁移失败丢失数据，旧系统凭据保留，可在系统凭据管理器手动删除。任务绑定模型配置，不静默切换供应商或地址。
- 普通连接测试为 1024 输出 tokens / 30 秒；代码对 `zai/glm-5.3`、`zai/glm-5.3-flash` 使用 4096 / 45 秒，并通过 `extra_body` 指定思考参数。这是当前实现策略，不是所有模型的通用保证。
- 模型预算读取 LiteLLM 本地目录中的输入、输出上限；未识别模型及自定义兼容接口暂按 8192 token 窗口保守规划，不把别名当成实际模型能力。使用 UTF-8 字节数保守估算文本 token，计入系统指令、JSON、前文、消息封装、输出空间和安全余量，不下载分词器；特殊智谱模型另预留推理预算。字符数不是精确 token 数，未知代理或分词规则仍可能拒绝请求。
- 每次发送前检查预算。仅明确的上下文超限或输出截断触发当前段拆小，最多递归缩小 5 次，段长不超过 80 字符时不再拆；截断回复丢弃，当前任务内已成功的前段不重复请求。网络、额度、权限及不明确的错误停止处理，LiteLLM 的通用重试仍关闭。尺寸调整后的新请求可能产生费用。
- 标题素材最多抽取 3600 字符，覆盖正文不同位置；同样检查输入和输出预算，必要时继续缩小摘录。标题处理失败保留已完成正文，不重复整理正文。空回复、截断、内容拦截不能记为成功。
- 智谱通用地址为 `/api/paas/v4`，Coding Plan 为 `/api/coding/paas/v4`。接入与测试不代表套餐支持此应用或此用途；使用前核对 [Coding Plan 使用须知](https://docs.bigmodel.cn/cn/coding-plan/usage-notes)。
- 原 AI 排障记录的参考资料：[GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[核心参数](https://docs.bigmodel.cn/cn/guide/start/concept-param)、[思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)、[对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[Coding Plan 接入](https://docs.bigmodel.cn/cn/coding-plan/quick-start)。参数调整时应重新核对官方说明。

## 日志与问题反馈

`<数据目录>/logs/` 保存 `sona.log` 和两份滚动备份，每份上限约 2 MiB，总量约 6 MiB。主进程和子进程共享写锁，轮转时关闭文件句柄。磁盘或锁异常时放弃该条诊断，不阻断业务；日志不是完整审计记录。

持久日志采用 JSON 行，只记录时间、级别、进程/任务编号、代码位置、白名单状态/数字、异常类型和调用位置。不保存原始日志消息、异常消息、源码行、局部变量、用户路径、正文或凭据，也不收集第三方库日志。维护者可用版本、模块、行号和状态定位问题。

「关于 → 导出日志」通过系统保存对话框导出 ZIP，只打包三份诊断日志及版本、平台、架构、Python 版本。不读取数据库、音频、文稿、钥匙串或其他目录，不自动上传。取消保存不写文件。

开发终端仍保留原来的控制台日志及 traceback；它与隐私受限的导出日志不同，不应未经检查就公开。`SONA_LOG_LEVEL=DEBUG` 只用于维护者排障。

## 验证

由开发者按 [验收清单](acceptance.md) 手动验证。模拟用例可手动运行 `uv run python -m unittest discover -s tests -v`。测试文件存在不代表测试通过；本轮修改未运行项目或测试，也未重建已有安装包。

## 发布与版本

GitHub 发行版本重置为 1.0.0，数据库版本仍为 6，应用标识和凭据存储名称不变。安装包、更新清单及反馈入口均使用当前 GitHub 仓库；详见 [GitHub 发布指南](github-releases.md)。只有推送版本标签触发正式发布，普通提交不发布。Actions 手动运行只产出构建附件，供维护者验收。

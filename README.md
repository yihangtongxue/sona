# Sona

Python / pywebview 本地音频转录应用。导入音频后自动排队转录，在“我的音频”中查看状态和结果。
音频与转录内容保存在本机，不上传到远程识别服务。
AI 模型配置仅保存连接信息；当前已支持模型管理和连接测试，AI 优化转录、自动笔记等上层能力仍在后续接入。

## 运行环境

| 环境 | 引擎 | 执行设备 |
| --- | --- | --- |
| Windows x64 | faster-whisper | 默认 CPU；用户下载并启用加速组件后使用 NVIDIA GPU |
| Apple 芯片 Mac | mlx-whisper | Apple GPU；使用原生 ARM64 Python |
| 支持的 Mac（macOS 26+） | Apple Speech | 系统原生中文离线识别；语言资源由系统管理 |

需要 Python 3.13 或更高版本及 uv。由开发者在项目根目录手动执行：

```sh
uv sync
uv run sona
```

依赖按平台安装。Windows 可在“模型设置 → 转录加速”点击“下载并启用”，应用会准备本地运行库；不要求用户安装 CUDA SDK、复制 DLL 或修改 PATH。系统仍需要兼容的 NVIDIA 显卡驱动。正式安装包尚未制作。

## 转录加速

Windows 启动时只检查本机硬件和已有组件，不联网下载。发现可用 NVIDIA 显卡后显示下载入口和总大小（约 1.4 GiB）；只有用户点击后才下载。下载和检查期间可继续 CPU 转录。暂停、断网或应用中断后，需要用户点击继续 / 重试，不在重启时自动恢复网络传输。

组件固定为 NVIDIA 官方 CUDA 12.8（cuBLAS、CUDA Runtime、NVRTC）与 cuDNN 9.8 的 Windows x64 归档，版本、字节数和 SHA-256 存放在 `acceleration/catalog.py`。来源为 [CUDA 官方清单](https://developer.download.nvidia.com/compute/cuda/redist/redistrib_12.8.1.json) 和 [cuDNN 官方清单](https://developer.download.nvidia.com/compute/cudnn/redist/redistrib_9.8.0.json)。只解包运行所需的 DLL 和许可证，保存在应用数据目录，不改变系统设置。

检查在独立子进程进行：读取驱动与设备能力、校验文件、加载完整运行库，再实际执行 cuBLAS 矩阵乘法与 cuDNN 卷积并核对数值。成功后才允许新任务使用显卡；这不保证任意大模型都能装入显存，实际识别仍有独立的 CPU 回退路径。

“关闭加速”保留组件，只影响之后开始的任务。显卡调用失败时，本条任务在新的 CPU 进程重试一次，后续任务也使用 CPU，直到重新检查通过。用户的启用偏好与组件可用状态分别保存。下载失败显示“重试”，运算检查失败显示“重新检查”；驱动问题会提示更新显卡驱动。

组件操作使用跨进程锁，状态和暂停请求通过 SQLite 共享。每次安装生成独立目录，正在转录的进程不受组件更新影响。成功安装后清理 ZIP 缓存；旧安装目录暂时保留，不提供组件卸载。技术原因只写入控制台日志，页面不展示 CUDA、DLL 或安装路径。Mac 使用 Apple 芯片加速，没有 Windows 组件下载入口。

Whisper 音频通过 PyAV 解码为 16 kHz 单声道数组；Apple Speech 分块解码为临时 PCM WAV，再交给系统的 SpeechAnalyzer。本应用的转录路径不调用外部 FFmpeg 命令。Mac 的 MLX Whisper 需使用原生 ARM64 Python；Apple Speech 的设备和语言支持由系统查询确认。桌面窗口仍需对应平台的 pywebview 图形后端。

数据库只维护当前表结构，新数据库直接按当前定义初始化，不提供历史版本迁移或旧数据补录。开发期间表结构变更后，由开发者自行清理不再匹配的数据库；应用不会自动删除或重建已有数据文件。

## 使用流程

1. 在模型设置准备一个模型并设为默认：Whisper 需下载模型文件；支持的 Mac 也可选择 Apple Speech，语言资源已安装时可直接设为默认，未安装时先点击下载。
2. 选择或拖入音频。文件完整保存后自动创建任务，并进入“我的音频”。
3. 后台一次转录一个文件，其他任务排队；切换页面不影响转录。
4. 完成后点击“查看结果”，可查看带时间戳的文字、复制全文或使用系统播放器打开音频。

没有默认模型时可以先导入，任务显示“等待模型”；选择并准备好模型后自动继续。任务已有模型时不会随默认选择变化；失败或取消后点击“重试”使用当前默认转录模型，未配置时保留原绑定。失败保留音频，不要求重新上传。

“取消”停止识别进程。关闭应用时停止当前识别，下次启动重新处理整条音频，不承诺从中间时间点继续。音频和结果均持久保存；删除会移除应用副本、任务及结果，不修改用户原文件。正在转录的音频必须先取消并等待进程结束后删除。

界面只展示列表和操作，无页面说明卡片或额外添加音频入口。转录中展示真实阶段；faster-whisper 可显示已处理到的时间点，MLX 不伪造百分比。

## 模型资源

Small、Medium、Turbo、Large V3 保持稳定的模型 ID。Windows 下载 CTranslate2 格式，Mac 下载 MLX 格式；界面无需选择引擎。

`model_catalog.json` 固定每个模型的 Hugging Face 仓库、提交版本、文件大小及校验值：

- CTranslate2 Small / Medium / Large V3：`Systran` 仓库。
- CTranslate2 Turbo：`mobiuslabsgmbh/faster-whisper-large-v3-turbo`。
- MLX 模型：`mlx-community` 对应的 Whisper 仓库。

下载使用固定提交链接和 HTTP Range 续传。大文件校验 SHA-256，普通 Git 文件按 Git blob 格式校验 SHA-1；全部资源通过后写入清单并原子移动到安装目录。状态刷新检查清单与文件大小，不重复读取数 GB 权重。模型只在显式点击下载后联网，转录时仅加载本地资源。

下载可以暂停、继续及清理。默认模型不提供删除入口，后端也拒绝删除；先将其他模型设为默认后，才能删除原模型。不同实例下载、删除、转录同一模型使用同一文件锁，切换默认与删除操作另以共享锁避免竞争。锁随进程结束释放，磁盘上的 `.lock` 文件不代表正在占用，不应手动删除。

旧版 `.pt` 文件继续保存在旧目录，不会误报为新引擎的已安装资源，也不会自动转换或删除。下载新格式不会覆盖旧文件；用户明确点击该模型的“删除 / 清理文件”时，会同时清理旧格式副本和未完成下载。

Apple Speech 使用 macOS 26+ 的 SpeechTranscriber / SpeechAnalyzer，当前提供简体中文离线转录。模型设置中的“设为默认 / 默认模型”与 Whisper 共用同一套选择机制；状态查询不申请下载，只有点击下载才向系统申请资源。语言资源由系统共享管理，不展示文件大小，也不提供应用内暂停或删除。

模型列表不显示“刷新状态”按钮。进入设置时自动检查资源；已安装但未选中的模型显示“设为默认”，当前选择只显示“默认模型”标记。系统下载等待或模型使用锁会在设置页自动跟进，检查失败时显示“重试”，下载失败时显示“重试下载”。

原生工具依次从 `SONA_SPEECH_HELPER`、`native/speech_asset_manager`、开发环境的 `xcrun swift` 查找。当前源码支持 `status`、`download`、`transcribe`，输出版本为 2 的逐行 JSON 协议；已配置或随应用附带的旧工具需要同步更新，不能仅替换 Python 文件。开发环境需要支持 macOS 26 API 的 Command Line Tools，正式分发时应携带编译后的工具。

Apple 转录先验证本机资源，再读取完整音频、结束分析并收齐最终片段后保存结果。采用 [Apple 官方文件转录流程](https://developer.apple.com/videos/play/wwdc2025/277/)，只保存最终片段及其时间戳；无语音可正常完成，工具异常退出或仅返回部分片段不会被标记为完成。取消和正常关闭会停止整个识别进程组并清理临时 WAV。任务记录使用 `apple-speech` 引擎及系统版本标识；该标识不是 Apple 模型权重的精确版本。

## 项目结构

```text
src/sona/
  __init__.py        轻量入口、子进程启动保护
  app.py             窗口、服务组装和生命周期
  api.py             前后端桥接接口
  ai_model_service.py AI 模型配置、默认模型与连接测试
  audio_library.py   分块导入、历史记录、文件操作和恢复
  database.py        SQLite 当前表结构、模型安装及选择记录
  models.py          模型定义、协议和平台引擎选择
  model_catalog.json 固定版本的模型文件清单
  model_service.py   后台资源管理任务
  paths.py           环境隔离与数据目录
  file_lock.py       Windows / POSIX 跨进程锁
  acceleration/
    catalog.py       固定版本的 NVIDIA 官方组件清单
    download.py      断点续传、归档校验和安全解包
    probe.py         硬件检测、DLL 定位和实际运算检查
    service.py       启用偏好、可用状态、后台操作和恢复
  providers/
    apple_speech.py   Apple 系统资源适配与原生工具定位
    whisper.py        原 checkpoint 管理及复用的 HTTP 续传传输
    whisper_bundle.py CTranslate2 / MLX 多文件资源管理
  transcription/
    repository.py    持久任务、状态转换、结果事务
    service.py       单实例队列调度、子进程监督和取消
    worker.py        音频解码、两端推理、CUDA 回退
  native/            Apple Swift 辅助程序
  web/               HTML、CSS、JavaScript
tests/               存储、队列和资源下载回归用例
docs/acceptance.md   开发者手动验收步骤
assets/              图标
```

## 本地数据与恢复

使用 SQLite WAL，不需要数据库服务。源码运行默认数据目录为 `.data/development/`；安装后 Windows 使用 `%LOCALAPPDATA%/Sona`，Mac 使用 `~/Library/Application Support/Sona`。

`SONA_ENV` 支持 `development`、`test`、`production`。设置 `SONA_DATA_DIR` 时，最终路径为 `<SONA_DATA_DIR>/<SONA_ENV>/`，保持环境隔离。

```text
<数据目录>/
  sona.sqlite3
  audio/<UUID>.<扩展名>
  models/<引擎>/<模型 ID>/
  downloads/<引擎>/<模型 ID>.<固定提交>.partial/
  acceleration/<组件版本>-<安装 ID>/
  acceleration/downloads/<组件版本>/
```

- `audio_files`：文件名、大小、扩展名、导入时间。
- `transcription_tasks`：排队状态、模型绑定、执行次数、设备、阶段和错误。
- `transcription_results`：全文、时间戳片段、语言、时长、模型版本和完成时间。
- `models` / `model_installations` / `model_selections`：模型清单、安装缓存和默认选择。
- `acceleration_settings`：加速启用偏好、组件路径、检查状态和暂停请求。

导入使用 256 KiB 桥接数据块。音频记录和任务通过数据库触发器在同一事务中生成；没有完成导入的文件不进队列。异常退出后恢复已保存但未登记的文件，清理未完成副本。

队列使用跨进程锁，确保同一数据目录只有一个调度者。识别使用 spawn 子进程，按执行次数防止旧结果覆盖重试。结果与“已完成”状态在同一事务提交，取消和完成之间的竞争也由数据库事务决定。

删除先将文件重命名为待清理文件，再提交数据库删除；事务失败会恢复文件。数据库已经删除但磁盘临时文件清理失败时，下次恢复继续清理，不重建缺失转录结果的历史记录。备份运行中的数据库应使用 SQLite 备份接口，不能只复制主文件。

## 当前边界

### 控制台日志

从终端启动应用后，默认输出 INFO 日志，包含时间、级别、进程名 / PID、任务 ID 和模块名。主进程与识别子进程都输出日志，涵盖接口调用、模型管理、音频解码、CUDA 检测、计算精度、模型加载、转录耗时、结果保存、取消和 CPU 回退。错误保留原始原因及 Python 堆栈；原生进程异常可查看退出码。

列表轮询和成功的音频数据块不会逐条打印；faster-whisper 的片段进度最多每 10 秒记录一次，主进程每 15 秒报告正在运行的任务阶段。MLX 没有实时片段回调，因此只报告当前阶段和运行时长，不伪造进度。日志不打印音频数据或转录正文。

需要更详细的任务事件时，可在启动前设置 `SONA_LOG_LEVEL=DEBUG`。PowerShell 示例：

```powershell
$env:SONA_LOG_LEVEL = "DEBUG"
uv run sona
```

日志输出到终端标准错误流；未连接终端的正式 GUI 启动方式不会额外弹出控制台窗口，当前不写日志文件。

### 功能范围

尚未实现说话人区分、文字编辑、字幕导出、应用内播放器、AI 优化转录、自动笔记及正式安装包。音频完整解码到内存，长录音的内存占用需要实机评估；当前每条任务单独加载模型，结束后释放进程及模型内存。

本次仅编写实现并进行差异静态检查，未启动应用、运行测试或下载真实模型；依赖锁文件需要由开发者同步依赖时更新。验收步骤见 [docs/acceptance.md](docs/acceptance.md)。

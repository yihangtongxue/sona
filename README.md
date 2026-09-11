# Sona

Python / pywebview 桌面应用，前端使用独立的 HTML、CSS 和 JavaScript。

当前可管理 Apple Speech 和 Whisper 模型资源、保存默认模型选择。音频转写、资料库持久化及 AI 模型配置尚未开放。选择的音频只在本次前端会话中保留，不会上传、转写或保存到资料库。

## 本地开发

需要 Python 3.13 或更高版本及 uv。在项目根目录手动执行：

```sh
uv sync
uv run sona
```

Apple Speech 资源管理需要 macOS 26 或更高版本、系统支持的设备及语言；其他平台会显示不支持。Whisper 资源管理不加载推理引擎，下载模型后仍不能执行转写。桌面运行还需要对应平台的 pywebview 图形后端。

## 目录

```text
src/sona/
  __init__.py       轻量命令入口
  app.py            应用组装、窗口及生命周期
  api.py            前后端桥接接口
  models.py         数据类型、提供方协议与内置模型定义
  model_service.py  后台任务、取消信号及默认选择
  database.py       SQLite 迁移与持久化
  paths.py          环境与数据路径
  providers/
    apple_speech.py Apple 原生工具适配
    whisper.py      Whisper 下载、校验、文件锁和删除
  native/           Swift 辅助程序源文件与编译产物位置
  web/              HTML、CSS 和原生 JavaScript
tests/              存储、任务和下载回归测试
docs/acceptance.md  手动验收步骤及待验证项
assets/             开发用应用图标
```

## 本地数据

使用 Python 标准库 `sqlite3`，不需要额外数据库服务。应用启动时自动创建数据库、迁移表结构并登记内置模型；此时不调用系统模型检测或下载。

通过 `SONA_ENV` 选择环境：`development`、`test`、`production`。源码工作目录默认使用 `development`；安装后的应用默认使用 `production`。

| 环境 | 默认数据库位置（macOS） |
| --- | --- |
| development | 项目内 `.data/development/sona.sqlite3` |
| test | 项目内 `.data/test/sona.sqlite3` |
| production | `~/Library/Application Support/Sona/sona.sqlite3` |

`SONA_DATA_DIR` 可指定数据根目录，实际数据库位于 `<SONA_DATA_DIR>/<SONA_ENV>/sona.sqlite3`，以保持环境隔离。安装后的非正式环境使用系统应用数据目录下的对应环境子目录。

数据库表：

- `models`：模型标识、提供方、用途、语言、资源管理方式和预留配置字段。
- `model_installations`：最近查询到的资源状态、文件位置（Apple 模型为空）、查询时间和错误。
- `model_selections`：每类用途当前选择的模型。

表结构使用 `PRAGMA user_version` 管理版本。每次数据库操作独立创建、提交或回滚并关闭连接，后台线程不共享连接。数据库采用 WAL 模式，备份运行中的数据库应使用 SQLite 备份接口，不应只复制主文件。

## Whisper 模型资源管理

内置的 Whisper Small、Medium、Turbo 和 Large V3 使用 OpenAI 公开的原始 checkpoint 作为模型制品；应用只负责下载和管理这些文件，不加载或执行模型。模型文件按环境隔离，保存于 `<数据目录>/models/<模型 ID>/`；下载中的文件保存于 `<数据目录>/downloads/<模型 ID>.partial/`。

每个安装目录都有 `manifest.json`，记录制品版本、来源、文件名、大小及 SHA-256。下载验证 HTTP Range 的续传范围，完成后校验 SHA-256，再将临时目录原子移动为正式安装目录。删除模型会同时移除正式文件和未完成下载；确认删除成功后，安装记录与默认选择在同一数据库事务中更新。删除失败会显示错误并保留选择，不会宣告成功。

下载中的字节进度、总字节数和制品版本约每 3 秒写入数据库，阶段变化及最终结果立即保存。界面约每 600 毫秒读取活跃任务，桥接请求串行执行；下载本身不会在应用启动时自动开始。文件大小以下载源响应为准，尚未获取且没有已下载数据时不显示大小说明；下载中总大小未知时仍显示已接收的数据量。

同一模型在同一时间只允许一个 Sona 实例下载或删除；另一个实例会显示“其他实例处理中”。互斥使用 Windows 字节范围锁或 POSIX 文件锁，锁随句柄关闭或进程退出自动释放。`.lock` 文件会保留，它的存在不代表正在下载，不应手动删除或依赖其内容判断占用。

Whisper 下载可暂停、继续，也可清理未完成下载文件。暂停需要等待当前网络读取或文件操作结束；网络连接/读取超时配置为 10 秒，哈希校验每个数据块检查取消信号。关闭应用时，各线程共享最多 11 秒的 join 等待预算，而不是逐个累加等待时间；该预算不保证操作系统 DNS、文件系统或驱动调用的耗时。应用关闭或网络中断后，partial 文件保留，再次下载可以续传。

## Apple Speech 模型流程

1. 每次启动后，首次进入“模型设置”时查询资源状态，不申请下载、不创建安装请求。之后切换回来复用本次会话状态，不重复启动原生查询工具；正在进行的任务继续更新进度。手动刷新和重新连接仍可重新查询。
2. 未安装时显示“下载”；点击后检查当前系统及语言支持并请求系统下载安装。
3. 下载阶段通过原生工具传回系统进度；完成、失败或等待系统后隐藏进度条。
4. 已安装时显示“设为默认”；点击后重新确认资源可用，再将用户选择写入数据库。
5. 已确认可用的当前选择显示“默认模型”，仅表示用于后续转写的偏好，没有启动语音识别。
6. 重启后恢复选择，首次进入模型设置时重新向系统确认资源。安装记录仅为缓存，查询失败显示“状态未知”，不会误报为未安装，可手动重新检查。
7. 系统仍在下载或等待网络恢复时显示“等待系统”，可点击“刷新状态”重新查询。

Apple 资源由 macOS 管理和跨应用共享，不保存在上述应用数据目录内。因此应用配置按环境隔离，但 Apple 系统模型不会按环境复制。

当前实现使用 macOS 26+ 的 `SpeechTranscriber` 和 `AssetInventory`，并由系统检查设备、语言是否支持。原生工具的调用顺序：

1. `SONA_SPEECH_HELPER` 指定的可执行文件。
2. `src/sona/native/speech_asset_manager` 同目录编译产物。
3. 开发环境使用 `xcrun swift` 运行 `speech_asset_manager.swift`，需要支持 macOS 26 SDK 的 Xcode / Command Line Tools。

正式打包时应携带编译好的辅助程序，不能要求终端用户安装开发工具。查询最多等待 120 秒，下载请求最多等待 1 小时；超时停止辅助进程，已提交的系统下载可能继续，可重新查询状态。Apple 资源不提供应用内暂停、删除入口。

## 后续模型扩展

在 `models.py` 登记稳定模型标识及其制品元数据，并在 `providers/` 实现 `ModelProvider` 协议。支持的资源操作由 `run()` 执行，`cancel()` 发出取消信号，`close()` 释放任务资源；系统提供方可以明确拒绝不支持的操作。在 `app.py` 注册提供方，并将同一组模型定义传入仓库与 `ModelService`，服务不自行绑定内置清单。

`ModelService` 统一负责后台任务和选择持久化，前端按模型列表渲染，不包含 Apple 模型 ID 的特判。模型文件保持独立存储，数据库只记录安装元数据。

## 验收与测试

运行命令、测试覆盖范围和人工检查步骤见 [验收文档](docs/acceptance.md)。测试使用模拟提供方、本地小文件和模拟 HTTP 响应，不下载真实模型或调用 Apple 下载接口。

接口参考：[Apple AssetInventory](https://developer.apple.com/documentation/speech/assetinventory)、[Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html)。

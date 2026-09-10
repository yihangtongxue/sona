# Sona

Python / pywebview 桌面应用，前端使用独立的 HTML、CSS 和 JavaScript。

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

## Apple Speech 模型流程

1. 每次启动后，首次进入“模型设置”时查询资源状态，不申请下载、不创建安装请求。之后切换回来复用本次会话状态，不重复启动原生查询工具；正在进行的任务继续更新进度。手动刷新和重新连接仍可重新查询。
2. 未安装时显示“下载”；点击后检查当前系统及语言支持并请求系统下载安装。
3. 下载阶段通过原生工具传回系统进度；完成、失败或等待系统后隐藏进度条。
4. 已安装时显示“使用”；点击后重新确认资源可用，再将用户选择写入数据库。
5. 当前选择显示“使用中”。这里只表示用于后续转写的默认模型，没有启动语音识别。
6. 重启后恢复选择，首次进入模型设置时重新向系统确认资源。安装记录仅为缓存，查询失败显示“状态未知”，不会误报为未安装，可手动重新检查。
7. 系统仍在下载或等待网络恢复时显示“等待系统”，可点击“刷新状态”重新查询。

Apple 资源由 macOS 管理和跨应用共享，不保存在上述应用数据目录内。因此应用配置按环境隔离，但 Apple 系统模型不会按环境复制。

当前实现使用 macOS 26+ 的 `SpeechTranscriber` 和 `AssetInventory`，并由系统检查设备、语言是否支持。原生工具的调用顺序：

1. `SONA_SPEECH_HELPER` 指定的可执行文件。
2. `src/sona/native/speech_asset_manager` 同目录编译产物。
3. 开发环境使用 `xcrun swift` 运行 `speech_asset_manager.swift`，需要支持 macOS 26 SDK 的 Xcode / Command Line Tools。

正式打包时应携带编译好的辅助程序，不能要求终端用户安装开发工具。本仓库此次没有执行打包。查询最多等待 120 秒，下载请求最多等待 1 小时；超时停止辅助进程，已提交的系统下载可能继续，可重新查询状态。

## 后续模型扩展

在 `models.py` 登记稳定模型标识，并为提供方实现 `ModelProvider.run()` 的 `status` / `download` 操作，在启动入口注册。`ModelService` 统一负责后台任务和选择持久化，前端按模型列表渲染，不包含 Apple 模型 ID 的特判。模型文件保持独立存储，数据库只记录元数据。

音频转文字执行、AI 模型配置和其他模型提供方尚未接入。

## 验收

本次未运行应用、原生工具、下载或测试。可按以下顺序人工验收：

1. 启动后停留在上传页面，不应启动系统模型检测。
2. 打开模型设置，已安装的 Apple 资源应直接显示“已安装 / 使用”，不用再点击下载。
3. 点击“使用”，成功后显示“使用中”；重启并进入设置后应恢复。
4. 切换到 `SONA_ENV=test`，用户选择应独立，系统安装状态应仍能识别。
5. 未安装的设备点击“下载”，检查阶段、进度、失败详情及完成后进度条收起。
6. 检查工具启动失败、系统不支持或查询失败时，不应显示“使用中”或擅自开始下载。
7. 本次会话查询结束后，来回切换上传页和模型页，不应再次显示“启动中 / 检查中”；点击“刷新状态”应正常重新查询。下载过程中切换页面，进度应继续更新。

`tests/test_models.py` 提供环境隔离、数据库迁移及选择恢复、只读状态查询和后台任务的测试，使用模拟提供方，不会调用 Apple 下载接口。测试尚未执行。

接口参考：[Apple AssetInventory](https://developer.apple.com/documentation/speech/assetinventory)、[Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html)。

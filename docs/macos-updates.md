# Mac 免费分发、打包与更新

本文面向发布维护者，普通用户无需执行以下命令。源码改动不自动进入已有安装包；重新构建后必须重新生成签名。正式发布前必须确认产物对应的代码及 [验收清单](acceptance.md)，不能将打包成功视为真实升级已通过。

## 发行配置

- Sona 1.0.0；作者：一航同学YIHANG；主页 https://maxcosmos.top；邮箱 leo.morrison.2001@gmail.com。
- Bundle ID：`com.yihang.sona`，后续保持不变；主应用、Speech 辅助程序、签名载荷和安装检查统一从发行配置派生。
- 发布仓库：`https://cnb.cool/yihangtongxue/sona-release`，固定 `main`。
- 清单：`https://cnb.cool/yihangtongxue/sona-release/-/git/raw/main/.release-hub/updates/stable.json`，直接读取 JSON，`schemaVersion: 1 / stable`。CNB 管理 API 需要认证，客户端不用它，也不携带发布 Token。
- 当前构建：Apple 芯片、macOS 26+。锁定的 mlx-metal 0.32.2 wheel 标记为 macOS 26；不能仅降低 Info.plist 就承诺旧系统支持。
- 首次安装 DMG，更新使用完整 ZIP（顶层只有 `Sona.app`）。模型权重不进包。
- 不购买 Apple Developer 会员、不公证；采用 ad-hoc 应用签名和独立 Ed25519 更新签名。

“正式版”不等于经过 Apple 公证。首次下载后 macOS 可能拦截；只有确认发布来源可信、系统允许时，用户才可以按 Apple 官方指引在“隐私与安全性”中对该应用选择“仍要打开”。受管理设备可能不允许。本项目不关闭 Gatekeeper、不运行解除隔离命令、不承诺免费发行没有安全提示。

## 两类签名

- **ad-hoc 应用签名**：用于 Mach-O 和 bundle 资源完整性；没有 Apple 开发者身份背书，不能单独证明更新包由作者发布。
- **Ed25519 更新签名**：私钥只在发布者本机，应用内置公钥。验证身份、版本、渠道、平台、架构、包类型、文件名、大小和 SHA-256。服务端替换公钥不能绕过客户端验证。

不从更新清单导入可信公钥。内置公钥缺失时拒绝自动安装；无签名或签名错误的 ZIP 也拒绝。首次安装包的来源仍需用户信任；这套签名不能替代 Apple 首次分发认证或恶意软件检查。

## 打包前确认

首次执行前确认：1.0.0、Apple 芯片/macOS 26+、接受未公证的安装提示、专用私钥保管目录。当前已有发布密钥，统一保存在 `/Users/leo/os/signing/sona/`；继续使用已有密钥，不要重新生成。缺少公钥将停止构建。

以下命令供确认后手动执行；已有密钥时跳过生成步骤，已完成的构建或签名不要重复覆盖。

### 1. 生成一次密钥

```sh
uv run python scripts/release_keys.py generate --directory /Users/leo/os/signing/sona
```

目录必须尚不存在且在 Git 仓库之外。工具交互读取至少 12 字符的密码，生成加密 PKCS8 `update-private.pem`（600）和公开的 `update-public-key.json`。不要在聊天、命令参数、环境变量、源码或 ReleaseHub 中提供密码/私钥。离线备份私钥和密码；丢失后不能继续给已有客户端发布受信任更新，需要手动重新安装。当前不实现在线密钥轮换。

### 2. 手动构建

需要支持 macOS 26 Speech API 的 Xcode Command Line Tools、原生 arm64 Python 3.13 和锁定依赖：

```sh
uv run --locked --with pyinstaller==6.22.2 python scripts/build_macos.py --public-key /Users/leo/os/signing/sona/update-public-key.json --confirm-version 1.0.0
```

工具检查版本和公钥，预编译 Speech helper，按 `packaging/macos/Sona.spec` 收集 Python、MLX/Metal、LiteLLM、PyAV、keyring、网页与图标，生成 ad-hoc `.app`、ZIP、DMG 和构建信息。不读取私钥、不上传、不覆盖已有输出；诊断文件保留在 `build/macos-*`。失败输出需人工核对后另存，再重试。

新版本先同步修改 `src/sona/version.py`、`pyproject.toml` 并更新 `uv.lock`，再替换命令中的版本号与产物路径。已发布版本不可覆盖；尚未发布的同版本重建也应先归档旧输出，不能复用旧签名。

产物位于 `dist/Sona-1.0.0-macos-arm64/`。保留第三方许可文件，发布前仍需核对许可证；不要把 `.data`、用户 API Key、本机模型或整个虚拟环境塞进包。

### 3. 验收后签名

```sh
uv run python scripts/release_keys.py sign --private-key /Users/leo/os/signing/sona/update-private.pem --public-key /Users/leo/os/signing/sona/update-public-key.json --confirm-version 1.0.0 dist/Sona-1.0.0-macos-arm64/Sona-1.0.0-macos-arm64.zip dist/Sona-1.0.0-macos-arm64/Sona-1.0.0-macos-arm64.dmg
```

工具验证密钥匹配、ZIP 内身份/版本/公钥，为各文件生成同名 `.sig.json`。DMG 由相同构建产生，仍需要人工验收；签名不是测试或安全审计。签名后不要修改或重新压缩安装包。

### 4. ReleaseHub 发布

在 ReleaseHub 的 CNB 产品中设置目标公开仓库及 `main` 分支、「更新包签名：必须签名」「应用标识：com.yihang.sona」。选择 ZIP（macos / arm64 / zip）和 DMG（macos / arm64 / dmg）。两份 `.sig.json` 留在各安装包旁，不作为安装包选择。

ReleaseHub 按产品配置校验签名，不应依赖 Sona 仓库名的特殊分支。在任何远端写入前读取 sidecar、验证签名及元数据，全部附件上传成功后才写 stable.json。签名经 `assets[].updateSignature` 透传。必须签名的产品缺 sidecar 会中止发布。ReleaseHub 不保管私钥，sidecar 公钥只用于一致性校验；真正信任锚是 Sona 内置公钥。发布时务必使用原密钥，避免服务端验证通过但旧客户端不信任新密钥。

发布 Token 只由 ReleaseHub 管理，不进入 Sona。失败需先核对远端 Release/tag，不能直接重复上传相同版本。“校验客户端下载”检查匿名下载和哈希，不等于真实升级验收。

CNB 附件使用 `https://cnb.cool/<组织>/<仓库>/-/releases/download/<tag>/<远端附件名>` 的公开稳定入口，允许经过安全校验后重定向到对象存储；不要保存临时签名地址或使用有次数限制的分享链接。远端附件名可以与清单 `fileName` 不同，签名仍验证原文件名与内容哈希。

本次更换更新源及 Bundle ID 后重建 1.0.0：旧包需要手动替换，不能通过相同版本号或跨标识自动升级。旧包和旧 `.sig.json` 不用于新 CNB 发布；确认新包构建成功后，可以清理旧构建产物，但不要删除签名密钥或用户数据。沿用现有 Ed25519 密钥，但必须对新包重新签名。数据仍保存在原 Sona 数据目录，钥匙串服务名称不变；系统可能重新请求访问许可，不自动删除或迁移用户数据。

## 签名协议

sidecar 字段：`algorithm: ed25519`、`keyId`（32 字节原始公钥 SHA-256 小写十六进制）、`publicKey`（公钥 Base64）、`payload`（UTF-8 JSON 原始字节的 Base64）、`signature`（64 字节签名 Base64）。

payload 字段：`schemaVersion: 1`、`appId`、`channel: stable`、`version`、`platform`、`architecture`、`packageType`、`fileName`、`size`、`sha256`。

客户端验证原始 payload 字节，再逐字段比对清单，不依赖跨语言序列化。ReleaseHub 不把 publicKey 加入清单。下载 URL 与说明未签名：链接可换 CDN，但内容必须匹配签名哈希；说明只作不可信纯文本。版本号严格递增；当前不支持过期签名检测或防服务端隐藏新版。

## 更新与恢复

安装到当前用户可写的 `/Applications/Sona.app` 或 `~/Applications/Sona.app`。正式版约启动 30 秒后检查，之后约每 6 小时检查；只提示，不自动下载或强制安装。

检查 → 下载 → 验证签名/大小/哈希与应用结构 → 用户确认重启。开发运行不替换程序；未内置公钥时不能验证已有签名清单。

导入、转录、文稿整理或模型任务运行时拒绝重启，准备期间暂停新任务。SQLite backup API 保存包含 WAL 的备份。独立更新程序来自当前应用副本，等待主进程退出并取得实例锁，再用旧版内置公钥验证原 ZIP，重新解压，不信任先前解压缓存；随后同文件系统暂存和替换，启动新版。

普通替换异常尝试恢复旧应用，保留 `.sona-update-*/previous.app` 及 `updates/<UUID>/before-update.sqlite3`。不删除用户文稿、模型或钥匙串密钥；更新成功后清理该次 ZIP 与临时副本，不自动删除备份。

## 已知边界与待验收

- 不支持断点续传、差分更新、提权、强制更新、密钥轮换或降级。
- 不保证没有 macOS 安全提示；须测试从互联网下载的真实包，而非只试本机 dist 目录。
- 断电/强杀发生在两次重命名之间可能需手动恢复；新版业务崩溃无自动健康回滚。
- 关闭后不恢复已下载就绪状态；强杀可能遗留临时文件，勿删除运行中的更新目录。
- 数据库版本 6 接续历史标记 5，与软件版本独立。兼容结构不清空，后续结构变化必须迁移。手动恢复前备份最新数据。
- 自动用例和实机升级的验收入口统一见 [验收清单](acceptance.md)；目前没有完整的实机升级通过记录。

参考：[PyInstaller](https://pyinstaller.org/en/stable/spec-files.html)、[Ed25519](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)、[Apple 安全打开 App](https://support.apple.com/zh-cn/102445)。

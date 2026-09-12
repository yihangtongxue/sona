# GitHub 双平台发布与更新

## 发行约定

- 版本从 **1.0.0** 开始。唯一发布仓库：[yihangtongxue/sona](https://github.com/yihangtongxue/sona)。仓库必须公开，安装版不携带 GitHub Token。
- 唯一更新入口：`https://github.com/yihangtongxue/sona/releases/latest/download/stable.json`。不使用旧发布服务、备用清单或旧仓库回退。
- 应用标识仍为 `com.yihang.sona`；公钥固定在 `src/sona/updates/update-public-key.json`，沿用原密钥。服务端清单不能替换可信公钥。
- Windows：x64、Windows 10 1809+ / Windows 11，PyInstaller + Inno Setup，安装在 `%LOCALAPPDATA%\Programs\Sona\app`，卸载程序在上一级。
- Mac：Apple 芯片、macOS 26+，PyInstaller + DMG。当前 MLX/Metal 依赖和 Speech helper 需要 macOS 26；不提供 Intel Mac 包。
- 软件版本重置不修改数据库版本（仍为 6）、用户数据目录、模型目录或凭据服务名称。

## 首次配置

进入 GitHub 仓库 **Settings → Environments**，创建名为 **release** 的环境，只允许发布标签 `v*` 使用该环境，并添加：

| Secret 名称 | 内容 |
| --- | --- |
| `SONA_UPDATE_PRIVATE_KEY_PEM` | 现有加密 PKCS8 `update-private.pem` 文件的完整文本，包含 BEGIN/END 行和真实换行 |
| `SONA_UPDATE_KEY_PASSWORD` | 该私钥原有的解密密码 |

现有私钥保存在维护者本机 `/Users/leo/os/signing/sona/update-private.pem`。不重新生成密钥；仓库中的公钥已来自对应的 `update-public-key.json`。私钥和密码只录入 GitHub Secrets，不提交源码，不发到聊天，不写入普通 Actions Variables。CI 在独立发布任务的签名步骤中读取并解密，构建任务拿不到私钥。

工作流会自动使用 `GITHUB_TOKEN` 上传 Release，无需额外配置 PAT。如果组织策略限制 Actions 写入，需允许发布任务的 `contents: write`。若希望完全自动发布，release 环境不设置人工审核人；仍应限制有权创建 `v*` 标签及修改工作流的人员。

首次配置完成且代码、版本标签推送到 GitHub 后，自动发布才会实际运行。环境中保存了 Secret 不代表私钥和密码已验证匹配，首次发布的签名步骤会完成验证。

## 日常发布

1. 同步修改 `src/sona/version.py`、`pyproject.toml`、`uv.lock` 中 **sona 项目自身**的版本；不要批量替换其他依赖的版本。首次为 `1.0.0`，后续如 `1.0.1`。
2. 在 `docs/releases/<版本>.md` 编写更新说明。缺少文件时使用默认说明。
3. 按 [验收清单](acceptance.md) 由维护者完成验证，提交并推送该版本代码。
4. 创建并推送对应标签，例如维护者手动执行：

   ```sh
   git tag v1.0.0
   git push origin v1.0.0
   ```

推送标签后 `.github/workflows/release.yml` 自动执行：检查标签及三处版本一致 → 分别在 `macos-26` 和 `windows-2025` 构建 → 收集双平台产物 → 签名 → 生成清单 → 创建 Release 草稿 → 上传并核对所有附件 → 公开为最新版本。

普通分支提交不触发发布。在 **Actions → Build and release Sona → Run workflow** 手动运行时，只构建并保存附件，供维护者下载验收；不会读取签名私钥或创建 Release。工作流不运行项目测试。手动构建的附件尚未签名，不能直接作为自动更新源发布。

不要提前手动创建同标签的 Release。已发布的相同版本及更高正式版本会阻止发布，避免覆盖和 latest 倒退。首次使用前如果 GitHub 已有旧的同名标签或更高正式 Release，需要维护者先确认历史处理方案；脚本不会自动删除历史。

## 发布产物

以 1.0.0 为例：

| 文件 | 用途 |
| --- | --- |
| `Sona-1.0.0-macos-arm64.dmg` | Mac 首次安装 |
| `Sona-1.0.0-macos-arm64.zip` | Mac 完整更新包，顶层 `Sona.app` |
| `Sona-1.0.0-windows-x64-setup.exe` | Windows 首次安装，当前用户权限 |
| `Sona-1.0.0-windows-x64.zip` | Windows 完整更新包，顶层 `Sona`；首次安装选 setup.exe |
| 各安装包的 `.sig.json` | Ed25519 发布签名 |
| `stable.json` | 双平台更新清单，`schemaVersion: 1` / `stable` |
| `SHA256SUMS.txt` | 四个安装包的 SHA-256 |

每个附件必须小于 2 GiB，这是 GitHub Releases 的单文件限制。模型权重、用户数据、API Key、虚拟环境和私钥不进入安装包。Actions 中间附件保留 14 天，用户正式下载使用 Release 附件。

Windows 安装程序会检测 WebView2，缺少时执行随包附带的微软 Bootstrapper，从微软下载运行时。构建脚本验证 Bootstrapper 的 Microsoft Authenticode 签名。无网络且缺少 WebView2 时安装会提示补装后重试；已安装运行时的设备不需要这一步下载。用户不需要 Python、uv 或 CUDA SDK。

## 签名和更新行为

Mac 保留 ad-hoc 签名，没有 Apple Developer ID 或公证。Windows 当前没有 Authenticode 发布者证书。首次下载和安装可能有系统安全提示；Ed25519 更新签名不能替代操作系统的发布者认证。

签名覆盖应用标识、版本、渠道、平台、架构、包类型、文件名、大小和 SHA-256。客户端下载 ZIP 前检查签名，下载后检查大小和哈希；独立更新程序来自当前应用副本，退出主程序后用旧版内置公钥重新验证原 ZIP，再重新解压并安装，不信任之前的解压缓存。

应用启动约 30 秒后检查新版，之后约每 6 小时检查。发现新版本只提示，由用户确认下载和重启。音频导入、转录、AI 整理或模型任务运行时不能重启更新。准备更新时使用 SQLite backup API 保存含 WAL 数据的备份。

Mac 只更新 `/Applications/Sona.app` 或 `~/Applications/Sona.app` 的可写安装。Windows 只更新安装程序指定的当前用户目录，不提权、不覆盖正在运行的程序；安装目录中的 `app` 文件夹整体替换，卸载程序和用户数据不参与替换。Windows 等待主进程退出，并对短暂文件占用重试。

## 旧版迁移

旧分发客户端的更新地址已经写入旧安装包，而且版本可能高于新的 1.0.0，因此不会通过这次版本重置自动更新。维护者不再发布旧源过渡版本；旧用户需要从 GitHub 下载并手动覆盖安装 1.0.0 一次。此后只接收 GitHub 上版本号严格递增的更新，不支持相同版本覆盖或自动降级。

用户数据保持原位置：Mac 的 `~/Library/Application Support/Sona/`、Windows 的 `%LOCALAPPDATA%\Sona\`；模型、文稿、数据库及系统凭据保留。手动覆盖安装前建议备份数据，尤其不要误删数据目录。

## 失败与恢复

- 任一平台构建失败，发布任务不执行。签名/清单检查失败，不创建 Release。附件上传或核对失败，Release 保持草稿，旧的 latest 不变。
- 失败重跑前先检查对应标签的 Release。若已有失败草稿，维护者确认后删除该草稿（保留标签），再重跑整次工作流。脚本不覆盖已有草稿、附件或已发布版本。
- 本机更新前数据库备份在 `<数据目录>/updates/<UUID>/before-update.sqlite3`。Mac 旧应用在安装目录旁 `.sona-update-*/previous.app`，Windows 旧程序在 `%LOCALAPPDATA%\Programs\Sona\.sona-update-*\previous`。替换异常尝试恢复旧程序，不自动恢复数据库。
- 成功启动新版后清理该次 ZIP、解压缓存和独立更新程序，保留旧程序及数据库备份。Windows 卸载保留用户数据；更新备份可能需要维护者在确认无误后自行清理。
- 不支持差分更新、断点续传、密钥轮换、强制更新、提权、自动降级或新版业务健康回滚。断电/强杀发生在替换间隙时，可能需要手动恢复应用。

代码中的校验不等于实机验收。本次仅编写和静态审阅代码，没有启动项目、运行测试、打包、签名产物或执行真实升级。

参考：[GitHub 构建环境](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[Release 下载入口](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)、[附件大小限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)、[WebView2 分发](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。

# Mac 发布与更新

发布流程已统一迁移到 [GitHub 双平台发布指南](github-releases.md)。

Mac 继续提供 Apple 芯片、macOS 26+ 的 DMG 和完整更新 ZIP，采用 ad-hoc 应用签名及现有 Ed25519 更新密钥。已有的 codesign 内联 requirement 修复保留：`-R` 参数以 `=` 开头。

GitHub 发行从 1.0.0 开始，旧分发版本需要手动覆盖安装一次。应用标识 `com.yihang.sona`、数据目录和钥匙串名称不变。后续安装包、更新清单及发布产物只使用 GitHub。

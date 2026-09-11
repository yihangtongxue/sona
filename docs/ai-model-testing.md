# AI 模型连接测试

## 智谱文档核对（2026-09-11）

- [GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)：模型标识为 `glm-5.3-flash`；文本参数与 GLM-5.3 一致，思考模式不能关闭。
- [核心参数](https://docs.bigmodel.cn/cn/guide/start/concept-param)：GLM-5.3 / GLM-5.3-Flash 支持 `reasoning_effort` 的 `low / high / max`，默认 `max`。`max_tokens` 是单次输出上限，不是账户或套餐余额。
- [思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)：`clear_thinking` 控制历史思考内容保留，不是关闭当前轮次思考的开关。
- [对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)：输出到达上限时 `finish_reason=length`；还应处理 `sensitive`、`network_error`、`model_context_window_exceeded`，不能仅凭有文本就认为正常完成。
- [Coding Plan 快速开始](https://docs.bigmodel.cn/cn/coding-plan/quick-start)：OpenAI 协议专用地址为 `https://open.bigmodel.cn/api/coding/paas/v4`，不同于通用 `/api/paas/v4`。
- [Coding Plan 使用须知](https://docs.bigmodel.cn/cn/coding-plan/usage-notes)：仅限官方支持的指定工具与产品环境；非支持工具可能被限制权益。Sona 的接口配置与连接测试不能证明套餐适用性，也不保证抵扣套餐额度。后续转录优化、笔记生成不能默认按 Coding Plan 通用用途处理。

## 当前测试策略

此前 64 tokens 的测试收到 `length` 且正文为空。结合强制思考特性，输出上限过小是主要排查方向；未保存原始回复，不能据此还原完整推理过程。

- 普通测试输出上限调整为 1024 tokens，请求超时设为 30 秒。
- 实际 LiteLLM 路由为 `zai/glm-5.3` 或 `zai/glm-5.3-flash` 时，使用 4096 tokens、45 秒超时、`thinking.type=enabled` 与 `reasoning_effort=low`。这是 Sona 的轻量连接测试配置，不是官方推荐的完整任务参数，也不保证任何情况下均可在该预算内完成。
- 当前安装的 LiteLLM ZAI 适配器未把 `reasoning_effort` 列为支持的标准参数，因此通过其支持的 `extra_body` 传递智谱参数，不修改第三方依赖。
- 其他供应商、未知模型与改写到其他提供商的高级路由不注入智谱参数。
- 每次点击只发送一次模型请求，关闭自动重试，不切换端点、模型或默认选择。输出上限不是固定实际消耗，模型提前完成即结束。
- 保留正文校验；仅有思考、空回复、截断或供应商明确异常不能测试通过。错误提示区分单次输出限制与套餐额度耗尽。
- 应用自有日志只记录安全错误说明、白名单终止原因和正文 / 思考字符数，不输出 Key、正文或思考原文。没有审计第三方库的所有日志行为。

已补充模拟回归用例；未运行测试，未读取真实密钥或发起真实模型请求。界面与真实供应商兼容性由开发者手动验收。

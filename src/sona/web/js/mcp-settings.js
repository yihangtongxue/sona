import { showToast } from "./toast.js";

const status = document.querySelector("#mcp-status");
const copyButton = document.querySelector("#copy-codex-config");
const warning = document.querySelector("#mcp-warning");
const snippet = document.querySelector("#mcp-snippet");
let config = null;

function describe(state) {
  if (!state.running) return `MCP 服务未运行${state.error ? `：${state.error}` : "。请重新启动 Sona。"}`;
  const last = state.last_request_at
    ? `，最近一次请求 ${new Date(state.last_request_at * 1000).toLocaleString("zh-CN", { hourCycle: "h23" })}`
    : "，尚未有客户端连接";
  return `MCP 服务运行中（仅本机 127.0.0.1:${state.port}）${last}。`;
}

export async function openMCPSettings() {
  if (!window.pywebview?.api) return;
  try {
    const state = await window.pywebview.api.mcp_status();
    status.textContent = describe(state);
    config = state.codex;
    copyButton.disabled = !config;
    warning.hidden = !config?.warning;
    warning.textContent = config?.warning ?? "";
    snippet.hidden = !config;
    snippet.textContent = config?.snippet ?? "";
  } catch (error) {
    status.textContent = `无法读取 MCP 状态：${error?.message ?? error}`;
  }
}

copyButton.addEventListener("click", async () => {
  if (!config) return;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("当前环境不支持剪贴板，请手动复制下方配置。");
    await navigator.clipboard.writeText(config.snippet);
    showToast("Codex 配置已复制，粘贴到 ~/.codex/config.toml 后重启 Codex。", "success");
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  }
});

import { formatBytes } from "./format.js";
import { showToast } from "./toast.js";

const panel = document.querySelector('[data-panel="settings"]');
const status = document.querySelector("#acceleration-status");
const detail = document.querySelector("#acceleration-detail");
const button = document.querySelector("#acceleration-action");
const region = document.querySelector("#acceleration-progress-region");
const progress = document.querySelector("#acceleration-progress");
const label = document.querySelector("#acceleration-progress-label");
const feedback = document.querySelector("#acceleration-feedback");
const feedbackMessage = document.querySelector("#acceleration-feedback-message");
const reconnect = document.querySelector("#acceleration-reconnect");
let snapshot;
let timer;
let actionPending = false;
let connected = true;
let queue = Promise.resolve();

const labels = {
  checking: "正在检查…", cpu: "当前使用 CPU 转录", apple: "已启用 Apple 芯片加速",
  unsupported: "此 Mac 暂不支持转录加速", available: "可使用显卡加速",
  downloading: "正在下载加速组件", preparing: "正在准备加速组件", verifying: "正在检查加速是否可用…",
  pausing: "正在暂停…", paused: "下载已暂停", ready: "已启用显卡加速",
  disabled: "当前使用 CPU 转录", download_failed: "加速组件下载失败",
  check_failed: "加速暂不可用", driver_required: "需要更新显卡驱动",
};

function render() {
  if (!snapshot) return;
  const state = snapshot.status;
  const size = formatBytes(snapshot.total_bytes);
  const descriptions = {
    available: `下载大小：${size}，下载后可加快转录。`,
    disabled: snapshot.has_runtime ? "已下载的组件仍保留。" : `下载大小：${size}，下载后可加快转录。`,
    downloading: "当前仍可使用 CPU 转录。", preparing: "当前仍可使用 CPU 转录。",
    paused: "当前使用 CPU 转录。", download_failed: `当前使用 CPU 转录。下载大小：${size}。`,
    check_failed: "当前使用 CPU 转录，可重新检查。",
    driver_required: "请更新显卡驱动后重新检查，当前使用 CPU 转录。",
  };
  const actions = {
    available: ["download", "下载并启用"], paused: ["download", "继续下载"],
    downloading: ["pause", "暂停下载"], preparing: ["pause", "暂停下载"],
    ready: ["disable", "关闭加速"], disabled: snapshot.has_runtime ? ["enable", "启用加速"] : ["download", "下载并启用"],
    download_failed: ["download", "重试"], check_failed: ["check", "重新检查"],
    driver_required: ["check", "重新检查"], cpu: ["check", "重新检查"],
  };
  const action = actions[state];
  status.textContent = labels[state] ?? "暂时无法读取加速状态";
  detail.textContent = descriptions[state] ?? "";
  detail.hidden = !detail.textContent;
  button.hidden = !action;
  button.dataset.action = action?.[0] ?? "";
  button.textContent = action?.[1] ?? "";
  button.disabled = actionPending || !connected;
  const showProgress = ["downloading", "preparing", "verifying", "pausing", "paused"].includes(state);
  region.hidden = !showProgress;
  if (showProgress) {
    if (["preparing", "verifying"].includes(state)) {
      progress.removeAttribute("value");
      label.textContent = state === "preparing" ? "正在准备…" : "正在检查…";
    } else {
      progress.value = Math.min(1, Math.max(0, snapshot.downloaded_bytes / snapshot.total_bytes));
      label.textContent = `${Math.floor(progress.value * 100)}% · ${formatBytes(snapshot.downloaded_bytes)} / ${size}`;
    }
  }
}

function request(action) {
  if (actionPending) return;
  clearTimeout(timer);
  if (action) {
    actionPending = true;
    render();
  }
  // Mutations queue behind polls so a stale snapshot never undoes a click.
  queue = queue.then(async () => {
    try {
      const api = window.pywebview?.api;
      if (!api) throw new Error("桌面连接尚未就绪，请稍后重试。");
      snapshot = await (action ? api.acceleration_action(action) : api.acceleration_status());
      connected = true;
      feedback.hidden = true;
      if (action) {
        const notices = {
          download: "已提交加速组件下载请求。", pause: "已提交暂停下载请求。",
          enable: "正在启用加速。", disable: "已关闭加速。", check: "已提交加速检查请求。",
        };
        if (["download_failed", "check_failed"].includes(snapshot.status)) {
          showToast(snapshot.error || labels[snapshot.status], "error");
        } else showToast(notices[action], action === "disable" ? "success" : "info");
      }
    } catch (error) {
      console.error("Acceleration request failed", error);
      if (action && connected && window.pywebview?.api) {
        showToast(`加速设置操作失败：${String(error?.message ?? error)}`, "error");
      } else {
        connected = false;
        feedbackMessage.textContent = "无法读取加速状态，请重新加载。";
        feedback.hidden = false;
      }
    } finally {
      if (action) actionPending = false;
      render();
      clearTimeout(timer);
      if (!panel.hidden && connected && window.pywebview?.api && !actionPending) {
        timer = setTimeout(() => request(), 1000);
      }
    }
  });
}

export function openAccelerationSettings() { request(); }
button.addEventListener("click", () => request(button.dataset.action));
reconnect.addEventListener("click", () => request());
window.addEventListener("view-changed", () => { if (panel.hidden) clearTimeout(timer); });
window.addEventListener("pywebviewready", () => { if (!panel.hidden) request(); });

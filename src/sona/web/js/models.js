import { formatBytes } from "./format.js";
import { showToast } from "./toast.js";
import { confirmAction } from "./dialog.js";

const list = document.querySelector("#speech-models");
const template = document.querySelector("#speech-model-template");
const feedback = document.querySelector("#models-feedback");
const feedbackMessage = document.querySelector("#models-feedback-message");
const reconnectButton = document.querySelector("#models-reconnect");
const rows = new Map();
let models = [];
let requestInFlight = false;
let confirmingDeletion = false;
let pollTimer;
let connected = true;
let hasLoaded = false;
let requestQueue = Promise.resolve();

const statusLabels = {
  unchecked: "待确认", starting: "启动中", checking: "检查中",
  preparing: "准备中", downloading: "下载中", verifying: "检查中",
  installed: "已安装", waiting: "等待系统", supported: "未安装",
  unsupported: "不支持", failed: "失败", unknown: "状态未知",
  locked: "使用中", paused: "已暂停",
};
const terminalStatuses = new Set(["installed", "waiting", "supported", "unsupported", "failed", "unknown", "locked", "paused"]);

export function openModelSettings() {
  // Page navigation reuses session state without interrupting task polling.
  if ((hasLoaded && connected) || requestInFlight) return;
  if (!window.pywebview?.api) {
    showConnectionError(new Error("正在等待桌面应用连接。"));
    return;
  }
  requestModels("refresh_models");
}

// Bridge readiness alone never starts a system query on the upload screen.
window.addEventListener("pywebviewready", () => {
  if (!document.querySelector('[data-panel="settings"]').hidden) openModelSettings();
});
reconnectButton.addEventListener("click", () => requestModels("refresh_models"));

function requestModels(method, modelId) {
  const isBackgroundPoll = method === "list_models";
  if (requestInFlight) return;
  clearTimeout(pollTimer);
  if (!isBackgroundPoll) {
    requestInFlight = true;
    reconnectButton.disabled = true;
    updateRows();
  }
  // A user action queues behind an outstanding poll. Only one bridge request
  // runs at a time, so an old snapshot can never overwrite a newer action.
  const request = requestQueue.then(() => performRequest(method, modelId, isBackgroundPoll));
  requestQueue = request.catch(() => {});
  return request;
}

async function performRequest(method, modelId, isBackgroundPoll) {
  try {
    if (!window.pywebview?.api) throw new Error("桌面应用连接尚未就绪。");
    const api = window.pywebview.api;
    const result = await (modelId === undefined ? api[method]() : api[method](modelId));
    if (!Array.isArray(result)) throw new Error("模型列表返回格式不正确。");
    models = result.filter((model) => model.kind === "speech");
    hasLoaded = true;
    connected = true;
    feedback.hidden = true;
    const notices = {
      download_model: "已提交模型下载请求。",
      pause_model: "已提交暂停下载请求。",
      select_model: "已提交默认模型切换请求。",
      delete_model: "已提交模型清理请求。",
    };
    if (notices[method]) {
      const model = models.find((item) => item.id === modelId);
      if (model?.status === "failed" && !model.active) {
        showToast(`模型操作失败：${model.error || model.detail || "请查看模型状态。"}`, "error");
      } else showToast(notices[method]);
    }
  } catch (error) {
    if (modelId !== undefined && connected && window.pywebview?.api) {
      showToast(`模型操作失败：${String(error?.message ?? error)}`, "error");
    } else showConnectionError(error);
  } finally {
    if (!isBackgroundPoll) {
      requestInFlight = false;
      reconnectButton.disabled = false;
    }
    updateRows();
    clearTimeout(pollTimer);
    if (connected && !requestInFlight && models.some((model) => model.active)) {
      pollTimer = setTimeout(() => requestModels("list_models"), 600);
    }
  }
}

function getAction(model) {
  if (!connected) return { method: "refresh_model", label: "重新连接" };
  if (model.active) {
    if (model.cancelling) return { method: "refresh_model", label: "正在暂停" };
    if (model.action === "delete") return { method: "refresh_model", label: "正在清理" };
    const label = terminalStatuses.has(model.status) ? "保存中" : statusLabels[model.status] ?? "处理中";
    return { method: "refresh_model", label: model.action === "select" ? "正在确认" : label };
  }
  if (model.status === "installed" && !model.selected && model.can_transcribe) {
    return { method: "select_model", label: "设为默认" };
  }
  if (["supported", "paused"].includes(model.status)) {
    return { method: "download_model", label: model.downloaded_bytes > 0 ? "继续下载" : "下载" };
  }
  if (model.status === "failed" && model.action === "download") {
    return { method: "download_model", label: "重试下载" };
  }
  return { method: "refresh_model", label: model.status === "unknown" ? "重新检查" : "刷新状态" };
}

function updateRows() {
  if (!hasLoaded) return;
  list.querySelector(".models-loading")?.remove();
  const modelIds = new Set(models.map((model) => model.id));
  for (const [id, row] of rows) {
    if (!modelIds.has(id)) {
      row.remove();
      rows.delete(id);
    }
  }
  if (!models.length) {
    const empty = document.createElement("p");
    empty.className = "models-loading";
    empty.textContent = "暂无音频模型";
    list.append(empty);
    return;
  }
  for (const model of models) {
    let row = rows.get(model.id);
    if (!row) {
      row = template.content.firstElementChild.cloneNode(true);
      row.dataset.modelId = model.id;
      row.querySelector("[data-model-action]").addEventListener("click", () => {
        const current = models.find((item) => item.id === model.id);
        if (!current || current.active || requestInFlight) return;
        row.querySelector("[data-model-error]").open = false;
        const action = getAction(current);
        requestModels(action.method, current.id);
      });
      row.querySelector("[data-model-delete]").addEventListener("click", async () => {
        const current = models.find((item) => item.id === model.id);
        if (!current || current.active || requestInFlight || confirmingDeletion || !connected) return;
        const confirmation = current.status === "installed"
          ? `将删除“${current.name}”。使用前需要重新下载。`
          : `将清理“${current.name}”已下载的文件，下次下载将重新开始。`;
        const selectionNote = current.selected ? "\n删除后需要重新选择默认模型。" : "";
        confirmingDeletion = true;
        let confirmed;
        try {
          confirmed = await confirmAction({
            title: current.status === "installed" ? "删除模型？" : "清理下载文件？",
            message: confirmation + selectionNote,
            confirmLabel: current.status === "installed" ? "删除模型" : "清理文件",
            destructive: true,
            getReturnFocus: () => rows.get(current.id)?.querySelector("[data-model-delete]"),
          });
        } finally { confirmingDeletion = false; }
        if (!confirmed) return;
        const latest = models.find((item) => item.id === current.id);
        if (!latest || latest.active || latest.status === "locked" || requestInFlight || !connected
          || latest.storage !== "managed" || !(latest.has_files || latest.status === "installed")
          || latest.selected !== current.selected || latest.status !== current.status) {
          showToast("模型状态已变化，请查看当前状态后重新操作。");
          return;
        }
        requestModels("delete_model", latest.id);
      });
      row.querySelector("[data-model-pause]").addEventListener("click", () => {
        const current = models.find((item) => item.id === model.id);
        if (!current?.active || current.cancelling || requestInFlight) return;
        requestModels("pause_model", current.id);
      });
      rows.set(model.id, row);
      list.append(row);
    }
    renderModel(row, model);
  }
}

function renderModel(row, model) {
  const find = (selector) => row.querySelector(selector);
  const busy = Boolean(model.active);
  const ready = connected && model.status === "installed" && !busy;
  const showProgress = connected && busy && model.action === "download" && !terminalStatuses.has(model.status);
  row.setAttribute("aria-label", `${model.name}${model.selected ? "，已选择" : ""}`);
  find("[data-model-name]").textContent = model.name;
  const description = find("[data-model-description]");
  description.textContent = model.description ?? "";
  description.hidden = !model.description;
  const detail = find("[data-model-detail]");
  detail.textContent = model.cancelling
    ? "正在暂停，请稍候。" : model.detail;
  // Routine state already appears in the badge/progress. Keep explanations for
  // failures, availability restrictions and unfinished resources visible.
  detail.hidden = !connected || !detail.textContent || !(model.cancelling || model.error
    || ["unknown", "unsupported", "failed", "locked", "waiting"].includes(model.status)
    || (model.status === "supported" && model.has_files));
  const size = find("[data-model-size]");
  size.hidden = model.storage !== "managed" || showProgress
    || !(model.total_bytes > 0 || model.downloaded_bytes > 0);
  const total = model.total_bytes > 0 ? formatBytes(model.total_bytes) : "总大小待确认";
  size.textContent = model.status === "installed" ? `文件大小：${total}`
    : model.downloaded_bytes > 0 ? `已下载 ${formatBytes(model.downloaded_bytes)} / ${total}`
    : model.total_bytes > 0 ? `下载大小：${total}` : "";
  const action = getAction(model);
  const badge = find("[data-model-status]");
  badge.textContent = !connected ? "连接异常"
    : busy ? action.label
    : ready && model.selected && model.can_transcribe ? "默认模型" : statusLabels[model.status] ?? "状态未知";
  badge.classList.toggle("is-muted", connected && !busy
    && ["unchecked", "supported", "unsupported", "paused"].includes(model.status));
  badge.classList.toggle("is-unavailable", !connected || ["failed", "unknown"].includes(model.status));
  const button = find("[data-model-action]");
  button.textContent = action.label;
  button.hidden = busy;
  const selectionPending = action.method === "select_model"
    && models.some((item) => item.active && item.action === "select");
  button.disabled = busy || requestInFlight || selectionPending;
  button.setAttribute("aria-busy", String(busy));
  button.setAttribute("aria-label", `${action.label}：${model.name}`);

  const deleteButton = find("[data-model-delete]");
  const canDelete = model.storage === "managed" && model.status !== "locked"
    && (model.has_files || model.status === "installed") && !busy;
  deleteButton.hidden = !canDelete;
  deleteButton.disabled = !canDelete || requestInFlight || !connected;
  deleteButton.textContent = model.status === "installed" ? "删除" : "清理文件";
  deleteButton.setAttribute("aria-label", `${deleteButton.textContent}：${model.name}`);

  const pauseButton = find("[data-model-pause]");
  pauseButton.hidden = !(busy && model.storage === "managed" && model.action === "download"
    && !terminalStatuses.has(model.status));
  pauseButton.disabled = Boolean(model.cancelling) || requestInFlight || !connected;
  pauseButton.setAttribute("aria-label", `暂停下载：${model.name}`);

  const progressRegion = find("[data-model-progress-region]");
  progressRegion.hidden = !showProgress;
  const progress = find("[data-model-progress]");
  const progressValue = find("[data-model-progress-value]");
  const elapsed = find("[data-model-elapsed]");
  if (!showProgress) {
    progress.removeAttribute("value");
    progressValue.textContent = "";
    elapsed.textContent = "";
  } else {
    const bytes = model.downloaded_bytes > 0
      ? `${formatBytes(model.downloaded_bytes)}${model.total_bytes > 0 ? ` / ${total}` : ""}` : "";
    let progressLabel;
    if (typeof model.progress === "number" && Number.isFinite(model.progress)) {
      progress.value = Math.max(0, Math.min(1, model.progress));
      progressLabel = `${Math.floor(progress.value * 100)}%`;
    } else {
      progress.removeAttribute("value");
      progressLabel = model.status === "downloading"
        ? "正在下载" : statusLabels[model.status] ?? "处理中";
    }
    progressValue.textContent = [progressLabel, bytes].filter(Boolean).join(" · ");
    elapsed.textContent = typeof model.elapsed === "number"
      ? `已用时 ${Math.floor(model.elapsed / 60)}分${model.elapsed % 60}秒` : "";
  }
  find("[data-model-error]").hidden = !model.error;
  find("[data-model-error-detail]").textContent = model.error ?? "";
}

function showConnectionError(error) {
  connected = false;
  feedback.hidden = false;
  feedbackMessage.textContent = `无法更新模型状态：${String(error?.message ?? error)}`;
  const loading = list.querySelector(".models-loading");
  if (loading) loading.textContent = "模型状态暂不可用";
  updateRows();
}

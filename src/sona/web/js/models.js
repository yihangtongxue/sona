const list = document.querySelector("#speech-models");
const template = document.querySelector("#speech-model-template");
const feedback = document.querySelector("#models-feedback");
const feedbackMessage = document.querySelector("#models-feedback-message");
const reconnectButton = document.querySelector("#models-reconnect");
const rows = new Map();
let models = [];
let requestInFlight = false;
let pollTimer;
let connected = true;
let hasLoaded = false;

const statusLabels = {
  unchecked: "待确认", starting: "启动中", checking: "检查中",
  preparing: "准备中", downloading: "下载中", verifying: "确认中",
  installed: "已安装", waiting: "等待系统", supported: "未安装",
  unsupported: "不支持", failed: "失败", unknown: "状态未知",
};
const terminalStatuses = new Set(["installed", "waiting", "supported", "unsupported", "failed", "unknown"]);

export function openModelSettings() {
  // Page navigation reuses session state without interrupting task polling.
  if (hasLoaded || requestInFlight) return;
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

async function requestModels(method, modelId) {
  if (requestInFlight) return;
  clearTimeout(pollTimer);
  requestInFlight = true;
  reconnectButton.disabled = true;
  updateRows();
  let succeeded = false;
  try {
    if (!window.pywebview?.api) throw new Error("桌面应用连接尚未就绪。");
    const api = window.pywebview.api;
    const result = await (modelId === undefined ? api[method]() : api[method](modelId));
    if (!Array.isArray(result)) throw new Error("模型列表返回格式不正确。");
    models = result.filter((model) => model.kind === "speech");
    hasLoaded = true;
    connected = true;
    feedback.hidden = true;
    succeeded = true;
  } catch (error) {
    showConnectionError(error);
  } finally {
    requestInFlight = false;
    reconnectButton.disabled = false;
    updateRows();
    if (succeeded && models.some((model) => model.active)) {
      pollTimer = setTimeout(() => requestModels("list_models"), 600);
    }
  }
}

function getAction(model) {
  if (!connected) return { method: "refresh_model", label: "重新连接" };
  if (model.active) {
    const label = terminalStatuses.has(model.status) ? "保存中" : statusLabels[model.status] ?? "处理中";
    return { method: "refresh_model", label: model.action === "select" ? "确认使用中" : label };
  }
  if (model.status === "installed" && !model.selected) {
    return { method: "select_model", label: "使用" };
  }
  if (model.status === "supported") return { method: "download_model", label: "下载" };
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
  row.classList.toggle("is-selected", Boolean(model.selected));
  row.setAttribute("aria-label", `${model.name}${model.selected ? "，已选择" : ""}`);
  find("[data-model-name]").textContent = model.name;
  find("[data-model-detail]").textContent = model.detail;
  const badge = find("[data-model-status]");
  badge.textContent = !connected ? "连接异常"
    : ready && model.selected ? "使用中" : statusLabels[model.status] ?? "状态未知";
  badge.hidden = connected && busy;
  badge.classList.toggle("is-unavailable", !connected || ["failed", "unsupported", "unknown"].includes(model.status));
  const action = getAction(model);
  const button = find("[data-model-action]");
  button.textContent = action.label;
  const selectionPending = action.method === "select_model"
    && models.some((item) => item.active && item.action === "select");
  button.disabled = busy || requestInFlight || selectionPending;
  button.setAttribute("aria-busy", String(busy));
  button.setAttribute("aria-label", `${action.label}：${model.name}`);

  const progressRegion = find("[data-model-progress-region]");
  const showProgress = connected && busy && model.action === "download" && !terminalStatuses.has(model.status);
  progressRegion.hidden = !showProgress;
  const progress = find("[data-model-progress]");
  const progressValue = find("[data-model-progress-value]");
  const elapsed = find("[data-model-elapsed]");
  if (!showProgress) {
    progress.removeAttribute("value");
    progressValue.textContent = "";
    elapsed.textContent = "";
  } else {
    if (typeof model.progress === "number" && Number.isFinite(model.progress)) {
      progress.value = model.progress;
      progressValue.textContent = `系统进度 ${Math.floor(model.progress * 100)}%`;
    } else {
      progress.removeAttribute("value");
      progressValue.textContent = model.status === "downloading"
        ? "等待系统提供进度" : statusLabels[model.status] ?? "处理中";
    }
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

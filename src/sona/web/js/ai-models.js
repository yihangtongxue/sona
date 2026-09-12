import { showToast } from "./toast.js";
import { confirmAction } from "./dialog.js";
import { createDropdown } from "./dropdown.js";

const list = document.querySelector("#ai-models");
const template = document.querySelector("#ai-model-template");
const addButton = document.querySelector("#add-ai-model");
const feedback = document.querySelector("#ai-model-feedback");
const feedbackMessage = document.querySelector("#ai-model-feedback-message");
const reconnectButton = document.querySelector("#ai-model-reconnect");
const dialog = document.querySelector("#ai-model-dialog");
const form = document.querySelector("#ai-model-form");
const dialogTitle = document.querySelector("#ai-model-dialog-title");
const dialogCancel = document.querySelector("#ai-model-dialog-cancel");
const providerInput = form.elements.provider;
const providerDropdown = createDropdown(document.querySelector("#ai-provider-select"), {
  value: providerInput.value,
  onChange: chooseProvider,
});
const urlField = document.querySelector("#ai-model-url-field");
const resetUrlButton = document.querySelector("#ai-model-reset-url");
const keyToggle = document.querySelector("#ai-model-key-toggle");
const keySlash = document.querySelector("#ai-model-key-slash");
const formError = document.querySelector("#ai-model-form-error");
const lastTestError = document.querySelector("#ai-model-last-error");
const saveButton = form.querySelector('[type="submit"]');
const rows = new Map();
const providerLabels = {
  openai: "OpenAI",
  anthropic: "Claude",
  google: "Google Gemini",
  zhipu: "智谱 GLM",
  "zhipu-coding": "智谱 Coding Plan",
  "openai-compatible": "自定义接口",
};
const statusLabels = { untested: "未测试", ready: "已验证", failed: "测试失败" };
let models = [];
let connected = true;
let loaded = false;
let requestInFlight = false;
let requestQueue = Promise.resolve();
let editingId = null;
let readOnly = false;
let saving = false;
let activeTestId = null;
let legacyUrlProvider = null;
let keyLoading = false;
let keyReadVersion = 0;
let keyAutofilled = false;

function setKeyVisible(visible) {
  form.elements.api_key.type = visible ? "text" : "password";
  keyToggle.setAttribute("aria-pressed", String(visible));
  keyToggle.setAttribute("aria-label", visible ? "隐藏 API Key" : "显示 API Key");
  keyToggle.title = visible ? "隐藏 API Key" : "显示 API Key";
  keySlash.toggleAttribute("hidden", !visible);
}

function clearKey() {
  form.elements.api_key.value = "";
  keyAutofilled = false;
  setKeyVisible(false);
}

function updateEditorControls() {
  setProvider(providerInput.value);
  const busy = saving || keyLoading;
  if (busy) form.setAttribute("aria-busy", "true");
  else form.removeAttribute("aria-busy");
  form.querySelectorAll("button, input").forEach((control) => {
    const readOnlyButton = readOnly && control.tagName === "BUTTON"
      && control !== keyToggle && control !== dialogCancel;
    control.disabled = busy || readOnlyButton || (control === form.elements.base_url && urlField.hidden);
    if (control.tagName === "INPUT") control.readOnly = readOnly;
  });
  saveButton.hidden = readOnly;
  dialogCancel.textContent = readOnly ? "关闭" : "取消";
  dialogCancel.disabled = saving;
}

function closeProviderMenu() {
  providerDropdown.close();
}

function chooseProvider(provider) {
  if (providerInput.value !== provider) {
    clearKey();
    form.elements.base_url.value = "";
    form.elements.model_name.value = "";
    legacyUrlProvider = null;
  }
  setProvider(provider);
}

function updateKeyField() {
  form.elements.api_key.required = true;
  form.elements.api_key.placeholder = "输入 API Key";
}

function setProvider(provider) {
  providerInput.value = provider;
  providerDropdown.setValue(provider);
  const custom = provider === "openai-compatible";
  const legacy = legacyUrlProvider === provider;
  urlField.hidden = !custom && !legacy;
  form.elements.base_url.disabled = urlField.hidden;
  form.elements.base_url.required = !urlField.hidden;
  resetUrlButton.hidden = readOnly || !legacy || custom;
  updateKeyField();
}

function api() {
  if (!window.pywebview?.api) throw new Error("桌面应用连接尚未就绪。");
  return window.pywebview.api;
}

export function openAIModelSettings() {
  if (!document.querySelector('[data-panel="settings"]').hidden) loadModels();
}

async function loadModels() {
  if (requestInFlight) return;
  requestInFlight = true;
  render();
  try {
    const result = await api().list_ai_models();
    if (!Array.isArray(result)) throw new Error("AI 模型列表返回格式不正确。");
    models = result;
    connected = true;
    loaded = true;
    feedback.hidden = true;
    render();
  } catch (error) {
    connected = false;
    feedback.hidden = false;
    feedbackMessage.textContent = `无法读取 AI 模型：${String(error?.message ?? error)}`;
    render();
  } finally {
    requestInFlight = false;
    render();
  }
}

function enqueue(action) {
  const request = requestQueue.then(action);
  requestQueue = request.catch(() => {});
  return request;
}

function update(result) {
  if (!Array.isArray(result)) throw new Error("AI 模型列表返回格式不正确。");
  models = result;
  connected = true;
  loaded = true;
  feedback.hidden = true;
  render();
}

function render() {
  addButton.disabled = requestInFlight || !connected;
  reconnectButton.disabled = requestInFlight;
  if (!loaded) {
    const loading = list.querySelector(".models-loading");
    if (loading) loading.hidden = !connected;
    return;
  }
  list.querySelector(".models-loading")?.remove();
  const ids = new Set(models.map((model) => model.id));
  for (const [id, row] of rows) {
    if (!ids.has(id)) { row.remove(); rows.delete(id); }
  }
  if (!models.length) {
    if (!list.querySelector(".models-empty")) {
      const empty = document.createElement("p");
      empty.className = "models-empty";
      empty.textContent = "还没有 AI 模型，点击“新增模型”开始配置。";
      list.append(empty);
    }
    return;
  }
  list.querySelector(".models-empty")?.remove();
  for (const model of models) {
    let row = rows.get(model.id);
    if (!row) {
      row = template.content.firstElementChild.cloneNode(true);
      row.dataset.modelId = model.id;
      row.querySelector("[data-ai-model-test]").addEventListener("click", () => runAction("test_ai_model", model.id));
      row.querySelector("[data-ai-model-select]").addEventListener("click", () => runAction("select_ai_model", model.id));
      row.querySelector("[data-ai-model-edit]").addEventListener("click", () => openEditor(model.id));
      row.querySelector("[data-ai-model-view]").addEventListener("click", () => openEditor(model.id));
      row.querySelector("[data-ai-model-delete]").addEventListener("click", () => removeModel(model.id));
      rows.set(model.id, row);
      list.append(row);
    }
    renderModel(row, model);
  }
}

function renderModel(row, model) {
  const status = model.status ?? "untested";
  row.querySelector("[data-ai-model-name]").textContent = model.name;
  const badge = row.querySelector("[data-ai-model-status]");
  badge.textContent = model.selected ? `默认模型 · ${statusLabels[status] ?? "状态未知"}` : statusLabels[status] ?? "状态未知";
  badge.classList.toggle("is-unavailable", status === "failed");
  badge.classList.toggle("is-muted", !model.selected && status === "untested");
  const test = row.querySelector("[data-ai-model-test]");
  test.disabled = !connected || requestInFlight;
  test.textContent = activeTestId === model.id ? "正在测试…" : "测试连接";
  const select = row.querySelector("[data-ai-model-select]");
  select.hidden = Boolean(model.selected);
  select.disabled = !connected || requestInFlight || status !== "ready";
  select.title = status !== "ready" ? "请先测试连接，通过后才能设为默认。" : "";
  const edit = row.querySelector("[data-ai-model-edit]");
  edit.hidden = Boolean(model.selected);
  edit.disabled = !connected || requestInFlight || Boolean(model.selected);
  const view = row.querySelector("[data-ai-model-view]");
  view.hidden = !model.selected;
  view.disabled = !connected || requestInFlight;
  const remove = row.querySelector("[data-ai-model-delete]");
  remove.hidden = Boolean(model.selected);
  remove.disabled = !connected || requestInFlight || Boolean(model.selected);
  const errorDetails = row.querySelector("[data-ai-model-error]");
  errorDetails.hidden = status !== "failed" || !model.last_error;
  if (errorDetails.hidden) errorDetails.open = false;
  row.querySelector("[data-ai-model-error-detail]").textContent = model.last_error || "";
}

async function runAction(method, identifier) {
  if (requestInFlight) return;
  const wasSelected = Boolean(models.find((model) => model.id === identifier)?.selected);
  requestInFlight = true;
  activeTestId = method === "test_ai_model" ? identifier : null;
  render();
  try {
    const result = await enqueue(() => api()[method](identifier));
    update(result);
    if (method === "test_ai_model") {
      const model = models.find((item) => item.id === identifier);
      if (model?.status === "ready") showToast("AI 模型连接测试成功。", "success");
      else {
        const defaultNotice = wasSelected && model && !model.selected ? "已取消默认模型，可编辑配置。" : "";
        showToast(`AI 模型测试失败：${model?.last_error || "请检查配置。"}${defaultNotice}`, "error");
      }
    } else if (method === "select_ai_model") showToast("已设置默认 AI 模型。", "success");
    else if (method === "delete_ai_model") showToast("模型配置已删除。", "success");
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  } finally {
    requestInFlight = false;
    activeTestId = null;
    render();
  }
}

async function openEditor(identifier = null) {
  if (requestInFlight || saving || dialog.open) return;
  const model = models.find((item) => item.id === identifier);
  readOnly = Boolean(model?.selected);
  const version = ++keyReadVersion;
  editingId = identifier;
  dialogTitle.textContent = readOnly ? "模型详情" : model ? "编辑模型" : "新增模型";
  form.reset();
  clearKey();
  closeProviderMenu();
  formError.hidden = true;
  lastTestError.textContent = model?.last_error ? `上次测试失败：${model.last_error}` : "";
  lastTestError.hidden = !lastTestError.textContent;
  legacyUrlProvider = model?.base_url && model.provider !== "openai-compatible" ? model.provider : null;
  if (model) {
    form.elements.name.value = model.name;
    form.elements.model_name.value = model.model_name;
    form.elements.base_url.value = model.base_url ?? "";
  }
  setProvider(model?.provider ?? "openai");
  keyLoading = Boolean(model?.has_api_key);
  updateEditorControls();
  if (keyLoading) form.elements.api_key.placeholder = "正在读取…";
  dialog.showModal();
  if (model && !keyLoading && !readOnly) form.elements.model_name.focus();
  else dialogTitle.focus({ preventScroll: true });
  if (!keyLoading) return;
  try {
    const secret = await api().get_ai_model_api_key(identifier);
    if (version !== keyReadVersion || !dialog.open) return;
    if (typeof secret !== "string" || !secret) throw new Error("无法读取已保存的 API Key。");
    form.elements.api_key.value = secret;
    keyAutofilled = Boolean(secret);
  } catch (error) {
    if (version !== keyReadVersion || !dialog.open) return;
    const reason = String(error?.message ?? "无法读取已保存的 API Key。");
    formError.textContent = readOnly ? reason : `${reason} 可以填写新密钥后重新保存。`;
    formError.hidden = false;
  } finally {
    if (version === keyReadVersion && dialog.open) {
      keyLoading = false;
      updateEditorControls();
    }
  }
}

async function saveModel(event) {
  event.preventDefault();
  if (readOnly || saving || keyLoading || requestInFlight) return;
  if (models.find((item) => item.id === editingId)?.selected) return;
  if (!form.elements.api_key.value.trim()) {
    formError.textContent = "请填写 API Key。";
    formError.hidden = false;
    form.elements.api_key.focus();
    return;
  }
  const identifier = editingId;
  const current = models.find((item) => item.id === identifier);
  const values = new FormData(form);
  const config = current?.provider === values.get("provider") ? current.config ?? {} : {};
  const args = [values.get("name"), values.get("provider"), values.get("model_name"),
    values.get("base_url") ?? "", values.get("api_key"), config];
  saving = true;
  closeProviderMenu();
  requestInFlight = true;
  formError.hidden = true;
  form.setAttribute("aria-busy", "true");
  form.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
  saveButton.textContent = "正在保存…";
  render();
  try {
    const result = await enqueue(() => identifier
      ? api().update_ai_model(identifier, ...args)
      : api().add_ai_model(...args));
    update(result);
    dialog.close();
    const saved = identifier ? models.find((model) => model.id === identifier) : null;
    const message = !identifier ? "模型已保存，请先测试连接。"
      : saved?.status === "untested" ? "模型配置已更新，请测试连接。" : "模型配置已保存。";
    showToast(message, "success");
  } catch (error) {
    formError.textContent = `保存失败：${String(error?.message ?? error)} 输入已保留，请修改后重试。`;
    formError.hidden = false;
    formError.scrollIntoView({ block: "nearest" });
  } finally {
    saving = false;
    requestInFlight = false;
    form.removeAttribute("aria-busy");
    form.querySelectorAll("button, input").forEach((control) => { control.disabled = false; });
    setProvider(providerInput.value);
    saveButton.textContent = "保存";
    if (!dialog.open) clearKey();
    render();
  }
}

async function removeModel(identifier) {
  if (requestInFlight) return;
  const model = models.find((item) => item.id === identifier);
  if (!model || model.selected) return;
  const confirmed = await confirmAction({
    title: "删除 AI 模型？",
    message: `将删除“${model.name}”的配置和凭据。`,
    confirmLabel: "删除模型",
    destructive: true,
    getReturnFocus: () => rows.get(identifier)?.querySelector("[data-ai-model-delete]"),
  });
  if (!confirmed) return;
  await runAction("delete_ai_model", identifier);
}

addButton.addEventListener("click", () => openEditor());
reconnectButton.addEventListener("click", loadModels);
resetUrlButton.addEventListener("click", () => {
  legacyUrlProvider = null;
  form.elements.base_url.value = "";
  clearKey();
  setProvider(providerInput.value);
});
form.elements.base_url.addEventListener("input", () => {
  // Do not send an automatically restored credential to a changed endpoint.
  if (keyAutofilled) clearKey();
  updateKeyField();
});
form.elements.api_key.addEventListener("input", () => { keyAutofilled = false; });
keyToggle.addEventListener("click", () => setKeyVisible(form.elements.api_key.type === "password"));
dialog.addEventListener("cancel", (event) => {
  if (saving) event.preventDefault();
});
dialog.addEventListener("close", () => {
  ++keyReadVersion;
  keyLoading = false;
  closeProviderMenu();
  clearKey();
  if (!saving) updateEditorControls();
});
dialogCancel.addEventListener("click", () => dialog.close());
form.addEventListener("submit", saveModel);
window.addEventListener("pywebviewready", () => {
  if (!document.querySelector('[data-panel="settings"]').hidden) loadModels();
});

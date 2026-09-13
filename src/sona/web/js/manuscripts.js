import { showToast } from "./toast.js";
import { confirmAction } from "./dialog.js";
import { confirmAIUsage } from "./ai-consent.js";

const panel = document.querySelector('[data-panel="manuscripts"]');
const list = document.querySelector("#manuscript-list");
const table = document.querySelector("#manuscript-table-region");
const viewer = document.querySelector("#manuscript");
const title = document.querySelector("#manuscript-title");
const content = document.querySelector("#manuscript-content");
const copy = document.querySelector("#manuscript-copy");
const exportButton = document.querySelector("#manuscript-export");
const feedback = document.querySelector("#manuscript-feedback");
const feedbackMessage = document.querySelector("#manuscript-feedback-message");
const rows = new Map();
const labels = { queued: "排队中", optimizing: "正在优化", completed: "已完成", failed: "优化失败" };
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
let current = null;
let busy = false;
let loading = false;
let revision = 0;
let timer;

function api() {
  if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
  return window.pywebview.api;
}

function showError(error) {
  feedback.hidden = false;
  feedbackMessage.textContent = String(error?.message ?? error);
}

function button(label, method, record) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `table-action${method === "delete_manuscript" ? " audio-delete" : ""}`;
  element.textContent = label;
  element.dataset.method = method;
  element.setAttribute("aria-label", `${label}：${record.title}`);
  if (method === "delete_manuscript") element.setAttribute("aria-haspopup", "dialog");
  if (method === "retry_manuscript") element.title = "使用当前默认 AI 模型继续未完成的内容，已保存的正文会保留，可能产生费用";
  element.addEventListener("click", () => perform(method, record));
  return element;
}

function render(records) {
  if (!Array.isArray(records)) throw new Error("文稿列表格式不正确。");
  list.querySelector(".audio-empty")?.closest("tr").remove();
  const identifiers = new Set(records.map((record) => record.id));
  for (const [id, entry] of rows) {
    if (!identifiers.has(id)) { entry.row.remove(); rows.delete(id); }
  }
  if (!records.length) {
    const cell = list.insertRow().insertCell();
    cell.colSpan = 4;
    cell.className = "audio-empty";
    cell.textContent = "暂无文稿";
    return;
  }
  records.forEach((record, index) => {
    let entry = rows.get(record.id);
    if (!entry) {
      entry = { row: document.createElement("tr"), signature: "" };
      rows.set(record.id, entry);
    }
    const row = entry.row;
    const signature = JSON.stringify(record);
    if (entry.signature !== signature) {
      const focused = row.contains(document.activeElement) ? document.activeElement.dataset.method : null;
      row.replaceChildren();
      const name = row.insertCell();
      name.className = "audio-name";
      const heading = document.createElement("strong");
      heading.textContent = record.title;
      heading.title = record.title;
      name.append(heading);
      const date = new Date(record.created_at);
      row.insertCell().textContent = Number.isNaN(date.getTime()) ? "—" : dateFormat.format(date);
      const state = row.insertCell();
      const badge = document.createElement("span");
      badge.className = `audio-status is-${record.status === "optimizing" ? "transcribing" : record.status}`;
      badge.textContent = record.status === "optimizing" ? record.detail || labels.optimizing : labels[record.status] || "待处理";
      state.append(badge);
      if (record.status === "failed" && record.source_offset > 0 && record.source_length > 0) {
        const progress = document.createElement("p");
        progress.className = "manuscript-progress";
        progress.textContent = record.source_offset >= record.source_length
          ? "正文已保存，重试仅生成标题"
          : `已保存 ${Math.floor(record.source_offset * 100 / record.source_length)}% 进度，重试将继续优化`;
        state.append(progress);
      }
      if (record.status === "failed" && record.error) {
        const details = document.createElement("details");
        details.className = "manuscript-error";
        const summary = document.createElement("summary");
        summary.textContent = "查看原因";
        const explanation = document.createElement("p");
        explanation.textContent = record.error;
        details.append(summary, explanation);
        state.append(details);
      }
      const operations = row.insertCell();
      operations.className = "audio-operations";
      const actions = document.createElement("div");
      actions.className = "audio-row-actions";
      if (record.status === "completed") actions.append(button("查看", "get_manuscript", record));
      if (record.status === "failed") actions.append(button(record.source_offset > 0 ? "继续优化" : "重试", "retry_manuscript", record));
      if (["completed", "failed"].includes(record.status)) actions.append(button("删除", "delete_manuscript", record));
      operations.append(actions);
      entry.signature = signature;
      if (focused) row.querySelector(`[data-method="${focused}"]`)?.focus({ preventScroll: true });
    }
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] ?? null);
  });
  list.querySelectorAll("button").forEach((element) => { element.disabled = busy; });
}

function schedule() {
  clearTimeout(timer);
  if (!panel.hidden && !current) timer = setTimeout(load, 1500);
}

async function load() {
  if (loading || busy) { schedule(); return; }
  loading = true;
  const token = revision;
  try {
    const records = await api().list_manuscripts();
    if (token !== revision || panel.hidden || current) return;
    render(records);
    feedback.hidden = true;
  } catch (error) {
    if (token === revision && !panel.hidden) {
      showError(error);
      const empty = list.querySelector(".audio-empty");
      if (empty?.textContent === "正在读取…") empty.textContent = "文稿列表暂不可用";
    }
  } finally { loading = false; schedule(); }
}

async function perform(method, record) {
  if (busy) return;
  busy = true;
  revision += 1;
  clearTimeout(timer);
  list.querySelectorAll("button").forEach((element) => { element.disabled = true; });
  const token = revision;
  try {
    if (method === "delete_manuscript") {
      const confirmed = await confirmAction({
        title: "删除文稿？", message: `将永久删除“${record.title}”。`,
        confirmLabel: "删除", destructive: true,
        getReturnFocus: () => rows.get(record.id)?.row.querySelector('[data-method="delete_manuscript"]'),
      });
      if (!confirmed) return;
    }
    if (method === "retry_manuscript" && !await confirmAIUsage(() => token === revision && !panel.hidden)) return;
    if (token !== revision || panel.hidden) return;
    const result = method === "retry_manuscript"
      ? await api().retry_manuscript(record.id, true) : await api()[method](record.id);
    if (method === "get_manuscript") {
      if (token !== revision || panel.hidden) return;
      current = result;
      title.textContent = result.title;
      content.textContent = result.body;
      table.hidden = true;
      viewer.hidden = false;
      feedback.hidden = true;
      title.focus();
    } else {
      showToast(method === "delete_manuscript" ? "已删除文稿。" : "已重新排队。", "success");
    }
  } catch (error) { showToast(String(error?.message ?? error), "error"); }
  finally {
    busy = false;
    list.querySelectorAll("button").forEach((element) => { element.disabled = false; });
    if (!panel.hidden && !current) load();
  }
}

export function openManuscripts() {
  revision += 1;
  current = null;
  viewer.hidden = true;
  table.hidden = false;
  load();
}

document.querySelector("#manuscript-back").addEventListener("click", openManuscripts);
document.querySelector("#manuscript-reload").addEventListener("click", load);
exportButton.addEventListener("click", async () => {
  if (!current || exportButton.disabled) return;
  const identifier = current.id;
  exportButton.disabled = true;
  exportButton.textContent = "正在导出…";
  try {
    if (await api().export_manuscript(identifier)) showToast("TXT 文稿已保存。", "success");
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  } finally {
    exportButton.disabled = false;
    exportButton.textContent = "导出 TXT";
  }
});
copy.addEventListener("click", async () => {
  if (!current) return;
  const text = current.body;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const field = document.createElement("textarea");
      field.value = text;
      field.className = "sr-only";
      document.body.append(field);
      try { field.select(); if (!document.execCommand("copy")) throw new Error("请选中文字后复制。"); }
      finally { field.remove(); copy.focus(); }
    }
    showToast("已复制全文。", "success");
  } catch { showToast("复制失败，请选中文字后复制。", "error"); }
});
window.addEventListener("view-changed", (event) => {
  if (event.detail !== "manuscripts") revision += 1;
  schedule();
});
window.addEventListener("pywebviewready", () => { if (!panel.hidden) load(); });

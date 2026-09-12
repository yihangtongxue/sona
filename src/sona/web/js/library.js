import { formatBytes } from "./format.js";
import { showToast } from "./toast.js";
import { confirmAction } from "./dialog.js";
import { confirmAIUsage } from "./ai-consent.js";

const panel = document.querySelector('[data-panel="library"]');
const list = document.querySelector("#audio-list");
const tableRegion = document.querySelector("#audio-table-region");
const feedback = document.querySelector("#audio-feedback");
const message = document.querySelector("#audio-feedback-message");
const retry = document.querySelector("#audio-retry");
const transcript = document.querySelector("#transcript");
const transcriptTitle = document.querySelector("#transcript-title");
const transcriptContent = document.querySelector("#transcript-content");
const copyButton = document.querySelector("#transcript-copy");
const optimizeButton = document.querySelector("#transcript-optimize");
const taskDialog = document.querySelector("#task-detail-dialog");
const taskDialogName = document.querySelector("#task-detail-name");
const taskDialogContent = document.querySelector("#task-detail-content");
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const labels = {
  waiting_model: "等待模型", queued: "排队中", transcribing: "转录中",
  cancelling: "正在取消", completed: "已完成", failed: "转录失败", cancelled: "已取消",
  waiting_fetch: "等待获取", resolving: "解析中", downloading: "下载中",
  importing: "正在入库", interrupted: "获取中断",
};
const sourceLabels = { manual_subtitles: "人工字幕", automatic_subtitles: "自动字幕", transcription: "本地转录" };
const rows = new Map();
let requestQueue = Promise.resolve();
let busy = false;
let confirmingDeletion = false;
let polling = false;
let timer;
let currentResult = null;
let resultRequest = 0;
let detailRecordId = null;
let creatingManuscript = false;
let locateId = null;
let transcriptRenderTimer;
const TRANSCRIPT_BATCH_SIZE = 200;

function stopTranscriptRender() {
  clearTimeout(transcriptRenderTimer);
  transcriptContent.removeAttribute("aria-busy");
}

function taskPresentation(record) {
  const detail = String(record.transcription_detail ?? "").trim();
  const error = String(record.transcription_error ?? "").trim();
  let label = labels[record.transcription_status] ?? "等待转录";
  let explanation = detail;
  let progress = "";
  if (record.is_podcast_import) {
    if (record.transcription_status === "failed") {
      label = { resolving: "解析失败", downloading: "下载失败", subtitles: "字幕获取失败", processing: "音频转换失败", importing: "入库失败" }[record.stage] ?? "获取失败";
    }
    if (record.transcription_status === "downloading" && record.stage === "processing") {
      label = "正在转换为 MP3";
    } else if (record.transcription_status === "downloading" && record.stage === "subtitles") {
      label = "正在获取字幕";
    } else if (record.transcription_status === "downloading") {
      progress = record.total_bytes > 0
        ? `${Math.min(100, Math.floor(record.downloaded_bytes / record.total_bytes * 100))}% · ${formatBytes(record.downloaded_bytes)}`
        : `已下载 ${formatBytes(record.downloaded_bytes)}`;
    }
    if (record.transcription_status === "importing") explanation = record.stage === "subtitles"
      ? "字幕已获取，正在保存文字结果。" : "下载完成，正在等待入库并加入转录队列。";
  }
  if (record.transcription_status === "transcribing") {
    const stages = { "正在准备转录。": "准备中", "正在加载模型。": "加载中", "正在转录。": "转录中" };
    const stage = Object.keys(stages).find((text) => detail.endsWith(text));
    const processed = detail.match(/已处理至 (\d+)分(\d+)秒。$/);
    if (stage) {
      label = stages[stage];
      explanation = detail.slice(0, -stage.length).trim();
    } else if (processed) {
      progress = `已处理 ${processed[1].padStart(2, "0")}:${processed[2].padStart(2, "0")}`;
      explanation = detail.slice(0, processed.index).trim();
    }
  }
  if (explanation.replace(/[。.!！\s]+$/u, "") === label) explanation = "";
  return { label, progress, hasDetails: Boolean(explanation || error),
    text: [...new Set([detail, error].filter(Boolean))].join("\n\n") || label };
}

function updateTaskDialog(record, presentation) {
  taskDialogName.textContent = record.name;
  taskDialogContent.textContent = presentation.text;
}

taskDialog.addEventListener("close", () => {
  const row = rows.get(detailRecordId)?.row;
  detailRecordId = null;
  row?.querySelector("button")?.focus({ preventScroll: true });
});

function api() {
  if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
  return window.pywebview.api;
}

function enqueue(work) {
  const request = requestQueue.then(work);
  requestQueue = request.catch(() => {});
  return request;
}

function showError(error) {
  feedback.hidden = false;
  message.textContent = String(error?.message ?? error);
}

function makeButton(method, text, record) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `table-action${method === "delete_audio" ? " audio-delete" : ""}${method.startsWith("cancel_") ? " is-secondary" : ""}`;
  button.textContent = text;
  button.dataset.method = method;
  button.setAttribute("aria-label", `${text}：${record.name}`);
  if (method === "delete_audio") button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", () => perform(method, record));
  return button;
}

function render(records) {
  if (!Array.isArray(records)) throw new Error("音频列表格式不正确。");
  list.querySelector(".audio-empty")?.closest("tr").remove();
  const ids = new Set(records.map((record) => record.id));
  for (const [id, entry] of rows) {
    if (!ids.has(id)) {
      if (detailRecordId === id) taskDialog.close();
      entry.row.remove(); rows.delete(id);
    }
  }
  if (!records.length) {
    const cell = list.insertRow().insertCell();
    cell.colSpan = 5;
    cell.className = "audio-empty";
    cell.textContent = "暂无音频";
    return;
  }
  for (const [index, record] of records.entries()) {
    let entry = rows.get(record.id);
    if (!entry) {
      entry = { row: document.createElement("tr"), signature: "" };
      rows.set(record.id, entry);
    }
    const row = entry.row;
    entry.record = record;
    const signature = JSON.stringify(record);
    if (signature !== entry.signature) {
      const focused = row.contains(document.activeElement) ? document.activeElement.dataset.method : null;
      row.replaceChildren();
      const name = row.insertCell();
      name.className = "audio-name";
      const title = document.createElement("strong");
      title.textContent = record.name;
      title.title = record.name;
      name.append(title);
      if (record.platform) {
        const source = document.createElement("small");
        source.className = "audio-source";
        const platform = { apple: "Apple Podcasts", xiaoyuzhou: "小宇宙", bilibili: "哔哩哔哩", youtube: "YouTube" }[record.platform] ?? record.platform;
        source.textContent = [record.podcast_title, platform,
          record.platform === "youtube" ? sourceLabels[record.source_kind] : ""].filter(Boolean).join(" · ");
        source.title = source.textContent;
        name.append(source);
      }
      if (!record.is_podcast_import && !record.available && !(record.transcription_status === "completed" && record.has_result)) {
        const missing = document.createElement("small");
        missing.textContent = "文件已丢失";
        name.append(missing);
      }
      const size = row.insertCell();
      size.className = "audio-size";
      size.textContent = record.source_kind?.endsWith("subtitles") ? "字幕" : record.size_bytes > 0 ? formatBytes(record.size_bytes) : "—";
      const imported = new Date(record.imported_at);
      row.insertCell().textContent = Number.isNaN(imported.getTime()) ? "—" : dateFormat.format(imported);
      const state = row.insertCell();
      const presentation = taskPresentation(record);
      const badge = document.createElement("span");
      badge.className = `audio-status is-${record.transcription_status}`;
      badge.textContent = presentation.label;
      state.append(badge);
      if (presentation.progress) {
        const progress = document.createElement("small");
        progress.className = "audio-task-progress";
        progress.textContent = presentation.progress;
        state.append(progress);
      }
      if (presentation.hasDetails) {
        const detailsButton = document.createElement("button");
        detailsButton.type = "button";
        detailsButton.className = "audio-task-info";
        detailsButton.dataset.method = "task-details";
        detailsButton.textContent = record.transcription_status === "failed" || record.transcription_error ? "查看原因" : "查看说明";
        detailsButton.setAttribute("aria-label", `${detailsButton.textContent}：${record.name}`);
        detailsButton.setAttribute("aria-haspopup", "dialog");
        detailsButton.addEventListener("click", () => {
          detailRecordId = record.id;
          updateTaskDialog(record, presentation);
          taskDialog.showModal();
        });
        state.append(detailsButton);
      }
      if (taskDialog.open && detailRecordId === record.id) updateTaskDialog(record, presentation);
      const operations = row.insertCell();
      operations.className = "audio-operations";
      const actions = document.createElement("div");
      actions.className = "audio-row-actions";
      const status = record.transcription_status;
      if (record.has_result) actions.append(makeButton("get_transcription", "查看结果", record));
      if (!record.is_podcast_import && ["failed", "cancelled"].includes(status) && record.available) {
        actions.append(makeButton("retry_transcription", "重试", record));
      }
      if (record.is_podcast_import && ["failed", "cancelled", "interrupted"].includes(status)) {
        actions.append(makeButton("retry_podcast_import", "重新获取", record));
      }
      if (record.is_podcast_import && ["waiting_fetch", "resolving", "downloading", "importing"].includes(status)) {
        actions.append(makeButton("cancel_podcast_import", "取消", record));
      }
      if (status === "waiting_model") actions.append(makeButton("settings", "前往设置", record));
      if (["waiting_model", "queued", "transcribing"].includes(status)) {
        actions.append(makeButton("cancel_transcription", "取消", record));
      }
      if (!["transcribing", "cancelling", "resolving", "downloading", "importing"].includes(status)) {
        actions.append(makeButton("delete_audio", "删除", record));
      }
      operations.append(actions);
      entry.signature = signature;
      if (focused) row.querySelector(`[data-method="${focused}"]`)?.focus({ preventScroll: true });
    }
    if (list.children[index] !== row) list.insertBefore(row, list.children[index] ?? null);
  }
  list.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
  if (locateId && rows.has(locateId)) {
    const row = rows.get(locateId).row;
    row.scrollIntoView({ block: "center", behavior: "auto" });
    row.tabIndex = -1;
    row.focus({ preventScroll: true });
    row.classList.add("is-located");
    setTimeout(() => row.classList.remove("is-located"), 3000);
    locateId = null;
  }
}

function schedule() {
  clearTimeout(timer);
  if (!panel.hidden && !currentResult) timer = setTimeout(loadLibrary, 1500);
}

async function loadLibrary() {
  if (polling) return;
  polling = true;
  try {
    await enqueue(async () => {
      render(await api().list_audio());
      if (!busy) feedback.hidden = true;
    });
  } catch (error) {
    showError(error);
    const empty = list.querySelector(".audio-empty");
    if (empty?.textContent === "正在读取…") empty.textContent = "音频列表暂不可用";
  } finally {
    polling = false;
    schedule();
  }
}

async function perform(method, record) {
  if (busy || confirmingDeletion) return;
  if (method === "settings") { window.dispatchEvent(new Event("open-model-settings")); return; }
  if (method === "delete_audio") {
    confirmingDeletion = true;
    let confirmed;
    try {
      confirmed = await confirmAction({
        title: "删除记录？",
        message: record.platform ? `将删除“${record.name}”及其下载音频和转录结果。`
          : `将删除“${record.name}”及其转录结果。\n原文件不受影响。`,
        confirmLabel: "删除记录",
        destructive: true,
        getReturnFocus: () => rows.get(record.id)?.row.querySelector('[data-method="delete_audio"]'),
      });
    } finally { confirmingDeletion = false; }
    if (!confirmed || busy) return;
    const current = rows.get(record.id)?.record;
    if (!current) { showToast("这条音频已不在列表中。"); return; }
    if (["transcribing", "cancelling", "resolving", "downloading", "importing"].includes(current.transcription_status)) {
      showToast("音频正在处理，请先取消任务，待取消完成后再删除。");
      return;
    }
    record = current;
  }
  busy = true;
  feedback.hidden = true;
  list.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  const token = resultRequest;
  try {
    await enqueue(async () => {
      const value = await api()[method](record.id);
      if (method === "get_transcription") {
        if (token === resultRequest && !panel.hidden) showResult(value);
      } else {
        const notices = {
          delete_audio: ["已删除记录及转录结果。", "success"],
          retry_transcription: ["已提交重新转录请求。", "info"],
          cancel_transcription: ["已提交取消转录请求。", "info"],
          retry_podcast_import: ["已提交重新获取请求。", "info"],
          cancel_podcast_import: ["已提交取消获取请求。", "info"],
        };
        if (notices[method]) showToast(...notices[method]);
        // A refresh failure must not misreport an already completed action.
        try { render(await api().list_audio()); }
        catch (error) { showError(error); }
      }
    });
  } catch (error) { showToast(String(error?.message ?? error), "error"); }
  finally {
    busy = false;
    list.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    schedule();
  }
}

function timestamp(value) {
  const seconds = Math.max(0, Math.floor(value));
  return `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
}

function showResult(result) {
  stopTranscriptRender();
  currentResult = result;
  const token = resultRequest;
  clearTimeout(timer);
  tableRegion.hidden = true;
  transcript.hidden = false;
  transcriptTitle.textContent = result.name;
  const source = document.querySelector("#transcript-source");
  source.hidden = false;
  source.textContent = [sourceLabels[result.source_kind] ?? "本地转录", result.language].filter(Boolean).join(" · ");
  copyButton.disabled = !result.text;
  optimizeButton.disabled = creatingManuscript || !result.text?.trim();
  transcriptContent.replaceChildren();
  if (!result.segments.length) {
    transcriptContent.textContent = result.text || "未识别到语音。";
  } else {
    let offset = 0;
    transcriptContent.setAttribute("aria-busy", "true");
    function appendBatch() {
      if (token !== resultRequest || currentResult !== result || panel.hidden) return;
      const fragment = document.createDocumentFragment();
      const end = Math.min(offset + TRANSCRIPT_BATCH_SIZE, result.segments.length);
      for (; offset < end; offset += 1) {
        const segment = result.segments[offset];
        const row = document.createElement("p");
        const time = document.createElement("span");
        time.className = "transcript-time";
        time.textContent = timestamp(segment.start);
        const text = document.createElement("span");
        text.textContent = segment.text.trim();
        row.append(time, text);
        fragment.append(row);
      }
      transcriptContent.append(fragment);
      // Yield between batches so a long transcript does not block navigation.
      if (offset < result.segments.length) transcriptRenderTimer = setTimeout(appendBatch, 16);
      else transcriptContent.removeAttribute("aria-busy");
    }
    appendBatch();
  }
  transcriptTitle.focus();
}

export function locateAudio(identifier) {
  locateId = identifier;
}

export function openAudioLibrary() {
  stopTranscriptRender();
  resultRequest += 1;
  currentResult = null;
  transcript.hidden = true;
  tableRegion.hidden = false;
  return loadLibrary();
}

document.querySelector("#transcript-back").addEventListener("click", openAudioLibrary);
optimizeButton.addEventListener("click", async () => {
  if (creatingManuscript || !currentResult?.text?.trim()) return;
  const result = currentResult;
  const token = resultRequest;
  creatingManuscript = true;
  optimizeButton.disabled = true;
  try {
    if (!await confirmAIUsage(() => token === resultRequest && !panel.hidden && currentResult === result)) return;
    if (token !== resultRequest || panel.hidden || currentResult !== result) return;
    optimizeButton.textContent = "正在创建";
    await api().optimize_transcription(result.audio_id, true);
    if (token === resultRequest && !panel.hidden) window.dispatchEvent(new Event("open-manuscripts"));
    showToast("已创建文稿，正在后台优化。", "success");
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  } finally {
    creatingManuscript = false;
    optimizeButton.disabled = !currentResult?.text?.trim();
    optimizeButton.textContent = "优化转录";
  }
});
copyButton.addEventListener("click", async () => {
  if (!currentResult) return;
  const result = currentResult;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(result.text);
    else {
      const field = document.createElement("textarea");
      field.value = result.text;
      field.className = "sr-only";
      document.body.append(field);
      try { field.select(); if (!document.execCommand("copy")) throw new Error("复制失败，请选择文字后复制。"); }
      finally { field.remove(); copyButton.focus(); }
    }
    showToast("已复制全文。", "success");
  } catch (error) { showToast(`复制失败：${String(error?.message ?? error)}`, "error"); }
});
retry.addEventListener("click", loadLibrary);
window.addEventListener("pywebviewready", () => { if (!panel.hidden) loadLibrary(); });
window.addEventListener("audio-library-changed", () => { if (!panel.hidden) loadLibrary(); });
window.addEventListener("view-changed", () => {
  stopTranscriptRender();
  resultRequest += 1;
  schedule();
});

export async function importAudio(file, onProgress, isCancelled) {
  const bridge = api();
  const identifier = await bridge.begin_audio_import(file.name, file.size);
  try {
    const chunkSize = 256 * 1024;
    for (let offset = 0; offset < file.size; offset += chunkSize) {
      if (isCancelled()) throw new DOMException("已取消导入。", "AbortError");
      const buffer = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
      let binary = "";
      for (let start = 0; start < buffer.length; start += 8192) {
        binary += String.fromCharCode(...buffer.subarray(start, start + 8192));
      }
      await bridge.append_audio_chunk(identifier, offset, btoa(binary));
      onProgress(Math.floor(Math.min(offset + buffer.length, file.size) / file.size * 100));
    }
    if (isCancelled()) throw new DOMException("已取消导入。", "AbortError");
    const result = await bridge.finish_audio_import(identifier);
    window.dispatchEvent(new Event("audio-library-changed"));
    return result;
  } catch (error) {
    try { await bridge.abort_audio_import(identifier); }
    catch { /* Pending imports are recovered after the process lock is released. */ }
    throw error;
  }
}

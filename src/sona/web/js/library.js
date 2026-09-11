import { formatBytes } from "./format.js";

const panel = document.querySelector('[data-panel="library"]');
const list = document.querySelector("#audio-list");
const feedback = document.querySelector("#audio-feedback");
const message = document.querySelector("#audio-feedback-message");
const retry = document.querySelector("#audio-retry");
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
let pending = false;
let reloadRequested = false;

function api() {
  if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
  return window.pywebview.api;
}

function render(records) {
  list.replaceChildren();
  if (!records.length) {
    const cell = list.insertRow().insertCell();
    cell.colSpan = 4;
    cell.className = "audio-empty";
    cell.textContent = "暂无音频";
    return;
  }
  for (const record of records) {
    const row = list.insertRow();
    const name = row.insertCell();
    name.className = "audio-name";
    const label = document.createElement("strong");
    label.textContent = record.name;
    name.append(label);
    if (!record.available) {
      const missing = document.createElement("small");
      missing.textContent = "文件已丢失";
      name.append(missing);
    }
    row.insertCell().textContent = formatBytes(record.size_bytes);
    const imported = new Date(record.imported_at);
    row.insertCell().textContent = Number.isNaN(imported.getTime()) ? "—" : dateFormat.format(imported);
    const operations = row.insertCell();
    operations.className = "audio-operations";
    const actions = document.createElement("div");
    actions.className = "audio-row-actions";
    for (const [method, text] of [["open_audio", "打开"], ["delete_audio", "删除"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `secondary-button${method === "delete_audio" ? " audio-delete" : ""}`;
      button.textContent = text;
      button.disabled = method === "open_audio" && !record.available;
      button.setAttribute("aria-label", `${text}：${record.name}`);
      if (method === "open_audio") button.title = "使用系统默认应用打开";
      button.addEventListener("click", () => perform(method, record));
      actions.append(button);
    }
    operations.append(actions);
  }
}

async function perform(method, record) {
  if (pending) return;
  if (method === "delete_audio" && !window.confirm(`删除“${record.name}”？仅删除 Sona 保存的副本，原文件不受影响。`)) return;
  pending = true;
  retry.disabled = true;
  feedback.hidden = true;
  const buttons = [...list.querySelectorAll("button")];
  const wasDisabled = buttons.map((button) => button.disabled);
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const bridge = api();
    if (method) await bridge[method](record.id);
    const records = await bridge.list_audio();
    if (!Array.isArray(records)) throw new Error("音频列表格式不正确。");
    render(records);
  } catch (error) {
    feedback.hidden = false;
    message.textContent = String(error?.message ?? error);
    buttons.forEach((button, index) => { button.disabled = wasDisabled[index]; });
    const initial = list.querySelector(".audio-empty");
    if (initial?.textContent === "正在读取…") initial.textContent = "音频列表暂不可用";
  } finally {
    pending = false;
    retry.disabled = false;
    if (reloadRequested) {
      reloadRequested = false;
      openAudioLibrary();
    }
  }
}

export function openAudioLibrary() {
  if (pending) {
    reloadRequested = true;
    return;
  }
  return perform();
}

retry.addEventListener("click", openAudioLibrary);
window.addEventListener("pywebviewready", () => { if (!panel.hidden) openAudioLibrary(); });
window.addEventListener("audio-library-changed", () => { if (!panel.hidden) openAudioLibrary(); });

export async function importAudio(file, onProgress, isCancelled) {
  const bridge = api();
  const identifier = await bridge.begin_audio_import(file.name, file.size);
  try {
    const chunkSize = 256 * 1024;
    for (let offset = 0; offset < file.size; offset += chunkSize) {
      if (isCancelled()) throw new DOMException("已取消导入。", "AbortError");
      const buffer = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
      // Bound bridge messages instead of encoding the entire audio in memory.
      let binary = "";
      for (let start = 0; start < buffer.length; start += 8192) {
        binary += String.fromCharCode(...buffer.subarray(start, start + 8192));
      }
      await bridge.append_audio_chunk(identifier, offset, btoa(binary));
      onProgress(Math.floor(Math.min(offset + buffer.length, file.size) / file.size * 100));
    }
    if (isCancelled()) throw new DOMException("已取消导入。", "AbortError");
    await bridge.finish_audio_import(identifier);
    window.dispatchEvent(new Event("audio-library-changed"));
  } catch (error) {
    try {
      await bridge.abort_audio_import(identifier);
    } catch {
      // The next process cleans interrupted imports after the OS lock is released.
    }
    throw error;
  }
}

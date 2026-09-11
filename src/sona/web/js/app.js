import { openModelSettings } from "./models.js";
import { formatBytes } from "./format.js";
import { importAudio, openAudioLibrary } from "./library.js";

const navigationItems = document.querySelectorAll("[data-view]");
const panels = document.querySelectorAll("[data-panel]");
const fileInput = document.querySelector("#audio-file");
const dropZone = document.querySelector("#drop-zone");
const selectedFile = document.querySelector("#selected-file");
const uploadTriggers = document.querySelectorAll("[data-upload-trigger]");
const fileFeedback = document.querySelector("#file-feedback");
const clearFileButton = document.querySelector("#clear-file");
let selectedAudioFile = null;
let dragDepth = 0;
let importing = false;
let importCancelled = false;

function showView(viewName) {
  navigationItems.forEach((item) => {
    const isActive = item.dataset.view === viewName;
    item.classList.toggle("is-active", isActive);
    if (isActive) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  panels.forEach((panel) => {
    panel.hidden = panel.dataset.panel !== viewName;
  });
  if (viewName === "settings") openModelSettings();
  if (viewName === "library") openAudioLibrary();
}

function renderSelectedFile() {
  selectedFile.textContent = selectedAudioFile
    ? `已导入：${selectedAudioFile.name}（${formatBytes(selectedAudioFile.size)}）` : "";
  clearFileButton.hidden = !selectedAudioFile;
}

async function selectAudioFile(files) {
  if (importing) return;
  if (!files.length) return; // Cancelling the picker preserves the previous file.
  fileFeedback.textContent = "";
  const file = files[0];
  if (files.length !== 1) {
    fileFeedback.textContent = "请一次选择一个音频文件。";
    return;
  }
  const audioExtension = /\.(mp3|wav|m4a|aac|flac|ogg|opus|aiff?|wma)$/i.test(file.name);
  const genericType = !file.type || file.type === "application/octet-stream";
  if (!file.type.startsWith("audio/") && !(genericType && audioExtension)) {
    fileFeedback.textContent = "请选择音频文件，例如 MP3、WAV 或 M4A。";
    return;
  }
  if (file.size === 0) {
    fileFeedback.textContent = "这个文件是空的，请选择其他音频文件。";
    return;
  }
  importing = true;
  importCancelled = false;
  uploadTriggers.forEach((button) => { button.disabled = true; });
  dropZone.setAttribute("aria-busy", "true");
  clearFileButton.hidden = false;
  clearFileButton.textContent = "取消导入";
  selectedFile.textContent = `正在导入：${file.name}（0%）`;
  try {
    await importAudio(file, (progress) => {
      selectedFile.textContent = `正在导入：${file.name}（${progress}%）`;
    }, () => importCancelled);
    selectedAudioFile = { name: file.name, size: file.size };
  } catch (error) {
    fileFeedback.textContent = error.name === "AbortError" ? "已取消导入。"
      : `导入失败：${String(error?.message ?? error)}`;
  } finally {
    importing = false;
    uploadTriggers.forEach((button) => { button.disabled = false; });
    dropZone.setAttribute("aria-busy", "false");
    clearFileButton.textContent = "清除";
    clearFileButton.disabled = false;
    renderSelectedFile();
  }
}

navigationItems.forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
uploadTriggers.forEach((trigger) => trigger.addEventListener("click", () => {
  if (importing) return;
  showView("upload");
  fileInput.click();
}));
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  showView("upload");
});
fileInput.addEventListener("change", () => {
  selectAudioFile(fileInput.files);
  fileInput.value = "";
});
clearFileButton.addEventListener("click", () => {
  if (importing) {
    importCancelled = true;
    clearFileButton.disabled = true;
    clearFileButton.textContent = "正在取消";
    return;
  }
  selectedAudioFile = null;
  fileInput.value = "";
  fileFeedback.textContent = "";
  renderSelectedFile();
  dropZone.focus();
});
dropZone.addEventListener("dragenter", (event) => {
  event.preventDefault();
  dragDepth += 1;
  dropZone.classList.add("is-dragging");
});
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
dropZone.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropZone.classList.remove("is-dragging");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  dropZone.classList.remove("is-dragging");
  selectAudioFile(event.dataTransfer.files);
});
// Dropping outside the picker must not navigate the webview away from the app.
["dragover", "drop"].forEach((eventName) => window.addEventListener(eventName, (event) => {
  event.preventDefault();
  if (eventName === "drop") {
    dragDepth = 0;
    dropZone.classList.remove("is-dragging");
  }
}));

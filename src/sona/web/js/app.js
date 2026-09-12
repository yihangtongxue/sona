import { openModelSettings } from "./models.js";
import { openAIModelSettings } from "./ai-models.js";
import { openAccelerationSettings } from "./acceleration.js";
import { importAudio, openAudioLibrary, locateAudio } from "./library.js";
import { openManuscripts } from "./manuscripts.js";
import { showToast } from "./toast.js";
import { openAbout } from "./updates.js";
import { initializeImportSources } from "./import-sources.js";

const navigationItems = document.querySelectorAll("[data-view]");
const panels = document.querySelectorAll("[data-panel]");
const fileInput = document.querySelector("#audio-file");
const dropZone = document.querySelector("#drop-zone");
const importProgress = document.querySelector("#import-progress");
const importStatus = document.querySelector("#import-status");
const uploadTriggers = document.querySelectorAll("[data-upload-trigger]");
const cancelImportButton = document.querySelector("#cancel-import");
let dragDepth = 0;
let importing = false;
let importCancelled = false;
initializeImportSources((result) => {
  locateAudio(result.id);
  showView("library");
});

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
  if (viewName === "settings") {
    openModelSettings();
    openAIModelSettings();
    openAccelerationSettings();
    openAbout();
  }
  if (viewName === "library") openAudioLibrary();
  if (viewName === "manuscripts") openManuscripts();
  window.dispatchEvent(new CustomEvent("view-changed", { detail: viewName }));
}

async function selectAudioFile(files) {
  if (importing) return;
  if (!files.length) return;
  const file = files[0];
  if (files.length !== 1) {
    showToast("请一次选择一个音频文件。");
    return;
  }
  const audioExtension = /\.(mp3|wav|m4a|aac|flac|ogg|opus|aiff?|wma)$/i.test(file.name);
  const genericType = !file.type || file.type === "application/octet-stream";
  if (!file.type.startsWith("audio/") && !(genericType && audioExtension)) {
    showToast("请选择音频文件，例如 MP3、WAV 或 M4A。");
    return;
  }
  if (file.size === 0) {
    showToast("这个文件是空的，请选择其他音频文件。", "error");
    return;
  }
  importing = true;
  importCancelled = false;
  uploadTriggers.forEach((button) => { button.disabled = true; });
  dropZone.setAttribute("aria-busy", "true");
  importProgress.hidden = false;
  importStatus.textContent = `正在导入：${file.name}（0%）`;
  try {
    await importAudio(file, (progress) => {
      importStatus.textContent = `正在导入：${file.name}（${progress}%）`;
    }, () => importCancelled);
    showView("library");
    showToast(`已导入“${file.name}”。`, "success");
  } catch (error) {
    if (error?.name === "AbortError") showToast("已取消导入。");
    else showToast(`导入失败：${String(error?.message ?? error)}`, "error");
  } finally {
    importing = false;
    uploadTriggers.forEach((button) => { button.disabled = false; });
    dropZone.setAttribute("aria-busy", "false");
    const returnFocus = document.activeElement === cancelImportButton;
    importProgress.hidden = true;
    importStatus.textContent = "";
    cancelImportButton.textContent = "取消导入";
    cancelImportButton.disabled = false;
    fileInput.value = "";
    if (returnFocus && !dropZone.closest('[data-panel="upload"]').hidden) dropZone.focus();
  }
}

navigationItems.forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
window.addEventListener("open-model-settings", () => showView("settings"));
window.addEventListener("open-manuscripts", () => showView("manuscripts"));
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
cancelImportButton.addEventListener("click", () => {
  if (!importing || importCancelled) return;
  importCancelled = true;
  cancelImportButton.disabled = true;
  cancelImportButton.textContent = "正在取消";
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

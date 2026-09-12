import { showToast } from "./toast.js";
import { confirmAction } from "./dialog.js";
import { formatBytes } from "./format.js";

const panel = document.querySelector('[data-panel="settings"]');
const version = document.querySelector("#app-version");
const message = document.querySelector("#update-message");
const progress = document.querySelector("#update-progress");
const notes = document.querySelector("#update-notes");
const notesContent = document.querySelector("#update-notes-content");
const check = document.querySelector("#check-update");
const download = document.querySelector("#download-update");
const install = document.querySelector("#install-update");
const cancel = document.querySelector("#cancel-update");
const exportLogs = document.querySelector("#export-diagnostics");
let latest;
let pending = false;
let polling = false;
let timer;
let notifiedVersion = "";
let revision = 0;

function api() {
  if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
  return window.pywebview.api;
}

function render(state) {
  latest = state;
  version.textContent = state.version;
  const busy = ["checking", "downloading", "verifying", "preparing", "restarting"].includes(state.state);
  check.disabled = pending || busy;
  check.textContent = state.state === "checking" ? "正在检查" : "检查更新";
  download.hidden = !(state.latest_version && state.total && ["available", "error"].includes(state.state) && state.can_install);
  install.hidden = state.state !== "ready";
  cancel.hidden = !["downloading", "verifying"].includes(state.state);
  download.disabled = install.disabled = cancel.disabled = pending;
  const stages = { checking: "正在检查更新…", downloading: "正在下载更新…", verifying: "正在校验更新包…",
    preparing: "正在准备安装，请稍候…", restarting: "正在重启更新…" };
  let text = state.message || stages[state.state] || "";
  if (state.state === "available" && !state.can_install) text += ` ${state.install_hint}`;
  if (state.state === "downloading" && state.total) {
    text = `${text} ${formatBytes(state.received)} / ${formatBytes(state.total)}`;
  }
  message.hidden = !text;
  message.textContent = text;
  message.classList.toggle("is-error", state.state === "error");
  progress.hidden = state.state !== "downloading";
  progress.max = state.total || 1;
  progress.value = state.received || 0;
  notes.hidden = !state.notes;
  notesContent.textContent = state.notes || "";
  if (state.state === "available" && state.latest_version !== notifiedVersion) {
    notifiedVersion = state.latest_version;
    showToast(`发现 Sona ${state.latest_version}，可在设置中更新。`);
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(load, panel.hidden ? 15000 : 1000);
}

async function load() {
  if (pending || polling) { schedule(); return; }
  polling = true;
  const token = revision;
  try {
    const state = await api().update_status();
    if (token === revision) render(state);
  } catch (error) {
    if (!panel.hidden) {
      message.hidden = false;
      message.textContent = String(error?.message ?? error);
    }
  } finally { polling = false; schedule(); }
}

async function perform(method) {
  if (pending) return;
  pending = true;
  revision += 1;
  if (latest) render(latest);
  try {
    if (method === "install_update") {
      if (!await confirmAction({ title: "重启更新？", message: "将关闭 Sona 并安装新版本，文稿和模型数据会保留。",
                                confirmLabel: "重启更新" })) return;
    }
    render(await api()[method]());
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  } finally {
    pending = false;
    if (latest) render(latest);
    load();
  }
}

check.addEventListener("click", () => perform("check_update"));
download.addEventListener("click", () => perform("download_update"));
install.addEventListener("click", () => perform("install_update"));
cancel.addEventListener("click", () => perform("cancel_update_download"));
export function openAbout() { load(); }
window.addEventListener("pywebviewready", load);
exportLogs.addEventListener("click", async () => {
  if (exportLogs.disabled) return;
  exportLogs.disabled = true;
  try {
    if (await api().export_diagnostics()) showToast("日志已保存，可在反馈问题时提供。", "success");
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  } finally { exportLogs.disabled = false; }
});

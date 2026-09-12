import { showToast } from "./toast.js";

// Add a source here to reuse the card, input dialog, and submission flow.
// The Python importer remains responsible for validating and resolving links.
const sources = [
  {
    id: "xiaoyuzhou",
    name: "小宇宙",
    description: "把想听的一期播客转成文字。",
    help: "打开想转录的那一期播客，点击分享，复制链接后粘贴到这里。",
    placeholder: "粘贴小宇宙链接",
    hosts: ["www.xiaoyuzhoufm.com", "xiaoyuzhoufm.com"],
    icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6.5" /><ellipse cx="12" cy="12" rx="11" ry="3.5" transform="rotate(-30 12 12)" /><path d="M18 3h.01" /></svg>',
  },
  {
    id: "apple",
    name: "Apple Podcasts",
    description: "把想听的一期播客转成文字。",
    help: "打开想转录的那一期播客，点击分享，复制链接后粘贴到这里。",
    placeholder: "粘贴 Apple Podcasts 链接",
    hosts: ["podcasts.apple.com"],
    icon: '<svg viewBox="0 0 24 24"><circle cx="12" cy="10" r="2" /><path d="M9.5 15a2.5 2.5 0 0 1 5 0l-.7 5h-3.6zM6 17a8 8 0 1 1 12 0M8 13a4.5 4.5 0 1 1 8 0" /></svg>',
  },
];

export function initializeImportSources(onImported) {
  const grid = document.querySelector("#import-sources");
  const template = document.querySelector("#podcast-source-template");
  const dialog = document.querySelector("#podcast-dialog");
  const form = document.querySelector("#podcast-form");
  const input = document.querySelector("#podcast-url");
  const submit = document.querySelector("#podcast-submit");
  const errorMessage = document.querySelector("#podcast-error");
  const close = document.querySelector("#podcast-close");
  const cancel = document.querySelector("#podcast-cancel");
  const drafts = new Map();
  let activeSource = null;
  let opener = null;
  let submitting = false;

  function clearError() {
    errorMessage.hidden = true;
    errorMessage.textContent = "";
    input.removeAttribute("aria-invalid");
    input.setCustomValidity("");
  }

  function closeDialog() {
    if (!submitting) {
      if (activeSource) drafts.set(activeSource.id, input.value);
      dialog.close();
    }
  }

  for (const source of sources) {
    const card = template.content.firstElementChild.cloneNode(true);
    card.dataset.source = source.id;
    const icon = card.querySelector("[data-source-icon]");
    icon.classList.add(`source-icon-${source.id}`);
    icon.innerHTML = source.icon; // Static application-owned SVG, never remote content.
    card.querySelector("[data-source-name]").textContent = source.name;
    card.querySelector("[data-source-description]").textContent = source.description;
    card.addEventListener("click", () => {
      if (submitting) return;
      activeSource = source;
      opener = card;
      clearError();
      document.querySelector("#podcast-title").textContent = `从 ${source.name} 导入`;
      document.querySelector("#podcast-help").textContent = source.help;
      const dialogIcon = document.querySelector("#podcast-dialog-icon");
      dialogIcon.className = `source-icon source-icon-${source.id}`;
      dialogIcon.innerHTML = source.icon;
      input.placeholder = source.placeholder;
      input.value = drafts.get(source.id) || "";
      dialog.showModal();
      input.focus();
    });
    grid.append(card);
  }

  input.addEventListener("input", clearError);
  close.addEventListener("click", closeDialog);
  cancel.addEventListener("click", closeDialog);
  dialog.addEventListener("cancel", (event) => {
    if (submitting) event.preventDefault();
    else if (activeSource) drafts.set(activeSource.id, input.value);
  });
  dialog.addEventListener("close", () => {
    if (opener && !opener.closest('[data-panel="upload"]').hidden) opener.focus();
  });
  // A local file may finish importing while this dialog is open.
  window.addEventListener("view-changed", (event) => {
    if (event.detail !== "upload" && dialog.open) {
      if (activeSource) drafts.set(activeSource.id, input.value);
      dialog.close();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || !activeSource) return;
    clearError();
    try {
      const url = new URL(input.value.trim());
      if (!activeSource.hosts.includes(url.hostname.toLowerCase())) {
        throw new Error(`请粘贴 ${activeSource.name} 的单集链接，或返回选择对应的音频来源。`);
      }
    } catch (error) {
      errorMessage.textContent = error instanceof TypeError ? "请粘贴完整的播客单集链接。" : error.message;
      errorMessage.hidden = false;
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    submitting = true;
    submit.disabled = input.disabled = close.disabled = cancel.disabled = true;
    submit.textContent = "正在提交…";
    form.setAttribute("aria-busy", "true");
    try {
      if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
      const result = await window.pywebview.api.import_podcast(input.value.trim());
      input.value = "";
      drafts.delete(activeSource.id);
      dialog.close();
      onImported(result);
      showToast(result.existing ? "该单集已有记录，已为你定位。" : "已添加播客，正在后台获取并转录。",
        result.existing ? "info" : "success");
    } catch (error) {
      errorMessage.textContent = String(error?.message ?? error);
      errorMessage.hidden = false;
      input.setAttribute("aria-invalid", "true");
      if (!dialog.open) showToast(errorMessage.textContent, "error");
    } finally {
      submitting = false;
      submit.disabled = input.disabled = close.disabled = cancel.disabled = false;
      submit.textContent = "获取并转录";
      form.removeAttribute("aria-busy");
      if (!errorMessage.hidden && dialog.open) input.focus();
    }
  });
}

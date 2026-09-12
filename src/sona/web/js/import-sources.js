import { showToast } from "./toast.js";
import { createDropdown } from "./dropdown.js";

// Add a source here to reuse the card, input dialog, and submission flow.
// The Python importer remains responsible for validating and resolving links.
const sources = [
  {
    id: "youtube",
    name: "YouTube",
    description: "优先获取字幕，没有字幕时本地转录。",
    help: "粘贴公开视频或 Shorts 链接。有可用字幕时无需安装语音模型；没有可用字幕时下载音轨并转录。暂不支持频道、播放列表、直播或需要登录的内容。",
    placeholder: "粘贴 YouTube 视频链接",
    hosts: ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"],
    icon: '<svg viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="4" /><path d="m10 9 5 3-5 3z" /></svg>',
  },
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
  {
    id: "bilibili",
    name: "哔哩哔哩",
    description: "把视频里的声音转成文字。",
    help: "粘贴公开 BV/AV 视频链接、b23.tv 短链接或分享文案。默认获取第一P；链接带 p 参数时只获取对应分P。暂不支持登录、充电内容、番剧、合集或直播。",
    placeholder: "粘贴 B站视频链接或分享文案",
    hosts: ["www.bilibili.com", "bilibili.com", "m.bilibili.com", "b23.tv"],
    icon: '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="14" rx="3" /><path d="m7 2 3 4m7-4-3 4M7 11v3m10-3v3m-7 2 2 1 2-1" /></svg>',
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
  const youtubeOptions = document.querySelector("#youtube-options");
  const strategy = document.querySelector("#youtube-strategy");
  const language = document.querySelector("#youtube-language");
  createDropdown(document.querySelector("#youtube-strategy-select"), {
    value: "subtitle_first",
    onChange: (value) => { document.querySelector("#youtube-language-field").hidden = value === "transcribe"; },
  });
  createDropdown(document.querySelector("#youtube-language-select"), { value: "original", onChange: () => {} });
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
      document.querySelector('label[for="podcast-url"]').textContent = ["bilibili", "youtube"].includes(source.id) ? "视频链接或分享文案" : "播客链接";
      youtubeOptions.hidden = source.id !== "youtube";
      submit.textContent = source.id === "youtube" ? "获取文字" : "获取并转录";
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
      let value = input.value.trim();
      if (["bilibili", "youtube"].includes(activeSource.id) && !/^https?:\/\//.test(value)) {
        const links = value.match(/https?:\/\/[^\s<>"\u3000]+/g) ?? [];
        if (links.length === 1) value = links[0].replace(/[。！？，；：、）】》」』)\]]+$/u, "");
      }
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol)) throw new TypeError();
      if (!activeSource.hosts.includes(url.hostname.toLowerCase())) {
        throw new Error(`请粘贴 ${activeSource.name} 的链接，或返回选择对应的音频来源。`);
      }
    } catch (error) {
      errorMessage.textContent = error instanceof TypeError ? "请粘贴完整链接，分享文案中请只保留一个链接。" : error.message;
      errorMessage.hidden = false;
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    submitting = true;
    submit.disabled = input.disabled = close.disabled = cancel.disabled = true;
    strategy.disabled = language.disabled = true;
    submit.textContent = "正在提交…";
    form.setAttribute("aria-busy", "true");
    try {
      if (!window.pywebview?.api) throw new Error("桌面连接尚未就绪，请稍后重试。");
      const result = await window.pywebview.api.import_podcast(input.value.trim(),
        activeSource.id === "youtube" ? strategy.value : "subtitle_first", language.value);
      input.value = "";
      drafts.delete(activeSource.id);
      dialog.close();
      onImported(result);
      showToast(result.existing ? "该内容已有记录，已为你定位。" : "已添加任务，正在后台获取文字。",
        result.existing ? "info" : "success");
    } catch (error) {
      errorMessage.textContent = String(error?.message ?? error);
      errorMessage.hidden = false;
      input.setAttribute("aria-invalid", "true");
      if (!dialog.open) showToast(errorMessage.textContent, "error");
    } finally {
      submitting = false;
      submit.disabled = input.disabled = close.disabled = cancel.disabled = false;
      strategy.disabled = language.disabled = false;
      submit.textContent = activeSource.id === "youtube" ? "获取文字" : "获取并转录";
      form.removeAttribute("aria-busy");
      if (!errorMessage.hidden && dialog.open) input.focus();
    }
  });
}

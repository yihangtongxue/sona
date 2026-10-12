import { showToast } from "./toast.js";

const form = document.querySelector("#library-search-form");
const input = document.querySelector("#library-search-input");
const clear = document.querySelector("#library-search-clear");
const results = document.querySelector("#search-results");
const kinds = { transcription: "转录", subtitle: "字幕", manuscript: "文稿" };
let request = 0;

function reset() {
  request += 1;
  results.replaceChildren();
  results.hidden = true;
  clear.hidden = true;
}

function render(items) {
  results.replaceChildren();
  results.hidden = false;
  clear.hidden = false;
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "search-empty";
    empty.textContent = "没有找到匹配的内容。";
    results.append(empty);
    return;
  }
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-result";
    const title = document.createElement("strong");
    title.textContent = `${kinds[item.kind] ?? ""} · ${item.title || "未命名"}`;
    const snippet = document.createElement("span");
    snippet.textContent = item.snippet;
    button.append(title, snippet);
    button.addEventListener("click", () => {
      window.dispatchEvent(new CustomEvent("sona:open-result", { detail: item }));
    });
    results.append(button);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query) { reset(); return; }
  const token = ++request;
  try {
    const items = await window.pywebview.api.search(query);
    if (token === request) render(items);
  } catch (error) {
    showToast(String(error?.message ?? error), "error");
  }
});
clear.addEventListener("click", () => { input.value = ""; reset(); input.focus(); });
input.addEventListener("input", () => { if (!input.value.trim()) reset(); });

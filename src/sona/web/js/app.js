const navigationItems = document.querySelectorAll("[data-view]");
const panels = document.querySelectorAll("[data-panel]");
const fileInput = document.querySelector("#audio-file");
const dropZone = document.querySelector("#drop-zone");
const selectedFile = document.querySelector("#selected-file");
const uploadTriggers = document.querySelectorAll("[data-upload-trigger]");

function showView(viewName) {
  navigationItems.forEach((item) => {
    const isActive = item.dataset.view === viewName;
    item.classList.toggle("is-active", isActive);
    item.toggleAttribute("aria-current", isActive);
  });
  panels.forEach((panel) => {
    const isActive = panel.dataset.panel === viewName;
    panel.hidden = !isActive;
  });
}

function displaySelectedFile(file) {
  selectedFile.textContent = file ? `已选择：${file.name}` : "";
}

navigationItems.forEach((item) => item.addEventListener("click", () => showView(item.dataset.view)));
uploadTriggers.forEach((trigger) => trigger.addEventListener("click", () => {
  showView("upload");
  fileInput.click();
}));
fileInput.addEventListener("change", () => displaySelectedFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropZone.classList.add("is-dragging");
}));
["dragleave", "drop"].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropZone.classList.remove("is-dragging");
}));
dropZone.addEventListener("drop", (event) => displaySelectedFile(event.dataTransfer.files[0]));

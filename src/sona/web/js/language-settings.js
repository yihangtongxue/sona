import { createDropdown } from "./dropdown.js";

const picker = document.querySelector("#language-picker");
const feedback = document.querySelector("#language-feedback");
const dropdown = createDropdown(document.querySelector("#language-select"), {
  value: "auto",
  onChange: () => picker.dispatchEvent(new Event("change", { bubbles: true })),
});
let ready = false;
let pending = false;
let saved = "auto";

function showError(error) {
  feedback.textContent = String(error?.message ?? error);
  feedback.hidden = false;
}

export async function openLanguageSettings() {
  if (ready || pending || !window.pywebview?.api) return;
  pending = true;
  picker.disabled = true;
  feedback.hidden = true;
  try {
    saved = (await window.pywebview.api.get_transcription_language()).language;
    dropdown.setValue(saved);
    ready = true;
  } catch (error) {
    showError(error);
  } finally {
    pending = false;
    picker.disabled = !ready;
  }
}

picker.addEventListener("change", async () => {
  if (!ready || pending) return;
  pending = true;
  picker.disabled = true;
  feedback.hidden = true;
  try {
    saved = await window.pywebview.api.set_transcription_language(picker.value);
  } catch (error) {
    showError(error);
  } finally {
    dropdown.setValue(saved);
    pending = false;
    picker.disabled = false;
  }
});
window.addEventListener("pywebviewready", openLanguageSettings);
if (window.pywebview?.api) openLanguageSettings();

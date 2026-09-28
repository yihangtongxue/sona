import { createDropdown } from "./dropdown.js";

const group = document.querySelector("#window-settings");
const picker = document.querySelector("#window-close-action");
const feedback = document.querySelector("#window-settings-feedback");
const retry = document.querySelector("#window-settings-retry");
const dropdown = createDropdown(document.querySelector("#window-close-select"), {
  value: "tray",
  onChange: () => picker.dispatchEvent(new Event("change", { bubbles: true })),
});
let ready = false;
let pending = false;
let savedAction = "tray";

function showError(error) {
  feedback.textContent = String(error?.message ?? error);
  feedback.hidden = false;
}

export async function openWindowSettings() {
  if (ready || pending || !window.pywebview?.api) return;
  pending = true;
  picker.disabled = true;
  feedback.hidden = true;
  retry.hidden = true;
  try {
    const settings = await window.pywebview.api.get_window_settings();
    group.hidden = !settings.supported;
    savedAction = settings.close_action;
    dropdown.setValue(savedAction);
    ready = true;
  } catch (error) {
    // A failed bridge call must still have a visible retry entry.
    group.hidden = false;
    showError(error);
    retry.hidden = false;
  } finally {
    pending = false;
    picker.disabled = !ready;
  }
}

picker.addEventListener("change", async () => {
  if (!ready || pending) return;
  const action = picker.value;
  const hadFocus = document.activeElement === picker;
  pending = true;
  picker.disabled = true;
  feedback.hidden = true;
  try {
    savedAction = await window.pywebview.api.set_close_action(action);
  } catch (error) {
    showError(error);
  } finally {
    dropdown.setValue(savedAction);
    pending = false;
    picker.disabled = false;
    if (hadFocus && document.activeElement === document.body && !picker.closest("[data-panel]").hidden) {
      picker.focus({ preventScroll: true });
    }
  }
});
retry.addEventListener("click", openWindowSettings);
window.addEventListener("pywebviewready", openWindowSettings);
if (window.pywebview?.api) openWindowSettings();

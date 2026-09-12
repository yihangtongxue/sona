import { createDropdown } from "./dropdown.js";

const picker = document.querySelector("#appearance-theme");
const feedback = document.querySelector("#appearance-feedback");
const retry = document.querySelector("#appearance-retry");
const theme = window.sonaTheme;
const dropdown = createDropdown(document.querySelector("#appearance-select"), {
  value: theme.preference,
  onChange: () => picker.dispatchEvent(new Event("change", { bubbles: true })),
});
let ready = false;
let pending = false;

function apply(preference) {
  theme.apply(preference);
  dropdown.setValue(theme.preference);
  // This cache only improves page reloads. The Python setting is authoritative.
  try { localStorage.setItem("sona.theme", theme.preference); } catch { /* Optional cache. */ }
}

function showError(error) {
  feedback.textContent = String(error?.message ?? error);
  feedback.hidden = false;
}

export async function openAppearanceSettings() {
  if (ready || pending) return;
  if (!window.pywebview?.api) return;
  pending = true;
  picker.disabled = true;
  retry.hidden = true;
  feedback.hidden = true;
  try {
    apply(await window.pywebview.api.get_theme());
    ready = true;
  } catch (error) {
    showError(error);
    retry.hidden = false;
  } finally {
    pending = false;
    picker.disabled = !ready;
  }
}

picker.addEventListener("change", async () => {
  if (!ready || pending) return;
  const previous = theme.preference;
  const preference = picker.value;
  const hadFocus = document.activeElement === picker;
  pending = true;
  picker.disabled = true;
  feedback.hidden = true;
  theme.apply(preference);
  try {
    apply(await window.pywebview.api.set_theme(preference));
  } catch (error) {
    apply(previous);
    showError(error);
  } finally {
    pending = false;
    picker.disabled = false;
    // Disabling the trigger while saving can move focus to the body.
    // Restore keyboard navigation without stealing focus from another control.
    if (hadFocus && document.activeElement === document.body && !picker.closest("[data-panel]").hidden) {
      picker.focus({ preventScroll: true });
    }
  }
});
retry.addEventListener("click", openAppearanceSettings);
window.addEventListener("pywebviewready", openAppearanceSettings);
if (window.pywebview?.api) openAppearanceSettings();

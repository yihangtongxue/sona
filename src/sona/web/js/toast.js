const region = document.querySelector("#toast-region");
const notifications = new Map();
const variants = {
  success: { icon: "✓", duration: 3000 },
  info: { icon: "i", duration: 4000 },
  error: { icon: "!", duration: 6000 },
};

export function showToast(message, type = "info") {
  const text = String(message ?? "").trim();
  if (!text) return;
  if (!Object.hasOwn(variants, type)) type = "info";
  const variant = variants[type];
  const key = `${type}:${text}`;
  const existing = notifications.get(key);
  if (existing) { existing.restart(); return; }

  // Bound the stack without removing a notification being used by keyboard.
  if (notifications.size >= 3) {
    const oldest = [...notifications.values()].find((entry) => !entry.element.contains(document.activeElement));
    oldest?.dismiss();
  }

  const previousFocus = document.activeElement;
  const element = document.createElement("div");
  element.className = `toast toast--${type}`;
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = variant.icon;
  const content = document.createElement("p");
  content.className = "toast-message";
  content.setAttribute("role", type === "error" ? "alert" : "status");
  content.setAttribute("aria-atomic", "true");
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "关闭提示");
  close.textContent = "×";
  element.append(icon, content, close);

  let timer;
  let startedAt;
  let remaining = variant.duration;
  let hovered = false;
  function pause() {
    if (timer === undefined) return;
    clearTimeout(timer);
    timer = undefined;
    remaining = Math.max(0, remaining - (performance.now() - startedAt));
  }
  function resume() {
    if (!element.isConnected || hovered || element.contains(document.activeElement) || timer !== undefined) return;
    startedAt = performance.now();
    timer = setTimeout(dismiss, remaining);
  }
  function restart() {
    pause();
    remaining = variant.duration;
    resume();
  }
  function dismiss() {
    clearTimeout(timer);
    const restoreFocus = element.contains(document.activeElement);
    element.remove();
    notifications.delete(key);
    if (restoreFocus && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  }
  close.addEventListener("click", dismiss);
  element.addEventListener("pointerenter", () => { hovered = true; pause(); });
  element.addEventListener("pointerleave", () => { hovered = false; resume(); });
  element.addEventListener("focusin", pause);
  element.addEventListener("focusout", (event) => {
    if (!element.contains(event.relatedTarget)) queueMicrotask(resume);
  });
  notifications.set(key, { element, restart, dismiss });
  region.append(element);
  requestAnimationFrame(() => { if (element.isConnected) content.textContent = text; });
  resume();
}

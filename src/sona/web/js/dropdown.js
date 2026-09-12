// Shared, select-only combobox. Selection is committed by click/Enter/Space;
// browsing with arrows and cancelling never changes the underlying setting.
let closeCurrent = null;

export function createDropdown(root, { value, onChange }) {
  const trigger = root.querySelector("[data-select-trigger]");
  const label = root.querySelector("[data-select-value]");
  const menu = root.querySelector("[data-select-menu]");
  const options = [...menu.querySelectorAll("[data-select-option]")];
  let opened = false;
  let activeIndex = 0;
  let search = "";
  let searchTime = 0;
  let popover = typeof menu.showPopover === "function";
  if (popover) menu.setAttribute("popover", "manual");

  function setValue(next) {
    const selected = options.find((option) => option.dataset.selectOption === next);
    if (!selected) return;
    trigger.value = next;
    label.textContent = selected.textContent.trim();
    options.forEach((option) => {
      option.setAttribute("aria-selected", String(option === selected));
    });
  }

  function close() {
    if (!opened) return;
    opened = false;
    if (popover && menu.matches(":popover-open")) menu.hidePopover();
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
    options.forEach((option) => option.classList.remove("is-active"));
    search = "";
    if (closeCurrent === close) closeCurrent = null;
  }

  function highlight(index) {
    activeIndex = index;
    options.forEach((option, position) => option.classList.toggle("is-active", position === index));
    const active = options[index];
    trigger.setAttribute("aria-activedescendant", active.id);
    // Scroll only the list, never its page or enclosing dialog.
    const rect = active.getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    if (rect.top < bounds.top + 5) menu.scrollTop -= bounds.top + 5 - rect.top;
    else if (rect.bottom > bounds.bottom - 5) menu.scrollTop += rect.bottom - bounds.bottom + 5;
  }

  function open() {
    if (opened || trigger.disabled || !options.length) return;
    closeCurrent?.();
    opened = true;
    closeCurrent = close;
    menu.hidden = false;
    if (popover) {
      try { menu.showPopover(); }
      catch {
        // Older embedded engines can fall back to a fixed-position CSS menu.
        menu.removeAttribute("popover");
        popover = false;
      }
    }
    const rect = trigger.getBoundingClientRect();
    const margin = 8;
    const gap = 5;
    const width = Math.min(rect.width, window.innerWidth - margin * 2);
    menu.style.width = `${width}px`;
    menu.style.maxHeight = "240px";
    const wanted = Math.min(menu.scrollHeight + 2, 240);
    const below = window.innerHeight - rect.bottom - margin - gap;
    const above = rect.top - margin - gap;
    const upwards = below < wanted && above > below;
    menu.style.maxHeight = `${Math.max(0, Math.min(240, upwards ? above : below))}px`;
    menu.style.left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))}px`;
    menu.style.top = `${upwards ? rect.top - gap - menu.getBoundingClientRect().height : rect.bottom + gap}px`;
    trigger.setAttribute("aria-expanded", "true");
    highlight(Math.max(0, options.findIndex((option) => option.dataset.selectOption === trigger.value)));
  }

  function choose(index) {
    const option = options[index];
    if (!option || trigger.disabled || option.disabled) return;
    const previous = trigger.value;
    const next = option.dataset.selectOption;
    setValue(next);
    close();
    trigger.focus({ preventScroll: true });
    if (previous !== next) onChange(next);
  }

  trigger.addEventListener("click", () => opened ? close() : open());
  trigger.addEventListener("keydown", (event) => {
    if (trigger.disabled || event.isComposing) return;
    const key = event.key;
    if (key === "Escape" && opened) {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (key === "Tab") close();
    else if (key === "Enter" || key === " ") {
      event.preventDefault();
      if (opened) choose(activeIndex);
      else open();
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(key)) {
      event.preventDefault();
      const wasOpen = opened;
      open();
      if (key === "Home") highlight(0);
      else if (key === "End") highlight(options.length - 1);
      else if (wasOpen) highlight((activeIndex + (key === "ArrowDown" ? 1 : -1) + options.length) % options.length);
    } else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const now = performance.now();
      search = now - searchTime > 700 ? key.toLowerCase() : search + key.toLowerCase();
      searchTime = now;
      const query = [...search].every((letter) => letter === search[0]) ? search[0] : search;
      const start = query.length === 1 ? activeIndex + 1 : activeIndex;
      const index = options.findIndex((_, offset) => {
        const candidate = options[(start + offset) % options.length];
        return candidate.textContent.trim().toLowerCase().startsWith(query);
      });
      if (index >= 0) {
        event.preventDefault();
        open();
        highlight((start + index) % options.length);
      }
    }
  });
  options.forEach((option, index) => {
    // The combobox owns focus; options are navigated via aria-activedescendant.
    option.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse") event.preventDefault();
    });
    option.addEventListener("pointermove", () => { if (opened && activeIndex !== index) highlight(index); });
    option.addEventListener("click", () => choose(index));
  });
  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });
  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) close();
  });
  document.addEventListener("scroll", (event) => {
    if (!menu.contains(event.target)) close();
  }, true);
  window.addEventListener("resize", close);
  window.addEventListener("blur", close);
  window.addEventListener("view-changed", close);
  const dialog = root.closest("dialog");
  dialog?.addEventListener("close", close);
  dialog?.addEventListener("cancel", (event) => {
    if (opened) { event.preventDefault(); close(); }
  });
  new MutationObserver(() => { if (trigger.disabled) close(); })
    .observe(trigger, { attributes: true, attributeFilter: ["disabled"] });
  setValue(value);
  return { setValue, close };
}

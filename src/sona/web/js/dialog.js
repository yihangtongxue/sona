const dialog = document.querySelector("#confirm-dialog");
const title = document.querySelector("#confirm-dialog-title");
const message = document.querySelector("#confirm-dialog-message");
const confirmButton = document.querySelector("#confirm-dialog-accept");
let pending = null;

export function confirmAction({ title: heading, message: description, confirmLabel = "确认", destructive = false, getReturnFocus }) {
  // A second request must never replace the action the user is reviewing.
  if (pending) return Promise.resolve(false);
  const previousFocus = document.activeElement;
  title.textContent = heading;
  message.textContent = description;
  confirmButton.textContent = confirmLabel;
  confirmButton.classList.toggle("is-destructive", destructive);
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    pending = { resolve, previousFocus, getReturnFocus };
    dialog.showModal();
  });
}

dialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  dialog.close("cancel");
});

dialog.addEventListener("close", () => {
  const request = pending;
  pending = null;
  if (!request) return;
  // Polling can replace the original row while its confirmation is open.
  const target = request.getReturnFocus?.() ?? request.previousFocus;
  if (target?.isConnected) target.focus({ preventScroll: true });
  request.resolve(dialog.returnValue === "confirm");
});

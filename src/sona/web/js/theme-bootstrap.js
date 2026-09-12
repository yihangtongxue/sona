// Run before the stylesheet and application modules to choose the initial palette.
(() => {
  const choices = ["system", "light", "dark"];
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let initial = window.__sonaThemePreference;
  if (!choices.includes(initial)) {
    try { initial = localStorage.getItem("sona.theme"); } catch { /* Storage is optional. */ }
  }
  const state = {
    preference: choices.includes(initial) ? initial : "system",
    apply(preference) {
      state.preference = choices.includes(preference) ? preference : "system";
      document.documentElement.dataset.theme = state.preference === "system"
        ? (system.matches ? "dark" : "light") : state.preference;
    },
  };
  window.sonaTheme = state;
  state.apply(state.preference);
  system.addEventListener("change", () => {
    if (state.preference === "system") state.apply("system");
  });
})();

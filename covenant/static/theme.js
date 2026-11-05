"use strict";
(function () {
  const colors = {covenant: "#10191c", midnight: "#101526", ember: "#211a17", graphite: "#17181b", plum: "#1d1622",
    light: "#f4f7f5", paper: "#f7f3ec", sky: "#f3f6fb"};
  function apply(name, save = true) {
    if (!Object.hasOwn(colors, name)) name = "covenant";
    document.documentElement.dataset.theme = name;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", colors[name]);
    if (save) {try {localStorage.setItem("covenant-theme", name);} catch {}}
    return name;
  }
  let saved = "covenant";
  try {saved = localStorage.getItem("covenant-theme") || saved;} catch {}
  apply(saved, false);
  window.CovenantTheme = {apply};
})();

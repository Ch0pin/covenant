"use strict";
(() => {
  const button = document.querySelector("#workspace-settings-toggle");
  const menu = document.querySelector("#workspace-settings-menu");
  let expanded = false;
  try {expanded = localStorage.getItem("covenant-settings-expanded") === "true";} catch {}
  function paint() {
    menu.hidden = !expanded;
    button.setAttribute("aria-expanded", String(expanded));
    button.title = `${expanded ? "Hide" : "Show"} workspace settings`;
  }
  button.onclick = () => {
    expanded = !expanded; paint();
    try {localStorage.setItem("covenant-settings-expanded", String(expanded));} catch {}
  };
  paint();
})();

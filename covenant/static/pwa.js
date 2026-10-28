"use strict";
// Installation is optional; the same UI remains available in the browser.
(() => {
  const buttons = [...document.querySelectorAll(".pwa-install")];
  const standalone = matchMedia("(display-mode: standalone)");
  let invitation = null, installed = Boolean(navigator.standalone), prompting = false;
  function update() {
    for (const button of buttons) {
      button.hidden = !invitation || installed || standalone.matches || prompting;
      button.disabled = prompting;
    }
  }
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault(); invitation = event; update();
  });
  window.addEventListener("appinstalled", () => {installed = true; invitation = null; update();});
  standalone.addEventListener("change", update);
  for (const button of buttons) button.addEventListener("click", async () => {
    if (!invitation || installed || standalone.matches || prompting) return;
    const event = invitation;
    invitation = null; prompting = true; update();
    try {
      await event.prompt();
      if ((await event.userChoice).outcome === "accepted") installed = true;
    } catch {
      if (typeof toast === "function") toast("Use your browser menu to install Covenant.");
    } finally {prompting = false; update();}
  });
  update();
})();

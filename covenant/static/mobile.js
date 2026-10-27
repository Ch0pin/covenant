"use strict";
// Mobile drawers have transient state; desktop panel preferences stay independent.
(() => {
  const media = matchMedia("(max-width: 900px)");
  const workspace = $("#workspace"), main = $(".main"), scrim = $("#mobile-scrim");
  const panels = {conversations: $("#conversations-panel"), participants: $("#participants-panel")};
  const triggers = {conversations: $("#mobile-chats"), participants: $("#mobile-details")};
  const background = [$("#mobile-toolbar"), $("#empty-state"), $("#connection-error"), $(".conversation-header"), $(".chat-column")];
  const actions = $(".room-actions"), roomHeader = $(".conversation-header");
  let active = null, opener = null;
  function sync() {
    if (media.matches && actions.parentElement !== panels.participants) {
      panels.participants.querySelector(".participants-title").after(actions);
    } else if (!media.matches && actions.parentElement !== roomHeader) {
      roomHeader.append(actions);
    }
    if (!media.matches || !state.me) active = null;
    if (active === "participants" && !state.room) active = null;
    main.inert = false;
    for (const area of background) area.inert = Boolean(active);
    scrim.hidden = !active;
    workspace.dataset.mobilePanel = active || "";
    triggers.participants.disabled = !state.room;
    for (const [name, panel] of Object.entries(panels)) {
      triggers[name].setAttribute("aria-expanded", String(active === name));
      if (media.matches) {
        panel.hidden = active !== name;
        panel.inert = active !== name;
        panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
        panel.setAttribute("aria-label", name === "conversations" ? "Chats" : "Room details");
        const close = $("#toggle-" + name);
        close.title = close.ariaLabel = name === "conversations" ? "Close chats" : "Close details";
        close.setAttribute("aria-expanded", String(active === name));
      } else {
        panel.inert = false;
        for (const attr of ["role", "aria-modal", "aria-label"]) panel.removeAttribute(attr);
      }
    }
    if (media.matches) {
      $("#reopen-conversations").hidden = true;
      $("#participants-reopen-rail").hidden = true;
    }
    if (state.room?.status === "active") $("#composer-hint").textContent = media.matches ? "Tap the arrow to send · Return for a new line" : "Enter to send · Shift+Enter for a new line";
  }
  function open(name) {
    if (!media.matches || !state.me || (name === "participants" && !state.room)) return;
    opener = triggers[name]; active = name;
    closeRoomMenu(); hideMentions(); sync();
    $("#toggle-" + name).focus({preventScroll: true});
  }
  function close(restoreFocus = true) {
    const wasOpen = active; active = null; sync();
    if (wasOpen && restoreFocus) opener?.focus({preventScroll: true});
  }
  function viewport() {
    if (!media.matches) {
      workspace.style.removeProperty("--mobile-height"); workspace.style.removeProperty("--mobile-top");
      document.documentElement.style.removeProperty("--mobile-height");
      workspace.classList.remove("mobile-keyboard"); return;
    }
    const view = window.visualViewport;
    // Keep pinch zoom available; only track the unzoomed keyboard/browser viewport.
    if (view && view.scale !== 1) return;
    workspace.style.setProperty("--mobile-height", `${view?.height || innerHeight}px`);
    document.documentElement.style.setProperty("--mobile-height", `${view?.height || innerHeight}px`);
    workspace.style.setProperty("--mobile-top", `${view?.offsetTop || 0}px`);
    workspace.classList.toggle("mobile-keyboard", Boolean(view && innerHeight - view.height > 150));
  }
  function setApprovalCount(count) {
    const badge = $("#mobile-approval-count"); badge.textContent = String(count); badge.hidden = !count;
    triggers.participants.setAttribute("aria-label", count ? `Room details, ${count} pending approvals` : "Room details");
  }
  globalThis.CovenantMobile = {
    get isMobile() {return media.matches;}, sync, open, setApprovalCount,
    togglePanel(name) {active === name ? close() : open(name);},
    roomSelected() {close();}
  };
  for (const [name, button] of Object.entries(triggers)) button.onclick = () => open(name);
  scrim.onclick = () => close();
  document.addEventListener("keydown", event => {
    if (!active || document.querySelector("dialog[open]")) return;
    if (event.key === "Escape") {event.preventDefault(); close(); return;}
    if (event.key !== "Tab") return;
    const controls = [...panels[active].querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex="0"]')].filter(node => node.getClientRects().length);
    const first = controls[0], last = controls.at(-1);
    if (!first) {event.preventDefault(); return;}
    if (event.shiftKey && (document.activeElement === first || !panels[active].contains(document.activeElement))) {event.preventDefault(); last.focus();}
    else if (!event.shiftKey && (document.activeElement === last || !panels[active].contains(document.activeElement))) {event.preventDefault(); first.focus();}
  });
  media.addEventListener("change", () => {active = null; viewport(); applyPanelPreferences();});
  window.addEventListener("resize", viewport);
  window.visualViewport?.addEventListener("resize", viewport);
  window.visualViewport?.addEventListener("scroll", viewport);
  window.addEventListener("covenant-login-change", () => {close(false); setApprovalCount(0);});
  setApprovalCount(Number($("#approvals-count").textContent) || 0);
  viewport(); sync();
})();

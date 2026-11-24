"use strict";
(() => {
  const menu = document.querySelector("#participant-menu");
  let target = null, selectedRow = null, selectedTrigger = null;
  function selectRow(row, trigger) {
    if (selectedRow) selectedRow.dataset.menuOpen = "false";
    selectedTrigger?.setAttribute("aria-expanded", "false");
    selectedRow = row; selectedTrigger = trigger;
    if (row) row.dataset.menuOpen = "true";
    trigger?.setAttribute("aria-expanded", "true");
  }
  function valid() {
    return target && state.me && state.room?.id === target.room && state.room.status !== "archived" &&
      state.agents.some(a => a.id === target.id && a.handle === target.handle && !!a.revoked === target.revoked);
  }
  function close(focus = false) {
    const id = target?.id;
    menu.hidden = true; target = null; menu.replaceChildren();
    selectRow(null, null);
    if (focus && id) document.querySelector(`.participant-menu-trigger[data-agent-id="${id}"]`)?.focus();
  }
  function sync() {if (target && !valid()) close();}
  function open(agent, controls, row, trigger, x, y) {
    close();
    target = {id: agent.id, handle: agent.handle, revoked: !!agent.revoked, room: state.room?.id};
    if (!valid()) {close(); return;}
    selectRow(row, trigger);
    if (typeof closeRoomMenu === "function") closeRoomMenu();
    for (const button of [...controls.children]) {
      const action = button.onclick;
      button.setAttribute("role", "menuitem");
      button.onpointermove = event => {
        if (event.pointerType !== "touch" && !button.disabled && document.activeElement !== button) {
          button.focus({preventScroll: true});
        }
      };
      button.onclick = event => {
        if (button.disabled) return;
        const allowed = valid(); close(true);
        if (allowed) return action?.(event);
      };
      menu.append(button);
    }
    menu.setAttribute("aria-label", `Actions for @${agent.handle}`);
    menu.hidden = false;
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
    menu.querySelector("button:not(:disabled)")?.focus();
  }
  function attach(row, controls, agent) {
    const trigger = element("button", "participant-menu-trigger", "⋯");
    trigger.type = "button"; trigger.dataset.agentId = agent.id;
    trigger.title = `Actions for @${agent.handle}`;
    trigger.setAttribute("aria-label", trigger.title); trigger.setAttribute("aria-haspopup", "menu");
    // Build fresh controls on demand, including after a previous menu has closed.
    const show = (x, y) => {
      const id = agent.id;
      renderAgents();
      const fresh = document.querySelector(`.participant-menu-trigger[data-agent-id="${id}"]`);
      if (fresh) fresh.openMenu(x, y);
    };
    trigger.setAttribute("aria-expanded", "false");
    if (target?.id === agent.id && valid()) selectRow(row, trigger);
    trigger.openMenu = (x, y) => open(agent, controls, row, trigger, x, y);
    trigger.onclick = () => {const r = trigger.getBoundingClientRect(); show(r.right, r.bottom);};
    row.oncontextmenu = event => {event.preventDefault(); show(event.clientX, event.clientY);};
    trigger.onkeydown = event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault(); trigger.onclick();
      }
    };
    row.append(trigger);
  }
  document.addEventListener("pointerdown", event => {if (!menu.contains(event.target)) close();});
  document.addEventListener("keydown", event => {
    if (menu.hidden) return;
    if (event.key === "Escape") {event.preventDefault(); close(true);}
    else if (event.key === "Tab") close();
    else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const items = [...menu.querySelectorAll("button:not(:disabled)")];
      const current = items.indexOf(document.activeElement);
      const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 :
        (current + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
      items[index]?.focus();
    }
  });
  document.addEventListener("scroll", event => {if (!menu.contains(event.target)) close();}, true);
  window.addEventListener("resize", () => close());
  window.addEventListener("covenant-login-change", () => close());
  globalThis.CovenantParticipantMenu = {attach, sync, close};
})();

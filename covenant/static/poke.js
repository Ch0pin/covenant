"use strict";
// Poke an agent: send it a short tracked message and show whether it acknowledges or answers.
// The poke is an ordinary operator message, so it uses the delivery receipts agents already know.
(() => {
  const WAIT_SECONDS = 60;
  let pokes = {}, busy = false;
  function pokeText(handle) {return `@${handle} Poke from the operator: are you still there? Acknowledge or reply briefly.`;}
  function seconds(value) {
    const total = Math.max(0, Math.round(value));
    return total < 60 ? `${total} s` : total % 60 ? `${Math.floor(total / 60)} min ${total % 60} s` : `${total / 60} min`;
  }
  function describe(poke, now = Date.now() / 1000) {
    const receipt = poke.receipt, sent = receipt?.queued_at ?? poke.sentAt;
    if (receipt?.answered_at != null) return {text: `Poke answered in ${seconds(receipt.answered_at - sent)}`, level: "ok", done: true};
    if (receipt?.acknowledged_at != null) return {text: `Poke acknowledged in ${seconds(receipt.acknowledged_at - sent)}`, level: "ok", done: true};
    const elapsed = now - poke.sentAt;
    if (elapsed >= WAIT_SECONDS) return {text: `No response to poke after ${seconds(WAIT_SECONDS)}`, level: "warning", done: true};
    return {text: `Poked · waiting ${seconds(elapsed)}`, level: "", done: false};
  }
  function paint(agent, line, button) {
    const poke = pokes[agent.id], shown = poke && poke.room === state.room?.id ? describe(poke) : null;
    line.hidden = !shown;
    line.textContent = shown?.text || "";
    line.dataset.level = shown?.level || "";
    if (shown) line.title = `Message #${poke.messageId}: the agent is expected to acknowledge it or reply to it.`;
    if (button) button.disabled = Boolean(shown && !shown.done);
  }
  function repaint() {
    for (const line of document.querySelectorAll(".poke-result")) {
      const id = line.dataset.agentId, agent = state.agents.find(a => a.id === id);
      if (agent) paint(agent, line, document.querySelector(`.poke-button[data-agent-id="${id}"]`));
    }
  }
  async function send(agent) {
    const room = state.room;
    if (!room || room.status === "archived" || agent.revoked) return;
    const current = pokes[agent.id];
    if (current && current.room === room.id && !describe(current).done) return;
    try {
      const message = await api(`/api/rooms/${room.id}/messages`, {method: "POST",
        body: JSON.stringify({text: pokeText(agent.handle), client_message_id: crypto.randomUUID()})});
      pokes[agent.id] = {room: room.id, handle: agent.handle, messageId: message.id, sentAt: Date.now() / 1000, receipt: null};
      toast(`Poked @${agent.handle}`);
    } catch (error) {toast(error.message);}
    repaint();
  }
  // Pull receipts for pokes that are still waiting; the elapsed time repaints on every tick.
  async function refresh() {
    if (!state.me) {pokes = {}; repaint(); return;}
    const room = state.room?.id;
    const waiting = Object.values(pokes).filter(poke => poke.room === room && !describe(poke).done);
    if (!room || !waiting.length || busy) {repaint(); return;}
    busy = true;
    try {
      const result = await api(`/api/rooms/${room}/message-receipts?ids=${waiting.map(poke => poke.messageId).join(",")}`);
      for (const poke of waiting) poke.receipt = (result.receipts[poke.messageId] || []).find(r => r.handle === poke.handle) || poke.receipt;
    } catch {}
    finally {busy = false; repaint();}
  }
  function button(agent) {
    const control = element("button", "poke-button", "Poke");
    control.dataset.agentId = agent.id;
    control.setAttribute("aria-label", `Poke ${agent.handle}`);
    control.title = "Send a short message that the agent should acknowledge, to check it is still responsive";
    control.onclick = () => send(agent);
    return control;
  }
  function line(agent) {
    const result = element("small", "poke-result"); result.dataset.agentId = agent.id; result.hidden = true;
    return result;
  }
  globalThis.CovenantPoke = {button, line, paint, send, refresh, describe, pokeText, WAIT_SECONDS};
  window.addEventListener("covenant-login-change", refresh);
  setInterval(refresh, 2000);
})();

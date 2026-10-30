"use strict";
(() => {
  const levels = new Set(["neutral", "ok", "info", "warning", "error"]);
  let roomId = null, statuses = {}, busy = false, clockOffset = 0, fetchedAt = 0;
  function duration(since) {
    if (!Number.isFinite(since)) return "";
    const seconds = Math.max(0, Date.now() / 1000 + clockOffset - since);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  }
  function paint(id, dot, line) {
    const current = roomId === state.room?.id && Date.now() - fetchedAt < 10000 ? statuses[id] : null;
    dot.dataset.level = levels.has(current?.level) ? current.level : "neutral";
    if (!current) {
      line.textContent = "Status unavailable";
      line.title = "No current status data. Refreshing automatically.";
      return;
    }
    const parts = [current.label];
    if (current.since != null) parts.push(duration(current.since));
    if (Number.isFinite(current.reset_at)) parts.push(`account reset ${new Date(current.reset_at * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`);
    line.textContent = parts.join(" · ");
    line.title = [current.detail, current.telemetry].filter(Boolean).join(" ");
  }
  function repaint() {
    for (const line of document.querySelectorAll(".participant-status")) {
      const dot = line.parentElement.querySelector(".participant-status-dot");
      if (dot) paint(line.dataset.agentId, dot, line);
    }
  }
  async function refresh() {
    if (!state.me || !state.room) {roomId = null; statuses = {}; fetchedAt = 0; repaint(); return;}
    if (busy) return;
    const user = state.me, id = state.room.id;
    busy = true;
    try {
      const data = await api(`/api/rooms/${id}/participant-status`);
      if (state.me !== user || state.room?.id !== id) return;
      statuses = data.statuses || {}; roomId = id; fetchedAt = Date.now();
      clockOffset = Number.isFinite(data.server_time) ? data.server_time - Date.now() / 1000 : 0;
    } catch {
      if (state.me === user && state.room?.id === id) {statuses = {}; roomId = id; fetchedAt = 0;}
    } finally {busy = false; repaint();}
  }
  globalThis.CovenantParticipantStatus = {paint, refresh};
  window.addEventListener("covenant-login-change", refresh);
  setInterval(refresh, 2000);
})();

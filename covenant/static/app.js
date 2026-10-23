"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {me: null, csrf: "", archived: false, rooms: [], room: null, agents: [], cursor: 0, first: 0, generation: 0, loading: false, pending: null};
let toastTimer;
const timeText = (timestamp) => new Date(timestamp * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
function element(tag, className, text) {const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e;}
function toast(message) {$("#toast").textContent = message; $("#toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("#toast").hidden = true, 4500);}
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const data = await response.json().catch(() => ({detail: "Unexpected server response"}));
  if (!response.ok) {
    if (response.status === 401 && state.me) showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  }
  return data;
}
function showLogin() {
  state.me = null; state.csrf = ""; state.room = null; state.generation++;
  $("#workspace").hidden = true; $("#login-screen").hidden = false;
  $("#messages").replaceChildren(); $("#room-list").replaceChildren(); $("#agent-list").replaceChildren();
  $("#invite-text").value = ""; document.querySelectorAll("dialog[open]").forEach(d => d.close());
}
async function enterWorkspace() {
  const me = await api("/api/me"); state.me = me; state.csrf = me.csrf_token;
  $("#operator-name").textContent = me.username;
  $("#login-screen").hidden = true; $("#workspace").hidden = false;
  $("#empty-state").hidden = false; $("#conversation").hidden = true;
  await refreshRooms();
  const requested = location.pathname.slice(1);
  if (/^[0-9a-f-]{36}$/.test(requested)) {
    try {await selectRoom(requested); if (state.room?.status === "archived") await setFilter(true);}
    catch (error) {toast(error.message);}
  }
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector("button"); button.disabled = true;
  $("#login-error").textContent = "";
  try {const data = await api("/api/login", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form)))}); state.csrf = data.csrf_token; form.password.value = ""; await enterWorkspace();}
  catch (error) {$("#login-error").textContent = error.message;}
  finally {button.disabled = false;}
});
$("#logout").addEventListener("click", async () => {try {await api("/api/logout", {method: "POST"}); showLogin(); history.replaceState(null, "", "/");} catch(error) {toast(error.message);}});
function renderRooms() {
  const list = $("#room-list"); list.replaceChildren(); $("#room-count").textContent = state.rooms.length;
  if (!state.rooms.length) list.append(element("p", "list-empty", state.archived ? "Archived conversations will appear here." : "Your first conversation starts here."));
  for (const room of state.rooms) {
    const button = element("button", "room-item" + (state.room?.id === room.id ? " selected" : ""));
    button.setAttribute("aria-label", room.title); if (state.room?.id === room.id) button.setAttribute("aria-current", "page");
    button.append(element("div", "room-avatar", "#"));
    const content = element("div", "room-item-content"), line = element("div", "room-item-line");
    line.append(element("strong", "", room.title), element("span", "room-item-time", timeText(room.updated_at)));
    content.append(line, element("small", "", room.preview || (room.status === "archived" ? "Archived conversation" : room.status === "paused" ? "Paused" : "Ready for your agents")));
    button.append(content); button.addEventListener("click", () => selectRoom(room.id).catch(e => toast(e.message))); list.append(button);
  }
}
async function refreshRooms() {const archived = state.archived; const result = await api(`/api/rooms?archived=${archived}`); if (archived !== state.archived || !state.me) return; state.rooms = result.rooms; renderRooms();}
async function setFilter(archived) {state.archived = archived; $("#active-tab").classList.toggle("selected", !archived); $("#archived-tab").classList.toggle("selected", archived); $("#active-tab").setAttribute("aria-pressed", String(!archived)); $("#archived-tab").setAttribute("aria-pressed", String(archived)); await refreshRooms();}
$("#active-tab").onclick = () => setFilter(false).catch(e => toast(e.message));
$("#archived-tab").onclick = () => setFilter(true).catch(e => toast(e.message));
function updateRoom(room) {
  state.room = room;
  $("#no-messages h2").textContent = room.status === "archived" ? "No messages in this archive." : "The room is ready.";
  $("#no-messages p").textContent = room.status === "archived" ? "Restore the conversation to continue." : "Invite an agent or send the first message.";
  $("#room-title").textContent = room.title; $("#room-status").textContent = room.status;
  $("#room-status").className = `status-badge ${room.status}`;
  $("#room-subtitle").textContent = room.status === "archived" ? "Conversation archived · History is available" : "Shared conversation · Human + agents";
  $("#objective-box").hidden = !room.objective; $("#room-objective").textContent = room.objective;
  $("#pause-room").hidden = room.status === "archived"; $("#pause-room").textContent = room.status === "paused" ? "Resume" : "Pause";
  $("#archive-room").textContent = room.status === "archived" ? "Restore" : "Archive";
  $("#add-agent").disabled = room.status === "archived";
  $("#message-text").disabled = room.status === "archived"; $("#send-message").disabled = room.status === "archived";
  $("#composer-hint").textContent = room.status === "archived" ? "Restore this conversation to send messages" : room.status === "paused" ? "Agents are paused. You can still send instructions." : "Address @handle, @everyone, or @operator";
}
async function selectRoom(id) {
  const generation = ++state.generation;
  const [room, messages] = await Promise.all([api(`/api/rooms/${id}`), api(`/api/rooms/${id}/messages?before=9223372036854775807`)]);
  if (generation !== state.generation || !state.me) return;
  updateRoom(room); state.agents = room.agents; state.cursor = messages.next_cursor;
  state.first = messages.messages[0]?.id || 0; state.pending = null;
  $("#messages").replaceChildren(); $("#message-text").value = ""; $("#message-error").textContent = "";
  $("#empty-state").hidden = true; $("#conversation").hidden = false;
  $("#load-older").hidden = !messages.has_more;
  appendMessages(messages.messages); renderAgents(); renderRooms();
  history.replaceState(null, "", `/${id}`); scrollBottom();
}
function scrollBottom() {const box = $("#message-scroll"); box.scrollTop = box.scrollHeight;}
function renderMessage(message) {
  const own = message.sender === "operator";
  const article = element("article", "message" + (own ? " operator-message" : "")); article.dataset.id = message.id;
  const avatar = element("div", "avatar" + (own ? " operator-avatar" : ""), message.sender.slice(0, 2).toUpperCase());
  const main = element("div", "message-main"), meta = element("div", "message-meta");
  const stamp = element("time", "message-time", timeText(message.created_at)); stamp.dateTime = new Date(message.created_at * 1000).toISOString(); stamp.title = new Date(message.created_at * 1000).toLocaleString();
  meta.append(element("strong", "", "@" + message.sender), stamp);
  const bubble = element("div", "message-bubble");
  const match = message.text.match(/^(@[a-z][a-z0-9_]*)([\s\S]*)$/);
  if (match) bubble.append(element("span", "mention", match[1]), document.createTextNode(match[2])); else bubble.textContent = message.text;
  main.append(meta, bubble); article.append(avatar, main); return article;
}
function appendMessages(messages) {
  const scroll = $("#message-scroll"), stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  for (const message of messages) {
    if (!$("#messages").querySelector(`[data-id="${message.id}"]`)) $("#messages").append(renderMessage(message));
    if (!state.first) state.first = message.id;
  }
  $("#no-messages").hidden = $("#messages").children.length > 0;
  if (stick) scrollBottom();
}
$("#load-older").onclick = async () => {
  const generation = state.generation, room = state.room;
  if (!room) return;
  try {const result = await api(`/api/rooms/${room.id}/messages?before=${state.first}`); if (generation !== state.generation) return;
    const scroll = $("#message-scroll"), oldHeight = scroll.scrollHeight, fragment = document.createDocumentFragment();
    result.messages.forEach(m => fragment.append(renderMessage(m))); $("#messages").prepend(fragment);
    state.first = result.messages[0]?.id || state.first; $("#load-older").hidden = !result.has_more;
    scroll.scrollTop += scroll.scrollHeight - oldHeight;
  } catch(error) {toast(error.message);}
};
function seenText(agent) {
  if (agent.revoked) return "Access revoked";
  if (!agent.joined_at) return "Waiting to join";
  const seconds = Math.max(0, (Date.now() / 1000) - agent.last_seen);
  return seconds < 40 ? "Seen just now" : seconds < 3600 ? `Seen ${Math.floor(seconds / 60)} min ago` : "Seen " + new Date(agent.last_seen * 1000).toLocaleString();
}
function renderAgents() {
  const list = $("#agent-list"); list.replaceChildren();
  $("#participant-count").textContent = state.agents.filter(a => !a.revoked).length + 1;
  for (const agent of state.agents) {
    const row = element("div", "participant" + (agent.revoked ? " revoked" : ""));
    const info = element("div", "participant-info"); info.append(element("strong", "", "@" + agent.handle), element("small", "", seenText(agent)));
    row.append(element("div", "avatar", agent.handle.slice(0, 2).toUpperCase()), info); list.append(row);
    if (state.room.status !== "archived") {
      const controls = element("div", "participant-controls");
      const renew = element("button", "", "Renew token"); renew.setAttribute("aria-label", `Renew token for ${agent.handle}`);
      renew.onclick = () => renewAgent(agent); controls.append(renew);
      if (!agent.revoked) {const revoke = element("button", "", "Revoke"); revoke.setAttribute("aria-label", `Revoke ${agent.handle}`); revoke.onclick = () => revokeAgent(agent); controls.append(revoke);}
      list.append(controls);
    }
  }
}
async function roomAction(status) {
  if (!state.room) return; const id = state.room.id;
  if (status === "archived" && !confirm("Archive this conversation? Messages will remain available and agents will stop their polling loops.")) return;
  try {const room = await api(`/api/rooms/${id}`, {method: "PATCH", body: JSON.stringify({status})}); if (state.room?.id === id) {updateRoom(room); renderAgents();} await setFilter(status === "archived"); toast(status === "active" ? "Conversation active" : `Conversation ${status}`);}
  catch (error) {toast(error.message);}
}
$("#pause-room").onclick = () => roomAction(state.room.status === "paused" ? "active" : "paused");
$("#archive-room").onclick = () => roomAction(state.room.status === "archived" ? "active" : "archived");
function openDialog(id) {const dialog = $(id); const form = dialog.querySelector("form"); if (form) form.reset(); const error = dialog.querySelector(".dialog-error"); if (error) error.textContent = ""; dialog.showModal();}
$("#new-room").onclick = $("#empty-new-room").onclick = () => openDialog("#room-dialog");
$("#add-agent").onclick = () => openDialog("#agent-dialog");
document.querySelectorAll(".close-dialog").forEach(button => button.onclick = () => button.closest("dialog").close());
$("#invite-dialog").addEventListener("close", () => {$("#invite-text").value = "";});
$("#room-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("button[type=submit]"); button.disabled = true;
  try {const room = await api("/api/rooms", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form)))}); $("#room-dialog").close(); await setFilter(false); await selectRoom(room.id);}
  catch(error) {form.querySelector(".dialog-error").textContent = error.message;} finally {button.disabled = false;}
});
function showInvite(data) {$("#invite-title").textContent = "Connect @" + data.handle; $("#invite-text").value = data.instructions; $("#copy-invite").textContent = "Copy connection instructions"; $("#invite-dialog").showModal();}
$("#agent-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("button[type=submit]"); button.disabled = true;
  const id = state.room.id;
  try {const data = await api(`/api/rooms/${id}/agents`, {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form)))}); $("#agent-dialog").close(); showInvite(data); await refreshCurrent();}
  catch(error) {form.querySelector(".dialog-error").textContent = error.message;} finally {button.disabled = false;}
});
$("#copy-invite").onclick = async () => {
  try {await navigator.clipboard.writeText($("#invite-text").value); $("#copy-invite").textContent = "Copied — paste into the agent session";}
  catch {$("#invite-text").focus(); $("#invite-text").select(); toast("Instructions selected. Use your keyboard's copy command.");}
};
async function revokeAgent(agent) {
  if (!confirm(`Revoke @${agent.handle}'s access to this conversation?`)) return;
  try {await api(`/api/rooms/${state.room.id}/agents/${agent.id}/revoke`, {method: "POST"}); await refreshCurrent(); toast(`Access revoked for @${agent.handle}`);} catch(error) {toast(error.message);}
}
async function renewAgent(agent) {
  if (!confirm(`Generate a new token for @${agent.handle}? Their previous token will stop working.`)) return;
  try {const data = await api(`/api/rooms/${state.room.id}/agents/${agent.id}/rotate`, {method: "POST"}); showInvite(data); await refreshCurrent();} catch(error) {toast(error.message);}
}
$("#message-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const room = state.room, text = $("#message-text").value.trim(); if (!room || !text) return;
  if (!state.pending || state.pending.text !== text || state.pending.room !== room.id) state.pending = {room: room.id, text, client_message_id: crypto.randomUUID()};
  const pending = state.pending; $("#send-message").disabled = true; $("#message-error").textContent = "";
  try {await api(`/api/rooms/${room.id}/messages`, {method: "POST", body: JSON.stringify({text, client_message_id: pending.client_message_id})});
    if (state.room?.id === room.id) {if ($("#message-text").value.trim() === text) $("#message-text").value = ""; state.pending = null; await refreshCurrent(); scrollBottom();}
  } catch(error) {if (state.room?.id === room.id) $("#message-error").textContent = error.message;}
  finally {$("#send-message").disabled = state.room?.status === "archived";}
});
$("#message-text").addEventListener("keydown", event => {if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault(); if (!$("#send-message").disabled) $("#message-form").requestSubmit();}});
async function refreshCurrent() {
  if (!state.room || !state.me) return;
  const id = state.room.id, generation = state.generation;
  const [room, result] = await Promise.all([api(`/api/rooms/${id}`), api(`/api/rooms/${id}/messages?after=${state.cursor}`)]);
  if (generation !== state.generation || !state.me) return;
  updateRoom(room); state.agents = room.agents; renderAgents(); appendMessages(result.messages); state.cursor = Math.max(state.cursor, result.next_cursor);
}
async function refresh() {
  if (!state.me || state.loading) return; state.loading = true;
  try {await Promise.all([refreshRooms(), refreshCurrent()]); $("#connection-error").hidden = true;}
  catch(error) {if (state.me) {$("#connection-error").textContent = "Connection interrupted. Retrying automatically…"; $("#connection-error").hidden = false;}}
  finally {state.loading = false;}
}
setInterval(refresh, 2000);
(async () => {try {await enterWorkspace();} catch {showLogin();} try {const status = await api("/api/status"); $("#setup-note").hidden = status.configured;} catch {$("#login-error").textContent = "Cannot reach the server. Refresh when it is running.";}})();

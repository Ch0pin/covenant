"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {me: null, csrf: "", archived: false, rooms: [], room: null, agents: [], cursor: 0, first: 0, generation: 0, loading: false, pending: null, replyTo: null};
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
    const error = new Error(typeof data.detail === "string" ? data.detail : "Request failed");
    error.status = response.status; throw error;
  }
  return data;
}
let notificationsEnabled = false, notificationCursor = null, notificationEpoch = 0, notificationsBusy = false;
const visibleNotifications = new Set();
try {notificationsEnabled = localStorage.getItem("covenant-notifications") === "on";} catch {}
function updateNotificationButton() {
  const button = $("#notifications-toggle");
  const supported = "Notification" in window && window.isSecureContext;
  button.disabled = !supported;
  button.textContent = !supported ? "Notifications unavailable" : notificationsEnabled && Notification.permission === "granted" ? "Disable notifications" : "Enable notifications";
  button.setAttribute("aria-pressed", String(supported && notificationsEnabled && Notification.permission === "granted"));
}
function resetNotifications() {
  notificationEpoch++; notificationCursor = null;
  for (const notification of visibleNotifications) notification.close();
  visibleNotifications.clear();
}
async function refreshNotifications() {
  if (!state.me || !notificationsEnabled || !("Notification" in window) || Notification.permission !== "granted" || notificationsBusy) return;
  const epoch = notificationEpoch;
  notificationsBusy = true;
  try {
    const result = await api("/api/operator-notifications" + (notificationCursor === null ? "" : `?after=${notificationCursor}`));
    if (epoch !== notificationEpoch || !state.me || !notificationsEnabled) return;
    for (const message of result.messages) {
      try {
        const notification = new Notification(`Covenant · ${message.room_title}`, {
          body: `@${message.sender}: ${message.text}`, tag: `covenant-${message.id}`, icon: "/static/icon.png"
        });
        visibleNotifications.add(notification);
        notification.onclose = () => visibleNotifications.delete(notification);
        notification.onclick = () => {
          notification.close();
          if (!state.me || epoch !== notificationEpoch) return;
          window.focus();
          setFilter(false).then(() => selectRoom(message.room_id)).catch(error => toast(error.message));
        };
      } catch {
        toast(`@${message.sender} needs your input in ${message.room_title}. Desktop notifications are unavailable in this browser.`);
      }
    }
    notificationCursor = result.next_cursor;
  } finally {notificationsBusy = false;}
}
$("#notifications-toggle").onclick = async () => {
  if (!("Notification" in window)) return;
  try {
    if (notificationsEnabled && Notification.permission === "granted") {
      notificationsEnabled = false; resetNotifications();
    } else {
      const epoch = notificationEpoch;
      const permission = await Notification.requestPermission();
      if (epoch !== notificationEpoch || !state.me) return;
      if (permission !== "granted") {
        toast("Allow notifications in your browser's site settings, then try again."); return;
      }
      notificationsEnabled = true; resetNotifications();
      await refreshNotifications();
      toast("Notifications enabled for new @operator messages in all chats.");
    }
  } catch (error) {toast(error.message);}
  finally {
    try {localStorage.setItem("covenant-notifications", notificationsEnabled ? "on" : "off");} catch {}
    updateNotificationButton();
  }
};
updateNotificationButton();
let panelPreferences = {conversations: false, participants: false, objective: false, "file-locks": false};
try {
  const saved = JSON.parse(localStorage.getItem("covenant-panels") || "{}");
  for (const name of Object.keys(panelPreferences)) panelPreferences[name] = saved?.[name] === true;
} catch {}
function applyPanelPreferences() {
  $("#workspace").classList.toggle("sidebar-collapsed", panelPreferences.conversations);
  for (const [name, target, label] of [["conversations", "conversations-panel", "chats"], ["participants", "participants-panel", "participants"],
      ["objective", "objective-content", "room objective"], ["file-locks", "file-lock-content", "file locks"]]) {
    const collapsed = panelPreferences[name], button = $("#toggle-" + name);
    $("#" + target).hidden = collapsed;
    const explanation = `${collapsed ? "Show" : "Hide"} ${label}`;
    button.title = explanation; button.setAttribute("aria-label", explanation);
    button.setAttribute("aria-expanded", String(!collapsed));
  }
  $("#reopen-conversations").hidden = !panelPreferences.conversations;
  $("#participants-reopen-rail").hidden = !panelPreferences.participants || !state.room;
  $("#reopen-participants").disabled = !state.room;
  $("#toggle-participants").disabled = !state.room;
  $("#toggle-objective").disabled = !state.room?.objective;
  $("#toggle-file-locks").disabled = !state.room;
}
function togglePanel(name) {
  const scroll = $("#message-scroll"), stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  closeRoomMenu(); hideMentions();
  panelPreferences[name] = !panelPreferences[name]; applyPanelPreferences();
  try {localStorage.setItem("covenant-panels", JSON.stringify(panelPreferences));} catch {}
  if (stick && state.room) scrollBottom();
  if (["conversations", "participants"].includes(name)) {
    $("#" + (panelPreferences[name] ? "reopen-" : "toggle-") + name).focus({preventScroll: true});
  }
}
$("#toggle-conversations").onclick = () => togglePanel("conversations");
$("#toggle-participants").onclick = () => togglePanel("participants");
$("#reopen-conversations").onclick = () => togglePanel("conversations");
$("#reopen-participants").onclick = () => togglePanel("participants");
$("#toggle-objective").onclick = () => togglePanel("objective");
$("#toggle-file-locks").onclick = () => togglePanel("file-locks");
applyPanelPreferences();
function showLogin() {
  clearReplyTarget();
  hideMentions();
  resetNotifications();
  closeRoomMenu(); state.me = null; state.csrf = ""; state.room = null; state.generation++; applyPanelPreferences();
  $("#workspace").hidden = true; $("#login-screen").hidden = false;
  $("#messages").replaceChildren(); $("#room-list").replaceChildren(); $("#agent-list").replaceChildren();
  $("#invite-text").value = ""; document.querySelectorAll("dialog[open]").forEach(d => d.close());
}
async function enterWorkspace() {
  const me = await api("/api/me"); state.me = me; state.csrf = me.csrf_token;
  resetNotifications(); updateNotificationButton();
  await refreshNotifications();
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
    button.dataset.roomId = room.id; button.setAttribute("aria-haspopup", "menu");
    button.addEventListener("contextmenu", event => {event.preventDefault(); openRoomMenu(room, event.clientX, event.clientY);});
    button.addEventListener("keydown", event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault(); const rect = button.getBoundingClientRect(); openRoomMenu(room, rect.left, rect.bottom);
      }
    });
    button.setAttribute("aria-label", room.title); if (state.room?.id === room.id) button.setAttribute("aria-current", "page");
    button.append(element("div", "room-avatar", "#"));
    const content = element("div", "room-item-content"), line = element("div", "room-item-line");
    line.append(element("strong", "", room.title), element("span", "room-item-time", timeText(room.updated_at)));
    content.append(line, element("small", "", room.preview || (room.status === "archived" ? "Archived conversation" : room.status === "paused" ? "Paused" : "Ready for your agents")));
    button.append(content); button.addEventListener("click", () => selectRoom(room.id).catch(e => toast(e.message))); list.append(button);
  }
}
async function refreshRooms() {const archived = state.archived; const result = await api(`/api/rooms?archived=${archived}`); if (archived !== state.archived || !state.me) return; state.rooms = result.rooms; renderRooms();}
async function setFilter(archived) {closeRoomMenu(); state.archived = archived; $("#active-tab").classList.toggle("selected", !archived); $("#archived-tab").classList.toggle("selected", archived); $("#active-tab").setAttribute("aria-pressed", String(!archived)); $("#archived-tab").setAttribute("aria-pressed", String(archived)); await refreshRooms();}
$("#active-tab").onclick = () => setFilter(false).catch(e => toast(e.message));
$("#archived-tab").onclick = () => setFilter(true).catch(e => toast(e.message));
function updateRoom(room) {
  if ("file_locks" in room || state.room?.id !== room.id) renderFileLocks(room.file_locks);
  state.room = room; applyPanelPreferences();
  if (room.status === "archived") clearReplyTarget();
  document.querySelectorAll(".reply-button").forEach(button => {button.disabled = room.status === "archived";});
  $("#no-messages h2").textContent = room.status === "archived" ? "No messages in this archive." : "The room is ready.";
  $("#no-messages p").textContent = room.status === "archived" ? "Restore the conversation to continue." : "Invite an agent or send the first message.";
  $("#room-title").textContent = room.title; $("#room-status").textContent = room.status;
  $("#room-status").className = `status-badge ${room.status}`;
  $("#room-subtitle").textContent = room.status === "archived" ? "Conversation archived · History is available" : "Shared conversation · Human + agents";
  $("#objective-box").hidden = !room.objective; $("#room-objective").textContent = room.objective;
  $("#pause-room").hidden = room.status === "archived"; $("#pause-room").textContent = room.status === "paused" ? "Resume" : "Pause";
  $("#archive-room").textContent = room.status === "archived" ? "Restore" : "Archive";
  $("#delete-room").hidden = room.status !== "archived";
  $("#add-agent").disabled = room.status === "archived";
  if (room.status === "archived") hideMentions();
  $("#message-text").disabled = room.status === "archived"; $("#send-message").disabled = room.status === "archived";
  $("#composer-hint").textContent = room.status === "archived" ? "Restore this conversation to send messages" : room.status === "paused" ? "Agents are paused. You can still send instructions." : "Address @handle, @everyone, or @operator";
}
async function selectRoom(id) {
  hideMentions();
  closeRoomMenu();
  const generation = ++state.generation;
  const [room, messages] = await Promise.all([api(`/api/rooms/${id}`), api(`/api/rooms/${id}/messages?before=9223372036854775807`)]);
  if (generation !== state.generation || !state.me) return;
  clearReplyTarget();
  updateRoom(room); state.agents = room.agents; state.cursor = messages.next_cursor;
  state.first = messages.messages[0]?.id || 0; state.pending = null;
  $("#messages").replaceChildren(); $("#message-text").value = ""; $("#message-error").textContent = "";
  $("#empty-state").hidden = true; $("#conversation").hidden = false;
  $("#load-older").hidden = !messages.has_more;
  appendMessages(messages.messages); renderAgents(); renderRooms();
  history.replaceState(null, "", `/${id}`); scrollBottom();
}
function scrollBottom() {const box = $("#message-scroll"); box.scrollTop = box.scrollHeight;}
function senderColors(handle) {
  let hash = 0;
  for (const char of handle) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) >>> 0;
  const hue = handle === "operator" ? 150 : hash % 360;
  return {foreground: `hsl(${hue}, 65%, 78%)`, background: `hsl(${hue}, 30%, 23%)`};
}
function senderAvatar(handle) {
  const avatar = element("div", "avatar" + (handle === "operator" ? " operator-avatar" : ""), handle.slice(0, 2).toUpperCase());
  const colors = senderColors(handle);
  avatar.style.color = colors.foreground; avatar.style.backgroundColor = colors.background;
  return avatar;
}
function updateMessageReceipts(article, receipts) {
  let container = article.querySelector(".message-receipts");
  if (!container) {
    container = element("div", "message-receipts");
    article.querySelector(".message-meta").append(container);
  }
  const snapshot = JSON.stringify(receipts || []);
  if (container.dataset.snapshot === snapshot) return;
  const expanded = new Set(Array.from(container.querySelectorAll("details[open]")).map(node => node.dataset.handle));
  const focusedHandle = container.contains(document.activeElement) ? document.activeElement.closest("details")?.dataset.handle : null;
  container.dataset.snapshot = snapshot;
  container.replaceChildren();
  if (!receipts?.length) {
    container.append(element("span", "receipt-unavailable", "Delivery tracking unavailable"));
    return;
  }
  const labels = {queued: "Queued", delivered: "Delivered", acknowledged: "Acknowledged", answered: "Answered"};
  const modes = {polling: "Polling", codex: "Codex background", claude: "Claude channel", catch_up: "Join catch-up"};
  for (const receipt of receipts) {
    const details = element("details", "recipient-receipt"); details.dataset.handle = receipt.handle;
    details.open = expanded.has(receipt.handle);
    const summary = element("summary", "receipt-status", `${receipt.handle}: ${labels[receipt.status] || "Unknown"}`);
    if (receipt.last_error || receipt.delivery_error) summary.append(element("span", "receipt-warning", " · attention"));
    details.append(summary);
    const info = element("div", "receipt-details");
    for (const [key, label] of [["queued_at", "Queued"], ["delivered_at", "Delivered"], ["acknowledged_at", "Acknowledged"], ["answered_at", "Answered"]]) {
      if (receipt[key] != null) info.append(element("div", "", `${label}: ${new Date(receipt[key] * 1000).toLocaleString()}`));
    }
    info.append(element("div", "", `Delivery: ${modes[receipt.delivery_mode] || "Awaiting transport confirmation"}`));
    if (receipt.current_mode) info.append(element("div", "", `Last reported connection: ${modes[receipt.current_mode] || receipt.current_mode} · ${receipt.delivery_state}`));
    if (receipt.delivery_updated_at != null) info.append(element("div", "", `Connection report: ${new Date(receipt.delivery_updated_at * 1000).toLocaleString()}`));
    if (receipt.revoked) info.append(element("div", "", "Agent access revoked"));
    if (receipt.reply_id) info.append(element("div", "", `Linked reply: #${receipt.reply_id}`));
    if (receipt.last_error || receipt.delivery_error) info.append(element("div", "receipt-error", receipt.last_error || receipt.delivery_error));
    info.append(element("div", "receipt-meaning", "Delivered confirms transport. Acknowledged confirms an explicit receipt. Answered confirms a linked reply."));
    details.append(info); container.append(details);
    if (focusedHandle === receipt.handle) summary.focus({preventScroll: true});
  }
}
async function refreshReceipts(roomId, generation) {
  const articles = Array.from($("#messages").querySelectorAll(".operator-message"));
  for (let offset = 0; offset < articles.length; offset += 100) {
    const batch = articles.slice(offset, offset + 100);
    let result;
    try { result = await api(`/api/rooms/${roomId}/message-receipts?ids=${batch.map(a => a.dataset.id).join(",")}`); }
    catch (error) { if (error.status === 404) return; throw error; }
    if (generation !== state.generation || !state.me) return;
    const scroll = $("#message-scroll"), stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
    for (const article of batch) updateMessageReceipts(article, result.receipts[article.dataset.id] || []);
    if (stick) scrollBottom();
  }
}
const threadViews = new WeakMap();
function clearReplyTarget() {
  state.replyTo = null; $("#reply-context").hidden = true; $("#reply-label").textContent = "";
}
function setReplyTarget(message) {
  if (!state.room || state.room.status === "archived") return;
  state.replyTo = {id: message.id, sender: message.sender}; state.pending = null;
  $("#reply-label").textContent = `Replying to ${message.sender} · #${message.id}`;
  $("#reply-context").hidden = false;
  const input = $("#message-text");
  if (!input.value.trim()) {
    let recipient = message.sender === "operator" ? (message.targets || []).find(handle => handle !== "operator") || "everyone" : message.sender;
    if (!["operator", "everyone"].includes(recipient) && !state.agents.some(a => a.handle === recipient && !a.revoked)) recipient = "everyone";
    input.value = `@${recipient} `;
  }
  input.focus();
}
$("#cancel-reply").onclick = clearReplyTarget;
async function loadThread(details) {
  const view = threadViews.get(details);
  if (!view || view.busy || !details.open || view.generation !== state.generation) return;
  view.busy = true; view.moreButton.disabled = true; view.error.textContent = "";
  try {
    const result = await api(`/api/rooms/${view.roomId}/threads/${view.messageId}?after=${view.cursor}&limit=20`);
    if (view.generation !== state.generation || !state.me || !details.isConnected) return;
    if (!view.loaded && result.root.id !== view.messageId) {
      view.body.append(element("p", "thread-root-label", "Thread started here"), renderMessage(result.root, true));
    }
    for (const message of result.messages) {
      if (!view.body.querySelector(`[data-id="${message.id}"]`)) {
        view.body.append(renderMessage(message, true)); view.count++;
      }
    }
    view.loaded = true; view.cursor = result.next_cursor; view.more = result.has_more;
    view.summary.textContent = `Thread · ${view.count}${view.more ? "+" : ""} ${view.count === 1 && !view.more ? "reply" : "replies"}`;
    view.moreButton.hidden = !view.more; view.moreButton.textContent = "Load more replies";
    if (!view.count && result.root.id === view.messageId) {
      if (!view.body.querySelector(".thread-empty")) view.body.append(element("p", "thread-empty muted", "No replies yet."));
    } else {view.body.querySelector(".thread-empty")?.remove();}
  } catch (error) {
    if (view.generation !== state.generation || !state.me) return;
    view.error.textContent = error.status === 404 ? "Thread unavailable. Update the server to enable threads." : error.message;
    view.moreButton.hidden = false; view.moreButton.textContent = "Retry";
  } finally {view.busy = false; view.moreButton.disabled = false;}
}
async function refreshThreads() {
  for (const details of $("#messages").querySelectorAll(".message-thread[open]")) {
    const view = threadViews.get(details);
    if (view && !view.more) await loadThread(details);
  }
}
function renderMessage(message, inThread = false) {
  const own = message.sender === "operator";
  const article = element("article", "message" + (own ? " operator-message" : "")); article.dataset.id = message.id;
  const avatar = senderAvatar(message.sender);
  const main = element("div", "message-main"), meta = element("div", "message-meta");
  const stamp = element("time", "message-time", timeText(message.created_at)); stamp.dateTime = new Date(message.created_at * 1000).toISOString(); stamp.title = new Date(message.created_at * 1000).toLocaleString();
  const label = element("strong", "sender-label", message.sender), colors = senderColors(message.sender);
  label.style.color = colors.foreground; label.style.backgroundColor = colors.background;
  meta.append(label, stamp);
  const bubble = element("div", "message-bubble");
  const {recipients, body} = CovenantMarkdown.splitRecipients(message.text);
  if (recipients.length) {
    const addresses = element("div", "message-recipients");
    recipients.forEach(handle => addresses.append(element("span", "mention message-recipient", "@" + handle)));
    bubble.append(addresses);
  }
  if (message.reply_to) {
    const parent = element("span", "reply-reference", `Reply to #${message.reply_to}`);
    bubble.append(parent);
  }
  const content = element("div", "markdown-content");
  // Only the bundled, HTML-disabled parser may produce message HTML.
  content.innerHTML = CovenantMarkdown.render(body);
  bubble.append(content);
  main.append(meta, bubble); article.append(avatar, main);
  if (own) updateMessageReceipts(article, message.receipts);
  const actions = element("div", "message-actions");
  const reply = element("button", "reply-button", "Reply"); reply.type = "button";
  reply.disabled = state.room?.status === "archived";
  reply.setAttribute("aria-label", `Reply to message #${message.id} from ${message.sender}`);
  reply.onclick = () => setReplyTarget(message); actions.append(reply); main.append(actions);
  if (!inThread) {
    const details = element("details", "message-thread"), summary = element("summary", "thread-summary", "View thread");
    const body = element("div", "thread-body"), error = element("p", "error thread-error");
    const more = element("button", "thread-more", "Load more replies"); more.type = "button"; more.hidden = true;
    actions.classList.add("has-thread");
    details.append(summary, body, error, more); actions.append(details);
    threadViews.set(details, {messageId: message.id, roomId: state.room.id, generation: state.generation,
      cursor: 0, loaded: false, busy: false, more: false, count: 0, body, error, moreButton: more, summary});
    details.ontoggle = () => {if (details.open) loadThread(details);};
    more.onclick = () => loadThread(details);
  }
  return article;
}
function appendMessages(messages) {
  const scroll = $("#message-scroll"), stick = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  for (const message of messages) {
    if (!$("#messages").querySelector(`:scope > [data-id="${message.id}"]`)) $("#messages").append(renderMessage(message));
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
function renderFileLocks(result) {
  const list = $("#file-lock-list"), empty = $("#file-lock-empty");
  list.replaceChildren();
  const locks = result?.locks || [];
  $("#file-lock-count").textContent = result ? `${locks.length}${result.truncated ? "+" : ""}` : "";
  $("#file-lock-limit").hidden = !result?.truncated;
  empty.hidden = locks.length > 0;
  empty.textContent = result ? "No files currently reserved." : "File-lock visibility unavailable until the server is updated.";
  for (const lock of locks) {
    const item = element("li", "file-lock-item");
    const owner = element("strong", "file-lock-owner", lock.owner);
    const colors = senderColors(lock.owner); owner.style.color = colors.foreground;
    const path = element("code", "file-lock-path", lock.path); path.title = lock.path;
    const seconds = Math.max(0, Math.ceil(lock.expires_at - result.server_time));
    const minutes = Math.floor(seconds / 60), remainder = seconds % 60;
    const remaining = minutes ? `${minutes}m ${remainder}s` : `${seconds}s`;
    const expiry = element("time", "file-lock-expiry", `Expires in ${remaining}`);
    expiry.dateTime = new Date(lock.expires_at * 1000).toISOString();
    expiry.title = `Expires ${new Date(lock.expires_at * 1000).toLocaleString()}`;
    const exact = element("span", "file-lock-exact", new Date(lock.expires_at * 1000).toLocaleTimeString());
    item.append(owner, path, expiry, exact); list.append(item);
  }
}
function renderAgents() {
  const list = $("#agent-list"); list.replaceChildren();
  $("#participant-count").textContent = state.agents.filter(a => !a.revoked).length + 1;
  for (const agent of state.agents) {
    const row = element("div", "participant" + (agent.revoked ? " revoked" : ""));
    const info = element("div", "participant-info"); info.append(element("strong", "", "@" + agent.handle), element("small", "", seenText(agent)));
    row.append(senderAvatar(agent.handle), info); list.append(row);
    if (state.room.status !== "archived") {
      const controls = element("div", "participant-controls");
      const renew = element("button", "", "Renew token"); renew.setAttribute("aria-label", `Renew token for ${agent.handle}`);
      renew.onclick = () => renewAgent(agent); controls.append(renew);
      if (!agent.revoked) {const revoke = element("button", "", "Revoke"); revoke.setAttribute("aria-label", `Revoke ${agent.handle}`); revoke.onclick = () => revokeAgent(agent); controls.append(revoke);}
      list.append(controls);
    }
  }
}
async function roomAction(status, target = state.room) {
  if (!target) return; const id = target.id;
  if (status === "archived" && !confirm("Archive this conversation? Messages will remain available and agents will stop their polling loops.")) return;
  try {const room = await api(`/api/rooms/${id}`, {method: "PATCH", body: JSON.stringify({status})}); if (state.room?.id === id) {updateRoom(room); renderAgents();} await setFilter(status === "archived"); toast(status === "active" ? "Conversation active" : `Conversation ${status}`);}
  catch (error) {toast(error.message);}
}
$("#pause-room").onclick = () => roomAction(state.room.status === "paused" ? "active" : "paused");
$("#archive-room").onclick = () => roomAction(state.room.status === "archived" ? "active" : "archived");
function clearConversation() {
  clearReplyTarget();
  hideMentions();
  state.generation++; state.room = null; state.agents = []; state.cursor = 0; state.first = 0; state.pending = null; applyPanelPreferences();
  $("#messages").replaceChildren(); $("#agent-list").replaceChildren(); $("#message-text").value = "";
  $("#invite-text").value = ""; document.querySelectorAll("dialog[open]").forEach(d => d.close());
  $("#conversation").hidden = true; $("#empty-state").hidden = false;
  history.replaceState(null, "", "/"); renderRooms();
}
const deletingRooms = new Set();
async function deleteRoom(target = state.room) {
  if (!target || deletingRooms.has(target.id)) return;
  const {id, title} = target;
  if (!confirm(`Permanently delete "${title}"? All messages and agent access will be removed. This cannot be undone.`)) return;
  deletingRooms.add(id);
  const button = $("#delete-room"); button.disabled = true;
  try {
    if (target.status !== "archived") {
      const archived = await api(`/api/rooms/${id}`, {method: "PATCH", body: JSON.stringify({status: "archived"})});
      if (state.room?.id === id) {updateRoom(archived); renderAgents();}
    }
    await api(`/api/rooms/${id}`, {method: "DELETE"});
    if (state.room?.id === id) clearConversation();
    await refreshRooms(); toast("Conversation permanently deleted");
  } catch (error) {toast(error.message);} finally {deletingRooms.delete(id); button.disabled = false;}
}
$("#delete-room").onclick = () => deleteRoom();
let roomMenuTarget = null;
function closeRoomMenu(restoreFocus = false) {
  const id = roomMenuTarget?.id;
  $("#room-menu").hidden = true; roomMenuTarget = null;
  if (restoreFocus && id) document.querySelector(`[data-room-id="${id}"]`)?.focus();
}
function openRoomMenu(room, x, y) {
  roomMenuTarget = room;
  const menu = $("#room-menu");
  $("#menu-archive").textContent = room.status === "archived" ? "Restore" : "Archive";
  $("#menu-delete").disabled = deletingRooms.has(room.id);
  menu.setAttribute("aria-label", `Actions for ${room.title}`); menu.hidden = false;
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  $("#menu-archive").focus();
}
$("#menu-archive").onclick = () => {
  const room = roomMenuTarget; closeRoomMenu(true);
  if (room) roomAction(room.status === "archived" ? "active" : "archived", room);
};
$("#menu-delete").onclick = () => {
  const room = roomMenuTarget; closeRoomMenu(true);
  if (room) deleteRoom(room);
};
document.addEventListener("pointerdown", event => {
  if (!$("#room-menu").contains(event.target)) closeRoomMenu();
});
document.addEventListener("keydown", event => {
  if ($("#room-menu").hidden) return;
  if (event.key === "Escape") {event.preventDefault(); closeRoomMenu(true);}
  else if (event.key === "Tab") closeRoomMenu();
  else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const items = [...$("#room-menu").querySelectorAll("button:not(:disabled)")];
    const current = items.indexOf(document.activeElement);
    const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 :
      (current + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
    items[index]?.focus();
  }
});
$("#room-list").addEventListener("scroll", () => closeRoomMenu());
window.addEventListener("resize", () => closeRoomMenu());
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
const mentionState = {matches: [], index: 0, range: null};
function mentionRange(text, start, end) {
  if (start !== end) return null;
  const match = text.slice(0, start).match(/(?:^|\s)@([a-z0-9_]*)$/i);
  if (!match) return null;
  return {start: start - match[1].length - 1,
    end: start + text.slice(start).match(/^[a-z0-9_]*/i)[0].length, query: match[1].toLowerCase()};
}
function hideMentions() {
  $("#mention-picker").hidden = true; $("#mention-options").replaceChildren();
  $("#message-text").removeAttribute("aria-activedescendant");
  mentionState.matches = []; mentionState.range = null; mentionState.index = 0;
}
function updateMentions() {
  const input = $("#message-text");
  const range = mentionRange(input.value, input.selectionStart, input.selectionEnd);
  if (document.activeElement !== input || !state.room || input.disabled || !range) {hideMentions(); return;}
  const handles = [...new Set([...state.agents.filter(a => !a.revoked).map(a => a.handle).sort(), "everyone", "operator"])];
  const matches = handles.filter(handle => handle.startsWith(range.query));
  if (!matches.length) {hideMentions(); return;}
  const selected = mentionState.matches[mentionState.index];
  mentionState.matches = matches; mentionState.range = range;
  mentionState.index = Math.max(0, matches.indexOf(selected));
  const list = $("#mention-options"); list.replaceChildren();
  matches.forEach((handle, index) => {
    const option = element("button", "mention-option"); option.type = "button"; option.tabIndex = -1;
    option.id = `mention-option-${index}`; option.setAttribute("role", "option");
    option.append(senderAvatar(handle), element("span", "", "@" + handle));
    option.addEventListener("pointerdown", event => event.preventDefault());
    option.onclick = () => completeMention(index); list.append(option);
  });
  $("#mention-picker").hidden = false; selectMention(mentionState.index);
}
function selectMention(index) {
  mentionState.index = index;
  [...$("#mention-options").children].forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
  const selected = $("#mention-options").children[index];
  $("#message-text").setAttribute("aria-activedescendant", selected.id);
  selected.scrollIntoView({block: "nearest"});
}
function completeMention(index = mentionState.index) {
  const input = $("#message-text"), handle = mentionState.matches[index];
  const range = mentionRange(input.value, input.selectionStart, input.selectionEnd);
  if (!handle || !range || !state.room || input.disabled) {hideMentions(); return;}
  const tail = input.value.slice(range.end), separator = /^\s/.test(tail) ? "" : " ";
  const replacement = "@" + handle + separator;
  const value = input.value.slice(0, range.start) + replacement + tail;
  if (value.length > input.maxLength) {$("#message-error").textContent = "Message is too long to complete this recipient."; return;}
  input.value = value;
  const cursor = range.start + replacement.length + (tail.startsWith(" ") ? 1 : 0);
  input.setSelectionRange(cursor, cursor); hideMentions(); input.focus();
}
$("#message-text").addEventListener("input", updateMentions);
$("#message-text").addEventListener("click", updateMentions);
$("#message-text").addEventListener("focus", updateMentions);
$("#message-text").addEventListener("blur", hideMentions);
$("#message-text").addEventListener("keyup", event => {
  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) updateMentions();
});
$("#message-form").addEventListener("submit", async (event) => {
  event.preventDefault(); hideMentions(); const room = state.room, text = $("#message-text").value.trim(); if (!room || !text) return;
  const replyTo = state.replyTo?.id || null;
  if (!state.pending || state.pending.text !== text || state.pending.room !== room.id || state.pending.reply_to !== replyTo) state.pending = {room: room.id, text, reply_to: replyTo, client_message_id: crypto.randomUUID()};
  const pending = state.pending; $("#send-message").disabled = true; $("#message-error").textContent = "";
  try {await api(`/api/rooms/${room.id}/messages`, {method: "POST", body: JSON.stringify({text, client_message_id: pending.client_message_id, ...(pending.reply_to ? {reply_to: pending.reply_to} : {})})});
    if (state.room?.id === room.id) {if ($("#message-text").value.trim() === text && (state.replyTo?.id || null) === pending.reply_to) {$("#message-text").value = ""; clearReplyTarget();} if (state.pending === pending) state.pending = null; await refreshCurrent(); scrollBottom();}
  } catch(error) {if (state.room?.id === room.id) $("#message-error").textContent = error.message;}
  finally {$("#send-message").disabled = state.room?.status === "archived";}
});
$("#message-text").addEventListener("keydown", event => {
  if (event.isComposing) return;
  if (!$("#mention-picker").hidden) {
    if ((event.key === "Tab" || event.key === "Enter") && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); completeMention(); return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); selectMention((mentionState.index + (event.key === "ArrowDown" ? 1 : -1) + mentionState.matches.length) % mentionState.matches.length); return;
    }
    if (event.key === "Escape") {event.preventDefault(); hideMentions(); return;}
  }
  if (event.key === "Enter" && !event.shiftKey) {event.preventDefault(); if (!$("#send-message").disabled) $("#message-form").requestSubmit();}
});
async function refreshCurrent() {
  if (!state.room || !state.me) return;
  const id = state.room.id, generation = state.generation;
  let room, result;
  try { [room, result] = await Promise.all([api(`/api/rooms/${id}`), api(`/api/rooms/${id}/messages?after=${state.cursor}`)]); }
  catch (error) {
    if (generation !== state.generation || !state.me) return;
    if (error.status === 404) {clearConversation(); await refreshRooms(); return;}
    throw error;
  }
  if (generation !== state.generation || !state.me) return;
  updateRoom(room); state.agents = room.agents; renderAgents();
  if (!$("#mention-picker").hidden) updateMentions();
  appendMessages(result.messages); state.cursor = Math.max(state.cursor, result.next_cursor);
  await refreshThreads();
  if (generation !== state.generation) return;
  await refreshReceipts(id, generation);
}
async function refresh() {
  if (!state.me || state.loading) return; state.loading = true;
  try {await Promise.all([refreshRooms(), refreshCurrent(), refreshNotifications()]); $("#connection-error").hidden = true;}
  catch(error) {if (state.me) {$("#connection-error").textContent = "Connection interrupted. Retrying automatically…"; $("#connection-error").hidden = false;}}
  finally {state.loading = false;}
}
setInterval(refresh, 2000);
(async () => {try {await enterWorkspace();} catch {showLogin();} try {const status = await api("/api/status"); $("#setup-note").hidden = status.configured;} catch {$("#login-error").textContent = "Cannot reach the server. Refresh when it is running.";}})();

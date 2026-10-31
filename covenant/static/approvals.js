"use strict";
// Independent polling: an older server or an approval outage never blocks chat refresh.
(() => {
  const button = $("#approvals-toggle"), dialog = $("#approvals-dialog"), list = $("#approvals-list");
  let busy = false, signature = "", known = new Set(), epoch = null;
  const labels = {pending: "Waiting for you", decided: "Decision queued", submitted: "Decision sent · awaiting harness", resolved: "Request closed", cancelled: "Cancelled", expired: "Expired"};
  const connectionStates = new Map();
  function paintConnection(node) {
    const connection = connectionStates.get(node.dataset.agentId);
    const label = {connected: "connected", ready: "ready", connecting: "connecting", disconnected: "disconnected"}[connection?.state] || "setup required";
    node.textContent = `Approvals: ${label}`;
    node.title = connection?.detail || (connection?.state === "connected" ? "Native approval forwarding is connected." : "Codex needs a registered session. Claude needs covenant setup-claude once, then a room join.");
  }
  function updateConnections(connections) {
    connectionStates.clear();
    for (const connection of connections) connectionStates.set(connection.agent_id, connection);
    $("#agent-list").querySelectorAll(".approval-connection").forEach(paintConnection);
  }
  globalThis.CovenantApprovalConnections = {paint: paintConnection};
  const roomPendingCounts = new Map();
  function paintRoomApproval(button) {
    const count = roomPendingCounts.get(button.dataset.roomId) || 0;
    const dot = button.querySelectorAll(".room-approval-dot")[0];
    if (!dot) return;
    dot.hidden = count === 0;
    const description = count ? `${count} pending approval${count === 1 ? "" : "s"} need your decision` : "";
    dot.title = description;
    button.setAttribute("aria-description", description);
  }
  function updateRoomApprovals(requests) {
    roomPendingCounts.clear();
    for (const request of requests) {
      if (request.status === "pending" && request.room_id) {
        roomPendingCounts.set(request.room_id, (roomPendingCounts.get(request.room_id) || 0) + 1);
      }
    }
    $("#room-list").querySelectorAll(".room-item").forEach(paintRoomApproval);
  }
  globalThis.CovenantRoomApprovals = {paint: paintRoomApproval};
  const expiredNotice = $("#expired-approvals-notice");
  expiredNotice.onclick = () => {dialog.showModal(); return refreshApprovals();};
  let currentRequests = [];
  const localDecisions = new Map(), submitting = new Set();
  const requestDialog = $("#approval-request-dialog"), requestBody = $("#approval-request-body");
  const requestActions = $("#approval-request-actions"), requestError = $("#approval-request-error");
  let selectedId = null, selectedSignature = "", unavailable = false;
  const autoApprove = $("#auto-approve-room"), policyHelp = $("#auto-approve-help");
  let policies = null, policyAvailable = false, policyPending = null, policyRevision = 0;
  function renderPolicy() {
    const room = state.room;
    const enabled = Boolean(room && policies?.[room.id]);
    autoApprove.checked = policyPending && policyPending.room === room?.id ? policyPending.enabled : enabled;
    autoApprove.disabled = !state.me || !room || !policyAvailable || Boolean(policyPending) || (room.status !== "active" && !enabled);
    policyHelp.textContent = !state.me || !room ? "Select a conversation" : policyPending?.room === room.id ? "Saving…" : !policyAvailable ? "Setting unavailable · reconnect or restart server" : enabled ? "On for this room · until switched off" : "This room · pending and future requests";
  }
  autoApprove.onchange = async () => {
    if (!state.me || !state.room || !policyAvailable || policyPending) {renderPolicy(); return;}
    const user = state.me, room = state.room.id, enabled = autoApprove.checked;
    policyRevision++; policyPending = {room, enabled}; renderPolicy();
    try {
      const result = await api(`/api/rooms/${room}/approval-policy`, {method: "PATCH", body: JSON.stringify({auto_approve: enabled})});
      if (state.me !== user) return;
      policies = {...policies, [room]: result.auto_approve};
      toast(result.auto_approve ? `Auto-approve enabled for this room · ${result.approved_count} pending requests approved` : "Auto-approve disabled for this room");
    } catch (error) {
      if (state.me === user) {policyAvailable = false; toast(error.message);}
    } finally {
      if (state.me === user) {policyPending = null; policyRevision++; renderPolicy(); await refreshApprovals();}
    }
  };
  requestDialog.onclose = () => {
    const previous = selectedId;
    selectedId = null; selectedSignature = "";
    requestBody.replaceChildren(); requestActions.replaceChildren(); requestError.hidden = true;
    if (state.me) {
      const rows = Array.from(pendingList.querySelectorAll(".approval-row"));
      (rows.find(row => row.dataset.approvalId === previous) || rows[0] || collapse).focus();
    }
  };
  const pendingList = $("#pending-approvals"), collapse = $("#toggle-approvals");
  collapse.onclick = () => {
    pendingList.hidden = !pendingList.hidden;
    collapse.setAttribute("aria-expanded", String(!pendingList.hidden));
    collapse.title = collapse.getAttribute("aria-label") === "Hide pending approvals" ? "Show pending approvals" : "Hide pending approvals";
    collapse.setAttribute("aria-label", collapse.title);
  };
  function showPendingCount(count) {
    $("#approvals-count").textContent = String(count);
    for (const selector of ["#reopen-participants", "#toggle-participants", "#toggle-approvals", "#mobile-details"]) {
      const control = $(selector);
      control.dataset.approvalPending = String(count > 0);
      const expired = currentRequests.filter(r => r.status === "expired" && !r.dismissed_at).length;
      control.setAttribute("aria-description", [count ? `${count} pending approval${count === 1 ? "" : "s"} need your decision` : "", expired ? `${expired} expired approvals to review` : ""].filter(Boolean).join(". "));
    }
    globalThis.CovenantMobile?.setApprovalCount(count);
  }
  function createCard(request, history = false, expanded = false, actionTarget = null) {
    const card = element(history ? "details" : "article", "approval-card");
    card.dataset.approvalId = request.id;
    if (history) card.open = expanded;
    const summary = element(history ? "summary" : "div", "approval-summary");
    summary.append(element("strong", "", `${request.handle} · ${request.harness}`));
    summary.append(element("span", "muted", `${request.details.tool_name || request.kind} · ${request.room_title}`));
    card.append(summary);
    const status = `${labels[request.status] || request.status}${request.decision ? " · " + (request.auto_approved ? "Auto-approved" : request.decision === "approve" ? "Approved once" : "Rejected") : ""}`;
    card.append(element("p", "approval-status", status));
    if (request.status === "pending" && request.expires_at) card.append(element("p", "muted", `Expires at ${timeText(request.expires_at)}`));
    if (request.status === "expired") {
      summary.append(element("span", "danger", "Expired"));
      card.append(element("p", "approval-status", request.native_resolved ? "The harness confirmed this request is closed." : "Native resolution is unconfirmed. Check the agent’s native approval prompt; it may still be waiting."));
      if (!request.dismissed_at) {
        const dismiss = element("button", "quiet", "Dismiss notice"); dismiss.type = "button";
        dismiss.onclick = async () => {
          try {await api(`/api/approvals/${request.id}/dismiss`, {method: "POST"}); signature = ""; await refreshApprovals();}
          catch (error) {toast(error.message);}
        };
        card.append(dismiss);
      }
    }
    const content = request.details;
    const preview = content.command || content.tool_input?.command || content.item?.command;
    if (content.tool_name) card.append(element("strong", "approval-tool", content.tool_name));
    if (content.reason) card.append(element("p", "", content.reason));
    card.append(element("pre", "approval-preview", preview || JSON.stringify(content.tool_input || content.item?.changes || content.networkApprovalContext || content, null, 2)));
    if (content.cwd) card.append(element("p", "approval-path muted", content.cwd));
    for (const key of ["additionalPermissions", "networkApprovalContext", "grantRoot"]) {
      if (content[key]) card.append(element("pre", "approval-preview", `Requested access: ${JSON.stringify(content[key], null, 2)}`));
    }
    if (content.rejection_effect) card.append(element("p", "muted", content.rejection_effect));
    const detail = element("details", "approval-details");
    detail.append(element("summary", "", `Full request · ${timeText(request.created_at)}`));
    detail.append(element("pre", "", JSON.stringify(request.details, null, 2)));
    detail.append(element("small", "muted", `Session: ${request.session_id}`));
    card.append(detail);
    if (request.status === "pending") {
      const actions = element("div", "approval-actions");
      for (const [decision, label] of [["approve", "Approve once"], ["reject", "Reject"]]) {
        const control = element("button", decision === "approve" ? "primary" : "quiet danger", label);
        control.type = "button"; control.dataset.approvalAction = `${request.id}:${decision}`;
        control.disabled = decision === "approve" && !request.can_approve;
        if (control.disabled) control.title = "Complete details or this decision are unavailable; use the harness prompt";
        control.disabled ||= submitting.has(request.id) || unavailable;
        control.onclick = () => decideRequest(request, decision);
        actions.append(control);
      }
      if (!request.can_approve) actions.append(element("small", "muted", "Approve from the harness after reviewing its full prompt."));
      (actionTarget || card).append(actions);
    }
    return card;
  }
  async function decideRequest(request, decision) {
    if (!state.me || unavailable || submitting.has(request.id)) return;
    const user = state.me;
    submitting.add(request.id); selectedSignature = ""; renderSelected();
    try {
      await api(`/api/approvals/${request.id}/decision`, {method: "POST", body: JSON.stringify({decision})});
      if (state.me !== user) return;
      localDecisions.set(request.id, decision);
      render(currentRequests.map(item => item.id === request.id ? {...item, status: "decided", decision} : item));
      if (selectedId === request.id && requestDialog.open) requestDialog.close();
      toast(decision === "approve" ? "Approval queued for the harness" : "Rejection queued for the harness");
    } catch (error) {
      if (state.me === user) {
        toast(error.message);
        requestError.textContent = error.message; requestError.hidden = false;
      }
    } finally {
      submitting.delete(request.id);
      if (state.me === user) {signature = ""; selectedSignature = ""; await refreshApprovals();}
    }
  }
  function renderSelected() {
    if (!selectedId) return;
    const request = currentRequests.find(item => item.id === selectedId);
    const next = JSON.stringify([request || null, submitting.has(selectedId), unavailable]);
    if (next === selectedSignature) return;
    const scroll = requestBody.scrollTop;
    const focused = document.activeElement?.dataset?.approvalAction;
    const expanded = requestBody.querySelectorAll(".approval-details")[0]?.open;
    selectedSignature = next;
    requestBody.replaceChildren(); requestActions.replaceChildren();
    if (!request) {
      requestBody.append(element("p", "muted", "This approval has ended or is no longer available."));
      return;
    }
    requestBody.append(createCard(request, false, false, requestActions));
    const detail = requestBody.querySelectorAll(".approval-details")[0];
    if (detail) detail.open = Boolean(expanded);
    requestBody.scrollTop = scroll;
    if (focused) Array.from(requestActions.querySelectorAll("button")).find(b => b.dataset.approvalAction === focused && !b.disabled)?.focus();
  }
  function openRequest(id) {
    if (!state.me) return;
    if (selectedId !== id) requestBody.scrollTop = 0;
    selectedId = id; selectedSignature = "";
    requestError.hidden = !unavailable;
    if (unavailable) requestError.textContent = "Approval connection unavailable. Refreshing automatically.";
    renderSelected();
    if (!requestDialog.open) requestDialog.showModal();
  }
  function render(requests) {
    const next = JSON.stringify(requests);
    if (next === signature) {renderSelected(); return;}
    const focusedId = document.activeElement?.dataset?.approvalId;
    const expanded = new Set([...list.querySelectorAll(".approval-card")].filter(card => card.open).map(card => card.dataset.approvalId));
    currentRequests = requests;
    signature = next; list.replaceChildren(); pendingList.replaceChildren();
    const pending = requests.filter(r => r.status === "pending");
    const expired = requests.filter(r => r.status === "expired" && !r.dismissed_at);
    expiredNotice.hidden = !expired.length;
    expiredNotice.textContent = `${expired.length} expired approval${expired.length === 1 ? "" : "s"} · Review`;
    for (const selector of ["#reopen-participants", "#toggle-participants", "#toggle-approvals", "#mobile-details"]) {
      $(selector).dataset.approvalExpired = String(expired.length > 0);
    }
    showPendingCount(pending.length); updateRoomApprovals(pending);
    if (!pending.length) pendingList.append(element("p", "muted", "No pending approvals."));
    for (const request of requests) {
      if (request.status !== "pending") {list.append(createCard(request, true, expanded.has(request.id))); continue;}
      const row = element("button", "approval-row"); row.type = "button";
      row.dataset.approvalId = request.id;
      row.setAttribute("aria-haspopup", "dialog"); row.setAttribute("aria-controls", "approval-request-dialog");
      row.append(element("strong", "", `${request.handle} · ${request.harness}`));
      row.append(element("span", "muted", `${request.details.tool_name || request.kind} · ${request.room_title}`));
      row.title = "Open approval request";
      row.disabled = unavailable;
      row.onclick = () => openRequest(request.id);
      pendingList.append(row);
    }
    if (!list.children.length) list.append(element("p", "muted", "No completed requests. Pending approvals appear in the right panel."));
    if (focusedId && !requestDialog.open) Array.from(pendingList.querySelectorAll(".approval-row")).find(row => row.dataset.approvalId === focusedId)?.focus();
    renderSelected();
  }
  async function refreshApprovals() {
    if (!state.me) {
      expiredNotice.hidden = true;
      for (const selector of ["#reopen-participants", "#toggle-participants", "#toggle-approvals", "#mobile-details"]) $(selector).dataset.approvalExpired = "false";
      updateConnections([]);
      updateRoomApprovals([]);
      signature = ""; known.clear(); localDecisions.clear(); currentRequests = []; epoch = null; showPendingCount(0); list.replaceChildren(); pendingList.replaceChildren();
      selectedId = null; selectedSignature = ""; submitting.clear(); unavailable = false;
      policies = null; policyAvailable = false; policyPending = null; policyRevision++; renderPolicy();
      requestBody.replaceChildren(); requestActions.replaceChildren();
      if (requestDialog.open) requestDialog.close();
      if (dialog.open) dialog.close();
      return;
    }
    if (busy) return;
    const user = state.me, policyVersion = policyRevision;
    if (epoch !== user) {known.clear(); signature = ""; epoch = user;}
    busy = true;
    try {
      const {requests, connections = [], auto_approve_rooms} = await api("/api/approvals");
      if (state.me !== user) return;
      if (policyVersion === policyRevision && !policyPending) {
        policyAvailable = auto_approve_rooms != null;
        if (policyAvailable) policies = auto_approve_rooms;
      }
      renderPolicy();
      unavailable = false; requestError.hidden = true;
      updateConnections(connections);
      $("#approvals-panel").hidden = false;
      $("#approvals-error").hidden = true;
      const pending = requests.filter(r => r.status === "pending");

      for (const request of pending) {
        if (!known.has(request.id) && !globalThis.CovenantPush?.active && notificationsEnabled && "Notification" in window && Notification.permission === "granted") {
          try {
            const notification = new Notification(`Covenant · Approval needed`, {body: `${request.handle} in ${request.room_title}`, tag: `approval-${request.id}`, icon: "/static/icon.png"});
            visibleNotifications.add(notification);
            notification.onclose = () => visibleNotifications.delete(notification);
            notification.onclick = () => {notification.close(); if (state.me === user) {window.focus(); setFilter(false).then(() => selectRoom(request.room_id)).then(() => {if (globalThis.CovenantMobile?.isMobile) globalThis.CovenantMobile.open("participants"); else {panelPreferences.participants = false; applyPanelPreferences();} if (pendingList.hidden) collapse.click(); openRequest(request.id);}).catch(error => toast(error.message));}};
          } catch {}
        }
        known.add(request.id);
      }
      render(requests.map(request => {
        if (request.status === "pending" && localDecisions.has(request.id)) return {...request, status: "decided", decision: localDecisions.get(request.id)};
        if (request.status !== "pending") localDecisions.delete(request.id);
        return request;
      }));
    } catch (error) {
      if (!state.me || state.me !== user) return;
      updateConnections([...connectionStates.values()].map(c => ({...c, state: "disconnected", detail: "Cannot reach Covenant approval forwarding."})));
      unavailable = true; policyAvailable = false; renderPolicy();
      requestError.textContent = "Approval connection unavailable. Refreshing automatically."; requestError.hidden = false;
      selectedSignature = ""; renderSelected();
      pendingList.querySelectorAll("button").forEach(b => b.disabled = true);
      if (error.status === 404) {updateRoomApprovals([]); $("#approvals-panel").hidden = true; showPendingCount(0); signature = "";}
      if (error.status !== 404) {
        $("#approvals-error").textContent = "Approval connection unavailable. Refreshing automatically; the native harness remains available.";
        $("#approvals-error").hidden = false;
        pendingList.querySelectorAll("button").forEach(b => b.disabled = true); signature = "";
      }
    } finally {busy = false;}
  }
  globalThis.CovenantApprovals = {renderPolicy, open: async (id, room) => {
    if (!/^[0-9a-f-]{36}$/i.test(id) || !/^[0-9a-f-]{36}$/i.test(room)) return;
    await setFilter(false); await selectRoom(room); await refreshApprovals();
    if (globalThis.CovenantMobile?.isMobile) globalThis.CovenantMobile.open("participants");
    else {panelPreferences.participants = false; applyPanelPreferences();}
    if (pendingList.hidden) collapse.click();
    const request = currentRequests.find(r => r.id === id);
    openRequest(id);
    if (!request) toast("This approval has ended or is no longer available.");
  }};
  window.addEventListener("covenant-login-change", refreshApprovals);
  button.onclick = () => {dialog.showModal(); refreshApprovals();};
  renderPolicy();
  setInterval(refreshApprovals, 2000);
})();

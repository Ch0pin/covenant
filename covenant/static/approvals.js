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
  let currentRequests = [];
  const localDecisions = new Map();
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
      control.setAttribute("aria-description", count ? `${count} pending approval${count === 1 ? "" : "s"} need your decision` : "");
    }
    globalThis.CovenantMobile?.setApprovalCount(count);
  }
  function render(requests) {
    const next = JSON.stringify(requests);
    if (next === signature) return;
    const focused = document.activeElement?.dataset?.approvalAction;
    const expanded = new Set([...pendingList.querySelectorAll(".approval-card"), ...list.querySelectorAll(".approval-card")].filter(card => card.open).map(card => card.dataset.approvalId));
    currentRequests = requests;
    signature = next; list.replaceChildren(); pendingList.replaceChildren();
    const pending = requests.filter(r => r.status === "pending");
    showPendingCount(pending.length);
    updateRoomApprovals(pending);
    if (!pending.length) pendingList.append(element("p", "muted", "No pending approvals."));
    for (const request of requests) {
      const card = element("details", "approval-card");
      card.dataset.approvalId = request.id; card.open = expanded.has(request.id);
      const summary = element("summary", "approval-summary");
      summary.append(element("strong", "", `${request.handle} · ${request.harness}`));
      summary.append(element("span", "muted", `${request.details.tool_name || request.kind} · ${request.room_title}`));
      card.append(summary);
      const status = `${labels[request.status] || request.status}${request.decision ? " · " + (request.decision === "approve" ? "Approved once" : "Rejected") : ""}`;
      card.append(element("p", "approval-status", status));
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
          control.onclick = async () => {
            actions.querySelectorAll("button").forEach(b => b.disabled = true);
            try {
              await api(`/api/approvals/${request.id}/decision`, {method: "POST", body: JSON.stringify({decision})});
              localDecisions.set(request.id, decision);
              render(currentRequests.map(item => item.id === request.id ? {...item, status: "decided", decision} : item));
              toast(decision === "approve" ? "Approval queued for the harness" : "Rejection queued for the harness");
            } catch (error) {toast(error.message);}
            finally {signature = ""; await refreshApprovals();}
          };
          actions.append(control);
        }
        if (!request.can_approve) actions.append(element("small", "muted", "Approve from the harness after reviewing its full prompt."));
        card.append(actions);
      }
      if (request.status === "pending") pendingList.append(card);
      else list.append(card);
    }
    if (!list.children.length) list.append(element("p", "muted", "No completed requests. Pending approvals appear in the right panel."));
    if (focused) Array.from(pendingList.querySelectorAll("button")).find(b => b.dataset.approvalAction === focused && !b.disabled)?.focus();
  }
  async function refreshApprovals() {
    if (!state.me) {
      updateConnections([]);
      updateRoomApprovals([]);
      signature = ""; known.clear(); localDecisions.clear(); currentRequests = []; epoch = null; showPendingCount(0); list.replaceChildren(); pendingList.replaceChildren();
      if (dialog.open) dialog.close();
      return;
    }
    if (busy) return;
    const user = state.me;
    if (epoch !== user) {known.clear(); signature = ""; epoch = user;}
    busy = true;
    try {
      const {requests, connections = []} = await api("/api/approvals");
      if (state.me !== user) return;
      updateConnections(connections);
      $("#approvals-panel").hidden = false;
      $("#approvals-error").hidden = true;
      const pending = requests.filter(r => r.status === "pending");

      for (const request of pending) {
        if (!known.has(request.id) && notificationsEnabled && "Notification" in window && Notification.permission === "granted") {
          try {
            const notification = new Notification(`Covenant · Approval needed`, {body: `${request.handle} in ${request.room_title}`, tag: `approval-${request.id}`, icon: "/static/icon.png"});
            visibleNotifications.add(notification);
            notification.onclose = () => visibleNotifications.delete(notification);
            notification.onclick = () => {notification.close(); if (state.me === user) {window.focus(); setFilter(false).then(() => selectRoom(request.room_id)).then(() => {if (globalThis.CovenantMobile?.isMobile) globalThis.CovenantMobile.open("participants"); else {panelPreferences.participants = false; applyPanelPreferences();} if (pendingList.hidden) collapse.click();}).catch(error => toast(error.message));}};
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
      if (error.status === 404) {updateRoomApprovals([]); $("#approvals-panel").hidden = true; showPendingCount(0); signature = "";}
      if (error.status !== 404) {
        $("#approvals-error").textContent = "Approval connection unavailable. Refreshing automatically; the native harness remains available.";
        $("#approvals-error").hidden = false;
        pendingList.querySelectorAll("button").forEach(b => b.disabled = true); signature = "";
      }
    } finally {busy = false;}
  }
  window.addEventListener("covenant-login-change", refreshApprovals);
  button.onclick = () => {dialog.showModal(); refreshApprovals();};
  setInterval(refreshApprovals, 2000);
})();

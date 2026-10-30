"use strict";
// Push only: do not cache authenticated pages, credentials, or chat history.
self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
self.addEventListener("push", event => {
  event.waitUntil((async () => {
    let data;
    try {data = event.data.json();} catch {return;}
    if (!uuid.test(data.room_id) || !uuid.test(data.approval_id) || !["pending", "expired"].includes(data.kind)) return;
    if (data.kind === "pending" && data.expires_at * 1000 <= Date.now()) return;
    try {
      const response = await fetch("/api/approvals", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(5000)});
      if (response.status === 401) return;
      if (response.ok) {
        const request = (await response.json()).requests.find(r => r.id === data.approval_id);
        if (!request || request.status !== data.kind || request.dismissed_at) return;
      }
    } catch { /* The notification can still open the server when connectivity returns. */ }
    await self.registration.showNotification(data.kind === "pending" ? "Covenant · Approval needed" : "Covenant · Approval expired", {
      body: data.kind === "pending" ? "An agent needs your decision. Tap to review." : "An approval expired. Tap to check the session’s status.",
      icon: "/static/icon-192.png", badge: "/static/icon-192.png", tag: `approval-${data.approval_id}`,
      data: {room_id: data.room_id, approval_id: data.approval_id},
    });
  })());
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const data = event.notification.data || {};
  if (!uuid.test(data.room_id) || !uuid.test(data.approval_id)) return;
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const client = clients.find(c => new URL(c.url).origin === self.location.origin);
    if (client) {
      await client.focus(); client.postMessage({type: "covenant-approval", ...data});
    } else {
      await self.clients.openWindow(`/${data.room_id}?approval=${data.approval_id}`);
    }
  })());
});

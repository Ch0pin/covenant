"use strict";
(() => {
  const button = $("#push-toggle");
  const supported = window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  let enabled = false, active = false, busy = false, registration, connectionError = "";
  try {enabled = localStorage.getItem("covenant-approval-push") === "on";} catch {}
  function remember(value) {
    enabled = value;
    try {localStorage.setItem("covenant-approval-push", value ? "on" : "off");} catch {}
  }
  function paint() {
    button.disabled = !supported || busy;
    button.textContent = active ? "Disable approval alerts" : "Enable approval alerts";
    button.setAttribute("aria-pressed", String(active));
    button.title = connectionError || (supported ? "Background push alerts for pending and expired approvals on this device" : "Use HTTPS and a supported browser. On iPhone, add Covenant to the Home Screen first.");
  }
  async function worker() {
    if (!registration) {
      await navigator.serviceWorker.register("/service-worker.js", {scope: "/"});
      registration = await navigator.serviceWorker.ready;
    }
    return registration;
  }
  function decodeKey(value) {
    return Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4)), c => c.charCodeAt(0));
  }
  async function subscribe() {
    const user = state.me;
    const config = await api("/api/push/config"), reg = await worker();
    let subscription = await reg.pushManager.getSubscription();
    const key = decodeKey(config.public_key);
    if (subscription?.options.applicationServerKey && (subscription.options.applicationServerKey.byteLength !== key.length || !Array.from(new Uint8Array(subscription.options.applicationServerKey)).every((v, i) => v === key[i]))) {
      await subscription.unsubscribe(); subscription = null;
    }
    subscription ||= await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
    if (state.me !== user || !state.me) return;
    await api("/api/push/subscriptions", {method: "POST", body: JSON.stringify(subscription.toJSON())});
    if (state.me !== user) return;
    active = true; connectionError = ""; remember(true);
  }
  async function sync() {
    active = false;
    if (!state.me || !supported || !enabled || Notification.permission !== "granted") {paint(); return;}
    if (busy) return;
    busy = true; paint();
    try {await subscribe();} catch {connectionError = "Approval push is disconnected. Select Enable approval alerts to reconnect.";}
    finally {busy = false; paint();}
  }
  button.onclick = async () => {
    if (busy || !supported || !state.me) return;
    busy = true; paint();
    try {
      if (active) {
        const sub = await (await worker()).pushManager.getSubscription();
        if (sub) {
          await api("/api/push/unsubscribe", {method: "POST", body: JSON.stringify({endpoint: sub.endpoint})});
          await sub.unsubscribe();
        }
        active = false; remember(false); toast("Approval alerts disabled on this device.");
      } else {
        // Request directly from the tap: iOS requires user activation.
        const permission = await Notification.requestPermission();
        if (permission !== "granted") throw new Error("Allow notifications in your browser or phone settings, then try again.");
        await subscribe();
        if (active) toast("Approval alerts enabled, including while Covenant is closed.");
      }
    } catch (error) {toast(error.message);}
    finally {busy = false; paint();}
  };
  let target = null;
  const id = new URLSearchParams(location.search).get("approval");
  if (id) target = {approval_id: id, room_id: location.pathname.slice(1)};
  async function openTarget() {
    if (!target || !state.me || !globalThis.CovenantApprovals) return;
    const next = target; target = null;
    try {await globalThis.CovenantApprovals.open(next.approval_id, next.room_id);}
    catch (error) {toast(error.message);}
  }
  if (supported) navigator.serviceWorker.addEventListener("message", event => {
    if (event.data?.type !== "covenant-approval") return;
    target = event.data; openTarget();
  });
  window.addEventListener("covenant-login-change", () => {sync(); openTarget();});
  globalThis.CovenantPush = {get active() {return active;}, sync};
  paint(); sync(); openTarget();
})();

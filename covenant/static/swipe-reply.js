"use strict";
// Touch swipe on a message bubble, in either direction, picks it as the reply target.
// Mouse dragging is left alone so desktop text selection keeps working; the Reply button stays.
(() => {
  const START = 10, TRIGGER = 64, MAX = 88;
  const interactive = "a, button, input, textarea, select, summary, details, pre, code, table";
  function attach(target, onReply) {
    let drag = null, swallowClick = false;
    function reset() {
      target.classList.remove("swiping", "swipe-ready");
      delete target.dataset.swipe;
      target.style.transform = "";
      target.style.removeProperty("--swipe-progress");
      document.body.classList.remove("swiping-reply");
      drag = null;
    }
    target.addEventListener("pointerdown", event => {
      if (event.pointerType === "mouse" || !event.isPrimary || drag) return;
      // Links, code blocks and tables may scroll or act on their own.
      if (event.target?.closest?.(interactive)) return;
      drag = {id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, active: false, ready: false};
    });
    target.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.active) {
        if (Math.abs(dy) > START && Math.abs(dy) >= Math.abs(dx)) {drag = null; return;}
        if (Math.abs(dx) < START || Math.abs(dx) < Math.abs(dy) * 1.5) return;
        drag.active = true;
        target.classList.add("swiping");
        document.body.classList.add("swiping-reply");
        // Capture keeps the gesture if the finger leaves the bubble; losing it must not break the swipe.
        try {target.setPointerCapture?.(event.pointerId);} catch {}
      }
      drag.dx = Math.sign(dx) * Math.min(MAX, Math.abs(dx));
      target.dataset.swipe = drag.dx > 0 ? "right" : "left";
      target.style.transform = `translateX(${drag.dx}px)`;
      target.style.setProperty("--swipe-progress", String(Math.min(1, Math.abs(drag.dx) / TRIGGER)));
      const ready = Math.abs(drag.dx) >= TRIGGER;
      if (ready !== drag.ready) {
        drag.ready = ready;
        target.classList.toggle("swipe-ready", ready);
        if (ready) try {navigator.vibrate?.(10);} catch {}
      }
    });
    function finish(event) {
      if (!drag || event.pointerId !== drag.id) return;
      const {active, ready} = drag;
      reset();
      if (!active) return;
      swallowClick = true; setTimeout(() => {swallowClick = false;}, 0);
      if (ready && event.type === "pointerup") onReply();
    }
    target.addEventListener("pointerup", finish);
    target.addEventListener("pointercancel", finish);
    // A finished swipe must not also open a link under the finger.
    target.addEventListener("click", event => {
      if (swallowClick) {event.preventDefault(); event.stopPropagation(); swallowClick = false;}
    }, true);
  }
  globalThis.CovenantSwipeReply = {attach, TRIGGER};
})();

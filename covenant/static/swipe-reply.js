"use strict";
// Touch swipe anywhere on a message, in either direction, picks it as the reply target; the bubble slides.
// Mouse dragging is left alone so desktop text selection keeps working; the Reply button stays.
(() => {
  const START = 10, TRIGGER = 64, MAX = 88;
  // Typing fields keep their own gestures; so do code blocks and tables that really scroll sideways.
  const typing = "input, textarea, select";
  function scrollsSideways(node, area) {
    for (; node && node !== area; node = node.parentElement) {
      if (node.scrollWidth > node.clientWidth + 1 && /^(auto|scroll)$/.test(globalThis.getComputedStyle?.(node).overflowX || "")) return true;
    }
    return false;
  }
  function attach(target, onReply, area = target) {
    let drag = null, swallowClick = false;
    function reset() {
      target.classList.remove("swiping", "swipe-ready");
      delete target.dataset.swipe;
      target.style.transform = "";
      target.style.removeProperty("--swipe-progress");
      document.body.classList.remove("swiping-reply");
      drag = null;
    }
    area.addEventListener("pointerdown", event => {
      // A reply shown inside an open thread is its own message; the thread's parent must not swipe too.
      if (event.pointerType === "mouse" || !event.isPrimary || drag || event.swipeClaimed) return;
      if (event.target?.closest?.(typing) || scrollsSideways(event.target, area)) return;
      event.swipeClaimed = true;
      drag = {id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, active: false, ready: false};
    });
    area.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.active) {
        if (Math.abs(dy) > START && Math.abs(dy) >= Math.abs(dx)) {drag = null; return;}
        if (Math.abs(dx) < START || Math.abs(dx) < Math.abs(dy) * 1.5) return;
        drag.active = true;
        target.classList.add("swiping");
        document.body.classList.add("swiping-reply");
        // Capture keeps the gesture if the finger leaves the bubble; losing it must not break the swipe.
        try {area.setPointerCapture?.(event.pointerId);} catch {}
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
    area.addEventListener("pointerup", finish);
    area.addEventListener("pointercancel", finish);
    // A finished swipe must not also open a link or press a button under the finger.
    area.addEventListener("click", event => {
      if (swallowClick) {event.preventDefault(); event.stopPropagation(); swallowClick = false;}
    }, true);
  }
  globalThis.CovenantSwipeReply = {attach, TRIGGER};
})();

"use strict";
// Settings shows the server address as a QR code, so a phone can open it after the address changes.
(() => {
  const QUIET = 4, SIZE = 126;
  function modules(address) {
    const code = qrcode(0, "M"); code.addData(address); code.make();
    const count = code.getModuleCount();
    return {count, dark: (row, column) => code.isDark(row, column)};
  }
  // Always dark on white with a quiet border: phone cameras need that contrast in every theme.
  function draw(canvas, address) {
    const {count, dark} = modules(address), scale = Math.max(1, Math.floor(SIZE / (count + QUIET * 2)));
    const side = (count + QUIET * 2) * scale, context = canvas.getContext("2d");
    canvas.width = canvas.height = side;
    context.fillStyle = "#fff"; context.fillRect(0, 0, side, side);
    context.fillStyle = "#000";
    for (let row = 0; row < count; row++) for (let column = 0; column < count; column++)
      if (dark(row, column)) context.fillRect((column + QUIET) * scale, (row + QUIET) * scale, scale, scale);
  }
  function paint() {
    const box = document.querySelector("#server-qr"), canvas = document.querySelector("#server-qr-code"), link = document.querySelector("#server-qr-address");
    if (!box || !canvas || !link) return;
    const address = (typeof state !== "undefined" && state.me?.public_base_url) || "";
    box.hidden = !address;
    if (!address || box.dataset.address === address) return;
    try {draw(canvas, address);} catch {box.hidden = true; return;}
    box.dataset.address = address; link.textContent = address; link.href = address;
    canvas.setAttribute("aria-label", `QR code for ${address}`);
  }
  window.addEventListener("covenant-login-change", paint);
  globalThis.CovenantServerQr = {paint, modules, draw};
})();

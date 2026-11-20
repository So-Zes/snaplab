/* Automatic shell updates. The page never reloads while the user is typing in a field; it waits until the field
   loses focus. Open fields, scroll and caret are carried over the reload in this tab only (sessionStorage) and
   removed on restore. What is typed in a composer is also kept on the device by app.js (drafts). */
(() => {
  "use strict";
  const version = document.currentScript.dataset.version;
  const key = "snaplab-update-state";
  let checking = false, reloading = false, composing = false, pending = "";
  document.addEventListener("compositionstart", () => { composing = true; });
  document.addEventListener("compositionend", () => { composing = false; refresh(); });
  const controls = () => Array.from(document.querySelectorAll("input:not([type=password]):not([type=file]):not([type=hidden]),textarea,select,details"));
  const identity = el => {
    if (el.id) return el.id;
    const parent = el.closest("[data-mid],[data-q]");
    return [parent?.dataset.mid || parent?.dataset.q || "page", el.tagName, el.getAttribute("name"),
      el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.className,
      el.tagName === "DETAILS" ? el.querySelector("summary")?.textContent : ""].join(":");
  };
  const identifiedControls = () => {
    const counts = new Map();
    return controls().map(el => { const base = identity(el), n = counts.get(base) || 0;
      counts.set(base, n + 1); return [el, base + ":" + n]; });
  };
  const typing = () => {
    const el = document.activeElement;
    return !!el && (el.tagName === "TEXTAREA" || el.isContentEditable ||
      (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "range", "file", "hidden"].includes(el.type)));
  };
  function save() {
    // A dialog may hold unsaved images or state in closures. Keep it alive until closed.
    // While a text field has the focus (the keyboard is open), wait: the update applies once the user leaves it.
    if (composing || typing() || document.querySelector(".sheet.in") || document.querySelector('input[type=file]')?.files?.length) return false;
    const fields = identifiedControls().map(([el, id]) => ({id, value: el.value, checked: el.checked,
      open: el.open, start: el.selectionStart, end: el.selectionEnd, focus: el === document.activeElement}));
    try {
      sessionStorage.setItem(key, JSON.stringify({hash: location.hash, target: pending, fields, x: scrollX, y: scrollY, at: Date.now()}));
      return true;
    } catch { return false; } // Never discard drafts if storage is unavailable.
  }
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(key)); } catch {}
  if (saved && saved.hash === location.hash && Date.now() - saved.at < 300000) {
    const remaining = new Map(saved.fields.map(f => [f.id, f]));
    const restore = () => {
      identifiedControls().forEach(([el, id]) => {
        const f = remaining.get(id);
        if (!f) return;
        if (f.value !== undefined) el.value = f.value;
        if (f.checked !== undefined) el.checked = f.checked;
        if (f.open !== undefined) el.open = f.open;
        el.dispatchEvent(new Event("input", {bubbles: true}));
        if (f.focus) { el.focus(); try { el.setSelectionRange(f.start, f.end); } catch {} }
        remaining.delete(id);
      });
      if (!remaining.size) { observer.disconnect(); sessionStorage.removeItem(key); scrollTo(saved.x, saved.y); }
    };
    const observer = new MutationObserver(restore);
    observer.observe(document.body, {childList: true, subtree: true});
    restore();
    setTimeout(() => { observer.disconnect(); sessionStorage.removeItem(key); }, 30000);
  } else { try { sessionStorage.removeItem(key); } catch {} }
  async function refresh() {
    if (checking || reloading || document.hidden || navigator.onLine === false) return;
    checking = true;
    try {
      const response = await fetch("/api/web-version", {cache: "no-store", credentials: "same-origin"});
      if (!response.ok) return;
      pending = (await response.json()).version;
      if (!pending || pending === version) return;
      // A failed/old navigation must not cause an unbounded reload loop.
      if (saved?.target === pending) return;
      if (save()) { reloading = true; location.reload(); }
    } catch { /* Offline: retain the currently working screen and its draft. */ }
    finally { checking = false; }
  }
  window.addEventListener("focus", refresh);
  window.addEventListener("pageshow", refresh);
  window.addEventListener("online", refresh);
  document.addEventListener("visibilitychange", refresh);
  navigator.serviceWorker?.addEventListener("controllerchange", refresh);
  setInterval(refresh, 15000);
  // Retry a deferred update after a sheet closes or composition ends.
  setInterval(() => { if (pending && pending !== version && !typing() && !composing) refresh(); }, 1000);
  refresh();
})();

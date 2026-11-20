/* SnapLab service worker.
   - Keeps only the app shell (scripts, styles, icons, offline page) on the device. Conversations and data are never cached.
   - Shows Web Push notifications and opens the right screen when one is tapped.
   The version is written in by the server, so a new build replaces the cached shell. */
"use strict";
const VERSION = "__SNAPLAB_VERSION__";
const CACHE = "snaplab-shell-" + VERSION;
const PUBLIC_SHELL = ["/offline.html", "/offline.js", "/manifest.webmanifest", "/logo.png", "/i18n.js", "/app.css",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/badge-96.png"];
const PRIVATE_SHELL = ["/app.js", "/update.js"];
// The page loads these by versioned address; they never change under one address, so a start-up can read them
// from the device without waiting for the PC (the version in the address changes with every build).
const VERSIONED = ["app.js", "app.css", "i18n.js", "update.js"].map((f) => "/assets/" + VERSION + "/" + f);
const SHELL = new Set(PUBLIC_SHELL.concat(PRIVATE_SHELL));
const NETWORK_ONLY = ["/api/", "/pair/", "/healthz", "/sw.js"];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await cache.addAll(PUBLIC_SHELL).catch(() => null);
    await Promise.all(PRIVATE_SHELL.concat(VERSIONED).map((u) => fetch(u, { credentials: "same-origin", cache: "no-store" })
      .then((r) => (r.ok ? cache.put(u, r) : null)).catch(() => null)));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("snaplab-shell-") && k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (NETWORK_ONLY.some((p) => url.pathname.startsWith(p))) return;
  if (req.mode === "navigate") { event.respondWith(navigate(req)); return; }
  if (SHELL.has(url.pathname) || /^\/assets\/[a-zA-Z0-9_-]+\/(app\.js|app\.css|i18n\.js|update\.js)$/.test(url.pathname))
    event.respondWith(shell(req, url.pathname));
});

async function navigate(req) {
  try {
    const res = await Promise.race([fetch(req), new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 10000))]);
    if (res.status >= 502 && res.status <= 504) return offline();
    return res;
  } catch (e) {
    return offline();
  }
}

async function offline() {
  const cached = await caches.match("/offline.html");
  const html = cached ? await cached.text() : `<!doctype html><meta charset=utf-8><title>SnapLab</title><p>Cannot reach your PC.</p><script>
    let lang=navigator.language;try{lang=localStorage.getItem('snaplab-lang')||lang}catch{}
    if(String(lang).toLowerCase().startsWith('ru')){document.documentElement.lang='ru';document.querySelector('p').textContent='Не удалось подключиться к вашему компьютеру.'}
  </script>`;
  return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

async function shell(req, path) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(path);
  if (cached && path.startsWith("/assets/" + VERSION + "/")) return cached;
  try {
    const r = await fetch(req, {cache: "no-store"});
    if (r.ok) { await cache.put(path, r.clone()); return r; }
    if (r.status < 500) return r; // Never replace an authentication refusal with private cached code.
  } catch {}
  return cached || new Response("", { status: 504 });
}

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { title: "SnapLab", body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.title || "SnapLab", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, icon: "/icons/icon-192.png", badge: "/icons/badge-96.png",
    data: { url: d.url || "/" }, timestamp: Date.now(),
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        try { c.postMessage({ type: "open", url: target }); } catch (e) { /* ignore */ }
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});

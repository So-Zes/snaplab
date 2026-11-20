/* SnapLab phone app (no framework, no build step). */
(function () {
  "use strict";

  // ------------------------------------------------------------------ tiny helpers

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  function h(tag, attrs) {
    const el = document.createElement(tag);
    const kids = Array.prototype.slice.call(arguments, 2);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") { for (const sk in v) { if (sk.startsWith("--")) el.style.setProperty(sk, v[sk]); else el.style[sk] = v[sk]; } }
      else if (v === true) el.setAttribute(k, "");
      else el.setAttribute(k, v);
    }
    append(el, kids);
    return el;
  }
  function append(el, kids) {
    for (const c of kids) {
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) append(el, c);
      else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const S = {
    lang: "en", settings: null, bots: [], rooms: [], projects: [], sessions: [], questions: [], harnesses: [],
    dashboard: { widgets: [] }, usage: null, activity: [], push: {}, device: {}, busy: new Set(), tasks: [],
    msgs: {}, live: {}, sessionDetail: {}, connected: false, route: { name: "boot" }, ob: null, version: "",
  };
  const t = (key, vars) => window.snapT(S.lang, key, vars);
  const loc = () => window.snapT(S.lang, "lang.locale");
  // a stored line (activity, note, timeline event) in the language the phone uses now: "@key" values are looked up too
  const tv = (key, vars) => { const v = {}; Object.entries(vars || {}).forEach(([k, x]) => { v[k] = typeof x === "string" && x[0] === "@" && x.length > 1 ? t(x.slice(1)) : x; }); return t(key, v); };
  const lineText = (o) => (o && o.key && t(o.key) !== o.key ? tv(o.key, o.vars) : (o && o.text) || "");
  const NAME_MAX = 24;
  const chars = (s) => Array.from(String(s || "")).length;
  const bot = (id) => S.bots.find((b) => b.id === id);
  const orchestrator = () => S.bots.find((b) => b.orchestrator);

  // ------------------------------------------------------------------ icons
  // One SVG file per icon in web/icons/ui/ (24px grid, stroke="currentColor"). The server puts them into the page
  // as one hidden sprite, so replacing the files in that folder replaces every icon (see web/icons/ui/README.md).
  function icon(name, cls) {
    const span = document.createElement("span");
    span.className = "ic" + (cls ? " " + cls : "");
    span.setAttribute("aria-hidden", "true");
    span.innerHTML = '<svg viewBox="0 0 24 24"><use href="#i-' + String(name).replace(/[^a-z0-9-]/g, "") + '"></use></svg>';
    if (/^[a-z0-9-]+$/.test(name)) append(span, [
      h("img", { class: "icon-3d light", src: "/icons/soft3d/" + name + ".png", alt: "", loading: "lazy" }),
      h("img", { class: "icon-3d dark", src: "/icons/soft3d/dark/" + name + ".png", alt: "", loading: "lazy" })]);
    return span;
  }

  // ------------------------------------------------------------------ light / dark (round 6): the phone's choice unless the user picked one
  function applyTheme() {
    const pick = (S.settings && S.settings.theme) || "system";
    const root = document.documentElement;
    if (pick === "light" || pick === "dark") root.setAttribute("data-theme", pick); else root.removeAttribute("data-theme");
    const dark = pick === "dark" || (pick === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.dataset.iconStyle = (S.settings && S.settings.icon_style) || "line";
    root.dataset.iconTheme = dark ? "dark" : "light";
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", dark ? "#0E0F13" : "#FBF8F6"));
  }
  if (window.matchMedia) { try { window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme); } catch (e) { /* old browsers */ } }

  // ------------------------------------------------------------------ formatting

  function relTime(iso) {
    if (!iso) return "";
    const d = new Date(iso), now = new Date();
    const s = Math.max(0, (now - d) / 1000);
    if (s < 45) return t("common.now");
    if (s < 3600) return t("common.m", { n: Math.round(s / 60) });
    if (s < 86400 && d.getDate() === now.getDate()) return d.toLocaleTimeString(loc(), { hour: "numeric", minute: "2-digit" });
    if (s < 172800) return t("common.yesterday");
    return d.toLocaleDateString(loc(), { month: "short", day: "numeric" });
  }
  function clockTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString(loc(), { hour: "numeric", minute: "2-digit" });
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return t("common.sec", { n: sec });
    if (sec < 3600) return t("common.min", { m: Math.floor(sec / 60), s: sec % 60 });
    return t("common.hr", { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60) });
  }
  function num(n) {
    n = Number(n || 0);
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  // Markdown -> one plain line (for list previews)
  function plain(text) {
    return String(text || "").replace(/```[^`]*```/g, " ").replace(/[*`#>]+/g, "").replace(/^\s*(?:[-•]|\d+[.)])\s+/gm, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  }

  // light Markdown -> HTML (input is escaped first; only http(s) links)
  function md(text) {
    let src = String(text || "");
    const blocks = [];
    src = src.replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => { blocks.push(code.replace(/\n$/, "")); return "\u0000" + (blocks.length - 1) + "\u0000"; });
    let s = esc(src);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, txt, url) => '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + txt + "</a>");
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, url) => pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>");
    const lines = s.split("\n");
    const out = [];
    const stack = [];                     // open lists: {kind, indent}
    const ITEM = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/;
    const openList = (kind, num, indent) => { out.push(kind === "ol" && num > 1 ? '<ol start="' + num + '">' : "<" + kind + ">"); stack.push({ kind, indent }); };
    const closeAll = () => { while (stack.length) out.push("</li></" + stack.pop().kind + ">"); };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const m = ln.match(ITEM);
      const hd = ln.match(/^\s*#{1,4}\s+(.*)$/);
      if (m) {
        const indent = m[1].replace(/\t/g, "    ").length, kind = /\d/.test(m[2]) ? "ol" : "ul", num = parseInt(m[2], 10) || 1;
        while (stack.length && stack[stack.length - 1].indent > indent) out.push("</li></" + stack.pop().kind + ">");
        const top = stack[stack.length - 1];
        if (!top || indent > top.indent) openList(kind, num, indent);
        else if (top.kind !== kind) { out.push("</li></" + stack.pop().kind + ">"); openList(kind, num, indent); }
        else out.push("</li>");
        out.push("<li>" + m[3]);
        continue;
      }
      if (stack.length) {
        if (ln.trim() === "") {                       // a blank line between items keeps the list going
          const next = lines.slice(i + 1).find((x) => x.trim() !== "");
          if (next && ITEM.test(next)) continue;
        } else if (/^\s{2,}\S/.test(ln)) { out.push("<br>" + ln.trim()); continue; }   // the item goes on
        closeAll();
      }
      if (hd) out.push('<p class="md-h">' + hd[1] + "</p>");
      else if (ln.trim() === "") out.push('<div class="md-gap"></div>');
      else out.push("<p>" + ln + "</p>");
    }
    closeAll();
    let html = out.join("");
    html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => "<pre><code>" + esc(blocks[Number(i)]) + "</code></pre>");
    return html.replace(/(<div class="md-gap"><\/div>){2,}/g, '<div class="md-gap"></div>').replace(/(<div class="md-gap"><\/div>)+$/, "");
  }

  // ------------------------------------------------------------------ network

  async function api(path, body) {
    const opt = body === undefined ? { cache: "no-store", credentials: "same-origin" } :
      { method: "POST", cache: "no-store", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-SnapLab": "1" }, body: JSON.stringify(body) };
    let r;
    // A read that the PC's Tailscale proxy could not hand to SnapLab (502-504) is asked again: opening the app often
    // got one 502 for /api/bootstrap while the page and the other requests of that moment went through (2026-11-17).
    for (let attempt = 1; ; attempt++) {
      r = await fetch(path, opt);
      if (body !== undefined || attempt > 2 || !(r.status >= 502 && r.status <= 504)) break;
      await new Promise((resolve) => setTimeout(resolve, 400 * attempt));
    }
    let j = {};
    try { j = await r.json(); } catch (e) { j = {}; }
    if (!r.ok || j.ok === false) {
      const err = new Error(j.error || ("HTTP " + r.status));
      err.status = r.status; err.need = j.need;
      throw err;
    }
    return j;
  }

  function toast(text, kind) {
    const box = $("#toasts");
    const el = h("div", { class: "toast " + (kind || "") }, kind === "bad" ? icon("alert") : icon("check"), h("span", { text: text }));
    box.appendChild(el);
    requestAnimationFrame(() => el.classList.add("in"));
    setTimeout(() => { el.classList.remove("in"); setTimeout(() => el.remove(), 300); }, 2600);
  }
  const fail = (e) => toast(e && e.message ? e.message : t("err.generic"), "bad");

  // ------------------------------------------------------------------ drafts
  // What the user is typing is kept on this device (localStorage), one entry per room or SnapCode, so a reload,
  // an app update, a lost connection or the phone closing the app in the background never loses it.
  // An entry is removed once its message has been sent. Nothing here is sent to the PC.
  const DRAFT = "snaplab-draft:";
  const drafts = {
    get(key) { try { const v = JSON.parse(localStorage.getItem(DRAFT + key) || "null"); return v && typeof v.text === "string" ? v.text : ""; } catch (e) { return ""; } },
    set(key, text) {
      try { if (String(text || "").trim()) localStorage.setItem(DRAFT + key, JSON.stringify({ text, at: Date.now() })); else localStorage.removeItem(DRAFT + key); } catch (e) { /* storage full or blocked: the field still holds the text */ }
    },
    clear(key) { try { localStorage.removeItem(DRAFT + key); } catch (e) { /* ignore */ } },
  };
  // Fill an empty field from its saved draft and keep saving while the user types.
  function keepDraft(ta, key) {
    if (!ta.value) { const saved = drafts.get(key); if (saved) { ta.value = saved; setTimeout(() => ta.dispatchEvent(new Event("input")), 0); } }
    const save = () => { if (!ta.dataset.sending) drafts.set(key, ta.value); };
    ta.addEventListener("input", save);
    ta.addEventListener("change", save);
    ta.addEventListener("compositionend", save);
    return ta;
  }
  // Send the text of a field: the field empties at once, the saved draft goes only when the PC has taken it.
  async function sendDraft(ta, key, send) {
    const text = ta.value;
    ta.dataset.sending = "1"; ta.value = ""; ta.dispatchEvent(new Event("input")); delete ta.dataset.sending;
    try {
      await send(text);
      if (!ta.value.trim()) drafts.clear(key);
      return true;
    } catch (e) {
      ta.value = ta.value.trim() ? text + "\n" + ta.value : text;
      drafts.set(key, ta.value); ta.dispatchEvent(new Event("input"));
      throw e;
    }
  }

  let es = null, esRetry = 1000;
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/events");
    es.addEventListener("hello", () => {
      if (!S.connected) {
        const again = S.wasConnected;
        S.connected = true; S.wasConnected = true; esRetry = 1000; setConn(true);
        // After a lost connection, catch up without rebuilding a field the user may be typing in.
        if (again) { S.live = {}; redraw(); }
      }
    });
    es.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch (e) { return; } onEvent(ev); };
    es.onerror = () => {
      setConn(false);
      S.connected = false;
      es.close(); es = null;
      setTimeout(async () => { try { await load(); } catch (e) { /* stay offline */ } connect(); }, esRetry);
      esRetry = Math.min(esRetry * 2, 15000);
    };
  }
  function setConn(ok) {
    document.body.classList.toggle("offline", !ok);
    paintHealth();
  }

  // ------------------------------------------------------------------ state

  async function load() {
    // Both requests leave at once: on a phone far from the PC every extra round trip shows at start-up.
    const [j, faces] = await Promise.all([api("/api/bootstrap"), S.avatars ? null : api("/avatars/avatars.json")]);
    if (faces) S.avatars = faces;
    S.settings = j.settings; S.bots = j.bots; S.rooms = j.rooms; S.projects = j.projects; S.sessions = j.sessions;
    S.questions = j.questions; S.harnesses = j.harnesses; S.dashboard = j.dashboard; S.usage = j.usage; S.activity = j.activity;
    S.push = j.push; S.device = j.device; S.version = j.version; S.palette = j.palette; S.tones = j.tones;
    S.models = j.models || []; S.routing = j.routing || {}; S.toolLabels = j.tool_labels || {}; S.status = j.status || S.status;
    S.background = j.background || null; S.tasks = j.tasks || [];
    S.formerBots = j.former_bots || [];
    S.archivedBots = j.archived_bots || [];
    S.busy = new Set(j.busy_rooms || []);
    applyTheme();
    if (S.settings.lang) S.lang = S.settings.lang;
    if (!S.settings.onboarded && window.snapBrowserLang() === "ru") S.lang = "ru";
    document.documentElement.lang = S.lang; window.snapRememberLang(S.lang);
    return j;
  }

  function upsert(list, item, key) {
    key = key || "id";
    const i = list.findIndex((x) => x[key] === item[key]);
    if (i >= 0) list[i] = Object.assign({}, list[i], item); else list.unshift(item);
  }

  function onEvent(ev) {
    const r = S.route;
    switch (ev.type) {
      case "message.new": case "message.done": {
        const list = S.msgs[ev.room];
        if (list) {
          const i = list.findIndex((m) => m.id === ev.message.id);
          if (i >= 0) list[i] = ev.message; else list.push(ev.message);
        }
        if (ev.type === "message.done") { delete (S.live[ev.room] || {})[ev.message.id]; S.busy.delete(ev.room); }
        if (r.name === "room" && r.id === ev.room) roomUpsert(ev.message, ev.type === "message.done");
        break;
      }
      case "message.start": {
        (S.live[ev.room] = S.live[ev.room] || {})[ev.message.id] = Object.assign({}, ev.message, { _t0: Date.now() });
        S.busy.add(ev.room);
        if (r.name === "room" && r.id === ev.room) { roomUpsert(S.live[ev.room][ev.message.id], false); setBusy(true); }
        else refreshList();
        break;
      }
      case "message.delta": {
        const m = (S.live[ev.room] || {})[ev.id];
        if (!m) break;
        m.text = (m.text || "") + (ev.text || ""); m.thinking = (m.thinking || "") + (ev.thinking || "");
        if (ev.text && !m._tFirstText) m._tFirstText = Date.now();
        if (r.name === "room" && r.id === ev.room) streamUpdate(m);
        break;
      }
      case "message.wait": {
        const lm = (S.live[ev.room] || {})[ev.id];
        if (lm) { lm._wait = ev.position || 0; if (!ev.position) lm._t0 = Date.now(); }   // 0 = got a slot: thinking starts now
        if (r.name === "room" && r.id === ev.room) {
          const lbl = $('[data-mid="' + ev.id + '"] .think-label');
          if (lbl) lbl.textContent = ev.position ? t("msg.queued", { n: ev.position }) : t("msg.thinking");
          const ic = $('[data-mid="' + ev.id + '"] .think summary > .ic');
          if (ic) ic.replaceWith(icon(ev.position ? "clock" : "brain"));
        }
        break;
      }
      case "message.tool": {
        const m = (S.live[ev.room] || {})[ev.id];
        if (!m) break;
        m.tools = m.tools || [];
        const i = m.tools.findIndex((c) => c.id === ev.tool.id);
        if (i >= 0) m.tools[i] = ev.tool; else m.tools.push(ev.tool);
        if (r.name === "room" && r.id === ev.room) streamUpdate(m, true);
        break;
      }
      case "room.updated": if (ev.room && !ev.room.deleted) { upsert(S.rooms, ev.room); refreshList(); } break;
      case "rooms.updated": S.rooms = ev.rooms; refreshList(); break;
      case "bots.updated": S.bots = ev.bots; if (ev.archived_bots) S.archivedBots = ev.archived_bots; if (["bots", "chats", "home", "dashboard"].includes(r.name)) render(); break;
      case "projects.updated": S.projects = ev.projects; if (["projects", "home", "dashboard"].includes(r.name)) render(); break;
      case "session.updated": {
        upsert(S.sessions, ev.session);
        const d = S.sessionDetail[ev.session.id];
        if (d) Object.assign(d, ev.session, { events: d.events });
        if (r.name === "session" && r.id === ev.session.id) renderSessionBody();
        else if (["project", "home", "dashboard", "projects"].includes(r.name)) render();
        updateTabBadges();
        break;
      }
      case "session.screen": {
        const d = S.sessionDetail[ev.session] || S.sessions.find(x => x.id === ev.session);
        if (d) d.screen_seq = ev.seq;
        if (r.name === "session" && r.id === ev.session) {
          const im = document.querySelector('.desktop-live img');
          if (im && ev.seq) im.src = '/api/sessions/' + ev.session + '/screen?v=' + ev.seq;
          else renderSessionBody();
        }
        break;
      }
      case "session.event": {
        const d = S.sessionDetail[ev.session];
        if (d) { d.events = (d.events || []).concat([ev.event]).slice(-200); if (r.name === "session" && r.id === ev.session) { appendTimeline(ev.event); updateNow(ev.event); } }
        break;
      }
      case "question.new": upsert(S.questions, ev.question); refreshList(); break;
      case "question.updated": {
        S.questions = S.questions.filter((q) => q.id !== ev.question.id || ev.question.status === "open");
        $$('[data-q="' + ev.question.id + '"]').forEach((el) => el.replaceWith(questionCard(ev.question)));
        refreshList();
        break;
      }
      case "dashboard.updated": S.dashboard = ev.dashboard; if (r.name === "dashboard") render(); break;
      case "room.summary": if (S.summarySheet && S.summarySheet.room === ev.room && document.body.contains(S.summarySheet.sh.panel)) S.summarySheet.fill(); break;
      case "memory.updated": if (r.name === "bot" && S.memoryCard && S.memoryCard.bot === ev.bot) S.memoryCard.load(); break;
      case "usage.updated": S.usage = ev.usage; if (r.name === "dashboard") renderUsageCard(); else if (r.name === "home") renderHomeUsage(); else if (r.name === "usage") refreshUsageSoon(); break;
      case "settings.updated": S.settings = ev.settings; applyTheme(); break;
      case "tasks.updated": S.tasks = ev.tasks; if (["home", "dashboard"].includes(r.name)) render(); else if (r.name === "bot" && S.insideTasks) S.insideTasks(); break;
      case "activity.new": S.activity = [ev.activity].concat(S.activity || []).slice(0, 40); if (r.name === "dashboard") renderActivityCard(); break;
      case "status.updated": {
        S.status = ev.status; paintHealth();
        const slots = (ev.status.items || []).find((x) => x.id === "slots");
        if (slots && S.usage) S.usage.slots = { ...S.usage.slots, in_use: slots.in_use, count: slots.count, waiting: slots.waiting };
        if (r.name === "home") renderHomeUsage(); else if (r.name === "dashboard") renderUsageCard();
        break;
      }
      case "state.reload": load().then(redraw).catch(() => {}); break;
    }
  }
  // Redraw the current screen with new data. A room or a SnapCode page only updates its content, never its
  // composer; other screens with a focused text field wait (their data is drawn the next time they open).
  function redraw() {
    const n = S.route.name;
    if (n === "room") { if (roomEls && roomEls.id === S.route.id) refreshRoom(S.route.id); else render(); return; }
    if (n === "session") { if (sessEls && sessEls.id === S.route.id) refreshSession(S.route.id); else render(); return; }
    if (typingIn($("#app"))) return;
    if (["chats", "home", "dashboard", "usage", "projects", "project", "bots"].includes(n)) render();
  }
  function typingIn(root) {
    const el = document.activeElement;
    return !!(root && el && root.contains(el) && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && !["checkbox", "radio", "button", "range", "file"].includes(el.type)) || el.isContentEditable));
  }
  function refreshList() {
    const n = S.route.name;
    if (["chats", "home", "dashboard"].includes(n)) render();
    if (n === "room" && !S.rooms.some((r) => r.id === S.route.id)) go("#/chats");
    updateTabBadges();
  }

  // ------------------------------------------------------------------ routing

  function parseRoute() {
    const [path, query] = (location.hash || "#/home").replace(/^#\/?/, "").split("?");
    const p = path.split("/");
    const name = p[0] || "home";
    const q = {};
    (query || "").split("&").filter(Boolean).forEach((kv) => { const [k, v] = kv.split("="); q[decodeURIComponent(k)] = decodeURIComponent(v || ""); });
    return { name, id: p[1] || null, q };
  }
  function go(hash) { if (location.hash === hash) render(); else location.hash = hash; }
  window.addEventListener("hashchange", () => { activeSheets.slice().reverse().forEach((sh) => sh.close()); S.route = parseRoute(); render(); });
  if (navigator.serviceWorker) navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "open" && e.data.url) { const u = new URL(e.data.url); if (u.hash) location.hash = u.hash; }
  });

  // ------------------------------------------------------------------ shell

  const TABS = [["chats", "chat", "nav.chats"], ["home", "sail", "nav.home"], ["dashboard", "grid", "nav.dashboard"]];

  function avatar(b, size) {
    if (!b) return h("span", { class: "av " + (size || "") }, "?");
    const face = S.avatars && S.avatars.avatars.find((f) => f.id === b.avatar);
    const src = face ? "/avatars/" + face.files[size === "xl" ? "256" : "96"]
      : /^upload-[0-9a-f]{64}\.(png|jpg|webp)$/.test(b.avatar || "") ? "/api/avatars/" + b.avatar : "";
    const el = h("span", { class: "av " + (size || "") + (src ? " has-face" : ""), style: { "--c": b.color || "#E0182D" }, title: b.name },
      src ? h("img", { class: "avatar-image", src, alt: "", onerror: () => { el.classList.remove("has-face"); $(".avatar-image", el).remove(); el.prepend(document.createTextNode(b.initials || "?")); } }) : b.initials || "?");
    if (b.orchestrator) el.appendChild(h("span", { class: "avatar-lead", "aria-label": t("bots.orchestrator") }, h("img", { src: "/logo.png", alt: "" })));
    return el;
  }

  function recommendedFace(role) {
    const used = new Set(S.bots.map((b) => b.avatar));
    const score = (f) => Object.values(f.roles).flat().filter((r) => String(role || "").toLowerCase().includes(r.toLowerCase())).length;
    const faces = S.avatars.avatars.filter((f) => f.set === "c" && !f.lead_mark).sort((a, b) => score(b) - score(a));
    return (faces.find((f) => !used.has(f.id)) || faces[0]).id;
  }

  // The parent form owns the draft. Closing either picker or cropper discards only that sheet.
  function faceField(getBot, onPick) {
    const button = h("button", { class: "btn face-field", type: "button", onclick: () => avatarPicker(getBot(), (value) => { onPick(value); refresh(); }) });
    const refresh = () => { button.replaceChildren(avatar(getBot(), "lg"), h("span", {}, t("avatar.choose")), icon("right")); };
    refresh();
    return { el: button, refresh };
  }

  function avatarPicker(b, onPick) {
    let chosen = b.avatar || "", active = true;
    const preview = h("div", { class: "face-preview" });
    const choices = h("div", { class: "face-sets" });
    const paint = () => {
      preview.replaceChildren(avatar({ ...b, avatar: chosen }, "xl"), h("b", {}, b.name || t("avatar.choose")));
      $$("[data-face]", choices).forEach((el) => el.setAttribute("aria-pressed", String(el.dataset.face === chosen)));
    };
    const choice = (value, label) => h("button", { class: "face-choice", type: "button", "data-face": value, "aria-pressed": String(value === chosen),
      onclick: () => { chosen = value; paint(); } }, avatar({ ...b, avatar: value }, "lg"), h("span", {}, label));
    choices.appendChild(choice("", t("avatar.initials")));
    if (chosen.startsWith("upload-")) choices.appendChild(choice(chosen, t("avatar.uploaded")));
    ["d", "e", "c", "a", "b"].forEach((set) => choices.appendChild(h("section", {}, h("h3", {}, S.avatars.sets[set][S.lang] || S.avatars.sets[set].en),
      h("div", { class: "face-grid" }, S.avatars.avatars.filter((f) => f.set === set).map((f) => choice(f.id, f.name[S.lang] || f.name.en))))));
    const input = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp", hidden: true });
    input.addEventListener("change", () => { const file = input.files[0]; input.value = ""; if (file) cropAvatar(file, (value) => {
      if (!active) return;
      chosen = value;
      if (!$$("[data-face]", choices).some((el) => el.dataset.face === value)) choices.prepend(choice(value, t("avatar.uploaded")));
      paint();
    }); });
    const upload = h("button", { class: "btn wide", type: "button", onclick: () => input.click() }, icon("person-add"), t("avatar.upload"));
    const save = h("button", { class: "btn primary wide face-save", type: "button", onclick: () => { onPick(chosen); sh.close(); } }, t("avatar.use"));
    const sh = sheet(t("avatar.choose"), [preview, upload, input, choices, save], () => { active = false; });
    paint();
  }

  function cropAvatar(file, onPick) {
    if (file.size > 20 * 1024 * 1024) { toast(t("avatar.source.large"), "bad"); return; }
    const reader = new FileReader();
    reader.onerror = () => toast(t("avatar.invalid"), "bad");
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => toast(t("avatar.invalid"), "bad");
      img.onload = () => {
        if (!img.naturalWidth || !img.naturalHeight || img.naturalWidth * img.naturalHeight > 64000000) { toast(t("avatar.invalid"), "bad"); return; }
        let zoom = 1, cx = img.naturalWidth / 2, cy = img.naturalHeight / 2, active = true;
        const base = Math.min(img.naturalWidth, img.naturalHeight);
        const canvas = h("canvas", { width: 256, height: 256, class: "crop-canvas", tabindex: 0, "aria-label": t("avatar.move") });
        const ctx = canvas.getContext("2d");
        const pointers = new Map();
        const draw = () => {
          const side = base / zoom;
          cx = Math.max(side / 2, Math.min(img.naturalWidth - side / 2, cx));
          cy = Math.max(side / 2, Math.min(img.naturalHeight - side / 2, cy));
          ctx.clearRect(0, 0, 256, 256);
          ctx.drawImage(img, cx - side / 2, cy - side / 2, side, side, 0, 0, 256, 256);
          range.value = zoom;
        };
        const range = h("input", { type: "range", min: 1, max: 5, step: 0.01, value: 1, oninput: () => { zoom = Number(range.value); draw(); } });
        canvas.addEventListener("pointerdown", (e) => { canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId, { x: e.clientX, y: e.clientY }); });
        canvas.addEventListener("pointermove", (e) => {
          const old = pointers.get(e.pointerId); if (!old) return;
          const next = { x: e.clientX, y: e.clientY };
          const other = [...pointers.entries()].find(([id]) => id !== e.pointerId);
          if (other) {
            const p = other[1], before = Math.hypot(old.x - p.x, old.y - p.y), after = Math.hypot(next.x - p.x, next.y - p.y);
            if (before > 0) zoom = Math.max(1, Math.min(5, zoom * after / before));
          } else {
            const scale = base / zoom / canvas.getBoundingClientRect().width;
            cx -= (next.x - old.x) * scale; cy -= (next.y - old.y) * scale;
          }
          pointers.set(e.pointerId, next); draw();
        });
        ["pointerup", "pointercancel", "lostpointercapture"].forEach((type) => canvas.addEventListener(type, (e) => pointers.delete(e.pointerId)));
        canvas.addEventListener("keydown", (e) => {
          const dirs = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };
          if (!dirs[e.key]) return; e.preventDefault(); cx += dirs[e.key][0] * base / zoom / 20; cy += dirs[e.key][1] * base / zoom / 20; draw();
        });
        const save = h("button", { class: "btn primary wide", type: "button", onclick: async () => {
          save.disabled = true;
          try {
            // Canvas re-encoding sends pixels only, never the source file or EXIF.
            const j = await api("/api/avatars", { image: canvas.toDataURL("image/png").split(",")[1] });
            if (active) { onPick(j.avatar); sh.close(); }
          } catch (e) { if (active) { toast(t("avatar.failed"), "bad"); save.disabled = false; } }
        } }, t("avatar.use"));
        const sh = sheet(t("avatar.crop"), [h("p", { class: "lead-text" }, t("avatar.move")), canvas, field(t("avatar.zoom"), range), save], () => { active = false; });
        draw();
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  }
  function groupAvatar(room) {
    const bs = (room.bots || []).map(bot).filter(Boolean).slice(0, 3);
    return h("span", { class: "av-stack" }, bs.map((b) => avatar(b, "sm")));
  }

  function header(opts) {
    const left = opts.back ? h("button", { class: "icon-btn", "aria-label": t("ob.back"), onclick: () => (history.length > 1 ? history.back() : go(opts.back)) }, icon("back")) :
      h("div", { class: "brand" }, h("img", { src: "/logo.png", alt: "", class: "brand-logo" }), h("span", { class: "brand-name" }, "SnapLab"));
    return h("header", { class: "top" + (opts.back ? " sub" : "") },
      left,
      opts.titleEl || (opts.title ? h("div", { class: "top-title" }, opts.title) : h("div", { class: "top-title" })),
      h("div", { class: "top-actions" }, healthDot(), opts.back ? null : needsPill(), opts.actions || [],
        opts.back ? null : h("a", { class: "icon-btn", href: "#/settings", "aria-label": t("nav.settings"), onclick: () => { S.settingsFrom = location.hash; } }, icon("gear"))));
  }

  // ------------------------------------------------------------------ connection band (F18)

  function healthLevel() {
    if (!S.connected) return "bad";
    return (S.status && S.status.level) || "ok";
  }
  function healthDot() {
    return h("button", { class: "health-dot lv-" + healthLevel(), id: "health-dot", type: "button", "aria-label": t("health.title"), onclick: statusSheet },
      h("span", { class: "hd" }));
  }
  function healthText(i) {
    const k = "health." + i.id + "." + (i.code || i.state);
    const v = t(k, { n: i.in_use, count: i.count, waiting: i.waiting, running: i.running, minutes: i.minutes, label: i.label || "" });
    return v === k ? t("health.state." + i.state) : v;
  }
  function paintHealth() {
    const dot = $("#health-dot");
    if (dot) dot.className = "health-dot lv-" + healthLevel();
    const band = $("#health-band");
    if (!band) return;
    const bad = S.connected && S.status ? (S.status.items || []).find((i) => i.state === "bad") : null;
    band.classList.toggle("show", !!bad);
    if (bad) { $(".hb-text", band).textContent = t("health.name." + bad.id) + " · " + healthText(bad); }
  }
  function statusSheet() {
    const rows = h("div", { class: "health-rows" });
    const fill = () => {
      rows.innerHTML = "";
      const items = [{ id: "pc", state: S.connected ? "ok" : "bad", code: S.connected ? "ok" : "off" }].concat((S.status && S.status.items) || []);
      items.forEach((i) => rows.appendChild(h("div", { class: "health-row st-" + i.state },
        icon(i.id === "pc" ? "pc" : "connection"), h("span", { class: "hr-dot" }), h("div", { class: "hr-main" }, h("b", {}, t("health.name." + i.id)), h("span", {}, healthText(i))))));
      if (S.status && S.status.checked) rows.appendChild(h("p", { class: "hint" }, t("health.checked", { when: relTime(S.status.checked) })));
    };
    fill();
    sheet(t("health.title"), [rows]);
    api("/api/status").then((j) => { S.status = j.status; fill(); paintHealth(); }).catch(() => {});
  }

  function tabbar(active) {
    return h("nav", { class: "tabbar" }, TABS.map(([name, ic, label]) =>
      h("a", { class: "tab" + (active === name ? " on" : ""), href: "#/" + name, "data-tab": name }, icon(ic), h("span", { class: "tab-label" }, t(label)), h("span", { class: "tab-dot" }))));
  }
  function attentionWork() { return S.sessions.filter((s) => ["waiting", "failed", "review"].includes(s.status)); }
  function needsCount() { return S.questions.length + attentionWork().length; }
  function needsPill() {
    const n = needsCount();
    return h("button", { class: "needs-pill" + (n ? " show" : ""), id: "needs-pill", type: "button", "aria-label": t("needs.title"), onclick: needsSheet },
      icon("inbox"), h("span", { class: "needs-n" }, String(n)));
  }
  function updateTabBadges() {
    const dot = $('[data-tab="chats"] .tab-dot');
    if (dot) dot.classList.toggle("show", S.questions.length > 0);
    const pill = $("#needs-pill");
    if (pill) { const n = needsCount(); pill.classList.toggle("show", n > 0); $(".needs-n", pill).textContent = String(n); }
  }
  function needsSheet() {
    const body = h("div", { class: "needs-body" });
    const paint = () => {
      body.innerHTML = "";
      const qs = S.questions.slice().sort((a, b) => String(b.created || "").localeCompare(String(a.created || "")));
      const ss = attentionWork().sort(byUrgency);
      if (!qs.length && !ss.length) { body.appendChild(h("div", { class: "needs-empty" }, h("img", { src: "/brand/sail-blue.png", alt: "" }), h("p", {}, t("needs.empty")))); return; }
      if (qs.length) {
        body.appendChild(h("h3", { class: "section" }, t("needs.cards")));
        qs.forEach((q) => {
          const b = bot(q.bot);
          body.appendChild(h("div", { class: "needs-q" }, h("a", { class: "needs-from", href: "#/room/" + q.room, onclick: () => sh.close() }, avatar(b, "xs"), h("span", {}, b ? b.name : ""), icon("right")), questionCard(q)));
        });
      }
      if (ss.length) { body.appendChild(h("h3", { class: "section" }, t("needs.sessions"))); body.appendChild(h("div", { class: "sess-list" }, ss.map((s) => { const r = sessionRow(s); r.addEventListener("click", () => sh.close()); return r; }))); }
    };
    paint();
    const sh = sheet(t("needs.title"), body);
  }

  function screen(opts, body, extra) {
    const app = $("#app");
    app.className = "screen-" + (opts.cls || S.route.name);
    app.innerHTML = "";
    const main = h("main", { class: "content" + (opts.flush ? " flush" : "") }, body);
    app.appendChild(header(opts));
    app.appendChild(h("div", { class: "conn" }, h("span", { class: "spinner" }), t("err.offline")));
    app.appendChild(h("button", { class: "health-band", id: "health-band", type: "button", onclick: statusSheet }, icon("alert"), h("span", { class: "hb-text" }), icon("right")));
    app.appendChild(main);
    if (extra) app.appendChild(extra);
    if (!opts.noTabs) app.appendChild(tabbar(opts.tab || S.route.name));
    updateTabBadges();
    paintHealth();
    return main;
  }

  function confirmSheet(title, body, okLabel) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const yes = h("button", { class: "btn primary", type: "button" }, icon("check"), okLabel || t("common.ok"));
      const no = h("button", { class: "btn ghost", type: "button" }, t("common.cancel"));
      const sh = sheet(title, [h("div", { class: "confirm-body" }, body), h("div", { class: "qcard-actions" }, no, yes)], () => finish(false));
      yes.addEventListener("click", () => { finish(true); sh.close(); });
      no.addEventListener("click", () => { finish(false); sh.close(); });
    });
  }

  const activeSheets = [];
  function sheet(title, body, onClose) {
    const previousFocus = document.activeElement;
    const scrim = h("div", { class: "scrim" });
    const grip = h("div", { class: "sheet-grip", "aria-hidden": "true" });
    const panel = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": title, tabindex: "-1" },
      grip,
      h("div", { class: "sheet-head" }, h("h2", {}, title), h("button", { class: "icon-btn", "aria-label": t("common.close"), onclick: () => close() }, icon("x"))),
      h("div", { class: "sheet-body" }, body));
    const depth = activeSheets.length;
    scrim.style.zIndex = 40 + depth * 2;
    panel.style.zIndex = 41 + depth * 2;
    let closed = false, drag = null;
    function syncLock() {
      const locked = activeSheets.length > 0;
      document.documentElement.classList.toggle("sheet-open", locked);
      document.body.classList.toggle("sheet-open", locked);
      const app = $("#app"); if (app) app.inert = locked;
      activeSheets.forEach((sh, i) => { sh.panel.inert = i !== activeSheets.length - 1; });
    }
    function close() {
      if (closed) return;
      closed = true;
      activeSheets.splice(activeSheets.indexOf(handle), 1);
      panel.style.transform = ""; panel.style.transition = "";
      scrim.classList.remove("in"); panel.classList.remove("in"); panel.inert = true;
      document.removeEventListener("keydown", onKey);
      syncLock();
      setTimeout(() => { scrim.remove(); panel.remove(); }, 260);
      if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
      if (onClose) onClose();
    }
    function onKey(e) {
      if (activeSheets[activeSheets.length - 1] !== handle) return;
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Tab") {
        const items = $$("button, a[href], input, textarea, select, [tabindex='0']", panel).filter((el) => !el.disabled && el.getClientRects().length);
        const first = items[0] || panel, last = items[items.length - 1] || panel;
        if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && (document.activeElement === last || document.activeElement === panel)) { e.preventDefault(); first.focus(); }
      }
    }
    const handle = { close, panel };
    grip.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      drag = { id: e.pointerId, y: e.clientY, delta: 0 }; grip.setPointerCapture(e.pointerId);
      panel.style.transition = "none"; e.preventDefault();
    });
    grip.addEventListener("pointermove", (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      drag.delta = Math.max(0, e.clientY - drag.y);
      panel.style.transform = "translateY(" + drag.delta + "px)"; e.preventDefault();
    });
    const endDrag = (e) => {
      if (!drag || drag.id !== e.pointerId) return;
      const dismiss = e.type !== "pointercancel" && drag.delta >= 64;
      drag = null; panel.style.transition = ""; panel.style.transform = "";
      if (dismiss) close();
    };
    grip.addEventListener("pointerup", endDrag); grip.addEventListener("pointercancel", endDrag);
    scrim.addEventListener("click", close);
    document.body.appendChild(scrim); document.body.appendChild(panel);
    activeSheets.push(handle); syncLock(); document.addEventListener("keydown", onKey);
    requestAnimationFrame(() => { if (!closed) { scrim.classList.add("in"); panel.classList.add("in"); panel.focus(); } });
    return handle;
  }

  // floating action button: the main "new" action sits where the thumb is
  function fab(label, ic, onclick) {
    return h("button", { class: "fab", type: "button", onclick }, icon(ic), h("span", {}, label));
  }

  function counted(input, max) {
    const c = h("span", { class: "count-hint" });
    const fit = () => {
      const a = Array.from(input.value);
      if (a.length > max) { input.value = a.slice(0, max).join(""); }
      c.textContent = t("ob.name.count", { n: Math.min(a.length, max), max });
      c.classList.toggle("full", a.length >= max);
    };
    input.removeAttribute("maxlength");
    input.addEventListener("input", fit); fit();
    return h("div", { class: "counted" }, input, c);
  }
  function field(label, input, note) {
    return h("label", { class: "field" }, h("span", { class: "field-label" }, label), input, note ? h("span", { class: "field-note" }, note) : null);
  }
  function autosize(ta, max) {
    const fit = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, max || 160) + "px"; };
    ta.addEventListener("input", fit); setTimeout(fit, 0);
    return ta;
  }
  function swatches(selected, onPick) {
    const wrap = h("div", { class: "swatches" });
    (S.palette || ["#E5162B", "#2F6FED", "#12A37F", "#8B5CF6", "#F59E0B", "#0EA5E9", "#EC4899", "#64748B"]).forEach((c) => {
      const b = h("button", { type: "button", class: "swatch" + (c.toUpperCase() === (selected || "").toUpperCase() ? " on" : ""), style: { "--c": c }, "aria-label": c,
        onclick: () => { $$(".swatch", wrap).forEach((x) => x.classList.remove("on")); b.classList.add("on"); onPick(c); } });
      wrap.appendChild(b);
    });
    return wrap;
  }

  // ------------------------------------------------------------------ main render

  function render() {
    if (!S.settings) return;
    if (!S.settings.onboarded) return renderOnboarding();
    if (!S.settings.tour_done && !viaPhone()) setTimeout(showTour, 600);
    const r = S.route;
    if (r.name !== "browser") closeBrowserStream();
    document.body.classList.toggle("in-room", r.name === "room");
    switch (r.name) {
      case "room": return renderRoom(r.id);
      case "projects": return renderProjects();
      case "project": return renderProject(r.id);
      case "session": return renderSession(r.id);
      case "home": return renderHome();
      case "dashboard": return renderDashboard();
      case "usage": return renderUsage();
      case "bots": return renderBots();
      case "bot": return renderBot(r.id);
      case "settings": return renderSettings();
      case "phone": return renderPhone();
      case "browser": return renderBrowser(r.id);
      default: return renderChats();
    }
  }


  // ------------------------------------------------------------------ browser panel (the session's live browser)

  let browserLive = null;
  function closeBrowserStream() { if (browserLive) { browserLive.close(); browserLive = null; } }

  // Special keys go to the page as key presses; text (and finished IME text) goes as inserted text.
  const BP_KEYS = { Enter: 13, Backspace: 8, Tab: 9, Escape: 27, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39,
    ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34 };
  const bpMods = (e) => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);

  function renderBrowser(sid) {
    closeBrowserStream();
    const sess = S.sessions.find((x) => x.id === sid) || S.sessionDetail[sid] || {};
    let state = { status: "starting", tabs: [], holder: "agent", mode: null }, size = { w: 1024, h: 768 }, dialogSheet = null;
    const canvas = h("canvas", { class: "bp-canvas", width: 1024, height: 640, "aria-label": t("browser.page") });
    const ctx = canvas.getContext("2d");
    const veil = h("div", { class: "bp-veil" }, h("span", { class: "spinner" }), h("span", { class: "bp-veil-text" }, t("browser.starting")));
    const chip = h("span", { class: "chip bp-chip" });
    const url = h("input", { class: "input bp-url", type: "text", inputmode: "url", autocomplete: "off", autocapitalize: "off",
      spellcheck: "false", placeholder: t("browser.url.ph"), enterkeyhint: "go" });
    const typing = h("textarea", { class: "input bp-type", rows: "1", autocomplete: "off", autocapitalize: "off", spellcheck: "false",
      placeholder: t("browser.type.ph"), "aria-label": t("browser.keyboard") });
    const post = (body) => api("/api/browser/" + sid, body);
    let queue = [], sending = false;
    const flush = async () => {
      if (sending || !queue.length) return;
      sending = true;
      const events = queue.splice(0, 100);
      try { await post({ action: "input", events }); } catch (e) { if (!/take over/.test(e.message || "")) fail(e); }
      sending = false;
      if (queue.length) flush();
    };
    const send = (ev) => { if (!state.can_input) return; queue.push(ev); flush(); };
    const point = (e) => {
      const r = canvas.getBoundingClientRect(), fit = (v) => (v >= 0 && v <= 1 ? v : v > 1 ? 1 : 0);
      return { x: fit((e.clientX - (r.left || 0)) / (r.width || 1)), y: fit((e.clientY - (r.top || 0)) / (r.height || 1)) };
    };
    const btn = (ic, label, fn) => h("button", { class: "icon-btn", type: "button", "aria-label": label, title: label, onclick: fn }, icon(ic));
    const nav = (to, extra) => post({ action: "nav", to, ...(extra || {}) }).catch(fail);
    const tabsBtn = h("button", { class: "btn ghost small bp-tabs", type: "button", onclick: () => tabsSheet() }, icon("layers"), h("span", { class: "bp-tab-n" }, "0"));
    const navButtons = [btn("back", t("browser.back"), () => nav("back")), btn("right", t("browser.forward"), () => nav("forward")),
      btn("refresh", t("browser.reload"), () => nav("reload"))];
    const control = h("button", { class: "btn primary", type: "button", onclick: async () => {
      try { await post({ action: "control", holder: state.holder === "user" ? "agent" : "user" }); } catch (e) { fail(e); }
    } });
    const tabsSheet = () => {
      let sh = null;
      const list = h("div", { class: "bp-tablist" }, state.tabs.map((tb) => h("div", { class: "bp-tabrow" + (tb.active ? " on" : "") },
        h("button", { class: "bp-tabmain", type: "button", onclick: async () => { sh.close(); try { await post({ action: "tab", to: "show", tab: tb.id }); } catch (e) { fail(e); } } },
          h("b", {}, tb.title || tb.url || "about:blank"), h("span", { class: "muted" }, tb.url)),
        h("button", { class: "icon-btn", type: "button", "aria-label": t("browser.closetab"), onclick: async () => { sh.close(); try { await post({ action: "tab", to: "close", tab: tb.id }); } catch (e) { fail(e); } } }, icon("x")))));
      sh = sheet(t("browser.tabs"), [list, h("button", { class: "btn ghost", type: "button", onclick: async () => { sh.close(); try { await post({ action: "tab", to: "new" }); } catch (e) { fail(e); } } }, icon("plus"), t("browser.newtab"))]);
    };
    const showDialog = () => {
      if (dialogSheet) { dialogSheet.close(); dialogSheet = null; }
      const d = state.dialog;
      if (!d || !state.can_input) return;
      const answer = d.type === "prompt" ? h("input", { class: "input", value: d.default || "" }) : null;
      const reply = async (accept) => { const sh = dialogSheet; dialogSheet = null; if (sh) sh.close(); try { await post({ action: "dialog", accept, text: answer ? answer.value : "" }); } catch (e) { fail(e); } };
      dialogSheet = sheet(t("browser.dialog"), [h("p", { class: "bp-dialog-text" }, d.message || ""), answer,
        h("div", { class: "qcard-actions" }, d.type !== "alert" ? h("button", { class: "btn ghost", type: "button", onclick: () => reply(false) }, t("common.cancel")) : null,
          h("button", { class: "btn primary", type: "button", onclick: () => reply(true) }, t("common.ok")))]);
    };
    const paintState = () => {
      const active = state.tabs.find((x) => x.active) || {};
      if (document.activeElement !== url) url.value = active.url && active.url !== "about:blank" ? active.url : "";
      $(".bp-tab-n", tabsBtn).textContent = String(state.tabs.length);
      const ready = state.status === "ready";
      veil.classList.toggle("hide", ready);
      $(".bp-veil-text", veil).textContent = state.error ? state.error : t(state.status === "waiting" ? "browser.waiting" : "browser.starting");
      chip.className = "chip bp-chip " + (state.holder === "user" ? "good" : "");
      chip.textContent = !ready ? t("browser.connecting") : state.mode === "host" ? t("browser.host") : state.holder === "user" ? t("browser.you") : t("browser.agent");
      control.textContent = state.holder === "user" ? t("browser.handback") : t("browser.takeover");
      control.className = "btn " + (state.holder === "user" ? "ghost" : "primary");
      control.classList.toggle("hide", state.mode !== "turn" || !ready);     // no agent between turns
      canvas.classList.toggle("can-input", !!state.can_input);
      [...navButtons, url, tabsBtn, typing].forEach((el) => (el.disabled = !state.can_input));
      showDialog();
    };
    // Only the newest frame is drawn; a frame that arrives while one is decoding replaces the waiting one.
    let decoding = false, waiting = null;
    const draw = (f) => {
      if (decoding) { waiting = f; return; }
      decoding = true;
      const img = new Image();
      img.onload = () => {
        if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) { canvas.width = img.naturalWidth; canvas.height = img.naturalHeight; }
        ctx.drawImage(img, 0, 0);
        decoding = false;
        if (waiting) { const n = waiting; waiting = null; draw(n); }
      };
      img.onerror = () => { decoding = false; };
      img.src = "data:image/jpeg;base64," + f.jpeg;
      if (f.w && f.h) size = { w: f.w, h: f.h };
    };
    // Mouse: presses, releases and moves (moves at most every 50 ms); touch: a tap clicks, a drag scrolls.
    let lastMove = 0, touch = null;
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    canvas.addEventListener("pointerdown", (e) => {
      if (!state.can_input) return;
      e.preventDefault();
      if (e.pointerType === "mouse") {
        typing.focus({ preventScroll: true });
        send({ t: "down", ...point(e), button: ["left", "middle", "right"][e.button] || "left", buttons: e.buttons, count: e.detail || 1, mods: bpMods(e) });
      } else touch = { x: e.clientX, y: e.clientY, t: Date.now(), moved: false, last: point(e), lastY: e.clientY, lastX: e.clientX };
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!state.can_input) return;
      if (e.pointerType === "mouse") {
        if (Date.now() - lastMove < 50) return;
        lastMove = Date.now();
        send({ t: "move", ...point(e), buttons: e.buttons, mods: bpMods(e) });
      } else if (touch) {
        if (Math.hypot(e.clientX - touch.x, e.clientY - touch.y) > 8) touch.moved = true;
        if (touch.moved && Date.now() - lastMove > 60) {
          const r = canvas.getBoundingClientRect(), k = size.w / r.width;
          send({ t: "wheel", ...point(e), dx: (touch.lastX - e.clientX) * k, dy: (touch.lastY - e.clientY) * k });
          touch.lastX = e.clientX; touch.lastY = e.clientY; lastMove = Date.now();
        }
      }
    });
    canvas.addEventListener("pointerup", (e) => {
      if (!state.can_input) return;
      if (e.pointerType === "mouse") send({ t: "up", ...point(e), button: ["left", "middle", "right"][e.button] || "left", buttons: e.buttons, count: e.detail || 1, mods: bpMods(e) });
      else if (touch && !touch.moved) { send({ t: "tap", ...point(e) }); typing.focus({ preventScroll: true }); }
      touch = null;
    });
    canvas.addEventListener("wheel", (e) => {
      if (!state.can_input) return;
      e.preventDefault();
      const k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1;
      send({ t: "wheel", ...point(e), dx: e.deltaX * k, dy: e.deltaY * k, mods: bpMods(e) });
    }, { passive: false });
    // Typing: the bar at the bottom holds what is being composed (IME); finished text goes to the page at once.
    let composing = false, committed = null;
    typing.addEventListener("compositionstart", () => { composing = true; });
    typing.addEventListener("compositionend", (e) => {
      composing = false;
      if (e.data) { send({ t: "text", text: e.data }); committed = e.data; }
      typing.value = "";
    });
    typing.addEventListener("input", (e) => {
      if (composing || e.isComposing) return;
      if (e.inputType === "insertCompositionText" || (committed !== null && e.data === committed)) { committed = null; typing.value = ""; return; }
      committed = null;
      if (e.inputType === "deleteContentBackward") { send({ t: "key", type: "down", key: "Backspace", code: "Backspace", vk: 8 }); send({ t: "key", type: "up", key: "Backspace", code: "Backspace", vk: 8 }); }
      else if (e.inputType === "insertLineBreak") { send({ t: "key", type: "down", key: "Enter", code: "Enter", vk: 13, text: "\r" }); send({ t: "key", type: "up", key: "Enter", code: "Enter", vk: 13 }); }
      else if (typing.value) send({ t: "text", text: typing.value });
      typing.value = "";
    });
    typing.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229 || composing) return;
      const vk = BP_KEYS[e.key];
      const combo = (e.ctrlKey || e.metaKey) && e.key.length === 1;
      if (!vk && !combo) return;                 // ordinary characters arrive as input
      e.preventDefault();
      const key = { key: e.key, code: e.code, vk: vk || e.key.toUpperCase().charCodeAt(0), mods: bpMods(e) };
      send({ t: "key", type: "down", ...key, ...(e.key === "Enter" ? { text: "\r" } : {}) });
      send({ t: "key", type: "up", ...key });
    });
    const form = h("form", { class: "bp-bar", onsubmit: (e) => { e.preventDefault(); if (url.value.trim()) nav("go", { url: url.value.trim() }); url.blur(); } },
      ...navButtons, url, tabsBtn);
    const body = h("div", { class: "bp" },
      form,
      h("div", { class: "bp-row" }, chip, control),
      h("div", { class: "bp-stage" }, canvas, veil),
      typing,
      h("p", { class: "hint bp-note" }, t("browser.note")));
    screen({ back: "#/session/" + sid, title: t("browser.title") + (sess.title ? " · " + sess.title : ""), cls: "browser", noTabs: true }, body);
    paintState();
    // A phone gets pages laid out for its screen while it holds the browser.
    const narrow = (window.innerWidth || 1024) < 700;
    const es = new EventSource("/api/browser/" + sid + "/live" + (narrow ? "?phone=1&vw=" + Math.round(window.innerWidth) + "&vh=" + Math.round((window.innerHeight || 800) * 0.62) : ""));
    es.addEventListener("frame", (e) => { try { draw(JSON.parse(e.data)); } catch (err) { /* a broken frame is skipped */ } });
    es.addEventListener("state", (e) => { try { state = JSON.parse(e.data); paintState(); } catch (err) { /* ignored */ } });
    browserLive = es;
  }

  // ------------------------------------------------------------------ first-run tour and phone access

  const viaPhone = () => !/^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname);

  function showTour() {
    if (S.tourShown) return;
    S.tourShown = true;
    const slides = [["pc", "tour.pc"], ["chat", "tour.bots"], ["terminal", "tour.code"], ["phone", "tour.phone"]];
    let i = 0, sh = null;
    const body = h("div", { class: "tour" });
    const remember = () => api("/api/settings", { tour_done: true }).then((j) => { S.settings = j.settings; }).catch(() => {});
    const finish = async (phone) => {
      await remember();
      if (!phone) api("/api/phone", { action: "decline" }).catch(() => {});
      sh.close();
      if (phone) go("#/phone");
    };
    const paint = () => {
      const [ic, key] = slides[i];
      const last = i === slides.length - 1;
      body.replaceChildren(
        h("div", { class: "tour-icon" }, icon(ic)),
        h("h3", { class: "tour-title" }, t(key)),
        h("p", { class: "tour-text" }, t(key + ".body")),
        h("div", { class: "ob-dots tour-dots" }, slides.map((_, n) => h("span", { class: "ob-dot" + (n <= i ? " on" : "") }))),
        h("div", { class: "qcard-actions" },
          last ? h("button", { class: "btn ghost", type: "button", onclick: () => finish(false) }, t("tour.notnow"))
            : i > 0 ? h("button", { class: "btn ghost", type: "button", onclick: () => { i -= 1; paint(); } }, t("ob.back")) : null,
          last ? h("button", { class: "btn primary", type: "button", onclick: () => finish(true) }, icon("phone"), t("tour.setup"))
            : h("button", { class: "btn primary", type: "button", onclick: () => { i += 1; paint(); } }, t("ob.next"))));
    };
    paint();
    sh = sheet(t("tour.title"), body, () => { if (!S.settings.tour_done) remember(); });
  }

  function renderPhone() {
    const body = h("div", { class: "page phone-page" });
    screen({ back: "#/settings", title: t("phone.title"), tab: "settings" }, body);
    let last = "";
    const poll = async () => {
      if (S.route.name !== "phone") return;
      try {
        const st = await api("/api/phone");
        const key = JSON.stringify(st);
        if (key !== last) { last = key; body.replaceChildren(...phoneView(st)); }
      } catch (e) { /* offline: try again */ }
      clearTimeout(S.phoneTimer);
      S.phoneTimer = setTimeout(poll, 2500);
    };
    poll();
  }

  function phoneAction(action) {
    return api("/api/phone", { action }).then(() => { clearTimeout(S.phoneTimer); renderPhone(); }).catch(fail);
  }

  function phoneView(st) {
    const out = [];
    if (st.via_phone) {
      out.push(h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("check"), t("phone.here")), h("p", { class: "hint" }, t("phone.here.body"))));
      return out;
    }
    out.push(h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("shield"), t("phone.how")), h("p", { class: "hint" }, t("phone.how.body"))));
    const active = ["install", "signin", "https", "pair", "done", "pair_timeout"].includes(st.step);
    if (!active && !st.error) {
      if (st.enabled) {
        out.push(h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("phone"), t("phone.on")),
          h("p", { class: "hint" }, t("phone.on.body")), h("code", { class: "phone-url" }, st.url || ""),
          h("div", { class: "qcard-actions" },
            h("button", { class: "btn ghost", type: "button", onclick: () => phoneAction("disable") }, t("phone.off")),
            h("button", { class: "btn primary", type: "button", onclick: () => phoneAction("pair") }, icon("plus"), t("phone.pair")))));
      } else {
        out.push(h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("phone"), t("phone.start")),
          h("p", { class: "hint" }, t("phone.start.body")),
          h("button", { class: "btn primary wide", type: "button", onclick: () => phoneAction("start") }, t("phone.setup"))));
      }
      return out;
    }
    const order = ["install", "signin", "https", "pair"];
    const at = st.step === "done" ? 4 : st.step === "pair_timeout" ? 3 : order.indexOf(st.step);
    out.push(h("div", { class: "card form phone-steps" }, order.map((k, n) => h("div", { class: "phone-step" + (n < at ? " done" : n === at ? " now" : "") + (n === at && st.error ? " bad" : "") },
      h("span", { class: "phone-dot" }, n < at ? icon("check") : String(n + 1)), h("span", {}, t("phone.step." + k))))));
    const card = h("div", { class: "card form" });
    out.push(card);
    if (st.error) {
      append(card, [h("h3", { class: "card-h" }, icon("alert"), t("phone.stopped")), h("p", { class: "hint" }, t("phone.err." + st.error)),
        st.error === "install_manually" ? h("a", { class: "btn ghost", href: "https://tailscale.com/download", target: "_blank", rel: "noopener" }, t("phone.download")) : null,
        h("button", { class: "btn primary", type: "button", onclick: () => phoneAction("start") }, icon("refresh"), t("phone.retry"))]);
    } else if (st.step === "install") {
      append(card, [h("h3", { class: "card-h" }, h("span", { class: "spinner" }), t("phone.installing")), h("p", { class: "hint" }, t("phone.installing.body"))]);
    } else if (st.step === "signin") {
      append(card, [h("h3", { class: "card-h" }, t("phone.signin")), h("p", { class: "hint" }, t("phone.signin.body")),
        st.auth_url ? h("a", { class: "btn primary wide", href: st.auth_url, target: "_blank", rel: "noopener" }, t("phone.signin.go"))
          : h("p", { class: "hint" }, h("span", { class: "spinner" }), t("phone.wait"))]);
    } else if (st.step === "https") {
      append(card, [h("h3", { class: "card-h" }, t("phone.https")), h("p", { class: "hint" }, t("phone.https.body")),
        st.enable_url ? h("a", { class: "btn primary wide", href: st.enable_url, target: "_blank", rel: "noopener" }, t("phone.https.go"))
          : h("p", { class: "hint" }, h("span", { class: "spinner" }), t("phone.wait"))]);
    } else if (st.step === "pair" || st.step === "pair_timeout") {
      const qr = h("div", { class: "phone-qr", role: "img", "aria-label": t("phone.qr") });
      if (st.qr) qr.innerHTML = st.qr;
      append(card, [h("h3", { class: "card-h" }, t("phone.pair.title")),
        h("ol", { class: "phone-list" },
          h("li", {}, t("phone.pair.app"), " ", h("a", { href: "https://apps.apple.com/app/tailscale/id1470499037", target: "_blank", rel: "noopener" }, "App Store"), " · ",
            h("a", { href: "https://play.google.com/store/apps/details?id=com.tailscale.ipn", target: "_blank", rel: "noopener" }, "Google Play")),
          h("li", {}, t("phone.pair.scan"))),
        st.step === "pair" ? qr : null,
        st.pair_token && st.url ? h("p", { class: "hint phone-link" }, t("phone.pair.link"), " ", h("code", {}, st.url + "/pair/" + st.pair_token)) : null,
        h("button", { class: "btn ghost", type: "button", onclick: () => phoneAction("pair") }, icon("refresh"), t("phone.pair.new"))]);
    } else if (st.step === "done") {
      append(card, [h("h3", { class: "card-h" }, icon("check"), t("phone.done")), h("p", { class: "hint" }, t("phone.done.body")),
        h("button", { class: "btn primary wide", type: "button", onclick: () => api("/api/phone", { action: "ack" }).catch(() => {}).then(() => go("#/home")) }, t("phone.finish"))]);
    }
    return out;
  }

  // ------------------------------------------------------------------ onboarding

  function renderOnboarding() {
    if (!S.ob) S.ob = { step: 0, lang: S.lang, name: "", color: "#F59E0B", avatar: "c1", tone: "friendly", tone_custom: "", call_user: "", prompt: "", promptEdited: false };
    const ob = S.ob;
    S.lang = ob.lang;
    document.documentElement.lang = S.lang; window.snapRememberLang(S.lang);
    const app = $("#app");
    app.className = "screen-onboarding step-" + ob.step;
    app.innerHTML = "";
    const total = 3;
    const wrap = h("div", { class: "ob" });
    app.appendChild(wrap);
    if (ob.step === 0) {
      wrap.appendChild(h("div", { class: "ob-hero" },
        h("div", { class: "ob-sea" }, h("img", { src: "/brand/sail-navy.png", alt: "", class: "ob-ghost g1" }), h("img", { src: "/brand/sail-gold.png", alt: "", class: "ob-ghost g2" }), h("img", { src: "/logo.png", alt: "SnapLab", class: "ob-logo" }), h("div", { class: "ob-wave" })),
        h("h1", { class: "ob-word" }, "SnapLab"),
        h("p", { class: "ob-tag" }, t("app.tagline")),
        h("p", { class: "ob-sub" }, t("app.sub"))));
      wrap.appendChild(h("div", { class: "ob-panel" },
        h("p", { class: "ob-label" }, S.lang === "ru" ? t("lang.pick") : window.snapLangs().map((l) => window.snapT(l.code, "lang.pick")).join(" · ")),
        h("div", { class: "lang-cards" },
          window.snapLangs().map(({ code, name, hello }) =>
            h("button", { class: "lang-card" + (ob.lang === code ? " on" : ""), type: "button", onclick: () => { ob.lang = code; ob.step = 1; renderOnboarding(); } },
              h("span", { class: "lang-hello" }, hello), h("span", { class: "lang-name" }, name), icon("right"))))));
      return;
    }
    const steps = h("div", { class: "ob-top" },
      h("button", { class: "icon-btn", type: "button", "aria-label": t("ob.back"), onclick: () => { ob.step -= 1; renderOnboarding(); } }, icon("back")),
      h("div", { class: "ob-dots" }, [1, 2, 3].map((n) => h("span", { class: "ob-dot" + (n <= ob.step ? " on" : "") }))),
      h("span", { class: "ob-count" }, t("ob.step", { n: ob.step, total })));
    wrap.appendChild(steps);
    const body = h("div", { class: "ob-body" });
    wrap.appendChild(body);
    const next = h("button", { class: "btn primary wide", type: "button" }, t(ob.step === 3 ? "ob.finish" : "ob.next"));
    const foot = h("div", { class: "ob-foot" }, next);
    wrap.appendChild(foot);

    if (ob.step === 1) {
      const face = faceField(() => ({ ...ob, initials: initials(ob.name), orchestrator: true }), (value) => { ob.avatar = value; });
      const preview = face.el;
      const nameIn = h("input", { class: "input big", placeholder: t("ob.name.ph"), value: ob.name, autocomplete: "off", enterkeyhint: "next" });
      const sync = () => { ob.name = nameIn.value; face.refresh(); next.disabled = !ob.name.trim(); };
      nameIn.addEventListener("input", sync);
      nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && ob.name.trim()) next.click(); });
      const ideas = window.snapT(ob.lang, "ob.ideas").split(/,\s*/).filter(Boolean);
      const chips = h("div", { class: "name-ideas" }, h("span", { class: "ideas-label" }, t("ob.name.suggest")), ideas.map((n) =>
        h("button", { type: "button", class: "idea", onclick: () => { nameIn.value = n; sync(); nameIn.focus(); } }, n)));
      append(body, [preview, h("h2", { class: "ob-title" }, t("ob.name.title")), h("p", { class: "ob-text" }, t("ob.name.body")),
        field(t("ob.name.label"), counted(nameIn, NAME_MAX)), chips,
        field(t("ob.color.label"), swatches(ob.color, (c) => { ob.color = c; face.refresh(); }))]);
      sync();
      next.addEventListener("click", () => { if (!ob.name.trim()) return; ob.step = 2; renderOnboarding(); });
      setTimeout(() => nameIn.focus(), 350);
    } else if (ob.step === 2) {
      const callIn = h("input", { class: "input", maxlength: "32", placeholder: t("ob.call.ph"), value: ob.call_user, autocomplete: "off" });
      const custom = autosize(h("textarea", { class: "input", rows: "2", maxlength: "200", placeholder: t("ob.custom.ph") }, ob.tone_custom), 120);
      const bubble = h("div", { class: "ob-bubble" });
      const showBubble = () => {
        const sample = ob.tone === "custom" ? (ob.tone_custom || "…") : t("tone." + ob.tone + ".s");
        bubble.innerHTML = "";
        append(bubble, [avatar({ ...ob, initials: initials(ob.name), orchestrator: true }, "sm"), h("div", { class: "bubble-mini" }, h("b", {}, ob.name), h("span", {}, sample))]);
      };
      const cards = h("div", { class: "tone-grid" }, ["friendly", "polite", "concise", "playful", "custom"].map((k) =>
        h("button", { type: "button", class: "tone" + (ob.tone === k ? " on" : ""), "data-tone": k, onclick: (e) => {
          ob.tone = k; $$(".tone", cards).forEach((x) => x.classList.toggle("on", x.dataset.tone === k));
          custom.parentNode.classList.toggle("hide", k !== "custom"); showBubble(); validate();
        } }, h("span", { class: "tone-name" }, t("tone." + k)), h("span", { class: "tone-d" }, t("tone." + k + ".d")))));
      const customField = field(t("tone.custom"), custom);
      customField.classList.toggle("hide", ob.tone !== "custom");
      const whyText = h("span", { class: "ob-why-text" });
      const why = h("p", { class: "ob-why", role: "status" }, icon("help"), whyText);
      const validate = () => {
        const missing = !ob.call_user.trim() ? "ob.call.needed" : (ob.tone === "custom" && !ob.tone_custom.trim()) ? "ob.custom.needed" : "";
        next.disabled = !!missing;
        why.classList.toggle("hide", !missing);
        if (missing) whyText.textContent = t(missing);
      };
      callIn.addEventListener("input", () => { ob.call_user = callIn.value; validate(); });
      custom.addEventListener("input", () => { ob.tone_custom = custom.value; showBubble(); validate(); });
      append(body, [h("h2", { class: "ob-title" }, t("ob.tone.title")), h("p", { class: "ob-text" }, t("ob.tone.body")),
        field(t("ob.call.label"), callIn), cards, customField, bubble]);
      foot.insertBefore(why, next);
      showBubble(); validate();
      next.addEventListener("click", () => { ob.step = 3; ob.promptEdited = false; renderOnboarding(); });
    } else {
      const ta = h("textarea", { class: "input prompt-ta", rows: "12", maxlength: "8000", spellcheck: "false" }, ob.prompt);
      ta.addEventListener("input", () => { ob.prompt = ta.value; ob.promptEdited = true; });
      append(body, [h("h2", { class: "ob-title" }, t("ob.prompt.title", { name: ob.name })), h("p", { class: "ob-text" }, t("ob.prompt.body", { name: ob.name })), ta,
        h("p", { class: "ob-note" }, icon("shield"), h("span", {}, t("ob.prompt.note")))]);
      if (!ob.promptEdited) {
        ta.classList.add("loading");
        api("/api/onboarding/preview", { lang: ob.lang, name: ob.name, tone: ob.tone, tone_custom: ob.tone_custom, call_user: ob.call_user })
          .then((j) => { if (!ob.promptEdited) { ob.prompt = j.prompt; ta.value = j.prompt; } ta.classList.remove("loading"); }).catch(fail);
      }
      next.addEventListener("click", async () => {
        next.disabled = true; next.textContent = t("ob.finishing"); next.classList.add("busy");
        try {
          const j = await api("/api/onboarding", { lang: ob.lang, name: ob.name, color: ob.color, avatar: ob.avatar, tone: ob.tone, tone_custom: ob.tone_custom, call_user: ob.call_user, system_prompt: ta.value });
          await load();
          S.ob = null;
          document.getElementById("app").classList.add("sail-away");
          setTimeout(() => go("#/room/" + j.room), 420);
        } catch (e) { fail(e); next.disabled = false; next.textContent = t("ob.finish"); next.classList.remove("busy"); }
      });
    }
  }
  function initials(name) {
    const parts = String(name || "").replace(/[_-]/g, " ").split(/\s+/).filter(Boolean);
    if (!parts.length) return "";
    if (parts.length === 1) return Array.from(parts[0])[0].toUpperCase();
    return (Array.from(parts[0])[0] + Array.from(parts[1])[0]).toUpperCase();
  }

  // ------------------------------------------------------------------ chats

  function renderChats() {
    const body = h("div", { class: "page" });
    const qs = S.questions;
    if (qs.length) {
      const q = qs[0];
      body.appendChild(h("a", { class: "q-banner", href: "#/room/" + q.room }, icon("help"),
        h("div", {}, h("b", {}, t(qs.length === 1 ? "chats.questions" : "chats.questions.n", { n: qs.length })), h("span", {}, q.question)), icon("right")));
    }
    const rooms = S.rooms.slice().sort((a, b) => String(b.last_ts || "").localeCompare(String(a.last_ts || "")));
    const sections = [
      { key: "chats.pinned", rooms: rooms.filter((r) => r.pinned) },
      { key: "chats.bots", rooms: rooms.filter((r) => !r.pinned && r.kind === "dm") },
      { key: "chats.groups", rooms: rooms.filter((r) => !r.pinned && r.kind === "group") }
    ];
    sections.forEach((section) => {
    if (!section.rooms.length) return;
    const list = h("div", { class: "list card-list", "data-chat-section": section.key });
    body.appendChild(h("section", { class: "chat-section" }, h("h2", {}, t(section.key)), list));
    section.rooms.forEach((r) => {
      const b = r.kind === "dm" ? bot(r.bots[0]) : null;
      const busy = S.busy.has(r.id);
      const hasQ = qs.some((q) => q.room === r.id);
      const rowEl = h("a", { class: "row chat-row", href: "#/room/" + r.id, "data-room": r.id },
        b ? avatar(b, "md") : groupAvatar(r),
        h("div", { class: "row-main" },
          h("div", { class: "row-top" }, h("span", { class: "row-title" }, r.name, b && b.orchestrator ? h("span", { class: "chip tiny brand" }, t("room.orchestrator")) : null, r.kind === "group" ? h("span", { class: "chip tiny" }, t("chats.group")) : null),
            r.last_text ? h("span", { class: "row-time" }, relTime(r.last_ts)) : null),
          r.bot_only ? h("div", {}, h("span", { class: "bot-chat-label" }, icon("users"), t("group.bot_only"))) : null,
          h("div", { class: "row-sub" + (busy ? " typing" : "") + (r.last_text ? "" : " faint") }, busy ? [h("span", { class: "dots" }, h("i"), h("i"), h("i")), t("chats.typing")] :
            r.last_text ? [(r.last_role === "user" ? t("chats.you") + ": " : ""), plain(r.last_text).replace(/\s+/g, " ")] :
            (b ? (b.orchestrator ? t("room.orchestrator") : (b.role || t("chats.nomsg"))) : r.bots.map((x) => (bot(x) || {}).name).filter(Boolean).join(", ")))),
        hasQ ? h("span", { class: "badge-dot" }) : null);
      if (b) longPress(rowEl, () => botMenu(b));
      else if (r.kind === "group") longPress(rowEl, () => membersSheet(r));
      list.appendChild(h("div", { class: "chat-entry" }, rowEl, pinButton(r)));
    });
    });
    if (!rooms.length) body.appendChild(h("p", { class: "empty" }, t("chats.empty")));
    const archived = S.archivedBots || [];
    if (archived.length) body.appendChild(h("a", { class: "row archived-link", href: "#/bots?archived=1" }, icon("archive"),
      h("span", { class: "row-main" }, t("chats.archived", { n: archived.length })), icon("right")));
    if (S.bots.length < 2 || !S.rooms.some((r) => r.kind === "group")) {
      body.appendChild(h("div", { class: "card next-steps" }, h("h3", { class: "card-h" }, icon("spark"), t("chats.next.title")),
        S.bots.length < 2 ? h("button", { class: "next-row", type: "button", onclick: newBotSheet }, icon("person"), h("div", {}, h("b", {}, t("chats.next.bot")), h("span", {}, t("chats.next.bot.d"))), icon("right")) : null,
        S.bots.length >= 2 && !S.rooms.some((r) => r.kind === "group") ? h("button", { class: "next-row", type: "button", onclick: newGroupSheet }, icon("users"), h("div", {}, h("b", {}, t("chats.next.group")), h("span", {}, t("chats.next.group.d"))), icon("right")) : null));
    }
    const actions = h("div", { class: "chat-actions" },
      h("button", { class: "btn chat-act", type: "button", onclick: newGroupSheet, disabled: S.bots.length < 1 }, icon("users-add"), t("chats.newgroup")),
      h("button", { class: "btn chat-act primary", type: "button", onclick: newBotSheet }, icon("person-add"), t("chats.newbot")));
    screen({ title: t("chats.title") }, body, actions);
  }

  function pinButton(room) {
    const button = h("button", { class: "icon-btn pin-chat" + (room.pinned ? " pinned" : ""), type: "button",
      "aria-label": t(room.pinned ? "chats.unpin" : "chats.pin"), "aria-pressed": String(!!room.pinned), "data-room": room.id }, icon("bookmark"));
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const current = S.rooms.find((r) => r.id === room.id) || room;
        const j = await api("/api/rooms/" + room.id, { pinned: !current.pinned });
        upsert(S.rooms, j.room); render();
      } catch (e) { fail(e); button.disabled = false; }
    });
    return button;
  }

  function newGroupSheet() {
    const picked = new Set(S.bots.map((b) => b.id));
    const name = h("input", { class: "input", maxlength: "40", placeholder: t("group.name.ph") });
    const list = h("div", { class: "check-list" }, S.bots.map((b) => {
      const cb = h("input", { type: "checkbox", checked: true, onchange: () => (cb.checked ? picked.add(b.id) : picked.delete(b.id)) });
      return h("label", { class: "check-row" }, cb, avatar(b, "sm"), h("span", {}, b.name), b.orchestrator ? h("span", { class: "chip tiny brand" }, t("room.orchestrator")) : null);
    }));
    const btn = h("button", { class: "btn primary wide", type: "button" }, t("group.create"));
    const sh = sheet(t("group.title"), [field(t("group.name"), name), field(t("group.bots"), list), h("p", { class: "hint" }, t("group.hint")), btn]);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { const j = await api("/api/rooms", { name: name.value, bots: Array.from(picked) }); upsert(S.rooms, j.room); sh.close(); go("#/room/" + j.room.id); }
      catch (e) { fail(e); btn.disabled = false; }
    });
  }

  // ------------------------------------------------------------------ room

  let roomEls = null;

  async function renderRoom(id) {
    const room = S.rooms.find((r) => r.id === id);
    if (!room) { go("#/chats"); return; }
    const b = room.kind === "dm" ? bot(room.bots[0]) : null;
    const titleEl = h("button", { class: "room-title", type: "button", "aria-label": t("inside.open"),
      onclick: () => (b ? go("#/bot/" + b.id + "?room=" + room.id) : membersSheet(room)) }, b ? avatar(b, "sm") : groupAvatar(room),
      h("div", {}, h("div", { class: "room-name" }, room.name, icon("right", "room-chev")), h("div", { class: "room-sub" }, b ? modelDescription(b.model) : t("room.members", { n: room.bots.length }))));
    const log = h("div", { class: "log", id: "log" });
    // Drawing the same room again keeps its composer (the text, the caret and the open keyboard).
    const same = roomEls && roomEls.id === id && roomEls.composer && document.body.contains(roomEls.composer.el);
    const typing = same && document.activeElement === roomEls.composer.ta;
    const caret = typing ? [roomEls.composer.ta.selectionStart, roomEls.composer.ta.selectionEnd] : null;
    const composer = same ? roomEls.composer : buildComposer(room, b);
    const main = screen({ back: "#/chats", titleEl, noTabs: true, flush: true, cls: "room", actions: [
      h("button", { class: "icon-btn", type: "button", "aria-label": t("attachment.list"), title: t("attachment.list"), onclick: () => roomFiles(room) }, icon("folder")),
      b ? h("button", { class: "icon-btn", type: "button", "aria-label": t("room.renamebot"), onclick: () => profileSheet(bot(b.id)) }, icon("edit")) : null,
      h("button", { class: "icon-btn", type: "button", "aria-label": t("room.summary"), title: t("room.summary"), onclick: () => summarySheet(room) }, icon("page"))] },
      h("div", {}, h("div", { class: "room-models" }, (room.bots || []).map(id => { const member=bot(id); return member ? h("div", {}, (b ? "" : member.name + ": ") + modelDescription(member.model)) : null; })), log), composer.el);
    roomEls = { id, log, main, composer };
    composer.dropOn(main);
    if (caret) { try { composer.ta.focus({ preventScroll: true }); composer.ta.setSelectionRange(caret[0], caret[1]); } catch (e) { /* ignore */ } }
    log.appendChild(h("div", { class: "log-pad" }));
    const cached = S.msgs[id];
    if (cached) paintRoom(cached);
    if (S.prefill) {
      const ta = composer.ta;
      if (!ta.value.trim()) { ta.value = S.prefill; ta.dispatchEvent(new Event("input")); }      // never over a saved draft
      S.prefill = null;
    }
    await refreshRoom(id);
  }

  // Fetch a room's messages again and redraw only its log (after a lost connection, for example): the composer
  // and what is being typed in it stay as they are.
  async function refreshRoom(id) {
    try {
      const j = await api("/api/rooms/" + id + "/messages");
      S.msgs[id] = j.messages;
      const before = S.live[id] || {};
      S.live[id] = {};
      j.live.forEach((m) => { S.live[id][m.id] = Object.assign({ _t0: Date.parse(m.ts) || Date.now() }, before[m.id] || {}, m); });
      if (j.busy) S.busy.add(id); else S.busy.delete(id);
      if (!roomEls || roomEls.id !== id) return;
      paintRoom(j.messages);
      setBusy(S.busy.has(id));
    } catch (e) { fail(e); }
  }

  function membersSheet(room) {
    room = S.rooms.find((r) => r.id === room.id) || room;
    const picked = new Set(room.bots);
    const name = h("input", { class: "input group-name", value: room.name, maxlength: "40" });
    const save = h("button", { class: "btn primary wide group-save", type: "button" }, t("common.save"));
    const validate = () => { save.disabled = !picked.size || !name.value.trim(); };
    name.addEventListener("input", validate);
    const list = h("div", { class: "check-list group-members" }, S.bots.map((b) => {
      const cb = h("input", { type: "checkbox", checked: picked.has(b.id), "data-bot": b.id, onchange: () => {
        if (cb.checked) picked.add(b.id); else picked.delete(b.id); validate();
      } });
      return h("label", { class: "check-row" }, cb, avatar(b, "sm"), h("span", {}, b.name));
    }));
    const remove = h("button", { class: "btn danger wide group-delete", type: "button" }, icon("trash"), t("group.delete"));
    const sh = sheet(t("group.manage"), [field(t("group.name"), name), field(t("group.bots"), list),
      h("p", { class: "lead-text" }, t("group.manage.hint")), h("p", { class: "lead-text" }, t("dialogue.limits")), save, remove]);
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const j = await api("/api/rooms/" + room.id, { name: name.value, bots: Array.from(picked) });
        upsert(S.rooms, j.room); sh.close(); render(); toast(t("group.saved"));
      } catch (e) { fail(e); validate(); }
    });
    remove.addEventListener("click", async () => {
      if (!(await confirmSheet(t("group.delete"), h("p", {}, t("group.delete.body", { name: room.name })), t("group.delete")))) return;
      remove.disabled = true;
      try {
        await api("/api/rooms/" + room.id, { action: "delete", confirmed: true });
        S.rooms = S.rooms.filter((r) => r.id !== room.id); sh.close(); go("#/chats");
      } catch (e) { fail(e); remove.disabled = false; }
    });
  }

  function paintRoom(messages) {
    const log = roomEls.log;
    log.innerHTML = "";
    log.appendChild(h("div", { class: "log-pad" }));
    const room0 = S.rooms.find((r) => r.id === roomEls.id) || {};
    if (!messages.length) log.appendChild(room0.kind === "group" ? groupIntro(room0) : h("p", { class: "empty" }, t("room.empty")));
    let lastDay = "";
    messages.forEach((m) => {
      const day = (m.ts || "").slice(0, 10);
      if (day && day !== lastDay) { log.appendChild(h("div", { class: "day" }, new Date(m.ts).toLocaleDateString(loc(), { weekday: "short", month: "short", day: "numeric" }))); lastDay = day; }
      log.appendChild(messageEl(m));
    });
    Object.values(S.live[roomEls.id] || {}).forEach((m) => log.appendChild(messageEl(m)));
    if (messages.length === 1 && messages[0].role === "bot" && !Object.keys(S.live[roomEls.id] || {}).length) log.appendChild(suggestions());
    scrollEnd(true);
  }
  function summarySheet(room) {
    const body = h("div", { class: "summary-body md" }, "…");
    const meta = h("p", { class: "hint" });
    const btn = h("button", { class: "btn ghost wide", type: "button" }, icon("spark"), t("room.summary.update"));
    const fill = async () => {
      try {
        const j = await api("/api/rooms/" + room.id + "/summary");
        const every = ((S.background || {}).summary || {}).every_messages || 20;
        if (j.text && j.text.trim()) { body.innerHTML = md(j.text); body.classList.remove("muted"); }
        else { body.textContent = t("room.summary.empty", { n: every }); body.classList.add("muted"); }
        meta.textContent = j.running ? t("room.summary.running") : (j.meta && j.meta.updated ? t("room.summary.meta", { when: relTime(j.meta.updated), count: j.meta.count || 0, pending: j.pending }) : "");
        btn.disabled = !!j.running || !j.pending;
      } catch (e) { fail(e); }
    };
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await api("/api/rooms/" + room.id + "/summary", {}); meta.textContent = t("room.summary.running"); } catch (e) { fail(e); btn.disabled = false; }
    });
    const sh = sheet(t("room.summary"), [h("p", { class: "hint" }, t("room.summary.note")), body, meta, btn]);
    S.summarySheet = { room: room.id, fill, sh };
    fill();
  }
  function groupIntro(room) {
    const members = room.bots.map(bot).filter(Boolean);
    const lead = members.find((x) => x.orchestrator) || members[0];
    const fill = (text) => { const ta = $(".composer-input"); if (ta) { ta.value = text; ta.dispatchEvent(new Event("input")); ta.focus(); } };
    return h("div", { class: "group-intro" },
      h("div", { class: "gi-faces" }, members.map((x) => avatar(x, "md"))),
      h("h3", {}, t("group.intro.title", { name: room.name })),
      h("p", { class: "lead-text" }, t("group.intro.body", { lead: lead ? lead.name : "" })),
      h("div", { class: "try" }, h("span", { class: "try-label" }, t("room.try")),
        members.filter((x) => !x.orchestrator).slice(0, 3).map((x) => h("button", { class: "try-chip", type: "button", onclick: () => fill("@" + x.name + " ") }, "@" + x.name + " …")),
        h("button", { class: "try-chip", type: "button", onclick: () => fill(t("group.intro.ask")) }, t("group.intro.ask"))));
  }
  function suggestions() {
    const box = h("div", { class: "try" }, h("span", { class: "try-label" }, t("room.try")),
      [1, 2, 3, 4].map((i) => h("button", { class: "try-chip", type: "button", onclick: () => {
        const ta = $(".composer-input"); if (!ta) return;
        ta.value = t("room.try." + i); ta.dispatchEvent(new Event("input")); ta.focus();
      } }, t("room.try." + i))));
    return box;
  }

  function nearBottom() { const l = roomEls && roomEls.main; return !l || l.scrollHeight - l.scrollTop - l.clientHeight < 140; }
  function scrollEnd(force) {
    const l = roomEls && roomEls.main;
    if (!l) return;
    if (force || nearBottom()) requestAnimationFrame(() => { l.scrollTop = l.scrollHeight; });
  }

  function roomUpsert(m, done) {
    if (!roomEls) return;
    const stick = nearBottom();
    const old = $('[data-mid="' + m.id + '"]', roomEls.log);
    const el = messageEl(m);
    const tr = $(".try", roomEls.log); if (tr && m.role !== "bot") tr.remove();
    if (old) old.replaceWith(el); else { const e = $(".empty", roomEls.log); if (e) e.remove(); roomEls.log.appendChild(el); }
    if (done) setBusy(S.busy.has(roomEls.id));
    if (stick || m.role === "user") scrollEnd(true);
  }

  function streamUpdate(m, tools) {
    if (!roomEls) return;
    const el = $('[data-mid="' + m.id + '"]', roomEls.log);
    if (!el) { roomUpsert(m, false); return; }
    const stick = nearBottom();
    if (tools) { const tw = $(".tools", el); tw.replaceWith(toolsEl(m)); }
    const th = $(".think", el);
    if (th) {
      $(".think-text", th).textContent = (m.thinking || "").slice(-4000);
      th.classList.toggle("has", !!m.thinking);
    }
    const body = $(".bubble-body", el);
    if (m.text) {
      body.innerHTML = md(m.text);
      placeCaret(body);
      el.classList.add("has-text");
      body.parentNode.classList.remove("empty-bubble");
      if (th && !th.dataset.done) {
        th.dataset.done = "1"; th.classList.add("settled");
        $(".think-label", th).textContent = t("msg.thought", { t: dur(Math.max(1, ((m._tFirstText || Date.now()) - m._t0) / 1000)) });
        const dots = $("summary .dots", th); if (dots) dots.remove();
      }
    }
    if (stick) scrollEnd(true);
  }

  // put the typing caret at the end of the last line of text (inside the last paragraph or list item)
  function placeCaret(body) {
    let target = body;
    while (target.lastElementChild && !/^(PRE|CODE|BR)$/.test(target.lastElementChild.tagName)) target = target.lastElementChild;
    if (target.classList && target.classList.contains("md-gap")) target = body;
    target.appendChild(h("span", { class: "caret" }));
  }

  function toolsEl(m) {
    const w = h("div", { class: "tools" });
    (m.tools || []).forEach((c) => {
      const ic = { web_search: "search", read_page: "page", start_session: "play", get_session: "terminal", send_to_session: "send", stop_session: "stop",
        update_dashboard: "layout", ask_user: "help", write_memory: "memory", create_project: "folder", list_projects: "folder", list_sessions: "terminal",
        create_bot: "users", list_bots: "users", write_brief: "page", read_brief: "page", assign_project: "users",
        share_project: "users", message_bot: "send", create_group: "users-add", list_files: "folder", read_file: "file", write_file: "edit" }[c.name] || "spark";
      if (c.name === "ask_user" && m.question) return;          // the question card below says it already
      const st = c.status === "running" ? h("span", { class: "spinner sm" }) : c.status === "error" ? icon("x", "bad") : c.status === "waiting" ? icon("clock", "warn") : icon("check", "good");
      const waiting = c.status === "waiting" && c.name !== "ask_user";
      const verb = waiting ? t("tool.waiting") : toolVerb(c.name);
      const inner = [icon(waiting ? "shield" : ic, "tool-ic"), h("span", { class: "tool-verb" }, verb), c.label ? h("span", { class: "tool-label" }, c.label) : null, st];
      w.appendChild(c.ref ? h("a", { class: "tool st-" + c.status, href: c.ref }, inner) : h("div", { class: "tool st-" + c.status, title: c.detail || "" }, inner));
    });
    return w;
  }

  function foldable(bubble, long) {
    if (!long) return bubble;
    bubble.classList.add("folded");
    const more = h("button", { class: "fold-btn", type: "button", onclick: () => { bubble.classList.toggle("folded"); more.textContent = t(bubble.classList.contains("folded") ? "msg.more" : "msg.less"); } }, t("msg.more"));
    return h("div", { class: "fold-wrap" }, bubble, more);
  }
  function toolVerb(name) {
    const key = "tool." + name, v = t(key);
    if (v !== key) return v;
    const l = (S.toolLabels || {})[name] || {};
    return l[S.lang] || l.en || name;
  }

  function roomFiles(room) {
    const list = h("div", { class: "room-files" });
    let offset = 0, active = true;
    const more = h("button", { type: "button", class: "btn", onclick: () => loadFiles() }, t("attachment.more"));
    sheet(t("attachment.list"), [list, more], () => { active = false; });
    async function loadFiles() {
      more.disabled = true;
      try {
        const response = await api("/api/rooms/" + room.id + "/attachments?offset=" + offset);
        if (!active) return;
        if (!response.attachments.length && offset === 0) list.appendChild(h("p", { class: "attachment-note" }, t("attachment.empty")));
        const cards = attachmentsEl(response);
        if (cards) list.appendChild(cards);
        offset += response.attachments.length;
        more.hidden = !response.has_more;
      } catch (error) { if (active) fail(error); }
      finally { more.disabled = false; }
    }
    loadFiles();
  }

  function attachmentViewer(a) {
    if (a.kind !== "image") return;
    const base = "/api/attachments/" + a.id;
    const content = h("img", { class: "attachment-full", src: base + "/content", alt: a.caption || a.name });
    const sh = sheet(a.name, [
      content,
      h("a", { class: "btn", href: base + "/download", download: a.name }, icon("file"), t("attachment.save"))
    ]);
    sh.panel.classList.add("attachment-viewer");
  }

  function attachmentsEl(m) {
    const items = (Array.isArray(m.attachments) ? m.attachments : []).filter((a) => a && /^f-[0-9a-f]{32}$/.test(a.id));
    if (!items.length && !(m.attachment_errors || []).length) return null;
    return h("div", { class: "attachments" }, items.map((a) => {
      const base = "/api/attachments/" + a.id;
      const preview = ["image", "text", "pdf"].includes(a.kind);
      const image = a.kind === "image" ? h("img", { src: base + "/content", alt: a.caption || a.name, loading: "lazy" }) : null;
      if (image) image.addEventListener("error", () => {
        image.replaceWith(h("p", { class: "attachment-note", role: "status" }, t("attachment.unavailable")));
      });
      return h("div", { class: "attachment-card" },
        image ? h("button", { type: "button", class: "attachment-image", "aria-label": t("attachment.enlarge", { name: a.name }), onclick: () => attachmentViewer(a) }, image) : null,
        h("div", { class: "attachment-info" },
          a.kind === "html" ? h("div", { class: "attachment-type" }, t("attachment.html_file")) : null,
          h("div", { class: "attachment-name" }, a.name),
          a.caption ? h("p", { class: "attachment-caption" }, a.caption) : null,
          h("span", { class: "attachment-size" }, num(Math.max(1, Math.ceil((a.size || 0) / 1024))) + " KB")),
        h("div", { class: "attachment-actions" },
          preview ? (a.kind === "pdf" || a.kind === "text"
            ? h("a", { class: "btn", href: base + "/preview", target: "_blank", rel: "noopener noreferrer" }, icon("eye"), t("attachment.open"))
            : h("button", { type: "button", class: "btn", onclick: () => attachmentViewer(a) }, icon("eye"), t("attachment.open"))) : null,
          h("a", { class: "btn", href: base + "/download", download: a.name }, icon("file"), t("attachment.save"))));
    }), (m.attachment_errors || []).length ? h("p", { class: "attachment-note", role: "status" }, t("attachment.failed")) : null);
  }

  function messageEl(m) {
    if (m.status === "silent") return h("div", { class: "msg silent", "data-mid": m.id });
    if (m.role === "user") {
      return h("div", { class: "msg me", "data-mid": m.id },
        m.answer_to ? h("div", { class: "answer-to" }, icon("help"), h("span", {}, m.answer_to)) : null, myFilesEl(m),
        m.text || !(m.attachments || []).length ? foldable(h("div", { class: "bubble" }, h("div", { class: "bubble-body", html: md(m.text) })), (m.text || "").length > 700) : null,
        h("div", { class: "meta" }, clockTime(m.ts)));
    }
    if (m.role === "system" && m.result && m.session && !m.result_notification) {
      const head = m.title ? t("rc.headline", { title: m.title, project: m.project_name || "" }) : m.text.split("\n")[0];
      const r = m.result, d = r.diff || {}, tt = r.tests;
      return h("div", { class: "msg sys", "data-mid": m.id }, h("a", { class: "result-card st-" + normStatus(r.status), href: "#/session/" + m.session },
        h("div", { class: "rc-head" }, statusPill(r.status), h("span", { class: "rc-label" }, t("rc.title")), icon("right")),
        h("div", { class: "rc-title" }, head),
        h("div", { class: "rc-stats" },
          d.files !== undefined ? h("span", {}, icon("file"), t(d.files === 1 ? "rc.file1" : "rc.files", { n: d.files }), h("b", { class: "plus" }, "+" + (d.insertions || 0)), h("b", { class: "minus" }, "−" + (d.deletions || 0))) : null,
          tt ? h("span", { class: "tests-" + tt.status }, icon("flask"), testsShort(tt)) : null,
          r.duration_s ? h("span", {}, icon("clock"), dur(r.duration_s)) : null)), attachmentsEl(m));
    }
    if (m.role === "system") {
      const text = m.key ? tv(m.key, m.vars) + (m.note ? "\n" + t("srv.assign.note", { note: m.note }) : "") : m.text;
      const first = text.split("\n")[0], rest = text.split("\n").slice(1).join("\n");
      const pill = m.linked_room ? h("a", { class: "sys-pill wrap dialogue-note", href: "#/room/" + m.linked_room }, icon("users"), h("span", {}, first), icon("right"))
        : m.session ? h("a", { class: "sys-pill", href: "#/session/" + m.session }, icon(m.key ? "check" : "terminal"), h("span", {}, first), icon("right"))
        : h("div", { class: "sys-pill" + (m.assignment || (m.key || "").startsWith("srv.fresh") ? " wrap" : "") + ((m.key || "").startsWith("dialogue.") ? " wrap dialogue-note" : "") }, icon(m.assignment ? "folder" : "spark"), h("span", {}, m.assignment ? text : first));
      return h("div", { class: "msg sys", "data-mid": m.id }, pill, !m.result_notification && !m.assignment && rest && !m.key ? h("div", { class: "sys-detail", html: md(rest) }) : null, attachmentsEl(m));
    }
    const b = bot(m.bot) || (S.archivedBots || []).find((x) => x.id === m.bot) || (S.formerBots || []).find((x) => x.id === m.bot) || { name: "Bot", color: "#888", initials: "?" };
    const room = S.rooms.find((r) => r.id === m.room) || (roomEls && S.rooms.find((r) => r.id === roomEls.id));
    const streaming = m.status === "streaming";
    const think = h("details", { class: "think" + (m.thinking ? " has" : "") + (streaming ? "" : " settled") },
      h("summary", {}, icon("brain"), h("span", { class: "think-label" }, streaming && !m.text ? t("msg.thinking") : t("msg.thought", { t: dur(Math.max(1, thinkSecs(m))) })),
        streaming && !m.text ? h("span", { class: "dots" }, h("i"), h("i"), h("i")) : null, icon("chevron", "think-chev")),
      h("div", { class: "think-text" }, (m.thinking || "").slice(-4000)));
    if (!streaming && m.text) think.dataset.done = "1";
    const body = h("div", { class: "bubble-body", html: m.text ? md(m.text) : "" });
    if (streaming && m.text) placeCaret(body);
    const status = (m.status === "error" || m.status === "stopped") ? errorLine(m) : null;
    const q = m.question ? questionCard(Object.assign({}, m.question, (S.questions.find((x) => x.id === m.question.id) || {}))) : null;
    const showName = room && room.kind === "group";
    return h("div", { class: "msg bot" + (streaming ? " streaming" : "") + (m.text ? " has-text" : ""), "data-mid": m.id },
      avatar(b, "sm"),
      h("div", { class: "msg-col" },
        showName ? h("div", { class: "msg-name", style: { color: b.color } }, b.name) : null,
        m.bot_conversation ? h("div", { class: "bot-chat-label" }, icon("users"),
          m.to_bots && m.to_bots.length ? t("msg.to_bots", { names: m.to_bots.map((id) => (bot(id) || {}).name || id).join(", ") }) : t("msg.bot_conversation")) : null,
        (m.thinking || streaming) ? think : null,
        toolsEl(m),
        h("div", { class: "bubble" + (m.text ? "" : " empty-bubble") + (m.needs_you ? " needs" : "") }, body),
        attachmentsEl(m),
        m.needs_you ? h("div", { class: "needs-flag" }, icon("inbox"), t("msg.needs_you")) : null,
        m.pending_ok && m.question && (!m.question.status || m.question.status === "open") ? h("div", { class: "pending-ok" }, icon("shield"), t("msg.pending_ok")) : null,
        status, q,
        !streaming ? h("div", { class: "meta" }, clockTime(m.ts), m.elapsed_s ? " · " + dur(m.elapsed_s) : "",
          m.context ? h("button", { class: "saw-btn", type: "button", "aria-label": t("saw.title", { name: b.name }), onclick: () => sawSheet(m, b) },
            icon("eye"), h("span", {}, t("saw.btn", { n: num(m.context.total || 0) }))) : null) : null));
  }
  function errorLine(m) {
    const stopped = m.status === "stopped";
    const code = m.error_code || (stopped ? "" : "internal");
    const text = stopped ? t("msg.stopped") : t("err.code." + code);
    const retry = !m.retried ? h("button", { class: "retry-btn", type: "button", onclick: async (e) => {
      const btn = e.currentTarget; btn.disabled = true;
      try { await api("/api/rooms/" + (m.room || roomEls.id) + "/retry", { message: m.id }); } catch (err) { btn.disabled = false; fail(err); }
    } }, icon("play"), t("msg.retry")) : h("span", { class: "muted retried" }, t("msg.retried"));
    return h("div", { class: "msg-err" + (stopped ? " muted" : "") },
      h("div", { class: "err-row" }, icon(stopped ? "stop" : "alert"), h("span", { class: "err-text" }, text), retry),
      !stopped && m.error ? h("details", { class: "err-detail" }, h("summary", {}, t("msg.details")), h("pre", {}, m.error)) : null);
  }
  function thinkSecs(m) {
    if (m.status === "streaming") return ((m._tFirstText || Date.now()) - (m._t0 || Date.now())) / 1000;
    if (m.thinking_s) return m.thinking_s;
    return Math.max(1, Math.round((m.thinking || "").length / 160));
  }

  function fieldValue(v) {
    if (typeof v !== "string" || v[0] !== "@" || v.indexOf("|") < 0) return v;
    const [key, ...pairs] = v.slice(1).split("|");
    const vars = {};
    pairs.forEach((kv) => { const i = kv.indexOf("="); vars[kv.slice(0, i)] = kv.slice(i + 1); });
    if (vars.days) vars.days = vars.days.split(",").map((d) => t("day." + d)).join(t("list.sep"));
    if (key === "task.when.every" && Number(vars.n) % 60 === 0) return t("task.when.hours", { n: Number(vars.n) / 60 });
    return t(key, vars);
  }

  function questionCard(q) {
    const answered = q.status && q.status !== "open";
    const approval = q.kind === "approval";
    const p = q.prop || null;
    const title = p ? tv(p.key, p.vars) : q.question;
    const card = h("div", { class: "qcard" + (approval ? " approval" : "") + (answered ? " answered" : ""), "data-q": q.id },
      h("div", { class: "qcard-head" }, icon(approval ? "shield" : "help"), h("span", {}, t(approval ? "q.approval" : "q.title"))),
      h("div", { class: "qcard-q" }, title));
    if (p && (p.fields || []).length) card.appendChild(h("div", { class: "qcard-fields" }, p.fields.map(([k, v]) =>
      h("div", { class: "qf" + (k === "prop.f.instruction" || k === "prop.f.text" ? " long" : "") }, h("span", { class: "qf-k" }, t(k)), h("span", { class: "qf-v" }, fieldValue(v))))));
    if (p && p.why) card.appendChild(h("p", { class: "qcard-why" }, icon(["web", "web_conversation"].includes(p.why) ? "globe" : "stop"), h("span", {}, t("prop.why." + p.why, p.why_vars || {}))));
    if (q.detail && (!p || q.detail.length > 220 || !(p.fields || []).some(([, v]) => v === q.detail))) card.appendChild(h("details", { class: "qcard-detail" }, h("summary", {}, t("q.detail")), h("pre", {}, q.detail)));
    const shown = (a) => approval ? (/^approve/i.test(a || "") || a === t("q.approve") ? t("q.approved") : /^decline/i.test(a || "") || a === t("q.decline") ? t("q.declined") : a) : a;
    if (answered) { card.appendChild(h("div", { class: "qcard-a" }, icon(approval ? (/^approve/i.test(q.answer || "") || q.answer === t("q.approve") ? "check" : "x") : "check"), h("span", {}, t("q.answered")), h("b", {}, shown(q.answer || "")))); return card; }
    const send = async (text, btn) => {
      if (!text.trim()) return;
      $$("button", card).forEach((x) => (x.disabled = true));
      if (btn) btn.classList.add("busy");
      try { const j = await api("/api/questions/" + q.id + "/answer", { text }); card.replaceWith(questionCard(j.question)); S.questions = S.questions.filter((x) => x.id !== q.id); updateTabBadges(); }
      catch (e) { fail(e); $$("button", card).forEach((x) => (x.disabled = false)); }
    };
    if (approval) {
      const yes = h("button", { class: "btn primary", type: "button", onclick: () => send("Approve", yes) }, icon("approve"), t("q.approve"));
      const no = h("button", { class: "btn ghost", type: "button", onclick: () => send("Decline", no) }, t("q.decline"));
      card.appendChild(h("div", { class: "qcard-actions" }, no, yes));
    } else if ((q.options || []).length) {
      card.appendChild(h("div", { class: "qcard-opts" }, q.options.map((o) => { const b = h("button", { class: "btn opt", type: "button", onclick: () => send(o, b) }, o); return b; })));
    }
    if (!approval) {
      const inp = h("input", { class: "input", placeholder: t("q.answer.ph"), enterkeyhint: "send" });
      const go_ = h("button", { class: "icon-btn solid", type: "button", "aria-label": t("q.send"), onclick: () => send(inp.value) }, icon("send"));
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(inp.value); });
      card.appendChild(h("div", { class: "qcard-free" }, inp, go_));
    }
    return card;
  }

  // ------------------------------------------------------------------ files and photos for the chat (snaplab/uploads.py)
  // Picked, photographed, pasted or dropped files go up at once (with progress) and travel with the next message.
  const LINE_ICONS = {
    clip: '<path d="m19 11.2-6.6 6.6a4.4 4.4 0 0 1-6.2-6.2l7.1-7.1a2.9 2.9 0 0 1 4.1 4.1l-7.1 7.1a1.5 1.5 0 0 1-2-2.1l6.5-6.5"/>',
    camera: '<path d="M4 8.6A1.6 1.6 0 0 1 5.6 7h2.1l1.6-2.2h5.4L16.3 7h2.1A1.6 1.6 0 0 1 20 8.6v8.8a1.6 1.6 0 0 1-1.6 1.6H5.6A1.6 1.6 0 0 1 4 17.4z"/><circle cx="12" cy="12.6" r="3.4"/>',
    image: '<rect x="4" y="5" width="16" height="14" rx="2.2"/><circle cx="9.2" cy="10" r="1.6"/><path d="m5.2 17.6 4.6-4.6 3 3 2.1-2.1 3.9 3.9"/>',
  };
  // Drawings outside the icon set (web/icons/ui): they stay line drawings with either icon style.
  function lineIcon(name) {
    const span = h("span", { class: "ic line-ic", "aria-hidden": "true" });
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' + LINE_ICONS[name] + "</svg>";
    return span;
  }
  const fileExt = (name) => ((String(name || "").match(/\.([A-Za-z0-9]{1,5})$/) || [])[1] || "FILE").toUpperCase();
  function fileSize(n) {
    return n >= 1048576 ? num(Math.round(n / 104857.6) / 10) + " MB" : num(Math.max(1, Math.ceil((n || 0) / 1024))) + " KB";
  }
  const objectUrl = (blob) => (typeof URL !== "undefined" && URL.createObjectURL ? URL.createObjectURL(blob) : "");
  const dropUrl = (url) => { if (url && typeof URL !== "undefined" && URL.revokeObjectURL) URL.revokeObjectURL(url); };

  // A picture the browser can show but a model may not read (HEIC from an iPhone, AVIF, BMP) goes as JPEG.
  async function shareable(file) {
    if (!/^image\//.test(file.type || "") || /^image\/(png|jpeg|gif|webp|svg)/.test(file.type) || typeof createImageBitmap !== "function") return file;
    try {
      const bmp = await createImageBitmap(file);
      const c = document.createElement("canvas"); c.width = bmp.width; c.height = bmp.height;
      c.getContext("2d").drawImage(bmp, 0, 0);
      const blob = await new Promise((done) => c.toBlob(done, "image/jpeg", 0.92));
      return blob ? new File([blob], String(file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" }) : file;
    } catch (e) { return file; }
  }

  // The files waiting above the text field: a picture or the file type, the name, progress, and × to take it back.
  function fileTray(room, onChange) {
    const items = [];
    const el = h("div", { class: "file-tray" });
    el.hidden = true;
    function chip(it) {
      it.state_el = it.state === "failed"
        ? h("button", { type: "button", class: "file-state bad", onclick: () => upload(it) }, t("composer.failed"))
        : h("span", { class: "file-state" }, it.state === "uploading" ? t("composer.uploading", { p: it.progress }) : fileSize(it.size));
      it.bar = h("i", { class: "file-bar", style: { width: (it.state === "uploading" ? it.progress : 0) + "%" } });
      return h("div", { class: "file-chip " + it.state, title: it.name },
        h("div", { class: "file-thumb" }, it.url ? h("img", { src: it.url, alt: "" }) : h("span", { class: "file-ext" }, fileExt(it.name)), it.bar),
        h("div", { class: "file-meta" }, h("span", { class: "file-name" }, it.name), it.state_el),
        h("button", { type: "button", class: "file-x", "aria-label": t("composer.remove", { name: it.name }), onclick: () => remove(it) }, icon("x")));
    }
    function draw() { el.hidden = !items.length; el.replaceChildren(...items.map(chip)); onChange(); }
    function progress(it) {
      if (it.state_el) it.state_el.textContent = t("composer.uploading", { p: it.progress });
      if (it.bar) it.bar.style.width = it.progress + "%";
    }
    function upload(it) {
      it.state = "uploading"; it.progress = 0; it.id = null;
      it.done = new Promise((resolve) => {
        const x = new XMLHttpRequest();
        it.xhr = x;
        const finish = () => { it.xhr = null; if (!it.removed) draw(); resolve(); };
        x.open("POST", "/api/rooms/" + room.id + "/files");
        x.setRequestHeader("X-SnapLab", "1");
        x.setRequestHeader("X-SnapLab-Name", encodeURIComponent(it.name));
        x.setRequestHeader("Content-Type", it.file.type || "application/octet-stream");
        if (x.upload) x.upload.onprogress = (e) => { if (e.lengthComputable && e.total) { it.progress = Math.min(99, Math.floor(e.loaded * 100 / e.total)); progress(it); } };
        x.onload = () => {
          let j = {};
          try { j = JSON.parse(x.responseText || "{}"); } catch (e) { j = {}; }
          if (x.status === 200 && j.ok && j.file) { it.id = j.file.id; it.state = "ready"; }
          else { it.state = "failed"; it.error = j.error || ("HTTP " + x.status); }
          finish();
        };
        x.onerror = x.onabort = () => { it.state = "failed"; finish(); };
        x.send(it.file);
      });
      draw();
    }
    function remove(it) {
      it.removed = true;
      if (it.xhr) it.xhr.abort();
      if (it.id) api("/api/files/" + it.id + "/discard", {}).catch(() => {});
      dropUrl(it.url);
      items.splice(items.indexOf(it), 1);
      draw();
    }
    async function add(list) {
      for (const picked of Array.from(list || [])) {
        const file = await shareable(picked);
        const it = { file, name: file.name || "file", size: file.size || 0, state: "uploading", progress: 0,
          url: /^image\/(png|jpeg|gif|webp)$/.test(file.type || "") ? objectUrl(file) : "" };
        items.push(it);
        upload(it);
      }
    }
    return {
      el, add, count: () => items.length,
      pending: () => items.some((it) => it.state === "uploading"),
      failed: () => items.some((it) => it.state === "failed"),
      settle: () => Promise.all(items.map((it) => it.done)),
      ids: () => items.filter((it) => it.state === "ready").map((it) => it.id),
      sent(ids) { for (const it of items.filter((x) => ids.includes(x.id))) { dropUrl(it.url); items.splice(items.indexOf(it), 1); } draw(); },
    };
  }

  // The camera on a PC (getUserMedia). Phones open their own camera app through the file input instead.
  function cameraSheet(onShot) {
    const video = h("video", { class: "camera-view", autoplay: true, playsinline: true, muted: true });
    const still = h("img", { class: "camera-still", alt: "", hidden: true });
    const flip = h("button", { class: "btn", type: "button", hidden: true }, icon("refresh"), t("camera.switch"));
    const retake = h("button", { class: "btn", type: "button", hidden: true }, t("camera.retake"));
    const shoot = h("button", { class: "btn primary", type: "button" }, lineIcon("camera"), t("camera.shoot"));
    const use = h("button", { class: "btn primary", type: "button", hidden: true }, icon("check"), t("camera.use"));
    let stream = null, devices = [], which = -1, shot = null, open = true;
    const stop = () => { if (stream) stream.getTracks().forEach((track) => track.stop()); stream = null; };
    const sh = sheet(t("camera.title"), [h("div", { class: "camera-stage" }, video, still), h("div", { class: "camera-actions" }, flip, retake, shoot, use)],
      () => { open = false; stop(); dropUrl(still.src); });
    sh.panel.classList.add("camera-sheet");
    async function start() {
      stop();
      try {
        const id = which >= 0 && devices[which] ? devices[which].deviceId : null;
        stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: id ? { deviceId: { exact: id } }
          : { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } } });
        if (!open) { stop(); return; }
        video.srcObject = stream;
        devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
        flip.hidden = devices.length < 2 || !!shot;
      } catch (e) {
        sh.close();
        toast(t(e && (e.name === "NotFoundError" || e.name === "OverconstrainedError") ? "camera.none" : "camera.denied"), "bad");
      }
    }
    shoot.addEventListener("click", () => {
      if (!video.videoWidth) return;
      const c = document.createElement("canvas"); c.width = video.videoWidth; c.height = video.videoHeight;
      c.getContext("2d").drawImage(video, 0, 0);
      c.toBlob((blob) => {
        if (!blob) return;
        shot = blob; still.src = objectUrl(blob); still.hidden = false; video.hidden = true;
        shoot.hidden = flip.hidden = true; retake.hidden = use.hidden = false;
      }, "image/jpeg", 0.92);
    });
    retake.addEventListener("click", () => {
      dropUrl(still.src); shot = null; still.hidden = true; video.hidden = false;
      shoot.hidden = false; retake.hidden = use.hidden = true; flip.hidden = devices.length < 2;
    });
    use.addEventListener("click", () => {
      if (!shot) return;
      const d = new Date(), two = (n) => String(n).padStart(2, "0");
      const name = "photo-" + d.getFullYear() + two(d.getMonth() + 1) + two(d.getDate()) + "-" + two(d.getHours()) + two(d.getMinutes()) + two(d.getSeconds()) + ".jpg";
      onShot([new File([shot], name, { type: "image/jpeg" })]);
      sh.close();
    });
    flip.addEventListener("click", () => { which = (Math.max(0, which) + 1) % devices.length; start(); });
    start();
    return sh;
  }

  // The 📎 button and its menu: take a photo, pick photos or videos, pick any files.
  function attachMenu(onFiles) {
    const input = (attrs) => {
      const el = h("input", Object.assign({ type: "file", class: "attach-input", tabindex: "-1", "aria-hidden": "true" }, attrs));
      el.addEventListener("change", () => { const files = Array.from(el.files || []); el.value = ""; if (files.length) onFiles(files); });
      return el;
    };
    const camera = input({ accept: "image/*", capture: "environment" });
    const photos = input({ accept: "image/*,video/*", multiple: true });
    const files = input({ multiple: true });
    const btn = h("button", { class: "attach-btn", type: "button", "aria-label": t("composer.attach"), title: t("composer.attach"), "aria-haspopup": "menu", "aria-expanded": "false" }, lineIcon("clip"));
    let menu = null;
    const close = () => {
      if (!menu) return;
      menu.hidden = true; btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("pointerdown", outside, true);
    };
    const outside = (e) => { if (menu && !menu.contains(e.target) && !btn.contains(e.target)) close(); };
    const item = (key, ic, run) => h("button", { type: "button", role: "menuitem", class: "attach-item", onclick: () => { close(); run(); } }, ic, h("span", {}, t(key)));
    // A phone or tablet uses its camera app (the photo comes back like a picked file); a PC shows its camera here.
    const pcCamera = () => !window.matchMedia("(pointer: coarse)").matches && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    menu = h("div", { class: "attach-menu", role: "menu", "aria-label": t("composer.attach") },
      item("composer.camera", lineIcon("camera"), () => (pcCamera() ? cameraSheet(onFiles) : camera.click())),
      item("composer.photos", lineIcon("image"), () => photos.click()),
      item("composer.files", icon("file"), () => files.click()));
    menu.hidden = true;
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); btn.focus(); } });
    btn.addEventListener("click", () => {
      if (!menu.hidden) { close(); return; }
      menu.hidden = false; btn.setAttribute("aria-expanded", "true");
      document.addEventListener("pointerdown", outside, true);
      const first = menu.querySelector(".attach-item"); if (first) first.focus();
    });
    return { btn, menu, inputs: [camera, photos, files] };
  }

  function myFilesEl(m) {
    const items = (Array.isArray(m.attachments) ? m.attachments : []).filter((a) => a && /^f-[0-9a-f]{32}$/.test(a.id));
    if (!items.length) return null;
    const photos = items.filter((a) => a.kind === "image");
    return h("div", { class: "my-files" + (photos.length > 1 ? " grid" : "") }, items.map((a) => {
      const base = "/api/attachments/" + a.id;
      if (a.kind === "image") {
        const img = h("img", { src: base + "/content", alt: a.name, loading: "lazy" });
        img.addEventListener("error", () => img.replaceWith(h("span", { class: "file-ext" }, fileExt(a.name))));
        return h("button", { type: "button", class: "my-photo", "aria-label": t("attachment.enlarge", { name: a.name }), onclick: () => attachmentViewer(a) }, img);
      }
      const view = a.kind === "pdf" || a.kind === "text";
      return h("a", { class: "my-file", href: base + (view ? "/preview" : "/download"), target: view ? "_blank" : null, rel: "noopener noreferrer", download: view ? null : a.name },
        h("span", { class: "file-ext" }, fileExt(a.name)),
        h("span", { class: "file-meta" }, h("span", { class: "file-name" }, a.name), h("span", { class: "file-state" }, fileSize(a.size))));
    }));
  }

  function buildComposer(room, b) {
    const ta = h("textarea", { class: "composer-input", rows: "1", maxlength: "8000", placeholder: room.kind === "group" ? t("room.ph.group") : t("room.ph", { name: b ? b.name : room.name }), enterkeyhint: "send" });
    autosize(ta, 150);
    const sendBtn = h("button", { class: "send", type: "button", "aria-label": t("room.send") }, icon("send"));
    const stopBtn = h("button", { class: "stop-btn", type: "button", "aria-label": t("room.stop") }, icon("stop"));
    const mentions = h("div", { class: "mentions" });
    const ready = () => sendBtn.classList.toggle("ready", !!(ta.value || "").trim() || tray.count() > 0);
    const tray = fileTray(room, ready);
    const attach = attachMenu((files) => tray.add(files));
    const el = h("div", { class: "composer" }, mentions, attach.menu, tray.el,
      h("div", { class: "composer-row" }, attach.btn, ta, stopBtn, sendBtn), attach.inputs);
    const draftKey = "room:" + room.id;
    keepDraft(ta, draftKey);
    let sending = false;
    const doSend = async () => {
      if (sending) return;
      if (!(ta.value || "").trim() && !tray.count()) return;
      sending = true;
      sendBtn.classList.add("pulse"); setTimeout(() => sendBtn.classList.remove("pulse"), 400);
      try {
        if (tray.pending()) { sendBtn.classList.add("waiting"); toast(t("composer.wait")); await tray.settle(); }
        if (tray.failed()) { toast(t("composer.failed"), "bad"); return; }
        const files = tray.ids();
        await sendDraft(ta, draftKey, (text) => api("/api/rooms/" + room.id + "/send", files.length ? { text: text.trim(), files } : { text: text.trim() }));
        tray.sent(files);
      }
      catch (e) { fail(e); }
      finally { sending = false; sendBtn.classList.remove("waiting"); }
    };
    // A picture pasted from the clipboard (a screenshot, an image copied in the browser). Copied text stays text.
    ta.addEventListener("paste", (e) => {
      const data = e.clipboardData;
      const files = data && data.files ? Array.from(data.files) : [];
      if (!files.length || (data.getData("text/plain") || "").trim()) return;
      e.preventDefault();
      tray.add(files);
    });
    // Files dropped anywhere on the chat (PC).
    const dropOn = (target) => {
      let depth = 0;
      const hasFiles = (e) => !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files"));
      const done = () => { depth = 0; target.classList.remove("dropping"); };
      target.dataset.drop = t("composer.drop");
      target.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; target.classList.add("dropping"); });
      target.addEventListener("dragover", (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
      target.addEventListener("dragleave", (e) => { if (hasFiles(e) && --depth <= 0) done(); });
      target.addEventListener("drop", (e) => { if (!hasFiles(e)) return; e.preventDefault(); done(); tray.add(e.dataTransfer.files); });
    };
    sendBtn.addEventListener("click", doSend);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(pointer: fine)").matches) { e.preventDefault(); doSend(); } });
    stopBtn.addEventListener("click", async () => { try { await api("/api/rooms/" + room.id + "/stop", {}); } catch (e) { fail(e); } });
    ta.addEventListener("input", () => {
      ready();
      if (room.kind !== "group") return;
      const m = ta.value.slice(0, ta.selectionStart).match(/@([^\s@]*)$/);
      mentions.innerHTML = "";
      if (!m) { mentions.classList.remove("show"); return; }
      const q = m[1].toLowerCase();
      const opts = room.bots.map(bot).filter((x) => x && x.name.toLowerCase().startsWith(q));
      opts.forEach((x) => mentions.appendChild(h("button", { type: "button", class: "mention", onclick: () => {
        const pos = ta.selectionStart; const before = ta.value.slice(0, pos).replace(/@([^\s@]*)$/, "@" + x.name + " ");
        ta.value = before + ta.value.slice(pos); ta.focus(); ta.setSelectionRange(before.length, before.length); ta.dispatchEvent(new Event("input"));
      } }, avatar(x, "xs"), x.name)));
      mentions.classList.toggle("show", opts.length > 0);
    });
    return { el, ta, stopBtn, tray, dropOn };
  }
  function setBusy(on) {
    if (!roomEls) return;
    roomEls.composer.el.classList.toggle("busy", !!on);
  }

  // ------------------------------------------------------------------ projects

  const URGENCY = { waiting: 0, failed: 1, review: 2, running: 3, queued: 3, stopped: 4, idle: 5 };
  const normStatus = (st) => st;
  function byUrgency(a, b) {
    const ua = URGENCY[normStatus(a.status)], ub = URGENCY[normStatus(b.status)];
    return ((ua === undefined ? 9 : ua) - (ub === undefined ? 9 : ub)) || String(b.updated || b.created || "").localeCompare(String(a.updated || a.created || ""));
  }
  function statusPill(st, big) {
    st = normStatus(st);
    const ic = { waiting: "help", review: "review", done: "check", failed: "alert", stopped: "pause", idle: "moon" }[st];
    return h("span", { class: "status st-" + st + (big ? " big" : "") }, ic ? icon(ic) : h("span", { class: "pulse-dot" }), t("session.status." + st));
  }

  function renderProjects() {
    const body = h("div", { class: "page" });
    if (!S.projects.length) body.appendChild(h("div", { class: "empty-state" }, h("img", { src: "/brand/sail-blue.png", alt: "", class: "empty-art" }), h("p", {}, t("projects.empty")), h("button", { class: "btn primary", onclick: newProjectSheet }, icon("plus"), t("projects.new"))));
    const list = h("div", { class: "proj-list" });
    S.projects.slice().sort((a, b) => String(b.last || "").localeCompare(String(a.last || ""))).forEach((p) => {
      const owner = bot(p.bot);
      const ss = S.sessions.filter((s) => s.project === p.id);
      const last = ss[0];
      list.appendChild(h("a", { class: "card proj", href: "#/project/" + p.id },
        h("div", { class: "proj-head" }, h("span", { class: "proj-ic" }, icon("folder")), h("div", { class: "proj-title" }, p.name), p.running ? h("span", { class: "status st-running" }, h("span", { class: "pulse-dot" }), t("projects.running", { n: p.running })) : null),
        p.description ? h("p", { class: "proj-desc" }, p.description) : null,
        h("div", { class: "proj-foot" }, owner ? h("span", { class: "who" }, avatar(owner, "xs"), owner.name) : null,
          h("span", { class: "muted" }, ss.length === 1 ? t("projects.session1", { n: ss.length }) : t("projects.sessions", { n: ss.length })),
          last ? statusPill(last.status) : null, h("span", { class: "muted right" }, relTime(p.last)))));
    });
    body.appendChild(list);
    screen({ back: "#/dashboard", title: t("projects.title"), tab: "dashboard", cls: "projects" }, body, S.projects.length ? fab(t("projects.new"), "plus", newProjectSheet) : null);
  }

  function newProjectSheet() {
    const name = h("input", { class: "input", maxlength: "60", placeholder: t("np.name.ph") });
    const desc = autosize(h("textarea", { class: "input", rows: "2", maxlength: "600", placeholder: t("np.desc.ph") }), 140);
    const tests = h("input", { class: "input mono", maxlength: "300", placeholder: t("np.tests.ph"), autocapitalize: "off", spellcheck: "false" });
    const sel = h("select", { class: "input" }, S.bots.map((b) => h("option", { value: b.id }, b.name)));
    const btn = h("button", { class: "btn primary wide", type: "button" }, t("np.create"));
    const sh = sheet(t("np.title"), [field(t("np.name"), name), field(t("np.desc"), desc), field(t("np.tests"), tests), field(t("np.bot"), sel), btn]);
    setTimeout(() => name.focus(), 300);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const j = await api("/api/projects", { name: name.value, description: desc.value, test_command: tests.value, bot: sel.value });
        upsert(S.projects, Object.assign({ sessions: 0, running: 0 }, j.project)); sh.close(); toast(t("toast.created")); go("#/project/" + j.project.id);
      } catch (e) { fail(e); btn.disabled = false; }
    });
  }

  function renderProject(id) {
    const p = S.projects.find((x) => x.id === id);
    if (!p) { go("#/projects"); return; }
    const owner = bot(p.bot);
    const ss = S.sessions.filter((s) => s.project === p.id).sort(byUrgency);
    const body = h("div", { class: "page" },
      h("div", { class: "card proj-info" },
        p.description ? h("p", { class: "proj-desc full" }, p.description) : null,
        h("div", { class: "kv" }, h("span", {}, icon("folder"), t("project.folder")), h("code", {}, p.slug)),
        h("div", { class: "kv" }, h("span", {}, icon("flask"), t("project.tests")), p.test_command ? h("code", {}, p.test_command) : h("span", { class: "muted" }, t("project.tests.none"))),
        owner ? h("div", { class: "kv" }, h("span", {}, icon("person"), t("project.owner")), h("span", { class: "who" }, avatar(owner, "xs"), owner.name)) : null),
      h("div", { class: "card brief-card", id: "brief-card" }, h("div", { class: "card-h row" }, icon("page"), h("span", {}, t("project.brief")),
        h("button", { class: "btn ghost small right", type: "button", onclick: () => briefSheet(p) }, icon("edit"), t("project.brief.edit"))),
        h("p", { class: "hint" }, t("project.brief.note")), h("div", { class: "brief-body md muted" }, "…")),
      h("button", { class: "btn primary wide", type: "button", onclick: () => newSessionSheet(p) }, icon("play"), t("project.newsession")),
      h("h3", { class: "section" }, t("project.sessions")),
      ss.length ? h("div", { class: "sess-list" }, ss.map(sessionRow)) : h("p", { class: "empty" }, t("project.nosessions")));
    screen({ back: "#/projects", title: p.name, tab: "dashboard", actions: [h("button", { class: "icon-btn", "aria-label": t("project.edit"), onclick: () => editProjectSheet(p) }, icon("edit"))] }, body);
    loadBrief(p);
  }

  async function loadBrief(p) {
    try {
      const j = await api("/api/projects/" + p.id + "/brief");
      const body = $("#brief-card .brief-body");
      if (!body) return j.text;
      body.classList.remove("muted");
      body.innerHTML = j.text.trim() ? md(j.text.replace(/^#\s+.*\n+/, "")) : "";
      if (!j.text.trim()) { body.textContent = t("project.brief.empty"); body.classList.add("muted"); }
      return j.text;
    } catch (e) { return ""; }
  }
  async function briefSheet(p) {
    const text = await loadBrief(p);
    const ta = autosize(h("textarea", { class: "input prompt-ta", rows: "12", maxlength: "8000", spellcheck: "false" }, text || ""), 520);
    const btn = h("button", { class: "btn primary wide", type: "button" }, t("common.save"));
    const sh = sheet(t("project.brief") + " · " + p.name, [h("p", { class: "hint" }, t("project.brief.note")), ta, btn]);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await api("/api/projects/" + p.id + "/brief", { text: ta.value }); sh.close(); toast(t("project.brief.saved")); loadBrief(p); }
      catch (e) { fail(e); btn.disabled = false; }
    });
  }

  function editProjectSheet(p) {
    const desc = autosize(h("textarea", { class: "input", rows: "2", maxlength: "600" }, p.description || ""), 140);
    const tests = h("input", { class: "input mono", maxlength: "300", value: p.test_command || "", placeholder: t("np.tests.ph"), autocapitalize: "off", spellcheck: "false" });
    const sel = h("select", { class: "input" }, S.bots.map((b) => h("option", { value: b.id, selected: b.id === p.bot }, b.name)));
    const btn = h("button", { class: "btn primary wide", type: "button" }, t("common.save"));
    const sh = sheet(p.name, [field(t("np.desc"), desc), field(t("np.tests"), tests), field(t("np.bot"), sel), btn]);
    btn.addEventListener("click", async () => {
      try { const j = await api("/api/projects/" + p.id, { description: desc.value, test_command: tests.value, bot: sel.value }); Object.assign(p, j.project); sh.close(); render(); }
      catch (e) { fail(e); }
    });
  }

  function stepsLine(pl) {
    if (!pl || !pl.total) return null;
    return h("div", { class: "pp-prog" }, h("span", { class: "steps-bar" }, h("span", { style: { width: Math.round((pl.done / pl.total) * 100) + "%" } })),
      h("span", { class: "pp-num" }, t("work.steps", { done: pl.done, total: pl.total })));
  }
  function sessionRow(s) {
    const live = s.status === "running";
    return h("a", { class: "card sess st-" + normStatus(s.status), href: "#/session/" + s.id },
      h("div", { class: "sess-top" }, statusPill(s.status), h("span", { class: "harness-chip" }, icon(harnessIcon(s.harness)), harnessLabel(s.harness)), h("span", { class: "muted right" }, relTime(s.updated || s.created))),
      h("div", { class: "sess-title" }, s.title || s.id),
      s.doing ? h("div", { class: "sess-doing" }, h("span", { class: "spinner sm" }), h("span", {}, s.doing)) : null,
      stepsLine(s.plan),
      h("div", { class: "sess-stats" },
        h("span", {}, icon("clock"), h("span", live ? { "data-since": s.started, "data-base": s.elapsed_s - secsSince(s.started) } : {}, dur(s.elapsed_s))),
        h("span", {}, icon("coins"), num((s.tokens || {}).input + (s.tokens || {}).output)),
        h("span", {}, icon("file"), String(s.changed_count || 0)),
        s.tests ? h("span", { class: "tests-" + s.tests.status }, icon("flask"), testsShort(s.tests)) : null));
  }
  function secsSince(iso) { return iso ? (Date.now() - new Date(iso)) / 1000 : 0; }
  function harnessIcon(id) { const hh = S.harnesses.find((x) => x.id === id); return (hh && hh.icon && document.getElementById("i-" + hh.icon)) ? hh.icon : (id === "codex" ? "terminal" : "person"); }
  function harnessLabel(id) { const hh = S.harnesses.find((x) => x.id === id); return hh ? (hh.label[S.lang] || hh.label.en) : id; }
  function testsShort(tt) {
    if (!tt) return "";
    if (tt.status === "passed") return t("session.tests.passed", { n: tt.passed == null ? "✓" : tt.passed });
    if (tt.status === "failed") return t("session.tests.failed", { f: tt.failed || "?", n: tt.passed || 0 });
    if (tt.status === "no tests") return t("session.tests.notests");
    if (tt.status === "timeout") return t("session.tests.timeout");
    if (tt.status === "unchanged") return t("session.tests.unchanged");
    return t("session.tests.error");
  }
  // big number for the tests metric: "4/4" plus a short line underneath
  function testsMetric(tt) {
    if (!tt) return { value: "—", sub: null };
    const total = (tt.passed || 0) + (tt.failed || 0);
    if ((tt.status === "passed" || tt.status === "failed") && total) {
      return { value: t("session.tests.ratio", { p: tt.passed || 0, t: total }), sub: tt.failed ? t("session.tests.ratio.sub", { f: tt.failed }) : t("session.tests.all") };
    }
    return { value: testsShort(tt), sub: null };
  }

  function newSessionSheet(p) {
    let harness = (S.harnesses.find((x) => x.available) || S.harnesses[0] || {}).id;
    const cards = h("div", { class: "harness-cards" }, S.harnesses.map((hh) => {
      const c = h("button", { type: "button", class: "harness" + (hh.id === harness ? " on" : "") + (hh.available ? "" : " off"), disabled: !hh.available, "data-h": hh.id,
        onclick: () => { harness = hh.id; $$(".harness", cards).forEach((x) => x.classList.toggle("on", x.dataset.h === hh.id)); botField.classList.toggle("hide", harness !== "bot"); } },
        h("span", { class: "harness-ic" }, icon(harnessIcon(hh.id))),
        h("span", { class: "harness-name" }, hh.label[S.lang] || hh.label.en),
        h("span", { class: "harness-tag" }, hh.runs_code ? icon("shield") : null, hh.runs_code ? t("session.sandbox") : t("session.nocode")),
        h("span", { class: "harness-d" }, hh.available ? (hh.description[S.lang] || hh.description.en) : t("ns.unavailable", { why: typeof hh.reason === 'object' ? (hh.reason[S.lang] || hh.reason.en) : S.lang === "ru" ? t("harness.unavailable") : hh.reason })));
      return c;
    }));
    const sel = h("select", { class: "input" }, S.bots.map((b) => h("option", { value: b.id, selected: b.id === p.bot }, b.name)));
    const botField = field(t("ns.bot"), sel);
    botField.classList.toggle("hide", harness !== "bot");
    const draftKey = "new-session:" + p.id;
    const prompt = keepDraft(autosize(h("textarea", { class: "input", rows: "4", maxlength: "8000", placeholder: t("ns.prompt.ph") }), 220), draftKey);
    const btn = h("button", { class: "btn primary wide", type: "button" }, icon("play"), t("ns.start"));
    const sh = sheet(t("ns.title") + " · " + p.name, [field(t("ns.harness"), cards), botField, field(t("ns.prompt"), prompt), btn]);
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.classList.add("busy");
      try {
        const j = await api("/api/sessions", { project: p.id, harness, prompt: prompt.value, bot: harness === "bot" ? sel.value : "" });
        drafts.clear(draftKey);
        upsert(S.sessions, j.session); sh.close(); go("#/session/" + j.session.id);
      } catch (e) { fail(e); btn.disabled = false; btn.classList.remove("busy"); }
    });
  }

  // ------------------------------------------------------------------ session

  let sessEls = null;
  async function renderSession(id) {
    const s0 = S.sessionDetail[id] || S.sessions.find((x) => x.id === id);
    const holder = h("div", { class: "page sess-page" });
    const follow = h("div", { class: "composer follow" });
    screen({ back: s0 ? "#/project/" + s0.project : "#/projects", title: s0 ? (s0.project_name || "") : "", tab: "dashboard", noTabs: true, cls: "session" }, holder, follow);
    sessEls = { id, holder, follow };
    if (s0) renderSessionBody();
    await refreshSession(id);
  }
  async function refreshSession(id) {
    try {
      const j = await api("/api/sessions/" + id);
      S.sessionDetail[id] = j.session;
      upsert(S.sessions, Object.assign({}, j.session, { events: undefined }));
      if (sessEls && sessEls.id === id) renderSessionBody();
    } catch (e) { fail(e); }
  }

  function renderSessionBody() {
    if (!sessEls) return;
    const s = S.sessionDetail[sessEls.id] || S.sessions.find((x) => x.id === sessEls.id);
    if (!s) return;
    const running = s.status === "running" || s.status === "queued";
    const tk = s.tokens || { input: 0, output: 0 };
    const starter = s.started_by === "user" ? t("session.by.user") : t("session.by.bot", { name: (bot(s.started_by) || {}).name || "Bot" });
    const st = normStatus(s.status);
    const nowEv = running ? (s.events || []).slice().reverse().find((e) => ["command", "start", "file", "message", "tool", "tests", "note"].includes(e.kind)) : null;
    const hero = h("div", { class: "card sess-hero st-" + st },
      h("div", { class: "hero-band" }),
      h("p", { class: "hint model-current" }, modelDescription(s.model, "codex")),
      h("div", { class: "hero-row" },
        h("span", { class: "hero-ic" }, icon(harnessIcon(s.harness))),
        h("div", { class: "hero-main" }, statusPill(st, true), h("h2", { class: "hero-title" }, s.title || s.id),
          h("div", { class: "hero-sub" }, h("span", { class: "harness-chip" }, s.harness !== "bot" ? icon("shield") : null, harnessLabel(s.harness)), h("span", {}, starter), s.turns ? h("span", {}, t(s.turns === 1 ? "session.turn1" : "session.turns", { n: s.turns })) : null))),
      s.doing ? h("div", { class: "hero-now doing" }, h("span", { class: "spinner sm" }), h("span", { class: "now-kind" }, t("work.now")), h("span", { class: "now-step" }, s.doing))
        : nowEv ? h("div", { class: "hero-now" }, h("span", { class: "spinner sm" }), h("span", { class: "now-kind" }, t("ev." + nowEv.kind)), h("span", { class: "now-text" }, (nowEv.text || "").split("\n")[0].slice(0, 160))) : null,
      running ? h("button", { class: "btn danger small", type: "button", onclick: stopSession }, icon("stop"), t("session.stop")) : null,
      ["codex", "pi"].includes(s.harness) ? h("a", { class: "btn small", href: "#/browser/" + s.id }, icon("globe"), t(running ? "browser.watch" : "browser.open")) : null,
      s.branch ? h("div", { class: "hero-branch" }, icon("branch"), h("code", {}, s.branch), s.merged ? h("span", { class: "chip tiny good" }, t("session.merged")) : null) : null,
      st === "review" ? h("button", { class: "btn good", type: "button", onclick: markReviewed }, icon("review"), t("session.reviewed")) : null,
      s.branch && !s.merged && !running && (s.diff || {}).files ? h("button", { class: "btn", type: "button", onclick: mergeSession }, icon("branch"), t("session.merge")) : null);
    const metric = (ic, label, value, sub, cls) => h("div", { class: "metric " + (cls || "") }, h("div", { class: "metric-label" }, icon(ic), label), h("div", { class: "metric-value" }, value), sub ? h("div", { class: "metric-sub" }, sub) : null);
    const proj = S.projects.find((p) => p.id === s.project) || {};
    const tm = s.tests ? testsMetric(s.tests) : { value: proj.test_command ? "—" : t("session.tests.none"), sub: proj.test_command ? null : null };
    const metrics = h("div", { class: "metrics" },
      metric("clock", t("session.elapsed"), h("span", running && s.started ? { "data-since": s.started, "data-base": (s.elapsed_s || 0) - secsSince(s.started) } : {}, dur(s.elapsed_s))),
      metric("coins", t("session.tokens"), num(tk.input + tk.output), t("session.in") + " " + num(tk.input) + " · " + t("session.out") + " " + num(tk.output)),
      metric("file", t("session.files"), String(s.changed_count || (s.changed_files || []).length || 0),
        s.diff ? h("span", { class: "diffline" }, h("b", { class: "plus" }, "+" + (s.diff.insertions || 0)), " ", h("b", { class: "minus" }, "−" + (s.diff.deletions || 0))) : null),
      metric("flask", t("session.tests"), tm.value, tm.sub, s.tests ? "tests-" + s.tests.status : (proj.test_command ? "" : "dim")));
    const last = h("div", { class: "card exchange" },
      h("h3", { class: "card-h" }, t("session.last")),
      h("div", { class: "ex you" }, h("span", { class: "ex-who" }, s.started_by && s.started_by !== "user" && bot(s.started_by) ? bot(s.started_by).name : t("session.you")),
        h("div", { class: "ex-text md", html: md(s.last_prompt || "") })),
      h("div", { class: "ex reply" }, h("span", { class: "ex-who" }, t("session.reply")), s.last_message ? h("div", { class: "ex-text md", html: md(s.last_message) }) : h("div", { class: "ex-text muted" }, running || !S.sessionDetail[s.id] ? h("span", { class: "dots" }, h("i"), h("i"), h("i")) : t("session.noreply"))));
    const delivered = attachmentsEl(s.result || {});
    const desktop = running && s.screen_seq ? h('div', {class:'card desktop-live'},
      h('h3', {class:'card-h'}, t('session.desktop')),
      h('img', {src:'/api/sessions/' + s.id + '/screen?v=' + s.screen_seq, alt:t('session.desktop'), style:{width:'100%',height:'auto'}})) : null;
    if (delivered) last.appendChild(delivered);
    const files = (s.changed_files || []);
    const filesCard = files.length ? h("div", { class: "card" }, h("h3", { class: "card-h" }, t("session.changed"), h("span", { class: "count" }, String(files.length))),
      h("div", { class: "files" }, files.slice(0, 60).map((f) => h("div", { class: "file-row" }, h("span", { class: "fchg " + f.change }, f.change[0].toUpperCase()), h("code", {}, f.path))))) : null;
    const testsCard = s.tests && s.tests.output ? h("details", { class: "card tests-card tests-" + s.tests.status }, h("summary", {}, icon("flask"), h("span", {}, t("session.tests") + " · " + testsShort(s.tests)), h("code", { class: "muted" }, s.tests.command || "")), h("pre", { class: "out" }, s.tests.output)) : null;
    const tl = h("div", { class: "timeline", id: "timeline" });
    (s.events || []).slice(-80).forEach((ev) => tl.appendChild(eventEl(ev)));
    const errCard = s.status === "failed" && s.error ? h("div", { class: "card err-card" }, icon("alert"), h("span", {}, s.error)) : null;
    const pl = s.plan;
    const stepsCard = pl && pl.total ? h("div", { class: "card steps-card" }, h("div", { class: "card-h row" }, icon("steps"), h("span", {}, t("work.steps.title")),
        h("span", { class: "right pp-num" }, t("work.steps", { done: pl.done, total: pl.total }))),
      h("span", { class: "steps-bar big" }, h("span", { style: { width: Math.round((pl.done / pl.total) * 100) + "%" } })),
      h("ol", { class: "steps" }, pl.steps.map((x) => h("li", { class: x.done ? "done" : (x.text === pl.now && running ? "now" : "") },
        h("span", { class: "step-mark" }, x.done ? icon("check") : (x.text === pl.now && running ? h("span", { class: "spinner sm" }) : null)), h("span", {}, x.text)))))
      : (running ? h("div", { class: "card steps-card empty-steps" }, icon("steps"), h("span", {}, t("work.steps.waiting"))) : null);
    sessEls.holder.innerHTML = "";
    const tlHead = h("h3", { class: "section" }, t("session.timeline"));
    [hero, errCard, stepsCard, metrics, last, testsCard, filesCard, tlHead, tl].forEach((el, i) => { if (el) el.classList.add("o" + (i + 1)); });
    append(sessEls.holder, [h("div", { class: "sess-col a" }, hero, errCard, desktop, stepsCard, last, testsCard, filesCard),
      h("div", { class: "sess-col b" }, metrics, tlHead, tl)]);
    // follow-up composer: built once per page, so updates of the session never touch what is being typed
    sessEls.follow.classList.toggle("hide", running);
    if (!running && !sessEls.followTa) {
      const draftKey = "session:" + s.id;
      const ta = keepDraft(autosize(h("textarea", { class: "composer-input", rows: "1", maxlength: "8000", placeholder: t("session.followup.ph") }), 140), draftKey);
      const btn = h("button", { class: "send", type: "button", "aria-label": t("session.send") }, icon("send"));
      ta.addEventListener("input", () => btn.classList.toggle("ready", !!ta.value.trim()));
      btn.addEventListener("click", async () => {
        if (!ta.value.trim()) return;
        btn.disabled = true;
        try { await sendDraft(ta, draftKey, (prompt) => api("/api/sessions/" + s.id + "/send", { prompt })); toast(t("toast.sent")); }
        catch (e) { fail(e); } btn.disabled = false;
      });
      sessEls.followTa = ta;
      sessEls.follow.appendChild(h("div", { class: "composer-row" }, ta, btn));
    }
    document.getElementById("app").classList.toggle("has-follow", !running);
  }
  async function stopSession() { try { await api("/api/sessions/" + sessEls.id + "/stop", {}); } catch (e) { fail(e); } }
  async function mergeSession() {
    const s0 = S.sessionDetail[sessEls.id] || {};
    const ok = await confirmSheet(t("session.merge"), [
      h("p", {}, t("session.merge.confirm")),
      h("div", { class: "confirm-facts" }, h("span", {}, icon("branch"), h("code", {}, s0.branch || "")),
        h("span", {}, icon("file"), t((s0.changed_count || 0) === 1 ? "rc.file1" : "rc.files", { n: s0.changed_count || 0 })),
        s0.diff ? h("span", {}, h("b", { class: "plus" }, "+" + (s0.diff.insertions || 0)), " ", h("b", { class: "minus" }, "−" + (s0.diff.deletions || 0))) : null)],
      t("session.merge"));
    if (!ok) return;
    try {
      const j = await api("/api/sessions/" + sessEls.id + "/merge", {});
      upsert(S.sessions, j.session);
      const d = S.sessionDetail[sessEls.id]; if (d) Object.assign(d, j.session, { events: d.events });
      toast(t("session.merged")); renderSessionBody();
    } catch (e) { fail(e); }
  }

  // "What this bot saw": the sections of the system prompt and the history that went into one model call
  async function sawSheet(m, b) {
    const body = h("div", { class: "saw" }, h("div", { class: "saw-loading" }, h("span", { class: "spinner" })));
    sheet(t("saw.title", { name: b.name }), body);
    try {
      const v = (await api("/api/messages/" + m.id + "/view")).view;
      body.innerHTML = "";
      append(body, [h("p", { class: "lead-text" }, t("saw.note")), contextView(v, b, false)]);
      return;
      // eslint-disable-next-line no-unreachable
      const rep = v.report || {};
      const total = rep.total || 0, target = rep.target_tokens || 16000, max = rep.max_tokens || 32000;
      body.innerHTML = "";
      const parts = (v.sections || []).map((x) => ({ name: x.name, tokens: x.tokens, text: x.text }))
        .concat([{ name: "history", tokens: (v.history || []).reduce((a, x) => a + x.tokens, 0), text: null }]);
      const colors = { persona: "var(--brand)", rules: "var(--idle)", tools: "#8B5CF6", room: "var(--info)", context: "var(--info)", state: "#0EA5E9", fresh: "#F43F5E",
        memory: "var(--warn)", user: "#D97706", briefs: "#14B8A6", summary: "#64748B", history: "var(--good)" };
      append(body, [
        h("p", { class: "hint" }, t("saw.note")),
        h("div", { class: "saw-total" }, h("b", {}, num(total)), h("span", {}, t("saw.tokens", { target: num(target), max: num(max) }))),
        h("div", { class: "saw-bar" }, parts.map((x) => h("span", { style: { width: Math.max(0.6, (x.tokens / Math.max(max, total, 1)) * 100) + "%", background: colors[x.name] || "var(--muted)" }, title: x.name }))),
        h("div", { class: "saw-scale" }, h("span", { style: { left: Math.min(100, (target / max) * 100) + "%" } }, t("saw.target"))),
      ]);
      (v.sections || []).forEach((x) => body.appendChild(h("details", { class: "saw-sec" },
        h("summary", {}, h("i", { style: { background: colors[x.name] || "var(--muted)" } }), h("span", { class: "saw-name" }, t("saw.sec." + x.name)), h("span", { class: "saw-tok" }, num(x.tokens))),
        h("pre", {}, x.text))));
      const hist = h("details", { class: "saw-sec" }, h("summary", {}, h("i", { style: { background: colors.history } }), h("span", { class: "saw-name" }, t("saw.sec.history") + " · " + (v.history || []).length), h("span", { class: "saw-tok" }, num(parts[parts.length - 1].tokens))),
        h("div", { class: "saw-hist" }, (v.history || []).map((x) => h("div", { class: "saw-msg " + x.role }, h("span", { class: "saw-role" }, x.role === "assistant" ? b.name : (x.text.startsWith("[Reference]") ? t("saw.other") : x.text.startsWith("[SnapLab") ? "SnapLab" : t("session.you"))), h("span", { class: "saw-text" }, x.text.slice(0, 600))))));
      body.appendChild(hist);
    } catch (e) { body.innerHTML = ""; body.appendChild(h("p", { class: "empty" }, e.message)); }
  }

  async function markReviewed() {
    try {
      const j = await api("/api/sessions/" + sessEls.id + "/reviewed", {});
      upsert(S.sessions, j.session);
      const d = S.sessionDetail[sessEls.id]; if (d) Object.assign(d, j.session, { events: d.events });
      renderSessionBody(); updateTabBadges();
    } catch (e) { fail(e); }
  }

  function eventEl(ev) {
    const ic = { prompt: "chat", command: "terminal", start: "terminal", file: "edit", files: "file", message: "spark", tool: "page", tests: "flask", error: "alert", note: "clock", thinking: "brain" }[ev.kind] || "spark";
    const bad = ev.kind === "error" || (ev.kind === "command" && ev.exit_code && ev.exit_code !== 0) || ev.status === "error";
    const body = h("div", { class: "ev-body" },
      h("div", { class: "ev-top" }, h("span", { class: "ev-kind" }, t("ev." + ev.kind)), ev.kind === "command" && ev.exit_code !== undefined && ev.exit_code !== null ? h("span", { class: "exit " + (ev.exit_code === 0 ? "ok" : "bad") }, "exit " + ev.exit_code) : null, h("span", { class: "ev-time" }, clockTime(ev.ts))),
      ev.kind === "command" || ev.kind === "start" ? h("code", { class: "ev-cmd" }, ev.text) : ev.kind === "message" ? h("div", { class: "ev-text md", html: md((ev.text || "").slice(0, 1200)) }) : h("div", { class: "ev-text" }, lineText(ev)),
      ev.changes ? h("div", { class: "ev-files" }, ev.changes.slice(0, 12).map((c) => h("span", { class: "chg chg-" + c.change }, h("b", {}, t("chg." + c.change)), h("code", {}, c.path))))
        : ev.files ? h("div", { class: "ev-files" }, ev.files.slice(0, 12).map((f) => h("code", {}, f))) : null,
      ev.kind === "command" && ev.output ? h("details", { class: "ev-out" }, h("summary", {}, t("ev.output")), h("pre", {}, ev.output)) : null);
    return h("div", { class: "ev k-" + ev.kind + (bad ? " bad" : "") }, h("span", { class: "ev-dot" }, icon(ic)), body);
  }
  function updateNow(ev) {
    const el = $(".hero-now");
    if (!el || !["command", "file", "message", "tool", "tests", "note"].includes(ev.kind)) return;
    $(".now-kind", el).textContent = t("ev." + ev.kind);
    $(".now-text", el).textContent = lineText(ev).split("\n")[0].slice(0, 160);
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  }
  function appendTimeline(ev) {
    const tl = $("#timeline");
    if (!tl) return;
    const el = eventEl(ev);
    el.classList.add("new");
    tl.appendChild(el);
  }

  // ------------------------------------------------------------------ dashboard

  function greeting() {
    const hr = new Date().getHours();
    const name = (S.settings && S.settings.user_name) || "";
    return t(hr >= 5 && hr < 11 ? "dash.hello.morning" : hr >= 11 && hr < 18 ? "dash.hello.afternoon" : hr >= 18 ? "dash.hello.evening" : "dash.hello.night", { name });
  }

  function progressRing(plan) {
    const total = Math.max(0, Number(plan && plan.total) || 0), done = Math.min(total, Math.max(0, Number(plan && plan.done) || 0));
    const label = total ? t("home.steps", { done, total }) : t("home.noPlan");
    return h("div", { class: "progress-ring", role: "img", "aria-label": label, style: { "--progress": (total ? done / total * 360 : 0) + "deg" } },
      h("span", {}, total ? h("b", {}, done + " / " + total) : h("b", {}, "—")));
  }

  function renderHome() {
    const old = $(".home-work-strip"), scroll = old ? old.scrollLeft : 0;
    const work = S.sessions.filter((s) => ["running", "queued"].includes(s.status));
    const cards = h("div", { class: "home-work-strip" }, work.map((s) => {
      const project = S.projects.find((p) => p.id === s.project);
      const b = bot(s.bot) || bot(s.started_by) || bot(project && project.bot) || S.bots.find((b) => b.orchestrator), pl = s.plan;
      return h("a", { class: "card home-work-card", href: "#/session/" + s.id },
        h("div", { class: "home-work-top" }, avatar(b, "md"), progressRing(pl)),
        h("h3", {}, s.title || s.project_name || s.id),
        h("span", { class: "home-work-owner" }, b ? b.name : t("home.noBot")),
        h("span", { class: "home-step-count" }, pl && pl.total ? t("home.steps", { done: pl.done, total: pl.total }) : t("home.noPlan")),
        statusPill(s.status));
    }));
    // The browser of the newest SnapCode session (running first): the in-app browser, also between turns.
    const browsable = S.sessions.filter((s) => ["codex", "pi"].includes(s.harness));
    const target = browsable.find((s) => s.status === "running") || browsable[0];
    const browserBtn = h("button", { class: "btn ghost small home-browser", type: "button", onclick: () => {
      if (target) location.hash = "#/browser/" + target.id; else toast(t("browser.nosession"));
    } }, icon("globe"), t("browser.title"));
    const n = needsCount();
    const body = h("div", { class: "page home" },
      h("button", { class: "home-attention" + (n ? " has-items" : ""), type: "button", onclick: needsSheet },
        icon(n ? "inbox" : "check"), h("span", { class: "home-attention-number" }, String(n)),
        h("span", {}, t(n ? "home.decisions" : "home.clear")), icon("right")),
      h("section", {}, h("div", { class: "home-heading-row" }, h("h2", { class: "home-heading" }, t("home.work")), browserBtn),
        work.length ? cards : h("div", { class: "home-empty" }, icon("sail"), h("p", {}, t("home.work.empty")))),
      homeAgenda(), h("section", {}, h("h2", { class: "home-heading" }, t("home.api")), h("div", { id: "home-usage" })));
    screen({ title: t("nav.home") }, body);
    cards.scrollLeft = scroll;
    renderHomeUsage();
  }

  function homeAgenda(now = new Date()) {
    const days = [new Date(now), new Date(now)]; days[1].setDate(now.getDate() + 1);
    const entries = S.tasks.filter((tk) => tk.status === "active").flatMap((tk) =>
      (tk.upcoming || (tk.next_run ? [tk.next_run] : [])).map((at) => ({ tk, at: new Date(at) }))).sort((a, b) => a.at - b.at);
    return h("section", { class: "home-agenda" }, h("h2", { class: "home-heading" }, t("home.schedule")),
      days.map((day, i) => {
        const items = entries.filter((x) => x.at.toDateString() === day.toDateString());
        return h("div", { class: "agenda-day" }, h("h3", {}, t(i ? "home.tomorrow" : "home.today")),
          items.length ? h("div", { class: "time-strip" }, items.map(({ tk, at }) => h("button", { class: "time-stop", type: "button",
            onclick: () => sheet(tk.title, taskRow(tk, () => api("/api/tasks").then((j) => { S.tasks = j.tasks; activeSheets[activeSheets.length - 1]?.close(); render(); }))) },
            h("time", { datetime: at.toISOString() }, at.toLocaleTimeString(loc(), { hour: "2-digit", minute: "2-digit" })),
            h("span", { class: "time-node" }, avatar(bot(tk.bot), "xs")), h("span", {}, tk.title)))) : h("p", { class: "agenda-empty" }, t("home.schedule.empty")));
      }));
  }

  function renderHomeUsage() {
    const host = $("#home-usage"); if (!host) return;
    const u = S.usage || {}, today = u.today || {}, slots = u.slots || {}, key = u.key || {}, week = u.week || [];
    const peak = Math.max(1, ...week.map((d) => d.calls || 0));
    const keyLabel = !key.set ? t("dash.key.missing") : key.days_left == null ? t("home.key.unknown")
      : key.days_left < 0 ? t("dash.key.expired") : t("home.key.days", { n: key.days_left });
    host.replaceChildren(h("a", { class: "home-api-grid", href: "#/usage", "aria-label": t("usage.open") },
      h("div", { class: "home-api-cell" }, h("b", {}, num(today.calls || 0)), h("span", {}, t("home.calls")),
        h("div", { class: "home-spark", "aria-hidden": "true" }, week.map((d) => h("i", { style: { height: Math.max(8, (d.calls || 0) / peak * 100) + "%" } })))),
      h("div", { class: "home-api-cell" }, h("b", {}, (slots.in_use || 0) + " / " + (slots.count || 0)), h("span", {}, t("home.slots")),
        h("div", { class: "home-slots", "aria-hidden": "true" }, Array.from({ length: Math.min(30, slots.count || 0) }, (_, i) => h("i", { class: i < slots.in_use ? "on" : "" })))),
      h("div", { class: "home-api-cell" }, h("div", { class: "home-key", "aria-hidden": "true" }, icon("calendar")),
        h("b", { class: "home-key-days" }, keyLabel), h("span", {}, t("home.key")))));
  }

  const dashboardOpen = {};
  function dashboardFold(key, ic, title, body) {
    const fold = h("details", { class: "card dash-fold", "data-section": key, open: !!dashboardOpen[key] },
      h("summary", {}, icon(ic), title), body);
    fold.addEventListener("toggle", () => { dashboardOpen[key] = fold.open; });
    return fold;
  }

  // ------------------------------------------------------------------ dashboard cards
  // The dashboard starts with cards: API use (always) and what the bots put on the board.
  // The user can close a bot's card; SnapLab remembers that (dashboard.json "hidden")
  // and "Closed cards" at the end of the cards brings them back.
  const hiddenCards = () => new Set((S.dashboard && S.dashboard.hidden) || []);
  async function hideCard(key, hidden) {
    const d = S.dashboard = S.dashboard || { widgets: [] };
    const before = (d.hidden || []).slice();
    d.hidden = before.filter((k) => k !== key).concat(hidden ? [key] : []);
    if (S.route.name === "dashboard") render();
    try {
      const j = await api("/api/dashboard/hide", { card: key, hidden });
      S.dashboard = j.dashboard;
      if (hidden) toast(t("dash.card.closed"));
    } catch (e) { d.hidden = before; fail(e); if (S.route.name === "dashboard") render(); }
  }
  function closeButton(key, title) {
    return h("button", { class: "icon-btn card-close", type: "button", "aria-label": t("dash.card.close", { title: title || "" }), title: t("dash.card.close", { title: title || "" }),
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); hideCard(key, true); } }, icon("x"));
  }
  function closedCardsSheet() {
    const list = h("div", { class: "closed-cards" });
    const d = S.dashboard || { widgets: [] };
    const fill = () => {
      const hidden = hiddenCards();
      const rows = [];
      (d.widgets || []).forEach((w) => { if (hidden.has("widget:" + w.id)) rows.push(["widget:" + w.id, w.title || w.id, t("dash.card.bot")]); });
      list.replaceChildren(...(rows.length ? rows.map(([key, title, sub]) => h("div", { class: "closed-card" },
        h("div", {}, h("b", {}, title), h("span", { class: "muted" }, sub)),
        h("button", { class: "btn ghost small", type: "button", onclick: async () => { await hideCard(key, false); fill(); } }, icon("refresh"), t("dash.card.show"))))
        : [h("p", { class: "empty" }, t("dash.card.none"))]));
    };
    fill();
    sheet(t("dash.card.closedTitle"), [h("p", { class: "lead-text" }, t("dash.card.closedHint")), list]);
  }

  function renderDashboard() {
    const work = S.sessions.filter((s) => ["running", "queued"].includes(s.status)).sort(byUrgency);
    const history = S.sessions.filter((s) => !["running", "queued"].includes(s.status)).sort(byUrgency);
    const body = h("div", { class: "page dash" },
      h("div", { class: "dash-heading" }, h("h1", {}, t("dash.title"))),
      boardSection(),
      dashboardFold("projects", "folder", t("dash.projects.title"), projectsCard()),
      dashboardFold("work", "progress", t("dash.work.active", { n: work.length }),
        h("div", { class: "sess-list" }, work.length ? work.map(sessionRow) : h("p", {}, t("dash.work.empty")))),
      dashboardFold("history", "archive", t("dash.history", { n: history.length }),
        h("div", { class: "sess-list" }, history.length ? history.map(sessionRow) : h("p", {}, t("dash.history.empty")))),
      dashboardFold("tasks", "calendar", t("task.title"), tasksCard(null) || h("p", {}, t("task.empty"))),
      dashboardFold("team", "users", t("dash.team"), teamCard()),
      dashboardFold("activity", "clock", t("dash.activity"), h("div", { id: "activity-card" }, activityCard())));
    screen({ title: t("dash.title") }, body);
    renderUsageCard();
    api("/api/usage").then((j) => { S.usage = j.usage; S.activity = j.activity; renderUsageCard(); renderActivityCard(); }).catch(() => {});
  }

  // projects with their owner and how far their SnapWork has got (round 6)
  function projectsCard() {
    const ps = S.projects.slice().sort((a, b) => (b.running || 0) - (a.running || 0) || String(b.last || "").localeCompare(String(a.last || "")));
    const head = h("div", { class: "card-h row" }, icon("folder"), h("span", {}, t("dash.projects.title") + " · " + S.projects.length),
      h("a", { class: "btn ghost small right", href: "#/projects" }, t("dash.projects.all"), icon("right")));
    if (!ps.length) return h("div", { class: "card projects-card" }, head, h("p", { class: "lead-text" }, t("dash.projects.empty")),
      h("button", { class: "btn ghost", type: "button", onclick: newProjectSheet }, icon("plus"), t("projects.new")));
    return h("div", { class: "card projects-card" }, head, h("div", { class: "pp-list" }, ps.map((p) => {
      const owner = bot(p.bot);
      const pr = p.progress || null;
      return h("a", { class: "pp-row", href: "#/project/" + p.id },
        h("div", { class: "pp-top" }, h("b", { class: "pp-name" }, p.name), owner ? h("span", { class: "who" }, avatar(owner, "xs"), owner.name) : null),
        pr && pr.total ? h("div", { class: "pp-prog" }, h("span", { class: "bar" }, h("span", { style: { width: Math.round((pr.done / pr.total) * 100) + "%" } })),
          h("span", { class: "pp-num" }, t("work.steps", { done: pr.done, total: pr.total }))) : null,
        h("div", { class: "pp-sub" }, p.running ? h("span", { class: "status st-running" }, h("span", { class: "pulse-dot" }), t("projects.running", { n: p.running }))
          : h("span", {}, p.sessions ? t(p.sessions === 1 ? "work.count1" : "work.count", { n: p.sessions }) : t("work.none"))));
    })));
  }

  // scheduled tasks: on the dashboard all of them, on a bot's inside screen its own
  function whenText(sch) {
    if (!sch) return "";
    if (sch.kind === "once") { const at = new Date(sch.at); return t("task.when.once", { time: isNaN(at) ? "" : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }); }
    if (sch.kind === "every") { const m = sch.every_minutes || 0; return m % 60 === 0 ? t("task.when.hours", { n: m / 60 }) : t("task.when.every", { n: m }); }
    if (sch.kind === "weekly") return t("task.when.weekly", { days: (sch.days || []).map((d) => t("day." + d)).join(t("list.sep")), time: sch.time });
    return t("task.when.daily", { time: sch.time });
  }
  function taskRow(tk, onChange) {
    const act = async (action) => {
      if (action === "delete" && !(await confirmSheet(t("task.delete.title"), h("p", {}, t("task.delete.body", { title: tk.title })), t("task.delete")))) return;
      try { await api("/api/tasks/" + tk.id, { action }); toast(t("task.done." + action)); if (onChange) onChange(); } catch (e) { fail(e); }
    };
    const b = bot(tk.bot);
    return h("div", { class: "task-row" + (tk.status === "paused" ? " paused" : "") },
      h("div", { class: "task-main" }, h("b", {}, tk.title),
        h("span", { class: "task-when" }, icon("repeat"), whenText(tk.schedule)),
        h("span", { class: "task-next" }, tk.status === "paused" ? t("task.paused") : t("task.next", { when: tk.next_run ? relDay(tk.next_run) : "-" }),
          b ? h("span", { class: "who" }, avatar(b, "xs"), b.name) : null)),
      h("div", { class: "task-acts" },
        tk.status === "paused" ? h("button", { class: "btn ghost small", type: "button", onclick: () => act("resume") }, icon("play"), t("task.resume"))
          : h("button", { class: "btn ghost small", type: "button", onclick: () => act("pause") }, icon("pause"), t("task.pause")),
        h("button", { class: "btn ghost small", type: "button", onclick: () => act("run") }, icon("refresh"), t("task.run")),
        h("button", { class: "icon-btn", type: "button", "aria-label": t("task.delete"), onclick: () => act("delete") }, icon("trash"))));
  }
  function relDay(iso) {
    const d = new Date(iso), now = new Date();
    const time = d.toLocaleTimeString(loc(), { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return t("common.today.at", { time });
    const tm = new Date(now); tm.setDate(now.getDate() + 1);
    if (d.toDateString() === tm.toDateString()) return t("common.tomorrow.at", { time });
    return d.toLocaleDateString(loc(), { weekday: "short", month: "short", day: "numeric" }) + " " + time;
  }
  function tasksCard(botId) {
    const list = (S.tasks || []).filter((x) => !botId || x.bot === botId);
    if (!botId && !list.length) return null;
    return h("div", { class: "card tasks-card" }, h("div", { class: "card-h row" }, icon("calendar"), h("span", {}, t("task.title"))),
      list.length ? h("div", { class: "task-list" }, list.map((x) => taskRow(x, () => api("/api/tasks").then((j) => { S.tasks = j.tasks; render(); })))) :
        h("p", { class: "lead-text" }, t("task.empty")));
  }

  // the bots, as faces: tap one to see what it is doing and what it remembers
  function teamCard() {
    const bs = S.bots.slice().sort((a, b) => (b.orchestrator ? 1 : 0) - (a.orchestrator ? 1 : 0));
    return h("div", { class: "card team-card" }, h("div", { class: "card-h row" }, icon("users"), h("span", {}, t("dash.team") + " · " + S.bots.length),
      h("a", { class: "btn ghost small right", href: "#/bots" }, t("dash.team.all"), icon("right"))),
      h("div", { class: "team-row" }, bs.map((b) => {
        const face = h("a", { class: "team-face", href: "#/bot/" + b.id, "data-bot": b.id }, avatar(b, "md"), h("span", {}, b.name));
        longPress(face, () => botMenu(b));
        return face;
      }),
        h("button", { class: "team-face add", type: "button", onclick: newBotSheet }, h("span", { class: "av md add" }, icon("plus")), h("span", {}, t("chats.newbot")))));
  }

  function activityCard() {
    return h("div", { class: "card" }, h("h3", { class: "card-h" }, t("dash.activity")),
      S.activity.length ? h("div", { class: "activity" }, S.activity.slice(0, 12).map((a) => h("div", { class: "act" }, h("span", { class: "act-dot k-" + a.kind }), h("span", { class: "act-text" }, lineText(a)), h("span", { class: "act-time" }, relTime(a.ts))))) : h("p", { class: "empty" }, t("dash.activity.empty")));
  }
  function renderActivityCard() {
    const host = $("#activity-card");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(activityCard());
  }

  function needsCard() {
    const qs = S.questions;
    const ss = attentionWork().sort(byUrgency);
    if (!qs.length && !ss.length) return null;
    const rows = [];
    qs.slice(0, 3).forEach((q) => {
      const b = bot(q.bot);
      rows.push(h("a", { class: "needs-row q", href: "#/room/" + q.room }, h("span", { class: "needs-ic " + (q.kind === "approval" ? "info" : "warn") }, icon(q.kind === "approval" ? "shield" : "help")),
        h("div", { class: "needs-main" }, h("b", {}, q.kind === "approval" ? t("q.approval") : (b ? b.name : t("q.title"))), h("span", {}, q.question)), icon("right")));
    });
    ss.slice(0, 4).forEach((s) => rows.push(h("a", { class: "needs-row", href: "#/session/" + s.id }, statusPill(s.status),
      h("div", { class: "needs-main" }, h("b", {}, s.title || s.id), h("span", {}, s.project_name || "")), icon("right"))));
    return h("div", { class: "card needs-card" }, h("h3", { class: "card-h" }, icon("inbox"), t("needs.title"), needsCount() ? h("span", { class: "needs-badge" }, String(needsCount())) : null), h("div", { class: "needs-rows" }, rows));
  }

  function renderUsageCard() {
    const host = $("#usage-card");
    if (!host) return;
    const u = S.usage || { today: {}, slots: {}, key: {} };
    const slots = u.slots || { in_use: 0, count: 3 };
    const meter = h("div", { class: "slots" }, Array.from({ length: slots.count || 3 }, (_, i) => h("span", { class: "slot" + (i < slots.in_use ? " on" : "") + (i < (slots.mine || 0) ? " mine" : "") })));
    const key = u.key || {};
    let keyText = t("dash.key.none"), keyCls = "";
    if (!key.set) { keyText = t("dash.key.missing"); keyCls = "bad"; }
    else if (key.days_left != null) { keyText = key.days_left < 0 ? t("dash.key.expired") : key.days_left === 0 ? t("dash.key.today") : t("dash.key.days", { n: key.days_left }); keyCls = key.days_left < 7 ? "warn" : ""; }
    const today = u.today || {};
    const week = u.week || [];
    const tokens = (d) => (d.input_tokens || 0) + (d.output_tokens || 0);
    const card = h("a", { class: "api-summary", href: "#/usage", "aria-label": t("usage.open") },
      h("div", { class: "card-h row" }, icon("coins"), h("span", {}, t("dash.api")), h("span", { class: "right usage-more" }, t("usage.more"), icon("right"))),
      h("div", { class: "api-grid three" },
        h("div", { class: "api-cell" }, h("span", { class: "api-label" }, t("home.calls")), h("b", { class: "api-big" }, num(today.calls || 0)),
          today.errors ? h("span", { class: "api-note warn" }, t("usage.errors.n", { n: today.errors })) : null),
        h("div", { class: "api-cell" }, h("span", { class: "api-label" }, t("dash.tokens")), h("b", { class: "api-big" }, num(tokens(today))),
          h("span", { class: "api-note" }, t("session.in") + " " + num(today.input_tokens) + " · " + t("session.out") + " " + num(today.output_tokens))),
        h("div", { class: "api-cell" }, h("span", { class: "api-label" }, t("dash.week")),
          columnChart(week.map((d) => ({ value: tokens(d), title: d.day + " · " + num(tokens(d)) })), week.length - 1, null, { mini: true }))),
      h("div", { class: "api-foot" },
        h("span", { class: "api-slots" }, meter, h("span", {}, t("dash.slots") + " " + (slots.in_use || 0) + " / " + (slots.count || 3))),
        slots.waiting ? h("span", { class: "api-note warn" }, t("dash.slots.waiting", { n: slots.waiting })) : null,
        h("span", { class: "api-key " + keyCls }, t("dash.key") + " · " + keyText)));
    host.replaceChildren(card);
  }

  // A column chart in plain HTML (no library): one bar per item, the chosen one in full colour. Every bar is a
  // button with its value in words, so it can be read and chosen without seeing the colour.
  function columnChart(items, selected, onPick, opts) {
    opts = opts || {};
    const max = Math.max(1, ...items.map((x) => x.value || 0));
    const every = Math.max(1, Math.ceil(items.length / (opts.labels || 7)));
    const bars = items.map((x, i) => {
      const pct = x.value ? Math.max(3, Math.round((x.value / max) * 100)) : 0;
      const attrs = { class: "ubar" + (i === selected ? " on" : "") + (x.value ? "" : " zero"), title: x.title || "", "aria-label": x.title || "" };
      const bar = onPick ? h("button", Object.assign(attrs, { type: "button", "aria-pressed": String(i === selected), onclick: () => onPick(i) }), h("span", { style: { height: pct + "%" } }))
        : h("span", attrs, h("span", { style: { height: pct + "%" } }));
      return bar;
    });
    const chart = h("div", { class: "ucol" + (opts.mini ? " mini" : "") },
      opts.mini ? null : h("div", { class: "ucol-grid", "aria-hidden": "true" }, h("span", {}, opts.fmt ? opts.fmt(max) : num(max)), h("span", {}, opts.fmt ? opts.fmt(max / 2) : num(max / 2)), h("span", {}, "0")),
      h("div", { class: "ucol-bars", style: { "--n": items.length } }, bars));
    // Labels every few columns counted from the newest one; the chosen column always shows its label and its
    // close neighbours give way, so labels never overlap.
    const near = (i) => selected >= 0 && i !== selected && Math.abs(i - selected) < every;
    if (!opts.mini) chart.appendChild(h("div", { class: "ucol-x", style: { "--n": items.length }, "aria-hidden": "true" },
      items.map((x, i) => h("span", { class: i === selected ? "on" : "" }, i === selected || ((items.length - 1 - i) % every === 0 && !near(i)) ? (x.label || "") : ""))));
    return chart;
  }

  function boardSection() {
    const d = S.dashboard || { widgets: [] };
    const hidden = hiddenCards();
    const wrap = h("div", { class: "board-wrap" },
      h("div", { class: "board-head" }, h("h3", { class: "section" }, icon("layout"), t("dash.board")), d.updated_by ? h("span", { class: "muted small" }, t("dash.board.by", { name: d.updated_by, when: relTime(d.updated) })) : null));
    const grid = h("div", { class: "board" });
    grid.appendChild(h("div", { class: "card widget full dash-card usage-card", id: "usage-card" }));
    const shown = (d.widgets || []).filter((w) => !hidden.has("widget:" + w.id));
    shown.forEach((w) => grid.appendChild(widgetEl(w)));
    const closed = (d.widgets || []).filter((w) => hidden.has("widget:" + w.id)).length;
    const onlyIntro = !shown.length || (shown.length === 1 && shown[0].id === "welcome");
    if (onlyIntro) {
      const lead = S.bots.find((x) => x.orchestrator);
      const dm = lead && S.rooms.find((r) => r.kind === "dm" && r.bots[0] === lead.id);
      grid.appendChild(h("div", { class: "card board-invite full" }, h("img", { src: "/brand/sail-blue.png", alt: "", class: "invite-art" }),
        h("div", {}, h("b", {}, t("dash.board.invite.title")), h("p", { class: "muted" }, t("dash.board.invite.body", { name: lead ? lead.name : "" }))),
        dm ? h("button", { class: "btn primary", type: "button", onclick: () => { S.prefill = t("room.try.4"); go("#/room/" + dm.id); } }, icon("spark"), t("dash.board.invite.btn")) : null));
    }
    wrap.appendChild(grid);
    if (closed) wrap.appendChild(h("button", { class: "btn ghost small closed-link", type: "button", onclick: closedCardsSheet }, icon("eye"), t("dash.card.closedN", { n: closed })));
    return wrap;
  }

  function widgetEl(w) {
    const card = h("div", { class: "card widget dash-card w-" + w.type + " " + (w.size === "full" ? "full" : "half") + " tone-" + (w.tone || "neutral"), "data-card": "widget:" + w.id },
      closeButton("widget:" + w.id, w.title || w.id),
      w.title ? h("div", { class: "w-title" }, w.title) : null);
    if (w.type === "stat") append(card, [h("div", { class: "w-value" }, String(w.value), w.unit ? h("span", { class: "w-unit" }, w.unit) : null), w.note ? h("div", { class: "w-note" }, w.note) : null]);
    else if (w.type === "progress") append(card, [h("div", { class: "w-value" }, w.value + "%"), h("div", { class: "w-bar" }, h("span", { style: { width: Math.max(0, Math.min(100, w.value)) + "%" } })), w.note ? h("div", { class: "w-note" }, w.note) : null]);
    else if (w.type === "list") card.appendChild(h("ul", { class: "w-items" }, (w.items || []).map((it) => h("li", { class: "tone-" + (it.tone || "neutral") }, h("span", { class: "li-dot" }), h("span", { class: "li-text" }, it.text), it.meta ? h("span", { class: "li-meta" }, it.meta) : null))));
    else if (w.type === "note") card.appendChild(h("p", { class: "w-text" }, w.text));
    else if (w.type === "table") card.appendChild(h("div", { class: "w-table-wrap" }, h("table", { class: "w-table" }, h("thead", {}, h("tr", {}, w.columns.map((c) => h("th", {}, c)))), h("tbody", {}, w.rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c))))))));
    else if (w.type === "bars") {
      const max = Math.max(1, ...(w.items || []).map((i) => i.value));
      card.appendChild(h("div", { class: "w-bars" }, (w.items || []).map((i) => h("div", { class: "w-barrow" }, h("span", { class: "bl" }, i.label), h("span", { class: "bt" }, h("span", { style: { width: Math.round((i.value / max) * 100) + "%" } })), h("span", { class: "bv" }, String(i.value) + (w.unit ? " " + w.unit : ""))))));
    }
    return card;
  }

  // ------------------------------------------------------------------ API usage (dashboard → API usage)
  // Days, weeks or months as columns (input tokens, output tokens, calls or errors); tap a column to see that
  // period by model, by use, by bot, by room or SnapCode and by hour. Older days have fewer details recorded.

  const U = { unit: "day", metric: "input_tokens", offset: 0, sel: null, history: null, details: {}, timer: null };
  const UNITS = { day: 14, week: 12, month: 12 };
  const ymd = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const dayOf = (s) => { const [y, m, d] = String(s).split("-").map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const exact = (n) => Number(n || 0).toLocaleString(loc());

  function usageBuckets() {
    const today = dayOf((U.history && U.history.today) || ymd(new Date()));
    const n = UNITS[U.unit], out = [];
    for (let i = 0; i < n; i++) {
      const back = U.offset * n + (n - 1 - i);
      let from, to, label, title;
      if (U.unit === "day") {
        from = to = addDays(today, -back);
        label = from.toLocaleDateString(loc(), { month: "numeric", day: "numeric" });
        title = from.toLocaleDateString(loc(), { month: "long", day: "numeric", weekday: "short" });
      } else if (U.unit === "week") {
        const monday = addDays(today, -((today.getDay() + 6) % 7));
        from = addDays(monday, -7 * back); to = addDays(from, 6);
        label = from.toLocaleDateString(loc(), { month: "numeric", day: "numeric" });
        title = from.toLocaleDateString(loc(), { month: "long", day: "numeric" }) + " – " + to.toLocaleDateString(loc(), { month: "long", day: "numeric" });
      } else {
        from = new Date(today.getFullYear(), today.getMonth() - back, 1); to = new Date(from.getFullYear(), from.getMonth() + 1, 0);
        label = from.toLocaleDateString(loc(), { month: "short" });
        title = from.toLocaleDateString(loc(), { year: "numeric", month: "long" });
      }
      const a = ymd(from), b = ymd(to);
      const sum = { calls: 0, input_tokens: 0, output_tokens: 0, errors: 0 };
      ((U.history && U.history.days) || []).forEach((r) => { if (r.day >= a && r.day <= b) Object.keys(sum).forEach((k) => { sum[k] += r[k] || 0; }); });
      out.push({ from: a, to: b, label, title, ...sum });
    }
    return out;
  }
  const metricText = (m, v) => m === "calls" ? t("usage.calls.n", { n: exact(v) }) : m === "errors" ? t("usage.errors.n", { n: exact(v) }) : t("usage.tokens.n", { n: num(v) });

  async function loadUsage(force) {
    try {
      if (force || !U.history) U.history = await api("/api/usage/history");
      const bs = usageBuckets(), b = bs[U.sel == null ? bs.length - 1 : U.sel];
      const key = b.from + ":" + b.to;
      if (force || !U.details[key]) U.details[key] = await api("/api/usage/detail?from=" + b.from + "&to=" + b.to);
      if (S.route.name === "usage") renderUsage();
    } catch (e) { fail(e); }
  }
  function refreshUsageSoon() {
    if (U.timer) return;
    U.timer = setTimeout(() => { U.timer = null; if (S.route.name === "usage" && !document.hidden) { U.details = {}; loadUsage(true); } }, 5000);
  }

  function renderUsage() {
    const y = $(".usage-page") ? window.scrollY : 0;
    const bs = usageBuckets();
    if (U.sel == null || U.sel >= bs.length) U.sel = bs.length - 1;
    const b = bs[U.sel], d = U.details[b.from + ":" + b.to];
    const first = U.history && U.history.first_day;
    const seg = (items, value, pick) => h("div", { class: "seg" }, items.map(([v, label]) =>
      h("button", { type: "button", class: v === value ? "on" : "", "aria-pressed": String(v === value), onclick: () => pick(v) }, label)));
    const unitSeg = seg([["day", t("usage.unit.day")], ["week", t("usage.unit.week")], ["month", t("usage.unit.month")]], U.unit,
      (v) => { U.unit = v; U.offset = 0; U.sel = null; renderUsage(); });
    const metricSeg = seg([["input_tokens", t("usage.m.in")], ["output_tokens", t("usage.m.out")], ["calls", t("usage.m.calls")], ["errors", t("usage.m.errors")]], U.metric,
      (v) => { U.metric = v; renderUsage(); });
    const older = !!first && bs[0].from > first;
    const nav = h("div", { class: "usage-nav" },
      h("button", { class: "icon-btn", type: "button", disabled: !older, "aria-label": t("usage.older"), onclick: () => { U.offset++; U.sel = null; renderUsage(); } }, icon("back")),
      h("div", { class: "usage-range" }, h("b", {}, t("usage.chart." + U.metric)), h("span", {}, bs[0].title + " – " + bs[bs.length - 1].title)),
      h("button", { class: "icon-btn flip", type: "button", disabled: U.offset === 0, "aria-label": t("usage.newer"), onclick: () => { U.offset--; U.sel = null; renderUsage(); } }, icon("back")));
    const chart = U.history ? columnChart(bs.map((x) => ({ value: x[U.metric], label: x.label, title: x.title + " · " + metricText(U.metric, x[U.metric]) })), U.sel,
      (i) => { U.sel = i; renderUsage(); }, { labels: U.unit === "day" ? 5 : U.unit === "week" ? 4 : 6, fmt: (v) => U.metric.endsWith("tokens") ? num(Math.round(v)) : exact(Math.round(v)) })
      : h("div", { class: "usage-loading" }, h("span", { class: "spinner" }));
    const tile = (label, value, sub) => h("div", { class: "usage-tile" }, h("span", { class: "api-label" }, label), h("b", {}, value), sub ? h("span", { class: "api-note" }, sub) : null);
    const body = h("div", { class: "page usage-page" },
      unitSeg,
      h("section", { class: "card usage-chart" }, nav, metricSeg, chart),
      h("section", { class: "usage-period" },
        h("h2", { class: "usage-period-title" }, b.title),
        h("div", { class: "usage-tiles" },
          tile(t("usage.m.calls"), exact(b.calls), b.calls ? t("usage.errors.rate", { n: exact(b.errors), p: (b.errors / b.calls * 100).toFixed(1) }) : null),
          tile(t("usage.chart.input_tokens"), num(b.input_tokens), exact(b.input_tokens)),
          tile(t("usage.chart.output_tokens"), num(b.output_tokens), exact(b.output_tokens)),
          tile(t("usage.avg"), b.calls ? num(Math.round(b.input_tokens / b.calls)) + " / " + num(Math.round(b.output_tokens / b.calls)) : "—", t("usage.avg.sub"))),
        d ? usageBreakdowns(d) : h("div", { class: "usage-loading" }, h("span", { class: "spinner" }))),
      U.history ? h("p", { class: "usage-total" }, t("usage.total", { since: first ? dayOf(first).toLocaleDateString(loc(), { year: "numeric", month: "long", day: "numeric" }) : "—",
        calls: exact(U.history.total.calls), input: num(U.history.total.input_tokens), output: num(U.history.total.output_tokens), errors: exact(U.history.total.errors) })) : null);
    screen({ back: "#/dashboard", title: t("usage.title"), tab: "dashboard", cls: "usage" }, body);
    if (y) window.scrollTo(0, y);
    if (!U.history || !d) loadUsage();
  }

  function usageBreakdowns(d) {
    const m = U.metric;
    const line = (r) => [t("usage.calls.n", { n: exact(r.calls) }), t("session.in") + " " + num(r.input_tokens), t("session.out") + " " + num(r.output_tokens),
      r.errors ? t("usage.errors.n", { n: exact(r.errors) }) : null].filter(Boolean).join(" · ");
    const section = (title, rows, opts) => {
      opts = opts || {};
      if (!rows.length) return null;
      const val = (r) => r.callsOnly && m.endsWith("tokens") ? 0 : r[m] || 0;
      const max = Math.max(1, ...rows.map(val));
      const callsMax = Math.max(1, ...rows.map((r) => r.calls || 0));
      const onlyCalls = rows.some((r) => r.callsOnly) && m.endsWith("tokens");
      const list = h("div", { class: "ubreak-list" });
      const draw = (all) => list.replaceChildren(...rows.slice(0, all ? rows.length : 6).map((r) => h(r.href ? "a" : "div", { class: "ubreak", href: r.href || null },
        h("div", { class: "ubreak-top" }, r.lead || null, h("span", { class: "ubreak-name" }, r.name),
          h("b", { class: "ubreak-value" }, r.callsOnly && m.endsWith("tokens") ? metricText("calls", r.calls) : metricText(m, r[m]))),
        h("span", { class: "ubreak-track" + (r.callsOnly && m.endsWith("tokens") ? " calls" : ""), "aria-hidden": "true" }, h("span", { style: { width: (r.callsOnly && m.endsWith("tokens")
          ? Math.max(2, Math.round((r.calls / callsMax) * 100)) : (r[m] ? Math.max(2, Math.round((r[m] / max) * 100)) : 0)) + "%" } })),
        h("span", { class: "ubreak-sub" }, r.sub ? r.sub + " · " : "", r.callsOnly ? "" : line(r)))));
      draw(false);
      const more = rows.length > 6 ? h("button", { class: "btn ghost small", type: "button", onclick: () => { draw(true); more.remove(); } }, t("usage.showAll", { n: rows.length })) : null;
      const note = [opts.note, onlyCalls ? t("usage.callsOnly") : null].filter(Boolean).join(" ");
      return h("section", { class: "card usage-break" }, h("h3", { class: "card-h" }, icon(opts.icon || "coins"), title), note ? h("p", { class: "hint" }, note) : null, list, more);
    };
    const model = (id) => { const x = (S.models || []).find((mm) => mm.name === id); return x ? x.label || id : id; };
    const srcName = (id) => { const s = t("usage.src." + id); return s === "usage.src." + id ? id : s; };
    const bots = {};
    d.callers.forEach((c) => {
      const b0 = c.bot && bot(c.bot);
      const k = b0 ? b0.id : "";
      const row = bots[k] = bots[k] || { id: k, name: b0 ? b0.name : t("usage.bot.none"), lead: b0 ? avatar(b0, "xs") : icon("grid"), calls: 0, input_tokens: 0, output_tokens: 0, errors: 0 };
      ["calls", "input_tokens", "output_tokens", "errors"].forEach((f) => { row[f] += c[f] || 0; });
    });
    const byMetric = (a, b) => (b[m] || 0) - (a[m] || 0);
    const partial = d.days_with_detail < d.days;
    const since = U.history && U.history.detail_since;
    const note = partial ? (since ? t("usage.partial", { since: dayOf(since).toLocaleDateString(loc(), { month: "long", day: "numeric" }) }) : t("usage.partial.none")) : null;
    const callerRow = (c) => {
      const b0 = c.bot && bot(c.bot);
      if (c.kind === "session") return { ...c, name: c.name, sub: [c.project, b0 && b0.name].filter(Boolean).join(" · "), lead: icon("terminal"), href: "#/session/" + c.id };
      if (c.kind === "room") return { ...c, name: c.name, sub: c.group ? t("usage.caller.group") : (b0 ? b0.name : ""), lead: icon("chat"), href: "#/room/" + c.id };
      return { ...c, name: c.name || t("usage.caller.none"), lead: icon("grid") };
    };
    const hours = d.hours.map((x, i) => ({ value: x[m] || 0, label: String(i), title: t("usage.hour", { h: i }) + " · " + metricText(m, x[m] || 0) }));
    return h("div", { class: "usage-breaks" },
      d.days_with_detail ? null : h("p", { class: "hint usage-nodetail" }, note || t("usage.partial.none")),
      section(t("usage.by.model"), d.models.map((r) => ({ ...r, name: model(r.id) })).sort(byMetric), { icon: "brain" }),
      section(t("usage.by.source"), d.sources.map((r) => ({ ...r, name: srcName(r.id), callsOnly: r.calls_only })).sort(byMetric), { icon: "layers" }),
      section(t("usage.by.bot"), Object.values(bots).sort(byMetric), { icon: "users", note }),
      section(t("usage.by.caller"), d.callers.map(callerRow).sort(byMetric), { icon: "chat", note }),
      d.days_with_detail ? h("section", { class: "card usage-break" }, h("h3", { class: "card-h" }, icon("clock"), t("usage.by.hour")), note ? h("p", { class: "hint" }, note) : null,
        columnChart(hours, -1, null, { labels: 6, fmt: (v) => m.endsWith("tokens") ? num(Math.round(v)) : exact(Math.round(v)) })) : null);
  }

  // ------------------------------------------------------------------ bots

  function renderBots() {
    const body = h("div", { class: "page" });
    const grid = h("div", { class: "bot-grid" });
    S.bots.slice().sort((a, b) => (b.orchestrator ? 1 : 0) - (a.orchestrator ? 1 : 0)).forEach((b) => {
      const busy = S.sessions.filter((s) => s.started_by === b.id && (s.status === "running" || s.status === "queued")).length;
      const card = grid.appendChild(h("a", { class: "card bot-card" + (b.orchestrator ? " is-lead" : ""), href: "#/bot/" + b.id, style: { "--c": b.color }, "data-bot": b.id },
        h("div", { class: "bot-glow" }),
        b.orchestrator ? h("span", { class: "lead-badge" }, icon("sail"), t("bots.orchestrator")) : null,
        avatar(b, "lg"),
        h("div", { class: "bot-name" }, b.name),
        h("div", { class: "bot-role" }, b.orchestrator ? t("bots.lead.role") : (b.role || t("room.teammate"))),
        h("div", { class: "bot-meta" }, h("span", {}, icon("folder"), t((b.projects || []).length === 1 ? "bots.project1" : "bots.projects", { n: (b.projects || []).length })),
          busy ? h("span", { class: "busy" }, h("span", { class: "spinner sm" }), t("bots.working", { n: busy })) : null)));
      longPress(card, () => botMenu(b));
    });
    body.appendChild(grid);
    body.appendChild(h("p", { class: "hint bots-hold" }, t("bots.hold")));
    const archived = S.archivedBots || [];
    if (archived.length) {
      const section = body.appendChild(h("section", { class: "chat-section bots-archived" }, h("h2", {}, icon("archive"), t("bots.archived", { n: archived.length })),
        h("p", { class: "hint" }, t("bots.archived.d")),
        h("div", { class: "list card-list" }, archived.map((b) => h("div", { class: "row archived-row", "data-bot": b.id },
          avatar(b, "md"),
          h("div", { class: "row-main" }, h("div", { class: "row-top" }, h("span", { class: "row-title" }, b.name)),
            h("div", { class: "row-sub" }, b.archived_at ? t("bots.archived.at", { when: relTime(b.archived_at) }) : (b.role || ""))),
          h("button", { class: "btn small restore-bot", type: "button", onclick: () => restoreBot(b) }, icon("refresh"), t("bots.restore")))))));
      if ((S.route.q || {}).archived && section.scrollIntoView) setTimeout(() => section.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    }
    screen({ back: "#/dashboard", title: t("bots.title"), tab: "dashboard", cls: "bots" }, body, fab(t("bots.new"), "plus", newBotSheet));
  }

  // A long press on a phone, or a right click with a mouse, calls fn. The click that ends a long press does nothing
  // else, so the card does not open as well (2026-11-16).
  function longPress(el, fn) {
    let timer = null, start = null, fired = false;
    el.classList.add("holdable");
    el.setAttribute("draggable", "false");
    el.addEventListener("dragstart", (e) => e.preventDefault());
    const cancel = () => { if (timer) clearTimeout(timer); timer = null; };
    const open = (e) => { fired = true; guardClicks(); fn(e); };
    el.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      fired = false; start = { x: e.clientX, y: e.clientY }; cancel();
      timer = setTimeout(() => { timer = null; if (navigator.vibrate) navigator.vibrate(12); open(e); }, 500);
    });
    el.addEventListener("pointermove", (e) => {
      if (timer && start && Math.hypot((e.clientX || 0) - start.x, (e.clientY || 0) - start.y) > 10) cancel();
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((type) => el.addEventListener(type, cancel));
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); cancel(); if (!fired) open(e); });
    el.addEventListener("click", (e) => { if (fired) { e.preventDefault(); if (e.stopPropagation) e.stopPropagation(); fired = false; } }, true);
  }

  // When the finger lifts after a long press, the browser may send a click to whatever is under it now: the scrim of
  // the menu that just opened, which would close it at once (2026-11-17, seen with a real touch in Edge). Swallow
  // clicks until the next press starts; a real tap always begins with a press, so none is ever lost.
  let clickGuard = null;
  function guardClicks() {
    unguardClicks();
    const swallow = (e) => { e.preventDefault(); if (e.stopPropagation) e.stopPropagation(); unguardClicks(); };
    const release = () => unguardClicks();
    clickGuard = { swallow, release, timer: setTimeout(unguardClicks, 10000) };
    window.addEventListener("click", swallow, true);
    window.addEventListener("pointerdown", release, true);
  }
  function unguardClicks() {
    if (!clickGuard) return;
    window.removeEventListener("click", clickGuard.swallow, true);
    window.removeEventListener("pointerdown", clickGuard.release, true);
    clearTimeout(clickGuard.timer);
    clickGuard = null;
  }

  function botMenu(b) {
    const dm = S.rooms.find((x) => x.kind === "dm" && x.bots[0] === b.id);
    let sh = null;
    const row = (ic, label, fn, cls) => h("button", { class: "menu-row" + (cls ? " " + cls : ""), type: "button",
      onclick: () => { sh.close(); fn(); } }, icon(ic), h("span", {}, label));
    sh = sheet(b.name, h("div", { class: "menu-list" },
      dm ? row("chat", t("bot.chat"), () => go("#/room/" + dm.id)) : null,
      row("eye", t("botmenu.inside"), () => go("#/bot/" + b.id)),
      row("spark", t("botmenu.context"), () => go("#/bot/" + b.id + "?fresh=1")),
      row("edit", t("inside.profile"), () => profileSheet(b)),
      b.orchestrator ? h("p", { class: "hint" }, t("botmenu.lead")) : row("archive", t("botmenu.archive"), () => archiveBot(b))));
    return sh;
  }

  // Archiving puts a bot away with everything kept (2026-11-17: the user wanted this rather than deleting).
  async function archiveBot(b) {
    const busy = S.sessions.filter((s) => s.started_by === b.id && (s.status === "running" || s.status === "queued")).length;
    const tasks = (S.tasks || []).filter((x) => x.bot === b.id && x.status === "active").length;
    const ok = await confirmSheet(t("botarc.title", { name: b.name }), [
      h("p", {}, t("botarc.body", { name: b.name })),
      busy ? h("p", { class: "warn-line" }, icon("alert"), " ", t("botarc.running", { n: busy })) : null,
      tasks ? h("p", {}, t("botarc.tasks", { n: tasks })) : null,
      h("p", { class: "hint" }, t("botarc.back"))], t("botarc.ok"));
    if (!ok) return false;
    try {
      await api("/api/bots/" + b.id, { action: "archive", confirmed: true });
      S.bots = S.bots.filter((x) => x.id !== b.id);
      S.rooms = S.rooms.filter((r) => !(r.kind === "dm" && r.bots[0] === b.id));
      S.archivedBots = (S.archivedBots || []).filter((x) => x.id !== b.id).concat([{ id: b.id, name: b.name, color: b.color,
        initials: b.initials, avatar: b.avatar, role: b.role, archived_at: new Date().toISOString() }]);
      toast(t("botarc.done", { name: b.name }));
      if (S.route.name === "bot" && S.route.id === b.id) go("#/bots"); else render();
      return true;
    } catch (e) { fail(e); return false; }
  }

  async function restoreBot(b) {
    try {
      const j = await api("/api/bots/" + b.id, { action: "restore" });
      S.archivedBots = (S.archivedBots || []).filter((x) => x.id !== b.id);
      if (j.bot && !S.bots.some((x) => x.id === j.bot.id)) S.bots = S.bots.concat([j.bot]);
      toast(t("bots.restored", { name: b.name }));
      render();
      return true;
    } catch (e) { fail(e); return false; }
  }

  function newBotSheet() {
    const used = new Set(S.bots.map((x) => String(x.color || "").toUpperCase()));
    let color = (S.palette || []).find((c) => !used.has(String(c).toUpperCase())) || (S.palette || [])[S.bots.length % 8] || "#2F6FED";
    const name = h("input", { class: "input", placeholder: t("ob.name.ph") });
    const role = autosize(h("textarea", { class: "input", rows: "2", maxlength: "300", placeholder: t("bot.role.ph") }), 120);
    let selectedFace = recommendedFace(""), facePicked = false;
    const face = faceField(() => ({ name: name.value, initials: initials(name.value), color, avatar: selectedFace }), (value) => { selectedFace = value; facePicked = true; });
    name.addEventListener("input", face.refresh);
    role.addEventListener("input", () => { if (!facePicked) { selectedFace = recommendedFace(role.value); face.refresh(); } });
    const projects = projectPicker("");
    const btn = h("button", { class: "btn primary wide", type: "button" }, icon("chat"), t("nb.create"));
    const sh = sheet(t("nb.title"), [face.el, field(t("bot.name"), counted(name, NAME_MAX)), field(t("bot.role"), role), field(t("bot.color"), swatches(color, (c) => { color = c; face.refresh(); })), S.projects.length ? field(t("nb.project"), projects.el) : null, btn]);
    setTimeout(() => name.focus(), 300);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const j = await api("/api/bots", { name: name.value, role: role.value, color, avatar: selectedFace, projects: projects.value() });
        if (!S.bots.find((x) => x.id === j.bot.id)) S.bots.push(j.bot);
        if (j.room && !S.rooms.find((r) => r.id === j.room)) S.rooms.unshift({ id: j.room, kind: "dm", name: j.bot.name, bots: [j.bot.id], last_ts: new Date().toISOString(), last_text: "" });
        sh.close(); toast(t("toast.created")); go(j.room ? "#/room/" + j.room : "#/chats");
      } catch (e) { fail(e); btn.disabled = false; }
    });
  }

  // Each bot may own several projects. Selecting a project transfers its ownership.
  function projectPicker(current, self) {
    const value = new Set(Array.isArray(current) ? current : current ? [current] : []);
    const wrap = h("div", { class: "pick-list", role: "group", "aria-label": t("nb.project") });
    const row = (id, label, note) => {
      const b = h("button", { type: "button", class: "pick-row" + (value.has(id) ? " on" : ""), role: "checkbox", "aria-checked": value.has(id) ? "true" : "false",
        onclick: () => { value.has(id) ? value.delete(id) : value.add(id); const on = value.has(id); b.classList.toggle("on", on); b.setAttribute("aria-checked", on ? "true" : "false"); } },
        h("span", { class: "pick-dot" }), id ? icon("folder") : null, h("span", { class: "pick-label" }, label), note ? h("span", { class: "pick-note" }, note) : null);
      wrap.appendChild(b);
    };
    wrap.appendChild(h("p", { class: "lead-text" }, t("nb.projects.hint")));
    S.projects.forEach((p) => {
      const owner = bot(p.bot);
      row(p.id, p.name, owner && !owner.orchestrator && owner.id !== self ? t("nb.project.with", { name: owner.name }) : null);
    });
    return { el: wrap, value: () => [...value] };
  }

  // A bot's inside (round 6): what it sees now, its project, its SnapWork, its memory, a search of the raw record,
  // its scheduled tasks; the profile and instructions are one tap further.
  function renderBot(id) {
    const b = bot(id);
    if (!b) { go("#/chats"); return; }
    const roomId = (S.route.q || {}).room || ((S.rooms.find((x) => x.kind === "dm" && x.bots[0] === b.id) || {}).id);
    const dm = S.rooms.find((x) => x.kind === "dm" && x.bots[0] === b.id);
    const hero = h("div", { class: "bot-hero", style: { "--c": b.color } }, h("div", { class: "bot-hero-bg" }), avatar(b, "xl"), h("div", { class: "bot-hero-name" }, b.name),
      h("div", { class: "bot-hero-role" }, b.orchestrator ? t("bots.orchestrator") : (b.role || t("room.teammate"))),
      h("div", { class: "hero-btns" },
        dm ? h("a", { class: "btn primary", href: "#/room/" + dm.id }, icon("chat"), t("bot.chat")) : null,
        h("button", { class: "btn", type: "button", onclick: () => profileSheet(b) }, icon("edit"), t("inside.profile"))));
    const ctxHost = h("div", { class: "card inside-ctx" }, h("div", { class: "card-h row" }, icon("eye"), h("span", {}, t("inside.sees"))), h("div", { class: "saw-loading" }, h("span", { class: "spinner" })));
    const projHost = h("div", { class: "card" }, h("div", { class: "card-h row" }, icon("folder"), h("span", {}, t(b.orchestrator ? "inside.projects.lead" : "inside.project"))));
    const workHost = h("div", { class: "card" }, h("div", { class: "card-h row" }, icon("work"), h("span", {}, t("inside.work"))));
    const taskHost = h("div", {});
    const freshHost = h("div", {});
    const mem = memoryCard(b);
    const search = archiveSearch(b);
    const body = h("div", { class: "page inside" }, hero, ctxHost, freshHost, projHost, workHost, taskHost, mem.el, search.el);
    screen({ back: roomId ? "#/room/" + roomId : "#/chats", title: b.name, tab: "chats", cls: "bot" }, body);
    mem.load();
    const paintTasks = () => { taskHost.innerHTML = ""; taskHost.appendChild(tasksCard(b.id)); };
    S.insideTasks = paintTasks;
    paintTasks();
    api("/api/bots/" + b.id + "/inside" + (roomId ? "?room=" + roomId : "")).then((j) => {
      if (S.route.name !== "bot" || S.route.id !== b.id) return;
      ctxHost.innerHTML = "";
      append(ctxHost, [h("div", { class: "card-h row" }, icon("eye"), h("span", {}, t("inside.sees"))), contextView(j.view, b, true)]);
      freshHost.innerHTML = "";
      const fresh = freshHost.appendChild(freshCard(b, j.view));
      if ((S.route.q || {}).fresh && fresh.scrollIntoView) fresh.scrollIntoView({ behavior: "smooth", block: "start" });
      // project(s)
      const ps = j.projects || [];
      projHost.querySelectorAll(":scope > :not(.card-h)").forEach((x) => x.remove());
      if (b.orchestrator) {
        projHost.appendChild(h("p", { class: "lead-text" }, t("inside.projects.lead.d")));
        ps.slice(0, 6).forEach((p) => projHost.appendChild(projectLine(p)));
      } else if (ps.length) {
        ps.forEach((p) => {
          projHost.appendChild(projectLine(p));
          if (p.bot !== b.id) projHost.appendChild(h("p", { class: "lead-text" }, t("project.shared")));
        });
        projHost.appendChild(h("button", { class: "btn ghost small", type: "button", onclick: () => pickProjectSheet(b) }, icon("refresh"), t("inside.project.change")));
      } else {
        projHost.appendChild(h("p", { class: "lead-text" }, t("inside.project.none")));
        if (S.projects.length) projHost.appendChild(h("button", { class: "btn ghost", type: "button", onclick: () => pickProjectSheet(b) }, icon("folder"), t("inside.project.pick")));
      }
      // SnapWork
      const ss = j.sessions || [];
      workHost.querySelectorAll(":scope > :not(.card-h)").forEach((x) => x.remove());
      workHost.appendChild(ss.length ? h("div", { class: "sess-list" }, ss.slice(0, 4).map(sessionRow)) : h("p", { class: "lead-text" }, t("inside.work.none")));
      if (j.archive) search.count(j.archive.records || 0);
    }).catch((e) => { ctxHost.appendChild(h("p", { class: "lead-text" }, e.message)); });
  }

  // Cleaning up what a bot is given (2026-11-16). Starting a chat afresh hides the messages so far, the room summary
  // and finished work from the bot; nothing is deleted and it can be undone. The bot can do it itself when asked.
  function freshCard(b, v) {
    const fs = v.fresh_start;
    const room = v.room;
    const reload = () => { if (S.route.name === "bot" && S.route.id === b.id) render(); };
    const el = h("div", { class: "card form fresh-card" }, h("div", { class: "card-h row" }, icon("spark"), h("span", {}, t("fresh.title"))),
      h("p", { class: "lead-text" }, t(v.room_kind === "group" ? "fresh.d.group" : "fresh.d", { name: b.name, room: v.room_name || "" })));
    if (fs) {
      const when = new Date(fs.at).toLocaleString(loc(), { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const undo = h("button", { class: "btn ghost small", type: "button" }, icon("refresh"), t("fresh.undo"));
      undo.addEventListener("click", async () => {
        if (!(await confirmSheet(t("fresh.undo"), h("p", {}, t("fresh.undo.body")), t("fresh.undo")))) return;
        undo.disabled = true;
        try { await api("/api/rooms/" + room + "/fresh", { undo: true }); toast(t("fresh.undone")); reload(); }
        catch (e) { fail(e); undo.disabled = false; }
      });
      el.appendChild(h("div", { class: "fresh-now" }, h("div", { class: "fresh-when" }, icon("check"), h("b", {}, t("fresh.now", { when }))),
        fs.note ? h("pre", { class: "fresh-note" }, fs.note) : h("p", { class: "hint" }, t("fresh.nonote")), undo));
    }
    const ask = autosize(h("textarea", { class: "input", rows: "3", maxlength: "2000", placeholder: t("fresh.ask.ph") }), 220);
    const askBtn = h("button", { class: "btn primary wide", type: "button" }, icon("chat"), t("fresh.ask", { name: b.name }));
    askBtn.addEventListener("click", async () => {
      const what = ask.value.trim();
      if (!what) { ask.focus(); return; }
      askBtn.disabled = true;
      try {
        await api("/api/rooms/" + room + "/send", { text: t("fresh.ask.text", { what }) });
        ask.value = ""; go("#/room/" + room);
      } catch (e) { fail(e); askBtn.disabled = false; }
    });
    const keep = autosize(h("textarea", { class: "input", rows: "2", maxlength: "4000", placeholder: t("fresh.keep.ph") }), 220);
    const self = h("button", { class: "btn wide", type: "button" }, icon("refresh"), t("fresh.self"));
    self.addEventListener("click", async () => {
      if (!(await confirmSheet(t("fresh.self"), h("p", {}, t("fresh.self.body", { name: b.name })), t("fresh.self")))) return;
      self.disabled = true;
      try { await api("/api/rooms/" + room + "/fresh", { bot: b.id, note: keep.value.trim() }); toast(t("fresh.done")); reload(); }
      catch (e) { fail(e); self.disabled = false; }
    });
    append(el, [h("div", { class: "fresh-part" }, h("h4", {}, t("fresh.ask.h", { name: b.name })), h("p", { class: "hint" }, t("fresh.ask.d")), ask, askBtn),
      h("div", { class: "fresh-part" }, h("h4", {}, t("fresh.self.h")), h("p", { class: "hint" }, t("fresh.self.d")), keep, self)]);
    return el;
  }

  function projectLine(p) {
    return h("a", { class: "pp-row", href: "#/project/" + p.id },
      h("div", { class: "pp-top" }, h("b", { class: "pp-name" }, p.name), p.running ? h("span", { class: "status st-running" }, h("span", { class: "pulse-dot" }), t("projects.running", { n: p.running })) : icon("right")),
      p.progress && p.progress.total ? stepsLine(p.progress) : null);
  }

  function pickProjectSheet(b) {
    const picker = projectPicker(b.projects || [], b.id);
    const btn = h("button", { class: "btn primary wide", type: "button" }, t("common.save"));
    const sh = sheet(t("nb.project"), [picker.el, btn]);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { const j = await api("/api/bots/" + b.id, { projects: picker.value() }); Object.assign(b, j.bot); sh.close(); toast(t("bot.saved")); render(); }
      catch (e) { fail(e); btn.disabled = false; }
    });
  }

  // what goes into the model, part by part (used by "What it saw" under a reply and by the inside screen)
  function contextView(v, b, compact) {
    const rep = v.report || {};
    const total = rep.total || 0, target = rep.target_tokens || 16000, max = rep.max_tokens || 32000;
    const parts = (v.sections || []).map((x) => ({ name: x.name, tokens: x.tokens, text: x.text }))
      .concat([{ name: "history", tokens: (v.history || []).reduce((a, x) => a + x.tokens, 0), text: null }]);
    const colors = { persona: "var(--brand)", rules: "var(--idle)", tools: "#8B5CF6", room: "var(--info)", context: "var(--info)", state: "#0EA5E9", fresh: "#F43F5E",
      memory: "var(--warn)", user: "#D97706", briefs: "#14B8A6", summary: "#64748B", history: "var(--good)" };
    const wrap = h("div", { class: "saw" });
    append(wrap, [
      h("div", { class: "saw-total" }, h("b", {}, num(total)), h("span", {}, t("saw.tokens", { target: num(target), max: num(max) }))),
      h("div", { class: "saw-bar" }, parts.map((x) => h("span", { style: { width: Math.max(0.6, (x.tokens / Math.max(max, total, 1)) * 100) + "%", background: colors[x.name] || "var(--muted)" }, title: x.name }))),
      h("div", { class: "saw-scale" }, h("span", { style: { left: Math.min(100, (target / max) * 100) + "%" } }, t("saw.target"))),
    ]);
    (v.sections || []).forEach((x) => wrap.appendChild(h("details", { class: "saw-sec" },
      h("summary", {}, h("i", { style: { background: colors[x.name] || "var(--muted)" } }), h("span", { class: "saw-name" }, t("saw.sec." + x.name)), h("span", { class: "saw-tok" }, num(x.tokens))),
      h("pre", {}, x.text))));
    wrap.appendChild(h("details", { class: "saw-sec" }, h("summary", {}, h("i", { style: { background: colors.history } }), h("span", { class: "saw-name" }, t("saw.sec.history") + " · " + (v.history || []).length), h("span", { class: "saw-tok" }, num(parts[parts.length - 1].tokens))),
      h("div", { class: "saw-hist" }, (v.history || []).map((x) => h("div", { class: "saw-msg " + x.role }, h("span", { class: "saw-role" }, x.role === "assistant" ? b.name : (x.text.startsWith("[Reference]") ? t("saw.other") : x.text.startsWith("[SnapLab") ? "SnapLab" : t("session.you"))), h("span", { class: "saw-text" }, x.text.slice(0, 600)))))));
    if (compact) wrap.appendChild(h("p", { class: "lead-text small-gap" }, t("inside.sees.d")));
    return wrap;
  }

  // the raw record: everything this bot may look back on
  function archiveSearch(b) {
    const input = h("input", { class: "input", type: "search", placeholder: t("archive.ph"), enterkeyhint: "search" });
    const btn = h("button", { class: "btn", type: "button" }, icon("search"), t("archive.search"));
    const out = h("div", { class: "arc-list" });
    const countEl = h("span", { class: "right pp-num" });
    const run = async () => {
      const q = input.value.trim();
      if (!q) return;
      out.innerHTML = ""; out.appendChild(h("div", { class: "saw-loading" }, h("span", { class: "spinner" })));
      try {
        const j = await api("/api/archive?q=" + encodeURIComponent(q) + "&bot=" + b.id);
        out.innerHTML = "";
        if (!j.results.length) { out.appendChild(h("p", { class: "lead-text" }, t("archive.none"))); return; }
        j.results.forEach((r) => out.appendChild(h("button", { class: "arc-row", type: "button", onclick: () => recordSheet(r.id) },
          h("div", { class: "arc-top" }, h("span", { class: "arc-kind k-" + r.kind }, t("archive.kind." + r.kind)), h("span", { class: "arc-where" }, r.title || r.where), h("span", { class: "arc-time" }, relTime(r.ts))),
          h("div", { class: "arc-snip" }, r.snippet))));
      } catch (e) { out.innerHTML = ""; fail(e); }
    };
    btn.addEventListener("click", run);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    const el = h("div", { class: "card form archive-card" }, h("div", { class: "card-h row" }, icon("archive"), h("span", {}, t("archive.title")), countEl),
      h("p", { class: "lead-text" }, t(b.orchestrator ? "archive.d.lead" : "archive.d", { name: b.name })),
      h("div", { class: "arc-search" }, input, btn), out);
    return { el, count: (n) => { countEl.textContent = t("archive.count", { n: S.lang === "ru" ? n : num(n) }); } };
  }

  async function recordSheet(id) {
    const body = h("div", { class: "record" }, h("div", { class: "saw-loading" }, h("span", { class: "spinner" })));
    const sh = sheet(t("archive.record"), body);
    try {
      const r = (await api("/api/archive/" + id)).record;
      body.innerHTML = "";
      const link = r.ref && r.ref.startsWith("#/") ? h("a", { class: "btn ghost small", href: r.ref, onclick: () => sh.close() }, icon("right"), t("archive.open"))
        : r.ref && /^https?:/.test(r.ref) ? h("a", { class: "btn ghost small", href: r.ref, target: "_blank", rel: "noopener noreferrer" }, icon("globe"), t("archive.open.web")) : null;
      append(body, [h("div", { class: "arc-top" }, h("span", { class: "arc-kind k-" + r.kind }, t("archive.kind." + r.kind)), h("span", { class: "arc-where" }, r.where), h("span", { class: "arc-time" }, new Date(r.ts).toLocaleString(loc()))),
        link, h("pre", { class: "record-text" }, r.text), r.cut ? h("p", { class: "lead-text" }, t("archive.cut")) : null]);
    } catch (e) { body.innerHTML = ""; body.appendChild(h("p", { class: "lead-text" }, e.message)); }
  }

  // name, look, role, model and instructions (what used to be the whole bot page)
  function profileSheet(b) {
    let color = b.color;
    const nameIn = h("input", { class: "input", value: b.name });
    const iniIn = h("input", { class: "input center", maxlength: "2", value: b.initials });
    let selectedFace = b.avatar || "";
    const face = faceField(() => ({ ...b, name: nameIn.value, initials: iniIn.value, color, avatar: selectedFace }), (value) => { selectedFace = value; });
    nameIn.addEventListener("input", face.refresh);
    iniIn.addEventListener("input", face.refresh);
    const roleIn = autosize(h("textarea", { class: "input", rows: "2", maxlength: "300", placeholder: t("bot.role.ph") }, b.role || ""), 120);
    const callIn = h("input", { class: "input", maxlength: "32", value: b.call_user || "" });
    const promptIn = autosize(h("textarea", { class: "input prompt-ta", rows: "8", maxlength: "8000", spellcheck: "false" }, b.system_prompt || ""), 420);
    const models = S.models || [];
    const defName = (S.routing || {}).chat || (S.routing || {}).default;
    const defModel = models.find((x) => x.name === defName);
    const modelSel = h("select", { class: "input" },
      h("option", { value: "" }, t("bot.model.default", { name: defModel ? defModel.label : (defName || "") })),
      models.map((x) => h("option", { value: x.name, selected: b.model === x.name }, x.label + (x.key_set ? "" : " · " + t("bot.model.nokey")))));
    const modelField = models.length > 1 ? field(t("bot.model"), modelSel)
      : field(t("bot.model"), h("div", { class: "kv-line" }, icon("brain"), h("span", {}, defModel ? defModel.label : "-")));
    const save = h("button", { class: "btn primary wide", type: "button" }, t("bot.save"));
    const sh = sheet(t("inside.profile"), [h("div", { class: "form" },
      face.el,
      h("div", { class: "row2" }, field(t("bot.name"), counted(nameIn, NAME_MAX)), field(t("bot.initials"), iniIn)),
      field(t("bot.color"), swatches(color, (c) => { color = c; face.refresh(); })),
      b.orchestrator ? null : field(t("bot.role"), roleIn),
      field(t("bot.call"), callIn),
      modelField,
      h("p", { class: "hint" }, t("providers.notice")),
      field(t("bot.prompt"), promptIn),
      h("p", { class: "lead-text" }, icon("shield"), " ", t("bot.prompt.note"))), save]);
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const j = await api("/api/bots/" + b.id, { name: nameIn.value, initials: iniIn.value, color, avatar: selectedFace, role: roleIn.value, call_user: callIn.value, system_prompt: promptIn.value,
          ...(models.length > 1 ? { model: modelSel.value } : {}) });
        Object.assign(b, j.bot);
        S.rooms.filter((r) => r.kind === "dm" && r.bots[0] === b.id).forEach((r) => { r.name = j.bot.name; });
        toast(t("bot.saved")); sh.close(); render();
      } catch (e) { fail(e); save.disabled = false; }
    });
  }

  function memoryCard(b) {
    let text = "", limit = 4000;
    const list = h("div", { class: "mem-list" });
    const count = h("span", { class: "count-hint" });
    const addIn = h("input", { class: "input", maxlength: "400", placeholder: t("bot.memory.add.ph"), enterkeyhint: "done" });
    const addBtn = h("button", { class: "btn ghost", type: "button" }, icon("plus"), t("bot.memory.add"));
    const raw = autosize(h("textarea", { class: "input mono", rows: "4", maxlength: "20000", spellcheck: "false" }), 320);
    const rawSave = h("button", { class: "btn ghost", type: "button" }, icon("memory"), t("bot.memory.save"));
    const today = () => new Date().toISOString().slice(0, 10);
    const save = async (next) => {
      try { const j = await api("/api/bots/" + b.id + "/memory", { text: next }); text = j.memory || ""; paint(); toast(t("bot.saved")); }
      catch (e) { fail(e); }
    };
    const paint = () => {
      list.innerHTML = "";
      const ls = text.split("\n").filter((l) => l.trim());
      if (!ls.length) list.appendChild(h("p", { class: "empty small" }, t("bot.memory.empty")));
      ls.forEach((ln, i) => {
        const m = ln.match(/^\s*[-*]\s*(?:\[(\d{4}-\d{2}-\d{2})[^\]]*\]\s*)?(.*)$/);
        list.appendChild(h("div", { class: "mem-row" },
          h("div", { class: "mem-text" }, h("span", {}, m ? m[2] : ln), m && m[1] ? h("span", { class: "mem-date" }, m[1]) : null),
          h("button", { class: "icon-btn", type: "button", "aria-label": t("bot.memory.forget"), title: t("bot.memory.forget"), onclick: () => {
            const rest = ls.filter((_, j) => j !== i).join("\n"); save(rest ? rest + "\n" : "");
          } }, icon("x"))));
      });
      count.textContent = t("bot.memory.count", { n: chars(text), max: limit });
      raw.value = text; raw.dispatchEvent(new Event("input"));
    };
    const add = () => { const v = addIn.value.trim(); if (!v) return; addIn.value = ""; save((text.trim() ? text.trimEnd() + "\n" : "") + "- [" + today() + "] " + v + "\n"); };
    addBtn.addEventListener("click", add);
    addIn.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    rawSave.addEventListener("click", () => save(raw.value));
    const tidyBtn = h("button", { class: "btn ghost small", type: "button", onclick: async () => {
      tidyBtn.disabled = true;
      try { await api("/api/bots/" + b.id + "/memory/tidy", {}); toast(t("bot.memory.tidy.started")); } catch (e) { fail(e); }
      setTimeout(() => (tidyBtn.disabled = false), 4000);
    } }, icon("spark"), t("bot.memory.tidy"));
    const clearBtn = h("button", { class: "btn ghost small danger mem-clear", type: "button", onclick: async () => {
      if (!text.trim()) return;
      if (!(await confirmSheet(t("bot.memory.clear"), h("p", {}, t("bot.memory.clear.body", { name: b.name })), t("bot.memory.clear")))) return;
      save("");
    } }, icon("trash"), t("bot.memory.clear"));
    const el = h("div", { class: "card form memory" },
      h("h3", { class: "card-h" }, icon("memory"), t("bot.memory.title", { name: b.name }), count),
      h("p", { class: "hint" }, t("bot.memory.hint")), list,
      h("div", { class: "mem-add" }, addIn, addBtn),
      h("div", { class: "mem-foot" }, h("details", { class: "mem-raw" }, h("summary", {}, t("bot.memory.raw")), raw, rawSave), tidyBtn, clearBtn));
    const load = () => api("/api/bots/" + b.id + "/memory").then((j) => { text = j.memory || ""; limit = j.limit || limit; paint(); }).catch(() => {});
    S.memoryCard = { bot: b.id, load };
    return { el, load };
  }

  // ------------------------------------------------------------------ settings

  function renderSettings() {
    const n = S.settings.notify || {};
    const langSeg = h("div", { class: "seg" }, window.snapLangs().map(({ code, name }) =>
      h("button", { type: "button", class: S.lang === code ? "on" : "", onclick: async () => {
        try { const j = await api("/api/settings", { lang: code }); S.settings = j.settings; S.lang = code; document.documentElement.lang = code; window.snapRememberLang(code); await load(); render(); } catch (e) { fail(e); }
      } }, name)));
    const theme = S.settings.theme || "system";
    const iconSeg = h("div", { class: "seg" }, ["line", "soft3d"].map((style) => h("button", {
      type: "button", class: (S.settings.icon_style || "line") === style ? "on" : "",
      "aria-pressed": String((S.settings.icon_style || "line") === style), onclick: async () => {
        try { const j = await api("/api/settings", { icon_style: style }); S.settings = j.settings; applyTheme(); render(); } catch (e) { fail(e); }
      } }, t("settings.icons." + style))));
    const themeSeg = h("div", { class: "seg three" }, [["system", "phone"], ["light", "sun"], ["dark", "moon"]].map(([code, ic]) =>
      h("button", { type: "button", class: theme === code ? "on" : "", "aria-pressed": theme === code ? "true" : "false", onclick: async () => {
        try { const j = await api("/api/settings", { theme: code }); S.settings = j.settings; applyTheme(); render(); } catch (e) { fail(e); }
      } }, icon(ic), t("settings.theme." + code))));
    const pushState = h("div", { class: "push-state" });
    const toggles = h("div", { class: "toggles" }, ["needs_you", "review", "failed"].map((k) => {
      const cb = h("input", { type: "checkbox", checked: !!n[k], onchange: async () => { try { const j = await api("/api/settings", { notify: { [k]: cb.checked } }); S.settings = j.settings; } catch (e) { fail(e); cb.checked = !cb.checked; } } });
      return h("label", { class: "toggle-row" }, h("span", {}, t("settings.kind." + k)), h("span", { class: "switch" }, cb, h("span", { class: "knob" })));
    }));
    const needOn = h("p", { class: "hint need-on" }, icon("bell"), h("span", {}, t("settings.notify.need_on")));
    const pushOn = (on) => { toggles.classList.toggle("off", !on); needOn.classList.toggle("hide", on); $$("input", toggles).forEach((x) => (x.disabled = !on)); };
    pushOn(false);
    const you = autosize(h("textarea", { class: "input", rows: "3", maxlength: "1400", placeholder: t("settings.you.ph") }), 260);
    const youCount = h("span", { class: "count-hint" });
    const youFit = () => (youCount.textContent = t("bot.memory.count", { n: chars(you.value), max: 1400 }));
    you.addEventListener("input", youFit);
    const youSave = h("button", { class: "btn ghost", type: "button", onclick: async () => {
      try { const j = await api("/api/profile", { text: you.value }); you.value = j.text; youFit(); toast(t("settings.you.saved")); } catch (e) { fail(e); }
    } }, icon("check"), t("common.save"));
    api("/api/profile").then((j) => { you.value = j.text || ""; you.dispatchEvent(new Event("input")); youFit(); }).catch(() => {});
    const body = h("div", { class: "page" },
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("globe"), t("settings.lang")), langSeg),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("sun"), t("settings.theme")), themeSeg),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("spark"), t("settings.icons")), iconSeg),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("users"), t("settings.you"), youCount), h("p", { class: "hint" }, t("settings.you.note")), you, youSave),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("bell"), t("settings.notify")), pushState, needOn, toggles, h("p", { class: "hint" }, t("settings.notify.three"))),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("phone"), t("phone.title")), h("p", { class: "hint" }, t("phone.settings.body")),
        h("a", { class: "btn ghost", href: "#/phone" }, t("phone.open"), icon("right"))),
      h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("sail"), t("settings.install")), h("p", { class: "hint" }, t("settings.install.body")), installBtn()),
      modelsCard(),
      mcpCard(),
      backgroundCard(),
      advancedCard(),
      h("div", { class: "card form privacy" }, icon("shield"), h("p", {}, t("settings.privacy"))),
      h("div", { class: "card form about" }, h("h3", { class: "card-h" }, t("settings.about")),
        h("div", { class: "kv" }, h("span", {}, t("settings.version")), h("code", {}, "SnapLab " + S.version)),
        h("div", { class: "kv" }, h("span", {}, t("settings.device")), h("code", {}, (S.device.label || "") + " · " + (S.device.id || "")))));
    screen({ back: S.settingsFrom || "#/chats", title: t("settings.title"), tab: "settings" }, body);
    paintPushState(pushState, pushOn);
  }

  // ------------------------------------------------------------------ MCP servers (Settings)

  function mcpCard() {
    const list = h("div", { class: "mcp-list" }, h("p", { class: "hint" }, h("span", { class: "spinner" })));
    let paint;
    const run = async (body) => { try { const j = await api("/api/mcp", body); paint(j.servers); } catch (e) { fail(e); } };
    const row = (sv) => h("div", { class: "mcp-row" + (sv.enabled ? "" : " off") },
      h("div", { class: "mcp-main" },
        h("b", {}, sv.label || sv.name),
        h("code", { class: "mcp-cmd" }, sv.kind === "url" ? sv.url : sv.command),
        sv.error ? h("span", { class: "mcp-err" }, icon("alert"), h("span", {}, sv.error))
          : h("span", { class: "tag" + (sv.tools.length ? " good" : "") }, t("mcp.tools", { n: sv.tools.length })),
        sv.tools.length ? h("details", { class: "mcp-tools" }, h("summary", {}, t("mcp.showtools")), h("p", { class: "muted" }, sv.tools.join(", "))) : null),
      sv.source === "settings" ? h("div", { class: "mcp-actions" },
        h("label", { class: "toggle-row" }, h("span", {}, t("mcp.chat")), h("span", { class: "switch" },
          h("input", { type: "checkbox", checked: sv.chat, onchange: (e) => run({ action: "update", name: sv.name, chat: e.target.checked }) }), h("span", { class: "knob" }))),
        h("div", { class: "qcard-actions" },
          h("button", { class: "btn ghost", type: "button", onclick: () => run({ action: "test", name: sv.name }) }, icon("refresh"), t("mcp.test")),
          h("button", { class: "btn ghost", type: "button", onclick: async () => {
            if (await confirmSheet(t("mcp.remove.title", { name: sv.label || sv.name }), t("mcp.remove.body"), t("mcp.remove"))) run({ action: "remove", name: sv.name });
          } }, icon("trash"), t("mcp.remove"))))
        : h("p", { class: "hint" }, t("mcp.fromconfig")));
    paint = (servers) => list.replaceChildren(...(servers.length ? servers.map(row) : [h("p", { class: "hint" }, t("mcp.none"))]));
    api("/api/mcp").then((j) => paint(j.servers)).catch(() => paint([]));
    return h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("layers"), t("mcp.title")),
      h("p", { class: "hint" }, t("mcp.body")), list,
      h("button", { class: "btn ghost", type: "button", onclick: () => mcpAddSheet(paint) }, icon("plus"), t("mcp.add")));
  }

  function mcpAddSheet(paint) {
    let mode = "json", sh = null;
    const seg = h("div", { class: "seg three" }, ["json", "command", "url"].map((m) => h("button", { type: "button", class: m === mode ? "on" : "",
      onclick: (e) => { mode = m; $$("button", seg).forEach((b) => b.classList.toggle("on", b === e.currentTarget)); show(); } }, t("mcp.mode." + m))));
    const name = h("input", { class: "input", maxlength: "16", placeholder: t("mcp.name.ph"), autocomplete: "off" });
    const json = h("textarea", { class: "input mono", rows: "8", spellcheck: "false",
      placeholder: '{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\\\Users\\\\you\\\\Documents"]\n    }\n  }\n}' });
    const command = h("input", { class: "input mono", autocomplete: "off", spellcheck: "false",
      placeholder: "npx -y @modelcontextprotocol/server-filesystem C:\\Users\\you\\Documents" });
    const env = h("textarea", { class: "input mono", rows: "3", spellcheck: "false", placeholder: "API_KEY=..." });
    const url = h("input", { class: "input mono", type: "url", placeholder: "https://example.com/mcp", autocomplete: "off" });
    const token = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: t("mcp.token.ph") });
    const chat = h("input", { type: "checkbox", checked: true });
    const status = h("p", { class: "hint mcp-status", role: "status" });
    const fields = {
      json: [field(t("mcp.json"), json), h("p", { class: "hint" }, t("mcp.json.hint"))],
      command: [field(t("mcp.name"), name), field(t("mcp.command"), command), field(t("mcp.env"), env), h("p", { class: "hint" }, t("mcp.env.hint"))],
      url: [field(t("mcp.name"), name), field(t("mcp.url"), url), field(t("mcp.token"), token)],
    };
    const box = h("div", { class: "mcp-fields" });
    const show = () => { box.replaceChildren(...fields[mode]); status.textContent = ""; };
    const go = h("button", { class: "btn primary", type: "button" }, icon("check"), t("mcp.addgo"));
    go.addEventListener("click", async () => {
      go.disabled = true;
      status.replaceChildren(h("span", { class: "spinner" }), " " + t("mcp.testing"));
      try {
        const body = mode === "json" ? { action: "import", json: json.value, chat: chat.checked }
          : { action: "add", name: name.value, chat: chat.checked,
              ...(mode === "command" ? { command: command.value, env: env.value } : { url: url.value, token: token.value }) };
        const j = await api("/api/mcp", body);
        env.value = ""; token.value = "";
        paint(j.servers);
        const bad = (j.results || []).filter((r) => !r.ok);
        if (bad.length) { status.textContent = bad.map((r) => r.name + ": " + r.error).join("\n"); go.disabled = false; return; }
        toast(t("mcp.added"));
        sh.close();
      } catch (e) { status.textContent = e.message || t("err.generic"); go.disabled = false; }
    });
    show();
    sh = sheet(t("mcp.add"), [seg, box,
      h("label", { class: "toggle-row" }, h("span", {}, t("mcp.chat.long")), h("span", { class: "switch" }, chat, h("span", { class: "knob" }))),
      h("p", { class: "hint" }, t("mcp.where")), status, h("div", { class: "qcard-actions" }, go)]);
  }

  function backgroundCard() {
    const bg = S.background;
    if (!bg) return null;
    const s = bg.summary || {}, m = bg.memory_tidy || {};
    const row = (ic, text, on) => h("div", { class: "bg-row" + (on ? "" : " off") }, icon(ic), h("span", {}, text), on ? null : h("span", { class: "tag" }, t("settings.background.off")));
    return h("div", { class: "card form" }, h("h3", { class: "card-h" }, icon("clock"), t("settings.background")),
      row("page", t("settings.background.summary", { n: s.every_messages }), s.enabled),
      row("memory", t("settings.background.tidy", { days: m.every_days, hour: m.hour }), m.enabled),
      bg.last_tidy ? h("p", { class: "hint" }, t("settings.background.last", { when: relTime(bg.last_tidy) })) : null,
      h("p", { class: "hint" }, t("settings.background.note")));
  }
  function modelDescription(ref, kind="chat") {
    const m=(S.models || []).find(x=>x.name===(ref || (S.routing || {})[kind]));
    // Snapscribe shows only its product name ("Snapscribe 1"), so the model behind it can change.
    return m ? (m.kind === "snapscribe" ? m.label : m.model + " · " + (m.provider_label || m.provider)) : (ref || "");
  }

  function modelsCard() {
    const models = S.models || [];
    if (!models.length) return null;
    const run = async (body, done) => {
      try { await api("/api/models", body); await load(); render(); if (done) toast(done); } catch (e) { fail(e); }
    };
    const actions = (m) => {
      const uses = m.default_for || [];
      const out = [];
      if (models.length > 1 && !uses.includes("chat")) out.push(h("button", { class: "btn ghost small", type: "button",
        onclick: () => run({ action: "default", name: m.name }, t("settings.models.defaulted", { name: m.label })) }, icon("check"), t("settings.models.makedefault")));
      if (!uses.length) out.push(h("button", { class: "btn ghost small", type: "button", onclick: async () => {
        if (await confirmSheet(t("settings.models.remove.title", { name: m.label }), t("settings.models.remove.body"), t("settings.models.remove"))) run({ action: "remove", name: m.name });
      } }, icon("trash"), t("settings.models.remove")));
      return out.length ? h("div", { class: "model-actions" }, out) : null;
    };
    return h("div", { class: "card form models" }, h("h3", { class: "card-h" }, icon("brain"), t("settings.models")),
      h("div", { class: "model-rows" }, models.map((m) => h("div", { class: "model-row" },
        h("div", { class: "model-main" }, h("b", {}, m.label), h("span", { class: "muted" }, (m.kind === "snapscribe" ? m.provider_label : m.provider + " · " + m.model) + " · " + t("settings.models.ctx", { n: m.context_limit % 1024 === 0 ? (m.context_limit / 1024) + "K" : num(m.context_limit) }))),
        h("div", { class: "model-tags" },
          (m.default_for || []).filter((k) => k !== "default").map((k) => h("span", { class: "tag" }, t("settings.models.for." + k))),
          h("span", { class: "tag " + (m.key_set ? "good" : "bad") }, t(m.key_set ? "settings.models.key" : "settings.models.nokey"))),
        actions(m)))),
      h("p", { class: "hint" }, t("settings.models.hint")));
  }

  // ------------------------------------------------------------------ Advanced (Settings): other providers, AI apps

  function copyButton(text) {
    return h("button", { class: "btn ghost small", type: "button", onclick: async (e) => {
      const code = e.currentTarget.previousElementSibling;
      try { await navigator.clipboard.writeText(text); toast(t("common.copied")); }
      catch (err) { const r = document.createRange(); r.selectNodeContents(code); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
    } }, t("common.copy"));
  }

  function advancedCard() {
    const list = h("div", { class: "cli-list" }, h("p", { class: "hint" }, h("span", { class: "spinner" })));
    let paint, viaPhone = false;
    const refresh = async (fresh) => { try { const j = await api("/api/cli" + (fresh ? "?fresh=1" : "")); viaPhone = j.via_phone; paint(j.apps); } catch (e) { fail(e); } };
    const step = (a, which, command) => {
      if (a.can_open && !viaPhone) return h("div", { class: "cli-step" }, h("button", { class: "btn ghost", type: "button", onclick: async () => {
        try { await api("/api/cli", { action: "open", app: a.id, step: which }); toast(t("cli.opened")); } catch (e) { fail(e); }
      } }, icon("terminal"), t("cli." + which)), h("p", { class: "hint" }, t("cli.window")));
      return h("div", { class: "cli-step" }, h("p", { class: "hint" }, t(viaPhone ? "cli.onpc" : "cli.terminal")),
        h("div", { class: "cli-cmd" }, h("code", {}, command), copyButton(command)));
    };
    const row = (a) => {
      const state = !a.installed ? ["bad", t("cli.notinstalled")] : !a.signed_in ? ["warn", t("cli.signedout", { v: a.version || "?" })]
        : ["good", t("cli.signedin", { plan: a.plan || "", v: a.version || "?" })];
      const body = [];
      if (!a.installed) body.push(step(a, "install", a.install));
      else if (!a.signed_in) body.push(step(a, "signin", a.signin));
      else {
        const model = h("input", { class: "input mono", list: "cli-models-" + a.id, maxlength: "80", autocomplete: "off", spellcheck: "false",
          placeholder: a.models.length ? a.models[0] : t("cli.model.default") });
        const status = h("p", { class: "hint", role: "status" });
        const add = h("button", { class: "btn primary", type: "button", onclick: async () => {
          add.disabled = true; status.textContent = t("cli.testing");
          try { const j = await api("/api/cli", { action: "add", app: a.id, model: model.value.trim() }); await load(); render(); toast(t("cli.added", { name: (j.models.find((m) => m.name === j.added) || {}).label || "" })); }
          catch (e) { status.textContent = ""; add.disabled = false; fail(e); }
        } }, icon("plus"), t("cli.add"));
        body.push(field(t("cli.model"), model), h("datalist", { id: "cli-models-" + a.id }, a.models.map((m) => h("option", { value: m }))),
          h("p", { class: "hint" }, t(a.models.length ? "cli.model.hint" : "cli.model.hint.free")), add, status);
      }
      if (a.added.length) body.push(h("p", { class: "hint" }, t("cli.inuse", { names: a.added.map((m) => m.label).join(", ") })));
      return h("div", { class: "cli-row" }, h("div", { class: "cli-head" }, h("b", {}, a.label + " · " + a.program),
        h("span", { class: "tag " + state[0] }, state[1])), ...body);
    };
    paint = (apps) => list.replaceChildren(...apps.map(row));
    refresh(false);
    return h("details", { class: "card form advanced" },
      h("summary", { class: "card-h" }, icon("gear"), t("adv.title")),
      h("p", { class: "hint" }, t("adv.body")),
      h("h4", {}, t("adv.providers")),
      h("p", { class: "hint" }, t("providers.notice")),
      h("a", { class: "btn ghost", href: "/setup#add" }, icon("plus"), t("providers.add")),
      h("h4", {}, t("cli.title")),
      h("p", { class: "hint" }, t("cli.notice")),
      list,
      h("button", { class: "btn ghost", type: "button", onclick: () => refresh(true) }, icon("refresh"), t("cli.check")),
      h("p", { class: "hint" }, t("cli.gemini")));
  }

  let installEvt = null;
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; });
  function installBtn() {
    if (!installEvt) return null;
    return h("button", { class: "btn primary", type: "button", onclick: async () => { installEvt.prompt(); await installEvt.userChoice; installEvt = null; render(); } }, icon("plus"), t("settings.install.btn"));
  }

  async function paintPushState(el, onState) {
    const report = (on) => { if (onState) onState(on); };
    el.innerHTML = "";
    const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    if (!supported) { el.appendChild(h("p", { class: "hint" }, t("settings.notify.unsupported"))); report(false); return; }
    if (Notification.permission === "denied") { el.appendChild(h("p", { class: "hint bad" }, t("settings.notify.denied"))); report(false); return; }
    let sub = null;
    try {
      const reg = await Promise.race([navigator.serviceWorker.ready, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 3000))]);
      sub = await reg.pushManager.getSubscription();
    } catch (e) { sub = null; }
    report(!!(sub && Notification.permission === "granted"));
    if (sub && Notification.permission === "granted") {
      append(el, [h("p", { class: "hint good" }, icon("check"), t("settings.notify.on")),
        h("button", { class: "btn ghost", type: "button", onclick: async () => { try { await api("/api/push/subscribe", { subscription: sub.toJSON() }); await api("/api/push/test", {}); toast(t("toast.notify.ok")); } catch (e) { fail(e); } } }, icon("bell"), t("settings.notify.test"))]);
    } else {
      append(el, [h("p", { class: "hint" }, t("settings.notify.off")),
        h("button", { class: "btn primary", type: "button", onclick: async () => {
          try {
            const perm = await Notification.requestPermission();
            if (perm !== "granted") { paintPushState(el, onState); return; }
            const reg = await navigator.serviceWorker.ready;
            const s2 = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(S.push.public_key) });
            await api("/api/push/subscribe", { subscription: s2.toJSON() });
            paintPushState(el, onState);
          } catch (e) { fail(e); }
        } }, icon("bell"), t("settings.notify.enable"))]);
    }
  }
  function b64ToBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  // ------------------------------------------------------------------ live clocks

  setInterval(() => {
    $$("[data-since]").forEach((el) => {
      const base = Number(el.dataset.base || 0);
      el.textContent = dur(base + secsSince(el.dataset.since));
    });
    if (roomEls) $$(".msg.streaming .think:not(.settled) .think-label", roomEls.log).forEach((el) => {
      const mid = el.closest(".msg").dataset.mid;
      const m = (S.live[roomEls.id] || {})[mid];
      if (m && !m.text) el.textContent = m._wait ? t("msg.queued", { n: m._wait }) : t("msg.thinking") + " · " + dur((Date.now() - m._t0) / 1000);
    });
  }, 1000);

  // ------------------------------------------------------------------ start

  function notPaired() {
    const app = $("#app");
    app.className = "screen-gate";
    app.innerHTML = "";
    const lang = window.snapPickLang(navigator.language);
    S.lang = lang;
    app.appendChild(h("main", { class: "gate" }, h("div", { class: "gate-card" }, h("img", { class: "gate-logo", src: "/logo.png", alt: "SnapLab" }),
      h("h1", {}, t("err.notpaired")), h("p", {}, t("err.notpaired.body")), h("pre", {}, "python snaplab.py pair --label phone"))));
  }

  async function start() {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { updateViaCache: "none" }).catch(() => {});
    S.route = parseRoute();
    try { await load(); }
    catch (e) {
      if (e.status === 401) return notPaired();
      $("#app").innerHTML = "";
      $("#app").appendChild(h("main", { class: "gate" }, h("div", { class: "gate-card" }, h("img", { class: "gate-logo", src: "/logo.png", alt: "" }), h("h1", {}, t("err.generic")), h("p", {}, e.message))));
      return;
    }
    render();
    connect();
    // Update the calendar at midnight and on returning from a suspended installed app.
    const refreshHome = async () => {
      if (document.hidden || S.route.name !== "home" || activeSheets.length) return;
      try {
        const [tasks, usage] = await Promise.all([api("/api/tasks"), api("/api/usage")]);
        S.tasks = tasks.tasks; S.usage = usage.usage;
        if (S.route.name === "home" && !activeSheets.length) renderHome();
      } catch (e) { /* The connection band already reports offline status. */ }
    };
    setInterval(refreshHome, 60000);
    document.addEventListener("visibilitychange", refreshHome);
  }
  start();
})();

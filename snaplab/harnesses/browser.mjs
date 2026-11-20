// Browser for SnapCode: open pages in Chromium, act on them like a user, see the result, read what DevTools reads.
//
// Live browser (stays open for the whole turn, so page and game state carry over between commands):
//   node .snaplab/browser.mjs start [--devtools]        a real Chrome window on the job's desktop (the default when the
//                                                       job has one); the user can watch it and take over in SnapLab
//   node .snaplab/browser.mjs start --headless [--width 1280 --height 800 --mobile]   no window (also --mobile or a size)
//   (start keeps a running browser; --headful or --headless replaces it with the other kind)
//   open/do without start also start the live browser when the job has a desktop.
// While the user has taken over the browser in SnapLab (.snaplab/browser-control.json), open and do wait until they
// hand it back, then report it: look at the page again before acting.
// SnapLab's live view: `--live` (kept by start) relays the DevTools protocol on .snaplab/live.sock; `host` runs the
// browser alone, between turns, for the user.
//   node .snaplab/browser.mjs open URL_OR_FILE [STEP ...]   go to a page, then run steps
//   node .snaplab/browser.mjs do STEP [STEP ...]            run steps on the page that is open now
//   node .snaplab/browser.mjs status | stop
// Every open/do ends with a screenshot of the page in .snaplab/browser.png (look at it, decide the next step);
// --no-shot skips it. Without start, `open` (or just URL_OR_FILE) runs in a browser opened for that one command.
// The profile in .snaplab/browser-profile is kept, so logins, saves and localStorage survive either way.
// The headful window is the same browser that `python3 .snaplab/desktop.py` clicks and types into with the real
// mouse and keyboard (F12 opens DevTools there); x,y in steps below are page pixels (browser.png), not screen pixels.
//
// URL_OR_FILE: https://..., http://localhost:PORT/... or a workspace file such as index.html?level=2 (served over
// local http, so fetch and ES modules work). Steps run in order:
//   click:#id | click:text=Start | click:400,300     real mouse events (works for canvas games too)
//   dblclick:... | rightclick:... | hover:... | drag:100,200,300,200 | mousedown:x,y | mouseup:x,y
//   wheel:x,y,dy[,dx]        mouse wheel at a point (zoom or scroll inside a canvas or a list)
//   type:#name=hello         click the element, then type the text as key presses      type:=hello  focused element
//   press:Enter | press:ArrowLeft | press:Space | press:a | press:Control+s | press:F5
//   keydown:Shift | keyup:Shift   hold:ArrowRight=1.5   (hold a key for seconds, for games)
//   wait:2 | wait:#selector | wait:text=Game over       seconds, or until it appears (up to --timeout)
//   scroll:down | scroll:up | scroll:#selector
//   eval:JS                  result printed (e.g. eval:window.game.score)
//   console[:N]              the page's console messages (all levels) and errors since it loaded, last N (50)
//   network[:part-of-url]    requests the page made: url, type, status, size, time
//   dom[:selector]           HTML of the element (default body)
//   elements                 visible buttons, links, inputs, canvases with their centre x,y (and screen x,y)
//   a11y                     the accessibility tree: roles and names, as a screen reader reads the page
//   storage                  localStorage, sessionStorage and cookies
//   tabs | tab:N             list open tabs / act on tab N from now on
//   shot:name.png            screenshot into the workspace
//   text                     visible text of the page (first 4000 characters)
//   url:https://...          go to another page          reload                reload the page (after editing code)
// Prints one JSON object: each step's result, console errors, page errors and failed requests.
import { browserPipe } from "./capture.mjs";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

// [windowsVirtualKeyCode, text, code]
const KEYS = { Enter: [13, "\r"], Tab: [9, "\t"], Escape: [27], Backspace: [8], Delete: [46], Space: [32, " ", "Space"],
  ArrowLeft: [37], ArrowUp: [38], ArrowRight: [39], ArrowDown: [40], Home: [36], End: [35], PageUp: [33], PageDown: [34],
  Insert: [45], Shift: [16, "", "ShiftLeft"], Control: [17, "", "ControlLeft"], Alt: [18, "", "AltLeft"],
  Meta: [91, "", "MetaLeft"], ContextMenu: [93] };
for (let i = 1; i <= 12; i++) KEYS["F" + i] = [111 + i];
const PUNCT = { " ": [32, "Space"], ".": [190, "Period"], ",": [188, "Comma"], "/": [191, "Slash"], ";": [186, "Semicolon"],
  "'": [222, "Quote"], "[": [219, "BracketLeft"], "]": [221, "BracketRight"], "\\": [220, "Backslash"], "-": [189, "Minus"],
  "=": [187, "Equal"], "`": [192, "Backquote"] };
const ALIASES = { ctrl: "Control", control: "Control", alt: "Alt", option: "Alt", shift: "Shift", meta: "Meta", cmd: "Meta",
  command: "Meta", win: "Meta", super: "Meta", enter: "Enter", return: "Enter", esc: "Escape", escape: "Escape",
  space: "Space", spacebar: "Space", left: "ArrowLeft", right: "ArrowRight", up: "ArrowUp", down: "ArrowDown",
  arrowleft: "ArrowLeft", arrowright: "ArrowRight", arrowup: "ArrowUp", arrowdown: "ArrowDown", del: "Delete",
  delete: "Delete", backspace: "Backspace", tab: "Tab", pageup: "PageUp", pagedown: "PageDown", home: "Home", end: "End",
  insert: "Insert", plus: "+", minus: "-" };
const MODS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };
const TYPES = { ".html": "text/html", ".htm": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
  ".css": "text/css", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml", ".wasm": "application/wasm", ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg", ".wav": "audio/wav", ".mp4": "video/mp4", ".woff2": "font/woff2", ".woff": "font/woff",
  ".ttf": "font/ttf", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".hdr": "application/octet-stream",
  ".txt": "text/plain", ".xml": "application/xml", ".atlas": "text/plain" };

function browserPath() {
  const found = [];
  if (process.platform !== "win32") {
    for (const dir of (process.env.PATH || "").split(":"))
      for (const name of ["chromium", "chromium-browser", "google-chrome"]) found.push(path.join(dir, name));
  } else {
    for (const base of [process.env["PROGRAMFILES"], process.env["PROGRAMFILES(X86)"], process.env.LOCALAPPDATA])
      if (base) for (const name of ["Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"])
        found.push(path.join(base, name));
  }
  return found.find((p) => { try { return fs.statSync(p).isFile(); } catch { return false; } });
}

function inside(root, rel) {
  const full = path.resolve(root, rel);
  if (full !== root && !full.startsWith(root + path.sep)) throw new Error("path is outside the workspace: " + rel);
  return full;
}

// Serve the workspace on 127.0.0.1 only, read-only, without hidden folders.
function serve(root) {
  const server = http.createServer((req, res) => {
    try {
      const rel = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
      if (rel.split("/").some((p) => p.startsWith("."))) { res.writeHead(404); return res.end(); }
      let file = inside(root, rel);
      if (fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
      const body = fs.readFileSync(file);
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file).toLowerCase()] || "application/octet-stream",
        "Cache-Control": "no-store" });
      res.end(body);
    } catch { res.writeHead(404); res.end(); }
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok(server)));
}

function screenSize() {
  const m = /^(\d{3,4})x(\d{3,4})$/.exec(process.env.SNAPLAB_SCREEN || "");
  return m ? { width: Number(m[1]), height: Number(m[2]) } : { width: 1024, height: 768 };
}

function parseArgs(argv) {
  const opts = { width: 1280, height: 800, timeout: 120, mobile: false, fresh: false, shot: true, headful: false,
    devtools: false, sized: false, kind: false };
  const steps = [];
  let target = null, command = "open";
  if (["start", "stop", "status", "open", "do", "host"].includes(argv[0])) command = argv.shift();
  if (command === "do") target = "";
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--mobile") opts.mobile = true;
    else if (a === "--fresh") opts.fresh = true;
    else if (a === "--no-shot") opts.shot = false;
    else if (a === "--headful" || a === "--window") opts.headful = opts.kind = true;
    else if (a === "--headless") opts.headless = opts.kind = true;
    else if (a === "--devtools") opts.devtools = opts.headful = opts.kind = true;
    else if (["--width", "--height", "--timeout"].includes(a)) { opts[a.slice(2)] = Number(argv[++i]); if (a !== "--timeout") opts.sized = true; }
    else if (target === null) target = a;
    else steps.push(a);
  }
  if (target === null && command === "open") throw new Error("usage: node .snaplab/browser.mjs [start|open|do|stop] ...  (see the top of this file)");
  if (opts.mobile && opts.width === 1280) { opts.width = 390; opts.height = 844; }
  // A window on the job's screen unless asked otherwise: the user can watch it (the screen stayed black before,
  // because the default was a browser without a window). --mobile or a size still means page emulation, headless.
  if (!opts.headful && !opts.headless && process.env.DISPLAY && !opts.mobile && !opts.sized) opts.headful = true;
  if (opts.headful && !opts.sized) Object.assign(opts, screenSize());
  if (!(opts.width >= 200 && opts.width <= 3840 && opts.height >= 200 && opts.height <= 3000)) throw new Error("--width/--height out of range");
  if (!(opts.timeout >= 5 && opts.timeout <= 1800)) throw new Error("--timeout must be 5-1800 seconds");
  return { command, target, steps, opts };
}

// A CDP connection over the live browser's local WebSocket, with the same shape as browserPipe().
async function socketPipe(url, commandMs) {
  const ws = new WebSocket(url);
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = () => fail(new Error("cannot reach the live browser; run start again")); });
  let next = 0;
  const pending = new Map(), listeners = new Set();
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data), p = pending.get(msg.id);
    if (p) {
      clearTimeout(p.timer); pending.delete(msg.id);
      msg.error ? p.reject(new Error("browser command " + p.method + " failed: " + (msg.error.message || "unknown"))) : p.resolve(msg.result);
    } else for (const l of listeners) l(msg);
  };
  ws.onclose = () => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("the live browser closed")); } pending.clear(); };
  const send = (method, params = {}, sessionId, ms = commandMs) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("browser command " + method + " took longer than " + Math.round(ms / 1000) + " s")); }, ms);
    pending.set(id, { resolve, reject, timer, method });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  return { send, listeners, close: async () => ws.close() };   // closing the connection leaves the browser open
}

const LIVE = (root) => path.join(root, ".snaplab", "browser-live.json");
const CONTROL = (root) => path.join(root, ".snaplab", "browser-control.json");
const SOCKET = (root) => path.join(root, ".snaplab", "live.sock");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Who uses the browser now: "user" after Take over in SnapLab, otherwise the agent.
function controlHolder(root) {
  try { return JSON.parse(fs.readFileSync(CONTROL(root), "utf-8")).holder === "user" ? "user" : "agent"; } catch { return "agent"; }
}

// Actions wait while the user has the browser (up to 15 minutes), then say so.
async function waitForUser(root) {
  if (controlHolder(root) !== "user") return null;
  const t0 = Date.now();
  while (controlHolder(root) === "user") {
    if (Date.now() - t0 > 15 * 60000) throw new Error("the user still has control of the browser after 15 minutes; do other work or ask them");
    await sleep(1000);
  }
  return "the user used the browser for " + Math.round((Date.now() - t0) / 1000) + " s while you waited; look at the page again before acting";
}

// SnapLab's live view: each connection on .snaplab/live.sock gets its own DevTools connection to the live browser.
// Lines are JSON: DevTools messages both ways, plus {"snaplab":"control","holder":"user"|"agent"} from SnapLab.
// Taking over is given back when SnapLab's connection ends.
async function relay(root) {
  const sock = SOCKET(root);
  fs.rmSync(sock, { force: true });
  const setHolder = (holder) => {
    try {
      if (holder === "user") fs.writeFileSync(CONTROL(root), JSON.stringify({ holder, since: new Date().toISOString() }));
      else fs.rmSync(CONTROL(root), { force: true });
    } catch {}
  };
  setHolder("agent");
  const server = net.createServer((conn) => {
    conn.setEncoding("utf8");
    let ws = null, buf = "", closed = false, holding = false;
    const queue = [];
    const done = () => {
      if (closed) return;
      closed = true;
      if (holding) setHolder("agent");
      try { if (ws) ws.close(); } catch {}
      conn.destroy();
    };
    const live = liveState(root);
    if (!live) { conn.end(JSON.stringify({ snaplab: "nobrowser" }) + "\n"); return; }
    ws = new WebSocket(live.ws);
    ws.onopen = () => {
      conn.write(JSON.stringify({ snaplab: "hello", headful: !!live.headful, width: live.width, height: live.height }) + "\n");
      for (const q of queue.splice(0)) ws.send(q);
    };
    ws.onmessage = (e) => { if (!closed) conn.write(String(e.data) + "\n"); };
    ws.onclose = done;
    ws.onerror = done;
    conn.on("data", (chunk) => {
      buf += chunk;
      if (buf.length > 16 * 1024 * 1024) return done();
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg && msg.snaplab === "control") { holding = msg.holder === "user"; setHolder(holding ? "user" : "agent"); continue; }
        const text = JSON.stringify(msg);
        if (ws.readyState === 1) ws.send(text); else queue.push(text);
      }
    });
    conn.on("close", done);
    conn.on("error", done);
  });
  await new Promise((ok, fail) => { server.once("error", fail); server.listen(sock, ok); });
  fs.chmodSync(sock, 0o600);
  const stop = () => { try { server.close(); fs.rmSync(sock, { force: true }); setHolder("agent"); } catch {} process.exit(0); };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

// Between turns: the browser alone, a window on the host job's desktop, until it closes (SnapLab sends
// Browser.close, so cookies are written) or the job ends.
async function host(root, opts) {
  const started = await startLive(root, { ...opts, headful: !!process.env.DISPLAY, kind: true });
  console.log(JSON.stringify({ type: "snaplab.browser_host", ...started }));
  let misses = 0;
  while (misses < 2) {
    await sleep(1000);
    const live = liveState(root);
    try { if (!live) throw new Error("gone"); const p = await socketPipe(live.ws, 3000); await p.close(); misses = 0; }
    catch { misses++; }
  }
  stopLive(root);
}
const KEEP = (root, args) => spawnSync(process.env.SNAPLAB_PYTHON || (process.platform === "win32" ? "python" : "python3"),
  [path.join(root, ".snaplab", "keep.py"), ...args], { cwd: root, encoding: "utf-8" });

function liveState(root) {
  try {
    const live = JSON.parse(fs.readFileSync(LIVE(root), "utf-8"));
    const [port, wsPath] = fs.readFileSync(path.join(root, ".snaplab", "browser-profile", "DevToolsActivePort"), "utf-8").trim().split("\n");
    return { ...live, ws: "ws://127.0.0.1:" + port + wsPath };
  } catch { return null; }
}

function saveLive(root, changes) {
  try { fs.writeFileSync(LIVE(root), JSON.stringify({ ...JSON.parse(fs.readFileSync(LIVE(root), "utf-8")), ...changes })); } catch {}
}

function freePort() {
  return new Promise((ok) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => ok(p)); }); });
}

function ensureRelay(root) {
  try { if (fs.statSync(SOCKET(root)).isSocket() && KEEP(root, ["list"]).stdout.includes("browser-live")) return; } catch {}
  KEEP(root, ["stop", "browser-live"]);
  KEEP(root, ["start", "browser-live", "--", process.execPath, path.join(root, ".snaplab", "browser.mjs"), "--live"]);
}

async function startLive(root, opts) {
  const old = liveState(root);
  if (old) {
    try {
      const pipe = await socketPipe(old.ws, 3000); await pipe.close();
      // Keep the running browser unless the other kind was asked for explicitly.
      if (!!old.headful === opts.headful || !opts.kind) { ensureRelay(root); return { ok: true, already_running: true, ...publicLive(old) }; }
    } catch {}
  }
  if (opts.headful && !process.env.DISPLAY)
    throw new Error("there is no desktop display in this job; use start without --headful (a headless browser)");
  const browser = browserPath();
  if (!browser) throw new Error("Chromium is not installed here");
  const profile = path.join(root, ".snaplab", "browser-profile");
  for (const name of ["browser", "browser-files"]) KEEP(root, ["stop", name]);
  if (opts.fresh) fs.rmSync(profile, { recursive: true, force: true });
  fs.mkdirSync(profile, { recursive: true });
  fs.rmSync(path.join(profile, "DevToolsActivePort"), { force: true });
  // A new profile does not offer to save passwords or translate pages; those bubbles would cover the page.
  // Tabs and sign-ins come back after a restart ("continue where you left off"): the user signs in between turns
  // and the agent's next turn finds the same pages.
  const prefs = path.join(profile, "Default", "Preferences");
  let saved = {};
  try { saved = JSON.parse(fs.readFileSync(prefs, "utf-8")); } catch {}
  if (!fs.existsSync(prefs)) Object.assign(saved, { credentials_enable_service: false, profile: { password_manager_enabled: false },
    translate: { enabled: false }, browser: { check_default_browser: false } });
  saved.session = { ...(saved.session || {}), restore_on_startup: 1 };
  fs.mkdirSync(path.dirname(prefs), { recursive: true });
  fs.writeFileSync(prefs, JSON.stringify(saved));
  const filesPort = await freePort();
  const files = KEEP(root, ["start", "browser-files", "--", process.execPath, path.join(root, ".snaplab", "browser.mjs"), "--serve", String(filesPort)]);
  if (files.status !== 0) throw new Error("could not start the file server: " + (files.stdout || files.stderr).trim());
  const common = ["--no-sandbox", "--disable-gpu", "--allow-file-access-from-files", "--enable-unsafe-swiftshader",
    "--use-angle=swiftshader", "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--disable-sync", "--disable-component-update", "--password-store=basic", "--user-data-dir=" + profile];
  const flags = opts.headful
    // A normal window that fills the screen; --test-type hides the "unsupported flag" bar that --no-sandbox brings.
    ? [...common, "--test-type", "--disable-infobars", "--start-maximized", "--window-position=0,0", "--hide-crash-restore-bubble",
       "--window-size=" + opts.width + "," + opts.height, ...(opts.devtools ? ["--auto-open-devtools-for-tabs"] : [])]
    : [...common, "--headless=new", "--disable-extensions", "--disable-background-networking", "--hide-scrollbars",
       "--window-size=" + opts.width + "," + opts.height];
  if (process.env.HTTP_PROXY) flags.push("--proxy-server=" + process.env.HTTP_PROXY);
  if (!opts.headful) flags.push("about:blank");        // a window restores the last pages instead
  const started = KEEP(root, ["start", "browser", "--", browser, ...flags]);
  if (started.status !== 0) throw new Error("could not start the browser: " + (started.stdout || started.stderr).trim());
  fs.writeFileSync(LIVE(root), JSON.stringify({ base: "http://127.0.0.1:" + filesPort + "/", width: opts.width, height: opts.height,
    mobile: opts.mobile && !opts.headful, headful: opts.headful }));
  const t0 = Date.now();
  while (!liveState(root)) {
    if (Date.now() - t0 > 30000) throw new Error("the browser did not open within 30 s; see: python3 .snaplab/keep.py log browser");
    await new Promise((r) => setTimeout(r, 200));
  }
  ensureRelay(root);
  return { ok: true, started: true, ...publicLive(liveState(root)), kept: JSON.parse(started.stdout || "{}").kept_until };
}

function publicLive(live) {
  return { width: live.width, height: live.height, mobile: live.mobile, headful: !!live.headful,
    ...(live.headful ? { note: "a real Chrome window on the desktop (the user can watch it in SnapLab); desktop.py screenshot shows it, F12 there opens DevTools" } : {}) };
}

function stopLive(root) {
  for (const name of ["browser", "browser-files"]) KEEP(root, ["stop", name]);
  fs.rmSync(LIVE(root), { force: true });
  return { ok: true, stopped: true };
}

const isPage = (t) => t.type === "page" && !/^(devtools|chrome-extension):/.test(t.url);

async function main() {
  const argv = process.argv.slice(2);
  const root = fs.realpathSync(process.cwd());
  if (argv[0] === "--serve") {                       // the live browser's file server, kept by keep.py
    const server = http.createServer();
    const s = await serve(root);
    s.close();
    server.on("request", (req, res) => s.emit("request", req, res));
    server.listen(Number(argv[1]), "127.0.0.1");
    return;
  }
  if (argv[0] === "--live") return relay(root);
  const { command, target, steps, opts } = parseArgs(argv);
  if (command === "host") return host(root, opts);
  if (command === "start") return console.log(JSON.stringify(await startLive(root, opts)));
  if (command === "stop") return console.log(JSON.stringify(stopLive(root)));
  let live = liveState(root);
  if (command === "status") return console.log(JSON.stringify(live ? { running: true, ...publicLive(live), control: controlHolder(root) } : { running: false }));
  // With a desktop, open and do use the live window (started here when needed), so the user can watch.
  if (!live && opts.headful && (command === "open" || command === "do")) { await startLive(root, opts); live = liveState(root); }
  if (command === "do" && !live) throw new Error("no live browser; run: node .snaplab/browser.mjs start  (then open URL)");
  const waited = live ? await waitForUser(root) : null;
  const ms = opts.timeout * 1000;
  let server = null, base = live ? live.base : "", url = target;
  if (target && !/^(https?|file|data|about|chrome):/i.test(target)) {
    const file = target.split(/[?#]/)[0];
    if (!fs.existsSync(inside(root, file))) throw new Error("no such file in the workspace: " + file);
    if (!live) { server = await serve(root); base = "http://127.0.0.1:" + server.address().port + "/"; }
    url = base + target.replace(/^\.?\/+/, "");
  }
  let pipe;
  if (live) pipe = await socketPipe(live.ws, ms);
  else {
    const browser = browserPath();
    if (!browser) throw new Error("Chromium is not installed here");
    const profile = path.join(root, ".snaplab", "browser-profile");
    if (opts.fresh) fs.rmSync(profile, { recursive: true, force: true });
    fs.mkdirSync(profile, { recursive: true });
    pipe = await browserPipe(browser, profile, { fileAccess: true, commandMs: ms });
  }
  const headful = !!(live && live.headful);
  if (live && !opts.sized) { opts.width = live.width; opts.height = live.height; opts.mobile = live.mobile; }
  const out = { ...(target ? { url: target } : {}), live: !!live, ...(headful ? { headful: true } : {}), ...(waited ? { note: waited } : {}),
    steps: [], console_errors: [], page_errors: [], failed_requests: [] };
  const requests = new Map(), seen = [], messages = [];
  const began = Date.now();
  try {
    let targetId;
    if (live) {
      const { targetInfos } = await pipe.send("Target.getTargets");
      const pages = targetInfos.filter(isPage);
      const chosen = pages.find((t) => t.targetId === live.target) || pages[0];
      targetId = chosen ? chosen.targetId : (await pipe.send("Target.createTarget", { url: "about:blank" })).targetId;
      if (pages.length > 1) out.tabs = pages.length;
    } else targetId = (await pipe.send("Target.createTarget", { url: "about:blank" })).targetId;
    let sessionId = (await pipe.send("Target.attachToTarget", { targetId, flatten: true })).sessionId;
    let send = (method, params = {}, t = ms) => pipe.send(method, params, sessionId, t);
    pipe.listeners.add((m) => {
      if (m.sessionId !== sessionId) return;
      const p = m.params || {};
      const fresh = !p.timestamp || p.timestamp >= began - 1000;    // Runtime.enable replays messages from earlier commands
      if (m.method === "Runtime.exceptionThrown") {
        const d = p.exceptionDetails || {};
        const text = ((d.exception && d.exception.description) || d.text || "exception").split("\n").slice(0, 2).join(" ");
        messages.push({ level: "exception", text, at: d.url ? d.url.split("/").pop() + ":" + (d.lineNumber + 1) : undefined, earlier: !fresh });
        if (fresh) out.page_errors.push(text);
      } else if (m.method === "Runtime.consoleAPICalled") {
        const text = (p.args || []).map((a) => a.value !== undefined ? (typeof a.value === "string" ? a.value : JSON.stringify(a.value))
          : a.description ?? a.type).join(" ");
        const frame = (p.stackTrace && p.stackTrace.callFrames || [])[0];
        messages.push({ level: p.type, text, at: frame ? frame.url.split("/").pop() + ":" + (frame.lineNumber + 1) : undefined, earlier: !fresh });
        if (fresh && ["error", "warning", "assert"].includes(p.type)) out.console_errors.push(p.type + ": " + text);
      } else if (m.method === "Network.requestWillBeSent") { requests.set(p.requestId, p.request.url); seen.push({ id: p.requestId, method: p.request.method, url: p.request.url, type: p.type }); }
      else if (m.method === "Network.responseReceived") {
        const r = seen.find((s) => s.id === p.requestId); if (r) r.status = p.response.status;
        if (p.response.status >= 400 && !p.response.url.endsWith("/favicon.ico")) out.failed_requests.push(p.response.status + " " + p.response.url);
      } else if (m.method === "Network.loadingFailed" && !p.canceled) {
        const r = seen.find((s) => s.id === p.requestId); if (r) r.status = p.errorText || "failed";
        out.failed_requests.push((p.errorText || "failed") + " " + (requests.get(p.requestId) || ""));
      }
    });
    const prepare = async () => {
      for (const d of ["Page", "Runtime", "Network", "DOM"]) await send(d + ".enable");
      // A headful window keeps its real size; emulation would make page pixels differ from what is on screen.
      if (!headful) {
        await send("Emulation.setDeviceMetricsOverride", { width: opts.width, height: opts.height, deviceScaleFactor: 1, mobile: opts.mobile });
        if (opts.mobile) await send("Emulation.setTouchEmulationEnabled", { enabled: true });
      }
    };
    await prepare();

    const evaluate = async (expression) => {
      const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
      if (r.exceptionDetails) throw new Error("script error: " + ((r.exceptionDetails.exception || {}).description || r.exceptionDetails.text || "").split("\n")[0]);
      return r.result ? r.result.value : undefined;
    };
    const navigate = async (to) => {
      const loaded = new Promise((ok) => {
        const timer = setTimeout(() => { pipe.listeners.delete(l); ok(false); }, ms);
        const l = (m) => { if (m.sessionId === sessionId && m.method === "Page.loadEventFired") { clearTimeout(timer); pipe.listeners.delete(l); ok(true); } };
        pipe.listeners.add(l);
      });
      const r = await send("Page.navigate", { url: to });
      if (r.errorText) throw new Error("could not open " + to + ": " + r.errorText);
      if (!(await loaded)) out.steps.push({ step: "open", note: "the page did not finish loading within --timeout; continuing" });
      await evaluate("new Promise(r => requestAnimationFrame(() => setTimeout(r, 300)))");
    };
    // Where to click: "x,y", "text=Label" (smallest visible element containing the text) or a CSS selector.
    const point = async (spec) => {
      const xy = spec.match(/^(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)$/);
      if (xy) return { x: Number(xy[1]), y: Number(xy[2]) };
      const finder = spec.startsWith("text=")
        ? `(() => { const want = ${JSON.stringify(spec.slice(5).trim())}.toLowerCase(); let best = null;
            for (const el of document.querySelectorAll("body *")) { const r = el.getBoundingClientRect();
              if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
              const t = (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("placeholder") || "").trim().toLowerCase();
              if (t.includes(want) && (!best || r.width * r.height < best.a)) best = { el, a: r.width * r.height }; }
            return best && best.el; })()`
        : `document.querySelector(${JSON.stringify(spec)})`;
      const box = await evaluate(`(() => { const el = ${finder}; if (!el) return null; el.scrollIntoView({block: "center", inline: "center"});
        const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
      if (!box) throw new Error("nothing on the page matches " + spec);
      return box;
    };
    let modifiers = 0, buttons = 0;
    const mouse = async (type, p, extra = {}) => send("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, modifiers, buttons, button: "left", ...extra });
    const click = async (p, count = 1, button = "left") => {
      const bit = { left: 1, right: 2, middle: 4 }[button];
      await mouse("mouseMoved", p, { button: "none" });
      for (let i = 1; i <= count; i++) {
        await mouse("mousePressed", p, { button, clickCount: i, buttons: buttons | bit });
        await mouse("mouseReleased", p, { button, clickCount: i });
      }
    };
    const keyName = (name) => ALIASES[name.toLowerCase()] || (KEYS[name] ? name : /^f([1-9]|1[0-2])$/i.test(name) ? name.toUpperCase() : name);
    const key = async (raw, type) => {
      const name = keyName(raw);
      let vk, text = "", code = "";
      if (KEYS[name]) [vk, text = "", code = name] = KEYS[name];
      else if (name.length === 1) {
        const p = PUNCT[name];
        vk = p ? p[0] : name.toUpperCase().charCodeAt(0);
        code = p ? p[1] : /[a-z]/i.test(name) ? "Key" + name.toUpperCase() : /\d/.test(name) ? "Digit" + name : "";
        text = modifiers & 8 && /[a-z]/.test(name) ? name.toUpperCase() : name;
      } else throw new Error("unknown key " + raw + " (names: Enter Tab Escape Space ArrowLeft F12 Control Shift Alt Meta a 1 ...)");
      if (MODS[name] && type === "keyDown") modifiers |= MODS[name];
      if (MODS[name] && type === "keyUp") modifiers &= ~MODS[name];
      const shortcut = modifiers & (MODS.Control | MODS.Alt | MODS.Meta);
      await send("Input.dispatchKeyEvent", { type: type === "keyDown" ? (text && !shortcut ? "keyDown" : "rawKeyDown") : "keyUp",
        modifiers, key: name === "Space" ? " " : name, code, windowsVirtualKeyCode: vk,
        ...(type === "keyDown" && text && !shortcut ? { text, unmodifiedText: text } : {}) });
    };
    const typeText = async (text) => {
      for (const ch of text) {
        if (/^[\x20-\x7e]$/.test(ch)) { await key(ch === " " ? "Space" : ch, "keyDown"); await key(ch === " " ? "Space" : ch, "keyUp"); }
        else if (ch === "\n") { await key("Enter", "keyDown"); await key("Enter", "keyUp"); }
        else await send("Input.insertText", { text: ch });         // Japanese and other text without a key on the keyboard
      }
    };
    const waitFor = async (spec) => {
      if (/^\d+(\.\d+)?$/.test(spec)) return new Promise((r) => setTimeout(r, Number(spec) * 1000));
      const test = spec.startsWith("text=") ? `document.body && document.body.innerText.includes(${JSON.stringify(spec.slice(5))})`
        : `!!document.querySelector(${JSON.stringify(spec)})`;
      const ok = await evaluate(`new Promise((done) => { const t0 = Date.now(); const tick = () => { let v = false; try { v = ${test}; } catch {}
        if (v || Date.now() - t0 > ${ms}) done(v); else setTimeout(tick, 200); }; tick(); })`);
      if (!ok) throw new Error("waited " + opts.timeout + " s but " + spec + " did not appear");
    };
    // Page pixels -> screen pixels (headful only): a mouse event reports both its page and its screen position.
    const screenOffset = async () => {
      if (!headful) return null;
      await evaluate(`window.__snaplabOffset = null; addEventListener("mousemove", (e) => { window.__snaplabOffset = [e.screenX - e.clientX, e.screenY - e.clientY]; }, { once: true, capture: true }); 0`);
      await mouse("mouseMoved", { x: 2, y: 2 }, { button: "none" });
      return await evaluate("window.__snaplabOffset");
    };
    const clip = (v, n = 4000) => typeof v === "string" ? v.slice(0, n) : JSON.parse(JSON.stringify(v ?? null, null).slice(0, n) || "null");

    if (url) await navigate(url);
    out.title = await evaluate("document.title");
    for (const step of steps) {
      const i = step.indexOf(":"), name = i < 0 ? step : step.slice(0, i), arg = i < 0 ? "" : step.slice(i + 1);
      const rec = { step: step.length > 200 ? step.slice(0, 200) + "…" : step };
      try {
        if (name === "click" || name === "dblclick") await click(await point(arg), name === "dblclick" ? 2 : 1);
        else if (name === "rightclick") await click(await point(arg), 1, "right");
        else if (name === "hover" || name === "move") await mouse("mouseMoved", await point(arg), { button: "none" });
        else if (name === "mousedown") { const p = await point(arg); buttons |= 1; await mouse("mousePressed", p, { clickCount: 1 }); }
        else if (name === "mouseup") { const p = await point(arg); buttons &= ~1; await mouse("mouseReleased", p, { clickCount: 1 }); }
        else if (name === "drag") {
          const [x1, y1, x2, y2] = arg.split(",").map(Number);
          await mouse("mouseMoved", { x: x1, y: y1 }, { button: "none" }); await mouse("mousePressed", { x: x1, y: y1 }, { clickCount: 1, buttons: 1 });
          for (let s = 1; s <= 10; s++) await mouse("mouseMoved", { x: x1 + (x2 - x1) * s / 10, y: y1 + (y2 - y1) * s / 10 }, { buttons: 1 });
          await mouse("mouseReleased", { x: x2, y: y2 }, { clickCount: 1 });
        } else if (name === "wheel") {
          const [x, y, dy, dx = 0] = arg.split(",").map(Number);
          await mouse("mouseWheel", { x, y }, { button: "none", deltaX: dx, deltaY: dy });
        } else if (name === "type") {
          const eq = arg.indexOf("="), where = eq < 0 ? "" : arg.slice(0, eq), text = eq < 0 ? arg : arg.slice(eq + 1);
          if (where) await click(await point(where));
          await typeText(text);
        } else if (name === "press") {
          const parts = arg.split(/\+(?=.)/);
          for (const p of parts) await key(p, "keyDown");
          for (const p of parts.reverse()) await key(p, "keyUp");
        } else if (name === "keydown" || name === "keyup") await key(arg, name === "keydown" ? "keyDown" : "keyUp");
        else if (name === "hold") {
          const [k, s] = arg.split("=");
          await key(k, "keyDown"); await new Promise((r) => setTimeout(r, (Number(s) || 1) * 1000)); await key(k, "keyUp");
        } else if (name === "wait") await waitFor(arg || "1");
        else if (name === "scroll") {
          if (arg === "up" || arg === "down") await evaluate(`window.scrollBy(0, ${arg === "up" ? -0.8 : 0.8} * innerHeight)`);
          else await point(arg);
        } else if (name === "eval") rec.value = clip(await evaluate(arg));
        else if (name === "console") {
          await new Promise((r) => setTimeout(r, 100));
          const n = Number(arg) || 50;
          rec.value = messages.slice(-n).map((m) => (m.earlier ? "" : "[new] ") + m.level + ": " + String(m.text).slice(0, 500) + (m.at ? "  (" + m.at + ")" : ""));
          if (!messages.length) rec.note = "no console messages since the page loaded";
        } else if (name === "network") {
          const entries = await evaluate(`performance.getEntriesByType("navigation").concat(performance.getEntriesByType("resource")).map((e) => ({
            url: e.name, type: e.initiatorType || e.entryType, status: e.responseStatus || null, kb: Math.round((e.transferSize || 0) / 1024),
            ms: Math.round(e.duration) }))`);
          const strip = (u) => base ? u.split(base).join("") : u;
          rec.value = entries.filter((e) => !arg || e.url.includes(arg)).slice(-100).map((e) => ({ ...e, url: strip(e.url).slice(0, 300) }));
          const now = seen.filter((s) => !arg || s.url.includes(arg)).map((s) => ({ method: s.method, url: strip(s.url).slice(0, 300), type: s.type, status: s.status ?? "pending" }));
          if (now.length) rec.during_this_command = now.slice(-50);
        } else if (name === "dom") rec.value = String(await evaluate(`(() => { const el = ${arg ? `document.querySelector(${JSON.stringify(arg)})` : "document.body"};
            return el ? el.outerHTML : "nothing matches"; })()`)).slice(0, 8000);
        else if (name === "elements") {
          const offset = await screenOffset().catch(() => null);
          const list = await evaluate(`(() => { const out = [];
            const sel = 'a[href],button,input,select,textarea,summary,canvas,video,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[onclick],[contenteditable=""],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
            const listed = new Set(document.querySelectorAll(sel));
            // Many close marks, cookie buttons and game menus are plain divs or spans that only look clickable.
            for (const el of document.querySelectorAll("div,span,li,img,svg,i,p,label")) {
              if (listed.size > 1500) break;
              if (listed.has(el) || getComputedStyle(el).cursor !== "pointer") continue;
              const up = el.parentElement;
              if (up && getComputedStyle(up).cursor === "pointer") continue;   // the outermost clickable part only
              listed.add(el); }
            // What is on the screen first: a long page must not push a cookie bar at the end of the document out of the list.
            const onScreen = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth; };
            const ordered = [...listed].filter(onScreen).concat([...listed].filter((el) => !onScreen(el)));
            for (const el of ordered) { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
              if (!r.width || !r.height || cs.visibility === "hidden" || cs.display === "none") continue;
              const field = /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
              const text = (field ? (el.labels && el.labels[0] && el.labels[0].innerText) || el.placeholder || el.getAttribute("aria-label") || el.name || ""
                : el.innerText || el.getAttribute("aria-label") || el.title || el.alt || "").trim().replace(/\\s+/g, " ").slice(0, 80);
              const e = { el: el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (el.type && field ? "[type=" + el.type + "]" : ""), text,
                x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
              if (field) e.value = el.type === "password" ? (el.value ? "(" + el.value.length + " characters)" : "") : String(el.value).slice(0, 80);
              if (el.disabled) e.disabled = true;
              if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) e.offscreen = true;
              else { const top = document.elementFromPoint(e.x, e.y);   // a banner, dialog or ad frame over it
                if (top && top !== el && !el.contains(top) && !top.contains(el)) e.covered = true; }
              out.push(e); if (out.length >= 300) break; }
            return out; })()`);
          if (offset) for (const e of list) { e.sx = e.x + offset[0]; e.sy = e.y + offset[1]; }
          rec.value = list;
          rec.note = "x,y are page pixels (browser.mjs click:x,y)" + (offset ? "; sx,sy are screen pixels (desktop.py click --x sx --y sy)" : "");
        } else if (name === "a11y") {
          await send("Accessibility.enable");
          const { nodes } = await send("Accessibility.getFullAXTree");
          const byId = new Map(nodes.map((n) => [n.nodeId, n])), lines = [];
          const skip = new Set(["generic", "none", "InlineTextBox", "LineBreak", "RootWebArea"]);
          const walk = (n, depth) => {
            if (!n || lines.length >= 400) return;
            const role = n.role && n.role.value, label = n.name && n.name.value, value = n.value && n.value.value;
            const show = !n.ignored && role && !skip.has(role) && (label || value || !["StaticText", "paragraph"].includes(role));
            if (show && !(role === "StaticText" && !label)) lines.push("  ".repeat(Math.min(depth, 12)) + role + (label ? " " + JSON.stringify(String(label).slice(0, 120)) : "") + (value !== undefined && value !== "" ? " value=" + JSON.stringify(String(value).slice(0, 80)) : ""));
            for (const c of n.childIds || []) walk(byId.get(c), depth + (show ? 1 : 0));
          };
          walk(nodes[0], 0);
          rec.value = lines;
        } else if (name === "storage") {
          rec.value = await evaluate(`(() => { const dump = (s) => { const o = {}; try { for (let i = 0; i < s.length; i++) { const k = s.key(i); o[k] = String(s.getItem(k)).slice(0, 1000); } } catch (e) { return String(e); } return o; };
            return { origin: location.origin, localStorage: dump(localStorage), sessionStorage: dump(sessionStorage) }; })()`);
          const { cookies } = await send("Network.getCookies");
          rec.value.cookies = cookies.slice(0, 50).map((c) => ({ name: c.name, value: String(c.value).slice(0, 200), domain: c.domain, httpOnly: c.httpOnly }));
        } else if (name === "tabs") {
          const { targetInfos } = await pipe.send("Target.getTargets");
          rec.value = targetInfos.filter(isPage).map((t, n) => ({ tab: n, title: t.title, url: t.url.split(base || "\0").join(""), current: t.targetId === targetId }));
        } else if (name === "tab") {
          const { targetInfos } = await pipe.send("Target.getTargets");
          const pick = targetInfos.filter(isPage)[Number(arg)];
          if (!pick) throw new Error("no tab " + arg + "; use the tabs step to list them");
          targetId = pick.targetId;
          sessionId = (await pipe.send("Target.attachToTarget", { targetId, flatten: true })).sessionId;
          await pipe.send("Target.activateTarget", { targetId });
          await prepare();
          if (live) saveLive(root, { target: targetId });
          rec.value = { title: pick.title, url: pick.url.split(base || "\0").join("") };
        } else if (name === "shot") {
          const file = arg || "page.png";
          if (!file.endsWith(".png")) throw new Error("shot needs a .png name");
          const full = inside(root, file);
          const shot = await send("Page.captureScreenshot", { format: "png" });
          fs.mkdirSync(path.dirname(full), { recursive: true });
          fs.writeFileSync(full, Buffer.from(shot.data, "base64"));
          rec.saved = path.relative(root, full).split(path.sep).join("/");
        } else if (name === "text") rec.value = String(await evaluate("document.body ? document.body.innerText : ''")).slice(0, 4000);
        else if (name === "url") await navigate(/^(https?|file|data|about):/i.test(arg) || !base ? arg : base + arg.replace(/^\.?\/+/, ""));
        else if (name === "reload") {
          const loaded = new Promise((ok) => { const t = setTimeout(() => ok(), ms);
            const l = (m) => { if (m.sessionId === sessionId && m.method === "Page.loadEventFired") { clearTimeout(t); pipe.listeners.delete(l); ok(); } };
            pipe.listeners.add(l); });
          await send("Page.reload", { ignoreCache: true }); await loaded;
          await evaluate("new Promise(r => requestAnimationFrame(() => setTimeout(r, 300)))");
        }
        else throw new Error("unknown step " + name + " (see the top of .snaplab/browser.mjs)");
        rec.ok = true;
      } catch (e) {
        rec.ok = false; rec.error = String(e && e.message || e);
        out.steps.push(rec);
        break;                    // later steps would act on the wrong screen
      }
      out.steps.push(rec);
    }
    // Keys and buttons held with keydown/mousedown stay held in this browser only until the command ends.
    for (const [name, bit] of Object.entries(MODS)) if (modifiers & bit) await key(name, "keyUp").catch(() => {});
    out.final_url = await evaluate("location.href").catch(() => "");
    if (opts.shot && !(steps.length && steps[steps.length - 1].startsWith("shot:") && out.steps.every((s) => s.ok !== false))) {
      const shot = await send("Page.captureScreenshot", { format: "png" }).catch(() => null);
      if (shot) { fs.mkdirSync(path.join(root, ".snaplab"), { recursive: true }); fs.writeFileSync(path.join(root, ".snaplab", "browser.png"), Buffer.from(shot.data, "base64")); out.screenshot = ".snaplab/browser.png"; }
    }
  } finally {
    await pipe.close();
    if (server) server.close();
  }
  for (const k of ["console_errors", "page_errors", "failed_requests"]) {
    out[k] = [...new Set(out[k])].slice(0, 30).map((s) => s.slice(0, 300));
    if (base) out[k] = out[k].map((s) => s.split(base).join(""));
  }
  if (base && out.final_url) out.final_url = out.final_url.split(base).join("");
  console.log(JSON.stringify(out, null, 1));
  process.exitCode = out.steps.every((s) => s.ok !== false) ? 0 : 1;
}

main().catch((e) => { console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) })); process.exitCode = 1; });

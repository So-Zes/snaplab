// No packages and no TCP connection. This helper MUST run inside the session sandbox.
import { spawn } from "node:child_process";
import fs from "node:fs";

// options.fileAccess lets a local page load its own modules, textures and data over file:// (three.js, fetch).
// options.commandMs is how long one browser command may take; heavy 3D pages need far more than a phone page.
// 0 means no time limit (browser.mjs without --timeout); left out, it is 12 s.
export async function browserPipe(browser, profile, options = {}) {
  const commandMs = options.commandMs === undefined ? 12000 : options.commandMs;
  const child = spawn(browser, ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-pipe",
    ...(options.fileAccess ? ["--allow-file-access-from-files", "--enable-unsafe-swiftshader", "--use-angle=swiftshader"] : []),
    ...(process.env.HTTP_PROXY ? ["--proxy-server=" + process.env.HTTP_PROXY] : []),
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
    "--disable-component-update", "--disable-sync", "--hide-scrollbars", "--user-data-dir=" + profile, "about:blank"],
    { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"], windowsHide: true });
  let next = 0, buffer = "";
  const pending = new Map(), listeners = new Set();
  const fail = () => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("the browser stopped")); } pending.clear(); };
  child.on("error", fail); child.on("exit", fail);
  child.stdio[3].on("error", fail);
  child.stdio[4].setEncoding("utf8");
  child.stdio[4].on("data", (data) => {
    buffer += data;
    let end;
    while ((end = buffer.indexOf("\0")) >= 0) {
      const text = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (!text) continue;
      const msg = JSON.parse(text), p = pending.get(msg.id);
      if (p) {
        clearTimeout(p.timer); pending.delete(msg.id);
        msg.error ? p.reject(new Error("browser command " + p.method + " failed: " + (msg.error.message || "unknown")))
          : p.resolve(msg.result);
      } else for (const listener of listeners) listener(msg);
    }
  });
  const send = (method, params = {}, sessionId, ms = commandMs) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = ms ? setTimeout(() => { pending.delete(id); reject(new Error("browser command " + method + " took longer than " + Math.round(ms / 1000) + " s")); }, ms) : null;
    pending.set(id, { resolve, reject, timer, method });
    child.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + "\0");
  });
  const close = async () => {
    try { await send("Browser.close", {}, undefined, 5000); } catch {}
    if (child.exitCode === null) child.kill();
    fail();
  };
  return { send, close, listeners };
}

async function main() {
  const [browser, profile, url, output, width, height, timeoutS, waitS, ready] = process.argv.slice(2);
  if (!url?.startsWith("file://") || !output?.endsWith(".png")) throw new Error("local HTML and PNG required");
  const totalMs = Math.max(10, Number(timeoutS) || 300) * 1000;
  const pipe = await browserPipe(browser, profile, { fileAccess: true, commandMs: totalMs });
  const problems = [];
  try {
    const { targetId } = await pipe.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await pipe.send("Target.attachToTarget", { targetId, flatten: true });
    const send = (method, params = {}, ms) => pipe.send(method, params, sessionId, ms);
    // Page errors are reported with the result, so a blank picture comes with its reason.
    pipe.listeners.add((m) => {
      if (m.sessionId !== sessionId) return;
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails || {};
        problems.push("page error: " + ((d.exception && d.exception.description) || d.text || "exception").split("\n")[0]);
      } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        problems.push("console.error: " + (m.params.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
      } else if (m.method === "Log.entryAdded" && m.params.entry.level === "error") {
        problems.push("browser: " + m.params.entry.text + (m.params.entry.url ? " (" + m.params.entry.url.split("/").pop() + ")" : ""));
      }
    });
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Log.enable");
    await send("Emulation.setDeviceMetricsOverride", { width: Number(width), height: Number(height), deviceScaleFactor: 1,
      mobile: Number(width) <= 600 });
    if (Number(width) <= 600) await send("Emulation.setTouchEmulationEnabled", { enabled: true });
    // No remote resources, even if invoked in a less restrictive test environment.
    await send("Network.enable");
    await send("Network.setBlockedURLs", { urls: ["http://*", "https://*", "ws://*", "wss://*"] });
    const started = Date.now();
    const loaded = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pipe.listeners.delete(listener); reject(new Error("the page did not finish loading within " + Math.round(totalMs / 1000) + " s")); }, totalMs);
      const listener = (m) => { if (m.sessionId === sessionId && m.method === "Page.loadEventFired") {
        clearTimeout(timeout); pipe.listeners.delete(listener); resolve();
      }};
      pipe.listeners.add(listener);
    });
    await send("Page.navigate", { url });
    await loaded;
    const left = () => Math.max(1000, totalMs - (Date.now() - started));
    if (ready) {
      // Wait until the page says it has drawn (for example window.__ready === true), polling in the page.
      const expression = "new Promise((ok) => { const t0 = Date.now(); const tick = () => { let v = false; "
        + "try { v = !!(" + ready + "); } catch (e) {} if (v || Date.now() - t0 > " + left() + ") ok(v); else setTimeout(tick, 250); }; tick(); })";
      const r = await send("Runtime.evaluate", { expression, awaitPromise: true }, left() + 5000);
      if (!(r.result && r.result.value)) problems.push("--ready " + JSON.stringify(ready) + " never became true; the picture may be incomplete");
    }
    const waitMs = Math.max(0, Number(waitS) || 0) * 1000;
    await send("Runtime.evaluate", { expression: "document.fonts.ready.then(() => new Promise(r => requestAnimationFrame(() => setTimeout(r, " + waitMs + "))))", awaitPromise: true }, waitMs + left());
    const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, left());
    fs.writeFileSync(output, Buffer.from(shot.data, "base64"), { flag: "wx" });
  } finally {
    await pipe.close();
    if (problems.length) console.error([...new Set(problems)].slice(0, 12).map((p) => p.slice(0, 300)).join("\n"));
  }
}

if (process.argv[1]?.endsWith("capture.mjs")) {
  main().catch((e) => { console.error("Screenshot capture failed: " + (e && e.message || e)); process.exitCode = 1; });
}

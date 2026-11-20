// Real Chrome rendering + the real Python HTTP handler, connected by pipes inside the offline sandbox.
// Run node tests/round11_browser.mjs. Requires installed Chrome, Node and Python; installs nothing.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawn, execFileSync } from "node:child_process";
import readline from "node:readline";
import { browserPipe } from "../snaplab/harnesses/capture.mjs";

const root = path.resolve(import.meta.dirname, "..");
const scratch = fs.mkdtempSync(path.join(root, ".tmp-round11-browser-"));
const shots = path.join(scratch, "screenshots");
fs.mkdirSync(shots, { recursive: true });
fs.copyFileSync(path.join(root, "tests/fixtures/round11-app.html"), path.join(scratch, "index.html"));
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(k)));
execFileSync("python", [path.join(root, "snaplab/harnesses/capture.py"), "index.html", "--output", "phone.png"],
  { cwd: scratch, env, windowsHide: true, stdio: "pipe" });
const worker = spawn("python", ["-u", "tools/round11_browser_fixture.py", path.join(scratch, "phone.png")],
  { cwd: root, env, windowsHide: true, stdio: ["pipe", "pipe", "inherit"] });
let id = 0;
const pending = new Map();
let startResolve;
const ready = new Promise(resolve => { startResolve = resolve; });
readline.createInterface({ input: worker.stdout }).on("line", line => {
  const value = JSON.parse(line);
  if (!value.id) startResolve(value);
  else { const p = pending.get(value.id); pending.delete(value.id); p?.(value); }
});
const rpc = (item) => new Promise(resolve => { const n = ++id; pending.set(n, resolve); worker.stdin.write(JSON.stringify({ id: n, ...item }) + "\n"); });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const browser = [process.env.PROGRAMFILES, process.env["PROGRAMFILES(X86)"], process.env.LOCALAPPDATA]
  .filter(Boolean).map(p => path.join(p, "Google/Chrome/Application/chrome.exe")).find(fs.existsSync);
assert.ok(browser, "Chrome is required for the offline browser test");
let pipe;
try {
  const fixture = await ready;
  pipe = await browserPipe(browser, path.join(scratch, "profile"));
  const { targetId } = await pipe.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await pipe.send("Target.attachToTarget", { targetId, flatten: true });
  const send = (method, params = {}) => pipe.send(method, params, sessionId);
  const contexts = new Map();
  const failedRequests = [];
  const externalRequests = [];
  const interceptedSessions = new Set([sessionId]);
  pipe.listeners.add(async m => {
    if (!interceptedSessions.has(m.sessionId)) return;
    if (m.method === "Runtime.executionContextCreated") contexts.set(m.params.context.id, m.params.context);
    if (m.method === "Fetch.requestPaused") {
      const { requestId, request } = m.params;
      const reply = (method, params) => pipe.send(method, params, m.sessionId);
      try {
        const url = new URL(request.url);
        if (url.origin !== "https://snaplab.test") {
          externalRequests.push(request.url);
          await reply("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" }); return;
        }
        if (url.pathname === "/api/events") {
          await reply("Fetch.fulfillRequest", { requestId, responseCode: 200,
            responseHeaders: [{ name: "Content-Type", value: "text/event-stream" }], body: Buffer.from("retry: 60000\n\n").toString("base64") }); return;
        }
        // CDP pauses before Chrome adds Fetch Metadata headers. Reconstruct the
        // destination from Chrome's resource/frame metadata for this socket-free fixture.
        // Real HTTPS header delivery must also be checked outside the sandbox.
        const headers = { ...request.headers };
        if (m.params.resourceType === "Document") {
          headers["Sec-Fetch-Dest"] = m.params.frameId === targetId ? "document" : "iframe";
        }
        const result = await rpc({ path: url.pathname + url.search, method: request.method, headers, body: request.postData || "" });
        const { id: _, ...response } = result;
        if (response.responseCode >= 400) failedRequests.push([url.pathname, response.responseCode]);
        await reply("Fetch.fulfillRequest", { requestId, ...response });
      } catch (err) { console.error("Fixture request failed:", err.message); }
    }
  });
  await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
  assert.equal((await send("Page.getFrameTree")).frameTree.frame.id, targetId);
  // The fixture serves finite HTTP replies; hold the event channel open with a local stand-in.
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `window.EventSource = class extends EventTarget {
    constructor() { super(); setTimeout(() => this.dispatchEvent(new Event('hello')), 20); }
    close() {}
  };` });
  await send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  await send("Network.setExtraHTTPHeaders", { headers: { "Tailscale-User-Login": "owner@example.com" } });
  await send("Network.setCookie", { name: fixture.cookie_name, value: fixture.cookie_value, url: "https://snaplab.test/", secure: true, httpOnly: true });
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const evaluate = async (expression, targetSession = sessionId) => {
    const result = await pipe.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, targetSession);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  const wait = async (expr, contextId) => {
    for (let i = 0; i < 60; i++) { if (await evaluate(expr, contextId)) return; await delay(100); }
    throw new Error("Timed out: " + expr);
  };
  const shot = async name => {
    await delay(350);
    const img = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(shots, name + ".png"), Buffer.from(img.data, "base64"));
  };
  await send("Page.navigate", { url: "https://snaplab.test/#/room/" + fixture.room });
  await wait("document.querySelectorAll('.attachment-card').length === 2");
  await wait("document.querySelector('.attachment-image img')?.naturalWidth === 390");
  assert.equal(await evaluate("document.documentElement.scrollWidth <= 390"), true);
  assert.equal(await evaluate("[...document.querySelectorAll('.attachment-name,.attachment-caption,.attachment-size,.attachment-actions .btn')].every(e=>parseFloat(getComputedStyle(e).fontSize)>=14)"), true);
  await evaluate("document.querySelector('.result-card').scrollIntoView({block:'start'}); window.scrollBy(0,-72); true");
  await shot("chat-ja");
  await evaluate("document.querySelector('.attachment-image').click(); true");
  await wait("document.querySelector('.attachment-full')?.naturalWidth === 390");
  await shot("image-ja");
  await evaluate("document.querySelector('.sheet-head button').click(); true");
  await delay(300);
  const htmlCard = "document.querySelectorAll('.attachment-card')[1]";
  assert.equal(await evaluate(`${htmlCard}.querySelectorAll('button,iframe').length`), 0);
  assert.equal(await evaluate(`${htmlCard}.querySelectorAll('a').length`), 1);
  assert.equal(await evaluate(`${htmlCard}.querySelector('a').getAttribute('download')`), 'index.html');
  assert.equal(await evaluate("document.querySelectorAll('iframe').length"), 0);
  const downloadUrl = await evaluate(`${htmlCard}.querySelector('a').href`);
  // Chrome downloads bypass this socket-free Fetch fixture. Check bytes and headers
  // here; the coordinator verifies actual file saving over HTTPS on the phone.
  for (const mode of ['content', 'download']) {
    const result = await evaluate(`fetch(${JSON.stringify(downloadUrl.replace('/download', '/'))} + '${mode}').then(async r=>({status:r.status,type:r.headers.get('content-type'),disposition:r.headers.get('content-disposition'),body:await r.text()}))`);
    assert.equal(result.status, 200);
    assert.equal(result.type, 'application/octet-stream');
    assert.ok(result.disposition.startsWith('attachment;'));
    assert.equal(result.body, fs.readFileSync(path.join(root, 'tests/fixtures/round11-app.html'), 'utf8'));
  }
  await send("Page.reload");
  await wait("document.querySelectorAll('.attachment-card').length === 2");
  await wait("document.querySelector('.attachment-image img')?.naturalWidth === 390");
  await evaluate("document.querySelector('[aria-label=\"このチャットのファイル\"]').click(); true");
  await wait("document.querySelectorAll('.room-files .attachment-card').length === 2");
  assert.equal(await evaluate("document.querySelectorAll('.room-files button,.room-files iframe').length"), 2);
  assert.equal(await evaluate("[...document.querySelectorAll('.room-files a')].filter(a=>a.href.endsWith('/preview')).length"), 0);
  await shot("files-ja");
  await evaluate("document.querySelector('.sheet-head button').click(); true");
  await delay(300);
  assert.deepEqual(failedRequests, []);
  const source = await evaluate("document.querySelector('.attachment-image img').src");
  const attackUrl = 'https://snaplab.test/api/attachments/' + fixture.attack + '/preview';
  await evaluate(`{ const f=document.createElement('iframe'); f.src=${JSON.stringify(attackUrl)}; document.body.append(f); } true`);
  for (let i=0; i<60 && !failedRequests.some(([url, code]) => url === new URL(attackUrl).pathname && code === 404); i++) await delay(100);
  assert.ok(failedRequests.some(([url, code]) => url === new URL(attackUrl).pathname && code === 404), 'embedded HTML is rejected');
  // Error replies deny framing, so their document cannot be inspected from the parent.
  await send("Page.navigate", { url: attackUrl });
  await wait("document.body?.textContent.includes('not found')");
  assert.equal(await evaluate("window.htmlExecuted === true"), false);
  assert.ok(failedRequests.some(([url, code]) => url === new URL(attackUrl).pathname && code === 404));
  assert.deepEqual(externalRequests, [], "direct HTML must not execute and send input outside");
  await rpc({ revoke: true });
  assert.equal(await evaluate(`fetch(${JSON.stringify(source)}, {cache:'no-store'}).then(r=>r.status)`), 401);
  console.log("Round 11 browser: screenshot, 390px layout, download-only HTML and rejected direct preview, reload and revocation passed.");
} finally {
  if (pipe) await pipe.close();
  worker.stdin.end();
}

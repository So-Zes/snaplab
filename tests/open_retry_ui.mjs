// Opening the app: a read that the PC's Tailscale proxy answers with 502-504 is asked again instead of showing
// "HTTP 502" (2026-11-17); a change (POST) is never sent twice. Run: node tests/open_retry_ui.mjs
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { context, ui, timers } = makeUI();
const flush = () => new Promise((resolve) => setImmediate(resolve));
let answers = [];
const sent = [];
context.fetch = async (path, options = {}) => {
  sent.push(options.method || "GET");
  const status = answers.shift() ?? 200;
  return { ok: status < 400, status, json: async () => (status < 400 ? { ok: true, version: 1 } : Promise.reject(new Error("no body"))) };
};
async function settle(promise) {
  let out;
  promise.then((v) => { out = { v }; }, (e) => { out = { e }; });
  for (let i = 0; i < 20 && !out; i++) { await flush(); while (timers.length) timers.shift()(); }
  return out;
}

answers = [502];
let out = await settle(ui.api("/api/bootstrap"));
assert.deepEqual(out.v, { ok: true, version: 1 });
assert.deepEqual(sent, ["GET", "GET"]);

sent.length = 0; answers = [502, 504, 503];
out = await settle(ui.api("/api/bootstrap"));
assert.equal(out.e.message, "HTTP 503", "it gives up after three tries");
assert.equal(sent.length, 3);

sent.length = 0; answers = [502];
out = await settle(ui.api("/api/rooms/r1/messages", { text: "hi" }));
assert.equal(out.e.message, "HTTP 502");
assert.deepEqual(sent, ["POST"], "a change is not sent again");

sent.length = 0; answers = [401];
out = await settle(ui.api("/api/bootstrap"));
assert.equal(out.e.status, 401, "other refusals are not retried");
assert.equal(sent.length, 1);
console.log("open_retry_ui: ok");

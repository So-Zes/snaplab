// Drafts kept on the device, and the usage chart's periods. Dependency-free (see ui_harness.mjs).
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { context, ui, requests, timers, document } = makeUI();
const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);
const store = new Map();
context.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
context.Event = class { constructor(type) { this.type = type; } };
for (const name of ["dispatchEvent"]) context.Node.prototype[name] = function (ev) { (this.events[ev.type] || []).forEach((fn) => fn(ev)); return true; };
const flush = () => { while (timers.length) timers.shift()(); };
const room = { id: "r-00000001", kind: "dm", bots: ["b1"], name: "Lead" };
ui.S.bots = [{ id: "b1", name: "Lead", initials: "L" }];

// Typing is saved per room; a new composer for the same room (reload, restart, redraw) gets it back.
let c = ui.buildComposer(room, ui.S.bots[0]);
c.ta.value = "a long message typed on the phone"; c.ta.dispatchEvent(new context.Event("input"));
assert.equal(JSON.parse(store.get("snaplab-draft:room:r-00000001")).text, "a long message typed on the phone");
c = ui.buildComposer(room, ui.S.bots[0]);
flush();
assert.equal(c.ta.value, "a long message typed on the phone");
assert.equal(ui.buildComposer({ ...room, id: "r-00000002" }, ui.S.bots[0]).ta.value || "", "", "drafts are per room");

// A failed send keeps the draft and puts the text back; a successful one removes it.
context.reply = () => ({ ok: false, error: "offline" });
const send = c.el.querySelectorAll("button").find((b) => b.className.split(" ").includes("send"));
await send.emit("click");
assert.equal(c.ta.value, "a long message typed on the phone");
assert.ok(store.has("snaplab-draft:room:r-00000001"));
context.reply = () => ({ ok: true });
await send.emit("click");
assert.equal(requests.at(-1).path, "/api/rooms/r-00000001/send");
assert.equal(JSON.parse(requests.at(-1).options.body).text, "a long message typed on the phone");
assert.equal(c.ta.value, "");
assert.ok(!store.has("snaplab-draft:room:r-00000001"), "sent text is no longer a draft");

// Text typed while a send is on its way is not lost when the send finishes.
let finish;
c.ta.value = "first"; c.ta.dispatchEvent(new context.Event("input"));
const pending = ui.sendDraft(c.ta, "room:r-00000001", () => new Promise((r) => { finish = r; }));
c.ta.value = "second"; c.ta.dispatchEvent(new context.Event("input"));
finish(); await pending;
assert.equal(JSON.parse(store.get("snaplab-draft:room:r-00000001")).text, "second");

// Periods of the usage chart: 14 days, 12 weeks from Monday, 12 months; sums from the recorded days.
ui.U.history = { today: "2026-11-15", first_day: "2026-11-01", days: [
  { day: "2026-11-15", calls: 2, input_tokens: 30, output_tokens: 3, errors: 1 },
  { day: "2026-11-10", calls: 1, input_tokens: 10, output_tokens: 1, errors: 0 },
  { day: "2026-10-31", calls: 5, input_tokens: 50, output_tokens: 5, errors: 0 }] };
ui.U.unit = "day"; ui.U.offset = 0;
let bs = ui.usageBuckets();
assert.equal(bs.length, 14);
assert.equal(bs.at(-1).from, "2026-11-15"); assert.equal(bs.at(-1).calls, 2);
ui.U.unit = "week"; bs = ui.usageBuckets();
assert.equal(bs.at(-1).from, "2026-11-09"); assert.equal(bs.at(-1).to, "2026-11-15");   // Monday to Sunday
assert.equal(bs.at(-1).input_tokens, 40);
ui.U.unit = "month"; bs = ui.usageBuckets();
assert.equal(bs.length, 12);
assert.equal(bs.at(-1).from, "2026-11-01"); assert.equal(bs.at(-1).to, "2026-11-30");
assert.equal(bs.at(-2).calls, 5);
ui.U.offset = 1; bs = ui.usageBuckets();
assert.equal(bs.at(-1).from, "2025-11-01");

// The chart: one button per period with its value in words, the chosen one marked.
const picked = [];
const chart = ui.columnChart([{ value: 0, label: "a", title: "A · 0" }, { value: 5, label: "b", title: "B · 5" }], 1, (i) => picked.push(i));
const bars = chart.querySelectorAll("button");
assert.equal(bars.length, 2);
assert.equal(bars[1].attrs["aria-pressed"], "true");
assert.equal(bars[0].attrs["aria-label"], "A · 0");
await bars[0].emit("click");
assert.deepEqual(picked, [0]);
console.log("dash-ux UI: drafts per room survive redraws and failed sends; usage periods and chart passed.");

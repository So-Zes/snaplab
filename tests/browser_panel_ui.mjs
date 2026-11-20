// The browser panel (#/browser/<session>): live frames, Take over, taps and typing (IME text arrives finished).
// Run: node tests/browser_panel_ui.mjs
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { ui, requests, sources } = makeUI();
ui.S.sessions = [{ id: "s-0000abcd", title: "Sign in", harness: "codex" }];
ui.S.sessionDetail = ui.S.sessionDetail || {};
const settle = () => new Promise((r) => setImmediate(r));
const posts = () => requests.filter((r) => r.options.method === "POST" && r.path === "/api/browser/s-0000abcd")
  .map((r) => JSON.parse(r.options.body));

const body = ui.browser("s-0000abcd");
assert.equal(sources.length, 1);
assert.equal(sources[0].url, "/api/browser/s-0000abcd/live");
const canvas = body.querySelector(".bp-canvas");
const typing = body.querySelector(".bp-type");
const chip = body.querySelector(".bp-chip");
const control = body.querySelector(".bp-row").children[1];
const state = (s) => sources[0].handlers.state({ data: JSON.stringify(s) });

// The agent's turn: the page is shown, input is off until Take over.
state({ status: "ready", mode: "turn", holder: "agent", can_input: false, version: 1,
  tabs: [{ id: "T1", url: "https://example.com/login", title: "Log in", active: true }] });
assert.equal(chip.textContent, "browser.agent");
assert.equal(control.textContent, "browser.takeover");
assert.equal(body.querySelector(".bp-url").value, "https://example.com/login");
assert.equal(typing.disabled, true);
sources[0].handlers.frame({ data: JSON.stringify({ seq: 1, w: 1000, h: 500, jpeg: "BBBB" }) });
assert.ok(canvas.drawn, "the frame is drawn");
await canvas.emit("pointerdown", { pointerType: "touch", clientX: 64, clientY: 0 });
await canvas.emit("pointerup", { pointerType: "touch", clientX: 64, clientY: 0 });
await settle();
assert.equal(posts().filter((p) => p.action === "input").length, 0, "no input before Take over");

await control.emit("click");
await settle();
assert.deepEqual(posts().at(-1), { action: "control", holder: "user" });

// The user has control: a tap clicks; finished IME text and Enter go to the page.
state({ status: "ready", mode: "turn", holder: "user", can_input: true, version: 2,
  tabs: [{ id: "T1", url: "https://example.com/login", title: "Log in", active: true }] });
assert.equal(chip.textContent, "browser.you");
assert.equal(control.textContent, "browser.handback");
await canvas.emit("pointerdown", { pointerType: "touch", clientX: 64, clientY: 0 });
await canvas.emit("pointerup", { pointerType: "touch", clientX: 64, clientY: 0 });
await settle();
let input = posts().filter((p) => p.action === "input").flatMap((p) => p.events);
assert.deepEqual(input[0], { t: "tap", x: 0.25, y: 0 });
await typing.emit("compositionstart");
typing.value = "にほんご";
await typing.emit("input", { isComposing: true, inputType: "insertCompositionText", data: "にほんご" });
await typing.emit("compositionend", { data: "日本語" });
await typing.emit("input", { isComposing: false, inputType: "insertCompositionText", data: "日本語" });
await typing.emit("keydown", { key: "Enter", code: "Enter", keyCode: 13 });
await settle(); await settle();
input = posts().filter((p) => p.action === "input").flatMap((p) => p.events);
assert.deepEqual(input.filter((e) => e.t === "text"), [{ t: "text", text: "日本語" }], "composed text is sent once, when finished");
assert.deepEqual(input.filter((e) => e.t === "key").map((e) => [e.type, e.key]), [["down", "Enter"], ["up", "Enter"]]);

// The address bar opens a page.
body.querySelector(".bp-url").value = "example.org";
await body.querySelector(".bp-bar").emit("submit");
await settle();
assert.deepEqual(posts().at(-1), { action: "nav", to: "go", url: "example.org" });

// Leaving the panel closes the stream.
ui.closeBrowserStream();
assert.equal(sources[0].closed, true);
console.log("Browser panel: frames, Take over, taps, IME text, Enter and the address bar work; the stream closes on leave.");

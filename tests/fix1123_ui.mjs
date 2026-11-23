// 2026-11-23: Ctrl+V in the taken-over browser, screens drawn again keep still, waiting questions open and close.
// Run: node tests/fix1123_ui.mjs
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const settle = () => new Promise((r) => setImmediate(r));

{ // Ctrl+V pastes this PC's clipboard into the page as text; the key itself is not sent to the page
  const { ui, requests, sources } = makeUI();
  ui.S.sessions = [{ id: "s-0000abcd", title: "Sign in", harness: "codex" }];
  ui.S.sessionDetail = ui.S.sessionDetail || {};
  const body = ui.browser("s-0000abcd");
  const typing = body.querySelector(".bp-type");
  sources[0].handlers.state({ data: JSON.stringify({ status: "ready", mode: "turn", holder: "user", can_input: true, version: 1, tabs: [] }) });
  let stopped = false;
  await typing.emit("keydown", { key: "v", code: "KeyV", ctrlKey: true, keyCode: 86, preventDefault() { stopped = true; } });
  await typing.emit("paste", { clipboardData: { getData: (type) => (type === "text/plain" ? "copied from the PC" : "") } });
  await settle(); await settle();
  const events = requests.filter((r) => r.options.method === "POST" && r.path === "/api/browser/s-0000abcd")
    .map((r) => JSON.parse(r.options.body)).filter((p) => p.action === "input").flatMap((p) => p.events);
  assert.equal(stopped, false, "Ctrl+V is left to the browser so the paste event fires");
  assert.deepEqual(events, [{ t: "text", text: "copied from the PC" }]);
  // other shortcuts still go to the page
  await typing.emit("keydown", { key: "a", code: "KeyA", ctrlKey: true, keyCode: 65 });
  await settle(); await settle();
  const keys = requests.filter((r) => r.options.method === "POST").map((r) => JSON.parse(r.options.body))
    .filter((p) => p.action === "input").flatMap((p) => p.events).filter((e) => e.t === "key");
  assert.deepEqual(keys.map((e) => [e.type, e.key, e.mods]), [["down", "a", 2], ["up", "a", 2]]);
}

{ // the same screen drawn again gets .again (no entrance animation); another screen does not
  const { ui, document } = makeUI();
  const app = document.createElement("div"); app.setAttribute("id", "app"); document.body.appendChild(app);
  ui.S.route = { name: "usage" };
  ui.screen0({ title: "x" }, document.createElement("div"));
  assert.ok(!app.className.includes("again"));
  ui.screen0({ title: "x" }, document.createElement("div"));
  assert.ok(app.className.includes("again"));
  ui.S.route = { name: "dashboard" };
  ui.screen0({ title: "y" }, document.createElement("div"));
  assert.ok(!app.className.includes("again"));
}

{ // the banner in the chat list opens the waiting questions; a card closes without an answer
  const { context, ui, document, requests } = makeUI();
  const app = document.createElement("div"); app.setAttribute("id", "app"); document.body.appendChild(app);
  const old = new Date(Date.now() - 3 * 86400000).toISOString();
  ui.S.bots = [{ id: "b-00000001", name: "Lead", orchestrator: true }];
  ui.S.questions = [
    { id: "q-00000001", kind: "question", question: "Old one?", status: "open", bot: "b-00000001", room: "r-00000001", created: old, options: [] },
    { id: "q-00000002", kind: "question", question: "New one?", status: "open", bot: "b-00000001", room: "r-00000001", created: new Date().toISOString(), options: [] }];
  ui.S.route = { name: "chats" };
  const body = ui.chats();
  const banner = body.querySelector(".q-banner");
  let leftPage = true;
  const before = ui.activeSheets.length;
  await banner.emit("click", { preventDefault() { leftPage = false; } });
  assert.equal(leftPage, false, "the banner does not jump to a chat");
  assert.equal(ui.activeSheets.length, before + 1, "it opens the waiting questions");
  const sheetBody = document.body.querySelectorAll(".needs-body").at(-1);
  assert.equal(sheetBody.querySelectorAll(".qcard").length, 2);
  assert.equal(sheetBody.querySelectorAll(".needs-dismiss-old").length, 1, "older than a day can be closed at once");
  context.reply = (path, options) => path === "/api/questions/dismiss"
    ? { questions: JSON.parse(options.body).ids.map((id) => ({ ...ui.S.questions.find((q) => q.id === id), status: "dismissed" })) } : {};
  const card = ui.questionCard(ui.S.questions[1]);
  await card.querySelector(".qcard-dismiss").emit("click");
  await settle();
  const sent = requests.filter((r) => r.path === "/api/questions/dismiss").map((r) => JSON.parse(r.options.body));
  assert.deepEqual(sent, [{ ids: ["q-00000002"] }]);
  assert.deepEqual(ui.S.questions.map((q) => q.id), ["q-00000001"]);
  const closed = ui.questionCard({ ...ui.S.questions[0], status: "dismissed" });
  assert.equal(closed.querySelectorAll(".qcard-dismiss").length, 0);
  assert.match(closed.textContent, /q\.dismissed/);
}
console.log("fix1123_ui: paste, still redraws, opening and closing waiting questions work.");

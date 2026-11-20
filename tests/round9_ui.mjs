// Real app functions with a dependency-free DOM double. Device rendering is in e2e/drive.mjs.
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { context, ui, document, requests, timers } = makeUI();
const root = document.createElement("div"); root.setAttribute("id", "app"); document.body.appendChild(root);
const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);
const flush = () => { while (timers.length) timers.shift()(); };
const lastSheet = () => ui.activeSheets.at(-1).panel;
context.location.hash = "";
assert.equal(ui.parseRoute().name, "home");
assert.deepEqual(Array.from(ui.TABS, x => x[0]), ["chats", "home", "dashboard"]);
context.location.hash = "#/session/s-1";
assert.equal(ui.parseRoute().id, "s-1"); // deep links keep working
ui.S.bots = [{ id: "b1", name: "Lead", initials: "L" }, { id: "b2", name: "Builder", initials: "B" }];
ui.S.sessions = [
  { id: "work", title: "Build a game", status: "running", bot: "b2", plan: { done: 4, total: 6 } },
  { id: "wait", title: "A decision", status: "waiting" },
  { id: "review", title: "Ready", status: "review" },
  { id: "failed", title: "Test failed", status: "failed" },
  { id: "idle", title: "Past work", status: "idle" }
];
ui.S.questions = [{ id: "q1", question: "Approve?", room: "dm", bot: "b1" }];
assert.equal(ui.needsCount(), 4);
const home = ui.home();
assert.equal(home.querySelector(".home-attention-number").textContent, "4");
assert.equal(home.querySelectorAll(".home-work-card").length, 1);
assert.match(home.textContent, /Build a game/);
assert.match(home.querySelector(".progress-ring").attrs["aria-label"], /"done":4,"total":6/);
assert.ok(!home.textContent.includes("Past work"));
await home.querySelector(".home-attention").emit("click");
assert.ok(lastSheet().textContent.includes("Approve?"));
assert.equal(lastSheet().querySelectorAll(".sess").length, 3);
ui.activeSheets.at(-1).close(); flush();

const dashboard = ui.dashboard();
assert.equal(dashboard.querySelectorAll("details").length, 6);   // the board and API use are cards now
assert.ok(dashboard.querySelector("#usage-card"));
assert.ok(dashboard.querySelectorAll("details").every(n => n.attrs.open === undefined));
assert.equal(dashboard.querySelectorAll(".sess").length, 5);
const workFold = dashboard.querySelectorAll("details").find(x => x.dataset.section === "work");
workFold.open = true; await workFold.emit("toggle");
assert.equal(ui.dashboard().querySelectorAll("details").find(x => x.dataset.section === "work").attrs.open, "");

ui.S.tasks = [{ id: "daily", status: "active", title: "News", bot: "b1", upcoming: ["2026-11-01T08:00:00", "2026-11-02T08:00:00"] },
  { id: "paused", status: "paused", title: "Hidden", upcoming: ["2026-11-01T09:00:00"] }];
const agenda = ui.homeAgenda(new Date("2026-11-01T12:00:00"));
assert.equal(agenda.querySelectorAll(".agenda-day").length, 2);
assert.equal(agenda.querySelectorAll(".time-stop").length, 2);
assert.ok(!agenda.textContent.includes("Hidden"));

ui.S.rooms = [
  { id: "dm", name: "Lead", kind: "dm", bots: ["b1"], pinned: true, last_ts: "2026-10-01" },
  { id: "group", name: "Team", kind: "group", bots: ["b1", "b2"], pinned: true, last_ts: "2026-10-02" },
  { id: "dm2", name: "Builder", kind: "dm", bots: ["b2"], last_ts: "2026-10-03" },
  { id: "group2", name: "Other team", kind: "group", bots: ["b2"], last_ts: "2026-10-04" }
];
const chats = ui.chats();
const sections = chats.querySelectorAll("[data-chat-section]");
assert.deepEqual(sections.map(x => x.dataset["chat-section"]), ["chats.pinned", "chats.bots", "chats.groups"]);
assert.equal(sections[0].querySelectorAll(".chat-row")[0].attrs.href, "#/room/group");
assert.equal(chats.querySelectorAll(".pin-chat").length, 4);
context.reply = (path, options) => ({ room: { ...ui.S.rooms.find(r => path.endsWith(r.id)), ...JSON.parse(options.body) } });
await chats.querySelectorAll(".pin-chat")[0].emit("click");
assert.equal(JSON.parse(requests.at(-1).options.body).pinned, false);
assert.equal(ui.S.rooms.find(r => r.id === "group").pinned, false);

// Every dismissal path unlocks the page; a nested sheet keeps its parent locked.
let closes = 0;
const a = ui.sheet("Parent", document.createElement("p"), () => { closes++; });
assert.ok(document.documentElement.className.includes("sheet-open")); assert.equal(root.inert, true);
const b = ui.sheet("Child", []);
assert.equal(a.panel.inert, true);
b.close(); b.close(); flush(); assert.equal(a.panel.inert, false); assert.equal(root.inert, true);
let grip = a.panel.querySelector(".sheet-grip");
await grip.emit("pointerdown", { pointerId: 1, clientY: 100 });
await grip.emit("pointermove", { pointerId: 1, clientY: 140 });
await grip.emit("pointerup", { pointerId: 1 }); assert.equal(ui.activeSheets.length, 1);
await grip.emit("pointerdown", { pointerId: 1, clientY: 100 });
await grip.emit("pointermove", { pointerId: 1, clientY: 210 });
await grip.emit("pointercancel", { pointerId: 1 }); assert.equal(ui.activeSheets.length, 1);
await grip.emit("pointerdown", { pointerId: 1, clientY: 100 });
await grip.emit("pointermove", { pointerId: 1, clientY: 210 });
await grip.emit("pointerup", { pointerId: 1 }); flush();
assert.equal(closes, 1); assert.equal(root.inert, false); assert.equal(ui.activeSheets.length, 0);
ui.sheet("Outside", []); await document.querySelector(".scrim").emit("click"); flush();
assert.equal(ui.activeSheets.length, 0);
ui.sheet("Close", []); await lastSheet().querySelector("button").emit("click"); flush();
assert.equal(ui.activeSheets.length, 0);
ui.sheet("Escape", []); await document.emit("keydown", { key: "Escape" }); flush();
assert.equal(ui.activeSheets.length, 0);

// Save group membership/name together. Cancelling deletion sends nothing.
ui.membersSheet(ui.S.rooms.find(r => r.id === "group"));
const sh = lastSheet(); sh.querySelector(".group-name").value = "Game makers";
const checks = sh.querySelectorAll("input").filter(x => x.attrs.type === "checkbox");
checks[0].checked = false; await checks[0].emit("change");
await sh.querySelector(".group-save").emit("click"); flush();
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { name: "Game makers", bots: ["b2"] });
ui.membersSheet(ui.S.rooms.find(r => r.id === "group"));
const before = requests.length;
const pending = lastSheet().querySelector(".group-delete").emit("click");
assert.equal(ui.activeSheets.length, 2);
ui.activeSheets.at(-1).close(); await pending; flush();
assert.equal(requests.length, before); assert.equal(ui.activeSheets.length, 1);
const deleting = lastSheet().querySelector(".group-delete").emit("click");
await lastSheet().querySelectorAll("button").at(-1).emit("click"); await deleting; flush();
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { action: "delete", confirmed: true });
assert.ok(!ui.S.rooms.some(r => r.id === "group"));
assert.equal(context.location.hash, "#/chats");
assert.equal(root.inert, false);
console.log("Round 9 UI: home, tabs, attention, timeline, dashboard folds, pins, group editing/deletion, and sheet gestures passed.");

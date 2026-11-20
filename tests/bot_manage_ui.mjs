// The bot list's long-press menu, deleting a bot, and the context cleanup card (2026-11-16).
// Dependency-free (see ui_harness.mjs). The real touch gesture on a phone is checked in a browser.
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { context, ui, document, requests, timers } = makeUI();
const root = document.createElement("div"); root.setAttribute("id", "app"); document.body.appendChild(root);
const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);
const flush = () => { while (timers.length) timers.shift()(); };
const lastSheet = () => ui.activeSheets.at(-1).panel;
const closeAll = () => { while (ui.activeSheets.length) ui.activeSheets.at(-1).close(); flush(); };
const texts = (el) => el.querySelectorAll("button").map((b) => b.textContent);
context.reply = () => ({ ok: true });
context.location.hash = "#/bots";
ui.S.route = ui.parseRoute();
ui.S.bots = [{ id: "b1", name: "Lead", initials: "L", orchestrator: true, projects: [] },
             { id: "b2", name: "Mika", initials: "M", projects: [] }];
ui.S.rooms = [{ id: "r1", kind: "dm", bots: ["b1"], name: "Lead" }, { id: "r2", kind: "dm", bots: ["b2"], name: "Mika" }];
ui.S.sessions = [{ id: "s-1", started_by: "b2", status: "running" }];
ui.S.tasks = [{ id: "t-1", bot: "b2" }];

// A long press opens the menu; the click that ends it does not open the bot as well.
let body = ui.bots();
const card = body.querySelectorAll("a").find((a) => a.attrs["data-bot"] === "b2");
assert.ok(card, "each bot card says which bot it is");
assert.ok(body.querySelector(".bots-hold"), "the list says that a long press opens a menu");
await card.emit("pointerdown", { button: 0, clientX: 10, clientY: 10 });
assert.equal(ui.activeSheets.length, 0, "nothing before the press is long enough");
flush();
assert.equal(ui.activeSheets.length, 1);
let prevented = false;
await card.emit("click", { preventDefault: () => { prevented = true; } });
assert.ok(prevented, "the click after a long press is swallowed");
prevented = false;
await card.emit("click", { preventDefault: () => { prevented = true; } });
assert.ok(!prevented, "the next ordinary tap opens the bot as usual");
closeAll();

// Moving the finger (scrolling) cancels the long press.
await card.emit("pointerdown", { button: 0, clientX: 10, clientY: 10 });
await card.emit("pointermove", { clientX: 10, clientY: 60 });
flush();
assert.equal(ui.activeSheets.length, 0);

// A right click with a mouse opens the same menu.
await card.emit("contextmenu");
assert.equal(ui.activeSheets.length, 1);
const menu = texts(lastSheet());
for (const key of ["bot.chat", "botmenu.inside", "botmenu.context", "inside.profile", "botmenu.archive"]) {
  assert.ok(menu.some((x) => x.includes(key)), key + " is in the menu");
}
closeAll();

assert.ok(!menu.some((x) => x.includes("delete")), "the menu archives; it does not delete (2026-11-17)");
closeAll();

// The lead bot cannot be archived, so its menu has no archive button.
ui.botMenu(ui.S.bots[0]);
assert.ok(!texts(lastSheet()).some((x) => x.includes("botmenu.archive")));
assert.ok(lastSheet().textContent.includes("botmenu.lead"));
closeAll();

// Archiving asks first, says what stops and what pauses, and then puts the bot and its chat away.
ui.S.tasks = [{ id: "t-1", bot: "b2", status: "active" }];
ui.botMenu(ui.S.bots[1]);
assert.ok(lastSheet().querySelectorAll("button").some((b) => b.textContent.includes("botmenu.archive")));
closeAll();
const before = requests.length;
const cancelled = ui.archiveBot(ui.S.bots[1]);
assert.ok(lastSheet().textContent.includes("botarc.running"), "running work is mentioned");
assert.ok(lastSheet().textContent.includes("botarc.tasks"), "paused tasks are mentioned");
assert.ok(lastSheet().textContent.includes("botarc.back"), "it says the bot can come back");
ui.activeSheets.at(-1).close();
assert.equal(await cancelled, false);
assert.equal(requests.length, before, "cancelling sends nothing");
const archiving = ui.archiveBot(ui.S.bots[1]);
await lastSheet().querySelectorAll("button").at(-1).emit("click");
assert.equal(await archiving, true);
assert.equal(requests.at(-1).path, "/api/bots/b2");
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { action: "archive", confirmed: true });
assert.deepEqual(ui.S.bots.map((b) => b.id), ["b1"]);
assert.ok(!ui.S.rooms.some((r) => r.id === "r2"));
assert.equal(ui.S.archivedBots.at(-1).name, "Mika", "old messages keep its name");
closeAll();

// The bot list shows it under Archived with a button that brings it back.
body = ui.bots();
const archivedRow = body.querySelectorAll("div").find((d) => d.attrs["data-bot"] === "b2");
assert.ok(archivedRow && archivedRow.className.includes("archived-row"));
assert.ok(body.textContent.includes("bots.archived"));
context.reply = (path, options) => (path === "/api/bots/b2" ? { ok: true, restored: "b2", bot: { id: "b2", name: "Mika", initials: "M", projects: [] } } : { ok: true });
await archivedRow.querySelector(".restore-bot").emit("click");
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { action: "restore" });
assert.deepEqual(ui.S.bots.map((b) => b.id), ["b1", "b2"]);
assert.equal(ui.S.archivedBots.length, 0);
context.reply = () => ({ ok: true });
ui.S.archivedBots = [{ id: "b2", name: "Mika", initials: "M", archived_at: "2026-11-17T10:00:00+09:00" }];
ui.S.bots = ui.S.bots.filter((b) => b.id !== "b2");

// Its old messages in a group still show its name.
ui.S.rooms.push({ id: "g1", kind: "group", bots: ["b1"], name: "Team" });
const old = ui.messageEl({ id: "m-1", role: "bot", bot: "b2", text: "hello", status: "done", room: "g1", ts: "2026-11-16T10:00:00+09:00" });
assert.ok(old.textContent.includes("Mika"));

// The cleanup card: ask the bot in words, or start afresh yourself.
const wi = { id: "b2", name: "Mika", initials: "M", projects: [] };
let cardEl = ui.freshCard(wi, { room: "r2", room_kind: "dm", room_name: "Mika", fresh_start: null });
const areas = cardEl.querySelectorAll("textarea");
areas[0].value = "Keep only round 7 and later.";
const ask = cardEl.querySelectorAll("button").find((b) => b.textContent.includes("fresh.ask"));
await ask.emit("click");
assert.equal(requests.at(-1).path, "/api/rooms/r2/send");
assert.ok(JSON.parse(requests.at(-1).options.body).text.includes("Keep only round 7 and later."));
assert.equal(context.location.hash, "#/room/r2", "the chat opens so the user sees the bot do it");
areas[1].value = "Round 7 runs on WSL.";
const self = cardEl.querySelectorAll("button").find((b) => b.textContent.includes("fresh.self"));
const starting = self.emit("click");
await lastSheet().querySelectorAll("button").at(-1).emit("click");
await starting;
assert.equal(requests.at(-1).path, "/api/rooms/r2/fresh");
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { bot: "b2", note: "Round 7 runs on WSL." });
closeAll();

// After a fresh start the card shows what was kept and can undo it.
cardEl = ui.freshCard(wi, { room: "r2", room_kind: "dm", room_name: "Mika",
  fresh_start: { at: "2026-11-16T15:00:00.000+09:00", note: "Round 7 runs on WSL.", by: "you" } });
assert.ok(cardEl.textContent.includes("Round 7 runs on WSL."));
const undo = cardEl.querySelectorAll("button").find((b) => b.textContent.includes("fresh.undo"));
const undoing = undo.emit("click");
await lastSheet().querySelectorAll("button").at(-1).emit("click");
await undoing;
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { undo: true });
closeAll();
assert.ok(ui.freshCard(wi, { room: "g1", room_kind: "group", room_name: "Team" }).textContent.includes("fresh.d.group"),
  "in a group the card says it applies to every bot there");

// The memory card can forget everything at once (after asking).
context.reply = (path) => (path.endsWith("/memory") ? { ok: true, memory: "- [2026-11-16] old note\n", limit: 4000 } : { ok: true });
const mem = ui.memoryCard(wi);
await mem.load(); flush();
context.reply = () => ({ ok: true, memory: "" });
const clear = mem.el.querySelectorAll("button").find((b) => b.textContent.includes("bot.memory.clear"));
const clearing = clear.emit("click");
await lastSheet().querySelectorAll("button").at(-1).emit("click");
await clearing; flush();
assert.equal(requests.at(-1).path, "/api/bots/b2/memory");
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { text: "" });

// The chat list (the screen most people mean by "the bots"): a long press on a bot's chat opens the same menu, a long
// press on a group opens its members and delete sheet, and the browser's own link menu never opens (2026-11-17).
closeAll();
ui.S.bots = [{ id: "b1", name: "Lead", initials: "L", orchestrator: true, projects: [] }, { id: "b3", name: "Rox", initials: "R", projects: [] }];
ui.S.rooms = [{ id: "r1", kind: "dm", bots: ["b1"], name: "Lead" }, { id: "r3", kind: "dm", bots: ["b3"], name: "Rox" },
              { id: "g2", kind: "group", bots: ["b1", "b3"], name: "Team" }];
ui.S.questions = []; ui.S.busy = new Set();
body = ui.chats();
const rowOf = (id) => body.querySelectorAll("a").find((a) => a.attrs["data-room"] === id);
const rox = rowOf("r3");
assert.ok(rox, "each chat row says which chat it is");
assert.equal(rox.attrs.draggable, "false", "a held link is not dragged away");
assert.ok(rox.className.includes("holdable"), "a held row selects no text and shows no callout");
await rox.emit("pointerdown", { button: 0, clientX: 5, clientY: 5 });
flush();
assert.equal(ui.activeSheets.length, 1);
assert.ok(texts(lastSheet()).some((x) => x.includes("botmenu.archive")), "the bot can be archived from the chat list");
prevented = false;
await rox.emit("contextmenu", { preventDefault: () => { prevented = true; } });
assert.ok(prevented, "Chrome's own menu for the link does not open");
assert.equal(ui.activeSheets.length, 1, "the menu opens only once when the browser also reports the long press");
closeAll();
prevented = false;
await rowOf("r1").emit("contextmenu", { preventDefault: () => { prevented = true; } });
assert.ok(prevented && lastSheet().textContent.includes("botmenu.lead"), "the lead's menu says it cannot be archived");
closeAll();
await rowOf("g2").emit("contextmenu");
assert.ok(lastSheet().querySelector(".group-delete"), "a group's long press offers its members and deleting it");
closeAll();
ui.S.archivedBots = [{ id: "b9", name: "Old", initials: "O" }];
const link = ui.chats().querySelectorAll("a").find((a) => (a.attrs.href || "") === "#/bots?archived=1");
assert.ok(link && link.textContent.includes("chats.archived"), "the chat list says where archived bots are");

// The dashboard's row of faces opens the same menu.
ui.S.dashboard = { widgets: [], hidden: [] };
const dash = ui.dashboard();
const face = dash.querySelectorAll("a").find((a) => a.attrs["data-bot"] === "b3");
assert.ok(face, "each face says which bot it is");
await face.emit("contextmenu");
assert.ok(texts(lastSheet()).some((x) => x.includes("botmenu.archive")));
closeAll();

console.log("Bot management UI: long press menu (bot list, chat list, dashboard), archiving and bringing back, old names, context cleanup card and forgetting memory passed.");

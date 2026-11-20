// Replay events captured by the Python race test through the real UI handler.
import assert from "node:assert/strict";
import fs from "node:fs";
import { makeUI } from "./ui_harness.mjs";

const { rooms, bots, deleted, events } = JSON.parse(fs.readFileSync(0, "utf8"));
const { ui } = makeUI();
ui.S.rooms = rooms;
ui.S.bots = bots;
ui.S.route = { name: "chats" };
const hasRoomRow = () => ui.chats().querySelectorAll(".chat-row").some(row => row.attrs.href === "#/room/" + deleted);
assert.ok(ui.S.rooms.some(room => room.id === deleted));
assert.ok(hasRoomRow());
let deletionSeen = false;
for (const event of events) {
  ui.onEvent(event);
  if (event.type === "rooms.updated" && !event.rooms.some(room => room.id === deleted)) deletionSeen = true;
  if (deletionSeen) {
    assert.ok(!ui.S.rooms.some(room => room.id === deleted), "A later event restored the deleted room");
    assert.ok(!hasRoomRow(), "The deleted room is visible in the rendered list");
  }
}
assert.ok(deletionSeen, "The deletion notification was not replayed");
assert.deepEqual(Array.from(ui.S.rooms, room => room.id).sort(), rooms.filter(room => room.id !== deleted).map(room => room.id).sort());
console.log("Deleted room stays absent after replaying server events.");

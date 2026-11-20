// Sending photos and files from the chat: the 📎 menu, the PC camera, uploads with progress, × to take one back,
// pasted and dropped files, sending with or without text, and how sent files look. Dependency-free (ui_harness.mjs).
import assert from "node:assert/strict";
import { makeUI } from "./ui_harness.mjs";

const { context, ui, requests, timers, document } = makeUI();
const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);
const app = document.createElement("div"); app.setAttribute("id", "app"); document.body.appendChild(app);
const store = new Map();
context.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
context.Event = class { constructor(type) { this.type = type; } };
context.Node.prototype.dispatchEvent = function (ev) { (this.events[ev.type] || []).forEach((fn) => fn(ev)); return true; };
const xhrs = [];
context.XMLHttpRequest = class {
  constructor() { this.headers = {}; this.upload = {}; xhrs.push(this); }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) { this.body = body; }
  abort() { this.aborted = true; this.onabort && this.onabort(); }
};
context.URL = { createObjectURL: (f) => "blob:" + f.name, revokeObjectURL() {} };
const file = (name, type, size = 2048) => ({ name, type, size });
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
const finish = (x, id) => { x.status = 200; x.responseText = JSON.stringify({ ok: true, file: { id } }); x.onload(); };
const all = (el) => [el, ...(el.children || []).flatMap(all)];
const byClass = (el, name) => all(el).filter((n) => (n.className || "").split(/\s+/).includes(name));
const room = { id: "r-00000001", kind: "dm", bots: ["b1"], name: "Lead" };
ui.S.bots = [{ id: "b1", name: "Lead", initials: "L" }];
const ID1 = "f-" + "1".repeat(32), ID2 = "f-" + "2".repeat(32);

const c = ui.buildComposer(room, ui.S.bots[0]);
const send = byClass(c.el, "send")[0];

// The 📎 button opens a menu with the camera, photos and files; three file inputs back it.
const attach = byClass(c.el, "attach-btn")[0];
const menu = byClass(c.el, "attach-menu")[0];
assert.equal(menu.hidden, true);
await attach.emit("click");
assert.equal(menu.hidden, false);
assert.equal(attach.attrs["aria-expanded"], "true");
const items = byClass(menu, "attach-item");
assert.deepEqual(items.map((b) => b.children[1].textContent), ["composer.camera", "composer.photos", "composer.files"]);
const inputs = byClass(c.el, "attach-input");
assert.equal(inputs.length, 3);
assert.deepEqual([inputs[0].attrs.accept, inputs[0].attrs.capture], ["image/*", "environment"], "the phone's camera app");
assert.equal(inputs[1].attrs.accept, "image/*,video/*");
assert.ok(Object.hasOwn(inputs[2].attrs, "multiple") && !inputs[2].attrs.accept, "any file");
let clicked = 0; inputs[2].click = () => clicked++;
await items[2].emit("click");
assert.equal(clicked, 1); assert.equal(menu.hidden, true, "picking closes the menu");

// A picked file uploads at once with its name, shows progress, and the send button becomes ready.
inputs[2].files = [file("見積もり.pdf", "application/pdf", 300000)];
await inputs[2].emit("change"); await tick();
assert.equal(xhrs.length, 1);
assert.equal(xhrs[0].url, "/api/rooms/r-00000001/files");
assert.equal(xhrs[0].headers["X-SnapLab"], "1");
assert.equal(decodeURIComponent(xhrs[0].headers["X-SnapLab-Name"]), "見積もり.pdf");
assert.equal(xhrs[0].headers["Content-Type"], "application/pdf");
assert.equal(c.tray.el.hidden, false);
assert.ok(send.className.includes("ready"), "a file alone can be sent");
xhrs[0].upload.onprogress({ lengthComputable: true, loaded: 150000, total: 300000 });
assert.equal(byClass(c.tray.el, "file-state")[0].textContent, 'composer.uploading{"p":50}');

// Sending while it uploads waits for it, then sends the text with the file id.
c.ta.value = "これを見て"; c.ta.dispatchEvent(new context.Event("input"));
context.reply = () => ({ ok: true });
const sending = send.emit("click");
await tick();
assert.ok(!requests.some((r) => r.path.endsWith("/send")), "waits for the upload");
finish(xhrs[0], ID1);
await sending;
const sent = requests.find((r) => r.path === "/api/rooms/r-00000001/send");
assert.deepEqual(JSON.parse(sent.options.body), { text: "これを見て", files: [ID1] });
assert.equal(c.tray.count(), 0, "the tray empties after the send");
assert.equal(c.ta.value, "");

// × takes a file back: the upload stops, or the stored copy is discarded.
c.tray.add([file("a.png", "image/png"), file("b.txt", "text/plain")]); await tick();
assert.equal(byClass(c.tray.el, "file-chip").length, 2);
assert.equal(byClass(c.tray.el, "file-thumb")[0].children[0].attrs.src, "blob:a.png", "a picture shows itself");
finish(xhrs[1], ID2);
await byClass(c.tray.el, "file-x")[0].emit("click");
assert.equal(requests.at(-1).path, "/api/files/" + ID2 + "/discard");
await byClass(c.tray.el, "file-x")[0].emit("click");
assert.equal(xhrs[2].aborted, true);
assert.equal(c.tray.count(), 0);

// A failed upload stays visible with a retry; sending is refused until it is fixed or removed.
c.tray.add([file("big.mov", "video/quicktime")]); await tick();
xhrs[3].status = 500; xhrs[3].responseText = "{}"; xhrs[3].onload();
assert.ok(byClass(c.tray.el, "file-chip")[0].className.includes("failed"));
const before = requests.length;
await send.emit("click");
assert.equal(requests.length, before, "nothing is sent with a failed file");
await byClass(c.tray.el, "file-state")[0].emit("click");
assert.equal(xhrs.length, 5, "retry uploads again");
finish(xhrs[4], ID1);
await send.emit("click");
assert.deepEqual(JSON.parse(requests.at(-1).options.body), { text: "", files: [ID1] }, "files without text");

// A pasted screenshot becomes a file; copied text stays text.
let prevented = false;
await c.ta.emit("paste", { clipboardData: { files: [file("image.png", "image/png")], getData: () => "" }, preventDefault: () => { prevented = true; } });
await tick();
assert.ok(prevented); assert.equal(c.tray.count(), 1);
prevented = false;
await c.ta.emit("paste", { clipboardData: { files: [file("cells.png", "image/png")], getData: () => "A1\tB1" }, preventDefault: () => { prevented = true; } });
assert.ok(!prevented); assert.equal(c.tray.count(), 1, "text from a spreadsheet is pasted as text");

// Files dropped on the chat are attached; the chat shows where to drop while dragging.
const main = document.createElement("main");
c.dropOn(main);
assert.equal(main.dataset.drop, "composer.drop");
const dt = { types: ["Files"], files: [file("data.csv", "text/csv")] };
await main.emit("dragenter", { dataTransfer: dt });
assert.ok(main.className.includes("dropping"));
await main.emit("drop", { dataTransfer: dt }); await tick();
assert.ok(!main.className.includes("dropping"));
assert.equal(c.tray.count(), 2);
await main.emit("dragenter", { dataTransfer: { types: ["text/plain"] } });
assert.ok(!main.className.includes("dropping"), "dragging text is not a file");

// On a PC the camera opens in the app; the photo is attached like a picked file.
const tracks = [];
context.navigator.mediaDevices = {
  getUserMedia: async (want) => { assert.equal(want.audio, false); return { getTracks: () => [{ stop() { tracks.push("stopped"); } }] }; },
  enumerateDevices: async () => [{ kind: "videoinput", deviceId: "a" }, { kind: "videoinput", deviceId: "b" }],
};
context.File = class { constructor(parts, name, opts) { this.name = name; this.type = opts.type; this.size = 10; } };
await attach.emit("click");
await byClass(menu, "attach-item")[0].emit("click"); await tick();
const panel = ui.activeSheets.at(-1).panel;
const video = all(panel).find((n) => n.tag === "video");
assert.ok(video.srcObject, "the camera shows in the sheet");
const buttons = all(panel).filter((n) => n.tag === "button" && (n.className || "").split(" ").includes("btn"));
const [flip, retake, shoot, use] = buttons;
assert.equal(flip.hidden, false, "two cameras can be switched");
video.videoWidth = 1280; video.videoHeight = 720;
context.document.createElement = ((make) => (tag) => { const n = make(tag); if (tag === "canvas") n.toBlob = (cb) => cb({ size: 10 }); return n; })(context.document.createElement);
await shoot.emit("click");
assert.equal(use.hidden, false); assert.equal(retake.hidden, false);
const count = c.tray.count();
await use.emit("click"); await tick();
assert.equal(c.tray.count(), count + 1);
assert.match(c.tray.el.querySelectorAll(".file-name").at(-1).textContent, /^photo-\d{8}-\d{6}\.jpg$/);
flush();
assert.ok(tracks.includes("stopped"), "closing the camera turns it off");

// What the user sent: photos as pictures and other files as cards, with the text below.
const shown = ui.messageEl({ id: "m-1", role: "user", text: "", ts: "2026-11-16T10:00:00", attachments: [
  { id: ID1, name: "IMG_1.jpg", kind: "image", size: 900000 }, { id: ID2, name: "plan.pdf", kind: "pdf", size: 300000 }] });
const photo = byClass(shown, "my-photo")[0];
assert.equal(photo.children[0].attrs.src, "/api/attachments/" + ID1 + "/content");
const card = byClass(shown, "my-file")[0];
assert.equal(card.attrs.href, "/api/attachments/" + ID2 + "/preview");
assert.equal(byClass(shown, "bubble").length, 0, "no empty bubble for a message without text");
assert.equal(byClass(ui.messageEl({ id: "m-2", role: "user", text: "hi", ts: "2026-11-16T10:00:00" }), "bubble").length, 1);
console.log("chat files UI: menu, camera, uploads with progress and retry, paste, drop, send with files and the sent view passed.");

function flush() { while (timers.length) timers.shift()(); }

// Dependency-free checks of real UI handlers with a small DOM/canvas test double.
// Run: node tests/round7_ui.mjs. Real pixel rendering and device gestures remain browser checks.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

class Element {
  constructor(tag = "span") {
    this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this.dataset = {}; this.className = "";
    this.style = { setProperty: (key, value) => { this.style[key] = value; } };
    this.classList = {
      add: (...names) => { this.className += " " + names.join(" "); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter((x) => !names.includes(x)).join(" "); },
      toggle: (name, on) => on ? this.classList.add(name) : this.classList.remove(name),
    };
  }
  setAttribute(key, value) { this.attrs[key] = String(value); if (key === "value") this.value = value; if (key.startsWith("data-")) this.dataset[key.slice(5)] = value; }
  focus() { document.activeElement = this; }
  getClientRects() { return [{}]; }
  removeEventListener(type, fn) { this.events[type] = (this.events[type] || []).filter(x => x !== fn); }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  async emit(type, data = {}) { for (const fn of this.events[type] || []) await fn({ preventDefault() {}, type, ...data }); }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  prepend(node) { this.children.unshift(node); node.parentNode = this; }
  replaceChildren(...nodes) { this.children = []; nodes.forEach((node) => this.appendChild(node)); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((n) => n !== this); }
  matches(sel) {
    if (sel.startsWith(".")) return this.className.split(/\s+/).includes(sel.slice(1));
    if (sel.startsWith("#")) return this.attrs.id === sel.slice(1);
    if (sel.startsWith("[")) return Object.hasOwn(this.attrs, sel.slice(1, -1));
    return this.tag === sel;
  }
  querySelectorAll(sel) { return this.children.flatMap((n) => [...(n.matches(sel) ? [n] : []), ...n.querySelectorAll(sel)]); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(node) { return node === this || this.children.some((n) => n.contains(node)); }
  set innerHTML(value) { this.children = []; this.html = value; }
  set textContent(value) { this.children = []; this.text = value; }
  get textContent() { return (this.text || "") + this.children.map((n) => n.textContent).join(""); }
  getBoundingClientRect() { return { width: 256 }; }
  setPointerCapture() {}
  getContext() { return { clearRect() {}, drawImage: (...args) => { this.drawn = args.slice(1); } }; }
  toDataURL(mime) { assert.equal(mime, "image/png"); assert.equal(this.attrs.width, "256"); assert.equal(this.attrs.height, "256"); return "data:image/png;base64,PIXELS_ONLY"; }
}

const document = new Element("document");
document.documentElement = document.appendChild(new Element("html"));
document.body = document.documentElement.appendChild(new Element("body"));
document.createElement = (tag) => new Element(tag);
document.createTextNode = (value) => { const node = new Element("text"); node.textContent = value; return node; };
const requests = [];
const timers = [];
const uploaded = "upload-" + "a".repeat(64) + ".png";
const context = vm.createContext({
  document, Node: Element, navigator: {}, location: { hash: "#/chats" },
  window: { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }), snapT: (_, key, vars) => key + (vars ? JSON.stringify(vars) : "") },
  requestAnimationFrame: (fn) => fn(), setTimeout: (fn) => { timers.push(fn); }, setInterval() {},
  Image: class { naturalWidth = 1024; naturalHeight = 512; set src(value) { this.onload(); } },
  FileReader: class { readAsDataURL() { this.result = "data:image/jpeg;base64,SOURCE_WITH_EXIF"; this.onload(); } },
  fetch: async (path, options) => { requests.push({ path, options }); return { ok: true, json: async () => ({ avatar: uploaded }) }; },
});
let source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
assert.ok(source.includes("  start();\n})();"));
source = source.replace("  start();\n})();", `
  globalThis.ui = { S, avatar, avatarPicker, cropAvatar, applyTheme, recommendedFace,
    dashboard: () => { let body; screen = (_, b) => { body = b; }; renderUsageCard = () => {}; renderDashboard(); return body; }
  };
})();`);
vm.runInContext(source, context);
const { ui } = context;
ui.S.avatars = JSON.parse(fs.readFileSync(new URL("../web/avatars/avatars.json", import.meta.url), "utf8"));
ui.S.bots = [];

// The selected built-in face, old initials, and lead badge work independently.
const lead = ui.avatar({ name: "Lead", initials: "L", color: "#123456", avatar: "c1", orchestrator: true }, "xl");
assert.equal(lead.querySelector(".avatar-image").attrs.src, "/avatars/c/c1-256.png");
assert.ok(lead.querySelector(".avatar-lead"));
assert.equal(ui.avatar({ initials: "A" }).textContent, "A");
assert.equal(ui.recommendedFace("Code"), "c4");
assert.equal(ui.recommendedFace("文章"), "c5");
ui.S.settings = { icon_style: "soft3d", theme: "dark" };
ui.applyTheme();
assert.equal(document.documentElement.dataset.iconStyle, "soft3d");
assert.equal(document.documentElement.dataset.iconTheme, "dark");
ui.S.settings = { icon_style: "line", theme: "system" }; ui.applyTheme();
assert.equal(document.documentElement.dataset.iconStyle, "line");
assert.equal(document.documentElement.dataset.iconTheme, "light");

// Choosing a face is a draft until Use; closing leaves the parent form untouched.
let selected = "c1";
ui.avatarPicker({ name: "Lead", avatar: selected }, (value) => { selected = value; });
assert.equal(document.querySelectorAll(".face-choice").length, 35);
const pick = document.querySelectorAll(".face-choice").find((n) => n.dataset.face === "e8");
await pick.emit("click");
assert.equal(selected, "c1");
assert.equal(pick.attrs["aria-pressed"], "true");
await document.querySelector(".face-save").emit("click");
assert.equal(selected, "e8");
document.body.replaceChildren(); timers.length = 0;

// The real crop handlers clamp panning and zoom; only the 256px canvas is uploaded.
ui.cropAvatar({ size: 1000 }, (value) => { selected = value; });
const canvas = document.querySelector("canvas");
const range = document.querySelector("input");
assert.deepEqual(Array.from(canvas.drawn), [256, 0, 512, 512, 0, 0, 256, 256]);
range.value = 2; await range.emit("input");
assert.equal(canvas.drawn[2], 256);
await canvas.emit("pointerdown", { pointerId: 1, clientX: 100, clientY: 100 });
await canvas.emit("pointermove", { pointerId: 1, clientX: 172, clientY: 100 });
assert.equal(canvas.drawn[0], 312);
await canvas.emit("pointerdown", { pointerId: 2, clientX: 272, clientY: 100 });
await canvas.emit("pointermove", { pointerId: 2, clientX: 372, clientY: 100 });
assert.equal(canvas.drawn[2], 128); // pinch doubles zoom again
await canvas.emit("pointercancel", { pointerId: 1 });
await canvas.emit("pointerup", { pointerId: 2 });
await canvas.emit("keydown", { key: "ArrowLeft" });
assert.ok(canvas.drawn[0] >= 0 && canvas.drawn[0] + canvas.drawn[2] <= 1024);
const save = document.querySelectorAll("button").find((n) => n.textContent === "avatar.use");
await save.emit("click");
assert.equal(requests.length, 1);
assert.equal(requests[0].path, "/api/avatars");
assert.deepEqual(JSON.parse(requests[0].options.body), { image: "PIXELS_ONLY" });
assert.equal(selected, uploaded);

// Active work remains first even with more urgent review items; show every active item on expansion.
ui.S.sessions = [
  { id: "s-review", title: "Review me", status: "review" },
  ...Array.from({ length: 4 }, (_, i) => ({ id: "s-" + i, title: "Active " + i, status: "running", plan: { done: 4, total: 6, now: "Next step" } })),
];
const dashboard = ui.dashboard();
const work = dashboard.querySelectorAll("details").find((n) => n.dataset.section === "work");
assert.equal(work.querySelectorAll(".sess").length, 4);
assert.ok(!work.textContent.includes("Review me"));
assert.equal(dashboard.querySelectorAll(".kpi").length, 0);
assert.ok(dashboard.querySelector(".team-card"));
assert.ok(dashboard.querySelectorAll("details").every(n => !n.attrs.open));
console.log("Round 7 UI: faces, draft selection, theme, crop gestures, canvas-only upload, and dashboard passed.");

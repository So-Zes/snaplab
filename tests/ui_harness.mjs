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
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument && this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null; }
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

export function makeUI() {
const document = new Element("document");
document.documentElement = document.appendChild(new Element("html"));
document.body = document.documentElement.appendChild(new Element("body"));
document.createElement = (tag) => { const node = new Element(tag); node.ownerDocument = document; return node; };
document.createTextNode = (value) => { const node = new Element("text"); node.textContent = value; return node; };
const requests = [];
const timers = [];
const sources = [];
const uploaded = "upload-" + "a".repeat(64) + ".png";
const context = vm.createContext({
  document, Node: Element, navigator: {}, location: { hash: "#/chats" },
  window: { addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }), snapT: (_, key, vars) => key === "lang.locale" ? "en-US" : key + (vars ? JSON.stringify(vars) : "") },
  requestAnimationFrame: (fn) => fn(), setTimeout: (fn) => { timers.push(fn); return fn; }, setInterval() {},
  clearTimeout: (fn) => { const i = timers.indexOf(fn); if (i >= 0) timers.splice(i, 1); },
  Image: class { naturalWidth = 1024; naturalHeight = 512; set src(value) { this.onload(); } },
  EventSource: class { constructor(url) { this.url = url; this.handlers = {}; sources.push(this); } addEventListener(type, fn) { this.handlers[type] = fn; } close() { this.closed = true; } },
  FileReader: class { readAsDataURL() { this.result = "data:image/jpeg;base64,SOURCE_WITH_EXIF"; this.onload(); } },
  fetch: async (path, options = {}) => { requests.push({ path, options }); return { ok: true, json: async () => (context.reply ? context.reply(path, options) : { avatar: uploaded, usage: {}, activity: [] }) }; },
});
let source = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
assert.ok(source.includes("  start();\n})();"));
source = source.replace("  start();\n})();", `
  globalThis.ui = { S, api, avatar, avatarPicker, cropAvatar, applyTheme, recommendedFace,
    sheet, activeSheets, membersSheet, renderChats, parseRoute, TABS, homeAgenda, needsCount, pinButton, profileSheet, onEvent,
    projectPicker, pickProjectSheet, messageEl, questionCard, needsSheet, longPress, botMenu, archiveBot, restoreBot, freshCard, memoryCard,
    bots: () => { let body; screen = (_, b) => { body = b; }; renderBots(); return body; },
    buildComposer, drafts, keepDraft, sendDraft, columnChart, usageBuckets, U,
    chats: () => { let body; screen = (_, b) => { body = b; }; renderChats(); return body; },
    home: () => { let body; screen = (_, b) => { body = b; }; renderHomeUsage = () => {}; renderHome(); return body; },
    dashboard: () => { let body; screen = (_, b) => { body = b; }; renderUsageCard = () => {}; renderDashboard(); return body; },
    browser: (sid) => { let body; screen = (_, b) => { body = b; }; renderBrowser(sid); return body; }, closeBrowserStream
  };
})();`);
vm.runInContext(source, context);
return { context, ui: context.ui, document, requests, timers, sources };
}

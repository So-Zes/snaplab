// Settings > Web search: the order of sources with their pauses, saving a key, removing it.
// Run: node tests/search_ui.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { makeUI } from "./ui_harness.mjs";

const flush = () => new Promise((resolve) => setImmediate(resolve));
for (const lang of ["en", "ja", "ru"]) {
  const { context, ui, document, requests } = makeUI();
  for (const file of ["en", "ja", "ru", "core"]) vm.runInContext(fs.readFileSync(new URL(`../web/i18n/${file}.js`, import.meta.url), "utf8"), context);
  ui.S.lang = lang;
  const root = document.createElement("div"); root.setAttribute("id", "app"); document.body.appendChild(root);
  const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);

  ui.S.search = null;
  assert.equal(ui.searchCard(), null);

  // no key: DuckDuckGo paused, Tavily keyless in use; the hint points to app.tavily.com; nothing to remove
  ui.S.search = { mode: "auto", keys: { tavily: false, brave: false }, paused_minutes: 0, order: [
    { name: "duckduckgo", label: "DuckDuckGo", paused_minutes: 12 }, { name: "tavily-keyless", label: "Tavily (no key)", paused_minutes: 0 }] };
  let card = ui.searchCard();
  const tags = card.querySelectorAll(".tag");
  assert.equal(tags.length, 2);
  assert.match(tags[0].className, /bad/);
  assert.match(tags[0].textContent, /12/);
  assert.match(tags[1].className, /good/);
  assert.equal(card.querySelector("a").getAttribute("href"), "https://app.tavily.com");
  assert.equal(card.querySelector("input").getAttribute("type"), "password");
  assert.equal(card.querySelector(".model-actions"), null);

  // saving sends the chosen service and the key, then clears the field
  context.reply = (path) => path === "/api/search" ? { ok: true, search: { mode: "auto", keys: { tavily: true, brave: false }, paused_minutes: 0,
    order: [{ name: "tavily", label: "Tavily", paused_minutes: 0 }, { name: "duckduckgo", label: "DuckDuckGo", paused_minutes: 0 }] } } : {};
  const input = card.querySelector("input");
  input.value = "tvly-synthetic";
  card.querySelector("select").value = "tavily";
  const save = card.querySelectorAll("button").find((b) => b.className === "btn");
  await save.emit("click");
  assert.ok(!toasts.querySelector(".bad"), "no error toast");
  await flush(); await flush();
  const sent = requests.find((r) => r.path === "/api/search");
  assert.ok(sent, "posted");
  assert.deepEqual(JSON.parse(sent.options.body), { action: "save", provider: "tavily", key: "tvly-synthetic" });
  assert.equal(input.value, "");
  assert.equal(ui.S.search.keys.tavily, true);

  // with a key: a remove button for it, and the other hint
  card = ui.searchCard();
  assert.equal(card.querySelector(".model-actions").querySelectorAll("button").length, 1);
  assert.match(card.querySelectorAll(".kv")[0].textContent, /Tavily/);
  assert.match(card.querySelectorAll(".tag")[0].className, /good/);
}
console.log("search_ui: ok");

// Settings > Experiments: the decision watch switch, its facts and its problems. Run: node tests/watch_ui.mjs
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { makeUI } from "./ui_harness.mjs";

for (const lang of ["en", "ja", "ru"]) {
  const { context, ui, document, requests } = makeUI();
  for (const file of ["en", "ja", "ru", "core"]) vm.runInContext(fs.readFileSync(new URL(`../web/i18n/${file}.js`, import.meta.url), "utf8"), context);
  ui.S.lang = lang;
  const root = document.createElement("div"); root.setAttribute("id", "app"); document.body.appendChild(root);
  const toasts = document.createElement("div"); toasts.setAttribute("id", "toasts"); document.body.appendChild(toasts);
  ui.S.decisionWatch = null;
  assert.equal(ui.watchCard(), null);
  ui.S.decisionWatch = { enabled: false, model: "liquid/d1.2-ultra", key_env: "OPENROUTER_API_KEY", key_set: false, quiet_minutes: 10,
    wake_at: 0.6, checks: 0, asked: 0, woke: 0, cost_usd: 0, last_error: "" };
  let card = ui.watchCard();
  const cb = card.querySelector("input");
  assert.ok(!cb.checked);
  assert.match(card.textContent, /OPENROUTER_API_KEY/);
  context.reply = (path) => path === "/api/decision-watch" ? { ok: true, decision_watch: { ...ui.S.decisionWatch, enabled: true, key_set: true } } : {};
  cb.checked = true;
  await cb.emit("change");
  const sent = requests.find((r) => r.path === "/api/decision-watch");
  assert.deepEqual(JSON.parse(sent.options.body), { enabled: true });
  assert.equal(ui.S.decisionWatch.enabled, true);
  ui.S.decisionWatch = { ...ui.S.decisionWatch, checks: 5, asked: 2, woke: 1, cost_usd: 0.00003, last_error: "HTTP 402 budget" };
  card = ui.watchCard();
  assert.match(card.textContent, /402/);
  assert.match(card.textContent, /5/);
}
console.log("watch_ui: ok");

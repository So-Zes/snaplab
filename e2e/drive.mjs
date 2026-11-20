// SnapLab end-to-end driver: headless Edge (or Chrome) through the DevTools Protocol, Node's built-ins only.
//
//   node e2e/drive.mjs --pair <pairing link> --out <folder> --scenario <name> [--lang en|ja] [--prefix p_]
//                      [--header "Tailscale-User-Login: demo@example.com"] [--dark] [--chat "text"]
//
// Scenarios: onboarding, tour, chat, onboard-chat, session, demo, real, real2, round2, round3, round6 (demo model: errors,
// connection sheet, models), round7 (faces), round9 (home, chat management and sheets), round10 (projects and bot conversations),
// real3 / real3b (real model: BRIEF hand-over, Run tests harness). See the bottom of the file.
// Uses a throwaway browser profile, phone viewport 390x844 at 3x (or --scale), touch on. Prints one JSON result.
import { spawn, execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(`--${k}`); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };
const flag = (k) => argv.includes(`--${k}`);
const PAIR = arg("pair");
const OUT = path.resolve(arg("out", "docs/screenshots"));
const SCEN = arg("scenario", "tour");
const LANG = arg("lang", "en");
const PREFIX = arg("prefix", "");
const HEADER = arg("header", null);
const DARK = flag("dark");
const W = Number(arg("width", 390)), H = Number(arg("height", 844)), SCALE = Number(arg("scale", 2));
const CHAT = arg("chat", null);
const TIMEOUT = Number(arg("timeout", 240)) * 1000;
const BROWSERS = ["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
const BROWSER = arg("browser", BROWSERS.find((b) => fs.existsSync(b)));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
if (!PAIR) { console.log(JSON.stringify({ ok: false, fatal: "--pair is required" })); process.exit(2); }
const ORIGIN = new URL(PAIR).origin;
const HOST = new URL(PAIR).hostname;
fs.mkdirSync(OUT, { recursive: true });
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "snaplab-e2e-"));
const R = { ok: false, scenario: SCEN, lang: LANG, steps: [], shots: [], console_errors: [], data: {}, fatal: null };
const T0 = Date.now();
const step = (s) => R.steps.push(`${((Date.now() - T0) / 1000).toFixed(1)}s ${s}`);

let child, s, ev, browserSend;
async function boot() {
  child = spawn(BROWSER, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update", "--hide-scrollbars", "--lang=" + (LANG === "ja" ? "ja-JP" : "en-US"),
    `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE ${HOST}`, "--window-size=1280,900", "about:blank"], { stdio: "ignore", windowsHide: true });
  const portFile = path.join(PROFILE, "DevToolsActivePort");
  let port, wsPath;
  for (let i = 0; i < 150 && !port; i++) { await sleep(100); try { [port, wsPath] = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/); } catch (e) { } }
  if (!port) throw new Error("browser did not start");
  const ws = new WebSocket(`ws://127.0.0.1:${port}${wsPath}`);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("cannot reach DevTools")); });
  let id = 1; const pending = new Map(); const listeners = [];
  ws.onmessage = (m) => {
    const x = JSON.parse(m.data);
    if (x.id && pending.has(x.id)) { const p = pending.get(x.id); pending.delete(x.id); x.error ? p.rej(new Error(x.error.message)) : p.res(x.result); }
    else if (x.method) listeners.forEach((f) => f(x));
  };
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => { const n = id++; pending.set(n, { res, rej }); ws.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) })); });
  browserSend = (m, p) => send(m, p);
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId: S } = await send("Target.attachToTarget", { targetId, flatten: true });
  s = (m, p) => send(m, p, S);
  listeners.push((x) => {
    if (x.sessionId !== S) return;
    if (x.method === "Runtime.exceptionThrown") R.console_errors.push(String(x.params.exceptionDetails?.exception?.description || x.params.exceptionDetails?.text).slice(0, 300));
    if (x.method === "Runtime.consoleAPICalled" && x.params.type === "error") R.console_errors.push(x.params.args.map((a) => a.value || a.description).join(" ").slice(0, 300));
  });
  await s("Page.enable"); await s("Runtime.enable"); await s("Network.enable");
  if (HEADER) { const i = HEADER.indexOf(":"); await s("Network.setExtraHTTPHeaders", { headers: { [HEADER.slice(0, i).trim()]: HEADER.slice(i + 1).trim() } }); }
  await s("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: SCALE, mobile: true });
  await s("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
  await s("Emulation.setUserAgentOverride", { userAgent: "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 snaplab-e2e", acceptLanguage: LANG === "ja" ? "ja-JP,ja" : "en-US,en" });
  await s("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: DARK ? "dark" : "light" }, { name: "prefers-reduced-motion", value: "no-preference" }] });
  ev = async (expr) => { const r = await s("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error("eval: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text)); return r.result.value; };
}
const waitFor = async (expr, ms, label) => {
  const end = Date.now() + ms; let last;
  while (Date.now() < end) { try { const v = await ev(expr); if (v) return v; } catch (e) { last = e; } await sleep(200); }
  throw new Error(`timed out waiting for: ${label}${last ? " (" + last.message + ")" : ""}`);
};
const shot = async (name, full) => {
  await sleep(350);
  const params = { format: "png" };
  if (full) {
    await ev("document.body.classList.add('e2e-full'); true");
    const dims = await ev("({w: document.documentElement.clientWidth, h: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)})");
    params.captureBeyondViewport = true; params.clip = { x: 0, y: 0, width: dims.w, height: Math.min(dims.h, 4000), scale: 1 };
  }
  const r = await s("Page.captureScreenshot", params);
  if (full) await ev("document.body.classList.remove('e2e-full'); true");
  const f = path.join(OUT, `${PREFIX}${name}.png`); fs.writeFileSync(f, Buffer.from(r.data, "base64")); R.shots.push(f); step(`shot ${name}`);
};
const nav = async (hash) => { await ev(`location.hash = ${JSON.stringify(hash)}; true`); await sleep(500); };
const click = async (sel, label) => { await waitFor(`!!document.querySelector(${JSON.stringify(sel)})`, 15000, label || sel); await ev(`document.querySelector(${JSON.stringify(sel)}).click(); true`); await sleep(250); };
const clickText = async (sel, text) => {
  await waitFor(`[...document.querySelectorAll(${JSON.stringify(sel)})].some(e => e.textContent.includes(${JSON.stringify(text)}))`, 15000, `${sel} "${text}"`);
  await ev(`[...document.querySelectorAll(${JSON.stringify(sel)})].find(e => e.textContent.includes(${JSON.stringify(text)})).click(); true`); await sleep(250);
};
const type = async (sel, text) => {
  await waitFor(`!!document.querySelector(${JSON.stringify(sel)})`, 15000, sel);
  await ev(`(function(){var e=document.querySelector(${JSON.stringify(sel)}); e.focus(); e.value=${JSON.stringify(text)}; e.dispatchEvent(new Event('input',{bubbles:true})); return true;})()`);
};

async function pair() {
  await s("Page.navigate", { url: PAIR });
  await waitFor("document.readyState === 'complete' && !!document.querySelector('.gate-card')", 20000, "pairing page");
  await shot("00_pair");
  await click("#pair-go", "pair button");
  await waitFor("location.pathname === '/' && !!document.querySelector('#app') && !document.querySelector('.splash')", 20000, "app after pairing");
  R.data.cookie_visible_to_js = await ev("document.cookie.indexOf('snaplab') >= 0");
  step("paired");
}

async function onboarding() {
  await waitFor("!!document.querySelector('.lang-card')", 15000, "language step");
  await shot("01_welcome");
  await clickText(".lang-card", LANG === "ja" ? "日本語" : "English");
  await waitFor("!!document.querySelector('.ob-title')", 8000, "name step");
  await shot("02_name_empty");
  await type(".ob-body input.input", LANG === "ja" ? "ミナト" : "Marlin");
  await ev("document.querySelectorAll('.swatch')[1].click(); true");
  await shot("03_name");
  await click(".ob-foot .btn");
  await waitFor("!!document.querySelector('.tone-grid')", 8000, "tone step");
  await ev("document.querySelector('[data-tone=playful]').click(); true");
  await shot("04a_tone_hint");
  await type(".ob-body label.field input.input", "Alex");
  await shot("04_tone");
  await click(".ob-foot .btn");
  await waitFor("!!document.querySelector('.prompt-ta') && document.querySelector('.prompt-ta').value.length > 40", 10000, "prompt preview");
  R.data.prompt_preview = await ev("document.querySelector('.prompt-ta').value.slice(0, 200)");
  await shot("05_prompt");
  await click(".ob-foot .btn");
  await waitFor("location.hash.startsWith('#/room/') && document.querySelectorAll('.msg.bot').length >= 1", 15000, "first chat");
  await shot("06_first_chat");
  step("onboarding done");
}

async function sendChat(text, waitMs) {
  const before = await ev("document.querySelectorAll('.msg.bot:not(.streaming)').length");
  await type(".composer-input", text);
  await click(".composer .send");
  const tSend = Date.now();
  let sawThinking = false, sawStream = false, shots = 0;
  const end = Date.now() + (waitMs || TIMEOUT);
  while (Date.now() < end) {
    const st = await ev(`(function(){var m=document.querySelector('.msg.bot.streaming'); return {streaming: !!m, text: m ? (m.querySelector('.bubble-body')||{}).textContent||'' : '', tools: m ? m.querySelectorAll('.tool').length : 0, done: document.querySelectorAll('.msg.bot:not(.streaming)').length}})()`);
    if (st.streaming && !sawThinking) { sawThinking = true; R.data[`thinking_after_s_${text.slice(0, 10)}`] = (Date.now() - tSend) / 1000; await shot(`chat_${PREFIX ? "" : ""}${slug(text)}_a_thinking`); }
    if (st.streaming && st.text.length > 20 && !sawStream && shots < 1) { sawStream = true; shots++; await shot(`chat_${slug(text)}_b_streaming`); }
    if (st.done > before && !st.streaming) break;
    await sleep(120);
  }
  const reply = await ev("(function(){var b=document.querySelectorAll('.msg.bot:not(.streaming)'); var l=b[b.length-1]; return l ? {text: (l.querySelector('.bubble-body')||{}).textContent||'', tools: [...l.querySelectorAll('.tool')].map(t=>t.textContent), err: (l.querySelector('.msg-err')||{}).textContent||'', q: !!l.querySelector('.qcard')} : null})()");
  R.data[`reply_${slug(text)}`] = Object.assign({ seconds: (Date.now() - tSend) / 1000, saw_thinking: sawThinking, saw_stream: sawStream }, reply);
  await shot(`chat_${slug(text)}_c_reply`);
  return reply;
}
const slug = (t) => t.toLowerCase().replace(/[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+/g, "-").slice(0, 18).replace(/-+$/, "");

async function tour() {
  for (const [hash, name] of [["#/chats", "10_chats"], ["#/projects", "20_projects"], ["#/dashboard", "30_dashboard"], ["#/bots", "40_bots"], ["#/settings", "50_settings"]]) {
    await nav(hash); await sleep(500); await shot(name);
  }
  const bid = await ev("(document.querySelector('.bot-card[href]')||{}).getAttribute ? null : null");
  await nav("#/bots"); await sleep(300);
  await ev("(document.querySelector('a.bot-card')||{click(){}}).click(); true"); await sleep(600); await shot("41_bot_detail");
  await nav("#/projects"); await sleep(300);
  if (await ev("!!document.querySelector('a.proj')")) { await ev("document.querySelector('a.proj').click(); true"); await sleep(600); await shot("21_project"); }
  await nav("#/dashboard"); await sleep(500);
  await shot("31_dashboard_full", true);
  const sess = await ev("[...document.querySelectorAll('a.mini-row')].map(a => a.getAttribute('href'))");
  let i = 0;
  for (const href of (sess || []).slice(0, 4)) {
    await nav(href); await sleep(1200);
    const st = await ev("((document.querySelector('.sess-hero')||{}).className||'').replace(/.*st-/, '')");
    await shot(`2${2 + i}_session_${st}`); i++;
    if (i === 1) await shot("29_session_full", true);
  }
  await nav("#/dashboard"); await sleep(300);
  if (await ev("!!document.querySelector('#needs-pill.show')")) { await ev("document.querySelector('#needs-pill').click(); true"); await sleep(800); await shot("32_needs_you_sheet"); await ev("document.querySelector('.scrim').click(); true"); await sleep(400); }
  await nav("#/projects"); await sleep(400);
  if (await ev("!!document.querySelector('a.proj')")) {
    await ev("document.querySelector('a.proj').click(); true"); await sleep(600);
    await ev("document.querySelector('.page > .btn.primary').click(); true"); await sleep(800); await shot("25_new_session_sheet");
    await ev("document.querySelector('.scrim').click(); true"); await sleep(400);
  }
  void bid;
}

async function newProjectAndSession(name, prompt, harness, testCmd) {
  await nav("#/projects");
  await ev("(document.querySelector('.fab') || document.querySelector('.empty-state .btn')).click(); true");
  await waitFor("!!document.querySelector('.sheet.in')", 5000, "project sheet");
  await type(".sheet input.input", name);
  await type(".sheet textarea.input", LANG === "ja" ? "Codex に小さな道具を作ってもらう試し" : "A small trial: Codex builds a tiny tool");
  if (testCmd) await type(".sheet input.input.mono", testCmd);
  await shot("60_new_project");
  await click(".sheet .btn.primary");
  await waitFor("location.hash.startsWith('#/project/')", 10000, "project page");
  await sleep(500); await shot("61_project");
  await click(".page > .btn.primary", "new session");
  await waitFor("!!document.querySelector('.sheet.in .harness')", 5000, "session sheet");
  await ev(`(document.querySelector('.harness[data-h=${harness}]')||{click(){}}).click(); true`);
  await type(".sheet textarea.input", prompt);
  await shot("62_new_session");
  await click(".sheet .btn.primary");
  await waitFor("location.hash.startsWith('#/session/')", 10000, "session page");
  const tStart = Date.now();
  await sleep(1500); await shot("63_session_running");
  const end = Date.now() + TIMEOUT;
  let mid = false;
  while (Date.now() < end) {
    const st = await ev("(document.querySelector('.sess-hero .status')||{}).className || ''");
    if (!mid && Date.now() - tStart > 25000) { mid = true; await shot("64_session_progress"); }
    if (/st-(done|review|idle|failed|waiting|stopped)/.test(st)) break;
    await sleep(1000);
  }
  R.data.session = await ev("({status: (document.querySelector('.sess-hero .status')||{}).textContent, metrics: [...document.querySelectorAll('.metric')].map(m=>m.textContent), files: document.querySelectorAll('.file-row').length, events: document.querySelectorAll('.ev').length, err: (document.querySelector('.err-card')||{}).textContent||''})");
  R.data.session.seconds = (Date.now() - tStart) / 1000;
  await shot("65_session_done"); await shot("66_session_done_full", true);
}

async function main() {
  if (!BROWSER) throw new Error("no Edge/Chrome found");
  await boot();
  await pair();
  if (SCEN === "onboarding") { await onboarding(); }
  else if (SCEN === "tour") { await tour(); }
  else if (SCEN === "demo") {
    await onboarding();
    await sendChat(LANG === "ja" ? "こんにちは！" : "Hi there!");
    await sendChat(LANG === "ja" ? "Python の最新版を調べて" : "Search the web for the latest Python release");
    await sendChat(LANG === "ja" ? "ダッシュボードに今日の予定を出して" : "Put today's plan on the dashboard");
    await sendChat(LANG === "ja" ? "テストの形式を聞いて" : "Ask me something");
    await shot("07_question_card");
    // a group room with every bot, then call one bot by name
    await nav("#/chats");
    await click(".fab", "new group button");
    await waitFor("!!document.querySelector('.sheet.in')", 5000, "group sheet");
    await type(".sheet input.input", LANG === "ja" ? "開発チーム" : "Launch crew");
    await shot("11_new_group");
    await click(".sheet .btn.primary");
    await waitFor("location.hash.startsWith('#/room/') && !!document.querySelector('.composer-input')", 8000, "group room");
    await type(".composer-input", "@Co");
    await sleep(400); await shot("12_group_mention");
    await sendChat(LANG === "ja" ? "@Coral 市場調査のメモを見て、要点を教えて" : "@Coral take a look at the market notes and give me the gist");
    await tour();
  }
  else if (SCEN === "round2") {
    // proposal card -> approve -> session in its own worktree -> result card -> merge -> "what this bot saw"
    await onboarding();
    await sendChat(LANG === "ja" ? "セッションを始めて、わかったことをメモにまとめて" : "Start a session that writes up the findings as a note");
    await waitFor("!!document.querySelector('.qcard.approval .qcard-opts .btn.primary')", 20000, "proposal card");
    await shot("80_proposal_card");
    if (await ev("!!document.querySelector('.qcard.approval .qcard-detail')")) { await ev("document.querySelector('.qcard.approval .qcard-detail').open = true; true"); await sleep(300); await shot("80b_proposal_full_text"); }
    await ev("document.querySelector('.qcard.approval .qcard-opts .btn.primary').click(); true");
    await waitFor("!!document.querySelector('.result-card')", 60000, "result card");
    await sleep(600); await shot("81_result_card");
    await ev("document.querySelector('.result-card').click(); true");
    await waitFor("location.hash.startsWith('#/session/') && !!document.querySelector('.hero-branch')", 15000, "session page");
    await sleep(800); await shot("82_session_branch");
    await ev("window.confirm = () => true; [...document.querySelectorAll('.sess-hero .btn')].find(b => b.textContent.includes(" + JSON.stringify(LANG === "ja" ? "本流" : "Merge") + ")).click(); true");
    await waitFor("!!document.querySelector('.hero-branch .chip.good')", 15000, "merged chip");
    await sleep(500); await shot("83_session_merged");
    await nav(await firstRoom());
    await waitFor("!!document.querySelector('.saw-btn')", 10000, "saw button");
    await ev("[...document.querySelectorAll('.saw-btn')].pop().click(); true");
    await waitFor("!!document.querySelector('.saw-sec')", 10000, "saw sheet");
    await sleep(600); await shot("84_what_bot_saw");
    await ev("document.querySelector('.saw-sec').open = true; true");
    await sleep(300); await shot("85_what_bot_saw_open");
  }
  else if (SCEN === "real2") {
    await onboarding();
    await sendChat("Create a project called Word counter (test command: python -m unittest -v). Then propose a Codex session in it that writes wc.py with count_words(text) and one unittest test in test_wc.py.");
    if (!(await ev("!!document.querySelector('.qcard.approval .qcard-opts .btn.primary')"))) {
      await sendChat("Please propose that Codex session now with start_session.");
    }
    await waitFor("!!document.querySelector('.qcard.approval .qcard-opts .btn.primary')", 30000, "proposal card");
    await shot("80_proposal_card");
    await ev("document.querySelector('.qcard.approval .qcard-opts .btn.primary').click(); true");
    await waitFor("!!document.querySelector('.result-card')", 600000, "result card");
    await sleep(800); await shot("81_result_card");
    R.data.result_card = await ev("document.querySelector('.result-card').textContent");
    await ev("document.querySelector('.result-card').click(); true");
    await waitFor("location.hash.startsWith('#/session/') && !!document.querySelector('.hero-branch')", 15000, "session page");
    await sleep(1000); await shot("82_session_branch"); await shot("82b_session_full", true);
    R.data.session = await ev("({status: (document.querySelector('.sess-hero .status')||{}).textContent, metrics: [...document.querySelectorAll('.metric')].map(m=>m.textContent), files: [...document.querySelectorAll('.file-row')].map(f=>f.textContent)})");
    if (await ev("[...document.querySelectorAll('.sess-hero .btn')].some(b => b.textContent.includes('Merge'))")) {
      await ev("window.confirm = () => true; [...document.querySelectorAll('.sess-hero .btn')].find(b => b.textContent.includes('Merge')).click(); true");
      await waitFor("!!document.querySelector('.hero-branch .chip.good')", 15000, "merged chip");
      await sleep(500); await shot("83_session_merged");
    }
    await nav(await firstRoom());
    await ev("[...document.querySelectorAll('.saw-btn')].pop().click(); true");
    await waitFor("!!document.querySelector('.saw-sec')", 10000, "saw sheet");
    await sleep(700); await shot("84_what_bot_saw");
  }
  else if (SCEN === "chat") { await nav(await firstRoom()); await sendChat(CHAT || "Hi!"); }
  else if (SCEN === "onboard-chat") { await onboarding(); await sendChat(CHAT || (LANG === "ja" ? "はじめまして。一言で自己紹介して。" : "Hi! Introduce yourself in one sentence.")); }
  else if (SCEN === "real") {
    // the verification run against the real model: onboarding, talk, search, dashboard, project + Codex session
    await onboarding();
    await sendChat(arg("chat1", LANG === "ja" ? "はじめまして。一言で自己紹介して。" : "Nice to meet you. Introduce yourself in one sentence."));
    await sendChat(arg("chat2", LANG === "ja" ? "ウェブで「Python 3.13 の新機能」を調べて、3行でまとめて。" : "Search the web for what's new in Python 3.13 and summarize it in 3 lines."));
    await sendChat(arg("chat3", LANG === "ja" ? "ダッシュボードに、今日やることのリストを3つ出して（update_dashboard を使って）。" : "Use update_dashboard to put a list widget with 3 things to do today on the dashboard."));
    await nav("#/dashboard"); await sleep(800); await shot("70_dashboard_after"); await shot("71_dashboard_full", true);
    R.data.widgets = await ev("[...document.querySelectorAll('.widget')].map(w=>w.className + ' :: ' + w.textContent.slice(0,80))");
    if (!flag("no-session")) await newProjectAndSession(LANG === "ja" ? "小さな電卓" : "Tiny calculator",
      LANG === "ja" ? "calc.py に足し算と引き算の関数 add(a,b), sub(a,b) を作り、test_calc.py に unittest の試験を2つ書いてください。終わったら一言でまとめて。" :
        "Create calc.py with functions add(a, b) and sub(a, b), and test_calc.py with two unittest tests. Finish with a one-line summary.",
      "codex", "python -m unittest -v");
    await tour();
  }
  else if (SCEN === "tenmin") {
    // a first-time user's first ten minutes (demo model), every main screen: used for the before/after design review
    const J = LANG === "ja";
    await onboarding();
    await sendChat(J ? "こんにちは！" : "Hi there!");
    await sendChat(J ? "Python の最新版を調べて" : "Search the web for the latest Python release");
    await shot("t10_search_reply");
    await sendChat(J ? "ダッシュボードに今日の予定を出して" : "Put today's plan on the dashboard");
    await sendChat(J ? "テストの形式を聞いて" : "Ask me something");
    await shot("t11_question_card");
    await clickText(".qcard .btn", "unittest");
    await waitFor("document.querySelectorAll('.msg.bot:not(.streaming)').length >= 6", 20000, "answer reply");
    await sleep(600); await shot("t12_answered");
    await sendChat(J ? "新しいプロジェクトを作って" : "Create a new project for a tiny tool");
    await sendChat(J ? "セッションを始めて、わかったことをメモにまとめて" : "Start a session that writes up the findings as a note");
    await waitFor("!!document.querySelector('.qcard.approval .btn.primary')", 20000, "proposal card");
    await sleep(400); await shot("t13_proposal_card");
    await ev("document.querySelector('.qcard.approval .btn.primary').click(); true");
    await waitFor("!!document.querySelector('.result-card')", 60000, "result card");
    await sleep(2500); await shot("t14_result_card");
    await sendChat(J ? "これは fail のテストです" : "This message will fail the first time");
    await waitFor("!!document.querySelector('.msg-err .retry-btn')", 15000, "error line");
    await shot("t15_failed_reply");
    await ev("[...document.querySelectorAll('.saw-btn')].pop().click(); true");
    await waitFor("!!document.querySelector('.saw-sec')", 10000, "saw sheet");
    await sleep(500); await shot("t16_what_bot_saw");
    await ev("document.querySelector('.scrim') && document.querySelector('.scrim').click(); true"); await sleep(400);
    await ev("document.querySelector('.result-card').click(); true");
    await waitFor("location.hash.startsWith('#/session/')", 10000, "session page");
    await sleep(900); await shot("t20_session"); await shot("t21_session_full", true);
    await nav("#/projects"); await sleep(500); await shot("t22_projects");
    await ev("document.querySelector('a.proj').click(); true"); await sleep(700); await shot("t23_project"); await shot("t24_project_full", true);
    await nav("#/bots"); await sleep(400);
    await ev("document.querySelector('.fab').click(); true");
    await waitFor("!!document.querySelector('.sheet.in input.input')", 5000, "new bot sheet");
    await type(".sheet input.input", "Coral"); await sleep(300); await shot("t30_new_bot");
    await ev("document.querySelector('.sheet .btn.primary').click(); true");
    await waitFor("location.hash.startsWith('#/bot/')", 8000, "bot page");
    await sleep(700); await shot("t31_bot_page"); await shot("t32_bot_page_full", true);
    await nav("#/bots"); await sleep(500); await shot("t33_bots");
    await nav("#/chats"); await sleep(400);
    await click(".fab", "new group button");
    await waitFor("!!document.querySelector('.sheet.in')", 5000, "group sheet");
    await type(".sheet input.input", J ? "開発チーム" : "Launch crew");
    await shot("t40_new_group");
    await click(".sheet .btn.primary");
    await waitFor("location.hash.startsWith('#/room/') && !!document.querySelector('.composer-input')", 8000, "group room");
    await sendChat(J ? "@Coral 市場の動きを一言で" : "@Coral one line on the market, please");
    await shot("t41_group_room");
    await nav("#/chats"); await sleep(500); await shot("t42_chats");
    await nav("#/dashboard"); await sleep(900); await shot("t50_dashboard"); await shot("t51_dashboard_full", true);
    if (await ev("!!document.querySelector('#health-dot')")) {
      await click("#health-dot"); await waitFor("!!document.querySelector('.health-row')", 6000, "connection sheet");
      await sleep(400); await shot("t52_connection");
      await ev("document.querySelector('.scrim').click(); true"); await sleep(400);
    }
    await nav("#/settings"); await sleep(600); await shot("t60_settings"); await shot("t61_settings_full", true);
  }
  else if (SCEN === "real6") {
    // QA round 2, H-1: the English "hand it over" flow must stay English (reply, BRIEF.md, note, the teammate's plan)
    await onboarding();
    const post = (p, b) => `fetch(${JSON.stringify(p)}, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-SnapLab': '1'}, body: JSON.stringify(${JSON.stringify(b)})}).then(r => r.json())`;
    const coral = await ev(post("/api/bots", { name: "Coral", role: "Builds small Python tools and keeps them tested", color: "#12A37F" }));
    await sendChat('Create a project called "Word stats": a tiny Python module wordstats.py with count_words(text) and top_words(text, n), plus unittest tests (test command: python -m unittest -v). Write its BRIEF and hand the project to Coral.');
    const boot = await ev("fetch('/api/bootstrap').then(r => r.json())");
    const proj = boot.projects.find((p) => p.name === "Word stats");
    R.data.project = proj ? proj.id : null;
    R.data.brief = proj ? await ev(`fetch('/api/projects/${proj.id}/brief').then(r => r.json()).then(j => j.text)`) : null;
    R.data.replies = await ev("[...document.querySelectorAll('.msg.bot:not(.streaming) .bubble-body')].map(b => b.textContent)");
    await shot("r6r_10_lead_room");
    const dm = boot.rooms.find((r) => r.kind === "dm" && r.bots[0] === coral.bot.id).id;
    await nav("#/room/" + dm);
    await waitFor("(function(){var ms=[...document.querySelectorAll('.msg')]; var i=ms.findIndex(m=>m.classList.contains('sys')); return i>=0 && ms.slice(i+1).some(m=>m.classList.contains('bot') && !m.classList.contains('streaming'));})()", 240000, "Coral's plan");
    await sleep(800);
    R.data.coral = await ev("[...document.querySelectorAll('.msg')].map(m => m.textContent.slice(0, 400))");
    await shot("r6r_20_coral_room"); await shot("r6r_21_coral_room_full", true);
    const all = JSON.stringify([R.data.brief, R.data.replies, R.data.coral]);
    R.data.cjk_chars = (all.match(/[぀-ヿ一-鿿]/g) || []).length;
  }
  else if (SCEN === "real5") {
    // after the harness measurement, real model, small: remember (tool, not just words), answer from the context
    // block without listing, and a "clear the dashboard" request that must wait for a card instead of wiping
    await onboarding();
    const post = (p, b) => `fetch(${JSON.stringify(p)}, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-SnapLab': '1'}, body: JSON.stringify(${JSON.stringify(b)})}).then(r => r.json())`;
    await ev(post("/api/projects", { name: "Tiny calculator", description: "A tiny calculator CLI", test_command: "python -m unittest -v" }));
    await sendChat("Remember that I prefer answers in two lines or less.");
    await sendChat("Which projects do we have right now?");
    await sendChat("Please clear the whole dashboard.");
    await sleep(800);
    R.data.cards = await ev("[...document.querySelectorAll('.qcard')].map(q => q.textContent.slice(0, 160))");
    R.data.widgets_left = await ev("fetch('/api/bootstrap').then(r => r.json()).then(j => j.dashboard.widgets.length)");
    R.data.user_note = await ev("fetch('/api/profile').then(r => r.json()).then(j => j.text)");
    await shot("r5r_10_room"); await shot("r5r_11_room_full", true);
    const view = await ev("fetch('/api/messages/' + [...document.querySelectorAll('.msg.bot[data-mid]')].pop().dataset.mid + '/view').then(r => r.json()).then(j => ({sections: (j.view.sections||[]).map(s => s.name), last: ((j.view.history||[]).slice(-1)[0]||{}).text}))");
    R.data.view = view;
  }
  else if (SCEN === "round10") {
    if (!["127.0.0.1", "localhost"].includes(HOST) || HEADER !== "Tailscale-User-Login: demo@example.com")
      throw new Error("round10 requires a fresh local seeded demo server and the demo owner header");
    await onboarding();
    const boot = await ev("fetch('/api/bootstrap').then(r => r.json())");
    const peers = boot.bots.filter(b => !b.orchestrator);
    if (peers.length < 2) throw new Error("Use --seed en or --seed ja with a new demo data folder");
    const builder = peers[0], reviewer = peers[1];
    const dm = boot.rooms.find(r => r.kind === "dm" && r.bots[0] === builder.id);
    await nav("#/room/" + dm.id);
    await sendChat("r10-project-1"); await sendChat("r10-project-2");
    const projects = await ev("fetch('/api/bootstrap').then(r=>r.json()).then(j=>j.projects)");
    if (projects.filter(p => p.name.startsWith("Round10 Calendar") && p.bot === builder.id).length !== 2)
      throw new Error("The teammate did not retain both projects");
    await shot("r10_01_created_projects");
    await nav("#/bot/" + builder.id); await sleep(700); await shot("r10_02_multiple_projects", true);
    await clickText("button", LANG === "ja" ? "担当を選ぶ" : "Choose projects to manage");
    await shot("r10_03_assignments"); await click(".sheet-head button");
    await nav("#/room/" + dm.id);
    await sendChat("r10-contact:" + reviewer.id);
    const contact = await waitFor("fetch('/api/bootstrap').then(r=>r.json()).then(j=>j.rooms.find(r=>r.bot_only && r.bot_pair))", 12000, "public bot conversation");
    await nav("#/room/" + contact.id);
    await waitFor("document.querySelectorAll('.bot-chat-label').length >= 2 && !document.querySelector('.msg.streaming')", 20000, "bot reply");
    await shot("r10_04_bot_conversation", true);
    await nav("#/room/" + dm.id); await sendChat("r10-group:" + reviewer.id + "," + boot.bots.find(b=>b.orchestrator).id);
    await nav("#/chats"); await shot("r10_05_bot_group_badges");
    const group = await ev("fetch('/api/bootstrap').then(r=>r.json()).then(j=>j.rooms.find(r=>r.name==='Round10 planning'))");
    await nav("#/room/" + group.id); await sleep(1500); await shot("r10_06_invited_group", true);
    await type(".composer-input", LANG === "ja" ? "止めて" : "Stop"); await click(".composer .send");
    await waitFor("fetch('/api/bootstrap').then(r=>r.json()).then(j=>j.rooms.some(r=>r.id === " + JSON.stringify(group.id) + " && r.bots_paused))", 8000, "stopped group");
    await shot("r10_07_stopped_group");
    await nav("#/room/" + dm.id); await sendChat("r10-web-approval");
    await waitFor("!!document.querySelector('.qcard-why')", 8000, "approval reason");
    await shot("r10_08_web_approval", true);
    await nav("#/home"); await click(".home-attention"); await shot("r10_09_approval_inbox", true);
    const layout = await ev(`(() => ({overflow:document.documentElement.scrollWidth > innerWidth + 1,
      small:[...document.querySelectorAll('.bot-chat-label,.qcard-why,.lead-text')].filter(e=>e.getClientRects().length && parseFloat(getComputedStyle(e).fontSize)<14).length}))()`);
    if (layout.overflow || layout.small) throw new Error("round10 layout: " + JSON.stringify(layout));
    R.data.round10 = { builder: builder.id, contact: contact.id, group: group.id, projects: 2 };
  }
  else if (SCEN === "round9") {
    // Only the isolated demo server, with its scripted model. Never use production pairing links.
    if (!["127.0.0.1", "localhost"].includes(HOST) || HEADER !== "Tailscale-User-Login: demo@example.com")
      throw new Error("round9 requires the local demo server and the demo owner header");
    const J = LANG === "ja";
    const topClick = async (selector) => {
      await ev(`document.querySelectorAll('.sheet').item(document.querySelectorAll('.sheet').length - 1).querySelector(${JSON.stringify(selector)}).click(); true`);
      await sleep(350);
    };
    const checkLayout = async () => {
      const bad = await ev(`(() => {
        const small = [...document.querySelectorAll('#app *, .sheet *')].filter(e => e.getClientRects().length &&
          [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) &&
          parseFloat(getComputedStyle(e).fontSize) > 0 && parseFloat(getComputedStyle(e).fontSize) < 14);   // 0px is text hidden on purpose
        const images = [...document.images].filter(i => i.getClientRects().length && i.complete && !i.naturalWidth);
        return {small:small.length, smallText:small.map(e => (e.className || e.tagName) + " | " + e.textContent.trim().slice(0, 30) + " | " + getComputedStyle(e).fontSize),
                images:images.map(i=>i.getAttribute('src')), overflow:document.documentElement.scrollWidth > innerWidth + 1};
      })()`);
      if (bad.small || bad.images.length || bad.overflow) throw new Error("round9 layout: " + JSON.stringify(bad));
    };
    await onboarding();
    await nav("#/home");
    await ev("location.hash = ''; true"); await sleep(600);
    if (!(await ev("document.querySelector('#app').classList.contains('screen-home')"))) throw new Error("Home is not the default");
    R.data.tabs = await ev("[...document.querySelectorAll('.tab')].map(e => e.dataset.tab)");
    if (R.data.tabs.join(",") !== "chats,home,dashboard") throw new Error("Unexpected tab order");
    await shot("r9_01_home"); await shot("r9_02_home_full", true); await checkLayout();
    await click(".home-attention"); await shot("r9_03_decisions"); await topClick(".sheet-head button");
    await nav("#/dashboard");
    if (await ev("!!document.querySelector('.dash-fold[open]')")) throw new Error("Dashboard must start folded");
    await shot("r9_04_dashboard");
    await ev("document.querySelectorAll('.dash-fold').forEach(e => e.open = true); true");
    await shot("r9_05_dashboard_expanded", true); await checkLayout();
    await nav("#/chats"); await shot("r9_06_chat_sections");
    await clickText(".chat-act", J ? "グループを作る" : "New group");
    await type(".sheet input.input", "Round9 game team");
    await topClick(".btn.primary.wide"); await waitFor("location.hash.startsWith('#/room/')", 8000, "group chat");
    const groupHash = await ev("location.hash");
    await click(".room-title"); await shot("r9_07_group_management");
    await type(".group-name", "Round9 builders");
    await ev("document.querySelector('.group-members input').click(); true");
    await topClick(".group-save");
    await click(".room-title");
    if (!(await ev("document.querySelector('.group-name').value === 'Round9 builders' && !document.querySelector('.group-members input').checked")))
      throw new Error("Group edits did not persist");
    await ev("document.querySelector('.group-members input').click(); true");
    await topClick(".group-save"); // add the bot back
    await click(".room-title");
    await ev("window.__round9NoReload = 'still here'; true");
    const grip = await ev("(() => { const r=document.querySelector('.sheet-grip').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+12}; })()");
    await s("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: grip.x, y: grip.y }] });
    for (let dy = 20; dy <= 110; dy += 15) {
      await s("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: grip.x, y: grip.y + dy }] }); await sleep(40);
    }
    await s("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await waitFor("!document.querySelector('.sheet')", 8000, "drag closes details");
    if (!(await ev("window.__round9NoReload === 'still here' && !document.documentElement.classList.contains('sheet-open')")))
      throw new Error("Drag reloaded or left the page locked");
    await click(".room-title");
    await click(".scrim"); await waitFor("!document.querySelector('.sheet')", 8000, "outside closes details");
    await nav("#/chats");
    await ev(`document.querySelector('a.chat-row[href="${groupHash}"]').parentElement.querySelector('.pin-chat').click(); true`); await sleep(500);
    await ev("document.querySelector('[data-chat-section=\"chats.bots\"] .pin-chat').click(); true"); await sleep(500);
    await shot("r9_08_pinned_chats");
    if (await ev("document.querySelectorAll('[data-chat-section=\"chats.pinned\"] .chat-row').length") < 2) throw new Error("Both chat kinds must pin");
    // The direct-chat edit button opens the bot name without navigating through its inside page.
    await ev("[...document.querySelectorAll('[data-chat-section=\"chats.pinned\"] .chat-row')].find(e => !e.textContent.includes('Round9')).click(); true"); await sleep(600);
    await click(J ? '[aria-label="Bot の名前とプロフィールを変える"]' : '[aria-label="Edit bot name and profile"]');
    await shot("r9_09_bot_name"); await topClick(".sheet-head button");
    await nav(groupHash); await click(".room-title"); await topClick(".group-delete"); await shot("r9_10_delete_confirmation");
    await topClick(".qcard-actions .btn.ghost"); // cancel once
    await topClick(".group-delete"); await topClick(".qcard-actions .btn.primary");
    await waitFor("location.hash === '#/chats' && !document.querySelector('.sheet')", 8000, "group deleted");
    if (await ev(`!!document.querySelector('a[href="${groupHash}"]')`)) throw new Error("Deleted group remains visible");
    await nav("#/settings"); await clickText(".seg button", J ? "立体" : "3D");
    await clickText(".seg.three button", J ? "暗い" : "Dark");
    await nav("#/home"); await sleep(700); await shot("r9_11_home_dark_3d"); await checkLayout();
    await s("Emulation.setDeviceMetricsOverride", { width: 320, height: H, deviceScaleFactor: SCALE, mobile: true });
    await shot("r9_12_home_320"); await checkLayout();
    await nav("#/dashboard"); await shot("r9_13_dashboard_dark_320"); await checkLayout();
    R.data.device_check = "Coordinator must repeat dismissal in Android Chrome and the installed PWA; desktop touch emulation is not a PWA test.";
  }
  else if (SCEN === "round7") {
    // Fresh demo data only. No model calls; use --seed en/ja on the demo server.
    const J = LANG === "ja";
    const topClick = async (selector) => {
      await ev(`document.querySelectorAll('.sheet').item(document.querySelectorAll('.sheet').length - 1).querySelector(${JSON.stringify(selector)}).click(); true`);
      await sleep(350);
    };
    await onboarding();
    await nav("#/chats");
    await clickText(".chat-act", J ? "Bot を作る" : "New bot");
    await type(".sheet .counted input", "Round7");
    await topClick(".face-field");
    await waitFor("document.querySelectorAll('.face-choice').length >= 35", 8000, "34 faces and initials");
    await shot("r7_01_picker");
    await topClick('[data-face="d2"]');
    await topClick(".face-save");
    await shot("r7_02_new_bot");
    await topClick(".btn.primary.wide");
    await waitFor("location.hash.startsWith('#/room/')", 8000, "new bot chat");
    await click(".room-title");
    await waitFor("!!document.querySelector('.hero-btns')", 8000, "bot inside");
    await ev("document.querySelector('.hero-btns button').click(); true"); await sleep(400);
    await topClick(".face-field");
    // Feed a repository fixture through the actual file input, decoder and canvas.
    const doc = await s("DOM.getDocument", { depth: -1 });
    const inputs = await s("DOM.querySelectorAll", { nodeId: doc.root.nodeId, selector: '.sheet input[type="file"]' });
    await s("DOM.setFileInputFiles", { nodeId: inputs.nodeIds.at(-1), files: [path.resolve("web/avatars/e/e8-256.png")] });
    await waitFor("!!document.querySelector('.crop-canvas')", 8000, "crop image");
    await ev("(() => { const r = document.querySelector('.sheet input[type=range]'); r.value = 1.5; r.dispatchEvent(new Event('input')); const c = document.querySelector('.crop-canvas'); c.focus(); c.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', bubbles:true})); return true; })()");
    await shot("r7_03_crop");
    await topClick(".btn.primary.wide");
    await waitFor("!document.querySelector('.crop-canvas')", 8000, "upload saved");
    await topClick(".face-save");
    await topClick(".btn.primary.wide");
    await waitFor("!document.querySelector('.sheet') && !!document.querySelector('.bot-hero img[src^=\"/api/avatars/\"]')", 8000, "uploaded bot face");
    await shot("r7_04_uploaded_face");
    await nav("#/dashboard"); await shot("r7_05_dashboard"); await shot("r7_06_dashboard_full", true);
    R.data.active_progress = await ev("document.querySelector('[data-section=work]').textContent");
    await nav("#/settings");
    await clickText(".seg button", J ? "立体" : "3D");
    await shot("r7_07_settings_3d");
    await clickText(".seg.three button", J ? "暗い" : "Dark");
    await nav("#/dashboard"); await sleep(700); await shot("r7_08_dashboard_dark_3d");
    await nav("#/chats"); await shot("r7_09_chats_dark_3d");
    await clickText(".chat-act", J ? "Bot を作る" : "New bot");
    await topClick(".face-field"); await shot("r7_10_picker_dark");
    const badImages = await ev("[...document.images].filter(i => i.getClientRects().length && i.complete && !i.naturalWidth).map(i => i.getAttribute('src'))");
    if (badImages.length) throw new Error("Broken visible images: " + badImages.join(", "));
    const tooSmall = await ev("[...document.querySelectorAll('button, p, label, h2, h3, .face-choice span')].filter(e => e.getClientRects().length && parseFloat(getComputedStyle(e).fontSize) < 14).length");
    if (tooSmall) throw new Error("Text below 14px: " + tooSmall);
    R.data.icon_style = await ev("document.documentElement.dataset.iconStyle");
  }
  else if (SCEN === "round6") {
    // round 6 (seeded demo data): two main places, New bot, the inside screen, SnapWork steps, scheduled tasks, the record
    const J = LANG === "ja";
    await onboarding();
    await nav("#/chats"); await sleep(600); await shot("r6_01_chats");
    await clickText(".chat-act", J ? "Bot を作る" : "New bot"); await sleep(500);
    await type(".sheet .counted input.input", J ? "ハル" : "Ada");
    await ev("[...document.querySelectorAll('.sheet .pick-row')].slice(-1)[0].click(); true");   // the project nobody looks after yet
    await shot("r6_02_new_bot");
    await click(".sheet .btn.primary.wide"); await waitFor("location.hash.startsWith('#/room/')", 10000, "new bot chat"); await sleep(800);
    await shot("r6_03_new_bot_chat");
    await nav("#/dashboard"); await sleep(900); await shot("r6_10_dashboard"); await shot("r6_11_dashboard_full", true);
    await nav("#/projects"); await sleep(600); await shot("r6_12_projects");
    const running = await ev("(window.__s = [...document.querySelectorAll('a.sess')].map(a => a.getAttribute('href')), true) && null");
    await nav("#/dashboard"); await sleep(500);
    const workHref = await ev("(document.querySelector('.work-row') || {}).getAttribute ? document.querySelector('.work-row').getAttribute('href') : null");
    if (workHref) { await nav(workHref); await sleep(1100); await shot("r6_20_snapwork"); await shot("r6_21_snapwork_full", true); }
    // the inside screen from the chat header
    const nemoRoom = await ev("(() => { const a = [...document.querySelectorAll('a')]; return null; })()");
    await nav("#/chats"); await sleep(500);
    await clickText("a.chat-row", "Nemo"); await sleep(900);
    await click(".room-title"); await sleep(1500); await shot("r6_30_inside"); await shot("r6_31_inside_full", true);
    await type(".archive-card input", J ? "unittest" : "unittest");
    await ev("document.querySelector('.archive-card .btn').click(); true");
    await waitFor("document.querySelectorAll('.arc-row').length > 0", 8000, "archive results");
    await ev("document.querySelector('.archive-card').scrollIntoView({block: 'start'}); true"); await sleep(400);
    await shot("r6_32_record_search");
    await ev("document.querySelector('.arc-row').click(); true"); await sleep(800); await shot("r6_33_record");
    await ev("document.querySelector('.sheet .icon-btn').click(); true"); await sleep(400);
    await ev("document.querySelector('.hero-btns .btn:not(.primary)').click(); true"); await sleep(700); await shot("r6_34_profile");
    await ev("document.querySelector('.sheet .icon-btn').click(); true"); await sleep(400);
    // the lead bot: recall and a scheduled task
    await nav("#/chats"); await sleep(500); await clickText("a.chat-row", J ? "ミナト" : "Marlin"); await sleep(800);
    await sendChat(J ? "前にテストの形式をどう決めた？" : "What did we decide about the tests?");
    await shot("r6_40_recall");
    await sendChat(J ? "毎朝8時に Python のニュースを確かめて" : "Every morning at 8, check the Python news");
    await waitFor("!!document.querySelector('.qcard.approval .btn.primary')", 15000, "task card");
    await sleep(400); await shot("r6_41_task_card");
    await ev("document.querySelector('.qcard.approval .btn.primary').click(); true"); await sleep(1500);
    await shot("r6_42_task_set");
    await click(".room-title"); await sleep(1500);
    await ev("document.querySelector('.tasks-card') && document.querySelector('.tasks-card').scrollIntoView({block: 'start'}); true"); await sleep(400);
    await shot("r6_43_lead_inside_tasks");
    await nav("#/settings"); await sleep(600); await ev("window.scrollTo(0, 0); document.querySelectorAll('.content').forEach(c => c.scrollTo && c.scrollTo(0, 0)); true"); await sleep(300); await shot("r6_50_settings");
    await clickText(".seg.three button", J ? "暗い" : "Dark"); await sleep(700); await shot("r6_51_settings_dark");
    await nav("#/dashboard"); await sleep(800); await shot("r6_52_dashboard_dark");
    await nav(await firstRoom()); await sleep(800); await shot("r6_53_chat_dark");
    await nav("#/settings"); await sleep(500); await clickText(".seg.three button", J ? "電話と同じ" : "Match phone"); await sleep(500);
    R.data.theme_after = await ev("document.documentElement.getAttribute('data-theme')");
  }
  else if (SCEN === "views") {
    // seeded demo data, any viewport (used for the landscape and bot-grid checks)
    await onboarding();
    await nav("#/projects"); await sleep(600); await shot("v10_projects");
    await ev("document.querySelector('a.proj').click(); true"); await sleep(700); await shot("v11_project");
    const sessions = await ev("[...document.querySelectorAll('a.sess')].map(a => a.getAttribute('href'))");
    for (const [i, href] of (sessions || []).slice(0, 3).entries()) {
      await nav(href); await sleep(1000); await shot(`v2${i}_session`);
      if (i === 0) { await ev("window.scrollTo(0, 600); document.querySelector('.content') && document.querySelector('.content').scrollTo && document.querySelector('.content').scrollTo(0, 600); true"); await sleep(300); await shot("v29_session_scrolled"); }
    }
    await nav(await firstRoom()); await sleep(700);
    await sendChat(LANG === "ja" ? "こんにちは！" : "Hi there!");
    await shot("v30_room");
    await nav("#/bots"); await sleep(600); await shot("v40_bots"); await shot("v41_bots_full", true);
    await nav("#/dashboard"); await sleep(800); await shot("v50_dashboard");
    await nav("#/chats"); await sleep(500); await shot("v60_chats");
  }
  else if (SCEN === "real4") {
    // round 4, real model, small: does an English room stay in English (also after tool calls)?
    await onboarding();
    await sendChat("Nice to meet you. Introduce yourself in one sentence.");
    const post = (p, b) => `fetch(${JSON.stringify(p)}, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-SnapLab': '1'}, body: JSON.stringify(${JSON.stringify(b)})}).then(r => r.json())`;
    await ev(post("/api/bots", { name: "Coral", role: "Builds small Python tools and keeps them tested", color: "#12A37F" }));
    await ev(post("/api/projects", { name: "Tiny calculator", description: "A tiny calculator CLI", test_command: "python -m unittest -v" }));
    await sendChat("Use write_brief to write a short BRIEF.md for the project Tiny calculator (goal: a tiny calculator CLI; done when: python -m unittest passes), then use assign_project to hand it to Coral. Then reply with one line.");
    await sendChat("Remember that I prefer answers in two lines or less.");
    const texts = await ev("[...document.querySelectorAll('.msg.bot:not(.streaming) .bubble-body')].map(b => b.textContent)");
    R.data.replies = texts;
    R.data.japanese_in_replies = texts.filter((x) => /[぀-ヿ一-鿿]/.test(x));
    await shot("r4r_10_english_room"); await shot("r4r_11_english_room_full", true);
    const boot = await ev("fetch('/api/bootstrap').then(r => r.json())");
    const coral = boot.bots.find((b) => b.name === "Coral");
    const dm = boot.rooms.find((r) => r.kind === "dm" && r.bots[0] === coral.id).id;
    await nav("#/room/" + dm);
    await waitFor("(function(){var ms=[...document.querySelectorAll('.msg')]; var i=ms.findIndex(m=>m.classList.contains('sys')); return i>=0 && ms.slice(i+1).some(m=>m.classList.contains('bot') && !m.classList.contains('streaming'));})()", 240000, "Coral's plan");
    await sleep(800);
    const ctexts = await ev("[...document.querySelectorAll('.msg.bot:not(.streaming) .bubble-body')].map(b => b.textContent)");
    R.data.coral = ctexts;
    R.data.japanese_in_coral = ctexts.filter((x) => /[぀-ヿ一-鿿]/.test(x));
    await shot("r4r_20_coral"); await shot("r4r_21_coral_full", true);
    await nav("#/settings"); await sleep(800); await shot("r4r_30_settings_about_you");
  }
  else if (SCEN === "real3") {
    // round 3 against the real model, kept small: one chat, the orchestrator writes a BRIEF and hands the project
    // to a teammate (who answers in its own chat), a Codex session through the translator, a "Run tests" session.
    await onboarding();
    await sendChat(LANG === "ja" ? "はじめまして。一言で自己紹介して。" : "Nice to meet you. Introduce yourself in one sentence.");
    const coral = await ev("fetch('/api/bots', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-SnapLab': '1'}, body: JSON.stringify({name: 'Coral', role: 'Builds small Python tools and keeps them tested', color: '#12A37F'})}).then(r => r.json())");
    R.data.coral = coral && coral.bot ? coral.bot.id : coral;
    await newProjectAndSession("Tiny calculator", "Create calc.py with add(a, b) and test_calc.py with one unittest test for it. Finish with one short sentence.", "codex", "python -m unittest -v");
    await nav("#/projects"); await sleep(400);
    await ev("document.querySelector('a.proj').click(); true"); await sleep(600);
    await click(".page > .btn.primary", "new session");
    await waitFor("!!document.querySelector('.sheet.in .harness')", 5000, "session sheet");
    await ev("(document.querySelector('.harness[data-h=tests]')||{click(){}}).click(); true");
    await type(".sheet textarea.input", "Run the tests");
    await shot("r3r_70_tests_harness_sheet");
    await click(".sheet .btn.primary");
    await waitFor("location.hash.startsWith('#/session/')", 10000, "tests session page");
    await waitFor("/st-(review|failed)/.test((document.querySelector('.sess-hero .status')||{}).className||'')", 120000, "tests session done");
    R.data.tests_session = await ev("({status: (document.querySelector('.sess-hero .status')||{}).textContent, metrics: [...document.querySelectorAll('.metric')].map(m=>m.textContent)})");
    await sleep(500); await shot("r3r_71_tests_session");
    await nav(await firstRoom());
    await sendChat("Use write_brief to write a short BRIEF.md for the project Tiny calculator (goal: a tiny calculator CLI; done when: python -m unittest passes), then use assign_project to hand it to Coral. Then reply with one line.");
    const dm = await ev("fetch('/api/bootstrap').then(r => r.json()).then(j => j.rooms.find(r => r.kind === 'dm' && r.bots[0] === " + JSON.stringify(R.data.coral) + ").id)");
    await nav("#/room/" + dm);
    await waitFor("[...document.querySelectorAll('.msg.bot:not(.streaming)')].length >= 1", 180000, "Coral's answer");
    await sleep(800);
    R.data.coral_reply = await ev("[...document.querySelectorAll('.msg.bot:not(.streaming) .bubble-body')].map(b => b.textContent).pop()");
    await shot("r3r_80_coral_plan");
    await nav("#/dashboard"); await sleep(800); await click("#health-dot", "health dot");
    await waitFor("!!document.querySelector('.health-rows .health-row')", 8000, "connection sheet");
    R.data.health = await ev("[...document.querySelectorAll('.health-row')].map(r => r.textContent)");
    await shot("r3r_90_connection");
  }
  else if (SCEN === "real3b") {
    // continues real3 on the same data: the BRIEF + hand-over in the orchestrator's own room, then "Run tests" after merging
    const post = (p, b) => `fetch(${JSON.stringify(p)}, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-SnapLab': '1'}, body: JSON.stringify(${JSON.stringify(b)})}).then(r => r.json())`;
    const boot = await ev("fetch('/api/bootstrap').then(r => r.json())");
    const orch = boot.bots.find((b) => b.orchestrator), coral = boot.bots.find((b) => b.name === "Coral");
    const mroom = boot.rooms.find((r) => r.kind === "dm" && r.bots[0] === orch.id).id;
    const croom = boot.rooms.find((r) => r.kind === "dm" && r.bots[0] === coral.id).id;
    for (const q of boot.questions.filter((q) => q.status === "open")) await ev(post(`/api/questions/${q.id}/answer`, { text: "Decline" }));
    const codex = boot.sessions.find((x) => x.harness === "codex" && x.status === "review");
    if (codex) R.data.merge = await ev(post(`/api/sessions/${codex.id}/merge`, {}));
    const proj = boot.projects.find((p) => p.name === "Tiny calculator");
    const ts = await ev(post("/api/sessions", { project: proj.id, harness: "tests", prompt: "Run the tests after the merge" }));
    await nav("#/session/" + ts.session.id);
    await waitFor("/st-(review|failed)/.test((document.querySelector('.sess-hero .status')||{}).className||'')", 120000, "tests session done");
    R.data.tests_session = await ev("({status: (document.querySelector('.sess-hero .status')||{}).textContent, metrics: [...document.querySelectorAll('.metric')].map(m=>m.textContent)})");
    await sleep(500); await shot("r3r_72_tests_after_merge"); await shot("r3r_73_tests_after_merge_full", true);
    await nav("#/room/" + mroom); await sleep(800);
    await sendChat("Use write_brief to write a short BRIEF.md for the project Tiny calculator (goal: a tiny calculator CLI; done when: python -m unittest passes), then use assign_project to hand it to Coral. Then reply with one line.");
    await nav("#/room/" + croom);
    await waitFor("(function(){var ms=[...document.querySelectorAll('.msg')]; var i=ms.findIndex(m=>m.classList.contains('sys') && /handed you/.test(m.textContent)); return i>=0 && ms.slice(i+1).some(m=>m.classList.contains('bot') && !m.classList.contains('streaming'));})()", 240000, "Coral's plan after the hand-over");
    await sleep(800);
    R.data.coral_reply = await ev("[...document.querySelectorAll('.msg.bot:not(.streaming) .bubble-body')].map(b => b.textContent).pop()");
    await shot("r3r_80_coral_plan"); await shot("r3r_81_coral_plan_full", true);
    const brief = await ev("fetch('/api/messages/' + [...document.querySelectorAll('.msg.bot[data-mid]')].pop().dataset.mid + '/view').then(r => r.json()).then(j => (j.view.sections||[]).map(s => s.name))");
    R.data.coral_saw_sections = brief;
    await nav("#/room/" + mroom); await sleep(800); await shot("r3r_82_orchestrator_handover");
  }
  else if (SCEN === "round3") {
    // robustness and extension screens: failed reply + Try again, cut reply, connection sheet, models, bot model
    await onboarding();
    await sendChat(LANG === "ja" ? "これは fail のテストです" : "This message will fail the first time");
    await waitFor("!!document.querySelector('.msg-err .retry-btn')", 15000, "error line with Try again");
    R.data.error_text = await ev("document.querySelector('.msg-err .err-text').textContent");
    await shot("r3_10_failed_reply");
    await ev("document.querySelector('.msg-err details') && (document.querySelector('.msg-err details').open = true); true");
    await shot("r3_11_failed_details");
    await click(".msg-err .retry-btn", "Try again");
    await waitFor("document.querySelectorAll('.msg.bot:not(.streaming)').length >= 3 && !document.querySelector('.msg.streaming')", 20000, "reply after retry");
    await shot("r3_12_after_retry");
    await sendChat(LANG === "ja" ? "cut の返事を見せて" : "Show me a cut reply");
    await waitFor("[...document.querySelectorAll('.msg-err .err-text')].length >= 2", 15000, "cut reply marked");
    R.data.cut_text = await ev("[...document.querySelectorAll('.msg-err .err-text')].pop().textContent");
    await shot("r3_13_cut_reply");
    await nav("#/chats"); await sleep(500);
    await shot("r3_19_chats_with_dot");
    await click("#health-dot", "health dot");
    await waitFor("!!document.querySelector('.health-rows .health-row')", 8000, "connection sheet");
    R.data.health = await ev("[...document.querySelectorAll('.health-row')].map(r => r.textContent)");
    await shot("r3_20_connection");
    await ev("document.querySelector('.scrim') && document.querySelector('.scrim').click(); true"); await sleep(400);
    await nav("#/settings"); await sleep(600);
    await ev("document.querySelector('.models') && document.querySelector('.models').scrollIntoView(); true");
    await shot("r3_30_settings_models");
    await nav("#/bots"); await sleep(500);
    const firstBot = await ev("document.querySelector('a.bot-card, .bot-card') ? (document.querySelector('a.bot-card') || document.querySelector('.bot-card')).getAttribute('href') : null");
    if (firstBot) { await nav(firstBot); await sleep(600); await ev("[...document.querySelectorAll('.field')].find(f => f.textContent.includes('" + (LANG === "ja" ? "モデル" : "Model") + "'))?.scrollIntoView(); true"); await shot("r3_40_bot_model"); }
    await nav("#/dashboard"); await sleep(800);
    await shot("r3_50_dashboard");
  }
  else if (SCEN === "session") {
    await newProjectAndSession(arg("project", "Tiny calculator"), arg("prompt", "Create calc.py with add(a, b) and test_calc.py with one unittest test."), arg("harness", "codex"), arg("tests", "python -m unittest -v"));
  }
  R.ok = true;
}
async function firstRoom() { await waitFor("!!(window.location)", 1000, ""); await ev("location.hash='#/chats'; true"); await sleep(600); return await ev("document.querySelector('a.chat-row').getAttribute('href')"); }

main().catch((e) => { R.fatal = String(e && e.message || e); }).finally(async () => {
  R.seconds = (Date.now() - T0) / 1000;
  try { if (browserSend) await Promise.race([browserSend("Browser.close"), sleep(2000)]); } catch (e) { }
  try { if (child && process.platform === "win32") execSync(`taskkill /T /F /PID ${child.pid}`, { stdio: "ignore" }); else if (child) child.kill(); } catch (e) { }
  await sleep(500);
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) { }
  console.log(JSON.stringify(R, null, 1));
  process.exit(R.ok ? 0 : 1);
});

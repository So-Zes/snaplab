/* SnapLab interface strings: the lookup function. The strings themselves are in en.js, ja.js, ... in this folder;
   the server serves them all as one /i18n.js (core first, then English, then the other languages). */
"use strict";
window.SNAPLAB_I18N = window.SNAPLAB_I18N || {};
window.snapPluralAliases = {
  "rc.file1": "rc.files", "projects.session1": "projects.sessions",
  "session.turn1": "session.turns", "work.count1": "work.count",
  "bots.project1": "bots.projects", "chats.questions.n": "chats.questions",
  "ev.files.1": "ev.files.n"
};
const snapPluralRules = {};
window.snapT = function (lang, key, vars) {
  var all = window.SNAPLAB_I18N;
  var d = all[lang] || all.en;
  var base = window.snapPluralAliases[key] || key;
  var n = vars && vars.n !== undefined ? vars.n : (window.snapPluralAliases[key] && key !== "chats.questions.n" ? 1 : undefined);
  if (n !== undefined) {
    var rule = snapPluralRules[lang] || (snapPluralRules[lang] = new Intl.PluralRules((d["lang.locale"] || lang).replace("_", "-")));
    var plural = base + "." + rule.select(Number(n));
    if (d[plural] !== undefined) { key = plural; vars = Object.assign({}, vars, {n: n}); }
  }
  var s = d[key];
  if (s === undefined) s = all.en[key];          // a missing string falls back to English
  if (s === undefined) s = key;
  if (vars) s = s.replace(/\{(\w+)\}/g, function (_, k) { return vars[k] !== undefined ? vars[k] : "{" + k + "}"; });
  return s;
};
// Keep the UI language available to the offline and pairing pages without storing any chat data.
window.snapRememberLang = function (lang) {
  try { localStorage.setItem("snaplab-lang", lang); } catch (_) {}
  document.cookie = "snaplab_lang=" + encodeURIComponent(lang) + "; Path=/; SameSite=Strict; Max-Age=31536000";
};
window.snapBrowserLang = function () {
  var saved;
  try { saved = localStorage.getItem("snaplab-lang"); } catch (_) {}
  return window.snapPickLang(saved || navigator.language);
};
/* The languages that have a file, in the order the server sent them: [{code, name, hello, locale}]. */
window.snapLangs = function () {
  var all = window.SNAPLAB_I18N;
  return Object.keys(all).map(function (code) {
    var d = all[code];
    return { code: code, name: d["lang.name"] || code, hello: d["lang.hello"] || "", locale: d["lang.locale"] || code };
  });
};
/* The best language for this browser among the ones available (English when none matches). */
window.snapPickLang = function (wanted) {
  var codes = Object.keys(window.SNAPLAB_I18N);
  var w = String(wanted || "").toLowerCase();
  if (codes.indexOf(w) >= 0) return w;
  var base = w.split("-")[0];
  return codes.indexOf(base) >= 0 ? base : "en";
};

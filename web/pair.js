/* Pairing page: the token is used only when the button is pressed (link previews must not consume it). */
(function () {
  "use strict";
  var btn = document.getElementById("pair-go"), msg = document.getElementById("pair-msg");
  if (!btn) return;
  var lang = document.documentElement.lang || "en";
  var t = function (key) { return window.snapT(lang, key); };
  var token = location.pathname.replace(/^\/pair\//, "").replace(/\/$/, "");
  btn.addEventListener("click", function () {
    btn.disabled = true; msg.textContent = t("pair.working");
    fetch("/api/pair", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-SnapLab": "1" }, body: JSON.stringify({ token: token })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok && j.ok, j: j }; }); })
      .then(function (x) {
        if (x.ok) {
          msg.textContent = t("pair.done");
          history.replaceState(null, "", "/");
          setTimeout(function () { location.replace("/"); }, 450);
        } else { msg.textContent = lang === "en" && x.j.error ? x.j.error : t("pair.failed"); btn.disabled = false; }
      }).catch(function () { msg.textContent = t("pair.network"); btn.disabled = false; });
  });
})();

/* The launch capability stays in a URL fragment, out of request logs and referrers. */
(async () => {
  const token = location.hash.slice(1);
  history.replaceState(null, "", "/local");
  const lang = window.snapBrowserLang();
  if (lang !== 'en') {
    document.documentElement.lang = lang;
    document.getElementById('local-status').textContent = window.snapT(lang, 'local.opening');
    document.querySelector('h1').textContent = 'SnapLab';
    document.title = 'SnapLab';
  }
  try {
    if (!token) throw new Error("Run the SnapLab launch command again to open this browser.");
    const response = await fetch("/api/pair", {method: "POST", headers: {"Content-Type": "application/json", "X-SnapLab": "1"}, body: JSON.stringify({token})});
    if (!response.ok) throw new Error("This link has expired or was already used. Run the SnapLab launch command again.");
    location.replace("/");
  } catch (error) { document.getElementById("local-status").textContent = lang !== 'en' ? window.snapT(lang, 'local.retry') : error.message; }
})();

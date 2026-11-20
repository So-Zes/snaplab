(() => {
  const lang = window.snapBrowserLang();
  if (lang === 'en') return; // The page itself is in English.
  const t = key => window.snapT(lang, key);
  document.documentElement.lang = lang;
  document.title = t('offline.title') + ' · SnapLab';
  document.querySelector('h1').textContent = t('offline.title');
  document.querySelector('main p').textContent = t('offline.body');
  document.querySelector('.btn').textContent = t('offline.retry');
})();

/**
 * CentienC — Global Update Notifier
 * Silently checks /api/update-check (15-minute session cache) and
 * adds a pulsing orange dot badge to every "Updates" nav link when
 * a newer release is available on GitHub.
 */
(function () {
  'use strict';

  var CACHE_KEY = 'centc_upd_v1';
  var CACHE_TTL = 900 * 1000; // 15 minutes

  function injectStyles() {
    if (document.getElementById('upd-notifier-css')) return;
    var s = document.createElement('style');
    s.id = 'upd-notifier-css';
    s.textContent =
      '.upd-dot{display:inline-block;width:7px;height:7px;' +
      'background:#f0883e;border-radius:50%;margin-left:4px;' +
      'vertical-align:middle;flex-shrink:0;' +
      'animation:upd-glow 2s ease-in-out infinite}' +
      '@keyframes upd-glow{0%,100%{opacity:1;transform:scale(1)}' +
      '50%{opacity:.5;transform:scale(.85)}}';
    document.head.appendChild(s);
  }

  function addBadges() {
    injectStyles();
    document.querySelectorAll('a[href="/updates"]').forEach(function (el) {
      if (el.querySelector('.upd-dot')) return;
      var dot = document.createElement('span');
      dot.className = 'upd-dot';
      dot.title = 'Update available';
      el.appendChild(dot);
    });
  }

  function run() {
    // Check session cache first
    try {
      var raw = sessionStorage.getItem(CACHE_KEY);
      if (raw) {
        var cached = JSON.parse(raw);
        if (Date.now() - cached.ts < CACHE_TTL) {
          if (cached.update_available) addBadges();
          return;
        }
      }
    } catch (e) { /* ignore */ }

    fetch('/api/update-check')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        try {
          sessionStorage.setItem(CACHE_KEY, JSON.stringify({
            ts: Date.now(),
            update_available: !!data.update_available,
            latest: data.latest_version || ''
          }));
        } catch (e) { /* storage quota */ }
        if (data.update_available) addBadges();
      })
      .catch(function () { /* network errors are silent */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();

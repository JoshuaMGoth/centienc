/* CentienC Visitor Analytics tracker
 *
 * Drop-in beacon for static sites. Privacy-first: anonymous IDs only,
 * no cookies for identity, no PII collected, honours DNT and bot UAs.
 *
 * Usage — paste before </body> on every page:
 *   <script src="https://YOUR-CENTIENC:9099/centience-track.js?site=SITE_KEY" defer></script>
 * or with a data attribute:
 *   <script src="https://YOUR-CENTIENC:9099/centience-track.js" data-site="SITE_KEY" defer></script>
 *
 * Optional: data-endpoint overrides the ingest URL entirely.
 */
(function () {
  'use strict';

  var script = document.currentScript;
  if (!script) return;

  // ── Configuration ──────────────────────────────────────────────
  var src = script.getAttribute('src') || '';
  var q = src.indexOf('?') !== -1 ? src.slice(src.indexOf('?') + 1) : '';
  var params = new URLSearchParams(q);
  var siteKey = script.getAttribute('data-site') || params.get('site') || '';
  var endpoint = script.getAttribute('data-endpoint') || '';
  if (!endpoint && siteKey) {
    try {
      endpoint = new URL(src, location.href).origin + '/api/track/' + encodeURIComponent(siteKey);
    } catch (e) { return; }
  }
  if (!endpoint || !siteKey) return;

  // ── Respect privacy & skip bots ────────────────────────────────
  if (navigator.doNotTrack === '1' || navigator.globalPrivacyControl) return;
  if (/bot|spider|crawler|slurp|facebookexternalhit|preview|headless|phantom|selenium|lighthouse|pingdom|uptime|monitor/i.test(navigator.userAgent || '')) return;
  if (navigator.webdriver) return;

  // ── IDs ────────────────────────────────────────────────────────
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }
  var vid = null, sid = null, sidStarted = 0;
  try {
    vid = localStorage.getItem('cc_vid');
    if (!vid) { vid = uuid(); localStorage.setItem('cc_vid', vid); }
    sid = sessionStorage.getItem('cc_sid');
    sidStarted = parseInt(sessionStorage.getItem('cc_sid_ts') || '0', 10);
  } catch (e) { /* storage unavailable — fall back to memory */ }
  if (!vid) vid = uuid();
  var SESSION_MS = 30 * 60 * 1000; // 30-minute session window
  function ensureSession() {
    var now = Date.now();
    if (!sid || !sidStarted || now - sidStarted > SESSION_MS) {
      sid = uuid(); sidStarted = now;
      try {
        sessionStorage.setItem('cc_sid', sid);
        sessionStorage.setItem('cc_sid_ts', String(now));
      } catch (e) {}
    }
  }
  ensureSession();

  // ── Engagement tracking ────────────────────────────────────────
  var pageEnter = Date.now();
  var engagedMs = 0;          // visible time on current page
  var sessionEngagedMs = 0;   // visible time across the whole session
  var lastVisible = document.visibilityState === 'visible' ? Date.now() : 0;

  function accrueEngagement() {
    if (lastVisible) {
      var d = Date.now() - lastVisible;
      engagedMs += d;
      sessionEngagedMs += d;
      lastVisible = document.visibilityState === 'visible' ? Date.now() : 0;
    }
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      lastVisible = Date.now();
    } else {
      accrueEngagement();
      send('beat');
    }
  });

  var maxScroll = 0;
  function scrollDepth() {
    var h = document.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    if (max <= 0) return 100;
    return Math.min(100, Math.round((h.scrollTop + h.clientHeight) / max * 100));
  }
  var scrollTick = false;
  window.addEventListener('scroll', function () {
    if (scrollTick) return;
    scrollTick = true;
    setTimeout(function () {
      scrollTick = false;
      var d = scrollDepth();
      if (d > maxScroll) maxScroll = d;
    }, 250);
  }, { passive: true });

  // ── UTM / campaign capture (first landing only) ────────────────
  var utm = { source: '', medium: '', campaign: '' };
  try {
    var stored = sessionStorage.getItem('cc_utm');
    if (stored) {
      utm = JSON.parse(stored);
    } else {
      var u = new URLSearchParams(location.search);
      utm.source = (u.get('utm_source') || '').slice(0, 120);
      utm.medium = (u.get('utm_medium') || '').slice(0, 120);
      utm.campaign = (u.get('utm_campaign') || '').slice(0, 120);
      if (utm.source || utm.medium || utm.campaign) {
        sessionStorage.setItem('cc_utm', JSON.stringify(utm));
      }
    }
  } catch (e) {}

  function referrerHost() {
    try {
      if (!document.referrer) return '';
      var r = new URL(document.referrer);
      if (r.host === location.host) return '';
      return r.href.slice(0, 500);
    } catch (e) { return ''; }
  }

  function loadMs() {
    try {
      var n = performance.getEntriesByType('navigation')[0];
      if (n && n.loadEventEnd > 0) return Math.round(n.loadEventEnd);
      if (performance.timing) return Math.max(0, performance.timing.loadEventEnd - performance.timing.navigationStart);
    } catch (e) {}
    return null;
  }

  // ── Send ───────────────────────────────────────────────────────
  function payload(kind, extra) {
    ensureSession();
    accrueEngagement();
    var p = {
      v: vid, s: sid, k: kind,
      path: location.pathname + location.search,
      title: document.title || '',
      ref: referrerHost(),
      utm_source: utm.source, utm_medium: utm.medium, utm_campaign: utm.campaign,
      eng: Math.round(engagedMs / 1000),
      seng: Math.round(sessionEngagedMs / 1000),
      scroll: Math.max(maxScroll, scrollDepth()),
      screen: (screen.width || 0) + 'x' + (screen.height || 0),
      vw: window.innerWidth || 0,
      lang: (navigator.language || '').slice(0, 20),
      tz: (Intl.DateTimeFormat().resolvedOptions().timeZone || '').slice(0, 40),
      ts: Date.now()
    };
    if (kind === 'pv') { p.load = loadMs(); maxScroll = 0; }
    if (kind === 'evt' && extra) { p.name = extra.name; p.value = String(extra.value || '').slice(0, 250); }
    return p;
  }

  function send(kind, extra) {
    try {
      var body = JSON.stringify(payload(kind, extra));
      if (navigator.sendBeacon) {
        var blob = new Blob([body], { type: 'application/json' });
        if (navigator.sendBeacon(endpoint, blob)) return;
      }
      fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true,
        credentials: 'omit'
      }).catch(function () {});
    } catch (e) {}
  }

  // Public API for sites that want custom events
  window.centience = window.centience || {};
  window.centience.track = function (name, value) {
    if (name) send('evt', { name: String(name).slice(0, 80), value: value });
  };

  // ── Lifecycle ──────────────────────────────────────────────────
  send('pv');

  // SPA navigations
  function onNav() {
    pageEnter = Date.now();
    engagedMs = 0; maxScroll = 0;
    lastVisible = document.visibilityState === 'visible' ? Date.now() : 0;
    send('pv');
  }
  ['pushState', 'replaceState'].forEach(function (fn) {
    var orig = history[fn];
    if (typeof orig === 'function') {
      history[fn] = function () {
        var r = orig.apply(this, arguments);
        setTimeout(onNav, 0);
        return r;
      };
    }
  });
  window.addEventListener('popstate', onNav);

  // Heartbeat every 15s while visible
  setInterval(function () {
    if (document.visibilityState === 'visible') send('beat');
  }, 15000);

  // Exit signals
  window.addEventListener('pagehide', function () { send('exit'); });
  window.addEventListener('beforeunload', function () { send('exit'); });
})();

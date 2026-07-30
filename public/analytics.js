(function () {
  'use strict';

  var PREFERENCE_KEY = 'brevitas_analytics';
  var SENSITIVE_SELECTOR = '[data-ph-sensitive],.ph-sensitive,.ph-no-capture,[data-private]';
  var pending = [];

  // This one file is loaded by BOTH the marketing pages and the authenticated
  // SPA, and the two need opposite replay defaults. A marketing visit is
  // anonymous and must keep being counted; an authenticated view renders the
  // customer's own content — Audit shows scanner evidence taken from their
  // repository, Overview/Projects/Pipelines show their spend — and replaying that
  // into a subprocessor is a data export, not analytics.
  //
  // The discriminator is the SPA's own <body class="dashboard-app">
  // (dashboard/index.html, and the built public/dashboard/index.html), which no
  // static page carries. NOT the data-brevitas-analytics attribute: every public
  // page carries that too, so it identifies the bootstrap, not the host. The
  // path list is the second half of the same test, because next.config.ts rewrites
  // all of those aliases onto the SPA — plus /email-confirmed and /welcome, which
  // are static but receive GoTrue's access token in the URL and so must never be
  // recorded either.
  var APP_ROUTE = /^\/(?:dashboard|login|signup|waitlist|invite|email-confirmed|welcome)(?:\/|$)/;
  var hostIsApp = null;
  function hostIsApplication() {
    if (hostIsApp === null) {
      try {
        hostIsApp = /\bdashboard-app\b/.test((document.body && document.body.className) || '') ||
          APP_ROUTE.test(location.pathname);
      } catch (_) {
        // Fail closed: an unrecognised host is treated as the authenticated one.
        hostIsApp = true;
      }
    }
    return hostIsApp;
  }

  function privacySignalEnabled() {
    // GPC only. It is legally binding under CCPA/CPRA (California) and the Colorado and
    // Connecticut privacy acts, so it must be honoured. Do Not Track is deliberately NOT
    // checked: it carries no legal force in any jurisdiction, the industry abandoned it,
    // PostHog's own default is to ignore it (respect_dnt: false), and Firefox enables it
    // for many users who never made a specific choice — so honouring it silently discarded
    // real, countable traffic for no compliance benefit.
    return navigator.globalPrivacyControl === true;
  }

  function storedPreference() {
    try { return localStorage.getItem(PREFERENCE_KEY); } catch (_) { return null; }
  }

  function analyticsEnabled() {
    if (privacySignalEnabled()) return false;
    return storedPreference() !== 'off';
  }

  function cleanUrl(value) {
    if (!value || typeof value !== 'string') return value;
    try {
      var url = new URL(value, location.origin);
      return url.origin + url.pathname;
    } catch (_) {
      return value.split(/[?#]/)[0];
    }
  }

  // Keys the SDK owns and populates itself. The sensitivity filter below exists to keep
  // *application* properties clean; applying it to the SDK's own keys breaks ingestion.
  // Critically, posthog-js carries the project API key as the `token` PROPERTY and builds
  // the request body as `{api_key: properties.token, batch, sent_at}` — sanitize_properties
  // replaces the whole property bag, so dropping `token` yields `api_key: undefined`, which
  // JSON.stringify omits entirely and PostHog rejects with 400. That silently discards every
  // event. `$`-prefixed keys are SDK internals ($feature_flag_response, $session_entry_*) and
  // the campaign params are collected by the SDK, not by us.
  var SDK_RESERVED_KEY = /^\$|^(?:token|distinct_id)$|^utm_[a-z]+$|^(?:gclid|gad_source|fbclid|msclkid|twclid|dclid|igshid|ttclid|li_fat_id|mc_cid|rdt_cid|epik|qclid|sccid|irclid)$/;
  var SENSITIVE_KEY = /token|secret|password|authorization|api.?key|prompt|response|content|email/i;

  function sanitizeProperties(properties) {
    if (!properties || typeof properties !== 'object') return properties;
    var safe = {};
    Object.keys(properties).forEach(function (key) {
      if (!SDK_RESERVED_KEY.test(key) && SENSITIVE_KEY.test(key)) return;
      safe[key] = /url/i.test(key) ? cleanUrl(properties[key]) : properties[key];
    });
    return safe;
  }

  // Fires the initial $pageview and one more per SPA navigation. The SDK's own
  // 'history_change' mode is unusable here: it defers to a historyAutocapture chunk this
  // bootstrap never loads, so it recorded no $pageview at all. The dashboard is a
  // client-side SPA that routes via history.replaceState, so without this every in-app
  // navigation would be invisible to path and funnel analysis.
  function startPageviewTracking(ph) {
    var lastLocation = location.pathname + location.search;

    function capturePageview() {
      var current = location.pathname + location.search;
      // Guard against SPA routers that re-write the same URL repeatedly, which would
      // otherwise inflate pageview counts.
      if (current === lastLocation) return;
      lastLocation = current;
      ph.capture('$pageview');
    }

    ph.capture('$pageview');

    ['pushState', 'replaceState'].forEach(function (method) {
      var original = history[method];
      if (typeof original !== 'function') return;
      history[method] = function () {
        var result = original.apply(this, arguments);
        try { capturePageview(); } catch (_) { /* never break navigation */ }
        return result;
      };
    });
    window.addEventListener('popstate', capturePageview);
  }

  function call(method, args) {
    if (window.posthog && typeof window.posthog[method] === 'function') {
      window.posthog[method].apply(window.posthog, args);
    } else {
      pending.push([method, args]);
    }
  }

  function capture(event, properties) {
    if (!analyticsEnabled()) return;
    call('capture', [event, sanitizeProperties(properties || {})]);
  }

  function identify(userId, properties) {
    if (!analyticsEnabled() || !userId) return;
    call('identify', [String(userId), sanitizeProperties(properties || {})]);
  }

  function reset() {
    call('reset', []);
  }

  function setEnabled(enabled) {
    try { localStorage.setItem(PREFERENCE_KEY, enabled ? 'on' : 'off'); } catch (_) {}
    if (enabled && !privacySignalEnabled()) {
      call('opt_in_capturing', []);
      // startSessionRecording() overrides disable_session_recording, so on the
      // authenticated SPA it would re-arm exactly what the init below declines.
      // Consenting to analytics is not consenting to replay of another company's
      // data.
      if (!hostIsApplication()) call('startSessionRecording', []);
      capture('analytics_preference_changed', { enabled: true });
    } else {
      call('stopSessionRecording', []);
      call('opt_out_capturing', []);
      reset();
    }
    renderPrivacyControls();
  }

  window.brevitasAnalytics = {
    capture: capture,
    identify: identify,
    reset: reset,
    setEnabled: setEnabled,
    isEnabled: analyticsEnabled,
  };

  function renderPrivacyControls() {
    var existing = document.getElementById('brevitas-privacy-controls');
    if (existing) existing.remove();

    var signal = privacySignalEnabled();
    var preference = storedPreference();
    var wrapper = document.createElement('div');
    wrapper.id = 'brevitas-privacy-controls';
    wrapper.innerHTML =
      '<button class="bvt-privacy-button" type="button" aria-expanded="false" aria-controls="brevitas-privacy-panel">Privacy choices</button>' +
      '<section class="bvt-privacy-panel" id="brevitas-privacy-panel" hidden role="dialog" aria-modal="true" aria-label="Analytics privacy choices">' +
        '<strong>Analytics &amp; masked replay</strong>' +
        '<p>We use PostHog to understand visits and improve Brevitas. Inputs, secrets, account details, and network contents are excluded or masked.</p>' +
        (signal ? '<p class="bvt-privacy-signal">Your browser privacy signal is active, so analytics is off.</p>' : '') +
        '<div class="bvt-privacy-actions">' +
          '<button type="button" data-choice="on"' + (signal ? ' disabled' : '') + '>Allow analytics</button>' +
          '<button type="button" data-choice="off">Turn off</button>' +
          '<button class="bvt-privacy-close" type="button" data-close>Close</button>' +
          '<a href="/privacy">Privacy policy</a>' +
        '</div>' +
      '</section>' +
      (preference === null && !signal ?
        '<section class="bvt-privacy-notice" aria-label="Analytics notice"><span>We use analytics and strictly masked session replay. You can turn it off at any time.</span><button type="button" data-choice="on">Got it</button><button type="button" data-open>Choices</button></section>' : '');
    document.body.appendChild(wrapper);

    var button = wrapper.querySelector('.bvt-privacy-button');
    var panel = wrapper.querySelector('.bvt-privacy-panel');
    var notice = wrapper.querySelector('.bvt-privacy-notice');
    function toggle(open) {
      panel.hidden = !open;
      if (notice) notice.hidden = open;
      button.setAttribute('aria-expanded', String(open));
      if (open) {
        var firstAction = panel.querySelector('button:not([disabled]), a');
        if (firstAction) firstAction.focus();
      }
    }
    button.addEventListener('click', function () { toggle(panel.hidden); });
    wrapper.querySelectorAll('[data-open]').forEach(function (node) {
      node.addEventListener('click', function () { toggle(true); });
    });
    wrapper.querySelectorAll('[data-close]').forEach(function (node) {
      node.addEventListener('click', function () { toggle(false); button.focus(); });
    });
    wrapper.querySelectorAll('[data-choice]').forEach(function (node) {
      node.addEventListener('click', function () { setEnabled(node.dataset.choice === 'on'); });
    });
  }

  function loadStyles() {
    if (document.querySelector('link[href="/analytics.css"]')) return;
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/analytics.css';
    document.head.appendChild(link);
  }

  function loadPostHog(config) {
    if (!config.enabled || !config.projectToken) return;
    // PostHog's queueing bootstrap lets calls made during initial page render wait
    // safely for the asynchronously loaded SDK.
    (function (documentObject, posthog) {
      if (posthog.__SV) return;
      window.posthog = posthog;
      posthog._i = [];
      posthog.init = function (token, options, name) {
        function stub(target, method) {
          target[method] = function () {
            target.push([method].concat(Array.prototype.slice.call(arguments)));
          };
        }
        var script = documentObject.createElement('script');
        script.type = 'text/javascript';
        script.crossOrigin = 'anonymous';
        script.async = true;
        script.src = options.api_host + '/static/array.js';
        documentObject.head.appendChild(script);
        var instance = name ? (posthog[name] = []) : posthog;
        var methods = 'capture identify reset opt_in_capturing opt_out_capturing has_opted_out_capturing startSessionRecording stopSessionRecording set_config register unregister'.split(' ');
        instance.people = instance.people || [];
        methods.forEach(function (method) { stub(instance, method); });
        posthog._i.push([token, options, name]);
      };
      posthog.__SV = 1;
    })(document, window.posthog || []);

    window.posthog.init(config.projectToken, {
        api_host: config.apiHost,
        ui_host: config.uiHost,
        defaults: '2026-05-30',
        autocapture: true,
        // We fire $pageview ourselves from `loaded` below rather than letting the SDK do it.
        // Both SDK-driven modes were verified failing on a real page load here: `true` and
        // 'history_change' each produced $pageleave and $set but ZERO $pageview, because the
        // SDK's own initial-pageview call sits behind a consent check that a visitor who
        // never touches the privacy banner does not satisfy. Every visit must count, so this
        // must not depend on banner interaction. `false` also disables the SDK's pageview
        // call inside opt_in_capturing(), so a visitor who later clicks "Allow analytics"
        // cannot produce a second $pageview for the same page load.
        capture_pageview: false,
        loaded: function (ph) {
          // One $pageview per page load, for every visitor who has not opted out. Visitors
          // sending GPC, or who chose "Turn off", are already opted out by
          // opt_out_capturing_by_default below and the capture is dropped by the SDK; the
          // analyticsEnabled() guard just avoids queueing work we know will be discarded.
          if (analyticsEnabled()) startPageviewTracking(ph);
        },
        capture_pageleave: true,
        capture_exceptions: true,
        person_profiles: 'identified_only',
        opt_out_capturing_by_default: !analyticsEnabled(),
        sanitize_properties: sanitizeProperties,
        // Session replay is never armed on the authenticated SPA. rrweb
        // serialises the rendered DOM, and on those views the DOM IS customer
        // content (Audit renders capability.detail / capability.evidence[], which
        // the repository scanner fills with file paths and code excerpts from the
        // customer's own codebase). Whether the recorder runs otherwise depends
        // on a toggle inside the PostHog project, so declining it here is the only
        // control the product itself has. Marketing pages are unchanged.
        disable_session_recording: hostIsApplication(),
        session_recording: {
          maskAllInputs: true,
          // Default-DENY text on the SPA: element-level markers are applied
          // inconsistently across dashboard/src/components (Audit, Overview,
          // Projects and Pipelines carry none), so an opt-in policy leaks by
          // omission every time a component is added. '*' is posthog-js's
          // mask-everything selector — there is no `maskAllText` option, it would
          // be silently ignored. Belt and braces with the flag above, which a
          // stray startSessionRecording() call could otherwise defeat.
          maskTextSelector: hostIsApplication() ? '*' : SENSITIVE_SELECTOR,
          recordCrossOriginIframes: false,
          maskCapturedNetworkRequestFn: function (request) {
            request.name = cleanUrl(request.name || '');
            delete request.requestBody;
            delete request.responseBody;
            delete request.requestHeaders;
            delete request.responseHeaders;
            return request;
          },
        },
      });
    pending.splice(0).forEach(function (item) { call(item[0], item[1]); });
    if (!analyticsEnabled()) window.posthog.opt_out_capturing();
  }

  function start() {
    loadStyles();
    renderPrivacyControls();
    fetch('/api/analytics-config', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (response) { return response.ok ? response.json() : Promise.reject(new Error('analytics config unavailable')); })
      .then(loadPostHog)
      .catch(function () { /* Analytics must never affect the product. */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();

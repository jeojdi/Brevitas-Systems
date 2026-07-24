(function () {
  'use strict';

  var PREFERENCE_KEY = 'brevitas_analytics';
  var SENSITIVE_SELECTOR = '[data-ph-sensitive],.ph-sensitive,.ph-no-capture,[data-private]';
  var pending = [];

  function privacySignalEnabled() {
    return navigator.globalPrivacyControl === true || navigator.doNotTrack === '1' || window.doNotTrack === '1';
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

  function sanitizeProperties(properties) {
    if (!properties || typeof properties !== 'object') return properties;
    var safe = {};
    Object.keys(properties).forEach(function (key) {
      if (/token|secret|password|authorization|api.?key|prompt|response|content/i.test(key)) return;
      safe[key] = /url/i.test(key) ? cleanUrl(properties[key]) : properties[key];
    });
    return safe;
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
      call('startSessionRecording', []);
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
        capture_pageview: true,
        capture_pageleave: true,
        capture_exceptions: true,
        person_profiles: 'identified_only',
        opt_out_capturing_by_default: !analyticsEnabled(),
        sanitize_properties: sanitizeProperties,
        session_recording: {
          maskAllInputs: true,
          maskTextSelector: SENSITIVE_SELECTOR,
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

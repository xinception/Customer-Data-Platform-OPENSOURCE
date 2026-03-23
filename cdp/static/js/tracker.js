/**
 * CDP Client-Side Tracker
 *
 * Lightweight, privacy-respecting tracker for the Customer Data Platform.
 * No external dependencies. Designed for production use (<5KB minified).
 *
 * Usage:
 *   CDP.init({ endpoint: '/api/v1/track', autoPageView: true });
 *   CDP.track('button_click', { label: 'Sign Up' });
 *   CDP.identify('user-123', { email: 'user@example.com' });
 */
(function (window, document) {
  "use strict";

  // -----------------------------------------------------------------------
  // Constants
  // -----------------------------------------------------------------------
  var COOKIE_VISITOR = "_cdp_vid";
  var COOKIE_SESSION = "_cdp_sid";
  var COOKIE_CONSENT = "_cdp_consent";
  var SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
  var VISITOR_MAX_AGE_DAYS = 365;
  var QUEUE_FLUSH_INTERVAL_MS = 2000;
  var MAX_QUEUE_SIZE = 100;

  // -----------------------------------------------------------------------
  // Internal state
  // -----------------------------------------------------------------------
  var config = {
    endpoint: "/api/v1/track",
    autoPageView: true,
    trackClicks: true,
    trackScrollDepth: true,
    trackTimeOnPage: true,
    crossDomainDomains: [],
    cookieDomain: "",
    cookiePath: "/",
    cookieSecure: location.protocol === "https:",
    respectDNT: true,
    debug: false
  };

  var state = {
    initialized: false,
    visitorId: null,
    sessionId: null,
    sessionStart: 0,
    lastActivity: 0,
    userId: null,
    traits: {},
    consent: null, // null = not decided, object = categories
    queue: [],       // events queued before consent
    sendQueue: [],   // events ready to send
    pageEnteredAt: 0,
    maxScrollDepth: 0,
    utm: {}
  };

  var flushTimer = null;

  // -----------------------------------------------------------------------
  // Utility helpers
  // -----------------------------------------------------------------------
  function generateId() {
    // RFC 4122 v4-ish UUID without crypto dependency
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    var d = Date.now();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (d + Math.random() * 16) % 16 | 0;
      d = Math.floor(d / 16);
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function now() {
    return Date.now();
  }

  function log() {
    if (config.debug && window.console && console.log) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift("[CDP]");
      console.log.apply(console, args);
    }
  }

  // -----------------------------------------------------------------------
  // Cookie helpers
  // -----------------------------------------------------------------------
  function setCookie(name, value, days) {
    var parts = [name + "=" + encodeURIComponent(value), "path=" + config.cookiePath];
    if (days) {
      var d = new Date();
      d.setTime(d.getTime() + days * 86400000);
      parts.push("expires=" + d.toUTCString());
    }
    if (config.cookieDomain) {
      parts.push("domain=" + config.cookieDomain);
    }
    if (config.cookieSecure) {
      parts.push("Secure");
    }
    parts.push("SameSite=Lax");
    document.cookie = parts.join("; ");
  }

  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function deleteCookie(name) {
    setCookie(name, "", -1);
  }

  // -----------------------------------------------------------------------
  // Visitor & session management
  // -----------------------------------------------------------------------
  function getOrCreateVisitor() {
    var vid = getCookie(COOKIE_VISITOR);
    if (!vid) {
      vid = generateId();
      setCookie(COOKIE_VISITOR, vid, VISITOR_MAX_AGE_DAYS);
      log("New visitor:", vid);
    }
    state.visitorId = vid;
    return vid;
  }

  function getOrCreateSession() {
    var sid = getCookie(COOKIE_SESSION);
    var sessionValid = sid && state.lastActivity && (now() - state.lastActivity < SESSION_TIMEOUT_MS);

    if (!sessionValid) {
      sid = generateId();
      state.sessionStart = now();
      log("New session:", sid);
    }

    state.sessionId = sid;
    state.lastActivity = now();
    // Session cookie — no explicit expiry (browser session), but we set max
    // age so the server-side timeout wins.
    setCookie(COOKIE_SESSION, sid, 0);
    return sid;
  }

  function touchSession() {
    state.lastActivity = now();
    setCookie(COOKIE_SESSION, state.sessionId, 0);
  }

  // -----------------------------------------------------------------------
  // UTM parameter capture
  // -----------------------------------------------------------------------
  function captureUTM() {
    var params = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"];
    var search = window.location.search;
    if (!search) return;
    var qs = search.substring(1).split("&");
    for (var i = 0; i < qs.length; i++) {
      var pair = qs[i].split("=");
      var key = decodeURIComponent(pair[0]);
      if (params.indexOf(key) !== -1 && pair[1]) {
        state.utm[key] = decodeURIComponent(pair[1]);
      }
    }
  }

  // -----------------------------------------------------------------------
  // Consent management
  // -----------------------------------------------------------------------
  function loadConsent() {
    var raw = getCookie(COOKIE_CONSENT);
    if (raw) {
      try {
        state.consent = JSON.parse(raw);
      } catch (e) {
        state.consent = null;
      }
    }
  }

  function saveConsent(categories) {
    state.consent = categories;
    setCookie(COOKIE_CONSENT, JSON.stringify(categories), VISITOR_MAX_AGE_DAYS);
  }

  function hasConsent(category) {
    if (!state.consent) return false;
    if (category === "necessary") return true;
    return !!state.consent[category];
  }

  function canTrack() {
    // If consent has not been decided, events are queued but not sent.
    if (state.consent === null) return false;
    return hasConsent("analytics");
  }

  // -----------------------------------------------------------------------
  // Event construction
  // -----------------------------------------------------------------------
  function buildEvent(eventType, eventName, properties) {
    var evt = {
      event_id: generateId(),
      event_type: eventType,
      event_name: eventName || "",
      visitor_id: state.visitorId,
      session_id: state.sessionId,
      customer_id: state.userId || null,
      source: "web",
      timestamp: now() / 1000,
      properties: properties || {},
      context: {
        page: {
          url: location.href,
          path: location.pathname,
          title: document.title,
          referrer: document.referrer
        },
        user_agent: navigator.userAgent,
        language: navigator.language || "",
        screen: {
          width: screen.width,
          height: screen.height
        },
        utm: state.utm,
        traits: state.traits
      }
    };
    return evt;
  }

  // -----------------------------------------------------------------------
  // Event sending (Beacon API with XHR fallback)
  // -----------------------------------------------------------------------
  function sendEvents(events) {
    if (!events.length) return;

    var payload = JSON.stringify({ events: events });
    var url = config.endpoint;

    // Prefer Beacon API for reliability during page unload.
    if (navigator.sendBeacon) {
      var blob = new Blob([payload], { type: "application/json" });
      var sent = navigator.sendBeacon(url, blob);
      if (sent) {
        log("Beacon sent", events.length, "events");
        return;
      }
    }

    // Fallback: XHR
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.send(payload);
      log("XHR sent", events.length, "events");
    } catch (e) {
      log("Send failed", e);
    }
  }

  function enqueue(event) {
    if (canTrack()) {
      state.sendQueue.push(event);
      if (state.sendQueue.length >= MAX_QUEUE_SIZE) {
        flush();
      }
    } else {
      // Queue until consent is given.
      state.queue.push(event);
      if (state.queue.length > MAX_QUEUE_SIZE) {
        state.queue.shift(); // drop oldest
      }
    }
  }

  function flush() {
    if (state.sendQueue.length === 0) return;
    var batch = state.sendQueue.splice(0, MAX_QUEUE_SIZE);
    sendEvents(batch);
  }

  function drainPreConsentQueue() {
    // Move pre-consent events to the send queue.
    while (state.queue.length) {
      state.sendQueue.push(state.queue.shift());
    }
    flush();
  }

  function startFlushTimer() {
    if (flushTimer) return;
    flushTimer = setInterval(flush, QUEUE_FLUSH_INTERVAL_MS);
  }

  // -----------------------------------------------------------------------
  // Auto-tracking: clicks
  // -----------------------------------------------------------------------
  function setupClickTracking() {
    if (!config.trackClicks) return;

    document.addEventListener("click", function (e) {
      var target = e.target;
      // Walk up to find the nearest anchor or button.
      var el = target;
      for (var i = 0; i < 5 && el && el !== document; i++) {
        if (el.tagName === "A" || el.tagName === "BUTTON") break;
        el = el.parentElement;
      }
      if (!el || (el.tagName !== "A" && el.tagName !== "BUTTON")) return;

      var props = {
        tag: el.tagName,
        text: (el.innerText || "").substring(0, 255),
        href: el.href || null,
        id: el.id || null,
        classes: el.className || null
      };
      enqueue(buildEvent("track", "element_click", props));
      touchSession();
    }, true);
  }

  // -----------------------------------------------------------------------
  // Auto-tracking: scroll depth
  // -----------------------------------------------------------------------
  function setupScrollTracking() {
    if (!config.trackScrollDepth) return;

    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        var docHeight = Math.max(
          document.body.scrollHeight,
          document.documentElement.scrollHeight
        );
        var winHeight = window.innerHeight;
        var scrollTop = window.pageYOffset || document.documentElement.scrollTop;
        var depth = docHeight > winHeight ? Math.round((scrollTop + winHeight) / docHeight * 100) : 100;
        if (depth > state.maxScrollDepth) {
          state.maxScrollDepth = depth;
        }
        ticking = false;
      });
    }, { passive: true });
  }

  // -----------------------------------------------------------------------
  // Auto-tracking: time on page
  // -----------------------------------------------------------------------
  function setupTimeOnPage() {
    if (!config.trackTimeOnPage) return;
    state.pageEnteredAt = now();

    // Send time-on-page + scroll depth when leaving.
    function onLeave() {
      var timeSpent = Math.round((now() - state.pageEnteredAt) / 1000);
      var evt = buildEvent("track", "page_leave", {
        time_on_page: timeSpent,
        scroll_depth: state.maxScrollDepth
      });
      // Use beacon for unload reliability.
      var payload = JSON.stringify({ events: [evt] });
      if (navigator.sendBeacon) {
        navigator.sendBeacon(config.endpoint, new Blob([payload], { type: "application/json" }));
      }
    }

    // visibilitychange is more reliable than beforeunload in modern browsers.
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        onLeave();
      }
    });
    window.addEventListener("pagehide", onLeave);
  }

  // -----------------------------------------------------------------------
  // Cross-domain tracking
  // -----------------------------------------------------------------------
  function setupCrossDomain() {
    if (!config.crossDomainDomains.length) return;

    document.addEventListener("click", function (e) {
      var el = e.target;
      while (el && el.tagName !== "A") {
        el = el.parentElement;
      }
      if (!el || !el.href) return;

      try {
        var url = new URL(el.href);
        var domainMatch = false;
        for (var i = 0; i < config.crossDomainDomains.length; i++) {
          if (url.hostname === config.crossDomainDomains[i] || url.hostname.indexOf("." + config.crossDomainDomains[i]) !== -1) {
            domainMatch = true;
            break;
          }
        }
        if (domainMatch) {
          url.searchParams.set("_cdp_vid", state.visitorId);
          url.searchParams.set("_cdp_sid", state.sessionId);
          el.href = url.toString();
          log("Cross-domain link decorated:", el.href);
        }
      } catch (err) {
        // Invalid URL — ignore.
      }
    }, true);
  }

  function readCrossDomainParams() {
    var params = new URLSearchParams(window.location.search);
    var vid = params.get("_cdp_vid");
    var sid = params.get("_cdp_sid");
    if (vid) {
      state.visitorId = vid;
      setCookie(COOKIE_VISITOR, vid, VISITOR_MAX_AGE_DAYS);
      log("Cross-domain visitor restored:", vid);
    }
    if (sid) {
      state.sessionId = sid;
      state.lastActivity = now();
      setCookie(COOKIE_SESSION, sid, 0);
      log("Cross-domain session restored:", sid);
    }
  }

  // -----------------------------------------------------------------------
  // Public API
  // -----------------------------------------------------------------------
  var CDP = {
    /**
     * Initialise the tracker.
     * @param {Object} opts - Configuration overrides.
     */
    init: function (opts) {
      if (state.initialized) {
        log("Already initialized");
        return;
      }

      // Respect Do Not Track.
      if (config.respectDNT && navigator.doNotTrack === "1") {
        log("DNT enabled — tracker disabled");
        state.initialized = true;
        return;
      }

      // Merge user config.
      if (opts) {
        for (var key in opts) {
          if (opts.hasOwnProperty(key)) {
            config[key] = opts[key];
          }
        }
      }

      loadConsent();
      readCrossDomainParams();
      getOrCreateVisitor();
      getOrCreateSession();
      captureUTM();

      setupClickTracking();
      setupScrollTracking();
      setupTimeOnPage();
      setupCrossDomain();
      startFlushTimer();

      state.initialized = true;
      log("Initialized", config);

      // Auto page view.
      if (config.autoPageView) {
        CDP.page();
      }
    },

    /**
     * Track a custom event.
     * @param {string} eventName - Name of the event.
     * @param {Object} [properties] - Event properties.
     */
    track: function (eventName, properties) {
      if (!state.initialized) return;
      touchSession();
      enqueue(buildEvent("track", eventName, properties || {}));
    },

    /**
     * Track a page view.
     * @param {Object} [properties] - Additional page properties.
     */
    page: function (properties) {
      if (!state.initialized) return;
      touchSession();
      state.maxScrollDepth = 0;
      state.pageEnteredAt = now();
      var props = {
        url: location.href,
        path: location.pathname,
        title: document.title,
        referrer: document.referrer
      };
      if (properties) {
        for (var k in properties) {
          if (properties.hasOwnProperty(k)) props[k] = properties[k];
        }
      }
      enqueue(buildEvent("page_view", "page_view", props));
    },

    /**
     * Identify the current visitor as a known user.
     * @param {string} userId - The user's unique ID.
     * @param {Object} [traits] - User traits (email, name, etc.).
     */
    identify: function (userId, traits) {
      if (!state.initialized) return;
      state.userId = userId;
      state.traits = traits || {};
      touchSession();
      enqueue(buildEvent("identify", "identify", {
        user_id: userId,
        email: (traits && traits.email) || null,
        phone: (traits && traits.phone) || null,
        traits: traits || {}
      }));
    },

    /**
     * Grant consent for specified categories and flush queued events.
     * @param {Object} categories - e.g. { analytics: true, marketing: false }
     */
    consent: function (categories) {
      var cats = { necessary: true };
      if (categories) {
        for (var c in categories) {
          if (categories.hasOwnProperty(c)) cats[c] = !!categories[c];
        }
      }
      saveConsent(cats);
      log("Consent updated:", cats);

      if (canTrack()) {
        drainPreConsentQueue();
      }
    },

    /**
     * Revoke all optional consent and stop tracking.
     */
    revokeConsent: function () {
      saveConsent({ necessary: true, analytics: false, marketing: false, personalization: false });
      state.queue = [];
      state.sendQueue = [];
      log("Consent revoked");
    },

    /**
     * Get current consent status.
     * @returns {Object|null}
     */
    getConsent: function () {
      return state.consent;
    },

    /**
     * Get the current visitor ID.
     * @returns {string|null}
     */
    getVisitorId: function () {
      return state.visitorId;
    },

    /**
     * Get the current session ID.
     * @returns {string|null}
     */
    getSessionId: function () {
      return state.sessionId;
    },

    /**
     * Force-flush the send queue.
     */
    flush: function () {
      flush();
    },

    /**
     * Reset visitor and session (useful for logout).
     */
    reset: function () {
      deleteCookie(COOKIE_VISITOR);
      deleteCookie(COOKIE_SESSION);
      state.visitorId = null;
      state.sessionId = null;
      state.userId = null;
      state.traits = {};
      state.queue = [];
      state.sendQueue = [];
      getOrCreateVisitor();
      getOrCreateSession();
      log("Reset complete");
    }
  };

  // -----------------------------------------------------------------------
  // Expose globally
  // -----------------------------------------------------------------------
  window.CDP = CDP;

})(window, document);

/**
 * Module: manage.js
 * Purpose: Hardened fetch wrapper + UI helpers for the Tickles manage panel.
 * Location: /opt/tickles/shared/intelligence/manage_panel/static/manage.js
 */

(function () {
  'use strict';

  const CSRF_COOKIE = '__Host-csrf';
  const CSRF_HEADER = 'X-CSRF-Token';

  /**
   * Show a temporary error banner at the top of the page.
   * @param {string} msg
   * @param {number} [ttlMs=8000]
   */
  function showError(msg, ttlMs) {
    ttlMs = ttlMs || 8000;
    const banner = document.getElementById('error-banner');
    if (!banner) return;
    banner.textContent = msg;
    banner.style.display = 'block';
    setTimeout(function () {
      banner.style.display = 'none';
      banner.textContent = '';
    }, ttlMs);
  }

  /**
   * Read the CSRF token from the cookie.
   * @returns {string|null}
   */
  function getCsrfToken() {
    const match = document.cookie.split('; ').find(function (c) {
      return c.startsWith(CSRF_COOKIE + '=');
    });
    return match ? match.split('=')[1] : null;
  }

  /**
   * Hardened fetch wrapper for the manage panel.
   * Automatically injects CSRF header on mutating methods,
   * handles auth errors, rate limits, and network failures.
   *
   * @param {string} url
   * @param {object} [opts={}]
   * @returns {Promise<Response|object>}
   */
  async function tickFetch(url, opts) {
    opts = opts || {};
    opts.credentials = 'include';
    opts.headers = Object.assign({}, opts.headers || {});

    if (opts.method && opts.method !== 'GET' && opts.method !== 'HEAD') {
      const csrf = getCsrfToken();
      if (!csrf) {
        showError('Missing CSRF token — please reload the page.');
        throw new Error('no_csrf');
      }
      opts.headers[CSRF_HEADER] = csrf;
    }

    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (netErr) {
      showError('Network error — is Tailscale up?');
      throw netErr;
    }

    if (resp.status === 401) {
      window.location.href = '/login';
      throw new Error('auth');
    }
    if (resp.status === 403) {
      showError('Permission / CSRF rejected — reload page.');
      throw new Error('csrf');
    }
    if (resp.status === 429) {
      const retry = resp.headers.get('Retry-After') || '5';
      showError('Rate limited — retry in ' + retry + 's');
      throw new Error('rate');
    }
    if (!resp.ok) {
      showError('Server error ' + resp.status);
      throw new Error('http_' + resp.status);
    }

    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      return resp;
    }
    try {
      return await resp.json();
    } catch (e) {
      showError('Bad JSON from server');
      throw e;
    }
  }

  // Expose to global scope for inline onclick handlers
  window.tickFetch = tickFetch;
  window.showError = showError;

  /**
   * Disable a source by ID.
   * @param {number} id
   */
  window.disableSource = async function (id) {
    try {
      await tickFetch('/manage/api/sources/disable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('disableSource failed', e);
    }
  };

  /**
   * Enable a source by ID.
   * @param {number} id
   */
  window.enableSource = async function (id) {
    try {
      await tickFetch('/manage/api/sources/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('enableSource failed', e);
    }
  };

  /**
   * Disable a channel by ID.
   * @param {number} id
   */
  window.disableChannel = async function (id) {
    try {
      await tickFetch('/manage/api/channels/disable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('disableChannel failed', e);
    }
  };

  /**
   * Enable a channel by ID.
   * @param {number} id
   */
  window.enableChannel = async function (id) {
    try {
      await tickFetch('/manage/api/channels/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('enableChannel failed', e);
    }
  };

  /**
   * Disable a user by ID.
   * @param {number} id
   */
  window.disableUser = async function (id) {
    try {
      await tickFetch('/manage/api/users/disable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('disableUser failed', e);
    }
  };

  /**
   * Enable a user by ID.
   * @param {number} id
   */
  window.enableUser = async function (id) {
    try {
      await tickFetch('/manage/api/users/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: id }),
      });
      window.location.reload();
    } catch (e) {
      console.error('enableUser failed', e);
    }
  };
})();

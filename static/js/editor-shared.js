/* TestForTge — plumbing every editor needs (E4.4).
 *
 * The Test Cases editor (E4.3) and the Checklist editor (E4.4) do different
 * things, but they reach the server the same way: a CSRF token from the page's
 * meta tag, a JSON fetch, and one status line the user reads. Bugs (E4.5) and
 * Estimation (E4.6) will do it too.
 *
 * Copied, that plumbing would be four chances to forget the CSRF header —
 * which passes a test suite that disables CSRF and 400s in production. So it
 * lives here once and each editor keeps only what is specific to its entity.
 *
 * Deliberately not part of inline-edit.js: that component's job is one
 * editable field, it is loaded on pages that have no editor of their own, and
 * it has no opinion about page-level status messages.
 */
(function () {
    'use strict';

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    /**
     * A JSON request that never throws on an HTTP error.
     *
     * Returns {resp, payload}; payload is null when the body is not JSON —
     * which happens on the CSRF failure page and on a proxy timeout, and a
     * caller that assumed JSON would fail with a parse error instead of the
     * message it should be showing.
     */
    async function send(url, options) {
        const resp = await fetch(url, Object.assign({
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken(),
                'Accept': 'application/json',
            },
        }, options || {}));
        let payload = null;
        try {
            payload = await resp.json();
        } catch (err) {
            payload = null;
        }
        return { resp: resp, payload: payload };
    }

    /**
     * The page's one status line.
     *
     * ``selector`` names the element — each editor has its own bar. An 'ok'
     * message clears itself; an error stays until the next action, because an
     * error that vanishes is one the user may never have read.
     */
    function status(selector, message, kind) {
        const box = document.querySelector(selector);
        if (!box) return;
        const base = box.getAttribute('data-status-class') || '';
        box.textContent = message || '';
        box.className = base + (kind ? ' ' + base + '-' + kind : '');
        if (kind === 'ok') {
            window.setTimeout(() => {
                if (box.textContent === message) box.textContent = '';
            }, 2500);
        }
    }

    window.TestFortgeEditor = {
        csrfToken: csrfToken,
        send: send,
        status: status,
    };
})();

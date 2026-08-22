/* Delegated UI handlers — the replacement for inline on* attributes.
 *
 * The CSP this app sends is `script-src 'self' 'nonce-…' https://unpkg.com`
 * with no `'unsafe-inline'` (app.py:_apply_security_headers). A nonce
 * whitelists a <script> element; it does **not** whitelist an inline event
 * handler attribute. So every `onclick=` / `onchange=` / `onsubmit=` in a
 * template was dead — silently, because a blocked handler logs to the
 * console and the click otherwise behaves like a click on nothing.
 *
 * Thirty-one of them had accumulated. The visible symptom reported from
 * testing was the Traceability tab doing nothing. The dangerous ones were
 * quieter: four `onsubmit="return confirm(…)"` guards on destructive
 * actions — move artefacts, new session, delete project, discard pack —
 * which meant those actions fired on one click with no dialog at all,
 * because the handler that would have returned false never ran.
 *
 * Everything here is delegated from `document`, so it works for markup
 * added after load and needs no per-page wiring. Behaviour is driven by
 * data attributes; nothing dispatches on a function name in a string.
 *
 * The established pattern in this codebase is static/js/test-execution.js,
 * which did the same conversion for one page.
 */
(function () {
    'use strict';

    /* ── Tabs ──────────────────────────────────────────────────────────
     * <button class="tab" data-tab="traceability">
     * <div id="traceability" class="tab-content">
     *
     * Replaces a showTab() that was duplicated byte-for-byte across four
     * templates and read the implicit global `event` — which only has a
     * value because it was called from an inline attribute.
     */
    function activateTab(btn) {
        var id = btn.getAttribute('data-tab');
        if (!id) return;
        var panel = document.getElementById(id);
        if (!panel) return;

        // Scope to the button's own group so two tab strips on one page do
        // not deactivate each other.
        var group = btn.closest('[data-tabs]') || btn.parentElement || document;
        group.querySelectorAll('[data-tab]').forEach(function (el) {
            el.classList.remove('active');
        });
        btn.classList.add('active');

        // Panels are addressed by id and may live outside the button's
        // container, so they are collected from the document. Only panels
        // that some button in this group points at are touched.
        var owned = [];
        group.querySelectorAll('[data-tab]').forEach(function (el) {
            var p = document.getElementById(el.getAttribute('data-tab'));
            if (p) owned.push(p);
        });
        owned.forEach(function (p) { p.classList.remove('active'); });
        panel.classList.add('active');
    }

    /* ── Filters ───────────────────────────────────────────────────────
     * <button class="filter-btn" data-filter-scope=".tc-card"
     *         data-filter-key="category" data-filter="Positive">
     *
     * Filters compose multiplicatively per scope: a card shows iff it
     * passes every active key. That is what the Test Cases page already
     * did for category × suite; the checklist has one key and behaves
     * identically with a single entry.
     */
    var filterState = {};   // scope -> { key: value }

    function applyFilters(scope) {
        var keys = filterState[scope] || {};
        document.querySelectorAll(scope).forEach(function (card) {
            var visible = true;
            Object.keys(keys).forEach(function (key) {
                var want = keys[key];
                if (want && want !== 'all' && card.dataset[key] !== want) {
                    visible = false;
                }
            });
            card.style.display = visible ? '' : 'none';
        });
    }

    function activateFilter(btn) {
        var scope = btn.getAttribute('data-filter-scope');
        var key = btn.getAttribute('data-filter-key');
        var value = btn.getAttribute('data-filter');
        if (!scope || !key) return;

        // Deactivate only the buttons of this same key — the category row
        // and the suite row are independent selections.
        document.querySelectorAll(
            '[data-filter-key="' + key + '"][data-filter-scope="' + scope + '"]'
        ).forEach(function (el) { el.classList.remove('active'); });
        btn.classList.add('active');

        if (!filterState[scope]) filterState[scope] = {};
        filterState[scope][key] = value;
        applyFilters(scope);
    }

    /* ── Collapsible sections ──────────────────────────────────────────
     * <h2 class="collapsible-header" data-collapse>
     */
    function toggleCollapse(header) {
        header.classList.toggle('collapsed');
        var body = header.nextElementSibling;
        if (body) body.classList.toggle('collapsed');
    }

    /* ── Fill a field from nearby markup ───────────────────────────────
     * <button data-fill-target="requirements_text" data-fill-from="pre">
     *
     * Copies the text of the first `data-fill-from` match inside the
     * button's parent into the named field. Replaces an inline handler that
     * inlined that whole DOM walk as a string.
     */
    function fillTarget(btn) {
        var el = document.getElementById(btn.getAttribute('data-fill-target'));
        if (!el) return;
        var sel = btn.getAttribute('data-fill-from') || 'pre';
        var src = (btn.parentElement || document).querySelector(sel);
        if (!src) return;
        el.value = src.textContent;
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }

    /* ── Who owns the confirmation ─────────────────────────────────────
     * A click that ends in a form submission must NOT prompt here, because
     * the submit listener below will prompt for the same thing — and the
     * user would answer the same question twice. Measured, not assumed:
     * accepting the delete-project dialog produced two confirm() calls for
     * one click before this split existed.
     *
     * So the submit listener owns anything that submits a form (the form
     * itself, or a submit control inside one), and the click listener owns
     * everything else — links, type="button", controls with no form. Enter
     * in a text field never fires a click, which is the other half of why
     * the guard cannot live on click alone.
     */
    function willBeConfirmedOnSubmit(el) {
        if (el.tagName === 'FORM') return true;
        if (!el.form) return false;
        var type = (el.getAttribute('type') || '').toLowerCase();
        return type === 'submit'
            || (el.tagName === 'BUTTON' && type === '');
    }

    /* ── Click delegation ──────────────────────────────────────────────
     * One listener, and `data-confirm` is checked first so it can cancel
     * the rest.
     */
    document.addEventListener('click', function (ev) {
        var confirmEl = ev.target.closest('[data-confirm]');
        if (confirmEl && !willBeConfirmedOnSubmit(confirmEl)) {
            // Nothing downstream will re-check this one, so ask here.
            if (!window.confirm(confirmEl.getAttribute('data-confirm'))) {
                ev.preventDefault();
                return;
            }
        }

        var tab = ev.target.closest('[data-tab]');
        if (tab) { ev.preventDefault(); activateTab(tab); return; }

        var filter = ev.target.closest('[data-filter]');
        if (filter) { ev.preventDefault(); activateFilter(filter); return; }

        var collapse = ev.target.closest('[data-collapse]');
        if (collapse) { toggleCollapse(collapse); return; }

        var fill = ev.target.closest('[data-fill-target]');
        if (fill) { ev.preventDefault(); fillTarget(fill); return; }
    });

    /* ── Submit delegation ─────────────────────────────────────────────
     * The destructive-action guard. `data-confirm` on a <form>, or on the
     * submit button inside it, blocks the submission unless confirmed.
     * Enforced here rather than on click so keyboard submission is covered.
     */
    document.addEventListener('submit', function (ev) {
        var form = ev.target;
        if (!form || form.tagName !== 'FORM') return;

        var message = form.getAttribute('data-confirm');
        if (!message) {
            var submitter = ev.submitter
                || form.querySelector('[type="submit"][data-confirm]');
            if (submitter && submitter.hasAttribute('data-confirm')) {
                message = submitter.getAttribute('data-confirm');
            }
        }
        if (message && !window.confirm(message)) {
            ev.preventDefault();
            ev.stopPropagation();
        }
    });

    /* ── Change delegation ─────────────────────────────────────────────
     * data-autosubmit          — submit the owning form on change
     * data-status-class-prefix — restyle a <select> from its own value
     */
    document.addEventListener('change', function (ev) {
        var el = ev.target;
        if (!el) return;

        if (el.hasAttribute && el.hasAttribute('data-status-class-prefix')) {
            var prefix = el.getAttribute('data-status-class-prefix');
            el.className = prefix + ' ' + prefix + '-'
                + String(el.value || '').toLowerCase().replace(/\s+/g, '-');
        }

        if (el.hasAttribute && el.hasAttribute('data-autosubmit') && el.form) {
            el.form.submit();
        }
    });

    /* ── Initial state ─────────────────────────────────────────────────
     * Seed each filter scope from whichever button is already marked
     * active, so the first click on a *second* key does not silently drop
     * the first key's server-rendered selection.
     */
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('[data-filter].active').forEach(function (btn) {
            var scope = btn.getAttribute('data-filter-scope');
            var key = btn.getAttribute('data-filter-key');
            if (!scope || !key) return;
            if (!filterState[scope]) filterState[scope] = {};
            filterState[scope][key] = btn.getAttribute('data-filter');
        });
    });
})();

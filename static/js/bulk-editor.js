/* TestForTge — the bulk toolbar for test cases and checklists (E4.9).
 *
 * One file for both pages. The markup differs (cards vs table rows) so the
 * checkbox carries its own entity and id:
 *
 *     <input type="checkbox" class="bulk-check"
 *            data-bulk-entity="test_case" data-bulk-id="SC1_004">
 *
 * and the toolbar declares which fields it can set. Everything else — the
 * allowlist, the per-field guards, provenance, one audit row per operation —
 * belongs to engine.editable, so this file only gathers a selection and posts
 * it.
 *
 * Bug reports keep their own server-rendered toolbar from Sprint 4. It posts a
 * form and reloads, which is a different mechanism, and rewriting a working
 * one to share this file would be churn for its own sake.
 */
(function () {
    'use strict';

    const shared = window.TestFortgeEditor;
    const BAR = '[data-bulk-bar]';

    function checks() {
        return Array.from(document.querySelectorAll('.bulk-check'));
    }

    function selected() {
        return checks().filter((box) => box.checked);
    }

    function say(message, kind) {
        shared.status('[data-bulk-status]', message, kind);
    }

    /** Show the bar only when something is selected, and count it. */
    function sync() {
        const bar = document.querySelector(BAR);
        if (!bar) return;
        const chosen = selected();
        bar.hidden = chosen.length === 0;
        const counter = bar.querySelector('[data-bulk-count]');
        if (counter) counter.textContent = String(chosen.length);
        const all = document.querySelector('[data-bulk-select-all]');
        if (all) {
            all.checked = chosen.length > 0 && chosen.length === checks().length;
            all.indeterminate = chosen.length > 0
                && chosen.length < checks().length;
        }
    }

    async function post(path, body, verb) {
        const bar = document.querySelector(BAR);
        const entity = bar.getAttribute('data-bulk-bar');
        const ids = selected().map((box) => box.getAttribute('data-bulk-id'));
        if (!ids.length) return;

        say(verb + '…', 'info');
        const { resp, payload } = await shared.send(
            `/api/edit/${encodeURIComponent(entity)}/${path}`,
            { method: 'POST',
              body: JSON.stringify(Object.assign({ ids: ids }, body)) });

        if (resp.ok && payload) {
            // Reloaded rather than patched in place: a bulk change can move
            // rows between sections, renumber a checklist and change what the
            // filters show, and re-deriving all of that here would be a second
            // renderer of the page.
            window.location.reload();
            return;
        }
        say((payload && payload.message) || 'Could not apply that.', 'error');
    }

    document.addEventListener('change', (event) => {
        const target = event.target;
        if (!target.closest) return;
        if (target.matches('.bulk-check')) {
            sync();
            return;
        }
        if (target.matches('[data-bulk-select-all]')) {
            checks().forEach((box) => { box.checked = target.checked; });
            sync();
            return;
        }
        // The field select decides which value control is shown, because a
        // priority is a choice and a section is free text.
        if (target.matches('[data-bulk-field]')) {
            const bar = target.closest(BAR);
            bar.querySelectorAll('[data-bulk-value-for]').forEach((control) => {
                control.hidden = control.getAttribute('data-bulk-value-for')
                    !== target.value;
            });
        }
    });

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || !target.closest) return;

        if (target.closest('[data-bulk-apply]')) {
            const bar = target.closest(BAR);
            const field = bar.querySelector('[data-bulk-field]').value;
            const control = bar.querySelector(
                `[data-bulk-value-for="${field}"]`);
            const value = control ? control.value : '';
            if (!field) {
                say('Choose what to change.', 'error');
                return;
            }
            const changes = {};
            changes[field] = value;
            post('bulk', { changes: changes }, 'Applying');
            return;
        }

        if (target.closest('[data-bulk-delete]')) {
            const count = selected().length;
            if (!window.confirm(
                    `Delete ${count} item(s)? This cannot be undone.`)) return;
            post('bulk-delete', {}, 'Deleting');
            return;
        }

        if (target.closest('[data-bulk-clear]')) {
            checks().forEach((box) => { box.checked = false; });
            sync();
        }
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', sync);
    } else {
        sync();
    }
})();

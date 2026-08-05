/* TestForTge — the Estimation editor's controls (E4.6).
 *
 * Two things, and deliberately nothing else:
 *
 *   * collect the drivers and the feature list, PATCH /api/edit/estimation
 *   * POST /api/edit/estimation/revert
 *
 * No arithmetic. Not one line of it. Every hour, cost, PERT band and
 * Brooks's-Law penalty on that page comes from
 * engine.qa_estimator.compute_estimation, called on the server with the
 * inputs collected here — which is what makes the numbers on the page and the
 * numbers in the database the same numbers. A "live preview" computed in the
 * browser would be a second implementation of the estimator, and the first
 * time the two disagreed the page would be quietly lying about a figure
 * somebody is about to send a client.
 *
 * That is also why a successful save reloads the page instead of patching the
 * DOM: sixty derived numbers change, and re-rendering them here would be that
 * same second implementation wearing a different hat.
 */
(function () {
    'use strict';

    const shared = window.TestFortgeEditor;
    const STATUS = '[data-est-editor-status]';

    function say(message, kind) {
        shared.status(STATUS, message, kind);
    }

    /** The drivers, as the panel currently shows them. */
    function collectInputs() {
        const changes = {};
        document.querySelectorAll('[data-est-input]').forEach((field) => {
            const name = field.getAttribute('data-est-input');
            const raw = field.value;
            if (raw === '' || raw === null) return;
            changes[name] = (field.type === 'number') ? Number(raw) : raw;
        });
        return changes;
    }

    /**
     * The feature list, in table order.
     *
     * Sent whole rather than as a per-row edit: it is one array in one JSON
     * payload, so there is no row to address. The row version is what makes
     * that safe — a colleague's change between this page loading and this
     * save comes back 409 instead of overwriting their list with ours.
     */
    function collectFeatures() {
        const rows = document.querySelectorAll('[data-est-feature]');
        if (!rows.length) return null;
        const features = [];
        rows.forEach((row) => {
            const read = (name) => {
                const field = row.querySelector(`[data-est-field="${name}"]`);
                return field ? field.value : '';
            };
            const name = read('name').trim();
            if (!name) return;      // a blank name deletes the row
            features.push({
                name: name,
                test_cases: Number(read('test_cases') || 0),
                comment: read('comment'),
                is_section: row.getAttribute('data-est-section') === '1',
            });
        });
        return features;
    }

    function version(button) {
        const raw = button.getAttribute('data-est-version');
        return (raw === null || raw === '') ? undefined : Number(raw);
    }

    async function submit(button) {
        const changes = collectInputs();
        const features = collectFeatures();
        if (features) changes.features = features;
        if (!Object.keys(changes).length) {
            say('Nothing to change.', 'info');
            return;
        }

        button.disabled = true;
        say('Recalculating…', 'info');
        const { resp, payload } = await shared.send('/api/edit/estimation', {
            method: 'PATCH',
            body: JSON.stringify({ changes: changes,
                                   row_version: version(button) }),
        });
        button.disabled = false;

        if (resp.ok) {
            window.location.reload();
            return;
        }
        if (resp.status === 409) {
            say((payload && payload.message)
                || 'Someone else changed this estimation. Reload to see it.',
                'error');
            return;
        }
        // A refused input names itself, so the message can point at the field
        // rather than saying "invalid input" about a panel with eleven of them.
        say((payload && payload.message) || 'Could not recalculate.', 'error');
        if (payload && payload.field) {
            const field = document.querySelector(
                `[data-est-input="${payload.field}"]`);
            if (field) {
                field.setAttribute('aria-invalid', 'true');
                field.focus();
            }
        }
    }

    async function revert(button) {
        if (!window.confirm(
                "Put the model's original numbers back? Your changes to the "
                + 'drivers will be lost.')) return;
        button.disabled = true;
        const { resp, payload } = await shared.send(
            '/api/edit/estimation/revert',
            { method: 'POST',
              body: JSON.stringify({ row_version: version(button) }) });
        button.disabled = false;
        if (resp.ok) {
            window.location.reload();
            return;
        }
        say((payload && payload.message) || 'Could not revert.', 'error');
    }

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || !target.closest) return;
        const recalculate = target.closest('#est-recalculate');
        if (recalculate) {
            submit(recalculate);
            return;
        }
        const back = target.closest('#est-revert');
        if (back) revert(back);
    });

    // Clear the invalid mark as soon as the field is touched again, so it
    // marks "this is what was refused" and not "this field is bad forever".
    document.addEventListener('input', (event) => {
        const field = event.target.closest
            ? event.target.closest('[data-est-input]') : null;
        if (field) field.removeAttribute('aria-invalid');
    });
})();

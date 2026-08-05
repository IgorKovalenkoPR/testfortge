/* TestForTge — the Bug Reports editor's own controls (E4.5).
 *
 * The body fields are handled by inline-edit.js. This file adds the two
 * things specific to a bug report:
 *
 *   * the status control — PATCH /api/edit/bug/<id> with {status}
 *   * "New bug report"   — POST  /api/edit/bug
 *
 * Why status is a <select> and not an inline field
 * -----------------------------------------------
 * A status is not free text and not a fixed vocabulary either: which values
 * are legal depends on where the bug is now (engine/bug_workflow.py) and on
 * who is asking — closing is the sign-off that a fix was verified, so it is
 * admin-only. The template renders exactly the reachable options, and the
 * server checks again on the way in, because a missing <option> is UX and not
 * a permission.
 *
 * A refused move restores the select to the value it had. Leaving it showing
 * the rejected choice would tell the user the bug is Closed when the database
 * says Open — the one outcome worse than the refusal itself.
 */
(function () {
    'use strict';

    const shared = window.TestFortgeEditor;
    const STATUS_LINE = '[data-bug-editor-status]';

    function say(message, kind) {
        shared.status(STATUS_LINE, message, kind);
    }

    /** Keep the row's version in step across the fields and the select. */
    function setVersion(bugId, version) {
        if (version === null || version === undefined) return;
        const value = String(version);
        document.querySelectorAll(
            `[data-bug-status="${bugId}"]`
        ).forEach((el) => { el.setAttribute('data-bug-version', value); });
        document.querySelectorAll(
            `[data-ie-entity="bug"][data-ie-id="${bugId}"]`
        ).forEach((el) => { el.setAttribute('data-ie-version', value); });
    }

    function markEdited(bugId) {
        document.querySelectorAll(
            `[data-ie-entity="bug"][data-ie-id="${bugId}"]`
        ).forEach((el) => { el.setAttribute('data-ie-edited', '1'); });
        const row = document.getElementById(bugId);
        if (row) row.setAttribute('data-ie-edited', '1');
    }

    document.addEventListener('change', async (event) => {
        const select = event.target.closest
            ? event.target.closest('[data-bug-status]') : null;
        if (!select) return;

        const bugId = select.getAttribute('data-bug-status');
        const version = select.getAttribute('data-bug-version');
        const previous = select.getAttribute('data-bug-previous')
            || select.dataset.bugPrevious || '';
        const body = { changes: { status: select.value } };
        if (version !== null && version !== '') {
            body.row_version = Number(version);
        }

        select.disabled = true;
        const { resp, payload } = await shared.send(
            `/api/edit/bug/${encodeURIComponent(bugId)}`,
            { method: 'PATCH', body: JSON.stringify(body) });
        select.disabled = false;

        if (resp.ok && payload && payload.item) {
            setVersion(bugId, payload.item.row_version);
            markEdited(bugId);
            select.setAttribute('data-bug-previous', payload.item.status);
            say(`${bugId} is now ${payload.item.status}`, 'ok');
            return;
        }

        // Put the control back to what the database still says.
        if (previous) select.value = previous;
        if (resp.status === 409) {
            say((payload && payload.message)
                || 'Someone else changed this bug. Reload to see it.', 'error');
            return;
        }
        say((payload && payload.message) || 'Could not change the status.',
            'error');
    });

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || !target.closest) return;
        if (!target.closest('#bug-create')) return;

        say('Creating…', 'info');
        shared.send('/api/edit/bug', {
            method: 'POST',
            body: JSON.stringify({ values: {} }),
        }).then(({ resp, payload }) => {
            if (resp.ok) {
                // A reload rather than building the card here: it is 100 lines
                // of template with badges, an attachment gallery and a
                // checkbox that belongs to the bulk form, and a second
                // implementation would drift from the first.
                window.location.reload();
                return;
            }
            say((payload && payload.message) || 'Could not create a report.',
                'error');
        });
    });

    // Remember each select's rendered value, so a refused change has
    // something truthful to fall back to. Guarded on readyState rather than
    // trusting DOMContentLoaded: a deferred script runs before that event, but
    // this file should also work if a page ever loads it without defer.
    function remember() {
        document.querySelectorAll('[data-bug-status]').forEach((select) => {
            select.setAttribute('data-bug-previous', select.value);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', remember);
    } else {
        remember();
    }
})();

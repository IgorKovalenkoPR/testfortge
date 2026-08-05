/* TestForTge — the Test Cases editor's own controls (E4.3).
 *
 * The fields themselves are handled by inline-edit.js, which knows nothing
 * about test cases. This file adds the three things that are specific to
 * them:
 *
 *   * the steps list — add, edit, move, delete, each one operation posted to
 *     /api/edit/test_case/<id>/steps
 *   * "New test case"  → POST   /api/edit/test_case
 *   * "Delete this test case" → DELETE /api/edit/test_case/<id>
 *
 * Why the server owns the step operations
 * ---------------------------------------
 * The tempting shortcut is to reorder the list here and PATCH the joined
 * text. That sends a value computed from what this page last read, so two
 * people reordering different steps overwrite each other with a payload that
 * looks perfectly valid. Posting "move step 2 down" instead lets the server
 * apply it to the row as it is now, under the same version check as any
 * other edit — and a stale request comes back 409 rather than winning.
 *
 * No inline handlers and no innerHTML: the CSP has no unsafe-inline for
 * scripts, and every value here is somebody's test data.
 */
(function () {
    'use strict';

    const STEPS_LIST = '[data-tc-steps]';

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function status(message, kind) {
        const box = document.querySelector('[data-tc-editor-status]');
        if (!box) return;
        box.textContent = message || '';
        box.className = 'tc-editor-status'
            + (kind ? ' tc-editor-status-' + kind : '');
        if (kind === 'ok') {
            window.setTimeout(() => {
                if (box.textContent === message) box.textContent = '';
            }, 2500);
        }
    }

    /** Steps and fields of one case share a row version; keep them in step. */
    function setVersion(caseId, version) {
        if (version === null || version === undefined) return;
        const value = String(version);
        document.querySelectorAll(
            `[data-tc-steps="${caseId}"],[data-tc-delete="${caseId}"]`
        ).forEach((el) => { el.setAttribute('data-tc-version', value); });
        document.querySelectorAll(
            `[data-ie-entity="test_case"][data-ie-id="${caseId}"]`
        ).forEach((el) => { el.setAttribute('data-ie-version', value); });
    }

    async function send(url, options) {
        const resp = await fetch(url, Object.assign({
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken(),
                'Accept': 'application/json',
            },
        }, options));
        let payload = null;
        try {
            payload = await resp.json();
        } catch (err) {
            payload = null;
        }
        return { resp: resp, payload: payload };
    }

    // ── The steps list ────────────────────────────────────────────

    function renderSteps(list, steps) {
        const caseId = list.getAttribute('data-tc-steps');
        list.textContent = '';
        steps.forEach((text, index) => {
            const item = document.createElement('li');
            item.className = 'tc-step';
            item.setAttribute('data-step-index', String(index));

            const body = document.createElement('span');
            body.className = 'tc-step-text';
            body.textContent = text;
            item.appendChild(body);

            const actions = document.createElement('span');
            actions.className = 'tc-step-actions';
            [
                ['edit', '✎', 'Edit step', 0],
                ['move', '↑', 'Move up', -1],
                ['move', '↓', 'Move down', 1],
                ['remove', '✕', 'Delete step', 0],
            ].forEach(([op, glyph, label, delta]) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'tc-step-btn'
                    + (op === 'remove' ? ' tc-step-del' : '');
                button.setAttribute('data-step-op', op);
                if (op === 'move') {
                    button.setAttribute('data-step-delta', String(delta));
                    button.disabled = (delta < 0 && index === 0)
                        || (delta > 0 && index === steps.length - 1);
                }
                button.title = label;
                button.setAttribute('aria-label',
                    label + ' ' + (index + 1));
                button.textContent = glyph;
                actions.appendChild(button);
            });
            item.appendChild(actions);
            list.appendChild(item);
        });
        // Re-labelling is not optional: the numbers in the buttons' names are
        // positions, and a move that left them stale would tell a screen
        // reader the wrong step is about to move.
        if (caseId) list.setAttribute('data-tc-steps', caseId);
    }

    async function stepOp(list, op, options) {
        const caseId = list.getAttribute('data-tc-steps');
        const version = list.getAttribute('data-tc-version');
        const body = Object.assign({ op: op }, options || {});
        if (version !== null && version !== '') {
            body.row_version = Number(version);
        }
        const { resp, payload } = await send(
            `/api/edit/test_case/${encodeURIComponent(caseId)}/steps`,
            { method: 'POST', body: JSON.stringify(body) });

        if (resp.ok && payload) {
            renderSteps(list, payload.steps || []);
            setVersion(caseId, payload.item && payload.item.row_version);
            markEdited(caseId, payload.item);
            status('Steps saved', 'ok');
            showWarnings(list, (payload.warnings || {}).test_steps);
            return true;
        }
        if (resp.status === 409) {
            // Deliberately not auto-reloading: an unsaved edit elsewhere on
            // the page would go with it.
            status((payload && payload.message)
                || 'Someone else changed these steps. Reload to see them.',
                'error');
            return false;
        }
        status((payload && payload.message) || 'Could not save the step.',
            'error');
        return false;
    }

    function showWarnings(list, findings) {
        const container = list.parentNode;
        const existing = container.querySelector('.tc-step-warnings');
        if (existing) existing.remove();
        if (!findings || !findings.length) return;
        const box = document.createElement('ul');
        box.className = 'tc-step-warnings';
        findings.forEach((text) => {
            const item = document.createElement('li');
            item.textContent = text;
            box.appendChild(item);
        });
        container.appendChild(box);
    }

    function markEdited(caseId, item) {
        if (!item || item.ai_generated !== false) return;
        document.querySelectorAll(
            `[data-ie-entity="test_case"][data-ie-id="${caseId}"]`
        ).forEach((el) => { el.setAttribute('data-ie-edited', '1'); });
        // And the row itself, which is what the CSS keys on to reveal the
        // "edited" pill. Marking only the fields left the pill hidden until
        // the next page load — so a step edit looked like it had not counted
        // as a human edit, which is exactly what the pill exists to say.
        const row = document.getElementById(caseId);
        if (row) row.setAttribute('data-ie-edited', '1');
    }

    /** A small textarea in place of the step's text, for edit and add. */
    function promptForStep(host, initial, onDone) {
        if (host.querySelector('.tc-step-input')) return;
        const previous = host.textContent;
        const control = document.createElement('textarea');
        control.className = 'tc-step-input';
        control.rows = Math.min(6, Math.max(2, (initial || '').length / 60 + 1));
        control.value = initial || '';
        control.setAttribute('aria-label', 'Step text');
        host.textContent = '';
        host.appendChild(control);
        control.focus();
        // Caret at the end rather than a selection, for the reason
        // inline-edit.js documents: the first keystroke must not wipe the
        // step somebody clicked to amend.
        control.setSelectionRange(control.value.length, control.value.length);

        let settled = false;
        function finish(save) {
            if (settled) return;
            settled = true;
            const value = control.value;
            control.remove();
            host.textContent = previous;
            if (save) onDone(value);
        }
        control.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                finish(false);
            } else if (event.key === 'Enter'
                    && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                finish(true);
            }
        });
        control.addEventListener('blur', () => finish(true));
    }

    // ── Wiring ────────────────────────────────────────────────────

    document.addEventListener('click', (event) => {
        const target = event.target.closest ? event.target : null;
        if (!target) return;

        // A step's own controls.
        const stepButton = target.closest('[data-step-op]');
        if (stepButton && !stepButton.disabled) {
            const item = stepButton.closest('[data-step-index]');
            const list = stepButton.closest(STEPS_LIST);
            if (!item || !list) return;
            const index = Number(item.getAttribute('data-step-index'));
            const op = stepButton.getAttribute('data-step-op');
            if (op === 'move') {
                stepOp(list, 'move', {
                    index: index,
                    delta: Number(stepButton.getAttribute('data-step-delta')),
                });
            } else if (op === 'remove') {
                stepOp(list, 'remove', { index: index });
            } else if (op === 'edit') {
                const body = item.querySelector('.tc-step-text');
                if (body) {
                    promptForStep(body, body.textContent, (value) => {
                        if (value.trim() && value !== body.textContent) {
                            stepOp(list, 'edit',
                                { index: index, text: value });
                        }
                    });
                }
            }
            return;
        }

        // Add a step.
        const addButton = target.closest('[data-tc-steps-add]');
        if (addButton) {
            const caseId = addButton.getAttribute('data-tc-steps-add');
            const list = document.querySelector(
                `[data-tc-steps="${caseId}"]`);
            if (!list) return;
            const holder = document.createElement('li');
            holder.className = 'tc-step tc-step-new';
            const body = document.createElement('span');
            body.className = 'tc-step-text';
            holder.appendChild(body);
            list.appendChild(holder);
            promptForStep(body, '', (value) => {
                holder.remove();
                if (value.trim()) stepOp(list, 'add', { text: value });
            });
            return;
        }

        // Delete a whole case.
        const deleteButton = target.closest('[data-tc-delete]');
        if (deleteButton) {
            const caseId = deleteButton.getAttribute('data-tc-delete');
            const version = deleteButton.getAttribute('data-tc-version');
            if (!window.confirm(
                    `Delete ${caseId}? This cannot be undone.`)) return;
            const query = (version !== null && version !== '')
                ? `?row_version=${encodeURIComponent(version)}` : '';
            send(`/api/edit/test_case/${encodeURIComponent(caseId)}${query}`,
                { method: 'DELETE' }).then(({ resp, payload }) => {
                    if (resp.ok) {
                        const card = document.getElementById(caseId);
                        if (card) card.remove();
                        status(`${caseId} deleted`, 'ok');
                        return;
                    }
                    status((payload && payload.message)
                        || 'Could not delete this case.', 'error');
                });
            return;
        }

        // Create a case.
        if (target.closest('#tc-create')) {
            status('Creating…', 'info');
            send('/api/edit/test_case', {
                method: 'POST',
                body: JSON.stringify({ values: {} }),
            }).then(({ resp, payload }) => {
                if (resp.ok && payload && payload.item) {
                    // A reload rather than building the card here: the card
                    // is 200 lines of template with badges, a status row and
                    // two collapsible editors, and a second implementation of
                    // it in JavaScript would drift from the first.
                    window.location.reload();
                    return;
                }
                status((payload && payload.message)
                    || 'Could not create a test case.', 'error');
            });
        }
    });
})();

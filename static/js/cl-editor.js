/* TestForTge — the Checklist editor's own controls (E4.4).
 *
 * The fields themselves are handled by inline-edit.js. This file adds what is
 * specific to a checklist:
 *
 *   * move an item up or down  → POST /api/edit/checklist_item/<id>/move
 *   * move it to another section → POST /api/edit/checklist_item/<id>/section
 *   * rename a section         → POST /api/edit/checklist/rename-section
 *   * add / delete an item     → POST|DELETE /api/edit/checklist_item
 *
 * Numbers are reloaded from the response rather than recomputed here. The
 * house style decides which of them may change — a move renumbers its section,
 * an insert appends without touching siblings, a delete leaves its number
 * vacated — and that rule lives in engine/checklist_order.py. A second
 * implementation of it in JavaScript would eventually disagree, and the
 * disagreement would be invisible until somebody cited "2.4" in a bug report.
 *
 * No inline handlers and no innerHTML: the CSP has no unsafe-inline for
 * scripts, and every value here is somebody's test documentation.
 */
(function () {
    'use strict';

    const STATUS = '[data-cl-editor-status]';
    const shared = window.TestFortgeEditor;

    function say(message, kind) {
        shared.status(STATUS, message, kind);
    }

    /** Apply the numbers and sections the server just returned. */
    function applyOrder(items) {
        (items || []).forEach((item) => {
            document.querySelectorAll(
                `[data-cl-id="${item.id}"] [data-cl-num]`
            ).forEach((cell) => { cell.textContent = item.item_num || ''; });
        });
    }

    function rowVersion(row) {
        const field = row.querySelector('[data-ie-version]');
        return field ? field.getAttribute('data-ie-version') : null;
    }

    async function op(url, body, { reload = false } = {}) {
        const { resp, payload } = await shared.send(url, {
            method: 'POST',
            body: JSON.stringify(body || {}),
        });
        if (resp.ok && payload) {
            if (reload) {
                // Order or grouping changed, so which row sits under which
                // heading changed too. Rebuilding the table here would be a
                // second renderer of the template's grouping logic.
                window.location.reload();
                return true;
            }
            applyOrder(payload.items);
            say('Saved', 'ok');
            return true;
        }
        if (resp.status === 409) {
            say((payload && payload.message)
                || 'Someone else changed this checklist. Reload to see it.',
                'error');
            return false;
        }
        say((payload && payload.message) || 'Could not save.', 'error');
        return false;
    }

    document.addEventListener('click', (event) => {
        const target = event.target;
        if (!target || !target.closest) return;

        // ── Move up / down ────────────────────────────────────────
        const mover = target.closest('[data-cl-move]');
        if (mover && !mover.disabled) {
            const row = mover.closest('[data-cl-id]');
            if (!row) return;
            const id = row.getAttribute('data-cl-id');
            op(`/api/edit/checklist_item/${encodeURIComponent(id)}/move`,
                { delta: Number(mover.getAttribute('data-cl-move')),
                  row_version: Number(rowVersion(row)) || undefined },
                { reload: true });
            return;
        }

        // ── Delete one item ──────────────────────────────────────
        const remover = target.closest('[data-cl-delete]');
        if (remover) {
            const id = remover.getAttribute('data-cl-delete');
            if (!window.confirm(`Delete ${id}? This cannot be undone.`)) return;
            const row = remover.closest('[data-cl-id]');
            const version = row ? rowVersion(row) : null;
            const query = version ? `?row_version=${encodeURIComponent(version)}`
                : '';
            shared.send(
                `/api/edit/checklist_item/${encodeURIComponent(id)}${query}`,
                { method: 'DELETE' }
            ).then(({ resp, payload }) => {
                if (resp.ok) {
                    // The row goes, its number does not come back: a vacated
                    // number stays vacated, because the numbers are cited.
                    if (row) row.remove();
                    say(`${id} deleted`, 'ok');
                    return;
                }
                say((payload && payload.message) || 'Could not delete.',
                    'error');
            });
            return;
        }

        // ── Add an item ──────────────────────────────────────────
        const adder = target.closest('[data-cl-add]');
        if (adder) {
            const section = adder.getAttribute('data-cl-add');
            say('Adding…', 'info');
            shared.send('/api/edit/checklist_item', {
                method: 'POST',
                body: JSON.stringify({ values: { section: section } }),
            }).then(({ resp, payload }) => {
                if (resp.ok) {
                    window.location.reload();
                    return;
                }
                say((payload && payload.message) || 'Could not add an item.',
                    'error');
            });
            return;
        }

        // ── Rename a section ─────────────────────────────────────
        const renamer = target.closest('[data-cl-rename]');
        if (renamer) {
            const current = renamer.getAttribute('data-cl-rename');
            const next = window.prompt('Rename this section to:', current);
            if (next === null || next.trim() === '' || next === current) return;
            op('/api/edit/checklist/rename-section',
                { from: current, to: next }, { reload: true });
            return;
        }

        // ── Move an item to another section ──────────────────────
        const relocator = target.closest('[data-cl-relocate]');
        if (relocator) {
            const row = relocator.closest('[data-cl-id]');
            if (!row) return;
            const id = row.getAttribute('data-cl-id');
            const options = (relocator.getAttribute('data-cl-sections') || '')
                .split('|').filter(Boolean);
            const next = window.prompt(
                'Move to which section?\n\n' + options.join('\n'),
                relocator.getAttribute('data-cl-relocate') || '');
            if (next === null || next.trim() === '') return;
            op(`/api/edit/checklist_item/${encodeURIComponent(id)}/section`,
                { section: next, row_version: Number(rowVersion(row)) || undefined },
                { reload: true });
        }
    });
})();

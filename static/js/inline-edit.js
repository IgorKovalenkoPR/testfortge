/**
 * TestFortge — inline editing of generated artefacts (E4.2)
 *
 * The front end of the substrate E4.1 built. One component for all four
 * editors, driven entirely by data attributes, so a page opts a field in
 * with markup and no JavaScript of its own:
 *
 *   <span class="ie" data-ie-entity="test_case" data-ie-id="TC-001"
 *         data-ie-field="summary" data-ie-version="1"
 *         data-ie-kind="text">Sign in with valid credentials</span>
 *
 * `data-ie-kind` is text | textarea | choice. A choice also carries
 * `data-ie-choices="Critical|Major|Minor"`.
 *
 * Why an external file with delegated listeners
 * ---------------------------------------------
 * The app sends a strict CSP with a per-request nonce and no
 * 'unsafe-inline' for scripts. An `onclick=` attribute would simply not
 * run — silently, which is the worst way for an editor to be broken. One
 * delegated listener on document also means a row added to the DOM later
 * is editable without re-binding anything.
 *
 * The version is per row, not per field
 * -------------------------------------
 * `row_version` belongs to the row, so every field of one row shares it.
 * After any field saves, all the row's other fields are updated to the new
 * version — otherwise editing two fields of the same test case would make
 * the second edit look like a conflict.
 *
 * A conflict never discards what the user typed
 * ---------------------------------------------
 * On 409 the editor stays open with the text still in it. Somebody who has
 * just written three sentences of test steps must be able to copy them
 * before reloading; throwing them away to show a cleaner error would be a
 * worse bug than the conflict.
 */
(function () {
    'use strict';

    const SELECTOR = '[data-ie-entity][data-ie-id][data-ie-field]';
    const EDITING_CLASS = 'ie-editing';

    // ── CSRF ──────────────────────────────────────────────────────
    // Read once, from the meta tag base.html renders. Flask-WTF protects
    // every non-GET method, PATCH included.
    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // ── Announcements ─────────────────────────────────────────────
    // One polite live region for the whole page. Created lazily so a page
    // with no editable fields carries nothing extra.
    let liveRegion = null;
    function announce(message) {
        if (!liveRegion) {
            liveRegion = document.createElement('div');
            liveRegion.className = 'ie-live';
            liveRegion.setAttribute('role', 'status');
            liveRegion.setAttribute('aria-live', 'polite');
            document.body.appendChild(liveRegion);
        }
        // Cleared first: repeating identical text is not re-announced by
        // some screen readers otherwise.
        liveRegion.textContent = '';
        window.setTimeout(() => { liveRegion.textContent = message; }, 50);
    }

    // ── Helpers ───────────────────────────────────────────────────

    function fieldLabel(host) {
        return host.getAttribute('data-ie-label')
            || (host.getAttribute('data-ie-field') || '').replace(/_/g, ' ');
    }

    /**
     * The accessible name of a closed field: its value first, then what
     * clicking does.
     *
     * An aria-label *replaces* the element's text for assistive tech, so a
     * plain 'Edit summary' made every row in a table sound identical and
     * hid the one thing the cell is there to convey — found by reading the
     * accessibility tree of the harness page, where six fields all came out
     * as "Edit summary/test steps/priority. Press Enter to edit."
     *
     * A long value is clipped: the whole point is to hear which row this
     * is, and a screen reader reading 400 characters of steps before
     * saying "editable" defeats that. The full text is still in the
     * element, which is what a reader announces when the user navigates
     * into it rather than tabbing past.
     */
    const NAME_CLIP = 80;

    function accessibleName(host, value) {
        const label = fieldLabel(host);
        let spoken = (value === null || value === undefined)
            ? '' : String(value).replace(/\s+/g, ' ').trim();
        if (spoken.length > NAME_CLIP) {
            spoken = spoken.slice(0, NAME_CLIP) + '…';
        }
        if (!spoken) return 'Empty ' + label + '. Press Enter to edit.';
        return spoken + '. ' + label + '. Press Enter to edit.';
    }

    function refreshName(host) {
        if (host.hasAttribute('data-ie-readonly')) return;
        host.setAttribute(
            'aria-label',
            accessibleName(host, host.getAttribute('data-ie-original') || ''));
    }

    /** Every field of the same row, so a version bump reaches all of them. */
    function siblingFields(host) {
        const entity = host.getAttribute('data-ie-entity');
        const id = host.getAttribute('data-ie-id');
        return Array.from(document.querySelectorAll(
            `[data-ie-entity="${entity}"][data-ie-id="${id}"]`));
    }

    function setRowVersion(host, version) {
        if (version === null || version === undefined) return;
        siblingFields(host).forEach((el) => {
            el.setAttribute('data-ie-version', String(version));
        });
    }

    /** Mark the row as human-edited, for whatever the page shows. */
    function markEdited(host) {
        siblingFields(host).forEach((el) => {
            el.setAttribute('data-ie-edited', '1');
        });
        const row = host.closest('[data-ie-row]');
        if (row) row.setAttribute('data-ie-edited', '1');
    }

    function clearMessage(host) {
        const existing = host.parentNode
            && host.parentNode.querySelector('.ie-message');
        if (existing) existing.remove();
        host.removeAttribute('aria-invalid');
    }

    function showMessage(host, text, kind) {
        clearMessage(host);
        const note = document.createElement('span');
        note.className = 'ie-message ie-message-' + (kind || 'info');
        note.textContent = text;
        if (kind === 'error') {
            note.setAttribute('role', 'alert');
            host.setAttribute('aria-invalid', 'true');
        }
        host.parentNode.insertBefore(note, host.nextSibling);
        return note;
    }

    function flashSaved(host) {
        const note = showMessage(host, 'Saved', 'ok');
        window.setTimeout(() => {
            if (note.isConnected) note.remove();
        }, 2000);
        announce(fieldLabel(host) + ' saved');
    }

    // ── The editor ────────────────────────────────────────────────

    function buildControl(host, value) {
        const kind = host.getAttribute('data-ie-kind') || 'text';
        let control;
        if (kind === 'choice') {
            control = document.createElement('select');
            const raw = host.getAttribute('data-ie-choices') || '';
            const options = raw.split('|').filter((c) => c !== '');
            // An unrecognised current value is offered too, rather than
            // silently rewritten by the act of opening the editor. The
            // server's allowlist is what rejects a bad value, and it does
            // so on save with a message.
            if (value && options.indexOf(value) === -1) options.unshift(value);
            options.forEach((choice) => {
                const option = document.createElement('option');
                option.value = choice;
                option.textContent = choice;
                if (choice === value) option.selected = true;
                control.appendChild(option);
            });
        } else if (kind === 'textarea') {
            control = document.createElement('textarea');
            control.value = value;
            control.rows = Math.min(
                12, Math.max(3, value.split('\n').length + 1));
        } else {
            control = document.createElement('input');
            control.type = 'text';
            control.value = value;
        }
        control.className = 'ie-control';
        control.setAttribute(
            'aria-label', 'Edit ' + fieldLabel(host));
        return control;
    }

    function close(host, text) {
        const control = host.querySelector('.ie-control');
        if (control) {
            // Removing a focused element fires blur, and the blur handler's
            // job is to save. Marking the control first tells it this blur
            // is a teardown, not the user clicking away.
            //
            // Without this, Escape *saved* the text it was asked to throw
            // away: close() removed the control, the blur that followed
            // still saw the typed value, and it went to the server. Found
            // by pressing Escape in the browser — the field came back with
            // the discarded text in it and "test steps saved" announced.
            // The mark goes on before .remove() because some browsers fire
            // this blur synchronously and some do not.
            control.dataset.ieClosing = '1';
            control.remove();
        }
        host.classList.remove(EDITING_CLASS);
        host.textContent = text;
        host.setAttribute('tabindex', '0');
        // After a save the value is new, so the name that describes it is
        // stale; refreshing here covers every close path at once.
        refreshName(host);
        host.focus();
    }

    async function save(host, control) {
        const original = host.getAttribute('data-ie-original') || '';
        const value = control.value;
        if (value === original) {
            // Nothing changed: close without a request. The server treats a
            // no-op as a non-write anyway, but not asking is faster and
            // keeps the audit log honest without relying on it.
            close(host, original);
            return;
        }

        const entity = host.getAttribute('data-ie-entity');
        const id = host.getAttribute('data-ie-id');
        const field = host.getAttribute('data-ie-field');
        const version = host.getAttribute('data-ie-version');

        control.disabled = true;
        host.setAttribute('aria-busy', 'true');
        clearMessage(host);

        const body = { changes: {} };
        body.changes[field] = value;
        if (version !== null && version !== '') {
            body.row_version = Number(version);
        }

        let resp, payload;
        try {
            resp = await fetch(
                `/api/edit/${encodeURIComponent(entity)}/${encodeURIComponent(id)}`,
                {
                    method: 'PATCH',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken(),
                        'Accept': 'application/json',
                    },
                    body: JSON.stringify(body),
                });
            payload = await resp.json();
        } catch (err) {
            // Network or non-JSON. Keep the editor open with the text in
            // it — the user's words are worth more than a tidy error state.
            control.disabled = false;
            control.dataset.ieFailed = '1';
            host.removeAttribute('aria-busy');
            showMessage(host, 'Could not reach the server. Your text is '
                + 'still here — try again.', 'error');
            announce('Save failed: the server could not be reached');
            control.focus();
            return;
        }

        host.removeAttribute('aria-busy');

        if (resp.ok) {
            const item = (payload && payload.item) || {};
            const saved = (item[field] !== undefined && item[field] !== null)
                ? String(item[field]) : value;
            host.setAttribute('data-ie-original', saved);
            setRowVersion(host, item.row_version);
            if (item.ai_generated === false) markEdited(host);
            close(host, saved);
            flashSaved(host);
            return;
        }

        control.disabled = false;
        // The save was refused and the user has been told. A blur is not a
        // retry: the next attempt has to be something they did on purpose —
        // typing and pressing Enter, Escape, or Reload. Cleared as soon as
        // they type (see the input listener in open()).
        //
        // This used to be left to the `disabled` check in the blur handler,
        // which does not hold here because the line above re-enables the
        // control. Verified in the browser: blurring a conflicted field
        // sent a second PATCH. Mostly that is a wasted round trip, but if
        // the colleague's change had been undone in between it would be a
        // write the user never confirmed — the exact thing the version
        // check exists to prevent.
        control.dataset.ieFailed = '1';

        if (resp.status === 409) {
            // The editor stays open, deliberately. See the note at the top.
            const note = showMessage(
                host,
                (payload && payload.message)
                    || 'Someone else changed this item.',
                'error');
            const reload = document.createElement('button');
            reload.type = 'button';
            reload.className = 'ie-reload';
            reload.textContent = 'Reload';
            reload.addEventListener('click', () => window.location.reload());
            note.appendChild(document.createTextNode(' '));
            note.appendChild(reload);
            announce('Conflict: someone else changed this item');
            control.focus();
            return;
        }

        const message = (payload && payload.message) || 'Could not save.';
        showMessage(host, message, 'error');
        announce('Save failed: ' + message);
        control.focus();
    }

    function open(host) {
        if (host.classList.contains(EDITING_CLASS)) return;
        const value = (host.getAttribute('data-ie-original') !== null)
            ? host.getAttribute('data-ie-original')
            : host.textContent.trim();
        host.setAttribute('data-ie-original', value);
        clearMessage(host);

        const control = buildControl(host, value);
        host.textContent = '';
        host.classList.add(EDITING_CLASS);
        // While the control has focus the host must not also be a tab stop.
        host.removeAttribute('tabindex');
        host.appendChild(control);
        control.focus();
        // A short field is usually replaced wholesale, so selecting it is
        // the helpful default. A textarea is not: test steps are a list
        // people come back to append to, and selecting all of them means
        // the first keystroke deletes every step. Caret at the end there.
        //
        // Found in the browser: clicking the steps cell and typing one
        // line left the case with only that line.
        if (control.tagName === 'TEXTAREA') {
            control.setSelectionRange(value.length, value.length);
            control.scrollTop = control.scrollHeight;
        } else if (control.select) {
            control.select();
        }

        control.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                clearMessage(host);
                close(host, value);
                announce('Edit cancelled');
                return;
            }
            const isTextarea = control.tagName === 'TEXTAREA';
            // Enter saves a single-line field. In a textarea it inserts a
            // newline — test steps are multi-line and Enter belongs to the
            // text — so Ctrl/Cmd+Enter is the save there.
            if (event.key === 'Enter' && (!isTextarea
                    || event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                save(host, control);
            }
        });

        // Typing after a refusal is the user's next attempt, so the field
        // stops being "already refused" and a blur may save again.
        control.addEventListener('input', () => {
            delete control.dataset.ieFailed;
        });

        control.addEventListener('blur', () => {
            // The editor is already closing (Escape, or a save that
            // finished). Saving again here would either resurrect
            // discarded text or repeat a write.
            if (control.dataset.ieClosing) return;
            // A save was refused and nothing has been retyped since —
            // including the blur from clicking Reload in a conflict message.
            if (control.dataset.ieFailed) return;
            // A save is in flight.
            if (control.disabled) return;
            if (control.value === value) {
                close(host, value);
                return;
            }
            save(host, control);
        });
    }

    // ── Wiring ────────────────────────────────────────────────────
    // Delegated, so rows rendered after load are editable too, and so the
    // page needs no script of its own.

    document.addEventListener('click', (event) => {
        const host = event.target.closest
            ? event.target.closest(SELECTOR) : null;
        if (!host || host.classList.contains(EDITING_CLASS)) return;
        if (host.hasAttribute('data-ie-readonly')) return;
        open(host);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const host = document.activeElement;
        if (!host || !host.matches || !host.matches(SELECTOR)) return;
        if (host.classList.contains(EDITING_CLASS)) return;
        if (host.hasAttribute('data-ie-readonly')) return;
        event.preventDefault();
        open(host);
    });

    // Make every field reachable by keyboard and describe it, without the
    // page having to repeat the attributes on each one.
    function prepare(root) {
        (root || document).querySelectorAll(SELECTOR).forEach((host) => {
            if (host.hasAttribute('data-ie-readonly')) return;
            if (!host.hasAttribute('tabindex')) {
                host.setAttribute('tabindex', '0');
            }
            if (!host.hasAttribute('role')) {
                host.setAttribute('role', 'button');
            }
            if (!host.hasAttribute('aria-label')) {
                refreshName(host);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => prepare());
    } else {
        prepare();
    }

    // Exposed for pages that inject rows dynamically, and for tests.
    window.TestFortgeInlineEdit = { prepare: prepare };
})();

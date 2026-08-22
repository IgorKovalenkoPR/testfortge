/**
 * TestFortge — Main JavaScript
 * Handles: tooltips, tabs, filters, collapsibles, drag-drop
 */

document.addEventListener('DOMContentLoaded', () => {
    // Auto-hide alerts after 5 seconds
    document.querySelectorAll('.alert').forEach(alert => {
        setTimeout(() => {
            alert.style.transition = 'opacity .3s';
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 300);
        }, 5000);
    });

    // ── Loading overlay for slow generation endpoints ─────────────
    // /test-cases and /checklist can take 30-90 s on a free-tier
    // instance because they crawl the URL + run a real headless
    // browser. Without feedback the user thinks the page hung and
    // reloads, which discards the upload + re-runs the crawl. This
    // overlay shows a spinner once any matching form is submitted.
    //
    // 2026-05-04: /estimation/* routes were dropped from this list.
    // The estimation form has its own per-stage modal
    // (templates/estimation.html:est-gen-overlay) showing tab-aware
    // labels ("Asking Claude vision…", "Crawling pages…", etc.).
    // Letting THIS generic overlay fire on top would stack two
    // modals — operator-reported as the wrong UX.
    const SLOW_PATHS = ['/test-cases', '/checklist'];
    const isSlowEndpoint = (form) => {
        const action = form.getAttribute('action') || window.location.pathname;
        const url = new URL(action, window.location.origin);
        if (form.method && form.method.toUpperCase() !== 'POST') return false;
        // Skip the upload form on /test-cases or /checklist — it's fast.
        if (form.action && /\/upload$/.test(url.pathname)) return false;
        if (form.classList && form.classList.contains('upload-existing')) return false;
        // Skip forms that have their own async-progress modal — each of
        // these renders a dedicated stage-aware overlay backed by the
        // JobQueue. Stacking two would be double-overlay UX (operator-
        // reported on /estimation 2026-05-04).
        if (form.id === 'tc-gen-form'
            || form.id === 'cl-gen-form'
            || form.id === 'estimation-form') return false;
        return SLOW_PATHS.some(p => url.pathname === p);
    };

    const showOverlay = () => {
        if (document.getElementById('tf-loading-overlay')) return;
        const ov = document.createElement('div');
        ov.id = 'tf-loading-overlay';
        ov.innerHTML = `
            <div class="tf-loading-card">
              <div class="tf-loading-spinner" aria-hidden="true"></div>
              <h3 class="tf-loading-title">Working on it…</h3>
              <p class="tf-loading-msg">
                We're crawling the URL and running real browser checks.
                This usually takes <strong>30 – 90 seconds</strong> for a
                fresh URL on the free instance. Please don't reload —
                progress would be lost.
              </p>
              <p class="tf-loading-tip">
                Tip: subsequent runs against the same URL are
                significantly faster (results are cached for 5 min).
              </p>
            </div>`;
        document.body.appendChild(ov);
    };

    document.querySelectorAll('form').forEach(form => {
        if (!isSlowEndpoint(form)) return;
        form.addEventListener('submit', () => {
            // Disable the submit button so the user can't double-fire.
            form.querySelectorAll('button[type=submit]').forEach(btn => {
                btn.disabled = true;
                if (!btn.dataset.origText) btn.dataset.origText = btn.innerHTML;
                btn.innerHTML = btn.dataset.origText + ' …';
            });
            showOverlay();
        });
    });
});

/* Tab switching — removed.
 * This was the third identical copy (the others were inline in
 * recommendations / test_cases / test_metrics / tools), and every one read
 * the implicit global `event`, which only has a value when the function is
 * called from an inline attribute — the very thing the CSP blocks. Tabs
 * are delegated in static/js/ui-handlers.js via data-tab, using the real
 * event object.
 */

/* ── Collapsible sections ──────────────────────────
 * Removed. One of two identical definitions (the other lived inline in
 * user_stories.html), and its only caller was an onclick= attribute the
 * CSP blocks. Collapsing is now delegated in static/js/ui-handlers.js via
 * data-collapse, and the chevron rotates in CSS rather than having a text
 * triangle written over the lucide icon it contains.
 */

/* ── Test case filter ──────────────────────────────────────────
 * Removed. This was a *second* definition of filterTC — the one in
 * test_cases.html's inline block shadowed it, and that one also honoured
 * the suite filter this copy ignored. Both were dead either way: the
 * buttons calling them were inline onclick= attributes, which the CSP
 * blocks. Filtering now lives in static/js/ui-handlers.js.
 */

/* ── Drag & Drop file upload ───────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
    const dropZone = document.querySelector('.file-drop-zone');
    const fileInput = document.querySelector('input[name="req_files"]');

    if (dropZone && fileInput) {
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            dropZone.addEventListener(eventName, e => { e.preventDefault(); e.stopPropagation(); });
        });

        ['dragenter', 'dragover'].forEach(eventName => {
            dropZone.addEventListener(eventName, () => dropZone.classList.add('drag-over'));
        });

        ['dragleave', 'drop'].forEach(eventName => {
            dropZone.addEventListener(eventName, () => dropZone.classList.remove('drag-over'));
        });

        dropZone.addEventListener('drop', e => {
            fileInput.files = e.dataTransfer.files;
            updateFileList(fileInput);
        });

        dropZone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => updateFileList(fileInput));
    }
});

function updateFileList(input) {
    let listEl = document.querySelector('.file-list');
    if (!listEl) {
        listEl = document.createElement('div');
        listEl.className = 'file-list';
        input.closest('.file-drop-zone')?.after(listEl);
    }
    listEl.innerHTML = '';
    Array.from(input.files).forEach(f => {
        const size = f.size < 1024*1024 ? (f.size/1024).toFixed(1)+' KB' : (f.size/(1024*1024)).toFixed(1)+' MB';
        listEl.innerHTML += `<div class="file-item"><span class="file-name">${f.name}</span><span class="file-size">${size}</span></div>`;
    });
}

/* ── Insert example requirements ───────────────────────────── */
function insertExample() {
    const textarea = document.querySelector('textarea[name="requirements_text"]');
    if (textarea) {
        textarea.value = `REQ-001: The system must allow users to register with email and password
REQ-002: Users should be able to login using their credentials
REQ-003: The system must display a dashboard with key metrics
REQ-004: Users can search and filter products by category
REQ-005: The system must support payment processing via credit card
REQ-006: Users should receive email notifications for order status changes
REQ-007: Admin can manage user accounts (create, edit, deactivate)
REQ-008: The system must export reports in PDF and CSV formats`;
        textarea.focus();
    }
}

/* ── Status dropdown color ─────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.status-select, .status-select-sm').forEach(select => {
        applyStatusColor(select);
        select.addEventListener('change', () => applyStatusColor(select));
    });
});

function applyStatusColor(select) {
    const val = select.value.toLowerCase().replace(/\s+/g, '-');
    select.className = select.className.replace(/status-\S+/g, '').trim();
    select.classList.add('status-select');
    if (val && val !== 'unchecked') select.classList.add('status-' + val);
}

/* ── QA Assistant (chatbot) ──────────────────────────────────── */
(function () {
    const root = document.getElementById('qa-chat');
    if (!root) return;

    const toggleBtn = root.querySelector('.qa-chat-toggle');
    const panel     = root.querySelector('.qa-chat-panel');
    const closeBtn  = root.querySelector('[data-chat-close]');
    const resetBtn  = root.querySelector('[data-chat-reset]');
    const body      = root.querySelector('.qa-chat-body');
    const form      = root.querySelector('.qa-chat-foot');
    const input     = form.querySelector('input[name="message"]');
    const sendBtn   = form.querySelector('button[type="submit"]');
    const lang      = root.dataset.lang || 'en';
    const initialGreeting = root.dataset.greeting || '';

    function escapeHtml(s) {
        return String(s).replace(/[&<>"]/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'
        }[c]));
    }
    function mdToHtml(s) {
        // Italics were never handled, so every single-asterisk and
        // underscore emphasis reached the user as literal punctuation —
        // "— *EP (verbatim):*" from the guide replies, and the
        // "_Book · page 224_" citation the ISTQB retrieval path appends to
        // every answer. Bold ran first and still does, so by the time the
        // italic passes run there are no ** pairs left to confuse them.
        //
        // Both patterns are deliberately narrow. The underscore form
        // requires whitespace or a bracket before the opener and
        // punctuation or end-of-string after the closer, so snake_case
        // identifiers in answers about `browser_pool` or `run_id` are left
        // alone; the asterisk form refuses a leading space so a literal
        // "2 * 3" is not read as an opener.
        return escapeHtml(s)
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*\w])\*(?!\s)([^*\n]+?)\*(?!\*)/g,
                     '$1<em>$2</em>')
            .replace(/(^|[\s(])_(?!\s)([^_\n]+?)_(?=[\s.,;:!?)]|$)/g,
                     '$1<em>$2</em>')
            .replace(/\n/g, '<br>');
    }
    function scrollBottom() { body.scrollTop = body.scrollHeight; }

    function renderMessage(role, text, chips) {
        const msg = document.createElement('div');
        msg.className = 'qa-chat-msg ' + role;
        msg.innerHTML = mdToHtml(text);
        body.appendChild(msg);

        if (Array.isArray(chips) && chips.length) {
            const row = document.createElement('div');
            row.className = 'qa-chat-chips';
            chips.forEach(c => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'qa-chat-chip';
                chip.textContent = c;
                chip.addEventListener('click', () => { sendMessage(c); });
                row.appendChild(chip);
            });
            body.appendChild(row);
        }
        scrollBottom();
    }

    function openPanel() {
        panel.classList.add('open');
        toggleBtn.setAttribute('aria-expanded', 'true');
        if (!body.dataset.seeded) {
            body.dataset.seeded = '1';
            if (initialGreeting) {
                renderMessage('bot', initialGreeting);
            }
        }
        setTimeout(() => input.focus(), 50);
    }
    function closePanel() {
        panel.classList.remove('open');
        toggleBtn.setAttribute('aria-expanded', 'false');
    }

    // CSRF token is injected by base.html — forwarded as X-CSRFToken
    // so Flask-WTF accepts our JSON POSTs. Falls back gracefully if the
    // meta is missing (e.g. during development against an older build).
    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

    function csrfHeaders(extra) {
        const h = Object.assign({}, extra || {});
        if (csrfToken) h['X-CSRFToken'] = csrfToken;
        return h;
    }

    /* Render the in-chat bug-report form when intent === 'bug_form'.
       Required fields per the spec: Summary, Environment, Steps to
       Reproduce, Actual Result, Expected Result. Optional: Attachment,
       Note. POSTs as multipart/form-data to /chat/bug-form. */
    const BUG_FORM_LABELS = lang === 'ua' ? {
        legend:      'Створити баг-репорт',
        summary:     'Summary (короткий заголовок)',
        environment: 'Environment (OS / браузер / пристрій)',
        steps:       'Steps to Reproduce (по одному кроку на рядок)',
        actual:      'Actual Result (що сталося)',
        expected:    'Expected Result (що мало статися)',
        attachment:  'Attachment (опційно)',
        note:        'Note (опційно)',
        submit:      'Створити баг',
        cancel:      'Скасувати',
        success:     'Баг-репорт створено',
        missing:     'Заповніть, будь ласка, обовʼязкові поля',
    } : {
        legend:      'Create a bug report',
        summary:     'Summary (short title)',
        environment: 'Environment (OS / browser / device)',
        steps:       'Steps to Reproduce (one step per line)',
        actual:      'Actual Result (what happened)',
        expected:    'Expected Result (what should have happened)',
        attachment:  'Attachment (optional)',
        note:        'Note (optional)',
        submit:      'Create bug',
        cancel:      'Cancel',
        success:     'Bug report created',
        missing:     'Please fill in the required fields',
    };

    function renderBugForm() {
        const L = BUG_FORM_LABELS;
        const wrap = document.createElement('form');
        wrap.className = 'qa-chat-bugform';
        wrap.enctype = 'multipart/form-data';
        wrap.innerHTML = `
            <legend>${escapeHtml(L.legend)}</legend>
            <label>${escapeHtml(L.summary)}
                <input type="text" name="summary" required maxlength="200">
            </label>
            <label>${escapeHtml(L.environment)}
                <input type="text" name="environment" required
                       placeholder="Windows 11 / Chrome 138 / Desktop">
            </label>
            <label>${escapeHtml(L.steps)}
                <textarea name="steps_to_reproduce" required></textarea>
            </label>
            <label>${escapeHtml(L.actual)}
                <textarea name="actual_result" required></textarea>
            </label>
            <label>${escapeHtml(L.expected)}
                <textarea name="expected_result" required></textarea>
            </label>
            <label>${escapeHtml(L.attachment)}
                <input type="file" name="attachment">
            </label>
            <label>${escapeHtml(L.note)}
                <textarea name="note"></textarea>
            </label>
            <div class="qa-bug-error" hidden></div>
            <div class="qa-bug-actions">
                <button type="button" data-bug-cancel>${escapeHtml(L.cancel)}</button>
                <button type="submit">${escapeHtml(L.submit)}</button>
            </div>
        `;
        body.appendChild(wrap);
        // Scroll the form's "Create a bug report" header to the top of the
        // chat body so the first input is immediately visible (instead of
        // forcing the user to scroll up past prior messages).
        requestAnimationFrame(() => {
            const top = wrap.offsetTop - body.offsetTop;
            body.scrollTop = Math.max(0, top - 4);
            // Move focus into the form for keyboard users.
            const firstInput = wrap.querySelector('input[name="summary"]');
            if (firstInput) firstInput.focus({ preventScroll: true });
        });

        const errBox = wrap.querySelector('.qa-bug-error');
        wrap.querySelector('[data-bug-cancel]').addEventListener('click', () => {
            wrap.remove();
        });
        wrap.addEventListener('submit', async (e) => {
            e.preventDefault();
            errBox.hidden = true;
            const fd = new FormData(wrap);
            const submitBtn = wrap.querySelector('button[type=submit]');
            submitBtn.disabled = true;
            try {
                const resp = await fetch('/chat/bug-form', {
                    method: 'POST',
                    headers: csrfHeaders(),  // no Content-Type → browser sets multipart boundary
                    body: fd,
                });
                const data = await resp.json();
                if (!resp.ok || !data.ok) {
                    errBox.textContent = data.message || L.missing;
                    errBox.hidden = false;
                    submitBtn.disabled = false;
                    return;
                }
                wrap.remove();
                renderMessage('bot', `${L.success}: ${data.id}`);
            } catch (err) {
                errBox.textContent = 'Network error. Please try again.';
                errBox.hidden = false;
                submitBtn.disabled = false;
            }
        });
    }

    /* Legacy blocking POST path — also the fallback for browsers without
       EventSource and for any SSE error before the first delta lands. */
    async function sendMessagePost(msg) {
        try {
            const resp = await fetch('/chat', {
                method: 'POST',
                headers: csrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ message: msg, lang }),
            });
            const data = await resp.json();
            const chips = (data.follow_up && data.follow_up.length)
                ? data.follow_up
                : (data.suggestions || []);
            renderMessage('bot', data.text || '...', chips);
            if (data.intent === 'bug_form') {
                renderBugForm();
            }
        } catch (err) {
            renderMessage('bot', 'Network error. Please try again.');
        }
    }

    /* SSE streaming path — same contract as /chat but tokens arrive
       progressively. Frame protocol:
         event: meta   {intent, lang}             — before first delta
         event: delta  {text}                     — per token chunk
         event: full   {text, intent, ...}        — fast-path single shot
         event: done   {intent, suggestions, ...} — terminal frame
         event: error  {message}                  — failure mid-stream
    */
    function sendMessageStream(msg) {
        return new Promise((resolve) => {
            let es;
            try {
                const qs = new URLSearchParams({ message: msg, lang });
                es = new EventSource('/chat/stream?' + qs.toString());
            } catch (e) {
                resolve({ ok: false, reason: 'eventsource-construct' });
                return;
            }
            let botEl = null;
            let plainBuf = '';
            let receivedAny = false;
            let finalIntent = '';
            let finalChips = [];

            function ensureBotEl() {
                if (botEl) return botEl;
                botEl = document.createElement('div');
                botEl.className = 'qa-chat-msg bot';
                body.appendChild(botEl);
                return botEl;
            }
            function applyText(text) {
                ensureBotEl().innerHTML = mdToHtml(text);
                scrollBottom();
            }

            es.addEventListener('meta', () => {
                receivedAny = true;
                // Show a placeholder bot bubble so the user gets immediate feedback.
                ensureBotEl();
            });

            es.addEventListener('delta', (ev) => {
                receivedAny = true;
                try {
                    const d = JSON.parse(ev.data);
                    if (d && typeof d.text === 'string') {
                        plainBuf += d.text;
                        applyText(plainBuf);
                    }
                } catch (e) { /* malformed frame — ignore */ }
            });

            es.addEventListener('full', (ev) => {
                receivedAny = true;
                try {
                    const d = JSON.parse(ev.data);
                    plainBuf = d.text || '';
                    applyText(plainBuf);
                    if (d.intent) finalIntent = d.intent;
                    const chips = (d.follow_up && d.follow_up.length)
                        ? d.follow_up : (d.suggestions || []);
                    if (chips && chips.length) finalChips = chips;
                } catch (e) { /* ignore */ }
            });

            es.addEventListener('done', (ev) => {
                try {
                    const d = JSON.parse(ev.data);
                    if (d && d.intent) finalIntent = finalIntent || d.intent;
                    const chips = (d && d.follow_up && d.follow_up.length)
                        ? d.follow_up
                        : (d && d.suggestions) || [];
                    if (chips && chips.length) finalChips = chips;
                } catch (e) { /* tolerate empty done frame */ }
                es.close();
                // Render chip row (if any) attached after the streamed bubble.
                if (finalChips.length && botEl) {
                    const row = document.createElement('div');
                    row.className = 'qa-chat-chips';
                    finalChips.forEach(c => {
                        const chip = document.createElement('button');
                        chip.type = 'button';
                        chip.className = 'qa-chat-chip';
                        chip.textContent = c;
                        chip.addEventListener('click', () => { sendMessage(c); });
                        row.appendChild(chip);
                    });
                    body.appendChild(row);
                    scrollBottom();
                }
                if (finalIntent === 'bug_form') {
                    renderBugForm();
                }
                resolve({ ok: true });
            });

            es.addEventListener('error', (ev) => {
                // Either an explicit `event: error` frame from the server
                // or a transport-level failure (EventSource's default
                // onerror also fires here). Close and let the caller
                // decide whether to fall back to POST.
                try { es.close(); } catch (e) { /* noop */ }
                if (botEl && plainBuf) {
                    // We already streamed something — keep what the user saw.
                    resolve({ ok: true });
                } else {
                    // Drop the empty bubble so the POST fallback can render fresh.
                    if (botEl && botEl.parentNode) {
                        botEl.parentNode.removeChild(botEl);
                    }
                    resolve({ ok: false, reason: 'eventsource-error', receivedAny });
                }
            });
        });
    }

    async function sendMessage(text) {
        const msg = (text || '').trim();
        if (!msg) return;
        renderMessage('user', msg);
        input.value = '';
        sendBtn.disabled = true;
        try {
            if ('EventSource' in window) {
                const result = await sendMessageStream(msg);
                if (!result.ok) {
                    await sendMessagePost(msg);
                }
            } else {
                await sendMessagePost(msg);
            }
        } finally {
            sendBtn.disabled = false;
            input.focus();
        }
    }

    toggleBtn.addEventListener('click', openPanel);
    closeBtn.addEventListener('click', closePanel);
    if (resetBtn) {
        resetBtn.addEventListener('click', async () => {
            body.innerHTML = '';
            body.dataset.seeded = '';
            try {
                await fetch('/chat/reset', {
                    method: 'POST',
                    headers: csrfHeaders(),
                });
            } catch (e) {}
            openPanel();
        });
    }
    form.addEventListener('submit', e => {
        e.preventDefault();
        sendMessage(input.value);
    });

    // Optional: open panel with Ctrl+/
    document.addEventListener('keydown', e => {
        if (e.ctrlKey && e.key === '/') {
            e.preventDefault();
            panel.classList.contains('open') ? closePanel() : openPanel();
        }
    });
})();

/* ── Back-to-Top button ───────────────────────────────────────────
   Shows after the user scrolls 400px down; smooth-scrolls to the top
   when clicked. Uses requestAnimationFrame so the scroll listener
   stays cheap on long pages (Test Cases / Bug Reports lists).         */
(function () {
    const btn = document.getElementById('back-to-top');
    if (!btn) return;

    const SHOW_AFTER_PX = 250;
    let ticking = false;

    function update() {
        const y = window.scrollY || document.documentElement.scrollTop;
        if (y > SHOW_AFTER_PX) {
            btn.hidden = false;
            btn.classList.add('is-visible');
        } else {
            btn.classList.remove('is-visible');
            // Wait for the fade-out before re-hiding so screen readers
            // don't get a flicker of focusable button on every scroll.
            setTimeout(() => {
                if (!btn.classList.contains('is-visible')) btn.hidden = true;
            }, 220);
        }
        ticking = false;
    }

    window.addEventListener('scroll', () => {
        if (!ticking) {
            window.requestAnimationFrame(update);
            ticking = true;
        }
    }, { passive: true });

    btn.addEventListener('click', () => {
        window.scrollTo({ top: 0, behavior: 'smooth' });
    });

    // Initial state on page load (e.g. after browser-back to a scrolled page).
    update();
})();


/* ── Fetch + CSRF resilience helpers (window.TFG) ──────────────────
 *
 * Shared by every page that submits a form with fetch(). Extracted
 * after a production bug on /test-cases: the modal reported
 * "Could not reach the server — retrying directly." with Elapsed 0s
 * while the server had in fact answered, clearly, 400 "session
 * expired".
 *
 * Two mistakes combined:
 *   1. The page called r.json() on every response. The CSRF handler
 *      answered text/plain, so JSON.parse threw and the SyntaxError
 *      landed in the network .catch() — which blamed connectivity.
 *   2. Its Retry button re-posted the same dead token, so the error
 *      was unrecoverable without a manual reload.
 *
 * The trigger is routine rather than exotic on the free Render plan:
 * the service sleeps after ~15 min and SESSION_TYPE=filesystem sits on
 * an ephemeral disk, so every cold start invalidates the csrf_token
 * held by any tab that was already open.
 *
 * Four templates had copy-pasted the same pattern; they all use these
 * helpers now so the next copy cannot reintroduce it.
 */
(function () {
    'use strict';

    var TFG = window.TFG = window.TFG || {};

    /* Headers that make a request identifiably a fetch/XHR, so the
     * server's error handlers answer in JSON. */
    TFG.jsonHeaders = function () {
        return { 'Accept': 'application/json',
                 'X-Requested-With': 'XMLHttpRequest' };
    };

    /* Read a response without assuming its content type. Always
     * resolves; never throws on a non-JSON body. */
    TFG.readResponse = function (r) {
        return r.text().then(function (raw) {
            var body = null;
            try { body = raw ? JSON.parse(raw) : null; } catch (_) { body = null; }
            return { ok: r.ok, status: r.status, body: body, text: raw || '' };
        });
    };

    /* True when a rejection is an expired/missing CSRF token — i.e.
     * retrying the same payload is pointless until the token is
     * refreshed. Falls back to sniffing the text body so it still
     * works against an older server that answers text/plain. */
    TFG.isCsrfFailure = function (res) {
        if (!res || res.status !== 400) return false;
        if (res.body && (res.body.error === 'csrf' || res.body.reload_required)) {
            return true;
        }
        return /csrf|session expired/i.test(res.text || '');
    };

    /* Mint a fresh token and write it into the form's hidden field.
     * Resolves true on success, false otherwise — callers decide
     * whether to replay the submit or ask the user to reload. */
    TFG.refreshCsrfToken = function (form) {
        return fetch('/api/csrf-token', {
            cache: 'no-store', credentials: 'same-origin',
            headers: TFG.jsonHeaders()
        })
        .then(TFG.readResponse)
        .then(function (res) {
            var token = res.body && res.body.token;
            if (!res.ok || !token) return false;
            var field = form && form.querySelector('input[name="csrf_token"]');
            if (!field) return false;
            field.value = token;
            return true;
        })
        .catch(function () { return false; });
    };

    /* Best available human-readable message for a failed response,
     * preferring the server's own words over a generic guess. */
    TFG.errorMessage = function (res, fallback) {
        if (res && res.body && (res.body.message || res.body.error)) {
            return res.body.message || res.body.error;
        }
        if (res && res.text) return String(res.text).slice(0, 300);
        if (res && res.status) return 'Server returned HTTP ' + res.status + '.';
        return fallback || 'Something went wrong.';
    };
})();

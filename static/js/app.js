/**
 * TestFortge — Main JavaScript
 * Handles: tooltips, tabs, filters, collapsibles, drag-drop
 */

/* ── Phone-width navigation drawer ─────────────────────────────────
   At ≤768px (style.css) the sidebar is off-canvas and the .mobile-bar
   hamburger ([data-nav-toggle], aria-controls="sidebar-nav") opens it.
   aria-expanded carries the state; Esc, a click outside the drawer (the
   scrim) or following a link closes it, and closing by Esc or the scrim
   returns focus to the toggle. Above 768px the toggle is not displayed
   and an open drawer is closed, so a rotated tablet never keeps the
   scrim. */
(function () {
    const toggle = document.querySelector('[data-nav-toggle]');
    const nav = toggle && document.getElementById(toggle.getAttribute('aria-controls'));
    if (!toggle || !nav) return;
    const mq = window.matchMedia('(max-width: 768px)');

    function isOpen() { return toggle.getAttribute('aria-expanded') === 'true'; }
    function open() {
        toggle.setAttribute('aria-expanded', 'true');
        nav.classList.add('is-open');
        document.body.classList.add('nav-open');
        const first = nav.querySelector('a[href], button:not([disabled]), select, input');
        if (first) first.focus();
    }
    function close(returnFocus) {
        if (!isOpen()) return;
        toggle.setAttribute('aria-expanded', 'false');
        nav.classList.remove('is-open');
        document.body.classList.remove('nav-open');
        if (returnFocus) toggle.focus();
    }

    toggle.addEventListener('click', () => { if (isOpen()) close(true); else open(); });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isOpen()) { e.preventDefault(); close(true); }
    });
    document.addEventListener('click', (e) => {
        if (!isOpen()) return;
        if (nav.contains(e.target) || toggle.contains(e.target)) return;
        e.preventDefault();
        close(true);
    }, true);
    nav.addEventListener('click', (e) => {
        if (e.target.closest && e.target.closest('a[href]')) close(false);
    });
    const onChange = () => { if (!mq.matches) close(false); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
})();

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

/* ── Tab switching (global) ──────────────────────────────────── */
function showTab(tabId, btn) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    (btn || event.target).classList.add('active');
}

/* ── Collapsible sections ────────────────────────────────────── */
function toggleCollapse(header) {
    const body = header.nextElementSibling;
    const icon = header.querySelector('.collapse-icon');
    if (body) {
        body.classList.toggle('collapsed');
        if (icon) {
            icon.textContent = body.classList.contains('collapsed') ? '\u25B6' : '\u25BC';
        }
    }
}

/* ── Test case filter ────────────────────────────────────────── */
function filterTC(category, btn) {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tc-card').forEach(card => {
        card.hidden = !(category === 'all' || card.dataset.category === category);
    });
}

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
    /* [[REF]] citations (ADR 0003 T8): a ref the server retrieved —
       present in `refs` ({ref: url}) with a same-origin path — becomes a
       link; anything else is shown as its plain text. Runs on the
       already-escaped HTML, so the ref text and the URL are escaped. */
    function linkRefs(html, refs) {
        return html.replace(/\[\[([^\[\]\n]{1,80})\]\]/g, (m, ref) => {
            const key = ref.trim();
            const url = refs && Object.prototype.hasOwnProperty.call(refs, key)
                ? String(refs[key] || '') : '';
            if (url && url.charAt(0) === '/' && url.charAt(1) !== '/') {
                return '<a class="qa-chat-ref" href="' + escapeHtml(url) + '">' + ref + '</a>';
            }
            return ref;
        });
    }
    function mdToHtml(s, refs) {
        return linkRefs(mdToHtmlBase(s), refs);
    }
    function mdToHtmlBase(s) {
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

    function renderMessage(role, text, chips, refs) {
        const msg = document.createElement('div');
        msg.className = 'qa-chat-msg ' + role;
        msg.innerHTML = mdToHtml(text, refs);
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
                <button type="button" class="btn btn-secondary btn-sm" data-bug-cancel>${escapeHtml(L.cancel)}</button>
                <button type="submit" class="btn btn-primary btn-sm">${escapeHtml(L.submit)}</button>
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
            renderMessage('bot', data.text || '...', chips, data.refs);
            if (data.intent === 'bug_form') {
                renderBugForm();
            }
        } catch (err) {
            renderMessage('bot', 'Network error. Please try again.');
        }
    }

    /* SSE streaming path — same contract as /chat but tokens arrive
       progressively. Frame protocol:
         event: meta   {intent, lang, refs?}      — before first delta
                       (refs = {ref: url}: [[REF]] citations to link, T8)
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
            let refs = null;    // {ref: url} — T8 citations, from meta/full/done

            function ensureBotEl() {
                if (botEl) return botEl;
                botEl = document.createElement('div');
                botEl.className = 'qa-chat-msg bot';
                body.appendChild(botEl);
                return botEl;
            }
            function applyText(text) {
                ensureBotEl().innerHTML = mdToHtml(text, refs);
                scrollBottom();
            }

            es.addEventListener('meta', (ev) => {
                receivedAny = true;
                try {
                    const d = JSON.parse(ev.data);
                    if (d && d.refs) refs = d.refs;
                } catch (e) { /* tolerate an empty meta frame */ }
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
                    if (d.refs) refs = d.refs;
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
                    if (d && d.refs) {
                        refs = d.refs;
                        if (plainBuf) applyText(plainBuf);
                    }
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

/* ── Model switcher (ADR 0003 §4.7) ─────────────────────────────────
 * The sidebar <select> rendered by base.html when MODEL_SWITCHER is on.
 * PUTs {"*": tier} to /api/me/model-preference with the page's CSRF
 * token; an empty tier clears the choice (team default). The server
 * re-checks the org's allow-list and the premium-key rule, so a refused
 * choice reverts the control and says it was not saved. */
(function () {
    'use strict';
    document.addEventListener('DOMContentLoaded', function () {
        var box = document.querySelector('[data-model-switcher]');
        if (!box) return;
        var select = box.querySelector('select');
        var status = box.querySelector('[data-model-switcher-status]');
        if (!select) return;
        var previous = select.value;
        var meta = document.querySelector('meta[name="csrf-token"]');
        function fail() {
            select.value = previous;
            if (status) status.textContent = box.getAttribute('data-msg-failed');
        }
        select.addEventListener('change', function () {
            var headers = {'Content-Type': 'application/json',
                           'Accept': 'application/json'};
            if (meta) headers['X-CSRFToken'] = meta.getAttribute('content');
            select.disabled = true;
            fetch(box.getAttribute('data-url'), {
                method: 'PUT', credentials: 'same-origin', headers: headers,
                body: JSON.stringify({'*': select.value})
            })
            .then(function (resp) {
                return resp.json().catch(function () { return {}; })
                    .then(function (body) { return {ok: resp.ok, body: body}; });
            })
            .then(function (res) {
                if (!res.ok) { fail(); return; }
                previous = select.value;
                if (status) {
                    status.textContent = box.getAttribute('data-msg-saved') +
                        (res.body.effective_model ? ' · ' + res.body.effective_model : '');
                }
            })
            .catch(fail)
            .then(function () { select.disabled = false; });
        });
    });
})();

/* ── Declarative handlers ───────────────────────────────────────
   script-src carries a per-request nonce and no 'unsafe-inline' (commit
   466226e), so the browser never runs an on*="" attribute. Twenty-one of
   them were left in the templates and silently did nothing: the test-case
   tabs and filters, the checklist filters — and every confirm() before a
   destructive POST, so deleting a project asked nothing (found in the live
   audit, 2026-10-16). Templates declare behaviour with data attributes;
   these listeners carry it out. tests/test_csp_headers.py forbids on*=.

   data-call="fn" data-arg="x"   click calls fn(x, element); fn must be in
                                 CALLS and defined on the page
   data-confirm="message"        on a form: asked before it submits; on a
                                 button or link: asked before the click acts
   data-autosubmit               on a select: submits its form on change
   data-status-class             on a select: class follows the chosen status
*/
(function () {
    var CALLS = { showTab: 1, filterTC: 1, filterSuite: 1, filterCL: 1 };

    document.addEventListener('click', function (e) {
        var ask = e.target.closest('button[data-confirm], a[data-confirm], input[data-confirm]');
        if (ask && !window.confirm(ask.getAttribute('data-confirm'))) {
            e.preventDefault();
            e.stopImmediatePropagation();
            return;
        }
        var el = e.target.closest('[data-call]');
        if (!el) return;
        var fn = el.getAttribute('data-call');
        if (CALLS[fn] && typeof window[fn] === 'function') {
            window[fn](el.getAttribute('data-arg'), el);
        }
    });

    // Capture phase: refuse before any other submit listener (spinners,
    // fetch handlers) reacts to a submission the user called off.
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (form.matches && form.matches('form[data-confirm]')
                && !window.confirm(form.getAttribute('data-confirm'))) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);

    document.addEventListener('change', function (e) {
        var t = e.target;
        if (!t.matches) return;
        if (t.matches('select[data-autosubmit]') && t.form) {
            t.form.submit();
        }
        if (t.matches('select[data-status-class]')) {
            t.className = 'status-select status-'
                + t.value.toLowerCase().replace(' ', '-');
        }
    });
})();

/* â”€â”€ Toggletips (templates/_toggletip.html) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
   WCAG 2.1 SC 1.4.13: a note shown on hover or focus must be
   dismissible (Escape), hoverable (the pointer may move onto it) and
   persistent (no timeout). Mouse hover and keyboard focus open it for a
   look; a click or tap pins it open until the next click, a click
   outside, or Escape. Touch has no hover, so a tap is the way in there.
   One note is open at a time. The opener fires `tf-tip-open` on the
   wrapper so a note with remote content (the page tips) loads lazily.
*/
(function () {
    var open = null;        // the open [data-tip] wrapper
    var pinned = false;     // opened by click/tap rather than hover/focus
    var quiet = null;       // closed by Escape: no re-open until left
    var timer = null;

    function parts(w) {
        return { btn: w.querySelector('.tf-tip-btn'), note: w.querySelector('.tf-tip-bubble') };
    }

    // Keep the note inside the viewport: centred under its "?" where it
    // fits, slid sideways by just enough where it does not. A position
    // offset, not a display/colour write (block B rules); CSSOM writes
    // are not inline style attributes, so the CSP allows them.
    function place(note) {
        note.style.setProperty('--tip-shift', '0px');
        var r = note.getBoundingClientRect();
        var vw = document.documentElement.clientWidth;
        var shift = 0;
        if (r.right > vw - 8) shift = (vw - 8) - r.right;
        if (r.left + shift < 8) shift = 8 - r.left;
        note.style.setProperty('--tip-shift', Math.round(shift) + 'px');
    }

    function show(w, pin) {
        clearTimeout(timer);
        if (open && open !== w) hide(open);
        var p = parts(w);
        var was = open === w;
        open = w;
        pinned = pin || (was && pinned);
        if (!was) {
            p.note.hidden = false;
            p.btn.setAttribute('aria-expanded', 'true');
            place(p.note);
            w.dispatchEvent(new CustomEvent('tf-tip-open', { bubbles: true }));
        }
    }

    function hide(w) {
        clearTimeout(timer);
        var p = parts(w);
        p.note.hidden = true;
        p.btn.setAttribute('aria-expanded', 'false');
        if (open === w) { open = null; pinned = false; }
    }

    function later(w) {
        clearTimeout(timer);
        timer = setTimeout(function () { if (open === w && !pinned) hide(w); }, 300);
    }

    function tipOf(node) {
        return node && node.closest ? node.closest('[data-tip]') : null;
    }

    document.addEventListener('pointerover', function (e) {
        if (e.pointerType !== 'mouse') return;
        var w = tipOf(e.target);
        if (!w || w === quiet) return;
        show(w, false);
    });
    document.addEventListener('pointerout', function (e) {
        if (e.pointerType !== 'mouse') return;
        var w = tipOf(e.target);
        if (!w || w.contains(e.relatedTarget)) return;
        if (w === quiet) { quiet = null; return; }
        if (w === open && !pinned) later(w);
    });

    document.addEventListener('focusin', function (e) {
        var w = tipOf(e.target);
        if (w && w !== quiet && e.target.classList.contains('tf-tip-btn')) show(w, false);
    });
    document.addEventListener('focusout', function (e) {
        var w = tipOf(e.target);
        if (!w || w.contains(e.relatedTarget)) return;
        if (w === quiet) quiet = null;
        if (w === open && !pinned) hide(w);
    });

    document.addEventListener('click', function (e) {
        var btn = e.target.closest ? e.target.closest('.tf-tip-btn') : null;
        if (btn) {
            var w = tipOf(btn);
            // Inside a <label> the click must not also tick the input.
            e.preventDefault();
            quiet = null;
            // Closing by click: hover over the button must not reopen it.
            if (open === w && pinned) { hide(w); quiet = w; }
            else show(w, true);
            return;
        }
        if (open && !open.contains(e.target)) hide(open);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape' || !open) return;
        var w = open;
        var inside = w.contains(document.activeElement);
        hide(w);
        quiet = w;
        if (inside) parts(w).btn.focus();
        e.stopPropagation();
    });

    window.addEventListener('resize', function () {
        if (open) place(parts(open).note);
    });
})();

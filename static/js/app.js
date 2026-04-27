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
});

/* ── Tab switching (global) ──────────────────────────────────── */
function showTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    event.target.classList.add('active');
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
        card.style.display = (category === 'all' || card.dataset.category === category) ? '' : 'none';
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
    function mdToHtml(s) {
        return escapeHtml(s)
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
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

    async function sendMessage(text) {
        const msg = (text || '').trim();
        if (!msg) return;
        renderMessage('user', msg);
        input.value = '';
        sendBtn.disabled = true;
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
            // Open the structured bug form when the bot signals it.
            if (data.intent === 'bug_form') {
                renderBugForm();
            }
        } catch (err) {
            renderMessage('bot', 'Network error. Please try again.');
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

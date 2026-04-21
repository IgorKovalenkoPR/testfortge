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

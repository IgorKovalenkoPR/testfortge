/* TestForTge — folders for test cases and checklist items.
 *
 * Draws the tree in templates/_folder_tree.html from its JSON, filters the
 * page's items by the selected folder (sub-folders included), and — when
 * editing is on — creates, renames, deletes and moves folders and files
 * items into them through /api/folders (routes/folders.py).
 *
 * Every write re-reads GET /api/folders and redraws, rather than patching
 * the local copy: a colleague's change made since the page loaded then shows
 * up too, and there is one drawing path instead of five.
 *
 * Items are any element with data-folder-item="<public id>". Section headers
 * (data-folder-section) are hidden when nothing under them is visible, so a
 * filtered page does not show a column of empty headings.
 */
(function () {
    'use strict';

    const panel = document.querySelector('[data-folder-panel]');
    if (!panel) return;

    const kind = panel.getAttribute('data-folder-kind');
    const editable = panel.getAttribute('data-folder-editable') === '1';
    const treeEl = panel.querySelector('[data-folder-tree]');
    const statusEl = panel.querySelector('[data-folder-status]');
    const moveBox = panel.querySelector('[data-folder-move]');
    const moveSelect = panel.querySelector('[data-folder-move-target]');
    const i18n = JSON.parse(panel.querySelector('[data-folder-i18n]').textContent);
    let data = JSON.parse(panel.querySelector('[data-folder-json]').textContent);
    const storeKey = 'tf-folder-selected:' + kind;
    const ITEM_MIME = 'application/x-tf-item';
    const FOLDER_MIME = 'application/x-tf-folder';

    let selected = 'all';
    try {
        selected = window.localStorage.getItem(storeKey) || 'all';
    } catch (err) { /* private window — default is fine */ }
    const collapsed = new Set();

    // ── data helpers ─────────────────────────────────────────────

    function byId() {
        const map = new Map();
        data.folders.forEach(f => map.set(f.id, f));
        return map;
    }

    function childrenOf(parentId) {
        return data.folders.filter(f => (f.parent_id ?? null) === parentId);
    }

    function subtreeIds(folderId) {
        const out = new Set([folderId]);
        const stack = [folderId];
        while (stack.length) {
            const id = stack.pop();
            childrenOf(id).forEach(c => {
                if (!out.has(c.id)) { out.add(c.id); stack.push(c.id); }
            });
        }
        return out;
    }

    function assignments() {
        return (data.assignments && data.assignments[kind]) || {};
    }

    function items() {
        return Array.from(document.querySelectorAll('[data-folder-item]'));
    }

    function folderOf(itemId) {
        const fid = assignments()[itemId];
        return fid === undefined ? null : fid;
    }

    function pathOf(folderId) {
        const map = byId();
        const names = [];
        let cursor = map.get(folderId);
        const seen = new Set();
        while (cursor && !seen.has(cursor.id)) {
            seen.add(cursor.id);
            names.unshift(cursor.name);
            cursor = map.get(cursor.parent_id);
        }
        return names.join(' / ');
    }

    function counts() {
        // Direct counts per folder for the items this page lists, then
        // rolled up so a parent shows everything under it.
        const direct = new Map();
        let unfiled = 0;
        const present = new Set(items().map(el => el.getAttribute('data-folder-item')));
        present.forEach(id => {
            const fid = folderOf(id);
            if (fid === null || !byId().has(fid)) unfiled += 1;
            else direct.set(fid, (direct.get(fid) || 0) + 1);
        });
        const total = new Map();
        function roll(id) {
            let n = direct.get(id) || 0;
            childrenOf(id).forEach(c => { n += roll(c.id); });
            total.set(id, n);
            return n;
        }
        childrenOf(null).forEach(f => roll(f.id));
        return { total: total, unfiled: unfiled, all: present.size };
    }

    function t(key, vars) {
        let text = i18n[key] || key;
        Object.keys(vars || {}).forEach(k => {
            text = text.split('{' + k + '}').join(String(vars[k]));
        });
        return text;
    }

    // ── network ──────────────────────────────────────────────────

    function csrf() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    async function call(method, url, body) {
        const resp = await fetch(url, {
            method: method,
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-CSRFToken': csrf(),
            },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        let payload = null;
        try { payload = await resp.json(); } catch (err) { payload = null; }
        if (!resp.ok) {
            throw new Error((payload && payload.message) || t('error'));
        }
        return payload;
    }

    async function refresh() {
        data = await call('GET', '/api/folders');
        if (typeof selected === 'number' && !byId().has(selected)) {
            selected = 'all';
        }
        render();
    }

    function status(message, kindOf) {
        if (!statusEl) return;
        statusEl.textContent = message || '';
        statusEl.className = 'folder-status' + (kindOf ? ' folder-status-' + kindOf : '');
        if (kindOf === 'ok') {
            window.setTimeout(() => {
                if (statusEl.textContent === message) statusEl.textContent = '';
            }, 2500);
        }
    }

    async function act(fn) {
        try {
            const message = await fn();
            await refresh();
            status(message || t('saved'), 'ok');
        } catch (err) {
            status(err.message || t('error'), 'error');
        }
    }

    // ── drawing ──────────────────────────────────────────────────

    function node(label, count, key, opts) {
        const li = document.createElement('li');
        li.className = 'folder-node';
        li.setAttribute('role', 'treeitem');
        li.dataset.folderKey = String(key);
        const isSel = String(selected) === String(key);
        li.setAttribute('aria-selected', isSel ? 'true' : 'false');

        const row = document.createElement('div');
        row.className = 'folder-row' + (isSel ? ' is-selected' : '');
        row.style.setProperty('--folder-depth', String(opts.depth || 0));
        row.dataset.folderDrop = String(key);

        const caret = document.createElement('button');
        caret.type = 'button';
        caret.className = 'folder-caret';
        if (opts.hasChildren) {
            const open = !collapsed.has(key);
            li.setAttribute('aria-expanded', open ? 'true' : 'false');
            caret.textContent = open ? '▾' : '▸';
            caret.setAttribute('aria-label', label);
            caret.dataset.folderToggle = String(key);
        } else {
            caret.disabled = true;
            caret.setAttribute('aria-hidden', 'true');
            caret.tabIndex = -1;
        }
        row.appendChild(caret);

        const name = document.createElement('button');
        name.type = 'button';
        name.className = 'folder-name';
        name.dataset.folderSelect = String(key);
        const icon = opts.icon || '📁';
        name.innerHTML = '<span class="folder-icon" aria-hidden="true"></span>'
            + '<span class="folder-label"></span>'
            + '<span class="folder-count"></span>';
        name.querySelector('.folder-icon').textContent = icon;
        name.querySelector('.folder-label').textContent = label;
        name.querySelector('.folder-count').textContent = String(count);
        if (opts.source === 'auto') name.title = label + ' — auto';
        if (editable && typeof key === 'number') {
            name.draggable = true;
            name.dataset.folderDragFolder = String(key);
        }
        row.appendChild(name);

        if (editable && typeof key === 'number') {
            const actions = document.createElement('span');
            actions.className = 'folder-actions';
            [['add', '+', t('add_sub')], ['rename', '✎', t('rename')],
             ['delete', '✕', t('delete')]].forEach(([op, glyph, title]) => {
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'folder-action folder-action-' + op;
                b.textContent = glyph;
                b.title = title;
                b.setAttribute('aria-label', title + ': ' + label);
                b.dataset.folderOp = op;
                b.dataset.folderId = String(key);
                actions.appendChild(b);
            });
            row.appendChild(actions);
        }
        li.appendChild(row);
        return li;
    }

    function drawFolders(parentEl, parentId, depth, totals) {
        childrenOf(parentId).forEach(f => {
            const kids = childrenOf(f.id);
            const li = node(f.name, totals.get(f.id) || 0, f.id, {
                depth: depth, hasChildren: kids.length > 0, source: f.source,
            });
            if (kids.length && !collapsed.has(f.id)) {
                const ul = document.createElement('ul');
                ul.setAttribute('role', 'group');
                drawFolders(ul, f.id, depth + 1, totals);
                li.appendChild(ul);
            }
            parentEl.appendChild(li);
        });
    }

    function render() {
        const c = counts();
        treeEl.textContent = '';
        treeEl.appendChild(node(t('all'), c.all, 'all', { icon: '🗂' }));
        drawFolders(treeEl, null, 0, c.total);
        treeEl.appendChild(node(t('unfiled'), c.unfiled, 'unfiled', { icon: '📄' }));
        drawMoveTargets();
        applyFilter();
        drawBadges();
    }

    function drawMoveTargets() {
        if (!moveSelect) return;
        const previous = moveSelect.value;
        moveSelect.textContent = '';
        const none = document.createElement('option');
        none.value = '';
        none.textContent = t('unfiled');
        moveSelect.appendChild(none);
        (function walk(parentId, depth) {
            childrenOf(parentId).forEach(f => {
                const opt = document.createElement('option');
                opt.value = String(f.id);
                opt.textContent = '  '.repeat(depth) + f.name;
                moveSelect.appendChild(opt);
                walk(f.id, depth + 1);
            });
        })(null, 0);
        if (previous && moveSelect.querySelector('option[value="' + previous + '"]')) {
            moveSelect.value = previous;
        } else if (typeof selected === 'number') {
            moveSelect.value = String(selected);
        }
    }

    function drawBadges() {
        document.querySelectorAll('[data-folder-badge]').forEach(el => {
            const fid = folderOf(el.getAttribute('data-folder-badge'));
            const path = fid !== null && byId().has(fid) ? pathOf(fid) : '';
            // A table cell is narrow: the leaf there, the path on hover.
            const shown = el.classList.contains('folder-badge-row') && path
                ? byId().get(fid).name : path;
            el.textContent = shown ? '📁 ' + shown : '';
            el.title = path ? t('in_folder') + ': ' + path : '';
            el.hidden = !path;
        });
    }

    // ── filtering ────────────────────────────────────────────────

    function visibleSet() {
        if (selected === 'all') return null;
        if (selected === 'unfiled') return 'unfiled';
        return subtreeIds(selected);
    }

    function applyFilter() {
        const want = visibleSet();
        const known = byId();
        items().forEach(el => {
            const fid = folderOf(el.getAttribute('data-folder-item'));
            let show = true;
            if (want === 'unfiled') show = fid === null || !known.has(fid);
            else if (want) show = fid !== null && want.has(fid);
            el.classList.toggle('folder-out', !show);
        });
        syncSections();
    }

    function shown(el) {
        return !el.hidden && !el.classList.contains('folder-out');
    }

    function syncSections() {
        // A section header stays only if one of the items after it — up to
        // the next header — is still on screen.
        document.querySelectorAll('[data-folder-section]').forEach(header => {
            let sib = header.nextElementSibling;
            let any = false;
            while (sib && !sib.hasAttribute('data-folder-section')) {
                if (sib.hasAttribute('data-folder-item') && shown(sib)) {
                    any = true;
                    break;
                }
                sib = sib.nextElementSibling;
            }
            header.classList.toggle('folder-out', !any);
        });
    }

    function select(key) {
        selected = key;
        try { window.localStorage.setItem(storeKey, String(key)); } catch (err) { /* ignore */ }
        render();
    }

    // ── moving items ─────────────────────────────────────────────

    function checkedIds() {
        return Array.from(document.querySelectorAll('.bulk-check:checked'))
            .filter(cb => !cb.dataset.bulkEntity || cb.dataset.bulkEntity === kind)
            .map(cb => cb.dataset.bulkId);
    }

    function assign(ids, folderId) {
        return act(async () => {
            const result = await call('POST', '/api/folders/assign', {
                kind: kind, ids: ids, folder_id: folderId,
            });
            return t('moved', { n: (result && result.changed || []).length });
        });
    }

    function syncMoveBox() {
        if (moveBox) moveBox.hidden = checkedIds().length === 0;
    }

    // ── events ───────────────────────────────────────────────────

    treeEl.addEventListener('click', ev => {
        const toggle = ev.target.closest('[data-folder-toggle]');
        if (toggle) {
            const raw = toggle.dataset.folderToggle;
            const key = /^\d+$/.test(raw) ? Number(raw) : raw;
            if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
            render();
            return;
        }
        const op = ev.target.closest('[data-folder-op]');
        if (op) {
            const id = Number(op.dataset.folderId);
            const folder = byId().get(id);
            if (!folder) return;
            if (op.dataset.folderOp === 'add') {
                const name = window.prompt(t('prompt_new'), '');
                if (name && name.trim()) {
                    collapsed.delete(id);
                    act(() => call('POST', '/api/folders', { name: name, parent_id: id })
                        .then(() => null));
                }
            } else if (op.dataset.folderOp === 'rename') {
                const name = window.prompt(t('prompt_rename'), folder.name);
                if (name && name.trim() && name.trim() !== folder.name) {
                    act(() => call('PATCH', '/api/folders/' + id, { name: name })
                        .then(() => null));
                }
            } else if (op.dataset.folderOp === 'delete') {
                const n = counts().total.get(id) || 0;
                if (window.confirm(t('confirm_delete', { name: folder.name, count: n }))) {
                    if (typeof selected === 'number' && subtreeIds(id).has(selected)) {
                        selected = 'all';
                    }
                    act(() => call('DELETE', '/api/folders/' + id).then(() => null));
                }
            }
            return;
        }
        const pick = ev.target.closest('[data-folder-select]');
        if (pick) {
            const raw = pick.dataset.folderSelect;
            select(/^\d+$/.test(raw) ? Number(raw) : raw);
        }
    });

    const newBtn = panel.querySelector('[data-folder-new]');
    if (newBtn) {
        newBtn.addEventListener('click', () => {
            const name = window.prompt(t('prompt_new'), '');
            if (!name || !name.trim()) return;
            const parent = typeof selected === 'number' ? selected : null;
            if (parent !== null) collapsed.delete(parent);
            act(() => call('POST', '/api/folders', { name: name, parent_id: parent })
                .then(() => null));
        });
    }

    const moveApply = panel.querySelector('[data-folder-move-apply]');
    if (moveApply) {
        moveApply.addEventListener('click', () => {
            const ids = checkedIds();
            if (!ids.length) return;
            const target = moveSelect.value ? Number(moveSelect.value) : null;
            assign(ids, target);
        });
    }

    document.addEventListener('change', ev => {
        if (ev.target.matches && ev.target.matches('.bulk-check, [data-bulk-select-all]')) {
            // select-all toggles the boxes after its own change event.
            window.setTimeout(syncMoveBox, 0);
        }
    });

    // The category / suite chips hide items with the hidden attribute; the
    // section headers have to follow them as well as the folder filter.
    document.addEventListener('click', ev => {
        if (ev.target.closest && ev.target.closest('.filter-btn, .filter-btn-suite')) {
            window.setTimeout(syncSections, 0);
        }
    });

    // Drag and drop: an item handle or a folder name onto a folder row.
    document.addEventListener('dragstart', ev => {
        const handle = ev.target.closest && ev.target.closest('[data-folder-drag]');
        if (handle) {
            const id = handle.dataset.folderDrag;
            const ids = checkedIds();
            const payload = ids.includes(id) ? ids : [id];
            ev.dataTransfer.setData(ITEM_MIME, JSON.stringify(payload));
            ev.dataTransfer.effectAllowed = 'move';
            return;
        }
        const folder = ev.target.closest && ev.target.closest('[data-folder-drag-folder]');
        if (folder) {
            ev.dataTransfer.setData(FOLDER_MIME, folder.dataset.folderDragFolder);
            ev.dataTransfer.effectAllowed = 'move';
        }
    });

    function dropKey(ev) {
        const row = ev.target.closest && ev.target.closest('[data-folder-drop]');
        if (!row) return undefined;
        const raw = row.dataset.folderDrop;
        if (raw === 'all' || raw === 'unfiled') return null;
        return Number(raw);
    }

    treeEl.addEventListener('dragover', ev => {
        if (!editable) return;
        const types = Array.from(ev.dataTransfer.types || []);
        if (dropKey(ev) === undefined) return;
        if (!types.includes(ITEM_MIME) && !types.includes(FOLDER_MIME)) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = 'move';
        treeEl.querySelectorAll('.is-drop').forEach(el => el.classList.remove('is-drop'));
        ev.target.closest('[data-folder-drop]').classList.add('is-drop');
    });

    treeEl.addEventListener('dragleave', ev => {
        const row = ev.target.closest && ev.target.closest('[data-folder-drop]');
        if (row && !row.contains(ev.relatedTarget)) row.classList.remove('is-drop');
    });

    treeEl.addEventListener('drop', ev => {
        const target = dropKey(ev);
        treeEl.querySelectorAll('.is-drop').forEach(el => el.classList.remove('is-drop'));
        if (target === undefined || !editable) return;
        ev.preventDefault();
        const itemsRaw = ev.dataTransfer.getData(ITEM_MIME);
        if (itemsRaw) {
            assign(JSON.parse(itemsRaw), target);
            return;
        }
        const folderRaw = ev.dataTransfer.getData(FOLDER_MIME);
        if (folderRaw) {
            const id = Number(folderRaw);
            if (id === target) return;
            act(() => call('PATCH', '/api/folders/' + id, { parent_id: target })
                .then(() => null));
        }
    });

    // Re-file every item of this page's kind by its "[Folder -> Sub]"
    // summary prefix (routes/folders.py api_folders_refile).
    const refileBtn = panel.querySelector('[data-folder-refile]');
    if (refileBtn) {
        refileBtn.addEventListener('click', () => {
            if (!window.confirm(t('refile_confirm'))) return;
            act(async () => {
                const result = await call('POST', '/api/folders/refile', { kind: kind });
                return t('refiled', {
                    n: (result && result.changed || []).length,
                    skipped: (result && result.no_prefix) || 0,
                });
            });
        });
    }

    // An item created by hand is filed into the folder being viewed, so it
    // does not vanish from a filtered page on the reload that follows. The
    // editors call this before they reload.
    window.TestFortgeFolders = {
        refresh: function () {
            return refresh().catch(() => undefined);
        },
        fileNew: function (itemId) {
            if (!editable || typeof selected !== 'number' || !itemId) {
                return Promise.resolve();
            }
            return call('POST', '/api/folders/assign', {
                kind: kind, ids: [itemId], folder_id: selected,
            }).catch(() => undefined);
        },
    };

    if (typeof selected === 'string' && /^\d+$/.test(selected)) {
        selected = Number(selected);
    }
    if (typeof selected === 'number' && !byId().has(selected)) selected = 'all';
    // A generated tree can hold dozens of pages: open on the top level and
    // the menus under it, pages folded away — except the path down to the
    // folder that was selected last time.
    const open = new Set();
    if (typeof selected === 'number') {
        let cursor = byId().get(selected);
        while (cursor) { open.add(cursor.id); cursor = byId().get(cursor.parent_id); }
    }
    data.folders.forEach(f => {
        if (f.parent_id !== null && childrenOf(f.id).length && !open.has(f.id)) {
            collapsed.add(f.id);
        }
    });
    render();
})();

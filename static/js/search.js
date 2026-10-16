/* ═══════════════════════════════════════════════════════════════
   TestForTge — platform search UI (ADR 0003 §1.3, task T7)

   Loaded by base.html only while PLATFORM_SEARCH is on. Four parts:

   1. Typeahead — the header field is an ARIA 1.2 combobox over a grouped
      listbox. GET /api/search?quick=1 (titles + ids), debounced 150 ms,
      stale requests aborted. "/" focuses it from anywhere that is not a
      text field; arrows move, Enter opens, Esc closes.
   2. Results view — GET /api/search (full, explained). Corrections,
      translations, understood conditions as removable chips, relaxed
      filters, counts + timing, refinement chips and the "How this was
      found" panel.
   3. AI (only when #tf-search[data-ai="1"], i.e. PLATFORM_SEARCH_AI): the
      answer block above the results and the Ask side panel, both reading
      the NDJSON stream of POST /api/ask through fetch + ReadableStream,
      stoppable with an AbortController.
   4. Contextual hints — [data-hints] mounts fed by GET /api/hints.

   No framework, no innerHTML with server text except the search
   snippets, which the server escapes and marks up with <mark> only.
   ═══════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var root = document.getElementById('tf-search');
    var I18N = {};
    try {
        var blob = document.getElementById('tf-search-i18n');
        if (blob) I18N = JSON.parse(blob.textContent || '{}');
    } catch (e) { I18N = {}; }

    function tr(key, vars) {
        var s = I18N[key] || key;
        if (vars) {
            Object.keys(vars).forEach(function (k) {
                s = s.split('{' + k + '}').join(String(vars[k]));
            });
        }
        return s;
    }

    function el(tag, attrs, children) {
        var n = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                var v = attrs[k];
                if (v === null || v === undefined || v === false) return;
                if (k === 'text') n.textContent = v;
                else if (k === 'class') n.className = v;
                else if (k === 'html') n.innerHTML = v;  // only server-escaped snippets
                else n.setAttribute(k, v === true ? '' : v);
            });
        }
        (children || []).forEach(function (c) {
            if (c === null || c === undefined) return;
            n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
        });
        return n;
    }

    function csrfToken() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.getAttribute('content') : '';
    }

    /* Index titles carry their ref ("TC-007 · Password reset…"); the ref
       is shown on its own, so it is not repeated in the title. */
    function cleanTitle(ref, title) {
        title = title || '';
        if (ref && title.indexOf(ref) === 0) {
            return title.slice(ref.length).replace(/^\s*[·:\-–—]\s*/, '');
        }
        return title;
    }

    function groupLabel(kind) {
        return I18N['group_' + kind] || kind;
    }

    function fmtMs(ms) {
        var n = Number(ms) || 0;
        return n < 10 ? n.toFixed(1) : String(Math.round(n));
    }

    function isTextField(t) {
        if (!t) return false;
        var tag = (t.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' ||
            t.isContentEditable;
    }

    /* ── 4. Contextual hints (independent of the header field) ───── */

    // The tips sit in a toggletip beside the page title (owner ruling
    // 2026-10-16); app.js opens it and fires tf-tip-open, and the first
    // open fetches. A failed fetch is retried on the next open.
    function initHints(mount) {
        var page = mount.getAttribute('data-hints-page') || '';
        var list = mount.querySelector('[data-hints-list]');
        var loaded = false;

        function status(text) {
            list.textContent = '';
            list.appendChild(el('span', { class: 'tf-hints-status', role: 'listitem', text: text }));
            resized();
        }

        function resized() {
            mount.dispatchEvent(new CustomEvent('tf-tip-resized', { bubbles: true }));
        }

        function load() {
            if (loaded) return;
            loaded = true;
            status(mount.getAttribute('data-msg-loading'));
            var url = mount.getAttribute('data-hints-url') + '?page=' +
                encodeURIComponent(page) + '&lang=' +
                encodeURIComponent(mount.getAttribute('data-lang') || 'en');
            fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
                .then(function (body) { renderHints(body.hints || []); })
                .catch(function () {
                    loaded = false;
                    status(mount.getAttribute('data-msg-error'));
                });
        }

        function renderHints(hints) {
            list.textContent = '';
            var learn = mount.getAttribute('data-msg-learn-more');
            hints.forEach(function (h) {
                var li;
                if (h.type === 'tip') {
                    li = el('span', { class: 'tf-hint tf-hint-tip', role: 'listitem' }, [
                        el('span', { text: h.text }),
                        h.learn_more && h.learn_more.url ? el('a', {
                            class: 'tf-hint-link', href: h.learn_more.url,
                            'aria-label': learn + ': ' + (h.learn_more.title || '')
                        }, [learn]) : null
                    ]);
                } else if (h.type === 'guide') {
                    li = el('span', { class: 'tf-hint tf-hint-guide', role: 'listitem' }, [
                        el('strong', { text: h.topic }),
                        el('span', { text: ' ' + (h.excerpt || '') + ' ' }),
                        el('a', { class: 'tf-hint-link', href: h.url,
                                  'aria-label': learn + ': ' + h.topic }, [learn])
                    ]);
                } else if (h.ref) {
                    li = el('span', { class: 'tf-hint tf-hint-similar', role: 'listitem' }, [
                        el('span', { class: 'tf-chip tf-chip-reason', text: h.label || mount.getAttribute('data-msg-similar') }),
                        ' ',
                        el('a', { href: h.url || '#' }, [h.ref + ' · ' + cleanTitle(h.ref, h.title)])
                    ]);
                }
                if (li) list.appendChild(li);
            });
            resized();
        }

        mount.addEventListener('tf-tip-open', load);
    }

    Array.prototype.forEach.call(document.querySelectorAll('[data-hints]'), initHints);

    if (!root) return;

    /* ── Shared state ─────────────────────────────────────────────── */

    var SEARCH_URL = root.getAttribute('data-search-url');
    var ASK_URL = root.getAttribute('data-ask-url');
    var AI = root.getAttribute('data-ai') === '1';
    var LANG = root.getAttribute('data-lang') || 'en';
    var PROJECT = root.getAttribute('data-project-id') || '';

    var input = document.getElementById('tf-search-input');
    var dropdown = document.getElementById('tf-search-dropdown');
    var listbox = document.getElementById('tf-search-listbox');
    var ddStatus = document.getElementById('tf-search-dropdown-status');
    var results = document.getElementById('tf-results');
    var main = document.getElementById('main-content');

    var refUrls = {};      // ref → url, from everything shown so far

    /* ── 1. Typeahead ─────────────────────────────────────────────── */

    var DEBOUNCE_MS = 150;
    var quickTimer = null;
    var quickCtl = null;
    var options = [];      // [{el, action}]
    var active = -1;

    function setExpanded(open) {
        dropdown.hidden = !open;
        input.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (!open) setActive(-1);
    }

    function setActive(i) {
        if (active >= 0 && options[active]) {
            options[active].el.classList.remove('is-active');
            options[active].el.setAttribute('aria-selected', 'false');
        }
        active = i;
        if (i >= 0 && options[i]) {
            var o = options[i].el;
            o.classList.add('is-active');
            o.setAttribute('aria-selected', 'true');
            input.setAttribute('aria-activedescendant', o.id);
            if (o.scrollIntoView) o.scrollIntoView({ block: 'nearest' });
        } else {
            input.removeAttribute('aria-activedescendant');
        }
    }

    var optSeq = 0;
    function addOption(container, content, action, extraClass) {
        var id = 'tf-opt-' + (++optSeq);
        var o = el('div', { id: id, role: 'option', 'aria-selected': 'false',
                            class: 'tf-opt' + (extraClass ? ' ' + extraClass : '') }, content);
        var idx = options.length;
        o.addEventListener('mousedown', function (e) { e.preventDefault(); });
        o.addEventListener('click', function () { action(); });
        o.addEventListener('mousemove', function () { if (active !== idx) setActive(idx); });
        options.push({ el: o, action: action });
        container.appendChild(o);
        return o;
    }

    function go(url) {
        if (url) window.location.assign(url);
    }

    function quickReason(item, body) {
        var idq = body.query && body.query.id;
        if (idq && item.ref && item.ref.toUpperCase().replace(/[^A-Z0-9]/g, '') ===
                idq.toUpperCase().replace(/[^A-Z0-9]/g, '')) {
            return tr('reason_id');
        }
        return tr('reason_title');
    }

    function renderDropdown(q, body) {
        listbox.textContent = '';
        options = [];
        active = -1;
        var order = body.group_order || Object.keys(body.groups || {});
        var any = false;
        order.forEach(function (kind) {
            var items = (body.groups || {})[kind] || [];
            if (!items.length) return;
            any = true;
            var gid = 'tf-grp-' + kind;
            var group = el('div', { role: 'group', 'aria-labelledby': gid, class: 'tf-opt-group' }, [
                el('div', { id: gid, class: 'tf-opt-group-label', role: 'presentation', text: groupLabel(kind) })
            ]);
            items.forEach(function (item) {
                if (item.ref && item.url) refUrls[item.ref] = item.url;
                var meta = [];
                if (item.project && item.project.name) meta.push(item.project.name);
                addOption(group, [
                    el('span', { class: 'tf-opt-ref', text: item.ref || '' }),
                    el('span', { class: 'tf-opt-title', text: cleanTitle(item.ref, item.title) }),
                    el('span', { class: 'tf-chip tf-chip-reason', text: quickReason(item, body) }),
                    meta.length ? el('span', { class: 'tf-opt-meta', text: meta.join(' · ') }) : null
                ], function () { go(item.url); });
            });
            listbox.appendChild(group);
        });
        // Refinements: search only within one of the kinds offered.
        if (order.length > 1) {
            var ref = el('div', { role: 'group', 'aria-labelledby': 'tf-grp-refine', class: 'tf-opt-group' }, [
                el('div', { id: 'tf-grp-refine', class: 'tf-opt-group-label', role: 'presentation', text: tr('refine') })
            ]);
            order.slice(0, 3).forEach(function (kind) {
                addOption(ref, [
                    el('i', { 'data-lucide': 'filter', class: 'icon-sm', 'aria-hidden': 'true' }),
                    el('span', { text: tr('only_in', { group: groupLabel(kind) }) })
                ], function () { openResults({ q: q, kinds: [kind] }); }, 'tf-opt-action');
            });
            listbox.appendChild(ref);
        }
        var actions = el('div', { role: 'group', 'aria-label': tr('see_all', { q: q }), class: 'tf-opt-group tf-opt-actions' });
        addOption(actions, [
            el('i', { 'data-lucide': 'search', class: 'icon-sm', 'aria-hidden': 'true' }),
            el('span', { text: tr('see_all', { q: q }) })
        ], function () { openResults({ q: q }); }, 'tf-opt-action');
        if (AI) {
            addOption(actions, [
                el('i', { 'data-lucide': 'sparkles', class: 'icon-sm', 'aria-hidden': 'true' }),
                el('span', { text: tr('ask_about', { q: q }) })
            ], function () { closeDropdown(); askPanel.open(q); }, 'tf-opt-action tf-opt-ask');
        }
        listbox.appendChild(actions);
        ddStatus.textContent = any ? '' : tr('no_matches');
        hydrateIcons(listbox);
        setExpanded(true);
    }

    function hydrateIcons(scope) {
        if (window.lucide && typeof window.lucide.createIcons === 'function') {
            try { window.lucide.createIcons({ root: scope }); } catch (e) { /* cosmetic */ }
        }
    }

    function runQuick() {
        var q = input.value.trim();
        if (quickCtl) quickCtl.abort();
        if (!q) { closeDropdown(); return; }
        quickCtl = new AbortController();
        var url = SEARCH_URL + '?quick=1&q=' + encodeURIComponent(q);
        ddStatus.textContent = tr('loading');
        fetch(url, { credentials: 'same-origin', signal: quickCtl.signal,
                     headers: { 'Accept': 'application/json' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (body) {
                if (input.value.trim() !== q) return;   // overtaken
                renderDropdown(q, body);
            })
            .catch(function (e) {
                if (e && e.name === 'AbortError') return;
                listbox.textContent = '';
                options = [];
                ddStatus.textContent = tr('error');
                setExpanded(true);
            });
    }

    function closeDropdown() {
        if (quickCtl) quickCtl.abort();
        clearTimeout(quickTimer);
        setExpanded(false);
    }

    input.addEventListener('input', function () {
        clearTimeout(quickTimer);
        quickTimer = setTimeout(runQuick, DEBOUNCE_MS);
    });
    input.addEventListener('focus', function () {
        if (input.value.trim() && options.length) setExpanded(true);
    });
    input.addEventListener('blur', function () {
        // Let a click on an option land first (mousedown is prevented).
        setTimeout(function () {
            if (document.activeElement !== input) setExpanded(false);
        }, 120);
    });
    input.addEventListener('keydown', function (e) {
        var open = !dropdown.hidden;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (!open) { if (input.value.trim()) runQuick(); return; }
            setActive(options.length ? (active + 1) % options.length : -1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (open && options.length) setActive(active <= 0 ? options.length - 1 : active - 1);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (open && active >= 0 && options[active]) { options[active].action(); return; }
            var q = input.value.trim();
            if (q) { closeDropdown(); openResults({ q: q }); }
        } else if (e.key === 'Escape') {
            if (open) { e.preventDefault(); closeDropdown(); }
            else if (!results.hidden) { e.preventDefault(); closeResults(); }
            else if (input.value) { e.preventDefault(); input.value = ''; }
        } else if (e.key === 'Home' && open && e.ctrlKey) {
            e.preventDefault(); setActive(0);
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isTextField(e.target)) {
            e.preventDefault();
            input.focus();
            input.select();
        }
    });

    /* ── 2. Results view ──────────────────────────────────────────── */

    var current = null;      // {q, kinds, filters, project_id, linked}
    var fullCtl = null;
    var lastBody = null;
    var resultsMeta = document.getElementById('tf-results-meta');
    var resultsNotes = document.getElementById('tf-results-notes');
    var resultsHow = document.getElementById('tf-results-how');
    var resultsList = document.getElementById('tf-results-list');
    var resultsBook = document.getElementById('tf-results-book');

    function searchUrl(p) {
        var qs = ['q=' + encodeURIComponent(p.q), 'explain=1'];
        if (p.kinds && p.kinds.length) qs.push('kinds=' + encodeURIComponent(p.kinds.join(',')));
        Object.keys(p.filters || {}).forEach(function (k) {
            qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(p.filters[k]));
        });
        if (p.project_id) { qs.push('project_id=' + encodeURIComponent(p.project_id)); qs.push('scope=org'); }
        if (p.linked) qs.push('linked=' + encodeURIComponent(p.linked));
        return SEARCH_URL + '?' + qs.join('&');
    }

    function showResultsView(show) {
        results.hidden = !show;
        if (main) main.classList.toggle('is-searching', show);
    }

    function openResults(p) {
        current = { q: p.q, kinds: p.kinds || null, filters: p.filters || {},
                    project_id: p.project_id || null, linked: p.linked || null };
        input.value = p.q;
        closeDropdown();
        showResultsView(true);
        resultsMeta.textContent = tr('loading');
        resultsNotes.textContent = '';
        resultsList.textContent = '';
        resultsBook.textContent = '';
        resultsHow.hidden = true;
        aiBlock.reset();
        if (fullCtl) fullCtl.abort();
        fullCtl = new AbortController();
        var title = document.getElementById('tf-results-title');
        fetch(searchUrl(current), { credentials: 'same-origin', signal: fullCtl.signal,
                                    headers: { 'Accept': 'application/json' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function (body) {
                lastBody = body;
                renderResults(body);
                if (title) title.focus();
            })
            .catch(function (e) {
                if (e && e.name === 'AbortError') return;
                resultsMeta.textContent = tr('error');
            });
    }

    function closeResults() {
        if (fullCtl) fullCtl.abort();
        aiBlock.reset();
        showResultsView(false);
        input.focus();
    }

    results.querySelector('[data-results-close]').addEventListener('click', closeResults);
    results.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (!resultsHow.hidden) { e.preventDefault(); toggleHow(false); return; }
        e.preventDefault();
        closeResults();
    });

    function reasonChips(r) {
        var w = r.why || {};
        var chips = [];
        var labels = w.matched_terms || [];
        if ((w.boosts || []).indexOf('id_exact') >= 0) chips.push(tr('reason_id'));
        Object.keys(w.typo || {}).forEach(function (k) {
            chips.push(tr('reason_typo') + ': ' + k + ' → ' + w.typo[k]);
        });
        labels.forEach(function (l) {
            if (l.indexOf('→') > 0 && !(w.typo && w.typo[l.split('→')[0]])) {
                chips.push(tr('reason_translated') + ': ' + l);
            }
        });
        if (w.lex_rank) chips.push(tr('reason_keyword'));
        if (w.vec_rank && (w.concepts || []).length) {
            chips.push(tr('reason_meaning') + ': ' + w.concepts.slice(0, 2).join(', '));
        } else if (w.vec_rank && !w.lex_rank) {
            chips.push(tr('reason_meaning'));
        }
        var seen = {};
        return chips.filter(function (c) { if (seen[c]) return false; seen[c] = 1; return true; }).slice(0, 4);
    }

    function conditionChips(body) {
        var out = [];
        var req = (body.filters && body.filters.requested) || {};
        if (req.kinds) {
            out.push({ label: req.kinds.map(groupLabel).join(', '),
                       drop: function (p) { p.kinds = null; } });
        }
        ['status', 'priority', 'severity'].forEach(function (f) {
            if (req[f]) out.push({ label: f + ': ' + req[f],
                                   drop: function (p) { delete p.filters[f]; } });
        });
        if (req.project) {
            var name = req.project;
            (body.results || []).some(function (r) {
                if (r.project && r.project.id === req.project && r.project.name) { name = r.project.name; return true; }
                return false;
            });
            out.push({ label: name, drop: function (p) { p.project_id = null; } });
        }
        if (req.linked) out.push({ label: req.linked, drop: function (p) { p.linked = null; } });
        return out;
    }

    function rerun(mutate) {
        var p = JSON.parse(JSON.stringify(current));
        mutate(p);
        openResults(p);
    }

    function renderResults(body) {
        var q = body.query || {};
        resultsNotes.textContent = '';
        // Count + timing + the "How this was found" toggle.
        resultsMeta.textContent = '';
        resultsMeta.appendChild(el('span', { class: 'tf-results-count',
            text: tr('count', { n: body.total != null ? body.total : (body.results || []).length,
                                ms: fmtMs(body.took_ms) }) }));
        if (body.explain) {
            var howBtn = el('button', { type: 'button', class: 'btn btn-ghost btn-xs tf-how-toggle',
                                        'aria-expanded': 'false', 'aria-controls': 'tf-results-how' },
                            [tr('how_found')]);
            howBtn.addEventListener('click', function () { toggleHow(resultsHow.hidden); });
            resultsMeta.appendChild(howBtn);
        }
        // Corrected / translated lines.
        var corr = q.corrections || {};
        if (Object.keys(corr).length) {
            resultsNotes.appendChild(el('p', { class: 'tf-note tf-note-corrected' }, [
                el('strong', { text: tr('corrected') + ' ' }),
                Object.keys(corr).map(function (k) { return k + ' → ' + corr[k]; }).join(', ')
            ]));
        }
        var tl = q.translated || {};
        if (Object.keys(tl).length) {
            resultsNotes.appendChild(el('p', { class: 'tf-note tf-note-translated' }, [
                el('strong', { text: tr('translated') + ' ' }),
                Object.keys(tl).map(function (k) { return k + ' → ' + tl[k]; }).join(', ')
            ]));
        }
        // Understood conditions, each removable.
        var conds = conditionChips(body);
        if (conds.length) {
            var row = el('div', { class: 'tf-chip-row', role: 'group', 'aria-label': tr('conditions') }, [
                el('span', { class: 'tf-chip-row-label', text: tr('conditions') })
            ]);
            conds.forEach(function (c) {
                var b = el('button', { type: 'button', class: 'tf-chip tf-chip-cond',
                                       'aria-label': tr('remove_condition', { c: c.label }) },
                           [c.label, el('span', { 'aria-hidden': 'true', class: 'tf-chip-x', text: '×' })]);
                b.addEventListener('click', function () { rerun(c.drop); });
                row.appendChild(b);
            });
            resultsNotes.appendChild(row);
        }
        // Relaxed filters and a stale index.
        ((body.filters && body.filters.relaxed) || []).forEach(function (r) {
            var v = Array.isArray(r.value) ? r.value.map(groupLabel).join(', ') : r.value;
            resultsNotes.appendChild(el('p', { class: 'tf-note tf-note-relaxed', role: 'note',
                text: tr('relaxed', { filter: r.filter, value: v }) }));
        });
        if (body.stale) {
            resultsNotes.appendChild(el('p', { class: 'tf-note tf-note-stale', role: 'note', text: tr('stale') }));
        }
        // Refinement chips (deterministic, from the facets).
        var chips = body.chips || [];
        if (chips.length) {
            var crow = el('div', { class: 'tf-chip-row', role: 'group', 'aria-label': tr('refine') }, [
                el('span', { class: 'tf-chip-row-label', text: tr('refine') })
            ]);
            chips.forEach(function (c) {
                var b = el('button', { type: 'button', class: 'tf-chip tf-chip-refine', text: c.label });
                b.addEventListener('click', function () {
                    rerun(function (p) {
                        p.q = c.q || p.q;
                        if (c.kinds) p.kinds = c.kinds;
                        if (c.filters) Object.keys(c.filters).forEach(function (k) { p.filters[k] = c.filters[k]; });
                        if (c.project_id) p.project_id = c.project_id;
                        if (c.linked) p.linked = c.linked;
                    });
                });
                crow.appendChild(b);
            });
            resultsNotes.appendChild(crow);
        }
        // The hits.
        resultsList.textContent = '';
        var res = body.results || [];
        res.forEach(function (r) {
            if (r.ref && r.url) refUrls[r.ref] = r.url;
            var reasons = reasonChips(r);
            var facets = [];
            Object.keys(r.facets || {}).forEach(function (k) {
                var v = r.facets[k];
                if (v && typeof v !== 'object' && ['status', 'priority', 'severity'].indexOf(k) >= 0) facets.push(String(v));
            });
            var li = el('li', { class: 'tf-hit' }, [
                el('div', { class: 'tf-hit-head' }, [
                    el('span', { class: 'tf-kind tf-kind-' + r.kind, text: groupLabel(r.kind) }),
                    el('a', { class: 'tf-hit-title', href: r.url || '#' }, [
                        r.ref ? el('span', { class: 'tf-hit-ref', text: r.ref }) : null,
                        cleanTitle(r.ref, r.title)
                    ])
                ]),
                r.snippet ? el('p', { class: 'tf-hit-snippet', html: r.snippet }) : null,
                el('div', { class: 'tf-hit-foot' }, [
                    r.project && r.project.name ? el('span', { class: 'tf-hit-project', text: r.project.name }) : null
                ].concat(facets.map(function (f) { return el('span', { class: 'tf-chip tf-chip-facet', text: f }); }))
                 .concat(reasons.map(function (c) { return el('span', { class: 'tf-chip tf-chip-reason', text: c }); })))
            ]);
            resultsList.appendChild(li);
        });
        if (!res.length) {
            resultsList.appendChild(el('li', { class: 'tf-results-empty', text: tr('empty') }));
        }
        // The ISTQB textbook group.
        resultsBook.textContent = '';
        var book = (body.groups || {}).istqb_book || [];
        if (book.length) {
            resultsBook.appendChild(el('h3', { class: 'tf-book-title', text: groupLabel('istqb_book') }));
            var ul = el('ul', { class: 'tf-book-list' });
            book.forEach(function (b) {
                ul.appendChild(el('li', {}, [
                    el('span', { class: 'tf-chip tf-chip-facet', text: (b.source || '') + (b.page ? ' · ' + tr('istqb_page', { p: b.page }) : '') }),
                    ' ',
                    el('span', { text: b.excerpt || '' })
                ]));
            });
            resultsBook.appendChild(ul);
        }
        renderHow(body);
        aiBlock.offer(body);
    }

    /* "How this was found" — terms, engines, per-hit scores, filters,
       timings. A disclosure, not a modal: Esc or the toggle closes it. */
    function toggleHow(open) {
        resultsHow.hidden = !open;
        var btn = resultsMeta.querySelector('.tf-how-toggle');
        if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) {
            var h = resultsHow.querySelector('h3');
            if (h) h.focus();
        } else if (btn) {
            btn.focus();
        }
    }

    function fmtRank(rank, score) {
        if (!rank) return '—';
        return '#' + rank + (score != null ? ' · ' + Number(score).toFixed(3) : '');
    }

    function renderHow(body) {
        resultsHow.textContent = '';
        var ex = body.explain;
        if (!ex) return;
        resultsHow.appendChild(el('h3', { class: 'tf-how-title', tabindex: '-1', text: tr('how_found') }));
        var dl = el('dl', { class: 'tf-how-dl' });
        function row(term, value) {
            dl.appendChild(el('dt', { text: term }));
            dl.appendChild(el('dd', {}, [value]));
        }
        var terms = (ex.terms || []).map(function (t) { return t.label + (t.source !== 'query' ? ' (' + t.source + ')' : ''); });
        row(tr('how_terms'), terms.join(', ') || '—');
        row(tr('how_engine'), [ex.mode, ex.lexical, ex.vector].filter(Boolean).join(' · ') +
            (ex.weights ? '  (w_lex ' + ex.weights.lex + ', w_vec ' + ex.weights.vec + ', k ' + ex.weights.rrf_k + ')' : ''));
        var f = ex.filters || {};
        var fText = tr('how_before_after', { before: f.before != null ? f.before : '—', after: f.after != null ? f.after : '—' });
        var applied = Object.keys(f.applied || {}).map(function (k) { return k + '=' + f.applied[k]; });
        if (applied.length) fText += ' · ' + applied.join(', ');
        (f.relaxed || []).forEach(function (r) { fText += ' · ' + r.filter + ' ✕'; });
        row(tr('how_filters'), fText);
        var tm = ex.timings_ms || {};
        row(tr('how_timings'), Object.keys(tm).map(function (k) { return k + ' ' + fmtMs(tm[k]) + ' ms'; }).join(' · '));
        resultsHow.appendChild(dl);

        var hits = (body.results || []).filter(function (r) { return r.why; }).slice(0, 10);
        if (hits.length) {
            resultsHow.appendChild(el('h4', { class: 'tf-how-sub', text: tr('how_scores') }));
            var wrap = el('div', { class: 'tf-how-table-wrap' });
            var table = el('table', { class: 'tf-how-table' });
            table.appendChild(el('thead', {}, [el('tr', {}, [
                el('th', { scope: 'col', text: tr('how_col_item') }),
                el('th', { scope: 'col', text: tr('how_col_lex') }),
                el('th', { scope: 'col', text: tr('how_col_vec') }),
                el('th', { scope: 'col', text: tr('how_col_fused') })
            ])]));
            var tb = el('tbody');
            hits.forEach(function (r) {
                var w = r.why;
                tb.appendChild(el('tr', {}, [
                    el('th', { scope: 'row', text: r.ref || r.title }),
                    el('td', { text: fmtRank(w.lex_rank, w.lex_score) }),
                    el('td', { text: fmtRank(w.vec_rank, w.vec_score) }),
                    el('td', { text: Number(r.score).toFixed(4) + ((w.boosts || []).length ? ' (+' + w.boosts.join(', ') + ')' : '') })
                ]));
            });
            table.appendChild(tb);
            wrap.appendChild(table);
            resultsHow.appendChild(wrap);
        }
    }

    /* ── 3. NDJSON reader + AI ────────────────────────────────────── */

    /* POST *body* to /api/ask and call onEvent(obj) per NDJSON line.
       Resolves when the stream ends; rejects with {status} on an HTTP
       error, or the AbortError when stopped. */
    function streamAsk(payload, signal, onEvent) {
        return fetch(ASK_URL, {
            method: 'POST',
            credentials: 'same-origin',
            signal: signal,
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/x-ndjson',
                       'X-CSRFToken': csrfToken() },
            body: JSON.stringify(payload)
        }).then(function (resp) {
            if (!resp.ok) {
                var err = new Error('HTTP ' + resp.status);
                err.status = resp.status;
                throw err;
            }
            var decoder = new TextDecoder('utf-8');
            var buf = '';
            function handle(line) {
                line = line.trim();
                if (!line) return;
                var obj;
                try { obj = JSON.parse(line); } catch (e) { return; }
                onEvent(obj);
            }
            if (!resp.body || !resp.body.getReader) {
                return resp.text().then(function (t) { t.split('\n').forEach(handle); });
            }
            var reader = resp.body.getReader();
            function pump() {
                return reader.read().then(function (chunk) {
                    if (chunk.done) {
                        buf += decoder.decode();
                        buf.split('\n').forEach(handle);
                        return;
                    }
                    buf += decoder.decode(chunk.value, { stream: true });
                    var nl;
                    while ((nl = buf.indexOf('\n')) >= 0) {
                        handle(buf.slice(0, nl));
                        buf = buf.slice(nl + 1);
                    }
                    return pump();
                });
            }
            return pump();
        });
    }

    function itemRef(it) { return typeof it === 'string' ? it : (it && it.ref) || ''; }

    /* Render answer text: escaped, with [[REF]] as links to the item. */
    function renderAnswer(target, text, items) {
        (items || []).forEach(function (it) {
            if (it && typeof it === 'object' && it.ref && it.url) refUrls[it.ref] = it.url;
        });
        target.textContent = '';
        var re = /\[\[([A-Za-z]+-?[0-9]+|[^\[\]\s]{1,40})\]\]/g;
        var last = 0, m;
        while ((m = re.exec(text)) !== null) {
            if (m.index > last) target.appendChild(document.createTextNode(text.slice(last, m.index)));
            var ref = m[1];
            var url = refUrls[ref];
            var a = el('a', { class: 'tf-cite', href: url || (SEARCH_URL ? '#' : '#'), 'data-ref': ref, text: ref });
            if (!url) {
                a.addEventListener('click', function (r) {
                    return function (e) { e.preventDefault(); openResults({ q: r }); };
                }(ref));
            }
            target.appendChild(a);
            last = re.lastIndex;
        }
        if (last < text.length) target.appendChild(document.createTextNode(text.slice(last)));
    }

    function errorText(code, status) {
        var map = { rate_limited: 'err_rate_limited', cap: 'err_rate_limited', budget: 'err_budget',
                    no_key: 'err_no_key', llm_unavailable: 'err_no_key',
                    not_grounded: 'err_not_grounded', ungrounded: 'err_not_grounded',
                    upstream: 'err_upstream' };
        if (!code && status === 429) code = 'rate_limited';
        return tr(map[code] || 'err_generic');
    }

    function looksLikeQuestion(q) {
        var s = (q || '').trim().toLowerCase();
        if (!s) return false;
        if (/\?$/.test(s)) return true;
        if (/^(how|what|why|when|which|where|who|is|are|does|can|як|що|чому|коли|який|яка|які|де|как|что|почему)\b/.test(s)) return true;
        return s.split(/\s+/).length >= 4;
    }

    var aiBlock = (function () {
        var box = document.getElementById('tf-ai');
        if (!AI || !box) return { reset: function () {}, offer: function () {} };
        var text = document.getElementById('tf-ai-text');
        var getBtn = box.querySelector('[data-ai-get]');
        var cont = box.querySelector('[data-ai-continue]');
        var ctl = null;
        var timer = null;
        var lastQ = '';
        var lastAnswer = '';

        function reset() {
            if (ctl) ctl.abort();
            clearTimeout(timer);
            box.hidden = true;
            text.textContent = '';
            text.setAttribute('aria-busy', 'false');
            getBtn.hidden = true;
            lastAnswer = '';
        }

        function run(q) {
            if (ctl) ctl.abort();
            ctl = new AbortController();
            getBtn.hidden = true;
            text.textContent = '';
            text.setAttribute('aria-busy', 'true');
            box.hidden = false;
            var acc = '';
            var items = [];
            var failed = false;
            streamAsk(askPayload(q, []), ctl.signal, function (ev) {
                if (ev.type === 'meta') { items = ev.items || []; }
                else if (ev.type === 'text' || ev.type === 'delta') {
                    acc += ev.text || '';
                    renderAnswer(text, acc, items);
                } else if (ev.type === 'error') {
                    failed = true;
                    // Not grounded / unavailable: nothing worth showing above results.
                    if (['not_grounded', 'ungrounded', 'no_key', 'llm_unavailable'].indexOf(ev.code) >= 0) {
                        box.hidden = true;
                    } else {
                        text.textContent = errorText(ev.code);
                    }
                }
            }).then(function () {
                text.setAttribute('aria-busy', 'false');
                lastAnswer = acc;
                if (!acc && !failed) box.hidden = true;
            }).catch(function (e) {
                text.setAttribute('aria-busy', 'false');
                if (e && e.name === 'AbortError') return;
                // 404 = the Ask backend is not deployed / flag off: stay hidden.
                if (e && (e.status === 404 || e.status === 503)) { box.hidden = true; return; }
                text.textContent = errorText(null, e && e.status);
            });
        }

        getBtn.addEventListener('click', function () { run(lastQ); });
        cont.addEventListener('click', function () {
            askPanel.open(lastQ, lastAnswer ? { q: lastQ, a: lastAnswer } : null);
        });

        return {
            reset: reset,
            offer: function (body) {
                reset();
                lastQ = (body.query && body.query.raw) || (current && current.q) || '';
                // Hidden when nothing was found: there is nothing to ground on.
                if (!(body.results || []).length) return;
                box.hidden = false;
                if (looksLikeQuestion(lastQ)) {
                    // ADR §4.6: lazily, never per keystroke.
                    timer = setTimeout(function () { run(lastQ); }, 600);
                } else {
                    getBtn.hidden = false;
                }
            }
        };
    })();

    function askPayload(q, history) {
        var p = { q: q, lang: LANG, history: history || [] };
        if (PROJECT) { p.project = PROJECT; p.project_id = PROJECT; p.scope = 'project'; }
        else { p.scope = 'org'; }
        return p;
    }

    var askPanel = (function () {
        var panel = document.getElementById('tf-ask');
        var opener = root.querySelector('[data-ask-open]');
        if (!AI || !panel) return { open: function () {} };
        var log = document.getElementById('tf-ask-log');
        var form = document.getElementById('tf-ask-form');
        var box = document.getElementById('tf-ask-input');
        var send = form.querySelector('[data-ask-send]');
        var stop = form.querySelector('[data-ask-stop]');
        var empty = log.querySelector('[data-ask-empty]');
        var history = [];
        var ctl = null;
        var returnFocus = null;

        function setOpen(open) {
            panel.hidden = !open;
            document.body.classList.toggle('tf-ask-is-open', open);
            if (opener) opener.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) box.focus();
            else if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
        }

        function busy(on) {
            send.hidden = on;
            stop.hidden = !on;
            box.disabled = false;
        }

        function ask(q) {
            q = (q || '').trim();
            if (!q) return;
            if (ctl) ctl.abort();
            if (empty) empty.hidden = true;
            var turn = el('div', { class: 'tf-ask-turn' });
            turn.appendChild(el('div', { class: 'tf-ask-q' }, [
                el('span', { class: 'sr-only', text: tr('ask_you') + ': ' }), q]));
            var answer = el('div', { class: 'tf-ask-a', 'aria-busy': 'true' });
            turn.appendChild(answer);
            var extras = el('div', { class: 'tf-ask-extras' });
            turn.appendChild(extras);
            log.appendChild(turn);
            log.scrollTop = log.scrollHeight;

            ctl = new AbortController();
            busy(true);
            var t0 = performance.now();
            var acc = '';
            var items = [];
            var meta = {};
            var chips = [];
            var citations = null;
            var failed = false;
            var hist = history.slice(-2);

            streamAsk(askPayload(q, hist), ctl.signal, function (ev) {
                if (ev.type === 'meta') { meta = ev; items = ev.items || []; }
                else if (ev.type === 'text' || ev.type === 'delta') {
                    acc += ev.text || '';
                    renderAnswer(answer, acc, items);
                    log.scrollTop = log.scrollHeight;
                } else if (ev.type === 'chips' || ev.type === 'suggestions') {
                    chips = ev.chips || ev.items || [];
                } else if (ev.type === 'citations') {
                    citations = ev.refs || [];
                } else if (ev.type === 'error') {
                    failed = true;
                    answer.appendChild(el('p', { class: 'tf-ask-error', role: 'alert', text: errorText(ev.code) }));
                } else if (ev.type === 'done') {
                    if (ev.citations) citations = ev.citations;
                    if (ev.cached != null) meta.cached = ev.cached;
                }
            }).then(function () {
                finish(false);
            }).catch(function (e) {
                if (e && e.name === 'AbortError') {
                    answer.appendChild(el('p', { class: 'tf-ask-note', text: tr('ask_stopped') }));
                    finish(true);
                    return;
                }
                failed = true;
                answer.appendChild(el('p', { class: 'tf-ask-error', role: 'alert',
                                             text: errorText(null, e && e.status) }));
                finish(true);
            });

            function finish(aborted) {
                answer.setAttribute('aria-busy', 'false');
                busy(false);
                ctl = null;
                if (acc && !aborted) history.push({ q: q, a: acc.slice(0, 600) });
                // Cited item cards: the cited refs, else every item handed over.
                var refs = (citations || []).map(itemRef);
                var cards = items.filter(function (it) {
                    return typeof it === 'object' && it && (!refs.length || refs.indexOf(it.ref) >= 0);
                });
                if (!cards.length && refs.length) {
                    cards = refs.map(function (r) { return { ref: r, url: refUrls[r] }; });
                }
                if (cards.length && !failed) {
                    extras.appendChild(el('h3', { class: 'tf-ask-sub', text: tr('ask_cited') }));
                    var ul = el('ul', { class: 'tf-ask-cards' });
                    cards.slice(0, 6).forEach(function (c) {
                        ul.appendChild(el('li', { class: 'tf-ask-card' }, [
                            c.kind ? el('span', { class: 'tf-kind tf-kind-' + c.kind, text: groupLabel(c.kind) }) : null,
                            el('a', { href: c.url || refUrls[c.ref] || '#' }, [
                                el('span', { class: 'tf-hit-ref', text: c.ref }), cleanTitle(c.ref, c.title)])
                        ]));
                    });
                    extras.appendChild(ul);
                }
                var labels = chips.map(function (c) { return typeof c === 'string' ? c : (c.label || c.q || ''); })
                                  .filter(Boolean).slice(0, 4);
                if (labels.length) {
                    var row = el('div', { class: 'tf-chip-row', role: 'group', 'aria-label': tr('ask_followups') });
                    chips.slice(0, 4).forEach(function (c) {
                        var label = typeof c === 'string' ? c : (c.label || c.q || '');
                        var askQ = typeof c === 'string' ? c : (c.q || c.label || '');
                        if (!label) return;
                        var b = el('button', { type: 'button', class: 'tf-chip tf-chip-refine', text: label });
                        b.addEventListener('click', function () { ask(askQ); });
                        row.appendChild(b);
                    });
                    extras.appendChild(row);
                }
                var secs = ((performance.now() - t0) / 1000).toFixed(1);
                var n = (citations && citations.length) || items.length;
                var metaText = tr('ask_sources', { n: n, s: secs });
                if (meta.cached) metaText += ' · ' + tr('ask_cached');
                if (!failed) extras.appendChild(el('p', { class: 'tf-ask-meta', text: metaText }));
                log.scrollTop = log.scrollHeight;
            }
        }

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var q = box.value;
            box.value = '';
            ask(q);
        });
        box.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit')); }
        });
        stop.addEventListener('click', function () { if (ctl) ctl.abort(); box.focus(); });
        panel.querySelector('[data-ask-close]').addEventListener('click', function () { setOpen(false); });
        panel.querySelector('[data-ask-reset]').addEventListener('click', function () {
            if (ctl) ctl.abort();
            history = [];
            Array.prototype.slice.call(log.querySelectorAll('.tf-ask-turn')).forEach(function (n) { n.remove(); });
            if (empty) empty.hidden = false;
            box.focus();
        });
        panel.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { e.preventDefault(); setOpen(false); }
        });
        if (opener) {
            opener.addEventListener('click', function () {
                returnFocus = opener;
                setOpen(panel.hidden);
            });
        }

        return {
            open: function (q, seed) {
                returnFocus = document.activeElement && document.activeElement !== document.body
                    ? document.activeElement : input;
                setOpen(true);
                if (seed && seed.q && seed.a) {
                    // Continue from the answer already shown above the results.
                    if (empty) empty.hidden = true;
                    history.push(seed);
                    var turn = el('div', { class: 'tf-ask-turn' }, [
                        el('div', { class: 'tf-ask-q' }, [el('span', { class: 'sr-only', text: tr('ask_you') + ': ' }), seed.q]),
                        el('div', { class: 'tf-ask-a' })
                    ]);
                    renderAnswer(turn.lastChild, seed.a, []);
                    log.appendChild(turn);
                    return;
                }
                if (q) ask(q);
            }
        };
    })();
})();

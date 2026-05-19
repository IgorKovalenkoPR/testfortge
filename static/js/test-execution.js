/* TestExecution page client logic.
 *
 * Four IIFE-scoped sections, mirrored from the inline <script> blocks
 * that used to live in templates/test_execution.html:
 *   1. Active-run progress widget
 *   2. OS family mirror (web / mobile-web hidden field)
 *   3. Selection, env switcher, custom-select toggles, run overlay
 *   4. Upload drag-and-drop + auto-run
 *
 * Jinja-interpolated values shuttle in via <meta> tags so this file
 * stays static and can be served under a CSP that drops 'unsafe-inline'
 * from script-src in favour of a per-request nonce.
 */

(function teMetaHelpers() {
    function metaContent(name) {
        var el = document.querySelector('meta[name="' + name + '"]');
        return el ? el.getAttribute('content') || '' : '';
    }
    window.__teMeta = metaContent;
})();


/* ── 1. Active-run progress widget ──────────────────────────────── */
(function teActiveRunWidget() {
    var card = document.getElementById('te-active-run');
    if (!card) return;
    var runId = card.dataset.runId;
    var total = parseInt(card.dataset.caseCount, 10) || 0;
    var pill = document.getElementById('te-active-pill');
    var prog = document.getElementById('te-active-progress');
    var hint = document.getElementById('te-active-hint');
    var btn  = document.getElementById('te-active-results-btn');
    var statusUrl = "/test-execution/run-status/" + encodeURIComponent(runId);
    var infoUrl  = window.__teMeta('te-live-info-url');

    function paint(stat) {
        var s = (stat && stat.status) || 'queued';
        if (s === 'done') {
            pill.textContent = '✓ done';
            pill.style.background = '#e0e7ff';
            pill.style.color = '#3730a3';
            hint.textContent = 'Run finished. Importing results…';
            if (btn) { btn.style.display = ''; }
            // One-time auto-import; if it fails the user has the button.
            // 1.5s delay lets the success pill register before redirect.
            setTimeout(function () {
                window.location.href =
                    "/test-execution/results/" + encodeURIComponent(runId);
            }, 1500);
        } else if (s === 'stalled' || s === 'failed') {
            pill.textContent = (s === 'stalled') ? '⚠ stalled' : '✗ failed';
            pill.style.background = '#fee2e2';
            pill.style.color = '#991b1b';
            hint.textContent = stat.error
                || 'Worker did not finish cleanly.';
            if (btn) {
                btn.style.display = '';
                btn.textContent = 'Import partial results';
            }
        } else {
            pill.textContent = '● running';
            pill.style.background = '#dcfce7';
            pill.style.color = '#166534';
        }
    }

    function pollProgress() {
        // info.json gets cases_done updates from the worker even when
        // run-status hasn't flipped yet — gives sub-second feedback.
        if (!infoUrl) return;
        fetch(infoUrl, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (info) {
                if (info && info.cases_done >= 0) {
                    var done = info.cases_done || 0;
                    var grand = info.cases_total || total || 1;
                    prog.textContent = done + ' / ' + grand + ' cases';
                }
            })
            .catch(function () {});
    }

    function pollStatus() {
        fetch(statusUrl, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(paint)
            .catch(function () {});
    }

    pollStatus();
    pollProgress();
    var iv = setInterval(function () {
        pollStatus();
        pollProgress();
    }, 2000);
    // Defensive against memory leaks if the auto-redirect is delayed.
    window.addEventListener('beforeunload', function () {
        clearInterval(iv);
    });
})();


/* ── 2. OS family mirror ────────────────────────────────────────── */
(function teOsFamilyMirror() {
    function mirror(selectId, hiddenId) {
        var sel = document.getElementById(selectId);
        var hid = document.getElementById(hiddenId);
        if (!sel || !hid) return;
        function sync() {
            var opt = sel.options[sel.selectedIndex];
            if (opt && opt.parentNode && opt.parentNode.tagName === 'OPTGROUP') {
                hid.value = opt.parentNode.label || hid.value;
            }
        }
        sync();
        sel.addEventListener('change', sync);
    }
    mirror('web_os_version', 'web_platform_hidden');
    mirror('mw_os_version',  'mw_os_hidden');
})();


/* ── 3. Selection, env switcher, custom-selects, run overlay ───── */
(function teExecutionPage() {
    function toggleItemList() {
        var source = document.querySelector('input[name="source"]:checked');
        if (!source) return;
        var tcList = document.getElementById('items-tc');
        var clList = document.getElementById('items-cl');
        if (source.value === 'test_cases') {
            if (tcList) tcList.style.display = 'block';
            if (clList) clList.style.display = 'none';
            document.querySelectorAll('.item-cb-tc').forEach(function(cb) { cb.disabled = false; });
            document.querySelectorAll('.item-cb-cl').forEach(function(cb) { cb.disabled = true; });
        } else {
            if (tcList) tcList.style.display = 'none';
            if (clList) clList.style.display = 'block';
            document.querySelectorAll('.item-cb-tc').forEach(function(cb) { cb.disabled = true; });
            document.querySelectorAll('.item-cb-cl').forEach(function(cb) { cb.disabled = false; });
        }
        teSyncSectionCheckboxes();
        teUpdateCounter();
    }

    function teActiveItemSelector() {
        var source = document.querySelector('input[name="source"]:checked');
        return source && source.value === 'checklist' ? '.item-cb-cl' : '.item-cb-tc';
    }

    function selectAll() {
        var cls = teActiveItemSelector();
        document.querySelectorAll(cls).forEach(function(cb) { cb.checked = true; });
        teSyncSectionCheckboxes();
        teUpdateCounter();
    }

    function selectNone() {
        var cls = teActiveItemSelector();
        document.querySelectorAll(cls).forEach(function(cb) { cb.checked = false; });
        teSyncSectionCheckboxes();
        teUpdateCounter();
    }

    function teUpdateCounter() {
        var sel = teActiveItemSelector();
        var nodes = document.querySelectorAll(sel);
        var checked = 0;
        nodes.forEach(function(cb) { if (cb.checked && !cb.disabled) checked++; });
        var nowEl = document.getElementById('te-selected-now');
        var totalEl = document.getElementById('te-selected-total');
        if (nowEl) nowEl.textContent = checked;
        if (totalEl) totalEl.textContent = nodes.length;
    }

    function teSyncSectionCheckboxes() {
        document.querySelectorAll('.section-cb').forEach(function(secCb) {
            var target = secCb.dataset.target;
            if (!target) return;
            var children = document.querySelectorAll(target);
            if (!children.length) return;
            var on = 0, off = 0;
            children.forEach(function(c) { if (c.checked) on++; else off++; });
            secCb.checked = on > 0 && off === 0;
            secCb.indeterminate = on > 0 && off > 0;
        });
    }

    function toggleRunDetails(id) {
        var el = document.getElementById(id);
        if (!el) return;
        var toggle = document.getElementById('toggle-' + id);
        if (el.style.display === 'none') {
            el.style.display = 'block';
            if (toggle) {
                toggle.innerHTML = '<i data-lucide="chevron-up" class="icon-sm"></i>';
                if (window.lucide) window.lucide.createIcons();
            }
        } else {
            el.style.display = 'none';
            if (toggle) {
                toggle.innerHTML = '<i data-lucide="chevron-down" class="icon-sm"></i>';
                if (window.lucide) window.lucide.createIcons();
            }
        }
    }

    // Expose so legacy inline handlers keep working during the
    // commit-1 transition; the data-action delegation in commit 2
    // takes over after the inline onclick= attributes are removed.
    window.toggleItemList = toggleItemList;
    window.selectAll = selectAll;
    window.selectNone = selectNone;
    window.toggleRunDetails = toggleRunDetails;
    window.teUpdateCounter = teUpdateCounter;
    window.teSyncSectionCheckboxes = teSyncSectionCheckboxes;
    window.teActiveItemSelector = teActiveItemSelector;

    // Delegated change listener — one per item list so dynamic content
    // (rare here, but future-proof) keeps working.
    (function teBindSelection(){
        var card = document.getElementById('items-tc');
        var card2 = document.getElementById('items-cl');
        function onChange(ev) {
            var t = ev.target;
            if (t.classList.contains('section-cb')) {
                var sel = t.dataset.target;
                if (sel) {
                    document.querySelectorAll(sel).forEach(function(c) {
                        if (!c.disabled) c.checked = t.checked;
                    });
                }
                t.indeterminate = false;
                teUpdateCounter();
                return;
            }
            if (t.classList.contains('item-cb')) {
                teSyncSectionCheckboxes();
                teUpdateCounter();
            }
        }
        [card, card2].forEach(function(el) {
            if (el) el.addEventListener('change', onChange);
        });
        document.addEventListener('DOMContentLoaded', function() {
            teSyncSectionCheckboxes();
            teUpdateCounter();
        });
    })();

    // Source radios call back into toggleItemList; data-action keeps
    // CSP-friendly behaviour once the inline onchange= is removed.
    document.addEventListener('change', function (ev) {
        var t = ev.target;
        if (t && t.name === 'source' && t.type === 'radio') {
            toggleItemList();
        }
    });

    // Delegated click handler for the run-card header and the
    // All / None selection links. Replaces the inline onclick= calls.
    document.addEventListener('click', function (ev) {
        var el = ev.target;
        while (el && el !== document) {
            var action = el.getAttribute && el.getAttribute('data-action');
            if (action === 'toggle-run') {
                var runId = el.getAttribute('data-run-id');
                if (runId) toggleRunDetails('run-' + runId);
                return;
            }
            if (action === 'select-all') {
                ev.preventDefault();
                selectAll();
                return;
            }
            if (action === 'select-none') {
                ev.preventDefault();
                selectNone();
                return;
            }
            el = el.parentNode;
        }
    });

    // Multi-checkbox environment panel switcher. Each ticked env_type
    // reveals its panel; hidden panels still post default values but
    // the route ignores them unless their env_type box is checked.
    (function te_envSwitch() {
        var boxes = document.querySelectorAll('input[name="env_type"]');
        var panels = document.querySelectorAll('.te-env-panel');
        function syncPanels() {
            var active = new Set();
            boxes.forEach(function (b) { if (b.checked) active.add(b.value); });
            panels.forEach(function (p) {
                p.hidden = !active.has(p.getAttribute('data-env'));
            });
        }
        boxes.forEach(function (b) { b.addEventListener('change', syncPanels); });
        syncPanels();
    })();

    // "Custom..." → reveal the matching free-text input next to it.
    (function te_customSelectToggle() {
        var pairs = [
            ['select[name="mw_resolution"]', 'input[name="mw_resolution_custom"]'],
            ['select[name="ios_device"]',     'input[name="ios_device_custom"]'],
            ['select[name="android_device"]', 'input[name="android_device_custom"]'],
        ];
        pairs.forEach(function (pair) {
            var sel = document.querySelector(pair[0]);
            var inp = document.querySelector(pair[1]);
            if (!sel || !inp) return;
            sel.addEventListener('change', function () {
                inp.style.display = sel.value === '__custom' ? 'block' : 'none';
            });
        });
    })();

    // Auto-expand the latest test run.
    (function () {
        var runs = document.querySelectorAll('.run-details');
        if (runs.length > 0) {
            var last = runs[runs.length - 1];
            last.style.display = 'block';
            var toggleId = 'toggle-' + last.id;
            var toggle = document.getElementById(toggleId);
            if (toggle) {
                toggle.innerHTML = '<i data-lucide="chevron-up" class="icon-sm"></i>';
                if (window.lucide) window.lucide.createIcons();
            }
        }
    })();

    toggleItemList();

    /* ── Run progress overlay ────────────────────────────────── */
    (function teRunOverlay(){
        var form      = document.getElementById('exec-config-form');
        var overlay   = document.getElementById('te-run-overlay');
        var etaEl     = document.getElementById('te-overlay-eta');
        var elapsedEl = document.getElementById('te-overlay-elapsed');
        var liveBox   = document.getElementById('te-overlay-live');
        var liveTc    = document.getElementById('te-overlay-tc');
        var liveProg  = document.getElementById('te-overlay-progress');
        if (!form || !overlay) return;

        var liveInfoUrl = window.__teMeta('te-live-info-url');
        var slowMsg = window.__teMeta('te-i18n-overlay-slow');

        function fmtSec(s) {
            s = Math.max(0, Math.round(s));
            var m = Math.floor(s / 60);
            var r = s % 60;
            return (m > 0 ? m + 'm ' : '') + r + 's';
        }

        function computeEta() {
            var sel = teActiveItemSelector();
            var nodes = document.querySelectorAll(sel);
            var n = 0;
            nodes.forEach(function(c) { if (c.checked && !c.disabled) n++; });
            var envs = document.querySelectorAll('input[name="env_type"]:checked').length || 1;
            var baseUrl = (document.querySelector('input[name="base_url"]') || {}).value || '';
            var perCase = baseUrl ? 3.5 : 1.6;
            return Math.max(2, Math.round(n * envs * perCase));
        }

        var runStart = 0;
        var tickHandle = null;
        var pollHandle = null;
        var watchdog = null;

        function tick() {
            var elapsedSec = Math.round((Date.now() - runStart) / 1000);
            // Static ETA drifted on long runs and mis-informed operators;
            // show only actual elapsed time. Real cases-done / cases-total
            // lives in the inline live row below.
            if (etaEl) etaEl.textContent = '';
            if (elapsedEl) elapsedEl.textContent = 'Elapsed: ' + fmtSec(elapsedSec);
        }

        function pollLive() {
            if (!liveInfoUrl) return;
            fetch(liveInfoUrl, { cache: 'no-store' })
                .then(function(r) { return r.json(); })
                .then(function(info) {
                    if (!info || info.status === 'idle') return;
                    if (liveBox) liveBox.style.display = 'block';
                    if (liveTc) {
                        liveTc.textContent = info.current_tc
                            ? ('Test case: ' + info.current_tc)
                            : (info.status === 'starting' ? 'Browser launching…' : 'Run finished');
                    }
                    if (liveProg) {
                        if (info.status === 'done') {
                            liveProg.textContent = (info.cases_total || 0) + ' / '
                                                 + (info.cases_total || 0) + ' cases · finished';
                        } else {
                            liveProg.textContent = (info.cases_done || 0) + ' / '
                                                 + (info.cases_total || 0) + ' cases';
                        }
                    }
                })
                .catch(function() { /* keep polling on error */ });
        }

        form.addEventListener('submit', function(ev) {
            // Only show overlay for the main Run button — auxiliary buttons
            // (e.g. "Generate test account") use formaction.
            var submitter = ev.submitter || document.activeElement;
            if (submitter && submitter.id !== 'te-run-button') return;
            runStart = Date.now();
            computeEta();
            tick();
            if (tickHandle) clearInterval(tickHandle);
            tickHandle = setInterval(tick, 1000);
            if (pollHandle) clearInterval(pollHandle);
            pollHandle = setInterval(pollLive, 1500);
            pollLive();
            overlay.hidden = false;

            // 5-minute watchdog: surface "taking longer than expected"
            // and stop pretending to know the run state. Run continues
            // in the background; user refreshes manually to see results.
            if (watchdog) clearTimeout(watchdog);
            watchdog = setTimeout(function() {
                if (overlay.hidden) return;
                var card = overlay.querySelector('.te-run-card');
                if (!card) return;
                var notice = document.createElement('p');
                notice.style.cssText = 'margin-top:14px;padding:10px;'
                                    + 'background:#fef3c7;color:#92400e;'
                                    + 'border:1px solid #fbbf24;'
                                    + 'border-radius:6px;font-size:0.88em;';
                notice.textContent = slowMsg
                    || 'The run is taking longer than expected. The page '
                    +  'will not reload automatically — close this dialog '
                    +  'and refresh manually to see whatever results have '
                    +  'landed in the session.';
                card.appendChild(notice);
            }, 5 * 60 * 1000);
        });

        // Manual close — the synchronous POST may outlive gunicorn's
        // 300s ceiling and the page never reloads. The X / Escape give
        // the operator a way out without losing results that may have
        // already landed in the session.
        var closeBtn = document.getElementById('te-overlay-close');
        function closeOverlay() {
            overlay.hidden = true;
            if (tickHandle) clearInterval(tickHandle);
            if (pollHandle) clearInterval(pollHandle);
        }
        if (closeBtn) closeBtn.addEventListener('click', closeOverlay);
        document.addEventListener('keydown', function(ev) {
            if (ev.key === 'Escape' && !overlay.hidden) {
                ev.preventDefault();
                closeOverlay();
            }
        });

        window.addEventListener('pageshow', function() {
            overlay.hidden = true;
            if (tickHandle) clearInterval(tickHandle);
            if (pollHandle) clearInterval(pollHandle);
            if (watchdog) clearTimeout(watchdog);
        });
    })();
})();


/* ── 4. Upload drag-and-drop + auto-run ─────────────────────────── */
(function teUploadDnd() {
    document.querySelectorAll('form[data-te-upload]').forEach(function (form) {
        var zone    = form.querySelector('.te-drop-zone');
        var input   = zone && zone.querySelector('input[type="file"]');
        var summary = zone && zone.querySelector('.te-drop-summary');
        if (!zone || !input) return;

        function humanSize(bytes) {
            return bytes < 1024 * 1024
                ? (bytes / 1024).toFixed(1) + ' KB'
                : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }
        function render() {
            if (!summary) return;
            summary.innerHTML = '';
            Array.from(input.files).forEach(function (f) {
                var row = document.createElement('div');
                row.className = 'file-item';
                row.style.cssText = 'display:flex;justify-content:space-between;'
                                  + 'padding:4px 8px;background:#f1f5f9;'
                                  + 'border-radius:4px;margin-top:4px;'
                                  + 'font-size:0.85em;';
                row.innerHTML = '<span></span><span></span>';
                row.children[0].textContent = f.name;
                row.children[1].textContent = humanSize(f.size);
                summary.appendChild(row);
            });
        }
        zone.addEventListener('click', function (ev) {
            if (ev.target === input) return;
            ev.preventDefault();
            input.click();
        });
        zone.addEventListener('keydown', function (ev) {
            if (ev.key === 'Enter' || ev.key === ' ') {
                ev.preventDefault();
                input.click();
            }
        });
        ['dragenter', 'dragover'].forEach(function (n) {
            zone.addEventListener(n, function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
                zone.classList.add('drag-over');
            });
        });
        zone.addEventListener('dragleave', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            zone.classList.remove('drag-over');
        });
        zone.addEventListener('drop', function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            zone.classList.remove('drag-over');
            var dt = ev.dataTransfer;
            if (!dt || !dt.files || !dt.files.length) return;
            try { input.files = dt.files; }
            catch (_) {
                var t = new DataTransfer();
                Array.from(dt.files).forEach(function (f) { t.items.add(f); });
                input.files = t.files;
            }
            render();
        });
        input.addEventListener('change', render);
    });
})();

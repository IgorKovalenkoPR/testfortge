// PR-E — content script.
//
// Runs in every page (host_permissions <all_urls>) at document_start.
// Two responsibilities:
//   1. Detect the recording handoff fragment
//      (#testfortge-recorder-token=...) and notify the background
//      worker so it knows this tab is being recorded.
//   2. When recording is active for this tab, listen for click /
//      input / change / submit / SPA-navigation events, derive a
//      stable locator chain (port of PR-A's priority ladder), and
//      ship the resulting AutomationStep dict to the background.
//
// The recorder overlay is mounted into a Shadow DOM host so the SUT's
// CSS cannot reach it. The host element is also tagged with
// `data-testfortge-recorder` so the event listeners can skip clicks
// that originate from the overlay itself (REC dot, Stop button).

(function tfgRecorderContent() {
  'use strict';

  const HANDOFF_PARAM_TOKEN = 'testfortge-recorder-token';
  const HANDOFF_PARAM_FINISH = 'testfortge-finish-url';
  const HOST_ID = 'testfortge-recorder-host';

  // ── 1. Handoff detection ──────────────────────────────────────

  function readHandoff() {
    // Token may arrive on either query string OR fragment. Fragment
    // is the primary channel because the SUT page can't read it (it
    // stays client-side only) but extensions can. Query string is the
    // fallback when the SUT does redirects that strip fragments.
    const sources = [window.location.hash, window.location.search];
    for (const src of sources) {
      if (!src) continue;
      const params = new URLSearchParams(src.replace(/^[#?]/, ''));
      const token = params.get(HANDOFF_PARAM_TOKEN);
      const finishUrl = params.get(HANDOFF_PARAM_FINISH);
      if (token && finishUrl) {
        return {token, finishUrl};
      }
    }
    return null;
  }

  const handoff = readHandoff();
  if (handoff) {
    // Tell background to bind this tab to the recording session.
    chrome.runtime.sendMessage({
      type: 'register_session',
      token: handoff.token,
      finish_url: handoff.finishUrl,
      start_url: window.location.href,
    });
    // Scrub the handoff params from the URL so the SUT app doesn't
    // see them. Use replaceState to preserve back-button behaviour.
    try {
      const clean = new URL(window.location.href);
      const cleanHash = clean.hash
          .replace(/&?testfortge-recorder-token=[^&]*/, '')
          .replace(/&?testfortge-finish-url=[^&]*/, '')
          .replace(/^#&?/, '#')
          .replace(/^#$/, '');
      clean.hash = cleanHash;
      window.history.replaceState({}, '', clean.toString());
    } catch (e) { /* best-effort */ }
  }

  // ── 2. Active-state check + listener wiring ───────────────────

  // Every event handler calls into this — when no recording is bound
  // to this tab, returns false and the handler skips synthesising a
  // step. Cheap fast-path so listening on every page is harmless.
  let isActive = false;
  let lastStepUrl = '';

  function refreshActiveFromBackground() {
    chrome.runtime.sendMessage({type: 'is_active_for_tab'}, (resp) => {
      isActive = !!(resp && resp.active);
      if (isActive) {
        mountOverlay();
        emitGotoIfNew();
      }
    });
  }

  refreshActiveFromBackground();
  // Re-check on SPA navigation (history.pushState / popstate) — some
  // SUTs route entirely client-side and the content script would
  // otherwise miss that the recording is still ongoing.
  window.addEventListener('popstate', refreshActiveFromBackground);
  window.addEventListener('hashchange', refreshActiveFromBackground);

  // ── 3. Locator extraction (PR-A priority ladder, ported to JS) ──

  // Higher score = tried first by the runner's chain walk. Mirrors
  // engine/locator_registry.py:_STRATEGY_SCORE so the runner ranks the
  // same way at replay time.
  const STRATEGY_SCORE = {
    testid:      100,
    id:           90,
    role:         70,
    label:        60,
    placeholder:  55,
    alt:          50,
    title:        48,
    text:         40,
    css:          20,
    xpath:        10,
  };

  function pickRole(el) {
    // Trust author-specified role over implicit role. Author-specified
    // is closer to the recorder parser's view (it captures role=button
    // even if the element is a <div>).
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim();
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.href) return 'link';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'img') return 'img';
    return '';
  }

  function pickAccessibleName(el) {
    // Best-effort accessible name. Order roughly follows the ARIA
    // accessible-name computation:
    //   aria-label > aria-labelledby > <label for=> > text content >
    //   value attribute (for inputs).
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const ref = document.getElementById(labelledby);
      if (ref) return (ref.textContent || '').trim();
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (label) return (label.textContent || '').trim();
    }
    // For buttons + links, fall back to text. Limit length so we don't
    // capture a 300-char paragraph as the "name".
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'a') {
      const text = (el.textContent || '').trim();
      if (text && text.length <= 80) return text;
    }
    // Inputs: try the placeholder if no label.
    if (tag === 'input') {
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.trim();
    }
    return '';
  }

  function cssEscape(s) {
    // CSS.escape covers everything; older Chrome polyfills should
    // already have it but we're explicit so the test page is bulletproof.
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  function deriveCandidates(el) {
    // Returns a ranked candidate list for one element: same shape as
    // engine.locator_registry.LocatorCandidate. Higher score first.
    const cands = [];

    const testid = el.getAttribute('data-testid')
                || el.getAttribute('data-test')
                || el.getAttribute('data-qa');
    if (testid) {
      cands.push({strategy: 'testid', value: `data-testid=${testid}`,
                   score: STRATEGY_SCORE.testid});
    }

    if (el.id) {
      cands.push({strategy: 'id', value: `#${cssEscape(el.id)}`,
                   score: STRATEGY_SCORE.id});
    }

    const role = pickRole(el);
    const name = pickAccessibleName(el);
    if (role && name) {
      cands.push({strategy: 'role',
                   value: `role=${role}[name="${name.replace(/"/g, '\\"')}"]`,
                   score: STRATEGY_SCORE.role});
      // role-only relaxation — cheaper fallback when the visible name
      // changes (e.g. translation, A/B test).
      cands.push({strategy: 'role', value: `role=${role}`,
                   score: STRATEGY_SCORE.role - 5});
      cands.push({strategy: 'text', value: `text=${name}`,
                   score: STRATEGY_SCORE.text});
    } else if (role) {
      cands.push({strategy: 'role', value: `role=${role}`,
                   score: STRATEGY_SCORE.role});
    }

    if (el.tagName.toLowerCase() === 'input' ||
        el.tagName.toLowerCase() === 'textarea') {
      const ph = el.getAttribute('placeholder');
      if (ph) {
        cands.push({strategy: 'placeholder',
                     value: `placeholder=${ph}`,
                     score: STRATEGY_SCORE.placeholder});
      }
    }

    const alt = el.getAttribute('alt');
    if (alt) {
      cands.push({strategy: 'alt', value: `alt=${alt}`,
                   score: STRATEGY_SCORE.alt});
    }
    const title = el.getAttribute('title');
    if (title) {
      cands.push({strategy: 'title', value: `title=${title}`,
                   score: STRATEGY_SCORE.title});
    }

    // CSS fallback — last resort. Build a unique-enough path: tag +
    // .class + :nth-of-type(index) when the parent has siblings of the
    // same tag.
    cands.push({strategy: 'css', value: cssPath(el),
                 score: STRATEGY_SCORE.css});

    // Sort descending, dedupe by value.
    cands.sort((a, b) => b.score - a.score);
    const seen = new Set();
    return cands.filter(c => {
      if (!c.value || seen.has(c.value)) return false;
      seen.add(c.value);
      return true;
    });
  }

  function cssPath(el) {
    if (!(el instanceof Element)) return '';
    if (el.id) return `#${cssEscape(el.id)}`;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 4) {
      let part = cur.tagName.toLowerCase();
      if (cur.classList && cur.classList.length) {
        const cls = Array.from(cur.classList).slice(0, 2)
            .map(c => '.' + cssEscape(c)).join('');
        part += cls;
      }
      if (cur.parentElement) {
        const sibs = Array.from(cur.parentElement.children)
            .filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) {
          const i = sibs.indexOf(cur) + 1;
          part += `:nth-of-type(${i})`;
        }
      }
      parts.unshift(part);
      cur = cur.parentElement;
      if (cur && cur.tagName.toLowerCase() === 'body') break;
    }
    return parts.join(' > ');
  }

  function labelFromCandidates(cands) {
    // Stable Page Object DB key. Mirrors
    // engine.recorder_parser._label_from_chain: uses the leaf's
    // dominant strategy + value. Empty when no candidates.
    if (!cands.length) return '';
    const top = cands[0];
    if (top.strategy === 'testid') return `testid=${top.value.replace(/^data-testid=/, '')}`;
    if (top.strategy === 'role') {
      const m = top.value.match(/^role=([^\[]+)(?:\[name="(.+)"\])?$/);
      if (m && m[2]) return `role=${m[1]}:${m[2]}`;
      if (m) return `role=${m[1]}`;
    }
    return top.value;
  }

  function isOverlayEvent(target) {
    // Skip events that bubble up from our own REC overlay.
    let cur = target;
    while (cur) {
      if (cur.id === HOST_ID) return true;
      cur = cur.parentNode || (cur.host /* ShadowRoot */);
    }
    return false;
  }

  // ── 4. Event capture ──────────────────────────────────────────

  function emitStep(step) {
    if (!isActive) return;
    chrome.runtime.sendMessage({type: 'append_step', step}, () => {
      // Refresh overlay counter — keep popup + overlay in sync.
      updateOverlayCount();
    });
  }

  function emitGotoIfNew() {
    const url = window.location.href.split('#')[0];
    if (url === lastStepUrl) return;
    lastStepUrl = url;
    emitStep({
      action: 'goto',
      target: url,
      value: '',
      raw: `page.goto("${url}")`,
      comment: '',
      target_alternates: [],
      locator_label: '',
      kind: 'action',
      assertion_type: '',
    });
  }

  document.addEventListener('click', (e) => {
    if (!isActive) return;
    if (isOverlayEvent(e.target)) return;
    const cands = deriveCandidates(e.target);
    if (!cands.length) return;
    const primary = cands[0].value;
    const alternates = cands.slice(1, 5).map(c => c.value);
    emitStep({
      action: 'click',
      target: primary,
      value: '',
      raw: `page.locator("${primary.replace(/"/g, '\\"')}").click()`,
      comment: '',
      target_alternates: alternates,
      locator_label: labelFromCandidates(cands),
      kind: 'action',
      assertion_type: '',
    });
  }, true);  // capture phase so we beat SUT's preventDefault handlers

  document.addEventListener('change', (e) => {
    if (!isActive) return;
    if (isOverlayEvent(e.target)) return;
    const el = e.target;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return;
    const cands = deriveCandidates(el);
    if (!cands.length) return;
    const primary = cands[0].value;
    const alternates = cands.slice(1, 5).map(c => c.value);
    const value = (el.value || '').slice(0, 500);
    const action = tag === 'select' ? 'select' : 'fill';
    emitStep({
      action,
      target: primary,
      value,
      raw: `page.locator("${primary.replace(/"/g, '\\"')}").${action}("${value.replace(/"/g, '\\"')}")`,
      comment: '',
      target_alternates: alternates,
      locator_label: labelFromCandidates(cands),
      kind: 'action',
      assertion_type: '',
    });
  }, true);

  document.addEventListener('submit', (e) => {
    if (!isActive) return;
    if (isOverlayEvent(e.target)) return;
    // Submit usually fires alongside a click on a submit button; we
    // record a synthetic "form submitted" comment so the segmenter
    // sees a natural flow boundary. The runner doesn't need to replay
    // it — the click that triggered the submit already covers replay.
    emitStep({
      action: 'click',
      target: 'css=form',
      value: '',
      raw: 'page.locator("form").submit()',
      comment: 'form submitted',
      target_alternates: [],
      locator_label: '',
      kind: 'action',
      assertion_type: '',
    });
  }, true);

  // ── 5. Floating REC overlay ───────────────────────────────────

  let overlayShadow = null;
  let stepCountEl = null;

  function mountOverlay() {
    if (document.getElementById(HOST_ID)) return;  // already mounted
    const host = document.createElement('div');
    host.id = HOST_ID;
    host.setAttribute('data-testfortge-recorder', '1');
    document.documentElement.appendChild(host);
    overlayShadow = host.attachShadow({mode: 'open'});

    // Inline the CSS — web_accessible_resources path is brittle on
    // some SUTs (CSP restrictions on link tags). Keeps the overlay
    // self-contained.
    const style = document.createElement('style');
    style.textContent = `
      :host {
        all: initial;
        position: fixed !important;
        top: 16px !important; right: 16px !important;
        z-index: 2147483647 !important;
        font-family: system-ui, sans-serif !important;
      }
      .ov {
        background: #0f172a; color: #f8fafc;
        border-radius: 8px; padding: 10px 14px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.4);
        display: flex; align-items: center; gap: 10px;
        font-size: 13px; min-width: 220px;
      }
      .dot {
        width: 10px; height: 10px; border-radius: 50%;
        background: #ef4444;
        animation: blink 1.2s infinite;
      }
      @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
      .meta { display: flex; flex-direction: column; flex: 1; line-height: 1.3; }
      .label { color: #fb923c; font-weight: 600; font-size: 11px;
               letter-spacing: 0.05em; text-transform: uppercase; }
      .count { color: #cbd5e1; font-size: 12px; }
      .stop {
        background: #f97316; color: white; border: none;
        border-radius: 4px; padding: 6px 12px;
        cursor: pointer; font-size: 12px; font-weight: 600;
      }
      .stop:hover { background: #ea580c; }
      .stop:disabled { opacity: 0.6; cursor: not-allowed; }
    `;
    overlayShadow.appendChild(style);

    const wrap = document.createElement('div');
    wrap.className = 'ov';
    wrap.innerHTML =
        `<span class="dot"></span>` +
        `<div class="meta">` +
        `  <span class="label">TestForTge REC</span>` +
        `  <span class="count" id="tfg-rec-count">0 steps</span>` +
        `</div>` +
        `<button class="stop" id="tfg-rec-stop">Stop</button>`;
    overlayShadow.appendChild(wrap);

    stepCountEl = overlayShadow.getElementById('tfg-rec-count');
    overlayShadow.getElementById('tfg-rec-stop').addEventListener('click', () => {
      const btn = overlayShadow.getElementById('tfg-rec-stop');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      chrome.runtime.sendMessage({type: 'stop_recording'}, (resp) => {
        if (!resp || !resp.ok) {
          btn.disabled = false;
          btn.textContent = 'Retry';
        }
      });
    });
  }

  function updateOverlayCount() {
    if (!stepCountEl) return;
    chrome.runtime.sendMessage({type: 'get_state'}, (resp) => {
      if (!resp || !resp.active) return;
      const n = (resp.steps_buffer || []).length;
      stepCountEl.textContent = `${n} step${n === 1 ? '' : 's'}`;
    });
  }

  // ── 6. React to background events ─────────────────────────────

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    if (msg.type === 'session_started') {
      isActive = true;
      mountOverlay();
      emitGotoIfNew();
    } else if (msg.type === 'session_stopped') {
      isActive = false;
      const host = document.getElementById(HOST_ID);
      if (host) host.remove();
      overlayShadow = null;
      stepCountEl = null;
    }
  });
})();

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

  // One instance per page, enforced rather than assumed.
  //
  // The manifest injects this file once, so in principle this cannot
  // happen — but "in principle once" is what every listener that fires
  // twice was believed to be. Every handler below is registered on
  // `document`, so a second copy of this script would silently double
  // every captured step, and the symptom (a pack that clicks everything
  // twice on replay) points nowhere near the cause.
  if (window.__tfgRecorderContentInstalled) return;
  window.__tfgRecorderContentInstalled = true;

  const HANDOFF_PARAM_TOKEN = 'testfortge-recorder-token';
  const HANDOFF_PARAM_FINISH = 'testfortge-finish-url';
  const HOST_ID = 'testfortge-recorder-host';

  // ── 0. The bridge to the extension, and its death ─────────────
  //
  // Reloading or updating the extension orphans this script. The page
  // is untouched — listeners, timers and the overlay all keep running —
  // but `chrome.runtime` is now a corpse: `sendMessage` throws
  // "Extension context invalidated." at the call site, synchronously,
  // and `chrome.runtime.id` goes undefined. There is no event to
  // subscribe to; those two are the whole signal.
  //
  // Every call went through a bare `chrome.runtime.sendMessage` before,
  // so the first throw took out whatever came after it. In `emitStep`
  // that was `updateOverlayCount`, which is why the badge on staging
  // (2026-08-29 21:11:33) kept blinking REC over a counter frozen at
  // the last number it had managed to fetch, with a Stop button that
  // threw the same way and so never even reached its "Retry" fallback.
  // The tester went on testing; nothing was being recorded.
  //
  // So: one door out to the extension, and when it shuts, say so.
  let contextLost = false;

  // Every event handler calls into this — when no recording is bound
  // to this tab, returns false and the handler skips synthesising a
  // step. Cheap fast-path so listening on every page is harmless.
  //
  // Declared here rather than beside the listener wiring below because
  // `onContextLost` clears it, and the very first messages this script
  // sends (the handoffs) go out before that point. A `let` read before
  // its declaration is a ReferenceError, so an invalidated context at
  // page load would have crashed inside the handler for the crash.
  let isActive = false;
  let lastStepUrl = '';

  function isContextAlive() {
    // The id is present on a live context and gone on a dead one.
    // Reading it can itself throw depending on how far teardown got.
    try {
      return !!(chrome && chrome.runtime && chrome.runtime.id);
    } catch (e) {
      return false;
    }
  }

  function sendToBackground(msg, cb) {
    // Returns whether the message was handed off. Never throws: a dead
    // bridge must not take the page's event handlers down with it.
    if (contextLost) return false;
    try {
      chrome.runtime.sendMessage(msg, (resp) => {
        // Reading lastError is mandatory even when ignoring it —
        // an unread one prints itself to the SUT's console.
        const err = chrome.runtime.lastError;
        if (err) {
          // Not every lastError is fatal; a service worker that was
          // asleep answers late and noisily. Only the missing id
          // distinguishes "busy" from "gone", so ask.
          if (!isContextAlive()) onContextLost();
          return;
        }
        if (cb) cb(resp);
      });
      return true;
    } catch (e) {
      onContextLost();
      return false;
    }
  }

  //: How often the badge checks that the bridge is still there, in ms.
  //  This is a property read, not a message — no IPC, no round trip, no
  //  waking the service worker. Two seconds is chosen against a human
  //  looking at a badge, not against a machine.
  const ALIVE_POLL_MS = 2000;
  let _aliveTimer = null;

  function startAliveWatch() {
    // Detection used to be purely lazy: the context was only found dead
    // when something next tried to use it. Measured on staging while
    // walking the practice form — the extension was reloaded mid-
    // recording and the badge went on saying REC over "7 steps" until
    // the next click, which corrected it. That is the original lie for
    // as long as the tester looks without touching anything, and
    // whether they click next is not a property this fix gets to rely
    // on.
    if (_aliveTimer) return;
    _aliveTimer = setInterval(() => {
      // Self-cancel, and `onContextLost` clears the timer too. Mutation
      // testing says either alone is enough, so neither is covered by a
      // test — they cover each other, like `contextLost` and `isActive`
      // below. The pair is deliberate: this line stops a watch armed by
      // any future path that sets the flag directly, the other stops it
      // now rather than up to one tick from now.
      if (contextLost) { stopAliveWatch(); return; }
      checkAliveNow();
    }, ALIVE_POLL_MS);
    // Belt and braces, and honestly labelled as such.
    //
    // The argument for adding this was that a hidden tab's timers are
    // throttled — Chrome slows them in background tabs and, after five
    // minutes out of sight, can freeze the tab outright — and that
    // reloading an extension happens on chrome://extensions, so the
    // recording tab is hidden at exactly the moment its context dies.
    //
    // That argument was not borne out by the one measurement taken of
    // it (staging, 2026-08-30). A recording tab left hidden, running
    // the timer-only build, corrected its badge within about three
    // seconds of the reload — the timer ran on time. The severity the
    // listener was written for was predicted, not observed, and the
    // note in its commit message reads more confidently than the data
    // underneath it.
    //
    // It stays because it costs nothing, because tab freezing does stop
    // timers outright and that case is untested by anything else, and
    // because arriving on `visibilitychange` cannot be late for the
    // reason a timer can. It should not be read as the fix for a
    // measured defect. It is insurance.
    //
    // `visibilitychange` covers returning to the tab; `focus` covers
    // returning to the window.
    // Both are free, and neither can fire late for the reason the timer
    // can — the browser is done throttling by the time it tells you the
    // page is visible again.
    document.addEventListener('visibilitychange', onPageShown);
    window.addEventListener('focus', onPageShown);
  }

  function onPageShown() {
    if (document.visibilityState === 'hidden') return;
    checkAliveNow();
  }

  function checkAliveNow() {
    if (contextLost) return;
    if (!isContextAlive()) onContextLost();
  }

  function stopAliveWatch() {
    if (_aliveTimer) {
      clearInterval(_aliveTimer);
      _aliveTimer = null;
    }
    document.removeEventListener('visibilitychange', onPageShown);
    window.removeEventListener('focus', onPageShown);
  }

  function onContextLost() {
    if (contextLost) return;
    contextLost = true;
    stopAliveWatch();
    // Nothing can be recorded any more, so stop pretending to try.
    // Every handler below is gated on isActive, which makes this one
    // assignment the whole shutdown.
    //
    // It overlaps with the `contextLost` early-return above on purpose,
    // and mutation testing says so: delete either one alone and the
    // suite stays green, because the other still stops the calls. They
    // are not the same statement. `contextLost` shuts the door;
    // `isActive` stops the file's own notion of "we are recording"
    // from being a lie to whatever reads it next. Neither is covered
    // by the other's test — they are covered by each other.
    isActive = false;
    markOverlayLost();
  }

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

  // PR-F Phase 2 — control-session handoff. Same fragment mechanism as
  // recording, different params: a control token + the poll/result URLs
  // the background worker uses to talk to Flask. When present, this tab
  // becomes a remotely-driven executor (navigate / read_page / click /
  // fill / wait) for an MCP controller.
  const CONTROL_PARAM_TOKEN = 'testfortge-control-token';
  const CONTROL_PARAM_POLL = 'testfortge-poll-url';
  const CONTROL_PARAM_RESULT = 'testfortge-result-url';

  function readControlHandoff() {
    const sources = [window.location.hash, window.location.search];
    for (const src of sources) {
      if (!src) continue;
      const params = new URLSearchParams(src.replace(/^[#?]/, ''));
      const token = params.get(CONTROL_PARAM_TOKEN);
      if (token) {
        return {
          token,
          poll_url: params.get(CONTROL_PARAM_POLL) || '',
          result_url: params.get(CONTROL_PARAM_RESULT) || '',
        };
      }
    }
    return null;
  }

  const controlHandoff = readControlHandoff();
  if (controlHandoff) {
    sendToBackground({
      type: 'register_control',
      token: controlHandoff.token,
      poll_url: controlHandoff.poll_url,
      result_url: controlHandoff.result_url,
      start_url: window.location.href,
    });
    // Scrub the control params from the URL so the SUT never sees them.
    try {
      const clean = new URL(window.location.href);
      for (const p of [CONTROL_PARAM_TOKEN, CONTROL_PARAM_POLL,
                        CONTROL_PARAM_RESULT]) {
        clean.hash = clean.hash
            .replace(new RegExp('&?' + p + '=[^&]*'), '');
      }
      clean.hash = clean.hash.replace(/^#&?/, '#').replace(/^#$/, '');
      window.history.replaceState({}, '', clean.toString());
    } catch (e) { /* best-effort */ }
  }

  const handoff = readHandoff();
  if (handoff) {
    // Tell background to bind this tab to the recording session.
    sendToBackground({
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

  // `isActive` / `lastStepUrl` are declared up in section 0, next to
  // the bridge that clears them.
  function refreshActiveFromBackground() {
    sendToBackground({type: 'is_active_for_tab'}, (resp) => {
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
    // Implicit label — <label>Textarea <textarea></textarea></label>.
    // A wrapping label names its control under ARIA exactly as `for=`
    // does, and looking only for `for=` lost the name on a control that
    // plainly had one. Measured on staging 2026-08-29: a textarea inside
    // a wrapping label recorded as a bare `role=textbox`, which on that
    // page matched five elements.
    //
    // Only for the three controls a label actually names. The first
    // version of this asked `closest('label')` about *any* element, so
    // `<label>Terms <a href=…>Read them</a></label>` named the link
    // "Terms Read them" — the label's text, for an element the label
    // does not label. A <button> is labelable but names itself from its
    // own contents, which the branch below already does.
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      const implicit = el.closest ? el.closest('label') : null;
      if (implicit) {
        // The control's own content is not part of its name — a <select>
        // inside a label would otherwise contribute every option's text.
        let text = '';
        for (const node of implicit.childNodes) {
          if (node.nodeType === 3) {                       // text node
            text += node.textContent;
          } else if (node.nodeType === 1 &&
                     !node.matches('input,textarea,select,button')) {
            text += node.textContent || '';
          }
        }
        text = text.replace(/\s+/g, ' ').trim();
        if (text && text.length <= 80) return text;
      }
    }
    // For buttons + links, fall back to text. Limit length so we don't
    // capture a 300-char paragraph as the "name".
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

  // What a tester means when they say they clicked something.
  //
  // e.target is the deepest element under the pointer, which on a real
  // site is usually a <span>, an <svg> or a text wrapper inside the
  // actual control. Those carry no role, no id and no accessible name,
  // so deriveCandidates below had nothing to work with and fell all the
  // way to its CSS last resort: a click on a nav link recorded as
  // "ul > li:nth-of-type(1) > a > span:nth-of-type(1)", which breaks on
  // any markup change and tells a reader nothing. The ladder was never
  // the problem — it was being handed the wrong element.
  const ACTIONABLE_SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'label',
    'summary', 'option', '[role]', '[onclick]', '[tabindex]',
    '[data-testid]', '[data-test]', '[data-qa]',
    '[contenteditable=""]', '[contenteditable="true"]',
  ].join(',');

  // How far up to look. A control's inner markup is one to three levels
  // (a > span > svg); past that we would be guessing, and the guess gets
  // worse the further it goes — a page that wraps its content in
  // [role="main"] would swallow every click on plain text.
  const ACTIONABLE_MAX_DEPTH = 4;

  function labelledControl(node) {
    // A <label> is not a control; it is a handle on one, and clicking it
    // is how the browser is asked to click the control.
    //
    // That is also why it produced two steps. Clicking a label fires the
    // click on the label and then a second, synthetic click on the
    // control it labels — two events, two genuinely different elements,
    // so the duplicate window (which compares the resolved element)
    // could not see them as one. Measured on the Selenium practice form
    // on 2026-08-29: a click on a checkbox's label recorded
    // `click label.form-check-label` followed by `click #my-check-2`, so
    // a replay ticks the box and immediately unticks it.
    //
    // Resolving the label to its control is the same correction the
    // span-to-anchor duplicate needed — record what was interacted with,
    // not what the pointer happened to be over — and it makes the
    // existing window sufficient, because both events now resolve to one
    // element.
    //
    // `.control` covers both `for=` and a wrapped control, and is null
    // when the label names nothing; then the label is all there is.
    if (node && node.tagName && node.tagName.toLowerCase() === 'label') {
      const control = node.control;
      if (control) return control;
    }
    return node;
  }

  function actionableTarget(el) {
    let node = el;
    for (let depth = 0; node && depth <= ACTIONABLE_MAX_DEPTH; depth++) {
      if (node.nodeType === 1 && typeof node.matches === 'function'
          && node.matches(ACTIONABLE_SELECTOR)) {
        return labelledControl(node);
      }
      node = node.parentElement;
    }
    // Nothing actionable within reach: keep what was clicked. A CSS path
    // to the real target beats a role locator for a container that only
    // happens to be nearby.
    return el;
  }

  // Every element pickRole() can return a role for. Kept in step with
  // that function: a tag missing here would be counted as absent and a
  // non-unique role would pass the check below.
  const ROLE_BEARING_SELECTOR =
    '[role],button,a[href],input,textarea,select,img';

  function roleIsUnique(el, role) {
    // A bare `role=textbox` is only a locator if exactly one element on
    // the page carries that role. Otherwise it is a locator-shaped
    // guess: Playwright's strict mode rejects it, and a non-strict
    // runner silently takes the first match — which on the Selenium
    // practice form meant a fill aimed at the textarea landing in the
    // text input two fields above it. A CSS path is uglier and right.
    //
    // Counted rather than assumed, because uniqueness is a property of
    // the page and the recorder is standing in it. Bounded by an early
    // exit: the answer is known at the second match.
    let seen = 0;
    let matchedSelf = false;
    for (const cand of document.querySelectorAll(ROLE_BEARING_SELECTOR)) {
      if (pickRole(cand) !== role) continue;
      if (cand === el) matchedSelf = true;
      seen += 1;
      if (seen > 1) return false;
    }
    // matchedSelf guards the case the count alone cannot see: an element
    // inside a shadow root or an iframe is invisible to this query, so a
    // count of one would be about some *other* element entirely.
    return seen === 1 && matchedSelf;
  }

  function isInertClick(el) {
    // A click that landed on the page rather than on a control is not an
    // interaction. Measured on staging 2026-08-29: clicking empty space
    // beside a form recorded `click html.h-100 > body.d-flex.flex-column`
    // — a step that replays as a click on the document and asserts
    // nothing.
    //
    // `el` is what actionableTarget() resolved to, so "it matches
    // ACTIONABLE_SELECTOR" means the bounded walk found a real control
    // and there is nothing to decide.
    //
    // Otherwise the page's own statement about clickability is the
    // cursor. That is not a proxy for the truth, it is the same signal
    // the tester acted on: they clicked because the pointer said they
    // could. A layout <div> filling the empty half of a form says
    // `cursor: default` and gets dropped; a custom control built out of
    // a <div> says `pointer` and is kept, even though it carries no
    // role, no onclick attribute and no tabindex.
    //
    // The first version of this guard checked only for <body>/<html>, on
    // the grounds that dropping a step is worse than keeping a noisy
    // one. Re-running the walk showed that rule firing on almost
    // nothing: the same stray click lands on a container <div> the
    // moment a page has one, which is most pages. What it does cost is a
    // control with a listener, no ARIA, no tabindex and no pointer
    // cursor — one that is invisible to assistive technology and offers
    // the mouse no affordance either. That is rare and is itself a
    // defect; the noise was neither.
    if (!el || el.nodeType !== 1) return true;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'body' || tag === 'html') return true;
    if (typeof el.matches === 'function' && el.matches(ACTIONABLE_SELECTOR)) {
      return false;
    }
    try {
      return window.getComputedStyle(el).cursor !== 'pointer';
    } catch (err) {
      // Detached node, cross-origin quirk: keep the step. An unreadable
      // style is not evidence that nothing was clicked.
      return false;
    }
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
      // changes (e.g. translation, A/B test). Only when the role alone
      // identifies the element: otherwise this fallback fires precisely
      // when the name stopped matching and then picks whichever element
      // of that role comes first.
      if (roleIsUnique(el, role)) {
        cands.push({strategy: 'role', value: `role=${role}`,
                     score: STRATEGY_SCORE.role - 5});
      }
      cands.push({strategy: 'text', value: `text=${name}`,
                   score: STRATEGY_SCORE.text});
    } else if (role && roleIsUnique(el, role)) {
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
    sendToBackground({type: 'append_step', step}, () => {
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
    // A goto marks a fresh page — grab a DOM snapshot once it settles so
    // the review page can show what the tester was looking at (page
    // title, visible-text digest, and the interactive controls with
    // their locators). This is the content-script half of "read DOM":
    // the CDP side (network + console) lives in background.js.
    captureSnapshotSoon();
  }

  // ── DOM snapshot (the "read DOM" telemetry channel) ────────────

  let _snapshotTimer = null;

  function captureSnapshotSoon() {
    if (!isActive) return;
    // Debounce + let the page paint. SPA routes and full loads both
    // fire this; a short settle window catches client-rendered content
    // without waiting so long the tester has already moved on.
    if (_snapshotTimer) clearTimeout(_snapshotTimer);
    _snapshotTimer = setTimeout(() => {
      _snapshotTimer = null;
      try { captureSnapshot(); } catch (e) { /* best-effort */ }
    }, 500);
  }

  const INTERACTIVE_SELECTOR =
      'a[href], button, input, textarea, select, [role], [onclick], ' +
      '[contenteditable="true"], summary';

  function isVisible(el) {
    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
    const st = window.getComputedStyle(el);
    if (!st) return true;
    return st.visibility !== 'hidden' && st.display !== 'none' &&
           parseFloat(st.opacity || '1') > 0.01;
  }

  function captureSnapshot() {
    if (!isActive || !document.body) return;
    const interactive = [];
    const seen = new Set();
    const nodes = document.querySelectorAll(INTERACTIVE_SELECTOR);
    for (const el of nodes) {
      if (interactive.length >= 80) break;
      if (isOverlayEvent(el)) continue;
      if (!isVisible(el)) continue;
      const cands = deriveCandidates(el);
      if (!cands.length) continue;
      const locator = cands[0].value;
      if (seen.has(locator)) continue;
      seen.add(locator);
      const role = pickRole(el);
      const name = pickAccessibleName(el);
      const text = (el.textContent || '').trim().slice(0, 80);
      interactive.push({
        tag: el.tagName.toLowerCase(),
        role: role || '',
        name: name || '',
        text: name ? '' : text,
        locator,
        label: labelFromCandidates(cands),
      });
    }
    const digest = (document.body.innerText || '')
        .replace(/\s+/g, ' ').trim().slice(0, 2000);
    sendToBackground({
      type: 'append_snapshot',
      snapshot: {
        url: window.location.href.split('#')[0],
        title: (document.title || '').slice(0, 200),
        at: Date.now(),
        interactive,
        text_digest: digest,
        element_count: interactive.length,
      },
    });   // fire and forget; sendToBackground reads lastError for us
  }

  // One physical interaction must produce one step.
  //
  // Observed on staging 2026-08-28: a walk of two clicks produced four
  // click steps, each duplicated back to back. The cause was not
  // established — this page also carried five other extensions injecting
  // into it, and any of them re-dispatching an event would do it, as
  // would a second copy of this script (now prevented above). So this is
  // a guard on the property that matters rather than a fix aimed at a
  // cause: whatever delivers the same interaction twice, it is recorded
  // once. A replayed pack that clicks twice per step is worse than a
  // missing step — it can submit twice.
  //
  // The window is deliberately tiny. Duplicates of one physical event
  // arrive in the same task, microseconds apart; a human double-click
  // cannot be faster than about 60 ms, so a real one still records as
  // two steps and still replays as two clicks.
  const DUPLICATE_WINDOW_MS = 50;
  let lastEvent = {type: '', target: null, value: '', at: -Infinity};

  // ``target`` is the RESOLVED element, not e.target, and that
  // distinction is the whole guard.
  //
  // Measured on staging 2026-08-29: clicking the <span> inside a nav
  // link produced two events with two DIFFERENT raw targets — the span,
  // then the <a>, because the site forwards the click to the anchor.
  // Keyed on e.target they looked like two separate interactions and
  // both were recorded; resolved, they are one control clicked once.
  // Clicking the <a> directly never showed it, which is why the first
  // version of this guard looked complete.
  function isDuplicate(e, target, value) {
    const at = typeof e.timeStamp === 'number' ? e.timeStamp : Date.now();
    const same = lastEvent.type === e.type
      && lastEvent.target === target
      && lastEvent.value === (value || '')
      && (at - lastEvent.at) < DUPLICATE_WINDOW_MS;
    lastEvent = {type: e.type, target, value: value || '', at};
    return same;
  }

  document.addEventListener('click', (e) => {
    if (!isActive) return;
    if (isOverlayEvent(e.target)) return;
    // Resolve first, then dedupe: the ladder can only be as good as the
    // element it is given, and so can the duplicate check.
    const target = actionableTarget(e.target);
    if (isInertClick(target)) return;
    if (isDuplicate(e, target, '')) return;
    const cands = deriveCandidates(target);
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
    // A checkbox is toggled, not filled. Emitting `fill` for one was not
    // merely inelegant: Playwright refuses it outright — `Input of type
    // "checkbox" cannot be filled` — and the runner's fill branch calls
    // `loc.fill("")` before anything else, so every recorded pack
    // containing a checkbox or a radio failed at that step on replay.
    // Measured on the Selenium practice form on 2026-08-29, where the
    // change handler recorded `fill #my-check-2 = "on"`.
    //
    // `check` / `uncheck` rather than one verb carrying the state in
    // `value`: the runner's branch acts on the verb alone, so a state
    // smuggled into `value` would be read by nobody — which is exactly
    // how engine/recorder_parser already lost `uncheck`.
    //
    // Both are idempotent in Playwright, so the click step this pairs
    // with (clicking the box or its label toggles it too) replays as a
    // no-op rather than toggling it back.
    const toggle = tag === 'input' &&
        ['checkbox', 'radio'].includes(
            (el.getAttribute('type') || '').toLowerCase());
    const value = toggle ? '' : (el.value || '').slice(0, 500);
    // Same guard as the click path: a doubled fill writes the value
    // twice on replay, which an input with an append behaviour turns
    // into corrupt test data rather than a redundant step.
    if (isDuplicate(e, el, value)) return;
    let action;
    if (toggle) {
      action = el.checked ? 'check' : 'uncheck';
    } else {
      action = tag === 'select' ? 'select' : 'fill';
    }
    emitStep({
      action,
      target: primary,
      value,
      // check() and uncheck() take no argument, and the raw line is read
      // back by engine/recorder_parser — a stray "" would not parse as
      // the Playwright call it claims to be.
      raw: toggle
        ? `page.locator("${primary.replace(/"/g, '\\"')}").${action}()`
        : `page.locator("${primary.replace(/"/g, '\\"')}").${action}("${value.replace(/"/g, '\\"')}")`,
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
    //
    // Which is why it must not *look* replayable. This marker used to
    // go out as `click` on `css=form`, and "the runner doesn't need to"
    // was doing a lot of work in that sentence: nothing skipped it, so
    // AutomationRunner resolved the form and called `.click()` on it —
    // which lands on whatever child sits at the form's centre point. On
    // a four-field form that is a checkbox, and replay silently ticked
    // a box the tester never touched. A note about the past does not
    // get to be an instruction about the future.
    //
    // `submit` is a verb the rest of the pipeline already knows
    // (suite_classifier counts it as a form submission on its own), and
    // no target, because this addresses no element.
    emitStep({
      action: 'submit',
      target: '',
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

      /* The bridge to the extension is gone. Nothing here is recording
         any more, so nothing here may look like it is: the dot stops
         blinking and drains to grey, and the label drops the orange it
         shares with the live badge. */
      .ov.lost .dot { animation: none; background: #94a3b8; }
      .ov.lost .label { color: #94a3b8; }
      .ov.lost .count { color: #cbd5e1; max-width: 220px; }
      /* Orange is the colour of a running recording, on the dot, the
         label and the Stop button alike. A dead badge keeps none of it,
         Dismiss included — a tester reads the colour before the word. */
      .ov.lost .stop { background: #475569; }
      .ov.lost .stop:hover { background: #334155; }
    `;
    overlayShadow.appendChild(style);

    const wrap = document.createElement('div');
    wrap.className = 'ov';
    wrap.innerHTML =
        `<span class="dot"></span>` +
        `<div class="meta">` +
        `  <span class="label" id="tfg-rec-label">TestForTge REC</span>` +
        `  <span class="count" id="tfg-rec-count">0 steps</span>` +
        `</div>` +
        `<button class="stop" id="tfg-rec-stop">Stop</button>`;
    overlayShadow.appendChild(wrap);

    stepCountEl = overlayShadow.getElementById('tfg-rec-count');
    startAliveWatch();   // the badge is only honest while it is watched
    overlayShadow.getElementById('tfg-rec-stop').addEventListener('click', () => {
      const btn = overlayShadow.getElementById('tfg-rec-stop');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      sendToBackground({type: 'stop_recording'}, (resp) => {
        if (!resp || !resp.ok) {
          btn.disabled = false;
          btn.textContent = 'Retry';
        }
      });
    });
  }

  function markOverlayLost() {
    // Rewrite the badge into what is now true. Leaving it up matters:
    // removing it would read as "the recording ended", which is the
    // other half of the same lie, and the tester needs to know that the
    // steps they have taken since the extension reloaded are gone.
    //
    // Everything here runs on the page alone. Whatever this function
    // touches, it cannot need the extension — that is the one thing
    // known to be unavailable at the moment it is called.
    const host = document.getElementById(HOST_ID);
    const shadow = overlayShadow || (host && host.shadowRoot);
    if (!shadow) return;
    const wrap = shadow.querySelector('.ov');
    if (wrap) wrap.classList.add('lost');
    const label = shadow.getElementById('tfg-rec-label');
    if (label) label.textContent = 'Recording lost';
    const count = shadow.getElementById('tfg-rec-count');
    if (count) {
      count.textContent = 'Extension reloaded — start a new session';
    }
    const btn = shadow.getElementById('tfg-rec-stop');
    if (btn) {
      // Stop cannot save; it has no one left to save through. Offer the
      // only thing this page can still do by itself, and make sure it
      // is a button that works rather than a second dead control.
      btn.disabled = false;
      btn.textContent = 'Dismiss';
      // Cloning drops Stop's listener. Its effect is not observable
      // today — Stop's first act is to blank itself into "Saving…", and
      // the dismiss below removes the whole overlay in the same click,
      // so nothing is ever painted — and mutation testing confirms no
      // test here fails without it. It stays because a control that
      // carries two meanings is how this defect looked in the first
      // place, and the day dismissal stops being instant it would show.
      btn.replaceWith(btn.cloneNode(true));
      shadow.getElementById('tfg-rec-stop')
          .addEventListener('click', () => {
            const h = document.getElementById(HOST_ID);
            if (h) h.remove();
            overlayShadow = null;
            stepCountEl = null;
          });
    }
    stepCountEl = null;   // no further counter writes
  }

  function updateOverlayCount() {
    if (!stepCountEl) return;
    sendToBackground({type: 'get_state'}, (resp) => {
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
      stopAliveWatch();
      const host = document.getElementById(HOST_ID);
      if (host) host.remove();
      overlayShadow = null;
      stepCountEl = null;
    }
  });

  // ── 7. Active-driver executors (PR-F Phase 2) ──────────────────
  //
  // The background worker forwards each MCP command here as a
  // `control_exec` message. We answer with the structured result the
  // controller reads back. `read_page` assigns a fresh ref_N to every
  // visible interactive element and remembers it; `click` / `fill`
  // resolve those refs. Refs live for the lifetime of this content
  // script instance (i.e. until the next full navigation) — the agent
  // re-reads the page after navigating, exactly like read_page in the
  // Claude in Chrome flow.

  let _refCounter = 0;
  const _refMap = new Map();  // ref_N -> element

  function buildReadPage() {
    _refCounter = 0;
    _refMap.clear();
    const elements = [];
    const nodes = document.querySelectorAll(INTERACTIVE_SELECTOR);
    for (const el of nodes) {
      if (elements.length >= 200) break;
      if (isOverlayEvent(el)) continue;
      if (!isVisible(el)) continue;
      const ref = 'ref_' + (++_refCounter);
      _refMap.set(ref, el);
      const name = pickAccessibleName(el);
      elements.push({
        ref,
        tag: el.tagName.toLowerCase(),
        role: pickRole(el) || '',
        name: name || '',
        text: name ? '' : (el.textContent || '').trim().slice(0, 80),
      });
    }
    return {
      url: window.location.href,
      title: (document.title || '').slice(0, 200),
      elements,
      text_digest: (document.body ? document.body.innerText : '')
          .replace(/\s+/g, ' ').trim().slice(0, 2000),
    };
  }

  function resolveRef(ref) {
    const el = _refMap.get(ref);
    if (el && el.isConnected) return el;
    return null;
  }

  function execControl(verb, params) {
    params = params || {};
    if (verb === 'read_page') {
      return buildReadPage();
    }
    if (verb === 'click') {
      const el = resolveRef(params.ref);
      if (!el) return {__error: 'ref_not_found: ' + params.ref};
      try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) { /* */ }
      el.click();
      return {clicked: true, ref: params.ref};
    }
    if (verb === 'fill') {
      const el = resolveRef(params.ref);
      if (!el) return {__error: 'ref_not_found: ' + params.ref};
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      try { el.focus(); } catch (e) { /* */ }
      if (tag === 'input' || tag === 'textarea') {
        el.value = params.text != null ? String(params.text) : '';
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else if (el.isContentEditable) {
        el.textContent = params.text != null ? String(params.text) : '';
        el.dispatchEvent(new Event('input', {bubbles: true}));
      } else {
        return {__error: 'not_fillable: ' + tag};
      }
      return {filled: true, ref: params.ref};
    }
    return {__error: 'unknown_verb: ' + verb};
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== 'control_exec') return;
    let out;
    try {
      out = execControl(msg.verb, msg.params);
    } catch (e) {
      out = {__error: (e && e.message) ? e.message : String(e)};
    }
    sendResponse(out);
    return true;  // async-safe channel close
  });
})();

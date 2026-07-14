// PR-E + PR-F — service worker.
//
// Holds the recording session state across content-script tabs:
//   * token / finish_url come from /api/recorder-session/start
//     (TestForTge hands them off via URL fragment, content.js
//     forwards them here via `register_session`).
//   * steps_buffer accumulates AutomationStep dicts as content.js
//     emits them.
//   * telemetry (network + console + dom_snapshots) is captured LIVE
//     via the Chrome DevTools Protocol (chrome.debugger) — see the
//     "Deep capture (CDP)" section below. This is the PR-F upgrade
//     that brings the recorder to parity with what a DevTools-grade
//     recorder (or the Claude in Chrome extension) can observe:
//     real network requests with status codes, the page's real
//     console, and uncaught exceptions — none of which a content
//     script in its isolated world can ever see.
//   * On stop_recording: POST {token, steps, telemetry} to finish_url;
//     if 200, open the returned review_url in a new tab and clear state.
//
// MV3 service workers can be evicted between events. While a recording
// is active the debugger stream keeps the worker warm, but we still
// mirror state into chrome.storage.session (debounced) so a mid-session
// worker restart doesn't drop the buffer.

const STORAGE_KEY = 'tfg_recorder_state';
// Persisted (survives browser restart, unlike session state) — the
// TestForTge instance base URL. Auto-learned from the finish_url of any
// web-initiated recording, and settable from the popup so a fresh
// install can reach its instance before the first web session.
const BASE_URL_KEY = 'tfg_base_url';

// Telemetry caps — keep the finish payload bounded no matter how busy
// the SUT is. Oldest entries fall off the front once the cap is hit.
const NET_CAP = 500;        // network requests kept
const CONSOLE_CAP = 500;    // console + exception entries kept
const SNAPSHOT_CAP = 25;    // DOM snapshots kept
const STR_CAP = 2000;       // per-string truncation ceiling

const CDP_PROTOCOL = '1.3';

function _normaliseBase(url) {
  // Accept a full URL or bare host; return scheme://host[:port] with no
  // trailing slash, or '' if it doesn't parse to an http(s) origin.
  if (!url) return '';
  let u = String(url).trim();
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  try {
    const parsed = new URL(u);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    return parsed.origin;
  } catch (e) {
    return '';
  }
}

async function readBaseUrl() {
  return new Promise((resolve) => {
    chrome.storage.local.get([BASE_URL_KEY], (items) => {
      resolve((items && items[BASE_URL_KEY]) || '');
    });
  });
}

async function writeBaseUrl(url) {
  const base = _normaliseBase(url);
  if (!base) return '';
  return new Promise((resolve) => {
    chrome.storage.local.set({[BASE_URL_KEY]: base}, () => resolve(base));
  });
}

// ── State: in-memory CACHE + debounced persistence ────────────────
//
// The recorder mutates state on every captured event. Network events
// (via CDP) can fire dozens of times per page load, so a storage write
// per event would be wasteful and race-prone. Instead we keep an
// authoritative in-memory copy (CACHE) and flush it to storage.session
// at most once every PERSIST_MS. On a cold worker start CACHE is null
// and we hydrate it lazily from storage.

let CACHE = null;              // authoritative live state (or null when idle)
let _persistTimer = null;
const PERSIST_MS = 700;

function _emptyTelemetry() {
  return {
    network: [],
    console: [],
    dom_snapshots: [],
    meta: {debugger_ok: false, debugger_error: '', started_at: 0},
  };
}

function _storageGet() {
  return new Promise((resolve) => {
    chrome.storage.session.get([STORAGE_KEY], (items) => {
      resolve(items[STORAGE_KEY] || null);
    });
  });
}

function _storageSet(state) {
  return new Promise((resolve) => {
    chrome.storage.session.set({[STORAGE_KEY]: state}, resolve);
  });
}

async function loadState() {
  if (CACHE) return CACHE;
  CACHE = await _storageGet();
  return CACHE;
}

function schedulePersist() {
  if (_persistTimer) return;
  _persistTimer = setTimeout(async () => {
    _persistTimer = null;
    if (CACHE) await _storageSet(CACHE);
  }, PERSIST_MS);
}

async function persistNow() {
  if (_persistTimer) {
    clearTimeout(_persistTimer);
    _persistTimer = null;
  }
  if (CACHE) await _storageSet(CACHE);
}

async function readState() {
  // Public read — serve the live cache when present, else storage.
  return loadState();
}

async function clearState() {
  CACHE = null;
  if (_persistTimer) {
    clearTimeout(_persistTimer);
    _persistTimer = null;
  }
  return new Promise((resolve) => {
    chrome.storage.session.remove([STORAGE_KEY], resolve);
  });
}

async function isActiveForTab(tabId) {
  const state = await loadState();
  if (!state) return false;
  // We bind to a tab id when register_session fires from a specific
  // tab. If no tab is bound yet (race between content.js init and
  // storage write), assume any tab can be active — the URL-fragment
  // handoff already proved the tester explicitly launched recording.
  return !state.tab_id || state.tab_id === tabId;
}

async function appendStep(step) {
  const state = await loadState();
  if (!state) return false;
  state.steps_buffer = state.steps_buffer || [];
  state.steps_buffer.push(step);
  schedulePersist();
  // Update the action badge so the operator sees "REC" + count even
  // when popup is closed.
  try {
    chrome.action.setBadgeText({text: String(state.steps_buffer.length)});
    chrome.action.setBadgeBackgroundColor({color: '#dc2626'});
  } catch (e) { /* badge API best-effort */ }
  return true;
}

async function appendSnapshot(snapshot) {
  const state = await loadState();
  if (!state) return false;
  state.telemetry = state.telemetry || _emptyTelemetry();
  _pushCapped(state.telemetry.dom_snapshots, snapshot, SNAPSHOT_CAP);
  schedulePersist();
  return true;
}

// ── Deep capture (CDP via chrome.debugger) ────────────────────────
//
// chrome.debugger.attach opens a DevTools-protocol session to the SUT
// tab. This is the ONLY way an extension can observe real network
// traffic (status codes, failures, redirects) and the page's real
// console. The cost the operator sees is Chrome's yellow "…started
// debugging this browser" banner — the same banner Playwright codegen,
// the DevTools Recorder, and Claude in Chrome all raise. When attach
// fails (DevTools already open on the tab, a chrome:// page, or the
// user dismissed the banner) we degrade gracefully: step capture keeps
// working, telemetry.meta.debugger_ok stays false, and the review page
// shows a "deep capture unavailable" note instead of an empty panel.

let ATTACHED_TAB = null;       // tabId we currently hold a debugger session on

function _cdpSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    try {
      chrome.debugger.sendCommand({tabId}, method, params || {}, (res) => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(res);
      });
    } catch (e) {
      reject(e);
    }
  });
}

async function _markDebugger(ok, errText) {
  const state = await loadState();
  if (!state) return;
  state.telemetry = state.telemetry || _emptyTelemetry();
  state.telemetry.meta.debugger_ok = !!ok;
  state.telemetry.meta.debugger_error = errText || '';
  schedulePersist();
}

async function attachDebugger(tabId) {
  if (!tabId) return false;
  if (ATTACHED_TAB === tabId) return true;
  // Detach a stale session on a different tab first (session was
  // re-bound to a new tab, e.g. the operator opened the SUT afresh).
  if (ATTACHED_TAB && ATTACHED_TAB !== tabId) {
    try { await _detach(ATTACHED_TAB); } catch (e) { /* */ }
  }
  try {
    await new Promise((resolve, reject) => {
      chrome.debugger.attach({tabId}, CDP_PROTOCOL, () => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve();
      });
    });
  } catch (e) {
    await _markDebugger(false, (e && e.message) || String(e));
    return false;
  }
  ATTACHED_TAB = tabId;
  // Enable the domains we stream from. Best-effort each — a domain that
  // fails to enable just yields no events for that channel.
  for (const method of ['Network.enable', 'Runtime.enable',
                          'Log.enable', 'Page.enable']) {
    try { await _cdpSend(tabId, method); } catch (e) { /* */ }
  }
  await _markDebugger(true, '');
  return true;
}

async function _detach(tabId) {
  if (!tabId) return;
  await new Promise((resolve) => {
    try {
      chrome.debugger.detach({tabId}, () => {
        // Swallow lastError — detaching an already-gone target is fine.
        void chrome.runtime.lastError;
        resolve();
      });
    } catch (e) {
      resolve();
    }
  });
  if (ATTACHED_TAB === tabId) ATTACHED_TAB = null;
}

async function detachDebugger() {
  if (ATTACHED_TAB) await _detach(ATTACHED_TAB);
}

function _pushCapped(arr, item, cap) {
  arr.push(item);
  if (arr.length > cap) arr.splice(0, arr.length - cap);
}

function _trunc(s) {
  s = (s == null) ? '' : String(s);
  return s.length > STR_CAP ? s.slice(0, STR_CAP) : s;
}

function _findByReqId(arr, reqId) {
  // Scan from the back — the matching request is almost always recent.
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i].request_id === reqId) return arr[i];
  }
  return null;
}

function _stringifyCdpArgs(args) {
  // Runtime.consoleAPICalled args are RemoteObjects. Prefer .value for
  // primitives, .description for objects/errors, fall back to type.
  if (!Array.isArray(args)) return '';
  return args.map((a) => {
    if (!a || typeof a !== 'object') return String(a);
    if ('value' in a && a.value !== undefined) return String(a.value);
    if (a.description) return String(a.description);
    if (a.unserializableValue) return String(a.unserializableValue);
    return a.type || '';
  }).join(' ');
}

async function handleCdpEvent(method, params) {
  const state = await loadState();
  if (!state || !state.active) return;
  const tele = state.telemetry = state.telemetry || _emptyTelemetry();

  switch (method) {
    case 'Network.requestWillBeSent': {
      const r = params.request || {};
      // Redirects reuse the requestId — update the existing record's
      // URL rather than double-counting the hop.
      const existing = params.redirectResponse
          ? _findByReqId(tele.network, params.requestId) : null;
      if (existing) {
        existing.redirects = (existing.redirects || 0) + 1;
        existing.status = params.redirectResponse.status || existing.status;
      } else {
        _pushCapped(tele.network, {
          request_id: params.requestId,
          method: r.method || '',
          url: _trunc(r.url || '').slice(0, 500),
          type: params.type || '',
          status: 0,
          ok: null,
          mime: '',
          error: '',
          redirects: 0,
          started_at: Date.now(),
        }, NET_CAP);
      }
      break;
    }
    case 'Network.responseReceived': {
      const resp = params.response || {};
      const rec = _findByReqId(tele.network, params.requestId);
      if (rec) {
        rec.status = resp.status || 0;
        rec.ok = (resp.status >= 200 && resp.status < 400);
        rec.mime = resp.mimeType || '';
      }
      break;
    }
    case 'Network.loadingFailed': {
      const rec = _findByReqId(tele.network, params.requestId);
      if (rec) {
        rec.error = params.errorText || 'failed';
        rec.ok = false;
        // canceled requests (navigation aborts) aren't real errors.
        if (params.canceled) rec.error = 'canceled';
      }
      break;
    }
    case 'Runtime.consoleAPICalled': {
      _pushCapped(tele.console, {
        level: params.type || 'log',
        text: _trunc(_stringifyCdpArgs(params.args)),
        source: 'console',
        at: Date.now(),
      }, CONSOLE_CAP);
      break;
    }
    case 'Log.entryAdded': {
      const e = params.entry || {};
      _pushCapped(tele.console, {
        level: e.level || 'info',
        text: _trunc(e.text || ''),
        url: _trunc(e.url || ''),
        source: e.source || 'log',
        at: Date.now(),
      }, CONSOLE_CAP);
      break;
    }
    case 'Runtime.exceptionThrown': {
      const d = params.exceptionDetails || {};
      const ex = d.exception || {};
      const msg = ex.description || ex.value || d.text || 'uncaught exception';
      _pushCapped(tele.console, {
        level: 'error',
        text: _trunc(msg),
        source: 'exception',
        at: Date.now(),
      }, CONSOLE_CAP);
      break;
    }
    default:
      return;  // domain we don't record — don't schedule a write
  }
  schedulePersist();
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source || source.tabId !== ATTACHED_TAB) return;
  handleCdpEvent(method, params).catch(() => { /* best-effort */ });
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source && source.tabId === ATTACHED_TAB) {
    ATTACHED_TAB = null;
    // DevTools opened on the tab, tab closed, or the user clicked
    // "Cancel" on the banner. Note it so the review page can explain
    // why the telemetry panel is thin.
    _markDebugger(false, 'detached: ' + (reason || 'unknown'))
        .catch(() => {});
  }
});

// ── Session lifecycle ─────────────────────────────────────────────

async function registerSession(payload, senderTabId) {
  // Idempotent — re-registering on a SPA navigation keeps the same
  // token + buffer, just refreshes the bound tab id.
  let state = await loadState();
  if (state && state.token === payload.token) {
    state.tab_id = senderTabId || state.tab_id;
    CACHE = state;
    schedulePersist();
    if (senderTabId) await attachDebugger(senderTabId);
    return state;
  }
  state = {
    token: payload.token,
    finish_url: payload.finish_url,
    start_url: payload.start_url,
    tab_id: senderTabId || null,
    started_at: Date.now(),
    steps_buffer: [],
    telemetry: _emptyTelemetry(),
    active: true,
  };
  state.telemetry.meta.started_at = state.started_at;
  CACHE = state;
  await persistNow();
  // Learn the TestForTge instance from the finish_url's origin so the
  // popup's "Start" / "Open TestForTge" shortcuts know where to go on
  // the next idle visit — without the operator configuring anything.
  if (payload.finish_url) {
    try { await writeBaseUrl(payload.finish_url); } catch (e) { /* best-effort */ }
  }
  // Attach the DevTools-protocol session so network + console start
  // streaming immediately. Non-fatal on failure — step capture still
  // works and the review page notes deep-capture was unavailable.
  if (senderTabId) await attachDebugger(senderTabId);
  try {
    chrome.action.setBadgeText({text: 'REC'});
    chrome.action.setBadgeBackgroundColor({color: '#dc2626'});
  } catch (e) { /* */ }
  return state;
}

async function stopRecording() {
  const state = await loadState();
  if (!state || !state.active) {
    return {ok: false, error: 'no_active_session'};
  }
  // Flush any pending mutation, then detach the debugger so the SUT tab
  // loses its yellow banner the moment the operator hits Stop.
  await persistNow();
  await detachDebugger();
  const tele = state.telemetry || _emptyTelemetry();
  const body = {
    token: state.token,
    steps: state.steps_buffer || [],
    telemetry: {
      network: tele.network || [],
      console: tele.console || [],
      dom_snapshots: tele.dom_snapshots || [],
      meta: tele.meta || {},
    },
  };
  let resp;
  try {
    const r = await fetch(state.finish_url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    resp = {ok: r.ok, status: r.status, body: await r.json().catch(() => ({}))};
  } catch (e) {
    return {ok: false, error: 'network: ' + (e && e.message ? e.message : String(e))};
  }
  if (!resp.ok) {
    return {
      ok: false,
      error: (resp.body && resp.body.error) || `http ${resp.status}`,
    };
  }
  const reviewUrl = resp.body && resp.body.review_url;
  // Clear state regardless of whether the review tab opens — the
  // backend already consumed the token.
  await clearState();
  try {
    chrome.action.setBadgeText({text: ''});
  } catch (e) { /* */ }
  if (reviewUrl) {
    try {
      chrome.tabs.create({url: reviewUrl});
    } catch (e) { /* if tabs.create fails, operator still sees ok=true */ }
  }
  // Notify all tabs that the session ended so they tear down their
  // overlays.
  try {
    const tabs = await chrome.tabs.query({});
    for (const t of tabs) {
      if (t.id) {
        chrome.tabs.sendMessage(t.id, {type: 'session_stopped'})
            .catch(() => {});
      }
    }
  } catch (e) { /* */ }
  return {
    ok: true,
    review_url: reviewUrl || '',
    step_count: (body.steps || []).length,
    network_count: (body.telemetry.network || []).length,
    console_count: (body.telemetry.console || []).length,
  };
}

// ── Message router ──────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (!msg || !msg.type) {
      sendResponse({error: 'bad_message'});
      return;
    }
    switch (msg.type) {
      case 'register_session': {
        const s = await registerSession({
          token: msg.token,
          finish_url: msg.finish_url,
          start_url: msg.start_url,
        }, sender.tab && sender.tab.id);
        // Tell the originating tab the session is now active so it
        // can mount the overlay + emit the initial goto step.
        if (sender.tab && sender.tab.id) {
          try {
            chrome.tabs.sendMessage(sender.tab.id, {type: 'session_started'})
                .catch(() => {});
          } catch (e) { /* */ }
        }
        sendResponse({ok: true, state: s});
        break;
      }
      case 'is_active_for_tab': {
        const active = await isActiveForTab(sender.tab && sender.tab.id);
        sendResponse({active});
        break;
      }
      case 'get_state': {
        const state = await loadState();
        sendResponse(state || {active: false});
        break;
      }
      case 'get_base_url': {
        const base = await readBaseUrl();
        sendResponse({base_url: base});
        break;
      }
      case 'set_base_url': {
        const base = await writeBaseUrl(msg.base_url);
        if (base) sendResponse({ok: true, base_url: base});
        else sendResponse({ok: false, error: 'invalid_url'});
        break;
      }
      case 'append_step': {
        const ok = await appendStep(msg.step);
        sendResponse({ok});
        break;
      }
      case 'append_snapshot': {
        const ok = await appendSnapshot(msg.snapshot);
        sendResponse({ok});
        break;
      }
      case 'stop_recording': {
        const res = await stopRecording();
        sendResponse(res);
        break;
      }
      default:
        sendResponse({error: 'unknown_type'});
    }
  })();
  return true;  // keep the channel open for async sendResponse
});

// On install: clear any stale state (operator may have reloaded the
// extension mid-session — orphaned tokens are useless because the
// server-side mapping is also process-local). Detach any leftover
// debugger session too.
chrome.runtime.onInstalled.addListener(async () => {
  await detachDebugger();
  await clearState();
  try { chrome.action.setBadgeText({text: ''}); } catch (e) { /* */ }
});

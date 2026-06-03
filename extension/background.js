// PR-E — service worker.
//
// Holds the recording session state across content-script tabs:
//   * token / finish_url come from /api/recorder-session/start
//     (TestForTge hands them off via URL fragment, content.js
//     forwards them here via `register_session`).
//   * steps_buffer accumulates AutomationStep dicts as content.js
//     emits them.
//   * On stop_recording: POST {token, steps} to finish_url; if 200,
//     open the returned review_url in a new tab and clear state.
//
// MV3 service workers can be evicted between events — we persist
// state to chrome.storage.session so the recording survives a worker
// restart while the browser is still open.

const STORAGE_KEY = 'tfg_recorder_state';

async function readState() {
  return new Promise((resolve) => {
    chrome.storage.session.get([STORAGE_KEY], (items) => {
      resolve(items[STORAGE_KEY] || null);
    });
  });
}

async function writeState(state) {
  return new Promise((resolve) => {
    chrome.storage.session.set({[STORAGE_KEY]: state}, resolve);
  });
}

async function clearState() {
  return new Promise((resolve) => {
    chrome.storage.session.remove([STORAGE_KEY], resolve);
  });
}

async function isActiveForTab(tabId) {
  const state = await readState();
  if (!state) return false;
  // We bind to a tab id when register_session fires from a specific
  // tab. If no tab is bound yet (race between content.js init and
  // storage write), assume any tab can be active — the URL-fragment
  // handoff already proved the tester explicitly launched recording.
  return !state.tab_id || state.tab_id === tabId;
}

async function appendStep(step) {
  const state = await readState();
  if (!state) return false;
  state.steps_buffer = state.steps_buffer || [];
  state.steps_buffer.push(step);
  await writeState(state);
  // Update the action badge so the operator sees "REC" + count even
  // when popup is closed.
  try {
    chrome.action.setBadgeText({text: String(state.steps_buffer.length)});
    chrome.action.setBadgeBackgroundColor({color: '#dc2626'});
  } catch (e) { /* badge API best-effort */ }
  return true;
}

async function registerSession(payload, senderTabId) {
  // Idempotent — re-registering on a SPA navigation keeps the same
  // token + buffer, just refreshes the bound tab id.
  let state = await readState();
  if (state && state.token === payload.token) {
    state.tab_id = senderTabId || state.tab_id;
    await writeState(state);
    return state;
  }
  state = {
    token: payload.token,
    finish_url: payload.finish_url,
    start_url: payload.start_url,
    tab_id: senderTabId || null,
    started_at: Date.now(),
    steps_buffer: [],
    active: true,
  };
  await writeState(state);
  try {
    chrome.action.setBadgeText({text: 'REC'});
    chrome.action.setBadgeBackgroundColor({color: '#dc2626'});
  } catch (e) { /* */ }
  return state;
}

async function stopRecording() {
  const state = await readState();
  if (!state || !state.active) {
    return {ok: false, error: 'no_active_session'};
  }
  const body = {
    token: state.token,
    steps: state.steps_buffer || [],
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
        const state = await readState();
        sendResponse(state || {active: false});
        break;
      }
      case 'append_step': {
        const ok = await appendStep(msg.step);
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
// server-side mapping is also process-local).
chrome.runtime.onInstalled.addListener(async () => {
  await clearState();
  try { chrome.action.setBadgeText({text: ''}); } catch (e) { /* */ }
});

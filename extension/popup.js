// PR-E — popup that surfaces the recorder's current state and lets the
// operator kick off a new recording without hunting for the TestForTge
// tab first.
//
// Two states:
//   * ACTIVE  — a recording is bound to a tab → show live status + Stop.
//   * IDLE    — no recording → show a "Site to record" field + Start +
//               an "Open TestForTge" shortcut.
//
// Why Start routes through the page (not a direct /api/recorder-session/
// start fetch from here): the endpoint resolves the project from the
// TestForTge *session cookie*, which is SameSite=Lax + HttpOnly. A
// cross-site fetch from this extension origin would not carry it, so the
// server couldn't tell which project to bind. Instead we open
// `<instance>/test-cases#tfg-record=<sut>`; the page (same-origin, with
// cookie + active project) pre-fills the recorder modal and auto-launches
// it. The /start call then happens on-page exactly as the manual flow.

const DEFAULT_BASE = 'https://testfortge.onrender.com';

function sendMsg(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => resolve(resp || {}));
  });
}

(async function init() {
  const statusLine = document.getElementById('status-line');
  const statusCard = document.getElementById('status-card');
  const stopBtn = document.getElementById('stop-btn');
  const idle = document.getElementById('idle-controls');
  const sutInput = document.getElementById('sut-url');
  const startBtn = document.getElementById('start-btn');
  const openBtn = document.getElementById('open-tfg-btn');
  const idleStatus = document.getElementById('idle-status');
  const baseDisplay = document.getElementById('base-display');
  const baseEdit = document.getElementById('base-edit');
  const baseEditRow = document.getElementById('base-edit-row');
  const baseInput = document.getElementById('base-url');
  const baseSave = document.getElementById('base-save');

  let baseUrl = '';

  function setIdleStatus(txt, isErr) {
    idleStatus.textContent = txt || '';
    idleStatus.className = 'hint' + (isErr ? ' err' : '');
  }

  function renderIdle() {
    statusCard.classList.remove('status-active');
    statusLine.innerHTML = 'No active recording.';
    stopBtn.hidden = true;
    stopBtn.disabled = true;
    idle.hidden = false;
    baseDisplay.textContent = baseUrl || '— not set —';
    if (!baseInput.value) baseInput.value = baseUrl || DEFAULT_BASE;
  }

  function renderActive(state) {
    statusCard.classList.add('status-active');
    const count = state.steps_buffer ? state.steps_buffer.length : 0;
    const elapsedMs = state.started_at ? (Date.now() - state.started_at) : 0;
    const elapsedSec = Math.floor(elapsedMs / 1000);
    // Deep-capture (CDP) status line — network + console counts, or a
    // note when the debugger couldn't attach.
    const tele = state.telemetry || {};
    const meta = tele.meta || {};
    const netN = (tele.network || []).length;
    const conN = (tele.console || []).length;
    let deep;
    if (meta.debugger_ok) {
      deep = `🔎 Deep capture: <strong>on</strong> — ` +
             `${netN} req, ${conN} console`;
    } else if (meta.debugger_error) {
      deep = `🔎 Deep capture: <span class="err">off</span> ` +
             `(debugger unavailable)`;
    } else {
      deep = `🔎 Deep capture: starting…`;
    }
    statusLine.innerHTML =
        `<strong>● Recording</strong><br>` +
        `Steps captured: <span class="step-count">${count}</span><br>` +
        `Elapsed: ${elapsedSec}s<br>` +
        `<small>${deep}</small>`;
    idle.hidden = true;
    stopBtn.hidden = false;
    stopBtn.disabled = false;
  }

  // Resolve the configured instance first (auto-learned from any prior
  // web-initiated recording, else falls back to the default below).
  const baseResp = await sendMsg({type: 'get_base_url'});
  baseUrl = (baseResp && baseResp.base_url) || '';

  // ── Live-control mode (Phase 2 active driver) takes precedence ────
  // When an MCP-driven control session is bound to a tab, surface it +
  // a Stop button and hide the recorder controls (they're a different
  // mode). The card polls so "last command" stays fresh.
  const controlCard = document.getElementById('control-card');
  const controlLine = document.getElementById('control-line');
  const stopControlBtn = document.getElementById('stop-control-btn');

  function renderControl(cs) {
    controlCard.hidden = false;
    statusCard.hidden = true;
    idle.hidden = true;
    stopBtn.hidden = true;
    const secs = cs.started_at
        ? Math.floor((Date.now() - cs.started_at) / 1000) : 0;
    controlLine.innerHTML =
        `<strong style="color:#7c3aed;">🕹 Live control active</strong><br>` +
        `<span class="hint" style="color:#6d28d9;">Driven by an MCP agent · ${secs}s</span>`;
  }

  const controlState = await sendMsg({type: 'get_control_state'});
  if (controlState && controlState.active) {
    renderControl(controlState);
    stopControlBtn.addEventListener('click', async () => {
      stopControlBtn.disabled = true;
      await sendMsg({type: 'stop_control'});
      window.close();
    });
    return;  // control mode owns the popup; skip recorder rendering
  }

  const state = await sendMsg({type: 'get_state'});
  if (state && state.active) {
    renderActive(state);
    setInterval(async () => {
      const fresh = await sendMsg({type: 'get_state'});
      if (fresh && fresh.active) renderActive(fresh);
      else renderIdle();
    }, 1000);
  } else {
    renderIdle();
  }

  // ── Stop (active state) ──────────────────────────────────────────
  stopBtn.addEventListener('click', () => {
    stopBtn.disabled = true;
    statusLine.textContent = 'Stopping and uploading…';
    chrome.runtime.sendMessage({type: 'stop_recording'}, (resp) => {
      if (resp && resp.ok) {
        statusLine.innerHTML = `✓ Uploaded. Opening review…`;
        setTimeout(() => window.close(), 800);
      } else {
        const err = (resp && resp.error) || 'unknown';
        statusLine.innerHTML =
            `<strong class="err">Stop failed:</strong><br><code>${err}</code>`;
        stopBtn.disabled = false;
      }
    });
  });

  // ── Start (idle state) — open the page flow ──────────────────────
  function effectiveBase() {
    return baseUrl || DEFAULT_BASE;
  }

  startBtn.addEventListener('click', () => {
    const sut = (sutInput.value || '').trim();
    if (!/^https?:\/\//i.test(sut)) {
      setIdleStatus('Enter an http:// or https:// site URL.', true);
      sutInput.focus();
      return;
    }
    const base = effectiveBase();
    // Hash (not query) so the SUT URL never lands in the page's server
    // logs or the test_cases route's request args.
    const target = base + '/test-cases#tfg-record=' + encodeURIComponent(sut);
    setIdleStatus('Opening TestForTge to launch the recorder…');
    chrome.tabs.create({url: target});
    setTimeout(() => window.close(), 500);
  });

  openBtn.addEventListener('click', () => {
    chrome.tabs.create({url: effectiveBase() + '/test-cases'});
    setTimeout(() => window.close(), 200);
  });

  // ── Instance configuration ───────────────────────────────────────
  baseEdit.addEventListener('click', () => {
    baseEditRow.hidden = !baseEditRow.hidden;
    if (!baseEditRow.hidden) baseInput.focus();
  });

  baseSave.addEventListener('click', async () => {
    const resp = await sendMsg({type: 'set_base_url', base_url: baseInput.value});
    if (resp && resp.ok) {
      baseUrl = resp.base_url;
      baseDisplay.textContent = baseUrl;
      baseEditRow.hidden = true;
      setIdleStatus('✓ Instance saved.');
    } else {
      setIdleStatus('That doesn’t look like a valid URL.', true);
    }
  });
})();

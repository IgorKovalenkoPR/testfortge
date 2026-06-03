// PR-E — popup that surfaces the recorder's current state.
//
// Reads session state from chrome.storage.session (set by background.js)
// and offers a Stop button. The Start path lives in TestForTge's
// /test-cases page — the popup is read-only + Stop-only by design, so
// the operator never has to remember which TestForTge instance to bind
// to (the token from /api/recorder-session/start carries it).

(async function init() {
  const statusLine = document.getElementById('status-line');
  const statusCard = document.getElementById('status-card');
  const stopBtn = document.getElementById('stop-btn');

  function renderIdle() {
    statusCard.classList.remove('status-active');
    statusLine.innerHTML = 'No active recording.';
    stopBtn.disabled = true;
  }

  function renderActive(state) {
    statusCard.classList.add('status-active');
    const count = state.steps_buffer ? state.steps_buffer.length : 0;
    const elapsedMs = state.started_at
        ? (Date.now() - state.started_at) : 0;
    const elapsedSec = Math.floor(elapsedMs / 1000);
    statusLine.innerHTML =
        `<strong>● Recording</strong><br>` +
        `Steps captured: <span class="step-count">${count}</span><br>` +
        `Elapsed: ${elapsedSec}s`;
    stopBtn.disabled = false;
  }

  // Pull current session state from the service worker.
  const state = await new Promise((resolve) => {
    chrome.runtime.sendMessage({type: 'get_state'}, (resp) => {
      resolve(resp || {});
    });
  });

  if (state && state.active) {
    renderActive(state);
    // Refresh elapsed every second while the popup is open.
    setInterval(async () => {
      const fresh = await new Promise((resolve) => {
        chrome.runtime.sendMessage({type: 'get_state'}, (resp) => {
          resolve(resp || {});
        });
      });
      if (fresh && fresh.active) renderActive(fresh);
      else renderIdle();
    }, 1000);
  } else {
    renderIdle();
  }

  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true;
    statusLine.textContent = 'Stopping and uploading…';
    chrome.runtime.sendMessage({type: 'stop_recording'}, (resp) => {
      if (resp && resp.ok) {
        statusLine.innerHTML =
            `✓ Uploaded. Opening review…`;
        // background.js opens the review tab; popup closes itself.
        setTimeout(() => window.close(), 800);
      } else {
        const err = (resp && resp.error) || 'unknown';
        statusLine.innerHTML =
            `<strong style="color:#dc2626;">Stop failed:</strong><br>` +
            `<code>${err}</code>`;
        stopBtn.disabled = false;
      }
    });
  });
})();

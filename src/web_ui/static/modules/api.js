/**
 * api.js — Server Communication Layer
 *
 * Handles all backend interactions through three primary channels:
 *  1. SSE (Server-Sent Events) — Real-time stream for logs and state updates.
 *  2. HTTP Polling — Regular snapshot synchronization as a consistency safety net.
 *  3. REST Fetch — Loading static configuration and miner metadata.
 *
 * The module implements a "Premium Sync" policy: Trusting the real-time SSE stream
 * during active actions and only reverting to Polling snapshots once the system settles.
 *
 * Depends on: state.js, utils.js
 */

/** 
 * Syncs active system alerts from the backend. 
 * Called during dashboard initialization to ensure persistent alerts survive refresh.
 */
async function fetchActiveAlerts() {
  try {
    const res = await fetch('/api/system/alerts');
    const data = await res.json();
    if (data.alerts && data.alerts.length > 0) {
      appendLog(new Date().toLocaleTimeString(), `\u001b[36m[Sync] Restoring ${data.alerts.length} active system alerts...\u001b[0m`);
      data.alerts.forEach(alert => {
        registerAlert(alert.id, {
          type: alert.alert_type,
          title: alert.title,
          message: alert.message,
          section: alert.section,
          persistent: alert.persistent
        });
      });
    }
  } catch (err) {
    appendLog('', `\u001b[31m[Sync] Failed to fetch active alerts: ${err.message || err}\u001b[0m`, 'ERROR');
  }
}

/** 
 * Establishes the persistent SSE connection for real-time updates.
 * Reconnects automatically with an exponential backoff on failure.
 */
function connectSSE() {
  const es = new EventSource('/api/logs');

  es.onopen = () => {
    if (sseRetryDelay > 1000) appendLog('', `\u001b[32m[SSE] Connection restored\u001b[0m`, 'DEBUG');
    sseRetryDelay = 1000;
  };

  es.onmessage = e => {
    try {
      const data = JSON.parse(e.data);
      
      // 1. SYSTEM LOGS
      if (data.type === 'log') {
        appendLog(data.time, data.message, data.level);
      } 
      
      // 2. UI STATE UPDATES
      else if (data.type === 'ui_reset_cards') {
        // Shield: Protect newly started action transitions from late reset signals
        const holdsData = Object.keys(state.walletDetails).length > 0 || Object.keys(state.minerJourneys).length > 0;
        if (holdsData) return;

        resetUIStateForNewAction();
        if (typeof renderDataCards === 'function') renderDataCards();
      } else if (data.type === 'action_done') {
        // Cooldown: buttons remain locked until the final data synchronization occurs
        state.waitingForSync = true;
        setAppStatus('done');

        const actionName = state.actionNames[data.action] || data.action || 'Action';
        let msg = `Action "${actionName}" completed`;
        let type = data.status || 'success';
        
        if (type === 'partial') { msg = `Action "${actionName}" partially successful`; type = 'warning'; }
        else if (type === 'error') { msg = `Action "${actionName}" failed`; }
        
        showToast(msg, type);
        pollStatus(); // Immediate poll to capture final status strings
      }
      
      // 3. BLOCKCHAIN DATA SYNC
      else if (data.type === 'miner_data_update') {
        if (data.miner_data) {
          for (const [addr, info] of Object.entries(data.miner_data)) {
            state.walletMiners[addr.toLowerCase()] = info;
            state.minersLoading[addr.toLowerCase()] = false;
          }
          state.lastMinerSync = Date.now();
          renderMinersOverview();
          showToast('Balances and inventories updated', 'success');

          // Release the UI lock once the data is confirmed fresh
          if (state.waitingForSync) {
            state.waitingForSync = false;
            setRunning(false);
            setAppStatus('idle');
          }
        }
      }

      // 4. ALERTS & SAFETY
      else if (data.type === 'debt_wallets_blocked') {
        const names = (data.wallets || []).join(', ');
        showToast(`Debt safety: ${names} excluded from action`, 'warning');
      }
      else if (data.type === 'rate_limit_alert') {
        const retryMsg = data.retry_after ? ` (retry in ${data.retry_after}s)` : '';
        showToast(`API Rate Limit: ${data.message}${retryMsg}`, 'error');
      }
      else if (data.type === 'polling_alert') {
        showToast(data.message, data.level || 'info');
      }
      else if (data.type === 'system_alert') {
        registerAlert(data.id, {
          type: data.alert_type,
          title: data.title,
          message: data.message,
          section: data.section,
          persistent: data.persistent
        });
      }
      else if (data.type === 'remove_system_alert') {
        removeAlert(data.id);
      }
    } catch (err) { 
      appendLog('', `\u001b[31m[SSE] Event parse error: ${err.message || err}\u001b[0m`, 'ERROR'); 
    }
  };

  es.onerror = () => {
    es.close();
    const currentDelay = sseRetryDelay;
    sseRetryDelay = Math.min(sseRetryDelay * 2, MAX_SSE_RETRY_DELAY);
    appendLog('', `\u001b[31m[SSE] Connection lost. Retrying in ${currentDelay / 1000}s...\u001b[0m`, 'INFO');
    showToast(`Server connection lost. Retrying in ${currentDelay / 1000}s...`, 'error');
    setTimeout(connectSSE, currentDelay);
  };
}

/* ── Status Polling (Consistency Guard) ───────────────────────── */

/** 
 * Periodically fetches a snapshot of the full orchestrator state.
 * Uses an "Anti-Overwriting Shield" to prevent stale snapshots from wiping out
 * faster real-time SSE updates during action transitions.
 */
async function pollStatus() {
  if (state.isTransitioning) return; // Prevent stale polls from interfering with action start
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    const oldStr = JSON.stringify({ s: state.walletStatuses, d: state.walletDetails, j: state.minerJourneys, g: state.genericCards });
    const freshDetails = data.wallet_details || {};
    const freshJourneys = data.miner_journeys || {};

    const newStr = JSON.stringify({ s: data.wallet_statuses, d: freshDetails, j: freshJourneys, g: data.generic_cards });

    if (oldStr !== newStr) {
      // Shield: If the poll returns empty data while we are running, the SSE stream is likely ahead.
      // We ignore the "empty snapshot" to prevent the disappearing-cards flicker.
      const isActuallyEmpty = Object.keys(freshDetails).length === 0 && Object.keys(freshJourneys).length === 0;
      const currentlyHasData = Object.keys(state.walletDetails).length > 0 || Object.keys(state.minerJourneys).length > 0;
      
      if (data.status === 'running' && isActuallyEmpty && currentlyHasData) {
        setAppStatus(data.status);
        return;
      }

      state.walletStatuses = data.wallet_statuses || {};
      state.walletDetails = freshDetails;
      state.minerJourneys = freshJourneys;
      state.genericCards = data.generic_cards || {};
      
      updateSidebarBadges();
      renderDataCards();
    }

    setAppStatus(data.status);
    if (data.status === 'running') {
      clearTimeout(state.pollInterval);
      state.pollInterval = setTimeout(pollStatus, 1000);
    }
  } catch (err) { 
    appendLog('', `\u001b[31m[Poll] Snapshot sync failed: ${err.message || err}\u001b[0m`, 'ERROR'); 
  }
}

/* ── UI Logic ────────────────────────────────────────────────── */

/** Updates the visual status pill and text in the dashboard header. */
function setAppStatus(status) {
  const labels = { idle: 'Idle', running: 'Processing...', done: 'Done' };
  statusText.textContent = labels[status] || status;
  statusDot.className = `status-dot ${status}`;
}

/** 
 * Locks or unlocks the application controls toggling the 'running' state.
 * Prevents multiple simultaneous actions and handles the post-action cooldown.
 */
function setRunning(val) {
  state.running = val;
  if (typeof updateInteractiveState === 'function') updateInteractiveState();
  if (val) { clearTimeout(state.pollInterval); state.pollInterval = setTimeout(pollStatus, 600); }
}

/* ── App Config & Metadata ───────────────────────────────────── */

async function loadMinerTypes() {
  try {
    const res = await fetch('/api/miner_types');
    state.minerTypes = await res.json();
    state._nftToType = {};
    for (const [idx, mt] of Object.entries(state.minerTypes)) {
      if (mt.nftContract) state._nftToType[mt.nftContract.toLowerCase()] = { ...mt, idx };
    }
  } catch (err) { appendLog('', `\u001b[31m[API] Failed to load miner types: ${err.message || err}\u001b[0m`, 'ERROR'); }
}

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    state.config = data;
    if (data.action_names) state.actionNames = data.action_names;
    if (data.action_keys) state.actionKeys = data.action_keys;
    
    // Inject images
    const logoImg = document.querySelector('.logo-img');
    if (logoImg) logoImg.src = state.config.hcash_logo_url;
  } catch (err) { appendLog('', `\u001b[31m[API] Config load failed: ${err.message || err}\u001b[0m`, 'ERROR'); }
}

function getMinerLabel(nftContract, minerIndex) {
  const mt = nftContract ? state._nftToType?.[nftContract.toLowerCase()] : state.minerTypes?.[String(minerIndex)];
  if (!mt) return null;
  return {
    name: mt.nft_name || mt.name || `Type #${mt.idx || minerIndex}`,
    image: mt.nft_image || '',
    category: mt.category || 'miner'
  };
}

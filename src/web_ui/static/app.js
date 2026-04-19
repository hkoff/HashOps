/**
 * app.js — Application Orchestrator
 *
 * This is the central brain that synchronizes all specialized modules:
 *  - Initialization (loader.js -> initDashboard)
 *  - Action Orchestration (runAction)
 *  - Event handling & Button wiring
 *
 * It manages the lifecycle of transactions, from the initial visual transition
 * and state-clearing phase to the final server-side execution.
 *
 * Architecture:
 *   state.js    → Single source of truth (Data)
 *   utils.js    → Passive display helpers (UI)
 *   api.js      → Active networking & sync (Data Flow)
 */

/* ── Dashboard Lifecycle ─────────────────────────────────────── */

/**
 * Initializes the dashboard once the engine's boot sequence is complete.
 * Wires up data channels and restores any cached context.
 */
window.initDashboard = async function (preloadedResults = null) {
  appendLog(new Date().toLocaleTimeString(), '\u001b[95m── Dashboard Initialization ──\u001b[0m');

  try {
    // 1. Core Config (Wait for these as they define the UI structure)
    await loadConfig();
    await loadMinerTypes();

    // 2. State Restoration (Preloaded by the loader to avoid redundant RPCs)
    if (preloadedResults) {
      const dataAge = (Date.now() / 1000) - (preloadedResults.timestamp || 0);
      const isFresh = dataAge < 150; // Use data only if under 2.5 minutes old

      if (isFresh) {
        if (preloadedResults.batch_data) {
          Object.entries(preloadedResults.batch_data).forEach(([addr, info]) => {
            state.walletMiners[addr.toLowerCase()] = info;
          });
          state.lastMinerSync = preloadedResults.timestamp * 1000;
          showToast('Balances & Facilities pre-loaded from engine', 'success');
        }

        if (preloadedResults.gas_price_gwei) {
          const gasBtn = document.getElementById('refbar-gas-btn');
          if (gasBtn) gasBtn.textContent = `Gas ${formatDecimal(preloadedResults.gas_price_gwei, 2)} Gwei`;
          state.lastGasSync = preloadedResults.timestamp * 1000;
          state.gasInitialized = true;
          appendLog(new Date().toLocaleTimeString(), `⛽ Gas: \u001b[96m${formatDecimal(preloadedResults.gas_price_gwei, 2)} Gwei\u001b[0m (cached)`);
        }
        appendLog(new Date().toLocaleTimeString(), '\u001b[92m✓ Engine data pre-loaded successfully\u001b[0m');
      } else {
        appendLog(new Date().toLocaleTimeString(), `\u001b[33m⚠ Engine data is stale — triggering fresh sync...\u001b[0m`);
      }
    }

    // 3. Functional Initialization
    await loadWallets();
    if (!state.gasInitialized) {
      state.gasInitialized = true;
      await checkGasPrice();
    }

    // 4. Persistence Channels
    if (typeof fetchActiveAlerts === 'function') await fetchActiveAlerts();
    await connectSSE();
    await pollStatus();

    appendLog(new Date().toLocaleTimeString(), '\u001b[95m── Dashboard Ready ──\u001b[0m');
  } catch (err) {
    showToast('Dashboard initialization failed', 'error');
    appendLog(new Date().toLocaleTimeString(), `\u001b[31m✗ Dashboard init error: ${err.message || err}\u001b[0m`, 'ERROR');
  }
};

/* ── UI Transitions ──────────────────────────────────────────── */

/** Clears all centralized runtime data associated with actions. */
window.resetUIStateForNewAction = function() {
  state.walletStatuses = {};
  state.walletDetails = {};
  state.minerJourneys = {};
  state.genericCards = {};
  if (window.resetSidebarBadges) window.resetSidebarBadges();
};

/** Manual UI clear triggered by the operator. */
window.clearActionView = function() {
  resetUIStateForNewAction();
  if (typeof renderDataCards === 'function') renderDataCards();
};

/* ── Action Dispatching ──────────────────────────────────────── */

/**
 * Executes a batch action across selected wallets.
 *
 * Implements a "Transitioning" phase where existing visual state is 
 * cleared and animated out BEFORE the server request is dispatched.
 */
async function runAction(action, customPayload = null, customTargetCount = null) {
  // Prevent double-execution or interruption during transitions
  if (state.running || state.waitingForSync) return;
  
  if (action !== state.actionKeys.BATCH_MINERS && !state.selected.size) {
    showToast('Select at least one wallet', 'warning');
    return;
  }

  // Set transition mode to ignore stale polls until backend responds
  state.isTransitioning = true;

  // 1. Visual Cleanup Phase
  const hasActiveCards = Object.keys(state.walletDetails).length > 0 || Object.keys(state.minerJourneys).length > 0;
  
  // We trigger the exit animations and wait for them to finish (~600ms) before populating the UI with the new action's data stream.
  if (hasActiveCards) {
    clearActionView(); // Explicitly trigger exit animations
    await new Promise(resolve => setTimeout(resolve, state.transitionDelay + 50)); 
  }

  let bodyData;
  let targetCount = customTargetCount !== null ? customTargetCount : state.selected.size;

  if (action === state.actionKeys.CLAIM || action === state.actionKeys.DISPATCH_GAS) {
    bodyData = { action, wallets: [...state.selected] };
  } else if (action === state.actionKeys.BATCH_MINERS) {
    bodyData = customPayload;
  }

  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyData)
    });
    
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || 'Server error', 'error');
      // Unlock on error to allow immediate retry
      state.isTransitioning = false;
      state.waitingForSync = false;
      setRunning(false);
      return;
    }

    // Successful launch!
    state.isTransitioning = false;
    setRunning(true);
    setAppStatus('running');
    
    const actionNameDisplay = state.actionNames[action] || action;
    showToast(`${actionNameDisplay} started on ${targetCount} wallet(s)`, 'success');
    
    // Automatically open the Action View panel if it was closed
    if (!state.panelCardsOpen) toggleMainPanel('cards', true);
  } 
  catch (err) {
    appendLog('', `\u001b[31m[API] runAction failed: ${err.message || err}\u001b[0m`, 'ERROR');
    showToast('Connection error', 'error');
    state.waitingForSync = false;
    setRunning(false);
  }
}

window.runAction = runAction;

/* ── DOM Events ──────────────────────────────────────────────── */

// Action button wiring
document.getElementById('btn-claim')?.addEventListener('click', () => runAction(state.actionKeys.CLAIM));
document.getElementById('btn-dispatch-gas')?.addEventListener('click', () => runAction(state.actionKeys.DISPATCH_GAS));

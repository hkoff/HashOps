/**
 * layout.js — Panel Layout, Drag-Resize & Privacy Mode
 *
 * Manages the 3-panel main content area:
 *  • Miners Overview (top, open by default)
 *  • Wallet Cards (middle, opens when an action starts)
 *  • Logs Terminal (bottom, collapsed by default)
 *
 * Also handles:
 *  • Drag-to-resize between panels
 *  • Privacy blur mode (toggle via header button)
 *
 * Depends on: state.js, utils.js
 */

/* ── UI Elements Cache ────────────────────────────────────────── */
/**
 * Cached DOM references to avoid expensive repeated lookups.
 * Populated on DOMContentLoaded.
 */
const UI = {
  pMiners: null, pCards: null, pLogs: null,
  listMiners: null, listCards: null, termLogs: null,
  resizerMain: null, resizerLogsBottom: null,
  iconMiners: null, iconCards: null, iconLogs: null,
  btnPrivacy: null, privacyIcon: null
};

function initDOMReferences() {
  UI.pMiners = document.getElementById('miners-overview-section');
  UI.pCards = document.getElementById('cards-section');
  UI.pLogs = document.getElementById('logs-section');
  UI.listMiners = document.getElementById('miners-overview-list');
  UI.listCards = document.getElementById('wallet-cards');
  UI.termLogs = document.getElementById('log-terminal');
  UI.resizerMain = document.getElementById('resizer-main');
  UI.resizerLogsBottom = document.getElementById('resizer-logs-bottom');
  UI.iconMiners = document.getElementById('toggle-miners-icon');
  UI.iconCards = document.getElementById('toggle-cards-icon');
  UI.iconLogs = document.getElementById('toggle-icon');
  UI.btnPrivacy = document.getElementById('btn-toggle-privacy');
  UI.privacyIcon = document.getElementById('privacy-icon');
}

/* ── UI Helpers ──────────────────────────────────────────────── */

/** Transition duration for inner content show/hide (ms). Matches CSS .closing animation. */
const CONTENT_TRANSITION_MS = 400;

/**
 * Show or hide an inner content element with a smooth transition.
 *
 * Coordination:
 * - SHOW: Removes .hidden, forces reflow, then removes .closing.
 * - HIDE: Adds .closing (triggers CSS animation), waits for duration, then adds .hidden.
 *
 * @param {HTMLElement} el - The element to transition.
 * @param {boolean} shouldHide - Whether to hide or show.
 */
function setHiddenWithTransition(el, shouldHide) {
  if (!el) return;
  if (!shouldHide) {
    // SHOW: Unhide first, force reflow, then let CSS animate in if needed
    el.classList.remove('hidden');
    void el.offsetHeight; // Force reflow
    el.classList.remove('closing');
  } else {
    // HIDE: Add closing class (fadeOut animation), then set display:none after duration
    if (!el.classList.contains('hidden') && !el.classList.contains('closing')) {
      el.classList.add('closing');
      setTimeout(() => {
        if (el.classList.contains('closing')) {
          el.classList.add('hidden');
          el.classList.remove('closing');
        }
      }, CONTENT_TRANSITION_MS);
    }
  }
}

/* ── Privacy Mode (Blur) ─────────────────────────────────────── */

/**
 * Toggle global privacy state and persist to localStorage.
 */
function togglePrivacy() {
  state.privacyEnabled = !state.privacyEnabled;
  localStorage.setItem('privacy_mode', state.privacyEnabled);
  applyPrivacy();
}

/**
 * Sync privacy mode classes and icons based on current state.
 */
function applyPrivacy() {
  const btn = UI.btnPrivacy;
  const icon = UI.privacyIcon;
  if (state.privacyEnabled) {
    document.body.classList.add('privacy-mode');
    if (btn) btn.classList.add('active');
    if (icon) icon.setAttribute('name', 'eye-off');
  } else {
    document.body.classList.remove('privacy-mode');
    if (btn) btn.classList.remove('active');
    if (icon) icon.setAttribute('name', 'eye');
  }
}

window.togglePrivacy = togglePrivacy;

/* ── 3-Panel Flex Layout ─────────────────────────────────────── */

/**
 * Apply the current panel open/closed state to the DOM.
 * Coordinates sequential animations between parent sections and inner lists.
 *
 * OPEN sequence:  Remove .collapsed → wait for section expand → reveal content.
 * CLOSE sequence: Hide content → wait for fade-out → add .collapsed.
 */
function applyPanelsLayout() {
  /**
   * Internal helper to sync a panel's open/closed state.
   */
  function syncPanel(pEl, cEl, iconEl, isOpen) {
    if (!pEl || !cEl) return;

    if (isOpen) {
      // OPEN: Expand the section first, then reveal inner content
      if (pEl.classList.contains('collapsed')) {
        pEl.classList.remove('collapsed');
        if (iconEl) iconEl.classList.add('rotated');
        setTimeout(() => {
          setHiddenWithTransition(cEl, false);
        }, CONTENT_TRANSITION_MS);
      } else {
        setHiddenWithTransition(cEl, false);
        if (iconEl) iconEl.classList.add('rotated');
      }
    } else {
      // CLOSE: Hide content first, then collapse the section
      if (!pEl.classList.contains('collapsed')) {
        setHiddenWithTransition(cEl, true);
        if (iconEl) iconEl.classList.remove('rotated');
        setTimeout(() => {
          pEl.classList.add('collapsed');
        }, CONTENT_TRANSITION_MS);
      }
    }
  }

  // 1. Panel states sync
  syncPanel(UI.pMiners, UI.listMiners, UI.iconMiners, state.panelMinersOpen);
  syncPanel(UI.pCards, UI.listCards, UI.iconCards, state.panelCardsOpen);
  syncPanel(UI.pLogs, UI.termLogs, UI.iconLogs, state.panelLogsOpen);

  // 2. Specialized Layout: "Expanded Full" for cards when everything else is closed
  const onlyCardsOpen = state.panelCardsOpen && !state.panelMinersOpen && !state.panelLogsOpen;
  if (UI.pCards) {
    UI.pCards.classList.toggle('expanded-full', onlyCardsOpen);
  }

  // 3. Resizers visibility
  if (UI.resizerMain) {
    const showMainResizer = state.panelMinersOpen && (state.panelCardsOpen || state.panelLogsOpen);
    UI.resizerMain.classList.toggle('hidden', !showMainResizer);
  }

  if (UI.resizerLogsBottom) {
    setHiddenWithTransition(UI.resizerLogsBottom, !state.panelLogsOpen);
  }
}

/* ── Panel Toggles ───────────────────────────────────────────── */

/**
 * Toggle a main panel (Miners or Cards) and refresh layout.
 */
window.toggleMainPanel = function (panelName, forceOpen = false) {
  if (panelName === 'miners') {
    state.panelMinersOpen = forceOpen ? true : !state.panelMinersOpen;
  } else if (panelName === 'cards') {
    state.panelCardsOpen = forceOpen ? true : !state.panelCardsOpen;
  }
  applyPanelsLayout();
};

/**
 * Toggle the logs panel and refresh layout.
 */
window.toggleLogs = function () {
  state.panelLogsOpen = !state.panelLogsOpen;
  applyPanelsLayout();
};

/* ── DOMContentLoaded: Drag-Resize & Initial Setup ───────────── */

window.addEventListener('DOMContentLoaded', () => {
  // Initialize our cache
  initDOMReferences();

  // Resize state
  let currentResizingPanel = null;
  let isResizing = false;
  let startY = 0;
  let startHeight = 0;

  // Main resizer: drag to adjust miners panel height
  if (UI.resizerMain) {
    UI.resizerMain.addEventListener('mousedown', (e) => {
      isResizing = true;
      document.body.classList.add('is-resizing');
      currentResizingPanel = 'miners';
      startY = e.clientY;
      startHeight = UI.pMiners.offsetHeight;
      document.body.style.cursor = 'row-resize';
      e.preventDefault();
    });
  }

  // Logs bottom resizer: click to expand logs by 400px
  if (UI.resizerLogsBottom) {
    UI.resizerLogsBottom.addEventListener('click', () => {
      if (!UI.pLogs) return;
      const currentHeight = UI.pLogs.offsetHeight;
      const newHeight = currentHeight + 400;
      document.documentElement.style.setProperty('--logs-height', `${newHeight}px`);
      
      setTimeout(() => {
        UI.pLogs.scrollIntoView({ behavior: 'smooth', block: 'end' });
      }, 100);
    });
  }

  // Global mouse move for resizing logic
  document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;

    let deltaY = e.clientY - startY;
    let newHeight = startHeight + deltaY;
    if (newHeight < 50) newHeight = 50;

    const root = document.documentElement;
    if (currentResizingPanel === 'miners') {
      root.style.setProperty('--miners-height', `${newHeight}px`);
    } else if (currentResizingPanel === 'cards') {
      root.style.setProperty('--cards-height', `${newHeight}px`);
    } else if (currentResizingPanel === 'logs') {
      root.style.setProperty('--logs-height', `${newHeight}px`);
    }
  });

  // Global mouse up to stop resizing
  document.addEventListener('mouseup', () => {
    if (isResizing) {
      isResizing = false;
      currentResizingPanel = null;
      document.body.style.cursor = 'default';
      document.body.classList.remove('is-resizing');
    }
  });

  // Initial UI state sync
  applyPanelsLayout();
  applyPrivacy();

  if (UI.btnPrivacy) {
    UI.btnPrivacy.addEventListener('click', togglePrivacy);
  }
});

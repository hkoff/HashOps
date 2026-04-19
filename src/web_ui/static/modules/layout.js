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

/* ── Panel State ─────────────────────────────────────────────── */
let logsVisible = false;

/* ── UI Helpers ──────────────────────────────────────────────── */
function setHiddenWithTransition(el, shouldHide) {
  if (!el) return;
  if (!shouldHide) {
    el.classList.remove('hidden');
    el.classList.remove('closing');
  } else {
    // Only trigger transition if not already hidden or closing
    if (!el.classList.contains('hidden') && !el.classList.contains('closing')) {
      el.classList.add('closing');
      setTimeout(() => {
        if (el.classList.contains('closing')) {
          el.classList.add('hidden');
          el.classList.remove('closing');
        }
      }, 500);
    }
  }
}

/* ── Privacy Mode (Blur) ─────────────────────────────────────── */
function togglePrivacy() {
  state.privacyEnabled = !state.privacyEnabled;
  localStorage.setItem('privacy_mode', state.privacyEnabled);
  applyPrivacy();
}

function applyPrivacy() {
  const btn = document.getElementById('btn-toggle-privacy');
  const icon = document.getElementById('privacy-icon');
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
 * Uses flex-basis to distribute available space:
 *  - Open panels get `flex: 1 0 400px` (minimum 400px, grow to fill)
 *  - Closed panels get `flex: 0 0 auto` (header only)
 *  - Resizers are shown only between adjacent open panels
 */
function applyPanelsLayout() {
  const pMiners = document.getElementById('miners-overview-section');
  const pCards = document.getElementById('cards-section');
  const pLogs = document.getElementById('logs-section');

  const resizerMain = document.getElementById('resizer-main');
  const resizerLogsBottom = document.getElementById('resizer-logs-bottom');

  const listMiners = document.getElementById('miners-overview-list');
  const listCards = document.getElementById('wallet-cards');
  const termLogs = document.getElementById('log-terminal');

  const iconMiners = document.getElementById('toggle-miners-icon');
  const iconCards = document.getElementById('toggle-cards-icon');
  const iconLogs = document.getElementById('toggle-icon');

  // Miners panel
  if (state.panelMinersOpen) {
    pMiners.classList.remove('collapsed');
    setHiddenWithTransition(listMiners, false);
    if (iconMiners) iconMiners.classList.add('rotated');
  } else {
    pMiners.classList.add('collapsed');
    setHiddenWithTransition(listMiners, true);
    if (iconMiners) iconMiners.classList.remove('rotated');
  }

  // Cards panel
  if (state.panelCardsOpen) {
    pCards.classList.remove('collapsed');
    setHiddenWithTransition(listCards, false);
    if (iconCards) iconCards.classList.add('rotated');
  } else {
    pCards.classList.add('collapsed');
    setHiddenWithTransition(listCards, true);
    if (iconCards) iconCards.classList.remove('rotated');
  }

  // Logs panel
  if (logsVisible) {
    pLogs.classList.remove('collapsed');
    setHiddenWithTransition(termLogs, false);
    if (iconLogs) iconLogs.classList.add('rotated');
  } else {
    pLogs.classList.add('collapsed');
    setHiddenWithTransition(termLogs, true);
    if (iconLogs) iconLogs.classList.remove('rotated');
  }

  // Expanded full logic for cards-section
  const onlyCardsOpen = state.panelCardsOpen && !state.panelMinersOpen && !logsVisible;
  if (onlyCardsOpen) {
    pCards.classList.add('expanded-full');
  } else {
    pCards.classList.remove('expanded-full');
  }

  // Flex distribution — pure native Flex, no % or vh
  pMiners.style.flex = state.panelMinersOpen ? `1 0 400px` : '';
  pCards.style.flex = state.panelCardsOpen ? `0 0 auto` : '';
  pLogs.style.flex = logsVisible ? `0 0 400px` : '';

  // Resizer visibility
  if (resizerMain) {
    setHiddenWithTransition(resizerMain, !(state.panelMinersOpen && (state.panelCardsOpen || logsVisible)));
  }
}

/* ── Panel Toggles ───────────────────────────────────────────── */
window.toggleMainPanel = function (panelName, forceOpen = false) {
  if (panelName === 'miners') {
    state.panelMinersOpen = forceOpen ? true : !state.panelMinersOpen;
  } else if (panelName === 'cards') {
    state.panelCardsOpen = forceOpen ? true : !state.panelCardsOpen;
  }
  applyPanelsLayout();
};

window.toggleLogs = function () {
  logsVisible = !logsVisible;
  applyPanelsLayout();
};

/* ── DOMContentLoaded: Drag-Resize & Initial Setup ───────────── */
window.addEventListener('DOMContentLoaded', () => {
  // Resize state (scoped to this listener)
  let currentResizingPanel = null;
  let isResizing = false;
  let startY = 0;
  let startHeight = 0;

  const resizerMain = document.getElementById('resizer-main');
  const resizerLogsBottom = document.getElementById('resizer-logs-bottom');

  // Main resizer: drag to adjust miners panel height
  if (resizerMain) {
    resizerMain.addEventListener('mousedown', (e) => {
      isResizing = true;
      document.body.classList.add('is-resizing');
      currentResizingPanel = 'miners';
      startY = e.clientY;
      startHeight = document.getElementById('miners-overview-section').offsetHeight;
      document.body.style.cursor = 'row-resize';
      e.preventDefault();
    });
  }

  // Logs bottom resizer: click to expand logs by 400px
  if (resizerLogsBottom) {
    resizerLogsBottom.addEventListener('click', () => {
      const pLogs = document.getElementById('logs-section');
      const h = pLogs.offsetHeight;
      pLogs.style.flex = `0 0 ${h + 400}px`;
    });
  }

  document.addEventListener('mousemove', (e) => {
    if (!isResizing) return;

    const pMiners = document.getElementById('miners-overview-section');
    const pCards = document.getElementById('cards-section');
    const pLogs = document.getElementById('logs-section');

    let deltaY = e.clientY - startY;
    let newHeight = startHeight + deltaY;
    if (newHeight < 50) newHeight = 50;

    if (currentResizingPanel === 'miners') {
      pMiners.style.flex = `0 0 ${newHeight}px`;
    } else if (currentResizingPanel === 'cards') {
      pCards.style.flex = `0 0 ${newHeight}px`;
    } else if (currentResizingPanel === 'logs') {
      pLogs.style.flex = `0 0 ${newHeight}px`;
    }
  });

  document.addEventListener('mouseup', () => {
    if (isResizing) {
      isResizing = false;
      currentResizingPanel = null;
      document.body.style.cursor = 'default';
      document.body.classList.remove('is-resizing');
    }
  });

  // Initial layout + privacy state
  applyPanelsLayout();
  applyPrivacy();

  const btnPrivacy = document.getElementById('btn-toggle-privacy');
  if (btnPrivacy) btnPrivacy.addEventListener('click', togglePrivacy);
});

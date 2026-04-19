/**
 * sidebar.js — Wallet Sidebar
 *
 * Renders the left-hand wallet list, manages multi-selection, and updates per-wallet status badges during actions.
 *
 * Depends on: state.js, utils.js, api.js (getMinerLabel)
 * Runtime deps: miners.js → loadBatchWalletMiners()
 */

/* ── Wallet Loading ──────────────────────────────────────────── */
async function loadWallets(force = false) {
  try {
    if (!force && state.wallets.length > 0) return;
    const res = await fetch('/api/wallets');
    state.wallets = await res.json();
    renderSidebar();

    // Ultra-fast grouped loading via Multicall3 (anti-spam RPC integrated in backend)
    const addresses = state.wallets.map(w => w.address);
    loadBatchWalletMiners(addresses);
  } catch (err) {
    appendLog('', `\u001b[31m[API] loadWallets failed: ${err.message || err}\u001b[0m`, 'ERROR');
    showToast('Unable to load the wallets', 'error');
  }
}

/* ── Sidebar Rendering ───────────────────────────────────────── */
function renderSidebar() {
  const activeIds = [];
  
  for (const w of state.wallets) {
    const id = `sidebar-wallet-${w.name}`;
    activeIds.push(id);
    
    const short = w.address.slice(0, 6) + '...' + w.address.slice(-4);
    
    // Check for debt to include it in the template (for morphDOM stability)
    const info = state.walletMiners[w.address.toLowerCase()];
    const threshold = state.config.debt_threshold !== undefined ? state.config.debt_threshold : -0.001;
    const isLoading = state.minersLoading && state.minersLoading[w.address.toLowerCase()];
    const hasDebt = !isLoading && info && !info.error && info.net_claimable < threshold;

    const html = `
      <div class="wallet-checkbox"><span class="wallet-checkbox-icon">✓</span></div>
      <div class="wallet-info">
        <div class="wallet-name">
          ${w.name}${w.is_main ? '<span class="badge-main">MAIN</span>' : ''}
          ${hasDebt ? '<span class="wallet-debt-badge">⚠ DEBT</span>' : ''}
        </div>
        <div class="wallet-addr">
          <a href="${state.config.debank_url}${w.address}" target="_blank" onclick="event.stopPropagation()" class="addr-link" title="Debank Profile">
            <span class="privacy-data">${short}</span>
          </a>
        </div>
      </div>
      <span class="wallet-status-badge ws-idle" id="ws-${w.name}">—</span>`;

    const el = upsertElement(walletsList, id, `wallet-card${hasDebt ? ' wallet-debt' : ''}`, html);
    el.dataset.name = w.name;
    // Ensure the click listener is only added once if it's a new element (upsertElement handles existing ones)
    if (!el.onclick) el.onclick = () => toggleWallet(w.name);
  }
  
  cleanupElements(walletsList, activeIds);
  updateSelectionUI();
}

/* ── Selection Management ────────────────────────────────────── */
function toggleWallet(name) {
  state.selected.has(name) ? state.selected.delete(name) : state.selected.add(name);
  updateSelectionUI();
}

function updateSelectionUI() {
  document.querySelectorAll('.wallet-card').forEach(c =>
    c.classList.toggle('selected', state.selected.has(c.dataset.name))
  );
}

/* ── Status Badges ───────────────────────────────────────────── */
window.resetSidebarBadges = function() {
  document.querySelectorAll('.wallet-status-badge').forEach(b => {
    b.innerHTML = '—';
    b.className = 'wallet-status-badge ws-idle';
  });
};

function updateSidebarBadges() {
  const labels = {
    pending: '—',
    running: '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon>',
    success: '✓ OK',
    skipped: '⊘',
    partial: '⚠',
    warning: '⚠',
    error: '✗'
  };
  for (const [name, status] of Object.entries(state.walletStatuses)) {
    const b = document.getElementById(`ws-${name}`);
    if (b) {
      const newHtml = labels[status] || status;
      const newClass = `wallet-status-badge status-${status || 'idle'}`;
      if (b.innerHTML !== newHtml) b.innerHTML = newHtml;
      if (b.className !== newClass) b.className = newClass;
    }
  }
}

/* ── Select All / Deselect All ───────────────────────────────── */
let allSelected = false;
document.getElementById('select-all-btn').addEventListener('click', () => {
  if (allSelected) { state.selected.clear(); document.getElementById('select-all-btn').textContent = 'Select all'; }
  else { state.wallets.forEach(w => state.selected.add(w.name)); document.getElementById('select-all-btn').textContent = 'Deselect all'; }
  allSelected = !allSelected;
  updateSelectionUI();
});
document.getElementById('select-all-btn').textContent = 'Select all';

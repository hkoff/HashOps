/**
 * miners.js — Miners Inventory
 *
 * Everything related to loading, caching, and displaying per-wallet miner inventories (placed + owned NFTs):
 *  • Single and batch wallet loading via Multicall3
 *  • Gas price checking
 *  • Miner types cache refresh
 *  • Expand/collapse wallet detail sections
 *  • Full HTML rendering of the Inventory panel
 *  • Claim totals aggregation for the sidebar action button
 *
 * Depends on: state.js, utils.js, api.js (getMinerLabel, loadMinerTypes)
 */

/* ─────────────────────────────────────────────────────────────────
   DATA LOADING
   ───────────────────────────────────────────────────────────────── */
   
const uiCooldowns = { wallets: 0, cache: 0, gas: 0 };

function checkCooldown(actionType) {
  const now = Date.now();
  if (now - uiCooldowns[actionType] < 15000) {
    const remaining = Math.ceil((15000 - (now - uiCooldowns[actionType])) / 1000);
    showToast(`Please wait ${remaining}s before refreshing again`, 'warning');
    return false;
  }
  uiCooldowns[actionType] = now;
  return true;
}

/** Load miners for a single wallet address. */
async function loadWalletMiners(address, isRefresh = false) {
  const addrLow = address.toLowerCase();
  if (state.minersLoading[addrLow]) return;
  
  // Si c'est un chargement initial et qu'on a déjà les données, on skip
  if (!isRefresh && state.walletMiners[addrLow]) { renderMinersOverview(); return; }
  
  state.minersLoading[addrLow] = true;
  try {
    const res = await fetch(`/api/miners/${address}`);
    const data = await res.json();
    state.walletMiners[addrLow] = res.ok ? data : { error: data.error };
  } catch (err) {
    appendLog('', `\u001b[31m[API] Single wallet load failed: ${err.message || err}\u001b[0m`, 'ERROR');
    state.walletMiners[addrLow] = { error: 'Network error' };
  } finally {
    state.minersLoading[addrLow] = false;
    renderMinersOverview();
  }
}

/**
 * Load miners for multiple wallets in a single backend call.
 * The backend uses Multicall3 to batch all RPC reads, avoiding per-wallet rate-limiting issues.
 */
async function loadBatchWalletMiners(addresses) {
  if (!addresses.length) return true;
  // If data is fresh (< 2.5 min) and we have everything, skip refresh
  const isFresh = (Date.now() - state.lastMinerSync) < 150000;
  const hasAllData = addresses.every(addr => !!state.walletMiners[addr.toLowerCase()]);
  if (isFresh && hasAllData) {
    renderMinersOverview();
    return true;
  }
  addresses.forEach(addr => state.minersLoading[addr.toLowerCase()] = true);
  renderMinersOverview();

  try {
    const res = await fetch('/api/miners/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ addresses })
    });
    const data = await res.json();
    if (res.ok) {
      state.lastMinerSync = Date.now();
      for (const [addr, info] of Object.entries(data)) {
        state.walletMiners[addr.toLowerCase()] = info;
      }
      return true;
    } else {
      addresses.forEach(addr => state.walletMiners[addr.toLowerCase()] = { error: data.error || 'Batch error' });
      return false;
    }
  } catch (err) {
    appendLog('', `\u001b[31m[API] Batch wallet load failed: ${err.message || err}\u001b[0m`, 'ERROR');
    addresses.forEach(addr => state.walletMiners[addr.toLowerCase()] = { error: 'Network error (batch)' });
    return false;
  } finally {
    addresses.forEach(addr => state.minersLoading[addr.toLowerCase()] = false);
    renderMinersOverview();
  }
}

/* ─────────────────────────────────────────────────────────────────
   REFRESH ACTIONS
   ───────────────────────────────────────────────────────────────── */

/** Full refresh of all wallet miner data. Protected by 15s cooldown. */
async function refreshAllMiners() {
  if (!checkCooldown('wallets')) return;
  showToast('Updating Wallets & Facilities...', 'info');
  
  const btn = document.getElementById('refbar-wallets-btn');
  if (btn) btn.classList.add('loading');
  minersOvList.innerHTML = '<div class="loading-msg">Refreshing wallets...</div>';

  const addresses = state.wallets.map(w => w.address);
  addresses.forEach(addr => delete state.walletMiners[addr.toLowerCase()]);

  setRunning(true);
  setAppStatus('running');
  try {
    const success = await loadBatchWalletMiners(addresses);
    if (success) {
      removeAlert('refresh-wallets-fail');
      showToast('Wallets & Facilities refreshed', 'success');
    } else {
      // Find the first error to display in the alert
      const firstError = addresses.map(a => state.walletMiners[a.toLowerCase()]?.error).find(e => !!e) || 'Unknown error';
      registerAlert('refresh-wallets-fail', {
        type: 'error',
        title: 'Wallets & Facilities Refresh Failed',
        message: `The bot could not sync wallet data. RPC or network might be unstable. (${firstError})`,
        section: 'global',
        persistent: true
      });
      showToast('Wallets & Facilities refresh failed', 'error');
    }
  } catch (err) {
    appendLog('', `\u001b[31m[API] Critical error during refresh: ${err.message || err}\u001b[0m`, 'ERROR');
    registerAlert('refresh-wallets-fail', {
      type: 'error',
      title: 'Wallets & Facilities Refresh Failed',
      message: `Critical network error during synchronization. The backend might be unreachable. (${err.message || err})`,
      section: 'global',
      persistent: true
    });
    showToast('Network error', 'error');
  } finally {
    if (btn) btn.classList.remove('loading');
    setRunning(false);
    setAppStatus('idle');
  }
}

/** Force the backend to re-scrape miner type metadata (names, images). Protected by 15s cooldown. */
async function forceRefreshMinersCache() {
  if (!checkCooldown('cache')) return;

  const btn = document.getElementById('refbar-cache-btn');
  if (btn) btn.classList.add('loading');
  showToast('Updating miners cache...', 'info');
  setRunning(true);
  setAppStatus('running');
  try {
    const res = await fetch('/api/miners/cache/refresh', { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      removeAlert('refresh-cache-fail');
      await loadMinerTypes();
      renderMinersOverview();
      showToast(`Cache updated (${data.count} types)`, 'success');
    } else {
      registerAlert('refresh-cache-fail', {
        type: 'error',
        title: 'Cache Update Failed',
        message: data.error || 'The backend could not refresh miner metadata from the HashCash API.',
        section: 'global',
        persistent: true
      });
      showToast(data.error || 'Cache refresh error', 'error');
    }
  } catch (err) {
    appendLog('', `\u001b[31m[API] Cache refresh failed: ${err.message || err}\u001b[0m`, 'ERROR');
    registerAlert('refresh-cache-fail', {
      type: 'error',
      title: 'Cache Update Failed',
      message: `Network error: Could not reach the backend to refresh miner metadata. (${err.message || err})`,
      section: 'global',
      persistent: true
    });
    showToast('Network error', 'error');
  } finally {
    if (btn) btn.classList.remove('loading');
    setRunning(false);
    setAppStatus('idle');
  }
}

/** Fetch the current Avalanche gas price. Protected by 15s cooldown from UI. */
async function checkGasPrice() {
  // If the app just booted and preloaded gas, we set gasInitialized to bypass the initial check without triggering a manual fetch. 
  // But if this function is called, we process the 15s cooldown.
  // UI check for freshness (2.5 min limit)
  if (state.lastGasSync && (Date.now() - state.lastGasSync) < 150000) {
    showToast('Gas price is up-to-date', 'info');
    return;
  }

  // UI cooldown check (15s spam protection)
  if (!checkCooldown('gas')) return;

  const btn = document.getElementById('refbar-gas-btn');
  if (btn) btn.classList.add('loading');
  
  setRunning(true);
  setAppStatus('running');
  try {
    const res = await fetch('/api/gas');
    const data = await res.json();
    if (res.ok) {
      removeAlert('gas-check-fail');
      state.lastGasSync = Date.now();
      if (btn) btn.textContent = `Gas ${formatDecimal(data.gas_price_gwei, 2)} Gwei`;
      appendLog(new Date().toLocaleTimeString(), `⛽ Current Avalanche Gas: \u001b[96m${formatDecimal(data.gas_price_gwei, 2)} Gwei\u001b[0m`);
      showToast(`Current Gas: ${formatDecimal(data.gas_price_gwei, 2)} Gwei`, 'info');
    } else {
      registerAlert('gas-check-fail', {
        type: 'error',
        title: 'Gas Check Failed',
        message: data.error || 'The backend could not retrieve the current gas price from RPC.',
        section: 'global',
        persistent: true
      });
      showToast(data.error || 'Gas error', 'error');
    }
  } catch (err) {
    appendLog('', `\u001b[31m[API] Gas check failed: ${err.message || err}\u001b[0m`, 'ERROR');
    registerAlert('gas-check-fail', {
      type: 'error',
      title: 'Gas Check Failed',
      message: `Network error: Backend unreachable for gas verification. (${err.message || err})`,
      section: 'global',
      persistent: true
    });
    showToast('Network error (Gas)', 'error');
  } finally {
    if (btn) btn.classList.remove('loading');
    setRunning(false);
    setAppStatus('idle');
  }
}

/** Refresh a single wallet's miner list (from the per-wallet refresh button). */
function refreshWalletMiners(address) {
  const body = document.getElementById(`mov-body-${address}`);
  if (body) body.innerHTML = '<div class="">Refreshing...</div>';
  delete state.walletMiners[address.toLowerCase()];
  loadWalletMiners(address, true);
}

/* ─────────────────────────────────────────────────────────────────
   EXPAND / COLLAPSE
   ───────────────────────────────────────────────────────────────── */

/** Toggle expand/collapse for a single wallet's miner section. */
function toggleWalletMiners(address) {
  const addrLow = address.toLowerCase();
  state.minersExpanded[addrLow] = !state.minersExpanded[addrLow];
  const body = document.getElementById(`mov-body-${address}`);
  const header = document.getElementById(`mov-header-${address}`);
  const chevron = document.getElementById(`mov-chevron-${address}`);
  if (body) {
    if (state.minersExpanded[addrLow]) body.classList.add('expanded');
    else body.classList.remove('expanded');
  }
  if (header) {
    header.classList.toggle('expanded', state.minersExpanded[addrLow]);
  }
  if (chevron) chevron.classList.toggle('rotated', state.minersExpanded[addrLow]);
}

/** Expand or collapse all wallet sections at once. */
function toggleAllMiners() {
  state.allExpanded = !state.allExpanded;
  state.wallets.forEach(w => { state.minersExpanded[w.address.toLowerCase()] = state.allExpanded; });
  state.wallets.forEach(w => {
    const body = document.getElementById(`mov-body-${w.address}`);
    const header = document.getElementById(`mov-header-${w.address}`);
    const chevron = document.getElementById(`mov-chevron-${w.address}`);
    if (body) {
      if (state.allExpanded) body.classList.add('expanded');
      else body.classList.remove('expanded');
    }
    if (header) {
      header.classList.toggle('expanded', state.allExpanded);
    }
    if (chevron) chevron.classList.toggle('rotated', state.allExpanded);
  });
  const btn = document.getElementById('toggle-all-miners-btn');
  if (btn) btn.innerHTML = state.allExpanded
    ? `<svg-icon name="chevron-up" class="svg-size-sm"></svg-icon> Hide all`
    : `<svg-icon name="chevron-down" class="svg-size-sm"></svg-icon> Show all`;
}

/* ─────────────────────────────────────────────────────────────────
   RENDERING
   ───────────────────────────────────────────────────────────────── */

/** Full render of the Miners Overview panel (all wallets). */
function renderMinersOverview() {
  if (!state.wallets.length) return;

  const placeholder = minersOvList.querySelector('.loading-msg');
  if (placeholder) placeholder.remove();

  const activeIds = [];

  // ── Global Synchronisation Error Alert ──
  const syncError = state.wallets.some(w => {
    const info = state.walletMiners[w.address.toLowerCase()];
    return info?.inventory_error;
  });

  const bannerId = 'inventory-api-offline';
  if (syncError) {
    registerAlert(bannerId, {
      type: 'warning',
      section: 'global',
      title: "NFT's owner API Unreachable — Inventory Synchronization Impossible",
      message: "This is an external issue. The bot's core functions remain operational, but your inventory NFTs cannot be retrieved.",
      persistent: true
    });
  } else {
    removeAlert(bannerId);
  }

  for (const w of state.wallets) {
    const addrLow = w.address.toLowerCase();
    const info = state.walletMiners[addrLow];
    const short = w.address.slice(0, 6) + '...' + w.address.slice(-4);
    const expanded = !!state.minersExpanded[addrLow];
    const id = `mov-${w.address}`;
    activeIds.push(id);

    let avax_balance = info?.avax_balance || 0;
    let hcash_balance = info?.hcash_balance || 0;

    let bodyHtml = '';
    if (!info) {
      bodyHtml = '<div class="">Loading...</div>';
    } else if (info.error) {
      bodyHtml = `<div class="miners-ov-error">✗ ${info.error}</div>`;
    } else {
      const placed = info.placed || [];
      const { facility, owned } = info;

      // ── Facility Stats ──
      if (facility) {
        const totalMhs = (placed || []).reduce((s, m) => s + m.hashrate, 0);
        const threshold = state.config.debt_threshold !== undefined ? state.config.debt_threshold : -0.001;
        const isDebt = info.net_claimable < threshold;
        bodyHtml += `
        <div class="flex-inline">
          <div class="mov-facility">
            <span class="mov-facility-stat"><strong>Lv.${facility.facilityIndex}</strong></span>
          </div>
          <div class="mov-facility">
            <span class="mov-facility-stat">🏭 <strong>${facility.currMiners}/${facility.maxMiners}</strong> miners</span>
            <span class="mov-fstat-sep"></span>
            <span class="mov-facility-stat">⚡ <strong><span class="privacy-data">${displayPower(facility.currPowerOutput)}</span></strong> / <span class="privacy-data">${displayPower(facility.totalPowerOutput)}</span></span>
            <span class="mov-fstat-sep"></span>
            <span class="mov-facility-stat">⛏️ <strong><span class="privacy-data">${totalMhs}</span> MH/s</strong></span>
          </div>
          <div class="mov-facility">
            <span class="mov-facility-stat${isDebt ? ' mov-debt' : ''}" title="Net Claimable hCASH (Pending - Fees)">💎 <strong><span class="privacy-data">${formatDecimal(info.net_claimable, 4)}</span></strong> Net claimable hCASH ${isDebt ? ' <span class="wallet-debt-badge">⚠ DEBT</span>' : ''}</span>
            <span class="mov-fstat-sep"></span>
            <span class="mov-facility-stat" title="Electricity Fees Accrued">⚡ <strong><span class="privacy-data">${formatDecimal(info.electricity_owed, 4)}</span></strong> Electricity fees</span>
            <span class="mov-fstat-sep"></span>
            <span class="mov-facility-stat" title="Total hCASH Pending Rewards">⛏️ <strong><span class="privacy-data">${formatDecimal(info.pending, 4)}</span></strong> Total pending</span>
          </div>
        </div>
        `;
      } else {
        bodyHtml += `<div class="mov-facility mov-no-facility">⚠ Facility not initialized</div>`;
      }

      // ── Placed Miners ──
      bodyHtml += `<div class="mov-miners-group">`;
      const placedList = placed || [];
      if (placedList.length) {
        bodyHtml += `<div class="mov-section-label">⚡ Placed Miners [${placedList.length}]</div><div class="mov-chips">`;
        for (const m of placedList) {
          const mt = getMinerLabel(m.nftContract, m.minerIndex);
          const name = mt?.name || `Type #${m.minerIndex}`;
          const img = mt?.image ? `<img src="${mt.image}" class="miner-chip-img" alt="" onerror="this.src=this.src.replace('.png','.gif'); this.onerror=function(){this.style.display='none'};">` : '';
          const gameDetail = m.id ? ` (MINER #${m.id})` : '';
          const hasListing = !!m.listing;
          const warnIcon = hasListing ? '<span>⚠️</span>' : '';
          const titleSuffix = hasListing ? ` | ⚠️ LISTED ON ${m.listing.foreignWalletName}` : '';
          
          const title = `${name} #${m.nftTokenId}${gameDetail} — ${m.hashrate} MH/s ${m.powerConsumption * POWER_UNIT} W${titleSuffix}`;
          const classes = hasListing ? "miner-chip warning" : "miner-chip placed";

          bodyHtml += `<div class="${classes}" title="${title}">
            ${img}<span class="miner-chip-name">${name}</span>
            <span>#<span class="privacy-data">${m.nftTokenId}</span>${m.id ? ` (MINER #<span class="privacy-data">${m.id}</span>)` : ''}</span>
            <span class="miner-chip-coords">${m.x},${m.y}</span>
            ${warnIcon}
          </div>`;
        }
        bodyHtml += '</div>';
      } else {
        const errorMsg = info.placed_error ? `<span class="miners-none-error">⚠ Sync failed</span>` : '<span class="miners-none">none</span>';
        bodyHtml += `<div class="mov-section-label">⚡ Placed Miners — ${errorMsg}</div>`;
      }
      bodyHtml += `</div>`;

      // ── Marketplace Listings ──
      const listings = info.listings || [];
      if (listings.length) {
        bodyHtml += `<div class="mov-miners-group">`;
        bodyHtml += `<div class="mov-section-label">🛒 Listed on Marketplace [${listings.length}]</div><div class="mov-chips">`;
        for (const l of listings) {
          const mt = getMinerLabel(l.assetContract, l.minerIndex);
          const name = mt?.name || `NFT #${l.tokenId}`;
          const img = mt?.image ? `<img src="${mt.image}" class="miner-chip-img" alt="" onerror="this.src=this.src.replace('.png','.gif'); this.onerror=function(){this.style.display='none'};">` : '';
          
          const isDupe = l.duplicateCount > 1;
          const dupeText = isDupe ? ` | ⚠️ LISTED ${l.duplicateCount} TIMES` : '';
          const titleSuffix = l.isForeign ? ` (Listed on ${l.foreignWalletName})` : '';
          const title = `${name} #${l.tokenId} — Listed for ${l.priceDisplay} ${l.currencySymbol}${titleSuffix}${dupeText}`;
          const classes = (l.isForeign || isDupe) ? "miner-chip warning" : "miner-chip listed";
          
          let icons = '';
          if (l.isForeign) icons += '<span title="Foreign Listing">⚠️</span> ';
          if (isDupe) icons += `<span class="badge-dupe" title="Double Listing">⚠ DUP x${l.duplicateCount}</span>`;

          bodyHtml += `<div class="${classes}" title="${title}">
            ${img}<span class="miner-chip-name">${name}</span>
            <span>#<span class="privacy-data">${l.tokenId}</span></span>
            <span class="miner-chip-time">${l.timeRemainingStr}</span>
            ${icons}
          </div>`;
        }
        bodyHtml += '</div></div>';
      }

      // ── Inventory NFTs (Filtered: remove listed) ──
      bodyHtml += `<div class="mov-miners-group">`;
      const ownedEntries = Object.entries(owned || {});
      const filteredOwned = [];
      
      for (const [idx, tokenIds] of ownedEntries) {
        const typeData = state.minerTypes[idx] || {};
        const nftContract = typeData.nftContract?.toLowerCase();
        
        // Filter out IDs that appear in listings
        const remainingIds = tokenIds.filter(tid => {
          return !listings.some(l => l.assetContract.toLowerCase() === nftContract && l.tokenId == tid);
        });
        
        if (remainingIds.length > 0) {
          filteredOwned.push([idx, remainingIds]);
        }
      }

      if (filteredOwned.length) {
        bodyHtml += `<div class="mov-section-label">📦 Inventory NFTs</div><div class="mov-chips">`;
        for (const [idx, tokenIds] of filteredOwned) {
          const count = tokenIds.length;
          const mt = getMinerLabel(null, idx);
          const name = mt?.name || `Type #${idx}`;
          const img = mt?.image ? `<img src="${mt.image}" class="miner-chip-img" alt="" onerror="this.src=this.src.replace('.png','.gif'); this.onerror=function(){this.style.display='none'};">` : '';
          const idsList = tokenIds.map(id => `#<span class="privacy-data">${id}</span>`).join(', ');
          bodyHtml += `<div class="miner-chip owned" title="${name} : ${tokenIds.map(id => `#${id}`).join(', ')}">
            ${img}
            <div class="miner-chip-name">
              <span>${count}</span>
              <span>x</span>
              <span class="miner-chip-name">${name}</span>
            </div>
            <span>-</span>
            <div>
              <span>${idsList}</span>
            </div>
          </div>`;
        }
        bodyHtml += '</div>';
      } else {
        const errorMsg = info.inventory_error ? `<span class="miners-none-error">⚠ Sync failed</span>` : '<span class="miners-none">none</span>';
        bodyHtml += `<div class="mov-section-label">📦 Inventory — ${errorMsg}</div>`;
      }
      bodyHtml += `</div>`;
    }

    const html = `
      <div class="miners-ov-wallet-header ${expanded ? 'expanded' : ''}" id="mov-header-${w.address}" onclick="toggleWalletMiners('${w.address}')">
        <button class="mov-toggle-btn ${expanded ? 'rotated' : ''}" id="mov-chevron-${w.address}" onclick="event.stopPropagation(); toggleWalletMiners('${w.address}')"
          title="${expanded ? 'Collapse' : 'Expand'}">
          <svg-icon name="chevron-right" class="chevron-icon svg-size-sm"></svg-icon>
        </button>
        <span class="mov-wallet-name">${w.name}${w.is_main ? '<span class="badge-main">MAIN</span>' : ''}</span>
        <span class="mov-wallet-addr privacy-random"><a href="${state.config.debank_url}${w.address}" target="_blank" onclick="event.stopPropagation()" class="addr-link" title="DeBank Profile"><span class="privacy-random">${short}</span></a></span>
        <div class="mov-balances">
          <span class="mov-balance-token ${avax_balance > 0.0001 ? 'blue' : ''}">
            <img src="${state.config.avax_logo_url}" class="mov-asset-icon" alt="AVAX">
            <span class="privacy-data">${formatDecimal(avax_balance || 0, 4)}</span> AVAX
          </span>
          <span class="mov-balance-token ${hcash_balance > 0.0001 ? 'blue' : ''}">
            <img src="${state.config.hcash_logo_url}" class="mov-asset-icon" alt="hCASH">
            <span class="privacy-data">${formatDecimal(hcash_balance || 0, 4)}</span> hCASH
          </span>
        </div>
        <button class="mov-refresh-btn" onclick="event.stopPropagation(); refreshWalletMiners('${w.address}')" title="Refresh"><svg-icon name="refresh" class="svg-size-sm"></svg-icon></button>
      </div>
      <div class="miners-ov-body ${expanded ? 'expanded' : ''}" id="mov-body-${w.address}">
        <div class="miners-ov-body-inner">${bodyHtml}</div>
      </div>`;

    upsertElement(minersOvList, id, 'miners-ov-wallet', html);
  }
  cleanupElements(minersOvList, activeIds);
  updateClaimTotals();

  // Update sidebar debt badges via the now-morphed sidebar cards
  renderSidebar();
}

/** Aggregate net/pending totals and update the Claim action button. */
function updateClaimTotals() {
  let pending = 0;
  let net = 0;
  let hasData = false;
  for (const w of state.wallets) {
    const info = state.walletMiners[w.address.toLowerCase()];
    if (info && !info.error && info.facility) {
      pending += info.pending || 0;
      net += info.net_claimable || 0;
      hasData = true;
    }
  }

  const spanTotals = document.getElementById('claim-totals');
  const spanPending = document.getElementById('claim-total-pending');
  const spanNet = document.getElementById('claim-total-net');

  if (spanTotals && spanPending && spanNet) {
    if (hasData) {
      spanPending.textContent = formatDecimal(pending, 2);
      spanNet.textContent = formatDecimal(net, 2);
    }
  }
}

/* ── Window Exports (for HTML onclick handlers) ──────────────── */
window.refreshAllMiners = refreshAllMiners;
window.forceRefreshMinersCache = forceRefreshMinersCache;
window.checkGasPrice = checkGasPrice;
window.refreshWalletMiners = refreshWalletMiners;
window.toggleWalletMiners = toggleWalletMiners;
window.toggleAllMiners = toggleAllMiners;

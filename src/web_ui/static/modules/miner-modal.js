/**
 * miner-modal.js — Miner Management Modal
 *
 * Full-featured modal for batch miner operations:
 *  • Single-wallet view: facility grid + inventory list
 *  • Multi-wallet view: combined list view
 *  • Per-miner action config: Withdraw / Transfer / Place
 *  • Real-time facility forecast simulation
 *  • Batch submission to the backend
 *
 * Depends on: state.js, utils.js, api.js (getMinerLabel, setRunning, setAppStatus, pollStatus), cards.js (renderDataCards), layout.js (toggleMainPanel)
 */

/* ─────────────────────────────────────────────────────────────────
   MODAL OPEN / CLOSE
   ───────────────────────────────────────────────────────────────── */

/**
 * Check if the selected wallets have any miners or NFTs (placed or owned).
 * Returns the total count.
 */
function countAvailableItems(walletNames) {
  let count = 0;
  for (const wname of walletNames) {
    const w = state.wallets.find(x => x.name === wname);
    if (!w) continue;
    const info = state.walletMiners[w.address.toLowerCase()];
    if (!info || info.error) continue;

    // Placed
    if (info.placed) count += info.placed.length;

    // Owned (inventory)
    if (info.owned) {
      Object.values(info.owned).forEach(ids => { count += ids.length; });
    }
  }
  return count;
}

function closeMinerModal() {
  const modal = document.getElementById('miner-modal');
  const inner = modal.querySelector('.modal');
  modal.classList.add('closing');
  if (inner) inner.classList.add('closing');

  setTimeout(() => {
    modal.classList.add('hidden');
    modal.classList.remove('closing');
    if (inner) inner.classList.remove('closing');
  }, 500);
}

function openMinerModal() {
  if (!state.selected.size) { showToast('Select at least one wallet', 'warning'); return; }

  // Pre-check: if no items available, don't open modal, show toast
  const totalItems = countAvailableItems(state.selected);
  if (totalItems === 0) {
    showToast('No miners or NFTs found for the selected wallet(s).', 'warning');
    return;
  }

  // Reset batch config for new session
  state.minerBatchConfig = {};

  const mbody = document.getElementById('miner-modal-body');
  document.getElementById('miner-modal-error').textContent = '';

  mbody.innerHTML = `
    <div class="mm-layout">
      <div id="mm-left" class="mm-col-left"></div>
      <div id="mm-right" class="mm-col-right">
         <h4 class="mm-sim-title">Facility Forecast</h4>
         <div id="mm-sim-content" class="mm-sim-list"></div>
      </div>
    </div>
  `;
  const leftCol = document.getElementById('mm-left');

  // Single wallet → grid UI
  if (state.selected.size === 1) {
    const wname = [...state.selected][0];
    renderSingleWalletMinerUI(wname, leftCol);
    document.getElementById('miner-modal').classList.remove('hidden');
    updateMinerModalSimulation();
    return;
  }

  // Multi-wallet → list UI
  for (const wname of state.selected) {
    const w = state.wallets.find(x => x.name === wname);
    const info = state.walletMiners[w.address.toLowerCase()];
    if (!info || info.error) continue;

    const wDiv = document.createElement('div');
    wDiv.className = 'mm-section';
    const short = w.address.slice(0, 6) + '...' + w.address.slice(-4);
    wDiv.innerHTML = `
      <div class="mm-wallet-title">
        <span>${w.name}</span> 
        <span class="mov-wallet-addr privacy-data"><a href="${state.config.debank_url}${w.address}" target="_blank" onclick="event.stopPropagation()" class="addr-link" title="Profil Debank"><span class="privacy-data">${short}</span></a></span>
        ${renderBulkActionsHtml(w.name)}
      </div>
      <div class="mm-item-list" id="list-${w.name}"></div>
    `;
    leftCol.appendChild(wDiv);
    const listDiv = wDiv.querySelector(`#list-${w.name}`);

    // Placed miners/NFTs
    const placed = info.placed || [];
    if (placed.length) {
      placed.forEach(m => {
        m.gameId = m.id;
        listDiv.innerHTML += buildMinerRowHtml(w, m, getMinerLabel(m.nftContract, m.minerIndex), 'placed');
      });
    }

    // Owned miners/NFTs (inventory)
    if (info.owned) {
      Object.entries(info.owned).forEach(([cIdx, ids]) => {
        const typeData = state.minerTypes[cIdx] || {};
        const mt = getMinerLabel(null, cIdx);
        let nftContract = typeData.nftContract;
        if (!nftContract && mt && state._nftToType) {
          for (const [key, val] of Object.entries(state._nftToType)) {
            if (val.idx == cIdx) { nftContract = key; break; }
          }
        }
        ids.forEach(tokenId => {
          const p_raw = typeData.power !== undefined ? typeData.power : 0;
          const h_raw = typeData.hashrate !== undefined ? typeData.hashrate : 0;
          listDiv.innerHTML += buildMinerRowHtml(w, { id: tokenId, gameId: null, nftTokenId: tokenId, cIdx, power: p_raw, hashrate: h_raw, nftContract }, mt, 'owned');
        });
      });
    }
  }

  document.getElementById('btn-exec-miner-batch').disabled = true;
  document.getElementById('miner-modal').classList.remove('hidden');
  updateMinerModalSimulation();
}


/* ─────────────────────────────────────────────────────────────────
   SINGLE WALLET: FACILITY GRID VIEW
   ───────────────────────────────────────────────────────────────── */

function renderSingleWalletMinerUI(wname, container) {
  const w = state.wallets.find(x => x.name === wname);
  const info = state.walletMiners[w.address.toLowerCase()];
  if (!info || info.error) {
    container.innerHTML = `<div class="text-error">Error: Unable to load data for ${wname}</div>`;
    return;
  }

  state.minerBatchConfig = {};

  const facility = info.facility || { x: 5, y: 0, maxMiners: 0 };
  const maxX = facility.x || 5;
  const maxY = facility.y || (facility.maxMiners ? Math.ceil(facility.maxMiners / maxX) : 4);
  const placed = info.placed || [];

  const short = w.address.slice(0, 6) + '...' + w.address.slice(-4);
  let html = `
    <div class="mm-grid-container">
      <div class="mm-wallet-title">
        <div class="mm-grid-title">Facility Grid - ${wname}</div>
        <div class="mov-wallet-addr">${short}</div>
        ${renderBulkActionsHtml(wname)}
      </div>
      <div class="mm-facility-grid" style="--grid-cols: ${maxX}">
  `;

  // Pre-mapping for fast coordinate lookup
  const gridMap = {};
  placed.forEach(m => { 
    m.gameId = m.id;
    gridMap[m.x + ',' + m.y] = m; 
  });

  // Generate grid slots
  for (let y = 0; y < maxY; y++) {
    for (let x = 0; x < maxX; x++) {
      const m = gridMap[x + ',' + y];
      if (m) {
        const mt = getMinerLabel(m.nftContract, m.minerIndex);
        const name = mt?.name || `Type #${m.minerIndex}`;
        const img = mt?.image || "";

        html += `
          <div class="mm-grid-slot has-miner" id="slot-${m.id}" onclick="selectGridMiner('${m.id}', '${wname}')">
            ${img ? `<img src="${img}" class="mm-grid-miner-img">` : '<div class="status-error">The illustration of the Miner is missing</div>'}
            <span class="mm-grid-coords">${x},${y}</span>
          </div>
        `;
      } else {
        html += `<div class="mm-grid-slot empty"><span class="mm-grid-coords">${x},${y}</span></div>`;
      }
    }
  }

  html += `</div></div> <!-- End Grid Container -->
  <!-- Miner Action Panel -->
  <div id="mm-action-panel" class="mm-action-panel hidden"></div>`;

  // Inventory section
  html += `
    <div class="mm-section">
      <div class="mm-wallet-title">
        <svg-icon name="box" class="svg-size-xl"></svg-icon>
        Inventory (Owned NFTs)
      </div>
      <div class="mm-item-list" id="list-${wname}"></div>
    </div>
  `;

  container.innerHTML = html;
  const listDiv = container.querySelector(`#list-${wname}`);

  // Owned miners/NFTs
  if (info.owned) {
    Object.entries(info.owned).forEach(([cIdx, ids]) => {
      const typeData = state.minerTypes[cIdx] || {};
      const mt = getMinerLabel(null, cIdx);
      let nftContract = typeData.nftContract;
      if (!nftContract && mt && state._nftToType) {
        for (const [key, val] of Object.entries(state._nftToType)) {
          if (val.idx == cIdx) { nftContract = key; break; }
        }
      }
      ids.forEach(tokenId => {
        const p_raw = typeData.power !== undefined ? typeData.power : 0;
        const h_raw = typeData.hashrate !== undefined ? typeData.hashrate : 0;
        listDiv.innerHTML += buildMinerRowHtml(w, { id: tokenId, gameId: null, nftTokenId: tokenId, cIdx, power: p_raw, hashrate: h_raw, nftContract }, mt, 'owned');
      });
    });
  }
  document.getElementById('btn-exec-miner-batch').disabled = false;
}

/* ─────────────────────────────────────────────────────────────────
   PER-MINER ACTION CONFIGURATION
   ───────────────────────────────────────────────────────────────── */

/**
 * Get or create the action config for a specific miner.
 * Used as the single source of truth for withdraw/transfer/place state.
 */
window.getMinerActionConfig = function (wname, mid, m, type) {
  const key = `${wname}:${mid}`;
  if (!state.minerBatchConfig[key]) {
    state.minerBatchConfig[key] = {
      wname, mid,
      withdraw: false,
      transferDest: "",
      autoPlace: false,
      type,
      m,
      mt: getMinerLabel(m.nftContract, m.minerIndex !== undefined ? m.minerIndex : m.cIdx)
    };
  }
  return state.minerBatchConfig[key];
};

/** Sync all DOM controls for a miner (list + grid panel) to match state. */
window.syncMinerControlsUI = function (wname, mid) {
  const key = `${wname}:${mid}`;
  const cfg = state.minerBatchConfig[key];
  if (!cfg) return;

  const controls = document.querySelectorAll(`[data-miner-key="${wname}:${mid}"]`);

  controls.forEach(ctrl => {
    const btnWD = ctrl.querySelector('.mm-ap-btn.withdraw');
    if (btnWD) btnWD.classList.toggle('active', cfg.withdraw);

    const sel = ctrl.querySelector('.mm-ap-select');
    if (sel) {
      sel.value = cfg.transferDest;
      sel.classList.toggle('active', !!cfg.transferDest);
    }

    const btnPL = ctrl.querySelector('.mm-ap-btn.place');
    if (btnPL) btnPL.classList.toggle('active', cfg.autoPlace);
  });

  // Sync grid slot border color
  const slot = document.getElementById('slot-' + mid);
  if (slot) {
    slot.style.borderColor = cfg.transferDest ? 'var(--accent-blue)' : (cfg.withdraw ? 'var(--accent-red)' : '');
  }
};

window.toggleMinerWithdraw = function (wname, mid) {
  const cfg = getMinerActionConfig(wname, mid);
  cfg.withdraw = !cfg.withdraw;
  if (!cfg.withdraw) { cfg.transferDest = ""; cfg.autoPlace = false; }
  syncMinerControlsUI(wname, mid);
  updateMinerModalSimulation();
};

window.setMinerTransfer = function (wname, mid, dest) {
  const cfg = getMinerActionConfig(wname, mid);
  cfg.transferDest = dest;
  if (dest && cfg.type === 'placed') cfg.withdraw = true;
  if (!dest) cfg.autoPlace = false;
  syncMinerControlsUI(wname, mid);
  updateMinerModalSimulation();
};

window.toggleMinerPlace = function (wname, mid) {
  const cfg = getMinerActionConfig(wname, mid);
  const willActivate = !cfg.autoPlace;

  if (willActivate && cfg.type === 'placed') {
    if (!cfg.withdraw) {
      showToast("Please select 'Withdraw' first to be able to replace this miner.", "info");
      return;
    }
    if (!cfg.transferDest) {
      showToast("Withdrawing to replace in the same spot makes no sense. Use transfer to place it elsewhere.", "info");
      return;
    }
  }

  cfg.autoPlace = willActivate;
  syncMinerControlsUI(wname, mid);
  updateMinerModalSimulation();
};

/* ─────────────────────────────────────────────────────────────────
   BULK ACTIONS
   ───────────────────────────────────────────────────────────────── */

window.renderBulkActionsHtml = function(wname) {
  // Only show buttons if there are NFTs in the wallet
  const totalItems = countAvailableItems([wname]);
  if (totalItems === 0) return "";

  const walletOptions = state.wallets.map(optW => {
    const addrMask = state.privacyEnabled ? '0x...' : (optW.address.slice(0, 6) + '...' + optW.address.slice(-4));
    return `<option value="${optW.address}">${optW.name} - ${addrMask}</option>`;
  }).join('');

  return `
    <div class="mm-bulk-actions">
      <button class="mm-ap-btn withdraw mm-bulk-btn" onclick="bulkToggleWithdraw('${wname}')" title="Toggle Withdraw for all placed NFTs">
        <span class="mm-ap-btn-label">Withdraw All</span>
      </button>
      
      <select name="Wallet Destination" class="mm-ap-select mm-bulk-select" onchange="bulkSetTransfer('${wname}', this.value); this.value='';" title="Transfer all NFTs to...">
        <option value="">-- Transfer All --</option>
        ${walletOptions}
      </select>

      <button class="mm-ap-btn place mm-bulk-btn" onclick="bulkTogglePlace('${wname}')" title="Toggle Place for all NFTs">
        <span class="mm-ap-btn-label">Place All</span>
      </button>
    </div>
  `;
};

window.bulkToggleWithdraw = function(wname) {
  const w = state.wallets.find(x => x.name === wname);
  const info = state.walletMiners[w.address.toLowerCase()];
  if (!info || !info.placed) return;

  let anyNotWithdrawing = info.placed.some(m => !getMinerActionConfig(wname, m.id).withdraw);

  info.placed.forEach(m => {
    const cfg = getMinerActionConfig(wname, m.id, m, 'placed');
    cfg.withdraw = anyNotWithdrawing;
    if (!cfg.withdraw) { cfg.transferDest = ""; cfg.autoPlace = false; }
    syncMinerControlsUI(wname, m.id);
  });
  updateMinerModalSimulation();
};

window.bulkSetTransfer = function(wname, dest) {
  if (!dest) return;
  const w = state.wallets.find(x => x.name === wname);
  const info = state.walletMiners[w.address.toLowerCase()];
  if (!info) return;

  // Placed
  (info.placed || []).forEach(m => {
    const cfg = getMinerActionConfig(wname, m.id, m, 'placed');
    cfg.transferDest = dest;
    cfg.withdraw = true;
    syncMinerControlsUI(wname, m.id);
  });

  // Owned 
  if (info.owned) {
      Object.entries(info.owned).forEach(([cIdx, ids]) => {
          const typeData = state.minerTypes[cIdx] || {};
          let nftContract = typeData.nftContract;
          if (!nftContract && state._nftToType) {
              for (const [key, val] of Object.entries(state._nftToType)) {
                  if (val.idx == cIdx) { nftContract = key; break; }
              }
          }
          ids.forEach(tokenId => {
              const m = { id: tokenId, gameId: null, nftTokenId: tokenId, cIdx, power: typeData.power, hashrate: typeData.hashrate, nftContract };
              const cfg = getMinerActionConfig(wname, tokenId, m, 'owned');
              cfg.transferDest = dest;
              syncMinerControlsUI(wname, tokenId);
          });
      });
  }
  updateMinerModalSimulation();
};

window.bulkTogglePlace = function(wname) {
  const w = state.wallets.find(x => x.name === wname);
  const info = state.walletMiners[w.address.toLowerCase()];
  if (!info) return;

  let anyNotPlacing = false;
  if (info.placed && info.placed.some(m => !getMinerActionConfig(wname, m.id).autoPlace)) anyNotPlacing = true;
  if (!anyNotPlacing && info.owned) {
      Object.values(info.owned).forEach(ids => {
          if (ids.some(tid => !getMinerActionConfig(wname, tid).autoPlace)) anyNotPlacing = true;
      });
  }

  (info.placed || []).forEach(m => {
    const cfg = getMinerActionConfig(wname, m.id, m, 'placed');
    if (anyNotPlacing) {
        if (!cfg.withdraw) cfg.withdraw = true;
        // Logic check: if no transfer, placing back on same spot is blocked in single toggle but for bulk, we apply and let simulation/validation show the impact.
    }
    cfg.autoPlace = anyNotPlacing;
    syncMinerControlsUI(wname, m.id);
  });

  if (info.owned) {
      Object.entries(info.owned).forEach(([cIdx, ids]) => {
          const typeData = state.minerTypes[cIdx] || {};
          let nftContract = typeData.nftContract;
          if (!nftContract && state._nftToType) {
              for (const [key, val] of Object.entries(state._nftToType)) {
                  if (val.idx == cIdx) { nftContract = key; break; }
              }
          }
          ids.forEach(tokenId => {
              const m = { id: tokenId, gameId: null, nftTokenId: tokenId, cIdx, power: typeData.power, hashrate: typeData.hashrate, nftContract };
              const cfg = getMinerActionConfig(wname, tokenId, m, 'owned');
              cfg.autoPlace = anyNotPlacing;
              syncMinerControlsUI(wname, tokenId);
          });
      });
  }
  updateMinerModalSimulation();
};

/* ─────────────────────────────────────────────────────────────────
   MINER CONTROLS RENDERING
   ───────────────────────────────────────────────────────────────── */

/**
 * Render the action controls row for a single miner.
 * @param {boolean} isPanel - If true, returns inner HTML only (for the grid action panel).
 */
function renderMinerControlsHtml(wname, m, type, isPanel = false) {
  const cfg = getMinerActionConfig(wname, m.id, m, type);
  const mt = cfg.mt;

  const name = mt?.name || `Type #${m.minerIndex !== undefined ? m.minerIndex : m.cIdx}`;
  const img = mt?.image ? `<img src="${mt.image}" class="mm-ap-img">` : '<div class="modal-error-msg">?</div>';
  const unit = (typeof POWER_UNIT !== 'undefined' ? POWER_UNIT : 100);
  const powerW = (m.powerConsumption || m.power || 0) * unit;
  const nftIdPart = (m.nftTokenId !== undefined && m.nftTokenId !== null) ? `NFT #<span class="privacy-data">${m.nftTokenId}</span>` : '';
  const gameIdPart = (m.gameId !== undefined && m.gameId !== null) ? `MINER #<span class="privacy-data">${m.gameId}</span> ` : '';

  const walletOptions = state.wallets.map(optW => {
    const addrMask = state.privacyEnabled ? '0x...' : (optW.address.slice(0, 6) + '...' + optW.address.slice(-4));
    const selAttr = optW.address.toLowerCase() === cfg.transferDest.toLowerCase() ? 'selected' : '';
    return `<option value="${optW.address}" ${selAttr}>${optW.name} - ${addrMask}</option>`;
  }).join('');

  const controlsHtml = `
    <div class="mm-ap-info">
      ${img}
      <div class="mm-ap-details">
        <div class="mm-ap-id">${nftIdPart} ${gameIdPart}</div>
        <div class="mm-ap-name">${name}</div>
        <div class="mm-ap-miners-details">
          ${mt?.category === 'external_nft' ? 'Inventory NFT (Non-miner)' : `⛏️ ${m.hashrate || 0} MH/s <div class="refbar-sep"></div> ⚡ ${powerW} W`}
        </div>
      </div>
    </div>
    
    <div class="mm-ap-controls" data-miner-key="${wname}:${m.id}">
      ${type === 'placed' && mt?.category !== 'external_nft' ? `
        <div class="mm-ap-group">
          <button class="mm-ap-btn withdraw ${cfg.withdraw ? 'active' : ''}" onclick="toggleMinerWithdraw('${wname}', '${m.id}')">
            <span class="mm-ap-btn-label">Withdraw</span>
          </button>
        </div>
      ` : ''}
      <div class="mm-ap-group">
        <select name="Wallet Destination" class="mm-ap-select ${cfg.transferDest ? 'active' : ''}" onchange="setMinerTransfer('${wname}', '${m.id}', this.value)">
          <option value="">-- Transfer --</option>
          ${walletOptions}
        </select>
      </div>
      ${mt?.category !== 'external_nft' ? `
      <div class="mm-ap-group">
        <button class="mm-ap-btn place ${cfg.autoPlace ? 'active' : ''}" onclick="toggleMinerPlace('${wname}', '${m.id}')">
          <span class="mm-ap-btn-label">Place</span>
        </button>
      </div>
      ` : ''}
    </div>
  `;

  if (isPanel) return controlsHtml;
  return `<div class="mm-item">${controlsHtml}</div>`;
}

function buildMinerRowHtml(w, m, mt, type = 'placed') {
  return renderMinerControlsHtml(w.name, m, type, false);
}

/* ─────────────────────────────────────────────────────────────────
   GRID SELECTION
   ───────────────────────────────────────────────────────────────── */

window.selectGridMiner = function (id, wname) {
  const slot = document.getElementById('slot-' + id);
  const panel = document.getElementById('mm-action-panel');
  const w = state.wallets.find(x => x.name === wname);
  const info = state.walletMiners[w.address.toLowerCase()];
  const m = info.placed.find(x => x.id.toString() === id.toString());

  if (!m) return;

  // Visual selection
  document.querySelectorAll('.mm-grid-slot.selected').forEach(el => el.classList.remove('selected'));
  slot.classList.add('selected');

  // Render controls in the bottom panel
  panel.innerHTML = renderMinerControlsHtml(wname, m, 'placed', true);
  panel.classList.remove('hidden');
};

window.updateOverlayState = function (id) {
  // Overlays are synced via syncMinerControlsUI — no-op kept for compatibility
};

/* ─────────────────────────────────────────────────────────────────
   FACILITY SIMULATION
   ───────────────────────────────────────────────────────────────── */

/**
 * Recalculate the facility forecast for all wallets based on
 * the currently configured batch actions. Shows warnings when
 * miner count or power would exceed facility limits.
 */
window.updateMinerModalSimulation = function () {
  try {
    const sim = {};
    state.wallets.forEach(w => {
      const info = state.walletMiners[w.address.toLowerCase()];
      if (info && info.facility) {
        sim[w.name] = {
          maxM: parseInt(info.facility.maxMiners) || 0,
          maxP: parseInt(info.facility.totalPowerOutput) || 0,
          currM: parseInt(info.facility.currMiners) || 0,
          currP: parseInt(info.facility.currPowerOutput) || 0,
          deltaM: 0, deltaP: 0
        };
      }
    });

    let hasAction = false;
    Object.values(state.minerBatchConfig).forEach(cfg => {
      const wName = cfg.wname;
      const m = cfg.m;
      const pwr = parseInt(m.powerConsumption || m.power || 0);

      if (cfg.type === 'placed' && cfg.withdraw) {
        hasAction = true;
        if (sim[wName]) { sim[wName].deltaM -= 1; sim[wName].deltaP -= pwr; }
        if (cfg.transferDest && cfg.autoPlace) {
          const destW = state.wallets.find(x => x.address.toLowerCase() === cfg.transferDest.toLowerCase());
          if (destW && sim[destW.name]) { sim[destW.name].deltaM += 1; sim[destW.name].deltaP += pwr; }
        }
      } else if (cfg.type === 'owned') {
        if (cfg.transferDest && cfg.autoPlace) {
          hasAction = true;
          const destW = state.wallets.find(x => x.address.toLowerCase() === cfg.transferDest.toLowerCase());
          if (destW && sim[destW.name]) { sim[destW.name].deltaM += 1; sim[destW.name].deltaP += pwr; }
        } else if (!cfg.transferDest && cfg.autoPlace) {
          hasAction = true;
          if (sim[wName]) { sim[wName].deltaM += 1; sim[wName].deltaP += pwr; }
        } else if (cfg.transferDest) {
          hasAction = true;
        }
      }
    });

    let hasError = false;
    const simCtn = document.getElementById('mm-sim-content');
    if (simCtn) {
      // Clear manual list if we are transitioning to managed upserts (handle leftovers from previous bug)
      if (simCtn.querySelector('.mm-sim-group:not([id])')) simCtn.innerHTML = '';
      
      const activeIds = [];
      Object.keys(sim).forEach(wn => {
        const s = sim[wn];
        const finalM = s.currM + s.deltaM;
        const finalP = s.currP + s.deltaP;
        const mStyle = finalM > s.maxM ? 'mm-sim-error' : (finalM > s.currM ? 'mm-sim-success' : '');
        const pStyle = finalP > s.maxP ? 'mm-sim-error' : (finalP > s.currP ? 'mm-sim-success' : '');
        if (finalM > s.maxM || finalP > s.maxP) hasError = true;

        const id = `mm-sim-${wn}`;
        activeIds.push(id);

        const html = `
          <div class="mm-sim-row-title">${wn}</div>
          <div class="mm-sim-row">
             <span class="mm-sim-row-label flex-inline">
               <svg-icon name="slots" class="svg-size-sm"></svg-icon>
               Slots
             </span>
             <span class="${mStyle}"><span class="privacy-data">${finalM}</span> / <span class="privacy-data">${s.maxM}</span></span>
          </div>
          <div class="mm-sim-row">
             <span class="mm-sim-row-label flex-inline">
               <svg-icon name="bolt" class="svg-size-sm"></svg-icon>
               Power
             </span>
             <span class="${pStyle}"><span class="privacy-data">${displayPower(finalP)}</span> / <span class="privacy-data">${displayPower(s.maxP)}</span></span>
          </div>`;
        
        upsertElement(simCtn, id, 'mm-sim-group', html);
      });
      cleanupElements(simCtn, activeIds);
    }

    const errBox = document.getElementById('miner-modal-error');
    const btnExec = document.getElementById('btn-exec-miner-batch');

    const isLocked = typeof state.getSecurityLock === 'function' ? state.getSecurityLock() : false;

    if (isLocked) {
      errBox.textContent = '❌ Security Lock: Action blocked by Universal Guard.';
      if (btnExec) {
        btnExec.disabled = true;
        btnExec.classList.add('security-blocked');
      }
    } else if (!hasAction) {
      errBox.textContent = ''; 
      if (btnExec) {
        btnExec.disabled = true;
        btnExec.classList.remove('security-blocked');
      }
    } else if (hasError) {
      errBox.textContent = '❌ Facility limits exceeded for one or more wallets.'; 
      if (btnExec) {
        btnExec.disabled = true;
        btnExec.classList.remove('security-blocked');
      }
    } else {
      errBox.textContent = ''; 
      if (btnExec) {
        btnExec.disabled = false;
        btnExec.classList.remove('security-blocked');
      }
    }
  } catch (e) {
    appendLog('', `\u001b[31m[Modal] Simulation error: ${e.message || e}\u001b[0m`, 'ERROR');
    document.getElementById('miner-modal-error').textContent = 'Simulation Error: ' + e.message;
  }
};

/* ─────────────────────────────────────────────────────────────────
   BATCH SUBMISSION
   ───────────────────────────────────────────────────────────────── */

/**
 * Build the final payload from minerBatchConfig and send it to the backend.
 * Each miner's planned journey (withdraw → transfer → place) is tracked in state.minerJourneys for real-time progress display in cards.js.
 */
window.submitMinerBatch = async function () {
  const payload = { action: state.actionKeys.BATCH_MINERS, wallets_actions: {} };
  state.wallets.forEach(w => {
    payload.wallets_actions[w.name] = { withdraws: [], transfers: [], places: [] };
  });

  let selectedAnyWallet = new Set();
  const newJourneys = {}; // Accumulate tracking objects locally

  Object.values(state.minerBatchConfig).forEach(cfg => {
    const wName = cfg.wname;
    const m = cfg.m;
    const trackingId = parseInt(m.id);
    const gameId = (m.gameId !== undefined && m.gameId !== null) ? parseInt(m.gameId) : null;
    const nftTokenId = (m.nftTokenId !== undefined && m.nftTokenId !== null) ? parseInt(m.nftTokenId) : null;
    const typeIdx = parseInt(m.minerIndex !== undefined ? m.minerIndex : m.cIdx);
    const nft = m.nftContract;
    const name = cfg.mt?.name || "Miner";
    const image = cfg.mt?.image || "";

    const item = { id: trackingId, game_id: gameId, nft_token_id: nftTokenId, type_idx: typeIdx, nft, name, image };
    const journey = { name, image, nft_id: nftTokenId, game_id: gameId, planned: [], steps: [] };

    if (cfg.type === 'placed' && cfg.withdraw) {
      selectedAnyWallet.add(wName);
      payload.wallets_actions[wName].withdraws.push(item);
      journey.planned.push('Withdraw');

      if (cfg.transferDest) {
        payload.wallets_actions[wName].transfers.push({ ...item, dest: cfg.transferDest });
        journey.planned.push('Transfer');
        if (cfg.autoPlace) {
          const destW = state.wallets.find(x => x.address.toLowerCase() === cfg.transferDest.toLowerCase());
          if (destW) {
            payload.wallets_actions[destW.name].places.push(item);
            selectedAnyWallet.add(destW.name);
            journey.planned.push('Place');
          }
        }
      }
    } else if (cfg.type === 'owned') {
      if (cfg.transferDest) {
        selectedAnyWallet.add(wName);
        payload.wallets_actions[wName].transfers.push({ ...item, dest: cfg.transferDest });
        journey.planned.push('Transfer');
        if (cfg.autoPlace) {
          const destW = state.wallets.find(x => x.address.toLowerCase() === cfg.transferDest.toLowerCase());
          if (destW) {
            payload.wallets_actions[destW.name].places.push(item);
            selectedAnyWallet.add(destW.name);
            journey.planned.push('Place');
          }
        }
      } else if (cfg.autoPlace) {
        selectedAnyWallet.add(wName);
        payload.wallets_actions[wName].places.push(item);
        journey.planned.push('Place');
      }
    }

    if (journey.planned.length > 0) {
      newJourneys[trackingId] = journey;
    }
  });

  if (selectedAnyWallet.size === 0) {
    document.getElementById('miner-modal-error').textContent = 'No miner selected!';
    return;
  }

  const finalPayload = { action: state.actionKeys.BATCH_MINERS, wallets_actions: {} };
  selectedAnyWallet.forEach(wn => { finalPayload.wallets_actions[wn] = payload.wallets_actions[wn]; });

  closeMinerModal();

  // Redirect execution directly to the central Orchestrator.
  // We pass newJourneys so app.js can inject them safely AFTER wiping the global state.
  if (window.runAction) {
    window.runAction(state.actionKeys.BATCH_MINERS, finalPayload, selectedAnyWallet.size, newJourneys);
  }
};

/* ── Window Exports (for HTML onclick handlers) ──────────────── */
window.closeMinerModal = closeMinerModal;

/* ── Button Wiring ───────────────────────────────────────────── */
document.getElementById('btn-manage-miners').addEventListener('click', openMinerModal);

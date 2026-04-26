/**
 * cards.js — Wallet/Miner Cards & Generic Summaries
 *
 * Renders the "Action View" panel that appears during/after actions:
 *  • Wallet Cards: show claim/transfer/gas results per wallet
 *  • Miner Journey Cards: show NFT lifecycle (withdraw → transfer → place)
 *  • Generic Cards: show custom summaries (e.g. rewards recap)
 *
 * Uses micro-components from utils.js (pill, txRow, statusLabel).
 *
 * Depends on: state.js, utils.js, api.js (getMinerLabel)
 */

/* ── Main Orchestrator ───────────────────────────────────────── */
function renderDataCards() {
  const details = state.walletDetails || {};
  const journeys = state.minerJourneys || {};
  const journeyIds = Object.keys(journeys).sort();
  const genericCards = state.genericCards || {};

  const activeIds = [];
  let cardIndex = 0;

  // 1. Wallet Cards (hCASH Rewards / Gas / Transfers)
  for (const w of state.wallets) {
    const name = w.name;
    const d = details[name];
    if (!d) continue;

    const id = `wdc-wallet-${name}`;
    activeIds.push(id);
    
    const isDone = d.status === 'success';
    const isSkipped = d.status === 'skipped';
    const cardStatus = d.status === 'error' ? 'error' : (isSkipped ? 'skipped' : (isDone ? 'success' : 'running'));

    const html = getWalletCardHtml(name, d, cardStatus);
    
    // Stable stagger: use wallet's original index to prevent re-animation on list shifts
    upsertElement(walletCards, id, `wdc status-${cardStatus}`, html, 'div', w.index || 0);
  }

  // 2. Miner Journey Cards (NFT lifecycle)
  journeyIds.forEach((m_id, idx) => {
    const id = `wdc-miner-${m_id}`;
    activeIds.push(id);
    const m = journeys[m_id];
    
    const hasError = m.steps.some(s => s.status === 'error');
    const isAllDone = m.planned.length > 0 && m.planned.every(p => m.steps.some(s => s.type === p && s.status === 'success'));
    const cardStatus = hasError ? 'error' : (isAllDone ? 'success' : 'running');

    const html = getMinerCardHtml(m_id, m, cardStatus);

    // Stable stagger: use journey array index + wallet count offset
    upsertElement(walletCards, id, `wdc status-${cardStatus}`, html, 'div', state.wallets.length + idx);
  });

  // 3. Generic Action Summary Cards
  for (const cardId in genericCards) {
    const card = genericCards[cardId];
    const id = `wdc-generic-${cardId}`;
    activeIds.push(id);
    const html = getGenericCardHtml(cardId, card);
    upsertElement(walletCards, id, `wdc status-${card.status || 'idle'} wdc-generic`, html, 'div', cardIndex++);
  }

  cleanupElements(walletCards, activeIds);

  // Handle Placeholder Cleanup / Visibility
  if (activeIds.length > 0) {
    const placeholder = walletCards.querySelector('.cards-empty');
    if (placeholder) placeholder.remove();
  } else {
    // If no active cards, and no cards are currently being removed, show empty placeholder
    const currentlyRemoving = walletCards.querySelectorAll('.wdc.removing').length > 0;
    const placeholder = walletCards.querySelector('.cards-empty');
    
    if (!currentlyRemoving && !placeholder) {
      walletCards.innerHTML = '<div class="cards-empty">Start an action to see exactly what HashOps does in the background</div>';
    } else if (currentlyRemoving) {
      // Re-run once more after the animation delay to show placeholder
      setTimeout(() => {
        if (!Object.keys(state.walletDetails).length && !Object.keys(state.minerJourneys).length) {
          renderDataCards();
        }
      }, 600);
    }
  }
}

/* ── Wallet Card HTML ────────────────────────────────────────── */
function getWalletCardHtml(name, d, cardStatus) {
  const short = d.address ? d.address.slice(0, 6) + '...' + d.address.slice(-4) : '';
  const isSkipped = cardStatus === 'skipped';

  // --- 1. Snapshot Layer (Rigid structure) ---
  let snapshotsHtml = '';
  // We check if we have ANY snapshot info, if not we keep it empty but ready
  const hasSnap = (d.initial_pending !== null || d.initial_balance !== null || d.target_balance !== null);
  if (hasSnap) {
    snapshotsHtml = `<div class="wdc-snapshots wdc-sep">
        <span class="wdc-snap-label">Snap:</span>
        ${d.initial_pending !== null ? pill('Pending', `<span class="privacy-data">${formatDecimal(d.initial_pending, 4)}</span>`, 'snapshot') : ''}
        ${d.initial_balance !== null ? pill('Balance', `<span class="privacy-data">${formatDecimal(d.initial_balance, 4)}</span>`, 'snapshot') : ''}
        ${d.target_balance !== null ? pill('Target', `<span class="privacy-data">${formatDecimal(d.target_balance, 4)}</span>`, 'snapshot') : ''}
    </div>`;
  } else {
    // Empty but present placeholder to maintain layout if needed (optional)
    snapshotsHtml = ''; 
  }

  // --- 2. Live Progress / Result Layer (Always present wdc-amounts container) ---
  let resultPills = '';
  
  // Claim Progress
  if (d.actual_claimed !== null) {
    resultPills += pill('Claimed', `<span class="privacy-data">${formatDecimal(d.actual_claimed, 4)}</span>`, 'status-success-cyan');
  } else if (d.claim_status === 'pending') {
    resultPills += pill('Claiming', '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon>', 'status-running');
  }

  // Transfer Progress
  if (d.transfer_amount !== null) {
    resultPills += pill('Transfer', `<span class="privacy-data">${formatDecimal(d.transfer_amount, 4)}</span>`, 'status-success-cyan');
  } else if (d.transfer_status === 'pending') {
    resultPills += pill('Transfer', '<svg-icon name="spin" class="spin-icon-svg svg-size-sm"></svg-icon>', 'status-running');
  }

  // Specific "Skipped" message
  if (isSkipped && !d.recap_html) {
    const reason = d.skipped_reason || `Skipped: below ${state.config.claim_threshold || 10} hCASH`;
    resultPills += `<div class="skipped-msg status-skipped"><span>${reason}</span></div>`;
  }
  
  // AVAX transfers (Gas Dispatch)
  let avaxTxsHtml = '';
  if (d.transfer_avax_txs && Object.keys(d.transfer_avax_txs).length > 0) {
    const avaxResult = getAvaxTransfersMarkup(d.transfer_avax_txs);
    resultPills += avaxResult.pills;
    avaxTxsHtml = avaxResult.txs;
  }

  const amountsHtml = resultPills ? `<div class="wdc-amounts">${resultPills}</div>` : '';

  // --- 3. Transaction Details ---
  let txRows = '';
  let claimGroup = '';
  if (d.claim_tx) claimGroup += txRow(state.actionNames[state.actionKeys.CLAIM], d.claim_tx, d.claim_status);
  if (d.transfer_tx) claimGroup += txRow('Tokens Transfer', d.transfer_tx, d.transfer_status);
  if (claimGroup) txRows += dataGroup('hCASH Action', claimGroup, 'wallet');
  
  txRows += avaxTxsHtml;
  
  const txContainerHtml = txRows ? `<div class="wdc-txs">${txRows}</div>` : '';

  // --- 4. Alert & Final Recap Layer ---
  let footerHtml = '';
  if (d.error) footerHtml += `<div class="wdc-error">✗ ${d.error}</div>`;
  if (d.recap_html) footerHtml += `<div class="wdc-summary">${d.recap_html}</div>`;


  return `
    <div class="wdc-header">
      <div>
        <span class="wdc-name">${name}</span>
        ${short ? `<div class="wdc-addr"><a href="${state.config.debank_url}${d.address}" target="_blank" class="addr-link"><span class="privacy-data">${short}</span></a></div>` : ''}
      </div>
      <span class="wallet-status-badge status-${cardStatus}">${statusLabel(d.status)}</span>
    </div>
    ${snapshotsHtml}
    ${amountsHtml}
    ${txContainerHtml}
    ${footerHtml}
  `;
}

/**
 * Parses AVAX gas transfers to generate pills and transaction groups.
 */
function getAvaxTransfersMarkup(transferDict) {
  let totalIn = 0; let totalOut = 0;
  let statusIn = 'success'; let statusOut = 'success';
  let groups = {};

  Object.values(transferDict).forEach(tx => {
    const groupKey = tx.type === 'in' ? `From ${tx.target}` : `To ${tx.target}`;
    if (!groups[groupKey]) groups[groupKey] = [];
    groups[groupKey].push(tx);

    if (tx.type === 'in') {
      totalIn += tx.amount;
      if (tx.status === 'error') statusIn = 'error';
      else if (tx.status === 'pending' && statusIn !== 'error') statusIn = 'pending';
    } else {
      totalOut += tx.amount;
      if (tx.status === 'error') statusOut = 'error';
      else if (tx.status === 'pending' && statusOut !== 'error') statusOut = 'pending';
    }
  });

  let pills = '';
  if (totalIn > 0) {
    pills += pill('Reception', `<span class="privacy-data">${formatDecimal(totalIn, 4)}</span>`, statusClass(statusIn === 'success' ? 'success-cyan' : statusIn));
  }
  if (totalOut > 0) {
    pills += pill('Transfer', `<span class="privacy-data">${formatDecimal(totalOut, 4)}</span>`, statusClass(statusOut === 'success' ? 'success-cyan' : statusOut));
  }

  let txsHtml = '';
  Object.entries(groups).forEach(([walletAction, txs]) => {
    const groupContent = txs.map(tx => {
      let actionName = tx.type === 'in' ? 'Reception' : 'Transfer';
      let showTx = tx.type === 'in' ? null : tx.tx;
      return txRow(actionName, showTx, tx.status);
    }).join('');
    txsHtml += dataGroup(walletAction, groupContent, 'wallet');
  });

  return { pills, txs: txsHtml };
}

/* ── Miner Journey Card HTML ─────────────────────────────────── */
function getMinerCardHtml(m_id, m, cardStatus) {

  // Header
  const imgHtml = m.image ? `<img src="${m.image}" class="miner-chip-img-big" onerror="this.style.display='none'">` : '';
  const nftIdPart = m.nft_id ? `<span class="badge-main">NFT #<span class="privacy-data">${m.nft_id}</span></span>` : '';
  const gameIdPart = m.game_id ? `<span class="badge-main">MINER #<span class="privacy-data">${m.game_id}</span></span>` : '';

  // Summary bubbles
  let bubblesHtml = '<div class="wdc-amounts">';
  m.planned.forEach(pType => {
    const isDone = m.steps.some(s => s.type === pType && s.status === 'success');
    const isError = m.steps.some(s => s.type === pType && s.status === 'error');
    const cls = isDone ? 'status-success' : (isError ? 'status-error' : 'status-running');
    bubblesHtml += `<div class="wdc-amount-pill ${cls}">${pType}</div>`;
  });
  bubblesHtml += '</div>';

  // Steps grouped by wallet
  const groups = {};
  m.steps.forEach(s => {
    if (!groups[s.wallet]) groups[s.wallet] = [];
    groups[s.wallet].push(s);
  });

  let stepsHtml = '';
  Object.entries(groups).forEach(([walletName, steps]) => {
    const groupContent = steps.map(s => {
      let label = s.type;
      if (s.type === 'Transfer' && s.dest) label += ` ➔ ${s.dest}`;
      if ((s.type === 'Received' || s.type === 'Reception') && s.dest) label += ` ${s.dest}`;
      const showHash = (s.type !== 'Received' && s.type !== 'Reception');
      return txRow(label, showHash ? s.tx : null, s.status);
    }).join('');
    stepsHtml += dataGroup(walletName, groupContent, 'wallet');
  });

  return `
    <div class="wdc-header wdc-sep">
      ${imgHtml}
      <div class="wdc-nft-label">
        <span class="wdc-name">${m.name}</span>
        <div class="wdc-nft-id">${nftIdPart} ${gameIdPart}</div>
      </div>
      <span class="wallet-status-badge status-${cardStatus}">${statusLabel(cardStatus)}</span>
    </div>
    ${bubblesHtml}
    <div class="wdc-txs">
      ${stepsHtml}
    </div>
  `;
}

/* ── Generic Summary Card HTML ──────────────────────────────── */
function getGenericCardHtml(cardId, card) {
  const title = card.title || 'Summary';
  const status = card.status || 'success';
  const icon = card.icon || 'info';
  const badgeLabel = statusLabel(status);

  let snapshotsHtml = '';
  if (card.snapshots && card.snapshots.length > 0) {
    snapshotsHtml = `<div class="wdc-snapshots wdc-sep">
      ${card.snapshots.map(s => pill(s.label, s.value, 'snapshot')).join('')}
    </div>`;
  }

  let txHtml = '';
  if (card.txs && card.txs.length > 0) {
    txHtml = `<div class="wdc-txs">${card.txs.map(tx => txRow(tx.label, tx.tx, tx.status)).join('')}</div>`;
  }

  let summaryHtml = '';
  if (card.recap_html) summaryHtml = `<div class="wdc-summary">${card.recap_html}</div>`;

  return `
    <div class="wdc-header wdc-sep">
      <div class="flex-inline">
        ${icon.startsWith('http') 
          ? `<img src="${icon}" class="svg-size-md mr-sm">`
          : `<svg-icon name="${icon}" class="svg-size-md mr-sm"></svg-icon>`
        }
        <span class="wdc-name">${title}</span>
      </div>
      <span class="wallet-status-badge status-${status}">${badgeLabel}</span>
    </div>
    ${snapshotsHtml}
    ${txHtml}
    ${summaryHtml}
  `;
}

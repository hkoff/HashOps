/**
 * state.js — Centralized Application State & DOM References
 *
 * Single source of truth for all runtime data.
 * Loaded FIRST — every other module reads/writes this object.
 *
 * ─── Why a shared mutable object? ───────────────────────────
 * All UI modules (sidebar, miners, cards, modal) need read/write access to the same wallet list, miner cache, and selection set.
 * A single `state` const avoids prop-drilling and keeps the mental model simple: "state is THE place to look for current data."
 */

/* ── Application State ─────────────────────────────────────────── */
const state = {
  wallets: [],             // Array of wallet objects { name, address, is_main }
  selected: new Set(),     // Currently selected wallet names for actions
  running: false,          // True while a backend action is in progress
  isTransitioning: false,  // True during the short gap between clicking 'Run' and receiving backend response
  waitingForSync: false,   // True after action_done, until final data refresh arrives

  /* Component: System Alerts (Banners) ─────────────────────────── */
  alerts: {},              // { id: { type, title, message, section, persistent } }

  /* Per-wallet runtime data ────────────────────────────────────── */
  walletStatuses: {},      // { walletName: 'idle'|'running'|'success'|'error'|... }
  walletDetails: {},       // { walletName: { claim/transfer/gas detail fields } }
  walletMiners: {},        // { address: { facility, placed[], owned{}, balances } }
  minersLoading: {},       // { address: true } — prevents duplicate loads
  minersExpanded: {},      // { address: bool } — collapse state per wallet
  allExpanded: false,      // Global expand/collapse toggle

  /* Miner types & journeys ─────────────────────────────────────── */
  minerTypes: {},          // { "1": { name, nft_name, nft_image, nftContract, hashrate, ... } }
  minerJourneys: {},       // { minerId: { nft_id, name, image, planned[], steps[] } }
  minerBatchConfig: {},    // { "walletName:minerId": { withdraw, transferDest, autoPlace, ... } }
  genericCards: {},        // { cardId: { title, status, recap_html, ... } }

  /* UI state ───────────────────────────────────────────────────── */
  privacyEnabled: localStorage.getItem('privacy_mode') === 'true',
  showDebug: false,        // DEBUG log-level filter toggle
  panelMinersOpen: true,   // Miners overview panel open by default
  panelCardsOpen: false,   // Wallet cards panel closed by default

  /* Polling & caching ──────────────────────────────────────────── */
  pollInterval: null,      // setTimeout ID for status polling
  lastMinerSync: 0,        // Last Multicall3 sync time
  lastGasSync: 0,          // Last gas price sync time
  gasInitialized: false,   // True after first gas check

  /* Action Names Display Mapping (Populated by /api/config) ───── */
  actionNames: {},
  /* Action Technical Keys (Populated by /api/config) ──────────── */
  actionKeys: {},

  /* Backend config (populated at init) ─────────────────────────── */
  config: {
    hcash_logo_url: "https://cdn.popularhost.net/hashcash/hcash_token.png",
    avax_logo_url: "https://raw.githubusercontent.com/lifinance/types/main/src/assets/icons/chains/avalanche.svg"
  },
};

// Power multiplier: the smart-contract encodes power in units of 100 W
const POWER_UNIT = 100;

/* ── Connection Configuration ──────────────────────────────────── */
// Initial delay for SSE reconnection attempts (ms)
let sseRetryDelay = 1000;
// Maximum delay for SSE reconnection (ms) to avoid endless rapid retries
const MAX_SSE_RETRY_DELAY = 30000;

/** 
 * Gets the synchronized transition delay from CSS tokens.
 * Defaults to 600ms if the variable is missing or unparseable.
 */
function getTransitionDelay() {
  const cssVal = getComputedStyle(document.documentElement).getPropertyValue('--transition-action-exit').trim();
  if (!cssVal) return 600;
  const ms = parseFloat(cssVal) * (cssVal.includes('ms') ? 1 : 1000);
  return isNaN(ms) ? 600 : ms;
}
state.transitionDelay = getTransitionDelay();

/* ── DOM References ────────────────────────────────────────────── */
// Resolved once at load time (scripts are at the bottom of <body>).
const walletsList    = document.getElementById('wallets-list');
const walletCards    = document.getElementById('wallet-cards');
const logTerminal    = document.getElementById('log-terminal');
const statusText     = document.getElementById('status-text');
const statusDot      = document.getElementById('status-dot');
const toastContainer = document.getElementById('toast-container');
const toggleIcon     = document.getElementById('toggle-icon');
const minersOvList   = document.getElementById('miners-overview-list');

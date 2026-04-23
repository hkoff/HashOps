# src/web_ui/app.py — Flask backend for the HashOps Web Dashboard
#
# Architecture Overview
# ─────────────────────
# This file is the sole HTTP interface between the JavaScript frontend and the Python action orchestrators (claim, dispatch_gas, batch_miners).
#
# Communication patterns:
#   1. REST endpoints     — The frontend fetches data and triggers actions via POST /api/run.
#   2. SSE stream         — Real-time logs and action events are delegated to sse.py.
#   3. System Alerts      — Persistent UI warnings for critical issues are pushed via ui_alerts.py.
#   4. Background threads — Actions run in daemon threads to avoid blocking Flask handlers.
#
# Thread safety:
#   _state_lock   → protects _app_state (idle/running/done)
#   _init_lock    → protects _init_status (startup sequence)
#   ui_state.py   → has its own _status_lock and _details_lock
#
# Security model:
#   The server binds to 127.0.0.1 only (local-only, no auth needed).
#   No private keys or secrets are ever exposed through the API.
#   Wallet addresses and balances are public blockchain data.

import logging
import os
import re
import threading
import time

from flask import Flask, request, jsonify, render_template
from web3 import Web3

import src.config as config
from src.services.logger_setup import logger
from src.utils.helpers import cyan_bold, red_bold, yellow_bold, magenta_bold, green_bold
from src.actions.ui_state import (
    get_wallet_statuses, get_wallet_details, get_miner_journeys, 
    get_generic_cards, reset_ui_state, _init_detail, _prepare_miner_journey
)
from src.actions.ui_alerts import push_system_alert, remove_system_alert
from src.core.hcash_api import HCashApiRateLimitError, HCashApiError
from src.core.blockchain import (
    get_contract_address, 
    get_hcash_token_address, 
    get_wallet_miners_info, 
    get_batch_wallets_miners_info
)
from src.services.miner_cache import refresh_miner_cache_if_needed
from src.actions.claim_rewards import run_claim_all
from src.actions.dispatch_gas import run_dispatch_gas
from src.actions.batch_handle_nft_miners import run_all_miners_batches
from src.web_ui.sse import _broadcast, get_sse_response
from src.actions.ui_alerts import get_active_alerts
from src.core.security import initialize_security, validate_authorized_wallet, validate_contract, SecurityException

# Ethereum address regex (0x + 40 hex chars)
_ETH_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ─────────────────────────────────────────────────────────────────
# FLASK APP SETUP
# ─────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_DIR = os.path.join(_BASE_DIR, "templates")
_STATIC_DIR = os.path.join(_BASE_DIR, "static")

app = Flask(__name__, template_folder=_TEMPLATE_DIR, static_folder=_STATIC_DIR)
app.config["SECRET_KEY"] = "hcash-local-only-secure-key"

# Suppress repetitive Werkzeug logs for the high-frequency /api/status endpoint
_werkzeug_log = logging.getLogger('werkzeug')

class _StatusLogFilter(logging.Filter):
    """Filters out successful GET /api/status lines to keep the console clean."""
    def filter(self, record):
        msg = record.getMessage()
        return not ('GET /api/status' in msg and (' 200 ' in msg or ' 304 ' in msg))

_werkzeug_log.addFilter(_StatusLogFilter())


# ─────────────────────────────────────────────────────────────────
# GLOBAL STATE & CONTEXT INJECTION
# ─────────────────────────────────────────────────────────────────

# --- Application run state ---
_app_state = {
    "status": "idle",   # idle | running | done
    "action": None,     # Current or last action name
}
_state_lock = threading.Lock()

# --- Initialization sequence (loader screen) ---
_init_status = {
    "step": "Waiting for operator...",
    "percentage": 0,
    "details": [],       # Progress detail messages
    "miners": [],        # Discovered miner types [{name, image}]
    "waiting": True,     # True until operator clicks "Start"
    "ready": False,      # True when init completes successfully
    "failed": False,     # True if init fails fatally
    "error_message": "",
    "results": None,     # Final preloaded data (batch_data, gas_price)
}
_init_lock = threading.Lock()
_init_fn = None  # Registered initialization callback (set by main.py)

def register_init_fn(fn):
    """Register the initialization function called when the operator triggers start.

    Called once by main.py at server startup to inject the engine init callback.
    """
    global _init_fn
    _init_fn = fn

def update_init_status(step=None, percentage=None, detail=None, miner=None,
                       ready=None, failed=None, error_message=None, results=None):
    """Update the loader screen progress (called by the init thread).

    Args:
        step:          Current phase description (e.g. "Connecting to RPC...")
        percentage:    Progress bar value (0-100)
        detail:        A single detail line to append to the log
        miner:         A dict {name, image} to add to the discovered miners carousel
        ready:         Set True when initialization completes successfully
        failed:        Set True when initialization fails fatally
        error_message: Human-readable error for the loader error screen
        results:       Final data dict (batch_data, gas_price) to pre-populate dashboard
    """
    with _init_lock:
        if step is not None:
            _init_status["step"] = step
        if percentage is not None:
            _init_status["percentage"] = percentage
        if detail is not None:
            _init_status["details"].append(detail)
        if miner is not None:
            _init_status["miners"].append(miner)
        if ready is not None:
            _init_status["ready"] = ready
        if failed is not None:
            _init_status["failed"] = failed
        if error_message is not None:
            _init_status["error_message"] = error_message
        if results is not None:
            _init_status["results"] = results

# --- Blockchain context (injected at startup by main.py) ---
_wallets: list[dict]  = []
_w3                   = None    # Web3 instance
_game_main            = None    # Main game contract object
_game_token           = None    # hCASH token contract object
_burner1_address: str = None    # Primary wallet address (for transfers)
_miner_types: dict    = {}      # Miner type cache {idx: {name, nft_name, ...}}

# --- Debt Detection: cached batch data from last get_batch_wallets_miners_info call ---
# Updated at init, after every action (_finish_action), and on frontend batch refresh.
# No extra RPC calls — piggybacks on existing data pipeline.
_cached_batch_data: dict = {}
_batch_data_lock = threading.Lock()

def init_app_context(wallets, w3, game_main, game_token, burner1_address, miner_types=None, registry=None):
    """Inject blockchain references and initialize security."""
    global _wallets, _w3, _game_main, _game_token, _burner1_address, _miner_types
    _wallets         = wallets
    _w3              = w3
    _game_main       = game_main
    _game_token      = game_token
    _burner1_address = burner1_address
    _miner_types     = miner_types or {}
    
    # Initialize Universal Integrity & Transaction Guard
    if registry:
        initialize_security(wallets, registry)

def set_cached_batch_data(data: dict) -> None:
    """Populate the cached batch data from an external caller (e.g. main.py at init)."""
    with _batch_data_lock:
        # Normalize all keys to lowercase for robust lookup
        normalized = {k.lower(): v for k, v in data.items()}
        _cached_batch_data.update(normalized)
    _update_debt_alerts()

def _update_debt_alerts():
    """Evaluate debt for all configured wallets based on the latest cache and emit top-level system alerts."""
    with _batch_data_lock:
        for w in _wallets:
            addr_key = w["address"].lower()
            info = _cached_batch_data.get(addr_key)
            if not info:
                continue
                
            net = info.get("net_claimable", 0)
            alert_id = f"debt-{w['name']}"
            if net < config.DEBT_THRESHOLD:
                # Log a warning to the terminal (only if first detection or persistent reminder)
                logger.warning(yellow_bold(f"[DEBT] Wallet {w['name']} has facility debt ({round(net, 4)} hCASH). Dangerous operations blocked."))
                
                push_system_alert(
                    alert_id=alert_id,
                    title=f"Facility Debt — {w['name']}",
                    message=(
                        f"Electricity fees exceed pending rewards (net: {round(net, 4)} hCASH). "
                        "HashOps will NOT execute Claim, Withdraw, or Place actions for this wallet to prevent involuntary debt payment. "
                        "Resolve this manually via the HashCash dashboard."
                    ),
                    alert_type="warning",
                    section="global",
                    persistent=True
                )
            else:
                remove_system_alert(alert_id)

def _get_debt_wallets(wallet_names: list[str]) -> set[str]:
    """Check cached batch data for wallets in debt. No RPC call — reads cache only."""
    debt = set()
    with _batch_data_lock:
        for w in _wallets:
            if w["name"] in wallet_names:
                # Force lowercase lookup to match normalized cache keys
                addr_key = w["address"].lower()
                info = _cached_batch_data.get(addr_key)
                
                if info is None:
                    logger.debug(yellow_bold(f"[DEBT] Warning: No cached data found for {w['name']} ({addr_key}) during guard check."))
                    continue

                net = info.get("net_claimable", 0)
                if net < config.DEBT_THRESHOLD:
                    debt.add(w["name"])
                    logger.warning(yellow_bold(
                        f"[DEBT] {w['name']} has facility debt (net_claimable: {round(net, 4)} hCASH)"
                    ))
    return debt


# ─────────────────────────────────────────────────────────────────
# READ ENDPOINTS — Data served to the frontend (REST & SSE)
# ─────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    """SSE endpoint — persistent event stream for real-time log delivery.
    Progress and logs are pushed from the backend orchestrators.
    """
    return get_sse_response()

@app.route("/")
def index():
    """Serve the single-page dashboard HTML."""
    return render_template(
        "index.html",
        hcash_logo_url=config.HCASH_LOGO_URL,
        avax_logo_url=config.AVAX_LOGO_URL,
        action_names=config.ACTION_NAMES,
    )

@app.route("/api/wallets")
def api_wallets():
    """Return the list of configured wallets (public identifiers only).

    Response: [{name, address, index, is_main}, ...]
    No private keys or sensitive data are exposed.
    """
    return jsonify([
        {"name": w["name"], "address": w["address"], "index": w["index"], "is_main": w["index"] == 1}
        for w in _wallets
    ])

@app.route("/api/config")
def api_config():
    """Return UI configuration constants (URLs, thresholds, chain info).

    These values are set in src/config.py and are safe to expose publicly.
    The frontend uses them for explorer links, logo images, and claim threshold display.
    """
    # api_config fetches static contract addresses
    try:
        contract_addr = get_contract_address()
        hcash_addr    = get_hcash_token_address()
    except RuntimeError:
        contract_addr = "(not initialized)"
        hcash_addr    = "(not initialized)"
    return jsonify({
        "chain_id":        config.CHAIN_ID,
        "contract":        contract_addr,
        "hcash_token":     hcash_addr,
        "claim_threshold": config.CLAIM_THRESHOLD,
        "hcash_logo_url":  config.HCASH_LOGO_URL,
        "avax_logo_url":   config.AVAX_LOGO_URL,
        "explorer_url":    config.BLOCK_EXPLORER_URL,
        "debank_url":      config.DEBANK_URL,
        "debt_threshold":  config.DEBT_THRESHOLD,
        "action_names":    config.ACTION_NAMES,
        "action_keys":     {
            "CLAIM":        config.ACTION_KEY_CLAIM,
            "DISPATCH_GAS": config.ACTION_KEY_DISPATCH_GAS,
            "BATCH_MINERS": config.ACTION_KEY_BATCH_MINERS
        }
    })

@app.route("/api/status")
def api_status():
    """Return current action state and per-wallet progress.

    Polled every ~1s by the frontend while an action is running.
    Returns wallet_statuses (sidebar badges), wallet_details (card data),
    and miner_journeys (NFT lifecycle tracking).
    """
    with _state_lock:
        return jsonify({
            "status":          _app_state["status"],
            "action":          _app_state["action"],
            "wallet_statuses": get_wallet_statuses(),
            "wallet_details":  get_wallet_details(),
            "miner_journeys":  get_miner_journeys(),
            "generic_cards":   get_generic_cards(),
        })

@app.route("/api/miner_types")
def api_miner_types():
    """Return the cached miner types dictionary.

    Used by the frontend to resolve miner names, images, and NFT contracts
    from type indices. Populated at init and refreshable via POST /api/miners/cache/refresh.
    """
    return jsonify(_miner_types)

@app.route("/api/miners/<address>")
def api_miners(address: str):
    """Return wallet details (miners, balances, facility) for a single address.

    Performs a live blockchain read via the RPC node. The address is validated
    as a proper Ethereum address before any Web3 call.
    """
    # F2 fix: validate address format before calling Web3
    if not _ETH_ADDR_RE.match(address):
        return jsonify({"error": f"Invalid address format: {address}"}), 400

    try:
        if _w3 is None or _game_main is None:
            return jsonify({"error": "RPC not initialized"}), 503

        checksum = Web3.to_checksum_address(address)
        info = get_wallet_miners_info(_w3, _game_main, _miner_types, checksum, _game_token)
        info["avax_balance"]  = round(info.get("avax_balance", 0.0), 6)
        info["hcash_balance"] = round(info.get("hcash_balance", 0.0), 4)
        return jsonify(info)
    except Exception as e:
        logger.error(red_bold(f"[API] Miner error ({address}): {e}"))
        return jsonify({"error": str(e)}), 500

@app.route("/api/gas")
def api_gas():
    """Return current Avalanche C-Chain gas price in Gwei.

    Simple RPC read — the frontend caches this for 10s to avoid spam.
    """
    if _w3 is None:
        return jsonify({"error": "Web3 instance missing"}), 503
    try:
        gas_price_gwei = _w3.eth.gas_price / 1e9
        return jsonify({"gas_price_gwei": round(gas_price_gwei, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# WRITE ENDPOINTS — Mutations and actions
# ─────────────────────────────────────────────────────────────────

@app.route("/api/init-status", methods=["GET", "POST"])
def api_init_status():
    """Loader screen initialization endpoint.

    GET:  Returns current init progress (polled by loader.js every 500ms).
    POST: Triggers the engine initialization (operator clicks "Start").
          Resets stale progress data and launches the init thread.
    """
    if request.method == "POST":
        with _init_lock:
            if not _init_status["waiting"]:
                return jsonify({"error": "Initialization already started"}), 409
            # F4 fix: reset stale data from any previous init attempt
            _init_status["waiting"] = False
            _init_status["step"] = "Starting..."
            _init_status["percentage"] = 2
            _init_status["details"] = []
            _init_status["miners"] = []
            _init_status["failed"] = False
            _init_status["error_message"] = ""
            _init_status["results"] = None

        if _init_fn:
            threading.Thread(target=_init_fn, daemon=True).start()
            return jsonify({"success": True})
        else:
            return jsonify({"error": "No initialization function registered"}), 500

    # GET — return current progress snapshot
    with _init_lock:
        return jsonify(_init_status)

@app.route('/api/system/alerts', methods=['GET'])
def get_system_alerts():
    """Returns the list of currently active system-wide alerts."""
    return jsonify({"alerts": list(get_active_alerts().values())})

@app.route("/api/miners/batch", methods=["POST"])
def api_miners_batch():
    """Return miner data for multiple wallets in a single Multicall3 pass.

    Expects JSON body: {"addresses": ["0x...", "0x...", ...]}
    This is the primary data loading endpoint — more efficient than
    calling /api/miners/<addr> individually for each wallet.
    """
    # F1 fix: strict JSON parsing instead of force=True
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    try:
        addresses = data.get("addresses", [])
        if not addresses:
            return jsonify({"error": "No addresses provided"}), 400

        logger.info(cyan_bold(f"[API] Manual refresh requested for {len(addresses)} wallet(s)."))
        if _w3 is None or _game_main is None:
            return jsonify({"error": "RPC not initialized"}), 503

        batch_results = get_batch_wallets_miners_info(
            _w3, addresses, _game_main, _game_token, _miner_types
        )

        # Update debt detection cache (piggybacks on this existing RPC call)
        with _batch_data_lock:
            # Normalize keys to lowercase for consistency
            _cached_batch_data.update({k.lower(): v for k, v in batch_results.items()})

        _update_debt_alerts()

        # Round values for clean UI display
        for addr in batch_results:
            info = batch_results[addr]
            info["avax_balance"]  = round(info.get("avax_balance", 0.0), 6)
            info["hcash_balance"] = round(info.get("hcash_balance", 0.0), 4)

        return jsonify(batch_results)
    except Exception as e:
        logger.error(red_bold(f"[API] {config.ACTION_NAMES[config.ACTION_KEY_BATCH_MINERS]} error: {e}"))
        return jsonify({"error": str(e)}), 500

@app.route("/api/miners/cache/refresh", methods=["POST"])
def api_miners_cache_refresh():
    """Force a full refresh of the miner types cache from the official API.

    This re-downloads all miner metadata (names, images, NFT contracts)
    and replaces the in-memory cache. Rate-limit errors are forwarded to
    the frontend as toast alerts via SSE.
    """
    global _miner_types

    if _w3 is None or _game_main is None:
        return jsonify({"error": "RPC not initialized"}), 503
    try:
        new_cache = refresh_miner_cache_if_needed(_w3, _game_main, force=True)
        _miner_types.clear()
        _miner_types.update(new_cache)
        return jsonify({"success": True, "count": len(_miner_types)})
    except HCashApiRateLimitError as e:
        _broadcast({
            "type":        "rate_limit_alert",
            "message":     str(e),
            "retry_after": e.retry_after,
        })
        logger.error(red_bold(f"[API] Rate limit reached during cache refresh: {e}"))
        return jsonify({"error": str(e), "retry_after": e.retry_after}), 429
    except HCashApiError as e:
        _broadcast({"type": "rate_limit_alert", "message": f"API Error: {e}", "retry_after": None})
        logger.error(red_bold(f"[API] API Error during cache refresh: {e}"))
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        logger.error(red_bold(f"[API] Unexpected error during cache refresh: {e}"))
        return jsonify({"error": str(e)}), 500

def _validate_payload_security(data: dict) -> None:
    """
    Performs a deep scan of the action payload to ensure all destinations and contracts are whitelisted. 
    Raises SecurityException on violation.
    """
    action = data.get("action")
    
    # 1. Validate 'burner1_address' for global actions
    if burner := data.get("burner1_address"):
        validate_authorized_wallet(burner, "Global Destination")

    # 2. Validate 'wallets_actions' for Batch Miner actions
    if action == config.ACTION_KEY_BATCH_MINERS:
        wallets_actions = data.get("wallets_actions", {})
        for w_name, acts in wallets_actions.items():
            if not isinstance(acts, dict): continue
            
            # Check Transfers
            for t in acts.get("transfers", []):
                validate_authorized_wallet(t.get("dest"), f"NFT Transfer Dest ({w_name})")
                
                # Check NFT contract if provided
                if nft_addr := t.get("nft"):
                    if nft_addr.lower() != "undefined" and nft_addr != config.NULL_ADDRESS:
                        validate_contract(nft_addr, f"NFT Contract ({w_name})")
            
            # Check Places (target_nft)
            for p in acts.get("places", []):
                if nft_addr := p.get("nft"):
                    if nft_addr.lower() != "undefined" and nft_addr != config.NULL_ADDRESS:
                        validate_contract(nft_addr, f"Place Contract ({w_name})")

@app.route("/api/run", methods=["POST"])
def api_run():
    """Start an asynchronous blockchain action for selected wallets.

    Expects JSON body with:
      - action: ACTION_KEY_CLAIM | ACTION_KEY_DISPATCH_GAS | ACTION_KEY_BATCH_MINERS
      - wallets: ["BURNER-1", ...] (for claim/dispatch_gas)
      - wallets_actions: {...}     (for batch_miners only)

    Lifecycle:
      1. Validates input and checks no action is already running
      2. Resets previous UI state (cards, badges)
      3. Launches the action in a background thread
      4. Returns immediately — progress is pushed via SSE
    """
    # F1 fix: strict JSON parsing
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    action = data.get("action")
    names  = data.get("wallets", [])

    # Validate action type dynamically from our source of truth
    valid_actions = list(config.ACTION_NAMES.keys())
    if action not in valid_actions:
        return jsonify({"error": f"Invalid action: {action}"}), 400

    # F3 fix: atomic lock check + set to prevent TOCTOU race
    with _state_lock:
        if _app_state["status"] == "running":
            return jsonify({"error": "Action already in progress"}), 409
        _app_state["status"] = "running"
        _app_state["action"] = action

    # SECURITY SCALE: Universal Integrity & Transaction Guard
    # We perform a synchronous pre-check of the payload before even starting the thread.
    try:
        _validate_payload_security(data)
    except SecurityException as e:
        # State remains idle, we don't start anything.
        return jsonify({"error": str(e)}), 403

    # Clear all previous action data before starting a new one to ensure a clean state
    logger.info(yellow_bold(f"[API] 🧹 Clearing global UI state for new action: {action}"))
    reset_ui_state()

    # ── DEBT SAFETY GUARD ─────────────────────────────────────────
    # For dangerous actions (claim, batch_miners with withdraw/place), check cached wallet data for facility debt before allowing execution.
    # Dispatch Gas and NFT Transfers are safe and bypass this guard.

    # --- Manage Miner & NFT (special payload) ---
    if action == config.ACTION_KEY_BATCH_MINERS:
        wallets_actions = data.get("wallets_actions", {})
        names = list(wallets_actions.keys())
        selected = [w for w in _wallets if w["name"] in names]
        if not selected:
            # Revert state since we already set it to running
            with _state_lock:
                _app_state["status"] = "idle"
                _app_state["action"] = None
            return jsonify({"error": "No wallet selected for batch"}), 400

        # Debt guard: block wallets with debt from withdraw/place (transfers are safe)
        dangerous_wallet_names = [
            wn for wn, acts in wallets_actions.items()
            if isinstance(acts, dict) and (acts.get("withdraws") or acts.get("places"))
        ]
        if dangerous_wallet_names:
            debt_wallets = _get_debt_wallets(dangerous_wallet_names)
            if debt_wallets:
                # Remove debt wallets from wallets_actions for dangerous operations
                for dw in debt_wallets:
                    if dw in wallets_actions:
                        acts = wallets_actions[dw]
                        if isinstance(acts, dict):
                            acts.pop("withdraws", None)
                            acts.pop("places", None)
                            # If no transfers remain, remove the wallet entirely
                            if not acts.get("transfers"):
                                del wallets_actions[dw]

                _broadcast({"type": "debt_wallets_blocked", "wallets": list(debt_wallets)})
                logger.warning(yellow_bold(f"[DEBT] Blocked {len(debt_wallets)} wallet(s) from dangerous operations: {', '.join(debt_wallets)}"))

                # Rebuild names and selected after filtering
                names = list(wallets_actions.keys())
                selected = [w for w in _wallets if w["name"] in names]

                if not selected:
                    with _state_lock:
                        _app_state["status"] = "idle"
                        _app_state["action"] = None
                    return jsonify({
                        "error": "All selected wallets have facility debt. Dangerous operations (Withdraw/Place) blocked.",
                        "debt_wallets": list(debt_wallets)
                    }), 400

        all_miner_plans = {}
        # Prepare global miner tracking (Journey) early to avoid flicker
        for w_name, w_actions in wallets_actions.items():
            if not isinstance(w_actions, dict): continue
            for act_key, act_list in w_actions.items():
                if act_key not in ["withdraws", "transfers", "places"]: continue
                for item in act_list:
                    m_id = item["id"]
                    if m_id not in all_miner_plans:
                        all_miner_plans[m_id] = {
                            "m_id": m_id,
                            "nft_id": item.get("nft_token_id"),
                            "game_id": item.get("game_id"),
                            "name": item.get("name") or f"Miner #{m_id}",
                            "image": item.get("image"),
                            "planned": set()
                        }
                    step_type = act_key.rstrip('s').capitalize()
                    all_miner_plans[m_id]["planned"].add(step_type)
                    if step_type == "Transfer":
                        all_miner_plans[m_id]["planned"].add("Received")

        for m_id, p in all_miner_plans.items():
            order = ["Withdraw", "Transfer", "Received", "Place"]
            sorted_steps = [s for s in order if s in p["planned"]]
            _prepare_miner_journey(m_id, p["nft_id"], p["name"], p["image"], sorted_steps, game_id=p.get("game_id"))

        thread = threading.Thread(
            target=_run_batch_miners_background,
            args=(selected, wallets_actions),
            daemon=True,
        )
        thread.start()
        return jsonify({"success": True, "action": action})

    # --- Standard actions (claim, dispatch_gas) ---
    selected = [w for w in _wallets if w["name"] in names]
    if not selected:
        with _state_lock:
            _app_state["status"] = "idle"
            _app_state["action"] = None
        return jsonify({"error": "No wallet selected"}), 400

    # Debt guard for Claim action
    if action == config.ACTION_KEY_CLAIM:
        debt_wallets = _get_debt_wallets(names)
        if debt_wallets:
            healthy_names = [n for n in names if n not in debt_wallets]
            _broadcast({"type": "debt_wallets_blocked", "wallets": list(debt_wallets)})
            logger.warning(yellow_bold(f"[DEBT] Blocked {len(debt_wallets)} wallet(s) from Claim: {', '.join(debt_wallets)}"))

            if not healthy_names:
                with _state_lock:
                    _app_state["status"] = "idle"
                    _app_state["action"] = None
                return jsonify({
                    "error": "All selected wallets have facility debt. Claim action blocked.",
                    "debt_wallets": list(debt_wallets)
                }), 400
            names = healthy_names

    # --- Early Initialization (Atomic) ---
    selected = [w for w in _wallets if w["name"] in names]
    for w in selected:
        _init_detail(w["name"], w["address"], status="running")

    thread = threading.Thread(
        target=_run_action_background,
        args=(action, names),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "action": action})


# ─────────────────────────────────────────────────────────────────
# BACKGROUND ACTION RUNNERS
# ─────────────────────────────────────────────────────────────────

def _finish_action(msg: dict) -> None:
    """Finalize any action: broadcast result, update status, refresh data.

    Called in the `finally` block of every background action thread.
    Sequence:
      1. Set app status to "done" immediately
      2. Broadcast the result message to the frontend via SSE
      3. Wait 2s for blockchain state to settle (block confirmation)
      4. Fetch fresh balances/inventories via Multicall3
      5. Push updated data to the frontend via SSE
    """

    # 1. Signal completion to the UI immediately
    with _state_lock:
        _app_state["status"] = "done"
    _broadcast(msg)

    # 2. Delayed refresh — avoids reading stale on-chain state
    logger.debug(magenta_bold("⏳ 2s pause before final synchronization..."))
    time.sleep(2)

    try:
        addresses = [w["address"] for w in _wallets]
        logger.info(yellow_bold(f"[APP] Starting post-action global refresh for {len(addresses)} wallet(s)..."))
        data = get_batch_wallets_miners_info(
            _w3, addresses, _game_main, _game_token, _miner_types
        )

        # Update debt detection cache (piggybacks on this existing RPC call)
        with _batch_data_lock:
            # Normalize keys to lowercase for consistency
            _cached_batch_data.update({k.lower(): v for k, v in data.items()})

        _update_debt_alerts()

        _broadcast({
            "type": "miner_data_update",
            "miner_data": data,
        })
        logger.info(green_bold("[APP] ✓ Synchronization completed"))
    except Exception as e:
        logger.error(red_bold(f"[APP] Final refresh failed: {e}"))

def _run_action_background(action: str, names: list[str]) -> None:
    """Background thread dispatcher for standard actions (claim, dispatch_gas).

    Imports the action module lazily to avoid circular imports and to keep
    startup fast. Results are packed into an SSE message and finalized
    via _finish_action().
    """
    msg = {"type": "action_done", "action": action}
    try:
        target_wallets = [w for w in _wallets if w["name"] in names]

        if action == config.ACTION_KEY_CLAIM:
            res = run_claim_all(target_wallets, _burner1_address)
            msg["total_claimed"] = res.get("total_claimed", 0.0)
            msg["summary"] = res.get("summary", "")
            msg["status"] = res.get("status", "success")

        elif action == config.ACTION_KEY_DISPATCH_GAS:
            res = run_dispatch_gas(target_wallets, _burner1_address)
            msg["summary"] = res.get("summary", "")
            msg["status"] = res.get("status", "success")

    except Exception as e:
        logger.error(red_bold(f"[APP] {action} action failed: {e}"))
        msg["error"] = str(e)
        # F6 fix: ensure the frontend shows the correct error toast
        msg["status"] = "error"
    finally:
        _finish_action(msg)

def _run_batch_miners_background(target_wallets: list[dict], data: dict) -> None:
    """Background thread for batch miner operations (withdraw/transfer/place).

    Registers ALL wallet names first so that transfer destinations can
    resolve addresses to human-readable names (even non-participant wallets).
    """
    msg = {"type": "action_done", "action": config.ACTION_KEY_BATCH_MINERS}
    try:
        res = run_all_miners_batches(target_wallets, data)
        msg["wallets_results"] = res.get("wallets_results", {})
        msg["summary"] = res.get("summary", "")
        msg["status"] = res.get("status", "success")
    except Exception as e:
        logger.error(red_bold(f"[APP] {config.ACTION_KEY_BATCH_MINERS} orchestrator failed: {e}"))
        msg["error"] = str(e)
        # F6 fix: ensure the frontend shows the correct error toast
        msg["status"] = "error"
    finally:
        _finish_action(msg)

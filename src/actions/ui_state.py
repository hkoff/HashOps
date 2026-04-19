# src/actions/ui_state.py — Standard management of UI statuses and details

import threading
from typing import Dict, Any, Optional

# ─────────────────────────────────────────────────────────────────
# STATUS TRACKING (UI)
# ─────────────────────────────────────────────────────────────────
_wallet_statuses: Dict[str, str] = {}
_status_lock = threading.Lock()

def get_wallet_statuses() -> Dict[str, str]:
    """Returns a copy of the current wallet statuses."""
    with _status_lock:
        return dict(_wallet_statuses)

def _set_status(name: str, status: str) -> None:
    """Updates the global status of a wallet (Sidebar + Details Card)."""
    with _status_lock:
        _wallet_statuses[name] = status
    with _details_lock:
        if name in _wallet_details:
            _wallet_details[name]["status"] = status

# ─────────────────────────────────────────────────────────────────
# RICH DETAILS (UI)
# ─────────────────────────────────────────────────────────────────
_wallet_details: Dict[str, Dict[str, Any]] = {}
_address_to_name: Dict[str, str] = {}
_miner_journeys: Dict[str, Dict[str, Any]] = {}
_generic_cards: Dict[str, Dict[str, Any]] = {}
_details_lock = threading.Lock()

def register_wallet_names(wallets: list) -> None:
    """Registers the address → name mapping for ALL known wallets.
    Enables name resolution even for non-participating wallets (e.g., transfer destinations)."""
    with _details_lock:
        for w in wallets:
            _address_to_name[w["address"].lower()] = w["name"]

def get_wallet_name(address: str) -> str:
    """Returns the name associated with an address, or the short address if unknown."""
    addr_low = address.lower()
    with _details_lock:
        if addr_low in _address_to_name:
            return _address_to_name[addr_low]
    return f"{address[:6]}...{address[-4:]}"

def get_wallet_details() -> Dict[str, Dict[str, Any]]:
    """Returns the complete details of each wallet for display."""
    with _details_lock:
        return {k: dict(v) for k, v in _wallet_details.items()}

def get_miner_journeys() -> Dict[str, Dict[str, Any]]:
    """Returns the complete journey status for each involved miner."""
    with _details_lock:
        return {k: dict(v) for k, v in _miner_journeys.items()}

def get_generic_cards() -> Dict[str, Dict[str, Any]]:
    """Returns the complete state of generic summary cards."""
    with _details_lock:
        return {k: dict(v) for k, v in _generic_cards.items()}

def _init_detail(name: str, address: str, **kwargs) -> None:
    """Initializes or resets the data structure for a wallet.
    Accepts kwargs to populate initial state atomically (e.g., initial_balance, status)."""
    with _details_lock:
        _address_to_name[address.lower()] = name
        
        # Idempotent initialization: only create fresh entry if missing
        if name not in _wallet_details:
            _wallet_details[name] = {
                "name": name, "address": address, "status": "idle",
                "initial_balance": None, "initial_pending": None, "target_balance": None,
                "balance": None, "pending": None, "total": None,
                "claim_tx": None, "claim_status": None,
                "actual_claimed": None,
                "transfer_amount": None, "transfer_tx": None, "transfer_status": None,
                "error": None, "batch_summary": None, "recap_html": None,
                "transfer_avax_txs": {},
            }
        # Apply initial overrides (e.g. status="running", initial_balance=X)
        if kwargs:
            _wallet_details[name].update(kwargs)
    # Sync with sidebar if status is provided in kwargs
    if "status" in kwargs:
        _set_status(name, kwargs["status"])

def _set_avax_tx(name: str, tx_id: str, tx_info: Dict[str, Any]) -> None:
    """Updates or adds an AVAX transfer entry for a wallet."""
    with _details_lock:
        if name in _wallet_details:
            if "transfer_avax_txs" not in _wallet_details[name]:
                _wallet_details[name]["transfer_avax_txs"] = {}
            _wallet_details[name]["transfer_avax_txs"][tx_id] = tx_info

def _upd(name: str, **kwargs) -> None:
    """Updates wallet details in a thread-safe manner."""
    # If status is present, use _set_status to ensure Sidebar sync
    if "status" in kwargs:
        _set_status(name, kwargs.pop("status"))
    with _details_lock:
        if name in _wallet_details:
            _wallet_details[name].update(kwargs)

def _prepare_miner_journey(m_id: int, nft_id: Optional[int], name: str, image: Optional[str], planned_steps: list, game_id: Optional[int] = None) -> None:
    """Initializes a miner's tracking at the start of an orchestrator. Idempotent."""
    with _details_lock:
        s_id = str(m_id)
        # We only initialize if not already present or if we need a refresh of core metadata
        _miner_journeys[s_id] = {
            "m_id": m_id,
            "nft_id": nft_id,
            "game_id": game_id,
            "name": name,
            "image": image,
            "planned": planned_steps, # List of strings ['Withdraw', 'Transfer', 'Place']
            "steps": _miner_journeys.get(s_id, {}).get("steps", [])
        }

def _log_miner_action(wallet_name: str, m_id: int, step_type: str, tx_url: Optional[str] = None, status: Optional[str] = None, miner_name: Optional[str] = None, dest: Optional[str] = None, nft_id: Optional[int] = None, error_msg: Optional[str] = None) -> None:
    """Logs a single action on a miner within its global journey."""
    with _details_lock:
        s_id = str(m_id)
        if s_id not in _miner_journeys:
            # Fallback if not prepared (should not happen with the modern orchestrator)
            _miner_journeys[s_id] = {
                "m_id": m_id, "nft_id": nft_id,  "name": miner_name or f"Miner #{m_id}",
                "image": None, "planned": [], "steps": []
            }
        
        journey = _miner_journeys[s_id]
        if miner_name: journey["name"] = miner_name
        if nft_id: journey["nft_id"] = nft_id

        steps = journey["steps"]
        found = False
        for s in steps:
            # If we already have this type of step for this specific wallet, update it
            if s["type"] == step_type and s.get("wallet") == wallet_name:
                if status: s["status"] = status
                if tx_url: s["tx"] = tx_url
                if dest: s["dest"] = dest
                if error_msg: s["error_msg"] = error_msg
                found = True
                break
        
        if not found:
            step_obj = {
                "type": step_type, 
                "wallet": wallet_name,
                "status": status or "pending"
            }
            if tx_url: step_obj["tx"] = tx_url
            if dest: step_obj["dest"] = dest
            if error_msg: step_obj["error_msg"] = error_msg
            steps.append(step_obj)

def _init_generic_card(card_id: str, title: str, status: str = "running", **kwargs) -> None:
    """Initializes a generic summary/recap card."""
    with _details_lock:
        _generic_cards[card_id] = {
            "id": card_id,
            "title": title,
            "status": status,
            **kwargs
        }

def _upd_generic_card(card_id: str, **kwargs) -> None:
    """Updates a generic card in a thread-safe manner."""
    with _details_lock:
        if card_id in _generic_cards:
            _generic_cards[card_id].update(kwargs)

def reset_ui_state() -> None:
    """Resets ALL statuses and details (before a new UI action)."""
    with _status_lock:
        _wallet_statuses.clear()
    with _details_lock:
        _wallet_details.clear()
        _miner_journeys.clear()
        _generic_cards.clear()

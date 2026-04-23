# src/services/marketplace_cache.py
#
# Stateful tracker for the Marketplace to avoid O(N) full scans.
# Stores the highest listing ID scanned and the known active listing IDs belonging to our wallets.

import os
import json
from typing import Dict, Any, List, Set

from src.services.logger_setup import logger
from src.utils.helpers import cyan_bold, yellow_bold

CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "marketplace_cache.json")

_cache_state: Dict[str, Any] = {
    "last_scanned_id": 0,
    "active_listings": {}  # Dict[str, Dict] (stringified listingId -> listing dict)
}
_loaded = False

def _ensure_data_dir():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

def load_marketplace_state() -> Dict[str, Any]:
    global _loaded, _cache_state
    if _loaded:
        return _cache_state
        
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _cache_state["last_scanned_id"] = data.get("last_scanned_id", 0)
                _cache_state["active_listings"] = data.get("active_listings", {})
            _loaded = True
        except Exception as e:
            logger.warning(yellow_bold(f"[MARKETPLACE] Failed to load marketplace cache: {e}"))
    else:
        _loaded = True
        
    return _cache_state

def save_marketplace_state(last_scanned_id: int, active_listings: Dict[str, Any]) -> None:
    global _cache_state
    _cache_state["last_scanned_id"] = last_scanned_id
    _cache_state["active_listings"] = active_listings
    
    try:
        _ensure_data_dir()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache_state, f, indent=4)
    except Exception as e:
        logger.warning(yellow_bold(f"[MARKETPLACE] Failed to save marketplace cache: {e}"))

def reset_marketplace_state() -> None:
    """Forces a full rescan on next execution."""
    global _cache_state, _loaded
    _cache_state = {
        "last_scanned_id": 0,
        "active_listings": {}
    }
    _loaded = True
    save_marketplace_state(0, {})

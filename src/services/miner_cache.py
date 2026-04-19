# src/services/miner_cache.py — Local cache for hCASH miner types

# Data strategy: strict source of truth = official API.
# No fallback RPC calls, the HashCash registry is the reference.
# Any refresh completely overwrites the previous cache.

import json
from datetime import datetime
from typing import Dict, Any
from web3 import Web3

from src.config import CACHE_FILE_PATH
from src.services.logger_setup import logger
from src.utils.helpers import green_bold, yellow_bold, cyan_bold, red_bold, magenta_bold

def _empty_cache() -> Dict[str, Any]:
    return {"cached_max_index": 0, "last_updated": None, "miners": {}}

def load_miner_cache() -> Dict[str, Any]:
    """Loads cache from disk."""
    if not CACHE_FILE_PATH.exists():
        return _empty_cache()
    try:
        with CACHE_FILE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "miners" not in data:
            return _empty_cache()
        return data
    except Exception as e:
        logger.warning(yellow_bold(f"[CACHE] Read error: {str(e)}"))
        return _empty_cache()

def save_miner_cache(cache: Dict[str, Any]) -> None:
    """Saves cache to disk."""
    try:
        cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        CACHE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE_PATH.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(red_bold(f"[CACHE] Save failure: {str(e)}"))


# ─────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT (HASHCASH API ONLY)
# ─────────────────────────────────────────────────────────────────
def refresh_miner_cache_if_needed(
    w3: Any,
    game_main: Any,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Updates miner types cache from official API (source of truth).
    Preserves NO old data. The dictionary is entirely overwritten.
    On-chain RPC fallback has been removed to comply with HashCash release tracker.
    """
    cache = load_miner_cache()
    cached_miners = cache.get("miners", {})

    # Normal boot with cache → disk read only
    if not force and cached_miners:
        m_count = sum(1 for m in cached_miners.values() if m.get("category") == "miner_nft")
        e_count = sum(1 for m in cached_miners.values() if m.get("category") == "external_nft")
        logger.info(green_bold(
            f"[CACHE] {m_count} miners + {e_count} external NFTs loaded from disk ✓"
        ))
        return cached_miners

    if not force and not cached_miners:
        logger.info(cyan_bold("[CACHE] Cache empty — initial synchronization..."))
        force = True

    logger.info(magenta_bold("[CACHE] Full cache refresh from HashCash API..."))

    from src.core.hcash_api import get_client
    api_client = get_client()
    registry   = api_client.fetch_contracts()

    miner_nfts   = registry.get("by_category", {}).get("miner_nft", [])
    external_nfts = registry.get("by_category", {}).get("external_nft", [])
    
    if not miner_nfts and not external_nfts:
        logger.warning(yellow_bold("[CACHE] No NFTs (miner or external) in API registry"))
        return cached_miners

    new_miners = {}
    max_idx = 0

    # 1. Classical Miners Processing
    for c in miner_nfts:
        m_idx = c.get("minerIndex")
        if m_idx is None: continue
        m_idx   = int(m_idx)
        idx_str = str(m_idx)
        max_idx = max(max_idx, m_idx)

        stats = c.get("minerStats") or {}
        hashrate_mhps = stats.get("hashrateMhps") or 0
        power_raw = stats.get("powerRaw", 0)

        new_miners[idx_str] = {
            "minerIndex":           m_idx,
            "id":                   c.get("id"),
            "category":             "miner_nft",
            "nftContract":          Web3.to_checksum_address(c["address"]),
            "nft_name":             c.get("name", f"Miner #{m_idx}"),
            "nft_image":            c.get("imageUrl", ""),
            "hashrate":             int(hashrate_mhps),
            "power":                int(power_raw),
            "hashrate_formatted":   stats.get("hashrateFormatted", ""),
            "power_formatted":      stats.get("powerFormatted", ""),
        }

    # 2. External NFTs Processing (Crafting, Materials, etc.)
    for c in external_nfts:
        c_id = c.get("id")
        if not c_id: continue
        
        # Using 'ext:' prefix to avoid any collision with minerIndex
        idx_str = f"ext:{c_id}"
        
        new_miners[idx_str] = {
            "minerIndex":           None,
            "id":                   c_id,
            "category":             "external_nft",
            "nftContract":          Web3.to_checksum_address(c["address"]),
            "nft_name":             c.get("name", "External NFT"),
            "nft_image":            c.get("imageUrl", ""),
            "hashrate":             0,
            "power":                0,
            "hashrate_formatted":   "",
            "power_formatted":      "",
        }

    # Total overwrite (if a category disappears from the API, it disappears from our cache)
    cache["miners"] = new_miners
    cache["cached_max_index"] = max_idx
    save_miner_cache(cache)

    total_m = len(miner_nfts)
    total_e = len(external_nfts)
    logger.info(green_bold(
        f"[CACHE] ✓ Cache saved — {total_m} miners + {total_e} external NFTs"
    ))
    return new_miners


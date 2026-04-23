# src/services/marketplace_engine.py
#
# Dedicated engine for Marketplace synchronization.
# Handles the logic of discovering new listings and verifying the freshness of our known active listings via Multicall3.

import time
from typing import List, Dict, Any, Optional
from web3 import Web3

from src.services.logger_setup import logger
from src.utils.helpers import cyan_bold, red_bold, yellow_bold
from src.actions.ui_alerts import push_system_alert, remove_system_alert
from src.core.wallets import load_wallets
from src.services.marketplace_cache import load_marketplace_state, save_marketplace_state

def _format_listing_tuple(l: tuple) -> Dict[str, Any]:
    """Helper to convert a raw Listing struct tuple to a dictionary."""
    return {
        "listingId":      int(l[0]),
        "tokenId":        int(l[1]),
        "quantity":       int(l[2]),
        "pricePerToken":  int(l[3]),
        "startTimestamp": int(l[4]),
        "endTimestamp":   int(l[5]),
        "listingCreator": str(l[6]),
        "assetContract":  str(l[7]),
        "currency":       str(l[8]),
        "tokenType":      int(l[9]),
        "status":         int(l[10]),
        "reserved":       bool(l[11]),
    }

def _decode_single_listing_bytes(w3: Web3, data: bytes) -> Optional[Dict[str, Any]]:
    """Decodes a single IDirectListings.Listing struct from Multicall bytes."""
    if not data: return None
    try:
        l = w3.codec.decode(['(uint256,uint256,uint256,uint256,uint128,uint128,address,address,address,uint8,uint8,bool)'], data)[0]
        return _format_listing_tuple(l)
    except Exception: 
        return None

def sync_user_marketplace_listings(w3: Web3, mc: Any, marketplace: Any, chunk_size: int = 500) -> List[Dict[str, Any]]:
    """
    Stateful incremental synchronization engine for the user's marketplace listings.
    
    This function ensures that the web interface always displays the most accurate and 
    up-to-date information regarding the user's listed NFTs, without overloading the RPC node.
    
    Workflow:
    - Phase A (Discovery): Uses `getAllListings` to find newly created global listings since the last scan. If a new listing belongs to one of our wallets, it tracks its ID.
    - Phase B (Verification): Uses a single `Multicall3` with `getListing(id)` to instantly refresh the status, price, and expiration of ALL our tracked listings. Dead listings (sold/cancelled) are automatically purged from the tracker.
    - Phase C (Persistence): Saves the state (last scanned ID, active tracked IDs) to disk.
    
    Args:
        w3 (Web3): The active Web3 instance.
        mc (Any): The Multicall3 contract instance.
        marketplace (Any): The HashCash Marketplace contract instance.
        chunk_size (int): The number of listings to fetch per pagination chunk during discovery.
        
    Returns:
        List[Dict[str, Any]]: A list of dictionaries containing fully decoded, active listings 
                              belonging exclusively to the user's wallets.
    """
    state = load_marketplace_state()
    last_scanned_id = state.get("last_scanned_id", 0)
    active_listings_cache = state.get("active_listings", {})
    
    try:
        total = marketplace.functions.totalListings().call()
        remove_system_alert("marketplace-total-failed")
    except Exception as e:
        logger.error(red_bold(f"[MARKETPLACE] Error fetching totalListings: {e}"))
        push_system_alert(
            alert_id="marketplace-total-failed",
            title="Marketplace Synchronization Error",
            message="Unable to read total listings from the contract. The RPC might be struggling. Your active listings may not be completely up to date.",
            alert_type="error",
            section="global"
        )
        # If totalListings fails, we can at least return our offline cache!
        return list(active_listings_cache.values())

    if total == 0:
        return []
    
    now = time.time()
    
    # ─────────────────────────────────────────────────────────
    # PHASE A: DISCOVERY (Find new listings)
    # ─────────────────────────────────────────────────────────
    if total > last_scanned_id:
        logger.info(cyan_bold(f"[MARKETPLACE] Discovering new listings (from #{last_scanned_id} to #{total - 1}) in chunks of {chunk_size}..."))
        
        # Pre-load all our wallets to only track our own listings
        all_wallets = load_wallets()
        our_addresses = set(w["address"].lower() for w in all_wallets)

        consecutive_failures = 0
        current_chunk = chunk_size
        start = last_scanned_id
        
        while start < total:
            end = min(start + current_chunk - 1, total - 1)
            try:
                # getAllListings efficiently grabs a continuous chunk of raw memory
                raw_listings = marketplace.functions.getAllListings(start, end).call()
                for l in raw_listings:
                    l_dict = _format_listing_tuple(l)
                    # IDirectListings.Status: 1 = CREATED/ACTIVE
                    if l_dict["status"] == 1 and l_dict["quantity"] > 0 and l_dict["endTimestamp"] > now:
                        # Security: Only track it if we are the creator
                        if l_dict["listingCreator"].lower() in our_addresses:
                            active_listings_cache[str(l_dict["listingId"])] = l_dict
                
                consecutive_failures = 0
                start += current_chunk
            except Exception as e:
                consecutive_failures += 1
                logger.warning(yellow_bold(f"[MARKETPLACE] Discovery getAllListings({start}-{end}) failed: {e}"))
                
                if consecutive_failures >= 2 and current_chunk > 10:
                    current_chunk = max(10, current_chunk // 2)
                    logger.info(yellow_bold(f"[MARKETPLACE] Reducing discovery page size to {current_chunk}"))
                
                if consecutive_failures >= 5:
                    logger.error(red_bold("[MARKETPLACE] Too many consecutive failures — aborting discovery."))
                    push_system_alert(
                        alert_id="marketplace-discovery-failed",
                        title="Marketplace Discovery Interrupted",
                        message="RPC limits prevented full discovery of new listings. Some newly listed NFTs might not appear until the next refresh.",
                        alert_type="warning",
                        section="global"
                    )
                    break
        
        # Clear warning if we made it through without breaking
        if consecutive_failures < 5:
            remove_system_alert("marketplace-discovery-failed")
            
        # Update cursor to wherever we successfully reached
        last_scanned_id = start

    # ─────────────────────────────────────────────────────────
    # PHASE B: VERIFICATION (Refresh our known listings)
    # ─────────────────────────────────────────────────────────
    if active_listings_cache:
        calls = []
        ids_list = list(active_listings_cache.keys())
        for lid_str in ids_list:
            calls.append({
                "target": marketplace.address,
                "allowFailure": True,
                "callData": marketplace.encode_abi("getListing", [int(lid_str)])
            })
            
        logger.info(cyan_bold(f"[MARKETPLACE] Validating {len(ids_list)} tracked active listings via Multicall..."))
        
        # Safe chunking for Multicall3
        MC_CHUNK = 200
        for i in range(0, len(calls), MC_CHUNK):
            chunk_calls = calls[i:i + MC_CHUNK]
            chunk_ids = ids_list[i:i + MC_CHUNK]
            try:
                results = mc.functions.aggregate3(chunk_calls).call()
                for j, res in enumerate(results):
                    lid_str = chunk_ids[j]
                    if res[0] and res[1]:
                        l_dict = _decode_single_listing_bytes(w3, res[1])
                        if l_dict:
                            # Verify freshness
                            if l_dict["status"] == 1 and l_dict["quantity"] > 0 and l_dict["endTimestamp"] > now:
                                active_listings_cache[lid_str] = l_dict
                            else:
                                # Listing is dead (bought/cancelled/expired). Remove from tracker.
                                if lid_str in active_listings_cache:
                                    del active_listings_cache[lid_str]
            except Exception as e:
                logger.warning(yellow_bold(f"[MARKETPLACE] Verification Multicall batch failed: {e}"))
                # On failure, we KEEP the old data in active_listings_cache to avoid losing our listings on a network glitch!

    # ─────────────────────────────────────────────────────────
    # PHASE C: PERSISTENCE
    # ─────────────────────────────────────────────────────────
    save_marketplace_state(last_scanned_id, active_listings_cache)
    
    return list(active_listings_cache.values())

# src/core/blockchain.py — RPC Communications and hCASH contract management
#
# ABIs and contract addresses are loaded from the official API at startup via:
#   init_blockchain_abis(api_client)       → loads main.v1, bigcoin.v1, nfminer.v1
#   init_blockchain_addresses(registry)    → resolves game_main and game_token

import time
import json
import urllib.request
from typing import List, Dict, Optional, Any, Tuple
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from src.config import CHAIN_ID, MULTICALL_ADDRESS, NULL_ADDRESS, RPC_BATCH_SIZE
from src.utils.helpers import CustomCachingProvider, logger, magenta_bold, red_bold, yellow_bold, cyan_bold, green_bold
from src.core.wallets import get_rpc_url
from src.actions.ui_alerts import push_system_alert, remove_system_alert

# ─────────────────────────────────────────────────────────────────
# DYNAMIC ABIs loaded from HashCash API via init_blockchain_abis
# ─────────────────────────────────────────────────────────────────
_CONTRACT_ABI:      Optional[List] = None  # main.v1
_HCASH_ABI:         Optional[List] = None  # bigcoin.v1
_NFT_ABI:           Optional[List] = None  # nfminer.v1 (ERC-721 standard)

# Dynamically resolved addresses from API registry
_GAME_MAIN_ADDRESS:    Optional[str] = None  # game_main
_GAME_TOKEN_ADDRESS:   Optional[str] = None  # game_token

# Miner registry (minerIndex -> nftAddress) to avoid redundant RPC calls
_MINER_REGISTRY: Dict[int, str] = {}

# Circuit Breaker state: skip API calls for 5 mins if unreachable
_api_offline_until = 0
# RPC Health state: alert if multiple consecutive failures
_consecutive_rpc_errors = 0
_RPC_ERROR_THRESHOLD = 3

# Multicall3: standard universal contract, fixed address across all EVM chains
MULTICALL_ABI = [
    {"inputs": [{"components": [{"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"}, {"name": "callData", "type": "bytes"}], "name": "calls", "type": "tuple[]"}], "name": "aggregate3", "outputs": [{"components": [{"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}], "name": "returnData", "type": "tuple[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "addr", "type": "address"}], "name": "getEthBalance", "outputs": [{"name": "balance", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

# ─────────────────────────────────────────────────────────────────
# INITIALIZATION FROM HASHCASH API
# ─────────────────────────────────────────────────────────────────
def init_blockchain_from_api(api_client: Any, registry: Dict[str, Any]) -> None:
    """
    Initializes contracts (addresses and ABIs) from the HashCash API registry.
    Ensures the "abiId + abiSha256" binding.
    """
    global _CONTRACT_ABI, _HCASH_ABI, _NFT_ABI
    global _GAME_MAIN_ADDRESS, _GAME_TOKEN_ADDRESS, _MINER_REGISTRY

    game_main_list  = registry.get("by_category", {}).get("game_main", [])
    game_token_list = registry.get("by_category", {}).get("game_token", [])
    miner_nft_list  = registry.get("by_category", {}).get("miner_nft", [])

    if not game_main_list:
        raise RuntimeError("[BLOCKCHAIN] 'game_main' contract not found in API registry.")
    if not game_token_list:
        raise RuntimeError("[BLOCKCHAIN] 'game_token' contract not found in API registry.")

    # 1. Game Main
    c_main = game_main_list[0]
    _GAME_MAIN_ADDRESS = Web3.to_checksum_address(c_main["address"])
    if "abiId" in c_main:
        _CONTRACT_ABI = api_client.fetch_abi(c_main["abiId"], expected_sha=c_main.get("abiSha256"))

    # 2. Game Token
    c_token = game_token_list[0]
    _GAME_TOKEN_ADDRESS = Web3.to_checksum_address(c_token["address"])
    if "abiId" in c_token:
        _HCASH_ABI = api_client.fetch_abi(c_token["abiId"], expected_sha=c_token.get("abiSha256"))

    # 3. Miner NFTs & ABI
    _MINER_REGISTRY.clear()
    for c in miner_nft_list:
        m_idx = c.get("minerIndex")
        if m_idx is not None:
            _MINER_REGISTRY[int(m_idx)] = Web3.to_checksum_address(c["address"])

    if miner_nft_list and "abiId" in miner_nft_list[0]:
        c_nft = miner_nft_list[0]
        _NFT_ABI = api_client.fetch_abi(c_nft["abiId"], expected_sha=c_nft.get("abiSha256"))
    else:
        logger.warning(yellow_bold("[BLOCKCHAIN] No 'miner_nft' contract or abiId found in API registry."))

    logger.info(green_bold("[BLOCKCHAIN] ✓ On-chain infrastructure initialized (Addresses + Dynamic ABIs)"))
    logger.info(cyan_bold(f"[BLOCKCHAIN] game_main  → {_GAME_MAIN_ADDRESS}"))
    logger.info(cyan_bold(f"[BLOCKCHAIN] game_token → {_GAME_TOKEN_ADDRESS}"))
    logger.info(cyan_bold(f"[BLOCKCHAIN] {len(_MINER_REGISTRY)} standard miner types indexed in local registry."))

def get_contract_address() -> str:
    """Returns the main contract address (resolved from API)."""
    if _GAME_MAIN_ADDRESS is None:
        raise RuntimeError("[BLOCKCHAIN] Contract address not initialized. Call init_blockchain_from_api() first.")
    return _GAME_MAIN_ADDRESS

def get_hcash_token_address() -> str:
    """Returns the hCASH token address (resolved from API)."""
    if _GAME_TOKEN_ADDRESS is None:
        raise RuntimeError("[BLOCKCHAIN] hCASH token address not initialized. Call init_blockchain_from_api() first.")
    return _GAME_TOKEN_ADDRESS

def get_miner_contract_address(miner_index: int) -> str:
    """Returns the NFT contract address for a given miner type (via local registry)."""
    addr = _MINER_REGISTRY.get(int(miner_index))
    if not addr:
        logger.warning(yellow_bold(f"[BLOCKCHAIN] minerIndex {miner_index} unknown in local registry."))
        return NULL_ADDRESS
    return addr

def _check_abis_ready() -> None:
    """Raises RuntimeError if ABIs are not yet loaded."""
    if _CONTRACT_ABI is None or _HCASH_ABI is None:
        raise RuntimeError(
            "[BLOCKCHAIN] ABIs not loaded. Call init_blockchain_from_api() before using contracts."
        )


# ─────────────────────────────────────────────────────────────────
# WEB3 SINGLETON & CACHES
# ─────────────────────────────────────────────────────────────────
_web3_instance: Optional[Web3] = None
_miner_token_id_cache: Dict[int, int] = {}  # minerId -> tokenId

def get_web3() -> Web3:
    """Returns the global Web3 instance with necessary middlewares."""
    global _web3_instance
    if _web3_instance is None:
        rpc_url = get_rpc_url()
        _web3_instance = Web3(CustomCachingProvider(rpc_url))
        # Middleware for Avalanche (PoA) support
        _web3_instance.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        logger.debug(cyan_bold(f"[BLOCKCHAIN] Web3 Initialized (Provider: HTTP)"))
    return _web3_instance


# ─────────────────────────────────────────────────────────────────
# CONTRACT ACCESS
# ─────────────────────────────────────────────────────────────────
def get_game_main_contract(w3: Web3) -> Any:
    """Main hCASH contract (game_main)."""
    _check_abis_ready()
    return w3.eth.contract(address=_GAME_MAIN_ADDRESS, abi=_CONTRACT_ABI)

def get_game_token_contract(w3: Web3) -> Any:
    """ $hCASH Token contract (game_token)."""
    _check_abis_ready()
    return w3.eth.contract(address=_GAME_TOKEN_ADDRESS, abi=_HCASH_ABI)

def get_nft_contract(w3: Web3, nft_address: str) -> Any:
    """Specific NFT contract for miners (nfminer.v1 = ERC-721 + miner-specific)."""
    _check_abis_ready()
    return w3.eth.contract(address=Web3.to_checksum_address(nft_address), abi=_NFT_ABI)

def get_multicall_contract(w3: Web3) -> Any:
    """Multicall3 contract for optimizing batched calls."""
    return w3.eth.contract(address=Web3.to_checksum_address(MULTICALL_ADDRESS), abi=MULTICALL_ABI)


# ─────────────────────────────────────────────────────────────────
# VIEW FUNCTIONS
# ─────────────────────────────────────────────────────────────────
def get_pending_rewards(w3: Web3, game_main: Any, address: str) -> int:
    """Retrieves pending rewards (Wei format)."""
    return game_main.functions.pendingRewards(address).call()

def get_hcash_balance(w3: Web3, game_token: Any, address: str) -> float:
    """hCASH balance of a user (float format)."""
    raw: int = game_token.functions.balanceOf(address).call()
    return raw / 1e18

def get_avax_balance(w3: Web3, address: str) -> float:
    """Native AVAX balance of a wallet."""
    try:
        raw = w3.eth.get_balance(Web3.to_checksum_address(address))
        return raw / 1e18
    except Exception:
        return 0.0

def check_connection(w3: Web3) -> bool:
    """Verifies that the RPC connection is active and Chain ID is correct."""
    global _consecutive_rpc_errors
    try:
        c_id = w3.eth.chain_id
        is_ok = c_id == CHAIN_ID
        if not is_ok:
            logger.error(red_bold(f"[BLOCKCHAIN] Chain ID error: received {c_id}, expected {CHAIN_ID}"))
            _consecutive_rpc_errors += 1
        else:
            if _consecutive_rpc_errors >= _RPC_ERROR_THRESHOLD:
                # Recovered
                remove_system_alert("rpc-outage")
            _consecutive_rpc_errors = 0
        return is_ok
    except Exception as e:
        _consecutive_rpc_errors += 1
        logger.error(red_bold(f"[BLOCKCHAIN] RPC connection failed: {e}"))
        
        if _consecutive_rpc_errors >= _RPC_ERROR_THRESHOLD:
            push_system_alert(
                alert_id="rpc-outage",
                title="Avalanche RPC Connectivity Issue",
                message=f"The bot is unable to reach the Avalanche network (RPC). Blockchain data might be stale. Check your connection or RPC provider.",
                alert_type="error",
                section="global",
                persistent=True
            )
        return False


# ─────────────────────────────────────────────────────────────────
# MINERS AND FACILITY MANAGEMENT
# ─────────────────────────────────────────────────────────────────
def get_facility_for_user(w3: Web3, game_main: Any, address: str) -> Optional[Dict[str, Any]]:
    """Retrieves facility state for a given address."""
    try:
        f = game_main.functions.getFacilityForUser(address).call()
        return {
            "facilityIndex":    int(f[0]),
            "maxMiners":        int(f[1]),
            "currMiners":       int(f[2]),
            "totalPowerOutput": int(f[3]),
            "currPowerOutput":  int(f[4]),
            "x":                int(f[5]),
            "y":                int(f[6]),
            "electricityCost":  int(f[7]),
            "cooldown":         int(f[8]),
        }
    except Exception as e:
        logger.warning(yellow_bold(f"[BLOCKCHAIN] Error getFacilityForUser ({address}): {e}"))
        return None

def get_placed_miners(w3: Web3, game_main: Any, address: str, page_size: int = 50) -> List[Dict[str, Any]]:
    """Retrieves the list of miners placed in a player's facility."""
    all_miners = []
    start = 0
    while True:
        try:
            page = game_main.functions.getPlayerMinersPaginated(address, start, page_size).call()
            if not page: break
            for m in page:
                all_miners.append({
                    "minerIndex":       int(m[0]),
                    "id":               int(m[1]),
                    "x":                int(m[2]),
                    "y":                int(m[3]),
                    "hashrate":         int(m[4]),
                    "powerConsumption": int(m[5]),
                    "nftContract":      str(m[11]),
                })
            if len(page) < page_size: break
            start += page_size
        except Exception as e:
            logger.error(red_bold(f"[BLOCKCHAIN] Miner pagination error ({address}): {e}"))
            break
    return all_miners

def get_wallet_owned_nfts_api(address: str, miner_types: Dict[str, Any]) -> Dict[str, List[int]]:
    """
    Retrieves NFT inventory via the HashCash API.
    This method is necessary as hCASH ERC721 contracts do not implement
    ERC721Enumerable (tokenOfOwnerByIndex), preventing pure RPC discovery.
    """
    global _api_offline_until
    
    # Circuit Breaker: fast-fail if API is flagged as offline
    if time.time() < _api_offline_until:
        raise ConnectionError("HashCash API currently flagged as offline (Circuit Breaker active)")

    owned = {}
    contract_to_indices = {}
    for idx_str, mt in miner_types.items():
        naddr = mt.get("nftContract")
        if naddr and naddr != NULL_ADDRESS:
            naddr_lower = naddr.lower()
            if naddr_lower not in contract_to_indices:
                contract_to_indices[naddr_lower] = []
            contract_to_indices[naddr_lower].append(idx_str)

    url = f"https://hashcash.club/api/nfts?owner={address.lower()}"
    req = urllib.request.Request(url, headers={"User-Agent": "hCASH-Bot/1.0"})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Flag API as offline for 5 minutes on network/timeout error
        _api_offline_until = time.time() + 300
        raise e
    
    nfts = data.get("data", {}).get("account", {}).get("nfts", [])
    for nft in nfts:
        nft_contract = nft.get("contract", {}).get("id", "").lower()
        token_id = int(nft.get("tokenId", -1))
        if nft_contract in contract_to_indices and token_id >= 0:
            for idx_str in contract_to_indices[nft_contract]:
                if idx_str not in owned: owned[idx_str] = []
                if token_id not in owned[idx_str]: owned[idx_str].append(token_id)
    return owned


# ─────────────────────────────────────────────────────────────────
# MULTICALL DECODING (HELPERS)
# ─────────────────────────────────────────────────────────────────
def _decode_facility(w3: Web3, data: bytes) -> Optional[Dict[str, Any]]:
    """Decodes a Facility struct from Multicall bytes."""
    if not data: return None
    try:
        f = w3.codec.decode(['(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)'], data)[0]
        if int(f[1]) == 0: return None
        return {
            "facilityIndex":    int(f[0]),
            "maxMiners":        int(f[1]),
            "currMiners":       int(f[2]),
            "totalPowerOutput": int(f[3]),
            "currPowerOutput":  int(f[4]),
            "x":                int(f[5]),
            "y":                int(f[6]),
            "electricityCost":  int(f[7]),
            "cooldown":         int(f[8]),
        }
    except Exception: return None

def _decode_miners_list(w3: Web3, data: bytes) -> List[Dict[str, Any]]:
    """Decodes a list of Miner structs from Multicall bytes."""
    if not data: return []
    try:
        # getPlayerMinersPaginated returns tuple[]
        page = w3.codec.decode(['(uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool,address)[]'], data)[0]
        miners = []
        for m in page:
            miners.append({
                "minerIndex":       int(m[0]),
                "id":               int(m[1]),
                "x":                int(m[2]),
                "y":                int(m[3]),
                "hashrate":         int(m[4]),
                "powerConsumption": int(m[5]),
                "nftContract":      str(m[11]),
            })
        return miners
    except Exception: return []


# ─────────────────────────────────────────────────────────────────
# AGGREGATED RETRIEVAL
# ─────────────────────────────────────────────────────────────────
def get_wallet_miners_info(
    w3: Web3,
    game_main: Any,
    miner_types: Dict[str, Any],
    address: str,
    game_token: Any = None,
) -> Dict[str, Any]:
    """Exhaustive retrieval of wallet info via unique batch."""
    res = get_batch_wallets_miners_info(w3, [address], game_main, game_token, miner_types)
    return res.get(address, {})

def get_batch_wallets_miners_info(
    w3: Web3,
    addresses: List[str],
    game_main: Any,
    game_token: Any,
    miner_types: Dict[str, Any],
    on_detail: Optional[Any] = None
) -> Dict[str, Dict[str, Any]]:
    """Retrieves all info for multiple wallets via Multicall3 (in chunks of 50)."""
    mc = get_multicall_contract(w3)
    final_data = {}
    
    # ─────────────────────────────────────────────────────────────
    # Use central RPC_BATCH_SIZE (paquets de 25) for stability
    for i in range(0, len(addresses), RPC_BATCH_SIZE):
        chunk = addresses[i:i + RPC_BATCH_SIZE]
        logger.debug(magenta_bold(f"[BLOCKCHAIN] Updating chunk #{i//RPC_BATCH_SIZE + 1} ({len(chunk)} wallets)..."))
        calls = []
        
        for addr in chunk:
            c_addr = Web3.to_checksum_address(addr)
            # 1. AVAX
            calls.append({"target": mc.address, "allowFailure": True, "callData": mc.encode_abi("getEthBalance", [c_addr])})
            # 2. hCASH (Optional)
            if game_token:
                calls.append({"target": game_token.address, "allowFailure": True, "callData": game_token.encode_abi("balanceOf", [c_addr])})
            else:
                calls.append({"target": NULL_ADDRESS, "allowFailure": True, "callData": b""})
            # 3. Rewards, 4. Facility, 5. Miners, 6. Electricity Fees
            calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("pendingRewards", [c_addr])})
            calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("getFacilityForUser", [c_addr])})
            calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("getPlayerMinersPaginated", [c_addr, 0, 50])})
            calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("electricityCostOwed", [c_addr])})

        results = mc.functions.aggregate3(calls).call()
        
        # 1. Decode primary results (Balances, Facility, Placed miners list, API Inventory NFTs)
        chunk_miners_by_addr = {}
        chunk_owned_by_addr = {}
        for idx, addr in enumerate(chunk):
            base = idx * 6
            r_avax, r_hcash, r_pending, r_facility, r_miners, r_electricity = results[base:base+6]
            
            avax_bal  = int.from_bytes(r_avax[1], "big") / 1e18 if r_avax[0] else 0.0
            hcash_bal = int.from_bytes(r_hcash[1], "big") / 1e18 if r_hcash[0] and r_hcash[1] else 0.0
            pending   = int.from_bytes(r_pending[1], "big") / 1e18 if r_pending[0] else 0.0
            elec_owed = int.from_bytes(r_electricity[1], "big") / 1e18 if r_electricity[0] else 0.0
            net_claim = pending - elec_owed
            
            facility = _decode_facility(w3, r_facility[1]) if r_facility[0] else None
            placed   = _decode_miners_list(w3, r_miners[1]) if r_miners[0] else []
            placed_err = not r_miners[0]
            err_msg  = None

            # 1.B Retrieval of Inventory NFTs with Error Handling & Circuit Breaker
            try:
                owned = get_wallet_owned_nfts_api(addr, miner_types)
                # Successful call: Clear API warning if it was active
                remove_system_alert("inventory-api-offline")
            except Exception as e:
                # Distinguish between first-time failure and fast-fail skip
                is_fast_fail = "Circuit Breaker" in str(e)
                owned = {}
                
                if not is_fast_fail:
                    err_msg = f"HashCash API Unreachable (External Issue) - Unable to sync inventory. Bot core functions remain operational."
                    logger.error(red_bold(err_msg))
                    
                    # Push a global system warning banner
                    push_system_alert(
                        alert_id="inventory-api-offline",
                        title="NFT's owner API Unreachable — Inventory Synchronization Impossible",
                        message="This is an external issue. The bot's core functions remain operational, but your inventory NFTs cannot be retrieved.",
                        alert_type="warning",
                        section="global",
                        persistent=True
                    )
                else:
                    err_msg = "API Offline (Circuit Breaker)"
            
            final_data[addr] = {
                "address": addr, "avax_balance": avax_bal, "hcash_balance": hcash_bal,
                "pending": pending, "electricity_owed": elec_owed, "net_claimable": net_claim,
                "facility": facility, "placed": placed, "owned": owned,
                "inventory_error": err_msg if 'e' in locals() or 'err_msg' in locals() else None,
                "placed_error": placed_err
            }
            chunk_miners_by_addr[addr] = placed
            chunk_owned_by_addr[addr] = owned

        # 2. Resolution of NFT Token IDs via Multicall (Two-Way Strategy)
        # Group all unknown associations in the chunk to resolve in one pass.
        # A. Direct Path: Miner ID (game) -> Token ID (blockchain) [For PLACED miners]
        unknown_placed_ids = set()
        for addr in chunk:
            for m in chunk_miners_by_addr[addr]:
                m_id = m["id"]
                if m_id not in _miner_token_id_cache:
                    unknown_placed_ids.add(m_id)
        
        # B. Reverse Path: Contract + Token ID -> Miner ID (game) [For INVENTORY miners]
        # This allows anticipating the game ID even before the miner is placed.
        unknown_owned_nfts = [] # List of (contract, tokenId, minerIndex)
        known_token_ids = set(_miner_token_id_cache.values())
        
        for addr in chunk:
            owned = chunk_owned_by_addr[addr]
            for idx_str, tokens in owned.items():
                m_type = miner_types.get(idx_str, {})
                
                # Correction: Only true hCASH miners can have a mapped minerId
                if m_type.get("category") != "miner_nft":
                    continue
                    
                nft_addr = m_type.get("nftContract")
                if not nft_addr or nft_addr == NULL_ADDRESS: continue
                
                for t_id in tokens:
                    if t_id not in known_token_ids:
                        unknown_owned_nfts.append((nft_addr, t_id, idx_str))

        # Execute resolution Multicall if necessary
        if unknown_placed_ids or unknown_owned_nfts:
            res_calls = []
            placed_list = list(unknown_placed_ids)
            
            # Direct Path Calls
            for m_id in placed_list:
                res_calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("minerIdToTokenId", [m_id])})
            
            # Reverse Path Calls
            for nft_addr, t_id, _ in unknown_owned_nfts:
                res_calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("contractToTokenIdToMinerId", [nft_addr, t_id])})
            
            try:
                results_all = mc.functions.aggregate3(res_calls).call()
                success_count = 0
                failed_count = 0
                pending_count = 0  # NFTs never placed in-game (no Miner ID yet — expected)
                
                # Decode Direct Path
                for i, m_id in enumerate(placed_list):
                    r = results_all[i]
                    if r[0] and r[1]:
                        t_id = int.from_bytes(r[1], "big")
                        if t_id > 0:
                            _miner_token_id_cache[m_id] = t_id
                            success_count += 1
                            m_info = next((m for addr in chunk for m in chunk_miners_by_addr[addr] if m["id"] == m_id), None)
                            m_name = miner_types.get(str(m_info["minerIndex"]), {}).get("nft_name", "Unknown") if m_info else "Unknown"
                            logger.debug(cyan_bold(f"[BLOCKCHAIN] [{m_name}] Direct Association: Miner ID #{m_id} <-> Token ID #{t_id}"))
                        else:
                            failed_count += 1
                            logger.error(red_bold(f"[BLOCKCHAIN] Resolution failed: Miner ID #{m_id} has no associated Token ID (0)."))

                # Decode Reverse Path
                offset = len(placed_list)
                for i, (nft_addr, t_id, idx_str) in enumerate(unknown_owned_nfts):
                    r = results_all[offset + i]
                    if r[0] and r[1]:
                        m_id = int.from_bytes(r[1], "big")
                        if m_id > 0:
                            _miner_token_id_cache[m_id] = t_id
                            success_count += 1
                            m_name = miner_types.get(idx_str, {}).get("nft_name", "Unknown")
                            logger.debug(cyan_bold(f"[BLOCKCHAIN] [{m_name}] Reverse Association: Miner ID #{m_id} <-> Token ID #{t_id}"))
                        else:
                            # Miner ID == 0: This NFT has never been placed in-game.
                            # Expected for newly assembled/crafted miners sitting in inventory — a Miner ID is only assigned on first placement.
                            pending_count += 1
                            m_name = miner_types.get(idx_str, {}).get("nft_name", "Unknown")
                            logger.debug(cyan_bold(f"[BLOCKCHAIN] [{m_name}] Token #{t_id} has no Miner ID yet (never placed). Pending."))

                if success_count > 0 or failed_count > 0 or pending_count > 0:
                    status_txt = cyan_bold("[BLOCKCHAIN] Miner ID Mapping:")
                    status_str = green_bold(f"✓ {success_count} resolutions")
                    if pending_count > 0:
                        status_str += yellow_bold(f" | ⏳ {pending_count} pending (new miners)")
                    if failed_count > 0:
                        status_str += red_bold(f" | ❌ {failed_count} errors")
                    logger.info(f"{status_txt} {status_str}")
                
            except Exception as e:
                logger.error(red_bold(f"[BLOCKCHAIN] Resolution Multicall failed: {e}"))

        # 3. Final assignment from cache for the entire chunk
        hit_count = 0
        miss_count = 0
        for addr in chunk:
            for m in chunk_miners_by_addr[addr]:
                t_id = _miner_token_id_cache.get(m["id"])
                if t_id:
                    m["nftTokenId"] = t_id
                    hit_count += 1
                else:
                    m["nftTokenId"] = None
                    miss_count += 1
            
        if hit_count > 0 or miss_count > 0:
            logger.debug(magenta_bold(f"[BLOCKCHAIN] Association cache summary: {hit_count} hits, {miss_count} misses for the chunk."))
            
    return final_data

def get_multiple_wallets_data(w3: Web3, addresses: List[str], game_main: Any, game_token: Any) -> Dict[str, Dict[str, Any]]:
    """Retrieves pendingRewards and hcashBalance for a list of wallets via Multicall3 (chunked)."""
    mc = get_multicall_contract(w3)
    data = {}
    
    # SECURITY SCALE: Process in chunks of 25 (RPC_BATCH_SIZE)
    for k in range(0, len(addresses), RPC_BATCH_SIZE):
        chunk = addresses[k:k + RPC_BATCH_SIZE]
        calls = []
        
        for addr in chunk:
            c_addr = Web3.to_checksum_address(addr)
            # Call 1: pendingRewards
            calls.append({"target": game_main.address, "allowFailure": True, "callData": game_main.encode_abi("pendingRewards", [c_addr])})
            # Call 2: balanceOf hCASH
            calls.append({"target": game_token.address, "allowFailure": True, "callData": game_token.encode_abi("balanceOf", [c_addr])})

        logger.debug(magenta_bold(f"[BLOCKCHAIN] Executing Multicall3 chunk ({len(chunk)} wallets)..."))
        results = mc.functions.aggregate3(calls).call()
        
        for i, addr in enumerate(chunk):
            pending_res = results[i*2]
            balance_res = results[i*2 + 1]
            
            pending_float = int.from_bytes(pending_res[1], "big") / 1e18 if pending_res[0] and pending_res[1] else 0.0
            balance_float = int.from_bytes(balance_res[1], "big") / 1e18 if balance_res[0] and balance_res[1] else 0.0
            
            pending_wei = int.from_bytes(pending_res[1], "big") if pending_res[0] and pending_res[1] else 0
            balance_wei = int.from_bytes(balance_res[1], "big") if balance_res[0] and balance_res[1] else 0

            data[addr.lower()] = {
                "pending": pending_float,
                "balance": balance_float,
                "pending_wei": pending_wei,
                "balance_wei": balance_wei
            }
    
    return data

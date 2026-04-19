# src/actions/bricks/place_miner.py — Action Place Miner to the game

from typing import List, Dict, Any
import math

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL, NULL_ADDRESS
from src.services.logger_setup import logger
from src.utils.helpers import red_bold, yellow_bold, cyan_bold, green_bold
from src.core.blockchain import get_nft_contract, get_miner_contract_address

from src.actions.ui_state import _upd, _log_miner_action
from src.actions.utils import format_web3_error
from src.core.security import validate_authorized_wallet, validate_contract, SecurityException

def get_empty_coordinates(max_x: int, max_y: int, max_m: int, placed_coords: set) -> tuple:
    """
    Finds the first free (x, y) pair.
    Priority to contract x/y dimensions, otherwise fallback to maxM.
    """
    # If dimensions are 0, deduce a default grid (width 5)
    w = max_x if max_x > 0 else 5
    h = max_y if max_y > 0 else math.ceil(max_m / w) if max_m > 0 else 20

    for y in range(h):
        for x in range(w):
            if (x, y) not in placed_coords:
                return (x, y)
    return (-1, -1)

def get_facility_and_placed_coords(game_main: Any, address: str) -> tuple:
    """Returns max_x, max_y, max_m, and the set of (x,y) already placed for a given wallet."""
    f_info = game_main.functions.getFacilityForUser(address).call()
    max_m = int(f_info[1])
    max_x = int(f_info[5])
    max_y = int(f_info[6])

    placed = []
    page_size, start = 50, 0
    while True:
        page = game_main.functions.getPlayerMinersPaginated(address, start, page_size).call()
        if not page: break
        for m in page:
            placed.append({"x": int(m[2]), "y": int(m[3])})
        if len(page) < page_size: break
        start += page_size

    placed_coords = set((m["x"], m["y"]) for m in placed)
    return max_x, max_y, max_m, placed_coords

def run_place_batch_for_wallet(
    wallet: Dict[str, Any], places: List[Dict[str, Any]], w3: Web3, 
    game_main: Any, pre_gas: int, gas_params: Dict[str, int],
    base_nonce: int = None
) -> Dict[str, int]:
    """Executes all placements for a wallet, returns tx_hashes."""
    name = wallet["name"]
    address = wallet["address"]
    signer = wallet["signer"]
    
    if not places:
        return {}
    
    tx_hashes = {}
    
    # 1 & 2. Facility Analysis & Retrieval of already placed miners
    try:
        max_x, max_y, max_m, placed_coords = get_facility_and_placed_coords(game_main, address)
        logger.debug(cyan_bold(f"[{name}] Facility: Capacity={max_m} | Native Grid={max_x}x{max_y}"))
    except Exception as e:
        logger.error(red_bold(f"[{name}] Unable to read Facility/Placed Miners: {e}"))
        _upd(name, place_status="error", status="error")
        return {}
        
    if base_nonce is None:
        base_nonce = w3.eth.get_transaction_count(address, "pending")
        
    approved_nfts = set()
    
    # 3. Placement loop
    for i, p_info in enumerate(places):
        try:
            m_id = p_info["id"]  # Miner ID (UI)
            nft_id = p_info.get("nft_token_id") # NFT Token ID (Blockchain)
            p_type_idx = p_info.get("type_idx")
            p_nft = p_info.get("nft")
            p_name = p_info.get("name", "Miner")
            
            if nft_id is None:
                logger.error(red_bold(f"[{name}] CRITICAL Error: Missing NFT ID for {p_name} (Miner #{m_id}). Aborting."))
                _upd(name, place_status="error", status="error")
                break

            # --- NFT contract resolution (Source of truth: hCASH contract) ---
            t_nft = NULL_ADDRESS
            if p_nft and p_nft.lower() != "undefined" and p_nft != NULL_ADDRESS:
                t_nft = Web3.to_checksum_address(p_nft)
            elif p_type_idx is not None:
                # Local resolution via API registry (Saves an RPC call)
                t_nft = get_miner_contract_address(p_type_idx)
                logger.debug(f"[{name}] NFT resolved via local registry for type {p_type_idx} -> {t_nft}")

            if t_nft == NULL_ADDRESS:
                logger.error(red_bold(f"[{name}] Unable to determine NFT contract for {p_name} #{nft_id}. Skipping."))
                continue

            # [SECURITY] Universal Integrity Guard Check
            validate_authorized_wallet(address, f"Place Owner ({name})")
            validate_contract(t_nft, f"NFT Contract ({name})")
            validate_contract(game_main.address, f"Game Main ({name})")

            # --- Auto-Approve Check ---
            if t_nft not in approved_nfts:
                nft_c = get_nft_contract(w3, t_nft)
                is_appr = nft_c.functions.isApprovedForAll(address, game_main.address).call()
                if not is_appr:
                    logger.info(yellow_bold(f"[{name}] (nonce:{base_nonce}) Sending Approve tx for {p_name}..."))
                    appr_tx = nft_c.functions.setApprovalForAll(game_main.address, True).build_transaction({
                        "chainId": CHAIN_ID, "from": address, "nonce": base_nonce,
                        "gas": 120000, **gas_params,
                    })
                    signed_appr = signer.sign_transaction(appr_tx)
                    w3.eth.send_raw_transaction(signed_appr.raw_transaction)
                    logger.debug(green_bold(f"[{name}] (nonce:{base_nonce}) Approve tx broadcast OK"))
                    base_nonce += 1
                approved_nfts.add(t_nft)
            
            # --- Coordinate lookup ---
            cx, cy = get_empty_coordinates(max_x, max_y, max_m, placed_coords)
            if cx == -1 or cy == -1:
                logger.error(red_bold(f"[{name}] No more space available (Capacity {max_m}). Interruption."))
                break
                
            placed_coords.add((cx, cy))
            
            # --- Placement Transaction ---
            # IMPORTANT: placeMiner uses the NFT ID (nft_id)
            logger.info(yellow_bold(f"[{name}] (nonce:{base_nonce}) Sending Place tx for {p_name} (NFT #{nft_id}) at ({cx}, {cy})..."))
            
            tx = game_main.functions.placeMiner(t_nft, nft_id, cx, cy).build_transaction({
                "chainId": CHAIN_ID, "from": address, "nonce": base_nonce,
                "gas": pre_gas, **gas_params,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.debug(green_bold(f"[{name}] (nonce:{base_nonce}) Place tx broadcast OK"))
            
            base_nonce += 1
            url_tx = f"{BLOCK_EXPLORER_URL}/tx/0x{tx_hash.hex()}"
            _log_miner_action(name, m_id, "Place", url_tx, status="pending", miner_name=p_name, nft_id=nft_id)
            
            tx_hashes[f"0x{tx_hash.hex()}"] = m_id
            
        except SecurityException as e:
            logger.critical(red_bold(f"[{name}] SECURITY VIOLATION: {e}"))
            err_msg = str(e)
            _upd(name, place_status="error", status="error", error=err_msg)
            return tx_hashes, base_nonce, err_msg
            
        except Exception as e:
            err_msg = format_web3_error("Place failed", e)
            _upd(name, place_status="error", status="error", error=err_msg)
            logger.error(red_bold(f"[{name}] (nonce:{base_nonce}) Placement error for {p_name} #{m_id}: {e}"))
            return tx_hashes, base_nonce, err_msg

    return tx_hashes, base_nonce, None


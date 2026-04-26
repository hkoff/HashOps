# src/actions/bricks/claim_hcash.py — Action Claim hCASH

from typing import Dict, Any, List

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL
from src.services.logger_setup import logger
from src.utils.helpers import green_bold, red_bold, format_decimal

from src.actions.ui_state import _upd, get_wallet_name, log_wallet_error
from src.actions.utils import format_web3_error
from src.utils.helpers import yellow_bold
from src.core.security import validate_authorized_wallet, validate_contract, SecurityException

def process_claim_receipt(
    receipt: Any, game_token: Any
) -> List[Dict[str, Any]]:
    """
    Extracts all hCASH transfers from a receipt.
    Returns a list of dicts: {"recipient": str, "amount": float, "amount_wei": int}
    """
    found_transfers = []
    
    try:
        hcash_addr = Web3.to_checksum_address(game_token.address)
        
        # Filter logs for the hCASH contract specifically and process them individually to avoid MismatchedABI warnings from logs emitted by other contracts in the receipt.
        for log in receipt.get('logs', []):
            if Web3.to_checksum_address(log.get('address', '')) != hcash_addr:
                continue
                
            try:
                evt = game_token.events.Transfer().process_log(log)
                args = evt.get('args', {})
                recipient = Web3.to_checksum_address(args.get('to', ''))
                v_raw = args.get('value', 0)
                
                found_transfers.append({
                    "recipient": recipient,
                    "amount": v_raw / 1e18,
                    "amount_wei": v_raw
                })
                logger.debug(green_bold(f"[ClaimBrick] hCASH Transfer detected: {v_raw / 1e18} to {get_wallet_name(recipient)}"))
            except Exception:
                # This log index matches the address but not the Transfer signature/layout, skip it safely.
                continue
                
    except Exception as e:
        logger.error(f"[ClaimBrick] Error in process_claim_receipt: {e}")
            
    return found_transfers

def run_claim_single_wallet(
    wallet: Dict[str, Any], w3: Web3, game_main: Any, game_token: Any,
    pre_gas: int, gas_params: Dict[str, int], 
    pending: float, initial_balance: float, initial_balance_wei: int = 0,
    nonce: int = None
) -> Dict[str, Any]:
    """Executes a claim for a wallet. Returns the tx hash for batched polling by PhaseEngine."""
    name = wallet["name"]
    address = wallet["address"]
    
    try:
        # [SECURITY] Universal Integrity Guard Check
        validate_authorized_wallet(address, f"Claimer ({name})")
        validate_contract(game_main.address, f"Game Main ({name})")
        validate_contract(game_token.address, f"Game Token ({name})")

        if nonce is None:
            nonce = w3.eth.get_transaction_count(address, "pending")
            
        _upd(name, claim_status="pending")
        if pending < 0.0001:
            _upd(name, claim_status="success", actual_claimed=0.0)
            logger.info(yellow_bold(f"[{name}] (nonce:{nonce}) Nothing to claim (<0.0001)"))
            return {"wallet": name, "claimed": 0.0, "claimed_wei": 0, "success": True}
        
        logger.info(yellow_bold(f"[{name}] (nonce:{nonce}) Phase 1: Sending claimRewards tx (pending: {format_decimal(pending)} hCASH)..."))
        
        tx = game_main.functions.claimRewards().build_transaction({
            "chainId": CHAIN_ID, "from": address, "nonce": nonce,
            "gas": pre_gas, **gas_params,
        })
        signed = wallet["signer"].sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = f"0x{tx_hash.hex()}"
        logger.debug(green_bold(f"[{name}] (nonce:{nonce}) Phase 1: Claim tx broadcast OK"))
        
        _upd(name, claim_tx=f"{BLOCK_EXPLORER_URL}/tx/{tx_hex}")

        return {"wallet": name, "tx_hash": tx_hex, "success": True, "next_nonce": nonce + 1}

    except SecurityException as e:
        logger.critical(red_bold(f"[{name}] SECURITY VIOLATION: {e}"))
        err_msg = str(e)
        _upd(name, claim_status="error")
        log_wallet_error(name, err_msg, address=address)
        return {"wallet": name, "claimed": 0.0, "success": False, "error": err_msg, "new_balance": initial_balance, "next_nonce": nonce, "error_msg": err_msg}

    except Exception as e:
        err_msg = format_web3_error("Claim failed", e)
        _upd(name, claim_status="error")
        log_wallet_error(name, err_msg, address=address)
        logger.error(red_bold(f"[{name}] (nonce:{nonce}) Claim failed: {e}"))
        return {"wallet": name, "claimed": 0.0, "success": False, "error": err_msg, "new_balance": initial_balance, "next_nonce": nonce, "error_msg": err_msg}

# src/actions/bricks/transfer_hcash.py — Action Transfer hCASH

from typing import Dict, Any, Optional

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL
from src.services.logger_setup import logger
from src.utils.helpers import green_bold, red_bold, yellow_bold, format_decimal

from src.actions.ui_state import _upd, log_wallet_error
from src.actions.utils import format_web3_error
from src.core.security import ANCHOR_GAME_TOKEN, validate_authorized_wallet, validate_contract, validate_asset, SecurityException

def run_transfer_single_wallet(
    wallet: Dict[str, Any], w3: Web3, game_token: Any, 
    burner1_address: str, pre_gas: int, gas_params: Dict[str, int],
    balance_to_transfer: float, balance_wei: Optional[int] = None,
    nonce: int = None
) -> Dict[str, Any]:
    """Executes hCASH transfer. Returns the tx hash for batched polling by PhaseEngine."""
    name = wallet["name"]
    address = wallet["address"]
    signer = wallet["signer"]
    
    if address.lower() == burner1_address.lower():
        return {"wallet": name, "transferred": 0.0, "success": True}

    try:
        # [SECURITY] Universal Integrity Guard Check
        validate_authorized_wallet(address, f"hCASH Transfer Sender ({name})")
        validate_authorized_wallet(burner1_address, f"hCASH Transfer Dest ({name})")
        validate_contract(game_token.address, f"hCASH Token Contract ({name})")
        
        # Cross-validation with hardcoded anchor as asset
        validate_asset(game_token.address, ANCHOR_GAME_TOKEN, f"hCASH Asset ({name})")

        if nonce is None:
            nonce = w3.eth.get_transaction_count(address, "pending")
            
        if balance_to_transfer <= 0.0001:
            return {"wallet": name, "transferred": 0.0, "success": True}
        
        if balance_wei is None:
            balance_wei = int(balance_to_transfer * 1e18)
        else:
            balance_to_transfer = balance_wei / 1e18
            
        logger.info(yellow_bold(f"[{name}] (nonce:{nonce}) Phase 2: Transferring {format_decimal(balance_to_transfer)} hCASH..."))
        _upd(name, transfer_amount=round(balance_to_transfer, 4), transfer_status="pending")
        
        tx = game_token.functions.transfer(burner1_address, balance_wei).build_transaction({
            "chainId": CHAIN_ID, "from": address, "nonce": nonce,
            "gas": pre_gas, **gas_params,
        })
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = f"0x{tx_hash.hex()}"
        logger.debug(green_bold(f"[{name}] (nonce:{nonce}) Phase 2: Transfer tx broadcast OK"))
        
        url_tx = f"{BLOCK_EXPLORER_URL}/tx/{tx_hex}"
        _upd(name, transfer_tx=url_tx)
        
        return {
            "wallet": name, 
            "tx_hash": f"0x{tx_hash.hex()}", 
            "success": True,
            "transferred": balance_to_transfer,
            "transferred_wei": balance_wei,
            "next_nonce": nonce + 1
        }
   
    except SecurityException as e:
        logger.critical(red_bold(f"[{name}] SECURITY VIOLATION: {e}"))
        err_msg = str(e)
        _upd(name, transfer_status="error")
        log_wallet_error(name, err_msg, address=address)
        return {"wallet": name, "transferred": 0.0, "success": False, "next_nonce": nonce, "error_msg": err_msg}

    except Exception as e:
        logger.error(red_bold(f"[{name}] (nonce:{nonce}) Phase 2 failure (Transfer): {e}"))
        err_msg = format_web3_error("Transfer failed", e)
        _upd(name, transfer_status="error")
        log_wallet_error(name, err_msg, address=address)
        return {"wallet": name, "transferred": 0.0, "success": False, "next_nonce": nonce, "error_msg": err_msg}


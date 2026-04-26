# src/actions/bricks/transfer_avax.py — Action Transfer AVAX

from typing import Dict, Any

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL
from src.services.logger_setup import logger
from src.utils.helpers import red_bold, yellow_bold, format_decimal, green_bold

from src.actions.ui_state import _upd, _set_avax_tx, log_wallet_error
from src.actions.utils import format_web3_error
from src.core.security import ANCHOR_AVAX_TOKEN, validate_authorized_wallet, validate_asset, SecurityException
from src.config import AVAX_TOKEN_ADDRESS

def run_transfer_avax(
    wallet: Dict[str, Any], w3: Web3, dest_address: str, dest_name: str,
    pre_gas: int, gas_params: Dict[str, int], amount_avax: float,
    nonce: int = None
) -> Dict[str, Any]:
    """Executes an AVAX transfer. Returns the tx hash for batched polling by PhaseEngine."""
    name = wallet["name"]
    address = wallet["address"]
    signer = wallet["signer"]
    amount_wei = int(amount_avax * 1e18)
    
    try:
        if amount_wei <= 0:
            return {"wallet": name, "transferred": 0.0, "success": True}
        
        # [SECURITY] Universal Integrity Guard Check
        validate_authorized_wallet(address, f"AVAX Transfer Sender ({name})")
        validate_authorized_wallet(dest_address, f"AVAX Transfer Dest ({name})")
        validate_asset(AVAX_TOKEN_ADDRESS, ANCHOR_AVAX_TOKEN, f"Native AVAX ({name})")

        if nonce is None:
            nonce = w3.eth.get_transaction_count(address, "pending")
        tx_id = f"tx_{nonce}"
        
        logger.info(yellow_bold(f"[GAS] {name} (nonce:{nonce}) ➔ {dest_name}: Transferring {format_decimal(amount_avax, 4)} AVAX..."))
        
        # Log pending SENDER
        _set_avax_tx(name, tx_id, {
            "type": "out", "amount": amount_avax, "target": dest_name, "status": "pending", "tx": None
        })
        # Log pending RECEIVER
        _set_avax_tx(dest_name, tx_id, {
            "type": "in", "amount": amount_avax, "target": name, "status": "pending", "tx": None
        })
        
        tx = {
            "chainId": CHAIN_ID, "from": address, "to": dest_address,
            "value": amount_wei, "nonce": nonce, "gas": pre_gas, **gas_params,
        }
        
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex = f"0x{tx_hash.hex()}"
        logger.debug(green_bold(f"[GAS] {name} (nonce:{nonce}) AVAX Transfer tx broadcast OK"))
        url_tx = f"{BLOCK_EXPLORER_URL}/tx/{tx_hex}"
        
        # Update Tx Hash early
        _set_avax_tx(name, tx_id, {"type": "out", "amount": amount_avax, "target": dest_name, "status": "pending", "tx": url_tx})
        _set_avax_tx(dest_name, tx_id, {"type": "in", "amount": amount_avax, "target": name, "status": "pending", "tx": url_tx})
        
        return {"wallet": name, "tx_hash": tx_hex, "tx_id": tx_id, "success": True, "next_nonce": nonce + 1}
        
    except SecurityException as e:
        logger.critical(red_bold(f"[GAS] {name} SECURITY VIOLATION: {e}"))
        err_msg = str(e)
        log_wallet_error(name, err_msg, address=address)
        if 'tx_id' in locals():
            _set_avax_tx(name, tx_id, {"type": "out", "amount": amount_avax, "target": dest_name, "status": "error", "tx": None})
            _set_avax_tx(dest_name, tx_id, {"type": "in", "amount": amount_avax, "target": name, "status": "error", "tx": None})
        return {"wallet": name, "transferred": 0.0, "success": False, "next_nonce": nonce, "error_msg": err_msg}

    except Exception as e:
        err_msg = format_web3_error("AVAX Transfer failed", e)
        logger.error(red_bold(f"[GAS] {name} (nonce:{nonce}) AVAX Transfer Failed: {e}"))
        log_wallet_error(name, err_msg, address=address)
        if 'tx_id' in locals():
            _set_avax_tx(name, tx_id, {"type": "out", "amount": amount_avax, "target": dest_name, "status": "error", "tx": None})
            _set_avax_tx(dest_name, tx_id, {"type": "in", "amount": amount_avax, "target": name, "status": "error", "tx": None})
        return {"wallet": name, "transferred": 0.0, "success": False, "next_nonce": nonce, "error_msg": err_msg}


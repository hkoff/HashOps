# src/actions/bricks/withdraw_miner.py — Action Withdraw Miner from the game

from typing import List, Dict, Any

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL
from src.services.logger_setup import logger
from src.utils.helpers import red_bold, yellow_bold, green_bold

from src.actions.ui_state import _upd, _log_miner_action
from src.actions.utils import format_web3_error

def run_withdraw_batch_for_wallet(
    wallet: Dict[str, Any], withdraws: List[Dict[str, Any]], w3: Web3, 
    game_main: Any, pre_gas: int, gas_params: Dict[str, int],
    base_nonce: int = None
) -> Dict[str, int]:
    """Executes all withdraws for a wallet, returns tx_hashes."""
    name = wallet["name"]
    address = wallet["address"]
    signer = wallet["signer"]
    
    if not withdraws:
        return {}
    
    tx_hashes = {}
    current_nonce = base_nonce if base_nonce is not None else w3.eth.get_transaction_count(address, "pending")
    
    for i, m_info in enumerate(withdraws):
        try:
            m_id = m_info["id"]
            nft_id = m_info.get("nft_token_id")
            m_name = m_info.get("name", "Miner")

            logger.info(yellow_bold(f"[{name}] (nonce:{current_nonce}) Sending Withdraw tx for {m_name} #{m_id} (NFT #{nft_id})..."))
            
            tx = game_main.functions.withdrawMiner(m_id).build_transaction({
                "chainId": CHAIN_ID, "from": address, "nonce": current_nonce,
                "gas": pre_gas, **gas_params,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.debug(green_bold(f"[{name}] (nonce:{current_nonce}) Withdraw tx broadcast OK"))
            
            # Nonce incremented ONLY if broadcast succeeded
            current_nonce += 1
            
            url_tx = f"{BLOCK_EXPLORER_URL}/tx/0x{tx_hash.hex()}"
            _log_miner_action(name, m_id, "Withdraw", url_tx, status="pending", miner_name=m_name, nft_id=nft_id)
            
            tx_hashes[f"0x{tx_hash.hex()}"] = m_id
        except Exception as e:
            err_msg = format_web3_error("Withdraw failed", e)
            _upd(name, withdraw_status="error", status="error", error=err_msg)
            logger.error(red_bold(f"[{name}] (nonce:{current_nonce}) Withdraw Miner {m_id} submission failed: {e}"))
            return tx_hashes, current_nonce, err_msg

    return tx_hashes, current_nonce, None

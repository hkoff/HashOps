# src/actions/bricks/transfer_miner.py — Action Transfer NFT/Miner

from typing import List, Dict, Any, Union

from web3 import Web3

from src.config import CHAIN_ID, BLOCK_EXPLORER_URL, NULL_ADDRESS, DEFAULT_GAS_TRANSFER
from src.services.logger_setup import logger
from src.utils.helpers import red_bold, yellow_bold, green_bold
from src.core.blockchain import get_nft_contract, get_miner_contract_address

from src.actions.ui_state import _upd, _log_miner_action, get_wallet_name
from src.actions.utils import format_web3_error
from src.core.security import validate_authorized_wallet, validate_contract, SecurityException

def run_transfer_batch_for_wallet(
    wallet: Dict[str, Any], transfers: List[Dict[str, Any]], w3: Web3, 
    pre_gas: Union[int, Dict[str, int]], gas_params: Dict[str, int],
    base_nonce: int = None
) -> Dict[str, int]:
    """Executes all transfers for a wallet, returns tx_hashes."""
    name = wallet["name"]
    address = wallet["address"]
    signer = wallet["signer"]
    
    if not transfers:
        return {}
    
    tx_hashes = {}
    current_nonce = base_nonce if base_nonce is not None else w3.eth.get_transaction_count(address, "pending")
    
    for i, t_info in enumerate(transfers):
        try:
            m_id = t_info["id"]  # Miner ID (for log/UI)
            nft_id = t_info.get("nft_token_id")  # NFT Token ID (for transaction)
            t_type_idx = t_info.get("type_idx")
            t_dest_address = t_info["dest"]
            t_nft = t_info.get("nft")
            t_name = t_info.get("name", "Miner")
            
            if nft_id is None:
                logger.error(red_bold(f"[{name}] CRITICAL Error: Missing NFT ID for {t_name} (Miner #{m_id}). Aborting."))
                _upd(name, transfer_status="error", status="error")
                break

            # --- NFT contract resolution (Source of truth: hCASH contract) ---
            nft_addr = NULL_ADDRESS
            if t_nft and t_nft.lower() != "undefined" and t_nft != NULL_ADDRESS:
                nft_addr = Web3.to_checksum_address(t_nft)
            elif t_type_idx is not None:
                # Local resolution via API registry (Saves an RPC call)
                nft_addr = get_miner_contract_address(t_type_idx)
                logger.debug(f"[{name}] NFT resolved via local registry for type {t_type_idx} -> {nft_addr}")

            if nft_addr == NULL_ADDRESS:
                logger.error(red_bold(f"[{name}] Unable to determine NFT contract for {t_name} #{nft_id}. Skipping."))
                continue

            # [SECURITY] Universal Integrity Guard Check
            validate_authorized_wallet(address, f"NFT Transfer Sender ({name})")
            validate_authorized_wallet(t_dest_address, f"NFT Transfer Dest ({name})")
            validate_contract(nft_addr, f"NFT Contract ({name})")

            nft_contract = get_nft_contract(w3, nft_addr)
            dest_chk = Web3.to_checksum_address(t_dest_address)
            
            # --- Gas Resolution ---
            if isinstance(pre_gas, dict):
                current_gas = pre_gas.get(nft_addr, DEFAULT_GAS_TRANSFER)
            else:
                current_gas = pre_gas
            
            # Name resolution for UI/Logs
            dest_name = get_wallet_name(dest_chk)
            
            # Using safeTransferFrom as requested. 
            logger.info(yellow_bold(f"[{name}] (nonce:{current_nonce}) Sending Transfer tx for {t_name} (NFT #{nft_id}) to {dest_name} with {current_gas} gas..."))
            tx = nft_contract.functions.safeTransferFrom(address, dest_chk, nft_id).build_transaction({
                "chainId": CHAIN_ID, "from": address, "nonce": current_nonce,
                "gas": current_gas, **gas_params,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.debug(green_bold(f"[{name}] (nonce:{current_nonce}) Transfer tx broadcast OK"))
            
            # Nonce incremented ONLY if broadcast succeeded
            current_nonce += 1
            
            url_tx = f"{BLOCK_EXPLORER_URL}/tx/0x{tx_hash.hex()}"
            
            # Log for sender
            _log_miner_action(name, m_id, "Transfer", url_tx, status="pending", miner_name=t_name, dest=dest_name, nft_id=nft_id)
            
            # Log for recipient (no hash shown for receiver)
            _log_miner_action(dest_name, m_id, "Received", None, status="pending", miner_name=t_name, nft_id=nft_id)
            
            tx_hashes[f"0x{tx_hash.hex()}"] = (m_id, dest_name)

        except SecurityException as e:
            logger.critical(red_bold(f"[{name}] SECURITY VIOLATION: {e}"))
            _upd(name, transfer_status="error", status="error", error=str(e))
            return tx_hashes, current_nonce, str(e)
            
        except Exception as e:
            err_msg = format_web3_error("Transfer failed", e)
            _upd(name, transfer_status="error", status="error", error=err_msg)
            logger.error(red_bold(f"[{name}] (nonce:{current_nonce}) Transfer Miner {m_id} submission failed: {e}"))
            return tx_hashes, current_nonce, err_msg

    return tx_hashes, current_nonce, None


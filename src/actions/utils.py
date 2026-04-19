# src/actions/utils.py — Shared utilities for actions

import ast
import re
import time
from typing import Any, Dict

from web3 import Web3

from src.config import (
    GAS_BOOST_MULTIPLIER, GAS_SAFETY_MAX_GWEI, POLL_MAX_INTERVAL,
    POLL_TOAST_AFTER, POLL_WARN_THRESHOLD, RPC_BATCH_SIZE,
    TX_POLL_INTERVAL, TX_RECEIPT_TIMEOUT
)
from src.services.logger_setup import logger
from src.utils.helpers import magenta_bold, red_bold, yellow_bold
from src.web_ui.sse import _broadcast

def format_web3_error(prefix: str, e: Exception) -> str:
    """Formats a Web3 exception into a clean HTML breakdown for the UI."""
    msg = str(e)
    code_str = ""
    clean_msg = ""
    
    # 1. Map of known Web3 errors (intercepting them immediately)
    KNOWN_ERRORS = {
        "insufficient funds": "Insufficient funds for gas",
        "nonce too low": "Nonce too low (transaction already processed)",
        "replacement transaction underpriced": "Replacement transaction underpriced",
        "already known": "Transaction already known (duplicate)",
    }
    
    msg_low = msg.lower()
    
    # Attempt to extract JSON-RPC dict first as it's the most reliable source
    err_dict = None
    try:
        if len(e.args) > 0 and isinstance(e.args[0], dict):
            err_dict = e.args[0]
        else:
            match = re.search(r'\{.*\}', msg)
            if match:
                err_dict = ast.literal_eval(match.group(0))
    except Exception:
        pass

    if err_dict and 'message' in err_dict:
        code = err_dict.get('code')
        code_str = f" - Code {code}" if code is not None else ""
        raw_msg = err_dict['message']
        
        # Intelligent Cleaning
        if 'insufficient funds' in raw_msg.lower():
            # If it's a typical Geth/EVM error with details: 
            # "insufficient funds for gas * price + value: balance 0, tx cost 123, overshot 123"
            if ':' in raw_msg:
                details = raw_msg.split(':', 1)[1].strip()
                clean_msg = f"Insufficient funds ({details.capitalize()})"
            else:
                clean_msg = "Insufficient funds for gas"
        else:
            # Generic clean: split at first colon for verbose Geth/EVM reverts
            clean_msg = raw_msg.split(':')[0] if ':' in raw_msg else raw_msg
            clean_msg = clean_msg.capitalize()
    else:
        # Fallback to string matching if no dict found
        for key, readable in KNOWN_ERRORS.items():
            if key in msg_low:
                clean_msg = readable
                code_str = " - Code -32000" if "-32000" in msg else ""
                if key == "insufficient funds" and ":" in msg:
                    details = msg.split(':', 1)[1].split('}', 1)[0].strip() # Strip json trailing brace if any
                    if 'balance' in details.lower():
                        clean_msg = f"{readable} ({details.capitalize()})"
                break

    if clean_msg:
        # Avoid prefix redundancy if prefix is already in clean_msg or vice versa
        final_prefix = f"{prefix}{code_str}"
        return f"{final_prefix}<br/>{clean_msg}"
        
    # 3. Ultimate structural fallback
    return f"{prefix}: {msg[:100]}..."

def _hex_to_int(val) -> int:
    """Safely converts a hex string or int to int. Handles raw JSON-RPC values."""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val, 16) if val.startswith("0x") else int(val)
        except (ValueError, TypeError):
            return 0
    return 0

def get_revert_reasons_batch(w3: Web3, failed_receipts: Dict[str, dict]) -> Dict[str, str]:
    """
    Batches JSON-RPC requests to extract precise revert reasons for multiple failed transactions.
    Zero-network check for Out-of-Gas (local, no RPC), and batched eth_call for VM reverts.
    
    IMPORTANT: Receipts from batch RPC contain hex strings ("0x..."), not Python ints.
    All numeric fields must be parsed via _hex_to_int before comparison.
    """
    if not failed_receipts:
        return {}
    
    reasons = {}
    tx_hex_list = list(failed_receipts.keys())
    logger.info(yellow_bold(f"[RevertDiag] Diagnosing {len(tx_hex_list)} failed tx(s)..."))
    
    # 1. Batch fetch transactions to get gas limit and input data
    payload = []
    for i, tx_hex in enumerate(tx_hex_list):
        payload.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_getTransactionByHash",
            "params": [tx_hex if tx_hex.startswith("0x") else f"0x{tx_hex}"]
        })
        
    try:
        responses = w3.provider.make_request(None, payload)
    except Exception as e:
        logger.error(red_bold(f"[RevertDiag] RPC batch eth_getTransaction failed: {e}"))
        return {h: "Transaction Reverted" for h in tx_hex_list}
    
    # Handle both list (batch) and dict (single) RPC responses
    tx_data_map = {}
    if isinstance(responses, list):
        for res in responses:
            req_id = res.get("id")
            if req_id is not None and req_id < len(tx_hex_list):
                tx_hex = tx_hex_list[req_id]
                tx_obj = res.get("result")
                if tx_obj:
                    tx_data_map[tx_hex] = tx_obj
    elif isinstance(responses, dict) and "result" in responses:
        # Single-element batch: some RPCs return a dict instead of a list
        if len(tx_hex_list) == 1:
            tx_data_map[tx_hex_list[0]] = responses["result"]
                    
    # 2. Check Out of Gas individually and prepare batched eth_call
    call_payload = []
    call_mapping = {} # req_id -> tx_hex
    
    for tx_hex, receipt in failed_receipts.items():
        tx = tx_data_map.get(tx_hex)
        if not tx:
            logger.debug(yellow_bold(f"[RevertDiag] {tx_hex[:18]}... → No tx data found, generic revert"))
            reasons[tx_hex] = "Transaction Reverted"
            continue
        
        # Parse hex values from raw RPC response
        gas_used = _hex_to_int(receipt.get("gasUsed"))
        gas_limit = _hex_to_int(tx.get("gas"))
        
        logger.debug(yellow_bold(f"[RevertDiag] {tx_hex[:18]}... → gasUsed={gas_used} / gasLimit={gas_limit}"))
        
        if gas_limit > 0 and gas_used >= gas_limit * 0.99:
            reasons[tx_hex] = "Out of Gas (Gas limit reached)"
            logger.debug(yellow_bold(f"[RevertDiag] {tx_hex[:18]}... → OUT OF GAS detected ({gas_used}/{gas_limit})"))
            continue
            
        block_num = _hex_to_int(receipt.get("blockNumber"))
        if block_num > 0:
            block_hex = hex(block_num - 1)
            tx_params = {
                "to": tx.get("to"),
                "from": tx.get("from"),
                "value": tx.get("value", "0x0"),
                "data": tx.get("input", "0x")
            }
            req_id = len(call_payload)
            call_payload.append({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "eth_call",
                "params": [tx_params, block_hex]
            })
            call_mapping[req_id] = tx_hex
        else:
            reasons[tx_hex] = "Transaction Reverted"
             
    # 3. Batch call eth_call for remaining (non Out-of-Gas) reverts
    if call_payload:
        try:
            call_responses = w3.provider.make_request(None, call_payload)
            resp_list = call_responses if isinstance(call_responses, list) else [call_responses]
            for res in resp_list:
                req_id = res.get("id")
                if req_id is not None and req_id in call_mapping:
                    tx_hex = call_mapping[req_id]
                    if "error" in res:
                        err_msg = res["error"].get("message", "Reverted by EVM")
                        try:
                            clean_msg = format_web3_error("Reverted", Exception(res["error"]))
                            reasons[tx_hex] = clean_msg
                        except Exception:
                            reasons[tx_hex] = f"Reverted: {err_msg}"
                    else:
                        reasons[tx_hex] = "Reverted by EVM (No reason provided)"
        except Exception as e:
            logger.error(red_bold(f"[RevertDiag] RPC batch eth_call failed: {e}"))
            for req_id, tx_hex in call_mapping.items():
                if tx_hex not in reasons:
                    reasons[tx_hex] = "Transaction Reverted"

    return reasons

def get_batch_receipts(w3: Web3, tx_hashes: list[str]) -> dict[str, Any]:
    """
    Retrieves multiple receipts. 
    Robust version: uses a simple call for N=1, and attempts a batch for N > 1.
    If the RPC batch fails, falls back to sequential mode.
    """
    if not tx_hashes:
        return {}
    
    results = {}
    
    # Case 1: A single hash -> Standard Web3 call (safer)
    if len(tx_hashes) == 1:
        tx_hex = tx_hashes[0]
        try:
            results[tx_hex] = w3.eth.get_transaction_receipt(tx_hex)
        except Exception:
            results[tx_hex] = None
        return results

    # Case 2: Multiple hashes -> Attempt JSON-RPC Batch (chunked for scaling)
    for k in range(0, len(tx_hashes), RPC_BATCH_SIZE):
        chunk = tx_hashes[k : k + RPC_BATCH_SIZE]
        payload = []
        for i, tx_hex in enumerate(chunk):
            rpc_tx_hex = tx_hex if tx_hex.startswith("0x") else f"0x{tx_hex}"
            payload.append({
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getTransactionReceipt",
                "params": [rpc_tx_hex]
            })
        
        batch_success = False
        try:
            responses = w3.provider.make_request(None, payload)
            if isinstance(responses, list):
                for res in responses:
                    req_id = res.get("id")
                    if req_id is not None and req_id < len(chunk):
                        tx_hex = chunk[req_id]
                        results[tx_hex] = res.get("result")
                batch_success = True
        except Exception as e:
            logger.debug(f"[get_batch_receipts] RPC batch attempt failed (falling back to sequential): {e}")

        if not batch_success:
            # Fallback for this chunk
            for tx_hex in chunk:
                try:
                    results[tx_hex] = w3.eth.get_transaction_receipt(tx_hex)
                except Exception:
                    results[tx_hex] = None

    return results

def wait_transactions_batch(
    w3: Web3, 
    all_tx_map: Dict[str, Dict[str, Any]], 
    action_type: str, 
    receipt_callback=None,  # callable(w_name, tx_hex, val, receipt, is_success) called upon confirmation
    timeout_override: int = TX_RECEIPT_TIMEOUT
) -> tuple[Dict[str, Any], list[str]]:
    """
    Optimized batched wait with exponential backoff polling.
    
    Starts polling at TX_POLL_INTERVAL (2s), doubles each iteration up to POLL_MAX_INTERVAL (30s). 
    Sends SSE toast alerts to the UI at graduated thresholds (info → warning → error) to keep the user informed.
    
    Returns { tx_hash: receipt } for all processed transactions.
    """
    processed_receipts = {}
    if not all_tx_map:
        return {}, []

    pending_hashes = []
    tx_meta = {} # tx_hex -> {w_name, data}

    for w_name, hashes in all_tx_map.items():
        for tx_hex, val in hashes.items():
            pending_hashes.append(tx_hex)
            tx_meta[tx_hex] = {"w_name": w_name, "val": val}

    total_count = len(pending_hashes)
    logger.debug(magenta_bold(f"⏳ Waiting for {total_count} receipts ({action_type})..."))

    start_time = time.time()
    current_interval = TX_POLL_INTERVAL
    poll_count = 0
    warned = False  # Only send warning toast once

    while pending_hashes and (time.time() - start_time) < timeout_override:
        poll_count += 1
        elapsed = time.time() - start_time
        
        logger.debug(magenta_bold(
            f"⏳ Poll #{poll_count} — {len(pending_hashes)} pending — "
            f"next in {current_interval:.0f}s (elapsed {elapsed:.0f}s)"
        ))
        
        # Toast alerts (only after initial grace period to avoid noise on fast txs)
        if poll_count >= POLL_TOAST_AFTER:
            if elapsed >= POLL_WARN_THRESHOLD and not warned:
                warned = True
                _broadcast({
                    "type": "polling_alert", "level": "warning",
                    "message": f"⏳ {len(pending_hashes)} tx still pending after {elapsed:.0f}s — transactions may be slow",
                })
            elif not warned:
                _broadcast({
                    "type": "polling_alert", "level": "info",
                    "message": f"⏳ Polling... {len(pending_hashes)} pending ({elapsed:.0f}s elapsed, retry #{poll_count})",
                })
        
        time.sleep(current_interval)
        current_interval = min(current_interval * 2, POLL_MAX_INTERVAL)
        
        found = get_batch_receipts(w3, pending_hashes)
        newly_processed = []
        
        failed_receipts = {}
        processed_temporarily = []

        for tx_hex, receipt in found.items():
            if receipt is None:
                continue
            processed_temporarily.append((tx_hex, receipt))
            status = receipt.get("status")
            if status not in (1, "0x1"):
                failed_receipts[tx_hex] = receipt

        revert_reasons = {}
        if failed_receipts:
            revert_reasons = get_revert_reasons_batch(w3, failed_receipts)

        for tx_hex, receipt in processed_temporarily:
            processed_receipts[tx_hex] = receipt
            meta = tx_meta[tx_hex]
            w_name = meta["w_name"]
            val = meta["val"]
            
            status = receipt.get("status")
            is_success = (status == 1 or status == "0x1")
            error_msg = revert_reasons.get(tx_hex) if not is_success else None

            if receipt_callback:
                try:
                    receipt_callback(w_name, tx_hex, val, receipt, is_success, error_msg=error_msg)
                except Exception as cb_err:
                    logger.error(red_bold(f"[receipt_callback] Error ignored: {cb_err}"))

            newly_processed.append(tx_hex)

        pending_hashes = [h for h in pending_hashes if h not in newly_processed]

    # Timeout handling: alert only
    if pending_hashes:
        _broadcast({
            "type": "polling_alert", "level": "warning",
            "message": f"{len(pending_hashes)} transaction(s) timed out after {timeout_override}s — analyzing rescue options...",
        })
            
    for h in pending_hashes:
        meta = tx_meta[h]
        w_name = meta["w_name"]
        logger.warning(red_bold(f"[{w_name}] Timeout receipt tx {h}"))

    return processed_receipts, pending_hashes

def get_batch_nonces(w3: Web3, addresses: list[str]) -> dict[str, int]:
    """
    Retrieves the nonces (transaction count) of multiple addresses in a single JSON-RPC call.
    Returns a dict { address_lower: nonce_int }.
    """
    if not addresses:
        return {}
    
    results = {}
    
    # Deduplicate and normalize
    unique_addresses = list(set(addr.lower() for addr in addresses))
    
    if len(unique_addresses) == 1:
        addr = unique_addresses[0]
        try:
            return {addr: w3.eth.get_transaction_count(Web3.to_checksum_address(addr), "pending")}
        except Exception:
            return {addr: 0}

    # Batch RPC with scaling (chunks of 25)
    for k in range(0, len(unique_addresses), RPC_BATCH_SIZE):
        chunk = unique_addresses[k : k + RPC_BATCH_SIZE]
        payload = []
        for i, addr in enumerate(chunk):
            payload.append({
                "jsonrpc": "2.0",
                "id": i,
                "method": "eth_getTransactionCount",
                "params": [Web3.to_checksum_address(addr), "pending"]
            })
        
        batch_success = False
        try:
            responses = w3.provider.make_request(None, payload)
            if isinstance(responses, list):
                for res in responses:
                    req_id = res.get("id")
                    if req_id is not None and req_id < len(chunk):
                        addr = chunk[req_id]
                        val = res.get("result")
                        if isinstance(val, str) and val.startswith("0x"):
                            results[addr] = int(val, 16)
                        else:
                            results[addr] = int(val if val is not None else 0)
                batch_success = True
        except Exception as e:
            logger.error(red_bold(f"[get_batch_nonces] RPC batch attempt failed (falling back to sequential): {e}"))

        if not batch_success:
            # Error in batch or unexpected response format -> sequential fallback for this chunk
            for addr in chunk:
                try:
                    results[addr] = w3.eth.get_transaction_count(Web3.to_checksum_address(addr), "pending")
                except Exception:
                    results[addr] = 0
                
    return results

def diagnose_stuck_transactions(w3: Web3, tx_to_address_map: dict[str, str]) -> dict[str, dict]:
    """
    Diagnoses stuck transactions by querying their current mempool state and the network's latest nonce.
    Uses an explicit hash-to-address map to handle cases where the transaction is, not in, or no longer in the mempool.
    Returns: { tx_hash: {"address": str, "tx_nonce": int, "latest_nonce": int, "status": str, "tx_data": dict} }
    """
    pending_hashes = list(tx_to_address_map.keys())
    if not pending_hashes:
        return {}
    
    # --- 1. Call eth_getTransaction for all stuck hashes ---
    payload = []
    for i, h in enumerate(pending_hashes):
        payload.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_getTransaction",
            "params": [h]
        })
    
    responses = []
    try:
        responses = w3.provider.make_request(None, payload)
    except Exception as e:
        logger.error(red_bold(f"[Diagnostics] RPC Error fetching txs: {e}"))

    txs_data = {}
    addresses_to_check = set(tx_to_address_map.values())
    
    if isinstance(responses, list):
        for res in responses:
            req_id = res.get("id")
            if req_id is not None and req_id < len(pending_hashes):
                h = pending_hashes[req_id]
                tx_obj = res.get("result")
                if tx_obj:
                    # Normalize hex integers
                    t_nonce = int(tx_obj.get("nonce", "0"), 16) if isinstance(tx_obj.get("nonce"), str) else int(tx_obj.get("nonce", 0))
                    txs_data[h] = {
                        "tx_nonce": t_nonce,
                        "tx_data": tx_obj
                    }

    # --- 2. Call eth_getTransactionCount ("latest" AND "pending") for all involved addresses ---
    payload_nonce = []
    addrs_list = list(addresses_to_check)
    N_addrs = len(addrs_list)
    for i, addr in enumerate(addrs_list):
        # Mined state
        payload_nonce.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_getTransactionCount",
            "params": [addr, "latest"]
        })
        # Mempool state
        payload_nonce.append({
            "jsonrpc": "2.0",
            "id": i + N_addrs,
            "method": "eth_getTransactionCount",
            "params": [addr, "pending"]
        })

    latest_nonces = {}
    pending_nonces = {}
    try:
        resp_nonce = w3.provider.make_request(None, payload_nonce)
        if isinstance(resp_nonce, list):
            for res in resp_nonce:
                req_id = res.get("id")
                if req_id is not None:
                    # Map back to addr
                    idx = req_id % N_addrs
                    if idx < len(addrs_list):
                        addr = addrs_list[idx]
                        val = res.get("result")
                        nonce_val = int(val, 16) if isinstance(val, str) and val.startswith("0x") else int(val or 0)
                        
                        if req_id < N_addrs:
                            latest_nonces[addr] = nonce_val
                        else:
                            pending_nonces[addr] = nonce_val
    except Exception as e:
        logger.error(red_bold(f"[Diagnostics] RPC Error fetching nonces: {e}"))

    # --- 3. Compute Diagnosis ---
    diagnostics = {}
    for h, addr in tx_to_address_map.items():
        latest_nonce = latest_nonces.get(addr, 0)
        pending_nonce = pending_nonces.get(addr, 0)
        
        # If tx was in mempool
        if h in txs_data:
            tx_nonce = txs_data[h]["tx_nonce"]
            status = "Unknown"
            if tx_nonce == latest_nonce:
                status = "Underpriced"
            elif tx_nonce > latest_nonce:
                status = "Nonce Gap"
            else:
                status = "Dropped or Already Mined"
                
            diagnostics[h] = {
                "address": addr,
                "tx_nonce": tx_nonce,
                "latest_nonce": latest_nonce,
                "pending_nonce": pending_nonce,
                "status": status,
                "tx_data": txs_data[h]["tx_data"]
            }
        else:
            # Hash not found in mempool -> use wallet state to guess
            diagnostics[h] = {
                "address": addr,
                "latest_nonce": latest_nonce,
                "pending_nonce": pending_nonce,
                "status": "Missing from Mempool / Potential Collision",
                "tx_data": None
            }
        
    return diagnostics

def rescue_stuck_transaction(w3: Web3, wallet: dict, h: str, diag: dict) -> str:
    """
    Attempts to rescue a stuck transaction either via Nonce Self-Healing or RBF Speed-Up.
    Returns the new transaction hash if successful, None otherwise.
    """
    w_name = wallet["name"]
    status = diag["status"]
    tx_data = diag["tx_data"]
    
    # 1. Clean the transaction object for re-signing
    valid_keys = {"to", "value", "data", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "chainId", "type", "nonce"}
    clean_tx = {}
    for key in valid_keys:
        if key in tx_data:
            val = tx_data[key]
            # Convert hex strings to int/bytes
            if isinstance(val, str) and val.startswith("0x"):
                if key in ["data", "to"]:
                    clean_tx[key] = val
                else:
                    clean_tx[key] = int(val, 16)
            else:
                clean_tx[key] = int(val) if val is not None else 0
                
    if status == "Nonce Gap":
        # System Self-Healing
        latest_nonce = diag["latest_nonce"]
        logger.warning(yellow_bold(f"[{w_name}] 🩹 Auto-Healing Nonce Gap: rewriting nonce {clean_tx['nonce']} -> {latest_nonce}"))
        clean_tx["nonce"] = latest_nonce
        
    elif status == "Underpriced":
        # RBF Speed-Up
        old_max = clean_tx.get("maxFeePerGas", 0)
        old_priority = clean_tx.get("maxPriorityFeePerGas", 0)
        
        new_max = int(old_max * GAS_BOOST_MULTIPLIER)
        new_priority = int(old_priority * GAS_BOOST_MULTIPLIER)
        
        # Check against Safety Limits (convert to Gwei for check)
        new_max_gwei = new_max / 10**9
        if new_max_gwei > GAS_SAFETY_MAX_GWEI:
            logger.error(red_bold(f"[{w_name}] 🚫 RBF Safety Abort: New gas ({new_max_gwei:.2f} Gwei) exceeds limit ({GAS_SAFETY_MAX_GWEI} Gwei)."))
            return None
            
        logger.warning(yellow_bold(f"[{w_name}] 🚀 RBF Speed-Up: gas {old_max/10**9:.2f} -> {new_max_gwei:.2f} Gwei (Nonce: {clean_tx['nonce']})"))
        clean_tx["maxFeePerGas"] = new_max
        clean_tx["maxPriorityFeePerGas"] = new_priority
        
    else:
        logger.debug(f"[{w_name}] No rescue action viable for status: {status}")
        return None

    # 2. Sign and Broadcast
    try:
        signer = wallet.get("signer")
        if not signer:
            logger.error(red_bold(f"[{w_name}] No signer found to rescue transaction."))
            return None
            
        signed = signer.sign_transaction(clean_tx)
        new_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        return new_hash.hex()
    except Exception as e:
        logger.error(red_bold(f"[{w_name}] Error during rescue broadcast: {e}"))
        return None

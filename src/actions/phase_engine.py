# src/actions/phase_engine.py

from typing import List, Dict, Any, Callable, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3

from src.config import RESCUE_TIMEOUT
from src.services.logger_setup import logger
from src.utils.helpers import magenta_bold, red_bold, yellow_bold, cyan_bold
from src.actions.utils import wait_transactions_batch, diagnose_stuck_transactions, rescue_stuck_transaction
from src.web_ui.sse import _broadcast

class SubmissionResult:
    """Encapsulates the result of a transaction submission."""
    def __init__(self, w_name: str, tx_dict: Optional[Dict[str, Any]] = None, status: str = "success", error_msg: Optional[str] = None):
        self.w_name = w_name
        self.tx_dict = tx_dict or {}
        self.status = status # "success", "skipped", "error"
        self.error_msg = error_msg

    @property
    def is_ok(self) -> bool:
        return self.status in ["success", "skipped"]

    @classmethod
    def success(cls, w_name: str, tx_dict: Dict[str, Any]):
        return cls(w_name, tx_dict, "success")

    @classmethod
    def skip(cls, w_name: str):
        return cls(w_name, {}, "skipped")

    @classmethod
    def error(cls, w_name: str, error_msg: Optional[str] = None):
        return cls(w_name, None, "error", error_msg=error_msg)

class Phase:
    def __init__(self,
                 name: str,
                 action_type: str,
                 items: List[Any],
                 prepare_fn: Optional[Callable[[List[Any]], Any]] = None,
                 submit_fn: Optional[Callable[[Any, Any], Tuple[str, Dict[str, Any]]]] = None,
                 on_submit_error: Optional[Callable[[Any, Optional[str]], None]] = None,
                 on_receipt_success: Optional[Callable[[str, str, Any, Any], None]] = None,
                 on_receipt_error: Optional[Callable[[str, str, Any, Any], None]] = None):
        """
        Declarative model for a Transaction Phase execution pipeline.
        
        Chronological Flow:
        1. items: List of elements to process (usually tuples with the wallet dict at index 0).
        2. prepare_fn(items): Called once per phase to prepare batch data (e.g. estimate gas).
        3. submit_fn(item, setup_data): Executed concurrently by ThreadPool to build, sign and broadcast TXs.
        4. on_submit_error(item, error_msg): Called when a wallet fails during submission (before reaching the mempool).
        5. Polling & Rescue: The engine batches receipt polling. Timed-out TXs undergo RPC diagnostics, Auto-Healing (Nonce), or RBF (Gas Speed-Up).
        6. on_receipt_success(w_name, tx_hex, val, receipt): Triggered identically for original or Rescued TXs that succeed.
        7. on_receipt_error(w_name, tx_hex, val, receipt): Triggered for network Reverts OR if the Rescue Loop ultimately gives up.
        """
        self.name = name
        self.action_type = action_type
        self.items = items
        self.prepare_fn = prepare_fn
        self.submit_fn = submit_fn
        self.on_submit_error = on_submit_error
        self.on_receipt_success = on_receipt_success
        self.on_receipt_error = on_receipt_error

class PhaseEngine:
    def __init__(self, w3: Web3, last_phase_map: Optional[Dict[str, str]] = None):
        """
        Unified engine for sequential transaction management in batch phases.
        - last_phase_map: Defines the final action of a wallet to trigger final UI successes in utils.py.
                          e.g., {"Wallet1": "Place", "Wallet2": "Transfer"}
        """
        self.w3 = w3
        self.last_phase_map = last_phase_map or {}
        self.global_failed_items: Set[Any] = set()

    def run_phase(self, phase: Phase) -> None:
        """
        Executes the complete sequence of a Phase:
        1. Preparation (Gas estimation)
        2. Asynchronous Batch Submission (ThreadPool)
        3. Handling submission errors
        4. Blocking wait / batched receipt polling
        """
        if not phase.items:
            logger.info(magenta_bold(f"No items to process for this phase."))
            return

        # 1. Estimation / Preparation
        setup_data = None
        if phase.prepare_fn:
            try:
                setup_data = phase.prepare_fn(phase.items)
            except Exception as e:
                logger.warning(red_bold(f"[PhaseEngine] Preparation error '{phase.name}': {e}"))
                # We don't block everything; we'll pass it to submit_fn which will handle its own fallback estimation.

        # 2. Asynchronous submission
        all_txs: Dict[str, Dict[str, Any]] = {}
        items_with_tx = set()
        submission_errors: Dict[int, Optional[str]] = {} # id(item) -> error_msg

        with ThreadPoolExecutor(max_workers=5) as executor:
            if phase.submit_fn:
                futures = {executor.submit(phase.submit_fn, item, setup_data): item for item in phase.items}
                for f in as_completed(futures):
                    item = futures[f]
                    try:
                        res = f.result()
                        # If the result is a SubmissionResult, use its explicit logic
                        if isinstance(res, SubmissionResult):
                            if res.is_ok:
                                if res.tx_dict:
                                    if res.w_name not in all_txs:
                                        all_txs[res.w_name] = {}
                                    all_txs[res.w_name].update(res.tx_dict)
                                items_with_tx.add(id(item))
                            else:
                                submission_errors[id(item)] = res.error_msg
                        # Fallback compatibility (tuple or None)
                        elif res:
                            w_name, tx_dict = res
                            if tx_dict is not None:
                                if tx_dict:
                                    if w_name not in all_txs:
                                        all_txs[w_name] = {}
                                    all_txs[w_name].update(tx_dict)
                                items_with_tx.add(id(item))
                    except Exception as e:
                        logger.error(red_bold(f"[PhaseEngine] Unexpected submission error for an item: {e}"))
                    
        # 3. Handling items that submitted nothing (omissions, logged errors without UI validation)
        if phase.on_submit_error:
            for item in phase.items:
                if id(item) not in items_with_tx:
                    error_msg = submission_errors.get(id(item))
                    phase.on_submit_error(item, error_msg=error_msg)

        if not all_txs:
            logger.info(yellow_bold(f"No transactions succeeded during submission phase for {phase.name}."))
            return

        # 4. Waiting for receipts (RPC Polling)
        def internal_receipt_callback(w_name, tx_hex, val, receipt, is_success, error_msg=None):
            if is_success:
                if phase.on_receipt_success:
                    phase.on_receipt_success(w_name, tx_hex, val, receipt)
            else:
                if phase.on_receipt_error:
                    phase.on_receipt_error(w_name, tx_hex, val, receipt, error_msg=error_msg)

        # Build reverse mappings for rescue diagnostics
        tx_to_wname = {}
        tx_to_val = {}
        for w_name, hashes in all_txs.items():
            for tx_hex, val in hashes.items():
                tx_to_wname[tx_hex] = w_name
                tx_to_val[tx_hex] = val
                
        wallet_map = {}
        for item in phase.items:
            if isinstance(item, dict):
                wallet_map[item["name"]] = item
            elif isinstance(item, (tuple, list)) and isinstance(item[0], dict):
                wallet_map[item[0]["name"]] = item[0]

        _, timed_out_hashes = wait_transactions_batch(
            self.w3, 
            all_txs, 
            phase.action_type, 
            receipt_callback=internal_receipt_callback
        )
        
        # 5. The Rescue Loop
        if timed_out_hashes:
            logger.info(cyan_bold(f"🚨 {len(timed_out_hashes)} transaction(s) stuck. Starting Rescue Diagnostics..."))
            _broadcast({"type": "polling_alert", "level": "warning", "message": "Diagnosing stuck transactions..."})
            
            tx_to_addr = {h: wallet_map[tx_to_wname[h]]["address"] for h in timed_out_hashes}
            diag_map = diagnose_stuck_transactions(self.w3, tx_to_addr)
            rescued_txs_map = {} # { w_name: { new_hash: val } }
            
            for h in timed_out_hashes:
                w_name = tx_to_wname[h]
                val = tx_to_val[h]
                diag = diag_map.get(h)
                
                # Handle non-rescuable states (Dropped, Already Mined, or Missing)
                if not diag or diag["status"] == "Dropped or Already Mined" or "Missing" in diag["status"]:
                    reason = diag["status"] if diag else "Unknown"
                    latest = diag.get('latest_nonce', '?') if diag else '?'
                    pending = diag.get('pending_nonce', '?') if diag else '?'
                    
                    # Extract expected nonce from metadata if available
                    expected = val.get('nonce', '?') if isinstance(val, dict) else '?'
                    
                    logger.warning(red_bold(
                        f"[{w_name}] ✗ Rescue skipped: {reason} "
                        f"(Expected: {expected}, Latest: {latest}, Pending: {pending})"
                    ))
                    internal_receipt_callback(w_name, h, val, {}, False)
                    continue

                if diag["status"] == "Nonce Gap":
                    _broadcast({"type": "polling_alert", "level": "info", "message": f"🩹 Auto-Healing Nonce pour {w_name}..."})
                elif diag["status"] == "Underpriced":
                    _broadcast({"type": "polling_alert", "level": "warning", "message": f"🚀 Mode RBF Speed-Up pour {w_name}..."})
                
                # Attempt physical rescue
                new_h = rescue_stuck_transaction(self.w3, wallet_map[w_name], h, diag)
                if new_h:
                    if w_name not in rescued_txs_map: rescued_txs_map[w_name] = {}
                    rescued_txs_map[w_name][new_h] = val
                else:
                    # Failed to rescue or fatal gap
                    internal_receipt_callback(w_name, h, val, {}, False)
                    
            if rescued_txs_map:
                total_rescued = sum(len(txs) for txs in rescued_txs_map.values())
                logger.info(cyan_bold(f"🚀 Wait for {total_rescued} Rescued Transactions ({RESCUE_TIMEOUT}s max)..."))
                _broadcast({"type": "polling_alert", "level": "info", "message": f"🚀 Polling {total_rescued} newly rescued tx..."})
                
                _, final_failed_hashes = wait_transactions_batch(
                    self.w3, 
                    rescued_txs_map, 
                    f"Rescue {phase.action_type}", 
                    receipt_callback=internal_receipt_callback,
                    timeout_override=RESCUE_TIMEOUT
                )
                
                for h in final_failed_hashes:
                    # To map back to the original callback, we need to extract from rescued_txs_map
                    for w_name, hashes in rescued_txs_map.items():
                        if h in hashes:
                            internal_receipt_callback(w_name, h, hashes[h], {}, False)
                            break
                            

# src/actions/dispatch_gas.py — Orchestrator Dispatch Gas Fees

import time
import threading
from typing import List, Dict, Any

from src.config import BLOCK_EXPLORER_URL, GAS_DISPATCH_MIN_BOTTOM, GAS_DISPATCH_STEP, GAS_DISPATCH_TOLERANCE, GAS_ESTIMATE_BUFFER, ACTION_NAMES
from src.services.logger_setup import logger
from src.utils.helpers import cyan_bold, yellow_bold, magenta_bold, red_bold, format_decimal, green_bold
from src.core.blockchain import get_web3, get_batch_wallets_miners_info, get_game_main_contract, get_game_token_contract
from src.core.gas import get_eip1559_gas_params

from src.actions.utils import get_batch_nonces
from src.actions.ui_state import _upd, get_wallet_details, _set_avax_tx
from src.actions.bricks.transfer_avax import run_transfer_avax
from src.actions.phase_engine import PhaseEngine, Phase, SubmissionResult
from src.core.security import ANCHOR_AVAX_TOKEN, validate_authorized_wallet, validate_contract, validate_asset, SecurityException
from src.config import AVAX_TOKEN_ADDRESS

class BatchGasPhaser:
    def __init__(self, w3, engine, gas_params, main_wallet, wallet_nonces):
        self.w3 = w3
        self.engine = engine
        self.gas_params = gas_params
        self.main_wallet = main_wallet
        self.wallet_nonces = wallet_nonces
        self.success_count = 0
        self.nonce_lock = threading.Lock()

    def dispatch_prepare(self, items):
        try:
            if self.main_wallet and len(items) > 0:
                est = self.w3.eth.estimate_gas({
                    "from": items[0][0]["address"], 
                    "to": items[0][1]["address"], 
                    "value": int(items[0][2] * 1e18)
                })
                pre_gas = int(est * GAS_ESTIMATE_BUFFER)
                logger.debug(yellow_bold(f"[GAS] AVAX transfer estimated at {est} units. Applying {pre_gas} gas_limit (+50%)"))
                return pre_gas
        except Exception as e:
            logger.warning(red_bold(f"[GAS] Native AVAX estimation failed, fallback: {e}"))
        return int(21000 * GAS_ESTIMATE_BUFFER)

    def dispatch_submit(self, item, setup_data):
        pre_gas = setup_data or int(21000 * GAS_ESTIMATE_BUFFER)
        sender, dest, amt = item
        
        addr_lower = sender["address"].lower()
        
        with self.nonce_lock:
            nonce = self.wallet_nonces.get(addr_lower)
            self.wallet_nonces[addr_lower] = nonce + 1  # Optimistic increment to prevent race conditions
        
        res = run_transfer_avax(
            sender, self.w3, dest["address"], dest["name"], pre_gas, self.gas_params, amt, nonce
        )
        w_name = res.get("wallet") or sender["name"]
        if res.get("success"):
            if "tx_hash" in res:
                meta = {
                    "type": "avax",
                    "tx_id": res["tx_id"],
                    "dest_name": dest["name"],
                    "amount": amt,
                    "nonce": nonce,
                    "url": f"{BLOCK_EXPLORER_URL}/tx/{res['tx_hash']}"
                }
                return SubmissionResult.success(w_name, {res["tx_hash"]: meta})
            return SubmissionResult.skip(w_name)
        return SubmissionResult.error(w_name, error_msg=res.get("error_msg"))

    def dispatch_submit_error(self, item, error_msg=None):
        sender, dest, amt = item
        w_name = sender["name"]
        err_msg = error_msg or "AVAX submission failed."
        _upd(w_name, error=err_msg, status="error")

    def on_gas_success(self, w_name, tx_hex, val, receipt):
        if isinstance(val, dict) and val.get("type") == "avax":
            dest = val.get("dest_name", "?")
            amt  = val.get("amount", 0.0)
            tx_id = val.get("tx_id")
            url = val.get("url")
            
            _set_avax_tx(w_name, tx_id, {"type": "out", "amount": amt, "target": dest, "status": "success", "tx": url})
            _set_avax_tx(dest, tx_id, {"type": "in", "amount": amt, "target": w_name, "status": "success", "tx": url})

            logger.info(green_bold(f"[{w_name}] ✓ {format_decimal(amt, 4)} AVAX → {dest} confirmed"))
            self.success_count += 1
            _upd(w_name, status="success")

    def on_gas_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        if isinstance(val, dict) and val.get("type") == "avax":
            dest = val.get("dest_name", "?")
            amt  = val.get("amount", 0.0)
            tx_id = val.get("tx_id")
            url = val.get("url", "")
            
            _set_avax_tx(w_name, tx_id, {"type": "out", "amount": amt, "target": dest, "status": "error", "tx": url})
            _set_avax_tx(dest, tx_id, {"type": "in", "amount": amt, "target": w_name, "status": "error", "tx": url})

            if not error_msg: error_msg = "Unknown failure"
            log_err_msg = error_msg.replace("<br/>", " - ")
            logger.error(red_bold(f"[{w_name}] ✗ AVAX transfer to {dest} failed: {log_err_msg}"))
            _upd(w_name, status="error", error=error_msg)

def run_dispatch_gas(target_wallets: List[Dict[str, Any]], burner1_address: str) -> Dict[str, Any]:
    """Main entry point for the Dispatch Gas Fees Action via the UI."""
    if not target_wallets:
        return {"total_claimed": 0.0, "success": False, "error": "No valid wallets."}

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    logger.info(cyan_bold(f"⛽ Launching {ACTION_NAMES['dispatch_gas']} on {len(target_wallets)} wallet(s)..."))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 0: PREPARATION ---"))

    # 1. Web3 & Contracts Preparation (once)
    w3 = get_web3()
    game_main  = get_game_main_contract(w3)
    game_token = get_game_token_contract(w3)
    
    # --- [SECURITY] Verification ---
    try:
        validate_authorized_wallet(burner1_address, "Main Wallet (Burner 1)")
        for w in target_wallets:
            validate_authorized_wallet(w["address"], f"Participant Wallet ({w['name']})")
        validate_contract(game_main.address, "Core Game Main")
        validate_contract(game_token.address, "Core hCASH Token")
        validate_asset(AVAX_TOKEN_ADDRESS, ANCHOR_AVAX_TOKEN, "AVAX Asset Native Anchor")

    except SecurityException as e:
        logger.critical(red_bold(f"[SECURITY] Dispatch aborted: {e}"))
        return {"success": False, "error": str(e), "status": "error"}

    # 2. Balance retrieval (batch_data contains "avax_balance")
    addresses = [w["address"] for w in target_wallets]
    batch_data = get_batch_wallets_miners_info(w3, addresses, game_main, game_token, {})
    
    main_wallet = None
    burners = []
    
    # 3. Separate main_wallet and burners
    for w in target_wallets:
        addr_lower = w["address"].lower()
        info = batch_data.get(w["address"], {"avax_balance": 0.0, "hcash_balance": 0.0})
        bal = info.get("avax_balance", 0.0)
        
        is_main = addr_lower == burner1_address.lower()
        if is_main:
            main_wallet = (w, bal)
        else:
            burners.append((w, bal))

    if not burners:
        if main_wallet:
            # Atomic skip for the main wallet if we abort
            _upd(main_wallet[0]["name"], status="skipped")
        summary = "No targets."
        logger.info(yellow_bold(f"⛽ {summary}"))
        return {"summary": summary, "success": True}
        
    # 4. Immediate Atomic Initialization (No flicker)
    if main_wallet:
        _upd(main_wallet[0]["name"], initial_balance=main_wallet[1])

    burner_initial_data = [] # List of tuples to preserve calc order
    N = len(burners)
    for i, (b, bal) in enumerate(burners):
        target = GAS_DISPATCH_MIN_BOTTOM + (N - 1 - i) * GAS_DISPATCH_STEP
        burner_initial_data.append((b, bal, target))
        # Update cards with balances and targets
        _upd(b["name"], initial_balance=bal, target_balance=target)
        
    providers = []
    receivers = []
    active_participant_names = set()
    
    # 5. Tolerance Filter & matching
    for (b, bal, target) in burner_initial_data:
        diff = bal - target
        if abs(diff) <= GAS_DISPATCH_TOLERANCE:
            _upd(b["name"], status="skipped", skipped_reason="This Wallet is already balanced")
            logger.debug(yellow_bold(f"[GAS] {b['name']} is balanced (~{bal:.3f} for target {target:.3f})."))
        elif diff > 0:
            providers.append({"wallet": b, "excess": diff, "is_main": False})
        else:
            receivers.append({"wallet": b, "deficit": abs(diff)})
            
    # Main Wallet as provider
    if main_wallet and main_wallet[1] > 0.05:
        providers.append({"wallet": main_wallet[0], "excess": float('inf'), "is_main": True})

    # Debt Matching Algorithm
    transfers = [] # (sender_w, dest_w, amount)
    for rec in receivers:
        needed = rec["deficit"]
        while needed > 0.00001 and providers:
            prov = providers[0]
            amount_to_send = min(needed, prov["excess"])
            transfers.append((prov["wallet"], rec["wallet"], amount_to_send))
            active_participant_names.add(prov["wallet"]["name"])
            active_participant_names.add(rec["wallet"]["name"])
            needed -= amount_to_send
            if not prov["is_main"]:
                prov["excess"] -= amount_to_send
                if prov["excess"] <= 0.00001: providers.pop(0)

    # Return final excesses to Main Wallet
    for prov in providers:
        if not prov["is_main"] and prov["excess"] > GAS_DISPATCH_TOLERANCE:
            if main_wallet:
                transfers.append((prov["wallet"], main_wallet[0], prov["excess"]))
                active_participant_names.add(prov["wallet"]["name"])
                active_participant_names.add(main_wallet[0]["name"])

    if not transfers:
        logger.info(yellow_bold("⛽ All selected wallets are already balanced or ignored based on tolerance."))
        if main_wallet:
            _upd(main_wallet[0]["name"], status="success", recap_html="⛽ All selected wallets are already balanced.")
        
        # Set any remaining 'running' wallets to 'success'
        # Do not touch those already marked as 'skipped' during the tolerance filter
        details = get_wallet_details()
        for b, bal, target in burner_initial_data:
            w_name = b["name"]
            if details.get(w_name, {}).get("status") == "running":
                _upd(w_name, status="success")
            
        summary = "Wallets are already all balanced"
        return {"summary": summary, "success": True}
        
    # Final status sync before PhaseEngine
    # (Optional: set those NOT in active_participant_names but NOT skipped to success?)
    # ...
        
    # --- PHASE 1: DISPATCH GAS ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 1: GAS DISPATCH ---"))
    gas_params = get_eip1559_gas_params(w3)
    if not gas_params:
        summary = "Prohibitive gas"
        logger.error(red_bold(f"[GAS] {summary} (safety triggered). Cancelling."))
        return {"summary": summary, "success": False}

    # --- NONCE BATCH RPC ---
    senders = list(set(t[0]["address"] for t in transfers))
    wallet_nonces = get_batch_nonces(w3, senders)
        
    engine = PhaseEngine(w3, last_phase_map={t[0]["name"]: "Gas Dispatch" for t in transfers})
    ctx = BatchGasPhaser(w3, engine, gas_params, main_wallet, wallet_nonces)
    total_tx = len(transfers)

    phase = Phase(
        name="Gas Dispatch",
        action_type="Gas Dispatch",
        items=transfers,
        prepare_fn=ctx.dispatch_prepare,
        submit_fn=ctx.dispatch_submit,
        on_receipt_success=ctx.on_gas_success,
        on_receipt_error=ctx.on_gas_error,
        on_submit_error=ctx.dispatch_submit_error
    )
    engine.run_phase(phase)

    # --- FINAL UI RECONCILIATION ---
    if main_wallet:
        total_avax_out = sum(t[2] for t in transfers if t[0]["name"] == main_wallet[0]["name"])
        total_avax_in = sum(t[2] for t in transfers if t[1]["name"] == main_wallet[0]["name"])
        
        recap = f"Txs Validated: {ctx.success_count}/{total_tx}<br>"
        recap += f"Total transferred from: <span class=\"privacy-data\">{format_decimal(total_avax_out, 4)}</span> AVAX<br>"
        recap += f"Total Returned to Main Wallet: <span class=\"privacy-data\">{format_decimal(total_avax_in, 4)}</span> AVAX"
        _upd(main_wallet[0]["name"], recap_html=f"⛽ {recap}")

    # Dynamic status cleanup for the UI
    details = get_wallet_details()
    for w in target_wallets:
        wn = w["name"]
        d = details.get(wn)
        if not d or d["status"] != "running":
            continue

        # Look for errors in this wallet's transactions
        has_error = False
        tx_logs = d.get("transfer_avax_txs", {})
        if tx_logs:
            for tx in tx_logs.values():
                if tx.get("status") == "error":
                    has_error = True
                    break
        
        if has_error:
            _upd(wn, status="error", error="An AVAX transaction failed.")
        else:
            _upd(wn, status="success")

    # Final report calculation for logs
    num_success = ctx.success_count

    if num_success == total_tx:
        summary = f"✅ Operations completed successfully - {num_success} transfers."
        status = "success"
    elif num_success > 0:
        summary = f"⚠️ {num_success} / {total_tx} operations successful - Partial."
        status = "partial"
    else:
        summary = f"❌ {ACTION_NAMES['dispatch_gas']} action failed."
        status = "error"

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    logger.info(cyan_bold(f"{summary}"))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    
    return {
        "success": num_success > 0, 
        "summary": summary, 
        "status": status
    }

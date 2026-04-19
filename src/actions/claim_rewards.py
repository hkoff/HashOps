# src/actions/claim_rewards.py — Orchestrator Claim All & Transfer hCASH to main wallet

from web3 import Web3
from typing import List, Dict, Any

from src.config import CLAIM_THRESHOLD, GAS_ESTIMATE_BUFFER, DEFAULT_GAS_CLAIM, DEFAULT_GAS_TRANSFER, ACTION_NAMES, ACTION_KEY_CLAIM
from src.services.logger_setup import logger
from src.utils.helpers import cyan_bold, format_decimal, yellow_bold, magenta_bold, red_bold, green_bold
from src.core.blockchain import get_web3, get_game_token_contract, get_game_main_contract, get_multiple_wallets_data
from src.core.gas import get_eip1559_gas_params

from src.actions.utils import get_batch_nonces
from src.actions.ui_state import _init_detail, _upd
from src.actions.bricks.claim_hcash import run_claim_single_wallet, process_claim_receipt
from src.actions.bricks.transfer_hcash import run_transfer_single_wallet
from src.actions.phase_engine import PhaseEngine, Phase, SubmissionResult

class BatchClaimPhaser:
    def __init__(self, w3, game_main, game_token, engine, gas_params, eligible, burner1_address, wallet_nonces):
        self.w3 = w3
        self.game_main = game_main
        self.game_token = game_token
        self.engine = engine
        self.gas_params = gas_params
        self.eligible = eligible
        self.burner1_address = burner1_address
        self.wallet_nonces = wallet_nonces
        self.total_net = 0.0
        self.total_transferred = 0.0

    # --- PHASE 1: CLAIM ---
    def claim_prepare(self, items):
        try:
            estim_w = next((x[0] for x in items if x[1] > 0.0), None)
            if estim_w:
                est = self.game_main.functions.claimRewards().estimate_gas({'from': estim_w["address"]})
                pre_gas = int(est * GAS_ESTIMATE_BUFFER)
                logger.info(yellow_bold(f"[GAS] Claim estimated at {est} units. Applying {pre_gas} (+50%)"))
                return pre_gas
        except Exception as e:
            logger.warning(red_bold(f"[GAS] Error during Claim estimation: {e}"))
        return DEFAULT_GAS_CLAIM

    def claim_submit(self, item, pre_gas):
        w, pending, balance, balance_wei = item
        if not pre_gas: pre_gas = DEFAULT_GAS_CLAIM
        
        addr_lower = w["address"].lower()
        nonce = self.wallet_nonces.get(addr_lower)
        
        res = run_claim_single_wallet(
            w, self.w3, self.game_main, self.game_token, pre_gas, self.gas_params, 
            pending, balance, balance_wei, nonce=nonce
        )
        if res.get("success"):
            # Update nonce from brick result (incremented only if broadcast succeeded)
            if "next_nonce" in res: self.wallet_nonces[addr_lower] = res["next_nonce"]
            
            if "tx_hash" in res:
                return SubmissionResult.success(w["name"], {res["tx_hash"]: None})
            return SubmissionResult.skip(w["name"])
        return SubmissionResult.error(w["name"], error_msg=res.get("error_msg"))

    def claim_submit_error(self, item, error_msg=None):
        w_name = item[0]["name"]
        err_msg = error_msg or "Claim submission failed."
        _upd(w_name, claim_status="error", status="error", error=err_msg)
        self.engine.global_failed_items.add(w_name)

    def on_claim_success(self, w_name, tx_hex, val, receipt):
        # Rewards Tracking (Zero-RPC Multi-Wallet)
        found = process_claim_receipt(receipt, self.game_token)
        
        # Build address map for lookup
        address_map = {Web3.to_checksum_address(x[0]["address"]): x[0]["name"] for x in self.eligible}
        # Get triggering wallet address
        trigger_addr = next((Web3.to_checksum_address(x[0]["address"]) for x in self.eligible if x[0]["name"] == w_name), None)
        
        claimed_for_trigger = 0.0
        for entry in found:
            recipient = entry["recipient"]
            claimed = entry["amount"]
            claimed_wei = entry["amount_wei"]
            
            if recipient == trigger_addr:
                claimed_for_trigger = claimed

            if recipient in address_map:
                target_w_name = address_map[recipient]
                # Find the target wallet object and its initial state
                target_w_obj = next((x[0] for x in self.eligible if x[0]["name"] == target_w_name), None)
                init_b = next((x[2] for x in self.eligible if x[0]["name"] == target_w_name), 0.0)
                init_b_wei = next((x[3] for x in self.eligible if x[0]["name"] == target_w_name), 0)
                
                if target_w_obj:
                    self.total_net += claimed
                    target_w_obj["post_claim_balance"] = init_b + claimed
                    target_w_obj["post_claim_balance_wei"] = init_b_wei + claimed_wei
                    logger.info(green_bold(f"[{target_w_name}] Claim account confirmed: +{round(claimed, 4)} hCASH"))
        
        _upd(w_name, claim_status="success", actual_claimed=round(claimed_for_trigger, 4))
        logger.info(green_bold(f"[{w_name}] ✓ Phase 1 OK (+{format_decimal(claimed_for_trigger)} hCASH)"))

    def on_claim_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        if not error_msg: error_msg = "Unknown failure"
        log_err_msg = error_msg.replace("<br/>", " - ")
        _upd(w_name, claim_status="error", status="error", error=error_msg)
        logger.error(red_bold(f"[{w_name}] Phase 1 failed: {log_err_msg}"))
        self.engine.global_failed_items.add(w_name)

    # --- PHASE 2: TRANSFER ---
    def transfer_prepare(self, items):
        try:
            if items:
                first_w, bal_f, bal_w = items[0]
                est = self.game_token.functions.transfer(self.burner1_address, bal_w).estimate_gas({'from': first_w["address"]})
                pre_gas = int(est * GAS_ESTIMATE_BUFFER)
                logger.info(yellow_bold(f"[GAS] Transfer estimated at {est} units. Applying {pre_gas} (+50%)"))
                return pre_gas
        except Exception as e:
            logger.warning(red_bold(f"[GAS] Error during Transfer estimation: {e}"))
        return DEFAULT_GAS_TRANSFER

    def transfer_submit(self, item, pre_gas):
        w, bal_f, bal_w = item
        if not pre_gas: pre_gas = DEFAULT_GAS_TRANSFER
        
        addr_lower = w["address"].lower()
        nonce = self.wallet_nonces.get(addr_lower)
        
        res = run_transfer_single_wallet(
            w, self.w3, self.game_token, self.burner1_address, pre_gas, self.gas_params,
            bal_f, bal_w, nonce=nonce
        )
        if res.get("success"):
            # Update nonce from brick result
            if "next_nonce" in res: self.wallet_nonces[addr_lower] = res["next_nonce"]

            if "tx_hash" in res:
                self.total_transferred += res.get("transferred", 0.0)
                return SubmissionResult.success(w["name"], {res["tx_hash"]: None})
            return SubmissionResult.skip(w["name"])
        return SubmissionResult.error(w["name"], error_msg=res.get("error_msg"))

    def transfer_submit_error(self, item, error_msg=None):
        w_name = item[0]["name"]
        err_msg = error_msg or "Transfer submission failed."
        _upd(w_name, transfer_status="error", status="error", error=err_msg)
        self.engine.global_failed_items.add(w_name)

    def on_transfer_success(self, w_name, tx_hex, val, receipt):
        logger.info(green_bold(f"[{w_name}] Phase 2 Transfer confirmed"))
        _upd(w_name, transfer_status="success", balance=0.0, total=0.0, status="success")

    def on_transfer_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        if not error_msg: error_msg = "Unknown failure"
        log_err_msg = error_msg.replace("<br/>", " - ")
        logger.error(red_bold(f"[{w_name}] Phase 2 Transfer failed: {log_err_msg}"))
        _upd(w_name, transfer_status="error", status="error", error=error_msg)
        self.engine.global_failed_items.add(w_name)

def run_claim_all(target_wallets: List[Dict[str, Any]], burner1_address: str) -> Dict[str, Any]:
    """Main entry point for the Claim Action via the UI."""
    if not target_wallets:
        return {"total_claimed": 0.0, "success": False, "error": "No valid wallets."}

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    logger.info(cyan_bold(f"🚀 Launching {ACTION_NAMES[ACTION_KEY_CLAIM]} on {len(target_wallets)} wallet(s)..."))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    
    # 1. Web3 & Contracts Preparation (once)
    w3 = get_web3()
    game_main  = get_game_main_contract(w3)
    game_token = get_game_token_contract(w3)
    
    # --- PHASE 0: BATCH VERIFICATION (MULTICALL3) ---
    logger.debug(magenta_bold("══════════════════════════════════════════════"))
    logger.debug(magenta_bold("--- PHASE 0: BATCH VERIFICATION (MULTICALL3) ---"))  

    addresses = [w["address"] for w in target_wallets]
    batch_data = get_multiple_wallets_data(w3, addresses, game_main, game_token)
    
    eligible = []
    for w in target_wallets:
        addr_lower = w["address"].lower()
        info = batch_data.get(addr_lower, {"pending": 0.0, "balance": 0.0, "balance_wei": 0})
        total_p = info["pending"] + info["balance"]
        
        # Initialize wallet cards explicitly for BATCH action
        _init_detail(w["name"], w["address"], status="running", initial_pending=info["pending"], initial_balance=info["balance"])
        
        if total_p >= CLAIM_THRESHOLD:
            eligible.append((w, info["pending"], info["balance"], info["balance_wei"]))
            logger.debug(yellow_bold(f"[{w['name']}] Eligible: {format_decimal(total_p, 2)} >= {CLAIM_THRESHOLD}"))
        else:
            logger.debug(yellow_bold(f"[{w['name']}] Skip: {format_decimal(total_p, 2)} < {CLAIM_THRESHOLD}"))
            _upd(w["name"], status="skipped")

    if not eligible:
        logger.info(cyan_bold(f"══════════════════════════════════════════════"))
        logger.info(yellow_bold(f"No addresses reach the threshold of {CLAIM_THRESHOLD} tokens."))
        logger.info(cyan_bold(f"══════════════════════════════════════════════"))
        return {"total_claimed": 0.0, "success": True}

    # --- GAS PARAMS ---
    gas_params = get_eip1559_gas_params(w3)
    if not gas_params:
        logger.error(red_bold("[GAS] Prohibitive gas price (safety triggered). Cancelling."))
        return {"total_claimed": 0.0, "success": False, "error": "Prohibitive gas."}

    # --- NONCE BATCH RPC ---
    eligible_addrs = [x[0]["address"] for x in eligible]
    wallet_nonces = get_batch_nonces(w3, eligible_addrs)

    engine = PhaseEngine(w3)
    ctx = BatchClaimPhaser(w3, game_main, game_token, engine, gas_params, eligible, burner1_address, wallet_nonces)

    # --- PHASE 1: CLAIM ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 1: CLAIM ---"))

    phase1 = Phase(
        name="Claim",
        action_type="Claiming Rewards",
        items=eligible,
        prepare_fn=ctx.claim_prepare,
        submit_fn=ctx.claim_submit,
        on_receipt_success=ctx.on_claim_success,
        on_receipt_error=ctx.on_claim_error,
        on_submit_error=ctx.claim_submit_error
    )
    engine.run_phase(phase1)

    # --- PHASE 2: BATCH REFRESH & TRANSFER ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 2: TRANSFER ---"))
    
    to_transfer = []
    for w, pending, initial_balance, initial_balance_wei in eligible:
        post_bal = w.get("post_claim_balance", initial_balance)
        bal_wei = w.get("post_claim_balance_wei", initial_balance_wei)
        
        # Optimized Virtual UI Sync
        _upd(w["name"], balance=post_bal, pending=0.0, total=post_bal)
        
        addr_lower = w["address"].lower()
        if addr_lower == burner1_address.lower(): continue
        
        if bal_wei > 100000000000000: # 0.0001 hcash in wei
            to_transfer.append((w, post_bal, bal_wei))

    # Terminal UI status synchronization
    transfer_wallets_names = {w["name"] for w, _, _ in to_transfer}
    for w, _, _, _ in eligible:
        addr_lower = w["address"].lower()
        if w["name"] not in transfer_wallets_names and addr_lower != burner1_address.lower():
            if w["name"] in engine.global_failed_items:
                _upd(w["name"], status="error")
            else:
                _upd(w["name"], status="success")

    total_transferred = 0.0
    to_transfer_active = [x for x in to_transfer if x[0]["name"] not in engine.global_failed_items]

    engine.last_phase_map = {x[0]["name"]: "Transfer Rewards" for x in to_transfer_active}
    
    phase2 = Phase(
        name="Transfer",
        action_type="Transfer Rewards",
        items=to_transfer_active,
        prepare_fn=ctx.transfer_prepare,
        submit_fn=ctx.transfer_submit,
        on_receipt_success=ctx.on_transfer_success,
        on_receipt_error=ctx.on_transfer_error,
        on_submit_error=ctx.transfer_submit_error
    )
    engine.run_phase(phase2)

    # Completion signal on the main wallet
    main_w = next((w for w in target_wallets if w["address"].lower() == burner1_address.lower()), None)
    if main_w:
        recap = f"📤 Total Transferred: <span class=\"privacy-data\">{format_decimal(ctx.total_transferred, 4)}</span> hCASH<br>"
        recap += f"⛏️ Total Net Claimed: <span class=\"privacy-data\">{format_decimal(ctx.total_net, 4)}</span> hCASH "
        # SVG_COPY to be replaced
        recap += f"<svg-icon name=\"copy\" class=\"copy-btn\" onclick=\"copyToClipboard('{ctx.total_net}')\" title=\"Copy amount\"></svg-icon>"
        _upd(main_w["name"], recap_html=recap, status="success")

    # Final tally calculation
    total_eligible = len(eligible)
    num_failed = len([n for n in engine.global_failed_items if any(x[0]["name"] == n for x in eligible)])
    num_success = total_eligible - num_failed
    
    if num_success == total_eligible:
        summary = f"✅ Claim & Transfer successful ({num_success} wallets)"
        status = "success"
    elif num_success > 0:
        summary = f"⚠️ {num_success} / {total_eligible} wallets processed"
        status = "partial"
    else:
        summary = f"❌ {ACTION_NAMES[ACTION_KEY_CLAIM]} action failed."
        status = "error"

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    msg = f"{summary} Net claimed: {format_decimal(ctx.total_net, 4)} hCASH"
    if ctx.total_transferred > 0:
        msg += f" | Transferred: {format_decimal(ctx.total_transferred, 4)} hCASH"
    logger.info(cyan_bold(msg))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    
    return {
        "total_claimed": ctx.total_net, 
        "success": num_success > 0, 
        "summary": summary, 
        "status": status
    }

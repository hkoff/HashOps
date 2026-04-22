# src/actions/batch_handle_nft_miners.py — Orchestrator Manage Miner & NFT (Withdraw, Transfer, Place)

from typing import List, Dict, Any
from web3 import Web3

from src.config import (
    DEFAULT_GAS_WITHDRAW, DEFAULT_GAS_TRANSFER, DEFAULT_GAS_PLACE, 
    GAS_ESTIMATE_BUFFER, ACTION_NAMES, NULL_ADDRESS, HCASH_LOGO_URL
)
from src.services.logger_setup import logger
from src.utils.helpers import magenta_bold, yellow_bold, cyan_bold, red_bold, green_bold, format_decimal
from src.core.blockchain import get_web3, get_game_main_contract, get_game_token_contract, get_nft_contract, get_miner_contract_address, _MINER_REGISTRY    
from src.core.gas import get_eip1559_gas_params

from src.actions.utils import get_batch_nonces
from src.actions.ui_state import _init_detail, _init_generic_card, _log_miner_action, _prepare_miner_journey, _upd, get_wallet_details
from src.actions.bricks.withdraw_miner import run_withdraw_batch_for_wallet
from src.actions.bricks.transfer_miner import run_transfer_batch_for_wallet
from src.actions.bricks.place_miner import run_place_batch_for_wallet, get_facility_and_placed_coords, get_empty_coordinates
from src.actions.bricks.claim_hcash import process_claim_receipt     

from src.actions.phase_engine import PhaseEngine, Phase, SubmissionResult
from src.core.security import validate_authorized_wallet, validate_contract, SecurityException

class BatchMinersPhaser:
    """Encapsulates the methods for each phase to clean up the main entry point."""
    def __init__(self, w3: Web3, game_main: Any, game_token: Any, engine: PhaseEngine, gas_params: Dict[str, int], data: Dict[str, Any], last_phase_per_wallet: Dict[str, str], wallet_nonces: Dict[str, int], wallets: List[Dict[str, Any]]):
        self.w3 = w3
        self.game_main = game_main
        self.game_token = game_token
        self.engine = engine
        self.gas_params = gas_params
        self.data = data
        self.last_phase_per_wallet = last_phase_per_wallet
        self.wallet_nonces = wallet_nonces
        self.wallets = wallets
        
        # Build a lookup map: {checksummed_address: wallet_name}
        self.address_map = {Web3.to_checksum_address(w["address"]): w["name"] for w in self.wallets}
        
        # Rewards Tracking (Zero-RPC)
        self.total_claimed = 0.0
        self.per_wallet_rewards = {} # {wallet_name: float}

    def _track_rewards_from_receipt(self, receipt):
        """Helper to capture all hCASH rewards in a receipt and attribute them to our wallets."""
        found = process_claim_receipt(receipt, self.game_token)
        for entry in found:
            recipient = entry["recipient"]
            amount = entry["amount"]
            if recipient in self.address_map:
                w_name = self.address_map[recipient]
                self.total_claimed += amount
                self.per_wallet_rewards[w_name] = self.per_wallet_rewards.get(w_name, 0.0) + amount
                logger.info(green_bold(f"[{w_name}] Captured {round(amount, 4)} hCASH rewards from transaction"))

    # --- UI Callbacks ---
    def on_withdraw_success(self, w_name, tx_hex, val, receipt):
        m_id = val[0] if isinstance(val, tuple) else val
        if m_id is not None:
            _log_miner_action(w_name, m_id, "Withdraw", status="success")
            logger.info(green_bold(f"[{w_name}] ✓ Withdraw verified for Miner #{m_id}"))
            
            # --- Capture rewards for ANY of our wallets in this receipt ---
            self._track_rewards_from_receipt(receipt)

    def on_withdraw_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        m_id = val[0] if isinstance(val, tuple) else val
        if m_id is not None:
            if not error_msg: error_msg = "Unknown failure"
            log_err_msg = error_msg.replace("<br/>", " - ")
            logger.error(red_bold(f"[{w_name}] ❌ Withdraw failed for Miner #{m_id}: {log_err_msg}"))
            _log_miner_action(w_name, m_id, "Withdraw", status="error", error_msg=error_msg)
            self.engine.global_failed_items.add(m_id)
            _upd(w_name, status="error", error=error_msg)

    def on_transfer_success(self, w_name, tx_hex, val, receipt):
        m_id = val[0] if isinstance(val, tuple) else val
        dest = val[1] if isinstance(val, tuple) else None
        if m_id is not None:
            _log_miner_action(w_name, m_id, "Transfer", status="success")
            logger.info(green_bold(f"[{w_name}] ✓ Transfer verified for Miner #{m_id} to {dest}"))
            if dest: 
                # dest is already the resolved name from metadata
                _log_miner_action(dest, m_id, "Received", status="success")

    def on_transfer_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        m_id = val[0] if isinstance(val, tuple) else val
        dest = val[1] if isinstance(val, tuple) else None
        if m_id is not None:
            if not error_msg: error_msg = "Unknown failure"
            log_err_msg = error_msg.replace("<br/>", " - ")
            logger.error(red_bold(f"[{w_name}] ❌ Transfer failed for Miner #{m_id}: {log_err_msg}"))
            _log_miner_action(w_name, m_id, "Transfer", status="error", error_msg=error_msg)
            if dest: 
                # dest is already the resolved name from metadata
                _log_miner_action(dest, m_id, "Received", status="error", error_msg="Transfer failed")
            self.engine.global_failed_items.add(m_id)
            _upd(w_name, status="error", error=error_msg)

    def on_place_success(self, w_name, tx_hex, val, receipt):
        m_id = val[0] if isinstance(val, tuple) else val
        if m_id is not None:
            _log_miner_action(w_name, m_id, "Place", status="success")
            logger.info(green_bold(f"[{w_name}] ✓ Place verified for Miner #{m_id}"))

            # --- Capture rewards for ANY of our wallets in this receipt ---
            self._track_rewards_from_receipt(receipt)

    def on_place_error(self, w_name, tx_hex, val, receipt, error_msg="Unknown failure"):
        m_id = val[0] if isinstance(val, tuple) else val
        if m_id is not None:
            if not error_msg: error_msg = "Unknown failure"
            log_err_msg = error_msg.replace("<br/>", " - ")
            logger.error(red_bold(f"[{w_name}] ❌ Place failed for Miner #{m_id}: {log_err_msg}"))
            _log_miner_action(w_name, m_id, "Place", status="error", error_msg=error_msg)
            self.engine.global_failed_items.add(m_id)
            _upd(w_name, status="error", error=error_msg)

    # --- Prepare/Submit Withdraw ---
    def withdraw_prepare(self, items):
        try:
            w0 = items[0]
            m0_id = self.data[w0["name"]]["withdraws"][0]["id"]
            est = self.game_main.functions.withdrawMiner(m0_id).estimate_gas({'from': w0["address"]})
            pre_gas = int(est * GAS_ESTIMATE_BUFFER)
            logger.debug(yellow_bold(f"[GAS] Withdraw estimated at {est}. Buffer -> {pre_gas}"))
            return pre_gas
        except Exception as e:
            logger.warning(red_bold(f"[GAS] Error during Withdraw estimation: {e}"))
            return DEFAULT_GAS_WITHDRAW

    def withdraw_submit(self, w, setup_data):
        pre_gas = setup_data or DEFAULT_GAS_WITHDRAW
        addr_lower = w["address"].lower()
        base_nonce = self.wallet_nonces.get(addr_lower)
        
        w_txs, next_nonce, brick_err = run_withdraw_batch_for_wallet(w, self.data[w["name"]].get("withdraws", []), self.w3, self.game_main, pre_gas, self.gas_params, base_nonce=base_nonce)
        if next_nonce is not None:
            self.wallet_nonces[addr_lower] = next_nonce
            
        if w_txs:
            return SubmissionResult.success(w["name"], w_txs)
        if not self.data[w["name"]].get("withdraws"):
            return SubmissionResult.skip(w["name"])
        return SubmissionResult.error(w["name"], error_msg=brick_err)

    def withdraw_submit_error(self, w, error_msg=None):
        w_name = w["name"]
        err_msg = error_msg or "Withdraw submission failed."
        # Force error on remaining miners if not already done
        for item in self.data[w_name].get("withdraws", []):
            if item["id"] not in self.engine.global_failed_items:
                self.engine.global_failed_items.add(item["id"])
                _log_miner_action(w_name, item["id"], "Withdraw", status="error", error_msg=err_msg)
        
        # Force Wallet Card creation on critical submission error
        addr = next((wal["address"] for wal in self.wallets if wal["name"] == w_name), None)
        if addr: _init_detail(w_name, addr, status="error", error=err_msg)
        else: _upd(w_name, status="error", error=err_msg)

    # --- Prepare/Submit Transfer ---
    def transfer_prepare(self, items):
        try:
            gas_dict = {}
            estimated_groups = {}  # Cache estimations by category

            standard_miner_addrs = list(_MINER_REGISTRY.values())

            for w in items:
                items_for_w = self.data[w["name"]].get("transfers", [])
                
                for p_item in items_for_w:
                    nft_addr = NULL_ADDRESS
                    if p_item.get("nft") and p_item.get("nft").lower() != "undefined":
                        nft_addr = Web3.to_checksum_address(p_item["nft"])
                    elif p_item.get("type_idx") is not None:
                        nft_addr = get_miner_contract_address(p_item["type_idx"])

                    if nft_addr == NULL_ADDRESS:
                        continue

                    # Robust category detection via registry
                    if nft_addr in standard_miner_addrs:
                        category = "miner_nft"
                    else:
                        # Group all external NFTs into a single category as per user instructions
                        category = "external_nft"

                    if nft_addr not in gas_dict:
                        if category in estimated_groups:
                            # Already estimated this category, reuse it for this contract!
                            gas_dict[nft_addr] = estimated_groups[category]
                        else:
                            # 1st time seeing this category -> run estimation
                            nft_id = p_item.get("nft_token_id")
                            if nft_id is not None:
                                dest_chk = Web3.to_checksum_address(p_item["dest"])
                                nft_contract = get_nft_contract(self.w3, nft_addr)

                                try:
                                    est = nft_contract.functions.safeTransferFrom(w["address"], dest_chk, nft_id).estimate_gas({'from': w["address"]})
                                    pre_gas = int(est * GAS_ESTIMATE_BUFFER)
                                    logger.debug(yellow_bold(f"[GAS] Transfer estimated at {est} for category '{category}' (NFT {nft_addr}). Buffer -> {pre_gas}"))
                                    
                                    estimated_groups[category] = pre_gas
                                    gas_dict[nft_addr] = pre_gas
                                except Exception as e:
                                    logger.debug(f"[GAS] Error during Transfer estimation for {nft_addr} on wallet {w['name']}: {e}")

            if gas_dict:
                return gas_dict
            else:
                return DEFAULT_GAS_TRANSFER
        except Exception as e:
            logger.debug(f"[GAS] Error during Transfer preparation: {e}")
            return DEFAULT_GAS_TRANSFER

    def transfer_submit(self, w, setup_data):
        pre_gas = setup_data or DEFAULT_GAS_TRANSFER
        items_filtered = [item for item in self.data[w["name"]].get("transfers", []) if item["id"] not in self.engine.global_failed_items]
        
        addr_lower = w["address"].lower()
        base_nonce = self.wallet_nonces.get(addr_lower)
            
        t_txs, next_nonce, brick_err = run_transfer_batch_for_wallet(w, items_filtered, self.w3, pre_gas, self.gas_params, base_nonce=base_nonce)
        if next_nonce is not None:
            self.wallet_nonces[addr_lower] = next_nonce
            
        if t_txs:
            return SubmissionResult.success(w["name"], t_txs)
        if not items_filtered:
            return SubmissionResult.skip(w["name"])
        return SubmissionResult.error(w["name"], error_msg=brick_err)

    def transfer_submit_error(self, w, error_msg=None):
        w_name = w["name"]
        err_msg = error_msg or "Transfer submission failed."
        for item in self.data[w_name].get("transfers", []):
            if item["id"] not in self.engine.global_failed_items:
                self.engine.global_failed_items.add(item["id"])
                _log_miner_action(w_name, item["id"], "Transfer", status="error", error_msg=err_msg)
        
        # Force Wallet Card creation on critical submission error
        addr = next((wal["address"] for wal in self.wallets if wal["name"] == w_name), None)
        if addr: _init_detail(w_name, addr, status="error", error=err_msg)
        else: _upd(w_name, status="error", error=err_msg)

    # --- Prepare/Submit Place ---
    def filter_places(self, places_list):
        clean, skipped = [], []
        for item in places_list:
            if item["id"] in self.engine.global_failed_items:
                skipped.append(item)
            else:
                clean.append(item)
        return clean, skipped

    def place_prepare(self, items):
        try:
            w0 = items[0]
            clean0, _ = self.filter_places(self.data[w0["name"]]["places"])
            p0 = clean0[0]
            target_nft = Web3.to_checksum_address(p0["nft"]) if p0.get("nft") and p0.get("nft").lower() != "undefined" else NULL_ADDRESS
            nft_id = p0.get("nft_token_id")
            if nft_id is None:
                nft_id = p0["id"] # Fallback to miner id
                
            # Fetch empty coordinates dynamically like in submit
            addr = w0["address"]
            max_x, max_y, max_m, placed_coords = get_facility_and_placed_coords(self.game_main, addr)
            cx, cy = get_empty_coordinates(max_x, max_y, max_m, placed_coords)
            
            if cx == -1 or cy == -1:
                return DEFAULT_GAS_PLACE
            
            logger.debug(magenta_bold(f"[GAS] DEBUG Try Place => target_nft={target_nft}, nft_id={nft_id}, cx={cx}, cy={cy}, from={w0['name']}"))
            
            # --- Approval pre-check to avoid estimation revert ---
            try:
                nft_c = get_nft_contract(self.w3, target_nft)
                is_appr = nft_c.functions.isApprovedForAll(addr, self.game_main.address).call()
                if not is_appr:
                    logger.debug(yellow_bold(f"[GAS] Place: Missing approval for {w0['name']}. Using default gas."))
                    return DEFAULT_GAS_PLACE
            except Exception as e:
                logger.debug(f"[GAS] Error during approval check: {e}")

            est = self.game_main.functions.placeMiner(target_nft, nft_id, cx, cy).estimate_gas({'from': addr})
            pre_gas = int(est * GAS_ESTIMATE_BUFFER)
            logger.debug(yellow_bold(f"[GAS] Place estimated at {est}. Buffer -> {pre_gas}"))
            return pre_gas
        except Exception as e:
            logger.warning(red_bold(f"[GAS] Error during Place estimation: {e}"))
            return DEFAULT_GAS_PLACE

    def place_submit(self, w, setup_data):
        pre_gas = setup_data or DEFAULT_GAS_PLACE
        clean_items, _ = self.filter_places(self.data[w["name"]].get("places", []))
        
        addr_lower = w["address"].lower()
        base_nonce = self.wallet_nonces.get(addr_lower)
        p_txs, next_nonce, brick_err = run_place_batch_for_wallet(w, clean_items, self.w3, self.game_main, pre_gas, self.gas_params, base_nonce=base_nonce)
        if next_nonce is not None:
            self.wallet_nonces[addr_lower] = next_nonce
            
        if p_txs:
            return SubmissionResult.success(w["name"], p_txs)
        if not clean_items:
            return SubmissionResult.skip(w["name"])
        return SubmissionResult.error(w["name"], error_msg=brick_err)

    def place_submit_error(self, w, error_msg=None):
        w_name = w["name"]
        err_msg = error_msg or "Place submission failed."
        for item in self.data[w_name].get("places", []):
            if item["id"] not in self.engine.global_failed_items:
                self.engine.global_failed_items.add(item["id"])
                _log_miner_action(w_name, item["id"], "Place", status="error", error_msg=err_msg)
        
        # Force Wallet Card creation on critical submission error
        addr = next((wal["address"] for wal in self.wallets if wal["name"] == w_name), None)
        if addr: _init_detail(w_name, addr, status="error", error=err_msg)
        else: _upd(w_name, status="error", error=err_msg)


def run_all_miners_batches(target_wallets: List[Dict[str, Any]], data: Dict[str, Any]) -> Dict[str, Any]:
    """Main entry point for Manage Miners & NFT Action (Withdraw/Transfer/Place)."""
    if not target_wallets:
        return {"success": False, "error": "No target wallets."}

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    logger.info(cyan_bold(f"🚀 Launching Batch Orchestrator on {len(target_wallets)} wallet(s)..."))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    
    # --- PHASE 0: SETUP ---
    w3 = get_web3()
    game_main = get_game_main_contract(w3)    
    game_token = get_game_token_contract(w3)
    gas_params = get_eip1559_gas_params(w3)
    
    if not gas_params:
        return {"success": False, "error": "Prohibitive gas."}

    # Determine the planned last phase for each wallet for the UI
    last_phase_per_wallet = {}
    for w in target_wallets:
        wn = w["name"]
        wdata = data.get(wn, {})
        if wdata.get("places"):
            last_phase_per_wallet[wn] = "Place"
        elif wdata.get("transfers"):
            last_phase_per_wallet[wn] = "Transfer"
        elif wdata.get("withdraws"):
            last_phase_per_wallet[wn] = "Withdraw"
        else:
            # Rare case: nothing planned, already success
            _upd(wn, status="success")

    # --- NONCE BATCH RPC ---
    target_addrs = [w["address"] for w in target_wallets]
    
    # --- [SECURITY] Payload Deep Scan ---
    # Final validation before initializing the engine
    try:
        # Validate all participating signers
        for w in target_wallets:
            validate_authorized_wallet(w["address"], f"Batch Participant ({w['name']})")

        for wn, w_actions in data.items():
            if not isinstance(w_actions, dict): continue
            
            # Check Transfers
            for t in w_actions.get("transfers", []):
                validate_authorized_wallet(t.get("dest"), f"Transfer Dest ({wn})")
                if nft_addr := t.get("nft"):
                    if nft_addr.lower() != "undefined" and nft_addr != NULL_ADDRESS:
                        validate_contract(nft_addr, f"NFT Contract ({wn})")

            # Check Places
            for p in w_actions.get("places", []):
                if nft_addr := p.get("nft"):
                    if nft_addr.lower() != "undefined" and nft_addr != NULL_ADDRESS:
                        validate_contract(nft_addr, f"Place Contract ({wn})")
        
        # Verify Game Main Contract (Safety Anchor)
        validate_contract(game_main.address, "Core Game Main")
        validate_contract(game_token.address, "Core hCASH Token")

    except SecurityException as e:
        logger.critical(red_bold(f"[SECURITY] Batch aborted: {e}"))
        return {"success": False, "error": str(e), "status": "error"}

    wallet_nonces = get_batch_nonces(w3, target_addrs)

    # --- ENGINE & CONTEXT ---
    engine = PhaseEngine(w3, last_phase_map=last_phase_per_wallet)
    ctx = BatchMinersPhaser(w3, game_main, game_token, engine, gas_params, data, last_phase_per_wallet, wallet_nonces, target_wallets)

    # --- PHASE 1: WITHDRAW ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 1: WITHDRAW ---"))
    eligible_withdraw = [w for w in target_wallets if data.get(w["name"], {}).get("withdraws")]
    if eligible_withdraw:
        phase1 = Phase(
            name="Withdraw",
            action_type="Withdraw",
            items=eligible_withdraw,
            prepare_fn=ctx.withdraw_prepare,
            submit_fn=ctx.withdraw_submit,
            on_receipt_success=ctx.on_withdraw_success,
            on_receipt_error=ctx.on_withdraw_error,
            on_submit_error=ctx.withdraw_submit_error
        )
        engine.run_phase(phase1)
    else:
        logger.info(magenta_bold("No Withdraw to perform"))

    # --- PHASE 2: TRANSFER ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 2: TRANSFER ---"))
    eligible_transfer = [w for w in target_wallets if data.get(w["name"], {}).get("transfers")]
    if eligible_transfer:
        for w in eligible_transfer:
            w_name = w["name"]
            skipped = [item for item in data[w_name].get("transfers", []) if item["id"] in engine.global_failed_items]
            for item in skipped:
                logger.warning(red_bold(f"[{w_name}] NFT #{item['id']} ignored in Transfer (Previous error)."))
                _log_miner_action(w_name, item["id"], "Transfer", status="error")

        eligible_transfer_filtered = [
            w for w in eligible_transfer
            if any(item["id"] not in engine.global_failed_items for item in data[w["name"]].get("transfers", []))
        ]

        if eligible_transfer_filtered:
            phase2 = Phase(
                name="Transfer",
                action_type="Transfer",
                items=eligible_transfer_filtered,
                prepare_fn=ctx.transfer_prepare,
                submit_fn=ctx.transfer_submit,
                on_receipt_success=ctx.on_transfer_success,
                on_receipt_error=ctx.on_transfer_error,
                on_submit_error=ctx.transfer_submit_error
            )
            engine.run_phase(phase2)
    else:
        logger.info(magenta_bold("No Transfer to perform"))
        
    # --- PHASE 3: PLACE ---
    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("--- PHASE 3: PLACE ---"))
    eligible_place = [w for w in target_wallets if data.get(w["name"], {}).get("places")]
    if eligible_place:
        for w in eligible_place:
            w_name = w["name"]
            _, skipped = ctx.filter_places(data[w_name].get("places", []))
            for item in skipped:
                logger.warning(red_bold(f"[{w_name}] NFT #{item['id']} ignored in Place (Previous error)."))
                _log_miner_action(w_name, item["id"], "Place", status="error")

        eligible_place_filtered = [
            w for w in eligible_place
            if any(item["id"] not in engine.global_failed_items for item in data[w["name"]].get("places", []))
        ]

        if eligible_place_filtered:
            phase3 = Phase(
                name="Place",
                action_type="Place",
                items=eligible_place_filtered,
                prepare_fn=ctx.place_prepare,
                submit_fn=ctx.place_submit,
                on_receipt_success=ctx.on_place_success,
                on_receipt_error=ctx.on_place_error,
                on_submit_error=ctx.place_submit_error
            )
            engine.run_phase(phase3)
        else:
            logger.warning(red_bold("⚠️ Phase PLACE ignored: all eligible miners are in error."))
    else:
        logger.info(magenta_bold("No Place to perform"))

    # --- FINAL UI RECONCILIATION ---
    # Update Sidebar Badges
    for w in target_wallets:
        wn = w["name"]
        w_actions = data.get(wn, {})
        if not isinstance(w_actions, dict): continue
        
        # Identify planned items for this wallet to determine its specific status
        my_items_ids = []
        for act_key in ["withdraws", "transfers", "places"]:
            items_list = w_actions.get(act_key, [])
            for item in items_list:
                my_items_ids.append(item["id"])
            
        if not my_items_ids:
            continue # Portfolios without planned actions are already handled (skipped or success)

        failed_for_this_wallet = [m_id for m_id in my_items_ids if m_id in engine.global_failed_items]
        
        if not failed_for_this_wallet:
            _upd(wn, status="success")
        elif len(failed_for_this_wallet) < len(my_items_ids):
            # Only set a generic error if one isn't already set, usually by specific phase submit_error
            _upd(wn, status="partial")
        else:
            _upd(wn, status="error")

    # --- REWARDS SUMMARY CARD ---
    if ctx.total_claimed > 0:
        recap = f"⛏️ Total Net Claimed: <span class=\"privacy-data\">{format_decimal(ctx.total_claimed, 4)}</span> hCASH "
        recap += f"<svg-icon name=\"copy\" class=\"copy-btn\" onclick=\"copyToClipboard('{ctx.total_claimed}')\" title=\"Copy amount\"></svg-icon>"
        
        if len(ctx.per_wallet_rewards) > 1:
            recap += "<div class=\"wdc-summary-list\">"
            for w_name, amt in ctx.per_wallet_rewards.items():
                recap += f"<div class=\"wdc-summary-item\"><b>{w_name}:</b> <span class=\"privacy-data\">{format_decimal(amt, 4)}</span></div>"
            recap += "</div>"
            
        _init_generic_card(
            "recap-rewards", 
            "Rewards Summary", 
            status="success", 
            recap_html=recap,
            icon=HCASH_LOGO_URL
        )

    # Final report calculation for logs
    # Identify unique miners across all actions
    all_miner_ids = set()
    for w_actions in data.values():
        if not isinstance(w_actions, dict): continue
        for act_list in w_actions.values():
            if isinstance(act_list, list):
                for item in act_list:
                    if "id" in item: all_miner_ids.add(item["id"])
    
    total_miners = len(all_miner_ids)
    num_failed = len(engine.global_failed_items)
    num_success = total_miners - num_failed

    if num_success == total_miners and total_miners > 0:
        summary = f"✅ Operations completed successfully - {num_success} miners."
        status = "success"
    elif num_success > 0:
        summary = f"⚠️ {num_success} / {total_miners} operations successful - Partial."
        status = "partial"
    else:
        summary = f"❌ {ACTION_NAMES['batch_miners']} action failed."
        status = "error"    

    logger.info(cyan_bold(f"══════════════════════════════════════════════"))
    logger.info(cyan_bold(f"{summary}"))
    logger.info(cyan_bold(f"══════════════════════════════════════════════"))

    return {
        "success": num_success > 0, 
        "summary": summary, 
        "status": status
    }

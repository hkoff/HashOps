# main.py — HashOps Entry Point
#
# This module orchestrates the two-phase startup:
#   1. Server Launch  — Flask starts, browser opens, UI polls for readiness.
#   2. Engine Start   — Triggered by operator via the UI button. Runs the full initialization sequence in a background thread.

import sys
import threading
import webbrowser
import time

from src.config import WEB_UI_HOST, WEB_UI_PORT, CHAIN_ID, MULTICALL_ADDRESS, DEBT_THRESHOLD
from src.services.logger_setup import logger
from src.utils.helpers import green_bold, red_bold, yellow_bold, cyan_bold, magenta_bold
from src.core.wallets import load_wallets, get_rpc_url, get_burner1, log_wallet_summary
from src.core.hcash_api import get_client
from src.core.blockchain import (
    init_blockchain_from_api,
    get_contract_address, get_hcash_token_address,
    get_web3, get_game_main_contract, get_game_token_contract,
    check_connection, get_batch_wallets_miners_info,
)
from src.services.miner_cache import refresh_miner_cache_if_needed
from src.actions.ui_state import register_wallet_names
from src.web_ui.app import app, init_app_context, update_init_status, register_init_fn, set_cached_batch_data
from src.actions.ui_alerts import push_system_alert, remove_system_alert

# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def _fail(detail: str, error_message: str):
    """Log and broadcast a fatal initialization error."""
    logger.error(red_bold(f"Failed: {detail}"))
    update_init_status(step="Failed", detail=detail, failed=True, error_message=error_message)

def _discover_miners(w3, addresses, game_main, game_token, miner_types) -> dict:
    """
    Discover all miners across wallets using Multicall3.
    Updates the loader UI with incremental progress and populates the miner tray.

    Returns the batch_data dict (address → miner info).
    """
    try:
        batch_data = get_batch_wallets_miners_info(
            w3, addresses, game_main, game_token, miner_types,
            on_detail=lambda msg: update_init_status(detail=msg)
        )

        m_ingame    = 0
        m_inventory = 0
        ext_nfts    = 0
        facilities  = 0
        total       = len(batch_data)

        for wallet_idx, (addr, info) in enumerate(batch_data.items()):
            if info.get("facility") is not None:
                facilities += 1

            # Placed miners (in-game)
            placed = info.get("placed", [])
            m_ingame += len(placed)
            for m in placed:
                m_idx = str(m.get("minerIndex"))
                mt = miner_types.get(m_idx)
                if mt:
                    update_init_status(miner={"name": mt["nft_name"], "image": mt["nft_image"]})

            # Owned NFTs (inventory)
            for idx_str, tokens in info.get("owned", {}).items():
                mt = miner_types.get(idx_str, {})
                is_miner = mt.get("category") == "miner_nft"
                count = len(tokens)

                if is_miner:
                    m_inventory += count
                else:
                    ext_nfts += count

                for _ in range(count):
                    update_init_status(miner={
                        "name": mt.get("nft_name", "NFT"),
                        "image": mt.get("nft_image", ""),
                    })

            # Incremental progress (60% → 83%)
            if total > 0:
                p = 60 + int(((wallet_idx + 1) / total) * 23)
                update_init_status(percentage=p)

        # Summary
        total_assets = m_ingame + m_inventory + ext_nfts
        logger.info(green_bold(
            f"Found {total_assets} assets "
            f"({m_ingame} in-game, {m_inventory} in inventory, {ext_nfts} complementary NFTs) ✓"
        ))
        update_init_status(detail=f"Found {facilities} facilities in {total} wallets ✓",   percentage=86)
        update_init_status(detail=f"Found {m_ingame} miners in facilities ✓",              percentage=89)
        update_init_status(detail=f"Found {m_inventory} miners in inventory ✓",            percentage=92)
        update_init_status(detail=f"Found {ext_nfts} complementary NFTs ✓",                percentage=95)
        update_init_status(detail=f"{total_assets} total assets found and displayed ✓",    percentage=98)

        return batch_data

    except Exception as e:
        logger.error(red_bold(f"Discovery error: {e}"))
        update_init_status(detail="Miner discovery delayed.")
        return {}


# ─────────────────────────────────────────────────────────────────
# INITIALIZATION SEQUENCE
# ─────────────────────────────────────────────────────────────────
def initialization_sequence():
    """
    Full system initialization — triggered by the UI operator.

    Phases:
      1. Load wallets from secrets.env
      2. Connect to HashCash API and load contract registry + ABIs
      3. Establish RPC connection to Avalanche C-Chain
      4. Load/refresh miner types cache
      5. Discover all miners across wallets (Multicall3)
      6. Finalize and bundle results for the dashboard
    """
    try:
        # ── Phase 1 · Wallets ────────────────────────────────────────────────────────────────────
        logger.info(cyan_bold("[1/5] Loading wallets..."))
        update_init_status(step="Loading Wallets...", percentage=5)

        wallets = load_wallets()
        if not wallets:
            _fail("No wallets found.", "No wallets found in config.")
            return

        # Register names immediately for debt detection and UI consistency
        register_wallet_names(wallets)
        
        update_init_status(detail=f"Found {len(wallets)} wallets in config ✓")
        log_wallet_summary(wallets)

        burner1 = get_burner1(wallets)
        if not burner1:
            _fail("BURNER-1 (Main) not found.", "BURNER-1 (Main) not found. Check your secrets.env.")
            return
        update_init_status(detail="Main wallet identified ✓", percentage=10)

        # ── Phase 2 · HashCash API ─────────────────────────────────────────────────────────────────
        logger.info(cyan_bold("[2/5] Initializing HashCash API (contracts + ABIs)..."))
        update_init_status(step="Authenticating with HashCash API...", percentage=15)

        try:
            api_client = get_client()
            update_init_status(detail="HashCash API connection established ✓", percentage=17)

            registry = api_client.fetch_contracts()
            contracts = registry.get("contracts", [])
            by_cat = registry.get("by_category", {})
            update_init_status(detail=f"Registry loaded ({len(contracts)} contracts) ✓", percentage=20)

            update_init_status(step="Loading Contract ABIs...")
            if by_cat.get("game_main"):
                update_init_status(detail="hCASH Core contract indexed ✓", percentage=23)
            if by_cat.get("game_token"):
                update_init_status(detail="$hCASH Token contract indexed ✓", percentage=26)
            miner_nft_count = len(by_cat.get("miner_nft", []))
            if miner_nft_count:
                update_init_status(detail=f"{miner_nft_count} Miner NFT types indexed ✓", percentage=29)

            init_blockchain_from_api(api_client, registry)
            logger.info(green_bold("HashCash API initialized ✓"))
            update_init_status(detail="On-chain infrastructure ready ✓", percentage=32)

        except Exception as e:
            logger.error(red_bold(f"HashCash API failed: {e}"))
            update_init_status(
                step="API Error", detail=str(e),
                failed=True, error_message=f"HashCash API error: {e}"
            )
            # Push global alert even if we fail startup so user can see what happened once dashboard opens (if it opens)
            push_system_alert(
                alert_id="startup-api-failure",
                title="Startup Failure: HashCash API",
                message=f"Critical error during contract registry fetch: {e}. Check your API key and connection.",
                alert_type="error",
                section="global",
                persistent=True
            )
            return

        # ── Phase 3 · RPC Infrastructure ─────────────────────────────────────────────────────────────────
        logger.info(cyan_bold(f"[3/5] Connecting to Avalanche network (Chain ID: {CHAIN_ID})..."))
        update_init_status(step="Connecting to Avalanche...", percentage=35)

        rpc_url = get_rpc_url()
        rpc_domain = rpc_url.split("//")[-1].split("/")[0] if "//" in rpc_url else "Local/Private RPC"
        logger.info(cyan_bold(f"      · RPC        : {rpc_domain}"))
        logger.info(cyan_bold(f"      · Multicall3 : {MULTICALL_ADDRESS}"))

        w3 = get_web3()
        if not check_connection(w3):
            _fail("Unable to reach Avalanche RPC.", "Unable to reach Avalanche RPC. Check your connection or RPC URL.")
            return

        game_main  = get_game_main_contract(w3)
        game_token = get_game_token_contract(w3)
        logger.info(cyan_bold(f"      · game_main  : {get_contract_address()}"))
        logger.info(cyan_bold(f"      · game_token : {get_hcash_token_address()}"))
        logger.info(green_bold("Web3 Infrastructure ready ✓"))
        update_init_status(detail=f"Connected to Avalanche C-Chain #{CHAIN_ID} ✓", percentage=40)

        # ── Phase 4 · Miners Cache ─────────────────────────────────────────────────────────────────
        logger.info(cyan_bold("[4/5] Loading miner types cache..."))
        update_init_status(step="Syncing Miner Types...", percentage=45)

        try:
            miner_types = refresh_miner_cache_if_needed(w3, game_main, force=False)
            m_count = sum(1 for m in miner_types.values() if m.get("category") == "miner_nft")
            e_count = sum(1 for m in miner_types.values() if m.get("category") == "external_nft")
            update_init_status(detail=f"Loaded {m_count} miners + {e_count} external NFTs ✓", percentage=55)
            logger.info(green_bold(f"Miner cache ready ({m_count} miners, {e_count} external) ✓"))
            remove_system_alert("miner-cache-stale")
        except Exception as e:
            logger.error(red_bold(f"API error at startup (miners cache): {e}"))
            update_init_status(detail=f"Cache warning: {e}")
            miner_types = {}

        # ── Miner Discovery ────────────────────────────────
        update_init_status(step="Discovering Miners...", percentage=60)
        addresses = [w["address"] for w in wallets]
        batch_data = _discover_miners(w3, addresses, game_main, game_token, miner_types)
                
        # ── Phase 5 · Finalize ────────────────────────────────────────────────────────────────────
        logger.info(cyan_bold("[5/5] Finalizing startup..."))

        try:
            gas_price_gwei = round(w3.eth.gas_price / 1e9, 2)
        except Exception:
            gas_price_gwei = 0.0

        init_app_context(
            wallets=wallets, w3=w3, game_main=game_main,
            game_token=game_token, burner1_address=burner1["address"],
            miner_types=miner_types, registry=registry
        )

        # Populate debt detection cache from the data we already have
        if batch_data:
            set_cached_batch_data(batch_data)

        # Detect wallets with facility debt
        debt_wallets = [
            addr for addr, info in batch_data.items()
            if info.get("net_claimable", 0) < DEBT_THRESHOLD
        ]
        if debt_wallets:
            logger.warning(yellow_bold(f"[DEBT] ⚠ {len(debt_wallets)} wallet(s) detected with facility debt at startup."))

        results = {
            "batch_data": batch_data,
            "gas_price_gwei": gas_price_gwei,
            "timestamp": time.time(),
            "debt_wallets": debt_wallets,
        }
        update_init_status(step="Initialization completed", percentage=100, ready=True, results=results)
        logger.info(green_bold("▶ Bot initialized and ready!"))

    except Exception as e:
        logger.error(red_bold(f"Initialization fatal error: {e}"))
        update_init_status(step="Fatal Error", detail=str(e), failed=True, error_message=str(e))


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────
def main():
    """Launch the HashOps: start the web server and wait for operator confirmation."""

    logger.info(magenta_bold("══════════════════════════════════════════════"))
    logger.info(magenta_bold("   ⬡  HashOps — Avalanche C-Chain (43114)"))
    logger.info(magenta_bold("══════════════════════════════════════════════"))

    # Register initialization function (triggered by UI operator confirmation)
    register_init_fn(initialization_sequence)

    # Open browser
    url = f"http://{WEB_UI_HOST}:{WEB_UI_PORT}"
    logger.info(magenta_bold(f"▶ Interface accessible at: {url}"))
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    logger.info(yellow_bold("Use Ctrl+C to properly stop the bot"))
    logger.info(magenta_bold("──────────────────────────────────────────────"))

    # Pre-load a heartbeat message so the UI confirms connectivity on first poll
    update_init_status(step="Engine Standby", detail="Interface successfully loaded ✓")

    app.run(
        host=WEB_UI_HOST,
        port=WEB_UI_PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info(yellow_bold("\nBot stop requested by user. See you soon!"))
        sys.exit(0)

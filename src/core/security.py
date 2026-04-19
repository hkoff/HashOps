# src/core/security.py — Universal Integrity & Transaction Guard
#
# This module acts as a centralized watchdog for the bot's security.
# It enforces whitelisting of authorized wallets and official HashCash contracts.

from src.utils.helpers import green_bold
import logging
from web3 import Web3
from typing import Set, Dict, Any, Optional

from src.services.logger_setup import logger
from src.actions.ui_alerts import push_system_alert
from src.utils.helpers import red_bold, yellow_bold

# ─────────────────────────────────────────────────────────────────
# SAFETY ANCHORS (Production Constants)
# ─────────────────────────────────────────────────────────────────
# These addresses are hardcoded as a final "Ground Truth" defense.
# If the API returns different addresses for these core contracts, it indicates a potential compromise or a major protocol migration.
ANCHOR_GAME_MAIN  = "0x105fecae0c48d683da63620de1f2d1582de9e98a" 
ANCHOR_GAME_TOKEN = "0xBa5444409257967E5E50b113C395A766B0678C03"
ANCHOR_AVAX_TOKEN = "0x0000000000000000000000000000000000000000"

# ─────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────
class SecurityException(Exception):
    """Raised when a security violation is detected (Unauthorized Address/Contract)."""
    pass

# ─────────────────────────────────────────────────────────────────
# STATE (Memory Only, Strictly Internal)
# ─────────────────────────────────────────────────────────────────
_allowed_wallets: Set[str] = set()
_allowed_contracts: Set[str] = set()
_initialized = False

# ─────────────────────────────────────────────────────────────────
# INITIALIZATION & UPDATES
# ─────────────────────────────────────────────────────────────────
def initialize_security(wallets_config: list, registry: Dict[str, Any]) -> None:
    """Initializes the whitelists from local config and official API registry."""
    global _allowed_wallets, _allowed_contracts, _initialized

    # 1. Populate Authorized Wallets (from secrets.env)
    _allowed_wallets.clear()
    for w in wallets_config:
        addr = w.get("address")
        if addr:
            _allowed_wallets.add(addr.lower())

    # 2. Populate Authorized Contracts (from API Registry)
    update_authorized_contracts(registry)
    
    _initialized = True
    logger.info(yellow_bold(f"[SECURITY] Guard initialized: {len(_allowed_wallets)} wallets, {len(_allowed_contracts)} contracts whitelisted."))

def update_authorized_contracts(registry: Dict[str, Any]) -> None:
    """
    Updates the contract whitelist from an official API registry object.
    Performs safety anchor verification.
    If an anchor mismatch is detected, the whitelist UPDATE IS ABORTED to prevent any interaction.
    """
    global _allowed_contracts
    
    new_whitelist = set()
    contracts = registry.get("contracts", [])
    
    found_main = False
    found_token = False
    skipped_count = 0

    for c in contracts:
        addr_raw = c.get("address")
        c_id = c.get("id") or "unknown"
        
        if not addr_raw:
            skipped_count += 1
            logger.debug(f"[SECURITY] Skipping contract '{c_id}': No address in registry.")
            continue
        
        addr = addr_raw.lower()
        
        # Verify Safety Anchors (Case-Insensitive)
        if c_id == "game_main":
            found_main = True
            if addr != ANCHOR_GAME_MAIN.lower():
                _trigger_critical_alert("SECURITY BREACH", f"Core Game Main anchor deviation! Registry={addr}, Expected={ANCHOR_GAME_MAIN}. ABORTING WHITELIST.")
                _allowed_contracts.clear() # Emergency wipe to block all future interactions
                return
        
        if c_id == "game_token":
            found_token = True
            if addr != ANCHOR_GAME_TOKEN.lower():
                _trigger_critical_alert("SECURITY BREACH", f"Core Token anchor deviation! Registry={addr}, Expected={ANCHOR_GAME_TOKEN}. ABORTING WHITELIST.")
                _allowed_contracts.clear() # Emergency wipe to block all future interactions
                return

        new_whitelist.add(addr)

    # Final check: Core contracts MUST be present
    if not found_main or not found_token:
        _trigger_critical_alert("MISSING CORE", "Mandatory core contracts missing from API registry! ABORTING.")
        _allowed_contracts.clear()
        return

    # Only log if the whitelist content actually changed
    if new_whitelist != _allowed_contracts:
        _allowed_contracts = new_whitelist
        logger.info(yellow_bold(f"[SECURITY] Whitelist updated: {len(_allowed_contracts)} authorized contracts ({skipped_count} contracts ignored because they lack an address)."))
    else:
        logger.debug(green_bold("[SECURITY] Whitelist verification complete (no changes)."))

# ─────────────────────────────────────────────────────────────────
# VALIDATION ENGINE
# ─────────────────────────────────────────────────────────────────
def validate_authorized_wallet(address: str, context: str = "Wallet") -> None:
    """Ensures an address is one of our own whitelisted wallets (Sender or Recipient)."""
    if not address: return
    addr_low = address.lower()
    if addr_low not in _allowed_wallets:
        _trigger_critical_alert("UNAUTHORIZED ADDRESS", f"Blocked interaction with unauthorized wallet ({context}): {address}")
        raise SecurityException(f"Security Block: Unauthorized wallet address {address}")

def validate_contract(address: str, context: str = "Contract Call") -> None:
    """Ensures a contract address belongs to the official HashCash registry."""
    if not address: return
    addr_low = address.lower()
    if addr_low not in _allowed_contracts:
        _trigger_critical_alert("BLOCKED CONTRACT", f"Attempted {context} via unauthorized contract: {address}")
        raise SecurityException(f"Security Block: Unauthorized contract address {address}")

def validate_asset(address: str, expected_address: str, context: str = "Asset Transfer") -> None:
    """Strictly enforces that the asset being moved matches the expected configuration."""
    if address.lower() != expected_address.lower():
        _trigger_critical_alert("ASSET MISMATCH", f"Attempted {context} using unauthorized asset: {address} (Expected: {expected_address})")
        raise SecurityException(f"Security Block: Asset mismatch. Expected {expected_address}, got {address}")

# ─────────────────────────────────────────────────────────────────
# INTERNAL UTILS
# ─────────────────────────────────────────────────────────────────
def _trigger_critical_alert(title: str, message: str) -> None:
    """Logs a critical error and pushes a persistent UI alert banner."""
    full_msg = f"CRITICAL SECURITY: {message}"
    logger.critical(red_bold(full_msg))
    
    # Add a Footer for UI display
    ui_message = f"{message}\n\n[!] Please report this issue if it hasn't already been reported."
    
    # Push red alert banner to the UI
    push_system_alert(
        alert_id=f"security-{hash(message)}",
        title=title,
        message=ui_message,
        alert_type="error",
        section="global",
        persistent=True
    )

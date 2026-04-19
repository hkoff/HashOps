# src/core/wallets.py — Loading and managing hCASH wallets

import os
from pathlib import Path
from typing import List, Dict, Optional, Any
from dotenv import dotenv_values
from web3 import Web3

from src.services.logger_setup import logger
from src.core.signer import Signer
from src.utils.helpers import green_bold, red_bold, yellow_bold

# Determine the path to secrets.env (at the project root)
_ROOT_DIR = Path(__file__).parent.parent.parent
_ENV_PATH = _ROOT_DIR / "secrets.env"

# SECURITY HARDENING: Memory-only Configuration
# We intentionally DO NOT use os.environ (e.g. load_dotenv()) to store our secrets.
# Why? os.environ is global. Any third-party dependency, subprocess, or malware running in the same context could easily scrape all environment variables to steal priv keys.
# Instead, we load the secrets directly into a localized dictionary (_CONFIG).
# Once the Signer objects are created, local string fragments are garbage collected, leaving no trace of the actual private keys accessible anywhere in the running state.
_CONFIG: Dict[str, Optional[str]] = {}

def _ensure_config_loaded() -> None:
    """Loads configuration into memory once if it hasn't been loaded yet."""
    if not _CONFIG:
        # dotenv_values returns a dict of the parsed file WITHOUT touching os.environ
        parsed = dotenv_values(_ENV_PATH)
        for k, v in parsed.items():
            _CONFIG[k] = v

_PLACEHOLDER = "0xYourPublicAddressHere"

def _is_valid_hex(s: str, length: int) -> bool:
    """Checks if a string is a valid hexadecimal of a specific length."""
    if len(s) != length:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False

def load_wallets() -> List[Dict[str, Any]]:
    """
    Loads wallets configured in secrets.env using memory-only config.
    
    Returns:
        A list of dictionaries containing {name, address, signer, index}.
    """
    _ensure_config_loaded()
    wallets = []

    # Identify all configured burner indices by scanning the config keys
    burner_indices = set()
    for key in _CONFIG.keys():
        if key.startswith("BURNER_") and key.endswith("_ADDRESS"):
            try:
                idx = int(key.split("_")[1])
                burner_indices.add(idx)
            except ValueError:
                pass

    if not burner_indices:
        return []

    for i in sorted(list(burner_indices)):
        prefix = f"BURNER_{i}"
        name    = _CONFIG.get(f"{prefix}_NAME", f"BURNER-{i}")
        address = (_CONFIG.get(f"{prefix}_ADDRESS") or "").strip()
        key_a   = (_CONFIG.get(f"{prefix}_KEY_A") or "").strip()
        key_b   = (_CONFIG.get(f"{prefix}_KEY_B") or "").strip()

        if not address or address == _PLACEHOLDER:
            continue

        # Strict validation of private key fragments
        if not key_a or not key_b:
            raise ValueError(
                f"[CONFIG] {name} — Private key fragments (KEY_A/KEY_B) are missing in secrets.env. "
                "Please ensure you have renamed DEFAULT-secrets.env to secrets.env and filled in your keys."
            )

        if not _is_valid_hex(key_a, 32) or not _is_valid_hex(key_b, 32):
            raise ValueError(
                f"[CONFIG] {name} — Invalid private key format. Each fragment (KEY_A, KEY_B) must be exactly "
                "32 hexadecimal characters (no 0x). Please check your secrets.env file."
            )

        try:
            checksum_address = Web3.to_checksum_address(address)
        except Exception:
            raise ValueError(f"[CONFIG] {name} — Invalid Wallet address: {address}")

        try:
            signer = Signer(key_a, key_b)
        except Exception as e:
            logger.error(red_bold(f"[WALLETS] {name} — private key error: {e}"))
            continue

        wallets.append({
            "name":    name,
            "address": checksum_address,
            "signer":  signer,
            "index":   i,
        })

    return wallets

def get_rpc_url() -> str:
    """Retrieves the Avalanche RPC URL from the local memory config."""
    _ensure_config_loaded()
    url = (_CONFIG.get("RPC_URL_AVALANCHE") or "").strip()
    if not url or not url.startswith(("https://")):
        raise EnvironmentError(
            "RPC_URL_AVALANCHE is not configured or invalid in secrets.env. "
            "It must be a valid URL starting with https://. "
            "Please check your secrets.env file."
        )
    return url

def get_api_key() -> str:
    """Retrieves the HashCash API key from local memory config."""
    _ensure_config_loaded()
    return (_CONFIG.get("HCASH_API") or "").strip()

def get_burner1(wallets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Returns the main wallet (BURNER-1)."""
    for w in wallets:
        if w["index"] == 1:
            return w
    return None

def log_wallet_summary(wallets: List[Dict[str, Any]]) -> None:
    """Displays a compact summary of active wallets."""
    if not wallets:
        logger.warning(yellow_bold("[WALLETS] No wallets detected"))
        return

    logger.info(green_bold(f"[WALLETS] {len(wallets)} wallet(s) operational:"))
    for w in wallets:
        short_addr = f"{w['address'][:6]}...{w['address'][-4:]}"
        label = " [MAIN]" if w["index"] == 1 else ""
        logger.info(green_bold(f"  · {w['name']}: {short_addr}{label}"))


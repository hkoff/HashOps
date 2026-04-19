# src/config.py — Global constants for the HashOps

from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# PATHS AND FILESYSTEM
# ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# Data files
CACHE_FILE_PATH = DATA_DIR / "miner_types_cache.json"

# Log levels
DEBUG_MODE: bool = False  # Set to True to display [DEBUG] logs

# ─────────────────────────────────────────────────────────────────
# BLOCKCHAIN
# ─────────────────────────────────────────────────────────────────
CHAIN_ID: int = 43114  # Avalanche C-Chain

# $AVAX Token (ERC-20)
AVAX_TOKEN_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# Multicall3
# Universal contract deployed at the same address on all EVM-compatible chains
MULTICALL_ADDRESS: str = "0xcA11bde05977b3631167028862be2a173976ca11"

# Null address
NULL_ADDRESS: str = "0x0000000000000000000000000000000000000000"

# Timeout for HTTP calls
HTTP_TIMEOUT: int = 6

# ─────────────────────────────────────────────────────────────────
# HASHCASH API
# ─────────────────────────────────────────────────────────────────
# Local cache directory for ABIs (immutable, permanent cache)
ABI_CACHE_DIR = DATA_DIR / "abi_cache"

# ─────────────────────────────────────────────────────────────────
# THRESHOLDS & LIMITS
# ─────────────────────────────────────────────────────────────────
# Max number of items per JSON-RPC batch or Multicall
RPC_BATCH_SIZE: int = 25

# Minimum number of pendingRewards + available balance tokens to trigger a claim
CLAIM_THRESHOLD: float = 10.0

# Debt safety threshold: net_claimable below this = wallet has facility debt
DEBT_THRESHOLD: float = -0.0001

# EIP-1559 baseFee multiplier (1.05 = +5% above baseFee)
GAS_MULTIPLIER: float = 1.05

# Max security threshold (gwei): if maxFeePerGas exceeds this value, no transaction is sent and an alert log is issued
GAS_SAFETY_MAX_GWEI: float = 200.0

# Maximum wait time for a transaction receipt (seconds)
TX_RECEIPT_TIMEOUT: int = 60

# Polling interval for receipts (seconds)
TX_POLL_INTERVAL: float = 2.0

POLL_MAX_INTERVAL: float = 10.0     # Exponential backoff cap for receipt polling (seconds)
POLL_WARN_THRESHOLD: int = 30       # Elapsed time (seconds) before warning toast about slow transactions
POLL_TOAST_AFTER: int = 3           # Number of polls before starting to show toast alerts (avoids noise on fast txs)

# RESCUE SYSTEM CONSTANTS
RESCUE_TIMEOUT: int = 45            # Number of seconds to poll for a rescued/RBF transaction before giving up
GAS_BOOST_MULTIPLIER: float = 1.20  # Minimum EVM replacement multiplier (20%)

# Gas buffer on estimation (50% above estimation)
GAS_ESTIMATE_BUFFER: float = 1.50

# Default gas values (fallbacks)
DEFAULT_GAS_CLAIM: int    = 400_000
DEFAULT_GAS_TRANSFER: int = 300_000
DEFAULT_GAS_WITHDRAW: int = 300_000
DEFAULT_GAS_PLACE: int    = 600_000

# Dispatch Gas Fees action parameters
GAS_DISPATCH_MIN_BOTTOM: float = 0.04
GAS_DISPATCH_STEP: float = 0.015
GAS_DISPATCH_TOLERANCE: float = 0.005

# ─────────────────────────────────────────────────────────────────
# BLOCK EXPLORER
# ─────────────────────────────────────────────────────────────────
BLOCK_EXPLORER_URL: str = "https://snowtrace.io"
DEBANK_URL: str = "https://debank.com/profile/"

# ─────────────────────────────────────────────────────────────────
# LOCAL WEB UI
# ─────────────────────────────────────────────────────────────────
WEB_UI_HOST: str = "127.0.0.1"
WEB_UI_PORT: int = 5001

# ─────────────────────────────────────────────────────────────────
# ASSETS & ICON LOGOS
# ─────────────────────────────────────────────────────────────────
HCASH_LOGO_URL: str = "https://cdn.popularhost.net/hashcash/hcash_token.png"
AVAX_LOGO_URL: str  = "https://raw.githubusercontent.com/lifinance/types/main/src/assets/icons/chains/avalanche.svg"

# ─────────────────────────────────────────────────────────────────
# ACTION NAMES (Global Source of Truth)
# ─────────────────────────────────────────────────────────────────
ACTION_KEY_CLAIM = "claim"
ACTION_KEY_DISPATCH_GAS = "dispatch_gas"
ACTION_KEY_BATCH_MINERS = "batch_miners"

ACTION_NAMES = {
    ACTION_KEY_CLAIM: "Claim Rewards",
    ACTION_KEY_DISPATCH_GAS: "Dispatch Gas Fees",
    ACTION_KEY_BATCH_MINERS: "Manage Miners & NFTs"
}

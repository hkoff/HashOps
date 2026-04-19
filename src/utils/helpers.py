# src/utils/helpers.py — Shared utilities (Colors, Web3 Provider, API)

import time
import random
import asyncio
import aiohttp
from typing import Optional, Set, Tuple, Dict, Any

from web3.providers.rpc import HTTPProvider
from src.services.logger_setup import logger

# ─────────────────────────────────────────────────────────────────
# ANSI Colors
# ─────────────────────────────────────────────────────────────────
# RESET  : Return to normal
# BOLD   : Bold text
# GREEN  : Success / Validation / Everything is OK (Confirmations)
# RED    : Errors / Exceptions / What is NOT expected
# YELLOW : Warnings / Attention required / Cautions
# CYAN   : Headers / Major steps / Progression ( [1/5] ... )
# MAGENTA: Decorative elements / Banners / UI Interface (URLs, Ports)
# BLUE   : Specific data / Values / Addresses (within a message)

RESET   = "\033[0m"
BOLD    = "\033[1m"
RED     = "\033[91m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
CYAN    = "\033[96m"

def red_bold(text: str) -> str:     return f"{BOLD}{RED}{text}{RESET}"
def green_bold(text: str) -> str:   return f"{BOLD}{GREEN}{text}{RESET}"
def yellow_bold(text: str) -> str:  return f"{BOLD}{YELLOW}{text}{RESET}"
def blue_bold(text: str) -> str:    return f"{BOLD}{BLUE}{text}{RESET}"
def magenta_bold(text: str) -> str: return f"{BOLD}{MAGENTA}{text}{RESET}"
def cyan_bold(text: str) -> str:    return f"{BOLD}{CYAN}{text}{RESET}"

def format_decimal(value: Any, precision: int = 4) -> str:
    """Formats a number with a comma as the decimal separator."""
    try:
        if value is None: return "0," + "0"*precision
        val = float(value)
        return f"{val:.{precision}f}".replace(".", ",")
    except (ValueError, TypeError):
        return str(value)


# ─────────────────────────────────────────────────────────────────
# CustomCachingProvider — Web3 Optimization
# ─────────────────────────────────────────────────────────────────
class CustomCachingProvider(HTTPProvider):
    """
    Custom Web3 provider with caching for static methods and exponential retry mechanism.
    """

    def __init__(
        self,
        *args,
        retries: int = 1,
        retry_delay: float = 0.8,
        cache_methods: Tuple[str, ... ] = ("eth_chainId", "net_version"),
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._cache: Dict[Tuple[str, Tuple[Any, ...]], Any] = {}
        self._retries: int = int(retries)
        self._retry_delay: float = float(retry_delay)
        self._cache_methods: Set[str] = set(cache_methods)

    def make_request(self, method: str, params: Any) -> Dict[str, Any]:
        """Performs an RPC request with caching and retry. Supports batching if method is None."""
        if method and method in self._cache_methods:
            key = (method, tuple(params))
            if key in self._cache:
                return self._cache[key]

        last_exc = None
        for attempt in range(1, self._retries + 2):
            try:
                # If method is None, we are in RAW BATCH mode.
                # Web3 HTTPProvider.make_request does not support method=None.
                if method is None and isinstance(params, list):
                    import urllib.request
                    import json
                    req = urllib.request.Request(
                        self.endpoint_uri,
                        data=json.dumps(params).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=10) as response:
                        return json.loads(response.read().decode("utf-8"))

                response = super().make_request(method, params)
                if method and method in self._cache_methods:
                    self._cache[(method, tuple(params))] = response
                return response
            except Exception as e:
                last_exc = e
                m_name = method if method else "BATCH"
                logger.debug(red_bold(f"[RPC] Attempt {attempt} failed for {m_name}: {e}"))
                if attempt <= self._retries:
                    sleep_t = self._retry_delay * (1 + random.random() * 0.3)
                    time.sleep(sleep_t)
                else:
                    raise last_exc
        
        raise last_exc


# ─────────────────────────────────────────────────────────────────
# KyberSwap Price Support (Future Use)
# ─────────────────────────────────────────────────────────────────
async def get_quote_from_kyberswap(
    token_in: str,
    token_out: str,
    amount_wei: int,
    api_url: str = "https://aggregator-api.kyberswap.com/avalanche/api/v1/routes",
    timeout_seconds: int = 10,
) -> Optional[float]:
    """
    Retrieves a price estimation via KyberSwap on Avalanche.
    
    Args:
        token_in: Input token address.
        token_out: Output token address.
        amount_wei: Input amount in wei.
        api_url: API endpoint.
        timeout_seconds: Maximum waiting time.
        
    Returns:
        The output amount in tokens (float) or None if error.
    """
    url = f"{api_url}?tokenIn={token_in}&tokenOut={token_out}&amountIn={amount_wei}"

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()

                out = data.get("data", {}).get("routeSummary", {}).get("amountOut")
                if out is None:
                    return None

                try:
                    amount_out_int = int(out)
                except (ValueError, TypeError):
                    logger.warning(f"Unexpected format for amountOut: {out}")
                    return None

                return amount_out_int / 1e18

    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        logger.warning(f"KyberSwap error ({type(e).__name__}) : {token_in} -> {token_out}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during KyberSwap quote: {e}")
        return None

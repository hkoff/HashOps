# src/core/hcash_api.py — Official HashCash API Client
#
# Endpoints used:
#   GET /api/v1/public/contracts       → live contract registry
#   GET /api/v1/public/abis/{id}.json  → versioned ABIs (immutable)
#
# Authentication: x-api-key header from HCASH_API (secrets.env)
# Rate limits:
#   /contracts: 30 req/min/key+IP
#   /abis/{id}: 240 req/min/key+IP  (immutable → permanent disk cache)

import json
import time
import random
import hashlib
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

from src.config import ABI_CACHE_DIR
from src.services.logger_setup import logger
from src.core.wallets import get_api_key
from src.utils.helpers import red_bold, yellow_bold, cyan_bold, green_bold
from src.actions.ui_alerts import push_system_alert, remove_system_alert

# ─────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────
class HCashApiError(Exception):
    """Generic HashCash API error."""

class HCashApiRateLimitError(HCashApiError):
    """The API returned HTTP 429 (rate limit reached)."""
    def __init__(self, retry_after: Optional[int] = None):
        self.retry_after = retry_after
        suffix = f" — Retry-After: {retry_after}s" if retry_after else ""
        super().__init__(f"API Rate limit reached{suffix}. Please wait before retrying.")

class HCashApiIntegrityError(HCashApiError):
    """The SHA-256 of a received ABI does not match the value declared by the API."""

# ─────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────
class HCashApiClient:
    BASE_URL = "https://api.hashcash.club"
    TIMEOUT  = 15  # seconds

    def __init__(self) -> None:
        self._api_key = get_api_key()
        if not self._api_key or not self._api_key.startswith("hc_live_"):
            raise HCashApiError(
                "HCASH_API is not configured or invalid in secrets.env. "
                "The API key must start with 'hc_live_'. "
                "Please check your secrets.env file and contact HashCash support if needed."
            )
        # Memory cache: immutable ABIs, no need to re-fetch in the same session
        self._abi_memory: Dict[str, List[Dict[str, Any]]] = {}

    # ── Internal HTTP request ──────────────────────────────────────
    def _get(self, path: str, max_retries: int = 5) -> Dict[str, Any]:
        """
        Performs an authenticated GET request with exponential backoff.
        Raises:
          - HCashApiRateLimitError on persistent 429
          - HCashApiError on 401/403, repeated 5xx, or network errors
        """
        url = f"{self.BASE_URL}{path}"
        req = urllib.request.Request(url, headers={
            "x-api-key":    self._api_key,
            "User-Agent":   "hCASH-Bot/2.0",
            "Accept":       "application/json",
        })

        attempt = 0
        base_delay = 1.5

        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                    # Clear any existing rate limit alert on success
                    remove_system_alert("hcapi-ratelimit")
                    # Log rate-limit headers for traceability
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    limit     = resp.headers.get("X-RateLimit-Limit")
                    req_id    = resp.headers.get("X-Request-Id", "")
                    if remaining is not None:
                        logger.debug(
                            cyan_bold(f"[HC-API] {path} — "
                                      f"RateLimit: {remaining}/{limit} (reqId: {req_id})")
                        )
                    body = resp.read().decode("utf-8")
                    return json.loads(body)

            except urllib.error.HTTPError as exc:
                # 429: Rate Limit
                if exc.code == 429:
                    attempt += 1
                    retry_after_raw = exc.headers.get("Retry-After")
                    reset_raw       = exc.headers.get("X-RateLimit-Reset")
                    
                    # Determine wait time: Retry-After (seconds) > Reset (timestamp) > Default exponential
                    wait = 0
                    if retry_after_raw:
                        wait = int(retry_after_raw)
                    elif reset_raw:
                        try:
                            # Usually a Unix timestamp
                            wait = max(1, int(reset_raw) - int(time.time()))
                        except: pass

                    if not wait:
                        wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    
                    # Push alert to UI
                    push_system_alert(
                        alert_id="hcapi-ratelimit",
                        title="HashCash API Rate Limited",
                        message=f"The official API is limiting our requests. Waiting {wait:.1f}s before retrying. Persistent limits might delay data updates.",
                        alert_type="warning",
                        section="global",
                        persistent=False
                    )

                    if attempt > max_retries:
                        raise HCashApiRateLimitError(None) from exc
                    
                    logger.warning(yellow_bold(
                        f"[HC-API] 429 Rate Limit on {path}. Waiting {wait:.1f}s... (attempt {attempt}/{max_retries})"
                    ))
                    time.sleep(wait)
                    continue

                # Server Errors (Temporary)
                if exc.code >= 500:
                    attempt += 1
                    if attempt > max_retries:
                        raise HCashApiError(f"HTTP {exc.code} on {path} after {max_retries} retries: {exc.reason}") from exc
                    
                    wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(yellow_bold(
                        f"[HC-API] HTTP {exc.code} on {path}. Retrying in {wait:.1f}s... (attempt {attempt}/{max_retries})"
                    ))
                    time.sleep(wait)
                    continue

                # Read body for specific API error messages (Auth, etc.)
                try:
                    error_body = exc.read().decode("utf-8")
                    error_data = json.loads(error_body)
                    api_msg    = error_data.get("message")
                    api_code   = error_data.get("errorCode")
                    detail = f" — {api_code}: {api_msg}" if api_msg else f" — {exc.reason}"
                except:
                    detail = f" — {exc.reason}"

                if exc.code == 401:
                    raise HCashApiError(f"Invalid API key (HTTP 401){detail}") from exc
                if exc.code == 403:
                    raise HCashApiError(f"API key restricted (HTTP 403){detail}") from exc
                
                raise HCashApiError(f"HTTP {exc.code} on {path}{detail}") from exc

            except urllib.error.URLError as exc:
                # Network errors (DNS, Timeout, connection refused)
                attempt += 1
                if attempt > max_retries:
                    raise HCashApiError(f"Network error on {path} after {max_retries} retries: {exc.reason}") from exc
                
                wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                logger.warning(yellow_bold(
                    f"[HC-API] Network error on {path}: {exc.reason}. Retrying in {wait:.1f}s... (attempt {attempt}/{max_retries})"
                ))
                time.sleep(wait)
                continue

            except Exception as exc:
                raise HCashApiError(f"Unexpected error on {path}: {exc}") from exc

    # ── Contracts Registry ─────────────────────────────────────
    def fetch_contracts(self) -> Dict[str, Any]:
        """
        Retrieves the live registry of HashCash contracts.
        Rate limit: 30 req/min → to be called only at boot and on force-refresh.

        Returns a dict:
          {
            "contracts":   [contract_record, ...],
            "by_id":       {"game_main": record, "miner_nft:1": record, ...},
            "by_category": {"game_main": [...], "miner_nft": [...], ...},
          }
        """
        logger.info(cyan_bold("[HC-API] Fetching /contracts..."))
        data = self._get("/api/v1/public/contracts")
        contracts = data.get("contracts", [])

        by_id: Dict[str, Any] = {}
        by_category: Dict[str, List[Any]] = {}
        for c in contracts:
            if cid := c.get("id"):
                by_id[cid] = c
            if cat := c.get("category"):
                by_category.setdefault(cat, []).append(c)

        # API Warnings (e.g., next release)
        for w in data.get("meta", {}).get("warnings", []):
            logger.warning(yellow_bold(f"[HC-API] ⚠ API Warning: {w}"))

        next_release = data.get("meta", {}).get("nextReleaseAt")
        if next_release:
            logger.info(cyan_bold(f"[HC-API] Next scheduled release: {next_release}"))

        categories_summary = ", ".join(f"{k}×{len(v)}" for k, v in by_category.items())
        logger.info(green_bold(
            f"[HC-API] {len(contracts)} contracts retrieved — {categories_summary}"
        ))
        return {"contracts": contracts, "by_id": by_id, "by_category": by_category}

    # ── ABI ───────────────────────────────────────────────────────
    def fetch_abi(self, abi_id: str, expected_sha: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves a versioned ABI by its ID.
        Rate limit: 240 req/min → safe at boot (≤9 ABIs max).
        ABIs are immutable: permanent disk cache + memory cache.

        Caching strategy:
          1. Memory (in-process)
          2. Disk   (data/abi_cache/{abi_id}.json)
          3. API    (re-fetch if missing or invalid SHA-256)

        Raises HCashApiIntegrityError if the SHA-256 does not match.
        """
        # 1. Memory cache
        if abi_id in self._abi_memory:
            logger.debug(green_bold(f"[HC-API] ABI {abi_id} — memory hit"))
            return self._abi_memory[abi_id]

        # 2. Disk cache
        cache_file = ABI_CACHE_DIR / f"{abi_id}.json"
        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as f:
                    cached = json.load(f)
                stored_sha = cached.get("sha256", "")
                abi = cached.get("abi", [])
                # Disk file integrity check
                computed = _sha256_abi(abi)
                if computed == stored_sha:
                    logger.debug(green_bold(f"[HC-API] ABI {abi_id} — disk hit (SHA ✓)"))
                    self._abi_memory[abi_id] = abi
                    return abi
                else:
                    logger.warning(yellow_bold(
                        f"[HC-API] ABI {abi_id} — invalid disk SHA "
                        f"(expected: {stored_sha[:12]}..., computed: {computed[:12]}...) → re-fetch"
                    ))
            except Exception as exc:
                logger.warning(yellow_bold(
                    f"[HC-API] ABI {abi_id} — disk cache read error: {exc} → re-fetch"
                ))

        # 3. API fetch
        logger.info(cyan_bold(f"[HC-API] Fetching ABI {abi_id}..."))
        payload = self._get(f"/api/v1/public/abis/{abi_id}.json")
        abi      = payload.get("abi", [])
        api_sha  = payload.get("meta", {}).get("abiSha256", "")

        if not abi:
            raise HCashApiError(f"Empty ABI {abi_id} in API response")

        # Integrity check (SHA-256 of compact json.dumps = JSON.stringify)
        my_sha = _sha256_abi(abi)
        target_sha = expected_sha or api_sha
        
        if target_sha and my_sha != target_sha:
            raise HCashApiIntegrityError(
                f"ABI {abi_id} — invalid SHA-256: "
                f"Expected={target_sha[:16]}... computed={my_sha[:16]}... "
                f"(JSON serialization inconsistency possible)"
            )

        # Write disk cache
        ABI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as f:
            json.dump({"sha256": my_sha, "abi": abi}, f, indent=2, ensure_ascii=False)

        logger.info(green_bold(
            f"[HC-API] ABI {abi_id} — {len(abi)} fragments, "
            f"SHA {my_sha[:12]}... — cached ✓"
        ))
        self._abi_memory[abi_id] = abi
        return abi


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────
def _sha256_abi(abi: List[Dict[str, Any]]) -> str:
    """
    Calculates the SHA-256 of an ABI following the JSON.stringify convention:
    compact, no spaces, ensure_ascii=False (native Unicode).
    """
    serialized = json.dumps(abi, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────
_client: Optional[HCashApiClient] = None

def get_client() -> HCashApiClient:
    """Returns the singleton API client (initialized once)."""
    global _client
    if _client is None:
        _client = HCashApiClient()
    return _client


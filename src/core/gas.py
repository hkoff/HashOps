# src/core/gas.py — Avalanche transaction fee management (EIP-1559)

from typing import Dict, Optional
from web3 import Web3

from src.config import GAS_MULTIPLIER, GAS_SAFETY_MAX_GWEI
from src.services.logger_setup import logger
from src.utils.helpers import red_bold, yellow_bold, format_decimal
from src.actions.ui_alerts import push_system_alert, remove_system_alert

def get_eip1559_gas_params(w3: Web3) -> Optional[Dict[str, int]]:
    """
    Calculates maxFeePerGas and maxPriorityFeePerGas for the C-Chain network.
    Args:
        w3: Active Web3 instance.
    Returns:
        A dictionary with gas parameters or None if fees are prohibitive.
    """
    try:
        block = w3.eth.get_block("latest")

        if "baseFeePerGas" not in block or block["baseFeePerGas"] is None:
            logger.error(red_bold("[GAS] Error: baseFeePerGas missing from the latest block."))
            return None

        base_fee: int = block["baseFeePerGas"]

        # Retrieves the tip (priority fee) suggested by the node
        try:
            max_priority_fee: int = w3.eth.max_priority_fee
        except Exception:
            # Fallback to 1 kwei if eth_maxPriorityFee is not supported
            max_priority_fee = w3.to_wei(1, "kwei")

        # Calculation: baseFee * multiplier + priorityFee
        max_fee: int = int(base_fee * GAS_MULTIPLIER) + max_priority_fee

        # Security limit check (Gwei)
        max_fee_gwei: float  = float(w3.from_wei(max_fee, "gwei"))
        base_fee_gwei: float = float(w3.from_wei(base_fee, "gwei"))
        tip_gwei: float      = float(w3.from_wei(max_priority_fee, "gwei"))

        if max_fee_gwei > GAS_SAFETY_MAX_GWEI:
            logger.error(
                red_bold(
                    f"[GAS] SECURITY: Fees too high ({format_decimal(max_fee_gwei, 2)} Gwei > {format_decimal(GAS_SAFETY_MAX_GWEI, 2)} Gwei). "
                    "Transaction suspended."
                )
            )
            # Push global alert
            push_system_alert(
                alert_id="gas-safety-limit",
                title="High Network Fees — Transactions Paused",
                message=f"Current gas fees ({format_decimal(max_fee_gwei, 1)} Gwei) exceed your safety limit ({format_decimal(GAS_SAFETY_MAX_GWEI, 1)} Gwei). "
                        f"The bot is waiting for fees to drop. (Base: {format_decimal(base_fee_gwei, 1)} | Tip: {format_decimal(tip_gwei, 2)})",
                alert_type="warning",
                section="global",
                persistent=True
            )
            return None
        else:
            # Clear alert if it was active
            remove_system_alert("gas-safety-limit")

        logger.debug(
            yellow_bold(
                f"[GAS] Base: {format_decimal(base_fee_gwei, 2)} | Max: {format_decimal(max_fee_gwei, 2)} | Tip: {format_decimal(tip_gwei, 6)} Gwei"
            )
        )

        return {
            "maxFeePerGas":         max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
        }

    except Exception as e:
        logger.error(red_bold(f"[GAS] Fee calculation failed: {e}"))
        return None


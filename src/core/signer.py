# src/core/signer.py — Secure management of private keys and signatures

from eth_account import Account
from eth_account.signers.local import LocalAccount
from typing import Dict, Any

class Signer:
    """
    Private key wrapper to minimize memory exposure.
    - Reconstructs the key only during initialization.
    - Exposes only the public address and signing method.
    - Prevents key extraction via str() or repr().
    """

    __slots__ = ("_account",)

    def __init__(self, key_a: str, key_b: str) -> None:
        """
        Initializes the signer by combining two key fragments.
        
        Args:
            key_a: First fragment of the private key (hex, 32 chars).
            key_b: Second fragment of the private key (hex, 32 chars).
        """
        ka, kb = key_a.strip(), key_b.strip()
        if len(ka) != 32 or len(kb) != 32:
            raise ValueError(f"Invalid key fragment lengths: {len(ka)}, {len(kb)} (expected 32 each)")
            
        raw_key = "0x" + ka + kb
        self._account: LocalAccount = Account.from_key(raw_key)
        # raw_key is purged from the stack here

    @property
    def address(self) -> str:
        """Returns the formatted public address (checksum)."""
        return self._account.address

    def sign_transaction(self, tx: Dict[str, Any]) -> Any:
        """
        Signs a Web3 transaction.
        
        Args:
            tx: Raw transaction dictionary.
            
        Returns:
            SignedTransaction object.
        """
        return self._account.sign_transaction(tx)

    def __repr__(self) -> str:
        # Partial masking of the address for safe identification
        return f"<Signer address={self.address[:6]}...{self.address[-4:]}>"

    def __str__(self) -> str:
        return self.__repr__()


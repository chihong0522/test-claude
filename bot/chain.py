"""Base chain interaction layer — web3 helpers."""

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

from bot.config import Config


class Chain:
    """Thin wrapper around web3.py for Base chain."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.w3 = Web3(Web3.HTTPProvider(cfg.base_rpc_url))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.account = Account.from_key(cfg.private_key)
        assert self.w3.is_connected(), "Cannot connect to Base RPC"

    @property
    def address(self) -> str:
        return self.account.address

    def nonce(self) -> int:
        return self.w3.eth.get_transaction_count(self.address)

    def send_tx(self, tx: dict) -> str:
        """Sign and broadcast a transaction. Returns tx hash hex."""
        tx["from"] = self.address
        tx["nonce"] = self.nonce()
        tx["chainId"] = 8453  # Base mainnet
        if "gas" not in tx:
            tx["gas"] = self.cfg.gas_limit
        if "maxFeePerGas" not in tx:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
            tx["maxFeePerGas"] = base_fee * 2
            tx["maxPriorityFeePerGas"] = self.w3.to_wei(0.001, "gwei")

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def wait_receipt(self, tx_hash: str, timeout: int = 120):
        return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)

    def get_contract(self, address: str, abi: list):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=abi
        )

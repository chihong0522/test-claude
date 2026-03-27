"""Limitless protocol interaction — split, merge, order placement.

NOTE: Contract addresses and ABIs are PLACEHOLDERS.
You must fill them in after confirming the actual deployed contracts on Base.
"""

from web3 import Web3

from bot.chain import Chain
from bot.config import Config

# ─── Placeholder ABIs (minimal — extend after reading verified contract) ───

# ConditionalTokenFramework (CTF) — ERC-1155 for YES/NO outcome tokens
CTF_ABI = [
    {
        "name": "splitPosition",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "mergePositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# Orderbook — place / cancel limit orders
ORDERBOOK_ABI = [
    {
        "name": "placeOrder",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "amount", "type": "uint256"},
            {"name": "price", "type": "uint256"},
            {"name": "side", "type": "uint8"},  # 0=BID, 1=ASK
        ],
        "outputs": [{"name": "orderId", "type": "uint256"}],
    },
    {
        "name": "cancelOrder",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "orderId", "type": "uint256"}],
        "outputs": [],
    },
]

# ERC-20 approve
ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class LimitlessClient:
    """High-level interface to Limitless on-chain operations."""

    SIDE_BID = 0
    SIDE_ASK = 1

    def __init__(self, chain: Chain, cfg: Config):
        self.chain = chain
        self.cfg = cfg
        self.usdc = chain.get_contract(cfg.usdc_address, ERC20_ABI)

        # These will be None until addresses are configured
        self.ctf = (
            chain.get_contract(cfg.ctf_address, CTF_ABI)
            if cfg.ctf_address
            else None
        )
        self.orderbook = (
            chain.get_contract(cfg.orderbook_address, ORDERBOOK_ABI)
            if cfg.orderbook_address
            else None
        )

    # ── Approval ──────────────────────────────────────────────────────────

    def ensure_usdc_approval(self, spender: str, amount: int):
        """Approve USDC spending if allowance is insufficient."""
        current = self.usdc.functions.allowance(
            self.chain.address, Web3.to_checksum_address(spender)
        ).call()
        if current < amount:
            tx = self.usdc.functions.approve(
                Web3.to_checksum_address(spender),
                2**256 - 1,  # max approval
            ).build_transaction({})
            tx_hash = self.chain.send_tx(tx)
            self.chain.wait_receipt(tx_hash)
            return tx_hash
        return None

    # ── Split ─────────────────────────────────────────────────────────────

    def split(self, condition_id: bytes, amount: int) -> str:
        """
        Split `amount` USDC into YES + NO outcome tokens.

        1 USDC → 1 YES + 1 NO (for a binary market).
        """
        assert self.ctf, "CTF contract address not configured"

        # Approve CTF to spend USDC
        self.ensure_usdc_approval(self.cfg.ctf_address, amount)

        # partition = [0b01, 0b10] for binary YES/NO
        partition = [1, 2]
        parent = b"\x00" * 32

        tx = self.ctf.functions.splitPosition(
            Web3.to_checksum_address(self.cfg.usdc_address),
            parent,
            condition_id,
            partition,
            amount,
        ).build_transaction({})

        tx_hash = self.chain.send_tx(tx)
        receipt = self.chain.wait_receipt(tx_hash)
        assert receipt["status"] == 1, f"Split tx failed: {tx_hash}"
        return tx_hash

    # ── Merge ─────────────────────────────────────────────────────────────

    def merge(self, condition_id: bytes, amount: int) -> str:
        """Merge YES + NO back into USDC."""
        assert self.ctf, "CTF contract address not configured"

        partition = [1, 2]
        parent = b"\x00" * 32

        tx = self.ctf.functions.mergePositions(
            Web3.to_checksum_address(self.cfg.usdc_address),
            parent,
            condition_id,
            partition,
            amount,
        ).build_transaction({})

        tx_hash = self.chain.send_tx(tx)
        receipt = self.chain.wait_receipt(tx_hash)
        assert receipt["status"] == 1, f"Merge tx failed: {tx_hash}"
        return tx_hash

    # ── Place Ask (Sell outcome token) ────────────────────────────────────

    def place_ask(self, token_id: int, amount: int, price: int) -> str:
        """
        Place an ASK (sell) limit order on the orderbook.

        price: in basis points or token units (depends on contract spec).
        """
        assert self.orderbook, "Orderbook contract address not configured"

        tx = self.orderbook.functions.placeOrder(
            token_id, amount, price, self.SIDE_ASK
        ).build_transaction({})

        tx_hash = self.chain.send_tx(tx)
        receipt = self.chain.wait_receipt(tx_hash)
        assert receipt["status"] == 1, f"Ask order tx failed: {tx_hash}"
        return tx_hash

    def place_bid(self, token_id: int, amount: int, price: int) -> str:
        """Place a BID (buy) limit order."""
        assert self.orderbook, "Orderbook contract address not configured"

        tx = self.orderbook.functions.placeOrder(
            token_id, amount, price, self.SIDE_BID
        ).build_transaction({})

        tx_hash = self.chain.send_tx(tx)
        receipt = self.chain.wait_receipt(tx_hash)
        assert receipt["status"] == 1, f"Bid order tx failed: {tx_hash}"
        return tx_hash

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: int) -> str:
        assert self.orderbook, "Orderbook contract address not configured"

        tx = self.orderbook.functions.cancelOrder(
            order_id
        ).build_transaction({})

        tx_hash = self.chain.send_tx(tx)
        self.chain.wait_receipt(tx_hash)
        return tx_hash

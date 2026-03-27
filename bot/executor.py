"""Step 4 — Executor: split USDC + place dual ask orders.

Flow per trade decision:
  1. Split N USDC → N YES + N NO tokens (on-chain via CTF contract)
  2. Place ASK on YES book @ yes_ask_price
  3. Place ASK on NO book @ no_ask_price
  4. Return order IDs for monitoring

Uses the limitless-sdk OrderClient for order placement (handles EIP-712
signing internally) and web3.py for the on-chain split operation.
"""

import logging
import time
from dataclasses import dataclass

from eth_account import Account
from limitless_sdk.api import HttpClient
from limitless_sdk.markets import MarketFetcher
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from bot.config import Config
from bot.limitless import CTF_ADDRESS, USDC_ADDRESS
from bot.strategy import TradeDecision

log = logging.getLogger(__name__)

# ── Minimal ABIs for split/merge ──────────────────────────────────────

ERC20_APPROVE_ABI = [{
    "name": "approve",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "outputs": [{"name": "", "type": "bool"}],
}]

CTF_SPLIT_ABI = [{
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
}]


@dataclass
class ExecutionResult:
    slug: str
    split_tx: str | None
    yes_order_id: str | None
    no_order_id: str | None
    success: bool
    error: str | None = None


class Executor:
    """Handles on-chain split and off-chain order placement."""

    CHAIN_ID = 8453  # Base mainnet
    BASE_RPC = "https://mainnet.base.org"

    def __init__(self, cfg: Config, http_client: HttpClient, fetcher: MarketFetcher):
        self.cfg = cfg
        self.http_client = http_client
        self.fetcher = fetcher
        self.account = Account.from_key(cfg.private_key)

        # web3 for on-chain split
        self.w3 = Web3(Web3.HTTPProvider(self.BASE_RPC))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_APPROVE_ABI,
        )
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_SPLIT_ABI,
        )

    # ── On-chain: Split ───────────────────────────────────────────────

    def _send_tx(self, tx: dict) -> str:
        tx["from"] = self.account.address
        tx["nonce"] = self.w3.eth.get_transaction_count(self.account.address)
        tx["chainId"] = self.CHAIN_ID
        if "gas" not in tx:
            tx["gas"] = 500_000
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        tx["maxFeePerGas"] = base_fee * 2
        tx["maxPriorityFeePerGas"] = self.w3.to_wei(0.001, "gwei")

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def split_usdc(self, condition_id: bytes, amount_usdc: float) -> str:
        """
        Split USDC into YES + NO tokens on-chain.

        amount_usdc: human-readable (e.g. 10.0 = $10)
        Returns tx hash.
        """
        amount_raw = int(amount_usdc * 1e6)  # USDC has 6 decimals

        # Approve CTF to spend USDC
        approve_tx = self.usdc.functions.approve(
            Web3.to_checksum_address(CTF_ADDRESS),
            amount_raw,
        ).build_transaction({})
        approve_hash = self._send_tx(approve_tx)
        self.w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
        log.info("USDC approval tx: %s", approve_hash)

        # Split
        split_tx = self.ctf.functions.splitPosition(
            Web3.to_checksum_address(USDC_ADDRESS),
            b"\x00" * 32,      # parentCollectionId
            condition_id,
            [1, 2],            # binary partition: YES=0b01, NO=0b10
            amount_raw,
        ).build_transaction({})
        split_hash = self._send_tx(split_tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(split_hash, timeout=120)
        assert receipt["status"] == 1, f"Split failed: {split_hash}"
        log.info("Split tx: %s ($%.2f)", split_hash, amount_usdc)
        return split_hash

    # ── Off-chain: Place orders via API ────────────────────────────────

    async def _place_ask_order(
        self,
        slug: str,
        token_id: str,
        price: float,
        size: float,
        exchange_address: str,
    ) -> str | None:
        """
        Place a SELL (ask) limit order via Limitless API.
        Returns order ID on success, None on failure.
        """
        salt = int(time.time() * 1000)
        maker_amount = int(size * 1e6)              # shares to sell
        taker_amount = int(size * price * 1e6)       # USDC to receive

        order_data = {
            "salt": salt,
            "maker": Web3.to_checksum_address(self.account.address),
            "signer": Web3.to_checksum_address(self.account.address),
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": 0,
            "nonce": 0,
            "feeRateBps": 0,
            "side": 1,  # SELL
            "signatureType": 0,  # EOA
        }

        signature = self._sign_order(order_data, exchange_address)
        order_data["signature"] = signature

        payload = {
            "order": order_data,
            "ownerId": 0,  # Will be resolved by API from API key
            "orderType": "GTC",
            "marketSlug": slug,
        }

        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.cfg.api_base}/orders",
                json=payload,
                headers={
                    "X-API-Key": self.cfg.api_key,
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    order_id = data.get("order", {}).get("id", "unknown")
                    log.info(
                        "[%s] ASK placed: token=%s price=%.3f size=%.1f id=%s",
                        slug, token_id[:12], price, size, order_id,
                    )
                    return order_id
                else:
                    text = await resp.text()
                    log.error("[%s] ASK failed (%d): %s", slug, resp.status, text)
                    return None

    def _sign_order(self, order_data: dict, exchange_address: str) -> str:
        """EIP-712 sign an order."""
        from eth_account.messages import encode_typed_data

        domain = {
            "name": "Limitless CTF Exchange",
            "version": "1",
            "chainId": self.CHAIN_ID,
            "verifyingContract": Web3.to_checksum_address(exchange_address),
        }
        types = {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        }
        msg = {
            "salt": order_data["salt"],
            "maker": Web3.to_checksum_address(order_data["maker"]),
            "signer": Web3.to_checksum_address(order_data["signer"]),
            "taker": Web3.to_checksum_address(order_data["taker"]),
            "tokenId": int(order_data["tokenId"]),
            "makerAmount": order_data["makerAmount"],
            "takerAmount": order_data["takerAmount"],
            "expiration": order_data["expiration"],
            "nonce": order_data["nonce"],
            "feeRateBps": order_data["feeRateBps"],
            "side": order_data["side"],
            "signatureType": order_data["signatureType"],
        }
        encoded = encode_typed_data({
            "types": types,
            "primaryType": "Order",
            "domain": domain,
            "message": msg,
        })
        signed = Account.sign_message(encoded, private_key=self.cfg.private_key)
        return signed.signature.hex()

    # ── Cancel order via API ──────────────────────────────────────────

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID. Returns True on success."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{self.cfg.api_base}/orders/{order_id}",
                headers={"X-API-Key": self.cfg.api_key},
            ) as resp:
                if resp.status == 200:
                    log.info("Cancelled order %s", order_id)
                    return True
                else:
                    text = await resp.text()
                    log.error("Cancel failed for %s (%d): %s", order_id, resp.status, text)
                    return False

    async def cancel_and_reprice(
        self,
        slug: str,
        old_yes_order_id: str | None,
        old_no_order_id: str | None,
        yes_token: str,
        no_token: str,
        new_yes_price: float,
        new_no_price: float,
        size: float,
        exchange_address: str,
    ) -> tuple[str | None, str | None]:
        """
        Cancel existing orders and place new ones at updated prices.
        Returns (new_yes_order_id, new_no_order_id).
        """
        # Cancel old orders
        if old_yes_order_id:
            await self.cancel_order(old_yes_order_id)
        if old_no_order_id:
            await self.cancel_order(old_no_order_id)

        # Place new orders at updated prices
        new_yes = await self._place_ask_order(
            slug=slug, token_id=yes_token,
            price=new_yes_price, size=size,
            exchange_address=exchange_address,
        )
        new_no = await self._place_ask_order(
            slug=slug, token_id=no_token,
            price=new_no_price, size=size,
            exchange_address=exchange_address,
        )

        log.info(
            "[%s] REPRICED: YES=%.3f (id=%s)  NO=%.3f (id=%s)",
            slug, new_yes_price, new_yes, new_no_price, new_no,
        )
        return new_yes, new_no

    # ── Execute full flow ─────────────────────────────────────────────

    async def execute(self, decision: TradeDecision) -> ExecutionResult:
        """
        Execute a single trade decision:
          1. Split USDC on-chain
          2. Place YES ask
          3. Place NO ask
        """
        opp = decision.opportunity
        slug = opp.slug

        log.info("[%s] Executing: split $%.2f → dual ask", slug, decision.split_amount)

        # TODO: condition_id needs to come from market data.
        # For now this is a placeholder — you'll need to extract it from
        # the market's on-chain state or API response.
        # split_tx = self.split_usdc(condition_id, decision.split_amount)
        split_tx = None
        log.warning(
            "[%s] Split skipped — condition_id not yet implemented. "
            "Pre-split tokens manually or implement condition_id lookup.",
            slug,
        )

        # Place dual asks
        yes_order = await self._place_ask_order(
            slug=slug,
            token_id=opp.yes_token,
            price=opp.yes_ask_price,
            size=opp.yes_ask_size,
            exchange_address=opp.exchange_address,
        )
        no_order = await self._place_ask_order(
            slug=slug,
            token_id=opp.no_token,
            price=opp.no_ask_price,
            size=opp.no_ask_size,
            exchange_address=opp.exchange_address,
        )

        success = yes_order is not None and no_order is not None

        return ExecutionResult(
            slug=slug,
            split_tx=split_tx,
            yes_order_id=yes_order,
            no_order_id=no_order,
            success=success,
            error=None if success else "One or both orders failed",
        )

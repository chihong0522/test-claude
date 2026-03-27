"""Base chain constants and helpers.

Contract addresses for Limitless on Base mainnet.
"""

# Base mainnet chain ID
CHAIN_ID = 8453

# ── Token contracts ────────────────────────────────────────────────────
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CTF = "0xC9c98965297Bc527861c898329Ee280632B76e18"   # ERC-1155 conditional tokens

# ── Limitless exchange contracts (all 3 versions) ──────────────────────
EXCHANGES = {
    "v1": {
        "exchange": "0xa4409D988CA2218d956BeEFD3874100F444f0DC3",
        "fee_module": "0x6d8A7D1898306CA129a74c296d14e55e20aaE87D",
    },
    "v2": {
        "exchange": "0xF1De958F8641448A5ba78c01f434085385Af096D",
        "fee_module": "0xEECD2Cf0FF29D712648fC328be4EE02FC7931c7A",
    },
    "v3": {
        "exchange": "0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5",
        "fee_module": "0x5130c2c398F930c4f43B15635410047cBEa9D6EB",
    },
}

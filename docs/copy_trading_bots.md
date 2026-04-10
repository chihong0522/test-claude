# Copy Trading Bot Research — Polymarket 2026

Research conducted April 2026 for choosing the right copy-trading bot for
BTC 5-minute markets with $1-5k capital.

## ⚠️ Security warnings (read first)

1. **Polycule** — Hacked January 13, 2026, ~$230K stolen. Despite claiming
   non-custodial, it was server-side custody. Withdrawals halted. Suspected
   exit scam. **DO NOT USE.**

2. **Active malware campaign** — The `dev-protocol` GitHub org was hijacked
   and is distributing typosquatted "polymarket-copy-trading-bot" npm packages
   that steal private keys and open SSH backdoors. **Do not run random GitHub
   repos with this name.**

3. **Phishing domains** — `polymarket-trading-bot.com` flagged as phishing.
   Multiple clone Telegram bots impersonate legitimate ones.

4. **"Non-custodial" is often marketing, not architecture** on Telegram bots.
   Polycule made this claim and turned out to be lying. Any Telegram bot
   where you interact with balances in chat is running a hot wallet derived
   from a seed the service has touched at some point.

## Comparison table

| Tool | UI | Custody | Latency | Fees | Wallets | 5-min BTC? | Recommended? |
|------|----|---------|---------|------|---------|------------|--------------|
| **Build your own** (py-clob-client + VPS) | Self-hosted | True non-custodial | <200ms achievable | Polymarket's 2% only | Unlimited | **Yes if built right** | **1st choice (technical)** |
| **MirrorCopy** | Web/PWA/Android | Safe-integrated trade-only approval | 50-300ms WS, <2s exec | $29-$149/mo flat | 1/10/unlimited | Technically capable | **1st choice (non-technical)** |
| **PolyCop** | Telegram | Non-custodial claim (hot wallet) | 0 blocks 30% / 1 block 70% | 0.5%/trade | Unlimited | Not confirmed (docs cite 15-min) | 2nd choice |
| **PolyGun** (+ Polymarket Analytics) | Telegram | Non-custodial claim (hot wallet) | Sub-second claim | 1%/trade | Not disclosed | Not mentioned | 3rd choice |
| **PolyFollow** | Web | Smart-wallet (ERC-4337, strong) | Not quantified | 1-5% profit share | Unlimited | Likely too slow for 5-min | Good for long-form markets |
| **Polycule** | — | Claimed non-custodial, was custodial | — | — | — | — | **❌ AVOID — suspected rug** |
| **PolyDex** | Telegram | Server-held encrypted (opaque) | Unknown | $299/mo | Curated only | Unknown | ❌ Bad value |
| **PolyCopy.dev** | Web | Hot wallet, AES-encrypted | <500ms claim | 0.05%/trade | Unknown | **Explicitly NO** | ❌ |
| Random GitHub repos | — | — | — | — | — | — | **❌ Malware suspect** |

## Recommended: Build your own (for technical users)

- Use `Polymarket/py-clob-client` as the low-level SDK
- Reference implementation: QuickNode TypeScript guide
- Host on a VPS in AWS us-east-1 (close to most Polygon RPC providers)
- Hold EOA in MetaMask or hardware wallet
- Cost: ~$10/mo VPS + your time
- Fees: only Polymarket's native 2% taker fee

## Recommended: MirrorCopy (for non-technical users)

- Tier: $79/mo Growth (10 wallets, matches your 5-10 copy target range)
- URL: https://www.mirrorcopy.com
- Custody: Uses Polymarket's Safe/proxy wallet with **trade-only** scoped
  approvals — enforced on-chain, not by vendor promise. The service can
  place trades on your behalf but **cannot withdraw funds**.
- Latency: published 50-300ms WebSocket + <2s end-to-end
- Flat fee is friendlier than % fees for high-frequency scalping

## Hard truth about 5-minute markets

**No surveyed tool explicitly supports 5-minute markets.** All docs cite
15-minute markets. Assume you're beta-testing 5-min support on any tool
you choose.

Worse: 5-minute markets are dominated by professional market makers with
co-located infra and direct oracle-lag arbitrage. Copy-trading them means:

- You're always 1-3 blocks behind
- Bot fee (0.5-1%) + Polymarket taker fee (2%) + slippage (0.5-1%) =
  4-6% friction per round trip
- A 5-7% edge becomes 1% after fees

**Consider instead:** Use the BTC 5-min analysis as a *skill detector*, but
copy-trade those wallets on longer-duration markets (1-hour, 24-hour) where
2-second latency is noise, not edge.

## Action plan for safety-first deployment

1. Start with **$100**, not $1-5k
2. Test **withdrawal** before adding more
3. Never leave more than **2-3 days of trading capital** in any Telegram
   bot wallet
4. Rotate funds via on-chain withdrawal **nightly**
5. Run the Rotation page daily — remove declining traders
6. After 2 weeks of safe operation, scale to full capital
7. Avoid any tool with per-trade minimum fees on high-frequency strategies

## Absolute do-nots

- **Do not use Polycule** — suspected exit scam, users locked out
- **Do not run random GitHub "polymarket-copy-trading-bot" repos** —
  active malware campaign
- **Do not use PolyDex** ($299/mo, opaque, curated-only) — bad value
- **Do not send a seed phrase** to any support account claiming to help
  (clone bots + fake support are the #1 loss vector)
- **Do not keep more than 2-3 days of capital** in any Telegram bot's wallet

## Key sources

- PolyCop: https://polycopbot.com/ • https://polycop.gitbook.io/polycop-docs
- PolyGun: https://polygun.app/ • https://polymarketanalytics.com/copy-trade
- MirrorCopy: https://www.mirrorcopy.com
- PolyFollow: https://www.polyfollow.com/
- py-clob-client: https://github.com/Polymarket/py-clob-client
- QuickNode guide: https://www.quicknode.com/guides/defi/polymarket-copy-trading-bot
- Polycule hack report: https://www.kucoin.com/news/flash/telegram-trading-bot-polycule-on-polymarket-hacked-230k-stolen
- StepSecurity malware report: https://www.stepsecurity.io/blog/malicious-polymarket-bot-hides-in-hijacked-dev-protocol-github-org-and-steals-wallet-keys
- PolyGun acquires Polymarket Analytics: https://www.globenewswire.com/news-release/2026/03/09/3252008/0/en/PolyGun-Acquires-Polymarket-Analytics-in-a-Landmark-Deal-for-the-Prediction-Market-Industry.html

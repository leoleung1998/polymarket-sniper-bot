# Polymarket Sniper Bot

Automated trading bot for Polymarket prediction markets. Three strategies, one codebase. Run it locally in 5 minutes, or go full autopilot with cloud deployment and AI-powered remote control.

**Start simple. Add complexity when you're ready.**

| Level | What You Get | Time to Set Up |
|-------|-------------|----------------|
| **Basic** | Bot runs on your laptop, you watch the terminal | 5 minutes |
| **+ Telegram Alerts** | Get trade notifications on your phone | +2 minutes |
| **+ Cloud Deployment** | Bot runs 24/7 even when your laptop is closed | +20 minutes |
| **+ AI Control Bot** | Message your bot from your phone, Claude AI diagnoses issues and makes fixes | +5 minutes |

## The Five Strategies

1. **Weather Bracket Bot** — Trades daily weather temperature brackets using the GFS 31-member ensemble forecast. Counts how many ensemble members land in each bracket to compute probability. Buys when edge > 8%.
2. **Crypto Maker Bot** — Trades 15-min BTC/ETH/SOL up/down markets. Enters aggressively at best bid + 1 tick (capped at our price ceiling), chases fills every 30s by cancel-replacing, filtered by Allium on-chain smart money signals. Auto-redeems winning CTF tokens in background before each bet cycle. Running at **88% win rate**.
3. **Delta-Neutral Pairs Bot (v6)** — Trades correlated crypto pairs (BTC/ETH) on 5-min or 15-min up/down markets. Buys both legs simultaneously when their combined price is below 1.0. Wins whether both go UP or both go DOWN (~95% of windows). Loses only on genuine divergence (~5%). Completely direction-agnostic. Entry confirmed by Binance rolling correlation on **completed candles only**.
4. **Sniper** — Buys outcomes priced under 3 cents. High volume, low cost, lottery-ticket math.
5. **Sniper + Take Profit** — Same as Sniper, but automatically sells positions when gain exceeds TP_THRESHOLD (default +40%). Monitors open positions every 60 seconds and places aggressive GTC sell orders N basis points below the real best ask.

---

# Level 1: Run Locally (5 minutes)

Everything you need to get trading. No cloud, no Telegram, no API keys beyond Polymarket.

### Step 1: Clone and install

```bash
git clone https://github.com/kylecwalden/polymarket-sniper-bot.git
cd polymarket-sniper-bot
pip install -r requirements.txt
cp .env.example .env
```

### Step 2: Get your Polymarket credentials

1. Go to [polymarket.com](https://polymarket.com) and create an account (deposit at least $20 USDC)
2. Click your profile icon (top right) > **Cash** > **...** (three dots) > **Export Private Key**
3. Open `.env` in any editor and fill in:
   ```
   PRIVATE_KEY=your_private_key_here
   WALLET_ADDRESS=your_wallet_address_here
   SIGNATURE_TYPE=1    # 1 = email login, 0 = MetaMask/EOA wallet
   ```

### Step 3: Connect VPN and run

Polymarket blocks US IPs. Connect [ProtonVPN](https://pr.tn/ref/WMF7NFH4) (or any VPN) to a non-US server, then:

```bash
# Pick your strategy:
python bot.py bracket     # Weather bot (recommended to start)
python bot.py maker       # Crypto maker (15-min BTC/ETH/SOL)
python bot.py pairs       # Delta-neutral pairs bot v6 (BTC/ETH correlated)
python bot.py dual        # Both weather + crypto maker in parallel
python bot.py tp          # Sniper + Take Profit (buy cheap, sell at +40%)

# Utilities:
python bot.py scan        # Preview cheap outcomes without buying
python bot.py positions   # Check your P&L
```

**That's it.** The bot scans Polymarket, finds edges, and places trades. You'll see everything in your terminal. Keep it running as long as you want — close it anytime with `Ctrl+C`.

---

# Level 2: Add Telegram Alerts (+2 minutes)

Get notifications on your phone for every trade, win, loss, and status update. No more watching the terminal.

1. Open Telegram, search for **@BotFather**, send `/newbot`, follow the prompts, copy the **bot token**
2. Search for **@userinfobot**, send any message, copy your **chat ID**
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```
4. Restart the bot. You'll now get messages like:

   > 🎯 **NEW TRADE PLACED**
   > *Dallas High Temp Above 75°F*
   > Betting: YES @ $0.42/share
   > Cost: $5.00 (11 shares)
   > Edge: 12.3% over market
   > If we win: +$6.38 profit

---

# Level 3: Deploy to the Cloud (+20 minutes)

Run the bot 24/7 on a cloud server so it keeps trading when your laptop is closed, sleeping, or off.

**Recommended:** [AWS Free Tier](https://aws.amazon.com/free/) — 12 months free on a t3.micro instance. Pick a European region (like Ireland) and you get a non-US IP automatically, so you don't even need a VPN.

The setup is the same as Level 1 — just do it on a cloud server instead of your laptop:

1. Spin up a Linux instance (Ubuntu 22.04) on AWS, DigitalOcean, Linode, etc.
2. SSH in, clone the repo, install dependencies, create `.env`
3. Run with `nohup python bot.py dual &` or set up a systemd service for auto-restart on crashes

**Pro tip:** If you pick an EU region, set `PROTON_VPN_REQUIRED=false` in your `.env` — the server's IP is already non-US.

---

# Level 4: AI-Powered Remote Control (+5 minutes)

This is the fun one. Add a Claude AI brain to your Telegram bot so you can control everything from your phone using natural language.

**What it does:** You message your Telegram bot, Claude reads your code and logs, diagnoses issues, edits files, restarts services — all from your phone. It's like having a DevOps engineer on call 24/7.

**Requires:** An [Anthropic API key](https://console.anthropic.com) (~$0.01-0.05 per message)

1. Sign up at [console.anthropic.com](https://console.anthropic.com) and create an API key
2. Add to `.env`:
   ```
   ANTHROPIC_API_KEY=your_anthropic_api_key
   ```
3. Run the control bot (in addition to your trading bot):
   ```bash
   python telegram_control.py
   ```
4. Message your Telegram bot:

   | Message | What Happens |
   |---------|-------------|
   | `/status` | Bot health, bankroll, P&L at a glance |
   | `/logs` | Recent activity from both bots |
   | `/restart` | Restart the trading bots |
   | `/pause` / `/resume` | Pause or resume trading |
   | *"Why aren't any trades going through?"* | Claude reads logs and code, tells you what's wrong |
   | *"Lower the edge threshold to 5%"* | Claude edits the config and restarts the bot |
   | *"How much have we made today?"* | Claude checks P&L and gives you a summary |

---

## How the Strategies Work

### Weather Bracket Bot (`python bot.py bracket`)

Trades daily temperature bracket markets across 20+ global cities (Dallas, Seoul, Tokyo, London, etc.)

**The edge:** We use the GFS 31-member ensemble from Open-Meteo's free API. Each ensemble member runs a slightly different simulation of the atmosphere. If 28/31 members predict a high above 70°F, that's a 90.3% probability — far more accurate than a single-forecast guess. When Polymarket prices a bracket at 50% but our ensemble says 90%, we buy.

**Flow:**
1. Fetches GFS 31-member ensemble from Open-Meteo (`ensemble-api.open-meteo.com`)
2. Counts how many members land in each temperature bracket
3. Compares ensemble probability vs Polymarket price
4. When edge > 8%, places GTC limit order at the real orderbook best ask (auto-cancels after 20s if unfilled)
5. Falls back to NOAA/Open-Meteo single forecast + normal distribution if ensemble unavailable
6. Skips single-degree brackets (too noisy for ensemble resolution)
7. Skips cities past 4 PM local time (observation window closed)

**Why this works:** The [top weather bots on Polymarket ($24K+ profit)](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09) all use GFS ensemble counting. Single-forecast models can't compete.

### Crypto Maker Bot (`python bot.py maker`)

Trades 15-minute BTC/ETH/SOL "Up or Down" markets using a maker (limit order) strategy.

**Why taker arbitrage is dead:** In Feb 2026, Polymarket introduced [dynamic taker fees up to 3.15%](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/) and removed the 500ms taker delay. The old strategy of FOK-ing the spread no longer works.

**The strategy:**
1. Connects to Binance WebSocket for real-time BTC/ETH/SOL prices
2. Uses the **15-min Binance kline open price** as the true window start (accurate even when joining mid-window)
3. At T-480s (8 min) before close: checks if price moved >0.1% in one direction
4. Queries **Allium on-chain data** for smart money confirmation — if wallets with 70%+ win rates disagree, the trade is blocked
5. If clear AND smart money agrees → places aggressive limit at **best bid + 1 tick**, capped at our price ceiling
6. **Chases fills every 30s**: cancel + replace at new best bid + 1 tick until filled or T-60s before close
7. Stops chasing 60s before close — leaves final order on the book to stand
8. After window close: collect $1.00/share on wins

**Bid price ceiling logic:**
- Scales from `MAKER_BID_PRICE_LOW` (small moves) to `MAKER_BID_PRICE_HIGH` (large moves)
- **Per-band auto-cap**: tracks win rates in three confidence bands (small/medium/large). Once a band has 10+ trades, caps bid at `observed_win_rate - 5%` to stay profitable
- Chase price is always `min(best_bid + 0.0001, ceiling)` — falls back to ceiling if orderbook unavailable

**Order chasing (cancel-replace):**
- Entry: `best_bid + 1 tick` if below ceiling, otherwise `ceiling`
- Every 30s: check if filled → if not, cancel + re-place at new best bid + 1 tick
- Stops at T-60s before close (last order left to stand)
- Ceiling is never violated — protects edge regardless of orderbook movement

**Auto-redemption (`redeemer.py`):**

Winning positions on Polymarket are CTF (Conditional Token Framework) tokens held in the Gnosis Safe proxy wallet. They don't auto-convert to USDC — they must be explicitly redeemed on-chain. This is non-trivial because:

- The **FUNDER** wallet (`0x438B...`) is a Gnosis Safe, not a regular EOA
- The **WALLET_ADDRESS** (`0x084A...`) is the EOA that *owns* the Safe
- Redemption requires calling `CTF.redeemPositions()` *through* the Safe via `Safe.execTransaction()`
- The EOA signs the Safe transaction and pays POL gas (~0.04 POL per redemption)

**Call chain:**
```
EOA (signs) → Safe.execTransaction(to=CTF, data=redeemPositions(...))
                └─ CTF.redeemPositions(USDC, bytes32(0), conditionId, [1,2])
                      └─ USDC lands back in the Safe (FUNDER wallet)
```

**`redeemPositions` parameters:**
```python
CTF.redeemPositions(
    collateralToken    = USDC_ADDRESS,          # 0x2791Bca...
    parentCollectionId = bytes32(0),            # always zero for top-level markets
    conditionId        = bytes32(conditionId),  # from Polymarket position data
    indexSets          = [1, 2],                # [0b01, 0b10] = both YES and NO slots
)
```

**Safe signature packing (`SIGNATURE_TYPE=2`):**

Gnosis Safe requires a specific 65-byte signature format. The EOA signs the Safe transaction hash (not the raw tx hash), then the `v` byte is adjusted:
```python
# v adjustment for Gnosis Safe contract signatures
if v in (0, 1):  v += 31   # → 31 or 32
if v in (27, 28): v += 4   # → 31 or 32
packed = r (32 bytes) + s (32 bytes) + v (1 byte)
```

**Non-blocking design:**

`check_and_redeem()` is called before each bet but returns instantly:
1. Collects any USDC redeemed by a previous background thread (`pop_redeemed()`)
2. If new winning positions exist and no thread is running, starts a new daemon thread
3. The daemon waits 120s (oracle settlement buffer), then redeems sequentially

```
Main loop ──► check_and_redeem() ──► returns $X from last thread (adds to bankroll)
                    │
                    └──► [background daemon thread]
                              wait 120s
                              fetch POL balance
                              if POL >= 0.005 → on-chain via Safe.execTransaction
                              else            → gasless relay (polymarket relayer-v2)
                              store result in _pending_redeemed
                              send Telegram alert
```

**Two execution paths:**

| Path | Trigger | Gas | Speed |
|------|---------|-----|-------|
| On-chain | EOA POL ≥ 0.005 | ~0.04 POL/tx | ~30s |
| Relay | POL < 0.005 | Free (gasless) | ~60s, shared quota |

EOA currently holds ~111 POL (~2,900 redemptions worth). On-chain path is preferred.

**Fail-safe guarantees:**
- All errors caught + sent to Telegram — redemption never raises or blocks trading
- `_redeemed_this_session` set prevents double-redemption within a session
- Only one background thread runs at a time (checked before starting new one)
- Position must have `curPrice >= 0.99` and `currentValue >= $0.50` to qualify

**Persistent session state:**
- W/L counts, balance, and per-band win rates are saved to `data/maker_state.json` after every trade
- Restored on restart (band data is the most critical — it drives the bid price auto-cap)
- State older than 7 days is discarded (band data goes stale)
- Daily loss counter resets at midnight UTC

**Why it works:** 8-minute entry + aggressive chasing means high fill rates while Allium smart money filter blocks bad calls. Break-even bid prices (0.82/0.88) give realistic margins vs win rate. Running at **88% win rate** since adding the Allium filter. Maker orders = zero fees + maker rebates.

### Delta-Neutral Pairs Bot (`python bot.py pairs`)

Trades correlated crypto pairs on Polymarket's 5-min or 15-min up/down markets. Works by buying both sides of a correlated pair simultaneously — direction doesn't matter.

**The edge:** BTC and ETH move the same direction ~95% of windows. When Polymarket prices them independently, the combined cost of buying both correlation outcomes is sometimes below $1.00 — i.e. `BTC_UP + ETH_DOWN < 1.00`. You collect that discount whether BTC and ETH both go up (BTC_UP wins) or both go down (ETH_DOWN wins). You only lose if they genuinely diverge, which historically happens ~5% of the time.

**Flow:**
1. Connects to Binance WebSocket for real-time prices + bootstraps 20 completed candles at startup
2. Every tick: computes `sum_1 = BTC_UP + ETH_DOWN` and `sum_2 = BTC_DOWN + ETH_UP`
3. Takes the cheaper direction: `best_sum = min(sum_1, sum_2)`
4. Checks Binance **completed candle** Pearson ρ — if correlation is low, skip (it's real divergence, not mispricing)
5. Entry guard: `ρ > best_sum` (exact break-even identity — flat MIN_RHO is wrong, see §8)
6. Places aggressive GTC at ask price (effective taker), both legs sized by equal shares
7. Holds to resolution — one leg pays $1.00/share, the other pays $0
8. Resolution checked 30s after window close; P&L logged to `data/pairs/`

**Status panel shows:**
- Per-coin live UP/DOWN prices with stale detection
- Rolling Binance ρ (completed candles) with candle count
- Both directional sums for every pair + best edge
- Active signals, open positions, bankroll

**Telegram alerts:**
- New window notification at every boundary (with pair names and fresh token subscription confirmation)
- Trade entry, outcome (WIN_BOTH / WIN_A / WIN_B / LOSS)

**The forming candle trap (hard lesson from live testing):**

A live test showed BTC and ETH both mid-candle showing green (same direction) with sum=0.69 — looked like a perfect entry. Both legs lost. Reason: the *forming* candle hadn't closed yet. ETH reversed before the 15-minute window closed. The fix: `CandleTracker` only uses **closed candles** (`klines[:-1]` from Binance REST). Never the current open candle.

**Why 5-min delivers 3× more trades:**
Same infrastructure, same edge per trade, 288 windows/day vs 96. Set `PAIRS_MARKET_WINDOW=5` in `.env`. See §9 for full WS reconfiguration requirements at 5-min frequency.

---

### Sniper Mode (`python bot.py scan` / `run`)

Scans all active Polymarket events for outcomes priced under 3 cents. Places small bets on extreme long shots with asymmetric upside.

**The math:** Buy 100 outcomes at $0.02 each = $200 total. If 1 wins = $500 payout.

### Sniper + Take Profit (`python bot.py tp`)

Enriches the Sniper with an automatic exit strategy. Scans for cheap outcomes as normal, but also monitors all open positions every 60 seconds. When a position gains more than `TP_THRESHOLD` (default 40%), it automatically sells.

**How selling works:** Fetches the live CLOB orderbook, skips AMM floor/ceiling orders ($0.01/$0.99), finds the real best ask from actual market makers, then places a GTC sell order `TP_AGGRESSION_BPS` below the ask. If the price crosses the best bid, it fills immediately as a taker. Otherwise it sits at the front of the sell queue.

**Works across all strategies** — monitors `data/orders.json` (Sniper), `data/v4_trades.json` (Weather), and logs sells to `data/tp_log.json`.

---

## Configuration Reference

All settings are in `.env`. Defaults work out of the box — only `PRIVATE_KEY` and `WALLET_ADDRESS` are required.

### Core (Required)

| Variable | Default | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | required | Polymarket wallet private key |
| `WALLET_ADDRESS` | required | Your Polygon wallet address |
| `SIGNATURE_TYPE` | `1` | `0` = EOA wallet, `1` = email/Magic login, `2` = MetaMask/Gnosis Safe |
| `FUNDER` | — | Proxy wallet address (required for `SIGNATURE_TYPE=2`) |
| `PROTON_VPN_REQUIRED` | `true` | Require non-US IP before trading |

### Weather Bot

| Variable | Default | Description |
|----------|---------|-------------|
| `V4_MIN_EDGE_WEATHER` | `0.08` | Min edge to trade (8%) |
| `V4_SCAN_INTERVAL` | `300` | Seconds between scans |
| `V4_MAX_BET` | `10.0` | Max bet size (USDC) |
| `V4_MIN_BET` | `2.0` | Min bet size (USDC) |
| `V4_DAILY_BANKROLL` | `50.0` | Daily budget (USDC) |
| `V4_KELLY_FRACTION` | `0.10` | Kelly criterion fraction (10%) |
| `V4_MAX_ENTRY_PRICE` | `0.80` | Won't buy above 80 cents |
| `V4_MIN_WIN_PROB` | `0.65` | Only bet when model says 65%+ win chance |
| `V4_MAX_BUY_PRICE` | `0.50` | Max share price (cheap = better upside) |
| `V4_MAX_WEATHER_PER_CYCLE` | `6` | Max weather bets per scan cycle |

### Crypto Maker Bot

| Variable | Default | Description |
|----------|---------|-------------|
| `MAKER_COINS` | `BTC,ETH,SOL` | Coins to trade (comma-separated) |
| `MAKER_BET_SIZE` | `30` | Min bet floor (USDC) — also used as fixed size when `MAKER_BET_PCT=0` |
| `MAKER_MAX_BET` | `40.0` | Max bet ceiling (USDC) — Kelly and fractional bets never exceed this |
| `MAKER_BET_PCT` | `0.0` | Fractional bankroll sizing: `0.05` = bet 5% of current balance per trade. `0` = use fixed `MAKER_BET_SIZE` |
| `MAKER_DAILY_BANKROLL` | `100.0` | Daily budget (USDC) |
| `MAKER_DAILY_LOSS_LIMIT` | `80.0` | Stop trading after this much in losses |
| `MAKER_MIN_MOVE_PCT` | `0.10` | Min price move to bet (0.1%) |
| `MAKER_BID_PRICE_LOW` | `0.82` | Bid ceiling for small moves — break-even at 82% win rate |
| `MAKER_BID_PRICE_HIGH` | `0.88` | Bid ceiling for large moves — break-even at 88% win rate |
| `MAKER_ENTRY_SECONDS` | `480` | Enter at T-480s (8 min) before close |
| `MAKER_LOSS_STREAK_LIMIT` | `3` | Pause after 3 consecutive losses |
| `MAKER_LOSS_COOLDOWN` | `3600` | Cooldown after loss streak (seconds) |
| `MAKER_TARGET_EV` | `2.0` | Kelly bet target: $EV per trade (scales bet size once band has ≥10 trades) |
| `MAKER_SIGNAL_SCALE` | `2.0` | Binance→probability divisor: `prob = 0.5 + confidence / scale` (raise to be more conservative) |
| `MAKER_MIN_GAP` | `0.0` | Min gap between Binance signal and Polymarket price to enter. `0` = any edge, `-0.03` = tolerate 3% lag (more trades), `0.05` = require 5%+ confirmed edge (selective) |

### Sniper Bot / Take Profit Bot

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PRICE` | `0.005` | Min outcome price to buy (0.5 cents) |
| `MAX_PRICE` | `0.03` | Max outcome price to buy (3 cents) |
| `BET_SIZE_USDC` | `10` | USDC per bet |
| `MAX_DAILY_SPEND` | `100` | Daily spending cap |
| `SCAN_INTERVAL_MINUTES` | `30` | Minutes between scans |
| `TP_THRESHOLD` | `0.40` | Sell when gain exceeds this (40%) |
| `TP_SCAN_INTERVAL` | `60` | Check positions every N seconds |
| `TP_AGGRESSION_BPS` | `10` | Basis points below best ask when selling (crosses book = instant fill) |

### Optional Services

| Variable | Level | Description |
|----------|-------|-------------|
| `TELEGRAM_BOT_TOKEN` | Level 2 | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | Level 2 | Your Telegram chat ID |
| `ANTHROPIC_API_KEY` | Level 4 | Anthropic API key (for AI control bot) |
| `ALLIUM_API_KEY` | Optional | Allium on-chain data API key |

## All Commands

| Command | Description |
|---------|-------------|
| `python bot.py bracket` | Weather bracket bot (GFS ensemble) |
| `python bot.py maker` | Crypto maker bot (15-min BTC/ETH/SOL) |
| `python bot.py pairs` | Delta-neutral pairs bot v6 (BTC/ETH correlated, 5-min or 15-min) |
| `python bot.py dual` | Run weather + crypto maker in parallel |
| `python bot.py tp` | Sniper + Take Profit (buy cheap, sell at +40%) |
| `python bot.py tp test` | Test full buy→sell cycle with $5 |
| `python bot.py scan` | Preview cheap outcomes (no buying) |
| `python bot.py run` | Legacy v1 sniper bot (no TP) |
| `python bot.py positions` | Show positions and P&L |
| `python telegram_control.py` | AI-powered Telegram control bot (Level 4) |

## Project Structure

```
polymarket-sniper-bot/
├── bot.py                  # CLI entry point (bracket/maker/dual/scan/positions)
├── .env.example            # Config template — copy to .env
├── requirements.txt        # Python dependencies
│
├── # ── Weather Bracket Bot ──
├── arb_engine_v4.py        # Weather scoring + trading loop (GFS ensemble)
├── bracket_markets.py      # Discovers weather bracket events from Gamma API
├── bracket_model.py        # Probability models (ensemble counting + normal dist)
├── noaa_feed.py            # Weather data (GFS ensemble + NOAA + Open-Meteo)
│
├── # ── Crypto Maker Bot ──
├── arb_engine_v5_maker.py  # 15-min crypto maker strategy (chase + auto-redeem)
├── crypto_markets.py       # 15-min up/down market discovery (shared by maker + pairs)
├── binance_feed.py         # Real-time BTC/ETH/SOL prices (WebSocket)
├── poly_feed.py            # Polymarket odds feed (polls every 5s)
├── redeemer.py             # Auto-redeems winning CTF tokens via Safe.execTransaction
│
├── # ── Delta-Neutral Pairs Bot v6 ──
├── arb_engine_v6_pairs.py  # Correlated pairs strategy (Mode B delta-neutral)
│
├── # ── Remote Control (Level 4) ──
├── telegram_control.py     # Claude AI Telegram bot (control from phone)
├── telegram_alerts.py      # Trade alerts via Telegram (Level 2)
│
├── # ── Take Profit Engine ──
├── take_profit.py          # TP monitor: checks positions, sells at +40% gain
│
├── # ── Shared Infrastructure ──
├── trader.py               # CLOB order placement + tracking
├── tracker.py              # Position monitoring + P&L
├── scanner.py              # Gamma API market scanner
├── vpn.py                  # VPN connection verification
├── allium_feed.py          # On-chain smart money signals
│
└── data/                   # Auto-created: orders, trades, logs
    ├── orders.json         # Sniper positions
    ├── v4_trades.json      # Weather bot positions
    ├── tp_log.json         # Take profit sell history
    ├── maker_trades.json   # Maker bot fill history
    ├── maker_pending.json  # Live pending orders (used by kill switch)
    ├── maker_state.json    # Persistent W/L + band win rates (survives restarts)
    └── pairs/              # Pairs bot v6 state
        ├── pairs_state.json    # Balance, W/L, band wins (survives restarts)
        └── pairs_trades.jsonl  # All pairs trade entries and outcomes (JSONL)
```

## Safety Features

| Guard | Weather Bot | Crypto Maker | Pairs Bot v6 |
|-------|------------|--------------|--------------|
| Daily bankroll cap | $50 | $100 | Configurable (`PAIRS_BANKROLL`) |
| Daily loss limit | — | $80 | $30 (`PAIRS_DAILY_LOSS_LIMIT`) |
| Max drawdown | 35% (circuit breaker) | — | — |
| Loss streak pause | 5 losses → 30 min | 3 losses → 60 min | — |
| Win rate floor | Halts if <30% after 10 trades | — | — |
| Model sanity check | Skip if model vs market >40% apart | Skip if <0.1% price move | Skip if ρ ≤ sum (EV guard) |
| Smart money filter | — | Skip if Allium whales contradict direction | — |
| Bid price auto-cap | — | Caps bid at `observed_win_rate - 5%` per band (≥10 trades) | Kelly switches to observed band WR after 10 trades per band |
| Chase ceiling | — | Never exceeds `bid_price` regardless of orderbook | N/A (taker, fills immediately) |
| Orphan leg protection | — | — | Cancels filled leg if partner fails within 5s |
| Leg price range | — | — | Skip if leg < 0.15 or > 0.85 (near-resolved) |
| Minimum time guard | — | — | Skip if window < 300s remaining |
| Forming candle guard | — | — | ρ computed on **closed candles only** (never forming) |
| Redemption failure | — | Caught + Telegram alert, trading continues | N/A (CTF payout auto at resolution) |
| State file corruption | — | Discards + starts fresh | Discards + starts fresh |
| Telegram alerts | Every trade/win/loss/halt | Every trade/win/loss/halt/redeem | New window, every trade, outcome |

---

## Quant Notes — Known Weaknesses & Improvement Areas

This section documents the current signal model's assumptions and known limitations, written for a quant who wants to improve the edge.

### 1. Binance Signal → Win Probability (Heuristic, Not Empirical)

**Current formula** (`arb_engine_v5_maker.py`):
```python
binance_prob = min(0.98, 0.5 + confidence / MAKER_SIGNAL_SCALE)
```

Where `confidence` = absolute % price move from 15-min window open to T-480s, and `MAKER_SIGNAL_SCALE` (default `2.0`) controls steepness.

**This is a made-up heuristic.** The derivation:
- Start at 50% (random baseline — no Binance signal = coin flip)
- Add `confidence / scale` as a linear confidence boost
- Cap at 98% (never be fully certain)

**`MAKER_SIGNAL_SCALE` tuning guide:**
| Scale | confidence=0.5% | confidence=1.0% | Behavior |
|-------|-----------------|-----------------|----------|
| 1.0   | 100% (capped)   | 100% (capped)   | Very aggressive — almost always trades |
| 2.0   | 75%             | 100% (capped)   | Default — balanced |
| 3.0   | 67%             | 83%             | Conservative — needs larger moves |
| 4.0   | 63%             | 75%             | Very conservative |

Raise `MAKER_SIGNAL_SCALE` if you're getting too many gap-check skips. Lower it if the bot is trading too aggressively on small moves.

**Example (default scale=2):** ETH moves 0.105% down → `0.5 + 0.105/2 = 55.25%` → rounded to 55%

**The problem:** There is no statistical basis for this formula. A 0.5% move in 8 minutes is historically very predictive (~90%+ win rate in our data), yet the formula only assigns 75% confidence. This causes:
- Underestimating edge on large moves
- Over-reliance on the gap check to filter bad trades

**What to replace it with:**
Run a logistic regression on `maker_trades.json` (fields: `bid_price`, `size`, `cost`, `timestamp`, `outcome`):
```
P(win) = sigmoid(β0 + β1 * move_pct + β2 * poly_price + β3 * time_of_day)
```
Or simpler: bin trades by move_pct decile, compute empirical win rate per bin, fit a monotone curve.

The band tracking (small/medium/large) is a crude version of this — once you have 50+ trades per band it will partially self-correct via the auto-cap.

---

### 2. Gap Check (Edge Detection)

**Current logic:**
```python
gap = binance_prob - poly_price   # skip if gap < MAKER_MIN_GAP
```

`poly_price` is the **CLOB mid price** (best bid + best ask / 2) — fetched live every 5s. This replaced the old Gamma `outcomePrices` (last traded price) which could be minutes stale and caused false-positive entries.

**`MAKER_MIN_GAP` tuning:**
| Value | Behaviour |
|-------|-----------|
| `0.0` (default) | Enter only when Binance signal > Polymarket price — strict, fewest trades |
| `-0.03` | Tolerate up to 3% lag in signal — more trades, accepts slightly worse edge |
| `0.05` | Require 5%+ confirmed edge — very selective, highest quality entries |

If you're getting too many "already priced in" skips, lower `MAKER_MIN_GAP` (e.g. `-0.02`). If you're taking losing trades where Polymarket was ahead of you, raise it.

**Further improvement:** Both inputs are noisy.
- `binance_prob` is the heuristic above (biased — see §1)
- `poly_price` is the CLOB mid — but you're buying at the **ask**, not mid

Use the actual ask price for a more conservative check:
```python
gap = binance_prob - poly_ask   # avoids overpaying spread
```

---

### 3. Bid Price Ceiling (Static Bounds + Auto-Cap)

**Current formula:**
```python
t = min(1.0, max(0.0, (confidence_pct - MIN_MOVE_PCT) / 0.4))
bid = BID_LOW + t * (BID_HIGH - BID_LOW)   # linear interpolation: $0.82–$0.88
```

**Auto-cap** (kicks in after ≥10 trades per band):
```python
bid = min(bid, observed_win_rate - 0.05)
```

**Break-even math:** At bid price `b`, you need win rate `w = b` to break even.
- Profit per win: `(1 - b) × size`
- Loss per loss: `b × size`
- Break-even: `w × (1-b) = (1-w) × b` → `w = b`

So `BID_HIGH = 0.88` requires 88% sustained win rate. With 1 loss every 10 trades (90% win rate), EV per trade = `0.90 × 0.12 - 0.10 × 0.88 = +$0.02/share`.

**Improvement:** Replace static bounds with Kelly criterion bet sizing:
```python
edge = win_rate - bid_price          # your edge per dollar
kelly_bet = edge / (1 - bid_price)   # fraction of bankroll to bet
```
This auto-sizes both bid price and bet size based on observed edge.

---

### 4. Optimal Bet Size Formula

To reverse-calculate bet size from observed win rate:
```python
def optimal_bet(win_rate, bid=0.85, target_ev=2.0):
    """
    Returns bet size (USDC) that generates target_ev dollars EV per trade.
    Returns min_bet if no edge exists at this bid price.
    """
    edge = win_rate * (1 / bid - 1) - (1 - win_rate)
    if edge <= 0:
        return min_bet
    return round(min(max_bet, max(min_bet, target_ev / edge)), 2)
```

`target_ev` is set via `MAKER_TARGET_EV` env var (default `2.0` = target $2 EV per trade).

Example at 90% win rate, $0.85 bid, `MAKER_TARGET_EV=2.0`:
```
edge = 0.90 × (1/0.85 - 1) - 0.10 = 0.90 × 0.176 - 0.10 = 0.059
bet  = 2.0 / 0.059 ≈ $33.90
```

**`MAKER_TARGET_EV` tuning guide:**
- `1.0` → smaller bets, more conservative sizing
- `2.0` → default — balanced risk/reward
- `5.0` → larger bets when edge is confirmed (high win rate bands)

Note: bet is always clamped to `[base_bet, MAKER_MAX_BET]`, so Kelly only matters once the band has ≥10 trades.

---

### 4b. Fractional Bankroll Sizing (`MAKER_BET_PCT`)

An alternative to fixed bet sizing. Set `MAKER_BET_PCT=0.05` to bet 5% of your current balance each trade:

```
base_bet = balance × MAKER_BET_PCT
base_bet = clamp(base_bet, MAKER_BET_SIZE, MAKER_MAX_BET)
```

**Why use it:**
- Bets scale up automatically as you win (compounding)
- Bets shrink automatically after losses (anti-ruin)
- `MAKER_BET_SIZE` becomes a floor (never bet less), `MAKER_MAX_BET` stays the ceiling

**Example with `MAKER_BET_PCT=0.05`, `MAKER_BET_SIZE=20`, `MAKER_MAX_BET=50`:**

| Balance | Raw 5% | Actual bet |
|---------|--------|-----------|
| $300 | $15 | $20 (floor) |
| $500 | $25 | $25 |
| $800 | $40 | $40 |
| $1,200 | $60 | $50 (ceiling) |

Kelly still applies on top of `base_bet` once bands have ≥10 trades. If `MAKER_BET_PCT=0` (default), fixed `MAKER_BET_SIZE` is used unchanged.

---

### 5. WebSocket Zombie Connection at Window Boundaries

> **Critical for delta-neutral / 5-min markets:** The same bug occurs at every window boundary. On 5-min markets it fires 3× more often. See [V6_PLAN.md — Infrastructure: WS Zombie at 5-min Frequency](#) for required parameter changes.

---

#### What this bot does (background)

Polymarket creates a brand-new market for each 15-minute window — e.g. `btc-updown-15m-1742900700`. Every market has two tokens: `UP` (BTC closes higher) and `DOWN` (BTC closes lower), each with a unique 256-bit token ID like `0xabc123...`. The bot subscribes to these token IDs on Polymarket's WebSocket and receives live price updates in real time.

#### The bug: all coins show `(stale)` at every window transition

At exactly `HH:00:00`, `HH:15:00`, `HH:30:00`, `HH:45:00`, a new market opens. This requires the bot to:
1. Discover the new token IDs (new market = new token IDs)
2. Subscribe the WS to those new tokens
3. Wait for price events to arrive

**Two problems hit back-to-back at this boundary:**

**Problem A — Price cache cleared with no replacement:**

`register_tokens()` deletes all old-window prices from the in-memory cache immediately (old token IDs are useless once the market resolves). But the WS hasn't received any prices for the new tokens yet. For the next several seconds, `poly_feed.get_price()` returns `None` for every coin → display shows `(stale)`.

```
T+0s: register_tokens() called → all old prices deleted → (stale) appears
T+1s: WS subscribe sent to Polymarket server
T+15s: first price events arrive (market makers haven't quoted yet)
→ 15 seconds of (stale) on every window transition
```

**Problem B — Zombie WebSocket connection (the deeper bug):**

Even after the initial gap, prices would freeze permanently after the next transition. Investigation showed:

```
WS update counter: 555,398 (frozen, never increments again)
PING/PONG: ✅ working fine
price_change events: ❌ silently stopped
Polymarket UI: prices actively moving (market is NOT quiet)
```

The WS connection stays alive at the TCP level (PING/PONG works), but Polymarket's server **silently stops routing price_change events** for the subscribed tokens. This is a server-side state issue at market boundaries.

This was invisible at first because `_last_msg` (the silence watchdog's timestamp) was being refreshed by PONG messages every 10 seconds — so the watchdog never triggered. The bot had no way to tell "I'm getting PONGs but no price data".

#### What we confirmed through testing

| Test | Result |
|------|--------|
| `test_window_lag.py` — does Polymarket API have lag at transition? | **Zero lag** — next-window markets exist with `acceptingOrders=True` 5+ minutes before they open |
| `test_ws_subscribe.py` — does each subscribe replace or add tokens? | **ADD** (not replace) — each subscribe adds tokens, never drops existing ones |
| `test_bootstrap.py` — how long does REST bootstrap take? | **~638ms** — well within 1 loop tick |
| Live observation at `00:30:00` transition | Bootstrap worked (prices jumped from 0/1 resolved → 0.47/0.53 live in <1s), then WS delivered 1,438 events and froze permanently while Polymarket UI showed active price movement |

#### Three-layer fix

**Layer 1 — REST price bootstrap** (fixes the gap at transition):

Immediately after `register_tokens()` clears old prices, fetch fresh prices from the Gamma REST API and seed the cache. The display shows real prices within ~650ms, never `(stale)`.

```python
# arb_engine_v5_maker.py — after register_tokens()
for coin in _coins_needing_bootstrap:
    m = discover_market(coin)
    if m:
        poly_feed.update(m.up_token_id,   coin, "up",   m.up_price)
        poly_feed.update(m.down_token_id, coin, "down", m.down_price)
```

**Layer 2 — Force WS reconnect at every boundary** (attempts to clear zombie):

Close and reopen the WS connection at each window transition. A fresh connection gets a fresh server-side session with proper event routing.

```python
# poly_ws.py
async def reconnect(self):
    await self._ws.close()       # kill zombie
    self._pending_tokens = list(self._token_map.keys())  # re-subscribe everything
# run() loop reconnects automatically
```

This helps but is not guaranteed — Polymarket's server can go zombie again on the new connection after a few hundred events.

**Layer 3 — REST price heartbeat every 15s** (the definitive fix):

Since the WS is unreliable after window boundaries, poll the Gamma REST API every 15 seconds regardless of WS state. This keeps `poly_feed` prices fresh even when the WS is completely zombie.

```python
# arb_engine_v5_maker.py — background task
async def _rest_price_heartbeat():
    while True:
        await asyncio.sleep(15)
        for coin in MAKER_COINS:
            m = discover_market(coin)
            if m:
                poly_feed.update(m.up_token_id,   coin, "up",   m.up_price)
                poly_feed.update(m.down_token_id, coin, "down", m.down_price)
asyncio.create_task(_rest_price_heartbeat())
```

With `STALE_THRESHOLD = 60s` and prices refreshed every 15s, the cache **never expires** — even with a fully zombie WS.

**Layer 4 — Price event lag tracking** (visibility):

`_last_price_event` tracks the last time a `price_change` event was processed (not the last PONG). The status panel shows `price_lag=Xs` when no price events have arrived for > 5s — making zombie connections immediately visible in the display.

#### Summary

| | Before fix | After fix |
|---|---|---|
| Window transition gap | ~15–30s stale | <1s (bootstrap fills in ~650ms) |
| WS zombie | Permanent stale | REST heartbeat keeps prices fresh |
| `STALE_THRESHOLD` | 30s (too tight) | 60s (headroom for REST poll gaps) |
| REST heartbeat | None | Every 15s (3 refreshes per STALE window) |
| Zombie visibility | Invisible | `price_lag=Xs` in status panel |

**The WS is still used when it works** (real-time, low latency). The REST heartbeat is a safety net — it adds ~3 Gamma API calls/min per coin but ensures prices are never stale regardless of WS health.

---

### 6. Time-of-Day Signal

Not currently used. Hypothesis: Polymarket is less efficient at pricing crypto moves during low-liquidity hours (03:00–08:00 UTC) — more edge available. High-liquidity hours (13:00–21:00 UTC, US market hours) → Polymarket prices in moves faster → more skips. Worth adding `hour_of_day` as a feature.

---

### 7. Data Available for Model Training

All trades are logged to `data/maker_trades.json`:
```json
{
  "type": "maker_fill",
  "coin": "BTC",
  "direction": "up",
  "bid_price": 0.82,
  "size": 36.58,
  "cost": 29.99,
  "timestamp": "2026-03-23T21:45:05Z"
}
```

Outcome records (`type: "maker_outcome"`) include win/loss. Join on timestamp to get full feature set. Target variable: `outcome == "win"`. Features available: `coin`, `direction`, `bid_price`, `move_pct` (from log), `poly_price` (from log), `time_of_day`, `conf_band`.

Currently ~10 trades. Need 200+ per band for statistically significant model fitting.

---

### 8. Delta-Neutral Pairs — Full Quant Model (v6 Mode B)

This section documents the complete mathematical framework for Mode B pairs trading. Intended as a quant reference for calibration, scaling, and building the 5-min version.

#### Always take the cheaper direction

For any pair (A, B), compute both directional combos every tick and enter the one with the lower sum:

```python
sum_1 = price_a_up   + price_b_down   # buy A_up + B_down
sum_2 = price_a_down + price_b_up     # buy A_down + B_up
best_sum = min(sum_1, sum_2)          # always enter the cheaper side
```

The direction flips depending on which coin is leading at any moment. Never hardcode it.

```
Tick 1:  BTC UP=0.55  ETH DOWN=0.31  →  sum_1=0.86  sum_2=1.14  →  enter sum_1
Tick 2:  BTC UP=0.77  ETH DOWN=0.54  →  sum_1=1.31  sum_2=0.69  →  enter sum_2
```

Entering the wrong direction means paying 5–15% more for the same payout.

#### Why equal shares, not equal dollars

Each leg pays **$1 per share** if it wins. Sizing by equal shares means both legs deliver the same payout — the hedge is symmetric. Sizing by equal dollars distorts it: if BTC DOWN = 0.23 and ETH UP = 0.46, equal dollars would buy 2× more BTC DOWN shares, making the trade a net directional bet on BTC falling.

```
Leg A: buy N shares of BTC DOWN at 0.23  →  cost = 0.23N
Leg B: buy N shares of ETH UP   at 0.46  →  cost = 0.46N
Total spend = 0.69N  (the "sum")
```

#### EV formula

```
P(same direction) = ρ   (correlation — ~0.95 for BTC/ETH)
P(diverge)        = 1 - ρ

EV = ρ    × (N - 0.86N)    +    (1-ρ)/2 × (2N - 0.86N)    +    (1-ρ)/2 × (0 - 0.86N)
   = ρ    × 0.14N          +    (1-ρ)/2 × 1.14N            -    (1-ρ)/2 × 0.86N
   = 0.14N   (EV is constant regardless of ρ — see below)

ROI on capital = 0.14N / 0.86N = 16.3%
ROI after 5% fees = (N - 0.903N) / 0.903N = 10.7%
```

**Key insight: EV is correlation-invariant.** Lower ρ shifts probability mass from "win one leg" to "win both" and "lose both", but EV stays constant. What changes is *variance* — lower ρ → wilder swings. This is why ρ drives bet *sizing* (Kelly), not the entry decision itself.

**However EV is NOT profit-invariant.** The break-even condition is exactly:

```
EV = ρ×(1-sum) - (1-ρ)×sum = 0
→ ρ = sum  (exact mathematical identity)

sum=0.86 → need ρ > 0.86 to profit
sum=0.78 → need ρ > 0.78 to profit
sum=0.90 → need ρ > 0.90 to profit
```

This is the most important rule in the model: **the minimum viable ρ is not fixed — it equals the sum you're entering at.** A flat `MIN_RHO=0.80` is wrong for sum=0.86 (negative EV). The EV guard `ρ > sum` is mandatory on every entry.

#### Variance and risk model

```
Variance per trade (N shares, sum=s, correlation=ρ):

  σ² = ρ(1-ρ) × N²(1-s)²  +  (1-ρ)/2 × N²(2-s)²  +  (1-ρ)/2 × N²s²
     ≈ (1-ρ) × N²           (dominant term at low ρ)

Standard deviation of outcome ∝ sqrt(1-ρ)

  ρ=0.95  →  σ ∝ 0.224   (tight)
  ρ=0.85  →  σ ∝ 0.387   (73% wider)
  ρ=0.70  →  σ ∝ 0.548   (2.4× wider — skip at this level)
```

Expected consecutive losses before a win:
```
  ρ=0.95 → P(loss) = 2.5%  → 1 loss per ~40 trades
  ρ=0.85 → P(loss) = 7.5%  → 1 loss per ~13 trades
```

#### Proper Kelly bet sizing

```
For a pairs trade:
  b = net win per unit stake = (1-sum)/sum
  p = win prob ≈ ρ
  q = 1-ρ

  f* = (p×b - q) / b = ρ - (1-ρ)×sum/(1-sum)

  ρ=0.95, sum=0.86: f* = 64.3%  (quarter-Kelly = 16.1%)
  ρ=0.90, sum=0.86: f* = 56.0%  (quarter-Kelly = 14.0%)
  ρ=0.86, sum=0.86: f* = 0%     (break-even, no bet)
  ρ=0.80, sum=0.86: f* = -ve    (negative EV, skip)

Scale by ρ/0.95 to reduce size when correlation is below baseline:
  adjusted = f* × (ρ/0.95) × kelly_fraction × balance
```

#### Dynamic correlation factor from Binance (the v6 improvement)

`binance_feed.py` stores live price history for every coin. At each window close, record the window return `(close - open) / open` for BTC and ETH. Compute Pearson correlation over the last 20 windows (~5 hours on 15-min, ~1.7 hours on 5-min):

```python
def rolling_correlation(self, sym_a: str, sym_b: str, n: int = 20) -> float | None:
    a = list(self.window_returns[sym_a])[-n:]
    b = list(self.window_returns[sym_b])[-n:]
    if len(a) < 10: return None
    mean_a, mean_b = sum(a)/len(a), sum(b)/len(b)
    cov  = sum((x-mean_a)*(y-mean_b) for x,y in zip(a,b)) / len(a)
    sd_a = math.sqrt(sum((x-mean_a)**2 for x in a) / len(a))
    sd_b = math.sqrt(sum((x-mean_b)**2 for x in b) / len(b))
    return cov / (sd_a * sd_b) if sd_a and sd_b else None
```

Apply it to Kelly as a scaling factor:

```
corr_factor = observed_rho / baseline_rho (0.95)

ρ = 0.95  →  factor = 1.00  →  bet full half-Kelly
ρ = 0.85  →  factor = 0.89  →  bet 89% of half-Kelly
ρ = 0.75  →  factor = 0.79  →  bet 79% of half-Kelly
ρ < 0.70  →  skip trade entirely (correlation breakdown)
```

When macro or coin-specific news causes BTC and ETH to decouple on Binance, the system automatically sizes down or skips — without requiring any manual intervention.

#### Expected return per session

| | 15-min markets | 5-min markets |
|---|---|---|
| Windows per day | 96 | 288 |
| At 10% hit rate (sum < 0.90) | ~10 trades | ~29 trades |
| At 20% hit rate | ~19 trades | ~58 trades |

```
Example: $500 bankroll, half-Kelly = $40.50/trade, ROI after fees = 10.7%
EV per trade = $40.50 × 10.7% = $4.33

15-min at 10% hit rate:  10 × $4.33 = $43/day
5-min  at 10% hit rate:  29 × $4.33 = $126/day
```

The 5-min case delivers 3× daily EV from the same capital, same entry logic, and the same infrastructure (see [§5 WS Zombie fix](#5-websocket-zombie-connection-at-window-boundaries) for what needs retuning). Hit rate is the key unknown — must be measured via passive observation before live sizing.

#### What to validate before sizing up

| Question | How to measure |
|----------|---------------|
| Actual hit rate (how often sum < 0.90) | Passive observer 48–72h, log every signal |
| Actual correlation rate (is 2.5% loss rate accurate?) | Track `pairs_outcome` log after 50+ trades |
| Actual fee rate | Log `fee_paid` per trade, first 10 trades |
| Actual vs modelled ROI | Compare `net` in outcomes log vs 10.7% model |
| Correlation factor stability | Plot rolling ρ over 1 week, note regime changes |

---

### 9. Switching Between 15-min and 5-min Markets

Polymarket runs both `btc-updown-15m-{ts}` and `btc-updown-5m-{ts}` simultaneously.

**For the pairs bot:** One `.env` line controls everything — no code changes needed:

```bash
PAIRS_MARKET_WINDOW=15   # default: 15-min markets  (96 windows/day)
PAIRS_MARKET_WINDOW=5    # switch:   5-min markets (288 windows/day)
```

This is **isolated from the v5 maker bot** — `crypto_markets.py` stays hardcoded to 15-min. The pairs bot patches the module at runtime inside its own process.

**For the v5 maker bot** (not recommended to change): edit `crypto_markets.py` directly.

**Additional tuning needed for 5-min:**

| Setting | 15-min | 5-min | Why |
|---------|--------|-------|-----|
| `PAIRS_MIN_SECS_REMAINING` | `300` | `120` | Windows are shorter |
| `PAIRS_RHO_LOOKBACK` | `20` (5h history) | `20` (100min history) | Same candle count |
| `STALE_THRESHOLD` in `poly_feed.py` | `60` | `30` | Window is only 5 min |
| REST heartbeat in `poll_poly_prices` | `15s` | `10s` | More aggressive refresh |

**Why 5-min delivers 3× EV from the same capital:**
Same infrastructure, same entry logic, 288 windows/day vs 96. At 10% hit rate: 29 trades/day vs 10, multiplying daily EV by ~3.

**Why 5-min is harder operationally:**
288 window transitions/day = 288 WS reconnects and 288 WS zombie events to survive. The forming candle has 5 minutes to settle (vs 15) so correlation noise is higher per candle. The Binance ρ bootstrap uses the same 20-candle lookback but that now covers only ~100 minutes of history. Always validate on 15-min first — at least 50 trades — before switching.

---

### 10. Pairs Bot v6 Configuration Reference

Full `.env` settings for `python bot.py pairs`:

```bash
PAIRS_V6=BTC:ETH                  # Pairs (coin_a:coin_b, comma-separated). Phase 1: BTC:ETH only
                                  # Phase 2: BTC:ETH,ETH:SOL,BTC:SOL
PAIRS_MARKET_WINDOW=5             # 5 = 5-min markets | 15 = 15-min markets (default)
                                  # Does NOT affect v5 maker bot — pairs-only setting
PAIRS_ENTRY_THRESHOLD=0.88        # Enter when min(sum_1, sum_2) < this
PAIRS_MIN_RHO=0.80                # Hard ρ floor. EV guard (ρ > sum) applied on top.
PAIRS_RHO_LOOKBACK=20             # Completed candles for ρ calc (bootstrapped at startup)
PAIRS_MIN_LEG_PRICE=0.15          # Skip if either leg below this (near-certain outcome)
PAIRS_MAX_LEG_PRICE=0.85          # Skip if either leg above this (spread gone)
PAIRS_MIN_SECS_REMAINING=300      # Min window time remaining to enter
PAIRS_BANKROLL=100.0              # Set to actual Polymarket balance
PAIRS_MAX_BET=15.0                # Max total spend per trade (both legs combined)
PAIRS_MIN_BET=5.0                 # Min total spend per trade
PAIRS_KELLY_FRACTION=0.25         # Quarter-Kelly — conservative for Phase 1
PAIRS_DAILY_LOSS_LIMIT=30.0       # Halt for the day after this loss
PAIRS_DATA_DIR=data/pairs         # State + trade log directory
```

**Kelly sizing formula:**

```
f* = (ρ × b - (1-ρ)) / b    where b = (1-sum)/sum

Break-even rule: ρ must exceed sum to have positive EV.
  sum=0.86 → need ρ > 0.86
  sum=0.78 → need ρ > 0.78

Example: ρ=0.95, sum=0.86, $100 balance, quarter-Kelly
  raw Kelly = 64%  →  quarter = 16%  →  $16 spend (capped at MAX_BET)
```

After 10+ trades per band (`deep` <0.80, `medium` 0.80–0.86, `shallow` 0.86–0.90), the bot switches from theoretical ρ to observed band win rate — same auto-cap as v5.

---

## Research & References

This bot was built using research from the most profitable weather and crypto bots on Polymarket:

- [GFS Ensemble Weather Bot ($1,325 profit)](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
- [$24K weather bot teardown](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [Degen Doppler — 13-model weather edge finder](https://degendoppler.com/)
- [Polymarket dynamic fees killed latency arb](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [Polymarket CLOB docs](https://docs.polymarket.com/developers/CLOB/orders/create-order)
- [Open-Meteo GFS Ensemble API](https://open-meteo.com/en/docs/ensemble-api)

## Support This Project

If this bot makes you money, consider tipping the developer:

**Polygon/Ethereum (ERC-20):** `0x75A895ab14E58Af90e6CD9609EaACdfB5Ef07a36`

## Helpful Links

- [Sign up for Polymarket](https://polymarket.com)
- [Get ProtonVPN](https://pr.tn/ref/WMF7NFH4) — Free VPN required for trading from the US
- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api) — Free GFS ensemble data
- [Allium Data Platform](https://app.allium.so) — On-chain intelligence (optional)
- [Anthropic Console](https://console.anthropic.com) — API key for the AI control bot (Level 4)
- [AWS Free Tier](https://aws.amazon.com/free/) — Run 24/7 in the cloud for free (Level 3)

## Disclaimer

This is experimental software for educational purposes. Prediction markets carry risk of total loss. Past performance does not guarantee future results. This is not financial advice. Use at your own risk. You are responsible for compliance with all applicable laws in your jurisdiction.

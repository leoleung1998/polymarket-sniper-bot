# Polymarket Sniper Bot

Automated trading bot for Polymarket prediction markets. Three strategies, one codebase. Run it locally in 5 minutes, or go full autopilot with cloud deployment and AI-powered remote control.

**Start simple. Add complexity when you're ready.**

| Level | What You Get | Time to Set Up |
|-------|-------------|----------------|
| **Basic** | Bot runs on your laptop, you watch the terminal | 5 minutes |
| **+ Telegram Alerts** | Get trade notifications on your phone | +2 minutes |
| **+ Cloud Deployment** | Bot runs 24/7 even when your laptop is closed | +20 minutes |
| **+ AI Control Bot** | Message your bot from your phone, Claude AI diagnoses issues and makes fixes | +5 minutes |

## The Four Strategies

1. **Weather Bracket Bot** — Trades daily weather temperature brackets using the GFS 31-member ensemble forecast. Counts how many ensemble members land in each bracket to compute probability. Buys when edge > 8%.
2. **Crypto Maker Bot** — Trades 15-min BTC/ETH/SOL up/down markets. Enters aggressively at best bid + 1 tick (capped at our price ceiling), chases fills every 30s by cancel-replacing, filtered by Allium on-chain smart money signals. Auto-redeems winning CTF tokens in background before each bet cycle. Running at **88% win rate**.
3. **Sniper** — Buys outcomes priced under 3 cents. High volume, low cost, lottery-ticket math.
4. **Sniper + Take Profit** — Same as Sniper, but automatically sells positions when gain exceeds TP_THRESHOLD (default +40%). Monitors open positions every 60 seconds and places aggressive GTC sell orders N basis points below the real best ask.

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
python bot.py dual        # Both in parallel
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
├── crypto_markets.py       # 15-min up/down market discovery
├── binance_feed.py         # Real-time BTC/ETH/SOL prices (WebSocket)
├── poly_feed.py            # Polymarket odds feed (polls every 5s)
├── redeemer.py             # Auto-redeems winning CTF tokens via Safe.execTransaction
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
    └── maker_state.json    # Persistent W/L + band win rates (survives restarts)
```

## Safety Features

| Guard | Weather Bot | Crypto Maker |
|-------|------------|--------------|
| Daily bankroll cap | $50 | $100 |
| Daily loss limit | — | $80 |
| Max drawdown | 35% (circuit breaker) | — |
| Loss streak pause | 5 losses → 30 min | 3 losses → 60 min |
| Win rate floor | Halts if <30% after 10 trades | — |
| Model sanity check | Skip if model vs market >40% apart | Skip if <0.1% price move |
| Smart money filter | — | Skip if Allium whales contradict direction |
| Bid price auto-cap | — | Caps bid at `observed_win_rate - 5%` per confidence band (≥10 trades) |
| Chase ceiling | — | Never exceeds `bid_price` regardless of orderbook |
| Redemption failure | — | Caught + Telegram alert, trading continues |
| State file corruption | — | Discards + starts fresh, logs warning |
| Telegram alerts | Every trade/win/loss/halt | Every trade/win/loss/halt/redeem |

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

### 5. Time-of-Day Signal

Not currently used. Hypothesis: Polymarket is less efficient at pricing crypto moves during low-liquidity hours (03:00–08:00 UTC) — more edge available. High-liquidity hours (13:00–21:00 UTC, US market hours) → Polymarket prices in moves faster → more skips. Worth adding `hour_of_day` as a feature.

---

### 6. Data Available for Model Training

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

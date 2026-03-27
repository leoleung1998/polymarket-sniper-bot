# Microstructure Bot — Full Strategy Breakdown

**Run:** `python bot.py micro`
**Log:** `micro.log`, `data/micro/micro_trades.jsonl`, `data/micro/micro_state.json`

---

## What It Is

A short-term directional trading bot on Polymarket 5-minute BTC/ETH up/down binary markets.

Instead of predicting direction from fundamentals, it reads **real-time order book pressure** (OBI) and **Binance momentum** (EWMA) to catch moves that are already happening and not yet priced in.

Two execution modes depending on how much time is left:
- **Mode A** (T > 90s): maker entry → scalp exit, never holds to resolution
- **Mode B** (T ≤ 90s): taker entry → holds to resolution ($1.00 or $0.00)

---

## Signal 1 — Order Book Imbalance (OBI)

**What it measures:** bid vs ask volume near the current mid price.

```
OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol)

Range: -1.0 (all asks, sell pressure) to +1.0 (all bids, buy pressure)
```

**Near-market filter:** only levels within ±0.15 of mid are counted.
Polymarket books have giant passive orders at 0.01 and 0.99 (market makers hedging) that would completely dominate a raw OBI calculation. Those tell us nothing about short-term direction.

**Source:** Polymarket CLOB WebSocket (`ws-subscriptions-clob.polymarket.com`), `level: 2`.
- `book` events (on subscription) → full L2 snapshot, replaces entire book
- `price_change` events → single-level delta (add/update/remove)

**Interpretation:**
- OBI(UP token) > +0.15 → more bids than asks on the UP side → buy pressure → UP price will rise → **signal: buy UP**
- OBI(UP token) < -0.15 → more asks than bids on the UP side → sell pressure → **signal: buy DOWN**

Note: OBI(DOWN) is always ≈ -OBI(UP) in a binary market (YES + NO = $1). The two columns in the display confirm each other but carry the same information.

---

## Signal 2 — Binance EWMA Momentum

**What it measures:** whether Binance (the leading exchange) is trending up or down over the last ~30 seconds.

**Method:** time-decay dual EWMA (like MACD, but time-based not tick-based):

```
alpha_fast = 1 - exp(-dt / 10s)   # half-life ≈ 10s
alpha_slow = 1 - exp(-dt / 30s)   # half-life ≈ 30s

ewma_fast = alpha_fast * price + (1 - alpha_fast) * ewma_fast_prev
ewma_slow = alpha_slow * price + (1 - alpha_slow) * ewma_slow_prev

signal = (ewma_fast - ewma_slow) / ewma_slow * 100  (in %)
```

**Why time-decay and not tick-count alpha (2/(n+1)):**
Binance sends 8 ticks in one second sometimes, 100+ in another. Tick-count alpha is unstable and over-weights bursts. Time-decay alpha normalizes for real elapsed time regardless of tick rate.

**Warmup:** signal is unreliable for the first ~30s after startup (fast and slow haven't diverged yet). Bot shows `warm…` and skips entries during this period.

**Interpretation:**
- signal > +0.003% → BTC/ETH trending up on Binance → **confirms UP entry**
- signal < -0.003% → BTC/ETH trending down on Binance → **confirms DOWN entry**

---

## Entry Logic — Both Signals Must Agree

```
OBI(UP) > 0.15  AND  EWMA > +0.003%  →  buy UP token
OBI(UP) < -0.15 AND  EWMA < -0.003%  →  buy DOWN token
```

Additional filters before entry fires:
- `MICRO_MIN_PRICE ≤ price ≤ MICRO_MAX_PRICE` (default 0.20–0.80) — don't buy near-resolved markets
- `price + taker_fee < 0.98` — basic EV guard
- No existing position or pending order for this coin this window
- Daily loss limit not hit

---

## Mode A — Maker Scalp (T > 90s)

**Goal:** capture 3%+ price move without holding to resolution. Zero taker fee.

### Entry
Place a GTC **limit BID** just inside the spread:
```
bid_price = best_bid + 0.005
```
This sits one tick above the current best bid, improving queue position without crossing the book. No taker fee paid.

### After Fill
Immediately place a GTC **limit ASK** (sell) at:
```
exit_price = fill_price × (1 + MICRO_A_TP_PCT)   # default: fill + 3%
```

### Exit Outcomes
| Scenario | What happens |
|----------|-------------|
| Sell limit fills | 💰 Scalp profit recorded. Done. |
| Sell limit doesn't fill in `MICRO_A_HOLD_SECS` (45s) and T > 30s | Cancel sell → aggressive taker sell to exit flat |
| Sell limit doesn't fill and T ≤ 30s | Cancel sell → hold to resolution (can't exit cleanly) |
| Bid doesn't fill in `MICRO_A_HOLD_SECS` (45s) | Cancel bid → fall through to Mode B check |

### Why This Works
OBI > 0.15 means there's significantly more bid volume than ask volume near mid. That buy pressure typically compresses the spread and pushes price up 2-5 ticks within 30-60s. We're selling into that move.

---

## Mode B — Taker Sniper (T ≤ 90s)

**Goal:** enter at the right price with 90 seconds or less left, hold to resolution.

### Entry
Place an aggressive GTC **taker buy** at:
```
fill_price = best_ask + 0.002   (2 ticks above best ask, crosses book immediately)
```

### Exit
Hold to resolution. Polymarket settles the market:
- Winner gets $1.00 per share
- Loser gets $0.00 per share

Bot polls `gamma-api.polymarket.com` until `outcomePrices` shows one side ≥ 0.99.

### Why This Works
At T-90s, Binance price direction is highly predictive of final outcome (~80%+ accuracy). A strong OBI + EWMA signal at that point has very little time to reverse. We're essentially betting that what's already happening continues for 90 more seconds.

---

## Automatic Mode Switching

```
T > 90s  →  Mode A available
T ≤ 90s  →  Mode B available
```

Bot evaluates every 200ms. No manual intervention. Within one 5-minute window:

```
T=4:30  →  Mode A: OBI fires, maker bid placed
T=3:45  →  bid filled, sell order placed at entry+3%
T=3:20  →  sell fills → scalp profit, done for this window

-- OR if bid never filled --

T=3:00  →  bid expires (45s), cancelled
T=1:29  →  Mode B: OBI fires again, taker entry, hold to resolution
```

---

## Data Flow

```
Binance WS ──────────────────────────────────→ EWMAState (fast/slow per coin)
                                                       ↓
Polymarket CLOB WS ──→ book event   ──→ order_book.snapshot()
                   └──→ price_change ──→ order_book.update_level()
                                    └──→ poly_feed.update() (mid price)
                                                       ↓
                                             evaluate_signal()
                                                       ↓
                                          Mode A or Mode B entry
```

**Polymarket WS subscription:** `level: 2, initial_dump: True`
- `initial_dump: True` means the server sends a full `book` snapshot immediately on connect
- Without this, OBI stays in `warm…` until enough incremental deltas accumulate

**Price discovery fallback:** `poll_poly_prices()` runs every 3s via REST (CLOB `/book` endpoint) as a safety net if WS lags.

**WS zombie detection:** at each window boundary (new 5m market), `ws_feed.reconnect()` is called before re-subscribing. Long-lived connections can appear alive (PING/PONG responds) but the server silently stops routing events.

---

## Config Reference (`.env`)

| Variable | Default | Hot-reload | Description |
|----------|---------|------------|-------------|
| `MICRO_COINS` | `BTC,ETH` | No | Coins to trade |
| `MICRO_MARKET_WINDOW` | `5` | No | 5 or 15 minute markets |
| `MICRO_OBI_THRESHOLD` | `0.15` | Yes | Min \|OBI\| to fire signal |
| `MICRO_EWMA_THRESHOLD` | `0.003` | Yes | Min \|EWMA %\| to confirm |
| `MICRO_OBI_DEPTH` | `0.15` | Yes | Near-market filter radius |
| `MICRO_A_ENTRY_SECS` | `90` | Yes | Mode A available when T > this |
| `MICRO_A_HOLD_SECS` | `45` | Yes | Cancel bid/sell after this many seconds |
| `MICRO_A_TP_PCT` | `0.03` | No | Mode A take profit: sell at entry + 3% |
| `MICRO_A_FALLBACK_SECS` | `30` | No | Hold to resolution if T ≤ this when sell times out |
| `MICRO_B_ENTRY_SECS` | `90` | Yes | Mode B available when T ≤ this |
| `MICRO_MIN_PRICE` | `0.20` | Yes | Skip markets below this price |
| `MICRO_MAX_PRICE` | `0.80` | Yes | Skip markets above this price |
| `MICRO_TAKER_FEE` | `0.02` | Yes | 2% taker fee factored into EV guard |
| `MICRO_BET_SIZE` | `5.0` | Yes | Fixed $ per trade |
| `MICRO_MAX_BET` | `10.0` | Yes | Max bet ceiling |
| `MICRO_BET_PCT` | `0.0` | Yes | % of balance per trade (0 = use fixed) |
| `MICRO_BANKROLL` | `100.0` | No | Starting balance |
| `MICRO_DAILY_LOSS_LIMIT` | `50.0` | Yes | Stop trading for the day after this loss |
| `MICRO_STATUS_INTERVAL` | `1.0` | No | Display refresh in seconds |
| `MICRO_DATA_DIR` | `data/micro` | No | Where logs and state are saved |

Hot-reload: bot checks `.env` mtime every 15s. Changes to hot-reloadable vars take effect immediately and send a Telegram alert.

---

## Display Guide

```
🔬 Micro Bot  $108.26 (2W/0L)  loss=$0.00/50  WS live  23:58:31 UTC
 Coin  OBI↑ bid/ask      OBI↓ bid/ask      EWMA           T-     Mode  Position
 BTC   +0.22 0.51/0.53   -0.22 0.47/0.49   +0.006% $84k   2:15   A     —
 ETH   +0.18 0.61/0.63   -0.18 0.37/0.39   +0.004% $2k    2:15   A     MAKER UP 8sh bid@$0.612 (12/45s)
```

| Column | Meaning |
|--------|---------|
| `OBI↑ bid/ask` | OBI + best bid/ask for UP token. **Green** = buy signal, **Red** = sell signal |
| `OBI↓ bid/ask` | Same for DOWN token (always mirror of UP) |
| `EWMA` | Binance momentum signal + spot price. **Green** = up, **Red** = down |
| `T-` | Time remaining. Turns **bold red** in last 60s |
| `Mode` | `A` (cyan) or `B` (yellow) based on T- |
| `Position` | Current position or pending order status |
| Title `WS live` | Polymarket WS connected and receiving events |
| Title `lag=Xs` | Warning: no WS price events for >5s |

---

## Telegram Alerts

| Event | Message |
|-------|---------|
| Startup | `🔬 Micro Bot started` with config summary |
| Mode A entry | `🔬 MICRO UP BTC [A]` with OBI, EWMA, price, T- |
| Mode A scalp profit | `💰 MICRO SCALP UP BTC [A]` with buy→sell prices and profit |
| Mode A aggressive exit | `⚡ MICRO AGG EXIT BTC [A]` |
| Mode B entry | `🔬 MICRO UP BTC [B]` |
| Mode B win/loss | `🎉/💔 MICRO WIN/LOSS UP BTC [B]` with cost, payout, profit |
| Config change | `⚙️ Micro config reloaded: OBI_THRESHOLD: 0.15 → 0.20` |
| WS disconnect/reconnect | `⚠️/✅ Binance WS ...` or `⚠️/✅ PolyWS ...` |

---

## Key Files

| File | Purpose |
|------|---------|
| `micro_bot.py` | Main bot — signal evaluation, order management, display |
| `order_book.py` | L2 book tracker — OBI, spread, mid, best bid/ask per token |
| `poly_ws.py` | Polymarket CLOB WebSocket — feeds both `poly_feed` (mid prices) and `order_book` (L2) |
| `binance_feed.py` | Binance aggTrade stream — provides price history and EWMA state |
| `data/micro/micro_trades.jsonl` | Append-only trade log (entries, fills, results) |
| `data/micro/micro_state.json` | Persistent bankroll state (survives restarts) |
| `micro.log` | Full stdout log |

# v6 Maker Bot — Correlation Pairs Strategy

## Overview

v6 extends v5 as an additional signal layer. v5 continues running as-is (directional momentum).
v6 adds a new entry mode: **delta-neutral pairs trading** that profits from correlation between
BTC/ETH/SOL rather than predicting direction.

**Core insight:** Polymarket prices each coin independently, but BTC/ETH/SOL move together
~95% of the time. When the market implies different probabilities for correlated assets,
the pair is mispriced — and buying both sides captures that premium regardless of direction.

---

## Two Entry Modes

### Mode A — Laggard Discount (directional, lower complexity)

**Core idea:** Instead of hardcoding BTC/ETH as "leaders", compute the **global average UP
price across all coins**. Any coin significantly below that average is the laggard by
definition — no need to manually designate leaders.

**Setup (example):**
```
BTC UP = 0.75  ETH UP = 0.80  SOL UP = 0.55  XRP UP = 0.70  BNB UP = 0.65
global_avg_up = (0.75 + 0.80 + 0.55 + 0.70 + 0.65) / 5 = 0.69

discount per coin:
  BTC:  0.69 - 0.75 = -0.06  (above avg, overpriced)
  ETH:  0.69 - 0.80 = -0.11  (above avg)
  SOL:  0.69 - 0.55 = +0.14  ← laggard (0.14 below avg)
  XRP:  0.69 - 0.70 = -0.01
  BNB:  0.69 - 0.65 = +0.04

→ Buy SOL UP at 0.55 (largest positive discount)
```

**Signal conditions:**
```python
global_avg_up = mean(coin_up for all coins)
direction = "up"   if global_avg_up > 0.60  else
            "down" if global_avg_up < 0.40  else
            None   # no clear consensus — skip

if direction == "up":
    discount = global_avg_up - coin_up          # positive = cheap
elif direction == "down":
    discount = coin_down - global_avg_down      # symmetric

laggard_coin = argmax(discount)
laggard_price = coin_up (or coin_down)

# Entry conditions:
avg > CONSENSUS_THRESHOLD   # 0.65 — global market must lean strongly
discount > LAGGARD_GAP      # 0.15 — must be meaningfully below average
laggard_price < MAX_ENTRY   # 0.75 — skip if laggard already catching up
```

**Trade:** Buy the laggard coin in the global consensus direction.

**Edge source:** Market prices each coin independently. When 4 out of 5 coins lean UP,
the 5th is likely just slow to update — not fundamentally different. The global average
is a cleaner, more objective reference than picking specific leader coins.

**Worked example from live output:**
```
BTC UP=0.55  ETH UP=0.69  SOL UP=0.40  XRP UP=0.23  BNB UP=0.49
global_avg_up = 0.472  → no clear consensus (below 0.60) → SKIP

Better window:
BTC UP=0.82  ETH UP=0.85  SOL UP=0.60  XRP UP=0.78  BNB UP=0.72
global_avg_up = 0.754  → clear UP consensus
discounts: SOL=+0.154 (laggard), BNB=+0.034, XRP=-0.026...
→ Buy SOL UP at 0.60 (15.4% below consensus)
```

**Parameters:**
- `CONSENSUS_THRESHOLD = 0.65` — avg must exceed this for signal to fire
- `LAGGARD_GAP = 0.15` — min discount below average to qualify
- `MAX_LAGGARD_ENTRY = 0.75` — don't buy if laggard already > 0.75 (gap closing)
- `MIN_LAGGARD_ENTRY = 0.20` — skip near-zero prices (already resolved)

**Risk:** Works best when all coins are correlated. XRP and BNB occasionally diverge
from BTC/ETH for coin-specific reasons (regulatory news, exchange events). Consider
excluding XRP/BNB from the laggard candidate pool in Phase 1 and only use BTC/ETH/SOL.

---

### Mode B — Delta-Neutral Pairs (non-directional, higher edge)

**Setup:**
```
BTC UP = 0.55  ETH DOWN = 0.31
sum = 0.55 + 0.31 = 0.86  →  edge = 1.0 - 0.86 = 14%
```

**Trade:** Buy both sides simultaneously. Direction doesn't matter — you win as long
as the two coins move the same direction (which they do ~95% of the time).

**Payoff matrix (sum = 0.86, correlation = 95%):**

| Outcome | Probability | Result |
|---------|-------------|--------|
| Both UP (BTC UP wins) | ~47.5% | +0.14 |
| Both DOWN (ETH DOWN wins) | ~47.5% | +0.14 |
| BTC UP + ETH DOWN both win | ~2.5% | +1.14 |
| Both lose (BTC DOWN + ETH UP) | ~2.5% | -0.86 |

**EV ≈ +14% per trade** (completely direction-agnostic)

**Fee math:**
```
Total cost with taker fee = sum × (1 + fee_rate)

fee = 2%:  0.86 × 1.02 = 0.877  →  net edge = +12.3%
fee = 3%:  0.86 × 1.03 = 0.886  →  net edge = +11.4%
fee = 5%:  0.86 × 1.05 = 0.903  →  net edge = +9.7%

Breakeven: sum < 1 / (1 + fee_rate)
  fee = 3%  →  sum must be < 0.971
  fee = 5%  →  sum must be < 0.952
```

With conservative entry at sum < 0.90 → **8-10% net edge after worst-case fees**.

---

## Pairs to Monitor

| Pair | Estimated Correlation | Priority |
|------|----------------------|----------|
| BTC ↔ ETH | ~95% | High — most liquid, start here |
| ETH ↔ SOL | ~88% | Medium |
| BTC ↔ SOL | ~85% | Medium |
| BTC/ETH ↔ XRP | ~70% | Low — too much independent risk |
| BTC/ETH ↔ BNB | ~70% | Low |

**v6 Phase 1:** BTC/ETH pair only. Validate live before adding others.

---

## Entry Logic

```
Every tick, for each monitored pair (coin_a, coin_b):

  sum_1 = coin_a_up  + coin_b_down   # buy a_up + b_down
  sum_2 = coin_a_down + coin_b_up    # buy a_down + b_up
  best = min(sum_1, sum_2)

  Guard conditions:
    - both prices in range [0.15, 0.85]  (not near resolution)
    - time remaining > 300s              (enough time to fill)
    - no existing pairs position this window for this pair

  IF best < TAKER_THRESHOLD (0.90):
    → TAKER both legs immediately
    → guaranteed fill, fees covered by edge
    → log as "pairs_taker"

  ELIF best < MAKER_THRESHOLD (0.94):
    → MAKER both legs
    → watch 60s — cancel orphaned leg if partner doesn't fill
    → log as "pairs_maker"

  ELSE:
    → skip
```

---

## Order Execution

### Taker path (sum < 0.90)
```
1. Place taker order leg A  →  fills in milliseconds
2. Place taker order leg B  →  fills in milliseconds
3. Store both as a PairsTrade record
4. Hold to resolution — one leg pays $1.00, other pays $0.00 (95% of cases)
```

### Maker path (0.90 < sum < 0.94)
```
1. Place maker order leg A
2. Place maker order leg B
3. Watch both for 60s
   - Both filled  →  hold to resolution ✓
   - Neither filled  →  cancel both, no loss ✓
   - One filled, other didn't  →  cancel filled leg immediately
     (accept small cancel/refund loss rather than naked direction)
4. Store as PairsTrade record
```

---

## Risk Management

| Rule | Value | Reason |
|------|-------|--------|
| Max sum threshold (taker) | 0.90 | ~10% edge buffer over max fees |
| Max sum threshold (maker) | 0.94 | fees manageable, fill risk accepted |
| Min price per leg | 0.15 | below this = near-resolved, no liquidity |
| Max price per leg | 0.85 | above this = market too certain, not worth hedging |
| Min time remaining | 300s | need time for both legs to fill |
| Max 1 pairs trade per window per pair | — | no pyramiding until validated |
| Max pairs exposure | 50% of MAKER_MAX_BET | smaller size during validation phase |
| Correlation break stop | — | if 3+ consecutive losses (both sides lose), pause pairs |

---

## Leg Risk (the key operational risk)

If one taker leg fills and the other fails (network error, API error):
- Bot holds a naked directional position
- Must detect and cancel/hedge immediately

**Mitigation:**
```python
order_a = place_taker(...)
if order_a.filled:
    order_b = place_taker(...)
    if not order_b.filled within 5s:
        cancel_or_sell(order_a)  # exit immediately
        log_as_leg_failure()
```

For taker orders this scenario is very rare but must be handled.

---

## Data to Track (new fields in maker_trades.json)

```json
{
  "type": "pairs_fill",
  "pair": "BTC/ETH",
  "leg_a": {"coin": "BTC", "direction": "up", "price": 0.55, "size": 18},
  "leg_b": {"coin": "ETH", "direction": "down", "price": 0.31, "size": 32},
  "sum": 0.86,
  "edge": 0.14,
  "order_type": "taker",
  "fee_paid": 0.017,
  "timestamp": "..."
}
{
  "type": "pairs_outcome",
  "pair": "BTC/ETH",
  "winner": "leg_a",
  "payout": 18.0,
  "cost": 15.34,
  "net": 2.66,
  "correlation_held": true
}
```

This lets us measure actual correlation rate from live data over time.

---

## What v6 Does NOT Change

- v5 directional entries continue running unchanged
- Same WS feed, same bankroll object, same Binance signal
- Pairs trades are additive — separate P&L tracking
- Same kill switch covers pairs positions (they appear in pending_orders)

---

## Build Order (when ready)

1. `pairs_scanner.py` — scans poly_feed for pairs opportunities, returns best combo
2. `pairs_trader.py` — executes taker/maker legs, handles orphan detection
3. Wire into `arb_engine_v5_maker.py` — add pairs scan after main coin loop
4. Paper trade for 2-3 days to validate correlation assumption holds live
5. Go live with small size (50% of MAKER_MAX_BET per pairs trade)

---

## Critical Infrastructure: WS Zombie at 5-min Frequency

> **Read this before building v6 on 5-min markets.** This section documents a confirmed infrastructure bug that becomes 3× more disruptive at 5-min window frequency. The fix is already in v5 — but the parameters must be re-tuned for the shorter cycle.

---

### The problem (confirmed in v5 on 15-min markets)

Polymarket creates a brand-new market for every window — new slug, new token IDs. At every boundary the bot must discover new token IDs, re-subscribe the WS, and wait for price events. Two things go wrong:

**1. Price cache cleared with no replacement (~15s gap)**

`register_tokens()` deletes old-window prices immediately from `poly_feed`. The WS hasn't received prices for new tokens yet. Every coin shows `(stale)` for 10–30s until the market makers quote the new window.

**2. WS zombie connection (permanent stale)**

After subscribing to new tokens, Polymarket's server silently stops routing `price_change` events — even on a freshly reconnected WS. PING/PONG keeps working (TCP alive), but no price data flows. This was confirmed live: update counter frozen at 555,398 while Polymarket UI showed active price movement.

---

### Why 5-min markets make this worse

| Metric | 15-min markets | 5-min markets |
|--------|---------------|---------------|
| Window transitions per hour | 4 | 12 |
| Transitions per 8h trading session | 32 | 96 |
| WS zombie opportunities per session | ~32 | ~96 |
| Bot exposure if fix missing | ~30s × 32 = ~16 min/session stale | ~30s × 96 = ~48 min/session stale |

At 5-min cadence, a broken WS means the bot is blind for almost half the session. Every entry decision during stale is made on 0/1 (resolved market prices), creating catastrophic mis-pricing of the active window.

For a **delta-neutral pairs bot** this is especially dangerous: both legs of a pair must be priced correctly or the "sum < 0.90" edge calculation is worthless.

---

### The three-layer fix (already in v5 — re-tune for v6)

**Layer 1 — REST bootstrap at every boundary** (v5 code, works as-is):

Immediately after `register_tokens()` clears old prices, fetch fresh prices from Gamma REST API (~650ms). Display never shows `(stale)`.

```python
for coin in _coins_needing_bootstrap:
    m = discover_market(coin)
    if m:
        poly_feed.update(m.up_token_id, coin, "up", m.up_price)
        poly_feed.update(m.down_token_id, coin, "down", m.down_price)
```

**Layer 2 — Force WS reconnect at every boundary** (v5 code, works as-is):

`ws_feed.reconnect()` kills the zombie, `run()` loop reconnects fresh.

**Layer 3 — REST heartbeat** (v5: 15s — **must tighten for 5-min markets**):

Current: `asyncio.sleep(15)` — prices refreshed every 15s, `STALE_THRESHOLD = 60s`.

For 5-min markets you have a **5-minute window** to place orders. A 15s REST heartbeat is fine for 15-min markets but still exposes you to up to 15s of stale. For a pairs bot where you need both legs priced accurately this may cause missed entries.

**Recommended parameters for 5-min v6:**

```python
# poly_feed.py
STALE_THRESHOLD = 30   # 30s is plenty with a 10s heartbeat; don't need 60s buffer

# arb_engine_v6.py — REST heartbeat
async def _rest_price_heartbeat():
    while True:
        await asyncio.sleep(10)   # 10s instead of 15s — 3 refreshes per stale window
        ...
```

With `STALE_THRESHOLD=30` and heartbeat every 10s: worst-case stale window is 10s, and prices are always < 30s old. The 3:1 headroom ratio (30s threshold / 10s refresh) is the same safety margin as v5.

---

### Additional consideration: CLOB prices vs Gamma prices

The REST heartbeat uses `discover_market()` which fetches `outcomePrices` from the Gamma API (last-traded price). This can lag the live CLOB by 30–60 seconds during low-liquidity windows.

For the **pairs edge calculation** (sum = coin_a_up + coin_b_down), stale Gamma prices could cause false positives — the sum looks < 0.90 but the real CLOB prices are already back at 0.93.

**Fix:** Use CLOB mid prices in the heartbeat (same as `_clob_mid()` already in `poly_feed.py`):

```python
# Instead of discover_market() in heartbeat, call CLOB directly:
from poly_feed import _clob_mid  # or inline it
up_mid   = _clob_mid(market.up_token_id)   or market.up_price   # fallback to Gamma
down_mid = _clob_mid(market.down_token_id) or market.down_price
```

This ensures the pairs edge check is always comparing against real-time order book prices, not a potentially lagged snapshot.

---

### Checklist before v6 goes live on 5-min markets

- [ ] `STALE_THRESHOLD = 30` in `poly_feed.py`
- [ ] REST heartbeat interval `= 10s` in v6 engine
- [ ] REST heartbeat uses CLOB mid prices (not Gamma `outcomePrices`) for pairs accuracy
- [ ] `ws_feed.reconnect()` called at every 5-min boundary (same pattern as v5)
- [ ] REST bootstrap called for all coins after `register_tokens()` at each boundary
- [ ] Monitor `price_lag=Xs` in status panel at first few window transitions to confirm WS health
- [ ] Paper trade through at least 5 consecutive window transitions before live capital

---

## Open Questions

**Mode B — Pairs**
- [ ] What is the actual taker fee rate in current Polymarket conditions? (dynamic, need to observe)
- [ ] Does correlation hold during high-volatility windows (news events, liquidations)?
- [ ] What is the minimum sum discount we observe in practice? Need to monitor live data.
- [ ] Pairs trade bet sizing — fixed dollar amount or Kelly-scaled from observed correlation rate?

**Mode A — Laggard**
- [ ] Should the global average be **unweighted** or **liquidity-weighted**?
  - Unweighted: `avg = mean(btc, eth, sol, xrp, bnb)` — simple, no assumptions
  - Weighted by liquidity/correlation: `avg = btc×0.35 + eth×0.35 + sol×0.20 + xrp×0.05 + bnb×0.05`
  - Weighted makes BTC/ETH dominate the signal (they're most correlated with each other
    and most liquid) — XRP/BNB have independent drivers that could skew unweighted avg
  - **Suggested approach:** start unweighted, log both values, compare after 50+ windows
- [ ] Should XRP and BNB be excluded from the **laggard candidate pool** in Phase 1?
  - They can be included in the average (to inform consensus) but not bought as laggards
  - XRP: heavily influenced by regulatory news (decouples from BTC/ETH frequently)
  - BNB: exchange token with independent drivers (Binance-specific events)
  - **Suggested approach:** include in avg calculation, exclude as laggard buy targets initially
- [ ] What is the right `CONSENSUS_THRESHOLD`? (0.60 vs 0.65 vs 0.70)
  - Too low (0.60): fires in noisy markets where half the coins disagree
  - Too high (0.70): rarely fires, misses genuine lag opportunities
  - Need live data to calibrate — log every window's avg and discount spread
- [ ] How quickly does the laggard close the gap in practice?
  - If gap closes in <60s, maker entry at laggard price may not fill in time
  - If gap persists for 3-5 min, maker is fine; if fast-closing, taker may be needed

**General**
- [ ] Should Mode A and Mode B run simultaneously, or Mode B only to start?
- [ ] Passive observation script first — log all signals for 48h before placing any orders

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

## Mode B — Bet Sizing Math

### Step 0: Always take the cheaper direction

For any pair (A, B), **always compute both directional combos and enter the one with the lower sum**. Never hardcode which coin is "up" or "down" — the prices tell you each tick:

```python
sum_1 = price_a_up   + price_b_down   # scenario: A goes UP, B goes DOWN
sum_2 = price_a_down + price_b_up     # scenario: A goes DOWN, B goes UP

# Always enter the cheaper combo
if sum_1 <= sum_2:
    entry = ("up", "down"),  sum_1   # buy A_up + B_down
else:
    entry = ("down", "up"),  sum_2   # buy A_down + B_up
```

**Why this matters:** On any given tick one direction is always cheaper than the other. Entering the wrong direction means paying ~5–15% more for the same expected payout.

```
Example:
  BTC UP=0.55  ETH DOWN=0.31  →  sum_1 = 0.86  ← cheaper, enter this
  BTC DOWN=0.45  ETH UP=0.69  →  sum_2 = 1.14  ❌ too expensive

Another tick:
  BTC UP=0.77  ETH DOWN=0.54  →  sum_1 = 1.31  ❌
  BTC DOWN=0.23  ETH UP=0.46  →  sum_2 = 0.69  ← enter this
```

This is already in the Entry Logic section (`best = min(sum_1, sum_2)`) — the bet sizing math must use the same selected combo.

### Step 1: Equal shares, not equal dollars

Each leg pays **$1 per share** if it wins, regardless of price. The natural unit is **shares**, not dollars. Sizing by equal shares means both legs deliver the same payout when they win — which is what you want for a symmetric hedge.

```
Example: best_sum = 0.69 (BTC DOWN + ETH UP), buy N shares of each leg

  Leg A: BTC DOWN at 0.23  →  cost = 0.23 × N
  Leg B: ETH UP   at 0.46  →  cost = 0.46 × N
  Total spend = 0.69 × N
```

If you sized by equal dollars instead (spend $S on each leg), leg A would deliver $S/0.23 = 4.3× the payout of leg B — you'd be massively overweighting the cheaper leg. Equal shares keeps the hedge balanced.

### Step 2: EV per trade

```
EV = Σ P(outcome) × net_payout

= P(both UP)   × (N - 0.86N)     →  0.475 × 0.14N = 0.0665N
+ P(both DOWN) × (N - 0.86N)     →  0.475 × 0.14N = 0.0665N
+ P(diverge A) × (2N - 0.86N)    →  0.025 × 1.14N = 0.0285N
+ P(diverge B) × (0  - 0.86N)    →  0.025 × -0.86N = -0.0215N

EV = 0.14N  per trade
```

As a return on capital invested:
```
ROI per trade = EV / total_cost = 0.14N / 0.86N = 16.3%
```

After fees (worst case 5%):
```
Effective cost = 0.86N × 1.05 = 0.903N
Net EV = N - 0.903N = 0.097N
ROI after fees = 0.097 / 0.903 = 10.7%
```

### Step 3: Kelly bet sizing

Kelly fraction = edge / max_loss_per_unit
```
edge     = 1 - sum = 0.14
max_loss = sum      = 0.86   (both legs lose — the 2.5% scenario)

full Kelly = 0.14 / 0.86 = 16.3% of bankroll per trade
half Kelly = 8.1%   ← recommended starting point
```

Full Kelly is mathematically optimal for log-growth but produces violent drawdowns in practice. Half-Kelly is the standard conservative choice.

### Step 3b: Dynamic correlation factor from Binance (the key improvement)

The 95% correlation assumption is a long-run average. In practice, correlation varies:
- Normal trading day: ρ ≈ 0.92–0.97
- Macro event (Fed, CPI): ρ ≈ 0.98–0.99 (coins move together even more)
- Coin-specific news (ETH upgrade, BTC ETF): ρ can drop to 0.60–0.70

Lower ρ = more likely to hit the `-0.86` total-loss scenario = Kelly says size down.

**How to compute it:** `binance_feed.py` already stores `window_start_prices` and a `history` deque per coin. At the close of each window, record the window return for each coin. Compute Pearson correlation over the last 20 windows (= 5 hours on 15-min, 1.7 hours on 5-min):

```python
from collections import deque
import math

# Store per-window returns — add to binance_feed.PriceFeed
window_returns: dict = field(default_factory=lambda: {sym: deque(maxlen=50) for sym in SYMBOLS})

def record_window_return(self, symbol: str, open_price: float, close_price: float):
    ret = (close_price - open_price) / open_price
    self.window_returns[symbol].append(ret)

def rolling_correlation(self, sym_a: str, sym_b: str, n: int = 20) -> float | None:
    """Pearson correlation of last N window returns between two coins."""
    a = list(self.window_returns[sym_a])[-n:]
    b = list(self.window_returns[sym_b])[-n:]
    if len(a) < 10 or len(a) != len(b):
        return None   # not enough data yet
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    cov  = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b)) / len(a)
    sd_a = math.sqrt(sum((x - mean_a)**2 for x in a) / len(a))
    sd_b = math.sqrt(sum((x - mean_b)**2 for x in b) / len(b))
    if sd_a == 0 or sd_b == 0:
        return None
    return cov / (sd_a * sd_b)
```

**Apply it to Kelly sizing:**

```python
BASELINE_RHO   = 0.95   # assumed correlation when no history yet
MIN_RHO_TRADE  = 0.70   # skip pairs trade if correlation below this
MIN_RHO_SCALE  = 0.80   # start scaling down below this

def pairs_bet_size(
    bankroll: float,
    sum_price: float,
    price_a: float,
    price_b: float,
    observed_rho: float | None = None,
    kelly_fraction: float = 0.5,
    max_exposure: float = 0.20,
) -> tuple[float, float, float] | None:
    """
    Returns (shares_N, spend_leg_a, spend_leg_b) or None if correlation too low.
    """
    rho = observed_rho if observed_rho is not None else BASELINE_RHO

    if rho < MIN_RHO_TRADE:
        return None   # correlation breakdown — skip trade

    # Kelly base
    edge = 1.0 - sum_price
    full_kelly = edge / sum_price
    kelly = full_kelly * kelly_fraction

    # Correlation adjustment: scale linearly between MIN_RHO_SCALE and 1.0
    # At rho=0.95 (baseline): factor = 1.0
    # At rho=0.80: factor = 0.80/0.95 ≈ 0.84  (size down 16%)
    # At rho=0.70: factor = 0.0 (at boundary — trade skipped above)
    corr_factor = min(1.0, (rho - MIN_RHO_SCALE) / (1.0 - MIN_RHO_SCALE))
    adjusted_kelly = kelly * corr_factor

    # Cap total exposure
    fraction = min(adjusted_kelly, max_exposure)
    total_spend = bankroll * fraction
    shares_N = total_spend / sum_price
    return shares_N, price_a * shares_N, price_b * shares_N
```

**Worked example — normal day (ρ=0.95):**
```
bankroll=$500, sum=0.86, edge=0.14
full_kelly = 0.14/0.86 = 16.3%
half_kelly = 8.1%
corr_factor = (0.95-0.80)/(1.0-0.80) = 0.75/0.20...

Wait — baseline is 0.95, scale starts at 0.80:
corr_factor = (0.95 - 0.80) / (1.0 - 0.80) = 0.15/0.20 = 0.75

Hmm — actually use rho/BASELINE_RHO as the simpler factor:
corr_factor = 0.95/0.95 = 1.0 (at baseline, no adjustment)
corr_factor = 0.80/0.95 = 0.84 (at 80%, size down 16%)
corr_factor = 0.70/0.95 = 0.74 (at boundary, nearly skip)

adjusted_kelly = 8.1% × 1.0 = 8.1%
total_spend = $500 × 8.1% = $40.50
shares_N = $40.50 / 0.86 = 47.1 shares
leg_a = 0.55 × 47.1 = $25.90
leg_b = 0.31 × 47.1 = $14.60
```

**Worked example — diverging day (ρ=0.80, coin-specific news):**
```
corr_factor = 0.80/0.95 = 0.84
adjusted_kelly = 8.1% × 0.84 = 6.8%
total_spend = $500 × 6.8% = $34.00   ← automatically sized down
```

**Practical cap:** Kelly can suggest large sizes on strong edges. Cap total pairs exposure at 20% of bankroll per trade regardless of Kelly output until you have 50+ trades of live data.

### Step 4: Expected return per session

**Trade frequency** is the unknown — depends how often `sum < 0.90` in practice. Three scenarios based on assumed hit rate (needs passive observation to calibrate):

| Hit rate | 15-min windows/day | Trades/day | 5-min windows/day | Trades/day |
|----------|--------------------|------------|-------------------|------------|
| 5%  | 96  | ~5  | 288 | ~14 |
| 10% | 96  | ~10 | 288 | ~29 |
| 20% | 96  | ~19 | 288 | ~58 |

**Session return formula:**
```
Daily EV = trades_per_day × bet_size × ROI_per_trade

Example (conservative):
  bankroll        = $500
  half-Kelly size = $500 × 8.1% = $40.50 per trade
  ROI after fees  = 10.7%
  EV per trade    = $40.50 × 10.7% = $4.33

  At 10% hit rate:
    15-min: 10 trades/day  →  $43/day
    5-min:  29 trades/day  →  $126/day

  At 20% hit rate:
    15-min: 19 trades/day  →  $82/day
    5-min:  58 trades/day  →  $252/day
```

**Why 5-min is compelling:** Same infrastructure, same edge per trade, 3× more windows = 3× more trades = 3× daily EV. The only additional work is the infrastructure tuning documented in the WS Zombie section above.

### Step 5: What you need to validate before sizing up

| Question | How to measure | When |
|----------|---------------|------|
| Actual hit rate (how often sum < 0.90) | Passive observer, 48–72h | Before first live trade |
| Actual correlation rate (are the 95% divergence losses close to 2.5%?) | Track leg outcomes in `pairs_outcome` log | After 50+ trades |
| Actual fee rate observed | Log `fee_paid` per trade | First 10 trades |
| Actual ROI vs theoretical | Compare `net` field in outcomes vs model | After 20+ trades |

If observed correlation rate drops to 85% (e.g. due to news events):
```
EV = 0.425×0.14 + 0.425×0.14 + 0.075×1.14 + 0.075×(-0.86)
   = 0.0595 + 0.0595 + 0.0855 - 0.0645
   = 0.14  ← same EV (correlation doesn't change EV, only variance)
```

EV is actually **correlation-invariant** as long as prices stay at `sum = 0.86`. Lower correlation just means more jackpot wins and more total losses — higher variance, same expectation. What kills EV is sum rising above breakeven (fees eroding edge), not correlation dropping.

### Break-even rule (critical)

```
EV = ρ×(1-sum) - (1-ρ)×sum = 0  →  ρ = sum  (exact identity)

sum=0.86 → need ρ > 0.86 to profit
sum=0.78 → need ρ > 0.78 to profit
sum=0.90 → need ρ > 0.90 to profit
```

**A fixed `MIN_RHO` is wrong.** The minimum viable ρ is dynamic — it equals the sum you're entering at. The EV guard `ρ > sum` is enforced on every entry in `scan_pairs_signals()`.

### Proper Kelly derivation

```
For a pairs trade:
  b = net win per unit stake = (1-sum)/sum
  p = win prob ≈ ρ  (one leg wins when both coins go same direction)
  q = 1-ρ           (both lose when they diverge)

  Full Kelly: f* = (p×b - q) / b = ρ - (1-ρ)×sum/(1-sum)

  ρ=0.95, sum=0.86: f* = 64%   quarter-Kelly → 16% of balance
  ρ=0.90, sum=0.86: f* = 56%   quarter-Kelly → 14% of balance
  ρ=0.86, sum=0.86: f* = 0%    break-even → don't bet
  ρ=0.80, sum=0.86: f* = -ve   skip (negative EV)
```

After 10+ trades per band, Kelly switches to observed band win rate instead of theoretical ρ — same auto-cap as v5 maker bot.

### Variance and risk model

```
σ of outcome ∝ sqrt(1-ρ)

  ρ=0.95  →  P(both lose) = 2.5%   →  1 losing trade per ~40
  ρ=0.85  →  P(both lose) = 7.5%   →  1 losing trade per ~13
  ρ=0.70  →  P(both lose) = 15%    →  skip (too noisy)

Expected max drawdown over 50 trades at ρ=0.95:
  ~3-4 consecutive losses possible (tail event, ~2% probability)
  Daily loss limit ($30) stops the session before ruin
```

### Session return projections

```
Inputs: $100 bankroll, quarter-Kelly, ρ=0.95, sum=0.86, 3% fee

  Per-trade spend  = 16.1% × $100 = $16.10  (capped at MAX_BET=$15)
  Net edge         = EV - fee = 14% - 3% = 11%
  EV per trade     = $15 × 11% = $1.65

  15-min markets (96 windows/day):
    At 10% hit rate: ~10 trades → $16.50/day on $100 (16.5% daily)
    At 20% hit rate: ~19 trades → $31.35/day on $100 (31% daily)

  5-min markets (288 windows/day):
    At 10% hit rate: ~29 trades → $47.85/day on $100 (47.9% daily)
    At 20% hit rate: ~58 trades → $95.70/day on $100 (95.7% daily)

Hit rate (how often sum < threshold) is the key unknown.
Measure via passive observer before committing capital.
```

### Binance ρ as entry confirmation (not just risk filter)

When Polymarket prices show divergence (BTC UP=77%, ETH DOWN=54%) but Binance completed candles show **same direction** — this is genuine mispricing. The crowd is pricing a divergence that Binance history says is unusual.

When Polymarket shows divergence AND Binance completed candles also show divergence — Polymarket is correctly pricing reality. The low sum is not edge.

**Decision matrix:**

| Binance completed ρ | Polymarket sum | Action |
|---|---|---|
| High (> sum) | < threshold | ✅ Enter — genuine mispricing |
| High (> sum) | ≥ threshold | Skip — not enough edge |
| Low (≤ sum) | < threshold | ❌ Skip — real divergence, not mispricing |
| Low (≤ sum) | ≥ threshold | Skip — no edge either way |

Only the top-left box is a trade. This was validated live: a window showing BTC↑77%/ETH↓54% (sum=0.69) looked like huge edge, but Binance confirmed real divergence → both legs lost.

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

## Entry Logic (as built in `arb_engine_v6_pairs.py`)

```
Every tick, for each monitored pair (coin_a, coin_b):

  sum_1 = coin_a_up  + coin_b_down   # buy a_up + b_down
  sum_2 = coin_a_down + coin_b_up    # buy a_down + b_up
  best_sum = min(sum_1, sum_2)       # always take cheaper direction

  Guard conditions (ALL must pass):
    1. rho >= PAIRS_MIN_RHO              # hard floor (default 0.80)
    2. ev = rho*(1-best_sum) - (1-rho)*best_sum > 0  # dynamic EV guard: ρ > sum
    3. best_sum < PAIRS_ENTRY_THRESHOLD  # e.g. 0.88
    4. both leg prices in [0.15, 0.85]  (not near resolution)
    5. time remaining > PAIRS_MIN_SECS_REMAINING (default 300s)
    6. no existing position for this pair in the current window

  IF all guards pass:
    → place aggressive GTC at ask price (effective taker) for both legs
    → equal shares sizing: N shares each, total_cost = best_sum × N
    → hold to resolution — do not exit early
    → log as PairsPosition, check resolution 30s after window close
```

**Why only one threshold (not taker + maker ranges):**
A pairs trade requires both legs to fill in the same window. GTC at ask is aggressive enough to fill immediately (effective taker without the taker fee label). Adding a maker path (0.90–0.94) introduces orphan risk — one leg fills at the start and the other doesn't fill until near the close, leaving you briefly directional. The aggressive single-threshold approach is simpler and more reliable for delta-neutral sizing.

**EV guard is mandatory — flat MIN_RHO is insufficient:**
`MIN_RHO=0.80` alone allows entering sum=0.86 with ρ=0.80 (negative EV: 0.80 < 0.86). The per-signal EV check `ρ > sum` is the correct gate. The flat `MIN_RHO` is only a first-pass floor to skip obvious low-correlation environments fast.

---

## Order Execution (as built)

### Aggressive GTC (single path)
```
1. Compute best_sum = min(sum_1, sum_2), select directions
2. Compute bet_size via Kelly: f* = (ρ×b - (1-ρ)) / b, capped at MAX_BET
3. Calculate N shares = bet_size / best_sum
4. Place GTC at ask+2ticks for leg A  →  fills immediately (crosses book)
5. Place GTC at ask+2ticks for leg B  →  fills immediately
6. If leg B fails (order error): log orphan status, continue (not immediate cancel)
   — the 5/15-min window provides time to recover
7. Store as PairsPosition (window_ts, directions, fill prices, shares, cost, band)
8. Hold to resolution — do NOT exit early regardless of price movement
9. After window_end + 30s: poll Gamma API for outcomePrices ≥ 0.95 to detect winner
10. Record WIN_BOTH / WIN_A / WIN_B / LOSS, update bankroll, send Telegram alert
```

**One entry per pair per window:** Once entered, `traded_this_window` set blocks re-entry for the same pair until the next window. No pyramiding until validated.

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

## Isolation from v5 Maker

v6 pairs bot runs as a **completely separate process** from v5 maker:

| Property | v5 Maker | v6 Pairs |
|----------|----------|----------|
| Command | `python bot.py maker` | `python bot.py pairs` |
| Market window | Always 15-min (hardcoded) | 5-min or 15-min (`PAIRS_MARKET_WINDOW`) |
| Bankroll | `MAKER_DAILY_BANKROLL` | `PAIRS_BANKROLL` |
| State file | `data/maker_state.json` | `data/pairs/pairs_state.json` |
| Trade log | `data/maker_trades.json` | `data/pairs/pairs_trades.jsonl` |
| Signal source | Binance momentum (directional) | Binance ρ + Polymarket sum (non-directional) |
| Entry type | Limit order, maker, chases fills | GTC at ask, immediate fill |
| Hold strategy | Chase until filled or T-60s | Hold to resolution — no exit |

**`crypto_markets.py` is shared but NOT mutated by v5:**
The pairs bot patches `crypto_markets.COIN_SLUGS` and `crypto_markets.WINDOW_SECONDS` at runtime inside its own process for `PAIRS_MARKET_WINDOW=5`. The v5 maker's process is unaffected — it imports the module separately. Running both bots simultaneously is safe.

---

## Current Status (BUILT — `arb_engine_v6_pairs.py`)

The pairs bot is fully implemented as a standalone strategy. Run it with `python bot.py pairs`.

**What's live:**
- `arb_engine_v6_pairs.py` — complete delta-neutral pairs engine (~980 lines)
- Standalone process: own bankroll, own state file, own data directory
- Wired into `bot.py` as `python bot.py pairs`
- `PAIRS_MARKET_WINDOW` in `.env` switches between 5-min and 15-min without touching v5 maker

**What was built differently from the original plan:**
- Implemented as standalone bot (not wired into v5 maker) — cleaner separation of concerns
- Always GTC-at-ask (effective taker) — no "maker path" for 0.90–0.94 range; taker fills are more reliable for delta-neutral where both legs must fill in the same tick
- Orphan handling: if leg B fails after leg A fills, logs and proceeds (not immediate cancel) — the 5/15-min window gives time to recover
- ρ computed in `CandleTracker` (not in `binance_feed.py`) — decoupled from WS feed, bootstrapped at startup from Binance REST klines
- Status panel shows live Polymarket odds per coin (UP/DOWN prices, stale detection, feed stats)
- Telegram alert fires at every window boundary with pair names and token subscription confirmation

**Phase 1 validation checklist:**
- [ ] Run 48–72h passive observation — log every tick's `best_sum` to measure actual hit rate
- [ ] After 20 live trades: check `pairs_trades.jsonl` for actual correlation rate (how often both legs win)
- [ ] After 10 trades per band: verify Kelly auto-cap is switching to observed win rate correctly
- [ ] Compare actual fee_paid per trade vs model assumption (3%)
- [ ] Confirm Binance ρ > sum EV guard is correctly filtering marginal entries

**Phase 2 (after 50+ trades validation):**
- Add `ETH:SOL` and `BTC:SOL` pairs (lower ρ ~88%, require higher entry threshold)
- Switch to `PAIRS_MARKET_WINDOW=5` for 3× daily trade frequency
- Raise `PAIRS_MAX_BET` as bankroll grows

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

---

## Live Testing Lessons

These are lessons learned from actual live trades — not theory. Each one changed the code.

---

### Lesson 1: The Forming Candle Trap

**What happened:** A live window showed BTC and ETH mid-candle with the same color (both moving the same direction). sum=0.69 — massive edge. Both legs were entered. Both lost.

**Root cause:** The *forming* Binance candle (currently open, not yet closed) had not settled. ETH looked green mid-candle but reversed by close. The window ended with ETH moving the opposite direction from BTC.

**Why this is not obvious:** The Binance WebSocket delivers continuous tick data. At T-4 minutes, the candle can look decisively one way. At T-0, it can end the other way. A live candle is just a running average — it has not yet "decided".

**The fix:** `CandleTracker` uses `klines[:-1]` (exclude the last entry = the forming candle). Only **closed candles** are used for ρ computation. This means:
- At the very start of a window, the previous completed candle's return has just been locked in
- The candle contributing to ρ is final — it cannot reverse
- ρ reflects actual co-movement, not noise from in-progress candles

**Code location:** `bootstrap_candle_history()` fetches `klines[:-1]`. `CandleTracker.on_new_window()` is called at window boundaries — always recording the *completed* candle's return.

---

### Lesson 2: Low Sum ≠ Edge (Real Divergence vs Mispricing)

**What happened (same trade):** sum=0.69 from Polymarket. BTC and ETH genuinely were diverging — Polymarket was correctly pricing it. The "edge" was fake.

**The key insight:** A low sum can mean two completely different things:
1. **Genuine mispricing** — Polymarket priced a divergence that Binance history says rarely happens → enter
2. **Real divergence** — Polymarket correctly detected a coin-specific event → skip

Binance completed ρ is the discriminator. If ρ is high (coins moving together historically), a low sum is mispricing. If ρ is low (coins diverging historically), the low sum is accurate pricing.

**Decision matrix:**

| Binance completed ρ | Polymarket sum | Correct action |
|---|---|---|
| High (ρ > sum) | < threshold | ✅ Enter — genuine mispricing |
| High (ρ > sum) | ≥ threshold | Skip — not enough edge |
| Low (ρ ≤ sum) | < threshold | ❌ Skip — Polymarket is right |
| Low (ρ ≤ sum) | ≥ threshold | Skip — no edge at all |

**The EV guard:** `ev = ρ*(1-sum) - (1-ρ)*sum`. This is exactly zero when `ρ = sum`. A flat `MIN_RHO=0.80` does NOT protect you when sum=0.86 and ρ=0.82 (negative EV). The per-signal EV guard is mandatory.

---

### Lesson 3: Bootstrap Eliminates the 5-Hour Cold Start

**What happened (original code):** The `CandleTracker` only learned ρ from live window transitions. Each 15-min window = 1 new data point. With `RHO_LOOKBACK=20`, the bot needed 20 windows (= 5 hours) before ρ was available. It refused to trade for the first 5 hours of every session.

**The fix:** `bootstrap_candle_history()` fetches the last 20+ completed klines from Binance REST API at startup. The bot has a valid ρ estimate within seconds of starting — ready to trade on the very first qualifying window.

**Important:** Bootstrap uses `klines[:-1]` — excludes the forming candle, consistent with live operation.

---

## Configuration Reference — Pairs Bot v6

Full `.env` settings for `python bot.py pairs`:

```bash
# ── Pairs Bot v6 ─────────────────────────────────────────────────────────
PAIRS_V6=BTC:ETH                  # Pairs to trade (coin_a:coin_b, comma-separated)
                                  # Phase 1: BTC:ETH only
                                  # Phase 2: BTC:ETH,ETH:SOL,BTC:SOL

# Entry threshold — enter when min(sum_1, sum_2) < this
# Conservative: 0.86  |  Moderate: 0.88  |  Aggressive: 0.90
PAIRS_ENTRY_THRESHOLD=0.88

# Binance rolling ρ guard — completed candles only (NOT forming candle)
PAIRS_MIN_RHO=0.80                # Hard floor. EV guard (ρ > sum) is applied on top.
PAIRS_RHO_LOOKBACK=20             # 20 × 15min = 5 hours of history (bootstrapped at startup)

# Leg price range — both legs must be in this range
PAIRS_MIN_LEG_PRICE=0.15          # Below this = near certain loss, skip
PAIRS_MAX_LEG_PRICE=0.85          # Above this = near certain win, spread gone

# Timing — min time remaining in window to enter
PAIRS_MIN_SECS_REMAINING=300      # 5 min — completed candle is meaningful, not forming

# Bankroll and Kelly sizing
PAIRS_BANKROLL=100.0              # Set to actual Polymarket balance
PAIRS_MAX_BET=15.0                # Max total spend per pairs trade (both legs combined)
PAIRS_MIN_BET=5.0                 # Min total spend per pairs trade
PAIRS_KELLY_FRACTION=0.25         # Quarter-Kelly — conservative for Phase 1
PAIRS_DAILY_LOSS_LIMIT=30.0       # Stop for the day after this loss

PAIRS_DATA_DIR=data/pairs         # State + trade log directory
```

### Status Panel

The terminal status panel refreshes every second (same pattern as v5 maker). Example layout:

```
PAIRS BOT v6  14:35:02 UTC  window=14:35  T-4:22

BTC/ETH  ρ=0.947  (20/20 completed candles)

BTC  ↑$0.55  ↓$0.45   |   ETH  ↑$0.49  ↓$0.51

polls=142 errors=0 tokens=4

BTC↑+ETH↓=1.040   BTC↓+ETH↑=0.940   best=0.940  edge=6.0%

▶ SIGNALS (1)
  BTC:ETH  BTC_down@0.450 + ETH_up@0.490  sum=0.940  edge=6.0%  ρ=0.95

Open Positions (1)
  BTC:ETH  BTC_down@0.448 + ETH_up@0.487  cost=$14.84  shares=29  ENTERED | closes T-3:51

Bankroll: $100.00  P&L: +0.00  W/L: 0/0 (0%)  Daily loss: $0.00/$30
Bands: no trades yet
```

**What each section means:**

| Section | Description |
|---------|-------------|
| Header | Bot name, UTC time, current window start time, countdown to window close |
| `ρ=X.XXX (N/N candles)` | Binance rolling correlation on completed candles. Green ≥ MIN_RHO, yellow ≥ 0.70, red below |
| Per-coin odds row | Live UP/DOWN prices from Polymarket CLOB. Green if favored (>0.50), red if not. `(stale)` if no price received |
| Feed stats row | `polls=N` = REST heartbeat cycles, `errors=N` = failed polls, `tokens=N` = subscribed token IDs |
| Sums row | Both directional combos + best edge. Green if below entry threshold |
| SIGNALS | Active entry opportunities that passed all guards this tick |
| Open Positions | Current live positions with fill prices, cost, shares, and countdown to resolution |
| Bankroll | Running P&L, W/L record, daily loss vs limit |
| Bands | Per-band win rates (deep/medium/shallow) — shows auto-cap status once 10+ trades |

### Telegram Alerts

| Event | Message |
|-------|---------|
| New window boundary | `📡 Pairs v6 — new 5m window [14:35 UTC]` + pair names + token subscription confirmation |
| Trade entered | Pair, directions, prices, cost, shares, sum, edge, ρ |
| Resolution — WIN_BOTH | Both legs won. Payout, cost, profit |
| Resolution — WIN_A / WIN_B | One leg won. Payout, cost, profit |
| Resolution — LOSS | Both lost. Cost, loss amount |
| Daily loss limit hit | Alert + session paused message |

### Kelly sizing at a glance

```
f* = (ρ × b - (1-ρ)) / b   where b = edge/sum = (1-sum)/sum

Break-even: ρ = sum  (exact identity — sum=0.86 needs ρ > 0.86 to have positive EV)

Example: ρ=0.95, sum=0.86, balance=$100, quarter-Kelly
  b = 0.14/0.86 = 0.163
  raw_kelly = (0.95×0.163 - 0.05) / 0.163 = 0.643  (64% full Kelly)
  quarter-Kelly spend = 0.25 × 0.643 × $100 = $16.07
  → capped at MAX_BET=$15

After 10+ trades per band (deep/medium/shallow), Kelly switches from
theoretical ρ to observed band win rate — same auto-cap logic as v5.
```

---

## Switching Between 15-min and 5-min Markets

Polymarket runs both `btc-updown-15m-{ts}` and `btc-updown-5m-{ts}` simultaneously.

**One `.env` line controls the pairs bot window — no code changes required:**

```bash
PAIRS_MARKET_WINDOW=15   # default: 96 windows/day, 5h of ρ history at 20 candles
PAIRS_MARKET_WINDOW=5    # faster: 288 windows/day, 100 min of ρ history at 20 candles
```

The bot patches `crypto_markets.COIN_SLUGS` and `WINDOW_SECONDS` at process start. The v5 maker is unaffected.

**Adjust these `.env` values when switching to 5-min:**

```bash
PAIRS_MIN_SECS_REMAINING=120      # 2 min is enough (was 5 min)
PAIRS_ENTRY_THRESHOLD=0.86        # tighter — more windows means more selectivity
```

**And tighten stale detection in `poly_feed.py`:**
```python
STALE_THRESHOLD = 30   # was 60 — a 5-min window can't afford 60s stale prices
```

**Why this approach (patch at runtime, not edit source):**
- v5 maker hardcodes 15-min in `crypto_markets.py` — changing the source file would break v5
- Running both bots simultaneously (15-min maker + 5-min pairs) requires independent window configs per process
- The runtime patch is transparent: `WINDOW_SECONDS` and `COIN_SLUGS` are module-level globals, so all functions that import them (like `discover_market`) automatically use the patched values

**Validation sequence before switching:**
1. Run 15-min for 50+ trades to confirm ρ guard and Kelly sizing are correct
2. Measure actual hit rate (how often `best_sum < threshold`) in 15-min data
3. Switch to 5-min, monitor first 20 window transitions for WS zombie symptoms
4. Check `poly_feed.stats` in status panel — if `polls` counter is growing but `price_lag` is high, REST heartbeat needs tightening

And in `arb_engine_v6_pairs.py`, the Binance kline bootstrap must use `"5m"` interval:
```python
# bootstrap_candle_history() — change the interval param
params={"symbol": sym.upper(), "interval": "5m", "limit": lookback + 1}
```

### Why 5-min is harder

| | 15-min | 5-min |
|---|---|---|
| Windows per day | 96 | 288 |
| Potential trades | ~9 | ~27 |
| WS reconnects per day | 96 | 288 |
| Zombie recovery window | 15 min | 5 min |
| REST heartbeat budget | 15s interval | 10s interval |
| Forming candle noise | Lower | Higher (less time to settle) |
| ρ history per hour | 4 candles | 12 candles |

The WS zombie problem becomes 3× more disruptive at 5-min frequency. The REST heartbeat is more critical. **Validate on 15-min first, then migrate to 5-min** once the resolution logic and orphan handling are confirmed correct through multiple sessions.

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

"""
Delta-Neutral Pairs Strategy v6.0 — Mode B

Non-directional: buy two correlated tokens that capture the same directional outcome.
Win whether both go UP or both go DOWN (~95% of the time).
Lose only if they genuinely diverge (~5% of the time).

Entry condition (ALL must hold):
  1. Binance rolling ρ > PAIRS_MIN_RHO  (last N *completed* 15-min candles — NOT forming candle)
  2. min(p_a_up + p_b_down, p_a_down + p_b_up) < PAIRS_ENTRY_THRESHOLD
  3. Both leg prices in [MIN_LEG_PRICE, MAX_LEG_PRICE]  (not near resolution)
  4. Time remaining > MIN_SECS_REMAINING
  5. No existing position for this pair in the current window

Key insight: Low Polymarket sum + HIGH Binance ρ = genuine mispricing (enter).
             Low Polymarket sum + LOW  Binance ρ = real divergence (skip).
             The current *forming* Binance candle is noise — use completed candles only.

Execution: Aggressive GTC at ask price crosses the book immediately (effective taker).

Run:  python bot.py pairs
"""

import asyncio
import json
import math
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table

from binance_feed import feed as binance_feed, connect_binance, get_initial_prices, SYMBOLS
import crypto_markets
from crypto_markets import discover_market, CryptoMarket, get_current_window_timestamp
from poly_feed import poly_feed, poll_poly_prices
from trader import init_client
from vpn import ensure_vpn
import telegram_alerts as tg
from redeemer import check_and_redeem

# Load .env before reading any config — including PAIRS_MARKET_WINDOW
load_dotenv()

# Apply pairs-only window size — never touches v5 maker config
_pairs_window = int(os.getenv("PAIRS_MARKET_WINDOW", "15"))
if _pairs_window not in (5, 15):
    raise ValueError(f"PAIRS_MARKET_WINDOW must be 5 or 15, got {_pairs_window}")
if _pairs_window != 15:
    crypto_markets.WINDOW_SECONDS = _pairs_window * 60
    crypto_markets.COIN_SLUGS = {
        coin: slug.replace("-15m", f"-{_pairs_window}m")
        for coin, slug in crypto_markets.COIN_SLUGS.items()
    }

WINDOW_SECONDS = crypto_markets.WINDOW_SECONDS
COIN_SLUGS     = crypto_markets.COIN_SLUGS

# ── Logging ───────────────────────────────────────────────────────────────

class _Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, data):
        for s in self.streams: s.write(data)
    def flush(self):
        for s in self.streams: s.flush()
    def fileno(self): return self.streams[0].fileno()

_log_file = Path(__file__).parent / "pairs.log"
_log_fh = open(_log_file, "a", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

console = Console()

# ── Config ────────────────────────────────────────────────────────────────

PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
FUNDER         = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))
VPN_REQUIRED   = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# Pairs — "BTC:ETH,ETH:SOL" etc.
_raw_pairs = os.getenv("PAIRS_V6", "BTC:ETH")
PAIRS: list[tuple[str, str]] = [
    tuple(p.strip().split(":")) for p in _raw_pairs.split(",") if ":" in p
]

ENTRY_THRESHOLD   = float(os.getenv("PAIRS_ENTRY_THRESHOLD", "0.90"))
TAKER_FEE         = float(os.getenv("PAIRS_TAKER_FEE", "0.02"))   # 2% per leg × 2 legs = 4% total cost
MIN_RHO           = float(os.getenv("PAIRS_MIN_RHO", "0.80"))
RHO_LOOKBACK      = int(os.getenv("PAIRS_RHO_LOOKBACK", "20"))
MIN_LEG_PRICE     = float(os.getenv("PAIRS_MIN_LEG_PRICE", "0.15"))
MAX_LEG_PRICE     = float(os.getenv("PAIRS_MAX_LEG_PRICE", "0.85"))
MIN_SECS_REMAINING = int(os.getenv("PAIRS_MIN_SECS_REMAINING", "300"))

STARTING_BANKROLL = float(os.getenv("PAIRS_BANKROLL", "100.0"))
MAX_BET           = float(os.getenv("PAIRS_MAX_BET", "15.0"))
MIN_BET           = float(os.getenv("PAIRS_MIN_BET", "5.0"))
KELLY_FRACTION    = float(os.getenv("PAIRS_KELLY_FRACTION", "0.25"))  # quarter-Kelly
DAILY_LOSS_LIMIT  = float(os.getenv("PAIRS_DAILY_LOSS_LIMIT", "30.0"))

DATA_DIR = Path(os.getenv("PAIRS_DATA_DIR", "data/pairs"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE  = DATA_DIR / "pairs_state.json"
TRADES_FILE = DATA_DIR / "pairs_trades.jsonl"

# ── Hot-reload config (watches .env for changes every 15s) ──────────────────
_ENV_FILE  = Path(__file__).parent / ".env"
_env_mtime: float = 0.0


def _reload_config() -> None:
    """Re-read tunable .env vars without restarting. No-op if file unchanged."""
    global _env_mtime
    global ENTRY_THRESHOLD, TAKER_FEE, MIN_RHO, RHO_LOOKBACK, MIN_LEG_PRICE, MAX_LEG_PRICE
    global MIN_SECS_REMAINING, MAX_BET, MIN_BET, KELLY_FRACTION, DAILY_LOSS_LIMIT
    global EWMA_DECAY

    try:
        mtime = _ENV_FILE.stat().st_mtime
    except OSError:
        return
    if mtime == _env_mtime:
        return  # unchanged

    load_dotenv(override=True)
    _env_mtime = mtime

    prev = dict(
        ENTRY_THRESHOLD=ENTRY_THRESHOLD, TAKER_FEE=TAKER_FEE,
        MIN_RHO=MIN_RHO, RHO_LOOKBACK=RHO_LOOKBACK,
        MIN_LEG_PRICE=MIN_LEG_PRICE, MAX_LEG_PRICE=MAX_LEG_PRICE,
        MIN_SECS_REMAINING=MIN_SECS_REMAINING,
        MAX_BET=MAX_BET, MIN_BET=MIN_BET,
        KELLY_FRACTION=KELLY_FRACTION, DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
        EWMA_DECAY=EWMA_DECAY,
    )

    ENTRY_THRESHOLD    = float(os.getenv("PAIRS_ENTRY_THRESHOLD",   "0.90"))
    TAKER_FEE          = float(os.getenv("PAIRS_TAKER_FEE",         "0.02"))
    MIN_RHO            = float(os.getenv("PAIRS_MIN_RHO",           "0.80"))
    RHO_LOOKBACK       = int(  os.getenv("PAIRS_RHO_LOOKBACK",      "20"))
    MIN_LEG_PRICE      = float(os.getenv("PAIRS_MIN_LEG_PRICE",     "0.15"))
    MAX_LEG_PRICE      = float(os.getenv("PAIRS_MAX_LEG_PRICE",     "0.85"))
    MIN_SECS_REMAINING = int(  os.getenv("PAIRS_MIN_SECS_REMAINING","300"))
    MAX_BET            = float(os.getenv("PAIRS_MAX_BET",           "15.0"))
    MIN_BET            = float(os.getenv("PAIRS_MIN_BET",           "5.0"))
    KELLY_FRACTION     = float(os.getenv("PAIRS_KELLY_FRACTION",    "0.25"))
    DAILY_LOSS_LIMIT   = float(os.getenv("PAIRS_DAILY_LOSS_LIMIT",  "30.0"))
    EWMA_DECAY         = float(os.getenv("PAIRS_EWMA_DECAY",        "0.90"))

    new = dict(
        ENTRY_THRESHOLD=ENTRY_THRESHOLD, TAKER_FEE=TAKER_FEE,
        MIN_RHO=MIN_RHO, RHO_LOOKBACK=RHO_LOOKBACK,
        MIN_LEG_PRICE=MIN_LEG_PRICE, MAX_LEG_PRICE=MAX_LEG_PRICE,
        MIN_SECS_REMAINING=MIN_SECS_REMAINING,
        MAX_BET=MAX_BET, MIN_BET=MIN_BET,
        KELLY_FRACTION=KELLY_FRACTION, DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
        EWMA_DECAY=EWMA_DECAY,
    )
    changes = [f"{k}: {prev[k]} → {new[k]}" for k in prev if new[k] != prev[k]]
    if changes:
        msg = "⚙️ Config reloaded: " + " | ".join(changes)
        console.print(f"[cyan]{msg}[/cyan]")
        tg.send_message(msg)


def _start_config_watcher() -> None:
    def _loop():
        while True:
            time.sleep(2)
            try:
                _reload_config()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name="config-watcher").start()


def _log_trade(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps({"ts": ts, "msg": msg}) + "\n")


# ── Rolling Correlation Tracker ───────────────────────────────────────────

# EWMA decay factor — controls how fast old candles lose influence.
# λ=0.90: half-life ≈ 6 candles (30 min on 5-min, 1.5h on 15-min)
# λ=0.94: half-life ≈ 11 candles (55 min on 5-min, 2.75h on 15-min)
# Lower = more reactive to recent regime changes, higher = smoother/more stable.
EWMA_DECAY = float(os.getenv("PAIRS_EWMA_DECAY", "0.90"))


def _ewma_pearson(xs: list[float], ys: list[float], decay: float = EWMA_DECAY) -> float | None:
    """
    EWMA-weighted Pearson correlation.

    Weights: w_i = decay^(n-1-i)  — oldest candle has weight decay^(n-1),
    newest candle has weight 1.0 (decay^0).

    Why EWMA over simple Pearson:
    - Equal-weight Pearson treats a candle from 100 min ago the same as 5 min ago.
    - If BTC/ETH correlation breaks mid-session (news, divergence), simple Pearson
      takes 10+ candles to reflect it. EWMA detects it in 2-3 candles.
    - Half-life at λ=0.90 over 20 candles: recent candle weighs ~10× more than
      the oldest, so a regime change registers almost immediately.
    """
    n = len(xs)
    if n < 5:
        return None
    weights = [decay ** (n - 1 - i) for i in range(n)]
    w_sum = sum(weights)
    mx = sum(w * x for w, x in zip(weights, xs)) / w_sum
    my = sum(w * y for w, y in zip(weights, ys)) / w_sum
    num  = sum(w * (x - mx) * (y - my) for w, x, y in zip(weights, xs, ys))
    den_x = sum(w * (x - mx) ** 2 for w, x in zip(weights, xs))
    den_y = sum(w * (y - my) ** 2 for w, y in zip(weights, ys))
    den = math.sqrt(den_x * den_y)
    return num / den if den > 0 else 0.0


@dataclass
class CandleTracker:
    """
    Records completed Binance candle returns per coin.
    On each window transition: snapshots the window-open price and appends the
    completed return (close/open - 1) to the rolling history.

    Correlation is computed via EWMA-weighted Pearson (not simple equal-weight).
    Recent candles carry more weight — regime changes are detected in 2-3 candles
    rather than 10+.

    IMPORTANT: Never use the *current forming* candle for ρ — it hasn't closed yet
    and can reverse. Only completed candles are reliable. This is the lesson from
    the live test where BTC+ETH both looked green mid-candle but ETH reversed by close.
    """
    _window_open: dict = field(default_factory=dict)   # coin -> (ts, price)
    _returns: dict     = field(default_factory=dict)   # coin -> list[float]

    def on_new_window(self, coin: str, new_ts: int, new_price: float | None):
        """Call at every window boundary for every tracked coin."""
        if new_price is None:
            return
        prev = self._window_open.get(coin)
        if prev is not None:
            prev_ts, prev_price = prev
            if prev_price > 0:
                ret = (new_price - prev_price) / prev_price
                buf = self._returns.setdefault(coin, [])
                buf.append(ret)
                if len(buf) > RHO_LOOKBACK:
                    buf.pop(0)
        self._window_open[coin] = (new_ts, new_price)

    def get_rho(self, coin_a: str, coin_b: str) -> float | None:
        """EWMA-weighted Pearson ρ between coin_a and coin_b on completed windows."""
        ra = self._returns.get(coin_a, [])
        rb = self._returns.get(coin_b, [])
        n = min(len(ra), len(rb))
        if n < 5:
            return None
        return _ewma_pearson(ra[-n:], rb[-n:])

    def n_samples(self, coin: str) -> int:
        return len(self._returns.get(coin, []))


candle_tracker = CandleTracker()


def bootstrap_candle_history(coins: list[str], lookback: int = RHO_LOOKBACK):
    """
    Fetch last `lookback` completed 15-min Binance klines via REST at startup.
    Populates CandleTracker immediately so ρ is available on the first window
    without waiting hours for live candles to accumulate.

    Excludes the current forming (open) candle — only closed candles are used.
    """
    from binance_feed import SYMBOLS
    KLINES_URL = "https://data-api.binance.vision/api/v3/klines"

    console.print("[dim]Bootstrapping candle history from Binance REST...[/dim]")
    for coin in coins:
        sym = SYMBOLS.get(coin)
        if not sym:
            continue
        try:
            resp = requests.get(
                KLINES_URL,
                params={"symbol": sym.upper(), "interval": f"{_pairs_window}m", "limit": lookback + 1},
                timeout=10,
            )
            resp.raise_for_status()
            klines = resp.json()
            # Each kline: [open_time, open, high, low, close, ...]
            # Exclude the last entry (current forming candle)
            completed = klines[:-1]
            for k in completed:
                open_ts  = int(k[0]) // 1000   # ms → seconds
                open_p   = float(k[1])
                close_p  = float(k[4])
                # Inject as completed candle: record open then "transition" at close
                candle_tracker._window_open[coin] = (open_ts, open_p)
                ret = (close_p - open_p) / open_p if open_p > 0 else 0.0
                buf = candle_tracker._returns.setdefault(coin, [])
                buf.append(ret)
                if len(buf) > lookback:
                    buf.pop(0)
            n = len(candle_tracker._returns.get(coin, []))
            console.print(f"  [dim]{coin}: {n} completed candles loaded[/dim]")
        except Exception as e:
            console.print(f"  [yellow]{coin}: kline bootstrap failed — {e}[/yellow]")


# ── Bankroll ──────────────────────────────────────────────────────────────

def _sum_band(best_sum: float) -> str:
    """Bucket trade by sum into a named band for win-rate tracking."""
    if best_sum < 0.80:  return "deep"    # >20% edge
    if best_sum < 0.86:  return "medium"  # 14-20% edge
    return "shallow"                       # 10-14% edge


@dataclass
class PairsBankroll:
    balance: float
    wins: int = 0
    losses: int = 0
    daily_loss: float = 0.0
    band_wins:   dict = field(default_factory=lambda: {"deep": 0, "medium": 0, "shallow": 0})
    band_losses: dict = field(default_factory=lambda: {"deep": 0, "medium": 0, "shallow": 0})

    @property
    def win_rate(self) -> float:
        t = self.wins + self.losses
        return self.wins / t if t > 0 else 0.0

    @property
    def pnl(self) -> float:
        return self.balance - STARTING_BANKROLL

    @property
    def daily_limit_hit(self) -> bool:
        return self.daily_loss >= DAILY_LOSS_LIMIT

    def band_win_rate(self, band: str) -> float | None:
        """Observed win rate for a band. Returns None if < 10 trades (not enough data)."""
        w = self.band_wins.get(band, 0)
        l = self.band_losses.get(band, 0)
        return w / (w + l) if (w + l) >= 10 else None

    def kelly_bet(self, rho: float, best_sum: float) -> float:
        """
        Proper Kelly fraction for a pairs trade, with ρ and observed win rate adjustments.

        Kelly for pairs bet:
          b = net win per unit stake = edge / sum = (1-sum) / sum
          p = win prob ≈ ρ  (one leg wins when both coins move same direction)
          q = 1 - ρ         (both legs lose when coins diverge)

          f* = (p×b - q) / b  =  ρ - (1-ρ)×sum/edge

        Key insight: break-even ρ = sum exactly.
          sum=0.86 requires ρ > 0.86 to have positive EV.
          sum=0.78 requires ρ > 0.78 to have positive EV.

        If observed band win rate (≥10 trades) diverges from theoretical ρ,
        we use the observed rate instead — same auto-cap logic as v5.
        """
        edge = 1.0 - best_sum
        b    = edge / best_sum  # net win per unit stake

        # Use observed band win rate if we have enough data, else use ρ
        band = _sum_band(best_sum)
        obs_wr = self.band_win_rate(band)
        p = obs_wr if obs_wr is not None else rho
        q = 1.0 - p

        raw_kelly = (p * b - q) / b   # proper Kelly
        if raw_kelly <= 0:
            return MIN_BET  # negative EV at current ρ — bet minimum

        # Scale down when ρ is below 0.95 baseline
        rho_factor = min(1.0, max(0.2, rho / 0.95))

        spend = KELLY_FRACTION * raw_kelly * rho_factor * self.balance
        return round(min(MAX_BET, max(MIN_BET, spend)), 2)

    def ev_check(self, rho: float, best_sum: float) -> float:
        """
        Expected value per dollar spent. Must be positive to enter.
        EV = ρ×(1-sum) - (1-ρ)×sum
        Break-even: ρ = sum  (exact mathematical identity)
        """
        return rho * (1.0 - best_sum) - (1.0 - rho) * best_sum

    def band_stats(self) -> str:
        parts = []
        for band in ("deep", "medium", "shallow"):
            w = self.band_wins.get(band, 0)
            l = self.band_losses.get(band, 0)
            if w + l == 0:
                continue
            wr = w / (w + l)
            flag = " ⚠️" if (w + l >= 10 and wr < 0.85) else ""
            parts.append(f"{band}:{w}/{w+l}({wr:.0%}{flag})")
        return "  ".join(parts) if parts else "no trades yet"

    def record_result(self, cost_a: float, cost_b: float, payout: float, pair: str, outcome: str, band: str = ""):
        total_cost = cost_a + cost_b
        profit = payout - total_cost
        self.balance += profit
        if profit >= 0:
            self.wins += 1
            if band:
                self.band_wins[band] = self.band_wins.get(band, 0) + 1
        else:
            self.losses += 1
            self.daily_loss += abs(profit)
            if band:
                self.band_losses[band] = self.band_losses.get(band, 0) + 1
        self.save()
        _log_trade(
            f"{outcome}: {pair} band={band} cost=${total_cost:.2f} payout=${payout:.2f} "
            f"profit={profit:+.2f} balance=${self.balance:.2f}"
        )
        emoji = "✅" if profit >= 0 else "💔"
        tg.send_message(
            f"{emoji} *PAIRS {outcome}* {pair}\n"
            f"Cost ${total_cost:.2f} → Payout ${payout:.2f}\n"
            f"Profit: {profit:+.2f}  Band: {band}  Bankroll: ${self.balance:.2f}"
        )

    def save(self):
        STATE_FILE.write_text(json.dumps({
            "balance":     self.balance,
            "wins":        self.wins,
            "losses":      self.losses,
            "daily_loss":  self.daily_loss,
            "band_wins":   self.band_wins,
            "band_losses": self.band_losses,
            "saved_at":    time.time(),
        }, indent=2))

    @classmethod
    def load(cls) -> "PairsBankroll":
        try:
            if STATE_FILE.exists():
                d = json.loads(STATE_FILE.read_text())
                age = time.time() - d.get("saved_at", 0)
                daily_loss = d.get("daily_loss", 0.0) if age < 86400 else 0.0
                b = cls(
                    balance=d.get("balance", STARTING_BANKROLL),
                    wins=d.get("wins", 0),
                    losses=d.get("losses", 0),
                    daily_loss=daily_loss,
                )
                b.band_wins   = d.get("band_wins",   b.band_wins)
                b.band_losses = d.get("band_losses", b.band_losses)
                return b
        except Exception:
            pass
        return cls(balance=STARTING_BANKROLL)


# ── Position ──────────────────────────────────────────────────────────────

@dataclass
class PairsPosition:
    pair: str
    window_ts: int
    coin_a: str
    coin_b: str
    direction_a: str      # "up" or "down"
    direction_b: str
    token_a: str
    token_b: str
    fill_price_a: float
    fill_price_b: float
    shares: float         # equal shares on both legs
    cost_a: float
    cost_b: float
    best_sum: float
    rho: float
    band: str = ""        # sum band at entry — for win-rate tracking
    entered_at: float = field(default_factory=time.time)
    status: str = "open"  # open | won | lost | orphan

    @property
    def total_cost(self) -> float:
        return self.cost_a + self.cost_b

    @property
    def is_orphan(self) -> bool:
        return self.status == "orphan"


# ── Order Execution ───────────────────────────────────────────────────────

def _best_ask(token_id: str) -> float | None:
    """Fetch best ask from CLOB order book."""
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        book = resp.json()
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        real = [a for a in asks if 0.01 < float(a["price"]) < 0.99]
        return float(real[0]["price"]) if real else None
    except Exception:
        return None


async def _place_aggressive_buy(
    client, token_id: str, shares: float, poly_price: float, label: str
) -> tuple[bool, float]:
    """
    Place aggressive GTC buy at ask + 2 ticks — crosses the book immediately.
    Returns (success, fill_price).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    ask = _best_ask(token_id)
    fill_price = round(min(0.98, (ask or poly_price) + 0.002), 4)
    size = max(5.0, round(shares, 2))

    try:
        signed = client.create_order(OrderArgs(
            token_id=token_id,
            price=fill_price,
            size=size,
            side=BUY,
        ))
        resp = client.post_order(signed, OrderType.GTC)
        success = isinstance(resp, dict) and resp.get("success", True)
        if success:
            console.print(f"  [green]✅ {label}: {size:.0f} shares @ ${fill_price:.4f}[/green]")
        else:
            console.print(f"  [red]❌ {label}: rejected — {resp}[/red]")
        return success, fill_price
    except Exception as e:
        console.print(f"  [red]❌ {label} failed: {e}[/red]")
        return False, 0.0


async def enter_pairs(
    client,
    coin_a: str, coin_b: str,
    market_a: CryptoMarket, market_b: CryptoMarket,
    direction_a: str, direction_b: str,
    price_a: float, price_b: float,
    best_sum: float, rho: float,
    bankroll: PairsBankroll, window_ts: int,
) -> PairsPosition | None:
    """Place both legs. Returns position if Leg A fills (Leg B failure = orphan)."""

    band  = _sum_band(best_sum)
    spend = bankroll.kelly_bet(rho, best_sum)
    # Equal shares: N = spend / sum  (so each leg costs N × price_x = asymmetric dollars)
    shares = max(5.0, round(spend / best_sum, 2))

    token_a = market_a.up_token_id   if direction_a == "up" else market_a.down_token_id
    token_b = market_b.up_token_id   if direction_b == "up" else market_b.down_token_id
    pair    = f"{coin_a}:{coin_b}"

    # Pre-flight: validate CLOB asks before committing — feed prices can lag the book
    ask_a = _best_ask(token_a)
    ask_b = _best_ask(token_b)
    if ask_a is not None and ask_b is not None:
        clob_sum = round((ask_a + 0.005) + (ask_b + 0.005), 4)
        if clob_sum >= 1.0:
            console.print(
                f"  [yellow]⚠ CLOB sum={clob_sum:.3f} ≥ 1.00 — feed lagged, skipping entry[/yellow]"
            )
            return None

    console.print(f"\n[bold cyan]▶ PAIRS ENTRY {pair}[/bold cyan]")
    console.print(
        f"  {coin_a} {direction_a.upper()} @ {price_a:.3f}  +  "
        f"{coin_b} {direction_b.upper()} @ {price_b:.3f}"
    )
    console.print(
        f"  sum={best_sum:.3f}  edge={1-best_sum:.1%}  "
        f"ρ={rho:.2f}  shares={shares:.0f}  spend≈${spend:.2f}"
    )
    _log_trade(
        f"ENTRY: {pair} {coin_a}_{direction_a}@{price_a:.3f} "
        f"+ {coin_b}_{direction_b}@{price_b:.3f} "
        f"sum={best_sum:.3f} rho={rho:.2f} spend=${spend:.2f}"
    )

    ok_a, fa = await _place_aggressive_buy(
        client, token_a, shares, price_a, f"Leg A ({coin_a} {direction_a.upper()})"
    )
    if not ok_a:
        console.print("  [yellow]Leg A failed — aborting[/yellow]")
        return None

    ok_b, fb = await _place_aggressive_buy(
        client, token_b, shares, price_b, f"Leg B ({coin_b} {direction_b.upper()})"
    )

    cost_a = fa * shares
    cost_b = fb * shares if ok_b else 0.0
    status  = "open" if ok_b else "orphan"

    if not ok_b:
        console.print("  [red]⚠️  ORPHAN: Leg B failed — monitoring Leg A standalone[/red]")
        tg.send_message(
            f"⚠️ *PAIRS ORPHAN* {pair}\n"
            f"Leg A ({coin_a} {direction_a}) filled, Leg B failed\n"
            f"Tracking standalone"
        )
    else:
        tg.send_message(
            f"🎯 *PAIRS ENTRY* {pair}\n"
            f"{coin_a} {direction_a.upper()} @ ${fa:.3f}\n"
            f"{coin_b} {direction_b.upper()} @ ${fb:.3f}\n"
            f"Sum: {best_sum:.3f}  Edge: {1-best_sum:.1%}  ρ: {rho:.2f}\n"
            f"Shares: {shares:.0f}  Total: ${cost_a + cost_b:.2f}"
        )

    return PairsPosition(
        pair=pair, window_ts=window_ts,
        coin_a=coin_a, coin_b=coin_b,
        direction_a=direction_a, direction_b=direction_b,
        token_a=token_a, token_b=token_b,
        fill_price_a=fa, fill_price_b=fb,
        shares=shares,
        cost_a=cost_a, cost_b=cost_b,
        best_sum=best_sum, rho=rho, band=band,
        status=status,
    )


# ── Signal Detection ──────────────────────────────────────────────────────

@dataclass
class PairsSignal:
    pair: str
    coin_a: str
    coin_b: str
    direction_a: str
    direction_b: str
    price_a: float
    price_b: float
    best_sum: float
    rho: float
    market_a: CryptoMarket
    market_b: CryptoMarket

    @property
    def edge(self) -> float:
        return 1.0 - self.best_sum


def scan_pairs_signals(secs_remaining: int, cached_markets: dict = None) -> list[PairsSignal]:
    """
    Scan all configured pairs for Mode B entry signals.
    Returns signals sorted by best edge (lowest sum = highest edge).
    """
    if secs_remaining < MIN_SECS_REMAINING:
        return []

    signals = []
    for coin_a, coin_b in PAIRS:

        # 1. Binance rolling ρ on completed candles only
        rho = candle_tracker.get_rho(coin_a, coin_b)
        if rho is None or rho < MIN_RHO:
            continue

        # 2. Get current Polymarket prices
        up_a, dn_a = poly_feed.get_market_prices(coin_a)
        up_b, dn_b = poly_feed.get_market_prices(coin_b)
        if None in (up_a, dn_a, up_b, dn_b):
            continue

        # 3. Always take the cheaper direction (see V6_PLAN: Step 0)
        sum_1 = up_a + dn_b    # coin_a UP  + coin_b DOWN
        sum_2 = dn_a + up_b    # coin_a DOWN + coin_b UP

        if sum_1 <= sum_2:
            best_sum, dir_a, dir_b, p_a, p_b = sum_1, "up",   "down", up_a, dn_b
        else:
            best_sum, dir_a, dir_b, p_a, p_b = sum_2, "down", "up",   dn_a, up_b

        # 4. Only the two legs being bought must be in healthy range (not near resolution)
        #    Do NOT check non-traded legs — they can be near 0 or 1 without affecting us
        if not (MIN_LEG_PRICE <= p_a <= MAX_LEG_PRICE and MIN_LEG_PRICE <= p_b <= MAX_LEG_PRICE):
            continue

        if best_sum >= ENTRY_THRESHOLD:
            continue

        # 5. EV check: include taker fees (paid on both legs regardless of outcome)
        #    fee_cost = TAKER_FEE × best_sum  (2% × spend on both legs)
        #    EV = ρ×(1-sum) - (1-ρ)×sum - fee_cost  must be positive
        fee_cost = TAKER_FEE * best_sum
        ev = rho * (1.0 - best_sum) - (1.0 - rho) * best_sum - fee_cost
        if ev <= 0:
            continue  # negative EV after fees — skip

        # 6. Use cached market objects (refreshed every 30s in main loop — no HTTP per tick)
        if not cached_markets:
            continue
        market_a = cached_markets.get(coin_a)
        market_b = cached_markets.get(coin_b)
        if not market_a or not market_b:
            continue

        signals.append(PairsSignal(
            pair=f"{coin_a}:{coin_b}",
            coin_a=coin_a, coin_b=coin_b,
            direction_a=dir_a, direction_b=dir_b,
            price_a=p_a, price_b=p_b,
            best_sum=best_sum, rho=rho,
            market_a=market_a, market_b=market_b,
        ))

    return sorted(signals, key=lambda s: s.best_sum)


# ── Resolution ────────────────────────────────────────────────────────────

def _resolve_market_winner(coin: str, window_ts: int) -> str | None:
    """
    After window close, fetch the old market and determine which side won.
    Returns "up", "down", or None if not yet resolved.
    """
    try:
        prefix = COIN_SLUGS.get(coin)
        if not prefix:
            return None
        slug = f"{prefix}-{window_ts}"
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=8,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return None
        m = markets[0] if isinstance(markets, list) else markets

        # Parse outcomes in declared order (not assumed) — same as parse_market in crypto_markets.py
        raw_outcomes = m.get("outcomes", "[]")
        raw_prices   = m.get("outcomePrices", "[]")
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices   = json.loads(raw_prices)   if isinstance(raw_prices,   str) else raw_prices

        up_idx, down_idx = None, None
        for i, o in enumerate(outcomes):
            if str(o).lower() == "up":   up_idx   = i
            elif str(o).lower() == "down": down_idx = i

        if up_idx is not None and up_idx < len(prices) and float(prices[up_idx]) >= 0.95:
            return "up"
        if down_idx is not None and down_idx < len(prices) and float(prices[down_idx]) >= 0.95:
            return "down"
    except Exception:
        pass
    return None


async def check_resolution(
    positions: list[PairsPosition],
    bankroll: PairsBankroll,
) -> list[PairsPosition]:
    """Check resolved positions and record P&L. Returns still-open positions."""
    still_open = []
    now = time.time()

    for pos in positions:
        window_end = pos.window_ts + WINDOW_SECONDS

        # Only check after window closes + 30s buffer for resolution propagation
        if now < window_end + 30:
            still_open.append(pos)
            continue

        winner_a = _resolve_market_winner(pos.coin_a, pos.window_ts)
        winner_b = _resolve_market_winner(pos.coin_b, pos.window_ts)

        # Wait until BOTH legs have resolved (unless orphan — only leg A matters)
        a_ready = winner_a is not None
        b_ready = winner_b is not None or pos.is_orphan
        if not (a_ready and b_ready):
            if now < window_end + 300:  # give up after 5 min
                still_open.append(pos)
            else:
                console.print(f"[yellow]Resolution timeout for {pos.pair} — skipping[/yellow]")
            continue

        # Determine which legs won
        leg_a_won = (winner_a == pos.direction_a)
        leg_b_won = (winner_b == pos.direction_b) if not pos.is_orphan else False

        # Payout = $1 per share per winning leg
        payout = 0.0
        if leg_a_won:
            payout += pos.shares
        if leg_b_won:
            payout += pos.shares

        outcome = (
            "WIN_BOTH"  if leg_a_won and leg_b_won  else
            "WIN_A"     if leg_a_won                 else
            "WIN_B"     if leg_b_won                 else
            "LOSS"
        )

        console.print(
            f"[{'green' if payout > 0 else 'red'}]"
            f"Resolution {pos.pair}: {outcome}  "
            f"payout=${payout:.2f}  cost=${pos.total_cost:.2f}  "
            f"profit={payout-pos.total_cost:+.2f}[/]"
        )
        bankroll.record_result(pos.cost_a, pos.cost_b, payout, pos.pair, outcome, band=pos.band)

    return still_open


# ── Display Panel ─────────────────────────────────────────────────────────

def _rho_color(rho: float | None) -> str:
    if rho is None: return "dim"
    if rho >= MIN_RHO: return "green"
    if rho >= 0.70: return "yellow"
    return "red"


def build_display(
    bankroll: PairsBankroll,
    positions: list[PairsPosition],
    signals: list[PairsSignal],
    secs_remaining: int,
    window_ts: int,
) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(width=72)

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    mm, ss  = divmod(max(0, secs_remaining), 60)
    win_str = datetime.utcfromtimestamp(window_ts).strftime("%H:%M")

    table.add_row(f"[bold cyan]PAIRS BOT v6[/bold cyan]  [dim]{now_str}[/dim]  "
                  f"window=[cyan]{win_str}[/cyan]  T-[bold]{mm}:{ss:02d}[/bold]")
    table.add_row(f"[dim]threshold={ENTRY_THRESHOLD:.2f}  min_ρ={MIN_RHO:.2f}  "
                  f"min_secs={MIN_SECS_REMAINING}  kelly={KELLY_FRACTION:.0%}[/dim]")
    table.add_row("")

    # Rolling correlations (completed candles only)
    for ca, cb in PAIRS:
        rho = candle_tracker.get_rho(ca, cb)
        n_a = candle_tracker.n_samples(ca)
        n_b = candle_tracker.n_samples(cb)
        n   = min(n_a, n_b)
        rho_str = f"[{_rho_color(rho)}]ρ={rho:.3f}[/]" if rho is not None else f"[dim]ρ=n/a[/dim]"
        table.add_row(f"  {ca}/{cb}  {rho_str}  [dim]({n}/{RHO_LOOKBACK} completed candles)[/dim]")

    table.add_row("")

    # Per-coin Polymarket odds (live prices with stale detection)
    all_pair_coins = list(dict.fromkeys(c for pair in PAIRS for c in pair))
    odds_parts = []
    for coin in all_pair_coins:
        up, dn = poly_feed.get_market_prices(coin)
        if up is not None and dn is not None:
            up_col = "green" if up > 0.5 else "red"
            dn_col = "green" if dn > 0.5 else "red"
            odds_parts.append(
                f"[bold]{coin}[/bold] [{up_col}]↑${up:.2f}[/{up_col}] [{dn_col}]↓${dn:.2f}[/{dn_col}]"
            )
        else:
            odds_parts.append(f"[bold]{coin}[/bold] [dim](stale)[/dim]")
    table.add_row("  " + "   [dim]|[/dim]   ".join(odds_parts))
    table.add_row(f"  [dim]{poly_feed.stats}[/dim]")
    table.add_row("")

    # Current prices and sums for each pair
    for ca, cb in PAIRS:
        up_a, dn_a = poly_feed.get_market_prices(ca)
        up_b, dn_b = poly_feed.get_market_prices(cb)
        if None in (up_a, dn_a, up_b, dn_b):
            table.add_row(f"  [dim]{ca}/{cb}: prices unavailable[/dim]")
            continue

        sum_1 = round(up_a + dn_b, 3)
        sum_2 = round(dn_a + up_b, 3)
        best  = min(sum_1, sum_2)
        bc    = "green" if best < ENTRY_THRESHOLD else "yellow" if best < 0.95 else "dim"

        table.add_row(
            f"  {ca}↑+{cb}↓=[dim]{sum_1:.3f}[/dim]   "
            f"{ca}↓+{cb}↑=[dim]{sum_2:.3f}[/dim]   "
            f"best=[{bc}]{best:.3f}[/{bc}]  edge=[{bc}]{1-best:.1%}[/{bc}]"
        )

    table.add_row("")

    # Active signals
    if signals:
        table.add_row(f"[bold green]▶ SIGNALS ({len(signals)})[/bold green]")
        for sig in signals:
            table.add_row(
                f"  {sig.pair}  {sig.coin_a}_{sig.direction_a}@{sig.price_a:.3f} + "
                f"{sig.coin_b}_{sig.direction_b}@{sig.price_b:.3f}  "
                f"sum=[green]{sig.best_sum:.3f}[/green]  "
                f"edge=[green]{sig.edge:.1%}[/green]  ρ={sig.rho:.2f}"
            )
        table.add_row("")

    # Open positions
    if positions:
        table.add_row(f"[bold]Open Positions ({len(positions)})[/bold]")
        for pos in positions:
            age = int(time.time() - pos.entered_at)
            win_close = pos.window_ts + WINDOW_SECONDS
            secs_to_close = max(0, int(win_close - time.time()))
            mm2, ss2 = divmod(secs_to_close, 60)
            table.add_row(
                f"  {pos.pair}  {pos.coin_a}_{pos.direction_a}@{pos.fill_price_a:.3f} + "
                f"{pos.coin_b}_{pos.direction_b}@{pos.fill_price_b:.3f}  "
                f"cost=${pos.total_cost:.2f}  shares={pos.shares:.0f}  "
                f"[dim]{pos.status} | closes T-{mm2}:{ss2:02d}[/dim]"
            )
        table.add_row("")

    # Bankroll
    pnl_c = "green" if bankroll.pnl >= 0 else "red"
    table.add_row(
        f"Bankroll: [bold]${bankroll.balance:.2f}[/bold]  "
        f"P&L: [{pnl_c}]{bankroll.pnl:+.2f}[/{pnl_c}]  "
        f"W/L: {bankroll.wins}/{bankroll.losses} ({bankroll.win_rate:.0%})  "
        f"Daily loss: ${bankroll.daily_loss:.2f}/${DAILY_LOSS_LIMIT:.0f}"
    )
    table.add_row(f"[dim]Bands: {bankroll.band_stats()}[/dim]")

    return table


# ── Main Loop ─────────────────────────────────────────────────────────────

async def run_pairs_bot():
    console.print("[bold cyan]━━━ PAIRS BOT v6 — Delta-Neutral Mode B ━━━[/bold cyan]")
    console.print()

    _start_config_watcher()

    if VPN_REQUIRED and not ensure_vpn(required=True):
        return

    if not PRIVATE_KEY:
        console.print("[red]ERROR: PRIVATE_KEY not set in .env[/red]")
        return

    client = init_client(
        private_key=PRIVATE_KEY,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER if FUNDER else None,
    )

    bankroll = PairsBankroll.load()
    console.print(f"Bankroll: ${bankroll.balance:.2f}  W/L: {bankroll.wins}/{bankroll.losses}")
    console.print(f"Pairs: {PAIRS}")
    console.print(f"Entry threshold: {ENTRY_THRESHOLD}  Min ρ: {MIN_RHO}  Lookback: {RHO_LOOKBACK} candles")
    console.print()

    # Fetch initial Binance prices (REST, before WS connects)
    console.print("[dim]Fetching initial Binance prices...[/dim]")
    get_initial_prices()

    # Bootstrap ρ from historical klines — ready to trade on first window
    all_coins = list({c for pair in PAIRS for c in pair})
    bootstrap_candle_history(all_coins, lookback=RHO_LOOKBACK)

    # Log bootstrap result
    for ca, cb in PAIRS:
        rho = candle_tracker.get_rho(ca, cb)
        n   = min(candle_tracker.n_samples(ca), candle_tracker.n_samples(cb))
        console.print(
            f"  [dim]{ca}/{cb}: ρ={'N/A' if rho is None else f'{rho:.3f}'}  "
            f"({n}/{RHO_LOOKBACK} candles)[/dim]"
        )
    console.print()

    # Start feeds
    asyncio.create_task(connect_binance())
    asyncio.create_task(poll_poly_prices(coins=all_coins, interval=5.0))

    await asyncio.sleep(3)  # let WS settle

    # Seed candle tracker with current window start price (live)
    current_ts = get_current_window_timestamp()
    for coin in all_coins:
        candle_tracker.on_new_window(coin, current_ts, binance_feed.get_price(coin))

    prev_ts  = current_ts
    positions: list[PairsPosition] = []
    traded_this_window: set[str]   = set()  # pairs entered in current window

    console.print("[green]Started. Monitoring pairs...[/green]\n")

    last_status_print = 0.0
    last_market_refresh = 0.0
    cached_markets: dict[str, CryptoMarket] = {}  # coin -> market, refreshed every 30s

    try:
        while True:
            now        = time.time()
            current_ts = get_current_window_timestamp()
            secs_left  = int(current_ts + WINDOW_SECONDS - now)

            # Window transition
            if current_ts != prev_ts:
                win_label = datetime.fromtimestamp(current_ts, tz=timezone.utc).strftime('%H:%M UTC')
                console.print(
                    f"\n[cyan]── New window: {win_label} ──[/cyan]"
                )
                for coin in all_coins:
                    candle_tracker.on_new_window(coin, current_ts, binance_feed.get_price(coin))
                prev_ts = current_ts
                traded_this_window.clear()
                cached_markets.clear()  # force refresh on new window

                # Telegram: notify new window + fresh token subscriptions
                pair_strs = ", ".join(f"{ca}/{cb}" for ca, cb in PAIRS)
                tg.send_message(
                    f"📡 Pairs v6 — new {_pairs_window}m window [{win_label}]\n"
                    f"Pairs: {pair_strs}\n"
                    f"Subscribing to fresh Polymarket token IDs for this window."
                )

            # Refresh cached market objects every 30s (avoids blocking HTTP on every tick)
            if now - last_market_refresh > 30:
                for coin in all_coins:
                    m = discover_market(coin)
                    if m:
                        cached_markets[coin] = m
                last_market_refresh = now

            # Auto-redeem resolved winning positions (non-blocking background thread)
            if FUNDER and PRIVATE_KEY:
                redeemed = check_and_redeem(FUNDER, PRIVATE_KEY)
                if redeemed > 0:
                    bankroll.balance += redeemed

            # Daily loss guard
            if bankroll.daily_limit_hit:
                console.print("[red]Daily loss limit hit — paused for today[/red]")
                await asyncio.sleep(60)
                continue

            # Resolve old positions
            positions = await check_resolution(positions, bankroll)

            # Scan for signals (uses cached markets — no HTTP per tick)
            signals = scan_pairs_signals(secs_left, cached_markets)

            # Enter qualifying signals (one per pair per window)
            for sig in signals:
                if sig.pair in traded_this_window:
                    continue
                if any(p.pair == sig.pair and p.window_ts == current_ts for p in positions):
                    continue

                traded_this_window.add(sig.pair)  # mark attempted before placing — prevents retry if Leg A errors
                pos = await enter_pairs(
                    client=client,
                    coin_a=sig.coin_a, coin_b=sig.coin_b,
                    market_a=sig.market_a, market_b=sig.market_b,
                    direction_a=sig.direction_a, direction_b=sig.direction_b,
                    price_a=sig.price_a, price_b=sig.price_b,
                    best_sum=sig.best_sum, rho=sig.rho,
                    bankroll=bankroll, window_ts=current_ts,
                )
                if pos:
                    positions.append(pos)

            # Print status panel every second (same pattern as v5 maker)
            if now - last_status_print >= 1.0:
                last_status_print = now
                console.print(build_display(bankroll, positions, signals, secs_left, current_ts))

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Pairs bot stopped.[/yellow]")


def main():
    asyncio.run(run_pairs_bot())


if __name__ == "__main__":
    main()

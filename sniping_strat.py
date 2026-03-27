"""
Sniper Strategy — 5-min Last-Minute Range Buyer

Idea:
  At T-ENTRY_SECONDS before close, if both YES and NO are still in a
  "balanced" range [LOWER, UPPER], buy the cheaper side as a taker.

  Theory: markets that are still 30-70% at T-60s haven't resolved yet.
  The market may be mispricing the uncertainty — buying the cheap side
  captures that edge near resolution.

Entry conditions:
  1. T <= SNIPER_ENTRY_SECONDS before close (enter window)
  2. min(yes, no) >= SNIPER_LOWER  (e.g. 0.30 — not near-resolved)
  3. max(yes, no) <= SNIPER_UPPER  (e.g. 0.70 — not near-resolved)
  4. Buy the cheaper side (min of yes, no)
  5. One trade per market per window

Run: python bot.py sniper
"""

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

import crypto_markets
from binance_feed import feed as binance_feed, connect_binance, get_initial_prices
from crypto_markets import discover_market, CryptoMarket, get_current_window_timestamp
from poly_feed import poly_feed, poll_poly_prices
from trader import init_client
from vpn import ensure_vpn
import telegram_alerts as tg
from redeemer import check_and_redeem

load_dotenv()

# ── Patch window size for this bot ──────────────────────────────────────────
_sniper_window = int(os.getenv("SNIPER_MARKET_WINDOW", "5"))
if _sniper_window not in (5, 15):
    raise ValueError(f"SNIPER_MARKET_WINDOW must be 5 or 15, got {_sniper_window}")
if _sniper_window != 15:
    crypto_markets.WINDOW_SECONDS = _sniper_window * 60
    crypto_markets.COIN_SLUGS = {
        coin: slug.replace("-15m", f"-{_sniper_window}m")
        for coin, slug in crypto_markets.COIN_SLUGS.items()
    }

WINDOW_SECONDS = crypto_markets.WINDOW_SECONDS
COIN_SLUGS     = crypto_markets.COIN_SLUGS

# ── Config ───────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
FUNDER         = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))
VPN_REQUIRED   = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

SNIPER_COINS         = os.getenv("SNIPER_COINS", "BTC,ETH").split(",")
SNIPER_ENTRY_SECONDS = int(  os.getenv("SNIPER_ENTRY_SECONDS", "62"))   # enter at T-62s
SNIPER_LOWER         = float(os.getenv("SNIPER_LOWER",         "0.30")) # min(yes,no) >= this
SNIPER_UPPER         = float(os.getenv("SNIPER_UPPER",         "0.70")) # max(yes,no) <= this
SNIPER_TAKER_FEE     = float(os.getenv("SNIPER_TAKER_FEE",    "0.02")) # 2% taker fee

# Binance momentum filter
MOMENTUM_LOOKBACK    = int(  os.getenv("SNIPER_MOMENTUM_LOOKBACK", "30"))  # seconds of Binance price history
MOMENTUM_MIN_PCT     = float(os.getenv("SNIPER_MOMENTUM_MIN_PCT",  "0.03")) # ignore moves below this % (flat)

# Dip-after-surge signal
DIP_SURGE_MIN  = float(os.getenv("SNIPER_DIP_SURGE_MIN",  "0.75")) # side must have surged above this
DIP_MIN_DROP   = float(os.getenv("SNIPER_DIP_MIN_DROP",   "0.10")) # must have dropped by at least this
DIP_FLOOR      = float(os.getenv("SNIPER_DIP_FLOOR",      "0.60")) # current price must still be above this
DIP_LOOKBACK   = int(  os.getenv("SNIPER_DIP_LOOKBACK",   "30"))   # seconds of price history to check

# Favorite fallback signal — buy higher-priced side when no other signal fires
SNIPER_FAVORITE_MAX = float(os.getenv("SNIPER_FAVORITE_MAX", "0.90"))  # skip if favorite >= this (near-resolved)

STARTING_BANKROLL  = float(os.getenv("SNIPER_BANKROLL",        "183.0"))
BET_SIZE           = float(os.getenv("SNIPER_BET_SIZE",        "10.0"))
MAX_BET            = float(os.getenv("SNIPER_MAX_BET",         "20.0"))
BET_PCT            = float(os.getenv("SNIPER_BET_PCT",         "0.07"))   # 7% of balance
DAILY_LOSS_LIMIT   = float(os.getenv("SNIPER_DAILY_LOSS_LIMIT","100.0"))

DATA_DIR   = Path(os.getenv("SNIPER_DATA_DIR", "data/sniper"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE  = DATA_DIR / "sniper_state.json"
TRADES_FILE = DATA_DIR / "sniper_trades.jsonl"

console = Console()

# ── Hot-reload config ────────────────────────────────────────────────────────
_ENV_FILE  = Path(__file__).parent / ".env"
_env_mtime: float = 0.0


def _reload_config() -> None:
    global _env_mtime
    global SNIPER_ENTRY_SECONDS, SNIPER_LOWER, SNIPER_UPPER, SNIPER_TAKER_FEE
    global DIP_SURGE_MIN, DIP_MIN_DROP, DIP_FLOOR, DIP_LOOKBACK
    global SNIPER_FAVORITE_MAX, MOMENTUM_LOOKBACK, MOMENTUM_MIN_PCT
    global BET_SIZE, MAX_BET, BET_PCT, DAILY_LOSS_LIMIT

    try:
        mtime = _ENV_FILE.stat().st_mtime
    except OSError:
        return
    if mtime == _env_mtime:
        return

    load_dotenv(override=True)
    _env_mtime = mtime

    prev = dict(
        SNIPER_ENTRY_SECONDS=SNIPER_ENTRY_SECONDS, SNIPER_LOWER=SNIPER_LOWER,
        SNIPER_UPPER=SNIPER_UPPER, SNIPER_TAKER_FEE=SNIPER_TAKER_FEE,
        DIP_SURGE_MIN=DIP_SURGE_MIN, DIP_MIN_DROP=DIP_MIN_DROP,
        DIP_FLOOR=DIP_FLOOR, DIP_LOOKBACK=DIP_LOOKBACK,
        SNIPER_FAVORITE_MAX=SNIPER_FAVORITE_MAX,
        MOMENTUM_LOOKBACK=MOMENTUM_LOOKBACK, MOMENTUM_MIN_PCT=MOMENTUM_MIN_PCT,
        BET_SIZE=BET_SIZE, MAX_BET=MAX_BET, BET_PCT=BET_PCT,
        DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
    )

    SNIPER_ENTRY_SECONDS = int(  os.getenv("SNIPER_ENTRY_SECONDS", "62"))
    SNIPER_LOWER         = float(os.getenv("SNIPER_LOWER",         "0.30"))
    SNIPER_UPPER         = float(os.getenv("SNIPER_UPPER",         "0.70"))
    SNIPER_TAKER_FEE     = float(os.getenv("SNIPER_TAKER_FEE",    "0.02"))
    DIP_SURGE_MIN        = float(os.getenv("SNIPER_DIP_SURGE_MIN", "0.75"))
    DIP_MIN_DROP         = float(os.getenv("SNIPER_DIP_MIN_DROP",  "0.10"))
    DIP_FLOOR            = float(os.getenv("SNIPER_DIP_FLOOR",     "0.60"))
    DIP_LOOKBACK         = int(  os.getenv("SNIPER_DIP_LOOKBACK",  "30"))
    SNIPER_FAVORITE_MAX  = float(os.getenv("SNIPER_FAVORITE_MAX",  "0.90"))
    MOMENTUM_LOOKBACK    = int(  os.getenv("SNIPER_MOMENTUM_LOOKBACK", "30"))
    MOMENTUM_MIN_PCT     = float(os.getenv("SNIPER_MOMENTUM_MIN_PCT",  "0.03"))
    BET_SIZE             = float(os.getenv("SNIPER_BET_SIZE",      "10.0"))
    MAX_BET              = float(os.getenv("SNIPER_MAX_BET",       "20.0"))
    BET_PCT              = float(os.getenv("SNIPER_BET_PCT",       "0.07"))
    DAILY_LOSS_LIMIT     = float(os.getenv("SNIPER_DAILY_LOSS_LIMIT", "100.0"))

    new = dict(
        SNIPER_ENTRY_SECONDS=SNIPER_ENTRY_SECONDS, SNIPER_LOWER=SNIPER_LOWER,
        SNIPER_UPPER=SNIPER_UPPER, SNIPER_TAKER_FEE=SNIPER_TAKER_FEE,
        DIP_SURGE_MIN=DIP_SURGE_MIN, DIP_MIN_DROP=DIP_MIN_DROP,
        DIP_FLOOR=DIP_FLOOR, DIP_LOOKBACK=DIP_LOOKBACK,
        SNIPER_FAVORITE_MAX=SNIPER_FAVORITE_MAX,
        MOMENTUM_LOOKBACK=MOMENTUM_LOOKBACK, MOMENTUM_MIN_PCT=MOMENTUM_MIN_PCT,
        BET_SIZE=BET_SIZE, MAX_BET=MAX_BET, BET_PCT=BET_PCT,
        DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
    )
    changes = [f"{k}: {prev[k]} → {new[k]}" for k in prev if new[k] != prev[k]]
    if changes:
        msg = "⚙️ Sniper config reloaded: " + " | ".join(changes)
        console.print(f"[cyan]{msg}[/cyan]")
        tg.send_message(msg)


def _start_config_watcher() -> None:
    def _loop():
        while True:
            time.sleep(15)
            try:
                _reload_config()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name="sniper-config-watcher").start()


# ── Logging ──────────────────────────────────────────────────────────────────
def _log_trade(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADES_FILE, "a") as f:
        f.write(f"{ts}  {msg}\n")


# ── Bankroll ─────────────────────────────────────────────────────────────────
@dataclass
class SniperBankroll:
    balance:     float = STARTING_BANKROLL
    wins:        int   = 0
    losses:      int   = 0
    daily_loss:  float = 0.0

    @property
    def daily_limit_hit(self) -> bool:
        return self.daily_loss >= DAILY_LOSS_LIMIT

    def bet_size(self) -> float:
        """PCT of balance, clamped to [BET_SIZE, MAX_BET]."""
        pct_bet = self.balance * BET_PCT if BET_PCT > 0 else BET_SIZE
        return round(min(MAX_BET, max(BET_SIZE, pct_bet)), 2)

    def record_result(self, cost: float, payout: float, coin: str, direction: str):
        profit = payout - cost
        if payout > 0:
            self.wins  += 1
            self.balance += payout
        else:
            self.losses += 1
            self.daily_loss += cost
        _log_trade(f"RESULT: {coin} {direction} cost=${cost:.2f} payout=${payout:.2f} profit={profit:+.2f}")
        self._save()
        emoji = "🎉" if payout > 0 else "💔"
        outcome = "WIN" if payout > 0 else "LOSS"
        tg.send_message(
            f"{emoji} SNIPER {outcome} {coin} {direction.upper()}\n"
            f"Cost ${cost:.2f} → Payout ${payout:.2f}\n"
            f"Profit: {profit:+.2f}  Bankroll: ${self.balance:.2f}"
        )

    def _save(self):
        STATE_FILE.write_text(json.dumps({
            "balance":    self.balance,
            "wins":       self.wins,
            "losses":     self.losses,
            "daily_loss": self.daily_loss,
            "saved_at":   time.time(),
        }, indent=2))

    @classmethod
    def load(cls) -> "SniperBankroll":
        try:
            d = json.loads(STATE_FILE.read_text())
            b = cls()
            b.balance    = d.get("balance",    STARTING_BANKROLL)
            b.wins       = d.get("wins",       0)
            b.losses     = d.get("losses",     0)
            b.daily_loss = d.get("daily_loss", 0.0)
            return b
        except Exception:
            return cls()


# ── Position tracking ────────────────────────────────────────────────────────
@dataclass
class SniperPosition:
    coin:       str
    direction:  str   # "up" or "down"
    token_id:   str
    fill_price: float
    shares:     float
    cost:       float
    window_ts:  int
    status:     str = "open"   # open | won | lost

    @property
    def payout(self) -> float:
        return self.shares if self.status == "won" else 0.0


# ── Order placement ──────────────────────────────────────────────────────────
def _best_ask(token_id: str) -> float | None:
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


async def _place_taker_buy(
    client, token_id: str, shares: float, poly_price: float, label: str
) -> tuple[bool, float]:
    """Aggressive GTC buy at ask+2ticks. Returns (success, fill_price)."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    ask = _best_ask(token_id)
    fill_price = round(min(0.98, (ask or poly_price) + 0.002), 4)

    try:
        signed = client.create_order(OrderArgs(
            token_id=token_id,
            price=fill_price,
            size=round(shares, 2),
            side=BUY,
        ))
        resp    = client.post_order(signed, OrderType.GTC)
        success = isinstance(resp, dict) and resp.get("success", True)
        if success:
            console.print(f"  [green]✅ {label}: {shares:.0f} shares @ ${fill_price:.4f}[/green]")
        else:
            console.print(f"  [red]❌ {label}: rejected — {resp}[/red]")
        return success, fill_price
    except Exception as e:
        console.print(f"  [red]❌ {label} failed: {e}[/red]")
        return False, fill_price


# ── Signal ───────────────────────────────────────────────────────────────────
@dataclass
class SniperSignal:
    coin:        str
    direction:   str    # "up" or "down"
    price:       float  # the price we'll pay
    yes_price:   float
    no_price:    float
    signal_type: str = "range"   # "range" | "dip"


# ── Price history tracker (for dip-after-surge signal) ───────────────────────
class PriceHistory:
    """
    Keeps a rolling window of (timestamp, yes_price, no_price) tuples
    per coin. Used to detect dip-after-surge pattern.
    """
    def __init__(self):
        self._history: dict[str, list[tuple[float, float, float]]] = {}

    def record(self, coin: str, yes: float, no: float):
        now = time.time()
        if coin not in self._history:
            self._history[coin] = []
        self._history[coin].append((now, yes, no))
        # prune entries older than DIP_LOOKBACK + 10s buffer
        cutoff = now - DIP_LOOKBACK - 10
        self._history[coin] = [e for e in self._history[coin] if e[0] >= cutoff]

    def recent_high(self, coin: str, side: str, lookback_secs: float) -> float | None:
        """Max price seen for 'side' (yes/no) over the last lookback_secs."""
        entries = self._history.get(coin, [])
        cutoff  = time.time() - lookback_secs
        idx     = 0 if side == "yes" else 1  # (ts, yes, no) → yes=1, no=2 in tuple
        prices  = [e[idx + 1] for e in entries if e[0] >= cutoff]
        return max(prices) if prices else None

    def clear_window(self, coin: str):
        """Clear history on new window so old surges don't bleed in."""
        self._history[coin] = []


price_history = PriceHistory()


def ewma_momentum(coin: str) -> float | None:
    """
    EWMA-weighted momentum over the last MOMENTUM_LOOKBACK seconds of Binance ticks.
    Recent ticks get exponentially more weight than older ones.
    Returns % move (positive = up, negative = down), or None if insufficient data.
    """
    history = binance_feed.history.get(coin)
    if not history or len(history) < 2:
        return None

    now = time.time()
    cutoff = now - MOMENTUM_LOOKBACK
    ticks = [t for t in history if t.timestamp >= cutoff]
    if len(ticks) < 2:
        return None

    # EWMA: weight = exp(lambda * (t - t_oldest)) so newest tick has highest weight
    # Use span = lookback/3 so half-life ≈ 10s on 30s window
    alpha = 2.0 / (len(ticks) + 1)
    ewma = ticks[0].price
    for tick in ticks[1:]:
        ewma = alpha * tick.price + (1 - alpha) * ewma

    pct_move = (ewma - ticks[0].price) / ticks[0].price * 100
    return pct_move


def momentum_agrees(coin: str, direction: str) -> bool:
    """
    Returns True if Binance momentum agrees with the entry direction,
    or if momentum is flat (below threshold) — no filter applied.
    Returns False only when momentum clearly contradicts direction.
    """
    mom = ewma_momentum(coin)
    if mom is None or abs(mom) < MOMENTUM_MIN_PCT:
        return True  # flat/no data — don't block
    if direction == "up":
        return mom > 0
    else:
        return mom < 0


def scan_signal(coin: str, secs_remaining: int) -> SniperSignal | None:
    """
    Returns a signal if:
    - Within entry window (secs_remaining <= SNIPER_ENTRY_SECONDS)
    - min(yes,no) >= SNIPER_LOWER and max(yes,no) <= SNIPER_UPPER
    - Positive EV after taker fees
    """
    if secs_remaining > SNIPER_ENTRY_SECONDS or secs_remaining <= 0:
        return None

    yes_price, no_price = poly_feed.get_market_prices(coin)
    if yes_price is None or no_price is None:
        return None

    lo = min(yes_price, no_price)
    hi = max(yes_price, no_price)

    if lo < SNIPER_LOWER or hi > SNIPER_UPPER:
        return None

    # EV check: buying the cheap side at price p, paying taker fee
    # Win prob ≈ p (market-implied), but market may underprice near expiry
    # Basic EV: (1 - p - fee) / p > 0  →  p < 1 - fee = 0.98
    # Since p <= SNIPER_UPPER <= 0.70, this always passes. But we also
    # require p + fee < 0.98 to have some buffer.
    p = lo  # price of cheaper side
    if p + SNIPER_TAKER_FEE >= 0.98:
        return None

    direction = "up" if yes_price <= no_price else "down"
    return SniperSignal(
        coin=coin, direction=direction, price=p,
        yes_price=yes_price, no_price=no_price, signal_type="range",
    )


def scan_dip_signal(coin: str, secs_remaining: int) -> SniperSignal | None:
    """
    Dip-after-surge signal:
      - Within entry window
      - One side surged above DIP_SURGE_MIN (e.g. 0.75) in the last DIP_LOOKBACK seconds
      - Current price has dipped by at least DIP_MIN_DROP from that high (e.g. -0.10)
      - Current price is still above DIP_FLOOR (e.g. 0.60) — conviction intact
      - Buy the dipped side expecting recovery toward close
    """
    if secs_remaining > SNIPER_ENTRY_SECONDS or secs_remaining <= 0:
        return None

    yes_price, no_price = poly_feed.get_market_prices(coin)
    if yes_price is None or no_price is None:
        return None

    for side, current, direction in [("yes", yes_price, "up"), ("no", no_price, "down")]:
        high = price_history.recent_high(coin, side, DIP_LOOKBACK)
        if high is None:
            continue
        drop = high - current
        if high >= DIP_SURGE_MIN and drop >= DIP_MIN_DROP and current >= DIP_FLOOR:
            if current + SNIPER_TAKER_FEE >= 0.98:
                continue
            return SniperSignal(
                coin=coin, direction=direction, price=current,
                yes_price=yes_price, no_price=no_price, signal_type="dip",
            )
    return None


def scan_favorite_signal(coin: str, secs_remaining: int) -> SniperSignal | None:
    """
    Fallback signal: no range or dip pattern detected.
    Pick the higher-priced side (the market favorite / momentum side).
    Skip if favorite price + fee >= SNIPER_FAVORITE_MAX (market already near-resolved).
    """
    if secs_remaining > SNIPER_ENTRY_SECONDS or secs_remaining <= 0:
        return None

    yes_price, no_price = poly_feed.get_market_prices(coin)
    if yes_price is None or no_price is None:
        return None

    if yes_price >= no_price:
        price, direction = yes_price, "up"
    else:
        price, direction = no_price, "down"

    if price + SNIPER_TAKER_FEE >= SNIPER_FAVORITE_MAX:
        return None

    return SniperSignal(
        coin=coin, direction=direction, price=price,
        yes_price=yes_price, no_price=no_price, signal_type="favorite",
    )


# ── Resolution ───────────────────────────────────────────────────────────────
def _resolve_winner(coin: str, window_ts: int) -> str | None:
    """Returns 'up', 'down', or None if not yet resolved."""
    try:
        prefix = crypto_markets.COIN_SLUGS.get(coin)
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

        raw_outcomes = m.get("outcomes", "[]")
        raw_prices   = m.get("outcomePrices", "[]")
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices   = json.loads(raw_prices)   if isinstance(raw_prices,   str) else raw_prices

        for i, o in enumerate(outcomes):
            if i < len(prices) and float(prices[i]) >= 0.95:
                label = str(o).lower()
                if label == "up":   return "up"
                if label == "down": return "down"
    except Exception:
        pass
    return None


async def check_resolutions(
    positions: list[SniperPosition],
    bankroll: SniperBankroll,
) -> list[SniperPosition]:
    still_open = []
    now = time.time()

    for pos in positions:
        window_end = pos.window_ts + WINDOW_SECONDS
        if now < window_end + 30:
            still_open.append(pos)
            continue

        winner = _resolve_winner(pos.coin, pos.window_ts)
        if winner is None:
            if now < window_end + 300:
                still_open.append(pos)
            else:
                console.print(f"[yellow]Resolution timeout for {pos.coin} — skipping[/yellow]")
            continue

        won = (winner == pos.direction)
        pos.status = "won" if won else "lost"
        payout = pos.shares if won else 0.0

        console.print(
            f"[{'green' if won else 'red'}]"
            f"Resolution {pos.coin} {pos.direction.upper()}: {'WIN' if won else 'LOSS'}  "
            f"payout=${payout:.2f}  cost=${pos.cost:.2f}  profit={payout-pos.cost:+.2f}[/]"
        )
        bankroll.record_result(pos.cost, payout, pos.coin, pos.direction)

    return still_open


# ── Display ──────────────────────────────────────────────────────────────────
def build_display(
    bankroll: SniperBankroll,
    positions: list[SniperPosition],
    windows: dict,   # coin -> secs_remaining
    current_ts: int,
) -> Table:
    win_label = datetime.fromtimestamp(current_ts, tz=timezone.utc).strftime('%H:%M UTC')
    t = Table.grid(padding=(0, 1))

    # Per-coin row: Polymarket odds + Binance price + EWMA momentum
    for coin in SNIPER_COINS:
        yes, no = poly_feed.get_market_prices(coin)
        bnb_price = binance_feed.get_price(coin)
        mom = ewma_momentum(coin)

        secs = windows.get(coin, 0)
        mins, s = divmod(max(0, secs), 60)
        in_window = 0 < secs <= SNIPER_ENTRY_SECONDS
        timer_col = "yellow" if in_window else "dim"

        if yes is not None and no is not None:
            y_col = "green" if yes <= SNIPER_UPPER else "dim"
            n_col = "green" if no  <= SNIPER_UPPER else "dim"
            poly_str = (
                f"[{y_col}]Y${yes:.2f}[/{y_col}] "
                f"[{n_col}]N${no:.2f}[/{n_col}]"
            )
        else:
            poly_str = "[dim](stale)[/dim]"

        bnb_str  = f"${bnb_price:,.2f}" if bnb_price else "[dim]n/a[/dim]"
        if mom is not None:
            mom_col = "green" if mom > MOMENTUM_MIN_PCT else ("red" if mom < -MOMENTUM_MIN_PCT else "dim")
            mom_str = f"[{mom_col}]{mom:+.3f}%[/{mom_col}]"
        else:
            mom_str = "[dim]mom:n/a[/dim]"

        t.add_row(
            f"  [bold]{coin}[/bold]  "
            f"poly: {poly_str}   "
            f"binance: {bnb_str}  {mom_str}   "
            f"[{timer_col}]T-{mins}:{s:02d}[/{timer_col}]"
        )
    t.add_row(f"  [dim]{poly_feed.stats}[/dim]")
    t.add_row("")

    # Open positions
    if positions:
        t.add_row(f"  [bold]Open Positions ({len(positions)})[/bold]")
        now_ts = time.time()
        for pos in positions:
            pos_window_end = pos.window_ts + WINDOW_SECONDS
            secs_to_close = int(pos_window_end - now_ts)
            if secs_to_close > 0:
                mins, s = divmod(secs_to_close, 60)
                timing = f"[yellow]closes T-{mins}:{s:02d}[/yellow]"
            else:
                waited = int(now_ts - pos_window_end)
                timing = f"[dim]resolving... +{waited}s[/dim]"
            t.add_row(
                f"   {pos.coin} {pos.direction.upper()} "
                f"@ ${pos.fill_price:.3f}  "
                f"shares={pos.shares:.0f}  cost=${pos.cost:.2f}  "
                f"{timing}"
            )
        t.add_row("")

    # Bankroll
    wl_total = bankroll.wins + bankroll.losses
    wl_pct   = f"{bankroll.wins/wl_total:.0%}" if wl_total else "—"
    t.add_row(
        f"  [bold]Bankroll:[/bold] ${bankroll.balance:.2f}  "
        f"[bold]W/L:[/bold] {bankroll.wins}/{bankroll.losses} ({wl_pct})  "
        f"[bold]Daily loss:[/bold] ${bankroll.daily_loss:.2f}/${DAILY_LOSS_LIMIT:.0f}"
    )

    # Footer
    t.add_row(
        f"  [dim bold]SNIPER v1  {win_label}  window={_sniper_window}m  "
        f"entry=T-{SNIPER_ENTRY_SECONDS}s  "
        f"range=[{SNIPER_LOWER:.2f},{SNIPER_UPPER:.2f}][/dim bold]"
    )
    return t


# ── Main loop ────────────────────────────────────────────────────────────────
async def run_sniper_bot():
    console.print("[bold cyan]━━━ SNIPER BOT v1 — Last-Minute Range Buyer ━━━[/bold cyan]")
    console.print()

    _start_config_watcher()

    if VPN_REQUIRED and not ensure_vpn(required=True):
        console.print("[red]VPN required but not active — exiting[/red]")
        return

    client = init_client(PRIVATE_KEY, signature_type=SIGNATURE_TYPE, funder=FUNDER)

    # Start Binance WebSocket (1s tick prices for momentum)
    console.print("[dim]Fetching initial Binance prices...[/dim]")
    get_initial_prices()
    asyncio.create_task(connect_binance())

    # Start Polymarket REST price feed (5s poll — auto-tracks window token changes)
    asyncio.create_task(poll_poly_prices(SNIPER_COINS, interval=5.0))
    console.print("[dim]Polymarket price feed started (REST poll — 5s interval)[/dim]")
    await asyncio.sleep(5)  # let first polls complete

    bankroll  = SniperBankroll.load()
    positions: list[SniperPosition] = []
    traded_this_window: dict[str, int] = {}   # coin -> window_ts of last trade
    cached_markets: dict[str, CryptoMarket] = {}
    last_market_refresh = 0.0

    console.print(
        f"  Coins: {', '.join(SNIPER_COINS)}  "
        f"Balance: ${bankroll.balance:.2f}  "
        f"Entry: T-{SNIPER_ENTRY_SECONDS}s  "
        f"Range: [{SNIPER_LOWER},{SNIPER_UPPER}]"
    )
    console.print()

    tg.send_message(
        f"🚀 Sniper Bot started\n"
        f"Coins: {', '.join(SNIPER_COINS)}  |  {_sniper_window}m markets\n"
        f"Entry: T-{SNIPER_ENTRY_SECONDS}s  Range: [{SNIPER_LOWER}, {SNIPER_UPPER}]\n"
        f"Bet: ${BET_SIZE}–${MAX_BET}  Daily limit: ${DAILY_LOSS_LIMIT}\n"
        f"Balance: ${bankroll.balance:.2f}"
    )

    last_status_print = 0.0

    try:
        while True:
            now        = time.time()
            current_ts = get_current_window_timestamp()
            secs_left  = int(current_ts + WINDOW_SECONDS - now)

            windows = {coin: secs_left for coin in SNIPER_COINS}

            # Record prices for dip-after-surge detection
            for coin in SNIPER_COINS:
                yes, no = poly_feed.get_market_prices(coin)
                if yes is not None and no is not None:
                    price_history.record(coin, yes, no)

            # Refresh market cache every 30s
            if now - last_market_refresh > 30:
                for coin in SNIPER_COINS:
                    m = discover_market(coin)
                    if m:
                        cached_markets[coin] = m
                last_market_refresh = now

            # Auto-redeem
            if FUNDER and PRIVATE_KEY:
                redeemed = check_and_redeem(FUNDER, PRIVATE_KEY)
                if redeemed > 0:
                    bankroll.balance += redeemed

            # Daily loss guard
            if bankroll.daily_limit_hit:
                console.print("[red]Daily loss limit hit — paused[/red]")
                await asyncio.sleep(60)
                continue

            # Resolve old positions
            positions = await check_resolutions(positions, bankroll)

            # Scan and enter signals
            for coin in SNIPER_COINS:
                if traded_this_window.get(coin) == current_ts:
                    continue
                if any(p.coin == coin and p.window_ts == current_ts for p in positions):
                    continue

                market = cached_markets.get(coin)
                if not market:
                    continue

                sig = (
                    scan_signal(coin, secs_left)
                    or scan_dip_signal(coin, secs_left)
                    or scan_favorite_signal(coin, secs_left)
                )
                if not sig:
                    continue

                # Binance momentum filter — skip if momentum contradicts direction
                mom = ewma_momentum(coin)
                mom_str = f"{mom:+.3f}%" if mom is not None else "n/a"
                if not momentum_agrees(coin, sig.direction):
                    console.print(
                        f"  [dim]{coin}: momentum={mom_str} contradicts {sig.direction.upper()} — skip[/dim]"
                    )
                    continue

                # Pre-flight CLOB check
                token_id = market.up_token_id if sig.direction == "up" else market.down_token_id
                ask = _best_ask(token_id)
                if ask is not None and ask + SNIPER_TAKER_FEE >= 0.98:
                    console.print(f"  [yellow]{coin}: CLOB ask={ask:.3f} too high — skip[/yellow]")
                    continue

                spend  = bankroll.bet_size()
                shares = round(spend / sig.price, 2)

                console.print(
                    f"\n[bold cyan]▶ SNIPER [{sig.signal_type.upper()}] {coin} {sig.direction.upper()}[/bold cyan]  "
                    f"Y=${sig.yes_price:.3f}  N=${sig.no_price:.3f}  "
                    f"@ ${sig.price:.3f}  shares={shares:.0f}  spend≈${spend:.2f}  "
                    f"momentum={mom_str}"
                )
                _log_trade(
                    f"ENTRY [{sig.signal_type}]: {coin} {sig.direction} @ {sig.price:.3f} "
                    f"yes={sig.yes_price:.3f} no={sig.no_price:.3f} "
                    f"shares={shares:.0f} spend=${spend:.2f} momentum={mom_str}"
                )

                ok, fill_price = await _place_taker_buy(
                    client, token_id, shares, sig.price,
                    f"{coin} {sig.direction.upper()}"
                )

                if ok:
                    cost = fill_price * shares
                    bankroll.balance -= cost
                    traded_this_window[coin] = current_ts
                    positions.append(SniperPosition(
                        coin=coin, direction=sig.direction,
                        token_id=token_id, fill_price=fill_price,
                        shares=shares, cost=cost, window_ts=current_ts,
                    ))
                    tg.send_message(
                        f"🎯 SNIPER [{sig.signal_type.upper()}] {coin} {sig.direction.upper()}\n"
                        f"Y=${sig.yes_price:.3f}  N=${sig.no_price:.3f}\n"
                        f"Filled @ ${fill_price:.3f}  Shares: {shares:.0f}  Cost: ${cost:.2f}\n"
                        f"Momentum: {mom_str}  T-{secs_left}s remaining"
                    )

            # Print status panel every second
            if now - last_status_print >= 1.0:
                last_status_print = now
                console.print(build_display(bankroll, positions, windows, current_ts))

            await asyncio.sleep(1)

    except KeyboardInterrupt:
        console.print("\n[yellow]Sniper bot stopped.[/yellow]")


if __name__ == "__main__":
    asyncio.run(run_sniper_bot())

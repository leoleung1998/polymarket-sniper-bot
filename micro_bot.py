"""
Microstructure Bot — Order Book Imbalance + Binance EWMA

Entry signals:
  1. OBI (Order Book Imbalance) from near-market Polymarket L2 book
     Positive OBI on UP token  → buy pressure → price rising → buy UP
     Positive OBI on DOWN token → buy pressure → price rising → buy DOWN
  2. Binance time-decay EWMA momentum (fast 10s - slow 30s) confirms direction
     Both signals must agree. Signal fades quickly — enter or skip.

Trading modes:
  Mode A (T > MICRO_A_ENTRY_SECS):
    - Post GTC maker limit bid just inside the spread (best_bid + 0.005)
    - Taker order at best_ask + 2 ticks (crosses book) if bid misses after
      MICRO_A_HOLD_SECS seconds → cancel or replace as taker
    - Skip if OBI weakens before fill

  Mode B (T ≤ MICRO_B_ENTRY_SECS):
    - Taker order at best_ask + 2 ticks (crosses book immediately)
    - Hold to resolution ($1.00 or $0.00 per share)

Price filter: only enter if price in [MICRO_MIN_PRICE, MICRO_MAX_PRICE]
Risk: one position per coin per window, daily loss limit

Run:  python bot.py micro
"""

import asyncio
import json
import math
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
from rich.text import Text

import crypto_markets
from binance_feed import feed as binance_feed, connect_binance, get_initial_prices
from crypto_markets import discover_market, discover_market_tokens, CryptoMarket, get_current_window_timestamp
from order_book import order_book
from poly_feed import poly_feed, poll_poly_prices
from poly_ws import ws_feed
from trader import init_client
from vpn import ensure_vpn
import telegram_alerts as tg
from redeemer import check_and_redeem

load_dotenv()

# ── Window size ───────────────────────────────────────────────────────────────
_micro_window = int(os.getenv("MICRO_MARKET_WINDOW", "5"))
if _micro_window not in (5, 15):
    raise ValueError(f"MICRO_MARKET_WINDOW must be 5 or 15, got {_micro_window}")
if _micro_window != 15:
    crypto_markets.WINDOW_SECONDS = _micro_window * 60
    crypto_markets.COIN_SLUGS = {
        coin: slug.replace("-15m", f"-{_micro_window}m")
        for coin, slug in crypto_markets.COIN_SLUGS.items()
    }
WINDOW_SECONDS = crypto_markets.WINDOW_SECONDS
COIN_SLUGS     = crypto_markets.COIN_SLUGS

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
FUNDER         = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))
VPN_REQUIRED   = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

MICRO_COINS          = os.getenv("MICRO_COINS",          "BTC,ETH").split(",")

# Signal thresholds
MICRO_OBI_THRESHOLD  = float(os.getenv("MICRO_OBI_THRESHOLD",  "0.15"))  # min |OBI| to trade
MICRO_EWMA_THRESHOLD = float(os.getenv("MICRO_EWMA_THRESHOLD", "0.003")) # min |EWMA signal %| to trade
MICRO_OBI_DEPTH      = float(os.getenv("MICRO_OBI_DEPTH",      "0.15"))  # near-market filter radius

# Entry timing
MICRO_A_ENTRY_SECS   = int(  os.getenv("MICRO_A_ENTRY_SECS",  "90"))   # enter Mode A when T > this
MICRO_A_HOLD_SECS    = int(  os.getenv("MICRO_A_HOLD_SECS",   "45"))   # cancel Mode A after this
MICRO_B_ENTRY_SECS   = int(  os.getenv("MICRO_B_ENTRY_SECS",  "90"))   # enter Mode B when T <= this

# Price filter
MICRO_MIN_PRICE      = float(os.getenv("MICRO_MIN_PRICE",     "0.20"))  # don't buy below this
MICRO_MAX_PRICE      = float(os.getenv("MICRO_MAX_PRICE",     "0.80"))  # don't buy above this
MICRO_TAKER_FEE      = float(os.getenv("MICRO_TAKER_FEE",     "0.02"))  # 2% taker fee

# Sizing
STARTING_BANKROLL    = float(os.getenv("MICRO_BANKROLL",      "100.0"))
BET_SIZE             = float(os.getenv("MICRO_BET_SIZE",      "5.0"))
MAX_BET              = float(os.getenv("MICRO_MAX_BET",       "10.0"))
BET_PCT              = float(os.getenv("MICRO_BET_PCT",       "0.0"))   # 0 = use fixed BET_SIZE
DAILY_LOSS_LIMIT     = float(os.getenv("MICRO_DAILY_LOSS_LIMIT", "50.0"))
STATUS_INTERVAL      = float(os.getenv("MICRO_STATUS_INTERVAL",  "1.0"))  # display refresh rate (seconds)

DATA_DIR   = Path(os.getenv("MICRO_DATA_DIR", "data/micro"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE  = DATA_DIR / "micro_state.json"
TRADES_FILE = DATA_DIR / "micro_trades.jsonl"

console = Console()

# ── Logging ──────────────────────────────────────────────────────────────────
import sys

class _Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, data):
        for s in self.streams: s.write(data)
    def flush(self):
        for s in self.streams: s.flush()
    def fileno(self): return self.streams[0].fileno()

_log_file = Path(__file__).parent / "micro.log"
_log_fh = open(_log_file, "a", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _log_fh)


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADES_FILE, "a") as f:
        f.write(f"{ts}  {msg}\n")


# ── Hot-reload config ─────────────────────────────────────────────────────────
_ENV_FILE  = Path(__file__).parent / ".env"
_env_mtime: float = 0.0


def _reload_config() -> None:
    global _env_mtime
    global MICRO_OBI_THRESHOLD, MICRO_EWMA_THRESHOLD, MICRO_OBI_DEPTH
    global MICRO_A_ENTRY_SECS, MICRO_A_HOLD_SECS, MICRO_B_ENTRY_SECS
    global MICRO_MIN_PRICE, MICRO_MAX_PRICE, MICRO_TAKER_FEE
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
        MICRO_OBI_THRESHOLD=MICRO_OBI_THRESHOLD, MICRO_EWMA_THRESHOLD=MICRO_EWMA_THRESHOLD,
        MICRO_A_ENTRY_SECS=MICRO_A_ENTRY_SECS, MICRO_B_ENTRY_SECS=MICRO_B_ENTRY_SECS,
        BET_SIZE=BET_SIZE, MAX_BET=MAX_BET, DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
    )

    MICRO_OBI_THRESHOLD  = float(os.getenv("MICRO_OBI_THRESHOLD",  "0.15"))
    MICRO_EWMA_THRESHOLD = float(os.getenv("MICRO_EWMA_THRESHOLD", "0.003"))
    MICRO_OBI_DEPTH      = float(os.getenv("MICRO_OBI_DEPTH",      "0.15"))
    MICRO_A_ENTRY_SECS   = int(  os.getenv("MICRO_A_ENTRY_SECS",   "90"))
    MICRO_A_HOLD_SECS    = int(  os.getenv("MICRO_A_HOLD_SECS",    "45"))
    MICRO_B_ENTRY_SECS   = int(  os.getenv("MICRO_B_ENTRY_SECS",   "90"))
    MICRO_MIN_PRICE      = float(os.getenv("MICRO_MIN_PRICE",      "0.20"))
    MICRO_MAX_PRICE      = float(os.getenv("MICRO_MAX_PRICE",      "0.80"))
    MICRO_TAKER_FEE      = float(os.getenv("MICRO_TAKER_FEE",      "0.02"))
    BET_SIZE             = float(os.getenv("MICRO_BET_SIZE",        "5.0"))
    MAX_BET              = float(os.getenv("MICRO_MAX_BET",         "10.0"))
    BET_PCT              = float(os.getenv("MICRO_BET_PCT",         "0.0"))
    DAILY_LOSS_LIMIT     = float(os.getenv("MICRO_DAILY_LOSS_LIMIT","50.0"))

    new = dict(
        MICRO_OBI_THRESHOLD=MICRO_OBI_THRESHOLD, MICRO_EWMA_THRESHOLD=MICRO_EWMA_THRESHOLD,
        MICRO_A_ENTRY_SECS=MICRO_A_ENTRY_SECS, MICRO_B_ENTRY_SECS=MICRO_B_ENTRY_SECS,
        BET_SIZE=BET_SIZE, MAX_BET=MAX_BET, DAILY_LOSS_LIMIT=DAILY_LOSS_LIMIT,
    )
    changes = [f"{k}: {prev[k]} → {new[k]}" for k in prev if new[k] != prev[k]]
    if changes:
        msg = "⚙️ Micro config reloaded: " + " | ".join(changes)
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
    threading.Thread(target=_loop, daemon=True, name="micro-config-watcher").start()


# ── Bankroll ──────────────────────────────────────────────────────────────────
@dataclass
class MicroBankroll:
    balance:    float = STARTING_BANKROLL
    wins:       int   = 0
    losses:     int   = 0
    daily_loss: float = 0.0

    @property
    def daily_limit_hit(self) -> bool:
        return self.daily_loss >= DAILY_LOSS_LIMIT

    def bet_size(self) -> float:
        pct_bet = self.balance * BET_PCT if BET_PCT > 0 else BET_SIZE
        return round(min(MAX_BET, max(BET_SIZE, pct_bet)), 2)

    def record_result(self, cost: float, payout: float, coin: str, direction: str, mode: str):
        profit = payout - cost
        if payout > 0:
            self.wins    += 1
            self.balance += payout
        else:
            self.losses   += 1
            self.daily_loss += cost
        _log(f"RESULT: {coin} {direction} mode={mode} cost=${cost:.2f} payout=${payout:.2f} profit={profit:+.2f}")
        self._save()
        emoji = "🎉" if payout > 0 else "💔"
        tg.send_message(
            f"{emoji} MICRO {('WIN' if payout > 0 else 'LOSS')} {coin} {direction.upper()} [{mode}]\n"
            f"Cost ${cost:.2f} → Payout ${payout:.2f}  Profit: {profit:+.2f}\n"
            f"Bankroll: ${self.balance:.2f}"
        )

    def _save(self):
        STATE_FILE.write_text(json.dumps({
            "balance": self.balance, "wins": self.wins,
            "losses": self.losses, "daily_loss": self.daily_loss,
            "saved_at": time.time(),
        }, indent=2))

    @classmethod
    def load(cls) -> "MicroBankroll":
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


# ── Config: Take-profit ───────────────────────────────────────────────────────
MICRO_A_TP_PCT       = float(os.getenv("MICRO_A_TP_PCT",      "0.03"))  # Mode A: sell at entry + 3%
MICRO_A_FALLBACK_SECS= int(  os.getenv("MICRO_A_FALLBACK_SECS","30"))   # if T<=this when sell times out, hold to resolution
MICRO_B_TP_PCT       = float(os.getenv("MICRO_B_TP_PCT",      "0.20"))  # Mode B: take profit when price up 20% from entry


# ── Position ──────────────────────────────────────────────────────────────────
@dataclass
class MicroPosition:
    coin:          str
    direction:     str    # "up" or "down"
    token_id:      str
    fill_price:    float
    shares:        float
    cost:          float
    window_ts:     int
    mode:          str    # "A" or "B"
    order_id:      str   = ""
    # Mode A exit tracking
    exit_order_id: str   = ""
    exit_price:    float = 0.0
    exit_placed_at:float = 0.0
    entered_at:    float = field(default_factory=time.time)
    status:        str   = "open"   # open | sold | won | lost

    @property
    def payout(self) -> float:
        return self.shares if self.status == "won" else 0.0

    @property
    def age(self) -> float:
        return time.time() - self.entered_at

    @property
    def exit_age(self) -> float:
        return time.time() - self.exit_placed_at if self.exit_placed_at else 0.0


# ── Signal evaluation ─────────────────────────────────────────────────────────
@dataclass
class MicroSignal:
    coin:      str
    direction: str    # "up" or "down"
    token_id:  str
    price:     float
    obi:       float
    ewma:      float
    mode:      str    # "A" or "B"


def _ewma_signal(coin: str) -> float | None:
    """Time-decay EWMA signal: (fast - slow) / slow * 100.
    Positive = upward momentum. Negative = downward momentum.
    """
    return binance_feed.get_ewma_signal(coin)


def evaluate_signal(coin: str, market: CryptoMarket) -> MicroSignal | None:
    """
    Returns a MicroSignal if OBI + EWMA agree on direction and meet thresholds.
    Returns None if either signal is missing, weak, or contradictory.
    """
    secs = market.seconds_remaining
    if secs <= 0:
        return None

    # Determine mode
    if secs > MICRO_A_ENTRY_SECS:
        mode = "A"
    elif secs <= MICRO_B_ENTRY_SECS:
        mode = "B"
    else:
        return None  # gap between modes — skip

    # Get OBI for both tokens
    obi_up   = order_book.obi(market.up_token_id)
    obi_down = order_book.obi(market.down_token_id)

    if obi_up is None or obi_down is None:
        return None  # book not ready yet

    # EWMA signal
    ewma = _ewma_signal(coin)
    if ewma is None:
        return None  # warming up

    # OBI direction: which token has more buy pressure?
    # Positive OBI on a token = bids > asks near mid = price likely to rise
    if obi_up >= MICRO_OBI_THRESHOLD and ewma >= MICRO_EWMA_THRESHOLD:
        direction = "up"
        token_id  = market.up_token_id
        price     = market.up_price
        obi_val   = obi_up
    elif obi_down >= MICRO_OBI_THRESHOLD and ewma <= -MICRO_EWMA_THRESHOLD:
        direction = "down"
        token_id  = market.down_token_id
        price     = market.down_price
        obi_val   = obi_down
    else:
        return None  # no clear signal

    # Price filter
    if not (MICRO_MIN_PRICE <= price <= MICRO_MAX_PRICE):
        return None

    # EV guard: price + fee must leave positive expected value
    if price + MICRO_TAKER_FEE >= 0.98:
        return None

    return MicroSignal(
        coin=coin, direction=direction, token_id=token_id,
        price=price, obi=obi_val, ewma=ewma, mode=mode,
    )


# ── Order placement ───────────────────────────────────────────────────────────
def _clob_best_ask(token_id: str) -> float | None:
    """Fetch best ask from live order book. Tries order_book first, falls back to REST."""
    # Try order_book (WS-updated)
    ba = order_book.best_ask(token_id)
    if ba is not None and 0.01 < ba < 0.99:
        return ba
    # Fallback: REST
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=4,
        )
        resp.raise_for_status()
        book = resp.json()
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        real = [a for a in asks if 0.01 < float(a["price"]) < 0.99]
        return float(real[0]["price"]) if real else None
    except Exception:
        return None


def _clob_best_bid(token_id: str) -> float | None:
    """Fetch best bid from live order book."""
    bb = order_book.best_bid(token_id)
    if bb is not None and 0.01 < bb < 0.99:
        return bb
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=4,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        real = [b for b in bids if 0.01 < float(b["price"]) < 0.99]
        return float(real[0]["price"]) if real else None
    except Exception:
        return None


async def _place_taker_order(
    client, token_id: str, shares: float, poly_price: float, label: str
) -> tuple[bool, str, float]:
    """Aggressive GTC at ask + 2 ticks. Returns (success, order_id, fill_price)."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    ask = _clob_best_ask(token_id)
    fill_price = round(min(0.98, (ask or poly_price) + 0.002), 4)
    size = round(shares, 2)

    try:
        signed = client.create_order(OrderArgs(
            token_id=token_id, price=fill_price, size=size, side=BUY,
        ))
        resp     = client.post_order(signed, OrderType.GTC)
        success  = isinstance(resp, dict) and resp.get("success", True)
        order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
        if success:
            console.print(f"  [green]✅ {label}: {size:.0f} sh @ ${fill_price:.4f}[/green]")
        else:
            console.print(f"  [red]❌ {label}: rejected — {resp}[/red]")
        return success, order_id, fill_price
    except Exception as e:
        console.print(f"  [red]❌ {label} failed: {e}[/red]")
        return False, "", fill_price


async def _place_maker_order(
    client, token_id: str, shares: float, poly_price: float, label: str
) -> tuple[bool, str, float]:
    """GTC limit bid just inside the spread (best_bid + 0.005).
    Returns (success, order_id, bid_price).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    bid = _clob_best_bid(token_id)
    # One tick above best bid, capped at poly mid - 0.01 (don't accidentally cross the book)
    bid_price = round(min(poly_price - 0.01, (bid or poly_price - 0.02) + 0.005), 4)
    bid_price = max(MICRO_MIN_PRICE, bid_price)
    size = round(shares, 2)

    try:
        signed = client.create_order(OrderArgs(
            token_id=token_id, price=bid_price, size=size, side=BUY,
        ))
        resp     = client.post_order(signed, OrderType.GTC)
        success  = isinstance(resp, dict) and resp.get("success", True)
        order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
        if success:
            console.print(f"  [cyan]📋 {label}: maker {size:.0f} sh @ ${bid_price:.4f}[/cyan]")
        else:
            console.print(f"  [red]❌ {label} maker: rejected — {resp}[/red]")
        return success, order_id, bid_price
    except Exception as e:
        console.print(f"  [red]❌ {label} maker failed: {e}[/red]")
        return False, "", bid_price


async def _place_exit_sell(
    client, token_id: str, shares: float, fill_price: float, label: str
) -> tuple[bool, str, float]:
    """Place a GTC limit SELL at fill_price + MICRO_A_TP_PCT.
    Returns (success, order_id, exit_price).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    # Sell at fill_price + TP, capped at 0.98
    exit_price = round(min(0.98, fill_price * (1 + MICRO_A_TP_PCT)), 4)
    size = round(shares, 2)

    try:
        signed = client.create_order(OrderArgs(
            token_id=token_id, price=exit_price, size=size, side=SELL,
        ))
        resp     = client.post_order(signed, OrderType.GTC)
        success  = isinstance(resp, dict) and resp.get("success", True)
        order_id = resp.get("orderID", resp.get("id", "")) if isinstance(resp, dict) else ""
        if success:
            console.print(f"  [yellow]📤 {label}: exit sell {size:.0f} sh @ ${exit_price:.4f} (TP+{MICRO_A_TP_PCT*100:.0f}%)[/yellow]")
        else:
            console.print(f"  [red]❌ {label} exit sell rejected — {resp}[/red]")
        return success, order_id, exit_price
    except Exception as e:
        console.print(f"  [red]❌ {label} exit sell failed: {e}[/red]")
        return False, "", exit_price


async def _cancel_order(client, order_id: str) -> bool:
    try:
        client.cancel(order_id)
        return True
    except Exception:
        try:
            client.cancel_orders([order_id])
            return True
        except Exception as e:
            console.print(f"[yellow]  Cancel {order_id[:8]}... failed: {e}[/yellow]")
            return False


async def _is_filled(client, order_id: str) -> bool:
    try:
        live = client.get_orders()
        live_ids = {o.get("id", o.get("orderID", "")) for o in live}
        return order_id not in live_ids
    except Exception:
        return False


# ── Resolution ────────────────────────────────────────────────────────────────
def _resolve_winner(coin: str, window_ts: int) -> str | None:
    """Poll Gamma API until market resolves. Returns 'up', 'down', or None."""
    prefix = crypto_markets.COIN_SLUGS.get(coin)
    if not prefix:
        return None

    slug = f"{prefix}-{window_ts}"
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        market = data[0] if isinstance(data, list) and data else {}
        if not market.get("closed"):
            return None
        raw_outcomes = market.get("outcomes", "[]")
        raw_prices   = market.get("outcomePrices", "[]")
        import json as _json
        outcomes = _json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices   = _json.loads(raw_prices)   if isinstance(raw_prices, str)   else raw_prices
        for i, outcome in enumerate(outcomes):
            if i < len(prices) and float(prices[i]) >= 0.99:
                return outcome.lower()
    except Exception:
        pass
    return None


# ── Display ───────────────────────────────────────────────────────────────────
def _build_display(
    markets: dict[str, "CryptoMarket | None"],
    positions: dict[str, "MicroPosition | None"],
    bankroll: MicroBankroll,
    pending_a: dict[str, dict],
) -> Table:
    now = time.time()
    t = Table(show_header=True, header_style="bold", show_edge=False, padding=(0, 1))
    t.add_column("Coin",        width=5)
    t.add_column("OBI↑ bid/ask", width=16)   # UP token: OBI + best bid/ask
    t.add_column("OBI↓ bid/ask", width=16)   # DOWN token: OBI + best bid/ask
    t.add_column("EWMA",        width=13)
    t.add_column("T-",          width=6)
    t.add_column("Mode",        width=5)
    t.add_column("Position",    width=30)

    wr = f"{bankroll.wins}W/{bankroll.losses}L" if bankroll.wins + bankroll.losses else "—"
    ws_src = "[red]REST fallback[/red]" if ws_feed._using_fallback else "[green]WS live[/green]"
    price_lag = int(now - ws_feed._last_price_event) if ws_feed._update_count > 0 else -1
    lag_str = f" [yellow]lag={price_lag}s[/yellow]" if price_lag > 5 else ""

    t.title = (
        f"[bold]🔬 Micro Bot[/bold]  "
        f"${bankroll.balance:.2f} ({wr})  "
        f"loss=${bankroll.daily_loss:.2f}/{DAILY_LOSS_LIMIT:.0f}  "
        f"{ws_src}{lag_str}  "
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

    for coin in MICRO_COINS:
        market  = markets.get(coin)
        pos     = positions.get(coin)
        pending = pending_a.get(coin)

        if market is None:
            t.add_row(coin, "—", "—", "—", "—", "—", "[dim]discovering...[/dim]")
            continue

        secs = market.seconds_remaining
        if secs > 60:
            t_str = Text(f"{secs//60}:{secs%60:02d}", style="white")
        elif secs > 0:
            t_str = Text(f"0:{secs:02d}", style="bold red")
        else:
            t_str = Text("done", style="dim")

        book_ready = order_book.is_ready(market.up_token_id)

        def book_col(token_id, label):
            """OBI + bid/ask for one token in a single cell."""
            obi_v = order_book.obi(token_id)
            bb_v  = order_book.best_bid(token_id)
            ba_v  = order_book.best_ask(token_id)
            if obi_v is None:
                return Text(f"{label} warm…", style="dim")
            c = "green bold" if obi_v > MICRO_OBI_THRESHOLD else ("red bold" if obi_v < -MICRO_OBI_THRESHOLD else "white")
            ba_part = f" {bb_v:.3f}/{ba_v:.3f}" if bb_v and ba_v else ""
            return Text(f"{obi_v:+.3f}{ba_part}", style=c)

        # EWMA — from Binance time-decay fast/slow
        ewma      = _ewma_signal(coin)
        binance_p = binance_feed.get_price(coin)
        if ewma is None:
            ewma_str = Text("warm…", style="dim")
        else:
            c = "green bold" if ewma > MICRO_EWMA_THRESHOLD else ("red bold" if ewma < -MICRO_EWMA_THRESHOLD else "white")
            b_str = f" ${binance_p:,.0f}" if binance_p else ""
            ewma_str = Text(f"{ewma:+.4f}%{b_str}", style=c)

        # Mode
        if secs > MICRO_A_ENTRY_SECS:
            mode_str = Text("A", style="cyan")
        elif secs <= MICRO_B_ENTRY_SECS and secs > 0:
            mode_str = Text("B", style="bold yellow")
        else:
            mode_str = Text("—", style="dim")

        # Position / pending order
        if pos and pos.status == "open":
            hold_secs = int(pos.age)
            exit_part = ""
            if pos.mode == "A" and pos.exit_order_id:
                exit_part = f" → sell@${pos.exit_price:.3f}"
            elif pos.mode == "A" and not pos.exit_order_id:
                exit_part = " → placing sell…"
            pos_str = (
                f"[green]{pos.direction.upper()} {pos.shares:.0f}sh "
                f"@${pos.fill_price:.3f} [{pos.mode}]{exit_part} +{hold_secs}s[/green]"
            )
        elif pending:
            age = now - pending["placed_at"]
            state = pending.get("state", "entry")
            if state == "entry":
                pos_str = (
                    f"[cyan]MAKER {pending['direction'].upper()} "
                    f"{pending['shares']:.0f}sh bid@${pending['bid_price']:.3f} "
                    f"({age:.0f}/{MICRO_A_HOLD_SECS}s)[/cyan]"
                )
            else:
                pos_str = (
                    f"[yellow]SELL {pending['direction'].upper()} "
                    f"{pending['shares']:.0f}sh ask@${pending.get('exit_price', 0):.3f} "
                    f"({age:.0f}/{MICRO_A_HOLD_SECS}s)[/yellow]"
                )
        else:
            pos_str = "[dim]—[/dim]"

        t.add_row(
            coin,
            book_col(market.up_token_id,   "UP"),
            book_col(market.down_token_id, "DN"),
            ewma_str, t_str, mode_str, pos_str,
        )

    return t


# ── Main bot ──────────────────────────────────────────────────────────────────
async def run_micro_bot():
    if VPN_REQUIRED:
        ensure_vpn()

    # Init CLOB client
    client = init_client(PRIVATE_KEY, signature_type=SIGNATURE_TYPE, funder=FUNDER)

    # Load bankroll
    bankroll = MicroBankroll.load()
    console.print(f"[green]Micro bot started — ${bankroll.balance:.2f} bankroll[/green]")
    tg.send_message(
        f"🔬 Micro Bot started\n"
        f"Coins: {', '.join(MICRO_COINS)} | Window: {_micro_window}m\n"
        f"OBI≥{MICRO_OBI_THRESHOLD} EWMA≥{MICRO_EWMA_THRESHOLD}% | "
        f"Mode A T>{MICRO_A_ENTRY_SECS}s, Mode B T≤{MICRO_B_ENTRY_SECS}s\n"
        f"Bankroll: ${bankroll.balance:.2f}"
    )

    # Start background feeds
    get_initial_prices()
    asyncio.create_task(connect_binance())
    asyncio.create_task(poll_poly_prices(MICRO_COINS, interval=3.0))

    # State
    markets:     dict[str, CryptoMarket | None]  = {c: None for c in MICRO_COINS}
    positions:   dict[str, MicroPosition | None] = {c: None for c in MICRO_COINS}
    pending_a:   dict[str, dict]                 = {}   # coin -> {order_id, direction, shares, placed_at, ...}
    traded_windows: dict[str, int]               = {}   # coin -> window_ts (already traded)

    last_display = 0.0
    last_discover = 0.0

    # ── Initial WS token registration (before starting ws_feed.run) ──────────
    for coin in MICRO_COINS:
        m = discover_market_tokens(coin)  # doesn't require is_active
        if m:
            markets[coin] = m
            ws_feed.register_tokens(coin, m.up_token_id, m.down_token_id)

    ws_task = asyncio.create_task(ws_feed.run(MICRO_COINS))

    _start_config_watcher()

    _last_ws_resub_ts: int = 0   # track last window we force-reconnected on

    while True:
        now    = time.time()
        cur_ts = get_current_window_timestamp()

        # ── Discover markets (every 10s) ──────────────────────────────────
        if now - last_discover >= 10:
            last_discover = now
            new_tokens_this_tick: list[str] = []

            for coin in MICRO_COINS:
                # Use discover_market_tokens for WS — doesn't require accepting_orders
                m_ws = discover_market_tokens(coin)
                # Also try discover_market for active price + seconds_remaining
                m_active = discover_market(coin) or m_ws
                if not m_active:
                    continue

                old = markets.get(coin)
                if old is None or old.slug != m_active.slug:
                    console.print(f"[dim][micro] {coin}: new window {m_active.slug} ({m_active.seconds_remaining}s)[/dim]")
                    markets[coin] = m_active
                    if m_ws:
                        ws_feed.register_tokens(coin, m_ws.up_token_id, m_ws.down_token_id)
                        new_tokens_this_tick.append(coin)

            # At window boundary: reconnect first (kills zombies), then flush
            # Same pattern as v5 maker — PING/PONG can stay alive while
            # server silently stops routing price events to old token IDs.
            if new_tokens_this_tick and cur_ts != _last_ws_resub_ts:
                _last_ws_resub_ts = cur_ts
                await ws_feed.reconnect()

            await ws_feed.flush_pending_tokens()

        # ── Per-coin logic ────────────────────────────────────────────────
        for coin in MICRO_COINS:
            market = markets.get(coin)
            if not market or not market.is_active:
                continue

            secs    = market.seconds_remaining
            win_ts  = get_current_window_timestamp()

            # ── Handle pending Mode A orders (state machine) ──────────────
            if coin in pending_a:
                pa    = pending_a[coin]
                age   = now - pa["placed_at"]
                state = pa.get("state", "entry")

                if state == "entry":
                    # Waiting for maker bid to fill
                    filled = await _is_filled(client, pa["order_id"]) if pa["order_id"] else False
                    if filled:
                        # Bid filled — immediately place limit sell exit
                        label_exit = f"{coin} {pa['direction'].upper()} A-exit"
                        ok_sell, sell_id, exit_px = await _place_exit_sell(
                            client, pa["token_id"], pa["shares"], pa["bid_price"], label_exit
                        )
                        console.print(f"[green]  [{coin}] Mode A FILLED @ ${pa['bid_price']:.4f} → sell @ ${exit_px:.4f}[/green]")
                        _log(f"FILL_A: {coin} {pa['direction']} {pa['shares']:.0f}sh @ ${pa['bid_price']:.4f} → sell @ ${exit_px:.4f}")
                        if ok_sell:
                            pa["state"]        = "exit"
                            pa["exit_order_id"] = sell_id
                            pa["exit_price"]    = exit_px
                            pa["placed_at"]     = now  # reset timer for exit phase
                        else:
                            # Sell order failed — hold to resolution as fallback
                            positions[coin] = MicroPosition(
                                coin=coin, direction=pa["direction"],
                                token_id=pa["token_id"], fill_price=pa["bid_price"],
                                shares=pa["shares"], cost=pa["cost"],
                                window_ts=win_ts, mode="A", order_id=pa["order_id"],
                            )
                            del pending_a[coin]

                    elif age >= MICRO_A_HOLD_SECS or secs <= MICRO_B_ENTRY_SECS:
                        # Bid not filled in time — cancel
                        if pa["order_id"]:
                            await _cancel_order(client, pa["order_id"])
                        console.print(f"[yellow]  [{coin}] Mode A bid expired ({age:.0f}s) — cancelled[/yellow]")
                        del pending_a[coin]
                        # Don't mark traded — fall through to Mode B below

                elif state == "exit":
                    # Waiting for limit sell to fill
                    sell_id = pa.get("exit_order_id", "")
                    sell_filled = await _is_filled(client, sell_id) if sell_id else False
                    if sell_filled:
                        # Sold for profit — record as scalp win
                        profit = (pa["exit_price"] - pa["bid_price"]) * pa["shares"]
                        bankroll.balance += pa["cost"] + profit
                        _log(f"SOLD_A: {coin} {pa['direction']} profit=${profit:+.2f} exit=${pa['exit_price']:.4f}")
                        tg.send_message(
                            f"💰 MICRO SCALP {pa['direction'].upper()} {coin} [A]\n"
                            f"Buy ${pa['bid_price']:.4f} → Sell ${pa['exit_price']:.4f}\n"
                            f"Profit: +${profit:.2f}  Bankroll: ${bankroll.balance:.2f}"
                        )
                        console.print(f"[green]  [{coin}] Mode A SCALP SOLD profit=+${profit:.2f}[/green]")
                        bankroll._save()
                        del pending_a[coin]
                        traded_windows[coin] = win_ts

                    elif age >= MICRO_A_HOLD_SECS:
                        # Sell limit not filled in time — cancel and decide fate
                        if sell_id:
                            await _cancel_order(client, sell_id)

                        # Check current price — only exit aggressively if still profitable
                        current_px = poly_feed.get_price(pa["token_id"]) or order_book.mid(pa["token_id"])
                        still_profitable = current_px and current_px > pa["bid_price"]

                        if secs <= MICRO_A_FALLBACK_SECS or not still_profitable:
                            # T is very low OR price dropped below entry — hold to resolution
                            reason = f"T={secs}s" if secs <= MICRO_A_FALLBACK_SECS else f"price dropped to ${current_px:.3f} < entry ${pa['bid_price']:.3f}"
                            console.print(f"[yellow]  [{coin}] Mode A sell timed out — holding to resolution ({reason})[/yellow]")
                            positions[coin] = MicroPosition(
                                coin=coin, direction=pa["direction"],
                                token_id=pa["token_id"], fill_price=pa["bid_price"],
                                shares=pa["shares"], cost=pa["cost"],
                                window_ts=win_ts, mode="A", order_id=pa["order_id"],
                            )
                        else:
                            # Still above entry and enough time — aggressive taker sell
                            console.print(f"[yellow]  [{coin}] Mode A sell timed out — aggressive exit (price=${current_px:.3f})[/yellow]")
                            ok_agg, _, agg_px = await _place_taker_order(
                                client, pa["token_id"], pa["shares"],
                                current_px, f"{coin} A-agg-exit"
                            )
                            profit = (agg_px - pa["bid_price"]) * pa["shares"]
                            bankroll.balance += pa["cost"] + profit
                            _log(f"AGG_EXIT_A: {coin} {pa['direction']} profit=${profit:+.2f}")
                            tg.send_message(
                                f"⚡ MICRO AGG EXIT {coin} [A]\n"
                                f"Buy ${pa['bid_price']:.4f} → Sell ${agg_px:.4f}\n"
                                f"Profit: {profit:+.2f}  Bankroll: ${bankroll.balance:.2f}"
                            )
                            bankroll._save()
                        del pending_a[coin]
                        traded_windows[coin] = win_ts

            # ── Skip if already traded or position open ───────────────────
            if coin in pending_a:
                continue
            if positions.get(coin) and positions[coin].status == "open":
                continue
            if traded_windows.get(coin) == win_ts:
                continue
            if bankroll.daily_limit_hit:
                continue

            # ── Evaluate signal ───────────────────────────────────────────
            sig = evaluate_signal(coin, market)
            if sig is None:
                continue

            # ── Execute ───────────────────────────────────────────────────
            cost       = bankroll.bet_size()
            shares     = round(cost / sig.price, 2)
            label      = f"{coin} {sig.direction.upper()} {sig.mode}"
            console.print(
                f"[bold]🔬 MICRO {label}[/bold]  "
                f"OBI={sig.obi:+.3f} EWMA={sig.ewma:+.4f}%  "
                f"price=${sig.price:.4f}  T-{secs}s"
            )
            _log(f"ENTRY: {coin} {sig.direction} mode={sig.mode} price={sig.price:.4f} "
                 f"obi={sig.obi:+.3f} ewma={sig.ewma:+.4f} cost=${cost:.2f}")
            tg.send_message(
                f"🔬 MICRO {sig.direction.upper()} {coin} [{sig.mode}]\n"
                f"OBI={sig.obi:+.3f} EWMA={sig.ewma:+.4f}%\n"
                f"Price=${sig.price:.4f} Cost=${cost:.2f} T-{secs}s"
            )

            if sig.mode == "A":
                ok, order_id, bid_price = await _place_maker_order(
                    client, sig.token_id, shares, sig.price, label
                )
                if ok:
                    bankroll.balance -= cost
                    pending_a[coin] = {
                        "order_id": order_id, "direction": sig.direction,
                        "token_id": sig.token_id, "shares": shares,
                        "bid_price": bid_price, "cost": cost,
                        "placed_at": now,
                    }
            else:  # Mode B — taker, hold to resolution
                ok, order_id, fill_price = await _place_taker_order(
                    client, sig.token_id, shares, sig.price, label
                )
                if ok:
                    bankroll.balance -= cost
                    positions[coin] = MicroPosition(
                        coin=coin, direction=sig.direction,
                        token_id=sig.token_id, fill_price=fill_price,
                        shares=shares, cost=cost, window_ts=win_ts,
                        mode="B", order_id=order_id,
                    )
                    traded_windows[coin] = win_ts

        # ── Mode B / Mode A-fallback: TP check + exit sell tracking ─────
        for coin, pos in list(positions.items()):
            if not pos or pos.status != "open":
                continue

            market = markets.get(coin)
            secs   = market.seconds_remaining if market else 0

            if not pos.exit_order_id:
                # No exit sell placed yet — check if TP hit
                current_px = poly_feed.get_price(pos.token_id) or order_book.mid(pos.token_id)
                tp_target  = pos.fill_price * (1 + MICRO_B_TP_PCT)
                if current_px and current_px >= tp_target and secs > 5:
                    label = f"{coin} {pos.direction.upper()} TP"
                    console.print(
                        f"[bold yellow]🎯 TP HIT {coin} [{pos.mode}]  "
                        f"entry=${pos.fill_price:.3f} now=${current_px:.3f} "
                        f"(+{(current_px/pos.fill_price-1)*100:.1f}%)[/bold yellow]"
                    )
                    ok_tp, tp_id, tp_px = await _place_exit_sell(
                        client, pos.token_id, pos.shares, pos.fill_price, label
                    )
                    if ok_tp:
                        pos.exit_order_id  = tp_id
                        pos.exit_price     = tp_px
                        pos.exit_placed_at = now
            else:
                # Exit sell already placed — check if it filled
                tp_filled = await _is_filled(client, pos.exit_order_id)
                if tp_filled:
                    profit = (pos.exit_price - pos.fill_price) * pos.shares
                    bankroll.balance += pos.cost + profit
                    bankroll._save()
                    _log(f"TP_SOLD: {coin} {pos.direction} mode={pos.mode} "
                         f"entry=${pos.fill_price:.4f} exit=${pos.exit_price:.4f} profit=${profit:+.2f}")
                    tg.send_message(
                        f"💰 MICRO TP {pos.direction.upper()} {coin} [{pos.mode}]\n"
                        f"Entry ${pos.fill_price:.4f} → Exit ${pos.exit_price:.4f}\n"
                        f"Profit: +${profit:.2f}  Bankroll: ${bankroll.balance:.2f}"
                    )
                    console.print(
                        f"[green]💰 [{coin}] TP SOLD [{pos.mode}] "
                        f"${pos.fill_price:.3f}→${pos.exit_price:.3f} profit=+${profit:.2f}[/green]"
                    )
                    positions[coin] = None
                    traded_windows[coin] = pos.window_ts

        # ── Resolve closed positions (Mode B + Mode A fallback) ──────────
        for coin, pos in list(positions.items()):
            if not pos or pos.status not in ("open",):
                continue
            market = markets.get(coin)
            if market and market.seconds_remaining > 10:
                continue  # still running

            winner = _resolve_winner(coin, pos.window_ts)
            if winner is None:
                continue  # not settled yet

            pos.status = "won" if winner == pos.direction else "lost"
            bankroll.record_result(pos.cost, pos.payout, coin, pos.direction, pos.mode)
            pnl = pos.payout - pos.cost
            console.print(
                f"{'[green]✅' if pos.status == 'won' else '[red]❌'} "
                f"{coin} {pos.direction.upper()} [{pos.mode}] "
                f"→ {pos.status.upper()} {pnl:+.2f}[/]"
            )

            # Trigger redemption
            try:
                asyncio.create_task(asyncio.to_thread(
                    check_and_redeem, client, 10, False
                ))
            except Exception:
                pass

            positions[coin] = None
            traded_windows[coin] = pos.window_ts

        # ── Display ───────────────────────────────────────────────────────
        if now - last_display >= STATUS_INTERVAL:
            last_display = now
            console.print(_build_display(markets, positions, bankroll, pending_a))

        await asyncio.sleep(0.2)


def main():
    asyncio.run(run_micro_bot())


if __name__ == "__main__":
    main()

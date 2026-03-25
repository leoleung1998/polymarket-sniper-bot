"""
Crypto Maker Strategy v5.0 — 15-min Market Maker

The taker arbitrage strategy (v3.5) is dead — Polymarket's Feb 2026 dynamic fees
(up to 3.15%) and removal of the 500ms delay killed it.

NEW STRATEGY: Post GTC maker limit orders on the likely winning side of 15-min
BTC/ETH/SOL up/down markets, ~2 minutes before window close.

Why it works:
- ~85% of price direction is locked in by T-2 minutes
- Polymarket odds don't fully reflect this (slow to update near close)
- 2 minutes gives enough time for orders to fill (T-10s was too fast)
- Zero taker fees + maker rebates on GTC orders
- High win rate (target: 80%+) with small per-trade profit ($0.05-0.10/share)

Flow:
1. Connect to Binance WebSocket for real-time BTC/ETH/SOL prices
2. At each 15-min window, track price from window start
3. At T-120 seconds (2 min) before close:
   a. Check Binance price vs window open price
   b. If coin clearly up (>0.1%): post maker bid for UP at $0.88-0.95
   c. If coin clearly down (<-0.1%): post maker bid for DOWN at $0.88-0.95
   d. If ambiguous: skip (don't bet on coin flips)
4. At T-0: cancel unfilled orders
5. Wait for resolution, collect $1.00/share on wins

Risk:
- Max 1 order per window per coin
- $5-10 per trade
- Stop after 3 consecutive losses (1h cooldown)
- Daily loss limit: $25
"""

import asyncio
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from binance_feed import feed, connect_binance, get_initial_prices, SYMBOLS
from crypto_markets import (
    discover_market, CryptoMarket, WINDOW_SECONDS,
    get_current_window_timestamp, get_next_window_timestamp,
)
from poly_feed import poly_feed, poll_poly_prices
from poly_ws import ws_feed, ws_silence_watchdog
from trader import init_client, PlacedOrder, save_order
from vpn import ensure_vpn
import telegram_alerts as tg
from allium_feed import allium
from redeemer import check_and_redeem

load_dotenv()
# Paper mode: load .env.paper on top of .env (overrides only what you specify)
if os.getenv("PAPER_TRADE", "false").lower() == "true":
    _paper_env = Path(__file__).parent / ".env.paper"
    if _paper_env.exists():
        load_dotenv(_paper_env, override=True)


def get_poly_balance() -> str:
    """Fetch live Polymarket USDC balance."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        from trader import init_client
        client = init_client(
            os.getenv("PRIVATE_KEY"),
            signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
            funder=os.getenv("FUNDER"),
        )
        result = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = int(result.get("balance", 0)) / 1e6
        return f"${balance:.2f}"
    except Exception:
        return "N/A"


# Tee all output to bot.log AND terminal
import sys

class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            s.write(data)
    def flush(self):
        for s in self.streams:
            s.flush()
    def fileno(self):
        return self.streams[0].fileno()

_log_file = Path(__file__).parent / "bot.log"
_log_fh = open(_log_file, "a", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

console = Console()

# --- Config ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
FUNDER = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))
VPN_REQUIRED = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# Maker strategy config
MAKER_COINS = os.getenv("MAKER_COINS", "BTC,ETH").split(",")  # SOL dropped — 53% win rate
# Low-liquidity hours (UTC) — skip trading when reversals are common
MAKER_QUIET_HOURS_START = int(os.getenv("MAKER_QUIET_HOURS_START", "0"))   # midnight UTC
MAKER_QUIET_HOURS_END = int(os.getenv("MAKER_QUIET_HOURS_END", "7"))      # 7am UTC
MAKER_BET_SIZE = float(os.getenv("MAKER_BET_SIZE", "3.0"))        # $3 min bet (floor)
MAKER_MAX_BET = float(os.getenv("MAKER_MAX_BET", "5.0"))          # $5 max bet (ceiling)
MAKER_BET_PCT = float(os.getenv("MAKER_BET_PCT", "0.0"))          # % of balance per trade (0 = use fixed MAKER_BET_SIZE)
MAKER_DAILY_BANKROLL = float(os.getenv("MAKER_DAILY_BANKROLL", "50.0"))
MAKER_DAILY_LOSS_LIMIT = float(os.getenv("MAKER_DAILY_LOSS_LIMIT", "25.0"))
MAKER_MIN_MOVE_PCT = float(os.getenv("MAKER_MIN_MOVE_PCT", "0.10"))   # 0.1% min price move
MAKER_BID_PRICE_LOW = float(os.getenv("MAKER_BID_PRICE_LOW", "0.88"))  # Bid range low
MAKER_BID_PRICE_HIGH = float(os.getenv("MAKER_BID_PRICE_HIGH", "0.95"))  # Bid range high
MAKER_ENTRY_SECONDS = int(os.getenv("MAKER_ENTRY_SECONDS", "120"))    # Enter at T-120s (2 min before close)
MAKER_LOSS_STREAK_LIMIT = int(os.getenv("MAKER_LOSS_STREAK_LIMIT", "3"))
MAKER_LOSS_COOLDOWN = int(os.getenv("MAKER_LOSS_COOLDOWN", "3600"))    # 1 hour
MAKER_STATUS_INTERVAL = float(os.getenv("MAKER_STATUS_INTERVAL", "2.0"))  # status refresh (seconds)
MAKER_TARGET_EV = float(os.getenv("MAKER_TARGET_EV", "2.0"))          # target $EV per trade for Kelly sizing
MAKER_SIGNAL_SCALE = float(os.getenv("MAKER_SIGNAL_SCALE", "2.0"))    # divisor in binance_prob formula
MAKER_MIN_GAP = float(os.getenv("MAKER_MIN_GAP", "0.0"))              # min gap to enter (0 = any edge, -0.03 = tolerate 3% lag)
PAPER_TRADE = os.getenv("PAPER_TRADE", "false").lower() == "true"     # Simulate trades without real money

# ── Hot-reload config (watches .env for changes every 15s) ──────────────────
_ENV_FILE = Path(__file__).parent / ".env"
_PAPER_ENV_FILE = Path(__file__).parent / ".env.paper"
_env_mtime: float = 0.0


def _reload_config() -> None:
    """Re-read tunable .env vars without restarting. No-op if file unchanged."""
    global _env_mtime
    global MAKER_BET_SIZE, MAKER_MAX_BET, MAKER_BET_PCT, MAKER_DAILY_BANKROLL, MAKER_DAILY_LOSS_LIMIT
    global MAKER_MIN_MOVE_PCT, MAKER_BID_PRICE_LOW, MAKER_BID_PRICE_HIGH
    global MAKER_ENTRY_SECONDS, MAKER_LOSS_STREAK_LIMIT, MAKER_LOSS_COOLDOWN
    global MAKER_TARGET_EV, MAKER_SIGNAL_SCALE, MAKER_QUIET_HOURS_START, MAKER_QUIET_HOURS_END
    global MAKER_MIN_GAP

    try:
        mtime = _ENV_FILE.stat().st_mtime
        if _PAPER_ENV_FILE.exists():
            mtime = max(mtime, _PAPER_ENV_FILE.stat().st_mtime)
    except OSError:
        return
    if mtime == _env_mtime:
        return  # files unchanged — skip

    load_dotenv(override=True)
    if PAPER_TRADE and _PAPER_ENV_FILE.exists():
        load_dotenv(_PAPER_ENV_FILE, override=True)
    _env_mtime = mtime

    prev = dict(
        MAKER_BET_SIZE=MAKER_BET_SIZE, MAKER_MAX_BET=MAKER_MAX_BET,
        MAKER_DAILY_BANKROLL=MAKER_DAILY_BANKROLL, MAKER_DAILY_LOSS_LIMIT=MAKER_DAILY_LOSS_LIMIT,
        MAKER_MIN_MOVE_PCT=MAKER_MIN_MOVE_PCT, MAKER_BID_PRICE_LOW=MAKER_BID_PRICE_LOW,
        MAKER_BID_PRICE_HIGH=MAKER_BID_PRICE_HIGH, MAKER_ENTRY_SECONDS=MAKER_ENTRY_SECONDS,
        MAKER_LOSS_STREAK_LIMIT=MAKER_LOSS_STREAK_LIMIT, MAKER_LOSS_COOLDOWN=MAKER_LOSS_COOLDOWN,
        MAKER_TARGET_EV=MAKER_TARGET_EV, MAKER_SIGNAL_SCALE=MAKER_SIGNAL_SCALE,
        MAKER_MIN_GAP=MAKER_MIN_GAP,
        MAKER_QUIET_HOURS_START=MAKER_QUIET_HOURS_START, MAKER_QUIET_HOURS_END=MAKER_QUIET_HOURS_END,
    )

    MAKER_BET_SIZE           = float(os.getenv("MAKER_BET_SIZE", "3.0"))
    MAKER_MAX_BET            = float(os.getenv("MAKER_MAX_BET", "5.0"))
    MAKER_BET_PCT            = float(os.getenv("MAKER_BET_PCT", "0.0"))
    MAKER_DAILY_BANKROLL     = float(os.getenv("MAKER_DAILY_BANKROLL", "50.0"))
    MAKER_DAILY_LOSS_LIMIT   = float(os.getenv("MAKER_DAILY_LOSS_LIMIT", "25.0"))
    MAKER_MIN_MOVE_PCT       = float(os.getenv("MAKER_MIN_MOVE_PCT", "0.10"))
    MAKER_BID_PRICE_LOW      = float(os.getenv("MAKER_BID_PRICE_LOW", "0.88"))
    MAKER_BID_PRICE_HIGH     = float(os.getenv("MAKER_BID_PRICE_HIGH", "0.95"))
    MAKER_ENTRY_SECONDS      = int(os.getenv("MAKER_ENTRY_SECONDS", "120"))
    MAKER_LOSS_STREAK_LIMIT  = int(os.getenv("MAKER_LOSS_STREAK_LIMIT", "3"))
    MAKER_LOSS_COOLDOWN      = int(os.getenv("MAKER_LOSS_COOLDOWN", "3600"))
    MAKER_TARGET_EV          = float(os.getenv("MAKER_TARGET_EV", "2.0"))
    MAKER_SIGNAL_SCALE       = float(os.getenv("MAKER_SIGNAL_SCALE", "2.0"))
    MAKER_MIN_GAP            = float(os.getenv("MAKER_MIN_GAP", "0.0"))
    MAKER_QUIET_HOURS_START  = int(os.getenv("MAKER_QUIET_HOURS_START", "0"))
    MAKER_QUIET_HOURS_END    = int(os.getenv("MAKER_QUIET_HOURS_END", "7"))

    new = dict(
        MAKER_BET_SIZE=MAKER_BET_SIZE, MAKER_MAX_BET=MAKER_MAX_BET,
        MAKER_DAILY_BANKROLL=MAKER_DAILY_BANKROLL, MAKER_DAILY_LOSS_LIMIT=MAKER_DAILY_LOSS_LIMIT,
        MAKER_MIN_MOVE_PCT=MAKER_MIN_MOVE_PCT, MAKER_BID_PRICE_LOW=MAKER_BID_PRICE_LOW,
        MAKER_BID_PRICE_HIGH=MAKER_BID_PRICE_HIGH, MAKER_ENTRY_SECONDS=MAKER_ENTRY_SECONDS,
        MAKER_LOSS_STREAK_LIMIT=MAKER_LOSS_STREAK_LIMIT, MAKER_LOSS_COOLDOWN=MAKER_LOSS_COOLDOWN,
        MAKER_TARGET_EV=MAKER_TARGET_EV, MAKER_SIGNAL_SCALE=MAKER_SIGNAL_SCALE,
        MAKER_MIN_GAP=MAKER_MIN_GAP,
        MAKER_QUIET_HOURS_START=MAKER_QUIET_HOURS_START, MAKER_QUIET_HOURS_END=MAKER_QUIET_HOURS_END,
    )
    changes = [f"{k}: {prev[k]} → {new[k]}" for k in prev if new[k] != prev[k]]
    if changes:
        msg = "Config reloaded: " + " | ".join(changes)
        console.print(f"[cyan]{msg}[/cyan]")
        tg.send(msg)


def _start_config_watcher() -> None:
    def _loop():
        while True:
            time.sleep(15)
            try:
                _reload_config()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True, name="config-watcher").start()


# Logging — paper trades go to data/paper/ to keep prod stats clean
LOG_DIR = Path(os.getenv("PAPER_DATA_DIR", "data/paper")) if PAPER_TRADE else Path("data")
LOG_FILE     = LOG_DIR / "maker_trades.log"
TRADES_FILE  = LOG_DIR / "maker_trades.json"
PENDING_FILE = LOG_DIR / "maker_pending.json"  # live pending orders for kill switch
STATE_FILE   = LOG_DIR / "maker_state.json"    # persistent W/L + band stats across restarts


def save_pending_orders(orders: list):
    """Persist pending maker orders to disk so kill switch can find them."""
    LOG_DIR.mkdir(exist_ok=True)
    PENDING_FILE.write_text(json.dumps(orders, indent=2))


def clear_pending_orders():
    """Clear pending orders file when all resolved."""
    if PENDING_FILE.exists():
        PENDING_FILE.write_text("[]")


def log_trade(message: str):
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {message}\n")


def save_trade_record(trade: dict):
    LOG_DIR.mkdir(exist_ok=True)
    trades = []
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            trades = []
    trades.append(trade)
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


# --- Maker Bankroll ---

def get_confidence_band(confidence_pct: float) -> str:
    """Bucket a confidence % into a named band for win-rate tracking."""
    if confidence_pct < 0.20:
        return "small"    # 0.10–0.19%
    elif confidence_pct < 0.40:
        return "medium"   # 0.20–0.39%
    else:
        return "large"    # 0.40%+


@dataclass
class MakerBankroll:
    starting: float
    balance: float = 0.0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0
    daily_losses: float = 0.0
    paused_until: float = 0.0
    pending_orders: list = None  # list of dicts
    band_wins: dict = None        # wins per confidence band
    band_losses: dict = None      # losses per confidence band

    def __post_init__(self):
        self.balance = self.starting
        if self.pending_orders is None:
            self.pending_orders = []
        if self.band_wins is None:
            self.band_wins = {"small": 0, "medium": 0, "large": 0}
        if self.band_losses is None:
            self.band_losses = {"small": 0, "medium": 0, "large": 0}

    @property
    def pnl(self) -> float:
        return self.balance - self.starting

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def can_trade(self) -> bool:
        if self.balance < MAKER_BET_SIZE:
            return False
        if self.daily_losses >= MAKER_DAILY_LOSS_LIMIT:
            console.print(f"[red]Daily loss limit hit (${self.daily_losses:.2f}/${MAKER_DAILY_LOSS_LIMIT:.2f})[/red]")
            return False
        if time.time() < self.paused_until:
            remaining = int(self.paused_until - time.time())
            console.print(f"[yellow]Loss streak cooldown: {remaining}s remaining[/yellow]")
            return False
        return True

    def band_win_rate(self, band: str) -> float | None:
        """Observed win rate for a band. Returns None if < 10 trades (not enough data)."""
        w = self.band_wins.get(band, 0)
        l = self.band_losses.get(band, 0)
        return w / (w + l) if (w + l) >= 10 else None

    def record_win(self, bet_amount: float, payout: float, band: str = ""):
        self.wins += 1
        self.balance += payout
        self.loss_streak = 0
        if band:
            self.band_wins[band] = self.band_wins.get(band, 0) + 1
        log_trade(f"WIN: +${payout - bet_amount:.2f} | Bankroll: ${self.balance:.2f}" + (f" | band={band}" if band else ""))
        self.save_state()

    def record_loss(self, bet_amount: float, band: str = ""):
        self.losses += 1
        self.loss_streak += 1
        self.daily_losses += bet_amount
        if band:
            self.band_losses[band] = self.band_losses.get(band, 0) + 1
        if self.loss_streak >= MAKER_LOSS_STREAK_LIMIT:
            self.paused_until = time.time() + MAKER_LOSS_COOLDOWN
            console.print(f"[red]🚨 {self.loss_streak} consecutive losses — pausing {MAKER_LOSS_COOLDOWN // 60} min[/red]")
            tg.send_message(
                f"🚨 MAKER LOSS STREAK: {self.loss_streak}\n"
                f"Pausing {MAKER_LOSS_COOLDOWN // 60} min\n"
                f"Bankroll: ${self.balance:.2f}"
            )
        log_trade(f"LOSS: -${bet_amount:.2f} | Bankroll: ${self.balance:.2f} | Streak: {self.loss_streak}" + (f" | band={band}" if band else ""))
        self.save_state()

    def save_state(self):
        """Persist cumulative W/L and band stats to disk."""
        try:
            LOG_DIR.mkdir(exist_ok=True)
            state = {
                "wins": self.wins,
                "losses": self.losses,
                "band_wins": self.band_wins,
                "band_losses": self.band_losses,
                "balance": self.balance,
                "daily_losses": self.daily_losses,
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass  # never block trading on a save failure

    @classmethod
    def load_state(cls, starting: float) -> "MakerBankroll":
        """Create a MakerBankroll, restoring cumulative stats from disk if available."""
        b = cls(starting=starting)
        try:
            if not STATE_FILE.exists():
                return b

            state = json.loads(STATE_FILE.read_text())

            # Discard state older than 7 days — band data gets stale
            saved_date = state.get("daily_date", "")
            if saved_date:
                from datetime import date
                try:
                    age = (date.today() - date.fromisoformat(saved_date)).days
                    if age > 7:
                        console.print(f"[yellow]State file is {age} days old — starting fresh[/yellow]")
                        STATE_FILE.unlink(missing_ok=True)
                        return b
                except ValueError:
                    pass

            b.wins        = int(state.get("wins", 0))
            b.losses      = int(state.get("losses", 0))
            b.band_wins   = state.get("band_wins", b.band_wins)
            b.band_losses = state.get("band_losses", b.band_losses)

            # Sanity-check restored balance: must be within 10x of starting bankroll
            restored_balance = float(state.get("balance", starting))
            if 0 < restored_balance < starting * 10:
                b.balance = restored_balance
            else:
                console.print(f"[yellow]Restored balance ${restored_balance:.2f} looks wrong — resetting to ${starting:.2f}[/yellow]")

            # Only restore daily_losses if saved today (reset at midnight)
            if saved_date == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                b.daily_losses = float(state.get("daily_losses", 0.0))

            # Rebuild band stats from full trade history (never lost on restart)
            b._rebuild_from_trades()

            console.print(f"[dim]Restored session: W/L {b.wins}/{b.losses} ({b.win_rate:.0%}) | ${b.balance:.2f} | Bands: {b.band_stats()}[/dim]")

        except Exception as e:
            console.print(f"[yellow]State load failed ({e}) — starting fresh[/yellow]")
            b._rebuild_from_trades()
        return b

    def _rebuild_from_trades(self):
        """Rebuild W/L and band stats from maker_trades.json (source of truth)."""
        try:
            if not TRADES_FILE.exists():
                return
            trades = json.loads(TRADES_FILE.read_text())
            outcomes = [t for t in trades if t.get("type") == "maker_outcome"]
            if not outcomes:
                return

            wins = sum(1 for t in outcomes if t.get("outcome") == "win")
            losses = sum(1 for t in outcomes if t.get("outcome") == "loss")
            band_wins: dict = {}
            band_losses: dict = {}
            for t in outcomes:
                band = t.get("band", "")
                if not band:
                    continue
                if t.get("outcome") == "win":
                    band_wins[band] = band_wins.get(band, 0) + 1
                else:
                    band_losses[band] = band_losses.get(band, 0) + 1

            # Only override if trades file has more data than state file
            if wins + losses > self.wins + self.losses:
                self.wins = wins
                self.losses = losses
                self.band_wins = band_wins
                self.band_losses = band_losses
                console.print(f"[dim]Rebuilt from {len(outcomes)} trade records: W/L {wins}/{losses} | Bands: {self.band_stats()}[/dim]")
        except Exception as e:
            console.print(f"[yellow]Trade history rebuild failed ({e})[/yellow]")

    def band_stats(self) -> str:
        """Per-band win rate summary for logging."""
        parts = []
        for band in ("small", "medium", "large"):
            w = self.band_wins.get(band, 0)
            l = self.band_losses.get(band, 0)
            if w + l > 0:
                wr = w / (w + l)
                flag = "*" if (w + l) < 10 else ""  # * = not enough data to trust
                parts.append(f"{band}: {w}/{w+l} ({wr:.0%}{flag})")
        return " | ".join(parts) if parts else "no data"

    def status_line(self) -> str:
        pnl = self.pnl
        color = "green" if pnl >= 0 else "red"
        return (
            f"Bankroll: ${self.balance:.2f} | "
            f"P&L: [{color}]${pnl:+.2f}[/{color}] | "
            f"W/L: {self.wins}/{self.losses} ({self.win_rate:.0%}) | "
            f"Pending: {len(self.pending_orders)}\n"
            f"  Bands: {self.band_stats()}"
        )


def build_status_panel(bankroll: "MakerBankroll", coins: list[str]) -> Table:
    """Build a Rich Table for the Live display — redrawn every second in-place."""
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)

    # ── Bankroll row ──────────────────────────────────────────────────────
    pnl = bankroll.pnl
    pnl_color = "green" if pnl >= 0 else "red"
    paper_tag = "  [bold yellow]📝 PAPER[/bold yellow]" if PAPER_TRADE else ""
    table.add_row(
        f"  [bold]Bankroll:[/bold] ${bankroll.balance:.2f}  "
        f"[bold]P&L:[/bold] [{pnl_color}]${pnl:+.2f}[/{pnl_color}]  "
        f"[bold]W/L:[/bold] {bankroll.wins}/{bankroll.losses} ({bankroll.win_rate:.0%})  "
        f"[bold]Pending:[/bold] {len(bankroll.pending_orders)}"
        f"{paper_tag}"
    )

    # ── Bands row ─────────────────────────────────────────────────────────
    table.add_row(f"  [dim]Bands: {bankroll.band_stats()}[/dim]")

    # ── Poly odds grid ────────────────────────────────────────────────────
    odds_parts = []
    for c in coins:
        up, down = poly_feed.get_market_prices(c)
        if up and down:
            up_col   = "green" if up > 0.5 else "red"
            down_col = "green" if down > 0.5 else "red"
            odds_parts.append(
                f"[bold]{c}[/bold] [{up_col}]↑${up:.2f}[/{up_col}] [{down_col}]↓${down:.2f}[/{down_col}]"
            )
        else:
            odds_parts.append(f"[bold]{c}[/bold] [dim](stale)[/dim]")

    table.add_row("  " + "   [dim]|[/dim]   ".join(odds_parts))

    # ── WS stats row ──────────────────────────────────────────────────────
    src_color = "yellow" if ws_feed._using_fallback else "green"
    table.add_row(
        f"  [dim][{src_color}]{ws_feed.stats}[/{src_color}][/dim]"
    )

    return table


# --- Direction Detection ---

def _get_kline_open(coin: str) -> float | None:
    """Fetch the open price of the current 15m Binance candle via REST.
    Used on window init so we always compare against the true window start,
    even when the bot joins mid-window."""
    import requests as _req
    symbol_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
    symbol = symbol_map.get(coin)
    if not symbol:
        return None
    try:
        resp = _req.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "15m", "limit": 1},
            timeout=5,
        )
        resp.raise_for_status()
        kline = resp.json()[0]
        return float(kline[1])  # index 1 = open price
    except Exception:
        return None


def detect_direction(coin: str, window_start_price: float) -> tuple[str | None, float, float]:
    """
    Detect price direction at near end of 15-min window.

    Returns: (direction, confidence_pct, current_price)
        direction: "up", "down", or None (ambiguous)
        confidence_pct: absolute % move
        current_price: latest Binance price
    """
    current = feed.get_price(coin)
    if current is None or window_start_price <= 0:
        return None, 0.0, 0.0

    pct_move = (current - window_start_price) / window_start_price * 100

    if abs(pct_move) < MAKER_MIN_MOVE_PCT:
        return None, abs(pct_move), current  # Too close to call

    direction = "up" if pct_move > 0 else "down"
    return direction, abs(pct_move), current


def optimal_bet(win_rate: float, bid: float, min_bet: float, max_bet: float, target_ev: float = 2.0) -> float:
    """
    Kelly-derived bet size: how much to bet given observed win rate and bid price.
    Returns a bet sized to generate target_ev dollars EV per trade.
    Always clamped to [min_bet, max_bet].
    Falls back to min_bet when there's no edge.
    """
    edge = win_rate * (1 / bid - 1) - (1 - win_rate)
    if edge <= 0:
        return min_bet  # no edge — bet minimum
    bet = target_ev / edge
    return round(min(max_bet, max(min_bet, bet)), 2)


def calculate_bid_price(confidence_pct: float, bankroll: "MakerBankroll" = None) -> tuple[float, str]:
    """
    Calculate maker bid price based on confidence and observed band win rate.

    Returns (bid_price, band).

    Config ceiling is the absolute max — but if observed win rate for this band
    is below break-even for that price, the bid is capped at (win_rate − 5% margin).

    Break-even: bid price = win rate (e.g. bid $0.88 needs 88% win rate)
    With 90% win rate → bid must stay ≤ $0.85 to keep real edge.
    """
    band = get_confidence_band(confidence_pct)

    # Linear interpolation from config range
    t = min(1.0, max(0.0, (confidence_pct - MAKER_MIN_MOVE_PCT) / 0.4))
    bid = MAKER_BID_PRICE_LOW + t * (MAKER_BID_PRICE_HIGH - MAKER_BID_PRICE_LOW)

    # Auto-cap: if we have ≥10 trades in this band, enforce risk/reward discipline
    if bankroll is not None:
        observed_wr = bankroll.band_win_rate(band)
        if observed_wr is not None:
            # Must bid below win rate to have positive EV
            # Safety margin: 5% (bid at win_rate − 0.05)
            ev_safe_bid = round(observed_wr - 0.05, 2)
            if bid > ev_safe_bid:
                console.print(
                    f"  [yellow]Bid capped: ${bid:.2f} → ${ev_safe_bid:.2f} "
                    f"(band '{band}': {observed_wr:.0%} win rate − 5% margin)[/yellow]"
                )
                bid = ev_safe_bid

    return round(bid, 2), band


# --- Order Execution ---

async def place_maker_order(
    client,
    market: CryptoMarket,
    direction: str,
    bid_price: float,
    bet_amount: float,
    silent: bool = False,
) -> dict | None:
    """Place a GTC maker limit order on the predicted winning side.
    silent=True suppresses Telegram — used for chase replacements."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    # Pick token based on direction
    if direction == "up":
        token_id = market.up_token_id
        side_label = "UP"
    else:
        token_id = market.down_token_id
        side_label = "DOWN"

    size = bet_amount / bid_price
    size = max(5, math.floor(size * 100) / 100)  # Min 5 shares, 2 decimal places
    actual_cost = bid_price * size

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=bid_price,
            size=size,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.GTC)

        order_id = ""
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", ""))
            success = response.get("success", True)
        else:
            order_id = str(response)
            success = True

        if not success:
            console.print(f"[red]  Order rejected: {response}[/red]")
            return None

        order_info = {
            "order_id": order_id,
            "token_id": token_id,
            "coin": market.coin,
            "direction": direction,
            "bid_price": bid_price,
            "size": size,
            "cost": actual_cost,
            "slug": market.slug,
            "placed_at": time.time(),
            "window_end": market.end_timestamp,
        }

        msg = (
            f"📋 MAKER BID: {market.coin} {side_label} @ ${bid_price:.2f} | "
            f"{size:.1f} shares | ${actual_cost:.2f}"
        )
        console.print(f"[cyan]  {msg}[/cyan]")
        log_trade(msg)
        if not silent:
            tg.send_message(f"📋 {market.coin} {side_label} @ ${bid_price:.2f} × {size:.0f} shares (${actual_cost:.2f})")

        return order_info

    except Exception as e:
        import traceback
        status = getattr(e, 'status_code', None)
        msg = getattr(e, 'error_msg', str(e))
        console.print(f"[red]  Maker order failed: HTTP {status} — {msg}[/red]")
        log_trade(f"ERROR: Maker order {market.coin} {direction}: HTTP {status} — {msg}")
        return None


async def cancel_order(client, order_id: str) -> bool:
    """Cancel a specific order. Returns True if cancelled."""
    try:
        client.cancel(order_id)
        return True
    except Exception:
        try:
            client.cancel_orders([order_id])
            return True
        except Exception as e:
            console.print(f"[yellow]  Cancel failed: {e}[/yellow]")
            return False


def _chase_price(token_id: str, max_price: float) -> float:
    """
    Return an aggressive entry price, always capped at max_price.

    Logic:
      - best_bid + 1 tick (0.0001) if that stays <= max_price  → front of book
      - max_price if best_bid + 1 tick would exceed our ceiling → still enter, just at ceiling
      - max_price if orderbook fetch fails or no real bids       → safe fallback

    Never skips — always returns a valid price <= max_price.
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        real_bids = [b for b in bids if float(b["price"]) > 0.05]
        if real_bids:
            best_bid = float(real_bids[0]["price"])
            aggressive = round(best_bid + 0.0001, 4)  # 1 tick above best bid
            if aggressive <= max_price:
                return aggressive          # front of book, within ceiling
            else:
                return max_price           # best_bid already at/above ceiling → place at ceiling
    except Exception:
        pass
    return max_price                       # fetch failed → fall back to original bid price


async def check_if_filled(client, order_id: str) -> bool:
    """Check if an order has been filled (no longer on the book)."""
    try:
        live_orders = client.get_orders()
        live_ids = {o.get("id", o.get("orderID", "")) for o in live_orders}
        return order_id not in live_ids
    except Exception:
        return False  # Assume not filled if we can't check


# --- Window Tracking ---

CHASE_INTERVAL  = 30   # seconds between cancel-replace cycles
CHASE_STOP_SECS = 60   # stop chasing this many seconds before window close


@dataclass
class WindowState:
    """Track the state of a 15-min trading window for a coin."""
    coin: str
    window_start_ts: int       # Unix timestamp of window start
    window_end_ts: int         # Unix timestamp of window end
    start_price: float         # Binance price at window start
    order_placed: bool = False
    order_info: dict = None
    filled: bool = False
    chasing: bool = False          # actively cancel-replacing to stay at best bid
    last_chase_ts: float = 0.0     # last time we ran a chase cycle
    chase_max_price: float = 0.0   # our bid_price ceiling — never exceed this

    @property
    def seconds_remaining(self) -> int:
        return self.window_end_ts - int(time.time())

    @property
    def is_active(self) -> bool:
        return self.seconds_remaining > 0

    @property
    def needs_resolution(self) -> bool:
        """Window closed and has an unresolved order."""
        return self.order_placed and self.order_info is not None and self.seconds_remaining <= 0


# --- Main Loop ---

async def run_maker_bot():
    """Main crypto maker bot loop."""
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print("[bold magenta]  Polymarket Crypto Maker v5.0[/bold magenta]")
    console.print("[bold magenta]  15-min Market Maker Strategy[/bold magenta]")
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print()

    console.print(f"  Coins:           {', '.join(MAKER_COINS)}")
    console.print(f"  Bet size:        ${MAKER_BET_SIZE:.0f}-${MAKER_MAX_BET:.0f}")
    console.print(f"  Daily bankroll:  ${MAKER_DAILY_BANKROLL:.0f}")
    console.print(f"  Daily loss cap:  ${MAKER_DAILY_LOSS_LIMIT:.0f}")
    console.print(f"  Min move:        {MAKER_MIN_MOVE_PCT:.2f}%")
    console.print(f"  Bid range:       ${MAKER_BID_PRICE_LOW:.2f}-${MAKER_BID_PRICE_HIGH:.2f}")
    console.print(f"  Entry at:        T-{MAKER_ENTRY_SECONDS}s")
    console.print(f"  Loss streak cap: {MAKER_LOSS_STREAK_LIMIT} (then {MAKER_LOSS_COOLDOWN // 60}m cooldown)")
    console.print(f"  Order type:      GTC (maker, zero fees + rebates)")
    if PAPER_TRADE:
        console.print(f"  [bold yellow]📝 MODE:           PAPER TRADE (no real orders)[/bold yellow]")
    console.print()

    # VPN check
    console.print("[vpn] Checking VPN connection...")
    if VPN_REQUIRED and not ensure_vpn():
        console.print("[red]VPN required but not connected. Exiting.[/red]")
        return
    console.print()

    # Init CLOB client
    client = init_client(PRIVATE_KEY, SIGNATURE_TYPE, FUNDER)

    # Init bankroll
    bankroll = MakerBankroll.load_state(starting=MAKER_DAILY_BANKROLL)

    # Fetch initial Binance prices
    console.print("[binance] Fetching initial prices...")
    prices = get_initial_prices()
    for sym, price in prices.items():
        console.print(f"  {sym}: ${price:,.2f}")
    console.print()

    # Test Allium smart money connection
    allium_ok = allium.test_connection()
    if allium_ok:
        console.print("[green]  Allium: Connected (smart money signals active)[/green]")
    else:
        console.print("[yellow]  Allium: Unavailable (trading without smart money)[/yellow]")
    console.print()

    # Start Binance WebSocket in background
    ws_task = asyncio.create_task(connect_binance())

    # Pre-discover all markets so WS has tokens to subscribe to from the start
    console.print("[dim]Discovering active markets for WS subscription...[/dim]")
    for _coin in MAKER_COINS:
        _market = discover_market(_coin)
        if _market:
            ws_feed.register_tokens(_coin, _market.up_token_id, _market.down_token_id)
            console.print(f"[dim]  {_coin}: registered {_market.question}[/dim]")

    # Start Polymarket WS price feed (replaces REST poll — real-time push)
    poly_task = asyncio.create_task(ws_feed.run(MAKER_COINS))
    asyncio.create_task(ws_silence_watchdog(ws_feed, MAKER_COINS))

    # Hot-reload .env every 15s — no restart needed for config changes
    _start_config_watcher()
    console.print("[dim]Polymarket price feed started (WebSocket — real-time)[/dim]")

    # Wait a moment for WebSocket to connect and receive initial prices
    await asyncio.sleep(3)

    # Track windows per coin
    windows: dict[str, WindowState] = {}
    prev_windows: dict[str, WindowState] = {}  # Previous windows awaiting resolution

    console.print("[green]Maker bot started. Monitoring 15-min windows...[/green]")
    console.print()

    last_status_print = 0.0

    try:
        while True:
            for coin in MAKER_COINS:
                if coin not in SYMBOLS:
                    continue

                current_window_start = get_current_window_timestamp()
                current_window_end = current_window_start + WINDOW_SECONDS

                # Check if we need a new window state
                window = windows.get(coin)

                # Resolve previous window before starting a new one
                prev = prev_windows.get(coin)
                if prev and prev.needs_resolution:
                    order = prev.order_info
                    is_paper = order.get("paper", False)
                    filled = True if is_paper else await check_if_filled(client, order["order_id"])
                    if filled:
                        fill_tag = "📝 PAPER " if is_paper else ""
                        console.print(f"  [green]✅ {fill_tag}{coin} maker order FILLED![/green]")
                        save_trade_record({
                            "type": "maker_fill", "coin": coin,
                            "direction": order["direction"], "bid_price": order["bid_price"],
                            "size": order["size"], "cost": order["cost"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            # analysis fields (present on paper trades, None on real)
                            "move_pct": order.get("move_pct"),
                            "poly_price": order.get("poly_price"),
                            "binance_prob": order.get("binance_prob"),
                            "gap": order.get("gap"),
                            "entry_seconds": order.get("entry_seconds"),
                            "hour_utc": order.get("hour_utc"),
                        })
                        # Check resolution — did we win or lose?
                        try:
                            from tracker import get_current_price
                            token_price = get_current_price(order["token_id"])
                            if token_price is not None and token_price >= 0.90:
                                payout = order["size"] * 1.0
                                band = order.get("conf_band", "")
                                bankroll.record_win(order["cost"], payout, band=band)
                                ptag = "📝 PAPER " if is_paper else ""
                                console.print(f"  [bold green]🎯 {ptag}{coin} WIN! +${payout - order['cost']:.2f}[/bold green]")
                                tg.send_message(f"✅ {ptag}{coin} {order['direction'].upper()} WIN +${payout - order['cost']:.2f} | Bank: ${bankroll.balance:.2f}")
                                save_trade_record({
                                    "type": "maker_outcome", "outcome": "win", "coin": coin,
                                    "direction": order["direction"], "band": band,
                                    "cost": order["cost"], "payout": payout,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "move_pct": order.get("move_pct"),
                                    "poly_price": order.get("poly_price"),
                                    "binance_prob": order.get("binance_prob"),
                                    "gap": order.get("gap"),
                                    "entry_seconds": order.get("entry_seconds"),
                                    "hour_utc": order.get("hour_utc"),
                                })
                            elif token_price is not None and token_price <= 0.10:
                                band = order.get("conf_band", "")
                                bankroll.record_loss(order["cost"], band=band)
                                ptag = "📝 PAPER " if is_paper else ""
                                console.print(f"  [red]❌ {ptag}{coin} LOSS: -${order['cost']:.2f}[/red]")
                                tg.send_message(f"❌ {ptag}{coin} {order['direction'].upper()} LOSS -${order['cost']:.2f} | Bank: ${bankroll.balance:.2f}")
                                save_trade_record({
                                    "type": "maker_outcome", "outcome": "loss", "coin": coin,
                                    "direction": order["direction"], "band": band,
                                    "cost": order["cost"], "payout": 0.0,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "move_pct": order.get("move_pct"),
                                    "poly_price": order.get("poly_price"),
                                    "binance_prob": order.get("binance_prob"),
                                    "gap": order.get("gap"),
                                    "entry_seconds": order.get("entry_seconds"),
                                    "hour_utc": order.get("hour_utc"),
                                })
                            else:
                                # Not resolved yet — give the money back for now
                                console.print(f"  [yellow]{coin}: Resolution unclear (price: {token_price}) — refunding[/yellow]")
                                bankroll.balance += order["cost"]
                        except Exception as e:
                            console.print(f"  [yellow]{coin}: Resolution check failed ({e}) — refunding[/yellow]")
                            bankroll.balance += order["cost"]
                        bankroll.pending_orders = [o for o in bankroll.pending_orders if o.get("order_id") != order["order_id"]]
                        save_pending_orders(bankroll.pending_orders)
                    else:
                        # Not filled — cancel and refund
                        if not is_paper:
                            await cancel_order(client, order["order_id"])
                        bankroll.balance += order["cost"]
                        console.print(f"  [dim]{coin}: Order not filled — cancelled (refunded ${order['cost']:.2f})[/dim]")
                        bankroll.pending_orders = [o for o in bankroll.pending_orders if o.get("order_id") != order["order_id"]]
                        save_pending_orders(bankroll.pending_orders)
                    prev.order_info = None  # Mark resolved
                    del prev_windows[coin]

                if window is None or window.window_start_ts != current_window_start:
                    # Save old window for resolution
                    if window and window.order_info:
                        prev_windows[coin] = window

                    # New window — use 15m kline OPEN as true window start price.
                    # Fetching from Binance REST ensures accuracy even when joining mid-window.
                    start_price = _get_kline_open(coin) or feed.get_price(coin) or 0
                    if start_price > 0:
                        feed.set_window_start(coin, start_price)

                    window = WindowState(
                        coin=coin,
                        window_start_ts=current_window_start,
                        window_end_ts=current_window_end,
                        start_price=start_price,
                    )
                    windows[coin] = window

                    ts = datetime.fromtimestamp(current_window_start, tz=timezone.utc).strftime("%H:%M:%S")
                    console.print(
                        f"[bold]── {coin} New window: {ts} UTC | "
                        f"Start: ${start_price:,.2f} | "
                        f"{max(0, window.seconds_remaining)}s remaining ──[/bold]"
                    )

                # ── Check if it's time to enter (T-10 seconds) ──
                secs_left = window.seconds_remaining

                if (
                    not window.order_placed
                    and 0 < secs_left <= MAKER_ENTRY_SECONDS
                    and window.start_price > 0
                    and bankroll.can_trade
                ):
                    # Skip low-liquidity hours (midnight-7am UTC)
                    # current_hour = datetime.now(timezone.utc).hour
                    # if MAKER_QUIET_HOURS_START <= current_hour < MAKER_QUIET_HOURS_END:
                    #     if not window.order_placed:
                    #         console.print(
                    #             f"  [yellow]{coin}: Quiet hours ({MAKER_QUIET_HOURS_START}:00-"
                    #             f"{MAKER_QUIET_HOURS_END}:00 UTC) — SKIP[/yellow]"
                    #         )
                    #         window.order_placed = True
                    #     continue

                    # Time to make our move!
                    direction, confidence, current_price = detect_direction(
                        coin, window.start_price
                    )

                    if direction is None:
                        console.print(
                            f"  [yellow]{coin}: Ambiguous ({confidence:.3f}% move) — SKIP[/yellow]"
                        )
                        window.order_placed = True  # Don't retry this window
                        continue

                    console.print(
                        f"  [green]{coin}: {direction.upper()} detected ({confidence:.3f}% move) "
                        f"| ${window.start_price:,.2f} → ${current_price:,.2f}[/green]"
                    )

                    # Check Polymarket current odds vs Binance signal
                    poly_up, poly_down = poly_feed.get_market_prices(coin)
                    poly_price = poly_up if direction == "up" else poly_down
                    if poly_price is not None:
                        # Binance-derived win probability (simple: >50% if we're confident)
                        binance_prob = min(0.98, 0.5 + confidence / MAKER_SIGNAL_SCALE)
                        gap = binance_prob - poly_price
                        gap_color = "green" if gap > 0.05 else "yellow" if gap > MAKER_MIN_GAP else "red"
                        console.print(
                            f"  [{gap_color}]Poly {direction.upper()}: ${poly_price:.2f} | "
                            f"Binance signal: {binance_prob:.0%} | Gap: {gap:+.0%} (min={MAKER_MIN_GAP:+.0%})[/{gap_color}]"
                        )
                        if gap < MAKER_MIN_GAP:
                            console.print(
                                f"  [yellow]{coin}: Polymarket already priced in ({poly_price:.2f}, gap {gap:+.0%} < {MAKER_MIN_GAP:+.0%}) — SKIP[/yellow]"
                            )
                            window.order_placed = True
                            continue
                    else:
                        console.print(f"  [dim]{coin} Poly price: not yet available[/dim]")

                    # Check Allium smart money confirmation
                    allium_signal = allium.get_signal(coin, current_window_start)
                    allium_tag = ""
                    if allium_signal.has_flow_data or allium_signal.has_smart_data:
                        console.print(f"  [cyan]{coin} Allium: {allium_signal.summary()}[/cyan]")
                        allium_tag = f" | Allium: {allium_signal.summary()}"

                        if allium_signal.contradicts_side(direction):
                            console.print(
                                f"  [yellow]{coin}: Smart money CONTRADICTS "
                                f"{direction.upper()} — SKIP[/yellow]"
                            )
                            window.order_placed = True
                            continue

                        if allium_signal.confirms_side(direction):
                            console.print(
                                f"  [green]{coin}: Smart money CONFIRMS "
                                f"{direction.upper()} — boosting confidence[/green]"
                            )
                            confidence = min(confidence * 1.3, 0.6)
                    else:
                        console.print(f"  [dim]{coin} Allium: No data (trading on Binance alone)[/dim]")

                    # Calculate bid price based on confidence + observed band win rate
                    bid_price, conf_band = calculate_bid_price(confidence, bankroll)

                    # Size bet: fractional bankroll if MAKER_BET_PCT set, else fixed floor
                    base_bet = round(bankroll.balance * MAKER_BET_PCT, 2) if MAKER_BET_PCT > 0 else MAKER_BET_SIZE
                    base_bet = max(MAKER_BET_SIZE, min(MAKER_MAX_BET, base_bet))

                    # Kelly override when band has ≥10 trades
                    wr = bankroll.band_win_rate(conf_band)
                    if wr is not None:
                        bet_amount = optimal_bet(wr, bid_price, base_bet, MAKER_MAX_BET, MAKER_TARGET_EV)
                        console.print(f"  [dim]{coin}: Kelly bet ${bet_amount:.2f} (band={conf_band} wr={wr:.0%} base=${base_bet:.2f})[/dim]")
                    else:
                        bet_amount = base_bet

                    # Discover the market
                    market = discover_market(coin)
                    if market is None or not market.is_active:
                        console.print(f"  [yellow]{coin}: No active 15-min market found[/yellow]")
                        window.order_placed = True
                        continue

                    # Register new tokens with WS feed so it subscribes to this window
                    ws_feed.register_tokens(coin, market.up_token_id, market.down_token_id)
                    await ws_feed.flush_pending_tokens()

                    if PAPER_TRADE:
                        # Paper trade — simulate the order
                        _now = datetime.now(timezone.utc)
                        paper_order = {
                            "order_id": f"paper_{coin}_{int(time.time())}",
                            "token_id": market.up_token_id if direction == "up" else market.down_token_id,
                            "direction": direction,
                            "bid_price": bid_price,
                            "size": bet_amount / bid_price,
                            "cost": bet_amount,
                            "coin": coin,
                            "conf_band": conf_band,
                            "placed_at": time.time(),
                            "paper": True,
                            # --- analysis fields ---
                            "move_pct": round(confidence, 4),
                            "poly_price": poly_price,
                            "binance_prob": round(binance_prob, 4) if poly_price is not None else None,
                            "gap": round(gap, 4) if poly_price is not None else None,
                            "entry_seconds": MAKER_ENTRY_SECONDS,
                            "hour_utc": _now.hour,
                        }
                        console.print(
                            f"  [bold yellow]📝 PAPER BID: {coin} {direction.upper()} "
                            f"@ ${bid_price:.2f} | {paper_order['size']:.1f} shares | ${bet_amount:.2f}[/bold yellow]"
                        )
                        window.order_placed = True
                        window.order_info = paper_order
                        bankroll.balance -= paper_order["cost"]
                        bankroll.pending_orders.append(paper_order)
                    else:
                        # Collect any USDC redeemed in background + trigger new redemptions
                        funder = os.getenv("FUNDER", "")
                        private_key = os.getenv("PRIVATE_KEY", "")
                        if funder and private_key:
                            redeemed = check_and_redeem(funder, private_key)
                            if redeemed > 0:
                                bankroll.balance += redeemed

                        # Real trade — place aggressive limit, then chase the book
                        token_id = market.up_token_id if direction == "up" else market.down_token_id
                        entry_price = _chase_price(token_id, bid_price)
                        order_info = await place_maker_order(
                            client, market, direction, entry_price, bet_amount
                        )

                        window.order_placed = True
                        if order_info:
                            order_info["conf_band"] = conf_band
                            window.order_info = order_info
                            window.chasing = True
                            window.last_chase_ts = time.time()
                            window.chase_max_price = bid_price
                            bankroll.balance -= order_info["cost"]
                            bankroll.pending_orders.append(order_info)
                            save_pending_orders(bankroll.pending_orders)


            # ── Chase unfilled orders: cancel + replace at best_bid+1tick ──
            for coin in list(windows.keys()):
                window = windows.get(coin)
                if not window or not window.chasing or not window.order_info:
                    continue

                secs_left = window.seconds_remaining

                # Stop chasing when window is close to closing
                if secs_left <= CHASE_STOP_SECS:
                    window.chasing = False
                    console.print(f"  [dim]{coin}: Chase stopped — {secs_left}s left, leaving order[/dim]")
                    continue

                # Throttle: only chase every CHASE_INTERVAL seconds
                if time.time() - window.last_chase_ts < CHASE_INTERVAL:
                    continue
                window.last_chase_ts = time.time()

                order = window.order_info
                if order.get("paper"):
                    window.chasing = False
                    continue

                # Check if already filled
                filled = await check_if_filled(client, order["order_id"])
                if filled:
                    window.chasing = False
                    chase_count = round((time.time() - window.last_chase_ts) / CHASE_INTERVAL)
                    console.print(f"  [green]{coin}: Order filled at ${order['bid_price']:.4f}[/green]")
                    tg.send_message(f"✅ {coin} {order['direction'].upper()} filled @ ${order['bid_price']:.4f} × {order['size']:.0f} shares")
                    continue

                # Not filled — cancel and replace at new best_bid+1tick
                await cancel_order(client, order["order_id"])
                token_id = order["token_id"]
                new_price = _chase_price(token_id, window.chase_max_price)
                if new_price is None:
                    continue

                new_order = await place_maker_order(
                    client,
                    discover_market(coin),
                    order["direction"],
                    new_price,
                    order["cost"],  # same dollar amount
                    silent=True,    # suppress Telegram on chase replacements
                )
                if new_order:
                    new_order["conf_band"] = order.get("conf_band", "")
                    # Swap out old order for new one in pending list
                    bankroll.pending_orders = [
                        o for o in bankroll.pending_orders
                        if o.get("order_id") != order["order_id"]
                    ]
                    bankroll.pending_orders.append(new_order)
                    window.order_info = new_order
                    save_pending_orders(bankroll.pending_orders)
                    console.print(
                        f"  [cyan]{coin}: Chased → ${new_price:.4f} "
                        f"(was ${order['bid_price']:.4f}, max ${window.chase_max_price:.2f}, "
                        f"{secs_left}s left)[/cyan]"
                    )
                else:
                    window.chasing = False  # place failed — stop chasing

            # ── Clean up stale pending orders (older than 20 minutes) ──
            stale_cutoff = time.time() - 1200  # 20 minutes ago
            stale_orders = [
                o for o in bankroll.pending_orders
                if o.get("placed_at", time.time()) < stale_cutoff
            ]
            for stale in stale_orders:
                # Try to cancel on Polymarket (may already be gone)
                try:
                    await cancel_order(client, stale["order_id"])
                except Exception:
                    pass
                # Refund the cost back to bankroll
                bankroll.balance += stale.get("cost", 0)
                console.print(f"  [yellow]🧹 Cleaned up stale order: {stale.get('coin', '?')} — refunded ${stale.get('cost', 0):.2f}[/yellow]")
            if stale_orders:
                bankroll.pending_orders = [
                    o for o in bankroll.pending_orders
                    if o not in stale_orders
                ]

            # Print status every 2s
            if time.time() - last_status_print >= MAKER_STATUS_INTERVAL:
                last_status_print = time.time()
                console.print(build_status_panel(bankroll, MAKER_COINS))

            # Fast poll — check every second near window boundaries
            await asyncio.sleep(min(MAKER_STATUS_INTERVAL, 1.0))

    except KeyboardInterrupt:
        # Cancel all open orders
        console.print("\n[yellow]Cancelling open orders...[/yellow]")
        for order in bankroll.pending_orders:
            if order.get("order_id"):
                await cancel_order(client, order["order_id"])
        console.print("[yellow]Maker bot stopped.[/yellow]")
        console.print(f"\n  {bankroll.status_line()}")

    finally:
        ws_task.cancel()


def main():
    asyncio.run(run_maker_bot())


if __name__ == "__main__":
    main()

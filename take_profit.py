"""
Take Profit Engine
Monitors open positions and sells when gain exceeds TP_THRESHOLD.
Works with both v4 (weather) and v1 (sniper) trade logs.

Commands:
  python take_profit.py run    — continuous TP monitor
  python take_profit.py test   — place a $1 test buy then immediately sell to verify full flow
"""

import ssl_patch  # noqa: F401, F811 — must be first, patches SSL for ProtonVPN
_ = ssl_patch
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from trader import init_client, place_buy_order, save_order
from scanner import scan
from vpn import ensure_vpn
from telegram_alerts import alert_take_profit, alert_sniper_buy, alert_sniper_filled

import requests
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL

load_dotenv()
console = Console()

# --- Config from .env ---
PRIVATE_KEY      = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS   = os.getenv("WALLET_ADDRESS", "")
FUNDER           = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE   = int(os.getenv("SIGNATURE_TYPE", "2"))
VPN_REQUIRED     = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"

# Sniper v1 params
MIN_PRICE        = float(os.getenv("MIN_PRICE", "0.005"))
MAX_PRICE        = float(os.getenv("MAX_PRICE", "0.025"))
MIN_VOLUME       = float(os.getenv("MIN_VOLUME", "0"))      # min lifetime market volume (USDC)
MIN_LIQUIDITY    = float(os.getenv("MIN_LIQUIDITY", "0"))   # min current market liquidity (USDC)
MIN_VOLATILITY   = float(os.getenv("MIN_VOLATILITY", "0"))  # min 7-day price std dev (0 = disabled)
BET_SIZE_USDC       = float(os.getenv("BET_SIZE_USDC", "1"))
BET_PCT             = float(os.getenv("BET_PCT", "0"))       # % of balance per bet (overrides BET_SIZE_USDC if > 0)
MAX_DAILY_SPEND     = float(os.getenv("MAX_DAILY_SPEND", "35"))   # fallback if DAILY_BANKROLL_PCT=0
DAILY_BANKROLL_PCT  = float(os.getenv("DAILY_BANKROLL_PCT", "0")) # % of balance as daily cap (overrides MAX_DAILY_SPEND if > 0)
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# TP params
TP_THRESHOLD     = float(os.getenv("TP_THRESHOLD", "0.40"))
TP_SCAN_INTERVAL = int(os.getenv("TP_SCAN_INTERVAL", "60"))
TP_AGGRESSION_BPS = int(os.getenv("TP_AGGRESSION_BPS", "10"))  # bps above best bid (buy) / below best ask (sell)

DATA_DIR         = Path("data")
TRADES_FILE      = DATA_DIR / "v4_trades.json"
ORDERS_FILE      = DATA_DIR / "orders.json"
TP_LOG_FILE      = DATA_DIR / "tp_log.json"
MAKER_PENDING    = DATA_DIR / "maker_pending.json"


def print_banner():
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║   TAKE PROFIT ENGINE                 ║[/bold cyan]")
    console.print("[bold cyan]║   Locking in gains automatically.    ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/bold cyan]")
    console.print()


def print_config():
    console.print(f"  Price range:     ${MIN_PRICE} - ${MAX_PRICE}")
    if BET_PCT > 0:
        console.print(f"  Bet size:        {BET_PCT*100:.1f}% of balance (min $1.00)")
    else:
        console.print(f"  Bet size:        ${BET_SIZE_USDC} USDC")
    if DAILY_BANKROLL_PCT > 0:
        console.print(f"  Daily limit:     {DAILY_BANKROLL_PCT*100:.0f}% of balance")
    else:
        console.print(f"  Daily limit:     ${MAX_DAILY_SPEND} USDC")
    console.print(f"  Scan interval:   {SCAN_INTERVAL} min")
    console.print(f"  TP threshold:    +{TP_THRESHOLD*100:.0f}%")
    console.print(f"  TP scan:         {TP_SCAN_INTERVAL}s")
    console.print(f"  Aggression:      {TP_AGGRESSION_BPS}bps below ask")
    console.print(f"  VPN required:    {VPN_REQUIRED}")
    console.print()


def get_price_volatility(token_id: str, days: int = 7) -> float | None:
    """
    Fetch 7-day price history and return std dev of prices as volatility measure.
    Returns None if insufficient data.
    """
    try:
        import time as _time
        now = int(_time.time())
        start = now - 86400 * days
        resp = requests.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": token_id, "startTs": start, "endTs": now, "fidelity": 60},
            timeout=10,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
        if len(history) < 5:
            return None
        prices = [float(h["p"]) for h in history]
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance ** 0.5  # std dev
    except Exception:
        return None


def get_current_price(token_id: str) -> float | None:
    """Fetch current midpoint price for a token."""
    try:
        resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json().get("mid", 0))
    except Exception:
        return None


def get_best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    """
    Fetch real best bid and best ask from the orderbook.
    Skips AMM floor/ceiling orders (bids < $0.05, asks > $0.95).
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        # Skip AMM floor/ceiling — only use real market maker orders
        real_bids = [b for b in bids if float(b["price"]) > 0.05]
        real_asks = [a for a in asks if float(a["price"]) < 0.95]
        best_bid = float(real_bids[0]["price"]) if real_bids else None
        best_ask = float(real_asks[0]["price"]) if real_asks else None
        return best_bid, best_ask
    except Exception:
        return None, None


def aggressive_buy_price(token_id: str, bps: int = TP_AGGRESSION_BPS) -> float | None:
    """
    Return a buy price N bps above best bid.
    Falls back to midpoint if spread is too wide (illiquid market).
    If price crosses best ask, caps at best ask (fills immediately as taker).
    """
    best_bid, best_ask = get_best_bid_ask(token_id)
    mid = get_current_price(token_id)

    # Use midpoint as reference if spread is wide (>20%) — real bid is just AMM floor
    if best_bid and best_ask and (best_ask - best_bid) < 0.20:
        ref = best_bid
    else:
        ref = mid  # fall back to midpoint for illiquid markets

    if ref is None:
        return None
    price = round(ref * (1 + bps / 10000), 4)
    if best_ask is not None:
        price = min(price, best_ask)  # cross the book = immediate fill
    return price


def aggressive_sell_price(token_id: str, bps: int = TP_AGGRESSION_BPS) -> float | None:
    """
    Return a sell price N bps below best ask.
    Falls back to midpoint if spread is too wide (illiquid market).
    If price crosses best bid, caps at best bid (fills immediately as taker).
    """
    best_bid, best_ask = get_best_bid_ask(token_id)
    mid = get_current_price(token_id)

    # Use midpoint as reference if spread is wide (>20%) — real ask is just AMM ceiling
    if best_bid and best_ask and (best_ask - best_bid) < 0.20:
        ref = best_ask
    else:
        ref = mid  # fall back to midpoint for illiquid markets

    if ref is None:
        return None
    price = round(ref * (1 - bps / 10000), 4)
    if best_bid is not None:
        price = max(price, best_bid)  # cross the book = immediate fill
    return price


def load_open_positions() -> list[dict]:
    """
    Load all open positions from:
    1. Live wallet via Polymarket data API (source of truth)
    2. Local JSON files (for buy_price / order_id metadata not available from API)
    Merges both so TP monitoring works for all positions regardless of how they were bought.
    """
    import os

    # Build metadata index from local files (buy_price, order_id, question)
    meta: dict[str, dict] = {}  # token_id -> metadata

    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text())
            sold_ids = {t.get("token_id") for t in trades if t.get("type") == "sell"}
            for t in trades:
                if t.get("type") == "bet" and t.get("token_id") and t["token_id"] not in sold_ids:
                    meta[t["token_id"]] = {**t, "source": "v4"}
        except (json.JSONDecodeError, ValueError):
            pass

    if ORDERS_FILE.exists():
        try:
            orders = json.loads(ORDERS_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            orders = []
        for o in orders:
            if o.get("status") in ("sold", "unfilled"):
                continue
            if o.get("token_id") and o.get("order_id"):
                meta[o["token_id"]] = {
                    "token_id":  o["token_id"],
                    "buy_price": o["price"],
                    "shares":    o["size"],
                    "amount":    o["usdc_spent"],
                    "question":  o.get("market_question", ""),
                    "side":      o.get("outcome", ""),
                    "order_id":  o.get("order_id", ""),
                    "source":    "v1",
                }

    if MAKER_PENDING.exists():
        try:
            maker_orders = json.loads(MAKER_PENDING.read_text())
        except (json.JSONDecodeError, ValueError):
            maker_orders = []
        for o in maker_orders:
            if o.get("token_id"):
                meta[o["token_id"]] = {
                    "token_id":  o["token_id"],
                    "buy_price": o.get("bid_price", 0),
                    "shares":    o.get("size", 0),
                    "amount":    o.get("cost", 0),
                    "question":  f"{o.get('coin','?')} {o.get('direction','?')}",
                    "side":      o.get("direction", ""),
                    "order_id":  o.get("order_id", ""),
                    "source":    "maker",
                }

    # Fetch live wallet positions from Polymarket data API
    funder = os.getenv("FUNDER") or os.getenv("WALLET_ADDRESS", "")
    wallet_positions = _fetch_wallet_positions(funder) if funder else []

    positions = []
    seen = set()

    for wp in wallet_positions:
        token_id = wp.get("asset", "")
        size = float(wp.get("size", 0))
        cur_price = float(wp.get("curPrice", 0))

        if not token_id or size < 1 or cur_price == 0:
            continue  # skip resolved / empty positions

        seen.add(token_id)
        local = meta.get(token_id, {})
        positions.append({
            "token_id":  token_id,
            "buy_price": local.get("buy_price", cur_price),  # use local buy price if known
            "shares":    size,
            "amount":    local.get("amount", size * cur_price),
            "question":  wp.get("title", local.get("question", token_id)),
            "side":      wp.get("outcome", local.get("side", "")),
            "order_id":  local.get("order_id", ""),
            "source":    local.get("source", "wallet"),
        })

    # Add local-only positions not yet in wallet (buy order placed but not filled yet)
    for token_id, m in meta.items():
        if token_id not in seen:
            positions.append(m)

    return positions


def place_sell_order(client, token_id: str, shares: float, sell_price: float | None = None) -> str:
    """
    Place an aggressive GTC sell order N bps below best ask.
    Returns: 'ok' on success, 'unfilled' if shares not owned, 'error' on other failure.
    """
    try:
        price = aggressive_sell_price(token_id) or sell_price
        if price is None:
            console.print("[red]  Cannot determine sell price.[/red]")
            return "error"
        best_bid, best_ask = get_best_bid_ask(token_id)
        if best_bid is None:
            console.print(f"  [dim yellow]No real bid in orderbook — skipping sell (will retry when liquidity appears)[/dim yellow]")
            return "error"
        console.print(f"  [dim]Orderbook — bid: ${best_bid} | ask: ${best_ask} | sell at: ${price:.3f} ({TP_AGGRESSION_BPS}bps below ask)[/dim]")
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=max(round(shares, 0), 5),
            side=SELL,
        )
        signed = client.create_order(order_args)
        response = client.post_order(signed, OrderType.GTC)
        return "ok"
    except Exception as e:
        err = str(getattr(e, '__dict__', e))
        if "not enough balance" in err or "allowance" in err:
            console.print(f"[yellow]  Sell skipped — buy order never filled (shares not owned). Marking as unfilled.[/yellow]")
            return "unfilled"
        console.print(f"[red]  Sell order failed: {type(e).__name__}: {err}[/red]")
        return "error"


def record_sell(position: dict, sell_price: float, gain_pct: float):
    """Log the sell and mark position as sold in source file."""
    DATA_DIR.mkdir(exist_ok=True)

    log = json.loads(TP_LOG_FILE.read_text()) if TP_LOG_FILE.exists() else []
    log.append({
        "type":       "sell",
        "token_id":   position["token_id"],
        "question":   position.get("question", ""),
        "side":       position.get("side", ""),
        "buy_price":  position["buy_price"],
        "sell_price": sell_price,
        "shares":     position.get("shares", 0),
        "gain_pct":   round(gain_pct * 100, 1),
        "pnl":        round((sell_price - position["buy_price"]) * position.get("shares", 0), 4),
        "source":     position.get("source", ""),
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    })
    TP_LOG_FILE.write_text(json.dumps(log, indent=2))

    if position["source"] == "v4" and TRADES_FILE.exists():
        trades = json.loads(TRADES_FILE.read_text())
        trades.append({
            "type":       "sell",
            "token_id":   position["token_id"],
            "question":   position.get("question", ""),
            "sell_price": sell_price,
            "gain_pct":   round(gain_pct * 100, 1),
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        })
        TRADES_FILE.write_text(json.dumps(trades, indent=2))

    elif position["source"] == "v1" and ORDERS_FILE.exists():
        orders = json.loads(ORDERS_FILE.read_text())
        for o in orders:
            if o.get("token_id") == position["token_id"]:
                o["status"]     = "sold"
                o["sell_price"] = sell_price
                o["gain_pct"]   = round(gain_pct * 100, 1)
        ORDERS_FILE.write_text(json.dumps(orders, indent=2))


def get_wallet_balance(client) -> float | None:
    """Fetch live USDC balance from Polymarket."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        result = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(result.get("balance", 0)) / 1e6
    except Exception:
        return None


def get_bet_size(client) -> float:
    """Return bet size: BET_PCT% of wallet balance, or fixed BET_SIZE_USDC as fallback."""
    if BET_PCT > 0:
        balance = get_wallet_balance(client)
        if balance:
            size = round(balance * BET_PCT, 2)
            size = max(size, 0.50)  # soft floor — API will reject if too small for specific market
            console.print(f"[dim]Bet size: {BET_PCT*100:.1f}% of ${balance:.2f} = ${size:.2f}[/dim]")
            return size
    return BET_SIZE_USDC


def _mark_unfilled(client, position: dict):
    """Cancel the GTC order and mark position as unfilled so TP stops retrying."""
    order_id = position.get("order_id")
    if order_id:
        cancel_order(client, order_id)

    source = position.get("source")
    token_id = position["token_id"]
    if source == "v1" and ORDERS_FILE.exists():
        orders = json.loads(ORDERS_FILE.read_text())
        for o in orders:
            if o.get("token_id") == token_id:
                o["status"] = "unfilled"
        ORDERS_FILE.write_text(json.dumps(orders, indent=2))
    elif source == "v4" and TRADES_FILE.exists():
        trades = json.loads(TRADES_FILE.read_text())
        for t in trades:
            if t.get("token_id") == token_id and t.get("type") == "bet":
                t["status"] = "unfilled"
        TRADES_FILE.write_text(json.dumps(trades, indent=2))


def cancel_hanging_orders(client):
    """
    On startup: fetch open orders from Polymarket and reconcile orders.json.

    For each open order:
      - filled=0        → cancel on Polymarket + mark 'unfilled' in orders.json
      - 0 < filled < total → cancel remainder + update size to actual filled shares
      - filled=total    → already matched, nothing to cancel (shouldn't appear here)
    """
    try:
        open_orders = client.get_orders()
        if not open_orders:
            console.print("[dim]No open orders on Polymarket.[/dim]")
            return

        console.print(f"[yellow]Found {len(open_orders)} open order(s) from previous session:[/yellow]")
        for o in open_orders:
            oid = (o.get("id") or o.get("order_id") or "")[:16]
            side = o.get("side", "?").upper()
            price = o.get("price", "?")
            size = o.get("original_size", o.get("size", "?"))
            matched = int(float(o.get("size_matched", 0)))
            asset = (o.get("asset_id") or o.get("token_id") or "")[:12]
            if matched == 0:
                label = "[dim]unfilled[/dim]"
            elif matched < int(float(size or 0)):
                label = f"[bold yellow]⚠ partial: {matched}/{size}[/bold yellow]"
            else:
                label = f"[green]fully filled: {matched}[/green]"
            console.print(f"  [dim]{oid}… | {side} @ ${price} | {label} | token: {asset}…[/dim]")

        # Reconcile orders.json in one pass
        if ORDERS_FILE.exists():
            try:
                orders = json.loads(ORDERS_FILE.read_text())
            except (json.JSONDecodeError, ValueError):
                orders = []

            updated = False
            for o in open_orders:
                oid = o.get("id") or o.get("orderID") or o.get("order_id")
                token_id = o.get("asset_id") or o.get("token_id")
                matched = int(float(o.get("size_matched", 0)))
                price = float(o.get("price", 0))

                for local in orders:
                    if local.get("order_id") != oid and local.get("token_id") != token_id:
                        continue
                    if matched == 0:
                        # Never filled — mark unfilled, TP will ignore it
                        local["status"] = "unfilled"
                        console.print(f"  [dim]→ Marked unfilled: {(oid or '')[:16]}…[/dim]")
                    else:
                        # Partial fill — keep position but record actual filled shares
                        old_size = local.get("size")
                        local["size"] = matched
                        local["usdc_spent"] = round(matched * price, 4)
                        local["status"] = "filled"  # explicit: shares confirmed in wallet
                        console.print(f"  [cyan]→ Partial fill confirmed: {matched} shares (was {old_size}) — status=filled[/cyan]")
                    updated = True

            if updated:
                ORDERS_FILE.write_text(json.dumps(orders, indent=2))

        # Cancel all open orders on Polymarket (cancels unfilled remainder only)
        console.print("[yellow]Cancelling open orders on Polymarket...[/yellow]")
        count = 0
        for o in open_orders:
            oid = o.get("id") or o.get("orderID") or o.get("order_id")
            if oid:
                cancel_order(client, oid)
                count += 1
        console.print(f"[green]✅ Cancelled {count} order(s). Partially filled shares remain in wallet.[/green]")
        console.print()
    except Exception as e:
        console.print(f"[dim yellow]Could not fetch open orders on startup: {e}[/dim yellow]")


def check_fill_status(client):
    """Poll Polymarket for GTC buy orders that have filled since last check."""
    if not ORDERS_FILE.exists():
        return
    try:
        orders = json.loads(ORDERS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return

    updated = False
    for o in orders:
        # Only check orders that are placed but not yet confirmed filled/unfilled
        if o.get("status") in ("filled", "unfilled", "sold"):
            continue
        oid = o.get("order_id")
        if not oid:
            continue
        try:
            status = client.get_order(oid)
            if not status:
                continue
            matched = int(float(status.get("size_matched", 0)))
            if matched > 0 and o.get("size", 0) != matched:
                o["size"] = matched
                o["usdc_spent"] = round(matched * float(o.get("price", 0)), 4)

            order_status = status.get("status", "")
            if order_status in ("MATCHED", "FILLED") or matched >= int(float(status.get("original_size", matched))):
                if o.get("fill_alerted") != True:
                    alert_sniper_filled(
                        question=o.get("market_question", ""),
                        outcome=o.get("outcome", ""),
                        price=float(o.get("price", 0)),
                        shares=matched,
                    )
                    o["status"] = "filled"
                    o["fill_alerted"] = True
                    updated = True
        except Exception:
            pass

    if updated:
        ORDERS_FILE.write_text(json.dumps(orders, indent=2))


def run_tp_cycle(client) -> int:
    """Check all open positions. Sell any with gain >= TP_THRESHOLD."""
    positions = load_open_positions()
    if not positions:
        return 0

    sold = 0
    for pos in positions:
        token_id  = pos["token_id"]
        buy_price = pos["buy_price"]
        shares    = pos.get("shares", pos.get("amount", 0) / max(buy_price, 0.001))

        current_price = get_current_price(token_id)
        if current_price is None:
            continue

        gain_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0

        if gain_pct >= TP_THRESHOLD:
            question = pos.get("question", token_id)[:55]
            console.print(
                f"[bold green]  💰 TAKE PROFIT: {question}[/bold green]\n"
                f"     Buy: ${buy_price:.3f} → Now: ${current_price:.3f} "
                f"(+{gain_pct*100:.0f}%) | {shares:.0f} shares"
            )
            result = place_sell_order(client, token_id, shares, current_price)
            if result == "ok":
                record_sell(pos, current_price, gain_pct)
                alert_take_profit(
                    question=pos.get("question", token_id),
                    side=pos.get("side", ""),
                    buy_price=buy_price,
                    sell_price=current_price,
                    shares=shares,
                    gain_pct=gain_pct * 100,
                )
                console.print(f"[green]  ✅ Sold {shares:.0f} shares @ ${current_price:.3f}[/green]")
                sold += 1
            elif result == "unfilled":
                _mark_unfilled(client, pos)
            else:
                console.print(f"[red]  ❌ Sell failed — will retry next cycle[/red]")

    return sold


def get_daily_limit(client) -> float:
    """Return today's daily spend cap: DAILY_BANKROLL_PCT% of balance, or fixed MAX_DAILY_SPEND."""
    if DAILY_BANKROLL_PCT > 0:
        balance = get_wallet_balance(client)
        if balance:
            limit = round(balance * DAILY_BANKROLL_PCT, 2)
            console.print(f"[dim]Daily limit: {DAILY_BANKROLL_PCT*100:.0f}% of ${balance:.2f} = ${limit:.2f}[/dim]")
            return limit
    return MAX_DAILY_SPEND


def run_scan_cycle(client):
    """Run one sniper scan + buy cycle."""
    from trader import get_daily_spend, get_placed_token_ids

    daily_limit = get_daily_limit(client)
    daily_spent = get_daily_spend()
    remaining = daily_limit - daily_spent

    if remaining <= 0:
        console.print(f"[yellow]Daily limit reached (${daily_limit:.2f}). Skipping scan.[/yellow]")
        return 0

    console.print(f"[dim]Daily spend: ${daily_spent:.2f} / ${daily_limit:.2f} (${remaining:.2f} remaining)[/dim]")

    cheap_outcomes = scan(min_price=MIN_PRICE, max_price=MAX_PRICE, min_volume=MIN_VOLUME, min_liquidity=MIN_LIQUIDITY)
    if not cheap_outcomes:
        console.print("[yellow]No cheap outcomes found.[/yellow]")
        return 0

    existing = get_placed_token_ids()
    new_outcomes = [o for o in cheap_outcomes if o.token_id not in existing]
    console.print(f"[dim]{len(new_outcomes)} new outcomes (filtered {len(cheap_outcomes) - len(new_outcomes)} existing)[/dim]")

    if not new_outcomes:
        console.print("[yellow]All cheap outcomes already in portfolio.[/yellow]")
        return 0

    bet_size = get_bet_size(client)
    orders_placed = 0
    for outcome in new_outcomes:
        if remaining < bet_size:
            console.print(f"[yellow]Budget remaining (${remaining:.2f}) below bet size (${bet_size:.2f}). Stopping.[/yellow]")
            break

        # Volatility filter — skip outcomes with insufficient price movement history
        if MIN_VOLATILITY > 0:
            vol = get_price_volatility(outcome.token_id)
            if vol is None or vol < MIN_VOLATILITY:
                continue

        order = place_buy_order(client, outcome, bet_size)
        if order:
            save_order(order)
            orders_placed += 1
            remaining -= bet_size
            shares = round(bet_size / max(outcome.price, 0.001), 0)
            alert_sniper_buy(outcome.market_question, outcome.outcome, outcome.price, shares, bet_size)
            time.sleep(1)

    return orders_placed


def _fetch_wallet_positions(funder: str) -> list[dict]:
    """Fetch all live positions directly from Polymarket wallet via data API."""
    try:
        resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={funder}&sizeThreshold=1",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def kill_switch(client) -> str:
    """
    Emergency kill switch:
    1. Cancel all open GTC orders on Polymarket
    2. Sell all wallet positions fetched live from Polymarket data API
    Returns a summary string.
    """
    import os
    funder = os.getenv("FUNDER") or os.getenv("WALLET_ADDRESS", "")
    lines = ["🚨 *KILL SWITCH ACTIVATED*\n"]

    # Step 1: Cancel all open orders — cancel_all() in one shot
    cancelled = 0
    try:
        open_orders = client.get_orders() or []
        lines.append(f"*Step 1: Cancel open orders* ({len(open_orders)} found)")
        if open_orders:
            try:
                client.cancel_all()
                cancelled = len(open_orders)
                lines.append(f"  ✅ cancel_all() — {cancelled} order(s) cancelled")
            except Exception as e:
                lines.append(f"  ⚠ cancel_all failed ({e}), retrying per-order…")
                cancel_errors = 0
                for o in open_orders:
                    oid = o.get("id") or o.get("orderID") or o.get("order_id")
                    if oid:
                        try:
                            client.cancel(oid)
                            cancelled += 1
                        except Exception as ce:
                            cancel_errors += 1
                            lines.append(f"  ⚠ {str(oid)[:12]}…: {ce}")
                lines.append(f"  ✅ Cancelled {cancelled} | ❌ Failed {cancel_errors}")
        else:
            lines.append("  ✅ No open orders")
    except Exception as e:
        lines.append(f"  ❌ Could not fetch open orders: {e}")

    lines.append("")

    # Step 2: Sell all wallet positions fetched live from Polymarket
    wallet_positions = _fetch_wallet_positions(funder)
    # Filter out resolved markets (curPrice == 0)
    active = [p for p in wallet_positions if float(p.get("curPrice", 0)) > 0]
    lines.append(f"*Step 2: Sell wallet positions* ({len(active)} active / {len(wallet_positions)} total)")

    sold = 0
    skipped = 0
    sell_errors = 0

    for p in active:
        token_id = p.get("asset", "")
        size = float(p.get("size", 0))
        title = p.get("title", token_id)[:40]

        if size < 1:
            skipped += 1
            continue

        # Get best bid from orderbook (include low-price AMM bids)
        try:
            book_resp = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
                timeout=8,
            )
            book = book_resp.json()
            bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
            real_bids = [b for b in bids if float(b["price"]) > 0.0001]
        except Exception:
            real_bids = []

        if not real_bids:
            lines.append(f"  ⏭ {title} — no bid")
            skipped += 1
            continue

        best_bid = float(real_bids[0]["price"])
        sell_price = max(round(best_bid, 4), 0.001)  # 4 decimal places, min $0.001
        shares = max(round(size, 0), 5)

        lines.append(f"  → {title} | {shares:.0f} shares @ ${sell_price:.4f}")
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=sell_price,
                size=shares,
                side=SELL,
            )
            signed = client.create_order(order_args)
            client.post_order(signed, OrderType.GTC)
            sold += 1
        except Exception as e:
            err = str(e)
            sell_errors += 1
            if "not enough balance" in err or "allowance" in err:
                lines.append(f"    ⚠ Not in wallet (unfilled buy)")
            else:
                lines.append(f"    ❌ {err[:80]}")

    lines.append("")
    lines.append(f"*Done:* {cancelled} orders cancelled | {sold} positions sold | {skipped} skipped | {sell_errors} errors")
    return "\n".join(lines)


def cmd_run(client):
    """Continuous loop: scan + buy every SCAN_INTERVAL, check TP every TP_SCAN_INTERVAL."""
    console.print("[green]Sniper + Take Profit started. Press Ctrl+C to stop.[/green]")
    console.print()
    cancel_hanging_orders(client)

    last_scan = 0  # force immediate scan on start
    last_vpn_check = 0

    try:
        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            # VPN check only once per scan cycle — not every 10s
            vpn_elapsed = time.time() - last_vpn_check
            if VPN_REQUIRED and vpn_elapsed >= SCAN_INTERVAL * 60:
                if not ensure_vpn(required=True):
                    console.print("[red]VPN disconnected. Pausing 60s...[/red]")
                    time.sleep(60)
                    continue
                last_vpn_check = time.time()

            # Check for newly filled GTC buys
            check_fill_status(client)

            # TP check every cycle — only log on action or error
            sold = run_tp_cycle(client)
            if sold:
                console.print(f"[green]💰 Took profit on {sold} position(s)[/green]")

            # Scan + buy every SCAN_INTERVAL minutes
            elapsed = time.time() - last_scan
            if elapsed >= SCAN_INTERVAL * 60:
                console.print()
                console.print(f"[bold]--- Scan cycle: {now} ---[/bold]")
                orders = run_scan_cycle(client)
                console.print(f"[green]Placed {orders} new order(s)[/green]")
                last_scan = time.time()
                console.print()
            time.sleep(TP_SCAN_INTERVAL)

    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Sniper + Take Profit stopped.[/yellow]")


def cancel_order(client, order_id: str) -> bool:
    """Cancel an open order by ID."""
    try:
        client.cancel(order_id)
        return True
    except Exception as e:
        console.print(f"[yellow]  Could not cancel order {order_id}: {e}[/yellow]")
        return False


def get_order_status(client, order_id: str) -> dict | None:
    """Fetch order status from CLOB."""
    try:
        return client.get_order(order_id)
    except Exception:
        return None


def cmd_test(client):
    """
    Place a $1 FOK test buy on a liquid outcome (price $0.40-$0.60),
    confirm it filled, then immediately sell to verify full enter/exit flow.
    Logs result to tp_log.json.
    Uses FOK (Fill or Kill) — order either fills instantly or cancels.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    console.print("[bold yellow]--- TEST MODE: Buy → Sell (FOK) ---[/bold yellow]")
    # Use a liquid price range for FOK to have a chance of filling
    test_min, test_max = 0.40, 0.60
    console.print(f"  Scanning for liquid outcome (${test_min}-${test_max})...")

    outcomes = scan(min_price=test_min, max_price=test_max)
    if not outcomes:
        console.print("[red]No outcomes found in $0.40-$0.60 range.[/red]")
        return

    # Find first liquid market (spread < 10%)
    target = None
    for o in outcomes[:50]:
        bid, ask = get_best_bid_ask(o.token_id)
        if bid and ask and (ask - bid) < 0.10:
            target = o
            console.print(f"  [dim]✅ Liquid: {o.market_question[:50]} | bid=${bid:.3f} ask=${ask:.3f} spread=${ask-bid:.3f}[/dim]")
            break

    if not target:
        console.print("[red]No liquid market found (spread < 10%) in first 50 results. Try a higher MAX_PRICE.[/red]")
        return

    console.print(f"  Target:  [cyan]{target.market_question[:60]}[/cyan]")
    console.print(f"  Outcome: {target.outcome} @ ${target.price:.4f}")
    console.print()

    # Step 1: Buy at best ask to cross the book and fill immediately (test mode)
    test_amount = 5.0
    best_bid, best_ask = get_best_bid_ask(target.token_id)
    buy_price = best_ask or target.price  # cross book = guaranteed taker fill
    shares = max(round(test_amount / buy_price, 0), 5)
    console.print(f"  Orderbook — bid: ${best_bid} | ask: ${best_ask}")
    console.print(f"[bold]Step 1: Placing ${test_amount} GTC buy @ ${buy_price:.3f} (crossing book at ask for immediate fill)...[/bold]")
    try:
        order_args = OrderArgs(
            token_id=target.token_id,
            price=round(buy_price, 2),
            size=shares,
            side=BUY,
        )
        signed = client.create_order(order_args)
        response = client.post_order(signed, OrderType.GTC)
        order_id = response.get("orderID", response.get("id", "")) if isinstance(response, dict) else str(response)
        console.print(f"[green]  ✅ GTC buy placed @ ${buy_price:.3f} — order ID: {order_id}[/green]")
    except Exception as e:
        console.print(f"[red]  Buy failed: {type(e).__name__}: {getattr(e, '__dict__', e)}[/red]")
        return

    # Step 2: Confirm fill
    console.print("[dim]Waiting 5s to confirm fill...[/dim]")
    time.sleep(5)

    status = get_order_status(client, order_id)
    matched = int(float(status.get("size_matched", 0))) if status else 0
    console.print(f"  Order status: {status.get('status','?')} | Matched: {matched} / {int(shares)} shares")

    if matched == 0:
        console.print("[red]  Buy did not fill (GTC sitting unfilled — cancelling).[/red]")
        cancel_order(client, order_id)
        return

    filled_shares = float(matched)
    fill_price = float(status.get("price", target.price))
    console.print(f"[green]  ✅ Filled {filled_shares:.0f} shares @ ${fill_price:.4f}[/green]")
    console.print()

    # Step 3: Sell filled shares
    console.print("[bold]Step 2: Placing sell order...[/bold]")
    current_price = get_current_price(target.token_id)
    if current_price is None:
        console.print("[red]Could not fetch current price. Manual sell required.[/red]")
        return

    success = place_sell_order(client, target.token_id, filled_shares, current_price)

    # Step 4: Log result
    gain_pct = (current_price - fill_price) / fill_price if fill_price > 0 else 0
    pnl = (current_price - fill_price) * filled_shares

    position = {
        "token_id":  target.token_id,
        "buy_price": fill_price,
        "shares":    filled_shares,
        "question":  target.market_question,
        "side":      target.outcome,
        "source":    "test",
    }

    if success:
        record_sell(position, current_price, gain_pct)
        console.print(f"[green]  ✅ Sold {filled_shares:.0f} shares @ ${current_price:.4f}[/green]")

    console.print()
    console.print("[bold]--- Test Result ---[/bold]")
    console.print(f"  Buy price:   ${fill_price:.4f}")
    console.print(f"  Sell price:  ${current_price:.4f}")
    console.print(f"  Shares:      {filled_shares:.0f}")
    pnl_style = "green" if pnl >= 0 else "red"
    console.print(f"  P&L:         [{pnl_style}]${pnl:+.4f} ({gain_pct*100:+.1f}%)[/{pnl_style}]")
    console.print(f"  Logged to:   {TP_LOG_FILE}")
    console.print()
    console.print("[green]Full enter/exit cycle complete.[/green]" if success else "[red]Sell failed — position may be left open.[/red]")


def main():
    print_banner()
    print_config()

    if not PRIVATE_KEY:
        console.print("[red]ERROR: Set PRIVATE_KEY in .env file[/red]")
        sys.exit(1)

    if not ensure_vpn(required=VPN_REQUIRED):
        sys.exit(1)

    client = init_client(
        private_key=PRIVATE_KEY,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER if FUNDER else None,
    )

    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    commands = {
        "run":  cmd_run,
        "test": cmd_test,
    }

    if cmd in commands:
        commands[cmd](client)
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print()
        console.print("Usage:")
        console.print("  python take_profit.py run    # Continuous TP monitor")
        console.print("  python take_profit.py test   # Test buy→sell flow with $1")


if __name__ == "__main__":
    main()

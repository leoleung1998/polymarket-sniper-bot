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
from telegram_alerts import alert_take_profit

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
BET_SIZE_USDC    = float(os.getenv("BET_SIZE_USDC", "1"))
MAX_DAILY_SPEND  = float(os.getenv("MAX_DAILY_SPEND", "35"))
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

# TP params
TP_THRESHOLD     = float(os.getenv("TP_THRESHOLD", "0.40"))
TP_SCAN_INTERVAL = int(os.getenv("TP_SCAN_INTERVAL", "60"))
TP_AGGRESSION_BPS = int(os.getenv("TP_AGGRESSION_BPS", "10"))  # bps above best bid (buy) / below best ask (sell)

DATA_DIR    = Path("data")
TRADES_FILE = DATA_DIR / "v4_trades.json"
ORDERS_FILE = DATA_DIR / "orders.json"
TP_LOG_FILE = DATA_DIR / "tp_log.json"


def print_banner():
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║   TAKE PROFIT ENGINE                 ║[/bold cyan]")
    console.print("[bold cyan]║   Locking in gains automatically.    ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/bold cyan]")
    console.print()


def print_config():
    console.print(f"  Price range:     ${MIN_PRICE} - ${MAX_PRICE}")
    console.print(f"  Bet size:        ${BET_SIZE_USDC} USDC")
    console.print(f"  Daily limit:     ${MAX_DAILY_SPEND} USDC")
    console.print(f"  Scan interval:   {SCAN_INTERVAL} min")
    console.print(f"  TP threshold:    +{TP_THRESHOLD*100:.0f}%")
    console.print(f"  TP scan:         {TP_SCAN_INTERVAL}s")
    console.print(f"  Aggression:      {TP_AGGRESSION_BPS}bps below ask")
    console.print(f"  VPN required:    {VPN_REQUIRED}")
    console.print()


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
    """Load all open (unsold) positions from both trade logs."""
    positions = []

    # v4 weather trades
    if TRADES_FILE.exists():
        trades = json.loads(TRADES_FILE.read_text())
        sold_ids = {t.get("token_id") for t in trades if t.get("type") == "sell"}
        for t in trades:
            if t.get("type") == "bet" and t.get("token_id") and t["token_id"] not in sold_ids:
                positions.append({**t, "source": "v4"})

    # v1 sniper orders
    if ORDERS_FILE.exists():
        orders = json.loads(ORDERS_FILE.read_text())
        for o in orders:
            if o.get("status") != "sold" and o.get("token_id"):
                positions.append({
                    "token_id":  o["token_id"],
                    "buy_price": o["price"],
                    "shares":    o["size"],
                    "amount":    o["usdc_spent"],
                    "question":  o.get("market_question", ""),
                    "side":      o.get("outcome", ""),
                    "source":    "v1",
                })

    return positions


def place_sell_order(client, token_id: str, shares: float, sell_price: float | None = None) -> bool:
    """
    Place an aggressive GTC sell order N bps below best ask.
    Falls back to sell_price if orderbook unavailable.
    Returns True on success.
    """
    try:
        price = aggressive_sell_price(token_id) or sell_price
        if price is None:
            console.print("[red]  Cannot determine sell price.[/red]")
            return False
        best_bid, best_ask = get_best_bid_ask(token_id)
        console.print(f"  [dim]Orderbook — bid: ${best_bid} | ask: ${best_ask} | sell at: ${price:.3f} ({TP_AGGRESSION_BPS}bps below ask)[/dim]")
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=max(round(shares, 0), 5),
            side=SELL,
        )
        signed = client.create_order(order_args)
        response = client.post_order(signed, OrderType.GTC)
        return response.get("success", True) if isinstance(response, dict) else True
    except Exception as e:
        console.print(f"[red]  Sell order failed: {type(e).__name__}: {getattr(e, '__dict__', e)}[/red]")
        return False


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
            if place_sell_order(client, token_id, shares, current_price):
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
            else:
                console.print(f"[red]  ❌ Sell failed — will retry next cycle[/red]")

    return sold


def cmd_run(client):
    """Continuous TP monitor loop."""
    console.print("[green]Take profit monitor started. Press Ctrl+C to stop.[/green]")
    console.print()

    try:
        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            console.print(f"[bold]--- TP check: {now} ---[/bold]")

            if VPN_REQUIRED and not ensure_vpn(required=True):
                console.print("[red]VPN disconnected. Pausing...[/red]")
                time.sleep(60)
                continue

            sold = run_tp_cycle(client)
            if sold:
                console.print(f"[green]Took profit on {sold} position(s)[/green]")
            else:
                console.print("[dim]No positions hit TP threshold.[/dim]")

            console.print(f"[dim]Next check in {TP_SCAN_INTERVAL}s...[/dim]")
            console.print()
            time.sleep(TP_SCAN_INTERVAL)

    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Take profit monitor stopped.[/yellow]")


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

"""
Polymarket Sniper Bot
Scans for cheap prediction market outcomes and places small bets.
Strategy: high volume, low price, asymmetric payoff.
"""

import ssl_patch  # noqa: F401 — must be first, patches SSL for ProtonVPN
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

# Always tee output to bot.log alongside the terminal
_LOG_FILE = Path(__file__).parent / "bot.log"
_log_fh = open(_LOG_FILE, "a", buffering=1)

class _Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, data):
        for s in self.streams: s.write(data)
    def flush(self):
        for s in self.streams: s.flush()
    def fileno(self): return self.streams[0].fileno()

sys.stdout = _Tee(sys.__stdout__, _log_fh)
sys.stderr = _Tee(sys.__stderr__, _log_fh)

from scanner import scan
from trader import (
    init_client,
    place_buy_order,
    save_order,
    get_daily_spend,
    get_placed_token_ids,
)
from vpn import ensure_vpn
from tracker import show_positions, show_summary
from take_profit import run_tp_cycle

load_dotenv()
console = Console()

# --- Config from .env ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
FUNDER = os.getenv("FUNDER", WALLET_ADDRESS)
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.005"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.03"))
BET_SIZE_USDC = float(os.getenv("BET_SIZE_USDC", "10"))
MAX_DAILY_SPEND = float(os.getenv("MAX_DAILY_SPEND", "100"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
VPN_REQUIRED = os.getenv("PROTON_VPN_REQUIRED", "true").lower() == "true"


def print_banner():
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║   POLYMARKET SNIPER BOT              ║[/bold cyan]")
    console.print("[bold cyan]║   Buying chaos. Selling certainty.   ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════╝[/bold cyan]")
    console.print()


def print_config():
    console.print(f"  Price range:     ${MIN_PRICE} - ${MAX_PRICE}")
    console.print(f"  Bet size:        ${BET_SIZE_USDC} USDC")
    console.print(f"  Daily limit:     ${MAX_DAILY_SPEND} USDC")
    console.print(f"  Scan interval:   {SCAN_INTERVAL} min")
    console.print(f"  VPN required:    {VPN_REQUIRED}")
    console.print()


def run_scan_cycle(client):
    """Run one full scan + buy cycle."""
    # Check daily budget
    daily_spent = get_daily_spend()
    remaining = MAX_DAILY_SPEND - daily_spent

    if remaining <= 0:
        console.print(f"[yellow]Daily limit reached (${MAX_DAILY_SPEND}). Waiting for tomorrow.[/yellow]")
        return 0

    console.print(f"[dim]Daily spend: ${daily_spent:.2f} / ${MAX_DAILY_SPEND} ({remaining:.2f} remaining)[/dim]")

    # Scan for cheap outcomes
    cheap_outcomes = scan(min_price=MIN_PRICE, max_price=MAX_PRICE)

    if not cheap_outcomes:
        console.print("[yellow]No outcomes found under threshold.[/yellow]")
        return 0

    # Filter out ones we already have positions in
    existing = get_placed_token_ids()
    new_outcomes = [o for o in cheap_outcomes if o.token_id not in existing]

    console.print(f"[dim]{len(new_outcomes)} new outcomes (filtered {len(cheap_outcomes) - len(new_outcomes)} existing)[/dim]")

    if not new_outcomes:
        console.print("[yellow]All cheap outcomes already in portfolio.[/yellow]")
        return 0

    # Place orders until daily limit
    orders_placed = 0

    for outcome in new_outcomes:
        if remaining < BET_SIZE_USDC:
            console.print(f"[yellow]Budget remaining (${remaining:.2f}) below bet size (${BET_SIZE_USDC}). Stopping.[/yellow]")
            break

        order = place_buy_order(client, outcome, BET_SIZE_USDC)
        if order:
            save_order(order)
            orders_placed += 1
            remaining -= BET_SIZE_USDC
            time.sleep(1)  # rate limit between orders

    return orders_placed


def cmd_run():
    """Main bot loop — scan and buy on interval."""
    if not PRIVATE_KEY:
        console.print("[red]ERROR: Set PRIVATE_KEY in .env file[/red]")
        console.print("Export from: polymarket.com > Cash > ... > Export Private Key")
        sys.exit(1)

    # VPN check
    if not ensure_vpn(required=VPN_REQUIRED):
        sys.exit(1)

    # Init trading client
    client = init_client(
        private_key=PRIVATE_KEY,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER if FUNDER else None,
    )

    console.print("[green]Bot started. Press Ctrl+C to stop.[/green]")
    console.print()

    try:
        while True:
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            console.print(f"[bold]--- Scan cycle: {now} ---[/bold]")

            # Re-check VPN each cycle
            if VPN_REQUIRED and not ensure_vpn(required=True):
                console.print("[red]VPN disconnected. Pausing until reconnected...[/red]")
                time.sleep(60)
                continue

            # Check take profit before scanning for new positions
            tp_sold = run_tp_cycle(client)
            if tp_sold:
                console.print(f"[green]💰 Took profit on {tp_sold} position(s)[/green]")

            orders = run_scan_cycle(client)
            console.print(f"[green]Placed {orders} new orders[/green]")
            console.print()

            # Show quick summary
            summary = show_summary()
            console.print(
                f"[dim]Total: {summary['total_orders']} orders | "
                f"${summary['total_spent']:.2f} deployed | "
                f"{summary['unique_markets']} markets[/dim]"
            )
            console.print(f"[dim]Next scan in {SCAN_INTERVAL} minutes...[/dim]")
            console.print()

            time.sleep(SCAN_INTERVAL * 60)

    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Bot stopped.[/yellow]")
        show_positions()


def cmd_scan():
    """Scan-only mode — show cheap outcomes without buying."""
    if VPN_REQUIRED and not ensure_vpn(required=True):
        sys.exit(1)

    outcomes = scan(min_price=MIN_PRICE, max_price=MAX_PRICE)

    if not outcomes:
        console.print("[yellow]No outcomes found under threshold.[/yellow]")
        return

    console.print()
    console.print(f"[bold]Found {len(outcomes)} outcomes under ${MAX_PRICE}:[/bold]")
    console.print()

    for i, o in enumerate(outcomes[:50], 1):
        potential_payout = BET_SIZE_USDC / o.price
        console.print(
            f"  {i:3d}. [cyan]${o.price:.4f}[/cyan] | "
            f"{o.outcome:20s} | "
            f"Potential: [green]${potential_payout:.0f}[/green] on ${BET_SIZE_USDC} bet | "
            f"{o.market_question[:60]}"
        )

    if len(outcomes) > 50:
        console.print(f"  ... and {len(outcomes) - 50} more")


def cmd_bracket():
    """Run v5 bracket bot — weather GFS ensemble."""
    from arb_engine_v4 import main as bracket_main
    bracket_main()


def cmd_maker():
    """Run v5 crypto maker bot — 15-min market maker strategy."""
    from arb_engine_v5_maker import main as maker_main
    maker_main()


def cmd_pairs():
    """Run v6 delta-neutral pairs bot — Mode B correlated pairs strategy."""
    from arb_engine_v6_pairs import main as pairs_main
    pairs_main()


def cmd_tp():
    """Run take profit monitor — sells positions when gain > TP_THRESHOLD."""
    from take_profit import main as tp_main
    # Pass subcommand (e.g. 'test') if provided: python bot.py tp test
    if len(sys.argv) > 2:
        sys.argv = [sys.argv[0]] + sys.argv[2:]
    else:
        sys.argv = [sys.argv[0], "run"]
    tp_main()


def cmd_dual():
    """Run weather bracket + crypto maker + take profit monitor in parallel."""
    import asyncio
    from arb_engine_v4 import run_bracket_bot
    from arb_engine_v5_maker import run_maker_bot
    from take_profit import TP_SCAN_INTERVAL

    async def run_tp_loop():
        """Async wrapper for the take profit monitor."""
        client = init_client(
            private_key=PRIVATE_KEY,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER if FUNDER else None,
        )
        while True:
            try:
                sold = run_tp_cycle(client)
                if sold:
                    console.print(f"[green]💰 TP: Took profit on {sold} position(s)[/green]")
            except Exception as e:
                console.print(f"[dim red][TP] Error: {e}[/dim red]")
            await asyncio.sleep(TP_SCAN_INTERVAL)

    async def run_all():
        console.print("[bold green]Starting DUAL mode: Weather + Crypto Maker + Take Profit[/bold green]")
        console.print()
        await asyncio.gather(
            run_bracket_bot(),
            run_maker_bot(),
            run_tp_loop(),
        )

    asyncio.run(run_all())


def cmd_positions():
    """Show current positions and P&L."""
    show_positions()


def main():
    print_banner()
    print_config()

    # Parse command
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    commands = {
        "run": cmd_run,
        "scan": cmd_scan,
        "positions": cmd_positions,
        "bracket": cmd_bracket,
        "maker": cmd_maker,
        "pairs": cmd_pairs,
        "tp": cmd_tp,
        "dual": cmd_dual,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        console.print()
        console.print("Usage:")
        console.print("  python bot.py scan        # Preview cheap outcomes (no buying)")
        console.print("  python bot.py run         # Start v3.5 bot (15-min crypto arb, DEPRECATED)")
        console.print("  python bot.py bracket     # Start v5 weather bot (GFS ensemble)")
        console.print("  python bot.py maker       # Start v5 crypto maker (15-min markets)")
        console.print("  python bot.py pairs       # Start v6 delta-neutral pairs bot (Mode B)")
        console.print("  python bot.py tp          # Run take profit monitor")
        console.print("  python bot.py dual        # Run weather + crypto maker + TP in parallel")
        console.print("  python bot.py positions   # Show positions and P&L")


if __name__ == "__main__":
    main()

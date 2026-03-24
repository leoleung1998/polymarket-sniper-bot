"""
Paper trading stats analyzer.

Usage:
    python paper_stats.py                  # reads data/paper/maker_trades.json
    python paper_stats.py data/paper       # explicit path
"""

import json
import sys
from pathlib import Path
from collections import defaultdict


def load_outcomes(data_dir: Path) -> list[dict]:
    trades_file = data_dir / "maker_trades.json"
    if not trades_file.exists():
        print(f"No trades file at {trades_file}")
        return []
    with open(trades_file) as f:
        trades = json.load(f)
    return [t for t in trades if t.get("type") == "maker_outcome"]


def win_rate(trades: list[dict]) -> tuple[float, int]:
    if not trades:
        return 0.0, 0
    wins = sum(1 for t in trades if t["outcome"] == "win")
    return wins / len(trades), len(trades)


def ev_per_trade(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    total = sum(
        (t.get("payout", 0) - t["cost"]) if t["outcome"] == "win" else -t["cost"]
        for t in trades
    )
    return total / len(trades)


def bucket(value, edges: list) -> str:
    if value is None:
        return "unknown"
    for i, edge in enumerate(edges):
        if value < edge:
            return f"<{edge}"
    return f">={edges[-1]}"


def print_table(title: str, groups: dict[str, list[dict]]):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")
    print(f"  {'Group':<20} {'Trades':>7} {'Win%':>7} {'EV/trade':>10}")
    print(f"  {'─'*20} {'─'*7} {'─'*7} {'─'*10}")
    for key in sorted(groups):
        ts = groups[key]
        wr, n = win_rate(ts)
        ev = ev_per_trade(ts)
        flag = " ✓" if wr >= 0.85 and n >= 5 else (" ✗" if wr < 0.70 and n >= 5 else "")
        print(f"  {str(key):<20} {n:>7} {wr:>7.0%} {ev:>+10.2f}{flag}")


def main():
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/paper")
    outcomes = load_outcomes(data_dir)

    if not outcomes:
        print("No completed trades yet.")
        return

    total_wr, total_n = win_rate(outcomes)
    total_ev = ev_per_trade(outcomes)
    print(f"\n{'='*60}")
    print(f"  PAPER TRADE STATS — {data_dir}")
    print(f"{'='*60}")
    print(f"  Total trades : {total_n}")
    print(f"  Win rate     : {total_wr:.1%}")
    print(f"  EV / trade   : ${total_ev:+.2f}")

    # 1. By confidence band
    by_band = defaultdict(list)
    for t in outcomes:
        by_band[t.get("band", "unknown")].append(t)
    print_table("Win rate by confidence band", by_band)

    # 2. By coin
    by_coin = defaultdict(list)
    for t in outcomes:
        by_coin[t.get("coin", "unknown")].append(t)
    print_table("Win rate by coin", by_coin)

    # 3. By gap bucket
    by_gap = defaultdict(list)
    for t in outcomes:
        g = t.get("gap")
        by_gap[bucket(g, [-0.05, 0.0, 0.05, 0.10])].append(t)
    print_table("Win rate by gap (binance_prob - poly_price)", by_gap)

    # 4. By move size
    by_move = defaultdict(list)
    for t in outcomes:
        m = t.get("move_pct")
        by_move[bucket(m, [0.10, 0.20, 0.40])].append(t)
    print_table("Win rate by move size (%)", by_move)

    # 5. By hour (UTC)
    by_hour = defaultdict(list)
    for t in outcomes:
        h = t.get("hour_utc")
        if h is None:
            by_hour["unknown"].append(t)
        elif 0 <= h < 8:
            by_hour["00-08 (low liquidity)"].append(t)
        elif 8 <= h < 13:
            by_hour["08-13 (Asia/EU open)"].append(t)
        elif 13 <= h < 21:
            by_hour["13-21 (US hours)"].append(t)
        else:
            by_hour["21-24 (late US)"].append(t)
    print_table("Win rate by hour (UTC)", by_hour)

    # 6. By direction
    by_dir = defaultdict(list)
    for t in outcomes:
        by_dir[t.get("direction", "unknown")].append(t)
    print_table("Win rate by direction", by_dir)

    print(f"\n  Note: ✓ = profitable (wr ≥ 85%, n ≥ 5)  ✗ = losing (wr < 70%, n ≥ 5)")
    print(f"  Need ≥ 10 trades per group for reliable conclusions.\n")


if __name__ == "__main__":
    main()

"""
Window Transition Lag Test
===========================
Measures how long Polymarket's API takes to make the new window market
available after a window boundary. Run this ~30 seconds before a window
boundary (e.g., at 23:13 for the 23:15 transition).

Usage:
  python test_window_lag.py

Prints a line every second showing when the new window appears in API.
"""

import json
import sys
import time

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
WINDOW_SECONDS = 900

COIN_SLUGS = {
    "BTC": "btc-updown-15m",
    "ETH": "eth-updown-15m",
    "SOL": "sol-updown-15m",
}


def get_current_window_ts():
    now = int(time.time())
    return (now // WINDOW_SECONDS) * WINDOW_SECONDS


def get_next_window_ts():
    return get_current_window_ts() + WINDOW_SECONDS


def try_fetch(slug: str) -> dict | None:
    """Returns market dict if found with valid tokens, else None."""
    try:
        r = requests.get(f"{GAMMA_URL}/markets", params={"slug": slug}, timeout=5)
        data = r.json()
        if not data:
            return None
        m = data[0] if isinstance(data, list) else data
        outcomes = json.loads(m.get("outcomes", "[]"))
        token_ids = json.loads(m.get("clobTokenIds", "[]"))
        if len(token_ids) < 2:
            return None
        return {
            "slug": m.get("slug"),
            "acceptingOrders": m.get("acceptingOrders"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "tokens": token_ids,
            "prices": json.loads(m.get("outcomePrices", "[]")),
        }
    except Exception as e:
        return None


print("Window Transition Lag Test")
print(f"Current window: {get_current_window_ts()}")
print(f"Next window:    {get_next_window_ts()}")
secs_to_next = get_next_window_ts() - int(time.time())
print(f"Time to next window: {secs_to_next}s ({secs_to_next//60}m {secs_to_next%60}s)")
print()
print("Polling every second. Waiting for window transition...")
print("Press Ctrl+C to stop.\n")

# Track when each coin's new window market first appears
found: dict[str, float] = {}
start_time = None
transition_ts = get_next_window_ts()
transitioned = False

while True:
    now = int(time.time())
    cur_ts = get_current_window_ts()

    if cur_ts >= transition_ts and not transitioned:
        transitioned = True
        start_time = time.time()
        print(f"\n{'='*60}")
        print(f"WINDOW TRANSITIONED at {time.strftime('%H:%M:%S')}")
        print(f"New window: {transition_ts}  (ends at {transition_ts + WINDOW_SECONDS})")
        print(f"{'='*60}\n")
        # Record a new target
        transition_ts = cur_ts  # lock to current

    if transitioned:
        elapsed = time.time() - start_time
        row = f"T+{elapsed:5.1f}s | "
        all_found = True

        for coin, prefix in COIN_SLUGS.items():
            if coin in found:
                row += f"{coin}:✅ ({found[coin]:.1f}s)  "
                continue

            slug = f"{prefix}-{transition_ts}"
            market = try_fetch(slug)

            if market:
                found[coin] = elapsed
                accepting = market["acceptingOrders"]
                row += f"{coin}:FOUND(acc={accepting}) at {elapsed:.1f}s  "
            else:
                # Also try search fallback
                try:
                    r = requests.get(
                        f"{GAMMA_URL}/markets",
                        params={"slug_contains": prefix, "active": "true", "closed": "false",
                                "order": "endDate", "ascending": "true", "limit": 3},
                        timeout=5,
                    )
                    markets = r.json()
                    new_market = next(
                        (m for m in markets if str(transition_ts) in m.get("slug", "")),
                        None,
                    )
                    if new_market:
                        found[coin] = elapsed
                        row += f"{coin}:SEARCH_FOUND at {elapsed:.1f}s  "
                    else:
                        row += f"{coin}:waiting...  "
                        all_found = False
                except Exception:
                    row += f"{coin}:err  "
                    all_found = False

        print(row)

        if all_found and len(found) == len(COIN_SLUGS):
            print(f"\n✅ All coins found. Max lag = {max(found.values()):.1f}s")
            break

        if elapsed > 300:
            print("\n❌ Timeout after 5 minutes — giving up")
            break
    else:
        row = f"T-{secs_to_next:3d}s | Current window: {cur_ts} | "
        secs_to_next = transition_ts - now
        for coin, prefix in COIN_SLUGS.items():
            slug = f"{prefix}-{transition_ts}"
            m = try_fetch(slug)
            if m:
                row += f"{coin}:PRE-EXISTS(acc={m['acceptingOrders']})  "
            else:
                row += f"{coin}:not yet  "
        print(row)

    time.sleep(1)

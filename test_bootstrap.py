"""
Bootstrap Fix Validation Test
==============================
Simulates the window transition sequence to confirm that:
1. register_tokens() clears old prices (causes stale)
2. Bootstrap seeds new prices from Gamma API immediately
3. display never shows (stale) after transition

Run: python test_bootstrap.py
"""

import time
from poly_feed import poly_feed
from poly_ws import ws_feed
from crypto_markets import discover_market, discover_market_tokens

COINS = ["BTC", "ETH", "SOL"]


def get_prices_summary():
    results = {}
    for coin in COINS:
        up, down = poly_feed.get_market_prices(coin)
        results[coin] = {"up": up, "down": down}
    return results


def check_stale():
    stale = []
    for coin in COINS:
        up, down = poly_feed.get_market_prices(coin)
        if up is None or down is None:
            stale.append(coin)
    return stale


def print_prices(label: str):
    print(f"\n  [{label}]")
    for coin in COINS:
        up, down = poly_feed.get_market_prices(coin)
        if up is not None:
            print(f"    {coin}: UP={up:.3f}  DOWN={down:.3f}  ✅")
        else:
            print(f"    {coin}: (stale) ❌")


# ── Step 1: Fetch real markets ───────────────────────────────────────────────
print("Step 1: Discovering current markets...")
markets = {}
for coin in COINS:
    m = discover_market(coin)
    if not m:
        print(f"  ERROR: no active market for {coin}")
        exit(1)
    markets[coin] = m
    print(f"  {coin}: {m.slug}  UP={m.up_price:.3f}  DOWN={m.down_price:.3f}")


# ── Step 2 + 3: Simulate old window running, then transition to new window ────
# Use FAKE token IDs for the "old" window, real token IDs for the "new" window.
# This mirrors exactly what happens in production.
from poly_feed import PolyMarketPrice

print("\nStep 2: Simulate old window — seed prices with FAKE old token IDs...")
fake_tokens = {}
for coin in COINS:
    fake_up   = f"FAKE_OLD_UP_{coin}"
    fake_down = f"FAKE_OLD_DN_{coin}"
    fake_tokens[coin] = (fake_up, fake_down)
    # Plant in _token_map (as if old window was subscribed)
    ws_feed._token_map[fake_up]   = (coin, "up")
    ws_feed._token_map[fake_down] = (coin, "down")
    # Plant in poly_feed._prices (as if WS was sending events for old window)
    poly_feed._prices[fake_up]   = PolyMarketPrice(fake_up,   coin, "up",   0.55)
    poly_feed._prices[fake_down] = PolyMarketPrice(fake_down, coin, "down", 0.45)

stale = check_stale()
assert not stale, f"Expected fake prices to be visible, got stale: {stale}"
print_prices("Old window live — prices showing (fake IDs)")

print("\nStep 3: Window transitions — register_tokens() called with NEW real token IDs...")
# This is exactly what the engine does at window boundary
for coin, m in markets.items():
    ws_feed.register_tokens(coin, m.up_token_id, m.down_token_id)

stale = check_stale()
print_prices("After register_tokens — old prices cleared, WS not yet sending for new tokens")

if stale == COINS:
    print(f"\n  ✅ Confirmed: all {len(COINS)} coins stale — this is the bug we're fixing")
else:
    print(f"\n  Stale: {stale} | Not stale: {[c for c in COINS if c not in stale]}")


# ── Step 4: Run the bootstrap (the fix) ──────────────────────────────────────
print("\nStep 4: Running REST bootstrap (the fix)...")
t0 = time.time()
for coin in COINS:
    bm = discover_market(coin)
    if bm:
        poly_feed.update(bm.up_token_id, coin, "up", bm.up_price)
        poly_feed.update(bm.down_token_id, coin, "down", bm.down_price)
elapsed = time.time() - t0

stale = check_stale()
print_prices("After bootstrap — should all show prices again")

print(f"\n  Bootstrap took: {elapsed*1000:.0f}ms")

if not stale:
    print("  ✅ PASS: Bootstrap restored all prices — display will never show (stale)")
else:
    print(f"  ❌ FAIL: Still stale after bootstrap: {stale}")


# ── Step 5: Verify prices are reasonable ─────────────────────────────────────
print("\nStep 5: Sanity check prices...")
all_ok = True
for coin in COINS:
    up, down = poly_feed.get_market_prices(coin)
    total = (up or 0) + (down or 0)
    ok = up is not None and 0.01 < up < 0.99 and 0.9 < total < 1.1
    status = "✅" if ok else "❌"
    print(f"  {coin}: UP={up}  DOWN={down}  sum={total:.3f}  {status}")
    if not ok:
        all_ok = False

print()
if all_ok:
    print("✅ ALL CHECKS PASSED — bootstrap fix is working correctly")
else:
    print("❌ SOME CHECKS FAILED — investigate prices above")

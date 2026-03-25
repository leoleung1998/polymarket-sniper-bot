"""
WS Subscribe Behavior Test
===========================
Confirms whether Polymarket WS subscribe is REPLACE or ADD.

Test plan:
  Phase 1 (10s): Subscribe to BTC tokens only → count BTC updates
  Phase 2 (10s): Send new subscribe with ETH tokens only → count BTC + ETH updates
  Phase 3 (10s): Send new subscribe with BOTH → count BTC + ETH updates

Expected if REPLACE:  Phase 2 → BTC drops to 0, ETH gets updates
Expected if ADD:      Phase 2 → both BTC and ETH get updates

Run: python test_ws_subscribe.py
"""

import asyncio
import json
import time
import sys

import websockets
import requests

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"


def fetch_tokens(slug_prefix: str) -> tuple[str, str] | None:
    """Fetch up/down token IDs for a coin's current window."""
    now = int(time.time())
    window_ts = (now // 900) * 900

    for ts in [window_ts, window_ts + 900]:
        slug = f"{slug_prefix}-{ts}"
        try:
            resp = requests.get(f"{GAMMA_URL}/markets", params={"slug": slug}, timeout=10)
            data = resp.json()
            if not data:
                continue
            market = data[0] if isinstance(data, list) else data
            outcomes = json.loads(market.get("outcomes", "[]"))
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
            if len(outcomes) < 2 or len(token_ids) < 2:
                continue
            up_idx   = next((i for i, o in enumerate(outcomes) if o.lower() == "up"),   None)
            down_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "down"), None)
            if up_idx is None or down_idx is None:
                continue
            print(f"  Found {slug}")
            return token_ids[up_idx], token_ids[down_idx]
        except Exception as e:
            print(f"  Error fetching {slug}: {e}")
    return None


def subscribe_msg(tokens: list[str]) -> str:
    return json.dumps({
        "assets_ids": tokens,
        "type": "market",
        "initial_dump": False,
        "level": 2,
        "custom_feature_enabled": False,
    })


async def run_test():
    print("Fetching BTC tokens...")
    btc = fetch_tokens("btc-updown-15m")
    print("Fetching ETH tokens...")
    eth = fetch_tokens("eth-updown-15m")

    if not btc or not eth:
        print("ERROR: could not fetch tokens. Is the market active?")
        sys.exit(1)

    btc_tokens = list(btc)
    eth_tokens = list(eth)
    print(f"\nBTC tokens: {btc_tokens[0][:16]}... {btc_tokens[1][:16]}...")
    print(f"ETH tokens: {eth_tokens[0][:16]}... {eth_tokens[1][:16]}...")

    counts: dict[str, int] = {
        "btc_phase1": 0,
        "btc_phase2": 0,
        "eth_phase2": 0,
        "btc_phase3": 0,
        "eth_phase3": 0,
    }

    phase = 1
    phase_start = time.time()
    PHASE_DURATION = 10  # seconds per phase

    print(f"\n{'='*60}")
    print(f"Phase 1 ({PHASE_DURATION}s): subscribed to BTC only")
    print(f"{'='*60}")

    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        # Phase 1: BTC only
        await ws.send(subscribe_msg(btc_tokens))

        async def heartbeat():
            while True:
                await asyncio.sleep(10)
                try:
                    await ws.send("PING")
                except Exception:
                    break
        asyncio.create_task(heartbeat())

        async for raw in ws:
            if raw == "PONG":
                continue
            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                if msg.get("event_type") not in ("price_change", "best_bid_ask"):
                    continue

                # Extract token id(s) from message
                token_ids_in_msg = []
                if msg.get("event_type") == "price_change":
                    for c in msg.get("price_changes", []):
                        token_ids_in_msg.append(c.get("asset_id", ""))
                else:
                    token_ids_in_msg.append(msg.get("asset_id", ""))

                for tid in token_ids_in_msg:
                    is_btc = tid in btc_tokens
                    is_eth = tid in eth_tokens

                    if phase == 1:
                        if is_btc:
                            counts["btc_phase1"] += 1
                    elif phase == 2:
                        if is_btc:
                            counts["btc_phase2"] += 1
                        if is_eth:
                            counts["eth_phase2"] += 1
                    elif phase == 3:
                        if is_btc:
                            counts["btc_phase3"] += 1
                        if is_eth:
                            counts["eth_phase3"] += 1

            now = time.time()
            elapsed = now - phase_start

            if phase == 1 and elapsed >= PHASE_DURATION:
                phase = 2
                phase_start = now
                await ws.send(subscribe_msg(eth_tokens))  # ETH only — replaces BTC?
                print(f"\n{'='*60}")
                print(f"Phase 1 result: BTC updates = {counts['btc_phase1']}")
                print(f"Phase 2 ({PHASE_DURATION}s): sent NEW subscribe with ETH only")
                print(f"  → If REPLACE: BTC drops to 0, ETH gets updates")
                print(f"  → If ADD:     both BTC and ETH get updates")
                print(f"{'='*60}")

            elif phase == 2 and elapsed >= PHASE_DURATION:
                phase = 3
                phase_start = now
                await ws.send(subscribe_msg(btc_tokens + eth_tokens))  # both
                print(f"\n{'='*60}")
                print(f"Phase 2 result: BTC={counts['btc_phase2']}  ETH={counts['eth_phase2']}")
                if counts["btc_phase2"] == 0 and counts["eth_phase2"] > 0:
                    print("  ✅ CONFIRMED REPLACE — BTC dropped, ETH appeared")
                elif counts["btc_phase2"] > 0 and counts["eth_phase2"] > 0:
                    print("  ⚠️  APPEARS TO BE ADD — both got updates (unexpected)")
                elif counts["btc_phase2"] == 0 and counts["eth_phase2"] == 0:
                    print("  ❓ No updates in phase 2 at all — market may be quiet")
                print(f"Phase 3 ({PHASE_DURATION}s): sent subscribe with BOTH BTC + ETH")
                print(f"{'='*60}")

            elif phase == 3 and elapsed >= PHASE_DURATION:
                break  # done

    print(f"\n{'='*60}")
    print("FINAL RESULTS")
    print(f"{'='*60}")
    print(f"Phase 1 — BTC only subscribed:      BTC={counts['btc_phase1']:4d}  ETH=  0")
    print(f"Phase 2 — ETH only subscribed:      BTC={counts['btc_phase2']:4d}  ETH={counts['eth_phase2']:4d}")
    print(f"Phase 3 — Both subscribed:          BTC={counts['btc_phase3']:4d}  ETH={counts['eth_phase3']:4d}")
    print()

    if counts["btc_phase2"] == 0 and counts["eth_phase2"] > 0:
        print("VERDICT: REPLACE ✅")
        print("  Each subscribe message replaces all previous subscriptions.")
        print("  Sending only new tokens drops the old ones — confirmed bug.")
    elif counts["btc_phase2"] > 0:
        print("VERDICT: ADD (or partial retain) ⚠️")
        print("  BTC still got updates after ETH-only subscribe.")
        print("  The flush bug may not be the issue — investigate further.")
    else:
        print("VERDICT: INCONCLUSIVE ❓")
        print("  No updates at all in phase 2. Market may have been quiet.")
        print("  Re-run during active trading hours.")


if __name__ == "__main__":
    asyncio.run(run_test())

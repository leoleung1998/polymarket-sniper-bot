"""
Polymarket CLOB WebSocket dummy test.

Tests two channels:
  1. market  — real-time order book + best_bid_ask updates for a token
  2. user    — fill/order events for your wallet (all markets)

Run:
    python test_poly_ws.py

Press Ctrl+C to stop.
"""

import asyncio
import json
import os

import websockets
from dotenv import load_dotenv

from trader import init_client
from crypto_markets import discover_market

load_dotenv()

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER_URL   = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
TEST_COIN     = "BTC"   # change to any coin in MAKER_COINS
PING_INTERVAL = 10      # seconds — required to keep connection alive


async def test_market_channel(token_id: str):
    """Subscribe to real-time best_bid_ask + trades for a token."""
    print(f"\n[market] Connecting to {WS_MARKET_URL}")
    async with websockets.connect(WS_MARKET_URL) as ws:
        sub = {
            "assets_ids": [token_id],
            "type": "market",
            "initial_dump": True,       # receive current orderbook snapshot immediately
            "level": 2,
            "custom_feature_enabled": True,  # enables best_bid_ask events
        }
        await ws.send(json.dumps(sub))
        print(f"[market] Subscribed to token {token_id[:16]}...")
        print(f"[market] Listening for price updates (best_bid_ask, price_change, last_trade_price)...\n")

        count = 0

        async def heartbeat():
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send("PING")

        asyncio.create_task(heartbeat())

        async for raw in ws:
            if raw == "PONG":
                continue  # heartbeat response — ignore

            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[market] non-JSON: {raw[:100]}")
                continue

            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                mtype = msg.get("event_type", msg.get("type", "unknown"))

                if mtype == "best_bid_ask":
                    count += 1
                    print(f"[market #{count}] best_bid={msg.get('best_bid')} best_ask={msg.get('best_ask')} spread={msg.get('spread')} asset={msg.get('asset_id','')[:12]}...")

                elif mtype == "last_trade_price":
                    count += 1
                    print(f"[market #{count}] TRADE price={msg.get('price')} size={msg.get('size')} side={msg.get('side')} asset={msg.get('asset_id','')[:12]}...")

                elif mtype == "price_change":
                    changes = msg.get("price_changes", [])
                    for c in changes:
                        count += 1
                        removed = c.get("size") == "0"
                        print(f"[market #{count}] price_change {'REMOVE' if removed else 'ADD'} side={c.get('side')} price={c.get('price')} size={c.get('size')} best_bid={c.get('best_bid')} best_ask={c.get('best_ask')}")

                elif mtype == "book":
                    bids = msg.get("bids", [])
                    asks = msg.get("asks", [])
                    best_bid = bids[0]["price"] if bids else "—"
                    best_ask = asks[0]["price"] if asks else "—"
                    print(f"[market] SNAPSHOT best_bid={best_bid} best_ask={best_ask} ({len(bids)} bids, {len(asks)} asks)")

                else:
                    print(f"[market] {mtype}: {json.dumps(msg)[:150]}")


async def test_user_channel(api_key: str, secret: str, passphrase: str):
    """Subscribe to fill events for your wallet (all markets)."""
    print(f"\n[user] Connecting to {WS_USER_URL}")
    async with websockets.connect(WS_USER_URL) as ws:
        sub = {
            "auth": {
                "apiKey":     api_key,
                "secret":     secret,
                "passphrase": passphrase,
            },
            "type": "user",
            # omit "markets" to receive events for ALL markets
        }
        await ws.send(json.dumps(sub))
        print(f"[user] Subscribed. Listening for order/fill events...\n")

        async def heartbeat():
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send("PING")

        asyncio.create_task(heartbeat())

        async for raw in ws:
            if raw == "PONG":
                continue

            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[user] non-JSON: {raw[:100]}")
                continue

            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                mtype = msg.get("event_type", msg.get("type", "unknown"))

                if mtype == "order":
                    otype = msg.get("type", "")
                    print(f"[user] ORDER {otype} id={msg.get('id','')[:12]}... price={msg.get('price')} size={msg.get('original_size')} matched={msg.get('size_matched')} side={msg.get('side')}")

                elif mtype == "trade":
                    print(f"[user] TRADE status={msg.get('status')} price={msg.get('price')} size={msg.get('size')} side={msg.get('side')} id={msg.get('id','')[:12]}...")

                else:
                    print(f"[user] {mtype}: {json.dumps(msg)[:200]}")


async def main():
    private_key    = os.getenv("PRIVATE_KEY")
    signature_type = int(os.getenv("SIGNATURE_TYPE", "2"))
    funder         = os.getenv("FUNDER") or os.getenv("WALLET_ADDRESS")

    if not private_key:
        print("ERROR: PRIVATE_KEY not set in .env")
        return

    print("Deriving CLOB API credentials...")
    client     = init_client(private_key, signature_type, funder)
    creds      = client.creds
    api_key    = creds.api_key
    secret     = creds.api_secret
    passphrase = creds.api_passphrase
    print(f"  api_key={api_key[:8]}...")

    print(f"\nLooking up active {TEST_COIN} 15-min market...")
    market = discover_market(TEST_COIN)
    if not market:
        print(f"ERROR: No active {TEST_COIN} market found. Try again at next window boundary (:00/:15/:30/:45)")
        return

    token_id = market.up_token_id
    print(f"  Market : {market.question}")
    print(f"  UP token: {token_id[:24]}...")
    print(f"  Remaining: {market.seconds_remaining}s")

    print("\n--- Running BOTH channels in parallel (Ctrl+C to stop) ---")
    print("market channel → best_bid_ask, price_change, trades")
    print("user channel   → your order placements + fills")
    print("-" * 55)

    try:
        await asyncio.gather(
            test_market_channel(token_id),
            test_user_channel(api_key, secret, passphrase),
        )
    except KeyboardInterrupt:
        print("\n[done] Interrupted.")


if __name__ == "__main__":
    asyncio.run(main())

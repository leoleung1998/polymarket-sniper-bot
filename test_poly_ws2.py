"""
Test: subscribe to all active 15-min up/down markets via Polymarket WS
and print live best_bid/best_ask for each coin.

Run:
    python test_poly_ws2.py
"""

import asyncio
import json
import os

import websockets
from dotenv import load_dotenv

from crypto_markets import discover_market

load_dotenv()

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COINS         = ["BTC", "ETH", "SOL", "XRP", "BNB"]
PING_INTERVAL = 10


async def main():
    # Discover all active markets
    print("Discovering active 15-min markets...")
    token_map = {}  # token_id → (coin, side)
    all_tokens = []

    for coin in COINS:
        market = discover_market(coin)
        if market:
            token_map[market.up_token_id]   = (coin, "UP")
            token_map[market.down_token_id] = (coin, "DOWN")
            all_tokens.extend([market.up_token_id, market.down_token_id])
            print(f"  {coin}: {market.question} ({market.seconds_remaining}s left)")
        else:
            print(f"  {coin}: no active market found")

    if not all_tokens:
        print("\nNo active markets found. Try again at next window boundary (:00/:15/:30/:45)")
        return

    print(f"\nSubscribing to {len(all_tokens)} tokens for {len(token_map)//2} coins...")
    print("-" * 55)

    async with websockets.connect(WS_MARKET_URL) as ws:
        sub = {
            "assets_ids": all_tokens,
            "type": "market",
            "initial_dump": False,       # skip initial snapshot, only live updates
            "level": 2,
            "custom_feature_enabled": True,  # enables best_bid_ask events
        }
        await ws.send(json.dumps(sub))
        print("Connected. Streaming live prices (Ctrl+C to stop)...\n")

        # Track last known prices
        prices = {}  # token_id → (best_bid, best_ask)

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
                continue

            if not isinstance(msgs, list):
                msgs = [msgs]

            for msg in msgs:
                mtype = msg.get("event_type", "")

                if mtype == "best_bid_ask":
                    token_id = msg.get("asset_id", "")
                    coin, side = token_map.get(token_id, ("?", "?"))
                    bid  = msg.get("best_bid", "—")
                    ask  = msg.get("best_ask", "—")
                    mid  = round((float(bid) + float(ask)) / 2, 4) if bid != "—" and ask != "—" else "—"
                    prices[token_id] = (bid, ask)
                    print(f"  {coin:3} {side:4} | bid={bid:5} ask={ask:5} mid={mid}")

                elif mtype == "price_change":
                    for c in msg.get("price_changes", []):
                        token_id = c.get("asset_id", "")
                        coin, side = token_map.get(token_id, ("?", "?"))
                        bid = c.get("best_bid")
                        ask = c.get("best_ask")
                        if bid and ask:
                            mid = round((float(bid) + float(ask)) / 2, 4)
                            print(f"  {coin:3} {side:4} | bid={bid:5} ask={ask:5} mid={mid}  (price_change)")

                elif mtype == "last_trade_price":
                    token_id = msg.get("asset_id", "")
                    coin, side = token_map.get(token_id, ("?", "?"))
                    print(f"  {coin:3} {side:4} | TRADE price={msg.get('price')} size={msg.get('size')} side={msg.get('side')}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[done]")

"""
Polymarket WebSocket price feed.

Replaces REST poll (poll_poly_prices) with push-based CLOB market channel.
Updates the same poly_feed instance the bot already reads — zero changes to
the rest of the codebase.

Architecture:
  - Single persistent WS connection to ws/market
  - Bot calls ws_feed.register_tokens(coin, up_id, down_id) on each new window
  - price_change events → poly_feed.update() in real-time
  - Silent >15s → falls back to REST poll_poly_prices
  - Reconnects up to 3 times on drop, then permanently falls back to REST
  - All failures → Telegram alert
"""

import asyncio
import json
import time

import websockets
import telegram_alerts as tg
from poly_feed import poly_feed, poll_poly_prices

WS_MARKET_URL  = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL  = 10    # seconds between PING heartbeats
SILENT_TIMEOUT = 15    # seconds of no updates before fallback triggers
MAX_RETRIES    = 3     # reconnect attempts before permanent fallback


class PolyWSFeed:
    def __init__(self):
        self._token_map: dict[str, tuple[str, str]] = {}  # token_id → (coin, side)
        self._pending_tokens: list[str] = []              # tokens to subscribe once connected
        self._ws = None
        self._connected = False
        self._last_msg: float = time.time()
        self._using_fallback = False
        self._disconnect_alerted = False                  # avoid repeat disconnect spam
        self._update_count = 0

    # ── Public API ──────────────────────────────────────────────────────────────

    def register_tokens(self, coin: str, up_token: str, down_token: str):
        """
        Call when a new 15-min window is discovered.
        Registers token→coin/side mapping and triggers subscription if connected.
        """
        self._token_map[up_token]   = (coin, "up")
        self._token_map[down_token] = (coin, "down")
        self._pending_tokens.extend([up_token, down_token])

    async def flush_pending_tokens(self):
        """Subscribe any tokens registered since last connection."""
        if not self._pending_tokens or not self._connected or self._ws is None:
            return
        tokens = list(set(self._pending_tokens))
        self._pending_tokens.clear()
        try:
            msg = {
                "assets_ids": tokens,
                "type":       "market",
                "initial_dump": False,
                "level": 2,
                "custom_feature_enabled": False,
            }
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            print(f"[poly_ws] subscribe failed: {e}")

    @property
    def stats(self) -> str:
        src = "REST fallback" if self._using_fallback else "WS"
        return f"source={src} updates={self._update_count} tokens={len(self._token_map)}"

    # ── Internal ────────────────────────────────────────────────────────────────

    async def _connect_and_stream(self, coins: list[str]):
        """Open WS, subscribe to all known tokens, stream updates."""
        async with websockets.connect(WS_MARKET_URL, ping_interval=None) as ws:
            self._ws = ws
            self._connected = True
            self._last_msg = time.time()
            print("[poly_ws] Connected to Polymarket WS market channel")

            # Subscribe to all currently known tokens
            all_tokens = list(self._token_map.keys()) + self._pending_tokens
            self._pending_tokens.clear()
            if all_tokens:
                await ws.send(json.dumps({
                    "assets_ids": list(set(all_tokens)),
                    "type":       "market",
                    "initial_dump": False,
                    "level": 2,
                    "custom_feature_enabled": False,
                }))

            if self._disconnect_alerted:
                tg.send_message("✅ PolyWS reconnected — live prices restored")
                self._disconnect_alerted = False

            # Heartbeat task
            async def heartbeat():
                while True:
                    await asyncio.sleep(PING_INTERVAL)
                    try:
                        await ws.send("PING")
                    except Exception:
                        break

            asyncio.create_task(heartbeat())

            # Flush any tokens registered while disconnected
            await self.flush_pending_tokens()

            # Main message loop
            async for raw in ws:
                if raw == "PONG":
                    self._last_msg = time.time()
                    continue

                try:
                    msgs = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if not isinstance(msgs, list):
                    msgs = [msgs]

                for msg in msgs:
                    self._handle_message(msg)

                self._last_msg = time.time()

    def _handle_message(self, msg: dict):
        mtype = msg.get("event_type", "")

        if mtype == "price_change":
            for c in msg.get("price_changes", []):
                token_id = c.get("asset_id", "")
                bid = c.get("best_bid")
                ask = c.get("best_ask")
                if not (bid and ask and token_id):
                    continue
                coin, side = self._token_map.get(token_id, (None, None))
                if coin is None:
                    continue
                try:
                    mid = round((float(bid) + float(ask)) / 2, 4)
                    poly_feed.update(token_id, coin, side, mid)
                    self._update_count += 1
                except (ValueError, TypeError):
                    pass

        elif mtype == "best_bid_ask":
            token_id = msg.get("asset_id", "")
            bid = msg.get("best_bid")
            ask = msg.get("best_ask")
            if not (bid and ask and token_id):
                return
            coin, side = self._token_map.get(token_id, (None, None))
            if coin is None:
                return
            try:
                mid = round((float(bid) + float(ask)) / 2, 4)
                poly_feed.update(token_id, coin, side, mid)
                self._update_count += 1
            except (ValueError, TypeError):
                pass

    async def run(self, coins: list[str]):
        """
        Long-running WS feed task.
        Retries up to MAX_RETRIES on disconnect, then falls back to REST poll.
        """
        retries = 0

        while retries < MAX_RETRIES:
            try:
                self._connected = False
                self._ws = None
                await self._connect_and_stream(coins)

            except websockets.exceptions.ConnectionClosed as e:
                retries += 1
                self._connected = False
                msg = f"⚠️ PolyWS disconnected ({e}) — reconnecting... (attempt {retries}/{MAX_RETRIES})"
                print(f"[poly_ws] {msg}")
                if not self._disconnect_alerted:
                    tg.send_message(msg)
                    self._disconnect_alerted = True
                await asyncio.sleep(2 ** retries)  # exponential backoff

            except Exception as e:
                retries += 1
                msg = f"⚠️ PolyWS error: {str(e)[:120]} — reconnecting... (attempt {retries}/{MAX_RETRIES})"
                print(f"[poly_ws] {msg}")
                if not self._disconnect_alerted:
                    tg.send_message(msg)
                    self._disconnect_alerted = True
                await asyncio.sleep(2 ** retries)

        # Max retries exceeded — fall back to REST permanently
        self._connected = False
        self._using_fallback = True
        err = f"❌ PolyWS gave up after {MAX_RETRIES} retries — falling back to REST poll"
        print(f"[poly_ws] {err}")
        tg.send_message(err)
        await poll_poly_prices(coins)  # takes over indefinitely


# ── Silence watchdog ──────────────────────────────────────────────────────────

async def ws_silence_watchdog(ws_feed: PolyWSFeed, coins: list[str]):
    """
    Monitors WS feed for silence. If no updates for SILENT_TIMEOUT seconds,
    sends Telegram alert. Does NOT switch to fallback — reconnect loop handles that.
    """
    alerted = False
    while True:
        await asyncio.sleep(5)
        if ws_feed._using_fallback:
            return  # fallback already active, watchdog not needed

        silent_for = time.time() - ws_feed._last_msg
        if silent_for > SILENT_TIMEOUT and not alerted:
            tg.send_message(f"⚠️ PolyWS no updates for {int(silent_for)}s — may be stale")
            alerted = True
        elif silent_for < SILENT_TIMEOUT and alerted:
            alerted = False  # recovered


# Global instance
ws_feed = PolyWSFeed()

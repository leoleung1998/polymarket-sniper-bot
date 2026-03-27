"""
Real-time portfolio tracker — Polymarket user WebSocket + REST.

Tracks:
  - USDC cash balance (polled via REST every 30s)
  - Open positions (bootstrapped from REST, updated by user WS fills)
  - Total portfolio value = cash + Σ(shares × current price)

Usage:
    portfolio.init(client)
    asyncio.create_task(portfolio.run())
    val = portfolio.total_value()
"""

import asyncio
import json
import time

import requests
import websockets

from poly_feed import poly_feed
from order_book import order_book

WS_USER_URL   = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
PING_INTERVAL = 10    # seconds between PING heartbeats
BALANCE_POLL  = 30    # seconds between USDC balance REST polls

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PortfolioTracker:
    def __init__(self):
        self.cash: float = 0.0                    # USDC in wallet
        self.positions: dict[str, float] = {}     # token_id -> shares held
        self.connected: bool = False
        self._api_key:    str = ""
        self._secret:     str = ""
        self._passphrase: str = ""
        self._address:    str = ""
        self._client      = None
        self._last_balance_fetch: float = 0.0
        self._fill_count: int = 0

    def init(self, client, address: str):
        """Call once after creating the CLOB client."""
        self._client     = client
        self._address    = address
        self._api_key    = client.creds.api_key
        self._secret     = client.creds.api_secret
        self._passphrase = client.creds.api_passphrase

    # ── Value ────────────────────────────────────────────────────────────────

    def position_value(self) -> float:
        """Current market value of all open positions."""
        total = 0.0
        for token_id, shares in self.positions.items():
            if shares <= 0:
                continue
            px = poly_feed.get_price(token_id) or order_book.mid(token_id)
            if px:
                total += shares * px
        return total

    def total_value(self) -> float:
        return self.cash + self.position_value()

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _fetch_balance(self):
        """Fetch USDC balance via CLOB REST. Balance is returned in raw units (6 decimals)."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            r = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = int(r.get("balance", 0))
            self.cash = raw / 1e6   # USDC has 6 decimals
            self._last_balance_fetch = time.time()
        except Exception as e:
            print(f"[portfolio] balance fetch failed: {e}")

    def _fetch_positions(self):
        """Bootstrap open positions from Polymarket data API."""
        try:
            resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": self._address, "sizeThreshold": "0.01"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for row in data:
                token_id = row.get("asset") or row.get("token_id") or row.get("conditionId")
                size     = float(row.get("size", 0))
                if token_id and size > 0:
                    self.positions[token_id] = size
            print(f"[portfolio] bootstrapped {len(self.positions)} open positions")
        except Exception as e:
            print(f"[portfolio] positions fetch failed: {e}")

    # ── User WS ───────────────────────────────────────────────────────────────

    def _handle_trade(self, msg: dict):
        """Update positions from a fill event."""
        status   = msg.get("status", "")
        if status not in ("MATCHED", "MINED"):
            return
        token_id = msg.get("asset_id", "")
        side     = msg.get("side", "")
        try:
            size = float(msg.get("size", 0))
        except (ValueError, TypeError):
            return
        if not token_id or size <= 0:
            return

        current = self.positions.get(token_id, 0.0)
        if side == "BUY":
            self.positions[token_id] = current + size
        elif side == "SELL":
            self.positions[token_id] = max(0.0, current - size)
            if self.positions[token_id] == 0:
                del self.positions[token_id]
        self._fill_count += 1

    async def _stream(self):
        """Open user WS and stream fill events."""
        async with websockets.connect(WS_USER_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({
                "auth": {
                    "apiKey":     self._api_key,
                    "secret":     self._secret,
                    "passphrase": self._passphrase,
                },
                "type": "user",
            }))
            self.connected = True
            print("[portfolio] user WS connected")

            async def heartbeat():
                while True:
                    await asyncio.sleep(PING_INTERVAL)
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
                    if msg.get("event_type") == "trade":
                        self._handle_trade(msg)

    async def run(self):
        """Long-running background task. Call after init()."""
        if not self._client:
            print("[portfolio] not initialized — call portfolio.init(client, address) first")
            return

        # Bootstrap
        await asyncio.to_thread(self._fetch_balance)
        await asyncio.to_thread(self._fetch_positions)

        # Periodic balance refresh in background
        async def balance_loop():
            while True:
                await asyncio.sleep(BALANCE_POLL)
                await asyncio.to_thread(self._fetch_balance)

        asyncio.create_task(balance_loop())

        # User WS with reconnect
        while True:
            try:
                self.connected = False
                await self._stream()
            except websockets.exceptions.ConnectionClosed as e:
                self.connected = False
                print(f"[portfolio] user WS disconnected: {e} — reconnecting in 5s")
                await asyncio.sleep(5)
            except Exception as e:
                self.connected = False
                print(f"[portfolio] user WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)


# Global instance
portfolio = PortfolioTracker()

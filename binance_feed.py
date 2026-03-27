"""
Binance real-time price feed via WebSocket.
No API key required — uses public market data streams.
"""

import asyncio
import json
import math
import ssl
import time
from dataclasses import dataclass, field
from collections import deque

import websockets


BINANCE_WS = "wss://stream.binance.com:9443/ws"
BINANCE_REST = "https://data-api.binance.vision/api/v3/ticker/price"

# Symbols we track
SYMBOLS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "BNB": "bnbusdt",
}


@dataclass
class PriceTick:
    symbol: str
    price: float
    timestamp: float


@dataclass
class EWMAState:
    """Holds running fast/slow EWMA state for one symbol."""
    fast: float | None = None   # ~10s half-life
    slow: float | None = None   # ~30s half-life
    last_ts: float | None = None

    # half-life constants (seconds)
    FAST_TAU: float = field(default=10.0, init=False, repr=False)
    SLOW_TAU: float = field(default=30.0, init=False, repr=False)

    def update(self, price: float):
        now = time.time()
        if self.last_ts is None:
            self.fast = price
            self.slow = price
            self.last_ts = now
            return
        dt = now - self.last_ts
        self.last_ts = now
        alpha_fast = 1.0 - math.exp(-dt / self.FAST_TAU)
        alpha_slow = 1.0 - math.exp(-dt / self.SLOW_TAU)
        self.fast = alpha_fast * price + (1.0 - alpha_fast) * self.fast
        self.slow = alpha_slow * price + (1.0 - alpha_slow) * self.slow

    @property
    def signal(self) -> float | None:
        """fast - slow, normalised as % of slow. Positive = upward momentum."""
        if self.fast is None or self.slow is None or self.slow == 0:
            return None
        return (self.fast - self.slow) / self.slow * 100


@dataclass
class PriceFeed:
    """Thread-safe price feed with history."""
    current: dict = field(default_factory=dict)         # symbol -> current price
    history: dict = field(default_factory=dict)          # symbol -> deque of PriceTick
    window_start_prices: dict = field(default_factory=dict)  # symbol -> price at window start
    ewma: dict = field(default_factory=dict)             # symbol -> EWMAState
    _running: bool = False

    def __post_init__(self):
        for sym in SYMBOLS:
            self.history[sym] = deque(maxlen=1000)
            self.ewma[sym] = EWMAState()

    def update(self, symbol: str, price: float):
        now = time.time()
        self.current[symbol] = price
        self.history[symbol].append(PriceTick(symbol, price, now))
        if symbol in self.ewma:
            self.ewma[symbol].update(price)

    def get_ewma_signal(self, symbol: str) -> float | None:
        """EWMA momentum signal: (fast - slow) / slow * 100.
        Positive = price trending up. Negative = trending down.
        Returns None until warmed up (first ~30s after startup).
        """
        state = self.ewma.get(symbol)
        return state.signal if state else None

    def get_price(self, symbol: str) -> float | None:
        return self.current.get(symbol)

    def set_window_start(self, symbol: str, price: float):
        """Set the reference price for the start of a 15-min window."""
        self.window_start_prices[symbol] = price

    def get_window_start(self, symbol: str) -> float | None:
        return self.window_start_prices.get(symbol)

    def get_implied_probability(self, symbol: str, seconds_remaining: int = 450) -> float | None:
        """
        Calculate implied probability that price will be UP at end of window.
        TIME-WEIGHTED: same price move means MORE when less time remains.

        With 900s left, a 0.1% move barely matters (price can reverse easily).
        With 60s left, a 0.1% move is very predictive.
        """
        start = self.get_window_start(symbol)
        current = self.get_price(symbol)
        if start is None or current is None or start == 0:
            return None

        import math

        # Percentage move from window start
        pct_move = (current - start) / start * 100

        # Time-weighted steepness:
        # - With 900s left: steepness = 5 (conservative — 0.1% → ~62%)
        # - With 300s left: steepness = 10 (moderate — 0.1% → ~73%)
        # - With 120s left: steepness = 18 (aggressive — 0.1% → ~86%)
        # - With 30s left:  steepness = 30 (very confident — 0.1% → ~95%)
        time_fraction = max(0.01, seconds_remaining / 900.0)  # 1.0 at start, ~0 at end
        steepness = 5.0 + 25.0 * (1.0 - time_fraction)

        prob_up = 1 / (1 + math.exp(-steepness * pct_move))
        return prob_up

    def get_momentum(self, symbol: str, lookback_seconds: int = 30) -> float | None:
        """Get price momentum over last N seconds (% change)."""
        history = self.history.get(symbol)
        if not history or len(history) < 2:
            return None

        now = time.time()
        cutoff = now - lookback_seconds

        old_price = None
        for tick in history:
            if tick.timestamp >= cutoff:
                old_price = tick.price
                break

        if old_price is None or old_price == 0:
            return None

        current = self.current.get(symbol, 0)
        return (current - old_price) / old_price * 100


# Global feed instance
feed = PriceFeed()


async def connect_binance():
    """Connect to Binance WebSocket and stream real-time prices."""
    streams = "/".join(f"{s}@aggTrade" for s in SYMBOLS.values())
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    print("[binance] Connecting to Binance WebSocket...")

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ssl=ssl_ctx) as ws:
                _was_disconnected = not feed._running
                print("[binance] Connected — streaming BTC/ETH/SOL prices")
                feed._running = True
                if _was_disconnected:
                    try:
                        import telegram_alerts as tg
                        tg.send_message("✅ Binance WS reconnected")
                    except Exception:
                        pass

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        stream_data = data.get("data", {})
                        symbol_raw = stream_data.get("s", "")  # e.g., "BTCUSDT"
                        price = float(stream_data.get("p", 0))

                        # Map back to our symbol names
                        for sym, binance_sym in SYMBOLS.items():
                            if symbol_raw.lower() == binance_sym:
                                feed.update(sym, price)
                                break

                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue

        except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"[binance] Connection lost: {e}. Reconnecting in 5s...")
            feed._running = False
            try:
                import telegram_alerts as tg
                tg.send_message(f"⚠️ Binance WS disconnected — reconnecting in 5s\n{str(e)[:80]}")
            except Exception:
                pass
            await asyncio.sleep(5)


def get_initial_prices() -> dict:
    """Fetch current prices via REST (for startup before WebSocket connects)."""
    import requests
    prices = {}
    for sym, binance_sym in SYMBOLS.items():
        try:
            resp = requests.get(
                BINANCE_REST,
                params={"symbol": binance_sym.upper()},
                timeout=5,
            )
            data = resp.json()
            prices[sym] = float(data["price"])
            feed.update(sym, prices[sym])
        except Exception as e:
            print(f"[binance] Failed to fetch {sym} price: {e}")
    return prices


if __name__ == "__main__":
    # Quick test: print prices for 10 seconds
    import requests

    print("Fetching initial prices via REST...")
    prices = get_initial_prices()
    for sym, price in prices.items():
        print(f"  {sym}: ${price:,.2f}")

    print("\nStreaming live prices (Ctrl+C to stop)...")

    async def test():
        task = asyncio.create_task(connect_binance())
        for _ in range(20):
            await asyncio.sleep(1)
            for sym in SYMBOLS:
                p = feed.get_price(sym)
                if p:
                    print(f"  {sym}: ${p:,.2f}")
        task.cancel()

    asyncio.run(test())

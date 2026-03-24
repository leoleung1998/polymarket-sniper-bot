"""
Polymarket live price feed — polls Gamma API every N seconds for current
up/down odds on all active 15-min crypto markets.

One bulk call fetches all 3 coins at once → ~12 req/min, well within rate limits.
"""

import asyncio
import time
from dataclasses import dataclass, field

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
POLL_INTERVAL = 5.0   # seconds between polls
STALE_THRESHOLD = 30  # seconds before price is considered stale


@dataclass
class PolyMarketPrice:
    token_id: str
    coin: str
    side: str       # "up" or "down"
    price: float
    updated_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.updated_at) > STALE_THRESHOLD


@dataclass
class PolyPriceFeed:
    """Live Polymarket up/down prices for all active 15-min crypto markets."""
    _prices: dict = field(default_factory=dict)   # token_id -> PolyMarketPrice
    _last_poll: float = 0.0
    _poll_count: int = 0
    _error_count: int = 0

    def update(self, token_id: str, coin: str, side: str, price: float):
        self._prices[token_id] = PolyMarketPrice(token_id, coin, side, price)

    def get_price(self, token_id: str) -> float | None:
        entry = self._prices.get(token_id)
        if entry is None or entry.is_stale:
            return None
        return entry.price

    def get_market_prices(self, coin: str) -> tuple[float | None, float | None]:
        """Return (up_price, down_price) for a coin using the most recently updated entry."""
        up = max(
            (e for e in self._prices.values() if e.coin == coin and e.side == "up" and not e.is_stale),
            key=lambda e: e.updated_at, default=None,
        )
        down = max(
            (e for e in self._prices.values() if e.coin == coin and e.side == "down" and not e.is_stale),
            key=lambda e: e.updated_at, default=None,
        )
        return (up.price if up else None), (down.price if down else None)

    def poly_implied_prob(self, coin: str, direction: str) -> float | None:
        """
        Return Polymarket's current implied probability for a direction.
        This is what the market thinks — compare vs Binance signal to find edge.
        """
        up_price, down_price = self.get_market_prices(coin)
        if direction == "up":
            return up_price
        elif direction == "down":
            return down_price
        return None

    def gap(self, coin: str, direction: str, binance_confidence: float) -> float | None:
        """
        Gap between Polymarket implied prob and Binance-derived confidence.
        Positive = Polymarket hasn't priced in what Binance shows (edge exists).
        Negative = Polymarket already ahead of Binance (no edge).

        binance_confidence: 0.0–1.0 (e.g. 0.90 = 90% confident direction is correct)
        """
        poly_prob = self.poly_implied_prob(coin, direction)
        if poly_prob is None:
            return None
        return binance_confidence - poly_prob

    @property
    def stats(self) -> str:
        return f"polls={self._poll_count} errors={self._error_count} tokens={len(self._prices)}"


# Global feed instance
poly_feed = PolyPriceFeed()


async def poll_poly_prices(coins: list[str] = None, interval: float = POLL_INTERVAL):
    """
    Background task: polls Gamma API for all active 15-min markets.
    Uses discover_market() per coin (3 calls per cycle) — same path that's known to work.
    """
    from crypto_markets import COIN_SLUGS, discover_market

    if coins is None:
        coins = list(COIN_SLUGS.keys())

    def _clob_mid(token_id: str) -> float | None:
        """Fetch real-time mid price from CLOB order book (best bid + best ask / 2)."""
        try:
            resp = requests.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
                timeout=4,
            )
            resp.raise_for_status()
            book = resp.json()
            bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
            asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            if best_bid and best_ask:
                return round((best_bid + best_ask) / 2, 4)
            return best_bid or best_ask
        except Exception:
            return None

    while True:
        for coin in coins:
            try:
                market = discover_market(coin)
                if market:
                    # Use live CLOB mid price — Gamma outcomePrices lags real-time
                    up_mid   = _clob_mid(market.up_token_id)   or market.up_price
                    down_mid = _clob_mid(market.down_token_id) or market.down_price
                    poly_feed.update(market.up_token_id,   coin, "up",   up_mid)
                    poly_feed.update(market.down_token_id, coin, "down", down_mid)
            except Exception as e:
                poly_feed._error_count += 1
                poly_feed._last_error = str(e)[:80]

        poly_feed._poll_count += 1
        poly_feed._last_poll = time.time()
        await asyncio.sleep(interval)

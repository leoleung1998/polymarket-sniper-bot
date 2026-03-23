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
        """Return (up_price, down_price) for a coin. None if stale/unknown."""
        up = next((e for e in self._prices.values() if e.coin == coin and e.side == "up"), None)
        down = next((e for e in self._prices.values() if e.coin == coin and e.side == "down"), None)
        up_price = up.price if up and not up.is_stale else None
        down_price = down.price if down and not down.is_stale else None
        return up_price, down_price

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

    while True:
        for coin in coins:
            try:
                market = discover_market(coin)
                if market:
                    poly_feed.update(market.up_token_id, coin, "up", market.up_price)
                    poly_feed.update(market.down_token_id, coin, "down", market.down_price)
            except Exception:
                poly_feed._error_count += 1

        poly_feed._poll_count += 1
        poly_feed._last_poll = time.time()
        await asyncio.sleep(interval)

"""
L2 order book tracker for Polymarket CLOB tokens.

Maintains a full bid/ask book per token and exposes:
  - obi(token_id)      — Order Book Imbalance [-1, +1], near-market levels only
  - spread(token_id)   — best ask - best bid
  - mid(token_id)      — (best bid + best ask) / 2
  - best_bid(token_id) — highest bid price
  - best_ask(token_id) — lowest ask price
  - top_n(token_id, n) — top N bid/ask levels

Near-market filter: only include levels within OBI_DEPTH of mid.
This prevents deep passive orders (at 0.01-0.03 / 0.97-0.99) from
dominating OBI. Those giant passive blocks are market makers hedging
and tell us nothing about short-term direction.
"""

import threading
from dataclasses import dataclass, field

OBI_DEPTH = 0.15     # only include levels within this range of mid
TOP_N_DEFAULT = 5


@dataclass
class _BookSide:
    _levels: dict = field(default_factory=dict)  # price -> size

    def update(self, price: float, size: float):
        if size == 0.0:
            self._levels.pop(price, None)
        else:
            self._levels[price] = size

    def clear(self):
        self._levels.clear()

    def best(self, is_bid: bool) -> float | None:
        if not self._levels:
            return None
        return max(self._levels) if is_bid else min(self._levels)

    def top_n(self, is_bid: bool, n: int) -> list[tuple[float, float]]:
        if not self._levels:
            return []
        return sorted(self._levels.items(), key=lambda x: x[0], reverse=is_bid)[:n]

    def near_volume(self, mid: float, depth: float) -> float:
        return sum(sz for px, sz in self._levels.items() if abs(px - mid) <= depth)


class TokenBook:
    """Full L2 order book for a single Polymarket token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids = _BookSide()
        self.asks = _BookSide()
        self._lock = threading.Lock()
        self.snapshot_count = 0
        self.update_count = 0

    def snapshot(self, raw_bids: list[dict], raw_asks: list[dict]):
        """Replace entire book from a WS `book` snapshot event."""
        with self._lock:
            self.bids.clear()
            self.asks.clear()
            for lvl in raw_bids:
                try:
                    self.bids.update(float(lvl["price"]), float(lvl["size"]))
                except (KeyError, ValueError, TypeError):
                    pass
            for lvl in raw_asks:
                try:
                    self.asks.update(float(lvl["price"]), float(lvl["size"]))
                except (KeyError, ValueError, TypeError):
                    pass
            self.snapshot_count += 1

    def update_level(self, side: str, price: float, size: float):
        """Apply a single delta from a `price_change` event.
        side: 'bid' or 'ask'   (caller maps BUY→bid, SELL→ask)
        size: 0 means remove the level
        """
        with self._lock:
            book = self.bids if side == "bid" else self.asks
            book.update(price, size)
            self.update_count += 1

    # ── Read accessors (all lock-free for speed — worst case stale by one tick) ──

    def best_bid(self) -> float | None:
        return self.bids.best(True)

    def best_ask(self) -> float | None:
        return self.asks.best(False)

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def spread(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return round(ba - bb, 4)

    def obi(self, depth: float = OBI_DEPTH) -> float | None:
        """
        Order Book Imbalance using near-market levels only.

        OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        Range: -1 (pure sell pressure) to +1 (pure buy pressure)
        Positive OBI → more size at the bid → expect price to rise.
        """
        m = self.mid()
        if m is None:
            return None
        bid_vol = self.bids.near_volume(m, depth)
        ask_vol = self.asks.near_volume(m, depth)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return round((bid_vol - ask_vol) / total, 4)

    def top_n(self, n: int = TOP_N_DEFAULT) -> dict:
        return {
            "bids": self.bids.top_n(True, n),
            "asks": self.asks.top_n(False, n),
        }

    def is_ready(self) -> bool:
        """True once we have at least one snapshot."""
        return self.snapshot_count > 0


class OrderBookManager:
    """Manages L2 books for all tracked Polymarket tokens."""

    def __init__(self):
        self._books: dict[str, TokenBook] = {}
        self._lock = threading.Lock()

    def _get(self, token_id: str) -> TokenBook | None:
        return self._books.get(token_id)

    def _get_or_create(self, token_id: str) -> TokenBook:
        with self._lock:
            if token_id not in self._books:
                self._books[token_id] = TokenBook(token_id)
            return self._books[token_id]

    # ── Write ops (called from WS thread) ─────────────────────────────────────

    def snapshot(self, token_id: str, raw_bids: list[dict], raw_asks: list[dict]):
        self._get_or_create(token_id).snapshot(raw_bids, raw_asks)

    def update_level(self, token_id: str, side: str, price: float, size: float):
        self._get_or_create(token_id).update_level(side, price, size)

    def clear(self, token_id: str):
        with self._lock:
            self._books.pop(token_id, None)

    # ── Read ops (called from strategy thread) ────────────────────────────────

    def obi(self, token_id: str) -> float | None:
        book = self._get(token_id)
        return book.obi() if book else None

    def mid(self, token_id: str) -> float | None:
        book = self._get(token_id)
        return book.mid() if book else None

    def spread(self, token_id: str) -> float | None:
        book = self._get(token_id)
        return book.spread() if book else None

    def best_bid(self, token_id: str) -> float | None:
        book = self._get(token_id)
        return book.best_bid() if book else None

    def best_ask(self, token_id: str) -> float | None:
        book = self._get(token_id)
        return book.best_ask() if book else None

    def top_n(self, token_id: str, n: int = TOP_N_DEFAULT) -> dict | None:
        book = self._get(token_id)
        return book.top_n(n) if book else None

    def is_ready(self, token_id: str) -> bool:
        book = self._get(token_id)
        return book.is_ready() if book else False

    def stats(self, token_id: str) -> str:
        book = self._get(token_id)
        if not book:
            return "no book"
        m = book.mid()
        o = book.obi()
        s = book.spread()
        return (
            f"mid={m:.3f} obi={o:+.3f} spread={s:.4f} "
            f"snaps={book.snapshot_count} deltas={book.update_count}"
            if m is not None
            else f"snaps={book.snapshot_count} deltas={book.update_count} (no mid yet)"
        )


# Global instance — imported by poly_ws.py and micro_bot.py
order_book = OrderBookManager()

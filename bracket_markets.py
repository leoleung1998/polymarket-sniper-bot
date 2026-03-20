"""
Polymarket daily bracket market discovery and management.
Discovers crypto price bracket events (BTC/ETH "above $X on date")
and weather temperature bracket events ("highest temp in city on date").
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"

# Patterns for discovering events
CRYPTO_EVENT_PATTERNS = {
    "BTC": ["bitcoin-above-on-", "bitcoin above"],
    "ETH": ["ethereum-above-on-", "ethereum above"],
}

WEATHER_SLUG_PREFIX = "highest-temperature-in-"


@dataclass
class BracketMarket:
    """A single bracket within a daily event."""
    coin: str               # "BTC", "ETH", or city name like "Dallas"
    market_type: str        # "crypto" or "weather"
    question: str
    threshold: float        # 72000.0 for crypto, 75.0 for weather
    threshold_high: float | None  # For range brackets: 77.0 in "76-77°F"; None for "above $X"
    threshold_unit: str     # "USD", "°F", "°C"
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    resolution_time: datetime
    slug: str
    accepting_orders: bool
    bracket_type: str       # "above", "below", "range", "at_or_below", "at_or_above"

    @property
    def is_active(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.accepting_orders and self.resolution_time > now

    @property
    def hours_remaining(self) -> float:
        now = datetime.now(timezone.utc)
        delta = self.resolution_time - now
        return max(0, delta.total_seconds() / 3600)


@dataclass
class BracketEvent:
    """A daily bracket event containing multiple threshold markets."""
    slug: str
    title: str
    market_type: str        # "crypto" or "weather"
    coin: str               # "BTC", "ETH", or city name
    resolution_date: date
    resolution_time: datetime
    markets: list[BracketMarket] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return any(m.is_active for m in self.markets)

    @property
    def hours_remaining(self) -> float:
        now = datetime.now(timezone.utc)
        delta = self.resolution_time - now
        return max(0, delta.total_seconds() / 3600)


# ── Threshold Extraction ─────────────────────────────────────────────

def extract_crypto_threshold(question: str) -> tuple[float, str] | None:
    """Extract dollar threshold from crypto bracket question.

    Examples:
        "Will the price of Bitcoin be above $72,000 on March 14?" → (72000.0, "above")
        "Will the price of Bitcoin be above $68,000 on March 14?" → (68000.0, "above")
    """
    # Match $XX,XXX or $XX000 patterns
    m = re.search(r'above\s+\$([0-9,]+)', question, re.IGNORECASE)
    if m:
        price = float(m.group(1).replace(",", ""))
        return (price, "above")

    m = re.search(r'below\s+\$([0-9,]+)', question, re.IGNORECASE)
    if m:
        price = float(m.group(1).replace(",", ""))
        return (price, "below")

    return None


def extract_weather_threshold(question: str) -> tuple[float, float | None, str, str] | None:
    """Extract temperature threshold from weather bracket question.

    Returns: (threshold_low, threshold_high, unit, bracket_type)

    Examples:
        "Will the highest temperature in Dallas be 75°F or below on March 14?"
            → (75.0, None, "°F", "at_or_below")
        "Will the highest temperature in Dallas be between 76-77°F on March 14?"
            → (76.0, 77.0, "°F", "range")
        "Will the highest temperature in Dallas be 90°F or higher on March 14?"
            → (90.0, None, "°F", "at_or_above")
        "Will the highest temperature in Paris be 7°C or below on March 13?"
            → (7.0, None, "°C", "at_or_below")
    """
    # Detect unit
    unit = "°F" if "°F" in question else "°C" if "°C" in question else None
    if not unit:
        return None

    # Range bracket: "between XX-YY°F"
    m = re.search(r'between\s+(-?\d+)\s*[-–]\s*(-?\d+)\s*°', question)
    if m:
        return (float(m.group(1)), float(m.group(2)), unit, "range")

    # Single degree: "be X°F on" (not "or below" / "or higher")
    m = re.search(r'be\s+(-?\d+)\s*°[FC]\s+on', question)
    if m:
        val = float(m.group(1))
        return (val, val, unit, "range")  # Treat single degree as range of 1

    # At or below: "XX°F or below"
    m = re.search(r'(-?\d+)\s*°[FC]\s+or\s+below', question)
    if m:
        return (float(m.group(1)), None, unit, "at_or_below")

    # At or above: "XX°F or higher"
    m = re.search(r'(-?\d+)\s*°[FC]\s+or\s+higher', question)
    if m:
        return (float(m.group(1)), None, unit, "at_or_above")

    return None


def extract_city_from_slug(slug: str) -> str:
    """Extract city name from weather event slug.

    Example: "highest-temperature-in-dallas-on-march-14-2026" → "Dallas"
    """
    m = re.search(r'highest-temperature-in-(.+?)-on-', slug)
    if m:
        city_slug = m.group(1)
        # Handle multi-word cities: "new-york-city" → "New York City"
        # Common slug → display name mappings
        city_map = {
            "nyc": "NYC",
            "buenos-aires": "Buenos Aires",
            "sao-paulo": "Sao Paulo",
            "tel-aviv": "Tel Aviv",
            "new-york-city": "NYC",
        }
        return city_map.get(city_slug, city_slug.replace("-", " ").title())
    return slug


# ── Market Parsing ────────────────────────────────────────────────────

def parse_bracket_market(market: dict, coin: str, market_type: str, resolution_time: datetime) -> BracketMarket | None:
    """Parse a Gamma API market response into a BracketMarket."""
    try:
        question = market.get("question", "")
        raw_outcomes = market.get("outcomes", "[]")
        raw_prices = market.get("outcomePrices", "[]")
        raw_token_ids = market.get("clobTokenIds", "[]")

        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids

        if len(outcomes) < 2 or len(token_ids) < 2:
            return None

        # Map Yes/No outcomes
        yes_idx = no_idx = None
        for i, o in enumerate(outcomes):
            if o.lower() == "yes":
                yes_idx = i
            elif o.lower() == "no":
                no_idx = i

        if yes_idx is None or no_idx is None:
            return None

        # Extract threshold based on market type
        if market_type == "crypto":
            parsed = extract_crypto_threshold(question)
            if not parsed:
                return None
            threshold, bracket_type = parsed
            threshold_high = None
            threshold_unit = "USD"
        else:
            parsed = extract_weather_threshold(question)
            if not parsed:
                return None
            threshold, threshold_high, threshold_unit, bracket_type = parsed

        return BracketMarket(
            coin=coin,
            market_type=market_type,
            question=question,
            threshold=threshold,
            threshold_high=threshold_high,
            threshold_unit=threshold_unit,
            yes_token_id=token_ids[yes_idx],
            no_token_id=token_ids[no_idx],
            yes_price=float(prices[yes_idx]) if yes_idx < len(prices) else 0.5,
            no_price=float(prices[no_idx]) if no_idx < len(prices) else 0.5,
            resolution_time=resolution_time,
            slug=market.get("slug", ""),
            accepting_orders=market.get("acceptingOrders", True),
            bracket_type=bracket_type,
        )

    except Exception as e:
        print(f"[brackets] Failed to parse market: {e}")
        return None


# ── Event Discovery ───────────────────────────────────────────────────

# Slug patterns for client-side filtering (Gamma API slug_contains is broken)
CRYPTO_SLUG_PATTERNS = {
    "BTC": "bitcoin-above",
    "ETH": "ethereum-above",
}


def _fetch_all_active_events(max_pages: int = 10) -> list[dict]:
    """Bulk-fetch all active events from Gamma API (paginated).

    The Gamma API's slug_contains/tag filters are unreliable, so we fetch
    everything and filter client-side.
    """
    all_events = []
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{GAMMA_URL}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                    "offset": page * 200,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
        except (requests.RequestException, ValueError) as e:
            print(f"[brackets] Failed to fetch events page {page}: {e}")
            break
    return all_events


def _parse_event(raw_event: dict, coin: str, market_type: str) -> BracketEvent | None:
    """Parse a raw Gamma API event into a BracketEvent."""
    slug = raw_event.get("slug", "")
    title = raw_event.get("title", "")
    end_date_str = raw_event.get("endDate", "")

    if not end_date_str:
        return None

    resolution_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    resolution_date = resolution_time.date()

    # Parse child markets
    bracket_markets = []
    for raw_market in raw_event.get("markets", []):
        bm = parse_bracket_market(raw_market, coin, market_type, resolution_time)
        if bm:
            bracket_markets.append(bm)

    if not bracket_markets:
        return None

    # Sort by threshold ascending
    bracket_markets.sort(key=lambda m: m.threshold)

    return BracketEvent(
        slug=slug,
        title=title,
        market_type=market_type,
        coin=coin,
        resolution_date=resolution_date,
        resolution_time=resolution_time,
        markets=bracket_markets,
    )


def discover_crypto_events(coins: list[str] | None = None, all_events: list[dict] | None = None) -> list[BracketEvent]:
    """Discover active crypto daily bracket events (BTC/ETH above $X on date)."""
    if coins is None:
        coins = ["BTC", "ETH"]

    if all_events is None:
        all_events = _fetch_all_active_events()

    events = []
    for coin in coins:
        slug_pattern = CRYPTO_SLUG_PATTERNS.get(coin, "")
        for raw_event in all_events:
            slug = raw_event.get("slug", "")
            if slug_pattern not in slug:
                continue

            parsed = _parse_event(raw_event, coin, "crypto")
            if parsed and parsed.is_active:
                events.append(parsed)

    return events


def discover_weather_events(all_events: list[dict] | None = None) -> list[BracketEvent]:
    """Discover active weather temperature bracket events."""
    if all_events is None:
        all_events = _fetch_all_active_events()

    events = []
    for raw_event in all_events:
        slug = raw_event.get("slug", "")
        if WEATHER_SLUG_PREFIX not in slug:
            continue

        city = extract_city_from_slug(slug)
        parsed = _parse_event(raw_event, city, "weather")
        if parsed and parsed.is_active:
            events.append(parsed)

    return events


def discover_all_events(coins: list[str] | None = None) -> list[BracketEvent]:
    """Discover all active bracket events (crypto + weather).

    Fetches all events once and filters client-side for efficiency.
    """
    all_raw = _fetch_all_active_events()
    crypto = discover_crypto_events(coins, all_events=all_raw)
    weather = discover_weather_events(all_events=all_raw)
    return crypto + weather


def refresh_event_prices(event: BracketEvent) -> bool:
    """Refresh Polymarket prices for all markets in an event."""
    try:
        resp = requests.get(
            f"{GAMMA_URL}/events",
            params={"slug": event.slug},
            timeout=10,
        )
        resp.raise_for_status()
        raw_events = resp.json()

        if not raw_events:
            return False

        raw_event = raw_events[0]
        raw_markets = raw_event.get("markets", [])

        # Build slug → prices lookup
        price_map = {}
        for rm in raw_markets:
            slug = rm.get("slug", "")
            prices = rm.get("outcomePrices", "[]")
            outcomes = rm.get("outcomes", "[]")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes

            yes_price = no_price = 0.5
            for i, o in enumerate(outcomes):
                if o.lower() == "yes" and i < len(prices):
                    yes_price = float(prices[i])
                elif o.lower() == "no" and i < len(prices):
                    no_price = float(prices[i])

            price_map[slug] = (yes_price, no_price)

        # Update our market objects
        for market in event.markets:
            if market.slug in price_map:
                market.yes_price, market.no_price = price_map[market.slug]

        return True

    except Exception as e:
        print(f"[brackets] Failed to refresh {event.slug}: {e}")
        return False


# ── Standalone test ───────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("Discovering daily bracket events...")
    print("=" * 70)

    events = discover_all_events(coins=["BTC", "ETH"])

    for event in events:
        icon = "₿" if event.market_type == "crypto" else "🌡️"
        print(f"\n{icon} {event.title}")
        print(f"  slug: {event.slug}")
        print(f"  resolves: {event.resolution_time.isoformat()} ({event.hours_remaining:.1f}h remaining)")
        print(f"  brackets: {len(event.markets)}")

        for m in event.markets:
            if m.market_type == "weather":
                if m.bracket_type == "range":
                    label = f"{m.threshold:.0f}-{m.threshold_high:.0f}{m.threshold_unit}"
                elif m.bracket_type == "at_or_below":
                    label = f"≤{m.threshold:.0f}{m.threshold_unit}"
                elif m.bracket_type == "at_or_above":
                    label = f"≥{m.threshold:.0f}{m.threshold_unit}"
                else:
                    label = f"{m.threshold:.0f}{m.threshold_unit}"
            else:
                label = f">${m.threshold:,.0f}"

            print(f"    {label:>12s}  YES=${m.yes_price:.4f}  NO=${m.no_price:.4f}")

    print(f"\n{'=' * 70}")
    print(f"Total: {len(events)} events")
    crypto_count = sum(1 for e in events if e.market_type == "crypto")
    weather_count = sum(1 for e in events if e.market_type == "weather")
    print(f"  Crypto: {crypto_count} events")
    print(f"  Weather: {weather_count} events")

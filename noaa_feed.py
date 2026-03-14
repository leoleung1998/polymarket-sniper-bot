"""
Weather forecast feed for Polymarket temperature bracket trading.

US cities: NOAA Weather API (api.weather.gov) — free, no key needed
International cities: Open-Meteo API (api.open-meteo.com) — free, no key needed

Both provide next-day high temperature forecasts with ~2°F / ~1°C accuracy.
"""

import time
from dataclasses import dataclass

import requests

NOAA_BASE = "https://api.weather.gov"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "PolymarketWeatherBot/1.0 (contact: bot@example.com)"

# Cache forecasts for 30 minutes (they don't change often)
CACHE_TTL = 1800


@dataclass
class CityForecast:
    city: str
    date: str               # "2026-03-14"
    high_temp: float         # Forecast high temperature
    low_temp: float | None   # Forecast low temperature (if available)
    unit: str                # "°F" or "°C"
    source: str              # "noaa" or "open-meteo"
    fetched_at: float        # time.time() when fetched

    @property
    def high_temp_f(self) -> float:
        """High temp in Fahrenheit."""
        if self.unit == "°F":
            return self.high_temp
        return self.high_temp * 9 / 5 + 32

    @property
    def high_temp_c(self) -> float:
        """High temp in Celsius."""
        if self.unit == "°C":
            return self.high_temp
        return (self.high_temp - 32) * 5 / 9


# ── City Coordinates ──────────────────────────────────────────────────
# lat, lon, country (US = use NOAA, else = Open-Meteo)

CITY_COORDS = {
    # US cities (NOAA)
    "Dallas": (32.7767, -96.7970, "US"),
    "Atlanta": (33.7490, -84.3880, "US"),
    "NYC": (40.7128, -74.0060, "US"),
    "Chicago": (41.8781, -87.6298, "US"),
    "Miami": (25.7617, -80.1918, "US"),
    "Seattle": (47.6062, -122.3321, "US"),

    # International cities (Open-Meteo)
    "Paris": (48.8566, 2.3522, "INT"),
    "London": (51.5074, -0.1278, "INT"),
    "Tokyo": (35.6762, 139.6503, "INT"),
    "Seoul": (37.5665, 126.9780, "INT"),
    "Toronto": (43.6532, -79.3832, "INT"),
    "Buenos Aires": (-34.6037, -58.3816, "INT"),
    "Sao Paulo": (-23.5505, -46.6333, "INT"),
    "Tel Aviv": (32.0853, 34.7818, "INT"),
    "Ankara": (39.9334, 32.8597, "INT"),
    "Munich": (48.1351, 11.5820, "INT"),
    "Wellington": (-41.2865, 174.7762, "INT"),
    "Shanghai": (31.2304, 121.4737, "INT"),
    "Lucknow": (26.8467, 80.9462, "INT"),
    "Singapore": (1.3521, 103.8198, "INT"),
}

# Forecast cache: key = "city|date" → CityForecast
_forecast_cache: dict[str, CityForecast] = {}

# NOAA grid cache: key = "lat,lon" → forecast_url
_noaa_grid_cache: dict[str, str] = {}


# ── NOAA (US cities) ─────────────────────────────────────────────────

def _get_noaa_forecast_url(lat: float, lon: float) -> str | None:
    """Get NOAA forecast URL from coordinates (cached)."""
    key = f"{lat},{lon}"
    if key in _noaa_grid_cache:
        return _noaa_grid_cache[key]

    try:
        resp = requests.get(
            f"{NOAA_BASE}/points/{lat},{lon}",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        url = resp.json().get("properties", {}).get("forecast", "")
        if url:
            _noaa_grid_cache[key] = url
        return url or None
    except Exception as e:
        print(f"[weather] NOAA grid lookup failed: {e}")
        return None


def _fetch_noaa_forecast(city: str, lat: float, lon: float) -> list[CityForecast]:
    """Fetch daily forecasts from NOAA for a US city."""
    forecast_url = _get_noaa_forecast_url(lat, lon)
    if not forecast_url:
        return []

    try:
        resp = requests.get(
            forecast_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        periods = resp.json().get("properties", {}).get("periods", [])

        forecasts = []
        now = time.time()

        # NOAA periods alternate: "Saturday" (day), "Saturday Night" (night), etc.
        # We want daytime periods for high temps
        for p in periods:
            if not p.get("isDaytime", False):
                continue

            # Parse date from startTime: "2026-03-14T06:00:00-05:00"
            start_time = p.get("startTime", "")
            forecast_date = start_time[:10] if start_time else ""

            temp = p.get("temperature")
            unit_str = p.get("temperatureUnit", "F")
            unit = "°F" if unit_str == "F" else "°C"

            if temp is not None and forecast_date:
                forecasts.append(CityForecast(
                    city=city,
                    date=forecast_date,
                    high_temp=float(temp),
                    low_temp=None,
                    unit=unit,
                    source="noaa",
                    fetched_at=now,
                ))

        return forecasts

    except Exception as e:
        print(f"[weather] NOAA forecast failed for {city}: {e}")
        return []


# ── Open-Meteo (International cities) ────────────────────────────────

def _fetch_open_meteo_forecast(city: str, lat: float, lon: float) -> list[CityForecast]:
    """Fetch daily forecasts from Open-Meteo for an international city."""
    try:
        resp = requests.get(
            OPEN_METEO_BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])

        forecasts = []
        now = time.time()

        for i, d in enumerate(dates):
            if i < len(highs) and highs[i] is not None:
                forecasts.append(CityForecast(
                    city=city,
                    date=d,
                    high_temp=float(highs[i]),
                    low_temp=float(lows[i]) if i < len(lows) and lows[i] is not None else None,
                    unit="°C",
                    source="open-meteo",
                    fetched_at=now,
                ))

        return forecasts

    except Exception as e:
        print(f"[weather] Open-Meteo forecast failed for {city}: {e}")
        return []


# ── Public API ────────────────────────────────────────────────────────

def get_forecast(city: str, target_date: str) -> CityForecast | None:
    """Get forecast for a city on a specific date.

    Args:
        city: City name (must be in CITY_COORDS)
        target_date: Date string "YYYY-MM-DD"

    Returns:
        CityForecast or None if city unknown or API fails
    """
    # Check cache
    cache_key = f"{city}|{target_date}"
    cached = _forecast_cache.get(cache_key)
    if cached and (time.time() - cached.fetched_at) < CACHE_TTL:
        return cached

    coords = CITY_COORDS.get(city)
    if not coords:
        # Try case-insensitive lookup
        for name, c in CITY_COORDS.items():
            if name.lower() == city.lower():
                coords = c
                city = name
                break

    if not coords:
        print(f"[weather] Unknown city: {city}")
        return None

    lat, lon, country = coords

    # Fetch from appropriate API
    if country == "US":
        forecasts = _fetch_noaa_forecast(city, lat, lon)
    else:
        forecasts = _fetch_open_meteo_forecast(city, lat, lon)

    # Cache all dates and return the requested one
    result = None
    for f in forecasts:
        key = f"{f.city}|{f.date}"
        _forecast_cache[key] = f
        if f.date == target_date:
            result = f

    return result


def get_all_forecasts(target_date: str) -> dict[str, CityForecast]:
    """Get forecasts for all known cities on a target date."""
    results = {}
    for city in CITY_COORDS:
        forecast = get_forecast(city, target_date)
        if forecast:
            results[city] = forecast
    return results


# ── Standalone test ───────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import date, timedelta

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    print(f"Fetching forecasts for {tomorrow}...")
    print("=" * 60)

    forecasts = get_all_forecasts(tomorrow)

    # Group by source
    noaa = {k: v for k, v in forecasts.items() if v.source == "noaa"}
    meteo = {k: v for k, v in forecasts.items() if v.source == "open-meteo"}

    print(f"\n🇺🇸 NOAA (US cities):")
    for city, f in sorted(noaa.items()):
        print(f"  {city:15s}  High: {f.high_temp:.0f}{f.unit}")

    print(f"\n🌍 Open-Meteo (International):")
    for city, f in sorted(meteo.items()):
        f_temp = f"({f.high_temp_f:.0f}°F)" if f.unit == "°C" else ""
        print(f"  {city:15s}  High: {f.high_temp:.1f}{f.unit} {f_temp}")

    print(f"\nTotal: {len(forecasts)} cities")

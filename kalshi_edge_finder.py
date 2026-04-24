"""
Kalshi Edge Finder v1.0
-----------------------
Pulls public market data from Kalshi's API and compares contract pricing
against external data sources (FRED, NOAA, CME FedWatch) to identify
potential mispricings.

No Kalshi account needed for market data. You DO need:
  - A free FRED API key: https://fred.stlouisfed.org/docs/api/api_key.html
  - (Optional) NOAA API token: https://www.ncdc.noaa.gov/cdo-web/token

Setup:
  pip install requests pandas tabulate python-dotenv

Usage:
  python kalshi_edge_finder.py

Author: Drew + Claude
License: Do whatever you want with it. Not financial advice. Obviously.
"""

import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Kalshi public API (no auth needed for market data)
KALSHI_BASE = "https://api.kalshi.com/trade-api/v2"

# FRED (Federal Reserve Economic Data) -- free key required
# Get yours: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY = os.environ.get("FRED_API_KEY", "YOUR_FRED_API_KEY_HERE")
FRED_BASE = "https://api.stlouisfed.org/fred"

# NOAA weather API (optional, for temperature markets)
NOAA_TOKEN = os.environ.get("NOAA_TOKEN", "")

# Rate limiting -- be polite to the APIs
REQUEST_DELAY = 0.3  # seconds between requests


@dataclass
class MarketData:
    """Represents a single Kalshi market contract."""
    ticker: str
    title: str
    subtitle: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0
    volume: int = 0
    open_interest: int = 0
    category: str = ""
    close_time: str = ""
    status: str = ""


@dataclass
class EdgeSignal:
    """A detected edge between market price and model estimate."""
    market_ticker: str
    market_title: str
    market_price: float          # current Yes price (0-1)
    model_estimate: float        # our estimated probability (0-1)
    edge: float                  # model_estimate - market_price
    edge_pct: float              # edge as percentage
    data_source: str             # where our estimate came from
    confidence: str              # LOW, MEDIUM, HIGH
    reasoning: str               # human-readable explanation
    potential_return: float = 0  # estimated return if correct


# ---------------------------------------------------------------------------
# Kalshi API Client (Public Endpoints Only)
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Minimal client for Kalshi's public REST API v2.
    No authentication needed for reading market data.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    def get_markets(
        self,
        limit: int = 100,
        cursor: str = None,
        event_ticker: str = None,
        series_ticker: str = None,
        status: str = "open",
        tickers: list = None
    ) -> dict:
        """
        Fetch markets from Kalshi.

        Args:
            limit: Max results per page (up to 200)
            cursor: Pagination cursor from previous response
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: 'open', 'closed', 'settled'
            tickers: List of specific market tickers

        Returns:
            Dict with 'markets' list and 'cursor' for pagination
        """
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if tickers:
            # Kalshi accepts comma-separated tickers
            params["tickers"] = ",".join(tickers)

        time.sleep(REQUEST_DELAY)
        resp = self.session.get(f"{KALSHI_BASE}/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker."""
        time.sleep(REQUEST_DELAY)
        resp = self.session.get(f"{KALSHI_BASE}/markets/{ticker}")
        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """
        Fetch the order book for a market.
        This shows real bid/ask depth, not just last traded price.
        """
        time.sleep(REQUEST_DELAY)
        params = {"depth": depth}
        resp = self.session.get(
            f"{KALSHI_BASE}/markets/{ticker}/orderbook",
            params=params
        )
        resp.raise_for_status()
        return resp.json()

    def get_events(self, limit: int = 50, status: str = "open", series_ticker: str = None) -> dict:
        """Fetch events (groups of related markets)."""
        params = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        time.sleep(REQUEST_DELAY)
        resp = self.session.get(f"{KALSHI_BASE}/events", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_series(self, series_ticker: str) -> dict:
        """Fetch a series (collection of events)."""
        time.sleep(REQUEST_DELAY)
        resp = self.session.get(f"{KALSHI_BASE}/series/{series_ticker}")
        resp.raise_for_status()
        return resp.json()

    def search_markets(self, query: str, limit: int = 50) -> list:
        """
        Search markets by keyword.
        Uses the events endpoint with text matching since Kalshi
        doesn't have a dedicated search endpoint in v2.
        """
        all_markets = []
        cursor = None
        pages = 0
        max_pages = 3  # cap to avoid hammering the API

        while pages < max_pages:
            data = self.get_markets(limit=200, cursor=cursor)
            markets = data.get("markets", [])
            if not markets:
                break

            query_lower = query.lower()
            for m in markets:
                title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
                if query_lower in title:
                    all_markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break
            pages += 1

        return all_markets[:limit]

    def parse_market(self, raw: dict) -> MarketData:
        """Convert raw API response to MarketData."""
        # v2 API uses dollar strings for prices
        yes_price = 0.0
        no_price = 0.0

        # Try different field names (API has evolved)
        for field_name in ["yes_ask", "last_price", "yes_price"]:
            val = raw.get(field_name)
            if val is not None:
                try:
                    yes_price = float(val)
                    break
                except (ValueError, TypeError):
                    pass

        # If yes_price looks like cents (>1), convert to dollars
        if yes_price > 1:
            yes_price = yes_price / 100.0

        no_price = 1.0 - yes_price if yes_price > 0 else 0.0

        return MarketData(
            ticker=raw.get("ticker", ""),
            title=raw.get("title", ""),
            subtitle=raw.get("subtitle", ""),
            yes_price=yes_price,
            no_price=no_price,
            volume=raw.get("volume", 0) or 0,
            open_interest=raw.get("open_interest", 0) or 0,
            category=raw.get("category", ""),
            close_time=raw.get("close_time", ""),
            status=raw.get("status", ""),
        )


# ---------------------------------------------------------------------------
# External Data Sources
# ---------------------------------------------------------------------------

class FREDClient:
    """Pull economic data from the Federal Reserve (FRED)."""

    def __init__(self, api_key: str = FRED_API_KEY):
        self.api_key = api_key
        self.session = requests.Session()

    def get_series(
        self,
        series_id: str,
        observation_start: str = None,
        observation_end: str = None,
        limit: int = 10,
        sort_order: str = "desc"
    ) -> pd.DataFrame:
        """
        Fetch a FRED time series.

        Common series IDs:
          CPIAUCSL    -- CPI All Urban Consumers
          CPILFESL    -- Core CPI (ex food/energy)
          FEDFUNDS    -- Federal Funds Rate
          UNRATE      -- Unemployment Rate
          GDP         -- Gross Domestic Product
          T10YIE      -- 10-Year Breakeven Inflation
          GASREGW      -- Regular Gas Price (weekly)
        """
        if self.api_key == "YOUR_FRED_API_KEY_HERE":
            print("[WARN] No FRED API key set. Get one free at:")
            print("       https://fred.stlouisfed.org/docs/api/api_key.html")
            print("       Then: export FRED_API_KEY=your_key_here")
            return pd.DataFrame()

        params = {
            "api_key": self.api_key,
            "series_id": series_id,
            "file_type": "json",
            "limit": limit,
            "sort_order": sort_order,
        }
        if observation_start:
            params["observation_start"] = observation_start
        if observation_end:
            params["observation_end"] = observation_end

        time.sleep(REQUEST_DELAY)
        resp = self.session.get(
            f"{FRED_BASE}/series/observations",
            params=params
        )
        resp.raise_for_status()
        data = resp.json()

        if "observations" not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data["observations"])
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df[["date", "value"]].dropna()

    def get_latest(self, series_id: str) -> Optional[float]:
        """Get the most recent value for a series."""
        df = self.get_series(series_id, limit=1)
        if df.empty:
            return None
        return float(df.iloc[0]["value"])

    def get_cpi_mom_history(self, periods: int = 12) -> pd.DataFrame:
        """
        Get month-over-month CPI changes.
        This is what Kalshi CPI markets typically resolve on.
        """
        df = self.get_series("CPIAUCSL", limit=periods + 1, sort_order="desc")
        if len(df) < 2:
            return pd.DataFrame()

        df = df.sort_values("date").reset_index(drop=True)
        df["mom_change"] = df["value"].pct_change() * 100
        df["mom_change_rounded"] = df["mom_change"].round(1)
        return df.dropna()


class NOAAClient:
    """Pull weather forecast data from NOAA for temperature markets."""

    def __init__(self, token: str = NOAA_TOKEN):
        self.token = token
        self.session = requests.Session()
        if token:
            self.session.headers["token"] = token

    def get_forecast(self, lat: float, lon: float) -> dict:
        """
        Get 7-day forecast from NWS API (no token needed).
        Returns daily high/low temperatures.
        """
        try:
            # Step 1: Get the forecast grid
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "KalshiEdgeFinder/1.0"}
            )
            resp.raise_for_status()
            grid_data = resp.json()
            forecast_url = grid_data["properties"]["forecast"]

            # Step 2: Get the actual forecast
            time.sleep(REQUEST_DELAY)
            resp = self.session.get(
                forecast_url,
                headers={"User-Agent": "KalshiEdgeFinder/1.0"}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[WARN] NOAA forecast fetch failed: {e}")
            return {}

    def get_city_forecast(self, city: str) -> dict:
        """Get forecast for common Kalshi temperature market cities."""
        coords = {
            "nyc":     (40.7128, -74.0060),
            "chicago": (41.8781, -87.6298),
            "miami":   (25.7617, -80.1918),
            "austin":  (30.2672, -97.7431),
            "la":      (34.0522, -118.2437),
        }
        city_key = city.lower().replace(" ", "")
        if city_key not in coords:
            print(f"[WARN] Unknown city '{city}'. Available: {list(coords.keys())}")
            return {}
        lat, lon = coords[city_key]
        return self.get_forecast(lat, lon)


# ---------------------------------------------------------------------------
# Edge Detection Engine
# ---------------------------------------------------------------------------

class EdgeFinder:
    """
    The brain of the operation.
    Compares Kalshi market prices against external data to find edges.
    """

    def __init__(self):
        self.kalshi = KalshiClient()
        self.fred = FREDClient()
        self.noaa = NOAAClient()
        self.signals: list[EdgeSignal] = []

    def scan_fed_rate_markets(self) -> list[EdgeSignal]:
        """
        Compare Kalshi Fed rate markets to CME FedWatch probabilities.
        The idea: if Kalshi prices diverge from CME futures-implied
        probabilities, that's a potential edge.
        """
        signals = []
        print("\n[*] Scanning Fed Funds Rate markets...")

        # Get current fed funds rate from FRED
        current_rate = self.fred.get_latest("FEDFUNDS")
        if current_rate is not None:
            print(f"    Current Fed Funds Rate: {current_rate}%")

        # Get 10-year breakeven inflation (market inflation expectations)
        breakeven = self.fred.get_latest("T10YIE")
        if breakeven is not None:
            print(f"    10Y Breakeven Inflation: {breakeven}%")

        # Search for Fed-related markets on Kalshi
        try:
            fed_markets = self.kalshi.search_markets("fed")
            rate_markets = self.kalshi.search_markets("rate cut")
            all_markets = fed_markets + rate_markets

            # Deduplicate by ticker
            seen = set()
            unique = []
            for m in all_markets:
                t = m.get("ticker", "")
                if t not in seen:
                    seen.add(t)
                    unique.append(m)

            if not unique:
                print("    No open Fed rate markets found.")
                return signals

            print(f"    Found {len(unique)} Fed-related markets")

            for raw in unique[:10]:  # cap at 10 to be reasonable
                market = self.kalshi.parse_market(raw)
                if market.yes_price <= 0:
                    continue

                # Simple heuristic: if breakeven inflation > 3%,
                # rate cut probability should be lower than market implies
                if breakeven and breakeven > 3.0 and market.yes_price > 0.5:
                    title_lower = market.title.lower()
                    if "cut" in title_lower or "lower" in title_lower:
                        edge = -0.15  # we think cuts are overpriced
                        signals.append(EdgeSignal(
                            market_ticker=market.ticker,
                            market_title=f"{market.title} {market.subtitle}",
                            market_price=market.yes_price,
                            model_estimate=max(0.05, market.yes_price + edge),
                            edge=edge,
                            edge_pct=edge / market.yes_price * 100 if market.yes_price else 0,
                            data_source="FRED T10YIE (breakeven inflation)",
                            confidence="MEDIUM",
                            reasoning=(
                                f"Breakeven inflation at {breakeven}% suggests "
                                f"persistent price pressures. Rate cuts at "
                                f"{market.yes_price:.0%} look overpriced."
                            ),
                            potential_return=abs(edge) / market.yes_price * 100
                        ))

        except Exception as e:
            print(f"    [ERROR] Fed market scan failed: {e}")

        return signals

    def scan_cpi_markets(self) -> list[EdgeSignal]:
        """
        Compare Kalshi CPI markets against historical CPI data
        and Cleveland Fed Nowcast estimates.
        """
        signals = []
        print("\n[*] Scanning CPI markets...")

        # Get CPI history for base rate analysis
        cpi_history = self.fred.get_cpi_mom_history(periods=24)
        if cpi_history.empty:
            print("    Could not fetch CPI history.")
            return signals

        # Calculate statistics
        recent = cpi_history.tail(12)
        avg_mom = recent["mom_change"].mean()
        std_mom = recent["mom_change"].std()
        last_mom = recent.iloc[-1]["mom_change"]

        print(f"    Last MoM CPI: {last_mom:.2f}%")
        print(f"    12-month avg MoM: {avg_mom:.2f}% (std: {std_mom:.2f}%)")

        # Search for CPI markets
        try:
            cpi_markets = self.kalshi.search_markets("CPI")
            inflation_markets = self.kalshi.search_markets("inflation")
            all_markets = cpi_markets + inflation_markets

            seen = set()
            unique = []
            for m in all_markets:
                t = m.get("ticker", "")
                if t not in seen:
                    seen.add(t)
                    unique.append(m)

            if not unique:
                print("    No open CPI/inflation markets found.")
                return signals

            print(f"    Found {len(unique)} CPI/inflation markets")

            for raw in unique[:10]:
                market = self.kalshi.parse_market(raw)
                if market.yes_price <= 0:
                    continue

                # Log what we found for manual analysis
                print(f"    -> {market.ticker}: {market.title} "
                      f"{market.subtitle} @ {market.yes_price:.2f}")

        except Exception as e:
            print(f"    [ERROR] CPI market scan failed: {e}")

        return signals

    def scan_weather_markets(self, city: str = "nyc") -> list[EdgeSignal]:
        """
        Compare Kalshi daily temperature markets against NWS forecasts.
        This is one of the most consistently exploitable edges because
        NWS forecasts are highly accurate 1-2 days out and freely available.
        """
        signals = []
        print(f"\n[*] Scanning weather markets for {city.upper()}...")

        forecast = self.noaa.get_city_forecast(city)
        if not forecast:
            print("    Could not fetch forecast data.")
            return signals

        periods = forecast.get("properties", {}).get("periods", [])
        if not periods:
            print("    No forecast periods returned.")
            return signals

        # Extract highs and lows
        print(f"    Forecast loaded: {len(periods)} periods")
        for p in periods[:4]:
            temp = p.get("temperature", "?")
            name = p.get("name", "")
            print(f"    -> {name}: {temp}F")

        # Search for temperature markets
        try:
            temp_markets = self.kalshi.search_markets(f"temperature {city}")
            if not temp_markets:
                temp_markets = self.kalshi.search_markets("temperature")

            if not temp_markets:
                print("    No open temperature markets found.")
                return signals

            print(f"    Found {len(temp_markets)} temperature markets")

            for raw in temp_markets[:10]:
                market = self.kalshi.parse_market(raw)
                if market.yes_price <= 0:
                    continue
                print(f"    -> {market.ticker}: {market.title} "
                      f"{market.subtitle} @ {market.yes_price:.2f}")

        except Exception as e:
            print(f"    [ERROR] Weather market scan failed: {e}")

        return signals

    def scan_gas_price_markets(self) -> list[EdgeSignal]:
        """
        Compare Kalshi gas price markets against EIA/AAA data.
        Gas prices are sticky short-term, so current data is highly
        predictive of next week's resolution.
        """
        signals = []
        print("\n[*] Scanning gas price markets...")

        # Get weekly gas price from FRED
        gas_price = self.fred.get_latest("GASREGW")
        if gas_price is not None:
            print(f"    Latest regular gas price: ${gas_price:.3f}/gal")
        else:
            print("    Could not fetch gas price data.")

        try:
            gas_markets = self.kalshi.search_markets("gas price")
            if not gas_markets:
                gas_markets = self.kalshi.search_markets("gasoline")

            if not gas_markets:
                print("    No open gas price markets found.")
                return signals

            print(f"    Found {len(gas_markets)} gas price markets")

            for raw in gas_markets[:10]:
                market = self.kalshi.parse_market(raw)
                if market.yes_price <= 0:
                    continue
                print(f"    -> {market.ticker}: {market.title} "
                      f"{market.subtitle} @ {market.yes_price:.2f}")

                # If we have current gas data, compare to market threshold
                if gas_price and market.subtitle:
                    # Try to extract threshold from subtitle
                    # e.g., "above $4.00" or "between $3.80 and $4.00"
                    subtitle_lower = market.subtitle.lower()
                    if "above" in subtitle_lower and "$" in subtitle_lower:
                        try:
                            threshold_str = subtitle_lower.split("$")[1].split()[0].rstrip("?")
                            threshold = float(threshold_str)
                            # Simple model: if current price is X% above/below
                            # threshold, estimate probability accordingly
                            diff_pct = (gas_price - threshold) / threshold * 100

                            if diff_pct > 5:
                                model_est = 0.85
                            elif diff_pct > 2:
                                model_est = 0.70
                            elif diff_pct > 0:
                                model_est = 0.55
                            elif diff_pct > -2:
                                model_est = 0.40
                            elif diff_pct > -5:
                                model_est = 0.25
                            else:
                                model_est = 0.10

                            edge = model_est - market.yes_price
                            if abs(edge) > 0.08:  # 8% minimum edge
                                signals.append(EdgeSignal(
                                    market_ticker=market.ticker,
                                    market_title=f"{market.title} {market.subtitle}",
                                    market_price=market.yes_price,
                                    model_estimate=model_est,
                                    edge=edge,
                                    edge_pct=edge / market.yes_price * 100 if market.yes_price else 0,
                                    data_source=f"FRED GASREGW (current: ${gas_price:.3f})",
                                    confidence="MEDIUM" if abs(edge) > 0.15 else "LOW",
                                    reasoning=(
                                        f"Current gas: ${gas_price:.3f}. "
                                        f"Threshold: ${threshold:.2f}. "
                                        f"Diff: {diff_pct:+.1f}%. "
                                        f"Market: {market.yes_price:.0%}, "
                                        f"Model: {model_est:.0%}."
                                    ),
                                    potential_return=abs(edge) / max(market.yes_price, 0.01) * 100
                                ))
                        except (ValueError, IndexError):
                            pass

        except Exception as e:
            print(f"    [ERROR] Gas market scan failed: {e}")

        return signals

    def scan_orderbook_depth(self, tickers: list) -> list[dict]:
        """
        Analyze order book depth for given tickers.
        Thin books = opportunity for limit orders.
        Wide spreads = opportunity for market making.
        """
        results = []
        print("\n[*] Analyzing order book depth...")

        for ticker in tickers:
            try:
                book = self.kalshi.get_orderbook(ticker)
                orderbook = book.get("orderbook", {})

                yes_bids = orderbook.get("yes", [])
                no_bids = orderbook.get("no", [])

                # Calculate spread
                best_yes_bid = max(
                    (float(b[0]) for b in yes_bids),
                    default=0
                ) if yes_bids else 0
                best_no_bid = max(
                    (float(b[0]) for b in no_bids),
                    default=0
                ) if no_bids else 0

                spread = 1.0 - best_yes_bid - best_no_bid
                total_depth = sum(
                    int(b[1]) for b in yes_bids
                ) + sum(
                    int(b[1]) for b in no_bids
                ) if yes_bids or no_bids else 0

                results.append({
                    "ticker": ticker,
                    "best_yes_bid": best_yes_bid,
                    "best_no_bid": best_no_bid,
                    "spread": spread,
                    "total_depth": total_depth,
                    "mm_opportunity": spread > 0.05,  # 5c+ spread = market making opp
                })

                print(f"    {ticker}: spread={spread:.2f}, "
                      f"depth={total_depth}, "
                      f"{'[MM OPPORTUNITY]' if spread > 0.05 else ''}")

            except Exception as e:
                print(f"    [ERROR] {ticker}: {e}")

        return results

    def find_longshot_overpricing(self) -> list[EdgeSignal]:
        """
        Scan for the favorite-longshot bias.
        Academic research shows contracts under 10c are systematically
        overpriced on Kalshi. Find them and flag as potential No buys.
        """
        signals = []
        print("\n[*] Scanning for overpriced longshots (favorite-longshot bias)...")

        try:
            cursor = None
            longshots = []

            for _ in range(3):  # 3 pages max
                data = self.kalshi.get_markets(limit=200, cursor=cursor)
                markets = data.get("markets", [])
                if not markets:
                    break

                for raw in markets:
                    market = self.kalshi.parse_market(raw)
                    # Longshots: priced under 10 cents with meaningful volume
                    if 0.01 < market.yes_price < 0.10 and market.volume > 100:
                        longshots.append(market)

                cursor = data.get("cursor")
                if not cursor:
                    break

            print(f"    Found {len(longshots)} longshot contracts (<10c, vol>100)")

            # Sort by volume (most liquid = most tradeable)
            longshots.sort(key=lambda m: m.volume, reverse=True)

            for market in longshots[:15]:
                # Research shows these win ~40% less often than price implies
                # A 5c contract implies 5% probability but wins ~3% of the time
                implied_prob = market.yes_price
                estimated_true_prob = implied_prob * 0.6  # 40% overpricing factor

                edge = estimated_true_prob - implied_prob
                signals.append(EdgeSignal(
                    market_ticker=market.ticker,
                    market_title=f"{market.title} {market.subtitle}",
                    market_price=implied_prob,
                    model_estimate=estimated_true_prob,
                    edge=edge,
                    edge_pct=edge / implied_prob * 100 if implied_prob else 0,
                    data_source="Favorite-longshot bias (Whelan 2025, CEPR)",
                    confidence="MEDIUM",
                    reasoning=(
                        f"Contract at {implied_prob:.0%} implies "
                        f"{implied_prob:.1%} probability. Research shows "
                        f"sub-10c contracts win ~40% less often than priced. "
                        f"Sell Yes / Buy No for edge."
                    ),
                    potential_return=(1.0 - market.yes_price) / (1.0 - market.yes_price) * abs(edge) * 100
                ))

        except Exception as e:
            print(f"    [ERROR] Longshot scan failed: {e}")

        return signals

    def run_full_scan(self) -> list[EdgeSignal]:
        """Run all scanners and aggregate results."""
        print("=" * 65)
        print("  KALSHI EDGE FINDER v1.0")
        print(f"  Scan started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 65)

        all_signals = []

        # 1. Fed rate markets vs FRED data
        all_signals.extend(self.scan_fed_rate_markets())

        # 2. CPI markets vs historical data
        all_signals.extend(self.scan_cpi_markets())

        # 3. Weather markets vs NWS forecasts
        all_signals.extend(self.scan_weather_markets("nyc"))

        # 4. Gas price markets vs EIA data
        all_signals.extend(self.scan_gas_price_markets())

        # 5. Favorite-longshot bias scan
        all_signals.extend(self.find_longshot_overpricing())

        self.signals = all_signals
        return all_signals

    def print_report(self):
        """Pretty-print the scan results."""
        print("\n" + "=" * 65)
        print("  EDGE REPORT")
        print("=" * 65)

        if not self.signals:
            print("\n  No actionable edges detected in this scan.")
            print("  This is normal -- genuine edges are rare.")
            print("  Run again before major data releases for best results.")
            return

        # Sort by absolute edge size
        sorted_signals = sorted(
            self.signals,
            key=lambda s: abs(s.edge),
            reverse=True
        )

        # Group by confidence
        for conf in ["HIGH", "MEDIUM", "LOW"]:
            conf_signals = [s for s in sorted_signals if s.confidence == conf]
            if not conf_signals:
                continue

            print(f"\n  [{conf} CONFIDENCE] ({len(conf_signals)} signals)")
            print("  " + "-" * 60)

            for s in conf_signals:
                direction = "BUY YES" if s.edge > 0 else "SELL YES (BUY NO)"
                print(f"\n  {s.market_ticker}")
                print(f"  {s.market_title}")
                print(f"  Market: {s.market_price:.0%} | "
                      f"Model: {s.model_estimate:.0%} | "
                      f"Edge: {s.edge:+.0%}")
                print(f"  Action: {direction}")
                print(f"  Source: {s.data_source}")
                print(f"  {s.reasoning}")

        # Summary table
        print("\n\n  SUMMARY TABLE")
        print("  " + "-" * 60)
        table_data = []
        for s in sorted_signals[:20]:
            table_data.append([
                s.market_ticker[:20],
                f"{s.market_price:.0%}",
                f"{s.model_estimate:.0%}",
                f"{s.edge:+.0%}",
                s.confidence,
                "BUY" if s.edge > 0 else "SELL",
            ])

        print(tabulate(
            table_data,
            headers=["Ticker", "Market", "Model", "Edge", "Conf", "Action"],
            tablefmt="simple",
            stralign="right"
        ))

        print("\n  DISCLAIMER: This is not financial advice. These are")
        print("  statistical signals, not guaranteed profits. Do your")
        print("  own research. Manage your risk. Don't bet the rent.")


# ---------------------------------------------------------------------------
# Utility: Environment Setup Helper
# ---------------------------------------------------------------------------

def print_setup_guide():
    """Print setup instructions for new users."""
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║           KALSHI EDGE FINDER -- SETUP GUIDE             ║
    ╚══════════════════════════════════════════════════════════╝

    1. Install dependencies:
       pip install requests pandas tabulate python-dotenv

    2. Get a FREE FRED API key:
       https://fred.stlouisfed.org/docs/api/api_key.html

    3. Set your API key:
       export FRED_API_KEY=your_key_here

       Or create a .env file:
       FRED_API_KEY=your_key_here

    4. (Optional) For weather markets, no extra key needed.
       NWS API is free and unauthenticated.

    5. (Optional) For trading, you need a Kalshi account
       and API key. This script only reads public data
       by default.

    6. Run it:
       python kalshi_edge_finder.py
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Entry point."""
    # Try loading .env file if python-dotenv is installed
    try:
        from dotenv import load_dotenv
        load_dotenv()
        # Re-read env vars after loading .env
        global FRED_API_KEY
        FRED_API_KEY = os.environ.get("FRED_API_KEY", "YOUR_FRED_API_KEY_HERE")
    except ImportError:
        pass

    # Check if we need to show setup guide
    if FRED_API_KEY == "YOUR_FRED_API_KEY_HERE":
        print_setup_guide()
        print("  [!] Running in demo mode (limited data)...\n")

    # Run the scanner
    finder = EdgeFinder()
    finder.run_full_scan()
    finder.print_report()

    print("\n\n  NEXT STEPS:")
    print("  1. Cross-reference signals with your own analysis")
    print("  2. Check order book depth on interesting markets")
    print("  3. Start small. Use limit orders. Be the house.")
    print("  4. Run this before major data releases (CPI, NFP, FOMC)")
    print()


if __name__ == "__main__":
    main()

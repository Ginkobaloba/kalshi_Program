"""
Cross-market arbitrage between Kalshi and Polymarket (international).

For each event pair in config/event_map.yaml, this strategy:
  1. Pulls all legs from both exchanges
  2. Matches legs by name (e.g., "Boston Celtics" on Polymarket -> "BOS" on Kalshi)
  3. Computes cross-exchange edge for each matched leg
  4. Emits TradeSignals for legs exceeding the configured threshold

CAVEAT: Polymarket International geoblocks US users from TRADING (reads OK).
The polymarket adapter refuses orders by ComplianceError. Three modes:

  paper_both:  Both legs simulated through paper adapter. Research mode.
  one_legged:  Only Kalshi leg traded. Polymarket price is the signal.
               Directional bet, not arb. Default for live use.
  disabled:    Skip entirely.

Internal throttling: full scans take 30-60s due to per-leg orderbook calls.
We cache results for `scan_interval_sec` (default 60s) so the main bot poll
loop (5s) doesn't hammer either exchange.

Event map supports both old format (single market pair) and new format
(multi-leg event pair) — the new format is preferred.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import Action, Side, TradeSignal, Venue
from pm_bot.strategies.base import Strategy
from pm_bot.util.math import bps, kalshi_taker_fee, polymarket_fee

log = get_logger("cross_market_arb")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _words(s: str) -> set[str]:
    """Extract significant words for fuzzy name matching."""
    return {w.lower().strip(",.?!()") for w in (s or "").split() if len(w) > 3}


class CrossMarketArbStrategy(Strategy):
    name = "cross_market_arb"

    def __init__(self, config: dict[str, Any], adapters: dict[Venue, ExchangeAdapter]):
        super().__init__(config, adapters)
        self.min_gap_cents = float(config.get("min_gap_cents", 1.0))
        self.mode = config.get("mode", "paper_both")
        self.event_map_path = config.get("event_map_path", "./config/event_map.yaml")
        self.scan_interval_sec = float(config.get("scan_interval_sec", 60))
        self.poly_throttle_sec = float(config.get("poly_throttle_sec", 0.3))
        self.size_per_signal = int(config.get("size_per_signal", 10))
        self._event_map = self._load_event_map()
        self._last_scan_ts = 0.0
        self._cached_signals: list[TradeSignal] = []
        self._poly_session = requests.Session()

    # ------------------------------------------------------------------
    # event map loading
    # ------------------------------------------------------------------

    def _load_event_map(self) -> list[dict]:
        p = Path(self.event_map_path)
        if not p.exists():
            log.info("No event map at %s — cross-market arb idle.", p)
            return []
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            return data.get("pairs", []) or []
        except Exception as e:
            log.warning("Failed to load event map: %s", e)
            return []

    # ------------------------------------------------------------------
    # public scan entry point — throttled
    # ------------------------------------------------------------------

    def scan(self) -> list[TradeSignal]:
        if not self.enabled or self.mode == "disabled":
            return []
        if not self._event_map:
            return []

        now = time.time()
        if now - self._last_scan_ts < self.scan_interval_sec:
            return self._cached_signals

        self._last_scan_ts = now
        try:
            self._cached_signals = self._do_full_scan()
        except Exception as e:
            log.exception("cross_market_arb scan failed: %s", e)
            self._cached_signals = []
        return self._cached_signals

    # ------------------------------------------------------------------
    # scan implementation
    # ------------------------------------------------------------------

    def _do_full_scan(self) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for pair in self._event_map:
            try:
                if "kalshi_event" in pair:
                    signals.extend(self._scan_multi_leg(pair))
                elif "kalshi_ticker" in pair:
                    signals.extend(self._scan_legacy_single(pair))
            except Exception as e:
                log.warning("pair scan failed (%s): %s",
                           pair.get("name", "?"), e)
        log.info("cross_market_arb scanned %d pairs, emitted %d signals",
                len(self._event_map), len(signals))
        return signals

    # ------------------------------------------------------------------
    # multi-leg scan (new format: kalshi_event + poly_keywords)
    # ------------------------------------------------------------------

    def _scan_multi_leg(self, pair: dict) -> list[TradeSignal]:
        kalshi = self.adapters.get(Venue.KALSHI)
        if not kalshi:
            return []

        pair_name = pair.get("name", "?")
        k_event = pair["kalshi_event"]
        p_keywords = pair.get("poly_keywords", []) or []

        # Kalshi side: pull markets + orderbooks
        kalshi_legs: dict[str, dict] = {}
        try:
            k_markets = kalshi.list_markets(event_id=k_event, limit=20)
        except Exception as e:
            log.warning("Kalshi list_markets failed for %s: %s", k_event, e)
            return []

        for m in k_markets:
            # Use Market summary fields directly — already best bid/ask
            if m.yes_ask >= 1.0 and m.yes_bid <= 0:
                continue
            team = (m.title or "").replace("yes ", "").replace("Yes ", "").strip()
            kalshi_legs[team] = {
                "ticker": m.ticker,
                "yes_bid": m.yes_bid,
                "yes_ask": m.yes_ask,
                "no_bid": m.no_bid,
                "no_ask": m.no_ask,
                "vol": m.volume,
            }

        if not kalshi_legs:
            return []

        # Polymarket side: find event by keyword, pull orderbooks
        poly_legs = self._fetch_polymarket_legs(p_keywords)
        if not poly_legs:
            log.debug("No Polymarket match for keywords %s", p_keywords)
            return []

        # Match legs by name + emit signals
        signals: list[TradeSignal] = []
        for k_team, kv in kalshi_legs.items():
            kw = _words(k_team)
            match = None
            match_name = None
            for p_name, pv in poly_legs.items():
                if kw & _words(p_name):
                    match = pv
                    match_name = p_name
                    break
            if not match:
                continue

            # Direction A: BUY P_YES + BUY K_NO
            # Cost = p_ask + (1 - k_bid)  (selling K_YES at bid = buying NO at 1-bid)
            # Profit = $1 - cost
            if match["ask"] > 0 and kv["yes_bid"] > 0 and match["ask"] < 1:
                cost_a = match["ask"] + (1 - kv["yes_bid"])
                edge_a_cents = (1 - cost_a) * 100
                if edge_a_cents >= self.min_gap_cents:
                    signals.extend(self._make_pair_signals(
                        pair_name, kv, match, match_name,
                        direction="P_YES+K_NO",
                        edge_cents=edge_a_cents,
                    ))

            # Direction B: BUY K_YES + BUY P_NO
            if kv["yes_ask"] > 0 and kv["yes_ask"] < 1 and match["bid"] > 0:
                cost_b = kv["yes_ask"] + (1 - match["bid"])
                edge_b_cents = (1 - cost_b) * 100
                if edge_b_cents >= self.min_gap_cents:
                    signals.extend(self._make_pair_signals(
                        pair_name, kv, match, match_name,
                        direction="K_YES+P_NO",
                        edge_cents=edge_b_cents,
                    ))

        return signals

    def _fetch_polymarket_legs(self, keywords: list[str]) -> dict:
        """Find a Polymarket event by keywords and return its legs as
        {leg_name: {bid, ask}}."""
        try:
            r = self._poly_session.get(
                f"{GAMMA_BASE}/events",
                params={"limit": 500, "active": "true", "closed": "false"},
                timeout=10,
            )
            events = r.json() if r.ok and isinstance(r.json(), list) else []
        except Exception as e:
            log.warning("Polymarket events fetch failed: %s", e)
            return {}

        target = None
        for ev in events:
            title = (ev.get("title") or "").lower()
            if any(all(w in title for w in kw.lower().split()) for kw in keywords):
                target = ev
                break
        if not target:
            return {}

        legs: dict = {}
        for m in target.get("markets") or []:
            name = (m.get("groupItemTitle") or m.get("question") or "").strip()
            tokens = m.get("clobTokenIds") or []
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []
            if not tokens or not tokens[0]:
                continue
            time.sleep(self.poly_throttle_sec)
            try:
                r = self._poly_session.get(
                    f"{CLOB_BASE}/book",
                    params={"token_id": tokens[0]},
                    timeout=5,
                )
                book = r.json() if r.ok else {}
            except Exception:
                continue
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            ba = min((float(a["price"]) for a in asks), default=0) if asks else 0
            bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
            legs[name] = {"bid": bb, "ask": ba}
        return legs

    # ------------------------------------------------------------------
    # signal construction
    # ------------------------------------------------------------------

    def _make_pair_signals(
        self,
        pair_name: str,
        kalshi_leg: dict,
        poly_leg: dict,
        poly_leg_name: str,
        direction: str,
        edge_cents: float,
    ) -> list[TradeSignal]:
        """
        Build TradeSignal(s) for a detected gap. Mode determines whether we
        emit one (Kalshi only) or two (both legs) signals.
        """
        edge_dollars = edge_cents / 100.0
        confidence = 0.85 if self.mode == "paper_both" else 0.60

        if direction == "P_YES+K_NO":
            # BUY YES on Polymarket, BUY NO on Kalshi (= sell YES on Kalshi)
            poly_signal = TradeSignal(
                strategy=self.name,
                venue=Venue.POLYMARKET,
                ticker="poly:" + (poly_leg_name[:30]),
                side=Side.YES,
                action=Action.BUY,
                price=poly_leg["ask"],
                size=self.size_per_signal,
                edge_bps=bps(edge_dollars),
                confidence=confidence,
                reasoning=(
                    f"cross-arb {pair_name}: P_YES@{poly_leg['ask']:.3f} + "
                    f"K_NO@{1 - kalshi_leg['yes_bid']:.3f} = "
                    f"{poly_leg['ask'] + (1 - kalshi_leg['yes_bid']):.3f}, "
                    f"edge={edge_cents:.2f}c"
                ),
            )
            kalshi_signal = TradeSignal(
                strategy=self.name,
                venue=Venue.KALSHI,
                ticker=kalshi_leg["ticker"],
                side=Side.NO,
                action=Action.BUY,
                price=1 - kalshi_leg["yes_bid"],
                size=self.size_per_signal,
                edge_bps=bps(edge_dollars),
                confidence=confidence,
                reasoning=(
                    f"cross-arb {pair_name} {poly_leg_name}: "
                    f"K_NO@{1 - kalshi_leg['yes_bid']:.3f}, edge={edge_cents:.2f}c"
                ),
            )
        else:  # K_YES+P_NO
            kalshi_signal = TradeSignal(
                strategy=self.name,
                venue=Venue.KALSHI,
                ticker=kalshi_leg["ticker"],
                side=Side.YES,
                action=Action.BUY,
                price=kalshi_leg["yes_ask"],
                size=self.size_per_signal,
                edge_bps=bps(edge_dollars),
                confidence=confidence,
                reasoning=(
                    f"cross-arb {pair_name} {poly_leg_name}: "
                    f"K_YES@{kalshi_leg['yes_ask']:.3f} + "
                    f"P_NO@{1 - poly_leg['bid']:.3f}, edge={edge_cents:.2f}c"
                ),
            )
            poly_signal = TradeSignal(
                strategy=self.name,
                venue=Venue.POLYMARKET,
                ticker="poly:" + (poly_leg_name[:30]),
                side=Side.NO,
                action=Action.BUY,
                price=1 - poly_leg["bid"],
                size=self.size_per_signal,
                edge_bps=bps(edge_dollars),
                confidence=confidence,
                reasoning=f"cross-arb {pair_name} companion leg",
            )

        if self.mode == "paper_both":
            kalshi_signal.companion_signals = [poly_signal]
            return [kalshi_signal]
        elif self.mode == "one_legged":
            kalshi_signal.reasoning += " [one-legged: Kalshi only]"
            return [kalshi_signal]
        return []

    # ------------------------------------------------------------------
    # legacy single-pair format (backward compatibility)
    # ------------------------------------------------------------------

    def _scan_legacy_single(self, pair: dict) -> list[TradeSignal]:
        """Old format: kalshi_ticker + poly_token_id, single leg each side."""
        kalshi = self.adapters.get(Venue.KALSHI)
        poly = self.adapters.get(Venue.POLYMARKET)
        if not kalshi or not poly:
            return []
        k_ticker = pair.get("kalshi_ticker")
        p_token = pair.get("poly_token_id")
        if not k_ticker or not p_token:
            return []
        kmkt = kalshi.get_market(k_ticker)
        pmkt = poly.get_market(p_token)
        if not kmkt or not pmkt:
            return []

        # Use the same A/B direction logic as multi-leg
        signals: list[TradeSignal] = []
        kv = {
            "ticker": k_ticker,
            "yes_bid": kmkt.yes_bid,
            "yes_ask": kmkt.yes_ask,
            "no_bid": kmkt.no_bid,
            "no_ask": kmkt.no_ask,
        }
        match = {"bid": pmkt.yes_bid, "ask": pmkt.yes_ask}

        # Direction A
        if match["ask"] > 0 and kv["yes_bid"] > 0 and match["ask"] < 1:
            cost = match["ask"] + (1 - kv["yes_bid"])
            edge = (1 - cost) * 100
            if edge >= self.min_gap_cents:
                signals.extend(self._make_pair_signals(
                    pair.get("name", k_ticker), kv, match, p_token,
                    direction="P_YES+K_NO", edge_cents=edge,
                ))
        # Direction B
        if kv["yes_ask"] > 0 and kv["yes_ask"] < 1 and match["bid"] > 0:
            cost = kv["yes_ask"] + (1 - match["bid"])
            edge = (1 - cost) * 100
            if edge >= self.min_gap_cents:
                signals.extend(self._make_pair_signals(
                    pair.get("name", k_ticker), kv, match, p_token,
                    direction="K_YES+P_NO", edge_cents=edge,
                ))
        return signals

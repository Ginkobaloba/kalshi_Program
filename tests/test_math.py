"""
Unit tests for pm_bot.util.math. Run with:
  pytest tests/test_math.py -v
"""

from __future__ import annotations

import pytest

from pm_bot.util.math import (
    bps,
    cross_market_arb_edge,
    kalshi_maker_fee,
    kalshi_taker_fee,
    kelly_contracts,
    kelly_fraction_of_bankroll,
    polymarket_fee,
    sum_prob_arb_edge,
)


class TestKalshiFees:
    def test_taker_fee_at_50_cents(self):
        fee = kalshi_taker_fee(0.50, 1)
        assert fee == pytest.approx(0.02, abs=0.001)

    def test_taker_fee_symmetric(self):
        a = kalshi_taker_fee(0.30, 100)
        b = kalshi_taker_fee(0.70, 100)
        assert a == pytest.approx(b, abs=0.011)

    def test_maker_fee_is_less_than_taker(self):
        taker = kalshi_taker_fee(0.50, 100)
        maker = kalshi_maker_fee(0.50, 100)
        assert maker < taker

    def test_taker_fee_near_zero_at_extremes(self):
        assert kalshi_taker_fee(0.99, 1) < kalshi_taker_fee(0.50, 1)

    def test_fee_scales_with_size(self):
        fee_1 = kalshi_taker_fee(0.50, 1)
        fee_100 = kalshi_taker_fee(0.50, 100)
        assert fee_100 > fee_1 * 50


class TestSumProbArb:
    def test_detects_obvious_arb(self):
        edge = sum_prob_arb_edge([0.30, 0.35, 0.25, 0.20], contracts_per_leg=10)
        assert edge > 0.3

    def test_no_arb_when_sum_equals_one(self):
        edge = sum_prob_arb_edge([0.25, 0.25, 0.25, 0.25], contracts_per_leg=10)
        assert edge < 0

    def test_single_outcome_returns_zero(self):
        assert sum_prob_arb_edge([0.55]) == 0.0


class TestCrossMarketArb:
    def test_detects_gap(self):
        edge = cross_market_arb_edge(0.40, 0.50, contracts=1)
        assert edge > 0.05

    def test_no_arb_when_sum_at_one(self):
        edge = cross_market_arb_edge(0.55, 0.45, contracts=1)
        assert edge < 0


class TestKelly:
    def test_fraction_positive_when_edge_positive(self):
        f = kelly_fraction_of_bankroll(0.60, 0.50, kelly_multiplier=1.0)
        assert f > 0

    def test_fraction_zero_when_no_edge(self):
        assert kelly_fraction_of_bankroll(0.50, 0.50) == 0.0
        assert kelly_fraction_of_bankroll(0.40, 0.50) == 0.0

    def test_fractional_kelly_reduces_bet(self):
        full = kelly_fraction_of_bankroll(0.60, 0.50, kelly_multiplier=1.0)
        quarter = kelly_fraction_of_bankroll(0.60, 0.50, kelly_multiplier=0.25)
        assert quarter == pytest.approx(full * 0.25)

    def test_contracts_respects_cap(self):
        n = kelly_contracts(0.95, 0.50, bankroll=10000, max_position_usd=50)
        assert n * 0.50 <= 50.01


class TestBps:
    def test_bps_conversion(self):
        assert bps(0.02) == 200
        assert bps(0.0001) == 1
        assert bps(-0.015) == -150


class TestPolymarketFee:
    def test_polymarket_fee_proportional(self):
        assert polymarket_fee(0.50, 100, bps=100) == pytest.approx(0.50, abs=0.01)

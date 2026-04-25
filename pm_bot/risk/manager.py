"""
Risk manager.

Sits between strategies and exchange adapters. A strategy emits a
TradeSignal; the risk manager decides whether to convert it to one or more
Orders and route them. It enforces:

  - Fractional Kelly sizing
  - Per-contract position cap (max_position_usd)
  - Concurrent position cap
  - Daily order-count cap
  - Daily loss kill switch
  - Minimum edge threshold
  - Minimum book depth threshold
  - Longshot filter (no sub-10c buys on YES side by default)
  - Price range guards

Every decision — accept, reject, trim — is logged to the DB with reason.
"""

from __future__ import annotations

from pm_bot.config import RiskConfig
from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import (
    Action,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
    TradeSignal,
    Venue,
)
from pm_bot.persistence.db import Database
from pm_bot.util.math import kelly_contracts

log = get_logger("risk")


class KillSwitchTriggered(RuntimeError):
    pass


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig,
        db: Database,
        adapters: dict[Venue, ExchangeAdapter],
    ):
        self.cfg = cfg
        self.db = db
        self.adapters = adapters
        self._kill = False

    # ----- evaluation ----------------------------------------------------

    def evaluate(
        self,
        signal: TradeSignal,
        book: OrderBook | None = None,
    ) -> tuple[bool, str]:
        """
        Decide whether to accept a signal. Returns (accept, reason).
        Reason is a short string (for logging) explaining rejection.
        """
        if self._kill:
            return False, "kill_switch_active"

        # Price range
        if signal.side == Side.YES and signal.action == Action.BUY:
            if signal.price < self.cfg.min_price:
                return False, f"price_below_min_{self.cfg.min_price}"
            if signal.price > self.cfg.max_price:
                return False, f"price_above_max_{self.cfg.max_price}"

        # Minimum edge
        if signal.edge_bps < self.cfg.min_edge_bps:
            return False, f"edge_below_min_{self.cfg.min_edge_bps}bps"

        # Daily loss kill switch
        pnl = self.db.today_realized_pnl()
        if pnl <= -abs(self.cfg.daily_loss_limit_usd):
            self._trigger_kill(f"daily_loss_{pnl:.2f}")
            return False, "daily_loss_limit"

        # Daily order count
        if self.db.today_order_count() >= self.cfg.max_daily_orders:
            return False, "daily_order_limit"

        # Book depth
        if book is not None:
            avail = book.depth(signal.side, signal.action)
            if avail < self.cfg.min_book_depth_contracts:
                return False, f"insufficient_depth_{avail}"

        # Concurrent positions
        adapter = self.adapters.get(signal.venue)
        if adapter is not None:
            current_positions = adapter.get_positions()
            if (len(current_positions) >= self.cfg.max_concurrent_positions
                    and signal.ticker not in {p.ticker for p in current_positions}):
                return False, "concurrent_position_limit"

        return True, "ok"

    def size(self, signal: TradeSignal) -> int:
        """
        Kelly-size the signal. For arb signals (edge_bps high, confidence 1),
        size up to max_position_usd. For directional, use fractional Kelly.
        """
        # If strategy already suggested a size, respect it as an upper bound
        strategy_suggested = signal.size

        if signal.confidence >= 0.99 and signal.edge_bps > 100:
            # Treat as arbitrage — size by dollar cap only
            dollars = min(self.cfg.max_position_usd, signal.size * signal.price)
            return max(0, int(dollars / max(signal.price, 0.01)))

        # Directional Kelly
        # edge_prob implied: market_price * (1 + edge_bps/10000) for BUY YES
        # This is a rough conversion; strategies should set confidence thoughtfully.
        edge_frac = signal.edge_bps / 10000.0
        edge_prob = min(1.0, signal.price + edge_frac) if signal.action == Action.BUY else max(0.0, signal.price - edge_frac)

        n = kelly_contracts(
            edge_prob=edge_prob,
            market_price=signal.price,
            bankroll=self.cfg.bankroll_usd,
            kelly_multiplier=self.cfg.kelly_fraction,
            max_position_usd=self.cfg.max_position_usd,
        )
        return min(n, strategy_suggested or n)

    # ----- routing --------------------------------------------------------

    def route(
        self,
        signal: TradeSignal,
        book: OrderBook | None = None,
    ) -> Order | None:
        """
        Full pipeline: evaluate -> size -> build Order. Returns None on reject.
        Logs either way.
        """
        accept, reason = self.evaluate(signal, book)

        if not accept:
            self.db.log_signal(
                strategy=signal.strategy,
                venue=signal.venue.value,
                ticker=signal.ticker,
                side=signal.side.value,
                action=signal.action.value,
                price=signal.price,
                size=signal.size,
                edge_bps=signal.edge_bps,
                confidence=signal.confidence,
                reasoning=signal.reasoning,
                outcome="rejected",
                rejection_reason=reason,
                companion_count=len(signal.companion_signals),
            )
            log.debug("Signal rejected (%s): %s %s %s @ %.2f",
                     reason, signal.strategy, signal.ticker,
                     signal.action.value, signal.price)
            return None

        contracts = self.size(signal)
        if contracts <= 0:
            self.db.log_signal(
                strategy=signal.strategy,
                venue=signal.venue.value,
                ticker=signal.ticker,
                side=signal.side.value,
                action=signal.action.value,
                price=signal.price,
                size=0,
                edge_bps=signal.edge_bps,
                confidence=signal.confidence,
                reasoning=signal.reasoning,
                outcome="rejected",
                rejection_reason="kelly_size_zero",
            )
            return None

        from pm_bot.exchanges.kalshi import new_client_order_id
        order = Order(
            venue=signal.venue,
            ticker=signal.ticker,
            side=signal.side,
            action=signal.action,
            order_type=OrderType.LIMIT,
            price=signal.price,
            size=contracts,
            client_order_id=new_client_order_id(signal.strategy[:3] if signal.strategy else "pmb"),
            strategy=signal.strategy,
            status=OrderStatus.PENDING,
            notes=signal.reasoning[:200],
        )

        self.db.log_signal(
            strategy=signal.strategy,
            venue=signal.venue.value,
            ticker=signal.ticker,
            side=signal.side.value,
            action=signal.action.value,
            price=signal.price,
            size=contracts,
            edge_bps=signal.edge_bps,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
            outcome="accepted",
            companion_count=len(signal.companion_signals),
        )

        return order

    # ----- kill switch ----------------------------------------------------

    def _trigger_kill(self, why: str) -> None:
        if self._kill:
            return
        self._kill = True
        log.error("KILL SWITCH ACTIVATED: %s", why)
        self.db.log_event("kill_switch", why)

    @property
    def killed(self) -> bool:
        return self._kill

    def reset_kill(self) -> None:
        """Operator override — use with full awareness."""
        self._kill = False
        self.db.log_event("kill_switch_reset", "operator manually reset kill switch")

"""
Paper-trading shim.

Wraps any ExchangeAdapter. Reads go through to the real adapter (we want
real market data). Writes (place_order / cancel_order) are simulated
locally — we track a virtual balance and position list.

Fill simulation is deliberately optimistic-but-honest:
  - Limit orders fill immediately at their limit if the limit is at or
    through the opposite side of the book (i.e. we'd have crossed).
  - Resting limit orders NOT yet at the book are left pending and filled
    on the next poll if the market moves to touch them.
  - We do NOT model queue position — treat this as optimistic on maker
    fills; calibrate expectations downward in post-hoc analysis.
  - Fees are applied using the real-fee functions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger
from pm_bot.models import (
    Action,
    Fill,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Venue,
)
from pm_bot.util.math import kalshi_maker_fee, kalshi_taker_fee, polymarket_fee

log = get_logger("paper")


class PaperAdapter(ExchangeAdapter):
    """In-memory simulator wrapping a real adapter's read surface."""

    def __init__(self, inner: ExchangeAdapter, starting_balance: float = 1000.0):
        self._inner = inner
        self.venue = inner.venue
        self._balance = starting_balance
        self._positions: dict[str, Position] = {}  # keyed by ticker
        self._pending_orders: dict[str, Order] = {}
        self._fills: list[Fill] = []

    # ----- read-through to real adapter -----------------------------------

    def list_markets(self, status="open", limit=200, event_id=None) -> list[Market]:
        return self._inner.list_markets(status=status, limit=limit, event_id=event_id)

    def get_market(self, ticker: str) -> Market | None:
        return self._inner.get_market(ticker)

    def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook | None:
        return self._inner.get_orderbook(ticker, depth=depth)

    # ----- simulated trading ---------------------------------------------

    def supports_trading(self) -> bool:
        return True

    def place_order(self, order: Order) -> Order:
        order.exchange_order_id = f"paper-{uuid.uuid4().hex[:8]}"
        order.status = OrderStatus.OPEN
        order.updated_at = datetime.utcnow()

        # Try to immediately match against current book for fills
        book = self._inner.get_orderbook(order.ticker)
        if book:
            self._try_match(order, book)

        if order.status == OrderStatus.OPEN:
            self._pending_orders[order.client_order_id] = order
            log.debug("Paper order resting: %s", order.client_order_id)
        return order

    def cancel_order(self, client_order_id: str) -> bool:
        o = self._pending_orders.pop(client_order_id, None)
        if o:
            o.status = OrderStatus.CANCELED
            return True
        return False

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_balance(self) -> float:
        return self._balance

    def check_pending(self) -> list[Fill]:
        """
        Call periodically from the main loop. For any pending order, if the
        current book would fill it at its limit price, execute the fill.
        Returns any new fills.
        """
        new_fills: list[Fill] = []
        for cid in list(self._pending_orders.keys()):
            order = self._pending_orders[cid]
            book = self._inner.get_orderbook(order.ticker)
            if not book:
                continue
            self._try_match(order, book)
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
                del self._pending_orders[cid]
                # Create Fill record
                f = Fill(
                    venue=self.venue,
                    ticker=order.ticker,
                    order_id=cid,
                    side=order.side,
                    action=order.action,
                    price=order.avg_fill_price,
                    size=order.filled_size,
                    fee=self._fee_for(order),
                )
                new_fills.append(f)
                self._fills.append(f)
        return new_fills

    # ----- internals ------------------------------------------------------

    def _try_match(self, order: Order, book: OrderBook) -> None:
        """
        If order would cross the book, fill it. Very simple sim:
        - BUY YES fills if yes_asks best <= limit.
        - SELL YES fills if yes_bids best >= limit.
        - BUY NO / SELL NO follow the equivalent on NO side.
        Market orders fill at the best opposing price.
        """
        if order.action == Action.BUY and order.side == Side.YES:
            opposing = book.yes_asks
        elif order.action == Action.SELL and order.side == Side.YES:
            opposing = book.yes_bids
        elif order.action == Action.BUY and order.side == Side.NO:
            opposing = book.no_asks
        else:
            opposing = book.no_bids

        if not opposing:
            return

        best = opposing[0]
        if order.order_type == OrderType.MARKET:
            fill_price = best.price
        else:
            # For a BUY, we fill if the best ask <= our limit
            if order.action == Action.BUY:
                if best.price > order.price:
                    return
            else:  # SELL
                if best.price < order.price:
                    return
            fill_price = order.price  # conservative: fill at our limit, not improved

        fillable = min(order.size - order.filled_size, best.size)
        if fillable <= 0:
            return

        cost = fillable * fill_price
        if order.action == Action.BUY:
            if cost > self._balance:
                order.status = OrderStatus.REJECTED
                order.notes = "insufficient paper balance"
                return
            self._balance -= cost
            self._apply_position(order.ticker, order.side, fillable, fill_price)
        else:
            # Selling: reduce position and credit balance
            self._balance += fillable * fill_price
            self._apply_position(order.ticker, order.side, -fillable, fill_price)

        order.filled_size += fillable
        order.avg_fill_price = fill_price
        order.status = (
            OrderStatus.FILLED if order.filled_size >= order.size
            else OrderStatus.PARTIAL
        )

    def _apply_position(self, ticker: str, side: Side, delta: int, price: float) -> None:
        pos = self._positions.get(ticker)
        if pos is None:
            if delta == 0:
                return
            self._positions[ticker] = Position(
                venue=self.venue, ticker=ticker, side=side,
                size=abs(delta), avg_entry_price=price,
            )
            return
        # Update weighted average entry
        if (pos.side == side and delta > 0) or (pos.side != side and delta < 0):
            new_size = pos.size + abs(delta)
            pos.avg_entry_price = (
                (pos.avg_entry_price * pos.size + price * abs(delta)) / new_size
                if new_size > 0 else 0.0
            )
            pos.size = new_size
        else:
            pos.size -= abs(delta)
            if pos.size <= 0:
                del self._positions[ticker]

    def _fee_for(self, order: Order) -> float:
        price = order.avg_fill_price or order.price
        if self.venue == Venue.KALSHI:
            # Treat market orders as taker, limit as maker (optimistic)
            if order.order_type == OrderType.MARKET:
                return kalshi_taker_fee(price, order.filled_size)
            return kalshi_maker_fee(price, order.filled_size)
        if self.venue in (Venue.POLYMARKET, Venue.POLYMARKET_US):
            return polymarket_fee(price, order.filled_size)
        return 0.0

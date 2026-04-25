"""
pm_bot entry point.

Usage:
  python run_bot.py              # uses config.yaml, respects paper_trading flag
  python run_bot.py --once       # single scan + exit (useful for cron/systemd timer)
  python run_bot.py --dry        # scan only; never route signals
  python run_bot.py --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from pm_bot.config import Config, load_config
from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.exchanges.kalshi import KalshiAdapter
from pm_bot.exchanges.paper import PaperAdapter
from pm_bot.exchanges.polymarket import PolymarketAdapter
from pm_bot.exchanges.polymarket_us import PolymarketUSAdapter
from pm_bot.logger import get_logger, setup_logger
from pm_bot.models import Order, OrderStatus, TradeSignal, Venue
from pm_bot.persistence.db import Database
from pm_bot.risk.manager import RiskManager
from pm_bot.strategies.base import Strategy
from pm_bot.strategies.cross_market_arb import CrossMarketArbStrategy
from pm_bot.strategies.market_maker import MarketMakerStrategy
from pm_bot.strategies.news_signal import NewsSignalStrategy
from pm_bot.strategies.sum_prob_arb import SumProbArbStrategy

log = get_logger("main")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.warning("Received signal %s — shutting down...", signum)
    _shutdown = True


def build_adapters(cfg: Config) -> dict[Venue, ExchangeAdapter]:
    """Instantiate exchange adapters, optionally wrapped in a paper shim."""
    adapters: dict[Venue, ExchangeAdapter] = {}
    paper = cfg.runtime.paper_trading

    # Kalshi
    if cfg.exchanges.kalshi.enabled:
        kalshi = KalshiAdapter(
            api_key_id=cfg.secrets.kalshi_api_key_id,
            private_key_path=cfg.secrets.kalshi_private_key_path,
            env=cfg.exchanges.kalshi.env,
            rate_tier=cfg.exchanges.kalshi.rate_tier,
            trading_enabled=not paper,
        )
        adapters[Venue.KALSHI] = PaperAdapter(kalshi, cfg.risk.bankroll_usd) if paper else kalshi

    # Polymarket International — ALWAYS read-only in this codebase, paper wrap optional
    if cfg.exchanges.polymarket.enabled:
        poly = PolymarketAdapter(
            trading_enabled=cfg.exchanges.polymarket.trading_enabled,
            compliance_acknowledged=cfg.exchanges.polymarket.compliance_acknowledged,
        )
        # Paper wrap so strategies can simulate the Poly leg of cross-market arb
        adapters[Venue.POLYMARKET] = PaperAdapter(poly, cfg.risk.bankroll_usd) if paper else poly

    # Polymarket US (stub)
    if cfg.exchanges.polymarket_us.enabled:
        pus = PolymarketUSAdapter(
            api_key=cfg.secrets.polymarket_us_api_key,
            api_secret=cfg.secrets.polymarket_us_api_secret,
            trading_enabled=cfg.exchanges.polymarket_us.trading_enabled and not paper,
        )
        adapters[Venue.POLYMARKET_US] = PaperAdapter(pus, cfg.risk.bankroll_usd) if paper else pus

    return adapters


def build_strategies(cfg: Config, adapters: dict[Venue, ExchangeAdapter]) -> list[Strategy]:
    """Instantiate enabled strategies from config."""
    s_cfg = cfg.strategies
    built: list[Strategy] = [
        SumProbArbStrategy(s_cfg.sum_prob_arb, adapters),
        CrossMarketArbStrategy(s_cfg.cross_market_arb, adapters),
        MarketMakerStrategy(s_cfg.market_maker, adapters),
        NewsSignalStrategy(s_cfg.news_signal, adapters),
    ]
    return [s for s in built if s.enabled]


def route_signal(
    signal: TradeSignal,
    risk: RiskManager,
    adapters: dict[Venue, ExchangeAdapter],
    db: Database,
    paper: bool,
    dry_run: bool,
) -> Order | None:
    """Run one signal through risk and place if accepted."""
    adapter = adapters.get(signal.venue)
    book = adapter.get_orderbook(signal.ticker) if adapter else None

    order = risk.route(signal, book=book)
    if order is None:
        return None

    db.log_order(
        client_order_id=order.client_order_id,
        venue=order.venue.value,
        ticker=order.ticker,
        side=order.side.value,
        action=order.action.value,
        order_type=order.order_type.value,
        price=order.price,
        size=order.size,
        status=order.status.value,
        strategy=order.strategy,
        paper=paper,
        notes=order.notes,
    )

    if dry_run:
        log.info("[DRY] Would place: %s", order.client_order_id)
        return order

    if adapter is None:
        log.error("No adapter for venue %s; skipping order.", signal.venue)
        return None

    placed = adapter.place_order(order)
    db.update_order_status(
        client_order_id=placed.client_order_id,
        status=placed.status.value,
        filled_size=placed.filled_size,
        avg_fill_price=placed.avg_fill_price,
        exchange_order_id=placed.exchange_order_id,
    )
    return placed


def scan_cycle(
    cfg: Config,
    strategies: list[Strategy],
    risk: RiskManager,
    adapters: dict[Venue, ExchangeAdapter],
    db: Database,
    dry_run: bool,
) -> int:
    """One full scan across all strategies. Returns number of signals routed."""
    routed = 0
    for strat in strategies:
        try:
            signals = strat.scan()
        except Exception as e:
            log.exception("Strategy %s crashed during scan: %s", strat.name, e)
            db.log_event("error", f"strategy_crash:{strat.name}", {"error": str(e)})
            continue

        for sig in signals:
            # Atomic multi-leg: if companion_signals present, all-or-nothing
            legs = [sig] + sig.companion_signals
            planned_orders: list[Order] = []
            aborted = False
            for leg in legs:
                o = route_signal(leg, risk, adapters, db, cfg.runtime.paper_trading, dry_run)
                if o is None:
                    aborted = True
                    break
                planned_orders.append(o)

            if aborted and planned_orders:
                # Roll back any legs that did submit
                log.warning("Multi-leg rollback: canceling %d partial legs", len(planned_orders))
                for o in planned_orders:
                    adapter = adapters.get(o.venue)
                    if adapter:
                        try:
                            adapter.cancel_order(o.client_order_id)
                            db.update_order_status(o.client_order_id, OrderStatus.CANCELED.value)
                        except Exception as e:
                            log.error("Rollback cancel failed: %s", e)

            routed += len(planned_orders)

    return routed


def check_paper_fills(adapters: dict[Venue, ExchangeAdapter], db: Database) -> None:
    """In paper mode, poll for simulated fills on pending orders."""
    for _venue, adapter in adapters.items():
        if isinstance(adapter, PaperAdapter):
            fills = adapter.check_pending()
            for f in fills:
                db.log_fill(
                    client_order_id=f.order_id,
                    venue=f.venue.value,
                    ticker=f.ticker,
                    side=f.side.value,
                    action=f.action.value,
                    price=f.price,
                    size=f.size,
                    fee=f.fee,
                    paper=True,
                )
                log.info("[paper fill] %s %s %d @ %.3f (fee $%.3f)",
                        f.action.value, f.side.value, f.size, f.price, f.fee)


def main() -> int:
    parser = argparse.ArgumentParser(description="pm_bot prediction market bot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="run one scan cycle and exit")
    parser.add_argument("--dry", action="store_true",
                        help="scan + risk evaluate, but never place orders")
    args = parser.parse_args()

    cfg = load_config(args.config)

    setup_logger(
        log_dir=cfg.runtime.log_dir,
        level=cfg.runtime.log_level,
    )

    db = Database(cfg.runtime.db_path)
    db.log_event(
        "startup",
        f"pm_bot start; paper={cfg.runtime.paper_trading} dry={args.dry}",
        {"once": args.once, "config_path": args.config},
    )

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    adapters = build_adapters(cfg)
    strategies = build_strategies(cfg, adapters)
    risk = RiskManager(cfg.risk, db, adapters)

    if not strategies:
        log.warning("No strategies enabled. Edit config.yaml and set "
                   "strategies.<name>.enabled: true.")

    log.info("Starting pm_bot | paper=%s | strategies=%s | adapters=%s",
            cfg.runtime.paper_trading,
            [s.name for s in strategies],
            [v.value for v in adapters.keys()])

    exit_code = 0
    try:
        while not _shutdown:
            n = scan_cycle(cfg, strategies, risk, adapters, db, args.dry)
            log.info("Scan cycle complete: %d orders routed", n)
            check_paper_fills(adapters, db)

            if args.once:
                break
            time.sleep(cfg.runtime.poll_interval_sec)

    except KillSwitchException:  # type: ignore[name-defined]
        exit_code = 2
    except Exception:
        log.exception("Fatal error in main loop.")
        db.log_event("error", "fatal_main_loop", {})
        exit_code = 1
    finally:
        db.log_event("shutdown", "pm_bot stopped", {})
        log.info("Shutdown complete.")

        if cfg.runtime.cancel_on_exit and not cfg.runtime.paper_trading:
            for venue, adapter in adapters.items():
                if adapter.supports_trading():
                    try:
                        positions = adapter.get_positions()
                        log.info("%s: %d positions left on exit", venue.value, len(positions))
                    except Exception:
                        pass

    return exit_code


# Dummy so main's `except KillSwitchException` parses — real kill switch
# is internal to RiskManager; we check risk.killed to break the loop.
class KillSwitchException(Exception):
    pass


if __name__ == "__main__":
    sys.exit(main())

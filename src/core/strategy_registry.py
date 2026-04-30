import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.strategies.base import Strategy
    from src.core.events import MarketEvent, SignalEvent

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """
    Routes MarketEvents only to strategies within their active window.

    Blueprint v8 — replaces hardcoded time gates in RiskManager.
    Adding a new strategy = register it here, zero other changes needed.
    """

    def __init__(self):
        self._strategies: list["Strategy"] = []
        logger.info("StrategyRegistry initialised.")

    def register(self, strategy: "Strategy") -> None:
        """Register a strategy. Call once per strategy at engine startup."""
        self._strategies.append(strategy)
        logger.info(f"  Registered: {strategy!r}")

    def active_strategies(self,
                          now: datetime = None) -> list["Strategy"]:
        """Return strategies currently within their active window."""
        if now is None:
            now = datetime.now()
        return [s for s in self._strategies if s.is_active(now)]

    def strategies_for_symbol(self, symbol: str,
                               now: datetime = None) -> list["Strategy"]:
        """Return active strategies for a specific symbol."""
        return [s for s in self.active_strategies(now)
                if s.symbol == symbol]

    def dispatch(self, event: "MarketEvent") -> None:
        """
        Dispatch MarketEvent to all active strategies for that symbol.
        Strategies outside their active window receive nothing.
        """
        now = datetime.now()
        for strategy in self.strategies_for_symbol(event.symbol, now):
            try:
                strategy.on_tick(event)
            except Exception as e:
                logger.error(f"  Strategy error {strategy.name} "
                            f"on {event.symbol}: {e}")

    def register_symbol(self, symbol: str,
                         strategies: list["Strategy"]) -> None:
        """Convenience: register multiple strategies for one symbol."""
        for s in strategies:
            self.register(s)

    def all_strategies(self) -> list["Strategy"]:
        return list(self._strategies)

    def summary(self) -> str:
        lines = [f"StrategyRegistry — {len(self._strategies)} strategies:"]
        for s in self._strategies:
            lines.append(f"  {s!r}")
        return "\n".join(lines)
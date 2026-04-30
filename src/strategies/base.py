from abc import ABC, abstractmethod
from datetime import datetime, time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.events import MarketEvent, SignalEvent

import logging
logger = logging.getLogger(__name__)


class Strategy(ABC):
    """
    Abstract base class for all Morning Beetle strategies.

    Every strategy must declare:
    - name:          unique identifier
    - active_window: (start_time, end_time) when strategy accepts signals
    - loss_bucket:   'morning' or 'post_morning' for risk bucketing
    - sl_pct:        stop loss percentage
    - target_pct:    profit target percentage

    Blueprint v8 — multi-window strategy framework.
    """

    # ── Subclasses must define these ─────────────────────────────────
    name:          str
    active_window: tuple[time, time]
    loss_bucket:   str    # 'morning' or 'post_morning'
    sl_pct:        float
    target_pct:    float

    def __init__(self, engine, symbol: str,
                 sentiment_score: float = 0.0):
        self.engine          = engine
        self.symbol          = symbol
        self.sentiment_score = sentiment_score
        self._tick_count     = 0

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """
        Returns True if current time is within strategy's active window.
        Used by RiskManager and StrategyRegistry to gate signal dispatch.
        """
        if now is None:
            now = datetime.now()
        current = now.time()
        start, end = self.active_window
        return start <= current < end

    @abstractmethod
    def on_tick(self, event: "MarketEvent") -> None:
        """
        Process a market tick. Emit SignalEvent to EventBus if conditions met.
        Called on every tick for subscribed symbols during active window.
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """
        Reset intraday state. Called at engine startup each morning.
        """
        pass

    def __repr__(self) -> str:
        start, end = self.active_window
        return (f"{self.__class__.__name__}("
                f"symbol={self.symbol}, "
                f"window={start.strftime('%H:%M')}-{end.strftime('%H:%M')}, "
                f"bucket={self.loss_bucket})")
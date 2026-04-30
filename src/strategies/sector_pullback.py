import logging
from datetime import time
from collections import deque
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent
from src.core.indicators import VWAPCalculator

logger = logging.getLogger(__name__)

# S3 Parameters per Blueprint v8
VWAP_TOUCH_TOLERANCE   = 0.002   # Price within 0.2% of VWAP = "touch"
VOLUME_CONFIRM_MULT    = 1.2     # Bounce volume must be > 1.2x 30-min avg
BOUNCE_CONFIRM_PCT     = 0.001   # Price must close 0.1% above VWAP (long)
MIN_TICKS_FOR_AVG      = 10      # Min ticks before volume avg is reliable
SL_PCT                 = 0.005   # 0.5% stop loss
TARGET_PCT             = 0.010   # 1.0% target


class SectorLeaderPullback(Strategy):
    """
    Strategy S3 — Sector Leader Pullback

    Thesis: A stock that gapped up on morning news and held above VWAP
    is a sector leader. When it pulls back to VWAP in post-morning
    consolidation, it offers a high-probability re-entry with tight risk.

    Active window: 10:30 AM – 12:00 PM
    Loss bucket:   post_morning
    """

    name          = "sector_pullback"
    active_window = (time(10, 30), time(12, 0))
    loss_bucket   = "post_morning"
    sl_pct        = SL_PCT
    target_pct    = TARGET_PCT

    def __init__(self, engine, symbol: str,
                 sentiment_score: float = 0.0):
        super().__init__(engine, symbol, sentiment_score)

        self.vwap         = VWAPCalculator(symbol)
        self._prices      = deque(maxlen=50)
        self._volumes     = deque(maxlen=30)   # 30-min volume window
        self._tick_count  = 0

        # State machine
        self._above_vwap      = False   # Was price above VWAP?
        self._touching_vwap   = False   # Is price near VWAP now?
        self._touch_low       = None    # Lowest price during VWAP touch
        self._signal_fired    = False   # One signal per session

        logger.info(f"  SectorLeaderPullback initialised for {symbol}")

    def on_tick(self, event: MarketEvent) -> None:
        """Process tick — evaluate VWAP pullback conditions."""
        ltp    = event.ltp
        volume = event.volume if hasattr(event, 'volume') and event.volume else 0

        # Update VWAP
        current_vwap = self.vwap.update(ltp, max(volume, 1))
        if current_vwap is None:
            return

        self._prices.append(ltp)
        if volume > 0:
            self._volumes.append(volume)
        self._tick_count += 1

        # Need minimum data
        if self._tick_count < MIN_TICKS_FOR_AVG:
            return

        # One signal per session
        if self._signal_fired:
            return

        distance_pct = abs(ltp - current_vwap) / current_vwap

        # Phase 1 — Confirm stock was above VWAP (sector leader)
        if not self._above_vwap:
            if ltp > current_vwap * (1 + VWAP_TOUCH_TOLERANCE):
                self._above_vwap = True
            return   # Wait until confirmed leader

        # Phase 2 — Detect VWAP touch (pullback)
        if not self._touching_vwap:
            if distance_pct <= VWAP_TOUCH_TOLERANCE:
                self._touching_vwap = True
                self._touch_low     = ltp
                logger.debug(f"  S3 {self.symbol}: VWAP touch detected "
                            f"@ {ltp:.2f} (VWAP={current_vwap:.2f})")
            return

        # Track lowest price during touch
        if ltp < self._touch_low:
            self._touch_low = ltp

        # If price falls too far below VWAP — not a touch, reset
        if ltp < current_vwap * (1 - VWAP_TOUCH_TOLERANCE * 2):
            self._touching_vwap = False
            self._touch_low     = None
            logger.debug(f"  S3 {self.symbol}: VWAP touch failed — price broke below")
            return

        # Phase 3 — Confirm bounce above VWAP with volume
        bounced = ltp > current_vwap * (1 + BOUNCE_CONFIRM_PCT)
        if not bounced:
            return

        # Volume confirmation
        avg_volume = sum(self._volumes) / len(self._volumes) if self._volumes else 0
        volume_ok  = volume > avg_volume * VOLUME_CONFIRM_MULT if avg_volume > 0 else False

        if not volume_ok:
            logger.debug(f"  S3 {self.symbol}: bounce seen but volume weak "
                        f"({volume:.0f} vs avg {avg_volume:.0f})")
            return

        # ✅ All conditions met — emit BUY signal
        self._signal_fired = True
        logger.info(f"  🎯 S3 SIGNAL: BUY {self.symbol} @ {ltp:.2f} "
                   f"[VWAP={current_vwap:.2f}, vol={volume:.0f} "
                   f"vs avg={avg_volume:.0f}]")

        signal = SignalEvent(
            symbol         = self.symbol,
            direction      = "BUY",
            strategy_name  = self.name,
            sentiment_score= self.sentiment_score,
            ltp            = ltp
        )
        self.engine.emit_event(signal)

    def reset(self) -> None:
        """Reset for new trading day."""
        self.vwap.reset()
        self._prices.clear()
        self._volumes.clear()
        self._tick_count    = 0
        self._above_vwap    = False
        self._touching_vwap = False
        self._touch_low     = None
        self._signal_fired  = False
import logging
from datetime import time
from collections import deque
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent
from src.core.indicators import ATRCalculator

logger = logging.getLogger(__name__)

# S4 Parameters per Blueprint v8
CONTRACTION_THRESHOLD  = 0.50    # Afternoon ATR < 50% of morning ATR
VOLUME_CONFIRM_MULT    = 1.5     # Breakout volume > 1.5x contraction avg
BREAKOUT_BUFFER        = 0.001   # 0.1% buffer above consolidation high
MIN_CONSOLIDATION_TICKS= 20      # Min ticks to establish consolidation range
SL_PCT                 = 0.008   # Below consolidation low (approx 0.8%)
TARGET_PCT             = 0.012   # 1.2% target


class VolatilityContractionBreakout(Strategy):
    """
    Strategy S4 — Volatility Contraction Breakout

    Thesis: After lunch, stocks coiling in tight ranges often break out
    as institutional flow returns. Combined with sector strength,
    these breakouts have asymmetric R:R.

    Active window: 13:30 PM – 14:30 PM
    Loss bucket:   post_morning
    """

    name          = "vol_contraction"
    active_window = (time(13, 30), time(14, 30))
    loss_bucket   = "post_morning"
    sl_pct        = SL_PCT
    target_pct    = TARGET_PCT

    def __init__(self, engine, symbol: str,
                 sentiment_score: float = 0.0):
        super().__init__(engine, symbol, sentiment_score)

        self.atr_morning     = ATRCalculator(symbol, period=14)
        self.atr_afternoon   = ATRCalculator(symbol, period=14)

        self._consolidation_prices = deque(maxlen=60)
        self._consolidation_volumes = deque(maxlen=60)
        self._morning_atr_value  = None
        self._signal_fired       = False
        self._tick_count         = 0

        logger.info(f"  VolatilityContractionBreakout initialised for {symbol}")

    def on_tick(self, event: MarketEvent) -> None:
        """Process tick — look for tight consolidation + breakout."""
        ltp    = event.ltp
        high   = event.high if hasattr(event, 'high') else ltp
        low    = event.low  if hasattr(event, 'low')  else ltp
        volume = event.volume if hasattr(event, 'volume') and event.volume else 0

        self._tick_count += 1

        # Store morning ATR (09:15-10:30) for comparison
        from datetime import datetime
        now = datetime.now().time()
        if time(9, 15) <= now < time(10, 30):
            result = self.atr_morning.update(high, low, ltp)
            if result:
                self._morning_atr_value = result
            return

        # One signal per session
        if self._signal_fired:
            return

        # Build consolidation range during afternoon window
        self._consolidation_prices.append(ltp)
        if volume > 0:
            self._consolidation_volumes.append(volume)

        if len(self._consolidation_prices) < MIN_CONSOLIDATION_TICKS:
            return

        # Check volatility contraction
        atr_now = self.atr_afternoon.update(high, low, ltp)
        if atr_now is None or self._morning_atr_value is None:
            return

        contraction_ratio = atr_now / self._morning_atr_value
        if contraction_ratio >= CONTRACTION_THRESHOLD:
            return   # Not contracted enough

        # Define consolidation range
        prices = list(self._consolidation_prices)
        consol_high = max(prices)
        consol_low  = min(prices)

        # Breakout trigger — price breaks above consolidation high
        breakout_level = consol_high * (1 + BREAKOUT_BUFFER)
        if ltp < breakout_level:
            return

        # Volume confirmation
        avg_vol = (sum(self._consolidation_volumes) /
                   len(self._consolidation_volumes)
                   if self._consolidation_volumes else 0)
        if avg_vol > 0 and volume < avg_vol * VOLUME_CONFIRM_MULT:
            logger.debug(f"  S4 {self.symbol}: breakout seen but volume weak")
            return

        # ✅ All conditions met — emit BUY signal
        self._signal_fired = True
        logger.info(f"  🎯 S4 SIGNAL: BUY {self.symbol} @ {ltp:.2f} "
                   f"[contraction={contraction_ratio:.2f}, "
                   f"range={consol_low:.2f}-{consol_high:.2f}]")

        signal = SignalEvent(
            symbol         = self.symbol,
            direction      = "BUY",
            strategy_name  = self.name,
            sentiment_score= self.sentiment_score,
            ltp            = ltp
        )
        self.engine.emit_event(signal)

    def reset(self) -> None:
        self.atr_morning.reset()
        self.atr_afternoon.reset()
        self._consolidation_prices.clear()
        self._consolidation_volumes.clear()
        self._morning_atr_value = None
        self._signal_fired      = False
        self._tick_count        = 0
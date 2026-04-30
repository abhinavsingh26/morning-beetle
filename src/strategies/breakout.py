import logging
import numpy as np
import pandas as pd
import talib
from datetime import datetime, time
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent

logger = logging.getLogger(__name__)

# Strategy parameters — tuned for NSE intraday
BREAKOUT_BUFFER    = 0.001   # 0.1% false breakout buffer
ATR_MIN_PCT        = 0.003   # Reduced from 0.5% to 0.3% — less strict
VOLUME_MULT        = 1.2     # Reduced from 1.5x to 1.2x — less strict
ATR_PERIOD         = 14
REFERENCE_CANDLE_END = time(9, 30)  # 15-min candle: 09:15–09:30


class MorningBreakout(Strategy):
    """
    Strategy S1 — Morning Breakout.
    Captures directional move after first 15-min candle consolidation.

    BUY  trigger: price breaks above Reference High + 0.1% buffer
    SELL trigger: price breaks below Reference Low  - 0.1% buffer
    Filters: ATR(14) > 0.3% of price, Volume > 1.2x 5-day avg

    Active window: 09:30 AM – 10:30 AM
    Loss bucket:   morning
    """

    name          = "morning_breakout"
    active_window = (time(9, 30), time(10, 30))
    loss_bucket   = "morning"
    sl_pct        = 0.008    # 0.8% stop loss per Blueprint v8
    target_pct    = 0.015    # 1.5% target per Blueprint v8

    def __init__(self, engine, symbol: str, sentiment_score: float = 0.0):
        super().__init__(engine, symbol, sentiment_score)
        self.strategy_name = "MorningBreakout"   # backward compat for SignalEvent

        # Reference candle (09:15–09:30)
        self.ref_high   = None
        self.ref_low    = None
        self.ref_set    = False

        # Candle data storage
        self.candles: list[dict] = []
        self.signal_fired = False   # Only fire once per day

        logger.info(f"MorningBreakout initialised for {symbol}")

    def _build_candle(self, tick: MarketEvent) -> dict:
        return {
            "time":   tick.timestamp,
            "open":   tick.open,
            "high":   tick.high,
            "low":    tick.low,
            "close":  tick.ltp,
            "volume": tick.volume
        }

    def _set_reference_candle(self, candle: dict):
        """Lock in the 09:15–09:30 reference candle range."""
        self.ref_high = candle["high"] * (1 + BREAKOUT_BUFFER)
        self.ref_low  = candle["low"]  * (1 - BREAKOUT_BUFFER)
        self.ref_set  = True
        logger.info(f"  {self.symbol} Reference candle set — "
                    f"High: {self.ref_high:.2f} Low: {self.ref_low:.2f}")

    def _check_atr_filter(self, closes: np.ndarray,
                           highs: np.ndarray,
                           lows: np.ndarray,
                           ltp: float) -> bool:
        """ATR(14) must be > 0.3% of current price."""
        if len(closes) < ATR_PERIOD + 1:
            logger.debug(f"  ATR SKIP: not enough candles ({len(closes)} < {ATR_PERIOD + 1})")
            return False
        atr = talib.ATR(highs, lows, closes, timeperiod=ATR_PERIOD)
        latest_atr = atr[-1]
        atr_pct = latest_atr / ltp
        passes = atr_pct > ATR_MIN_PCT
        logger.debug(f"  ATR: {latest_atr:.2f} = {atr_pct:.4f} of price — {'PASS' if passes else 'FAIL'}")
        return passes

    def _check_volume_filter(self, volumes: np.ndarray) -> bool:
        """Current volume must be > 1.2x 5-day average."""
        if len(volumes) < 6:
            logger.debug(f"  VOL SKIP: not enough candles ({len(volumes)} < 6)")
            return False
        avg_volume  = np.mean(volumes[-6:-1])
        current_vol = volumes[-1]
        if avg_volume == 0:
            return False
        passes = current_vol > avg_volume * VOLUME_MULT
        logger.debug(f"  VOL: {current_vol} vs avg {avg_volume:.0f} x{VOLUME_MULT} = {avg_volume*VOLUME_MULT:.0f} — {'PASS' if passes else 'FAIL'}")
        return passes

    def on_tick(self, event: MarketEvent) -> None:
        """
        Called on every MarketEvent for this symbol.
        Builds candle history and evaluates breakout conditions.
        """
        if event.symbol != self.symbol:
            return
        if self.signal_fired:
            return

        candle = self._build_candle(event)
        self.candles.append(candle)

        tick_time = event.timestamp.time()

        # Set reference candle from first tick at/after 09:30
        if not self.ref_set:
            if tick_time >= REFERENCE_CANDLE_END:
                # Use the accumulated candle range, not just this tick
                highs  = [c["high"] for c in self.candles]
                lows   = [c["low"]  for c in self.candles]
                ref_candle = {
                    "high": max(highs) if highs else candle["high"],
                    "low":  min(lows)  if lows  else candle["low"]
                }
                self._set_reference_candle(ref_candle)
            return   # Don't evaluate signals until ref is set

        if not self.ref_set:
            return

        # Need enough candles for indicators
        if len(self.candles) < ATR_PERIOD + 2:
            return

        df = pd.DataFrame(self.candles)
        closes  = df["close"].values.astype(float)
        highs   = df["high"].values.astype(float)
        lows    = df["low"].values.astype(float)
        volumes = df["volume"].values.astype(float)
        ltp     = event.ltp

        # Check filters
        if not self._check_atr_filter(closes, highs, lows, ltp):
            return
        if not self._check_volume_filter(volumes):
            return

        # Check breakout
        direction = None
        if ltp > self.ref_high:
            direction = "BUY"
        elif ltp < self.ref_low:
            direction = "SELL"

        if direction:
            self.signal_fired = True
            signal = SignalEvent(
                symbol         = self.symbol,
                direction      = direction,
                strategy_name  = self.strategy_name,
                sentiment_score = self.sentiment_score,
                ltp            = ltp
            )
            self.engine.emit_event(signal)
            logger.info(f"  🚀 SIGNAL: {direction} {self.symbol} @ {ltp:.2f} "
                       f"[Breakout above {self.ref_high:.2f}]")

    def reset(self) -> None:
        """Reset for new trading day."""
        self.ref_high     = None
        self.ref_low      = None
        self.ref_set      = False
        self.candles      = []
        self.signal_fired = False


if __name__ == "__main__":
    import random
    import time as time_module
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    from src.core.engine import TradingEngine

    engine  = TradingEngine(is_paper_trading=True)
    signals = []

    def on_signal(event: SignalEvent):
        signals.append(event)
        print(f"\n  🚀 SIGNAL CAPTURED: {event.direction} {event.symbol} "
              f"@ {event.ltp:.2f}")

    engine.register_handler("SIGNAL", on_signal)
    engine.run_in_thread()

    strategy = MorningBreakout(engine, "INFY", sentiment_score=-0.963)

    print("── MorningBreakout Backtest Simulation ──\n")
    print(f"Strategy: {strategy}")
    print(f"  active_window: {strategy.active_window}")
    print(f"  loss_bucket:   {strategy.loss_bucket}\n")

    # Phase 1: Build 30 pre-reference candles
    print("Phase 1: Building pre-reference history (30 candles)...")
    base = 1850.0
    for i in range(30):
        noise = random.uniform(-8, 8)
        ltp   = base + noise
        hour   = 9
        minute = 15 + (i % 14)
        tick = MarketEvent(
            timestamp = datetime(2026, 4, 22, hour, minute, 0),
            symbol    = "INFY",
            instrument_token = 408065,
            ltp    = ltp,
            open   = base - 5,
            high   = ltp + random.uniform(3, 12),
            low    = ltp - random.uniform(3, 12),
            close  = ltp,
            volume = random.randint(40000, 80000)
        )
        strategy.on_tick(tick)

    # Phase 2: Trigger reference candle lock at 09:31
    print("Phase 2: Locking reference candle at 09:31...")
    ref_tick = MarketEvent(
        timestamp = datetime(2026, 4, 22, 9, 31, 0),
        symbol    = "INFY",
        instrument_token = 408065,
        ltp=1850.0, open=1840.0,
        high=1862.0, low=1838.0,
        close=1850.0, volume=80000
    )
    strategy.on_tick(ref_tick)
    print(f"  Reference High: {strategy.ref_high:.2f}  Low: {strategy.ref_low:.2f}")

    # Phase 3: Breakout ticks
    print("Phase 3: Simulating breakout above reference high...")
    for i in range(20):
        ltp  = base + (i * 1.5)
        hour   = 9 + (32 + i) // 60
        minute = (32 + i) % 60
        vol = 300000 if ltp > strategy.ref_high else random.randint(40000, 80000)
        tick = MarketEvent(
            timestamp = datetime(2026, 4, 22, hour, minute, 0),
            symbol    = "INFY",
            instrument_token = 408065,
            ltp    = ltp,
            open   = base,
            high   = ltp + 3,
            low    = base - 2,
            close  = ltp,
            volume = vol
        )
        strategy.on_tick(tick)
        if signals:
            break

    time_module.sleep(0.5)
    engine.stop()

    print(f"\n── Results ──")
    print(f"  Reference High : {strategy.ref_high:.2f}")
    print(f"  Reference Low  : {strategy.ref_low:.2f}")
    print(f"  Signals fired  : {len(signals)}")
    if signals:
        s = signals[0]
        print(f"  Direction      : {s.direction}")
        print(f"  LTP at signal  : {s.ltp:.2f}")
        print(f"\n✅ MorningBreakout v8 (inherits Strategy ABC) working.")
    else:
        print(f"\n⚠️  No signal — price may not have crossed ref_high.")

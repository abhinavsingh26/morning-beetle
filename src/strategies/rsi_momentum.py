import logging
import numpy as np
import pandas as pd
import talib
from datetime import datetime, time
from src.core.events import MarketEvent, SignalEvent

logger = logging.getLogger(__name__)

# Strategy parameters per Blueprint
RSI_PERIOD    = 14
ADX_PERIOD    = 14
RSI_BUY       = 55    # RSI crosses above 55 → BUY
RSI_SELL      = 45    # RSI crosses below 45 → SELL
ADX_MIN       = 25    # ADX must be ≥ 25 (trend must be strong)
CANDLE_INTERVAL = 5   # 5-minute candles


class RSIMomentum:
    """
    Strategy 2 — RSI + ADX Intraday Momentum.
    Rides established intraday trends confirmed by both
    momentum (RSI) and trend strength (ADX).

    BUY  trigger: RSI(14) crosses above 55 AND ADX(14) ≥ 25
    SELL trigger: RSI(14) crosses below 45 AND ADX(14) ≥ 25
    """

    def __init__(self, engine, symbol: str, sentiment_score: float = 0.0):
        self.engine          = engine
        self.symbol          = symbol
        self.sentiment_score = sentiment_score
        self.strategy_name   = "RSIMomentum"

        self.candles: list[dict] = []
        self.signal_fired = False
        self.prev_rsi     = None   # Track previous RSI for crossover detection

        logger.info(f"RSIMomentum initialised for {symbol}")

    def _build_candle(self, tick: MarketEvent) -> dict:
        return {
            "time":   tick.timestamp,
            "open":   tick.open,
            "high":   tick.high,
            "low":    tick.low,
            "close":  tick.ltp,
            "volume": tick.volume
        }

    def _check_adx_filter(self, highs: np.ndarray,
                           lows: np.ndarray,
                           closes: np.ndarray) -> bool:
        """ADX(14) must be ≥ 25 — trend must be strong, not ranging."""
        if len(closes) < ADX_PERIOD + 1:
            logger.debug(f"  ADX SKIP: not enough candles ({len(closes)})")
            return False
        adx = talib.ADX(highs, lows, closes, timeperiod=ADX_PERIOD)
        latest_adx = adx[-1]
        passes = latest_adx >= ADX_MIN
        logger.debug(f"  ADX: {latest_adx:.2f} ≥ {ADX_MIN} — {'PASS' if passes else 'FAIL'}")
        return passes

    def on_tick(self, event: MarketEvent):
        """
        Called on every MarketEvent for this symbol.
        Evaluates RSI crossover with ADX confirmation.
        """
        if event.symbol != self.symbol:
            return
        if self.signal_fired:
            return

        candle = self._build_candle(event)
        self.candles.append(candle)

        # Need enough candles for RSI(14) + ADX(14)
        if len(self.candles) < RSI_PERIOD + ADX_PERIOD + 2:
            return

        df      = pd.DataFrame(self.candles)
        closes  = df["close"].values.astype(float)
        highs   = df["high"].values.astype(float)
        lows    = df["low"].values.astype(float)

        # Calculate RSI
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)
        current_rsi = rsi[-1]
        prev_rsi    = rsi[-2]

        if np.isnan(current_rsi) or np.isnan(prev_rsi):
            return

        # Check ADX filter first
        if not self._check_adx_filter(highs, lows, closes):
            self.prev_rsi = current_rsi
            return

        # Detect RSI crossover
        direction = None
        if prev_rsi < RSI_BUY <= current_rsi:
            direction = "BUY"
        elif prev_rsi > RSI_SELL >= current_rsi:
            direction = "SELL"

        if direction:
            self.signal_fired = True
            signal = SignalEvent(
                symbol          = self.symbol,
                direction       = direction,
                strategy_name   = self.strategy_name,
                sentiment_score = self.sentiment_score,
                ltp             = event.ltp
            )
            self.engine.emit_event(signal)
            logger.info(f"  🚀 SIGNAL: {direction} {self.symbol} @ {event.ltp:.2f} "
                       f"[RSI {prev_rsi:.1f} → {current_rsi:.1f}, "
                       f"crossed {'above 55' if direction == 'BUY' else 'below 45'}]")

        self.prev_rsi = current_rsi

    def reset(self):
        """Reset for new trading day."""
        self.candles      = []
        self.signal_fired = False
        self.prev_rsi     = None


if __name__ == "__main__":
    import random
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    from src.core.engine import TradingEngine

    engine  = TradingEngine(is_paper_trading=True)
    signals = []

    def on_signal(event: SignalEvent):
        signals.append(event)
        print(f"\n  🚀 SIGNAL CAPTURED: {event.direction} {event.symbol} "
              f"@ {event.ltp:.2f} via {event.strategy_name}")

    engine.register_handler("SIGNAL", on_signal)
    engine.run_in_thread()

    strategy = RSIMomentum(engine, "INFY", sentiment_score=-0.963)

    print("── RSIMomentum Backtest Simulation ──\n")

    # Phase 1: Build 40 candles with bearish trend (RSI < 45)
    # then flip to bullish to trigger RSI crossover above 55
    print("Phase 1: Building bearish history (RSI < 45)...")
    base = 1850.0
    for i in range(35):
        # Slowly declining price — builds bearish RSI
        ltp  = base - (i * 0.8) + random.uniform(-2, 2)
        hour   = 9 + (15 + i * 5) // 60
        minute = (15 + i * 5) % 60
        tick = MarketEvent(
            timestamp = datetime(2026, 4, 22, hour, minute, 0),
            symbol    = "INFY",
            instrument_token = 408065,
            ltp    = ltp,
            open   = ltp - 1,
            high   = ltp + random.uniform(2, 8),
            low    = ltp - random.uniform(2, 8),
            close  = ltp,
            volume = random.randint(50000, 100000)
        )
        strategy.on_tick(tick)

    print(f"  Candles built: {len(strategy.candles)}")

    # Phase 2: Strong bullish reversal — RSI should cross above 55
    print("Phase 2: Bullish reversal — RSI crossing above 55...")
    reversal_base = base - (34 * 0.8)   # Price where bearish phase ended
    for i in range(20):
        # Strong rising candles to push RSI above 55
        ltp  = reversal_base + (i * 3.5)
        hour   = 9 + (15 + (35 + i) * 5) // 60
        minute = (15 + (35 + i) * 5) % 60
        tick = MarketEvent(
            timestamp = datetime(2026, 4, 22, hour, minute, 0),
            symbol    = "INFY",
            instrument_token = 408065,
            ltp    = ltp,
            open   = ltp - 2,
            high   = ltp + random.uniform(3, 10),
            low    = ltp - random.uniform(1, 4),
            close  = ltp,
            volume = random.randint(80000, 150000)
        )
        strategy.on_tick(tick)
        if signals:
            break

    time.sleep(0.5)
    engine.stop()

    print(f"\n── Results ──")
    print(f"  Total candles  : {len(strategy.candles)}")
    print(f"  Signals fired  : {len(signals)}")
    if signals:
        s = signals[0]
        print(f"  Direction      : {s.direction}")
        print(f"  Symbol         : {s.symbol}")
        print(f"  LTP at signal  : {s.ltp:.2f}")
        print(f"\n✅ RSIMomentum strategy working.")
    else:
        # Show final RSI for debugging
        df = pd.DataFrame(strategy.candles)
        closes = df["close"].values.astype(float)
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)
        print(f"  Final RSI: {rsi[-1]:.2f}")
        print(f"  ⚠️  No signal fired — RSI may not have crossed 55.")
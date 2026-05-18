"""
RSIMomentum strategy (S2) — v9.4 with Supertrend regime filter.

v9.4 change (Day 11 EOD, May 18 2026):
    Day 11 showed 0W/4L (−₹493) on this strategy alone. Analysis of Stage 5a
    (28 trades) revealed:
      - 10–30 min after open: 18 trades, 27.8% win rate, −₹700 total
      - 30–60 min after open:  6 trades, 83.3% win rate, +₹1,062 total
      - SL exits: 16/16 losses (0% win)
      - TRAIL exits: 7/7 wins (100% win)
    RSI(14)/ADX(14) crossover frequently fires AFTER the move has played out
    on choppy/mean-reverting days, catching the reversal tail.

    Fix: Supertrend(10, 3) regime filter as a third gate.
      - BUY only fires when Supertrend is GREEN (uptrend in progress)
      - SELL only fires when Supertrend is RED (downtrend in progress)
    Expected effect: filters out ~50-60% of entries on choppy days,
    preserves nearly all trending-day entries.
    Trade count drops, per-trade win rate rises.
"""
import logging
import numpy as np
import pandas as pd
import talib
from datetime import datetime, time
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent
from src.core.indicators import compute_supertrend

logger = logging.getLogger(__name__)

# Strategy parameters per Blueprint
RSI_PERIOD    = 14
ADX_PERIOD    = 14
RSI_BUY       = 55    # RSI crosses above 55 → BUY
RSI_SELL      = 45    # RSI crosses below 45 → SELL
ADX_MIN       = 25    # ADX must be ≥ 25 (trend must be strong)
CANDLE_INTERVAL = 5   # 5-minute candles
SOFT_OPEN_GATE = time(9, 20)   # No RSI signals before 09:20 AM

# v9.4 — Supertrend regime filter
USE_SUPERTREND_FILTER = True
SUPERTREND_PERIOD     = 10
SUPERTREND_MULTIPLIER = 3.0


class RSIMomentum(Strategy):
    """
    Strategy S2 — RSI + ADX Intraday Momentum.
    Rides established intraday trends confirmed by both
    momentum (RSI) and trend strength (ADX).

    BUY  trigger: RSI(14) crosses above 55 AND ADX(14) ≥ 25
    SELL trigger: RSI(14) crosses below 45 AND ADX(14) ≥ 25

    Active window: 09:30 AM – 10:30 AM
    Loss bucket:   morning
    """

    name          = "rsi_momentum"
    active_window = (time(9, 30), time(10, 30))
    loss_bucket   = "morning"
    sl_pct        = 0.008    # 0.8% stop loss per Blueprint v8
    target_pct    = 0.015    # 1.5% target per Blueprint v8

    def __init__(self, engine, symbol: str, sentiment_score: float = 0.0):
        super().__init__(engine, symbol, sentiment_score)
        self.strategy_name = "RSIMomentum"   # backward compat for SignalEvent

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

    def _check_supertrend_filter(self, direction: str,
                                  highs: np.ndarray,
                                  lows: np.ndarray,
                                  closes: np.ndarray) -> tuple[bool, str]:
        """
        v9.4 — Supertrend regime filter.

        Returns (pass, regime_label) tuple.
          - BUY signal needs GREEN (uptrend regime, direction=+1)
          - SELL signal needs RED (downtrend regime, direction=-1)

        If insufficient data → fail-open (pass=True) with note "INSUFFICIENT".
        Insufficient data only happens in the first 10 candles, by which
        time the strategy hasn't fired yet anyway (RSI needs ~15 candles).
        """
        if not USE_SUPERTREND_FILTER:
            return True, "DISABLED"
        if len(closes) < SUPERTREND_PERIOD + 2:
            return True, "INSUFFICIENT"
        _, dir_arr = compute_supertrend(
            highs, lows, closes,
            period=SUPERTREND_PERIOD,
            multiplier=SUPERTREND_MULTIPLIER
        )
        latest_dir = dir_arr[-1]
        if np.isnan(latest_dir):
            return True, "INSUFFICIENT"

        regime = "GREEN" if latest_dir == 1 else "RED"
        if direction == "BUY":
            passes = (regime == "GREEN")
        else:  # SELL
            passes = (regime == "RED")
        logger.debug(f"  Supertrend: regime={regime}, want={'GREEN' if direction=='BUY' else 'RED'} "
                    f"— {'PASS' if passes else 'BLOCKED'}")
        return passes, regime

    def on_tick(self, event: MarketEvent) -> None:
        """
        Called on every MarketEvent for this symbol.
        Evaluates RSI crossover with ADX confirmation.
        """
        if event.symbol != self.symbol:
            return
        if self.signal_fired:
            return

        # Soft gate — no signals before 09:20
        if event.timestamp.time() < SOFT_OPEN_GATE:
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
            # v9.4 — Supertrend regime gate (third filter after RSI + ADX)
            st_pass, regime = self._check_supertrend_filter(
                direction, highs, lows, closes
            )
            if not st_pass:
                logger.info(
                    f"  ❌ FILTERED {direction} {self.symbol}: "
                    f"Supertrend regime={regime}, expected "
                    f"{'GREEN' if direction == 'BUY' else 'RED'} "
                    f"(RSI {prev_rsi:.1f}→{current_rsi:.1f})"
                )
                self.prev_rsi = current_rsi
                return

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
                       f"crossed {'above 55' if direction == 'BUY' else 'below 45'}, "
                       f"Supertrend {regime}]")

        self.prev_rsi = current_rsi

    def reset(self) -> None:
        """Reset for new trading day."""
        self.candles      = []
        self.signal_fired = False
        self.prev_rsi     = None


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
              f"@ {event.ltp:.2f} via {event.strategy_name}")

    engine.register_handler("SIGNAL", on_signal)
    engine.run_in_thread()

    strategy = RSIMomentum(engine, "INFY", sentiment_score=-0.963)

    print("── RSIMomentum Backtest Simulation ──\n")
    print(f"Strategy: {strategy}")
    print(f"  active_window: {strategy.active_window}")
    print(f"  loss_bucket:   {strategy.loss_bucket}\n")

    # Phase 1: Build 35 candles with bearish trend
    print("Phase 1: Building bearish history (RSI < 45)...")
    base = 1850.0
    for i in range(35):
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

    # Phase 2: Bullish reversal
    print("Phase 2: Bullish reversal — RSI crossing above 55...")
    reversal_base = base - (34 * 0.8)
    for i in range(20):
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

    time_module.sleep(0.5)
    engine.stop()

    print(f"\n── Results ──")
    print(f"  Total candles  : {len(strategy.candles)}")
    print(f"  Signals fired  : {len(signals)}")
    if signals:
        s = signals[0]
        print(f"  Direction      : {s.direction}")
        print(f"  LTP at signal  : {s.ltp:.2f}")
        print(f"\n✅ RSIMomentum v8 (inherits Strategy ABC) working.")
    else:
        df = pd.DataFrame(strategy.candles)
        closes = df["close"].values.astype(float)
        rsi = talib.RSI(closes, timeperiod=RSI_PERIOD)
        print(f"  Final RSI: {rsi[-1]:.2f}")
        print(f"  ⚠️  No signal — RSI may not have crossed 55.")

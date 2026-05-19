"""
RSIMomentum strategy (S2) — v9.5 with CandleAggregator + Historical Seeding.

v9.5 change (Day 12 EOD, May 19 2026)
======================================
Day 12 confirmed via TradingView comparison that the engine's Supertrend
was seeing RED while TradingView showed GREEN — because the engine was
treating every tick as a separate "candle" instead of aggregating ticks
into proper 5-min OHLCV bars.

Two fixes shipped together in v9.5:

1. CandleAggregator (src/core/candle_aggregator.py):
   - Bins streaming ticks into proper 5-min OHLCV candles aligned to
     09:15, 09:20, 09:25, ... boundaries (matches TradingView).
   - Strategy now consumes ONE completed candle per 5-min bin, not
     one "candle" per tick.

2. HistoricalSeeder (src/core/historical_seeder.py):
   - Pre-loads last 30 5-min bars per symbol from Kite REST historical_data
     at engine boot.
   - Strategy's self.candles is already warm at 09:15, so RSI(14)/ADX(14)/
     Supertrend(10) are meaningful from the very first live tick.
   - Engine fully armed and ready to fire signals at 09:30 (Blueprint window
     open), NOT at 10:25 as raw computation would require.

v9.4 carry-forward
-------------------
Supertrend(10, 3) regime filter remains as the third gate after RSI+ADX.
But now it's computed on REAL 5-min candles, not tick noise.
"""
import logging
import numpy as np
import pandas as pd
import talib
from datetime import datetime, time
from typing import Optional
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent
from src.core.indicators import compute_supertrend
from src.core.candle_aggregator import CandleAggregator

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

# v9.5 — Minimum candles needed before any signal can fire
# RSI(14) needs 14 prior + 1 current = 15
# ADX(14) needs same
# Supertrend(10) needs 10 prior + buffer
# Use max + safety margin
MIN_CANDLES_FOR_SIGNAL = RSI_PERIOD + ADX_PERIOD + 2  # = 30


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

    def __init__(self, engine, symbol: str, sentiment_score: float = 0.0,
                 seed_candles: Optional[list] = None):
        """
        Args:
            engine          — TradingEngine instance
            symbol          — ticker (e.g., "INFY")
            sentiment_score — pre-market FinBERT score for this ticker
            seed_candles    — optional list of OHLCV dicts to pre-populate
                              candle history (warm-up data from yesterday).
                              Allows strategy to fire signals from 09:30
                              instead of waiting until ~10:25.
        """
        super().__init__(engine, symbol, sentiment_score)
        self.strategy_name = "RSIMomentum"   # backward compat for SignalEvent

        # v9.5 — CandleAggregator replaces tick-as-candle pattern
        self.aggregator = CandleAggregator(
            symbol=symbol, interval_minutes=CANDLE_INTERVAL
        )

        # self.candles now stores ONLY completed 5-min OHLCV bars,
        # including any historical seed bars.
        self.candles: list[dict] = list(seed_candles) if seed_candles else []

        self.signal_fired = False
        self.prev_rsi     = None   # Track previous RSI for crossover detection

        if seed_candles:
            logger.info(
                f"RSIMomentum initialised for {symbol} — "
                f"seeded with {len(seed_candles)} historical candles "
                f"(ready to fire at 09:30)"
            )
        else:
            logger.info(
                f"RSIMomentum initialised for {symbol} — "
                f"NO historical seed (will arm ~10:25 via live ticks)"
            )

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

        v9.5 behaviour:
          1. Feed tick to CandleAggregator (always — even pre-09:20,
             so 09:15-09:20 bin gets built correctly)
          2. If a 5-min bin just closed → append completed candle to self.candles
          3. Apply soft gate: don't evaluate signals before 09:20
          4. Evaluate RSI/ADX/Supertrend on the new bar

        Signal evaluation happens ~12 times per hour per symbol
        (once per 5-min boundary cross), not on every tick.
        """
        if event.symbol != self.symbol:
            return
        if self.signal_fired:
            return

        # v9.5 — Aggregate tick into 5-min candle.
        # Run BEFORE soft gate so the 09:15-09:20 bin is built properly.
        # Returns a completed candle ONLY when bin boundary is crossed,
        # else returns None.
        completed = self.aggregator.on_tick(event)
        if completed is None:
            return  # Still building current bin — no evaluation

        # New 5-min candle just closed — append to history
        self.candles.append(completed)
        logger.debug(
            f"  {self.symbol} new 5m candle @ {completed['time'].time()}: "
            f"O={completed['open']:.2f} H={completed['high']:.2f} "
            f"L={completed['low']:.2f} C={completed['close']:.2f} "
            f"V={completed['volume']:.0f} "
            f"(total candles={len(self.candles)})"
        )

        # Soft gate — no SIGNAL evaluation before 09:20.
        # (Candle aggregation continues regardless, so RSI/Supertrend stay
        # current and ready to fire the moment the soft gate opens.)
        if event.timestamp.time() < SOFT_OPEN_GATE:
            return

        # Need enough candles for RSI(14) + ADX(14)
        if len(self.candles) < MIN_CANDLES_FOR_SIGNAL:
            logger.debug(
                f"  {self.symbol} arming — {len(self.candles)}/"
                f"{MIN_CANDLES_FOR_SIGNAL} candles ready"
            )
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
        """Reset for new trading day. Clears candles and aggregator state.

        Note: this clears ALL candles including any seed data. The next-day
        boot sequence should re-seed via HistoricalSeeder.
        """
        self.candles      = []
        self.signal_fired = False
        self.prev_rsi     = None
        self.aggregator.reset()


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

"""
CandleAggregator — bins streaming ticks into N-minute OHLCV candles.

v9.5 (Day 12 EOD, May 19 2026)
================================

Background — the bug this fixes
--------------------------------
Day 12 showed 0 trades with 15 ❌ FILTERED events, all BUY signals
blocked by Supertrend in supposed RED regime — despite TradingView
clearly showing 5m Supertrend(10, 3) in GREEN regime for the same
tickers (APOLLO confirmed visually).

Root cause: rsi_momentum.py treated every tick as a "candle" by
appending to self.candles on every on_tick(). With ~100 ticks/min,
the engine accumulated ~1500 "candles" in 15 minutes instead of 3
real 5-min candles. RSI/ADX/Supertrend computed against tick-level
noise, not 5-min price action.

This affects all 28 Stage 5a trades retroactively — they were never
running the strategy as designed.

What this module does
----------------------
Takes streaming ticks (MarketEvent objects) and produces real
OHLCV candles bucketed by 5-minute bin boundaries (09:15, 09:20,
09:25, ..., aligned to TradingView candle boundaries).

API
----
    agg = CandleAggregator(interval_minutes=5)

    completed = agg.on_tick(tick)
    if completed is not None:
        # A 5-min candle just closed. Run strategy logic.
        strategy_on_new_candle(completed)

    # Anywhere — peek at the bin still being built
    current = agg.get_current_candle()  # may return None if no ticks yet

    # All closed candles
    all_candles = agg.get_completed_candles()

Notes
------
- Kite WebSocket sends cumulative day volume, not per-tick delta.
  We compute bin volume as last_tick.volume - bin_open_volume.
- Pre-market ticks (before 09:15) are ignored.
- Day boundary auto-resets state.
- Thread-safe? No. Owned by single Strategy instance, one tick at a time.
"""
import logging
from datetime import datetime, time, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)


class CandleAggregator:
    """
    Buckets streaming ticks into N-minute OHLCV candles aligned
    to fixed bin boundaries (09:15, 09:20, ..., 15:25).

    Standard usage: one aggregator per (symbol, interval) pair,
    owned by the Strategy that consumes its output.
    """

    def __init__(self, symbol: str, interval_minutes: int = 5):
        self.symbol            = symbol
        self.interval          = interval_minutes
        self._completed: list[dict] = []
        self._current: Optional[dict] = None
        self._bin_open_volume: float  = 0.0   # cumulative volume at bin start
        self._last_tick_volume: float = 0.0   # cumulative volume at last tick in current bin
        self._current_bin_start: Optional[datetime] = None
        self._last_reset_date              = None
        logger.debug(f"  CandleAggregator initialised: {symbol} @ {interval_minutes}min")

    # ── Bin boundary helpers ───────────────────────────────────

    def _bin_start_for(self, ts: datetime) -> datetime:
        """
        Return the start-of-bin datetime for a given timestamp.
        Aligns to 5-min boundaries (09:15, 09:20, 09:25, ...).
        """
        floored_minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=floored_minute, second=0, microsecond=0)

    def _is_market_hours(self, ts: datetime) -> bool:
        """True if the tick's timestamp is within NSE intraday window."""
        t = ts.time()
        return MARKET_OPEN_IST <= t < MARKET_CLOSE_IST

    def _maybe_reset_for_new_day(self, ts: datetime) -> None:
        """Reset state if this tick is from a new trading day."""
        today = ts.date()
        if self._last_reset_date != today:
            if self._last_reset_date is not None:
                logger.debug(
                    f"  CandleAggregator day reset: {self.symbol} "
                    f"{self._last_reset_date} → {today}"
                )
            self._completed.clear()
            self._current = None
            self._bin_open_volume = 0.0
            self._last_tick_volume = 0.0
            self._current_bin_start = None
            self._last_reset_date = today

    # ── Core API ───────────────────────────────────────────────

    def on_tick(self, tick) -> Optional[dict]:
        """
        Feed one tick. Returns a completed candle dict if a bin
        boundary was just crossed, else None.

        Tick must have attributes:
          .timestamp (datetime), .ltp (float), .volume (float, cumulative)

        Returned candle dict shape:
          {
            "time":   datetime (bin start),
            "open":   float (first ltp in bin),
            "high":   float (max ltp in bin),
            "low":    float (min ltp in bin),
            "close":  float (last ltp in bin),
            "volume": float (cumulative_at_bin_end - cumulative_at_bin_start),
          }
        """
        ts = tick.timestamp

        # Day-boundary reset (no-op if same day)
        self._maybe_reset_for_new_day(ts)

        # Ignore pre/post-market ticks entirely
        if not self._is_market_hours(ts):
            return None

        bin_start = self._bin_start_for(ts)

        # ── Case 1 — first tick ever (no current bin) ─────────
        if self._current is None:
            self._start_new_bin(bin_start, tick)
            return None

        # ── Case 2 — same bin as current ──────────────────────
        if bin_start == self._current_bin_start:
            self._update_current(tick)
            return None

        # ── Case 3 — bin crossed → close current, start next ──
        completed = self._finalise_current(tick)
        self._completed.append(completed)
        self._start_new_bin(bin_start, tick)
        return completed

    def get_current_candle(self) -> Optional[dict]:
        """Return the in-progress (not yet closed) candle, or None."""
        if self._current is None:
            return None
        # Return a copy so caller can't mutate internal state
        return dict(self._current)

    def get_completed_candles(self) -> list[dict]:
        """Return all closed candles for the current day."""
        # Return a copy of the list (shallow — dicts are still references)
        return list(self._completed)

    def candle_count(self) -> int:
        """Number of completed candles today."""
        return len(self._completed)

    def reset(self) -> None:
        """Manual reset (for tests or forced new day)."""
        self._completed.clear()
        self._current = None
        self._bin_open_volume = 0.0
        self._last_tick_volume = 0.0
        self._current_bin_start = None
        self._last_reset_date = None
        logger.debug(f"  CandleAggregator manual reset: {self.symbol}")

    # ── Internal state helpers ─────────────────────────────────

    def _start_new_bin(self, bin_start: datetime, tick) -> None:
        """Begin tracking a new bin from this tick."""
        self._current_bin_start = bin_start
        self._bin_open_volume   = float(tick.volume)
        self._last_tick_volume  = float(tick.volume)
        self._current = {
            "time":   bin_start,
            "open":   float(tick.ltp),
            "high":   float(tick.ltp),
            "low":    float(tick.ltp),
            "close":  float(tick.ltp),
            "volume": 0.0,  # filled in at bin close
        }

    def _update_current(self, tick) -> None:
        """Update OHLC of the current bin with a new tick."""
        ltp = float(tick.ltp)
        if ltp > self._current["high"]:
            self._current["high"] = ltp
        if ltp < self._current["low"]:
            self._current["low"] = ltp
        self._current["close"] = ltp
        # Track the most recent cumulative volume seen in this bin
        self._last_tick_volume = float(tick.volume)

    def _finalise_current(self, next_tick) -> dict:
        """
        Close the current bin and return its completed candle.

        Bin volume = (cumulative volume at last tick in bin) - (cumulative
        volume at first tick in bin). Kite sends cumulative day volume,
        so this gives us the volume that flowed during this bin only.
        """
        bin_volume = max(0.0, self._last_tick_volume - self._bin_open_volume)
        self._current["volume"] = bin_volume
        return dict(self._current)

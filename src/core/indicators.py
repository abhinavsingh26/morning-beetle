import logging
import numpy as np
import talib
from collections import deque
from datetime import datetime, time

logger = logging.getLogger(__name__)


# ── v9.4 — Supertrend indicator (function-style, talib-based) ─
# Day 11 EOD finding: 0% win rate on 10-30 min entries; RSI fires
# AFTER the move is already underway. Supertrend acts as a
# regime filter — only allow BUYs when trend is GREEN (uptrend),
# only allow SELLs when trend is RED (downtrend).
#
# Used by S2 (RSI Momentum). Function-style to match talib usage
# pattern in rsi_momentum.py (talib.RSI(closes), talib.ADX(...)).
#
# Standard Indian intraday parameters: period=10, multiplier=3
# (matches default in pandas-ta and the configurations used by
# most NSE retail traders).

def compute_supertrend(highs: np.ndarray, lows: np.ndarray,
                       closes: np.ndarray,
                       period: int = 10,
                       multiplier: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute Supertrend indicator on OHLC arrays.

    Returns:
        supertrend: np.ndarray — the trend line value
        direction:  np.ndarray — +1 for GREEN (uptrend), -1 for RED (downtrend)

    Algorithm (standard):
        1. ATR(period) of bars
        2. hl2 = (high + low) / 2
        3. upper_band = hl2 + multiplier × ATR
        4. lower_band = hl2 - multiplier × ATR
        5. Band-locking: keep band tight on each side of price
        6. Trend flips when close crosses the active band

    Returns NaN for the first `period` bars (insufficient data).
    """
    n = len(closes)
    if n < period + 1:
        # Not enough data
        return np.full(n, np.nan), np.full(n, np.nan)

    atr = talib.ATR(highs, lows, closes, timeperiod=period)
    hl2 = (highs + lows) / 2.0

    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    upper_band = np.full(n, np.nan)
    lower_band = np.full(n, np.nan)
    supertrend = np.full(n, np.nan)
    direction  = np.full(n, np.nan)

    # Initialise at first valid bar (where ATR is defined)
    first_valid = period
    upper_band[first_valid] = upper_basic[first_valid]
    lower_band[first_valid] = lower_basic[first_valid]
    supertrend[first_valid] = upper_band[first_valid]
    direction[first_valid]  = -1  # neutral start, will flip on first cross

    for i in range(first_valid + 1, n):
        # Upper band: only updates downward (or holds) unless prev close was above it
        if upper_basic[i] < upper_band[i-1] or closes[i-1] > upper_band[i-1]:
            upper_band[i] = upper_basic[i]
        else:
            upper_band[i] = upper_band[i-1]

        # Lower band: only updates upward (or holds) unless prev close was below it
        if lower_basic[i] > lower_band[i-1] or closes[i-1] < lower_band[i-1]:
            lower_band[i] = lower_basic[i]
        else:
            lower_band[i] = lower_band[i-1]

        # Trend logic: previous supertrend determines which band is active
        prev_st  = supertrend[i-1]
        prev_dir = direction[i-1]

        if prev_st == upper_band[i-1]:
            # Was in downtrend (price below upper band)
            if closes[i] > upper_band[i]:
                # Close broke above → flip to uptrend
                supertrend[i] = lower_band[i]
                direction[i]  = 1
            else:
                supertrend[i] = upper_band[i]
                direction[i]  = -1
        else:
            # Was in uptrend (price above lower band)
            if closes[i] < lower_band[i]:
                # Close broke below → flip to downtrend
                supertrend[i] = upper_band[i]
                direction[i]  = -1
            else:
                supertrend[i] = lower_band[i]
                direction[i]  = 1

    return supertrend, direction


class VWAPCalculator:
    """
    Rolling intraday VWAP calculator.

    VWAP = Σ(Price × Volume) / Σ(Volume)

    Resets at market open (09:15) each day.
    Used by Strategy S3 (Sector Leader Pullback).
    """

    def __init__(self, symbol: str):
        self.symbol           = symbol
        self._cumulative_pv   = 0.0   # Price × Volume
        self._cumulative_vol  = 0.0   # Total Volume
        self._vwap            = None
        self._tick_count      = 0
        self._last_reset_date = None
        logger.info(f"  VWAPCalculator initialised for {symbol}")

    def update(self, price: float, volume: float,
               timestamp: datetime = None) -> float:
        """
        Update VWAP with new tick.
        Returns current VWAP value.
        """
        if timestamp is None:
            timestamp = datetime.now()

        # Reset at start of each trading day
        today = timestamp.date()
        if self._last_reset_date != today:
            self.reset()
            self._last_reset_date = today

        if volume <= 0:
            return self._vwap or price

        self._cumulative_pv  += price * volume
        self._cumulative_vol += volume
        self._tick_count     += 1

        self._vwap = round(
            self._cumulative_pv / self._cumulative_vol, 2
        )
        return self._vwap

    def get_vwap(self) -> float | None:
        """Return current VWAP or None if insufficient data."""
        return self._vwap

    def is_price_above_vwap(self, price: float) -> bool:
        """True if price is above VWAP."""
        if self._vwap is None:
            return False
        return price > self._vwap

    def is_price_below_vwap(self, price: float) -> bool:
        """True if price is below VWAP."""
        if self._vwap is None:
            return False
        return price < self._vwap

    def distance_from_vwap_pct(self, price: float) -> float:
        """
        Returns % distance of price from VWAP.
        Positive = above VWAP, Negative = below VWAP.
        """
        if self._vwap is None or self._vwap == 0:
            return 0.0
        return round((price - self._vwap) / self._vwap * 100, 4)

    def reset(self):
        """Reset for new trading day."""
        self._cumulative_pv  = 0.0
        self._cumulative_vol = 0.0
        self._vwap           = None
        self._tick_count     = 0
        logger.debug(f"  VWAP reset: {self.symbol}")

    def __repr__(self):
        return (f"VWAPCalculator({self.symbol}, "
                f"vwap={self._vwap}, ticks={self._tick_count})")


class ATRCalculator:
    """
    Rolling ATR (Average True Range) calculator.
    Used by S1 (Morning Breakout) and S4 (Volatility Contraction).
    """

    def __init__(self, symbol: str, period: int = 14):
        self.symbol  = symbol
        self.period  = period
        self._trs    = deque(maxlen=period)
        self._prev_close = None
        self._atr    = None

    def update(self, high: float, low: float,
               close: float) -> float | None:
        """Update ATR with new candle. Returns current ATR."""
        if self._prev_close is None:
            self._prev_close = close
            return None

        tr = max(
            high - low,
            abs(high - self._prev_close),
            abs(low  - self._prev_close)
        )
        self._trs.append(tr)
        self._prev_close = close

        if len(self._trs) >= self.period:
            self._atr = round(sum(self._trs) / len(self._trs), 4)
        return self._atr

    def get_atr(self) -> float | None:
        return self._atr

    def get_atr_pct(self, price: float) -> float:
        """ATR as % of price."""
        if self._atr is None or price == 0:
            return 0.0
        return round(self._atr / price * 100, 4)

    def reset(self):
        self._trs.clear()
        self._prev_close = None
        self._atr = None


class RSICalculator:
    """
    RSI calculator using Wilder's smoothing method.
    Used by S2 (RSI Momentum).
    """

    def __init__(self, symbol: str, period: int = 14):
        self.symbol  = symbol
        self.period  = period
        self._prices = deque(maxlen=period + 1)
        self._avg_gain = None
        self._avg_loss = None
        self._rsi    = None

    def update(self, price: float) -> float | None:
        """Update RSI with new price. Returns current RSI."""
        self._prices.append(price)

        if len(self._prices) < self.period + 1:
            return None

        prices = list(self._prices)
        changes = [prices[i] - prices[i-1]
                   for i in range(1, len(prices))]

        if self._avg_gain is None:
            gains = [c for c in changes if c > 0]
            losses = [abs(c) for c in changes if c < 0]
            self._avg_gain = sum(gains) / self.period
            self._avg_loss = sum(losses) / self.period
        else:
            change = changes[-1]
            gain = change if change > 0 else 0
            loss = abs(change) if change < 0 else 0
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self._rsi = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self._rsi = round(100 - (100 / (1 + rs)), 2)

        return self._rsi

    def get_rsi(self) -> float | None:
        return self._rsi

    def reset(self):
        self._prices.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._rsi = None
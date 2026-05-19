"""
HistoricalSeeder — pre-loads N prior 5-min candles per symbol at engine boot.

v9.5 (Day 12 EOD, May 19 2026)
================================

Problem this solves
--------------------
After fixing the tick-as-candle bug (CandleAggregator), RSI(14) needs
14 real 5-min bars before it can fire. From 09:15 market open, that's
14 × 5 = 70 minutes → first valid RSI signal at 10:25 AM.

But the morning catalyst window is 09:15-10:30 — the most profitable
trading window of the day. Waiting until 10:25 to fire signals misses
nearly all of it.

Solution
---------
Fetch the last 30 5-min candles from Kite's historical_data API at
engine boot. Pre-populate each strategy's candle history. At 09:15
when the first today's bar starts building, RSI/ADX/Supertrend are
already warm.

Net effect: engine fully armed and ready to fire signals at 09:30 AM,
not 10:25 AM.

Failure modes
--------------
If Kite historical_data fails (API down, rate-limited, auth expired),
fall back to NO seeding — log WARN, engine boots anyway. Strategy
will arm by 10:25 like before. Don't crash the engine over historical
data unavailability.

Usage
------
    seeder = HistoricalSeeder(kite_rest_client)
    candles_by_symbol = seeder.fetch_for_symbols(
        instrument_map={"INFY": 408065, "RELIANCE": 738561},
        num_candles=30,
        interval="5minute"
    )
    # → {"INFY": [candle, candle, ...], "RELIANCE": [...]}

    # Caller passes these into each Strategy at instantiation
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Kite historical_data interval strings
KITE_INTERVAL_MAP = {
    1:  "minute",
    3:  "3minute",
    5:  "5minute",
    10: "10minute",
    15: "15minute",
    30: "30minute",
    60: "60minute",
}

# How many calendar days back to query to be safe.
# 5 days covers weekends + 1-2 holidays.
LOOKBACK_DAYS = 5


class HistoricalSeeder:
    """
    Fetches recent 5-min OHLCV bars per symbol from Kite REST API
    so strategies can warm up indicators at engine boot.
    """

    def __init__(self, kite_rest):
        """
        kite_rest — an already-authenticated KiteConnect REST client.
                    Same one the kill switch uses for quote fallback.
        """
        self.kite = kite_rest
        logger.info("HistoricalSeeder initialised (Kite REST historical_data).")

    def fetch_for_symbols(self,
                          instrument_map: dict,
                          num_candles: int = 30,
                          interval_minutes: int = 5
                          ) -> dict:
        """
        Fetch the last `num_candles` candles per symbol.

        Args:
            instrument_map: {symbol: instrument_token}
            num_candles:    how many bars to retain per symbol (default 30)
            interval_minutes: 1, 3, 5, 10, 15, 30, or 60

        Returns:
            {symbol: [candle_dict, ...]} where each candle_dict is:
              {"time": datetime, "open": float, "high": float,
               "low": float, "close": float, "volume": float}

            Symbols that failed to fetch get an empty list.
            Caller decides what to do with missing data.
        """
        if interval_minutes not in KITE_INTERVAL_MAP:
            raise ValueError(
                f"interval_minutes must be one of {list(KITE_INTERVAL_MAP)}, "
                f"got {interval_minutes}"
            )

        interval_str = KITE_INTERVAL_MAP[interval_minutes]
        now = datetime.now()
        from_dt = now - timedelta(days=LOOKBACK_DAYS)

        results = {}
        for symbol, token in instrument_map.items():
            try:
                bars = self._fetch_one_symbol(
                    token=token,
                    symbol=symbol,
                    from_dt=from_dt,
                    to_dt=now,
                    interval_str=interval_str,
                )
                # Take last N bars
                if bars:
                    trimmed = bars[-num_candles:]
                    results[symbol] = self._normalise_bars(trimmed)
                    logger.info(
                        f"  Seeded {symbol}: {len(trimmed)} bars "
                        f"(from {trimmed[0]['date']} to {trimmed[-1]['date']})"
                    )
                else:
                    logger.warning(f"  Seeded {symbol}: 0 bars returned by Kite")
                    results[symbol] = []
            except Exception as e:
                # Don't let one failure kill the rest
                logger.error(
                    f"  Seed FAILED for {symbol}: {type(e).__name__}: {e}. "
                    f"Strategy will arm later via live ticks."
                )
                results[symbol] = []

        return results

    def _fetch_one_symbol(self,
                          token: int,
                          symbol: str,
                          from_dt: datetime,
                          to_dt: datetime,
                          interval_str: str) -> list:
        """
        Single-symbol Kite historical_data call.
        Returns the raw list of bar dicts from Kite, or [] on failure.

        Kite returns each bar as:
          {
            "date": datetime,
            "open": float, "high": float, "low": float, "close": float,
            "volume": int
          }
        """
        if self.kite is None:
            logger.error(f"  Kite REST client is None — cannot seed {symbol}")
            return []

        bars = self.kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=to_dt,
            interval=interval_str,
        )
        return bars or []

    def _normalise_bars(self, kite_bars: list) -> list:
        """
        Convert Kite bar format to internal candle format.
        Kite key 'date' → our key 'time' to match CandleAggregator output.
        """
        normalised = []
        for b in kite_bars:
            normalised.append({
                "time":   b["date"],
                "open":   float(b["open"]),
                "high":   float(b["high"]),
                "low":    float(b["low"]),
                "close":  float(b["close"]),
                "volume": float(b.get("volume", 0)),
            })
        return normalised

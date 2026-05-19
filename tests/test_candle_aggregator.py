"""
test_candle_aggregator.py — Comprehensive tests for v9.5 CandleAggregator.

Validates:
  1. Single tick → bin started but no candle emitted
  2. Multiple ticks in same bin → updates current, no emit
  3. Bin boundary crossed → completed candle emitted
  4. Bin alignment to 5-min boundaries (09:15, 09:20, 09:25, ...)
  5. OHLC computed correctly across ticks
  6. Volume = cumulative_at_last_tick - cumulative_at_first_tick
  7. Pre-market ticks ignored
  8. Post-market ticks ignored
  9. Day boundary auto-resets state
 10. Manual reset clears state
 11. APOLLO-on-Day-12 reconstruction: ~1500 ticks → 3 candles, not 1500
 12. current candle is a copy (no mutation leak)

Usage:
    python tests/test_candle_aggregator.py
"""
import sys
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.candle_aggregator import CandleAggregator


def make_tick(ts_str: str, ltp: float, volume: float = 0.0):
    """Build a minimal tick object with the fields CandleAggregator reads."""
    ts = datetime.fromisoformat(ts_str)
    return SimpleNamespace(timestamp=ts, ltp=ltp, volume=volume)


# ── Test 1: Single tick starts bin, no emit ────────────────────
def test_single_tick_no_emit():
    agg = CandleAggregator("TEST")
    result = agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    assert result is None, f"Expected None on first tick, got {result}"
    assert agg.candle_count() == 0
    current = agg.get_current_candle()
    assert current is not None
    assert current["open"] == 100.0
    assert current["close"] == 100.0
    print("✅ Test 1: Single tick starts bin, no emit")


# ── Test 2: Same-bin updates ───────────────────────────────────
def test_same_bin_updates():
    agg = CandleAggregator("TEST")
    # All within 09:15-09:20 bin
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    agg.on_tick(make_tick("2026-05-19 09:17:30", 105.0, 1500))
    agg.on_tick(make_tick("2026-05-19 09:18:00", 95.0,  2000))
    agg.on_tick(make_tick("2026-05-19 09:19:30", 102.0, 2800))
    assert agg.candle_count() == 0, "No candle should emit while in same bin"
    current = agg.get_current_candle()
    assert current["open"] == 100.0,  f"open={current['open']}"
    assert current["high"] == 105.0,  f"high={current['high']}"
    assert current["low"]  == 95.0,   f"low={current['low']}"
    assert current["close"] == 102.0, f"close={current['close']}"
    print("✅ Test 2: Same-bin OHLC updates correctly")


# ── Test 3: Bin boundary emits completed candle ────────────────
def test_bin_boundary_emit():
    agg = CandleAggregator("TEST")
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    agg.on_tick(make_tick("2026-05-19 09:17:00", 105.0, 1500))
    agg.on_tick(make_tick("2026-05-19 09:19:00", 102.0, 2500))
    # Next tick crosses into 09:20-09:25 bin → emits 09:15 candle
    completed = agg.on_tick(make_tick("2026-05-19 09:20:00", 110.0, 3000))
    assert completed is not None, "Expected completed candle on bin cross"
    assert completed["time"] == datetime(2026, 5, 19, 9, 15)
    assert completed["open"] == 100.0
    assert completed["high"] == 105.0
    assert completed["low"]  == 100.0
    assert completed["close"] == 102.0  # last tick in 09:15 bin
    assert completed["volume"] == 2500 - 1000  # 1500
    # New bin started
    new_current = agg.get_current_candle()
    assert new_current["open"] == 110.0
    print(f"✅ Test 3: Bin boundary emits correct candle: {completed['time'].time()} "
          f"O={completed['open']} H={completed['high']} L={completed['low']} "
          f"C={completed['close']} V={completed['volume']}")


# ── Test 4: Bin alignment to 5-min boundaries ──────────────────
def test_bin_alignment():
    agg = CandleAggregator("TEST")
    # First tick at 09:17 should be assigned to 09:15 bin
    agg.on_tick(make_tick("2026-05-19 09:17:23", 100.0, 1000))
    current = agg.get_current_candle()
    assert current["time"] == datetime(2026, 5, 19, 9, 15), \
        f"Expected 09:15 bin start, got {current['time']}"

    # Bin should close when a tick at 09:20:00 or later arrives
    completed = agg.on_tick(make_tick("2026-05-19 09:20:01", 105.0, 1500))
    assert completed["time"] == datetime(2026, 5, 19, 9, 15)
    # New bin should be 09:20-09:25
    new_current = agg.get_current_candle()
    assert new_current["time"] == datetime(2026, 5, 19, 9, 20)
    print("✅ Test 4: Bins aligned to 5-min boundaries (09:15, 09:20, ...)")


# ── Test 5: OHLC over many ticks ───────────────────────────────
def test_ohlc_over_many_ticks():
    agg = CandleAggregator("TEST")
    # 50 ticks within one bin, varying prices
    base_ts = datetime(2026, 5, 19, 9, 16, 0)
    prices = [100, 102, 98, 105, 99, 107, 95, 101, 103, 96,
              110, 92, 104, 100, 108, 94, 106, 102, 99, 105,
              101, 93, 109, 97, 100, 102, 108, 96, 104, 99,
              107, 95, 103, 100, 102, 98, 105, 99, 101, 104,
              97, 106, 100, 98, 103, 99, 102, 105, 100, 103]
    for i, p in enumerate(prices):
        ts = base_ts + timedelta(seconds=i * 2)  # 2s spacing
        agg.on_tick(SimpleNamespace(timestamp=ts, ltp=float(p), volume=1000 + i * 10))
    completed = agg.on_tick(make_tick("2026-05-19 09:20:00", 100.0, 5000))
    assert completed["open"] == 100, f"open={completed['open']}"
    assert completed["high"] == 110, f"high={completed['high']}"
    assert completed["low"]  == 92, f"low={completed['low']}"
    assert completed["close"] == 103, f"close={completed['close']}"
    print(f"✅ Test 5: 50 ticks → 1 candle (O=100 H=110 L=92 C=103) "
          f"NOT 50 candles")


# ── Test 6: Pre-market ticks ignored ───────────────────────────
def test_premarket_ignored():
    agg = CandleAggregator("TEST")
    # Ticks before 09:15 should be ignored entirely
    result = agg.on_tick(make_tick("2026-05-19 09:00:00", 100.0, 1000))
    assert result is None
    assert agg.candle_count() == 0
    assert agg.get_current_candle() is None
    # After market open, normal behaviour resumes
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1500))
    assert agg.get_current_candle() is not None
    print("✅ Test 6: Pre-market ticks (< 09:15) ignored")


# ── Test 7: Post-market ticks ignored ──────────────────────────
def test_postmarket_ignored():
    agg = CandleAggregator("TEST")
    # Normal bin
    agg.on_tick(make_tick("2026-05-19 15:25:00", 100.0, 1000))
    # Tick at 15:30 or later is post-market — should be ignored
    result = agg.on_tick(make_tick("2026-05-19 15:31:00", 101.0, 1100))
    assert result is None, f"Post-market tick should be ignored, got {result}"
    print("✅ Test 7: Post-market ticks (>= 15:30) ignored")


# ── Test 8: Day boundary auto-reset ────────────────────────────
def test_day_boundary_reset():
    agg = CandleAggregator("TEST")
    # Day 1 — build a candle
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    completed = agg.on_tick(make_tick("2026-05-19 09:20:00", 105.0, 1500))
    assert agg.candle_count() == 1
    # Next day — state should reset, completed list clears
    agg.on_tick(make_tick("2026-05-20 09:16:00", 200.0, 500))
    assert agg.candle_count() == 0, "Day reset should clear completed candles"
    current = agg.get_current_candle()
    assert current["time"] == datetime(2026, 5, 20, 9, 15)
    assert current["open"] == 200.0
    print("✅ Test 8: Day boundary auto-resets state")


# ── Test 9: Manual reset ───────────────────────────────────────
def test_manual_reset():
    agg = CandleAggregator("TEST")
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    agg.on_tick(make_tick("2026-05-19 09:20:00", 105.0, 1500))
    assert agg.candle_count() == 1
    agg.reset()
    assert agg.candle_count() == 0
    assert agg.get_current_candle() is None
    print("✅ Test 9: Manual reset clears state")


# ── Test 10: APOLLO Day 12 reconstruction ──────────────────────
def test_apollo_day_12_simulation():
    """
    Simulate APOLLO's tick stream from Day 12 logs:
      09:17: 67 ticks
      09:20: 98 ticks
      09:25: ~90 ticks
      09:30-09:35: ~93 ticks
    Total: ~350 ticks in first 20 minutes.

    With the bug: engine would have ~350 'candles'.
    With CandleAggregator: should produce exactly 4 candles.
    """
    agg = CandleAggregator("APOLLO")
    base_ts = datetime(2026, 5, 19, 9, 16, 0)
    cumulative_vol = 100

    # Simulate ~70 ticks/min over 20 minutes
    for minute in range(20):
        for sec in range(0, 60, 1):  # ~60 ticks per minute
            t = base_ts + timedelta(minutes=minute, seconds=sec)
            # Price drifts up slowly
            ltp = 295.0 + (minute * 0.5) + (sec / 120.0)
            cumulative_vol += 100
            agg.on_tick(SimpleNamespace(
                timestamp=t, ltp=ltp, volume=float(cumulative_vol)
            ))

    # After 20 minutes from 09:16, we crossed 09:20, 09:25, 09:30, 09:35 boundaries
    # → 4 completed candles (09:15, 09:20, 09:25, 09:30 bins)
    # Bins: 09:15 (4 min), 09:20 (5 min), 09:25 (5 min), 09:30 (5 min), 09:35 (in progress)
    assert agg.candle_count() == 4, \
        f"Expected 4 completed candles, got {agg.candle_count()}"
    # Sanity: current bin should be 09:35
    current = agg.get_current_candle()
    assert current["time"] == datetime(2026, 5, 19, 9, 35)
    # All completed candles should be properly bucketed
    completed = agg.get_completed_candles()
    expected_starts = [
        datetime(2026, 5, 19, 9, 15),
        datetime(2026, 5, 19, 9, 20),
        datetime(2026, 5, 19, 9, 25),
        datetime(2026, 5, 19, 9, 30),
    ]
    for c, exp in zip(completed, expected_starts):
        assert c["time"] == exp, f"Bad bin: {c['time']} vs {exp}"
    print(f"✅ Test 10: ~1200 ticks → 4 candles (was the bug: would be ~1200 'candles')")


# ── Test 11: get_current_candle returns a copy ─────────────────
def test_current_candle_is_copy():
    agg = CandleAggregator("TEST")
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    snap1 = agg.get_current_candle()
    snap1["high"] = 999999  # try to mutate
    snap2 = agg.get_current_candle()
    assert snap2["high"] != 999999, "Snapshot mutation leaked into aggregator state"
    print("✅ Test 11: get_current_candle returns a defensive copy")


# ── Test 12: get_completed_candles returns a copy of the list ──
def test_completed_list_is_copy():
    agg = CandleAggregator("TEST")
    agg.on_tick(make_tick("2026-05-19 09:16:00", 100.0, 1000))
    agg.on_tick(make_tick("2026-05-19 09:20:00", 105.0, 1500))
    lst = agg.get_completed_candles()
    lst.clear()  # mutate caller's copy
    assert agg.candle_count() == 1, "Mutating returned list affected internal state"
    print("✅ Test 12: get_completed_candles returns a list copy")


def main():
    print("=" * 70)
    print("  CandleAggregator v9.5 — Tests")
    print("=" * 70)
    print()

    tests = [
        test_single_tick_no_emit,
        test_same_bin_updates,
        test_bin_boundary_emit,
        test_bin_alignment,
        test_ohlc_over_many_ticks,
        test_premarket_ignored,
        test_postmarket_ignored,
        test_day_boundary_reset,
        test_manual_reset,
        test_apollo_day_12_simulation,
        test_current_candle_is_copy,
        test_completed_list_is_copy,
    ]

    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            failed.append((t.__name__, str(e)))
            print(f"❌ {t.__name__}: {e}")
        except Exception as e:
            failed.append((t.__name__, f"EXCEPTION: {e}"))
            print(f"❌ {t.__name__}: EXCEPTION {e}")
            import traceback; traceback.print_exc()

    print()
    print("=" * 70)
    print(f"  Results: {passed}/{len(tests)} passed")
    print("=" * 70)
    if failed:
        for name, msg in failed:
            print(f"  FAILED: {name}\n    {msg}")
        sys.exit(1)
    print("✅ All tests pass — v9.5 CandleAggregator verified")


if __name__ == "__main__":
    main()

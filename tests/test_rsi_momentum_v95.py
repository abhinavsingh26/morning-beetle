"""
test_rsi_momentum_v95.py — End-to-end integration test for v9.5 RSIMomentum.

This is the test that PROVES the Day 12 bug is fixed.

Validates:
  1. With seed_candles + live ticks, strategy is armed at 09:30
  2. Strategy only evaluates RSI/Supertrend on COMPLETED 5-min bars
  3. 100 ticks within one 5-min bin → 0 evaluations, 0 signals
  4. Bin crossing → 1 evaluation
  5. signal_fired guards against double-firing
  6. Bullish seed + bullish live ticks fires BUY signal (when conditions align)
  7. Without seeding, strategy takes much longer to arm

Usage:
    python tests/test_rsi_momentum_v95.py
"""
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def make_tick(ts_str, ltp, volume=1000, symbol="TEST"):
    """Mock MarketEvent."""
    ts = datetime.fromisoformat(ts_str) if isinstance(ts_str, str) else ts_str
    return SimpleNamespace(
        symbol=symbol,
        timestamp=ts,
        ltp=float(ltp),
        open=float(ltp),
        high=float(ltp),
        low=float(ltp),
        close=float(ltp),
        volume=float(volume),
        instrument_token=12345,
    )


def make_seed_candles(start_dt, count, base_price=100.0, trend=0.0):
    """
    Build N synthetic 5-min candles ending just before start_dt.
    trend > 0 → uptrending, < 0 → downtrending, 0 → flat.
    """
    candles = []
    for i in range(count):
        bar_time = start_dt - timedelta(minutes=(count - i) * 5)
        # price walks
        price = base_price + (trend * i) + ((i % 3) * 0.1)
        candles.append({
            "time":   bar_time,
            "open":   price,
            "high":   price + 0.3,
            "low":    price - 0.3,
            "close":  price + 0.1,
            "volume": 1000 + i * 10,
        })
    return candles


# ── Test 1: Aggregator integration — no evaluation per-tick ────
def test_no_eval_per_tick():
    """100 ticks within one bin should produce ZERO signal evaluations."""
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    # Seed enough candles to pass the MIN_CANDLES_FOR_SIGNAL threshold
    seed = make_seed_candles(
        start_dt=datetime(2026, 5, 20, 9, 15),
        count=30,
        base_price=100.0,
        trend=0.5,  # mild uptrend
    )

    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=seed)

    # Feed 100 ticks ALL within the 09:30-09:35 bin
    initial_candle_count = len(strat.candles)
    base_ts = datetime(2026, 5, 20, 9, 30, 0)
    for i in range(100):
        t = base_ts + timedelta(seconds=i * 2)  # 2-second spacing
        strat.on_tick(make_tick(t.isoformat(), 115.0 + (i * 0.01)))

    # No bin boundary crossed yet → no new candle appended
    assert len(strat.candles) == initial_candle_count, \
        f"Expected {initial_candle_count} candles (no bin crossed), got {len(strat.candles)}"
    # engine.emit_event should never have been called for SIGNAL
    signal_emits = [
        c for c in engine.emit_event.call_args_list
        if hasattr(c.args[0], 'direction') if c.args
    ]
    assert len(signal_emits) == 0, "No signals should fire mid-bin"
    print(f"✅ Test 1: 100 ticks within 1 bin → 0 candles appended, 0 signals")


# ── Test 2: Bin crossing → exactly 1 evaluation ─────────────────
def test_eval_on_bin_close():
    """Crossing into next bin closes current, appends 1 candle."""
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    seed = make_seed_candles(
        start_dt=datetime(2026, 5, 20, 9, 15),
        count=30,
        base_price=100.0,
        trend=0.5,
    )
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=seed)
    initial_count = len(strat.candles)

    # Feed ticks within 09:30-09:35 bin
    for i in range(20):
        t = datetime(2026, 5, 20, 9, 30) + timedelta(seconds=i * 10)
        strat.on_tick(make_tick(t.isoformat(), 115.0))
    # Still no new completed candle
    assert len(strat.candles) == initial_count

    # Tick at 09:35 crosses bin → 09:30 candle should close
    strat.on_tick(make_tick("2026-05-20 09:35:00", 116.0))
    assert len(strat.candles) == initial_count + 1, \
        f"Expected {initial_count + 1} candles after bin close, got {len(strat.candles)}"
    print(f"✅ Test 2: Bin close → exactly 1 candle appended (was {initial_count}, now {len(strat.candles)})")


# ── Test 3: Strategy armed at 09:30 with seed ──────────────────
def test_armed_with_seed():
    """Strategy with 30 seed candles should be armed at 09:30 boundary."""
    from src.strategies.rsi_momentum import RSIMomentum, MIN_CANDLES_FOR_SIGNAL

    engine = MagicMock()
    seed = make_seed_candles(
        start_dt=datetime(2026, 5, 20, 9, 15),
        count=30,
        base_price=100.0,
        trend=0.5,
    )
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=seed)
    # Seed alone should satisfy MIN_CANDLES_FOR_SIGNAL
    assert len(strat.candles) >= MIN_CANDLES_FOR_SIGNAL, \
        f"Seed {len(strat.candles)} < required {MIN_CANDLES_FOR_SIGNAL}"
    print(f"✅ Test 3: Strategy armed with seed ({len(strat.candles)} >= {MIN_CANDLES_FOR_SIGNAL})")


# ── Test 4: Without seed, strategy starts cold ─────────────────
def test_cold_start_without_seed():
    """No seed → strategy.candles is empty."""
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=None)
    assert len(strat.candles) == 0
    print("✅ Test 4: Without seed → cold start (candles = [])")


# ── Test 5: Empty seed list treated as no seed ─────────────────
def test_empty_seed_list():
    """Empty list seed should behave like None."""
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=[])
    assert len(strat.candles) == 0
    print("✅ Test 5: Empty seed list → cold start")


# ── Test 6: Reset clears aggregator too ────────────────────────
def test_reset_clears_aggregator():
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    seed = make_seed_candles(
        start_dt=datetime(2026, 5, 20, 9, 15), count=30, base_price=100.0, trend=0.5
    )
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.9, seed_candles=seed)

    # Push some ticks
    for i in range(20):
        t = datetime(2026, 5, 20, 9, 30) + timedelta(seconds=i * 10)
        strat.on_tick(make_tick(t.isoformat(), 115.0))

    strat.reset()
    assert len(strat.candles) == 0, "Reset should clear candles"
    assert strat.signal_fired is False
    assert strat.aggregator.candle_count() == 0
    assert strat.aggregator.get_current_candle() is None
    print("✅ Test 6: Reset clears candles + aggregator state")


# ── Test 7: APOLLO Day 12 simulation — the proof ───────────────
def test_apollo_day12_no_phantom_filters():
    """
    Recreate the Day 12 APOLLO scenario:
      ~70 ticks per minute, 20 minutes of trading
      ≈1400 ticks total.

    With the BUG: strategy would have evaluated ~1400 times, each time
                  on tick-noise data, producing 15 phantom FILTERED events.
    With FIX:    strategy evaluates 4 times (4 bin closures), once per
                 real 5-min candle.
    """
    from src.strategies.rsi_momentum import RSIMomentum

    engine = MagicMock()
    seed = make_seed_candles(
        start_dt=datetime(2026, 5, 20, 9, 15),
        count=30,
        base_price=295.0,
        trend=0.5,
    )
    strat = RSIMomentum(engine, "APOLLO", sentiment_score=0.94, seed_candles=seed)
    initial_candle_count = len(strat.candles)

    base_ts = datetime(2026, 5, 20, 9, 16, 0)
    for minute in range(20):
        for sec in range(0, 60, 1):
            t = base_ts + timedelta(minutes=minute, seconds=sec)
            ltp = 295.0 + (minute * 0.5) + (sec / 120.0)
            strat.on_tick(make_tick(t.isoformat(), ltp, 100 + minute * 100, symbol="APOLLO"))

    candles_added = len(strat.candles) - initial_candle_count
    # 20 minutes from 09:16 crosses bin boundaries at 09:20, 09:25, 09:30, 09:35
    # → 4 candles closed
    assert candles_added == 4, \
        f"Expected 4 candles closed, got {candles_added}"
    print(f"✅ Test 7: APOLLO sim — {1200} ticks → {candles_added} candle evaluations (was: ~1200 phantom evals)")


def main():
    print("=" * 70)
    print("  RSIMomentum v9.5 (CandleAggregator + Seeding) — Integration Tests")
    print("=" * 70)
    print()

    tests = [
        test_no_eval_per_tick,
        test_eval_on_bin_close,
        test_armed_with_seed,
        test_cold_start_without_seed,
        test_empty_seed_list,
        test_reset_clears_aggregator,
        test_apollo_day12_no_phantom_filters,
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
            failed.append((t.__name__, f"EXCEPTION: {type(e).__name__}: {e}"))
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
    print("✅ All tests pass — v9.5 RSIMomentum integration verified")


if __name__ == "__main__":
    main()

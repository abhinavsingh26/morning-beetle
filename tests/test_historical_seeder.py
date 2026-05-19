"""
test_historical_seeder.py — Tests for v9.5 HistoricalSeeder.

Tests:
  1. Happy path — fetches and normalises bars correctly
  2. Multi-symbol fetch — handles dict of {symbol: token}
  3. Empty result from Kite — graceful empty list
  4. Kite raises exception — caught, logged, empty list returned
  5. Invalid interval rejected
  6. Bar normalisation: 'date' → 'time' rename, float coercion
  7. num_candles trimming — returns last N when more available
  8. None kite client → all empty, no crash

Usage:
    python tests/test_historical_seeder.py
"""
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.historical_seeder import HistoricalSeeder, KITE_INTERVAL_MAP


def make_kite_bar(date_str, o, h, l, c, v):
    """Build a fake Kite bar response object."""
    return {
        "date":   datetime.fromisoformat(date_str),
        "open":   o,
        "high":   h,
        "low":    l,
        "close":  c,
        "volume": v,
    }


# ── Test 1: Happy path ─────────────────────────────────────────
def test_happy_path():
    kite = MagicMock()
    kite.historical_data.return_value = [
        make_kite_bar("2026-05-16 15:20:00", 100.0, 102.0, 99.0, 101.0, 5000),
        make_kite_bar("2026-05-16 15:25:00", 101.0, 103.0, 100.5, 102.5, 6000),
    ]
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"TEST": 12345},
        num_candles=30,
        interval_minutes=5,
    )
    assert "TEST" in result
    assert len(result["TEST"]) == 2
    first = result["TEST"][0]
    assert "time" in first, "Expected 'time' key (renamed from 'date')"
    assert first["time"] == datetime(2026, 5, 16, 15, 20)
    assert first["open"] == 100.0
    assert first["high"] == 102.0
    assert first["low"] == 99.0
    assert first["close"] == 101.0
    assert first["volume"] == 5000
    # And kite was called with the right interval
    kw = kite.historical_data.call_args.kwargs
    assert kw["interval"] == "5minute"
    assert kw["instrument_token"] == 12345
    print("✅ Test 1: Happy path — bars normalised correctly")


# ── Test 2: Multi-symbol fetch ─────────────────────────────────
def test_multi_symbol():
    kite = MagicMock()
    # Different return value per call
    kite.historical_data.side_effect = [
        [make_kite_bar("2026-05-16 15:20:00", 100, 101, 99, 100, 1000)],
        [make_kite_bar("2026-05-16 15:20:00", 200, 201, 199, 200, 2000)],
    ]
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"A": 1, "B": 2},
        num_candles=30,
    )
    assert set(result.keys()) == {"A", "B"}
    assert result["A"][0]["open"] == 100
    assert result["B"][0]["open"] == 200
    assert kite.historical_data.call_count == 2
    print("✅ Test 2: Multi-symbol fetch — each symbol fetched separately")


# ── Test 3: Empty result handled ───────────────────────────────
def test_empty_kite_response():
    kite = MagicMock()
    kite.historical_data.return_value = []
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"TEST": 12345},
    )
    assert result["TEST"] == [], f"Expected empty list, got {result['TEST']}"
    print("✅ Test 3: Empty Kite response → empty list, no crash")


# ── Test 4: Kite raises exception ──────────────────────────────
def test_kite_exception_caught():
    kite = MagicMock()
    kite.historical_data.side_effect = RuntimeError("Kite API rate limit")
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"TEST": 12345},
    )
    # Should return empty list for failed symbol, not crash
    assert result["TEST"] == [], "Failed fetch should produce empty list"
    print("✅ Test 4: Kite exception → caught, empty result, no engine crash")


# ── Test 5: One symbol fails, others succeed ───────────────────
def test_partial_failure():
    kite = MagicMock()
    kite.historical_data.side_effect = [
        [make_kite_bar("2026-05-16 15:20:00", 100, 101, 99, 100, 1000)],
        RuntimeError("token expired"),
        [make_kite_bar("2026-05-16 15:20:00", 200, 201, 199, 200, 2000)],
    ]
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"A": 1, "B": 2, "C": 3},
    )
    assert len(result["A"]) == 1
    assert result["B"] == []   # the one that failed
    assert len(result["C"]) == 1
    print("✅ Test 5: Partial failure (1 of 3 errored) — others succeed")


# ── Test 6: Invalid interval rejected ──────────────────────────
def test_invalid_interval():
    kite = MagicMock()
    seeder = HistoricalSeeder(kite)
    try:
        seeder.fetch_for_symbols(
            instrument_map={"TEST": 12345},
            interval_minutes=7,   # not in KITE_INTERVAL_MAP
        )
        raise AssertionError("Expected ValueError for unsupported interval")
    except ValueError as e:
        assert "interval_minutes" in str(e)
    print("✅ Test 6: Invalid interval_minutes → ValueError raised")


# ── Test 7: num_candles trims correctly ────────────────────────
def test_trimming():
    kite = MagicMock()
    # Return 50 bars
    bars = [
        make_kite_bar(f"2026-05-16 {15-(50-i)//12:02d}:{(((50-i)*5)%60):02d}:00",
                      100+i, 101+i, 99+i, 100+i, 1000+i)
        for i in range(50)
    ]
    kite.historical_data.return_value = bars
    seeder = HistoricalSeeder(kite)
    result = seeder.fetch_for_symbols(
        instrument_map={"TEST": 12345},
        num_candles=10,
    )
    assert len(result["TEST"]) == 10, f"Expected 10, got {len(result['TEST'])}"
    # Should be the LAST 10
    assert result["TEST"][-1]["open"] == 100 + 49  # last bar opens at 149
    print("✅ Test 7: num_candles=10 trims 50 bars → keeps last 10")


# ── Test 8: None kite client ───────────────────────────────────
def test_none_kite():
    seeder = HistoricalSeeder(kite_rest=None)
    result = seeder.fetch_for_symbols(
        instrument_map={"TEST": 12345},
    )
    assert result["TEST"] == [], "None kite should produce empty list, not crash"
    print("✅ Test 8: None kite client → empty result, no crash")


def main():
    print("=" * 70)
    print("  HistoricalSeeder v9.5 — Tests")
    print("=" * 70)
    print()

    tests = [
        test_happy_path,
        test_multi_symbol,
        test_empty_kite_response,
        test_kite_exception_caught,
        test_partial_failure,
        test_invalid_interval,
        test_trimming,
        test_none_kite,
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
    print("✅ All tests pass — v9.5 HistoricalSeeder verified")


if __name__ == "__main__":
    main()

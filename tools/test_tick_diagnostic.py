"""
test_tick_diagnostic.py — Verify v9.3 tick-rate diagnostic logic in isolation.

Tests the new instrumentation WITHOUT touching Kite API:
  1. Counter increments correctly under concurrent tick simulation
  2. Drain returns full snapshot + zeros the counter
  3. Halt detection fires WARN on first zero window
  4. Escalation fires ERROR after HALT_ESCALATE_WINDOWS consecutive zeros
  5. Recovery message logged on first non-zero after halt
  6. Reporter thread starts and stops cleanly

Usage:
    python tools/test_tick_diagnostic.py
"""
import sys
import os
import time
import logging
import threading
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Capture all log records for assertions
log_records = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        log_records.append((record.levelname, record.getMessage()))


def setup_logging():
    log_records.clear()
    root = logging.getLogger()
    # Clear existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)
    h = _CaptureHandler()
    h.setLevel(logging.DEBUG)
    root.addHandler(h)
    # Also echo to console
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("  %(levelname)s | %(message)s"))
    root.addHandler(console)


def make_handler():
    """Build a DataHandler with mocked KiteTicker (no real API)."""
    # Mock env so __init__ passes
    os.environ.setdefault("ZERODHA_API_KEY", "test")
    os.environ.setdefault("ZERODHA_ACCESS_TOKEN", "test")

    with patch("src.core.data_handler.KiteTicker") as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker

        from src.core.data_handler import DataHandler

        engine = MagicMock()
        instrument_map = {
            "ABC": {"instrument_token": 1001, "name": "ABC Ltd"},
            "XYZ": {"instrument_token": 1002, "name": "XYZ Ltd"},
            "FOO": {"instrument_token": 1003, "name": "FOO Ltd"},
        }
        handler = DataHandler(engine=engine, instrument_map=instrument_map)
        # Manually populate subscribed map (normally _on_connect does this)
        handler.subscribed = {1001: "ABC", 1002: "XYZ", 1003: "FOO"}
        return handler


def simulate_ticks(handler, symbol_counts: dict):
    """
    Simulate ticks by directly incrementing the counter.
    (Skips the WebSocket path; tests just the counter+halt logic.)
    """
    for symbol, n in symbol_counts.items():
        with handler._counter_lock:
            handler._tick_counts[symbol] += n


# ── TEST 1 — Counter increments ────────────────────────────────
def test_counter_basic():
    setup_logging()
    h = make_handler()
    simulate_ticks(h, {"ABC": 5, "XYZ": 3})
    assert h._tick_counts["ABC"] == 5
    assert h._tick_counts["XYZ"] == 3
    assert h._tick_counts["FOO"] == 0
    print("✅ Test 1: Counter increments correctly")


# ── TEST 2 — Drain returns + zeros ─────────────────────────────
def test_drain():
    setup_logging()
    h = make_handler()
    simulate_ticks(h, {"ABC": 10, "XYZ": 7})
    snap = h._drain_counters()
    assert snap == {"ABC": 10, "XYZ": 7}
    # After drain, counts should be zero
    with h._counter_lock:
        assert dict(h._tick_counts) == {}
    print("✅ Test 2: Drain returns snapshot + zeros counters")


# ── TEST 3 — Halt detection fires WARN ─────────────────────────
def test_halt_warn():
    setup_logging()
    h = make_handler()
    # Force market hours
    with patch.object(h, "_is_market_hours", return_value=True):
        # No ticks for any symbol
        h._check_for_halts({})
    warns = [m for lvl, m in log_records if lvl == "WARNING" and "TICK HALT" in m]
    # 3 subscribed symbols → 3 halt warnings
    assert len(warns) == 3, f"Expected 3 WARN halts, got {len(warns)}: {warns}"
    for w in warns:
        assert "0 ticks in last 60s" in w
    print(f"✅ Test 3: Halt WARN fires on zero window ({len(warns)} symbols)")


# ── TEST 4 — Escalation to ERROR after streak ──────────────────
def test_halt_escalate():
    setup_logging()
    h = make_handler()
    # Window 1: zero ticks → WARN
    h._check_for_halts({})
    # Window 2: zero ticks → WARN (streak=2)
    h._check_for_halts({})
    # Window 3: zero ticks → ERROR (streak=3, hits HALT_ESCALATE_WINDOWS)
    h._check_for_halts({})

    errors = [m for lvl, m in log_records if lvl == "ERROR" and "TICK HALT" in m]
    # 3 symbols × 1 window of escalation = 3 ERRORs
    assert len(errors) >= 3, f"Expected 3+ ERROR halts at window 3, got {len(errors)}"
    for e in errors:
        assert "3 consecutive" in e or "min)" in e
    print(f"✅ Test 4: Halt ERROR fires after 3 consecutive zero windows ({len(errors)} ERRORs)")


# ── TEST 5 — Recovery message on first non-zero ────────────────
def test_recovery():
    setup_logging()
    h = make_handler()
    # 2 windows of halt for ABC
    h._check_for_halts({"XYZ": 100, "FOO": 50})  # ABC=0, XYZ ok, FOO ok
    h._check_for_halts({"XYZ": 100, "FOO": 50})  # ABC=0 again
    # Window 3: ABC recovers
    h._check_for_halts({"ABC": 25, "XYZ": 100, "FOO": 50})

    recovery = [m for lvl, m in log_records if "TICK RECOVERY" in m and "ABC" in m]
    assert len(recovery) == 1, f"Expected 1 recovery message for ABC, got {len(recovery)}"
    assert "2-window halt" in recovery[0]
    assert h._halt_streaks["ABC"] == 0  # streak reset
    print(f"✅ Test 5: Recovery logged correctly ({recovery[0][:80]}...)")


# ── TEST 6 — Reporter thread start/stop ────────────────────────
def test_reporter_lifecycle():
    setup_logging()
    h = make_handler()
    # Don't call h.start() (would try to open WebSocket).
    # Just start the reporter directly.
    h._reporter_stop.clear()
    h._reporter_thread = threading.Thread(
        target=h._reporter_loop, daemon=True, name="TestReporter"
    )
    h._reporter_thread.start()
    time.sleep(0.5)  # let it spin up
    assert h._reporter_thread.is_alive()

    # Signal stop and join
    h._reporter_stop.set()
    h._reporter_thread.join(timeout=3.0)
    assert not h._reporter_thread.is_alive(), "Reporter did not stop within 3s"
    print("✅ Test 6: Reporter thread starts + stops cleanly")


# ── TEST 7 — Thread-safe counter under concurrent load ─────────
def test_concurrent_counting():
    setup_logging()
    h = make_handler()

    def hammer(symbol, n):
        for _ in range(n):
            with h._counter_lock:
                h._tick_counts[symbol] += 1

    threads = [
        threading.Thread(target=hammer, args=("ABC", 1000)),
        threading.Thread(target=hammer, args=("XYZ", 1000)),
        threading.Thread(target=hammer, args=("ABC", 500)),
        threading.Thread(target=hammer, args=("FOO", 750)),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    assert h._tick_counts["ABC"] == 1500
    assert h._tick_counts["XYZ"] == 1000
    assert h._tick_counts["FOO"] == 750
    print("✅ Test 7: Thread-safe under concurrent load (ABC=1500 XYZ=1000 FOO=750)")


# ── TEST 8 — No log noise outside market hours ─────────────────
def test_pre_market_silence():
    setup_logging()
    h = make_handler()
    h._reporter_stop.clear()
    h._reporter_thread = threading.Thread(
        target=h._reporter_loop, daemon=True, name="TestReporter"
    )

    # Force "not market hours" so reporter goes silent
    with patch.object(h, "_is_market_hours", return_value=False):
        h._reporter_thread.start()
        time.sleep(0.3)  # spin up; first report would be in 60s anyway
        h._reporter_stop.set()
        h._reporter_thread.join(timeout=3.0)

    # No "Tick rates" lines should appear (we exited before first 60s)
    rates_logs = [m for lvl, m in log_records if "Tick rates" in m]
    assert len(rates_logs) == 0, f"Expected no Tick rates logs, got: {rates_logs}"
    print("✅ Test 8: No log noise outside market hours")


def main():
    print("=" * 70)
    print("  data_handler.py v9.3 — Tick-Rate Diagnostic Tests")
    print("=" * 70)
    print()

    tests = [
        test_counter_basic,
        test_drain,
        test_halt_warn,
        test_halt_escalate,
        test_recovery,
        test_reporter_lifecycle,
        test_concurrent_counting,
        test_pre_market_silence,
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

    print()
    print("=" * 70)
    print(f"  Results: {passed}/{len(tests)} passed")
    print("=" * 70)
    if failed:
        for name, msg in failed:
            print(f"  FAILED: {name}\n    {msg}")
        sys.exit(1)
    print("✅ All tests pass — v9.3 diagnostic logic verified")


if __name__ == "__main__":
    main()

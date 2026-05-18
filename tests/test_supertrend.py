"""
test_supertrend.py — Standalone tests for v9.4 Supertrend filter.

Tests:
  1. compute_supertrend returns sensible direction array on synthetic uptrend
  2. compute_supertrend returns sensible direction array on synthetic downtrend
  3. compute_supertrend flips on a clear trend reversal
  4. RSIMomentum filter BLOCKS BUY when Supertrend is RED
  5. RSIMomentum filter BLOCKS SELL when Supertrend is GREEN
  6. RSIMomentum filter PASSES aligned signals
  7. Filter fails-open on insufficient data
  8. Disabling the filter (USE_SUPERTREND_FILTER=False) lets everything through

Usage:
    python tools/test_supertrend.py
"""
import sys
import os
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.indicators import compute_supertrend


# ── Test 1: Synthetic uptrend ─────────────────────────────────
def test_uptrend():
    """Steadily rising prices should end in GREEN (direction=+1)."""
    n = 50
    base = 100.0
    closes = np.array([base + i * 0.5 + np.random.uniform(-0.1, 0.1) for i in range(n)])
    highs = closes + 0.3
    lows = closes - 0.3
    _, direction = compute_supertrend(highs, lows, closes, period=10, multiplier=3.0)
    # After enough bars, direction should be +1 (GREEN)
    final_dir = direction[-1]
    assert final_dir == 1, f"Expected GREEN (+1) on uptrend, got {final_dir}"
    print(f"✅ Test 1: Uptrend → final direction = +1 (GREEN)")


# ── Test 2: Synthetic downtrend ───────────────────────────────
def test_downtrend():
    """Steadily falling prices should end in RED (direction=-1)."""
    n = 50
    base = 100.0
    closes = np.array([base - i * 0.5 + np.random.uniform(-0.1, 0.1) for i in range(n)])
    highs = closes + 0.3
    lows = closes - 0.3
    _, direction = compute_supertrend(highs, lows, closes, period=10, multiplier=3.0)
    final_dir = direction[-1]
    assert final_dir == -1, f"Expected RED (-1) on downtrend, got {final_dir}"
    print(f"✅ Test 2: Downtrend → final direction = -1 (RED)")


# ── Test 3: Trend reversal ────────────────────────────────────
def test_reversal():
    """Downtrend → strong uptrend should flip direction at some point."""
    n_down = 30
    n_up = 30
    base = 100.0
    downs = np.array([base - i * 0.6 for i in range(n_down)])
    ups = np.array([downs[-1] + (i + 1) * 1.5 for i in range(n_up)])
    closes = np.concatenate([downs, ups])
    highs = closes + 0.3
    lows = closes - 0.3
    _, direction = compute_supertrend(highs, lows, closes, period=10, multiplier=3.0)

    # Should be RED in first phase
    mid_down = direction[n_down - 5]   # near end of downtrend
    # Should be GREEN by end of uptrend
    final_dir = direction[-1]

    assert mid_down == -1, f"Expected RED in down phase, got {mid_down}"
    assert final_dir == 1, f"Expected GREEN at end of up phase, got {final_dir}"
    print(f"✅ Test 3: Reversal → flipped from RED to GREEN as expected")


# ── Test 4: Insufficient data returns NaN ─────────────────────
def test_insufficient_data():
    n = 5
    closes = np.linspace(100, 105, n)
    highs = closes + 0.5
    lows = closes - 0.5
    st, direction = compute_supertrend(highs, lows, closes, period=10, multiplier=3.0)
    # All values should be NaN since n < period+1
    assert np.all(np.isnan(direction)), "Expected NaN array on insufficient data"
    print(f"✅ Test 4: Insufficient data → all NaN as expected")


# ── Test 5: RSIMomentum filter logic (BUY in RED regime) ──────
def test_filter_blocks_buy_in_red():
    """Mock the strategy filter without running the full pipeline."""
    from unittest.mock import MagicMock
    from src.strategies.rsi_momentum import RSIMomentum

    # Construct synthetic DOWN-trending data → Supertrend = RED
    n = 50
    closes = np.array([200 - i * 0.5 for i in range(n)])
    highs = closes + 0.5
    lows = closes - 0.5

    # Make a minimally-initialised strategy instance
    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.0)
    passes, regime = strat._check_supertrend_filter("BUY", highs, lows, closes)
    assert regime == "RED", f"Expected RED regime, got {regime}"
    assert passes is False, f"Expected BUY to be BLOCKED in RED regime, got passes={passes}"
    print(f"✅ Test 5: BUY in RED regime → BLOCKED")


# ── Test 6: RSIMomentum filter logic (SELL in GREEN regime) ───
def test_filter_blocks_sell_in_green():
    from unittest.mock import MagicMock
    from src.strategies.rsi_momentum import RSIMomentum

    # Construct synthetic UP-trending data → Supertrend = GREEN
    n = 50
    closes = np.array([200 + i * 0.5 for i in range(n)])
    highs = closes + 0.5
    lows = closes - 0.5

    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.0)
    passes, regime = strat._check_supertrend_filter("SELL", highs, lows, closes)
    assert regime == "GREEN", f"Expected GREEN regime, got {regime}"
    assert passes is False, f"Expected SELL to be BLOCKED in GREEN regime, got passes={passes}"
    print(f"✅ Test 6: SELL in GREEN regime → BLOCKED")


# ── Test 7: RSIMomentum filter logic (aligned signals pass) ───
def test_filter_passes_aligned():
    from unittest.mock import MagicMock
    from src.strategies.rsi_momentum import RSIMomentum

    # Uptrend → BUY should pass
    n = 50
    up_closes = np.array([200 + i * 0.5 for i in range(n)])
    up_highs  = up_closes + 0.5
    up_lows   = up_closes - 0.5
    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.0)
    passes, regime = strat._check_supertrend_filter("BUY", up_highs, up_lows, up_closes)
    assert regime == "GREEN" and passes is True, f"Aligned BUY/GREEN failed: regime={regime}, passes={passes}"

    # Downtrend → SELL should pass
    dn_closes = np.array([200 - i * 0.5 for i in range(n)])
    dn_highs  = dn_closes + 0.5
    dn_lows   = dn_closes - 0.5
    strat2 = RSIMomentum(engine, "TEST2", sentiment_score=0.0)
    passes, regime = strat2._check_supertrend_filter("SELL", dn_highs, dn_lows, dn_closes)
    assert regime == "RED" and passes is True, f"Aligned SELL/RED failed: regime={regime}, passes={passes}"
    print(f"✅ Test 7: Aligned signals (BUY+GREEN, SELL+RED) → PASS")


# ── Test 8: Insufficient data fails open ──────────────────────
def test_filter_fails_open():
    from unittest.mock import MagicMock
    from src.strategies.rsi_momentum import RSIMomentum

    # Only 5 candles — well below SUPERTREND_PERIOD=10
    closes = np.array([100.0, 100.5, 101.0, 101.2, 101.5])
    highs  = closes + 0.5
    lows   = closes - 0.5
    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.0)
    passes, regime = strat._check_supertrend_filter("BUY", highs, lows, closes)
    assert passes is True, f"Expected fail-open on insufficient data, got passes={passes}"
    assert regime == "INSUFFICIENT", f"Expected INSUFFICIENT label, got {regime}"
    print(f"✅ Test 8: Insufficient data → fail-open (PASS, INSUFFICIENT)")


# ── Test 9: USE_SUPERTREND_FILTER=False bypass ────────────────
def test_filter_can_be_disabled():
    from unittest.mock import MagicMock, patch
    from src.strategies.rsi_momentum import RSIMomentum

    # Use DOWN data → would normally BLOCK a BUY
    closes = np.array([200 - i * 0.5 for i in range(50)])
    highs  = closes + 0.5
    lows   = closes - 0.5

    engine = MagicMock()
    strat = RSIMomentum(engine, "TEST", sentiment_score=0.0)
    # Patch the module-level flag
    with patch("src.strategies.rsi_momentum.USE_SUPERTREND_FILTER", False):
        passes, regime = strat._check_supertrend_filter("BUY", highs, lows, closes)
    assert passes is True, f"Disabled filter should always pass, got {passes}"
    assert regime == "DISABLED", f"Expected DISABLED label, got {regime}"
    print(f"✅ Test 9: Disabled filter → bypass (PASS, DISABLED)")


def main():
    print("=" * 70)
    print("  Supertrend Indicator + RSIMomentum v9.4 Filter — Tests")
    print("=" * 70)
    print()
    np.random.seed(42)  # reproducibility

    tests = [
        test_uptrend,
        test_downtrend,
        test_reversal,
        test_insufficient_data,
        test_filter_blocks_buy_in_red,
        test_filter_blocks_sell_in_green,
        test_filter_passes_aligned,
        test_filter_fails_open,
        test_filter_can_be_disabled,
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
    print("✅ All tests pass — v9.4 Supertrend indicator + filter verified")


if __name__ == "__main__":
    main()

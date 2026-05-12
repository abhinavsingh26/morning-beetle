"""
Ranker — composite scoring function for Signal Boy.

Purpose:
    Given a list of scored candidate signals (each with sentiment_score,
    headline, sector, sector_bias), compute a composite score and return
    the top N ranked candidates above a minimum threshold.

Design:
    Pure function. No I/O, no threading, no state.
    Stateless: same input → same output, every time.
    Cheap: ~100µs per candidate.

Composite Score Formula:
    composite = (
        abs(sentiment_score)  * 0.40 +   # FinBERT signal strength
        catalyst_strength     * 0.35 +   # Headline keyword catalysts
        sector_alignment      * 0.25     # Sector convergence bonus
    )

    Range: 0.0 to 1.0

Default thresholds:
    MIN_COMPOSITE_SCORE = 0.60  (anything below dropped)
    MAX_ACTIVE_SIGNALS  = 15    (top N kept after filtering)

These are tuned for "produces ~5-10 high-conviction signals per scan"
which is the sweet spot for Stage 5b/5c.

Author: Abhinav (Phase 6D.2, May 2026)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Tuning constants ─────────────────────────────────────────────────
SENTIMENT_WEIGHT = 0.40
CATALYST_WEIGHT  = 0.35
SECTOR_WEIGHT    = 0.25

# Validate weights sum to 1.0 (catches drift if someone edits the values)
assert abs((SENTIMENT_WEIGHT + CATALYST_WEIGHT + SECTOR_WEIGHT) - 1.0) < 0.001, \
    "Composite score weights must sum to 1.0"

MIN_COMPOSITE_SCORE_DEFAULT = 0.60
MAX_ACTIVE_SIGNALS_DEFAULT  = 15


# ── Catalyst keyword tiers ───────────────────────────────────────────
# Higher tier = stronger market reaction historically.
# Catalyst strength is computed as the highest-tier keyword present.

CATALYST_TIER_HIGH = {
    # Earnings beats / misses are the strongest single-stock movers
    "Q1 RESULTS BEAT", "Q2 RESULTS BEAT", "Q3 RESULTS BEAT", "Q4 RESULTS BEAT",
    "RESULTS BEAT", "BEATS ESTIMATES", "BEAT ESTIMATES", "BEAT EXPECTATIONS",
    "PROFIT JUMPS", "PROFIT SURGES", "PAT JUMPS", "PAT RISES", "PAT DOUBLES",
    "REVENUE DOUBLES", "REVENUE SURGES", "EBITDA SURGES",
    # Corporate actions with big price impact
    "ORDER WIN", "WINS ORDER", "LARGE ORDER", "MEGA ORDER",
    "WINS CONTRACT", "BAGS ORDER", "BAGS CONTRACT",
    "RECEIVES ORDER", "SECURES ORDER", "ORDER FROM",
    "ACQUISITION", "MERGER", "BUYBACK ANNOUNCED",
    "BONUS ISSUE", "STOCK SPLIT",
    # Rating actions
    "RATING UPGRADE", "UPGRADED TO BUY", "TARGET RAISED",
}

CATALYST_TIER_MEDIUM = {
    # Earnings without explicit beat/miss
    "Q1 RESULTS", "Q2 RESULTS", "Q3 RESULTS", "Q4 RESULTS",
    "QUARTERLY RESULTS", "FY26 RESULTS", "FY27 RESULTS",
    "NET PROFIT", "REVENUE GROWTH", "NII",
    # Capital actions
    "DIVIDEND", "RIGHTS ISSUE", "PREFERENTIAL ISSUE", "DEMERGER",
    "STAKE SALE", "STAKE PURCHASE",
    # Operational positives
    "CONTRACT", "DEAL", "PARTNERSHIP", "TIE-UP",
    "GUIDANCE RAISED", "OUTLOOK POSITIVE",
}

CATALYST_TIER_LOW = {
    # Generic news — still indicates something happened, but mild
    "ANNOUNCES", "STATEMENT", "COMMENT", "OUTLOOK",
    "MANAGEMENT", "INVESTOR", "CONFERENCE",
}

CATALYST_TIER_SCORES = {
    "high":   1.00,
    "medium": 0.65,
    "low":    0.30,
    "none":   0.10,
}


# ── Core functions ──────────────────────────────────────────────────
def compute_catalyst_strength(headline: str) -> float:
    """
    Return catalyst strength score in [0.0, 1.0] based on keyword presence.

    Tiers:
        HIGH    → 1.00  (earnings beat, order win, acquisition, rating upgrade)
        MEDIUM  → 0.65  (results, dividend, contract, partnership)
        LOW     → 0.30  (generic announcement, statement)
        none    → 0.10  (no catalyst keywords detected)

    Highest-tier match wins. Multiple keywords don't stack.
    """
    if not headline:
        return CATALYST_TIER_SCORES["none"]

    headline_upper = headline.upper()

    for kw in CATALYST_TIER_HIGH:
        if kw in headline_upper:
            return CATALYST_TIER_SCORES["high"]

    for kw in CATALYST_TIER_MEDIUM:
        if kw in headline_upper:
            return CATALYST_TIER_SCORES["medium"]

    for kw in CATALYST_TIER_LOW:
        if kw in headline_upper:
            return CATALYST_TIER_SCORES["low"]

    return CATALYST_TIER_SCORES["none"]


def compute_sector_alignment(sentiment_label: str, sector_bias: str) -> float:
    """
    Return sector alignment score in [0.0, 1.0].

    Rules:
        BULLISH sentiment + BULLISH sector → 1.0 (perfect alignment)
        BEARISH sentiment + BEARISH sector → 1.0 (perfect alignment)
        BULLISH + BEARISH                  → 0.0 (opposed — should drop)
        BEARISH + BULLISH                  → 0.0 (opposed — should drop)
        anything + NEUTRAL                 → 0.5 (no info either way)
        anything + UNKNOWN                 → 0.5 (no sector mapping)

    Note: Opposed cases (0.0) are typically filtered out earlier by
    the convergence gate in intelligence.py, but this scoring still
    protects Signal Boy's queue if such a candidate slips through.
    """
    sentiment_label = (sentiment_label or "").upper()
    sector_bias     = (sector_bias or "").upper()

    if sector_bias in ("NEUTRAL", "UNKNOWN", ""):
        return 0.5

    if sentiment_label == "BULLISH" and sector_bias == "BULLISH":
        return 1.0
    if sentiment_label == "BEARISH" and sector_bias == "BEARISH":
        return 1.0
    if sentiment_label == "BULLISH" and sector_bias == "BEARISH":
        return 0.0
    if sentiment_label == "BEARISH" and sector_bias == "BULLISH":
        return 0.0

    # Unknown sentiment label
    return 0.5


def compute_composite_score(candidate: dict) -> float:
    """
    Compute a single candidate's composite score in [0.0, 1.0].

    Expected candidate dict keys:
        sentiment_score (float, -1.0 to +1.0)   ← required
        sentiment_label (str, BULLISH/BEARISH)  ← required
        sector_bias     (str)                    ← required
        headline        (str)                    ← required

    Missing keys are treated as worst-case (alignment 0.5, catalyst 0.1, etc.)
    """
    sentiment_score = float(candidate.get("sentiment_score", 0.0))
    sentiment_label = candidate.get("sentiment_label", "")
    sector_bias     = candidate.get("sector_bias", "")
    headline        = candidate.get("headline", "")

    sentiment_strength = min(abs(sentiment_score), 1.0)
    catalyst_strength  = compute_catalyst_strength(headline)
    sector_alignment   = compute_sector_alignment(sentiment_label, sector_bias)

    composite = (
        sentiment_strength * SENTIMENT_WEIGHT +
        catalyst_strength  * CATALYST_WEIGHT +
        sector_alignment   * SECTOR_WEIGHT
    )
    return round(composite, 4)


def rank_signals(candidates: list,
                 max_n: int = MAX_ACTIVE_SIGNALS_DEFAULT,
                 min_score: float = MIN_COMPOSITE_SCORE_DEFAULT) -> list:
    """
    Rank a list of candidates and return the top N above min_score.

    Args:
        candidates: list of candidate dicts (see compute_composite_score).
        max_n: maximum number of candidates to return.
        min_score: minimum composite score required to make the cut.

    Returns:
        Sorted list of candidates with new fields:
            - composite_score (float)
            - rank (1-indexed int)
            - catalyst_strength (float)
            - sector_alignment (float)

    Below-threshold candidates are dropped.
    Empty input returns empty list.
    """
    if not candidates:
        return []

    enriched = []
    for c in candidates:
        try:
            composite = compute_composite_score(c)
            if composite < min_score:
                continue

            enriched.append({
                **c,
                "composite_score":   composite,
                "catalyst_strength": compute_catalyst_strength(
                    c.get("headline", "")),
                "sector_alignment":  compute_sector_alignment(
                    c.get("sentiment_label", ""),
                    c.get("sector_bias", "")),
            })
        except Exception as e:
            logger.warning(f"  Ranker: skipping bad candidate "
                          f"({c.get('symbol', '?')}): {e}")
            continue

    # Sort descending by composite_score, tie-break by sentiment magnitude
    enriched.sort(
        key=lambda x: (
            x["composite_score"],
            abs(float(x.get("sentiment_score", 0.0))),
        ),
        reverse=True
    )

    # Truncate to top N and assign rank
    top = enriched[:max_n]
    for i, item in enumerate(top, start=1):
        item["rank"] = i

    return top


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Standalone test — no engine dependency.
    Verifies catalyst tiers, sector alignment matrix, composite scoring,
    ranking, threshold filtering, max_n cap, and edge cases.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  Ranker — Standalone Test (Phase 6D.2)")
    print("=" * 60 + "\n")

    tests_passed = 0
    tests_failed = 0

    def check(label, actual, expected):
        global tests_passed, tests_failed
        ok = (
            abs(actual - expected) < 0.001
            if isinstance(actual, float) and isinstance(expected, float)
            else actual == expected
        )
        status = "✅" if ok else "❌"
        print(f"  {status} {label}: got={actual} expected={expected}")
        if ok:
            tests_passed += 1
        else:
            tests_failed += 1

    # ── Test group 1: catalyst tiers ─────────────────────────────
    print("[1/6] Catalyst keyword tiers")
    check(
        "Q4 RESULTS BEAT → HIGH",
        compute_catalyst_strength("Reliance Q4 results beat estimates"),
        1.0
    )
    check(
        "ORDER WIN → HIGH",
        compute_catalyst_strength("HAL wins ₹40,000 cr order from MoD"),
        1.0
    )
    check(
        "DIVIDEND → MEDIUM",
        compute_catalyst_strength("Sasken declares ₹25 dividend"),
        0.65
    )
    check(
        "ANNOUNCES (generic) → LOW",
        compute_catalyst_strength("Company announces management changes"),
        0.30
    )
    check(
        "No keywords → none",
        compute_catalyst_strength("Markets close mixed on Tuesday"),
        0.10
    )
    check(
        "Empty headline → none",
        compute_catalyst_strength(""),
        0.10
    )
    print()

    # ── Test group 2: sector alignment ───────────────────────────
    print("[2/6] Sector alignment matrix")
    check("BULLISH + BULLISH → 1.0", compute_sector_alignment("BULLISH", "BULLISH"), 1.0)
    check("BEARISH + BEARISH → 1.0", compute_sector_alignment("BEARISH", "BEARISH"), 1.0)
    check("BULLISH + BEARISH → 0.0", compute_sector_alignment("BULLISH", "BEARISH"), 0.0)
    check("BEARISH + BULLISH → 0.0", compute_sector_alignment("BEARISH", "BULLISH"), 0.0)
    check("BULLISH + NEUTRAL → 0.5", compute_sector_alignment("BULLISH", "NEUTRAL"), 0.5)
    check("BULLISH + UNKNOWN → 0.5", compute_sector_alignment("BULLISH", "UNKNOWN"), 0.5)
    check("Empty + UNKNOWN → 0.5",   compute_sector_alignment("",        "UNKNOWN"), 0.5)
    print()

    # ── Test group 3: composite score correctness ───────────────
    print("[3/6] Composite score formula")
    # Perfect candidate: strong sentiment + high catalyst + aligned sector
    perfect = {
        "sentiment_score": 0.95,
        "sentiment_label": "BULLISH",
        "sector_bias":     "BULLISH",
        "headline":        "Polycab Q4 results beat estimates, profit jumps 32%",
    }
    # Expected: 0.95 * 0.40 + 1.0 * 0.35 + 1.0 * 0.25 = 0.38 + 0.35 + 0.25 = 0.98
    score = compute_composite_score(perfect)
    check("Perfect candidate → ~0.98", score, 0.98)

    # Mediocre: weak sentiment, medium catalyst, no sector data
    mediocre = {
        "sentiment_score": 0.35,
        "sentiment_label": "BULLISH",
        "sector_bias":     "UNKNOWN",
        "headline":        "Marico Q4 results, revenue grows YoY",
    }
    # Expected: 0.35 * 0.40 + 0.65 * 0.35 + 0.5 * 0.25 = 0.14 + 0.2275 + 0.125 = 0.4925
    score = compute_composite_score(mediocre)
    check("Mediocre candidate → ~0.49", score, 0.4925)

    # Opposed sentiment vs sector — should be low
    opposed = {
        "sentiment_score": 0.90,
        "sentiment_label": "BULLISH",
        "sector_bias":     "BEARISH",
        "headline":        "Some bullish news",
    }
    # Expected: 0.90 * 0.40 + 0.10 * 0.35 + 0.0 * 0.25 = 0.36 + 0.035 + 0.0 = 0.395
    score = compute_composite_score(opposed)
    check("Opposed candidate → ~0.395", score, 0.395)
    print()

    # ── Test group 4: ranking + threshold ───────────────────────
    print("[4/6] rank_signals — threshold + ordering")
    candidates = [
        {"symbol": "POLYCAB",  **perfect},
        {"symbol": "MARICO",   **mediocre},
        {"symbol": "BADSTOCK", **opposed},
        {"symbol": "CANBK", "sentiment_score": -0.92, "sentiment_label": "BEARISH",
         "sector_bias": "BEARISH",
         "headline": "Canara Bank: Q4 results, target trimmed by Motilal Oswal"},
    ]
    ranked = rank_signals(candidates, max_n=10, min_score=0.60)
    check("Default threshold drops MARICO and BADSTOCK",
          len(ranked), 2)
    check("Top rank is POLYCAB", ranked[0]["symbol"], "POLYCAB")
    check("Second rank is CANBK", ranked[1]["symbol"], "CANBK")
    check("rank field is set",   ranked[0]["rank"], 1)
    check("composite_score is set on output",
          "composite_score" in ranked[0], True)
    print()

    # ── Test group 5: max_n cap ─────────────────────────────────
    print("[5/6] max_n cap behavior")
    many = [
        {
            "symbol":          f"SYM{i}",
            "sentiment_score": 0.95,
            "sentiment_label": "BULLISH",
            "sector_bias":     "BULLISH",
            "headline":        "Q4 results beat estimates"
        }
        for i in range(20)
    ]
    ranked = rank_signals(many, max_n=5, min_score=0.60)
    check("max_n=5 caps output", len(ranked), 5)
    check("ranks are 1..5", [r["rank"] for r in ranked], [1, 2, 3, 4, 5])
    print()

    # ── Test group 6: edge cases ────────────────────────────────
    print("[6/6] Edge cases")
    check("Empty input → []", rank_signals([]), [])
    check("All below threshold → []",
          len(rank_signals([opposed], min_score=0.60)), 0)
    check("Lower threshold lets weak signals through",
          len(rank_signals([mediocre], min_score=0.40)), 1)

    # Malformed candidate — missing keys, bad types
    perfect_with_symbol = {"symbol": "POLYCAB", **perfect}
    malformed = [
        {"symbol": "BROKEN"},                                       # missing all fields
        {"symbol": "BADTYPE", "sentiment_score": "not_a_number"},   # bad type
        perfect_with_symbol,                                         # good one
        {},                                                          # empty dict — no symbol
    ]
    ranked = rank_signals(malformed, min_score=0.60)
    symbols_in_ranked = [r.get("symbol", "<NO_SYMBOL>") for r in ranked]
    check("Malformed candidates filtered, good one kept",
          symbols_in_ranked, ["POLYCAB"])
    print()

    # ── Summary ──
    total = tests_passed + tests_failed
    print("=" * 60)
    if tests_failed == 0:
        print(f"  ✅ ALL {total} TESTS PASSED")
    else:
        print(f"  ❌ {tests_failed}/{total} TESTS FAILED")
    print("=" * 60 + "\n")

    if tests_failed == 0:
        print("Signal Boy 6D.2 (Ranker) is working correctly.")
        print()
        print("Next: 6D.3 (SignalBoy orchestrator) — background thread, 15-min loop.")
        print("Build trigger: Thu 14 May evening.")
        print()

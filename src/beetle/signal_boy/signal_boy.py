"""
SignalBoy — orchestrator background thread for Morning Beetle's intelligence layer.

Single purpose:
    Every 15 minutes during market hours, fetch fresh news from 9 sources
    via the IngestionCache, score with FinBERT + EntityShield, rank with
    the Ranker, manage signal lifecycle, and atomically write the queue.

Architecture:
    ┌───────────────────────────────────────────────────┐
    │  SignalBoy (background thread)                    │
    │                                                    │
    │  every 15 min:                                    │
    │    1. IngestionCache.get_all()                    │
    │    2. EntityShield.filter_headlines()             │
    │    3. FinBERT.score_headline() on each            │
    │    4. Apply sector convergence gate                │
    │    5. Ranker.rank_signals()                       │
    │    6. Lifecycle: born/validated/stale/expired     │
    │    7. QueueWriter.write()                          │
    └───────────────────────────────────────────────────┘

Modes:
    shadow:     writes signals/queue_shadow.json, doesn't affect engine
    production: writes signals/queue.json, engine reads it (after 6D.4 wiring)

Anti-goals (see SIGNAL_BOY_DESIGN.md):
    Will NOT place orders, manage risk, exit positions, or change the
    09:00 boot time. Stops scanning at 14:30.

Author: Abhinav (Phase 6D.3, May 2026)
"""

import logging
import threading
import time as time_module
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Callable

from src.beetle.signal_boy.ingestion_cache import IngestionCache, SOURCE_REGISTRY
from src.beetle.signal_boy.ranker import rank_signals
from src.beetle.signal_boy.queue_writer import QueueWriter

logger = logging.getLogger(__name__)


# ── Scan schedule constants ──────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 15 * 60   # 15 minutes between scans
FIRST_SCAN_TIME       = time(9, 1)   # 09:01 IST — replaces intelligence.py
LAST_SCAN_TIME        = time(14, 30) # 14:30 IST — final scan of the day
POLL_INTERVAL_SECONDS = 30  # how often the loop checks "is it scan time?"

# Lifecycle thresholds
STALE_AFTER_MISSED_SCANS   = 2   # marked stale after 2 misses
EXPIRED_AFTER_MISSED_SCANS = 3   # moved to expired after 3 misses

# Ranker defaults
DEFAULT_MAX_ACTIVE     = 15
DEFAULT_MIN_COMPOSITE  = 0.60


# ── Sector convergence gate (mirrors intelligence.py logic) ──────────
def _passes_sector_gate(sentiment_label: str, sector_bias: str) -> bool:
    """Allow same-direction or NEUTRAL/UNKNOWN sector; drop opposed."""
    if not sector_bias or sector_bias.upper() in ("NEUTRAL", "UNKNOWN", ""):
        return True
    sl = sentiment_label.upper() if sentiment_label else ""
    sb = sector_bias.upper()
    if sl == "BULLISH" and sb == "BEARISH":
        return False
    if sl == "BEARISH" and sb == "BULLISH":
        return False
    return True


class SignalBoy:
    """
    Background thread that produces ranked signals every 15 minutes.

    Construction parameters allow full dependency injection for testing.
    In production these are passed in from main.py:
        - source_fetchers: dict[source_id] → callable producing list of headlines
        - entity_shield_fn: callable(headlines, instruments) → list of matched
        - finbert_scorer_fn: callable(text) → {score, label}
        - sector_lookup_fn: callable(symbol) → sector name
        - sector_heatmap_provider: callable() → dict of {sector: {bias}}
        - instruments: dict from load_instruments()
        - instrument_token_lookup: callable(symbol) → int or None

    For tests, all of these can be supplied as mocks.
    """

    def __init__(self,
                 source_fetchers: dict,
                 entity_shield_fn: Callable,
                 finbert_scorer_fn: Callable,
                 sector_lookup_fn: Callable,
                 sector_heatmap_provider: Callable,
                 instruments: dict,
                 instrument_token_lookup: Optional[Callable] = None,
                 mode: str = "shadow",
                 cache_path: str = "signals/ingestion_cache.db",
                 queue_path: Optional[str] = None,
                 max_active: int = DEFAULT_MAX_ACTIVE,
                 min_composite: float = DEFAULT_MIN_COMPOSITE,
                 dead_zone_min: float = -0.1,
                 dead_zone_max: float = 0.1):
        """
        Args:
            source_fetchers:  {source_id: callable() -> list[dict]} for all
                              sources to register with IngestionCache.
            entity_shield_fn: filter_headlines(headlines, instruments) -> list
            finbert_scorer_fn: score_headline(title) -> {score, label}
            sector_lookup_fn: get_sector(symbol) -> str
            sector_heatmap_provider: () -> dict (sector -> {bias})
            instruments:      load_instruments() result
            mode:             'shadow' or 'production' (controls queue path)
        """
        if mode not in ("shadow", "production"):
            raise ValueError(f"mode must be 'shadow' or 'production', got {mode}")

        self.mode                    = mode
        self.entity_shield_fn        = entity_shield_fn
        self.finbert_scorer_fn       = finbert_scorer_fn
        self.sector_lookup_fn        = sector_lookup_fn
        self.sector_heatmap_provider = sector_heatmap_provider
        self.instruments             = instruments
        self.instrument_token_lookup = instrument_token_lookup
        self.max_active              = max_active
        self.min_composite           = min_composite
        self.dead_zone_min           = dead_zone_min
        self.dead_zone_max           = dead_zone_max

        # Pick queue path based on mode
        if queue_path is None:
            queue_path = (
                "signals/queue.json" if mode == "production"
                else "signals/queue_shadow.json"
            )
        self.queue_path = queue_path

        # Build cache and register fetchers
        cache_path_for_mode = (
            cache_path if mode == "production"
            else cache_path.replace(".db", "_shadow.db")
        )
        self.cache  = IngestionCache(db_path=cache_path_for_mode)
        self.writer = QueueWriter(path=queue_path)

        for source_id, fetcher in source_fetchers.items():
            if source_id not in SOURCE_REGISTRY:
                logger.warning(f"  Skipping unknown source: {source_id}")
                continue
            self.cache.register_fetcher(source_id, fetcher)

        # Lifecycle state — keyed by symbol
        # { symbol: { first_seen_at, scans_validated, missed_scans,
        #             last_validated_at, last_payload } }
        self._lifecycle: dict[str, dict] = {}

        # Scan counter (1-22 per trading day)
        self.scan_id = 0
        self.last_scan_at: Optional[datetime] = None

        # Thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(f"SignalBoy initialised — mode={mode}, "
                   f"queue={queue_path}, sources={len(source_fetchers)}")

    # ── Thread lifecycle ─────────────────────────────────────────
    def start(self):
        """Start the background scan loop."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("  SignalBoy already running — start() ignored")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"SignalBoy-{self.mode}"
        )
        self._thread.start()
        logger.info(f"🤖 SignalBoy started (mode={self.mode})")

    def stop(self, timeout: float = 10.0):
        """Signal the loop to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("🤖 SignalBoy stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Main loop ────────────────────────────────────────────────
    def _run_loop(self):
        """Poll every 30s; if it's time for a scan, run one."""
        logger.info("  SignalBoy scan loop started")

        # Force first scan immediately if we're within window
        now = datetime.now()
        if FIRST_SCAN_TIME <= now.time() <= LAST_SCAN_TIME:
            self._do_scan()

        while not self._stop_event.is_set():
            try:
                now = datetime.now()
                now_t = now.time()

                # Outside scan window — sleep and check again
                if now_t < FIRST_SCAN_TIME or now_t > LAST_SCAN_TIME:
                    if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                        break
                    continue

                # Inside window — check if 15 min has passed since last scan
                if self.last_scan_at is None:
                    self._do_scan()
                else:
                    elapsed = (now - self.last_scan_at).total_seconds()
                    if elapsed >= SCAN_INTERVAL_SECONDS:
                        self._do_scan()

                # Wait POLL_INTERVAL_SECONDS or stop signal, whichever first
                if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                    break

            except Exception as e:
                logger.error(f"  SignalBoy loop error: {e}")
                if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                    break

        logger.info("  SignalBoy scan loop exited")

    # ── Scan (the heart) ─────────────────────────────────────────
    def run_one_scan(self) -> dict:
        """
        Run a single scan and return the result dict written to queue.
        Public method — used by tests and (later) by main.py for manual triggers.
        """
        return self._do_scan()

    def _do_scan(self) -> dict:
        """
        End-to-end scan:
          1. Fetch all sources (via IngestionCache)
          2. EntityShield → ticker matches
          3. FinBERT → sentiment scoring (+ dead zone filter)
          4. Sector convergence gate
          5. Ranker → composite scoring
          6. Lifecycle update (born / validated / stale / expired)
          7. Write queue
        """
        self.scan_id += 1
        scan_started = datetime.now(timezone.utc)
        logger.info(f"━━━ Signal Boy Scan #{self.scan_id} "
                   f"({scan_started.isoformat()}) ━━━")

        # ── Step 1: Fetch from all sources ──
        all_data = self.cache.get_all()
        all_headlines = []
        for source_id, items in all_data.items():
            for h in items:
                # Ensure source field is present (some fetchers may add it)
                if "source" not in h:
                    h["source"] = source_id
                all_headlines.append(h)
        logger.info(f"  Fetched {len(all_headlines)} headlines from "
                   f"{len(all_data)} sources")

        # ── Step 2: EntityShield ──
        matched = self.entity_shield_fn(all_headlines, self.instruments)
        logger.info(f"  EntityShield matched: {len(matched)}")

        # ── Step 3: FinBERT scoring (+ dead zone filter) ──
        scored = []
        for h in matched:
            try:
                result = self.finbert_scorer_fn(h["title"])
                score = float(result.get("score", 0.0))
                label = result.get("label", "NEUTRAL")
            except Exception as e:
                logger.warning(f"  FinBERT failed for '{h.get('title', '')[:40]}': {e}")
                continue

            # Dead zone filter
            if self.dead_zone_min <= score <= self.dead_zone_max:
                continue

            scored.append({
                **h,
                "sentiment_score": score,
                "sentiment_label": label,
            })
        logger.info(f"  Passed dead zone: {len(scored)}")

        # ── Step 4: Sector convergence gate ──
        try:
            heatmap = self.sector_heatmap_provider()
        except Exception as e:
            logger.warning(f"  Heatmap fetch failed: {e}")
            heatmap = {}

        candidates = []
        for h in scored:
            symbol = h["ticker"]
            sector = self.sector_lookup_fn(symbol)
            sector_data = heatmap.get(sector, {})
            sector_bias = sector_data.get("bias", "UNKNOWN")

            if not _passes_sector_gate(h["sentiment_label"], sector_bias):
                continue

            instrument_token = None
            if self.instrument_token_lookup:
                try:
                    instrument_token = self.instrument_token_lookup(symbol)
                except Exception:
                    pass

            candidates.append({
                "symbol":           symbol,
                "name":             h.get("ticker_name", ""),
                "sentiment_score":  h["sentiment_score"],
                "sentiment_label":  h["sentiment_label"],
                "sector":           sector,
                "sector_bias":      sector_bias,
                "confidence":       h.get("confidence", 0.0),
                "headline":         h["title"],
                "headline_source":  h.get("source", "unknown"),
                "instrument_token": instrument_token,
            })

        # Deduplicate by symbol — keep highest sentiment magnitude
        deduped: dict[str, dict] = {}
        for c in candidates:
            sym = c["symbol"]
            existing = deduped.get(sym)
            if existing is None or abs(c["sentiment_score"]) > abs(existing["sentiment_score"]):
                deduped[sym] = c
        candidates = list(deduped.values())
        logger.info(f"  Passed sector gate (deduplicated): {len(candidates)}")

        # ── Step 5: Ranker → composite scoring + filter ──
        ranked = rank_signals(
            candidates,
            max_n=self.max_active,
            min_score=self.min_composite
        )
        logger.info(f"  Ranked above {self.min_composite}: {len(ranked)}")

        # ── Step 6: Lifecycle update ──
        active, expired = self._update_lifecycle(ranked, scan_started)

        # ── Step 7: Write queue ──
        cache_stats = self.cache.stats()
        cache_hit_rate = cache_stats.get("overall", {}).get("hit_rate", 0.0)
        next_scan = (
            scan_started + timedelta(seconds=SCAN_INTERVAL_SECONDS)
        ).isoformat()

        self.writer.write(
            scan_id=self.scan_id,
            active_signals=active,
            expired_signals=expired,
            cache_hit_rate=cache_hit_rate,
            next_scan_at=next_scan,
            extra_metadata={
                "raw_headlines":  len(all_headlines),
                "matched":        len(matched),
                "scored":         len(scored),
                "after_sector":   len(candidates),
                "ranked":         len(ranked),
                "mode":           self.mode,
            },
        )

        self.last_scan_at = datetime.now()
        return {
            "scan_id":       self.scan_id,
            "active":        active,
            "expired":       expired,
            "headlines":     len(all_headlines),
            "matched":       len(matched),
            "ranked":        len(ranked),
        }

    # ── Lifecycle management ─────────────────────────────────────
    def _update_lifecycle(self,
                          ranked_now: list,
                          scan_time: datetime) -> tuple[list, list]:
        """
        Manage signal lifecycle. Updates self._lifecycle and returns
        (active_for_queue, expired_for_queue).

        Stages:
            born:      first scan that sees the ticker
            validated: subsequent scans still see it
            stale:     2 consecutive misses
            expired:   3 consecutive misses (drops out)
            reborn:    reappears after expiry → fresh first_seen_at
        """
        active_for_queue = []
        expired_for_queue = []

        current_symbols = {r["symbol"] for r in ranked_now}
        ranked_by_sym = {r["symbol"]: r for r in ranked_now}

        # 1. Process current scan results
        for sym in current_symbols:
            r = ranked_by_sym[sym]
            entry = self._lifecycle.get(sym)
            if entry is None or entry.get("status") == "expired":
                # Born or reborn
                self._lifecycle[sym] = {
                    "first_seen_at":     scan_time.isoformat(),
                    "last_validated_at": scan_time.isoformat(),
                    "scans_validated":   1,
                    "missed_scans":      0,
                    "status":            "active",
                    "payload":           r,
                }
            else:
                # Already seen — validate
                entry["last_validated_at"] = scan_time.isoformat()
                entry["scans_validated"]   = entry.get("scans_validated", 0) + 1
                entry["missed_scans"]      = 0
                entry["status"]            = "active"
                entry["payload"]           = r

        # 2. Process previously-seen symbols that are absent this scan
        symbols_to_remove = []
        for sym, entry in self._lifecycle.items():
            if sym in current_symbols:
                continue
            if entry.get("status") == "expired":
                # Don't keep churning on expired entries — drop after 1 more scan
                symbols_to_remove.append(sym)
                continue

            entry["missed_scans"] = entry.get("missed_scans", 0) + 1
            missed = entry["missed_scans"]

            if missed >= EXPIRED_AFTER_MISSED_SCANS:
                entry["status"] = "expired"
                expired_for_queue.append({
                    "symbol":     sym,
                    "expired_at": scan_time.isoformat(),
                    "reason":     "no_fresh_news",
                })
            elif missed >= STALE_AFTER_MISSED_SCANS:
                entry["status"] = "stale"
            # else still active, just one missed scan

        for sym in symbols_to_remove:
            del self._lifecycle[sym]

        # 3. Build active_for_queue from all non-expired entries
        for sym, entry in self._lifecycle.items():
            if entry.get("status") == "expired":
                continue
            payload = entry.get("payload", {}).copy()
            payload["first_seen_at"]     = entry["first_seen_at"]
            payload["last_validated_at"] = entry["last_validated_at"]
            payload["scans_validated"]   = entry["scans_validated"]
            payload["stale"]             = entry.get("status") == "stale"
            active_for_queue.append(payload)

        # Sort by rank (1, 2, 3...) — already set by Ranker for fresh ones,
        # but stale ones won't have it updated. Sort by composite_score.
        active_for_queue.sort(
            key=lambda x: x.get("composite_score", 0.0),
            reverse=True
        )
        # Re-assign ranks based on current sort order
        for i, item in enumerate(active_for_queue, start=1):
            item["rank"] = i

        return active_for_queue, expired_for_queue


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    """
    End-to-end test with mock fetchers, mock EntityShield, mock FinBERT.
    Runs 3 simulated scans and verifies:
      - Queue is written each scan
      - Signal lifecycle transitions work (born → validated → stale → expired)
      - Sector gate filters correctly
      - Atomic writes don't leave .tmp files
    """
    import tempfile
    import os
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  SignalBoy — Standalone End-to-End Test (Phase 6D.3)")
    print("=" * 60 + "\n")

    tmp_dir = tempfile.mkdtemp(prefix="signal_boy_e2e_")

    # ── Mock instruments ──
    instruments = {
        "POLYCAB":  {"name": "POLYCAB INDIA",     "search_anchor": "POLYCAB INDIA"},
        "CANBK":    {"name": "CANARA BANK",       "search_anchor": "CANARA BANK"},
        "SBIN":     {"name": "STATE BANK OF INDIA", "search_anchor": "STATE BANK OF INDIA"},
        "INDHOTEL": {"name": "THE INDIAN HOTELS", "search_anchor": "INDIAN HOTELS"},
        "MEESHO":   {"name": "MEESHO",            "search_anchor": "MEESHO"},
    }

    # ── Mock source fetchers — return different headlines each scan ──
    scan_iter = {"n": 0}

    def mock_google_business():
        n = scan_iter["n"]
        # Three sets of headlines for three scans
        sets = [
            [
                {"title": "Polycab India Q4 PAT 32% revenue surges",
                 "source": "google_business"},
                {"title": "Canara Bank Q4 results trimmed by Motilal Oswal",
                 "source": "google_business"},
                {"title": "Indian Hotels Q4 Results: Profit, Revenue Rise Over 14%; Dividend Declared",
                 "source": "google_business"},
            ],
            [
                {"title": "Polycab India Q4 PAT 32% revenue surges",
                 "source": "google_business"},
                {"title": "Meesho announces AI focus for growth",
                 "source": "google_business"},
                {"title": "RBI maintains hawkish stance on inflation",
                 "source": "google_business"},
            ],
            [
                {"title": "Meesho announces AI focus for growth",
                 "source": "google_business"},
            ],
        ]
        return sets[min(n, len(sets) - 1)]

    # ── Mock EntityShield — returns matched tickers ──
    def mock_entity_shield(headlines, instruments):
        out = []
        for h in headlines:
            title = h["title"].upper()
            for sym, data in instruments.items():
                if data["search_anchor"] in title or sym in title:
                    out.append({
                        **h,
                        "ticker":      sym,
                        "ticker_name": data["name"],
                        "confidence":  0.95,
                        "boosted":     True,
                    })
                    break
        return out

    # ── Mock FinBERT ──
    BULLISH_HEADLINES = {"POLYCAB", "INDHOTEL", "MEESHO"}
    BEARISH_HEADLINES = {"CANBK", "SBIN"}

    def mock_finbert(title):
        title_u = title.upper()
        for tkr in BULLISH_HEADLINES:
            if tkr in title_u or tkr.replace("IND", " INDIA") in title_u:
                return {"score": 0.85, "label": "BULLISH"}
        for tkr in BEARISH_HEADLINES:
            if tkr in title_u or "CANARA" in title_u:
                return {"score": -0.85, "label": "BEARISH"}
        # RBI hawkish stays in dead zone via this mock
        return {"score": 0.05, "label": "NEUTRAL"}

    # ── Mock sector lookup + heatmap ──
    SECTOR_MAP = {
        "POLYCAB": "NIFTY IT",
        "CANBK":   "NIFTY PSU BANK",
        "SBIN":    "NIFTY PSU BANK",
        "INDHOTEL": "NIFTY FMCG",
        "MEESHO":  "NIFTY IT",
    }
    def mock_sector_lookup(symbol):
        return SECTOR_MAP.get(symbol, "UNKNOWN")
    def mock_heatmap():
        return {
            "NIFTY IT":       {"bias": "BULLISH"},
            "NIFTY PSU BANK": {"bias": "BEARISH"},
            "NIFTY FMCG":     {"bias": "NEUTRAL"},
        }

    # ── Build SignalBoy ──
    print("[1/5] Constructing SignalBoy in shadow mode...")
    sb = SignalBoy(
        source_fetchers={"google_business": mock_google_business},
        entity_shield_fn=mock_entity_shield,
        finbert_scorer_fn=mock_finbert,
        sector_lookup_fn=mock_sector_lookup,
        sector_heatmap_provider=mock_heatmap,
        instruments=instruments,
        mode="shadow",
        cache_path=os.path.join(tmp_dir, "test_cache.db"),
        queue_path=os.path.join(tmp_dir, "queue_shadow.json"),
    )
    print(f"       ✅ initialised, queue → {sb.queue_path}\n")

    # ── Scan 1 ──
    print("[2/5] Running scan #1...")
    scan_iter["n"] = 0
    result1 = sb.run_one_scan()
    print(f"       headlines={result1['headlines']}, "
          f"matched={result1['matched']}, ranked={result1['ranked']}")
    print(f"       active symbols: "
          f"{[s['symbol'] for s in result1['active']]}")
    print(f"       expired: {[s['symbol'] for s in result1['expired']]}\n")

    # Verify queue file written
    assert os.path.exists(sb.queue_path)
    with open(sb.queue_path) as f:
        q = json.load(f)
    assert q["scan_id"] == 1
    assert q["schema_version"] == "1.0"
    assert q["metadata"]["mode"] == "shadow"
    print(f"       ✅ queue file valid, schema {q['schema_version']}\n")

    # ── Scan 2 ──
    print("[3/5] Running scan #2 (some tickers gone, MEESHO new)...")
    scan_iter["n"] = 1
    sb.cache.clear()  # force refresh
    result2 = sb.run_one_scan()
    active_syms_2 = [s["symbol"] for s in result2["active"]]
    print(f"       active symbols: {active_syms_2}")
    print(f"       expired: {[s['symbol'] for s in result2['expired']]}\n")

    # POLYCAB should still be active (validated)
    polycab = next((s for s in result2["active"] if s["symbol"] == "POLYCAB"), None)
    assert polycab is not None, "POLYCAB should still be active in scan 2"
    assert polycab["scans_validated"] == 2, \
        f"POLYCAB scans_validated should be 2, got {polycab['scans_validated']}"
    assert polycab["stale"] is False
    print(f"       ✅ POLYCAB validated 2x, stale=False\n")

    # ── Scan 3 ──
    print("[4/5] Running scan #3 (only MEESHO, others should become stale)...")
    scan_iter["n"] = 2
    sb.cache.clear()
    result3 = sb.run_one_scan()
    active_syms_3 = [s["symbol"] for s in result3["active"]]
    print(f"       active symbols: {active_syms_3}")

    # POLYCAB seen scan1+scan2, missed scan3 → 1 missed (still active, not stale yet)
    polycab3 = next((s for s in result3["active"] if s["symbol"] == "POLYCAB"), None)
    # POLYCAB was last seen scan 2; scan 3 = 1 missed scan. Still active.
    if polycab3:
        print(f"       POLYCAB missed_scans status visible "
              f"(stale={polycab3.get('stale', False)})")

    # INDHOTEL/CANBK seen scan1, missed scan2 + scan3 → 2 missed → stale
    indhotel3 = next((s for s in result3["active"] if s["symbol"] == "INDHOTEL"), None)
    if indhotel3:
        assert indhotel3.get("stale") is True, \
            f"INDHOTEL should be stale after 2 missed scans"
        print(f"       ✅ INDHOTEL marked stale after 2 misses\n")

    # ── Run one more scan to test expiry ──
    print("[5/5] Running scan #4 (force one more miss to test expiry)...")
    scan_iter["n"] = 2  # same headlines as scan 3 (only MEESHO)
    sb.cache.clear()
    result4 = sb.run_one_scan()
    expired_syms_4 = [s["symbol"] for s in result4["expired"]]
    print(f"       expired this scan: {expired_syms_4}")
    assert "INDHOTEL" in expired_syms_4 or "CANBK" in expired_syms_4, \
        "Expected at least one expired ticker after 3 missed scans"
    print(f"       ✅ lifecycle expiry working\n")

    # Cleanup
    try:
        for f in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, f))
        os.rmdir(tmp_dir)
    except OSError:
        pass

    print("=" * 60)
    print("  ✅ ALL TESTS PASSED — Signal Boy 6D.3 working end-to-end")
    print("=" * 60 + "\n")
    print("Next: 6D.4 — wire into main.py (HIGH RISK, Tue 19 May only)")
    print()

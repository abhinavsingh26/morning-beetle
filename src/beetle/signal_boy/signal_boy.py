"""
SignalBoy v0.4 — orchestrator background thread.

CHANGES IN v0.4:
    - Wires NewsArchiver to capture every headline + scoring decision per scan
    - Builds per-headline archive records during pipeline execution
    - Archive failure NEVER stops a scan (logged warning only)

Previous changes in v0.3.0 (6D.3 base) unchanged.

Architecture remains:
    every 15 min:
      1. IngestionCache.get_all()
      2. EntityShield.filter_headlines()
      3. FinBERT.score_headline() on each
      4. Apply sector convergence gate
      5. Ranker.rank_signals()
      6. Lifecycle: born/validated/stale/expired
      7. QueueWriter.write()   ← latest snapshot + JSONL history
      8. NewsArchiver.archive() ← raw data for training (NEW v0.4)

Author: Abhinav (Phase 6D.3 v0.4, May 2026)
"""

import logging
import threading
import time as time_module
from datetime import datetime, time, timedelta, timezone
from typing import Optional, Callable

from src.beetle.signal_boy.ingestion_cache import IngestionCache, SOURCE_REGISTRY
from src.beetle.signal_boy.ranker import rank_signals
from src.beetle.signal_boy.queue_writer import QueueWriter
from src.beetle.signal_boy.news_archiver import NewsArchiver

logger = logging.getLogger(__name__)


# ── Scan schedule constants ──────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 15 * 60   # 15 minutes between scans
FIRST_SCAN_TIME       = time(9, 1)
LAST_SCAN_TIME        = time(14, 30)
POLL_INTERVAL_SECONDS = 30

# Lifecycle thresholds
STALE_AFTER_MISSED_SCANS   = 2
EXPIRED_AFTER_MISSED_SCANS = 3

# Ranker defaults
DEFAULT_MAX_ACTIVE     = 15
DEFAULT_MIN_COMPOSITE  = 0.60


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
    """Background thread that produces ranked signals every 15 minutes."""

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
                 history_dir: Optional[str] = None,
                 max_active: int = DEFAULT_MAX_ACTIVE,
                 min_composite: float = DEFAULT_MIN_COMPOSITE,
                 dead_zone_min: float = -0.1,
                 dead_zone_max: float = 0.1,
                 news_archiver: Optional[NewsArchiver] = None):
        """
        Args (unchanged from v0.3, plus):
            history_dir:   directory for daily queue JSONL history (None=default)
            news_archiver: optional NewsArchiver instance; if None, archiving off
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

        # Queue path resolution
        if queue_path is None:
            queue_path = (
                "signals/queue.json" if mode == "production"
                else "signals/queue_shadow.json"
            )
        self.queue_path = queue_path

        # History dir resolution
        if history_dir is None:
            history_dir = (
                "signals/history" if mode == "production"
                else "signals/history_shadow"
            )

        # Cache path per mode
        cache_path_for_mode = (
            cache_path if mode == "production"
            else cache_path.replace(".db", "_shadow.db")
        )
        self.cache  = IngestionCache(db_path=cache_path_for_mode)
        self.writer = QueueWriter(
            path=queue_path,
            history_dir=history_dir,
            history_enabled=True,
        )

        # ── NEW v0.4 — NewsArchiver (optional, off by default for back compat) ──
        self.news_archiver = news_archiver
        if self.news_archiver:
            logger.info(f"SignalBoy will archive every scan's headlines")

        # Register fetchers
        for source_id, fetcher in source_fetchers.items():
            if source_id not in SOURCE_REGISTRY:
                logger.warning(f"  Skipping unknown source: {source_id}")
                continue
            self.cache.register_fetcher(source_id, fetcher)

        # Lifecycle state
        self._lifecycle: dict[str, dict] = {}

        # Scan counter
        self.scan_id = 0
        self.last_scan_at: Optional[datetime] = None

        # Thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        logger.info(f"SignalBoy initialised — mode={mode}, "
                   f"queue={queue_path}, sources={len(source_fetchers)}")

    def start(self):
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
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("🤖 SignalBoy stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self):
        logger.info("  SignalBoy scan loop started")
        now = datetime.now()
        if FIRST_SCAN_TIME <= now.time() <= LAST_SCAN_TIME:
            self._do_scan()
        while not self._stop_event.is_set():
            try:
                now = datetime.now()
                now_t = now.time()
                if now_t < FIRST_SCAN_TIME or now_t > LAST_SCAN_TIME:
                    if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                        break
                    continue
                if self.last_scan_at is None:
                    self._do_scan()
                else:
                    elapsed = (now - self.last_scan_at).total_seconds()
                    if elapsed >= SCAN_INTERVAL_SECONDS:
                        self._do_scan()
                if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                    break
            except Exception as e:
                logger.error(f"  SignalBoy loop error: {e}")
                if self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
                    break
        logger.info("  SignalBoy scan loop exited")

    def run_one_scan(self) -> dict:
        return self._do_scan()

    def _do_scan(self) -> dict:
        """
        End-to-end scan with full per-headline archival.
        v0.4: builds archive_records throughout the pipeline.
        """
        self.scan_id += 1
        scan_started = datetime.now(timezone.utc)
        logger.info(f"━━━ Signal Boy Scan #{self.scan_id} "
                   f"({scan_started.isoformat()}) ━━━")

        # ── Track every headline through the pipeline for archiving ──
        # Key: headline id (or title hash). Value: dict accumulating fields.
        archive_records: dict[str, dict] = {}

        # ── Step 1: Fetch from all sources ──
        all_data = self.cache.get_all()
        all_headlines = []
        for source_id, items in all_data.items():
            for h in items:
                if "source" not in h:
                    h["source"] = source_id
                all_headlines.append(h)

                # Seed archive record
                key = h.get("id") or h.get("title", "")[:200]
                if key and key not in archive_records:
                    archive_records[key] = {
                        "id":               h.get("id"),
                        "source":           h.get("source"),
                        "title":            h.get("title"),
                        "published":        h.get("published"),
                        "matched_ticker":   None,
                        "match_confidence": None,
                        "boosted":          None,
                        "finbert_score":    None,
                        "finbert_label":    None,
                        "passed_dead_zone": False,
                        "passed_sector":    None,
                        "sector":           None,
                        "sector_bias":      None,
                        "composite_score":  None,
                        "final_rank":       None,
                        "made_watchlist":   False,
                    }
        logger.info(f"  Fetched {len(all_headlines)} headlines from "
                   f"{len(all_data)} sources")

        # ── Step 2: EntityShield ──
        matched = self.entity_shield_fn(all_headlines, self.instruments)
        logger.info(f"  EntityShield matched: {len(matched)}")

        # Update archive records for matches
        for h in matched:
            key = h.get("id") or h.get("title", "")[:200]
            if key in archive_records:
                archive_records[key]["matched_ticker"]   = h.get("ticker")
                archive_records[key]["match_confidence"] = h.get("confidence")
                archive_records[key]["boosted"]          = h.get("boosted")

        # ── Step 3: FinBERT scoring (+ dead zone filter) ──
        scored = []
        for h in matched:
            key = h.get("id") or h.get("title", "")[:200]
            try:
                result = self.finbert_scorer_fn(h["title"])
                score = float(result.get("score", 0.0))
                label = result.get("label", "NEUTRAL")
            except Exception as e:
                logger.warning(f"  FinBERT failed for '{h.get('title', '')[:40]}': {e}")
                continue

            if key in archive_records:
                archive_records[key]["finbert_score"] = score
                archive_records[key]["finbert_label"] = label

            if self.dead_zone_min <= score <= self.dead_zone_max:
                continue

            if key in archive_records:
                archive_records[key]["passed_dead_zone"] = True

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
            key = h.get("id") or h.get("title", "")[:200]
            symbol = h["ticker"]
            sector = self.sector_lookup_fn(symbol)
            sector_data = heatmap.get(sector, {})
            sector_bias = sector_data.get("bias", "UNKNOWN")

            if key in archive_records:
                archive_records[key]["sector"]      = sector
                archive_records[key]["sector_bias"] = sector_bias

            if not _passes_sector_gate(h["sentiment_label"], sector_bias):
                if key in archive_records:
                    archive_records[key]["passed_sector"] = False
                continue

            if key in archive_records:
                archive_records[key]["passed_sector"] = True

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
                "_archive_key":     key,
            })

        # Dedup by symbol
        deduped: dict[str, dict] = {}
        for c in candidates:
            sym = c["symbol"]
            existing = deduped.get(sym)
            if existing is None or abs(c["sentiment_score"]) > abs(existing["sentiment_score"]):
                deduped[sym] = c
        candidates = list(deduped.values())
        logger.info(f"  Passed sector gate (deduplicated): {len(candidates)}")

        # ── Step 5: Ranker ──
        ranked = rank_signals(
            candidates,
            max_n=self.max_active,
            min_score=self.min_composite
        )
        logger.info(f"  Ranked above {self.min_composite}: {len(ranked)}")

        # Annotate archive with composite + rank for those that made it
        for r in ranked:
            key = r.get("_archive_key")
            if key and key in archive_records:
                archive_records[key]["composite_score"] = r.get("composite_score")
                archive_records[key]["final_rank"]      = r.get("rank")
                archive_records[key]["made_watchlist"]  = True

        # Strip the internal _archive_key field from ranked records
        # before lifecycle/queue processing
        for r in ranked:
            r.pop("_archive_key", None)

        # ── Step 6: Lifecycle update ──
        active, expired = self._update_lifecycle(ranked, scan_started)

        # ── Step 7: Write queue (snapshot + history) ──
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
                "raw_headlines": len(all_headlines),
                "matched":       len(matched),
                "scored":        len(scored),
                "after_sector":  len(candidates),
                "ranked":        len(ranked),
                "mode":          self.mode,
            },
        )

        # ── Step 8: NEW v0.4 — Archive every headline ──
        if self.news_archiver:
            try:
                records_list = list(archive_records.values())
                self.news_archiver.archive(
                    scan_id=self.scan_id,
                    records=records_list,
                )
            except Exception as e:
                # MUST NEVER fail a scan
                logger.warning(f"  NewsArchiver step failed (non-fatal): {e}")

        self.last_scan_at = datetime.now()
        return {
            "scan_id":   self.scan_id,
            "active":    active,
            "expired":   expired,
            "headlines": len(all_headlines),
            "matched":   len(matched),
            "ranked":    len(ranked),
        }

    def _update_lifecycle(self,
                          ranked_now: list,
                          scan_time: datetime) -> tuple[list, list]:
        active_for_queue = []
        expired_for_queue = []

        current_symbols = {r["symbol"] for r in ranked_now}
        ranked_by_sym = {r["symbol"]: r for r in ranked_now}

        for sym in current_symbols:
            r = ranked_by_sym[sym]
            entry = self._lifecycle.get(sym)
            if entry is None or entry.get("status") == "expired":
                self._lifecycle[sym] = {
                    "first_seen_at":     scan_time.isoformat(),
                    "last_validated_at": scan_time.isoformat(),
                    "scans_validated":   1,
                    "missed_scans":      0,
                    "status":            "active",
                    "payload":           r,
                }
            else:
                entry["last_validated_at"] = scan_time.isoformat()
                entry["scans_validated"]   = entry.get("scans_validated", 0) + 1
                entry["missed_scans"]      = 0
                entry["status"]            = "active"
                entry["payload"]           = r

        symbols_to_remove = []
        for sym, entry in self._lifecycle.items():
            if sym in current_symbols:
                continue
            if entry.get("status") == "expired":
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

        for sym in symbols_to_remove:
            del self._lifecycle[sym]

        for sym, entry in self._lifecycle.items():
            if entry.get("status") == "expired":
                continue
            payload = entry.get("payload", {}).copy()
            payload["first_seen_at"]     = entry["first_seen_at"]
            payload["last_validated_at"] = entry["last_validated_at"]
            payload["scans_validated"]   = entry["scans_validated"]
            payload["stale"]             = entry.get("status") == "stale"
            active_for_queue.append(payload)

        active_for_queue.sort(
            key=lambda x: x.get("composite_score", 0.0),
            reverse=True
        )
        for i, item in enumerate(active_for_queue, start=1):
            item["rank"] = i

        return active_for_queue, expired_for_queue


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    import os
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  SignalBoy v0.4 — Standalone E2E Test (with NewsArchiver)")
    print("=" * 60 + "\n")

    tmp_dir = tempfile.mkdtemp(prefix="signal_boy_v04_")

    instruments = {
        "POLYCAB":  {"name": "POLYCAB INDIA",       "search_anchor": "POLYCAB INDIA"},
        "CANBK":    {"name": "CANARA BANK",         "search_anchor": "CANARA BANK"},
        "INDHOTEL": {"name": "INDIAN HOTELS",       "search_anchor": "INDIAN HOTELS"},
    }

    def mock_google():
        return [
            {"title": "Polycab India Q4 PAT 32%", "source": "google_business", "id": "h1"},
            {"title": "Canara Bank Q4 results",   "source": "google_business", "id": "h2"},
            {"title": "Random noise",             "source": "google_business", "id": "h3"},
        ]

    def mock_entity_shield(headlines, instruments):
        out = []
        for h in headlines:
            title = h["title"].upper()
            for sym, data in instruments.items():
                if data["search_anchor"] in title or sym in title:
                    out.append({**h, "ticker": sym, "ticker_name": data["name"],
                               "confidence": 0.95, "boosted": True})
                    break
        return out

    def mock_finbert(title):
        if "POLYCAB" in title.upper(): return {"score": 0.85, "label": "BULLISH"}
        if "CANARA" in title.upper(): return {"score": -0.85, "label": "BEARISH"}
        return {"score": 0.05, "label": "NEUTRAL"}

    SECTOR_MAP = {"POLYCAB": "NIFTY IT", "CANBK": "NIFTY PSU BANK", "INDHOTEL": "NIFTY FMCG"}
    def mock_sector(sym): return SECTOR_MAP.get(sym, "UNKNOWN")
    def mock_heatmap():
        return {"NIFTY IT": {"bias": "BULLISH"}, "NIFTY PSU BANK": {"bias": "BEARISH"}}

    archive_dir = os.path.join(tmp_dir, "news_archive")
    archiver = NewsArchiver(
        archive_dir=archive_dir,
        fallback_dir=os.path.join(tmp_dir, "archive_fallback")
    )

    print("[1/3] Constructing SignalBoy with NewsArchiver...")
    sb = SignalBoy(
        source_fetchers={"google_business": mock_google},
        entity_shield_fn=mock_entity_shield,
        finbert_scorer_fn=mock_finbert,
        sector_lookup_fn=mock_sector,
        sector_heatmap_provider=mock_heatmap,
        instruments=instruments,
        mode="shadow",
        cache_path=os.path.join(tmp_dir, "test_cache.db"),
        queue_path=os.path.join(tmp_dir, "queue_shadow.json"),
        history_dir=os.path.join(tmp_dir, "history_shadow"),
        news_archiver=archiver,
    )
    print(f"       ✅ initialised with archiver\n")

    print("[2/3] Running scan #1...")
    result = sb.run_one_scan()
    print(f"       headlines={result['headlines']}, matched={result['matched']}, ranked={result['ranked']}\n")

    print("[3/3] Verifying archive has all 3 headlines with full annotations...")
    archived = archiver.read_day()
    assert len(archived) == 3, f"Expected 3 archived, got {len(archived)}"

    # POLYCAB should have made watchlist
    polycab_rec = next((r for r in archived if r.get("matched_ticker") == "POLYCAB"), None)
    assert polycab_rec is not None
    assert polycab_rec["made_watchlist"] is True
    assert polycab_rec["finbert_score"] == 0.85
    assert polycab_rec["passed_dead_zone"] is True
    assert polycab_rec["passed_sector"] is True
    assert polycab_rec["composite_score"] is not None
    print(f"       ✅ POLYCAB record fully annotated: "
          f"matched=True, finbert=+0.85, made_watchlist=True\n")

    # CANBK should NOT have made watchlist (BEARISH + BEARISH? Yes, aligns → made it!)
    canbk_rec = next((r for r in archived if r.get("matched_ticker") == "CANBK"), None)
    assert canbk_rec is not None
    # CANBK is BEARISH sentiment + BEARISH sector = aligned → made watchlist
    print(f"       ✅ CANBK record: matched=True, finbert={canbk_rec['finbert_score']}, "
          f"made_watchlist={canbk_rec['made_watchlist']}\n")

    # Noise headline — not matched
    noise_rec = next((r for r in archived if r.get("title") == "Random noise"), None)
    assert noise_rec is not None
    assert noise_rec["matched_ticker"] is None
    assert noise_rec["made_watchlist"] is False
    print(f"       ✅ Noise record: matched=None, made_watchlist=False\n")

    # Cleanup
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("=" * 60)
    print("  ✅ ALL TESTS PASSED — Signal Boy v0.4 with full archival working")
    print("=" * 60 + "\n")

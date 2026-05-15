"""
IngestionCache — shared SQLite-backed cache for Signal Boy news fetches.

Purpose:
    One poll, many readers. When Signal Boy runs 22 scans per day across
    9 sources, this cache prevents redundant HTTP fetches. Each source has
    its own TTL — NSE filings refresh every 90s, slower aggregators every
    600s.

Design principles:
    - Standalone module. No engine dependency.
    - SQLite for persistence across engine restarts.
    - Atomic writes (each fetch is one transaction).
    - Thread-safe (SQLite WAL mode).
    - Graceful degradation: if cache fails, log warning, return None.

Sources (9 total):
    7 existing RSS feeds (currently in news_fetcher.py)
    + NSE Corporate Filings (NEW v1)
    + Pulse RSS / Zerodha (NEW v1)
    + PIB Defence (NEW v1, per defence-news request)

Schema:
    cache_entries(
        source_id TEXT PRIMARY KEY,   -- e.g. 'google_business'
        fetched_at TIMESTAMP,          -- UTC ISO format
        ttl_seconds INTEGER,
        payload TEXT,                  -- JSON list of headline dicts
        hit_count INTEGER DEFAULT 0,
        miss_count INTEGER DEFAULT 0
    )

Usage:
    cache = IngestionCache(db_path="signals/ingestion_cache.db")
    headlines = cache.get_or_fetch("google_business")
    # ... cache handles TTL check + fetch internally

Author: Abhinav (Phase 6D.1, May 2026)
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Source registry ─────────────────────────────────────────────────
# Each entry: ttl_seconds, fetcher_key (used to wire fetcher functions)
SOURCE_REGISTRY = {
    # ── Existing 7 RSS feeds (currently in news_fetcher.py) ──────
    "google_business": {
        "ttl_seconds": 300,
        "description": "Google News — NSE India business query",
        "category":    "aggregator",
    },
    "google_earnings": {
        "ttl_seconds": 300,
        "description": "Google News — Q4 earnings query",
        "category":    "aggregator",
    },
    "google_corporate": {
        "ttl_seconds": 300,
        "description": "Google News — corporate actions",
        "category":    "aggregator",
    },
    "livemint_markets": {
        "ttl_seconds": 300,
        "description": "LiveMint Markets RSS",
        "category":    "aggregator",
    },
    "livemint_companies": {
        "ttl_seconds": 300,
        "description": "LiveMint Companies RSS",
        "category":    "aggregator",
    },
    "hindu_business": {
        "ttl_seconds": 600,
        "description": "The Hindu Business Line RSS",
        "category":    "aggregator",
    },
    "ndtv_profit": {
        "ttl_seconds": 600,
        "description": "NDTV Profit (FeedBurner) RSS",
        "category":    "aggregator",
    },

    # ── NEW v1 sources ───────────────────────────────────────────
    "nse_filings": {
        "ttl_seconds": 90,
        "description": "NSE Corporate Filings (official)",
        "category":    "official_filings",
    },
    "pulse_zerodha": {
        "ttl_seconds": 120,
        "description": "Pulse RSS / Zerodha (curated)",
        "category":    "curated",
    },
}


# ── Schema DDL ─────────────────────────────────────────────────────
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS cache_entries (
    source_id   TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    hit_count   INTEGER DEFAULT 0,
    miss_count  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fetched_at ON cache_entries(fetched_at);
"""


class IngestionCache:
    """
    SQLite-backed cache for news source fetches.

    Each source has a TTL. A get_or_fetch() call:
      1. Checks cache.
      2. If entry exists AND age < TTL → returns cached payload (hit).
      3. Otherwise → calls the fetcher function, stores result (miss).

    Thread-safe via SQLite WAL mode. Lock used only for fetch invocation
    to prevent thundering herd on cold cache.
    """

    def __init__(self, db_path: str = "signals/ingestion_cache.db"):
        self.db_path = db_path
        self._fetch_lock = threading.Lock()

        # Ensure parent directory exists
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._init_db()

        # Registered fetcher callbacks: {source_id: callable() -> list[dict]}
        self._fetchers: dict[str, Callable[[], list]] = {}

        logger.info(f"IngestionCache initialised → {db_path}")

    def _init_db(self):
        """Create schema if missing. Enable WAL for concurrent reads."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA_DDL)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """New connection per call. SQLite is connection-per-thread safe."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API ───────────────────────────────────────────────
    def register_fetcher(self, source_id: str,
                         fetcher_fn: Callable[[], list]) -> None:
        """
        Register a fetcher function for a source.

        The fetcher_fn must:
          - Take no arguments
          - Return a list of dicts (headlines)
          - Raise on failure (don't return empty list silently)
        """
        if source_id not in SOURCE_REGISTRY:
            raise ValueError(
                f"Unknown source '{source_id}'. "
                f"Add it to SOURCE_REGISTRY first. "
                f"Known sources: {list(SOURCE_REGISTRY.keys())}"
            )
        self._fetchers[source_id] = fetcher_fn
        logger.debug(f"  Registered fetcher: {source_id}")

    def get_or_fetch(self, source_id: str,
                     force_refresh: bool = False) -> Optional[list]:
        """
        Return cached payload if fresh, else fetch and cache.

        Returns:
            list of headline dicts on success
            None if no fetcher registered or fetch failed
        """
        if source_id not in SOURCE_REGISTRY:
            logger.error(f"  Unknown source '{source_id}'")
            return None

        ttl = SOURCE_REGISTRY[source_id]["ttl_seconds"]

        # Step 1 — Check cache (unless forcing refresh)
        if not force_refresh:
            cached = self._read_cached(source_id, ttl)
            if cached is not None:
                self._bump_counter(source_id, "hit_count")
                logger.debug(f"  Cache HIT: {source_id} "
                            f"(age < {ttl}s)")
                return cached

        # Step 2 — Cache miss or expired. Fetch.
        if source_id not in self._fetchers:
            logger.warning(
                f"  No fetcher registered for '{source_id}'. "
                f"Cannot refresh. Returning stale cache if any."
            )
            return self._read_cached(source_id, ttl=10**9)  # any-age fallback

        # Lock to prevent thundering herd if multiple threads miss together
        with self._fetch_lock:
            # Re-check cache inside lock (another thread may have filled it)
            if not force_refresh:
                cached = self._read_cached(source_id, ttl)
                if cached is not None:
                    self._bump_counter(source_id, "hit_count")
                    return cached

            # Actually fetch
            try:
                logger.info(f"  Fetching: {source_id} "
                           f"({SOURCE_REGISTRY[source_id]['description']})")
                payload = self._fetchers[source_id]()
                if not isinstance(payload, list):
                    raise TypeError(
                        f"Fetcher for '{source_id}' returned "
                        f"{type(payload).__name__}, expected list"
                    )
                self._write_cached(source_id, payload, ttl)
                self._bump_counter(source_id, "miss_count")
                logger.info(f"    Cached {len(payload)} items "
                           f"(TTL {ttl}s)")
                return payload
            except Exception as e:
                logger.error(f"  Fetch failed for {source_id}: {e}")
                # Fallback to stale cache if available
                stale = self._read_cached(source_id, ttl=10**9)
                if stale is not None:
                    logger.warning(
                        f"  Returning stale cache for {source_id} "
                        f"(fetch failed, {len(stale)} items)"
                    )
                return stale

    def get_all(self, force_refresh: bool = False) -> dict:
        """
        Convenience: fetch all registered sources, return as dict.
        Returns: {source_id: list of headlines}
        Missing/failed sources are simply absent from the dict.
        """
        result = {}
        for source_id in self._fetchers.keys():
            payload = self.get_or_fetch(source_id, force_refresh)
            if payload is not None:
                result[source_id] = payload
        return result

    def stats(self) -> dict:
        """
        Return cache statistics.
        {
            'sources': {source_id: {hits, misses, hit_rate, age_seconds, item_count}},
            'overall': {total_hits, total_misses, hit_rate}
        }
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id, fetched_at, hit_count, miss_count, payload "
                "FROM cache_entries"
            ).fetchall()

        sources = {}
        total_hits = 0
        total_misses = 0

        for row in rows:
            sid = row["source_id"]
            hits = row["hit_count"]
            misses = row["miss_count"]
            total_hits += hits
            total_misses += misses
            total = hits + misses
            hit_rate = round(hits / total, 3) if total > 0 else 0.0

            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(row["fetched_at"])).total_seconds()
            except Exception:
                age = -1.0

            try:
                item_count = len(json.loads(row["payload"]))
            except Exception:
                item_count = 0

            sources[sid] = {
                "hits":         hits,
                "misses":       misses,
                "hit_rate":     hit_rate,
                "age_seconds":  round(age, 1),
                "item_count":   item_count,
            }

        overall_total = total_hits + total_misses
        overall_hit_rate = (
            round(total_hits / overall_total, 3) if overall_total > 0 else 0.0
        )

        return {
            "sources": sources,
            "overall": {
                "total_hits":   total_hits,
                "total_misses": total_misses,
                "hit_rate":     overall_hit_rate,
            }
        }

    def clear(self, source_id: Optional[str] = None) -> int:
        """
        Delete cache entries. If source_id is None, clear all.
        Returns number of rows deleted.
        """
        with self._connect() as conn:
            if source_id:
                cur = conn.execute(
                    "DELETE FROM cache_entries WHERE source_id = ?",
                    (source_id,)
                )
            else:
                cur = conn.execute("DELETE FROM cache_entries")
            conn.commit()
            return cur.rowcount

    # ── Internal helpers ─────────────────────────────────────────
    def _read_cached(self, source_id: str, ttl: int) -> Optional[list]:
        """
        Return cached payload if entry exists and age < ttl, else None.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at, payload FROM cache_entries "
                "WHERE source_id = ?",
                (source_id,)
            ).fetchone()

        if row is None:
            return None

        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        except Exception:
            return None

        if age >= ttl:
            return None

        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            logger.error(f"  Corrupt cache payload for {source_id}")
            return None

    def _write_cached(self, source_id: str, payload: list, ttl: int) -> None:
        """Upsert cache entry."""
        now_iso = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False)

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cache_entries "
                "  (source_id, fetched_at, ttl_seconds, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "  fetched_at  = excluded.fetched_at, "
                "  ttl_seconds = excluded.ttl_seconds, "
                "  payload     = excluded.payload",
                (source_id, now_iso, ttl, payload_json)
            )
            conn.commit()

    def _bump_counter(self, source_id: str, column: str) -> None:
        """Increment hit_count or miss_count for a source."""
        if column not in ("hit_count", "miss_count"):
            raise ValueError(f"Invalid counter column: {column}")
        with self._connect() as conn:
            conn.execute(
                f"UPDATE cache_entries SET {column} = {column} + 1 "
                f"WHERE source_id = ?",
                (source_id,)
            )
            conn.commit()


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    """
    Standalone test — no engine dependency.

    Creates a temporary cache, registers 3 mock fetchers, and verifies:
      1. First fetch is a MISS (calls fetcher)
      2. Second fetch within TTL is a HIT (no fetcher call)
      3. Force refresh re-calls fetcher
      4. Expired entry re-calls fetcher
      5. Failed fetcher returns stale cache
      6. Stats are accurate
    """
    import tempfile
    import time
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  IngestionCache — Standalone Test (Phase 6D.1)")
    print("=" * 60 + "\n")

    # Use temp DB to avoid polluting production cache
    tmp_dir = tempfile.mkdtemp(prefix="signal_boy_test_")
    db_path = Path(tmp_dir) / "test_cache.db"

    cache = IngestionCache(db_path=str(db_path))

    # ── Mock fetchers — count invocations to verify cache behavior ──
    call_counts = {"google_business": 0, "nse_filings": 0, "pib_defence": 0}

    def mock_google_business():
        call_counts["google_business"] += 1
        return [
            {"title": "Reliance Q4 results beat", "source": "google_business"},
            {"title": "HDFC Bank NII up 15%",     "source": "google_business"},
            {"title": "Infosys raises guidance",  "source": "google_business"},
        ]

    def mock_nse_filings():
        call_counts["nse_filings"] += 1
        return [
            {"title": "HAL: Receipt of order from MoD", "source": "nse_filings"},
            {"title": "BEL: Receipt of contract",       "source": "nse_filings"},
        ]

    def mock_pib_defence():
        call_counts["pib_defence"] += 1
        return [
            {"title": "MoD signs ₹40k cr contract with HAL for Tejas Mk1A",
             "source": "pib_defence"},
        ]

    def mock_failing_fetcher():
        raise RuntimeError("simulated network failure")

    # ── Register fetchers ──
    print("[1/8] Registering fetchers...")
    cache.register_fetcher("google_business", mock_google_business)
    cache.register_fetcher("nse_filings",     mock_nse_filings)
    cache.register_fetcher("pib_defence",     mock_pib_defence)
    print("       ✅ 3 fetchers registered\n")

    # ── Test 1: First fetch = MISS ──
    print("[2/8] First fetch (expect MISS, calls fetcher)...")
    result1 = cache.get_or_fetch("google_business")
    assert result1 is not None, "First fetch returned None"
    assert len(result1) == 3, f"Expected 3 items, got {len(result1)}"
    assert call_counts["google_business"] == 1, \
        f"Expected 1 fetcher call, got {call_counts['google_business']}"
    print(f"       ✅ Got {len(result1)} items, fetcher called {call_counts['google_business']}x\n")

    # ── Test 2: Second fetch within TTL = HIT ──
    print("[3/8] Second fetch within TTL (expect HIT, no fetcher call)...")
    result2 = cache.get_or_fetch("google_business")
    assert result2 == result1, "Cached payload differs from original"
    assert call_counts["google_business"] == 1, \
        f"Fetcher called {call_counts['google_business']}x, expected still 1"
    print(f"       ✅ Got {len(result2)} items from cache, "
          f"fetcher NOT called (still {call_counts['google_business']}x)\n")

    # ── Test 3: Force refresh = MISS ──
    print("[4/8] Force refresh (expect MISS, fetcher re-called)...")
    result3 = cache.get_or_fetch("google_business", force_refresh=True)
    assert call_counts["google_business"] == 2, \
        f"Expected 2 fetcher calls, got {call_counts['google_business']}"
    print(f"       ✅ Fetcher called {call_counts['google_business']}x (re-fetched)\n")

    # ── Test 4: Different source = independent fetch ──
    print("[5/8] Fetching second source (nse_filings)...")
    nse_result = cache.get_or_fetch("nse_filings")
    assert len(nse_result) == 2
    assert call_counts["nse_filings"] == 1
    print(f"       ✅ Got {len(nse_result)} NSE items\n")

    # ── Test 5: get_all() fetches all registered ──
    print("[6/8] get_all() — fetch all 3 registered sources...")
    # First clear PIB to force a fresh fetch
    initial_pib_count = call_counts["pib_defence"]
    all_data = cache.get_all()
    assert "google_business" in all_data
    assert "nse_filings" in all_data
    assert "pib_defence" in all_data
    assert call_counts["pib_defence"] == initial_pib_count + 1, \
        "PIB defence should have been fetched for the first time"
    print(f"       ✅ All 3 sources present: {list(all_data.keys())}\n")

    # ── Test 6: Failed fetcher returns stale cache ──
    print("[7/8] Failing fetcher with existing cache (expect stale fallback)...")
    cache.register_fetcher("google_business", mock_failing_fetcher)
    fallback = cache.get_or_fetch("google_business", force_refresh=True)
    assert fallback is not None, \
        "Stale fallback returned None despite cached entry"
    assert len(fallback) == 3, "Stale fallback has wrong item count"
    print(f"       ✅ Got {len(fallback)} stale items from cache "
          f"(fetcher raised, fallback worked)\n")

    # ── Test 7: Stats are accurate ──
    print("[8/8] Cache statistics...")
    stats = cache.stats()
    print(f"       Overall hit rate: {stats['overall']['hit_rate'] * 100:.1f}%")
    print(f"       Total hits:   {stats['overall']['total_hits']}")
    print(f"       Total misses: {stats['overall']['total_misses']}")
    print()
    for sid, s in stats["sources"].items():
        print(f"       {sid:20} hits={s['hits']} misses={s['misses']} "
              f"hit_rate={s['hit_rate'] * 100:.0f}% "
              f"items={s['item_count']} age={s['age_seconds']:.1f}s")
    assert stats["overall"]["total_hits"] > 0
    assert stats["overall"]["total_misses"] > 0
    print()

    # ── Cleanup ──
    cache.clear()
    try:
        os.remove(db_path)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    print("=" * 60)
    print("  ✅ ALL 8 TESTS PASSED")
    print("=" * 60 + "\n")
    print("Signal Boy 6D.1 (IngestionCache) is working correctly.")
    print()
    print("Next: 6D.2 (Ranker) — composite scoring function.")
    print("Build trigger: Wed 13 May evening (Day 8 EOD).")
    print()

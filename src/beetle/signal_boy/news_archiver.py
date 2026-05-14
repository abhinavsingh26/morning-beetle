"""
NewsArchiver — long-term news data collection for Signal Boy.

Purpose:
    Capture every headline that Signal Boy fetches, regardless of whether
    it makes it through EntityShield, FinBERT, or sector gates. The full
    corpus accumulates on the I: drive (cold storage) for future:
      - FinBERT fine-tuning with India-specific sentiment data
      - Back-testing strategies on historical news + outcome data
      - Analyzing why certain headlines were/weren't picked up

Design:
    - Append-only daily JSONL files (one JSON per line)
    - One file per UTC date
    - Stores raw headlines + EntityShield results + FinBERT scores + gates
    - Lightweight: just text + small JSON metadata, ~1KB per headline
    - Estimated volume: 4,000 headlines/day × ~600 bytes = ~2.4 MB/day
                       → ~70 MB/month → ~860 MB/year (trivial on 5TB HDD)

Schema (one line per headline):
    {
      "id":                "md5 hash",
      "captured_at":       "ISO timestamp",
      "scan_id":           int,
      "source":            "google_business" / "pib_defence" / ...
      "title":             "Polycab India Q4 PAT 32%",
      "published":         "raw publication string",
      "matched_ticker":    "POLYCAB" or null,
      "match_confidence":  0.95 or null,
      "boosted":           true/false or null,
      "finbert_score":     0.94 or null,
      "finbert_label":     "BULLISH"/"BEARISH"/"NEUTRAL" or null,
      "passed_dead_zone":  bool,
      "passed_sector":     bool or null,
      "sector":            "NIFTY IT" or null,
      "sector_bias":       "BULLISH" or null,
      "composite_score":   0.95 or null,
      "final_rank":        1 or null,
      "made_watchlist":    bool
    }

Failure mode:
    Any archiver failure is logged as a warning and silently swallowed.
    A broken archiver MUST NEVER stop a Signal Boy scan.

Author: Abhinav (Phase 6D.3 — data collection, May 2026)
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Default archive location — cold storage on I: drive
DEFAULT_ARCHIVE_DIR = r"I:\01_Active_Projects\Morning_Beetle\04_news_archive"


class NewsArchiver:
    """
    Append-only JSONL writer for raw news + scoring metadata.
    Thread-safe via file-level append (POSIX/Windows both atomic for
    small writes < 4KB on default file system buffers).
    """

    def __init__(self,
                 archive_dir: Optional[str] = None,
                 enabled: bool = True,
                 fallback_dir: str = "signals/news_archive"):
        """
        Args:
            archive_dir:  primary archive location (e.g. I: drive)
            enabled:      master kill switch
            fallback_dir: used if archive_dir doesn't exist / unwritable
        """
        self.enabled = enabled
        if not enabled:
            self.archive_dir = None
            logger.info("NewsArchiver DISABLED")
            return

        # Try primary archive_dir; fall back if not writable
        primary = archive_dir or DEFAULT_ARCHIVE_DIR
        self.archive_dir = self._pick_writable_dir(primary, fallback_dir)
        logger.info(f"NewsArchiver enabled → {self.archive_dir}")

    def _pick_writable_dir(self, primary: str, fallback: str) -> str:
        """Use primary if writable, else fallback."""
        for candidate in (primary, fallback):
            try:
                os.makedirs(candidate, exist_ok=True)
                # Test write
                test_path = os.path.join(candidate, ".write_test")
                with open(test_path, "w") as f:
                    f.write("ok")
                os.remove(test_path)
                return candidate
            except (OSError, PermissionError) as e:
                logger.warning(f"  NewsArchiver: cannot write to {candidate}: {e}")
                continue
        raise RuntimeError(
            "NewsArchiver: no writable directory found "
            "(tried primary + fallback)"
        )

    def _today_path(self) -> str:
        """Get the path for today's UTC archive file."""
        date_str = datetime.now(timezone.utc).date().isoformat()
        return os.path.join(self.archive_dir, f"{date_str}_news.jsonl")

    def archive(self, scan_id: int, records: list) -> bool:
        """
        Append a list of headline records for the current scan.

        Args:
            scan_id: scan number (1-22 per day)
            records: list of dicts (see schema in module docstring)

        Returns:
            True on success, False on failure (logged warning).
        """
        if not self.enabled or not self.archive_dir:
            return True

        if not records:
            return True

        captured_at = datetime.now(timezone.utc).isoformat()
        path = self._today_path()

        # Enrich each record with scan_id and captured_at
        try:
            with open(path, "a", encoding="utf-8") as f:
                for rec in records:
                    full_record = {
                        "scan_id":     scan_id,
                        "captured_at": captured_at,
                        **rec,
                    }
                    f.write(json.dumps(full_record, ensure_ascii=False) + "\n")
            logger.info(f"  NewsArchiver: appended {len(records)} records "
                       f"for scan #{scan_id} → {path}")
            return True
        except Exception as e:
            logger.warning(f"  NewsArchiver append failed (non-fatal): {e}")
            return False

    def read_day(self, date_str: Optional[str] = None) -> list:
        """
        Read all headlines for a UTC date. Used by analysis tools.
        date_str: 'YYYY-MM-DD' (defaults to today UTC).
        Returns list of dicts.
        """
        if not self.archive_dir:
            return []
        if date_str is None:
            date_str = datetime.now(timezone.utc).date().isoformat()
        path = os.path.join(self.archive_dir, f"{date_str}_news.jsonl")
        if not os.path.exists(path):
            return []
        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"  Bad JSONL line: {e}")
        except Exception as e:
            logger.error(f"  NewsArchiver read failed: {e}")
        return records


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  NewsArchiver — Standalone Test")
    print("=" * 60 + "\n")

    tmp_dir = tempfile.mkdtemp(prefix="news_archiver_")
    archive_dir = os.path.join(tmp_dir, "archive")

    # Test 1: Basic archive
    print("[1/5] Construct archiver in test directory...")
    archiver = NewsArchiver(
        archive_dir=archive_dir,
        fallback_dir=os.path.join(tmp_dir, "fallback")
    )
    print(f"       ✅ archive dir → {archiver.archive_dir}\n")

    # Test 2: Append records
    print("[2/5] Append 3 records for scan #1...")
    records_1 = [
        {
            "id": "abc123", "source": "google_business",
            "title": "Polycab India Q4 PAT 32%",
            "matched_ticker": "POLYCAB", "match_confidence": 0.95,
            "finbert_score": 0.94, "finbert_label": "BULLISH",
            "passed_dead_zone": True, "passed_sector": True,
            "made_watchlist": True, "composite_score": 0.91, "final_rank": 1,
        },
        {
            "id": "def456", "source": "livemint_markets",
            "title": "Random noise headline",
            "matched_ticker": None,
            "finbert_score": None, "made_watchlist": False,
        },
        {
            "id": "ghi789", "source": "pib_defence",
            "title": "MoD signs contract with HAL",
            "matched_ticker": "HAL", "match_confidence": 0.99,
            "finbert_score": 0.88, "finbert_label": "BULLISH",
            "passed_dead_zone": True, "passed_sector": True,
            "made_watchlist": True, "composite_score": 0.83, "final_rank": 2,
        },
    ]
    ok = archiver.archive(scan_id=1, records=records_1)
    assert ok
    print(f"       ✅ appended {len(records_1)} records\n")

    # Test 3: Append more for scan #2
    print("[3/5] Append 2 more records for scan #2...")
    records_2 = [
        {
            "id": "jkl012", "source": "google_business",
            "title": "Bharti Airtel Q4 preview",
            "matched_ticker": "BHARTIARTL", "match_confidence": 1.0,
            "finbert_score": 0.85, "finbert_label": "BULLISH",
            "passed_dead_zone": True, "passed_sector": True,
            "made_watchlist": True, "composite_score": 0.88, "final_rank": 1,
        },
        {
            "id": "mno345", "source": "google_corporate",
            "title": "Another noise headline",
            "matched_ticker": None, "made_watchlist": False,
        },
    ]
    archiver.archive(scan_id=2, records=records_2)
    print(f"       ✅ appended {len(records_2)} more records\n")

    # Test 4: Read back and verify
    print("[4/5] Read day's full archive...")
    all_records = archiver.read_day()
    assert len(all_records) == 5, f"Expected 5, got {len(all_records)}"
    assert all_records[0]["scan_id"] == 1
    assert all_records[-1]["scan_id"] == 2
    # Verify enrichment happened
    assert "captured_at" in all_records[0]
    print(f"       ✅ read {len(all_records)} records, "
          f"scans {[r['scan_id'] for r in all_records]}\n")

    # Test 5: Disabled archiver
    print("[5/5] Disabled archiver no-ops gracefully...")
    archiver_off = NewsArchiver(enabled=False)
    ok = archiver_off.archive(scan_id=1, records=records_1)
    assert ok  # Returns True (success) but does nothing
    assert archiver_off.read_day() == []
    print(f"       ✅ disabled archiver returns gracefully\n")

    # Cleanup
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("=" * 60)
    print("  ✅ ALL 5 TESTS PASSED")
    print("=" * 60 + "\n")
    print("Schema reminder — fields per record:")
    print("  id, captured_at, scan_id, source, title, published,")
    print("  matched_ticker, match_confidence, boosted,")
    print("  finbert_score, finbert_label, passed_dead_zone,")
    print("  passed_sector, sector, sector_bias, composite_score,")
    print("  final_rank, made_watchlist")
    print()

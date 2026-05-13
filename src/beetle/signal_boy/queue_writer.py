"""
QueueWriter — atomic JSON writes for Signal Boy.

Purpose:
    Write ranked signals to signals/queue.json (or queue_shadow.json in
    shadow mode) atomically. The engine reads this file on each sync tick;
    we must never leave it in a half-written state.

Pattern:
    1. Write to <path>.tmp
    2. os.replace(<path>.tmp, <path>)  ← atomic on POSIX and Windows
    3. Engine never sees a partial file.

Schema version: 1.0 — see docs/SIGNAL_BOY_DESIGN.md §5.

Author: Abhinav (Phase 6D.3, May 2026)
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


class QueueWriter:
    """Atomic JSON queue writer."""

    def __init__(self, path: str = "signals/queue.json"):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        logger.info(f"QueueWriter initialised → {path}")

    def write(self,
              scan_id: int,
              active_signals: list,
              expired_signals: list,
              cache_hit_rate: Optional[float] = None,
              next_scan_at: Optional[str] = None,
              extra_metadata: Optional[dict] = None) -> bool:
        """
        Write a complete queue snapshot atomically.

        Args:
            scan_id:         sequential scan number (1-22 per day)
            active_signals:  ranked list of current candidates
            expired_signals: tickers that aged out (3 missed scans)
            cache_hit_rate:  optional, for metadata
            next_scan_at:    optional ISO timestamp
            extra_metadata:  optional dict merged into metadata

        Returns:
            True on successful write, False on failure (logged).
        """
        now = datetime.now(timezone.utc)

        metadata = {
            "scans_today":     scan_id,
            "total_active":    len(active_signals),
            "total_expired":   len(expired_signals),
        }
        if cache_hit_rate is not None:
            metadata["cache_hit_rate"] = round(cache_hit_rate, 3)
        if next_scan_at is not None:
            metadata["next_scan_at"] = next_scan_at
        if extra_metadata:
            metadata.update(extra_metadata)

        payload = {
            "schema_version":   SCHEMA_VERSION,
            "generated_at":     now.isoformat(),
            "scan_id":          scan_id,
            "trading_date":     now.date().isoformat(),
            "active_signals":   active_signals,
            "expired_signals":  expired_signals,
            "metadata":         metadata,
        }

        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            # Atomic rename: on Windows this is also atomic when target exists
            os.replace(tmp_path, self.path)
            logger.info(f"  Queue written: scan #{scan_id}, "
                       f"{len(active_signals)} active, "
                       f"{len(expired_signals)} expired → {self.path}")
            return True
        except Exception as e:
            logger.error(f"  QueueWriter failed: {e}")
            # Clean up partial tmp file
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False

    def read(self) -> Optional[dict]:
        """
        Read the current queue. Returns None if missing or malformed.
        Engine uses this on each sync tick.
        """
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"  QueueWriter read failed: {e}")
            return None


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  QueueWriter — Standalone Test")
    print("=" * 60 + "\n")

    tmp_dir = tempfile.mkdtemp(prefix="queue_writer_test_")
    tmp_path = os.path.join(tmp_dir, "test_queue.json")

    writer = QueueWriter(path=tmp_path)

    # Mock signals
    active = [
        {
            "symbol":          "POLYCAB", "rank": 1,
            "sentiment_score": 0.93, "sentiment_label": "BULLISH",
            "sector":          "NIFTY IT", "sector_bias": "BULLISH",
            "catalyst_strength": 0.88, "composite_score": 0.91,
            "headline":          "Polycab India Q4 PAT 32%",
            "headline_source":   "google_earnings",
            "first_seen_at":     datetime.now(timezone.utc).isoformat(),
            "last_validated_at": datetime.now(timezone.utc).isoformat(),
            "scans_validated":   1,
            "stale":             False,
            "instrument_token":  2455041,
        },
    ]
    expired = [
        {
            "symbol":     "STALETICKER",
            "expired_at": datetime.now(timezone.utc).isoformat(),
            "reason":     "no_fresh_news"
        }
    ]

    print("[1/4] Write payload...")
    ok = writer.write(scan_id=1, active_signals=active,
                      expired_signals=expired,
                      cache_hit_rate=0.85)
    assert ok, "Write failed"
    assert os.path.exists(tmp_path), "Output file missing"
    print("       ✅ wrote successfully\n")

    print("[2/4] Read back and validate schema...")
    data = writer.read()
    assert data is not None, "Read returned None"
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["scan_id"] == 1
    assert len(data["active_signals"]) == 1
    assert data["active_signals"][0]["symbol"] == "POLYCAB"
    assert data["metadata"]["cache_hit_rate"] == 0.85
    print("       ✅ schema verified\n")

    print("[3/4] Atomic write — no partial state...")
    # Verify tmp file doesn't linger
    assert not os.path.exists(tmp_path + ".tmp"), "Tmp file left behind"
    print("       ✅ no .tmp residue\n")

    print("[4/4] Overwrite preserves atomicity...")
    ok = writer.write(scan_id=2, active_signals=[], expired_signals=[],
                      cache_hit_rate=0.70)
    assert ok
    data2 = writer.read()
    assert data2["scan_id"] == 2
    print("       ✅ overwrite worked\n")

    # Cleanup
    try:
        os.remove(tmp_path)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    print("=" * 60)
    print("  ✅ ALL 4 TESTS PASSED")
    print("=" * 60 + "\n")

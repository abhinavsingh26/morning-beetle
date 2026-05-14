"""
QueueWriter v1.1 — atomic JSON writes for Signal Boy.

CHANGES IN v1.1:
    - Adds daily history JSONL append (signals/history/YYYY-MM-DD_scans.jsonl)
    - Each scan now writes BOTH:
        a) signals/queue_shadow.json (or queue.json) — LATEST snapshot (overwritten)
        b) signals/history/YYYY-MM-DD_scans.jsonl — APPEND-ONLY full history
    - Engine reads (a). Analysis reads (b).

Why:
    queue.json gets overwritten every 15 min — by EOD you've lost all earlier
    scan state. The JSONL history preserves every scan so you can compare
    against market behavior after the fact.

Schema version: 1.0 (unchanged).
Author: Abhinav (Phase 6D.3 v1.1, May 2026)
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


class QueueWriter:
    """Atomic JSON queue writer with daily JSONL history."""

    def __init__(self,
                 path: str = "signals/queue.json",
                 history_dir: Optional[str] = "signals/history",
                 history_enabled: bool = True):
        """
        Args:
            path:            path for the LATEST snapshot (overwritten)
            history_dir:     directory for daily JSONL append files
            history_enabled: master switch (True for shadow + production)
        """
        self.path            = path
        self.history_dir     = history_dir
        self.history_enabled = history_enabled

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if self.history_enabled and self.history_dir:
            os.makedirs(self.history_dir, exist_ok=True)

        logger.info(f"QueueWriter initialised → {path}")
        if self.history_enabled:
            logger.info(f"  history → {self.history_dir}/YYYY-MM-DD_scans.jsonl")

    def write(self,
              scan_id: int,
              active_signals: list,
              expired_signals: list,
              cache_hit_rate: Optional[float] = None,
              next_scan_at: Optional[str] = None,
              extra_metadata: Optional[dict] = None) -> bool:
        """
        Write a complete queue snapshot atomically.
        Also appends a copy to the daily history JSONL file.
        """
        now = datetime.now(timezone.utc)

        metadata = {
            "scans_today":   scan_id,
            "total_active":  len(active_signals),
            "total_expired": len(expired_signals),
        }
        if cache_hit_rate is not None:
            metadata["cache_hit_rate"] = round(cache_hit_rate, 3)
        if next_scan_at is not None:
            metadata["next_scan_at"] = next_scan_at
        if extra_metadata:
            metadata.update(extra_metadata)

        payload = {
            "schema_version":  SCHEMA_VERSION,
            "generated_at":    now.isoformat(),
            "scan_id":         scan_id,
            "trading_date":    now.date().isoformat(),
            "active_signals":  active_signals,
            "expired_signals": expired_signals,
            "metadata":        metadata,
        }

        # ── (a) Atomic write to the latest-snapshot file ──
        tmp_path = self.path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)
            logger.info(f"  Queue written: scan #{scan_id}, "
                       f"{len(active_signals)} active, "
                       f"{len(expired_signals)} expired → {self.path}")
        except Exception as e:
            logger.error(f"  QueueWriter latest write failed: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False

        # ── (b) Append to daily JSONL history ──
        if self.history_enabled and self.history_dir:
            try:
                date_str = now.date().isoformat()
                history_path = os.path.join(
                    self.history_dir,
                    f"{date_str}_scans.jsonl"
                )
                # One JSON object per line — append-only
                with open(history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                logger.debug(f"  History appended → {history_path}")
            except Exception as e:
                # History failure should NOT fail the scan
                logger.warning(f"  History append failed (non-fatal): {e}")

        return True

    def read(self) -> Optional[dict]:
        """Read the current latest-snapshot queue."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"  QueueWriter read failed: {e}")
            return None

    def read_history(self, date_str: Optional[str] = None) -> list:
        """
        Read a day's complete scan history.
        Returns list of scan payload dicts (in scan order).
        date_str: 'YYYY-MM-DD' (defaults to today UTC).
        """
        if not self.history_enabled or not self.history_dir:
            return []
        if date_str is None:
            date_str = datetime.now(timezone.utc).date().isoformat()
        history_path = os.path.join(
            self.history_dir, f"{date_str}_scans.jsonl"
        )
        if not os.path.exists(history_path):
            return []
        scans = []
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        scans.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"  Bad JSONL line: {e}")
        except Exception as e:
            logger.error(f"  History read failed: {e}")
        return scans


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 60)
    print("  QueueWriter v1.1 — Standalone Test")
    print("  (snapshot + daily JSONL history)")
    print("=" * 60 + "\n")

    tmp_dir = tempfile.mkdtemp(prefix="queue_writer_v11_")
    tmp_path = os.path.join(tmp_dir, "test_queue.json")
    history_dir = os.path.join(tmp_dir, "history")

    writer = QueueWriter(path=tmp_path, history_dir=history_dir)

    # ── Test 1: Three sequential writes ──
    print("[1/6] Write three sequential scans...")
    for i in range(1, 4):
        ok = writer.write(
            scan_id=i,
            active_signals=[
                {"symbol": "POLYCAB", "rank": 1, "composite_score": 0.9},
            ],
            expired_signals=[],
            cache_hit_rate=0.5 + i * 0.1,
        )
        assert ok
    print("       ✅ three writes succeeded\n")

    # ── Test 2: Latest snapshot is scan #3 ──
    print("[2/6] Verify latest snapshot is scan #3...")
    latest = writer.read()
    assert latest["scan_id"] == 3
    assert latest["schema_version"] == "1.0"
    print(f"       ✅ latest scan_id = {latest['scan_id']}\n")

    # ── Test 3: History has all 3 scans ──
    print("[3/6] Verify history has all 3 scans...")
    history = writer.read_history()
    assert len(history) == 3, f"Expected 3 history entries, got {len(history)}"
    assert [h["scan_id"] for h in history] == [1, 2, 3]
    print(f"       ✅ history contains {len(history)} scans in order\n")

    # ── Test 4: History file has one JSON per line ──
    print("[4/6] Verify JSONL format (one JSON per line)...")
    today = datetime.now(timezone.utc).date().isoformat()
    history_path = os.path.join(history_dir, f"{today}_scans.jsonl")
    with open(history_path) as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # must be valid JSON
    print(f"       ✅ {len(lines)} valid JSON lines\n")

    # ── Test 5: No .tmp residue ──
    print("[5/6] No .tmp residue after writes...")
    assert not os.path.exists(tmp_path + ".tmp")
    print("       ✅ clean\n")

    # ── Test 6: history_enabled=False skips append ──
    print("[6/6] history_enabled=False skips appending...")
    no_hist_path = os.path.join(tmp_dir, "no_hist.json")
    no_hist_dir = os.path.join(tmp_dir, "no_hist_history")
    writer_no = QueueWriter(
        path=no_hist_path,
        history_dir=no_hist_dir,
        history_enabled=False
    )
    writer_no.write(scan_id=1, active_signals=[], expired_signals=[])
    # History dir should NOT exist
    assert not os.path.exists(no_hist_dir)
    print("       ✅ history disabled, dir not created\n")

    # Cleanup
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print("=" * 60)
    print("  ✅ ALL 6 TESTS PASSED")
    print("=" * 60 + "\n")

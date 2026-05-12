"""
Signal Boy — intelligence layer for Morning Beetle.

Generates ranked, evidence-backed signals every 15 minutes throughout the
trading day. Lives as a background thread inside the main Morning Beetle
process (not a separate program).

Components:
    ingestion_cache.py   — SQLite-backed cache with per-source TTLs (THIS FILE: 6D.1)
    ranker.py            — composite scoring (6D.2, pending)
    queue_writer.py      — atomic JSON write (6D.3, pending)
    signal_boy.py        — orchestrator background thread (6D.3, pending)

Built in stages — see docs/SIGNAL_BOY_DESIGN.md.
"""

__version__ = "0.1.0"  # 6D.1 only — IngestionCache built

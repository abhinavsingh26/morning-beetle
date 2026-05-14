"""
Signal Boy — intelligence layer for Morning Beetle.

Components:
    ingestion_cache.py   — SQLite-backed cache (6D.1)
    ranker.py            — composite scoring (6D.2)
    queue_writer.py      — atomic JSON writes + daily JSONL history (6D.3 + Option C)
    news_archiver.py     — long-term news archive (Option C)
    signal_boy.py        — orchestrator background thread (6D.3 + Option C)
"""

__version__ = "0.4.0"   # Option C — history + archive + log rotation

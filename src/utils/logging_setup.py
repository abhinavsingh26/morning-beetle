"""
logging_setup.py — Shared logging configuration for Morning Beetle.

Provides setup_daily_rotating_logger() for any module that needs
date-stamped log files.

Behavior:
    - Logs go to logs/<component>/YYYY-MM-DD.log
    - Rotates at midnight (local time)
    - Keeps last 90 days, deletes older
    - Also prints to stdout (for console visibility)

Usage in any module:
    from src.utils.logging_setup import setup_daily_rotating_logger
    setup_daily_rotating_logger(component="engine", retention_days=90)
    # ... then logger = logging.getLogger(__name__) anywhere works

Author: Abhinav (Phase 6D housekeeping, May 2026)
"""

import os
import sys
import logging
import logging.handlers
from datetime import datetime


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_daily_rotating_logger(
    component: str = "engine",
    log_root: str = "logs",
    retention_days: int = 90,
    level: int = logging.INFO,
    console: bool = True,
    suppress_libs: bool = True,
) -> str:
    """
    Configure the ROOT logger with a daily rotating file handler.

    Args:
        component:      sub-folder name under log_root (e.g. 'engine', 'signal_boy')
        log_root:       top-level logs dir (default 'logs/')
        retention_days: number of dated files to keep (default 90)
        level:          logging level (default INFO)
        console:        also log to stdout (default True)
        suppress_libs:  downgrade noisy library loggers to WARNING

    Returns:
        The full path to the active log file.
    """
    log_dir = os.path.join(log_root, component)
    os.makedirs(log_dir, exist_ok=True)

    # Base filename is YYYY-MM-DD.log; rotator appends date suffix.
    today = datetime.now().date().isoformat()
    log_path = os.path.join(log_dir, f"{today}.log")

    # Get root logger
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (idempotent setup)
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    # File handler — rotate at midnight, keep last N days
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    # Suffix uses date format — files become 2026-05-14.log.2026-05-15 etc.
    # Override suffix for cleaner names:
    file_handler.suffix = "%Y-%m-%d"
    root.addHandler(file_handler)

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    # Tame noisy libraries
    if suppress_libs:
        for noisy in ("urllib3", "httpx", "httpcore", "feedparser",
                      "transformers", "huggingface_hub", "torch",
                      "filelock", "asyncio", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info(f"✅ Logger initialised → {log_path} (retention {retention_days} days)")
    return log_path


# ── Standalone test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    import shutil

    print("\n" + "=" * 60)
    print("  logging_setup — Standalone Test")
    print("=" * 60 + "\n")

    tmp_root = tempfile.mkdtemp(prefix="logging_setup_test_")

    print(f"[1/3] Set up rotating logger in {tmp_root}...")
    path = setup_daily_rotating_logger(
        component="test_component",
        log_root=tmp_root,
        retention_days=30,
    )
    print(f"       ✅ logger initialised → {path}\n")

    print("[2/3] Emit some log lines at different levels...")
    logger = logging.getLogger(__name__)
    logger.info("Info line")
    logger.warning("Warning line")
    logger.error("Error line")
    print("       ✅ emitted 3 lines\n")

    print("[3/3] Verify file exists and contains entries...")
    assert os.path.exists(path), f"Log file missing: {path}"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Info line" in content
    assert "Warning line" in content
    assert "Error line" in content
    print(f"       ✅ log file has all entries ({len(content)} bytes)\n")

    shutil.rmtree(tmp_root, ignore_errors=True)

    print("=" * 60)
    print("  ✅ ALL 3 TESTS PASSED")
    print("=" * 60 + "\n")
    print("Usage in modules:")
    print('  from src.utils.logging_setup import setup_daily_rotating_logger')
    print('  setup_daily_rotating_logger(component="engine")')
    print()

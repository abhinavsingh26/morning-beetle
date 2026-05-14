"""
run_signal_boy_shadow.py v2 — Shadow mode runner with Option C improvements.

CHANGES vs v1:
    - Uses daily rotating logs in logs/signal_boy/YYYY-MM-DD.log
    - Wires NewsArchiver → I:/01_Active_Projects/Morning_Beetle/04_news_archive/
    - Queue snapshot + JSONL history both written (via QueueWriter v1.1)

Outputs (per Signal Boy run):
    logs/signal_boy/2026-05-14.log               ← rotated daily
    signals/queue_shadow.json                     ← latest snapshot
    signals/history_shadow/2026-05-14_scans.jsonl ← every scan, append-only
    I:/.../04_news_archive/2026-05-14_news.jsonl  ← every headline ever seen

Usage (separate PowerShell terminal):
    cd C:\\Users\\Abhinav\\MorningBeetle_Dev
    python tools/run_signal_boy_shadow.py

Author: Abhinav (Phase 6D.3 Option C, May 2026)
"""

import sys
import os
import logging
import signal
from datetime import datetime

# ── Make src/ importable when running from project root ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Initialise rotating logger BEFORE any other imports ──
from src.utils.logging_setup import setup_daily_rotating_logger
log_path = setup_daily_rotating_logger(
    component="signal_boy",
    log_root="logs",
    retention_days=90,
)

logger = logging.getLogger("signal_boy_shadow")


def build_source_fetchers():
    """
    Build {source_id: callable() -> list[dict]} for SignalBoy.
    7 existing RSS feeds + 3 new sources (NSE filings/Pulse stubs + real PIB).
    """
    from src.beetle.news_fetcher import FEEDS, fetch_feed

    fetchers = {}
    for source_id, url in FEEDS.items():
        fetchers[source_id] = (
            lambda u=url, s=source_id: fetch_feed(u, s)
        )

    def _nse_filings_stub():
        logger.debug("  [stub] nse_filings — returning empty list")
        return []

    def _pulse_zerodha_stub():
        logger.debug("  [stub] pulse_zerodha — returning empty list")
        return []

    def _pib_defence_fetcher():
        try:
            import feedparser, calendar
            from datetime import timezone
            from src.beetle.news_fetcher import _headline_id

            url = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"
            feed = feedparser.parse(url)
            now = datetime.now(timezone.utc)
            headlines = []
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                if not title or len(title) < 10:
                    continue
                published_parsed = entry.get("published_parsed")
                published_str = entry.get("published", "")
                if published_parsed:
                    pub_ts = calendar.timegm(published_parsed)
                    pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                    if (now - pub_dt).total_seconds() / 3600 > 48:
                        continue
                headlines.append({
                    "title":     title,
                    "source":    "pib_defence",
                    "published": published_str,
                    "published_parsed": published_parsed,
                    "id":        _headline_id(title),
                })
            logger.info(f"  pib_defence: {len(headlines)} headlines")
            return headlines
        except Exception as e:
            logger.warning(f"  pib_defence fetch failed: {e}")
            return []

    fetchers["nse_filings"]   = _nse_filings_stub
    fetchers["pulse_zerodha"] = _pulse_zerodha_stub
    fetchers["pib_defence"]   = _pib_defence_fetcher

    return fetchers


def main():
    print("\n" + "=" * 70)
    print("  SIGNAL BOY — SHADOW MODE RUNNER v2")
    print("  (real RSS · real FinBERT · real EntityShield · NewsArchiver)")
    print("  Engine is NOT affected")
    print(f"  Log: {log_path}")
    print(f"  Queue snapshot: signals/queue_shadow.json")
    print(f"  Queue history:  signals/history_shadow/YYYY-MM-DD_scans.jsonl")
    print(f"  News archive:   I:/.../04_news_archive/YYYY-MM-DD_news.jsonl")
    print("=" * 70 + "\n")

    logger.info("─" * 60)
    logger.info(f"Signal Boy SHADOW starting at "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("─" * 60)

    logger.info("Loading real components...")
    from src.beetle.instrument_master import load_instruments
    from src.beetle.entity_shield     import filter_headlines
    from src.beetle.finbert_scorer    import score_headline
    from src.beetle.sector_heatmap    import get_heatmap
    from src.beetle.intelligence      import get_sector
    from src.beetle.signal_boy.signal_boy import SignalBoy
    from src.beetle.signal_boy.news_archiver import NewsArchiver

    logger.info("  → Loading instrument master...")
    instruments = load_instruments()
    logger.info(f"    {len(instruments)} instruments loaded")

    def instrument_token_lookup(symbol):
        data = instruments.get(symbol)
        if not data:
            return None
        return data.get("instrument_token") or data.get("token")

    logger.info("  → Wiring source fetchers...")
    source_fetchers = build_source_fetchers()
    logger.info(f"    {len(source_fetchers)} sources configured")
    for sid in source_fetchers:
        logger.info(f"      • {sid}")

    def sector_heatmap_provider():
        try:
            return get_heatmap(use_mock_if_closed=True)
        except Exception as e:
            logger.warning(f"  Heatmap fetch failed: {e}")
            return {}

    # ── NEW Option C — NewsArchiver wired ──
    logger.info("  → Initialising NewsArchiver...")
    news_archiver = NewsArchiver(
        archive_dir=r"I:\01_Active_Projects\Morning_Beetle\04_news_archive",
        fallback_dir="signals/news_archive",  # used if I: drive unavailable
        enabled=True,
    )

    logger.info("  → Constructing SignalBoy (shadow mode)...")
    sb = SignalBoy(
        source_fetchers          = source_fetchers,
        entity_shield_fn         = filter_headlines,
        finbert_scorer_fn        = score_headline,
        sector_lookup_fn         = get_sector,
        sector_heatmap_provider  = sector_heatmap_provider,
        instruments              = instruments,
        instrument_token_lookup  = instrument_token_lookup,
        mode                     = "shadow",
        cache_path               = "signals/ingestion_cache.db",
        queue_path               = "signals/queue_shadow.json",
        history_dir              = "signals/history_shadow",
        news_archiver            = news_archiver,
    )

    # Graceful shutdown
    shutdown_requested = {"flag": False}

    def handle_sigint(sig, frame):
        if shutdown_requested["flag"]:
            logger.warning("Second Ctrl+C — forcing exit")
            sys.exit(1)
        shutdown_requested["flag"] = True
        logger.info("Ctrl+C received — stopping SignalBoy gracefully...")
        sb.stop(timeout=15.0)
        logger.info("✅ Signal Boy stopped cleanly. Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        signal.signal(signal.SIGTERM, handle_sigint)
    except (ValueError, AttributeError):
        pass

    # Run one immediate scan
    logger.info("")
    logger.info("Running INITIAL scan to verify wiring...")
    try:
        result = sb.run_one_scan()
        logger.info(f"  Initial scan complete: "
                   f"{result['headlines']} headlines → "
                   f"{result['matched']} matched → "
                   f"{result['ranked']} ranked")
        if result['ranked'] > 0:
            logger.info("  TOP RANKED:")
            for s in result['active'][:5]:
                logger.info(
                    f"    #{s.get('rank', '?')}  {s['symbol']:<12} "
                    f"composite={s.get('composite_score', 0):.3f}  "
                    f"sentiment={s.get('sentiment_score', 0):+.2f}  "
                    f"{s.get('headline', '')[:70]}"
                )
        else:
            logger.info("  No tickers passed the 0.60 composite threshold.")
    except Exception as e:
        logger.error(f"  Initial scan FAILED: {e}", exc_info=True)
        sys.exit(1)

    logger.info("")
    logger.info("Starting 15-minute scan loop...")
    logger.info("  Scans run between 09:01 and 14:30 IST")
    logger.info("  Press Ctrl+C to stop")
    logger.info("─" * 60)

    sb.start()

    try:
        while sb.is_running() and not shutdown_requested["flag"]:
            import time
            time.sleep(5)
    except KeyboardInterrupt:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()

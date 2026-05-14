"""
run_signal_boy_shadow.py — Shadow mode runner for Signal Boy.

Purpose:
    Run Signal Boy with REAL fetchers, REAL EntityShield, REAL FinBERT
    in a separate process from the live engine. Writes to
    signals/queue_shadow.json (NOT queue.json). Engine is untouched.

How it works:
    - Imports the actual src.beetle modules
    - Wires 7 existing RSS feeds via news_fetcher.FEEDS
    - Adds 3 new sources (NSE filings, Pulse RSS, PIB Defence) as stubs
      that gracefully no-op until real fetcher functions exist
    - Runs SignalBoy in shadow mode
    - Outputs to signals/queue_shadow.json + signal_boy_shadow.log

Usage:
    Open a SEPARATE PowerShell terminal at the project root:

        cd C:\\Users\\Abhinav\\MorningBeetle_Dev
        python tools/run_signal_boy_shadow.py

    Leave it running alongside the engine. Press Ctrl+C to stop.
    Inspect signals/queue_shadow.json at any time to see latest scan output.

Safety:
    - This script DOES NOT touch main.py
    - This script DOES NOT modify the live watchlist.json
    - This script DOES NOT subscribe to WebSocket / place orders
    - It only reads RSS feeds and writes shadow output

Author: Abhinav (Phase 6D.3 — pre-6D.4 shadow validation, May 2026)
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

# ── Logging setup — separate log file so shadow output is independent ──
LOG_FILE = os.path.join(PROJECT_ROOT, "signal_boy_shadow.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("signal_boy_shadow")


def build_source_fetchers():
    """
    Build the dict of {source_id: callable() -> list[dict]} that
    SignalBoy expects.

    The 7 existing sources are wrapped lambdas around fetch_feed().
    The 3 new sources (NSE filings, Pulse RSS, PIB Defence) are added
    as safe no-op stubs — they'll return [] until real implementations
    are added. SignalBoy handles empty source returns gracefully.
    """
    from src.beetle.news_fetcher import FEEDS, fetch_feed

    fetchers = {}

    # ── Wrap existing 7 sources ──
    for source_id, url in FEEDS.items():
        # Capture by default-arg trick to avoid late binding
        fetchers[source_id] = (
            lambda u=url, s=source_id: fetch_feed(u, s)
        )

    # ── NEW v1 sources — placeholders, return empty list ──
    # When real fetchers exist, replace these lambdas with the actual
    # callables. SignalBoy's IngestionCache will still cache an empty
    # response so it doesn't repeatedly hammer a broken endpoint.

    def _nse_filings_stub():
        # TODO: implement NSE corporate filings fetcher in Phase 6E
        logger.debug("  [stub] nse_filings — returning empty list")
        return []

    def _pulse_zerodha_stub():
        # TODO: implement Pulse RSS / Zerodha fetcher in Phase 6E
        logger.debug("  [stub] pulse_zerodha — returning empty list")
        return []

    def _pib_defence_stub():
        # PIB Defence RSS — try to fetch with feedparser; if it works,
        # treat like a normal feed
        try:
            import feedparser
            import calendar
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
                    age_hours = (now - pub_dt).total_seconds() / 3600
                    if age_hours > 48:
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
    fetchers["pib_defence"]   = _pib_defence_stub

    return fetchers


def main():
    print("\n" + "=" * 70)
    print("  SIGNAL BOY — SHADOW MODE RUNNER")
    print("  (real RSS · real FinBERT · real EntityShield)")
    print("  Engine is NOT affected. Output → signals/queue_shadow.json")
    print("=" * 70 + "\n")

    logger.info("─" * 60)
    logger.info(f"Signal Boy SHADOW starting at "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("─" * 60)

    # ── Import REAL components ──
    logger.info("Loading real components...")
    from src.beetle.instrument_master import load_instruments
    from src.beetle.entity_shield     import filter_headlines
    from src.beetle.finbert_scorer    import score_headline
    from src.beetle.sector_heatmap    import get_heatmap
    from src.beetle.intelligence      import get_sector
    from src.beetle.signal_boy.signal_boy import SignalBoy

    logger.info("  → Loading instrument master...")
    instruments = load_instruments()
    logger.info(f"    {len(instruments)} instruments loaded")

    # Try to wire instrument_token lookup (used by 6D.4 universe manager)
    def instrument_token_lookup(symbol):
        data = instruments.get(symbol)
        if not data:
            return None
        return data.get("instrument_token") or data.get("token")

    # Wire source fetchers
    logger.info("  → Wiring source fetchers...")
    source_fetchers = build_source_fetchers()
    logger.info(f"    {len(source_fetchers)} sources configured")
    for sid in source_fetchers:
        logger.info(f"      • {sid}")

    # Sector heatmap provider — use mock if market closed
    def sector_heatmap_provider():
        try:
            return get_heatmap(use_mock_if_closed=True)
        except Exception as e:
            logger.warning(f"  Heatmap fetch failed: {e}")
            return {}

    # ── Construct SignalBoy in SHADOW mode ──
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
    )

    # ── Graceful shutdown handler ──
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

    signal.signal(signal.SIGINT,  handle_sigint)
    try:
        signal.signal(signal.SIGTERM, handle_sigint)
    except (ValueError, AttributeError):
        pass  # SIGTERM not always available on Windows

    # ── Run one immediate scan, then start the loop ──
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
            logger.info("  No tickers passed the 0.60 composite threshold "
                       "this scan.")
    except Exception as e:
        logger.error(f"  Initial scan FAILED: {e}", exc_info=True)
        logger.error("  Check news_fetcher, finbert_scorer, entity_shield, "
                    "and sector_heatmap modules.")
        sys.exit(1)

    # ── Start background loop ──
    logger.info("")
    logger.info("Starting 15-minute scan loop...")
    logger.info("  Scans run between 09:01 and 14:30 IST")
    logger.info("  Output → signals/queue_shadow.json (refreshed every 15 min)")
    logger.info("  Log    → signal_boy_shadow.log")
    logger.info("  Press Ctrl+C to stop")
    logger.info("─" * 60)

    sb.start()

    # ── Idle loop ──
    try:
        while sb.is_running() and not shutdown_requested["flag"]:
            import time
            time.sleep(5)
    except KeyboardInterrupt:
        handle_sigint(None, None)


if __name__ == "__main__":
    main()

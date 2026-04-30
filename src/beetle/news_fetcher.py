import feedparser
import requests
import hashlib
import logging
import time as time_module
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# News sources — NSE primary, Google News secondary
FEEDS = {
    "google_business":   "https://news.google.com/rss/search?q=NSE+India+stock+market&hl=en-IN&gl=IN&ceid=IN:en",
    "google_earnings":   "https://news.google.com/rss/search?q=India+quarterly+results+earnings&hl=en-IN&gl=IN&ceid=IN:en",
    "google_corporate":  "https://news.google.com/rss/search?q=India+stock+dividend+acquisition+order+win&hl=en-IN&gl=IN&ceid=IN:en",
    # ── New sources ───────────────────────────────────────────────
    "livemint_markets":  "https://www.livemint.com/rss/markets",
    "livemint_companies":"https://www.livemint.com/rss/companies",
    "hindu_business":    "https://www.thehindu.com/business/markets/feeder/default.rss",
    "ndtv_profit":       "https://feeds.feedburner.com/ndtvprofit-latest",
}

# ── On-demand ticker headline cache (used by Strategy S5) ──────────
_ticker_cache = {}   # symbol -> (timestamp, headlines)
_CACHE_TTL_SECONDS = 300   # 5 minutes


def _headline_id(title: str) -> str:
    """Generate dedup key from headline text."""
    return hashlib.md5(title.strip().lower().encode()).hexdigest()


def fetch_feed(url: str, source_name: str) -> list:
    """Fetch a single RSS feed. Returns list of headline dicts."""
    headlines = []
    try:
        feed = feedparser.parse(url)
        now  = datetime.now(timezone.utc)

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            if not title or len(title) < 10:
                continue

            # Parse published date
            published_parsed = entry.get("published_parsed")
            published_str    = entry.get("published", "")

            if published_parsed:
                import calendar
                pub_timestamp = calendar.timegm(published_parsed)
                pub_dt = datetime.fromtimestamp(pub_timestamp,
                                                tz=timezone.utc)
                age_hours = (now - pub_dt).total_seconds() / 3600

                # Skip headlines older than 48 hours
                if age_hours > 48:
                    logger.debug(f"  Skipping stale headline "
                                f"({age_hours:.0f}h old): {title[:40]}")
                    continue

            headlines.append({
                "title":            title,
                "source":           source_name,
                "published":        published_str,
                "published_parsed": published_parsed,
                "id":               _headline_id(title)
            })

        logger.info(f"  {source_name}: {len(headlines)} headlines")
    except Exception as e:
        logger.warning(f"  {source_name}: FAILED — {e}")
    return headlines


def fetch_all_headlines(max_per_source: int = 20) -> list:
    """
    Fetch from all sources. Deduplicate by headline hash.
    Returns list of unique headline dicts, newest first.
    """
    logger.info("Fetching headlines from all sources...")
    seen_ids = set()
    all_headlines = []

    for source_name, url in FEEDS.items():
        headlines = fetch_feed(url, source_name)
        for h in headlines[:max_per_source]:
            if h["id"] not in seen_ids:
                seen_ids.add(h["id"])
                all_headlines.append(h)

    logger.info(f"Total unique headlines: {len(all_headlines)}")
    return all_headlines


def fetch_for_ticker(symbol: str,
                     since_minutes: int = 120) -> list:
    """
    Fetch fresh headlines mentioning a specific ticker.
    Used by Strategy S5 for on-demand FinBERT re-confirmation.

    Args:
        symbol:        NSE ticker (e.g. 'INFY', 'TCS')
        since_minutes: Only return headlines from last N minutes (default 120)

    Returns:
        List of headline dicts filtered to mentions of this ticker.
        Cached for 5 minutes per ticker to avoid repeated RSS hits.
    """
    now_ts = time_module.time()

    # Check cache
    if symbol in _ticker_cache:
        cached_time, cached_headlines = _ticker_cache[symbol]
        if (now_ts - cached_time) < _CACHE_TTL_SECONDS:
            logger.debug(f"  fetch_for_ticker({symbol}): cache hit "
                        f"({len(cached_headlines)} headlines)")
            return cached_headlines

    # Fetch all headlines
    all_headlines = fetch_all_headlines(max_per_source=20)

    # Build search terms — ticker symbol + known aliases
    search_terms = [symbol.upper()]
    try:
        from src.beetle.entity_shield import KNOWN_ALIASES
        for alias, mapped_symbol in KNOWN_ALIASES.items():
            if mapped_symbol == symbol:
                search_terms.append(alias.upper())
    except Exception as e:
        logger.debug(f"  Could not load aliases: {e}")

    # Filter to headlines mentioning this ticker
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    matched = []

    for h in all_headlines:
        title_upper = h["title"].upper()

        # Check if any search term is in the headline
        matches_ticker = any(term in title_upper for term in search_terms)
        if not matches_ticker:
            continue

        # Check freshness if pubDate available
        published_parsed = h.get("published_parsed")
        if published_parsed:
            import calendar
            pub_ts = calendar.timegm(published_parsed)
            pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
            if pub_dt < cutoff:
                continue

        matched.append(h)

    # Cache result
    _ticker_cache[symbol] = (now_ts, matched)
    logger.info(f"  fetch_for_ticker({symbol}): {len(matched)} fresh headlines")
    return matched


def clear_ticker_cache():
    """Clear the on-demand ticker headline cache."""
    _ticker_cache.clear()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    headlines = fetch_all_headlines()

    print(f"\n✅ Fetched {len(headlines)} unique headlines\n")
    print(f"{'#':<4} {'Source':<20} {'Headline'}")
    print("-" * 90)
    for i, h in enumerate(headlines, 1):
        title = h['title'][:65] + "..." if len(h['title']) > 65 else h['title']
        print(f"{i:<4} {h['source']:<20} {title}")
import feedparser
import requests
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# News sources — NSE primary, Google News secondary
FEEDS = {
    "google_business":   "https://news.google.com/rss/search?q=NSE+India+stock+market&hl=en-IN&gl=IN&ceid=IN:en",
    "google_earnings":   "https://news.google.com/rss/search?q=India+quarterly+results+earnings&hl=en-IN&gl=IN&ceid=IN:en",
    "google_corporate":  "https://news.google.com/rss/search?q=India+stock+dividend+acquisition+order+win&hl=en-IN&gl=IN&ceid=IN:en",
}

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
                "title":     title,
                "source":    source_name,
                "published": published_str,
                "id":        _headline_id(title)
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
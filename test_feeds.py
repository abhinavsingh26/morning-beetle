import feedparser

feeds = [
    "https://www.livemint.com/rss/markets",
    "https://www.livemint.com/rss/companies",
    "https://www.livemint.com/rss/news",
    "https://www.thehindu.com/business/markets/feeder/default.rss",
    "https://feeds.feedburner.com/ndtvprofit-latest",
    "https://www.financialexpress.com/market/feed/",
]

for url in feeds:
    feed = feedparser.parse(url)
    print(f"\n{url}")
    print(f"  Entries: {len(feed.entries)}")
    if feed.entries:
        title = feed.entries[0].get("title", "")[:60]
        date  = feed.entries[0].get("published", "no date")
        print(f"  Latest: {title}")
        print(f"  Date:   {date}")
    else:
        print("  No entries found")
# save as test_ticker_fetch2.py
from src.beetle.news_fetcher import fetch_for_ticker, clear_ticker_cache

# Clear cache for fresh test
clear_ticker_cache()

# Test with various tickers and longer freshness window
for symbol in ["MARUTI", "INFY", "TCS", "RELIANCE", "HDFCBANK", "COALINDIA"]:
    result = fetch_for_ticker(symbol, since_minutes=2880)  # 48 hours
    print(f"{symbol}: {len(result)} headlines")
    for h in result[:2]:
        print(f"  - {h['title'][:75]}")
    print()
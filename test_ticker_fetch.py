from src.beetle.news_fetcher import fetch_for_ticker
import time as t

# Test 1 — known ticker
result = fetch_for_ticker("MARUTI", since_minutes=240)
print(f"MARUTI fresh headlines: {len(result)}")
for h in result[:3]:
    title = h["title"][:80]
    print(f"  - {title}")

# Test 2 — cache hit on second call
start = t.time()
result2 = fetch_for_ticker("MARUTI", since_minutes=240)
elapsed = t.time() - start
print(f"\nSecond call (should be instant cache hit): {elapsed:.3f}s")
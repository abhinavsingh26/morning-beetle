"""
test_pib_feed_v2.py — Verify Accept-Language header forces English content.

The v1 diagnostic showed PIB returns Hindi when fetched via requests/feedparser,
even though browser shows English. We suspect it's Accept-Language based.

This v2 test:
    1. Fetches with various Accept-Language combinations
    2. Shows first few titles for each variant
    3. Identifies which (if any) returns English content

Usage:
    python tools/test_pib_feed_v2.py
"""

import sys
import os
import requests
import feedparser

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PIB_URL = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"


def is_english(title: str) -> bool:
    """Crude check: at least 80% ASCII chars suggests English."""
    if not title:
        return False
    ascii_count = sum(1 for c in title if ord(c) < 128)
    return (ascii_count / len(title)) > 0.8


def try_fetch(label: str, headers: dict):
    print(f"\n── {label} ──")
    print(f"   Headers: {headers}")
    try:
        resp = requests.get(PIB_URL, timeout=10, headers=headers)
        print(f"   Status: {resp.status_code}, size: {len(resp.content)} bytes")
        if resp.status_code != 200:
            print("   ❌ Non-200 — skipping")
            return None

        feed = feedparser.parse(resp.text)
        if len(feed.entries) == 0:
            print("   ❌ Zero entries parsed")
            return None

        english_count = 0
        hindi_count = 0
        sample = []
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            if not title:
                continue
            if is_english(title):
                english_count += 1
            else:
                hindi_count += 1
            sample.append(title[:80])

        print(f"   Entries: {len(feed.entries)} total")
        print(f"   Language sample (first 10): {english_count} English, {hindi_count} non-English")
        print(f"   First 3 titles:")
        for s in sample[:3]:
            print(f"     - {s}")

        if english_count > hindi_count:
            print(f"   ✅ ENGLISH CONTENT DETECTED")
            return "english"
        else:
            print(f"   ⚠️  Content appears non-English")
            return "non-english"
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return None


def main():
    print("=" * 75)
    print("  PIB Feed — Accept-Language Header Test")
    print("=" * 75)
    print(f"\n  URL: {PIB_URL}")

    results = {}

    # Variant 1: No headers at all (current behavior)
    results["bare"] = try_fetch(
        "Variant A: No headers",
        {}
    )

    # Variant 2: Just User-Agent
    results["ua_only"] = try_fetch(
        "Variant B: Browser User-Agent only",
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"}
    )

    # Variant 3: User-Agent + Accept-Language en-IN
    results["en_in"] = try_fetch(
        "Variant C: en-IN preference",
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
        }
    )

    # Variant 4: Accept-Language en-US
    results["en_us"] = try_fetch(
        "Variant D: en-US preference",
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # Variant 5: Accept header set to RSS
    results["accept_rss"] = try_fetch(
        "Variant E: Accept = application/rss+xml + en-IN",
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "application/rss+xml, application/xml, text/xml",
        }
    )

    # ── Summary ──
    print("\n" + "=" * 75)
    print("  SUMMARY")
    print("=" * 75)
    for label, result in results.items():
        emoji = "✅" if result == "english" else "❌" if result == "non-english" else "—"
        print(f"  {emoji} {label}: {result}")

    english_variants = [k for k, v in results.items() if v == "english"]
    if english_variants:
        print(f"\n  USE THIS: {english_variants[0]}")
    else:
        print("\n  ⚠️  No variant returned English. PIB may use cookies/sessions for language.")
        print("      Workaround: keep Lang=1 and translate Hindi titles, or filter on")
        print("      the bilingual headlines that DO appear in the feed.")

    print()


if __name__ == "__main__":
    main()
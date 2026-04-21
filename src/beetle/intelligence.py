import os
import json
import logging
from datetime import datetime
from collections import defaultdict

from src.beetle.instrument_master import load_instruments
from src.beetle.news_fetcher import fetch_all_headlines
from src.beetle.entity_shield import filter_headlines, DEAD_ZONE_MIN, DEAD_ZONE_MAX
from src.beetle.finbert_scorer import score_headline
from src.beetle.sector_heatmap import get_heatmap

load = __import__("dotenv").load_dotenv
load("config/.env")

logger = logging.getLogger(__name__)

# Sector map — ticker prefix to Nifty sector index
SECTOR_MAP = {
    "HDFCBANK":   "NIFTY BANK",
    "ICICIBANK":  "NIFTY BANK",
    "SBIN":       "NIFTY PSU BANK",
    "MAHABANK":   "NIFTY PSU BANK",
    "IDFCFIRSTB": "NIFTY BANK",
    "KOTAKBANK":  "NIFTY BANK",
    "AXISBANK":   "NIFTY BANK",
    "INDUSINDBK": "NIFTY BANK",
    "INFY":       "NIFTY IT",
    "TCS":        "NIFTY IT",
    "WIPRO":      "NIFTY IT",
    "HCLTECH":    "NIFTY IT",
    "TECHM":      "NIFTY IT",
    "LTIM":       "NIFTY IT",
    "PERSISTENT": "NIFTY IT",
    "MPHASIS":    "NIFTY IT",
    "HFCL":       "NIFTY IT",
    "NESTLEIND":  "NIFTY FMCG",
    "HINDUNILVR": "NIFTY FMCG",
    "BRITANNIA":  "NIFTY FMCG",
    "TATAMOTORS": "NIFTY AUTO",
    "MARUTI":     "NIFTY AUTO",
    "EICHERMOT":  "NIFTY AUTO",
    "HEROMOTOCO": "NIFTY AUTO",
    "M&M":        "NIFTY AUTO",
    "RELIANCE":   "NIFTY ENERGY",
    "ONGC":       "NIFTY ENERGY",
    "NTPC":       "NIFTY ENERGY",
    "ADANIPOWER": "NIFTY ENERGY",
    "SUNPHARMA":  "NIFTY PHARMA",
    "DRREDDY":    "NIFTY PHARMA",
    "CIPLA":      "NIFTY PHARMA",
    "DIVISLAB":   "NIFTY PHARMA",
    "TATASTEEL":  "NIFTY METAL",
    "JSWSTEEL":   "NIFTY METAL",
    "HINDALCO":   "NIFTY METAL",
    "VEDL":       "NIFTY METAL",
    "ADANIPORTS": "NIFTY REALTY",
    "ADANIENT":   "NIFTY REALTY",
    "MAZDOCK":    "NIFTY REALTY",
    "HAL":        "NIFTY REALTY",
    "BEL":        "NIFTY REALTY",
    "BDL":        "NIFTY REALTY",
    "ZENTEC":     "NIFTY REALTY",
    "DCXINDIA":   "NIFTY REALTY",
    "ASIANPAINT": "NIFTY CONSUMER DURABLES",
    "CASTROLIND": "NIFTY CONSUMER DURABLES",
}

MAX_WATCHLIST = 3


def get_sector(symbol: str) -> str:
    """Look up sector for a ticker symbol."""
    return SECTOR_MAP.get(symbol, "UNKNOWN")


def run_pipeline(use_mock_heatmap: bool = False) -> list:
    """
    Full Morning Beetle pre-market pipeline.
    Returns watchlist as list of dicts.
    """
    start_time = datetime.now()
    logger.info("=" * 55)
    logger.info("  MORNING BEETLE — Pre-Market Intelligence Run")
    logger.info(f"  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    # Step 1 — Load instruments
    logger.info("\n[1/5] Loading instrument master...")
    instruments = load_instruments()
    logger.info(f"      {len(instruments)} instruments loaded.")

    # Step 2 — Fetch headlines
    logger.info("\n[2/5] Fetching headlines...")
    raw_headlines = fetch_all_headlines(max_per_source=20)
    logger.info(f"      {len(raw_headlines)} unique headlines fetched.")

    # Step 3 — EntityShield: match to tickers
    logger.info("\n[3/5] Running EntityShield...")
    matched = filter_headlines(raw_headlines, instruments)
    logger.info(f"      {len(matched)} headlines matched to tickers.")

    # Step 4 — FinBERT scoring
    logger.info("\n[4/5] Scoring with FinBERT...")
    scored = []
    for h in matched:
        result = score_headline(h["title"])
        score  = result["score"]

        # Dead zone filter
        if DEAD_ZONE_MIN <= score <= DEAD_ZONE_MAX:
            logger.debug(f"      Dead zone: {h['ticker']} ({score:.3f}) — {h['title'][:50]}")
            continue

        scored.append({
            **h,
            "sentiment_score": score,
            "sentiment_label": result["label"]
        })

    logger.info(f"      {len(scored)} headlines passed dead zone filter.")

    # Step 5 — Sector heatmap + convergence gate
    logger.info("\n[5/5] Sector convergence gate...")
    heatmap = get_heatmap(use_mock_if_closed=True)

    candidates = []
    for h in scored:
        symbol = h["ticker"]
        sector = get_sector(symbol)
        sector_data = heatmap.get(sector, {})
        sector_bias = sector_data.get("bias", "UNKNOWN")

        # Convergence gate: sentiment must align with sector
        sentiment = h["sentiment_label"]
        if sentiment == "BULLISH" and sector_bias == "BEARISH":
            logger.info(f"      DROPPED {symbol}: BULLISH signal but {sector} is BEARISH")
            continue
        if sentiment == "BEARISH" and sector_bias == "BULLISH":
            logger.info(f"      DROPPED {symbol}: BEARISH signal but {sector} is BULLISH")
            continue

        candidates.append({
            "symbol":          symbol,
            "name":            h["ticker_name"],
            "sentiment_score": h["sentiment_score"],
            "sentiment_label": h["sentiment_label"],
            "sector":          sector,
            "sector_bias":     sector_bias,
            "confidence":      h["confidence"],
            "headline":        h["title"],
            "source":          h["source"]
        })

    # Deduplicate by symbol — keep highest confidence
    deduped = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in deduped or c["confidence"] > deduped[sym]["confidence"]:
            deduped[sym] = c

    # Sort by abs(sentiment_score) descending, take top 3
    watchlist = sorted(
        deduped.values(),
        key=lambda x: abs(x["sentiment_score"]),
        reverse=True
    )[:MAX_WATCHLIST]

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"\n✅ Pipeline complete in {elapsed:.1f}s")
    logger.info(f"   Watchlist: {[w['symbol'] for w in watchlist]}")

    return watchlist


def save_watchlist(watchlist: list, path: str = "watchlist.json"):
    """Save watchlist to JSON file."""
    output = {
        "generated_at": datetime.now().isoformat(),
        "count":        len(watchlist),
        "tickers":      watchlist
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"✅ watchlist.json saved → {path}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s — %(message)s"
    )

    watchlist = run_pipeline()
    save_watchlist(watchlist)

    print("\n" + "=" * 55)
    print("  MORNING BEETLE WATCHLIST")
    print("=" * 55)
    if not watchlist:
        print("  ⚠️  No high-conviction tickers found today.")
    else:
        for i, t in enumerate(watchlist, 1):
            score = t["sentiment_score"]
            icon  = "🟢" if score > 0 else "🔴"
            print(f"\n  [{i}] {t['symbol']} — {t['name']}")
            print(f"       Sentiment : {icon} {score:+.3f} ({t['sentiment_label']})")
            print(f"       Sector    : {t['sector']} → {t['sector_bias']}")
            print(f"       Confidence: {t['confidence']}")
            print(f"       Headline  : {t['headline'][:70]}")
    print("\n" + "=" * 55)
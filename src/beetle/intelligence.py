import os
import json
import logging
from datetime import datetime, time as dtime
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
    # ── Banking ──────────────────────────────────────────────────
    "HDFCBANK":   "NIFTY BANK",
    "ICICIBANK":  "NIFTY BANK",
    "IDFCFIRSTB": "NIFTY BANK",
    "KOTAKBANK":  "NIFTY BANK",
    "AXISBANK":   "NIFTY BANK",
    "INDUSINDBK": "NIFTY BANK",
    "BANDHANBNK": "NIFTY BANK",
    "FEDERALBNK": "NIFTY BANK",
    "RBLBANK":    "NIFTY BANK",
    "YESBANK":    "NIFTY BANK",
    # ── PSU Banks ─────────────────────────────────────────────────
    "SBIN":       "NIFTY PSU BANK",
    "MAHABANK":   "NIFTY PSU BANK",
    "PNB":        "NIFTY PSU BANK",
    "BANKBARODA": "NIFTY PSU BANK",
    "CANARABANK": "NIFTY PSU BANK",
    "UNIONBANK":  "NIFTY PSU BANK",
    "INDIANB":    "NIFTY PSU BANK",
    "UCOBANK":    "NIFTY PSU BANK",
    "CENTRALBK":  "NIFTY PSU BANK",
    "BANKINDIA":  "NIFTY PSU BANK",
    # ── IT ────────────────────────────────────────────────────────
    "INFY":       "NIFTY IT",
    "TCS":        "NIFTY IT",
    "WIPRO":      "NIFTY IT",
    "HCLTECH":    "NIFTY IT",
    "TECHM":      "NIFTY IT",
    "LTIM":       "NIFTY IT",
    "PERSISTENT": "NIFTY IT",
    "MPHASIS":    "NIFTY IT",
    "HFCL":       "NIFTY IT",
    "COFORGE":    "NIFTY IT",
    "KPITTECH":   "NIFTY IT",
    "TATAELXSI":  "NIFTY IT",
    "CMSINFO":    "NIFTY IT",
    # ── FMCG ─────────────────────────────────────────────────────
    "NESTLEIND":  "NIFTY FMCG",
    "HINDUNILVR": "NIFTY FMCG",
    "BRITANNIA":  "NIFTY FMCG",
    "DABUR":      "NIFTY FMCG",
    "MARICO":     "NIFTY FMCG",
    "COLPAL":     "NIFTY FMCG",
    "GODREJCP":   "NIFTY FMCG",
    "TRENT":      "NIFTY FMCG",
    "ABFRL":      "NIFTY FMCG",
    "DMART":      "NIFTY FMCG",
    "VBL":        "NIFTY FMCG",
    "ITC":        "NIFTY FMCG",
    # ── Auto ──────────────────────────────────────────────────────
    "TMCV":       "NIFTY AUTO",
    "MARUTI":     "NIFTY AUTO",
    "EICHERMOT":  "NIFTY AUTO",
    "HEROMOTOCO": "NIFTY AUTO",
    "M&M":        "NIFTY AUTO",
    "BAJAJ-AUTO": "NIFTY AUTO",
    "TVSMOTOR":   "NIFTY AUTO",
    "ASHOKLEY":   "NIFTY AUTO",
    "MOTHERSON":  "NIFTY AUTO",
    "BALKRISIND": "NIFTY AUTO",
    # ── Pharma ───────────────────────────────────────────────────
    "SUNPHARMA":  "NIFTY PHARMA",
    "DRREDDY":    "NIFTY PHARMA",
    "CIPLA":      "NIFTY PHARMA",
    "DIVISLAB":   "NIFTY PHARMA",
    "AUROPHARMA": "NIFTY PHARMA",
    "LUPIN":      "NIFTY PHARMA",
    "TORNTPHARM": "NIFTY PHARMA",
    "ALKEM":      "NIFTY PHARMA",
    "IPCALAB":    "NIFTY PHARMA",
    "GLENMARK":   "NIFTY PHARMA",
    # ── Metal ─────────────────────────────────────────────────────
    "TATASTEEL":  "NIFTY METAL",
    "JSWSTEEL":   "NIFTY METAL",
    "HINDALCO":   "NIFTY METAL",
    "VEDL":       "NIFTY METAL",
    "SAIL":       "NIFTY METAL",
    "NMDC":       "NIFTY METAL",
    "JINDALSTEL": "NIFTY METAL",
    "NATIONALUM": "NIFTY METAL",
    "COALINDIA":  "NIFTY METAL",
    # ── Energy ───────────────────────────────────────────────────
    "RELIANCE":   "NIFTY ENERGY",
    "ONGC":       "NIFTY ENERGY",
    "NTPC":       "NIFTY ENERGY",
    "ADANIPOWER": "NIFTY ENERGY",
    "POWERGRID":  "NIFTY ENERGY",
    "TATAPOWER":  "NIFTY ENERGY",
    "ADANIGREEN": "NIFTY ENERGY",
    "CESC":       "NIFTY ENERGY",
    "TORNTPOWER": "NIFTY ENERGY",
    "BPCL":       "NIFTY ENERGY",
    "IOC":        "NIFTY ENERGY",
    "HINDPETRO":  "NIFTY ENERGY",
    "GAIL":       "NIFTY ENERGY",
    # ── Realty ───────────────────────────────────────────────────
    "ADANIPORTS": "NIFTY REALTY",
    "DLF":        "NIFTY REALTY",
    "GODREJPROP": "NIFTY REALTY",
    "OBEROIRLTY": "NIFTY REALTY",
    "PHOENIXLTD": "NIFTY REALTY",
    "PRESTIGE":   "NIFTY REALTY",
    "BRIGADE":    "NIFTY REALTY",
    # ── Defence/PSU (mapped to Realty as closest) ─────────────────
    "HAL":        "NIFTY REALTY",
    "BEL":        "NIFTY REALTY",
    "BDL":        "NIFTY REALTY",
    "MAZDOCK":    "NIFTY REALTY",
    "ZENTEC":     "NIFTY REALTY",
    "DCXINDIA":   "NIFTY REALTY",
    "COCHINSHIP": "NIFTY REALTY",
    "GRSE":       "NIFTY REALTY",
    # ── Media ────────────────────────────────────────────────────
    "ZEEL":       "NIFTY MEDIA",
    "SUNTV":      "NIFTY MEDIA",
    "PVRINOX":    "NIFTY MEDIA",
    "NETWORK18":  "NIFTY MEDIA",
    # ── Consumer Durables ─────────────────────────────────────────
    "ASIANPAINT": "NIFTY CONSUMER DURABLES",
    "CASTROLIND": "NIFTY CONSUMER DURABLES",
    "VOLTAS":     "NIFTY CONSUMER DURABLES",
    "HAVELLS":    "NIFTY CONSUMER DURABLES",
    "TITAN":      "NIFTY CONSUMER DURABLES",
    "WHIRLPOOL":  "NIFTY CONSUMER DURABLES",
    "BLUEDART":   "NIFTY CONSUMER DURABLES",
    "CROMPTON":   "NIFTY CONSUMER DURABLES",
    # ── Healthcare ───────────────────────────────────────────────
    "APOLLOHOSP": "NIFTY HEALTHCARE",
    "FORTIS":     "NIFTY HEALTHCARE",
    "MAXHEALTH":  "NIFTY HEALTHCARE",
    "MEDANTA":    "NIFTY HEALTHCARE",
    "LALPATHLAB": "NIFTY HEALTHCARE",
    "METROPOLIS": "NIFTY HEALTHCARE",
    "THYROCARE":  "NIFTY HEALTHCARE",
    # ── Finance/NBFC ─────────────────────────────────────────────
    "BAJFINANCE": "NIFTY BANK",
    "BAJAJFINSV": "NIFTY BANK",
    "HDFCLIFE":   "NIFTY BANK",
    "SBILIFE":    "NIFTY BANK",
    "ICICIGI":    "NIFTY BANK",
    "MUTHOOTFIN": "NIFTY BANK",
    "CHOLAFIN":   "NIFTY BANK",
    "M&MFIN":     "NIFTY BANK",
    "IRFC":       "NIFTY PSU BANK",
    "RECLTD":     "NIFTY PSU BANK",
    "PFC":        "NIFTY PSU BANK",
    # ── Telecom ──────────────────────────────────────────────────
    "BHARTIARTL": "NIFTY IT",
    "IDEA":       "NIFTY IT",
    # ── Conglomerate ─────────────────────────────────────────────
    "ADANIENT":   "NIFTY ENERGY",
    "TATACHEM":   "NIFTY METAL",
    "GRASIM":     "NIFTY CONSUMER DURABLES",
    "ULTRACEMCO": "NIFTY CONSUMER DURABLES",
    "AMBUJACEM":  "NIFTY CONSUMER DURABLES",
    "ACCLIMITED": "NIFTY CONSUMER DURABLES",
    # ── New Age / Tech ────────────────────────────────────────────
    "ZOMATO":     "NIFTY IT",
    "PAYTM":      "NIFTY IT",
    "FSN":        "NIFTY FMCG",
    "POLICYBZR":  "NIFTY BANK",
    "DELHIVERY":  "NIFTY IT",
    "INDHOTEL":   "NIFTY FMCG",
}

MAX_WATCHLIST       = 5   # Scan top 5 tickers
MAX_POSITIONS       = 3   # Take first 3 that cross all gates


def get_sector(symbol: str) -> str:
    """Look up sector for a ticker symbol."""
    return SECTOR_MAP.get(symbol, "UNKNOWN")


def run_pipeline(use_mock_heatmap: bool = False) -> list:
    """
    Full Morning Beetle pre-market pipeline.
    Split timing:
    - 09:01 AM: News fetch + FinBERT scoring
    - 09:12 AM: Live heatmap fetch + convergence gate
    - Returns watchlist as list of dicts.
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
            continue

        scored.append({
            **h,
            "sentiment_score": score,
            "sentiment_label": result["label"]
        })

    logger.info(f"      {len(scored)} headlines passed dead zone filter.")

    # Step 5 — Wait for market open then fetch live heatmap
    logger.info("\n[5/5] Sector convergence gate...")
    now = datetime.now().time()
    market_open = dtime(9, 15)
    heatmap_time = dtime(9, 12)

    if now < heatmap_time and not use_mock_heatmap:
        # Wait until 09:12 for live sector data
        wait_seconds = (
            datetime.combine(datetime.today(), heatmap_time) -
            datetime.now()
        ).total_seconds()
        if wait_seconds > 0:
            logger.info(f"      Waiting {wait_seconds:.0f}s for market data "
                       f"(heatmap fetch at 09:12)...")
            import time as time_module
            time_module.sleep(wait_seconds)

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

    # Sort by abs(sentiment_score) descending, take top MAX_WATCHLIST
    watchlist = sorted(
        deduped.values(),
        key=lambda x: abs(x["sentiment_score"]),
        reverse=True
    )[:MAX_WATCHLIST]

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"\n✅ Pipeline complete in {elapsed:.1f}s")
    logger.info(f"   Watchlist: {[w['symbol'] for w in watchlist]}")
    logger.info(f"   Candidates expanded: {len(watchlist)}/{MAX_WATCHLIST} "
               f"(max {MAX_POSITIONS} positions)")

    return watchlist

def run_pipeline_fresh(exclude_symbols: list = []) -> list:
    """
    Re-run pipeline excluding already-subscribed symbols.
    Used when dynamic universe refresh is triggered.
    """
    logger.info("🔄 Dynamic universe refresh triggered...")
    
    # Step 1 — Load instruments
    instruments = load_instruments()

    # Step 2 — Fetch fresh headlines
    raw_headlines = fetch_all_headlines(max_per_source=20)

    # Step 3 — EntityShield
    matched = filter_headlines(raw_headlines, instruments)

    # Step 4 — FinBERT scoring
    scored = []
    for h in matched:
        result = score_headline(h["title"])
        score  = result["score"]
        if DEAD_ZONE_MIN <= score <= DEAD_ZONE_MAX:
            continue
        # Skip already subscribed symbols
        if h["ticker"] in exclude_symbols:
            continue
        scored.append({
            **h,
            "sentiment_score": score,
            "sentiment_label": result["label"]
        })

    # Step 5 — Heatmap gate
    heatmap = get_heatmap(use_mock_if_closed=True)
    candidates = []
    for h in scored:
        symbol = h["ticker"]
        sector = get_sector(symbol)
        sector_data = heatmap.get(sector, {})
        sector_bias = sector_data.get("bias", "UNKNOWN")
        sentiment   = h["sentiment_label"]

        if sentiment == "BULLISH" and sector_bias == "BEARISH":
            continue
        if sentiment == "BEARISH" and sector_bias == "BULLISH":
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

    # Deduplicate and sort
    deduped = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in deduped or c["confidence"] > deduped[sym]["confidence"]:
            deduped[sym] = c

    fresh = sorted(
        deduped.values(),
        key=lambda x: abs(x["sentiment_score"]),
        reverse=True
    )[:MAX_WATCHLIST]

    logger.info(f"🔄 Fresh candidates: {[f['symbol'] for f in fresh]}")
    return fresh

def save_watchlist(watchlist: list, path: str = "watchlist.json"):
    """Save watchlist to JSON file."""
    output = {
        "generated_at": datetime.now().isoformat(),
        "count":        len(watchlist),
        "max_positions": MAX_POSITIONS,
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
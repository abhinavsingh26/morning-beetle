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
from src.beetle.liquidity_filter import LiquidityFilter

load = __import__("dotenv").load_dotenv
load("config/.env")

logger = logging.getLogger(__name__)

# ── v9.4 NEW — Suffix exclusion list ──────────────────────────────
# Tickers with these suffixes are NOT eligible for intraday MIS:
#   -BZ : BSE Trade-for-Trade (T+0 settlement, illiquid)
#   -SM : SME segment (low volume, wide spreads)
#   -ST : SME Trade-for-Trade
#   -BE : Trade-for-Trade (NSE)
#   -IL : Illiquid
#   -IT : Illiquid Trade-for-Trade
EXCLUDED_SUFFIXES = ("-BZ", "-SM", "-ST", "-BE", "-IL", "-IT")


def is_tradeable(symbol: str) -> bool:
    """
    Filter out tickers that aren't suitable for intraday MIS:
    - Suffix-based exclusions (BSE Trade-for-Trade, SME, etc.)
    - Penny stocks (price will be checked at signal time, not here)
    """
    for suf in EXCLUDED_SUFFIXES:
        if symbol.endswith(suf):
            return False
    return True


# Sector map — ticker prefix to Nifty sector index
# ====================================================================
# MASTER_SECTOR_MAP - v9.6 (Day 13 EOD, 2026-05-20)
# 216 NSE tickers -> Nifty sector index.
#
# Sector NAMES here MUST exactly match keys in sector_heatmap.py
# SECTOR_INDICES, or the convergence gate resolves UNKNOWN.
#
# 4 renames applied vs draft (to match heatmap keys):
#   NIFTY EV & NEW AGE AUTOMOTIVE -> NIFTY EV
#   NIFTY HEALTHCARE INDEX        -> NIFTY HEALTHCARE
#   NIFTY OIL & GAS               -> NIFTY ENERGY
#   NIFTY PRIVATE BANK            -> NIFTY BANK
#
# Heatmap-covered (live bias): 176 tickers across 13 sectors.
# UNKNOWN (no live index): NIFTY500 HEALTHCARE (25) +
#   NIFTY FINANCIAL SERVICES 25/50 (15) = 40 tickers.
# ====================================================================
SECTOR_MAP = {
    # -- Banking (incl. private banks BANDHANBNK/RBLBANK folded in) (16) --
    "AUBANK":       "NIFTY BANK",
    "AXISBANK":     "NIFTY BANK",
    "BANDHANBNK":   "NIFTY BANK",
    "BANKBARODA":   "NIFTY BANK",
    "CANBK":        "NIFTY BANK",
    "FEDERALBNK":   "NIFTY BANK",
    "HDFCBANK":     "NIFTY BANK",
    "ICICIBANK":    "NIFTY BANK",
    "IDFCFIRSTB":   "NIFTY BANK",
    "INDUSINDBK":   "NIFTY BANK",
    "KOTAKBANK":    "NIFTY BANK",
    "PNB":          "NIFTY BANK",
    "RBLBANK":      "NIFTY BANK",
    "SBIN":         "NIFTY BANK",
    "UNIONBANK":    "NIFTY BANK",
    "YESBANK":      "NIFTY BANK",

    # -- PSU Banking (7) --
    "BANKINDIA":    "NIFTY PSU BANK",
    "CENTRALBK":    "NIFTY PSU BANK",
    "INDIANB":      "NIFTY PSU BANK",
    "IOB":          "NIFTY PSU BANK",
    "MAHABANK":     "NIFTY PSU BANK",
    "PSB":          "NIFTY PSU BANK",
    "UCOBANK":      "NIFTY PSU BANK",

    # -- IT (10) --
    "COFORGE":      "NIFTY IT",
    "HCLTECH":      "NIFTY IT",
    "INFY":         "NIFTY IT",
    "LTM":          "NIFTY IT",
    "MPHASIS":      "NIFTY IT",
    "OFSS":         "NIFTY IT",
    "PERSISTENT":   "NIFTY IT",
    "TCS":          "NIFTY IT",
    "TECHM":        "NIFTY IT",
    "WIPRO":        "NIFTY IT",

    # -- FMCG (15) --
    "BRITANNIA":    "NIFTY FMCG",
    "COLPAL":       "NIFTY FMCG",
    "DABUR":        "NIFTY FMCG",
    "EMAMILTD":     "NIFTY FMCG",
    "GODREJCP":     "NIFTY FMCG",
    "HINDUNILVR":   "NIFTY FMCG",
    "ITC":          "NIFTY FMCG",
    "MARICO":       "NIFTY FMCG",
    "NESTLEIND":    "NIFTY FMCG",
    "PATANJALI":    "NIFTY FMCG",
    "RADICO":       "NIFTY FMCG",
    "TATACONSUM":   "NIFTY FMCG",
    "UBL":          "NIFTY FMCG",
    "UNITDSPR":     "NIFTY FMCG",
    "VBL":          "NIFTY FMCG",

    # -- Auto (15) --
    "ASHOKLEY":     "NIFTY AUTO",
    "BAJAJ-AUTO":   "NIFTY AUTO",
    "BHARATFORG":   "NIFTY AUTO",
    "BOSCHLTD":     "NIFTY AUTO",
    "EICHERMOT":    "NIFTY AUTO",
    "EXIDEIND":     "NIFTY AUTO",
    "HEROMOTOCO":   "NIFTY AUTO",
    "M&M":          "NIFTY AUTO",
    "MARUTI":       "NIFTY AUTO",
    "MOTHERSON":    "NIFTY AUTO",
    "SONACOMS":     "NIFTY AUTO",
    "TIINDIA":      "NIFTY AUTO",
    "TMPV":         "NIFTY AUTO",
    "TVSMOTOR":     "NIFTY AUTO",
    "UNOMINDA":     "NIFTY AUTO",

    # -- Media (10) --
    "DBCORP":       "NIFTY MEDIA",
    "HATHWAY":      "NIFTY MEDIA",
    "NAZARA":       "NIFTY MEDIA",
    "NETWORK18":    "NIFTY MEDIA",
    "PFOCUS":       "NIFTY MEDIA",
    "PVRINOX":      "NIFTY MEDIA",
    "SAREGAMA":     "NIFTY MEDIA",
    "SUNTV":        "NIFTY MEDIA",
    "TIPSMUSIC":    "NIFTY MEDIA",
    "ZEEL":         "NIFTY MEDIA",

    # -- Pharma (20) --
    "ABBOTINDIA":   "NIFTY PHARMA",
    "AJANTPHARM":   "NIFTY PHARMA",
    "ALKEM":        "NIFTY PHARMA",
    "AUROPHARMA":   "NIFTY PHARMA",
    "BIOCON":       "NIFTY PHARMA",
    "CIPLA":        "NIFTY PHARMA",
    "DIVISLAB":     "NIFTY PHARMA",
    "DRREDDY":      "NIFTY PHARMA",
    "GLAND":        "NIFTY PHARMA",
    "GLENMARK":     "NIFTY PHARMA",
    "IPCALAB":      "NIFTY PHARMA",
    "JBCHEPHARM":   "NIFTY PHARMA",
    "LAURUSLABS":   "NIFTY PHARMA",
    "LUPIN":        "NIFTY PHARMA",
    "MANKIND":      "NIFTY PHARMA",
    "PPLPHARMA":    "NIFTY PHARMA",
    "SUNPHARMA":    "NIFTY PHARMA",
    "TORNTPHARM":   "NIFTY PHARMA",
    "WOCKPHARMA":   "NIFTY PHARMA",
    "ZYDUSLIFE":    "NIFTY PHARMA",

    # -- Healthcare (hospitals/large-cap) - heatmap key NIFTY HEALTHCARE (4) --
    "APOLLOHOSP":   "NIFTY HEALTHCARE",
    "FORTIS":       "NIFTY HEALTHCARE",
    "MAXHEALTH":    "NIFTY HEALTHCARE",
    "SYNGENE":      "NIFTY HEALTHCARE",

    # -- Healthcare 500 - NO live index, resolves UNKNOWN (6E.2 neutral-scoring) (25) --
    "ACUTAAS":      "NIFTY500 HEALTHCARE",
    "ANTHEM":       "NIFTY500 HEALTHCARE",
    "ASTERDM":      "NIFTY500 HEALTHCARE",
    "BLUEJET":      "NIFTY500 HEALTHCARE",
    "CAPLIPOINT":   "NIFTY500 HEALTHCARE",
    "COHANCE":      "NIFTY500 HEALTHCARE",
    "CONCORDBIO":   "NIFTY500 HEALTHCARE",
    "EMCURE":       "NIFTY500 HEALTHCARE",
    "ERIS":         "NIFTY500 HEALTHCARE",
    "GLAXO":        "NIFTY500 HEALTHCARE",
    "GRANULES":     "NIFTY500 HEALTHCARE",
    "INDGN":        "NIFTY500 HEALTHCARE",
    "JUBLPHARMA":   "NIFTY500 HEALTHCARE",
    "KIMS":         "NIFTY500 HEALTHCARE",
    "LALPATHLAB":   "NIFTY500 HEALTHCARE",
    "MEDANTA":      "NIFTY500 HEALTHCARE",
    "NATCOPHARM":   "NIFTY500 HEALTHCARE",
    "NEULANDLAB":   "NIFTY500 HEALTHCARE",
    "NH":           "NIFTY500 HEALTHCARE",
    "ONESOURCE":    "NIFTY500 HEALTHCARE",
    "PFIZER":       "NIFTY500 HEALTHCARE",
    "POLYMED":      "NIFTY500 HEALTHCARE",
    "RAINBOW":      "NIFTY500 HEALTHCARE",
    "SAILIFE":      "NIFTY500 HEALTHCARE",
    "VIJAYA":       "NIFTY500 HEALTHCARE",

    # -- Defence - heatmap key NIFTY INDIA DEFENCE (18) --
    "AEQUS":        "NIFTY INDIA DEFENCE",
    "APOLLO":       "NIFTY INDIA DEFENCE",
    "ASTRAMICRO":   "NIFTY INDIA DEFENCE",
    "AXISCADES":    "NIFTY INDIA DEFENCE",
    "BDL":          "NIFTY INDIA DEFENCE",
    "BEL":          "NIFTY INDIA DEFENCE",
    "BEML":         "NIFTY INDIA DEFENCE",
    "COCHINSHIP":   "NIFTY INDIA DEFENCE",
    "DATAPATTNS":   "NIFTY INDIA DEFENCE",
    "DYNAMATECH":   "NIFTY INDIA DEFENCE",
    "GRSE":         "NIFTY INDIA DEFENCE",
    "HAL":          "NIFTY INDIA DEFENCE",
    "MAZDOCK":      "NIFTY INDIA DEFENCE",
    "MIDHANI":      "NIFTY INDIA DEFENCE",
    "MTARTECH":     "NIFTY INDIA DEFENCE",
    "PARAS":        "NIFTY INDIA DEFENCE",
    "SOLARINDS":    "NIFTY INDIA DEFENCE",
    "ZENTEC":       "NIFTY INDIA DEFENCE",

    # -- Oil & Gas / Energy - heatmap key NIFTY ENERGY (was OIL & GAS) (15) --
    "AEGISLOG":     "NIFTY ENERGY",
    "AEGISVOPAK":   "NIFTY ENERGY",
    "ATGL":         "NIFTY ENERGY",
    "BPCL":         "NIFTY ENERGY",
    "CASTROLIND":   "NIFTY ENERGY",
    "CHENNPETRO":   "NIFTY ENERGY",
    "GAIL":         "NIFTY ENERGY",
    "HINDPETRO":    "NIFTY ENERGY",
    "IGL":          "NIFTY ENERGY",
    "IOC":          "NIFTY ENERGY",
    "MGL":          "NIFTY ENERGY",
    "OIL":          "NIFTY ENERGY",
    "ONGC":         "NIFTY ENERGY",
    "PETRONET":     "NIFTY ENERGY",
    "RELIANCE":     "NIFTY ENERGY",

    # -- Realty (10) --
    "ABREL":        "NIFTY REALTY",
    "ANANTRAJ":     "NIFTY REALTY",
    "BRIGADE":      "NIFTY REALTY",
    "DLF":          "NIFTY REALTY",
    "GODREJPROP":   "NIFTY REALTY",
    "LODHA":        "NIFTY REALTY",
    "OBEROIRLTY":   "NIFTY REALTY",
    "PHOENIXLTD":   "NIFTY REALTY",
    "PRESTIGE":     "NIFTY REALTY",
    "SOBHA":        "NIFTY REALTY",

    # -- Metal (15) --
    "ADANIENT":     "NIFTY METAL",
    "APLAPOLLO":    "NIFTY METAL",
    "HINDALCO":     "NIFTY METAL",
    "HINDCOPPER":   "NIFTY METAL",
    "HINDZINC":     "NIFTY METAL",
    "JINDALSTEL":   "NIFTY METAL",
    "JSL":          "NIFTY METAL",
    "JSWSTEEL":     "NIFTY METAL",
    "LLOYDSME":     "NIFTY METAL",
    "NATIONALUM":   "NIFTY METAL",
    "NMDC":         "NIFTY METAL",
    "SAIL":         "NIFTY METAL",
    "TATASTEEL":    "NIFTY METAL",
    "VEDL":         "NIFTY METAL",
    "WELCORP":      "NIFTY METAL",

    # -- Fin Services 25/50 - NOT in heatmap yet, resolves UNKNOWN (15) --
    "BAJAJFINSV":   "NIFTY FINANCIAL SERVICES 25/50",
    "BAJFINANCE":   "NIFTY FINANCIAL SERVICES 25/50",
    "BSE":          "NIFTY FINANCIAL SERVICES 25/50",
    "CHOLAFIN":     "NIFTY FINANCIAL SERVICES 25/50",
    "HDFCLIFE":     "NIFTY FINANCIAL SERVICES 25/50",
    "ICICIGI":      "NIFTY FINANCIAL SERVICES 25/50",
    "JIOFIN":       "NIFTY FINANCIAL SERVICES 25/50",
    "LICHSGFIN":    "NIFTY FINANCIAL SERVICES 25/50",
    "MFSL":         "NIFTY FINANCIAL SERVICES 25/50",
    "MUTHOOTFIN":   "NIFTY FINANCIAL SERVICES 25/50",
    "PFC":          "NIFTY FINANCIAL SERVICES 25/50",
    "RECLTD":       "NIFTY FINANCIAL SERVICES 25/50",
    "SBICARD":      "NIFTY FINANCIAL SERVICES 25/50",
    "SBILIFE":      "NIFTY FINANCIAL SERVICES 25/50",
    "SHRIRAMFIN":   "NIFTY FINANCIAL SERVICES 25/50",

    # -- EV & New Age Automotive - heatmap key NIFTY EV (21) --
    "ARE&M":        "NIFTY EV",
    "ATHERENERG":   "NIFTY EV",
    "FLUOROCHEM":   "NIFTY EV",
    "FORCEMOT":     "NIFTY EV",
    "HSCL":         "NIFTY EV",
    "HYUNDAI":      "NIFTY EV",
    "JBMA":         "NIFTY EV",
    "JWL":          "NIFTY EV",
    "KEI":          "NIFTY EV",
    "KPITTECH":     "NIFTY EV",
    "LTTS":         "NIFTY EV",
    "MINDACORP":    "NIFTY EV",
    "MSUMI":        "NIFTY EV",
    "OLAELEC":      "NIFTY EV",
    "OLECTRA":      "NIFTY EV",
    "SCHAEFFLER":   "NIFTY EV",
    "TATACHEM":     "NIFTY EV",
    "TATAELXSI":    "NIFTY EV",
    "TATATECH":     "NIFTY EV",
    "TMCV":         "NIFTY EV",
    "ZFCVINDIA":    "NIFTY EV",

}

MAX_WATCHLIST       = 15   # Scan top 10 tickers
MAX_POSITIONS       = 3   # Take first 3 that cross all gates


def get_sector(symbol: str) -> str:
    """Look up sector for a ticker symbol."""
    return SECTOR_MAP.get(symbol, "UNKNOWN")


def _apply_convergence_and_dedup(scored: list, heatmap: dict) -> list:
    """
    Shared logic for both run_pipeline and run_pipeline_fresh.
    Applies:
    1. Suffix-based eligibility filter (v9.4)
    2. Sector convergence gate
    3. Deduplication by symbol
    Returns list ready for sort+truncate.
    """
    candidates = []
    for h in scored:
        symbol = h["ticker"]

        # v9.4 — Suffix exclusion filter
        if not is_tradeable(symbol):
            logger.info(f"      DROPPED {symbol}: non-MIS-eligible suffix")
            continue

        sector = get_sector(symbol)
        sector_data = heatmap.get(sector, {})
        sector_bias = sector_data.get("bias", "UNKNOWN")

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
    # Deduplicate by symbol — keep highest confidence
    deduped = {}
    for c in candidates:
        sym = c["symbol"]
        if sym not in deduped or c["confidence"] > deduped[sym]["confidence"]:
            deduped[sym] = c
    deduped_list = list(deduped.values())

    # v9.6.1 — Liquidity filter (5 lakh shares/day floor)
    # Jun 1 SILINV disaster: microcap with 1-5 ticks/min lost Rs414.
    # This stops thin stocks from reaching the watchlist regardless
    # of how strong their sentiment is.
    try:
        instruments = load_instruments()
        instrument_map = {
            sym: instruments[sym]["instrument_token"]
            for sym in (c["symbol"] for c in deduped_list)
            if sym in instruments
        }
        from kiteconnect import KiteConnect
        api_key = os.getenv("ZERODHA_API_KEY")
        access_token = os.getenv("ZERODHA_ACCESS_TOKEN")
        if api_key and access_token:
            kite = KiteConnect(api_key=api_key)
            kite.set_access_token(access_token)
            liq = LiquidityFilter(kite_rest=kite)
            deduped_list = liq.filter_candidates(deduped_list, instrument_map)
        else:
            logger.warning("  LiquidityFilter SKIPPED — no Kite credentials")
    except Exception as e:
        logger.error(f"  LiquidityFilter error: {e} — fail-open (keeping all)")

    return deduped_list


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
    heatmap_time = dtime(9, 12)

    if now < heatmap_time and not use_mock_heatmap:
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

    # v9.4 — Use shared filter/dedup helper (includes suffix exclusion)
    deduped = _apply_convergence_and_dedup(scored, heatmap)

    # Sort by abs(sentiment_score) descending, take top MAX_WATCHLIST
    watchlist = sorted(
        deduped,
        key=lambda x: abs(x["sentiment_score"]),
        reverse=True
    )[:MAX_WATCHLIST]

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"\n✅ Pipeline complete in {elapsed:.1f}s")
    logger.info(f"   Watchlist: {[w['symbol'] for w in watchlist]}")
    logger.info(f"   Candidates expanded: {len(watchlist)}/{MAX_WATCHLIST} "
               f"(max {MAX_POSITIONS} positions)")

    return watchlist


def run_pipeline_fresh(exclude_symbols: list = None) -> list:
    """
    Re-run pipeline excluding already-subscribed symbols.
    Used when dynamic universe refresh is triggered.
    v9.4 — Now uses suffix filter, same as run_pipeline().
    """
    if exclude_symbols is None:
        exclude_symbols = []
    logger.info("🔄 Dynamic universe refresh triggered...")

    instruments = load_instruments()
    raw_headlines = fetch_all_headlines(max_per_source=20)
    matched = filter_headlines(raw_headlines, instruments)

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

    heatmap = get_heatmap(use_mock_if_closed=True)

    # v9.4 — Use shared filter/dedup helper (includes suffix exclusion)
    deduped = _apply_convergence_and_dedup(scored, heatmap)

    fresh = sorted(
        deduped,
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
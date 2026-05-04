import re
import logging
from thefuzz import fuzz
from src.beetle.instrument_master import load_instruments

logger = logging.getLogger(__name__)

# Keywords that boost confidence score
BOOST_KEYWORDS = [
    "DIVIDEND", "BONUS", "CONTRACT", "ACQUISITION", "ORDER WIN",
    "RESULTS BEAT", "QUARTERLY RESULTS", "Q1", "Q2", "Q3", "Q4",
    "IPO", "MERGER", "BUYBACK", "RIGHTS ISSUE", "DEMERGER",
    "PAT", "REVENUE", "NII", "EBITDA"
]

# Dead zone: discard headlines with sentiment score in this range
DEAD_ZONE_MIN = -0.1
DEAD_ZONE_MAX = +0.1

# Fuzzy match threshold
MATCH_THRESHOLD = 85

# Known aliases — longer aliases checked first (sort by length)
KNOWN_ALIASES = {
    # ── DISAMBIGUATION (specific phrases checked before generic) ──
    "KOTAK MAHINDRA BANK":   "KOTAKBANK",
    "KOTAK MAHINDRA":        "KOTAKBANK",
    "ASIAN ENERGY SERVICES": "ASIANENE",
    "ASIAN ENERGY":          "ASIANENE",
    # ── TATA (specific before general) ───────────────────────────
    "TATA CONSULTANCY":  "TCS",
    "TATA CHEMICALS":    "TATACHEM",
    "TATA CONSUMER":     "TATACONSUM",
    "TATA MOTORS":       "TMCV",
    "TATA STEEL":        "TATASTEEL",
    "TATA POWER":        "TATAPOWER",
    "TATA ELXSI":        "TATAELXSI",
    "TATA COMM":         "TATACOMM",
    "TATAMOTORS":        "TMCV",
    # ── Banks ────────────────────────────────────────────────────
    "HINDUSTAN UNILEVER":  "HINDUNILVR",
    "BANK OF MAHARASHTRA": "MAHABANK",
    "BHARAT ELECTRONICS":  "BEL",
    "BHARAT DYNAMICS":     "BDL",
    "BHARTI AIRTEL":       "BHARTIARTL",
    "BAJAJ FINANCE":       "BAJFINANCE",
    "BAJAJ FINSERV":       "BAJAJFINSV",
    "HDFC BANK":           "HDFCBANK",
    "HDFC LIFE":           "HDFCLIFE",
    "ICICI BANK":          "ICICIBANK",
    "AXIS BANK":           "AXISBANK",
    "IDFC FIRST":          "IDFCFIRSTB",
    "PNB HOUSING":         "PNBHOUSING",
    "INDUSIND":            "INDUSINDBK",
    "STATE BANK":          "SBIN",
    "KOTAK":               "KOTAKBANK",
    "SBI":                 "SBIN",
    # ── IT ───────────────────────────────────────────────────────
    "HCL TECHNOLOGIES":  "HCLTECH",
    "TECH MAHINDRA":     "TECHM",
    "LTIMINDTREE":       "LTIM",
    "HCL TECH":          "HCLTECH",
    "HCLTECH":           "HCLTECH",
    "PERSISTENT":        "PERSISTENT",
    "MPHASIS":           "MPHASIS",
    "INFOSYS":           "INFY",
    "WIPRO":             "WIPRO",
    "HFCL":              "HFCL",
    "HCL":               "HCLTECH",
    "LTI":               "LTIM",
    # ── Pharma ───────────────────────────────────────────────────
    "SUN PHARMA":  "SUNPHARMA",
    "DR REDDY":    "DRREDDY",
    "CIPLA":       "CIPLA",
    "DIVIS":       "DIVISLAB",
    # ── Energy/PSU ───────────────────────────────────────────────
    "ADANI ENTERPRISES": "ADANIENT",
    "ADANI PORTS":       "ADANIPORTS",
    "ADANI POWER":       "ADANIPOWER",
    "ADANI GREEN":       "ADANIGREEN",
    "POWER GRID":        "POWERGRID",
    "RELIANCE":          "RELIANCE",
    "ONGC":              "ONGC",
    "NTPC":              "NTPC",
    # ── Auto ─────────────────────────────────────────────────────
    "HERO MOTO": "HEROMOTOCO",
    "MAHINDRA":  "M&M",
    "MARUTI":    "MARUTI",
    "EICHER":    "EICHERMOT",
    # ── FMCG ─────────────────────────────────────────────────────
    "NESTLE":       "NESTLEIND",
    "NESTLÉ":       "NESTLEIND",
    "ASIAN PAINTS": "ASIANPAINT",
    "HUL":          "HINDUNILVR",
    "AIRTEL":       "BHARTIARTL",
    # ── Metal ────────────────────────────────────────────────────
    "JSW STEEL":  "JSWSTEEL",
    "HINDALCO":   "HINDALCO",
    "ULTRATECH":  "ULTRACEMCO",
    "VEDANTA":    "VEDL",
    "GRASIM":     "GRASIM",
    "COAL INDIA": "COALINDIA",
    "COALINDIA":  "COALINDIA",
    # ── Defence ──────────────────────────────────────────────────
    "MAZAGON DOCK":    "MAZDOCK",
    "COCHIN SHIPYARD": "COCHINSHIP",
    "GARDEN REACH":    "GRSE",
    "MAZAGON":         "MAZDOCK",
    "ZEN TECH":        "ZENTEC",
    "DCX SYSTEMS":     "DCXINDIA",
    "HAL":             "HAL",
    "BEL":             "BEL",
    # ── New Age ──────────────────────────────────────────────────
    "POLICYBAZAAR":  "POLICYBZR",
    "INDIAN HOTELS": "INDHOTEL",
    "DELHIVERY":     "DELHIVERY",
    "CASTROL":       "CASTROLIND",
    "ZOMATO":        "ZOMATO",
    "PAYTM":         "PAYTM",
    "NYKAA":         "FSN",
    "IRFC":          "IRFC",
}

# Generic market terms — skip these, no specific ticker
GENERIC_TERMS = [
    # Market-wide terms
    "SENSEX", "NIFTY", "MARKET", "INDICES", "INDEX",
    "BROADER MARKET", "BAROMETERS", "BAROMETER",
    "ADVANCE DECLINE", "BREADTH", "MARKET BREADTH",
    # Crypto/global noise
    "BITCOIN", "ETHEREUM", "CRYPTO", "CRYPTOCURRENCY",
    "ASIAN STOCKS RISE", "ASIAN STOCKS",
    # International figures/companies (not Indian market)
    "BERKSHIRE", "BUFFETT", "GREG ABEL", "WARREN BUFFETT",
    "BERKSHIRE HATHAWAY",
    # HR/internal corporate noise (not market-moving)
    "RESIGNATION OF", "ASSISTANT VICE PRESIDENT", "HUMAN RESOURCE",
    # Macro/Policy
    "RBI", "SEBI", "FII", "DII", "INFLATION", "GDP",
    "ECONOMY", "ECONOMIC", "INTEREST RATE", "REPO RATE",
    "MONETARY POLICY", "FISCAL POLICY", "BUDGET",
    # Market sentiment
    "BULL MARKET", "BEAR MARKET", "STOCK MARKET",
    "GLOBAL CUES", "CRUDE OIL", "RUPEE", "DOLLAR",
    "VIX", "VOLATILITY INDEX", "FEAR INDEX",
    # Generic market moves
    "MKTS", "MKT", "BLUECHIP", "BLUE-CHIP", "BLUE CHIP",
    "RALLY", "CORRECTION", "SELL-OFF", "SELLOFF",
    "STIMULUS", "BAILOUT", "ELECTION",
    # Indices
    "MIDCAP", "SMALLCAP", "LARGECAP", "BSE", "NSE",
    "NIFTY50", "NIFTY 50", "SENSEX30",
    # Politics/world events
    "EXIT POLL", "DONALD TRUMP", "US FED", "FEDERAL RESERVE",
    "IRAN", "TAIWAN", "KERALA", "PUDUCHERRY", "ALLAHABAD",
    "ELON MUSK", "OPENAI",
    # Weather/disasters
    "BENGALURU RAINS", "RAINS", "RAINFALL", "MONSOON",
    "FLOOD", "CYCLONE", "WEATHER",
    # Commodities
    "GOLD DEMAND", "WORLD GOLD COUNCIL", "WGC",
    "SILVER DEMAND", "GOLD PRICE", "OIL PRICES",
    # Macro commentary
    "DEVELOPED NATION", "JEFFREY SACHS", "GROWTH TRAJECTORY",
    "ECONOMIC GROWTH", "GDP GROWTH",
]

# Tickers with generic-sounding names — require specific context word.
# Prevents M&M matching headlines about Kotak Mahindra,
# BEL matching headlines about Berkshire's Greg Abel, etc.
AMBIGUOUS_TICKERS = {
    "ASIANENE":   ["ENERGY", "OIL", "GAS", "DRILLING", "ASIAN ENERGY"],
    "M&M":        ["MAHINDRA &", "M&M", "MAHINDRA AUTO",
                   "MAHINDRA SUV", "TRACTORS", "MAHINDRA Q"],
    "BEL":        ["BHARAT ELECTRONICS", "DEFENCE",
                   "RADAR", "ELECTRONICS LTD"],
    "RAIN":       ["RAIN INDUSTRIES", "RAIN COMMODITIES"],
    "GOCLCORP":   ["GOCL", "EXPLOSIVES", "DETONATOR"],
}


def _keyword_boost(headline: str) -> float:
    """Return +0.2 boost if headline contains a high-conviction keyword."""
    headline_upper = headline.upper()
    for kw in BOOST_KEYWORDS:
        if kw in headline_upper:
            return 0.2
    return 0.0


def _clean_headline(headline: str) -> str:
    """Strip punctuation and normalize for matching."""
    headline = re.sub(r'[^\w\s]', ' ', headline)
    return headline.upper().strip()


def _is_generic(headline: str) -> bool:
    """Return True if headline is about broad market, not a specific stock."""
    headline_upper = headline.upper()
    for term in GENERIC_TERMS:
        if term in headline_upper:
            return True
    return False


def _is_ambiguous_match(symbol: str, headline_upper: str) -> bool:
    """
    Return True if symbol is in AMBIGUOUS_TICKERS AND no required
    context word is present in the headline.
    Used to reject false-positive fuzzy matches.
    """
    context_words = AMBIGUOUS_TICKERS.get(symbol, [])
    if not context_words:
        return False
    return not any(word in headline_upper for word in context_words)


def find_ticker(headline: str, instruments: dict,
                threshold: int = MATCH_THRESHOLD) -> dict | None:
    """
    Match headline to ticker.
    Step 1: Special case for TATA MOTORS (NSE symbol = TMCV).
    Step 2: Check known aliases (longest first).
    Step 3: Filter out generic market headlines.
    Step 4: Fuzzy match against instrument search anchors.
    Step 5: Reject ambiguous matches missing required context.
    """
    headline_upper = headline.upper()
    cleaned = _clean_headline(headline)

    # Special case: TATA MOTORS → TMCV
    if "TATA MOTORS" in headline_upper:
        if "TMCV" in instruments:
            boost = _keyword_boost(headline)
            return {
                "symbol":     "TMCV",
                "name":       instruments["TMCV"]["name"],
                "confidence": round(min(1.0, 0.95 + boost), 3),
                "raw_score":  100,
                "boosted":    boost > 0
            }

    # Step 1 — Known aliases (longest first)
    for alias, sym in sorted(KNOWN_ALIASES.items(),
                              key=lambda x: len(x[0]), reverse=True):
        if alias.upper() in headline_upper:
            if sym in instruments:
                # Even known aliases get ambiguity check
                if _is_ambiguous_match(sym, headline_upper):
                    logger.debug(f"  Skipping ambiguous alias '{alias}'→{sym}")
                    continue
                boost = _keyword_boost(headline)
                return {
                    "symbol":     sym,
                    "name":       instruments[sym]["name"],
                    "confidence": round(min(1.0, 0.95 + boost), 3),
                    "raw_score":  100,
                    "boosted":    boost > 0
                }

    # Step 2 — Skip generic market headlines
    if _is_generic(headline):
        return None

    # Step 3 — Fuzzy match
    best_score = 0
    best_match = None

    for sym, data in instruments.items():
        anchor = data["search_anchor"]
        if not anchor or len(anchor) < 4:
            continue
        score1 = fuzz.partial_ratio(anchor, cleaned)
        score2 = fuzz.token_set_ratio(anchor, cleaned)
        score  = max(score1, score2)
        if score > best_score:
            best_score = score
            best_match = data

    if best_score < threshold:
        return None

    # Step 4 — Ambiguity check
    if _is_ambiguous_match(best_match["symbol"], headline_upper):
        logger.debug(f"  Skipping ambiguous fuzzy match: {best_match['symbol']}")
        return None

    boost = _keyword_boost(headline)
    return {
        "symbol":     best_match["symbol"],
        "name":       best_match["name"],
        "confidence": round(min(1.0, (best_score / 100) + boost), 3),
        "raw_score":  best_score,
        "boosted":    boost > 0
    }


def filter_headlines(headlines: list, instruments: dict) -> list:
    """
    Run EntityShield on a list of headline dicts.
    Returns filtered list with ticker matches attached.
    """
    results = []
    for h in headlines:
        match = find_ticker(h["title"], instruments)
        if match is None:
            continue
        results.append({
            **h,
            "ticker":      match["symbol"],
            "ticker_name": match["name"],
            "confidence":  match["confidence"],
            "boosted":     match["boosted"]
        })

    logger.info(
        f"EntityShield: {len(results)}/{len(headlines)} "
        f"headlines matched to tickers"
    )
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    instruments = load_instruments()

    # Yesterday's good and bad matches
    test_cases = [
        # (headline, expected_ticker_or_None)
        ("Nestlé India shares jump 6% to hit 52-week high on strong Q4", "NESTLEIND"),
        ("HDFC Bank Q3 beats estimates, NII up 15%",                     "HDFCBANK"),
        ("Tata Motors receives large order from defence ministry",       "TMCV"),
        ("Tata Chemicals Q4 results: Net profit rises 12%",              "TATACHEM"),
        ("Reliance Industries Q4 results: Net profit rises 12%",         "RELIANCE"),
        ("Infosys raises revenue guidance after strong Q3",              "INFY"),
        ("Netweb Technologies Q4 EBITDA Surges 63.6%",                   "NETWEB"),
        ("AI risks keep me up at night, says Kotak Bank CEO",            "KOTAKBANK"),
        ("Kotak Mahindra Bank Shares In Focus After Q4 Results",         "KOTAKBANK"),
        # False positives — should now return None
        ("RBI maintains hawkish stance on inflation",                    None),
        ("Bitcoin Tops $80,000 for Three-Month High as Asian Stocks Rise", None),
        ("CEO Greg Abel moves to assure Berkshire shareholders",         None),
        ("Laxmi India Finance Limited Announces Resignation of AVP",     None),
        ("Bengaluru Rains: Seven Dead As Hospital Wall Collapses",       None),
        ("Gold demand in Jan-Mar 2026 rose 10%",                         None),
    ]

    print("\n── EntityShield Test ──")
    print(f"{'Result':<7} {'Got':<12} {'Expected':<12} Headline")
    print("-" * 100)

    correct = 0
    for headline, expected in test_cases:
        match = find_ticker(headline, instruments)
        actual = match["symbol"] if match else None
        ok = actual == expected
        if ok:
            correct += 1
        status = "✅" if ok else "❌"
        a = actual if actual else "—"
        e = expected if expected else "—"
        print(f"{status:<7} {a:<12} {e:<12} {headline[:70]}")

    print(f"\n{correct}/{len(test_cases)} correct matches")
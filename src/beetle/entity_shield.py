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

# Tickers ≤ this length are considered "short" — they require either
# an exact word-boundary match or an explicit alias/context word.
# Prevents short tickers like SPIC matching "SpiceJet", FACT matching
# "Factory", HAL matching "Halt", BEL matching "Bell", etc.
SHORT_TICKER_LEN = 4

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
    "HERO MOTO":     "HEROMOTOCO",
    "MAHINDRA":      "M&M",
    "MARUTI":        "MARUTI",
    "EICHER":        "EICHERMOT",
    "ASHOK LEYLAND": "ASHOKLEY",
    "ATHER ENERGY":  "ATHERENERG",
    # ── FMCG ─────────────────────────────────────────────────────
    "NESTLE":             "NESTLEIND",
    "NESTLÉ":             "NESTLEIND",
    "ASIAN PAINTS":       "ASIANPAINT",
    "HUL":                "HINDUNILVR",
    "AIRTEL":             "BHARTIARTL",
    "GODREJ INDUSTRIES":  "GODREJIND",
    "GODREJ AGROVET":     "GODREJAGRO",
    "GODREJ CONSUMER":    "GODREJCP",
    "GODREJ PROPERTIES":  "GODREJPROP",
    # ── Metal ────────────────────────────────────────────────────
    "JSW STEEL":  "JSWSTEEL",
    "HINDALCO":   "HINDALCO",
    "ULTRATECH":  "ULTRACEMCO",
    "VEDANTA":    "VEDL",
    "GRASIM":     "GRASIM",
    "COAL INDIA": "COALINDIA",
    "COALINDIA":  "COALINDIA",
    # ── Defence ──────────────────────────────────────────────────
    "MAZAGON DOCK":          "MAZDOCK",
    "COCHIN SHIPYARD":       "COCHINSHIP",
    "GARDEN REACH":          "GRSE",
    "MAZAGON":               "MAZDOCK",
    "ZEN TECH":              "ZENTEC",
    "DCX SYSTEMS":           "DCXINDIA",
    "HAL":                   "HAL",
    "HINDUSTAN AERONAUTICS": "HAL",
    "BEL":                   "BEL",
    # ── Fertilizers / Specific Chemicals ─────────────────────────
    "SOUTHERN PETROCHEMICAL":      "SPIC",
    "FERTILIZERS AND CHEMICALS":   "FACT",
    "FACT KOCHI":                  "FACT",
    # ── New Age ──────────────────────────────────────────────────
    "POLICYBAZAAR":  "POLICYBZR",
    "INDIAN HOTELS": "INDHOTEL",
    "DELHIVERY":     "DELHIVERY",
    "CASTROL":       "CASTROLIND",
    "ZOMATO":        "ZOMATO",
    "PAYTM":         "PAYTM",
    "NYKAA":         "FSN",
    "IRFC":          "IRFC",
    "NETWEB":        "NETWEB",
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
    # International figures/companies
    "BERKSHIRE", "BUFFETT", "GREG ABEL", "WARREN BUFFETT",
    "BERKSHIRE HATHAWAY",
    # ── NEW v9.1 — US/global market noise ────────────────────────
    "GAMESTOP", "EBAY", "WALL STREET", "S&P 500", "S&P500",
    "NASDAQ", "DOW JONES", "DOW",
    "COGNIZANT",                # US-listed (CTSH)
    "MIDEAST TENSIONS", "MIDEAST", "MIDDLE EAST",
    "CHINA FIREWORK", "CHINA EXPLOSION", "XI JINPING",
    # HR/internal corporate noise
    "RESIGNATION OF", "ASSISTANT VICE PRESIDENT", "HUMAN RESOURCE",
    # Macro/Policy
    "RBI", "SEBI", "FII", "DII", "INFLATION", "GDP",
    "ECONOMY", "ECONOMIC", "INTEREST RATE", "REPO RATE",
    "MONETARY POLICY", "FISCAL POLICY", "BUDGET",
    # Market sentiment
    "BULL MARKET", "BEAR MARKET", "STOCK MARKET",
    "GLOBAL CUES", "CRUDE OIL", "RUPEE", "DOLLAR",
    "VIX", "VOLATILITY INDEX", "FEAR INDEX",
    # Generic moves
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
    # ── NEW v9.1 — generic earnings noise ────────────────────────
    "INDIA EARNINGS", "MIXED Q4 RESULTS", "WHALESBOOK",
    "MIXED RESULTS",
]

# Tickers with generic-sounding names — require a specific context word.
# Prevents M&M matching headlines about Kotak Mahindra,
# BEL matching Berkshire's Greg Abel, SPIC matching SpiceJet, etc.
AMBIGUOUS_TICKERS = {
    # Existing
    "ASIANENE":   ["ENERGY", "OIL", "GAS", "DRILLING", "ASIAN ENERGY"],
    "M&M":        ["MAHINDRA &", "M&M", "MAHINDRA AUTO",
                   "MAHINDRA SUV", "TRACTORS", "MAHINDRA Q"],
    "BEL":        ["BHARAT ELECTRONICS", "DEFENCE",
                   "RADAR", "ELECTRONICS LTD"],
    "RAIN":       ["RAIN INDUSTRIES", "RAIN COMMODITIES"],
    "GOCLCORP":   ["GOCL", "EXPLOSIVES", "DETONATOR"],
    # ── NEW v9.1 — Day 2 false positives ─────────────────────────
    "SPIC":       ["SOUTHERN PETROCHEMICAL", "SPIC FERTILIZER",
                   "SPIC LTD", "SPIC INDIA"],
    "FACT":       ["FERTILIZERS AND CHEMICALS", "FACT KOCHI",
                   "TRAVANCORE", "FACT LTD"],
    "HAL":        ["HINDUSTAN AERONAUTICS", "HAL LTD",
                   "TEJAS", "FIGHTER JET", "DEFENCE",
                   "AEROSPACE", "LCA"],
    "ADANIENT":   ["ADANI ENTERPRISES", "GAUTAM ADANI",
                   "ADANI GROUP FLAGSHIP", "ADANIENT"],
    "GODREJIND":  ["GODREJ INDUSTRIES", "GODREJ AGROVET",
                   "GODREJ CHEMICAL"],   # NOT Godrej Properties (GODREJPROP)
    "MIDWESTLTD": ["MIDWEST GOLD", "MIDWEST LIMITED",
                   "MIDWEST INDIA", "MIDWESTLTD"],
    "ATHERENERG": ["ATHER ENERGY", "ATHER 450", "ATHER SCOOTER",
                   "ATHERENERG", "ATHER IPO"],
    "GATECHDVR":  ["GACM TECHNOLOGIES", "GACM", "GATECHDVR"],
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
    """
    context_words = AMBIGUOUS_TICKERS.get(symbol, [])
    if not context_words:
        return False
    return not any(word in headline_upper for word in context_words)


def _is_short_ticker_substring_only(symbol: str,
                                     headline_upper: str) -> bool:
    """
    Structural guard for short tickers (≤ SHORT_TICKER_LEN chars).

    Short tickers can accidentally match as substrings of unrelated words:
        SPIC ⊂ "SpiceJet"
        FACT ⊂ "Factory"
        HAL  ⊂ "Halt", "Halloween", "Khaleda"
        BEL  ⊂ "Belief", "Bellatrix"
        RAIN ⊂ "Rains", "Rainfall"

    A match for a short ticker is only valid if the ticker appears as a
    standalone WORD in the headline (regex \\bTICKER\\b). Otherwise, this
    function returns True → match should be rejected.

    Returns True  → reject this match (substring-only, unsafe).
    Returns False → match is safe (either ticker is long enough OR appears
                    as a standalone word in headline).
    """
    if len(symbol) > SHORT_TICKER_LEN:
        return False   # long enough — substring match is fine

    # Build word-boundary regex. Escape special chars (e.g. M&M).
    pattern = r'\b' + re.escape(symbol) + r'\b'
    if re.search(pattern, headline_upper):
        return False   # standalone word match — safe

    return True   # short ticker, only matches as substring → reject


def find_ticker(headline: str, instruments: dict,
                threshold: int = MATCH_THRESHOLD) -> dict | None:
    """
    Match headline to ticker through layered defences:
    Step 1: Special case for TATA MOTORS → TMCV.
    Step 2: Known aliases (longest first).
    Step 3: Generic-headline rejection.
    Step 4: Fuzzy match against instrument search anchors.
    Step 5: Short-ticker substring guard.
    Step 6: Ambiguous-ticker context check.
    """
    headline_upper = headline.upper()
    cleaned = _clean_headline(headline)

    # Special case: TATA MOTORS → TMCV
    if "TATA MOTORS" in headline_upper and "TMCV" in instruments:
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
                # Aliases also pass through ambiguity check
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

    # Step 2 — Reject generic headlines
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

    matched_symbol = best_match["symbol"]

    # Step 4 — Short-ticker substring guard (NEW v9.1)
    if _is_short_ticker_substring_only(matched_symbol, headline_upper):
        logger.debug(f"  Skipping short-ticker substring match: "
                    f"{matched_symbol} not a standalone word in headline")
        return None

    # Step 5 — Ambiguous-ticker context check
    if _is_ambiguous_match(matched_symbol, headline_upper):
        logger.debug(f"  Skipping ambiguous fuzzy match: {matched_symbol}")
        return None

    boost = _keyword_boost(headline)
    return {
        "symbol":     matched_symbol,
        "name":       best_match["name"],
        "confidence": round(min(1.0, (best_score / 100) + boost), 3),
        "raw_score":  best_score,
        "boosted":    boost > 0
    }


def filter_headlines(headlines: list, instruments: dict) -> list:
    """Run EntityShield on a list of headline dicts."""
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

    # Combined test set: yesterday's wins + today's false positives
    test_cases = [
        # ── Should match correctly (true positives) ──────────────
        ("Nestlé India shares jump 6% to hit 52-week high",                "NESTLEIND"),
        ("HDFC Bank Q3 beats estimates, NII up 15%",                       "HDFCBANK"),
        ("Tata Motors receives large order from defence ministry",         "TMCV"),
        ("Tata Chemicals Q4 results: Net profit rises 12%",                "TATACHEM"),
        ("Reliance Industries Q4 results: Net profit rises 12%",           "RELIANCE"),
        ("Infosys raises revenue guidance after strong Q3",                "INFY"),
        ("Netweb Technologies Q4 EBITDA Surges 63.6%",                     "NETWEB"),
        ("AI risks keep me up at night, says Kotak Bank CEO",              "KOTAKBANK"),
        ("Kotak Mahindra Bank Shares In Focus After Q4 Results",           "KOTAKBANK"),
        ("Ashok Leyland reports 9% increase in April sales",               "ASHOKLEY"),
        ("HFCL Secures Rs 84 Crore OFC Supply Order",                      "HFCL"),
        ("Hindustan Aeronautics wins Tejas fighter jet deal",              "HAL"),
        ("SPIC fertilizer plant resumes production after maintenance",     "SPIC"),

        # ── Day 1 false positives (already fixed) ────────────────
        ("RBI maintains hawkish stance on inflation",                      None),
        ("Bitcoin Tops $80,000 as Asian Stocks Rise",                      None),
        ("CEO Greg Abel moves to assure Berkshire shareholders",           None),
        ("Laxmi India Finance Announces Resignation of AVP",               None),
        ("Bengaluru Rains: Seven Dead As Hospital Wall Collapses",         None),
        ("Gold demand in Jan-Mar 2026 rose 10%",                           None),

        # ── Day 2 false positives (NEW — should now reject) ──────
        ("SpiceJet's shrinking fleet puts international ops under scrutiny", None),
        ("GameStop shares drop 6.5% after ambitious $55.5 billion bid",      None),
        ("India Earnings: Mixed Q4 Results Meet High Valuations - Whalesbook", None),
        ("Wall Street Highlights: S&P 500, Nasdaq Fall From Record High",    None),
        ("21 Killed in China Firework Factory Explosion",                    None),
        ("Godrej Properties targets ₹39,000 cr in sales for FY27",           None),  # GODREJPROP not GODREJIND
        ("Ambuja Cements resets expansion strategy as Karan Adani flags",    None),
        ("Cognizant trims shareholder payouts as AI dealmaking gathers pace", None),
    ]

    print("\n── EntityShield v9.1 Test ──")
    print(f"{'Result':<7} {'Got':<12} {'Expected':<12} Headline")
    print("-" * 110)

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
        print(f"{status:<7} {a:<12} {e:<12} {headline[:75]}")

    print(f"\n{correct}/{len(test_cases)} correct matches")
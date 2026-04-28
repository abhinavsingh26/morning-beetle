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

# Known aliases for major companies — checked before fuzzy match
KNOWN_ALIASES = {
    "HCLTECH": "HCLTECH",
    "HCL": "HCLTECH",
    "NESTLE": "NESTLEIND",
    "NESTLÉ": "NESTLEIND",
    "INFOSYS": "INFY",
    "TATAMOTORS": "TATAMOTORS",
    "TATA MOTORS": "TATAMOTORS",
    "TATA STEEL": "TATASTEEL",
    "TATA POWER": "TATAPOWER",
    "TATA ELXSI": "TATAELXSI",
    "TATA CONSULTANCY": "TCS",
    "WIPRO": "WIPRO",
    "HFCL": "HFCL",
    "HDFC BANK": "HDFCBANK",
    "HDFC LIFE": "HDFCLIFE",
    "ICICI BANK": "ICICIBANK",
    "AXIS BANK": "AXISBANK",
    "KOTAK": "KOTAKBANK",
    "BAJAJ FINANCE": "BAJFINANCE",
    "BAJAJ FINSERV": "BAJAJFINSV",
    "MARUTI": "MARUTI",
    "ASIAN PAINTS": "ASIANPAINT",
    "HINDUSTAN UNILEVER": "HINDUNILVR",
    "HUL": "HINDUNILVR",
    "BHARTI AIRTEL": "BHARTIARTL",
    "AIRTEL": "BHARTIARTL",
    "SBI": "SBIN",
    "STATE BANK": "SBIN",
    "ONGC": "ONGC",
    "NTPC": "NTPC",
    "POWER GRID": "POWERGRID",
    "ADANI PORTS": "ADANIPORTS",
    "ADANI ENTERPRISES": "ADANIENT",
    "ADANI POWER": "ADANIPOWER",
    "ADANI GREEN": "ADANIGREEN",
    "SUN PHARMA": "SUNPHARMA",
    "DR REDDY": "DRREDDY",
    "CIPLA": "CIPLA",
    "DIVIS": "DIVISLAB",
    "TECH MAHINDRA": "TECHM",
    "HCL TECH": "HCLTECH",
    "HCL TECHNOLOGIES": "HCLTECH",
    "PERSISTENT": "PERSISTENT",
    "VEDANTA": "VEDL",
    "JSW STEEL": "JSWSTEEL",
    "HINDALCO": "HINDALCO",
    "ULTRATECH": "ULTRACEMCO",
    "GRASIM": "GRASIM",
    "EICHER": "EICHERMOT",
    "HERO MOTO": "HEROMOTOCO",
    "MAHINDRA": "M&M",
    "INDUSIND": "INDUSINDBK",
    "MAZAGON": "MAZDOCK",
    "ZEN TECH": "ZENTEC",
    "DCX SYSTEMS": "DCXINDIA",
    "CASTROL": "CASTROLIND",
    "RELIANCE": "RELIANCE",
    "WIPRO": "WIPRO",
    "LTI": "LTIM",
    "LTIMINDTREE": "LTIM",
    "MPHASIS": "MPHASIS",
    "ZOMATO": "ZOMATO",
    "PAYTM": "PAYTM",
    "NYKAA": "FSN",
    "POLICYBAZAAR": "POLICYBZR",
    "DELHIVERY": "DELHIVERY",
    "INDIAN HOTELS": "INDHOTEL",
    "IRFC": "IRFC",
    "HAL": "HAL",
    "BEL": "BEL",
    "BHARAT DYNAMICS": "BDL",
    "BHARAT ELECTRONICS": "BEL",
    "MAZAGON DOCK": "MAZDOCK",
    "COCHIN SHIPYARD": "COCHINSHIP",
    "GARDEN REACH": "GRSE",
    "IDFC FIRST": "IDFCFIRSTB",
    "PNB HOUSING": "PNBHOUSING",
    "BANK OF MAHARASHTRA": "MAHABANK",
}

# Generic market terms — skip these, no specific ticker
GENERIC_TERMS = [
    # Market-wide terms
    "SENSEX", "NIFTY", "MARKET", "INDICES", "INDEX",
    "BROADER MARKET", "BAROMETERS", "BAROMETER",
    "ADVANCE DECLINE", "BREADTH", "MARKET BREADTH",
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
]


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


def find_ticker(headline: str, instruments: dict,
                threshold: int = MATCH_THRESHOLD) -> dict | None:
    """
    Match headline to ticker.
    Step 1: Check known aliases (fast, accurate).
    Step 2: Filter out generic market headlines.
    Step 3: Fuzzy match against instrument search anchors.
    Returns match dict or None.
    """
    headline_upper = headline.upper()
    cleaned = _clean_headline(headline)

    # Step 1 — Known aliases first
    for alias, symbol in sorted(KNOWN_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias.upper() in headline_upper:
            if symbol in instruments:
                boost = _keyword_boost(headline)
                return {
                    "symbol":     symbol,
                    "name":       instruments[symbol]["name"],
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

    for symbol, data in instruments.items():
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
    Drops headlines with no match or low confidence.
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

    test_headlines = [
        "Nestlé India shares jump 6% to hit 52-week high on strong Q4 results",
        "HDFC Bank Q3 beats estimates, NII up 15%",
        "Tata Motors receives large order from defence ministry",
        "Reliance Industries Q4 results: Net profit rises 12%",
        "Infosys raises revenue guidance after strong Q3",
        "RBI maintains hawkish stance on inflation",
        "Markets rally on global cues, Sensex up 500 points",
        "HFCL wins over Rs 10000 crore global order",
        "Persistent Systems Q4 results: IT firm posts 33% rise in PAT",
        "Vedanta share price slips ahead of dividend announcement",
        "IDFC First Bank shares jump 20% in 2025",
        "Mazagon Dock declares interim dividend",
    ]

    print("\n── EntityShield Test ──")
    print(f"{'Ticker':<15} {'Conf':<8} {'Boost':<8} {'Headline'}")
    print("-" * 90)

    matched = 0
    for headline in test_headlines:
        match = find_ticker(headline, instruments)
        if match:
            matched += 1
            boost_flag = "✅" if match["boosted"] else ""
            print(
                f"{match['symbol']:<15} {match['confidence']:<8} "
                f"{boost_flag:<8} {headline[:60]}"
            )
        else:
            print(f"{'NO MATCH':<15} {'—':<8} {'':8} {headline[:60]}")

    print(f"\n✅ Matched {matched}/{len(test_headlines)} headlines")
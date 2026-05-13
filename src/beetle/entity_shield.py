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

DEAD_ZONE_MIN = -0.1
DEAD_ZONE_MAX = +0.1

MATCH_THRESHOLD = 85

# Tickers ≤ this length must appear as a standalone word (regex \bTICKER\b)
SHORT_TICKER_LEN = 4

# Known aliases — longer aliases checked first
KNOWN_ALIASES = {
    # ── DISAMBIGUATION (specific phrases checked before generic) ──
    "KOTAK MAHINDRA BANK":   "KOTAKBANK",
    "KOTAK MAHINDRA":        "KOTAKBANK",
    "ASIAN ENERGY SERVICES": "ASIANENE",
    "ASIAN ENERGY":          "ASIANENE",
    # ── Bajaj group disambiguation (v9.2) ────────────────────────
    "BAJAJ AUTO LTD":        "BAJAJ-AUTO",
    "BAJAJ AUTO":            "BAJAJ-AUTO",
    "BAJAJ FINANCE":         "BAJFINANCE",
    "BAJAJ FINSERV":         "BAJAJFINSV",
    "BAJAJ HOLDINGS":        "BAJAJHLDNG",
    "BAJAJ HIND SUGAR":      "BAJAJHIND",
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
    "HDFC BANK":           "HDFCBANK",
    "HDFC LIFE":           "HDFCLIFE",
    "ICICI BANK":          "ICICIBANK",
    "AXIS BANK":           "AXISBANK",
    "IDFC FIRST":          "IDFCFIRSTB",
    "PNB HOUSING":         "PNBHOUSING",
    "INDUSIND":            "INDUSINDBK",
    "STATE BANK":          "SBIN",
    "STATE BANK OF INDIA": "SBIN",
    "KOTAK":               "KOTAKBANK",
    "SBI":                 "SBIN",
    # ── IT ───────────────────────────────────────────────────────
    "HCL TECHNOLOGIES":    "HCLTECH",
    "TECH MAHINDRA":       "TECHM",
    "LTIMINDTREE":         "LTIM",
    "HCL TECH":            "HCLTECH",
    "HCLTECH":             "HCLTECH",
    "PERSISTENT":          "PERSISTENT",
    "MPHASIS":             "MPHASIS",
    "INFOSYS":             "INFY",
    "WIPRO":               "WIPRO",
    "HFCL":                "HFCL",
    "HCL":                 "HCLTECH",
    "LTI":                 "LTIM",
    "SASKEN":              "SASKEN",
    "SASKEN TECHNOLOGIES": "SASKEN",
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
    "ADANI GREEN ENERGY": "ADANIGREEN",
    "POWER GRID":        "POWERGRID",
    "RELIANCE":          "RELIANCE",
    "ONGC":               "ONGC",
    "NTPC":               "NTPC",
    # ── Auto ─────────────────────────────────────────────────────
    "HERO MOTO":         "HEROMOTOCO",
    "HERO MOTOCORP":     "HEROMOTOCO",
    "MAHINDRA & MAHINDRA": "M&M",     # v9.5 — proper M&M disambiguation
    "MAHINDRA":          "M&M",
    "MARUTI":            "MARUTI",
    "EICHER":            "EICHERMOT",
    "ASHOK LEYLAND":     "ASHOKLEY",
    "ATHER ENERGY":      "ATHERENERG",
    # ── FMCG ─────────────────────────────────────────────────────
    "NESTLE":              "NESTLEIND",
    "NESTLÉ":              "NESTLEIND",
    "ASIAN PAINTS":        "ASIANPAINT",
    "HUL":                 "HINDUNILVR",
    "AIRTEL":              "BHARTIARTL",
    "GODREJ INDUSTRIES":   "GODREJIND",
    "GODREJ AGROVET":      "GODREJAGRO",
    "GODREJ CONSUMER":     "GODREJCP",
    "GODREJ PROPERTIES":   "GODREJPROP",
    "PIDILITE":            "PIDILITIND",
    "PIDILITE INDUSTRIES": "PIDILITIND",
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
    # ── Industrial / Smaller specific names (v9.2) ───────────────
    "DEEP INDUSTRIES":       "DEEPINDS",
    "DEEP INDS":             "DEEPINDS",
    "DISA INDIA":            "DISAQ",
    "BF INVESTMENT":         "BFINVEST",
    "BHARAT FORGE":          "BHARATFORG",
    # ── Ramco group disambiguation (v9.3) ────────────────────────
    "RAMCO INDUSTRIES":      "RAMCOIND",
    "RAMCO CEMENTS":         "RAMCOCEM",
    "RAMCO CEMENT":          "RAMCOCEM",
    "RAMCO SYSTEMS":         "RAMCOSYS",
    # ── NEW v9.5 — Supreme Industries disambiguation ─────────────
    "SUPREME INDUSTRIES":    "SUPREMEIND",
    "SUPREME PETROCHEM":     "SUPPETRO",
    # ── NEW v9.5 — Birla group disambiguation ────────────────────
    "BIRLA CABLE":           "BIRLACABLE",
    "ADITYA BIRLA CAPITAL":  "ABCAPITAL",
    "ADITYA BIRLA FASHION":  "ABFRL",
    "GRASIM":                "GRASIM",
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

GENERIC_TERMS = [
    # Market-wide
    "SENSEX", "NIFTY", "MARKET", "INDICES", "INDEX",
    "BROADER MARKET", "BAROMETERS", "BAROMETER",
    "ADVANCE DECLINE", "BREADTH", "MARKET BREADTH",
    # Crypto/global
    "BITCOIN", "ETHEREUM", "CRYPTO", "CRYPTOCURRENCY",
    "ASIAN STOCKS RISE", "ASIAN STOCKS",
    # International figures/companies
    "BERKSHIRE", "BUFFETT", "GREG ABEL", "WARREN BUFFETT",
    "BERKSHIRE HATHAWAY",
    # US/global market noise (v9.1)
    "GAMESTOP", "EBAY", "WALL STREET", "S&P 500", "S&P500",
    "NASDAQ", "DOW JONES", "DOW",
    "COGNIZANT",
    "MIDEAST TENSIONS", "MIDEAST", "MIDDLE EAST",
    "CHINA FIREWORK", "CHINA EXPLOSION", "XI JINPING",
    # ── v9.2 — foreign tech / PE / generic India macro ───────────
    "DEEPMIND", "GOOGLE DEEPMIND", "ALPHABET",
    "PENTAGON", "PENTAGON AI",
    "PE FIRM", "PRIVATE EQUITY", "VC FIRM", "VENTURE CAPITAL",
    "PE FUND", "VENTURE FUND",
    "INDIAN COMPANIES ANNOUNCE", "CREATE 1500 JOBS",
    "INDIAN COMPANIES INVEST",
    "RECOGNIZE",                # the US PE firm name
    "1.7 BILLION FUND",
    "NICHE IT SERVICE",
    # ── v9.3 — Day 6 false positive filters ──────────────────────
    # Saudi Aramco / oil giant
    "ARAMCO", "SAUDI ARAMCO", "SAUDI OIL",
    "OIL GIANT", "OIL EXPORTS", "STRAIT OF HORMUZ",
    "SAUDI ARABIA", "GULF OIL",
    # Yes Bank / generic India lender noise
    "YES BANK LTD", "INDIA-FOCUSED LENDER", "AD HOC NEWS",
    "INE528G01035",
    "DIGITAL PUSH",
    # HR/internal noise
    "RESIGNATION OF", "ASSISTANT VICE PRESIDENT", "HUMAN RESOURCE",
    "UNIONISE", "UNIONISING", "WORKERS UNION",
    # Macro/Policy
    "RBI", "SEBI", "FII", "DII", "INFLATION", "GDP",
    "ECONOMY", "ECONOMIC", "INTEREST RATE", "REPO RATE",
    "MONETARY POLICY", "FISCAL POLICY", "BUDGET",
    # Sentiment
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
    # Generic earnings noise (v9.1)
    "INDIA EARNINGS", "MIXED Q4 RESULTS", "WHALESBOOK",
    "MIXED RESULTS",
    # Day 7 false positives (v9.4)
    "CATTLE FUTURES", "CME CATTLE", "BEEF IMPORT", "US BEEF",
    "LIVESTOCK", "PORK FUTURES", "COMMODITY FUTURES",
    "BLOCK DEAL", "GROWW", "EARLY INVESTORS REAP",
    "STARTUP IPO", "498 MILLION",
    "BUZZING STOCKS", "BUZZING STOCK", "STOCKS IN FOCUS", "STOCKS TO WATCH",
    "TOP BUZZING STOCKS",
    # ── NEW v9.5 — Day 8 false positives ─────────────────────────
    # "AD HOC NEWS" stock-mention noise (M&M, ADANIGREEN today)
    "TECHNICAL SIGNALS", "AMID TECHNICAL", "DROPS 2.54",
    "INE101A01026", "INE364U01010",  # ISIN codes in headlines
    # Supreme Court case fooled SUPREMEIND
    "SUPREME COURT", "MOVES SUPREME COURT", "RANI KAPUR",
    "FAMILY TRUST", "TRUST-LINKED BOARD",
    # Aditya Birla Health (not Birla Cable)
    "ADITYA BIRLA HEALTH", "BIRLA HEALTH",
    "WELLNESS INCENTIVES", "CLAIMS RATIOS",
    # Syrma SGS named someone else
    "SYRMA SGS", "JASBIR SINGH GUJRAL",
    # HG Infra mention in Sasken multi-stock headline
    "HG INFRA",
    # Generic earnings calendar noise
    "100+ FIRMS TO DECLARE", "FIRMS TO DECLARE EARNINGS",
    "DIXON TECH", "MOBIKWIK",  # mentioned alongside other Q4 calendar
]

# Tickers requiring specific context word
AMBIGUOUS_TICKERS = {
    # Existing
    "ASIANENE":   ["ENERGY", "OIL", "GAS", "DRILLING", "ASIAN ENERGY"],
    "M&M":        ["MAHINDRA &", "M&M", "MAHINDRA AUTO",
                   "MAHINDRA SUV", "TRACTORS", "MAHINDRA Q",
                   "MAHINDRA RESULTS", "MAHINDRA FINANCE",
                   "M&M FINANCIAL"],
    "BEL":        ["BHARAT ELECTRONICS", "DEFENCE",
                   "RADAR", "ELECTRONICS LTD"],
    "RAIN":       ["RAIN INDUSTRIES", "RAIN COMMODITIES"],
    "GOCLCORP":   ["GOCL", "EXPLOSIVES", "DETONATOR"],
    # v9.1
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
                   "GODREJ CHEMICAL"],
    "MIDWESTLTD": ["MIDWEST GOLD", "MIDWEST LIMITED",
                   "MIDWEST INDIA", "MIDWESTLTD"],
    "ATHERENERG": ["ATHER ENERGY", "ATHER 450", "ATHER SCOOTER",
                   "ATHERENERG", "ATHER IPO"],
    "GATECHDVR":  ["GACM TECHNOLOGIES", "GACM", "GATECHDVR"],
    # ── v9.2 — Day 3 false positives ─────────────────────────────
    "DEEPINDS":   ["DEEP INDUSTRIES", "DEEP INDS",
                   "DEEPINDS", "OILFIELD SERVICES"],
    "DISAQ":      ["DISA INDIA", "DISA TECHNOLOGIES",
                   "DISAQ", "DISA Q"],
    "BFINVEST":   ["BF INVESTMENT", "BHARAT FORGE",
                   "BFINVEST", "KALYANI"],
    "BAJFINANCE": ["BAJAJ FINANCE", "BAJFINANCE",
                   "BAJAJ FINSERV", "CONSUMER LOAN",
                   "EMI", "NBFC"],
    # ── v9.3 — Day 6 false positives ─────────────────────────────
    "RAMCOIND":   ["RAMCO INDUSTRIES", "RAMCOIND",
                   "ASBESTOS", "FIBRE CEMENT", "CEMENT SHEET",
                   "RAMCO LTD", "RAMCO GROUP"],
    "DUGLOBAL-SM": ["DUDIGITAL", "DU DIGITAL", "DU GLOBAL",
                    "DUGLOBAL", "DUDIGITAL GLOBAL"],
    "DUDIGITAL":   ["DUDIGITAL", "DU DIGITAL", "DU GLOBAL",
                    "DUDIGITAL GLOBAL"],
    # ── NEW v9.5 — Day 8 false positives ─────────────────────────
    # SUPREMEIND fooled by "Supreme Court" headline
    "SUPREMEIND":  ["SUPREME INDUSTRIES", "SUPREMEIND",
                    "PVC PIPE", "PVC PIPES", "POLYMER",
                    "PLASTIC PIPE", "BUILDING PRODUCTS",
                    "INDUSTRIAL PRODUCTS"],
    # BIRLACABLE fooled by "Aditya Birla Health" headline
    "BIRLACABLE":  ["BIRLA CABLE", "BIRLACABLE",
                    "OPTICAL FIBRE", "TELECOM CABLE",
                    "FIBRE OPTIC", "CABLE LTD",
                    "BIRLA ERICSSON"],
    # ADANIGREEN fooled by "AD HOC NEWS" pattern with generic stock dip
    "ADANIGREEN":  ["ADANI GREEN", "ADANIGREEN",
                    "ADANI GREEN ENERGY", "RENEWABLE",
                    "SOLAR", "WIND ENERGY", "GREEN HYDROGEN"],
    # SBIN fooled by "Syrma SGS" headline (name-fuzzy noise)
    # SBIN requires actual SBI / State Bank context
    "SBIN":        ["SBI", "STATE BANK", "STATE BANK OF INDIA",
                    "SBIN", "SBI Q", "SBI BANK",
                    "PSU BANK SBI", "SBI CARDS",
                    "SBI RESULTS", "SBI MUTUAL"],
}


def _keyword_boost(headline: str) -> float:
    headline_upper = headline.upper()
    for kw in BOOST_KEYWORDS:
        if kw in headline_upper:
            return 0.2
    return 0.0


def _clean_headline(headline: str) -> str:
    headline = re.sub(r'[^\w\s]', ' ', headline)
    return headline.upper().strip()


def _is_generic(headline: str) -> bool:
    headline_upper = headline.upper()
    for term in GENERIC_TERMS:
        if term in headline_upper:
            return True
    return False


def _is_ambiguous_match(symbol: str, headline_upper: str) -> bool:
    context_words = AMBIGUOUS_TICKERS.get(symbol, [])
    if not context_words:
        return False
    return not any(word in headline_upper for word in context_words)


def _is_short_ticker_substring_only(symbol: str,
                                     headline_upper: str) -> bool:
    """Short tickers (≤ 4 chars) must appear as standalone words."""
    if len(symbol) > SHORT_TICKER_LEN:
        return False
    pattern = r'\b' + re.escape(symbol) + r'\b'
    if re.search(pattern, headline_upper):
        return False
    return True


def find_ticker(headline: str, instruments: dict,
                threshold: int = MATCH_THRESHOLD) -> dict | None:
    """
    Match headline to ticker through layered defences:
    Step 0: Generic-headline rejection (v9.6 — moved earlier).
    Step 1: TATA MOTORS → TMCV special case.
    Step 2: Known aliases (longest first).
    Step 3: Fuzzy match.
    Step 4: Short-ticker substring guard.
    Step 5: Ambiguous-ticker context check.
    """
    headline_upper = headline.upper()
    cleaned = _clean_headline(headline)

    # ── Step 0 — Generic rejection (v9.6 — moved BEFORE alias check) ──
    # Prevents multi-stock noise headlines from matching real tickers
    # via the alias path. Example: "Q4 Results Today: Dixon Tech, Tata
    # Power, MobiKwik, Dr Reddy's Among 100+ Firms To Declare Earnings"
    # was matching TATAPOWER via the "TATA POWER" alias before generic
    # terms could intercept it.
    if _is_generic(headline):
        return None

    # Special: TATA MOTORS → TMCV
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

    # Step 2 — Fuzzy match
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

    # Step 3 — Short-ticker substring guard
    if _is_short_ticker_substring_only(matched_symbol, headline_upper):
        logger.debug(f"  Skipping short-ticker substring match: {matched_symbol}")
        return None

    # Step 4 — Ambiguous-ticker context check
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

    # Combined test set: Days 1+2+3+6+7+8
    test_cases = [
        # ── True positives (should match) ────────────────────────
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
        ("Hero MotoCorp Q4: Net profit jumps to Rs 1474 crore",            "HEROMOTOCO"),
        ("Tata Power's Bhutan hydro project gets $515 mn",                 "TATAPOWER"),
        ("Vedanta Group posts record FY26 earnings",                       "VEDL"),
        ("Bajaj Auto total sales up 40% at 5,13,792 units",                "BAJAJ-AUTO"),
        ("Bajaj Finance NBFC posts record consumer loan growth",           "BAJFINANCE"),
        ("Deep Industries wins oilfield services contract",                "DEEPINDS"),

        # ── Day 1 false positives (already fixed) ────────────────
        ("RBI maintains hawkish stance on inflation",                      None),
        ("Bitcoin Tops $80,000 as Asian Stocks Rise",                      None),
        ("CEO Greg Abel moves to assure Berkshire shareholders",           None),
        ("Bengaluru Rains: Seven Dead As Hospital Wall Collapses",         None),

        # ── Day 2 false positives ────────────────────────────────
        ("SpiceJet's shrinking fleet puts international ops under scrutiny", None),
        ("GameStop shares drop 6.5% after $55.5 billion bid",                None),
        ("India Earnings: Mixed Q4 Results - Whalesbook",                    None),
        ("Wall Street Highlights: S&P 500, Nasdaq Fall From Record High",    None),
        ("21 Killed in China Firework Factory Explosion",                    None),
        ("Cognizant trims shareholder payouts as AI dealmaking gathers pace", None),

        # ── Day 3 false positives ────────────────────────────────
        ("Why Google DeepMind workers in UK are trying to unionise over Pentagon AI", None),
        ("US PE firm Recognize, armed with $1.7 billion fund, scouts for niche IT", None),
        ("Indian Companies Announce $1.1 Billion Investment In US, Create 1500 Jobs", None),

        # ── Day 6 false positives ────────────────────────────────
        ("Saudi Oil Giant Aramco Sees 25% Jump In Q1 Profit After Shifting Exports From Strait of Hormuz", None),
        ("Yes Bank Ltd stock (INE528G01035): India-focused lender eyes growth amid reforms and digital push - AD HOC NEWS", None),

        # ── Day 6 true positives (must still match) ──────────────
        ("Sasken Technologies Q4 & FY26 Results: Revenue Doubles, PAT Rises", "SASKEN"),
        ("Pidilite walks pricing tightrope as raw material costs soar",      "PIDILITIND"),

        # ── Day 7 false positives ────────────────────────────────
        ("CME cattle futures pare losses after falling on US beef import plan", None),
        ("Early investors in Groww set to reap up to $498 million in block deal", None),
        ("NIFTY50 at 23,949, SENSEX down 838 pts in afternoon session; SBI, Swiggy, Canara Bank, Titan Company among buzzing stocks - Upstox", None),
        # Day 7 true positive must still pass
        ("Canara Bank: Motilal Oswal Trims Target Price After Q4 Results, Cites Tepid NIM Guidance", "CANBK"),

        # ── NEW v9.5 — Day 8 false positives ─────────────────────
        ("Mahindra & Mahindra Ltd stock (INE101A01026): Drops 2.54% amid technical signals - AD HOC NEWS", None),
        ("Adani Green Energy Ltd stock (INE364U01010): Share price dips 1.10% to ?1,350 - AD HOC NEWS", None),
        ("Syrma SGS Technology expects 30% revenue growth in tricky FY27, MD Jasbir Singh Gujral says", None),
        ("Rani Kapur moves Supreme Court over family trust-linked board meeting", None),
        ("Aditya Birla Health bets on wellness incentives to improve claims ratios", None),
        ("Sasken Technologies Q4 FY26 Results | HG Infra Secures Large Infrastructure Contract | Top Buzzing Stocks Today - Equitymaster", None),
        ("Q4FY26 Results Today: Dixon Tech, Tata Power, One MobiKwik, Dr Reddy's Among 100+ Firms To Declare Earnings - NDTV Profit", None),

        # ── NEW v9.5 — Day 8 true positives (must still match) ───
        ("Indian Hotels Q4 Results: Profit, Revenue Rise Over 14%; Dividend Declared - NDTV Profit", "INDHOTEL"),
        ("Bharti Airtel Q4 preview: strong user additions but flat Arpu may temper growth", "BHARTIARTL"),
    ]

    print("\n── EntityShield v9.5 Test ──")
    print(f"{'Result':<7} {'Got':<14} {'Expected':<14} Headline")
    print("-" * 115)

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
        print(f"{status:<7} {a:<14} {e:<14} {headline[:75]}")

    print(f"\n{correct}/{len(test_cases)} correct matches")

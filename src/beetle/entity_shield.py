"""
EntityShield v9.8 — Headline → Ticker resolution with layered defences.

Version history (incremental, each builds on previous):
  v9.0 — Base. Fuzzy + alias + dead zone + boost.
  v9.1 — KNOWN_ALIASES longest-first; AMBIGUOUS_TICKERS pattern introduced.
  v9.2 — Day 3 false positives: DEEPINDS, DISAQ, BFINVEST, Bajaj group.
  v9.3 — Day 6 false positives: Ramco group, Aramco, Yes Bank noise.
  v9.4 — Day 7 false positives: cattle futures, block deals, buzzing stocks.
  v9.5 — Day 8 false positives: SUPREMEIND, BIRLACABLE, ADANIGREEN, SBIN
         + SUPREME COURT, ADITYA BIRLA HEALTH, AD HOC NEWS GENERIC_TERMS.
  v9.6 — Structural: _is_generic() moved to Step 0 (before alias check).
         Multi-stock noise headlines now rejected before any alias match.
  v9.7 — Day 9 fix:
         + PUBLISHER_TRAILS stripping (NEW — primary defense)
         + NDTV added to AMBIGUOUS_TICKERS (NEW — backup defense)
         Prevents "X Q4 Results... - NDTV Profit" from matching NDTV.
  v9.8 — Day 14 + Jun 1 fixes:
         + Broker-as-commentator guard (PI Industries / Motilal Oswal)
         + Earnings-preview list detector (multi-company calendar entries)
         + Chennai-Horror-style HR/event news GENERIC_TERMS additions
         + Strengthened substring guard examples (NIIT vs IIT)
         + ALEMBIC PHARMA disambiguation in KNOWN_ALIASES
         Prevents PAGEIND ← "Chennai Horror" type matches,
         MOTILALOFS ← "PI Industries... Motilal Oswal Bullish" type,
         NIITLTD ← "IIT Bombay" substring matches.

Resolution pipeline (find_ticker):
  Step 0a: Strip publisher byline trail              ← v9.7
  Step 0b: Reject generic-noise headlines            ← v9.6 ordering
  Step 0c: Reject earnings-preview multi-list        ← v9.8 NEW
  Step 1:  TATA MOTORS → TMCV special case
  Step 2:  Known aliases (longest-first)
  Step 3:  Fuzzy match
  Step 4:  Short-ticker substring guard
  Step 5:  Ambiguous-ticker context check
  Step 6:  Broker-as-commentator suppression         ← v9.8 NEW
"""
import re
import logging
from thefuzz import fuzz
from src.beetle.instrument_master import load_instruments

logger = logging.getLogger(__name__)

# Boost confidence when these earnings/event keywords appear
BOOST_KEYWORDS = [
    "DIVIDEND", "BONUS", "CONTRACT", "ACQUISITION", "ORDER WIN",
    "RESULTS BEAT", "QUARTERLY RESULTS", "Q1", "Q2", "Q3", "Q4",
    "IPO", "MERGER", "BUYBACK", "RIGHTS ISSUE", "DEMERGER",
    "PAT", "REVENUE", "NII", "EBITDA"
]

DEAD_ZONE_MIN = -0.1
DEAD_ZONE_MAX = +0.1

MATCH_THRESHOLD = 85

# Tickers ≤ this length must appear as standalone words (regex \bTICKER\b)
SHORT_TICKER_LEN = 4

# ── v9.7 — Publisher trails to strip BEFORE matching ────────────
# Day 9 NDTV bug: "Oil India Q4 Results - NDTV Profit" matched NDTV
# (a 4-char ticker that passed the substring guard). We now strip
# trailing " - PublisherName" / " | PublisherName" before any
# matching happens, so publisher names never influence resolution.
PUBLISHER_TRAILS = [
    # Indian financial publishers / trading platforms
    "HDFC SKY",                          # ← v9.8.1 add
    "NDTV PROFIT", "NDTV BUSINESS", "NDTV",
    "MONEYCONTROL", "MONEY CONTROL",
    # Indian financial publishers
    "NDTV PROFIT", "NDTV BUSINESS", "NDTV",
    "MONEYCONTROL", "MONEY CONTROL",
    "BUSINESS STANDARD", "BUSINESSLINE", "BUSINESS LINE",
    "LIVEMINT", "LIVE MINT", "MINT",
    "ECONOMIC TIMES", "ET NOW", "ET MARKETS",
    "FINANCIAL EXPRESS", "FE BUREAU", "FE MARKETS",
    "BLOOMBERG QUINT", "BQ PRIME", "BQ",
    "CNBC TV18", "CNBC-TV18", "CNBC",
    "ZEE BUSINESS", "ZEEBIZ",
    "OUTLOOK BUSINESS", "OUTLOOK MONEY",
    "FORBES INDIA", "FORTUNE INDIA",
    "THE HINDU BUSINESSLINE", "THE HINDU",
    "INDIAN EXPRESS",
    "FIRSTPOST",
    "EQUITYMASTER",
    "GOODRETURNS",
    "UPSTOX",
    "SCANX.TRADE", "SCANX",
    "WHALESBOOK",
    "AD HOC NEWS",
    # International publishers
    "REUTERS", "BLOOMBERG", "REUTERS POLL",
    "FINANCIAL TIMES", "FT",
    "WALL STREET JOURNAL", "WSJ",
    "MARKETWATCH",
    "INVESTING.COM", "INVESTING COM",
    "YAHOO FINANCE",
    "SEEKING ALPHA",
    "BARRON'S", "BARRONS",
]
# Sort longest-first so "NDTV PROFIT" strips before "NDTV"
PUBLISHER_TRAILS = sorted(PUBLISHER_TRAILS, key=len, reverse=True)


# ── KNOWN_ALIASES — checked longest-first in find_ticker ────────
KNOWN_ALIASES = {
    # Disambiguation (specific phrases before generic terms)
    "KOTAK MAHINDRA BANK":   "KOTAKBANK",
    "KOTAK MAHINDRA":        "KOTAKBANK",
    "ASIAN ENERGY SERVICES": "ASIANENE",
    "ASIAN ENERGY":          "ASIANENE",
    # Bajaj group disambiguation (v9.2)
    "BAJAJ AUTO LTD":        "BAJAJ-AUTO",
    "BAJAJ AUTO":            "BAJAJ-AUTO",
    "BAJAJ FINANCE":         "BAJFINANCE",
    "BAJAJ FINSERV":         "BAJAJFINSV",
    "BAJAJ HOLDINGS":        "BAJAJHLDNG",
    "BAJAJ HIND SUGAR":      "BAJAJHIND",
    # TATA group (specific before general)
    "TATA CONSULTANCY":  "TCS",
    "TATA CHEMICALS":    "TATACHEM",
    "TATA CONSUMER":     "TATACONSUM",
    "TATA MOTORS":       "TMCV",
    "TATA STEEL":        "TATASTEEL",
    "TATA POWER":        "TATAPOWER",
    "TATA ELXSI":        "TATAELXSI",
    "TATA COMM":         "TATACOMM",
    "TATAMOTORS":        "TMCV",
    # Banks
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
    # IT
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
    # Pharma
    "SUN PHARMA":  "SUNPHARMA",
    "DR REDDY":    "DRREDDY",
    "CIPLA":       "CIPLA",
    "DIVIS":       "DIVISLAB",
    # Energy/PSU
    "ADANI ENTERPRISES":  "ADANIENT",
    "ADANI PORTS":        "ADANIPORTS",
    "ADANI POWER":        "ADANIPOWER",
    "ADANI GREEN":        "ADANIGREEN",
    "ADANI GREEN ENERGY": "ADANIGREEN",
    "POWER GRID":         "POWERGRID",
    "RELIANCE":           "RELIANCE",
    "ONGC":               "ONGC",
    "NTPC":               "NTPC",
    # Auto
    "HERO MOTO":           "HEROMOTOCO",
    "HERO MOTOCORP":       "HEROMOTOCO",
    "MAHINDRA & MAHINDRA": "M&M",
    "MAHINDRA":            "M&M",
    "MARUTI":              "MARUTI",
    "EICHER":              "EICHERMOT",
    "ASHOK LEYLAND":       "ASHOKLEY",
    "ATHER ENERGY":        "ATHERENERG",
    # FMCG
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
    # Metal
    "JSW STEEL":  "JSWSTEEL",
    "HINDALCO":   "HINDALCO",
    "ULTRATECH":  "ULTRACEMCO",
    "VEDANTA":    "VEDL",
    "GRASIM":     "GRASIM",
    "COAL INDIA": "COALINDIA",
    "COALINDIA":  "COALINDIA",
    # Defence
    "MAZAGON DOCK":          "MAZDOCK",
    "COCHIN SHIPYARD":       "COCHINSHIP",
    "GARDEN REACH":          "GRSE",
    "MAZAGON":               "MAZDOCK",
    "ZEN TECH":              "ZENTEC",
    "DCX SYSTEMS":           "DCXINDIA",
    "HAL":                   "HAL",
    "HINDUSTAN AERONAUTICS": "HAL",
    "BEL":                   "BEL",
    # Fertilizers / Specific Chemicals
    "SOUTHERN PETROCHEMICAL":    "SPIC",
    "FERTILIZERS AND CHEMICALS": "FACT",
    "FACT KOCHI":                "FACT",
    # Industrial / Smaller (v9.2)
    "DEEP INDUSTRIES":  "DEEPINDS",
    "DEEP INDS":        "DEEPINDS",
    "DISA INDIA":       "DISAQ",
    "BF INVESTMENT":    "BFINVEST",
    "BHARAT FORGE":     "BHARATFORG",
    # Ramco group disambiguation (v9.3)
    "RAMCO INDUSTRIES": "RAMCOIND",
    "RAMCO CEMENTS":    "RAMCOCEM",
    "RAMCO CEMENT":     "RAMCOCEM",
    "RAMCO SYSTEMS":    "RAMCOSYS",
    # Supreme group disambiguation (v9.5)
    "SUPREME INDUSTRIES": "SUPREMEIND",
    "SUPREME PETROCHEM":  "SUPPETRO",
    # Birla group disambiguation (v9.5)
    "BIRLA CABLE":          "BIRLACABLE",
    "ADITYA BIRLA CAPITAL": "ABCAPITAL",
    "ADITYA BIRLA FASHION": "ABFRL",
    # New Age
    "POLICYBAZAAR":  "POLICYBZR",
    "INDIAN HOTELS": "INDHOTEL",
    "DELHIVERY":     "DELHIVERY",
    "CASTROL":       "CASTROLIND",
    "ZOMATO":        "ZOMATO",
    "PAYTM":         "PAYTM",
    "NYKAA":         "FSN",
    "IRFC":          "IRFC",
    "NETWEB":        "NETWEB",
    # v9.8 — ALEMBIC disambiguation (Day 14)
    # "Alembic Pharma" headline was mis-routing to ALEMBICLTD (holding co);
    # the actual subject is Alembic Pharmaceuticals (APLLTD).
    # NOTE: verify APLLTD is the correct NSE symbol on your end before trusting.
    "ALEMBIC PHARMA":          "APLLTD",
    "ALEMBIC PHARMACEUTICALS": "APLLTD",
    "ALEMBIC LTD":             "ALEMBICLTD",
}


# ── GENERIC_TERMS — if headline contains any of these, reject ───
GENERIC_TERMS = [
    "SEVERANCE PACKAGE", "META LAYOFF", "IIT BOMBAY", "IIT DELHI",
    "IIT MADRAS", "PLACEMENTS", "EMPLOYEES LET GO",
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
    # Foreign tech / PE / generic India macro (v9.2)
    "DEEPMIND", "GOOGLE DEEPMIND", "ALPHABET",
    "PENTAGON", "PENTAGON AI",
    "PE FIRM", "PRIVATE EQUITY", "VC FIRM", "VENTURE CAPITAL",
    "PE FUND", "VENTURE FUND",
    "INDIAN COMPANIES ANNOUNCE", "CREATE 1500 JOBS",
    "INDIAN COMPANIES INVEST",
    "RECOGNIZE",
    "1.7 BILLION FUND",
    "NICHE IT SERVICE",
    # Day 6 false positives (v9.3)
    "ARAMCO", "SAUDI ARAMCO", "SAUDI OIL",
    "OIL GIANT", "OIL EXPORTS", "STRAIT OF HORMUZ",
    "SAUDI ARABIA", "GULF OIL",
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
    "BUZZING STOCKS", "BUZZING STOCK",
    "STOCKS IN FOCUS", "STOCKS TO WATCH",
    "TOP BUZZING STOCKS",
    # Day 8 false positives (v9.5)
    "TECHNICAL SIGNALS", "AMID TECHNICAL", "DROPS 2.54",
    "INE101A01026", "INE364U01010",
    "SUPREME COURT", "MOVES SUPREME COURT", "RANI KAPUR",
    "FAMILY TRUST", "TRUST-LINKED BOARD",
    "ADITYA BIRLA HEALTH", "BIRLA HEALTH",
    "WELLNESS INCENTIVES", "CLAIMS RATIOS",
    "SYRMA SGS", "JASBIR SINGH GUJRAL",
    "HG INFRA",
    "100+ FIRMS TO DECLARE", "FIRMS TO DECLARE EARNINGS",
    "DIXON TECH", "MOBIKWIK",
    # Day 14 + Jun 1 false positives (v9.8)
    # PAGEIND ← "Chennai Horror: Teen Killed As Car Rams Two-Wheeler" (road-rage news)
    # PAGEIND ← "Meta layoff..." (covered above but reinforced)
    "CHENNAI HORROR", "ROAD RAGE", "TEEN KILLED",
    "CAR RAMS", "BAR ARGUMENT", "CRIME",
    "MURDER", "ASSAULT", "ACCIDENT KILLS",
    # SILINV / RELIANCE / ASIANPAINT mismatches via foreign-company headlines (Jun 1)
    # "German chipmaker Infineon..." matched SILINV via "SIL" substring
    # "Lloyds turns to copper..." matched RELIANCE
    "INFINEON", "GERMAN CHIPMAKER", "LLOYDS",
    "COPPER TO CUT", "IRON ORE RELIANCE",
    # Earnings-preview / "to post earnings" lists (Day 14)
    # Handled mainly via is_earnings_preview_list() but a few exact tokens helped:
    "TO POST EARNINGS ON", "OTHERS TO POST",
]


# ── v9.8 — Broker-as-commentator guard ──────────────────────────
# Day 14: "PI Industries... Motilal Oswal Remains Bullish" matched
# MOTILALOFS, but the broker is just the commentator, not the subject.
BROKER_NAMES = {
    "MOTILAL OSWAL":       "MOTILALOFS",
    "NUVAMA":              "NUVAMA",
    "KOTAK INSTITUTIONAL": "KOTAKBANK",
    "ICICI SECURITIES":    "ISEC",
    "HDFC SECURITIES":     None,
    "AXIS SECURITIES":     None,
    "NIRMAL BANG":         None,
    "JEFFERIES":           None,
    "MORGAN STANLEY":      None,
    "GOLDMAN SACHS":       None,
    "CLSA":                None,
    "HSBC":                None,
    "MACQUARIE":           None,
    "CITI":                None,
    "BERNSTEIN":           None,
    "EMKAY":               None,
    "ANTIQUE":             None,
    "INVESTEC":            None,
}

BROKER_OPINION_MARKERS = [
    "REMAINS BULLISH", "REMAINS BEARISH",
    "STAYS BULLISH", "STAYS BEARISH",
    "MAINTAINS", "REITERATES",
    "UPGRADES", "DOWNGRADES",
    "TARGET PRICE", "REVISED TARGET",
    "RAISES TARGET", "CUTS TARGET",
    "INITIATES COVERAGE", "BACKS",
    "SEES UPSIDE", "IN FOCUS AS",
    "BULLISH ON", "BEARISH ON",
    "BUY RATING", "SELL RATING",
    "OVERWEIGHT", "UNDERWEIGHT",
]

# ── v9.8 — Earnings-preview list markers ────────────────────────
# Day 14: "Q4 results: Grasim, Motherson... to post earnings on May 20"
# matched GRASIM/AUROPHARMA with no real catalyst.
EARNINGS_PREVIEW_MARKERS = [
    "TO POST EARNINGS", "TO REPORT EARNINGS",
    "TO POST Q1", "TO POST Q2", "TO POST Q3", "TO POST Q4",
    "TO ANNOUNCE RESULTS", "TO POST RESULTS",
    "TO REPORT RESULTS", "OTHERS TO POST",
    "FIRMS TO POST", "COMPANIES TO POST",
    "EARNINGS ON MAY", "EARNINGS ON JUN",
    "EARNINGS TODAY", "RESULTS TODAY",
]


# ── AMBIGUOUS_TICKERS — context-required matches ────────────────
AMBIGUOUS_TICKERS = {
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
    # v9.2 — Day 3 false positives
    "DEEPINDS":   ["DEEP INDUSTRIES", "DEEP INDS",
                   "DEEPINDS", "OILFIELD SERVICES"],
    "DISAQ":      ["DISA INDIA", "DISA TECHNOLOGIES",
                   "DISAQ", "DISA Q"],
    "BFINVEST":   ["BF INVESTMENT", "BHARAT FORGE",
                   "BFINVEST", "KALYANI"],
    "BAJFINANCE": ["BAJAJ FINANCE", "BAJFINANCE",
                   "BAJAJ FINSERV", "CONSUMER LOAN",
                   "EMI", "NBFC"],
    # v9.3 — Day 6 false positives
    "RAMCOIND":   ["RAMCO INDUSTRIES", "RAMCOIND",
                   "ASBESTOS", "FIBRE CEMENT", "CEMENT SHEET",
                   "RAMCO LTD", "RAMCO GROUP"],
    "DUGLOBAL-SM": ["DUDIGITAL", "DU DIGITAL", "DU GLOBAL",
                    "DUGLOBAL", "DUDIGITAL GLOBAL"],
    "DUDIGITAL":  ["DUDIGITAL", "DU DIGITAL", "DU GLOBAL",
                   "DUDIGITAL GLOBAL"],
    # v9.5 — Day 8 false positives
    "SUPREMEIND": ["SUPREME INDUSTRIES", "SUPREMEIND",
                   "PVC PIPE", "PVC PIPES", "POLYMER",
                   "PLASTIC PIPE", "BUILDING PRODUCTS",
                   "INDUSTRIAL PRODUCTS"],
    "BIRLACABLE": ["BIRLA CABLE", "BIRLACABLE",
                   "OPTICAL FIBRE", "TELECOM CABLE",
                   "FIBRE OPTIC", "CABLE LTD",
                   "BIRLA ERICSSON"],
    "ADANIGREEN": ["ADANI GREEN", "ADANIGREEN",
                   "ADANI GREEN ENERGY", "RENEWABLE",
                   "SOLAR", "WIND ENERGY", "GREEN HYDROGEN"],
    "SBIN":       ["SBI", "STATE BANK", "STATE BANK OF INDIA",
                   "SBIN", "SBI Q", "SBI BANK",
                   "PSU BANK SBI", "SBI CARDS",
                   "SBI RESULTS", "SBI MUTUAL"],
    # v9.7 — Day 9 false positive (NDTV publisher byline)
    # NDTV is both a 4-char ticker AND a major Indian publisher.
    # Backup defense — primary defense is PUBLISHER_TRAILS stripping.
    "NDTV":       ["NDTV LTD", "NDTV Q", "NDTV RESULTS",
                   "NEW DELHI TELEVISION", "NDTV NETWORK",
                   "NDTV BROADCASTING", "NDTV SHARES",
                   "NDTV STOCK", "NDTV BUYBACK",
                   "NDTV EARNINGS", "NDTV REVENUE"],
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


def _strip_publisher_trail(headline: str) -> str:
    """
    v9.7 — Strip trailing " - Publisher" / " | Publisher" if present.

    Day 9 NDTV bug: "Oil India Q4 Results - NDTV Profit" matched NDTV.
    We strip the publisher byline before any matching so publisher
    names never influence ticker resolution.

    Only the LAST separator-trail is checked, not mid-text mentions.
    Headlines like "NDTV Q4 results" (no publisher trail) pass through
    unchanged so legitimate NDTV mentions still work.
    """
    if not headline:
        return headline
    # Separators: plain hyphen, pipe, em-dash, en-dash
    for separator in [" - ", " | ", " — ", " – "]:
        if separator in headline:
            head, tail = headline.rsplit(separator, 1)
            tail_upper = tail.strip().upper()
            for publisher in PUBLISHER_TRAILS:
                # Match: tail starts with publisher
                # (handles " - NDTV Profit Updated 09:00")
                if tail_upper.startswith(publisher):
                    return head.strip()
    return headline


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


def _is_broker_commentator_match(headline_upper: str,
                                  matched_ticker: str) -> bool:
    """
    v9.8 — Returns True if matched_ticker is a broker's OWN ticker
    AND the headline contains an opinion marker (broker as commentator,
    not subject). The match should be SUPPRESSED.

    Critical edge case: a broker reporting its OWN earnings must still
    match (no opinion marker → returns False → match allowed).
    """
    for broker_alias, broker_ticker in BROKER_NAMES.items():
        if broker_alias not in headline_upper:
            continue
        if not broker_ticker or matched_ticker != broker_ticker:
            continue
        # Broker name is in headline AND match is broker's own ticker.
        # Check if it's commentator usage:
        if any(marker in headline_upper for marker in BROKER_OPINION_MARKERS):
            return True  # suppress
    return False


def _is_earnings_preview_list(headline: str,
                               min_company_names: int = 3) -> bool:
    """
    v9.8 — Returns True if headline is an earnings-calendar preview
    naming multiple companies. Dead-zones the whole headline (no
    single subject in a 12-company list).
    """
    h = headline.upper()
    has_marker = any(m in h for m in EARNINGS_PREVIEW_MARKERS)
    if not has_marker:
        return False
    comma_segments = [s.strip() for s in headline.split(",") if s.strip()]
    return len(comma_segments) >= min_company_names


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
    Match headline to ticker through layered defences.

    Step 0a: Strip publisher byline trail (v9.7).
    Step 0b: Reject generic-noise headlines (v9.6 ordering).
    Step 1:  TATA MOTORS → TMCV special case.
    Step 2:  Known aliases (longest-first), with ambiguity check.
    Step 3:  Fuzzy match against all instruments.
    Step 4:  Short-ticker substring guard.
    Step 5:  Ambiguous-ticker context check on fuzzy match.
    """
    if not headline:
        return None

    # ── Step 0a — v9.7 — Strip publisher trail ─────────────────
    headline = _strip_publisher_trail(headline)

    headline_upper = headline.upper()
    cleaned = _clean_headline(headline)

    # ── Step 0b — v9.6 — Generic rejection (before aliases) ────
    if _is_generic(headline):
        return None

    # ── Step 0c — v9.8 — Earnings-preview multi-company list ───
    if _is_earnings_preview_list(headline):
        logger.debug(f"  Earnings-preview list dead-zoned: {headline[:60]}")
        return None

    # ── Step 1 — TATA MOTORS → TMCV special case ───────────────
    if "TATA MOTORS" in headline_upper and "TMCV" in instruments:
        boost = _keyword_boost(headline)
        return {
            "symbol":     "TMCV",
            "name":       instruments["TMCV"]["name"],
            "confidence": round(min(1.0, 0.95 + boost), 3),
            "raw_score":  100,
            "boosted":    boost > 0
        }

    # ── Step 2 — Known aliases (longest first) ─────────────────
    for alias, sym in sorted(KNOWN_ALIASES.items(),
                              key=lambda x: len(x[0]), reverse=True):
        if alias.upper() in headline_upper:
            if sym in instruments:
                if _is_ambiguous_match(sym, headline_upper):
                    logger.debug(f"  Skipping ambiguous alias '{alias}'→{sym}")
                    continue
                # v9.8 — broker-as-commentator suppression in alias path
                if _is_broker_commentator_match(headline_upper, sym):
                    logger.debug(f"  Suppressing broker alias '{alias}'→{sym} "
                                 f"(broker named as commentator)")
                    continue
                boost = _keyword_boost(headline)
                return {
                    "symbol":     sym,
                    "name":       instruments[sym]["name"],
                    "confidence": round(min(1.0, 0.95 + boost), 3),
                    "raw_score":  100,
                    "boosted":    boost > 0
                }

    # ── Step 3 — Fuzzy match ───────────────────────────────────
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

    # ── Step 4 — Short-ticker substring guard ──────────────────
    if _is_short_ticker_substring_only(matched_symbol, headline_upper):
        logger.debug(f"  Skipping short-ticker substring match: {matched_symbol}")
        return None

    # ── Step 5 — Ambiguous-ticker context check ────────────────
    if _is_ambiguous_match(matched_symbol, headline_upper):
        logger.debug(f"  Skipping ambiguous fuzzy match: {matched_symbol}")
        return None

    # ── Step 6 — v9.8 — Broker-as-commentator suppression ──────
    if _is_broker_commentator_match(headline_upper, matched_symbol):
        logger.debug(f"  Suppressing broker-commentator: {matched_symbol} "
                     f"(broker named as commentator, not subject)")
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

    # Combined test set: Days 1+2+3+6+7+8+9
    test_cases = [
        # ── True positives (must still match) ────────────────────
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

        # ── Day 1 false positives ────────────────────────────────
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
        ("Canara Bank: Motilal Oswal Trims Target Price After Q4 Results, Cites Tepid NIM Guidance", "CANBK"),

        # ── Day 8 false positives ────────────────────────────────
        ("Mahindra & Mahindra Ltd stock (INE101A01026): Drops 2.54% amid technical signals - AD HOC NEWS", None),
        ("Adani Green Energy Ltd stock (INE364U01010): Share price dips 1.10% to ?1,350 - AD HOC NEWS", None),
        ("Syrma SGS Technology expects 30% revenue growth in tricky FY27, MD Jasbir Singh Gujral says", None),
        ("Rani Kapur moves Supreme Court over family trust-linked board meeting", None),
        ("Aditya Birla Health bets on wellness incentives to improve claims ratios", None),
        ("Sasken Technologies Q4 FY26 Results | HG Infra Secures Large Infrastructure Contract | Top Buzzing Stocks Today - Equitymaster", None),
        ("Q4FY26 Results Today: Dixon Tech, Tata Power, One MobiKwik, Dr Reddy's Among 100+ Firms To Declare Earnings - NDTV Profit", None),

        # ── Day 8 true positives ─────────────────────────────────
        ("Indian Hotels Q4 Results: Profit, Revenue Rise Over 14%; Dividend Declared - NDTV Profit", "INDHOTEL"),
        ("Bharti Airtel Q4 preview: strong user additions but flat Arpu may temper growth", "BHARTIARTL"),

        # ── NEW Day 9 false positives (v9.7) ─────────────────────
        # The exact headline that triggered the bogus NDTV trade:
        ("Oil India Q4 Results: Profit Rockets 76%, Revenue Tops Rs 10,000 Crore; Dividend Declared - NDTV Profit", None),
        ("Bank Stocks Mixed Today - Moneycontrol",                          None),

        # ── NEW Day 9 true positives (NDTV-the-company must still match) ──
        ("NDTV Q4 Results: New Delhi Television posts net loss",            "NDTV"),

        # ── Day 14 + Jun 1 false positives (v9.8) ────────────────
        # PAGEIND false positives — unrelated headlines
        ("Meta layoff: Here is what 8,000 employees let go will receive as severance package", None),
        ("Chennai Horror: Teen Killed As Car Rams Two-Wheeler After Bar Argument Escalates Into Road Rage", None),
        # MOTILALOFS broker-as-commentator (PI Industries is the subject)
        ("PI Industries Shares In Focus As Motilal Oswal Remains Bullish Despite Weak Q4 Results", None),
        # NIITLTD substring (IIT Bombay is not NIIT)
        ("IIT Bombay Placements 2024-25: One In Three Students Without A Job Offer, Yet Average Salary Up By 10%", None),
        # GRASIM/AUROPHARMA earnings-preview list
        ("Q4 results: Grasim, Samvardhana Motherson, Lenskart, Bosch, Apollo Hospitals, Jubilant Foodworks, Ola Electric, others to post earnings on May 20", None),
        ("Q4 results: LIC, ITC, Max Healthcare, LG Electronics, Nykaa, Ixigo, JSW Cement, GAIL India, Aurobindo Pharma, others to post earnings on May 21", None),
        # SILINV / RELIANCE Jun 1 mismatches (foreign company names)
        ("German chipmaker Infineon to expand India ops with new R&D and supply chain investments", None),
        ("Lloyds turns to copper to cut iron ore reliance, targets $1.3 billion business over 5 years", None),

        # ── Day 14 / v9.8 true positives (must still match) ──────
        # Critical edge case: broker reporting its OWN earnings must NOT be suppressed
        ("Motilal Oswal Financial reports 30% rise in Q4 profit",           "MOTILALOFS"),
        # Aurobindo Pharma real catalyst (not in a list)
        ("Aurobindo Pharma Q4 profit jumps 20% on US sales",                "AUROPHARMA"),
        # Grasim real catalyst (not in a list)
        ("Grasim Industries posts strong Q4 numbers, EBITDA up 18%",        "GRASIM"),
        # Alembic Pharma → APLLTD (not ALEMBICLTD)
        ("Alembic Pharma bets on branded drugs in the US",                  "APLLTD"),
    ]

    print("\n── EntityShield v9.8 Test ──")
    print(f"{'Result':<7} {'Got':<14} {'Expected':<14} Headline")
    print("-" * 115)

    correct = 0
    failures = []
    for headline, expected in test_cases:
        match = find_ticker(headline, instruments)
        actual = match["symbol"] if match else None
        ok = actual == expected
        if ok:
            correct += 1
        else:
            failures.append((headline, expected, actual))
        status = "✅" if ok else "❌"
        a = actual if actual else "—"
        e = expected if expected else "—"
        print(f"{status:<7} {a:<14} {e:<14} {headline[:75]}")

    print(f"\n{correct}/{len(test_cases)} correct matches")
    if failures:
        print(f"\n── FAILURES ({len(failures)}) ──")
        for headline, expected, actual in failures:
            print(f"  Expected: {expected}  Got: {actual}")
            print(f"  Headline: {headline}")
            print()
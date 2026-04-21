import os
import json
import re
import logging
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv("config/.env")
logger = logging.getLogger(__name__)

# Suffixes to strip for fuzzy matching
CORPORATE_SUFFIXES = [
    "LIMITED", "LTD", "INDUSTRIES", "INDUSTRY", "CORPORATION", "CORP",
    "ENTERPRISES", "ENTERPRISE", "TECHNOLOGIES", "TECHNOLOGY", "TECH",
    "SOLUTIONS", "SOLUTION", "SERVICES", "SERVICE", "SYSTEMS", "SYSTEM",
    "INFOSYS", "HOLDINGS", "HOLDING", "VENTURES", "VENTURE", "FINANCE",
    "FINANCIAL", "BANK", "BANKS", "INDIA", "INDIAN", "INTERNATIONAL",
    "GLOBAL", "EXPORTS", "EXPORT", "IMPORTS", "IMPORT", "TRADING",
    "CHEMICALS", "CHEMICAL", "PHARMA", "PHARMACEUTICALS", "PHARMACEUTICAL",
    "ENERGY", "POWER", "MOTORS", "MOTOR", "CABLES", "CABLE",
    "INFRASTRUCTURE", "INFRA", "PROJECTS", "PROJECT", "DEVELOPERS",
    "DEVELOPER", "REALTY", "PROPERTIES", "PROPERTY"
]

def strip_suffixes(name: str) -> str:
    """Strip corporate suffixes to create search anchor for fuzzy matching."""
    name = name.upper().strip()
    # Remove special characters except spaces
    name = re.sub(r'[^\w\s]', '', name)
    words = name.split()
    # Remove trailing suffix words
    while words and words[-1] in CORPORATE_SUFFIXES:
        words.pop()
    return " ".join(words).strip()

def load_instruments(force_refresh: bool = False) -> dict:
    """
    Load NSE instruments from Kite API.
    Caches to instruments.json. Returns dict keyed by symbol.
    """
    cache_path = "instruments.json"

    if not force_refresh and os.path.exists(cache_path):
        logger.info("Loading instruments from cache...")
        with open(cache_path, "r") as f:
            return json.load(f)

    logger.info("Fetching instruments from Kite API...")
    api_key     = os.getenv("ZERODHA_API_KEY")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

    if not api_key or not access_token:
        raise ValueError("ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN missing from .env")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    all_instruments = kite.instruments("NSE")
    logger.info(f"Fetched {len(all_instruments)} NSE instruments")

    # Build lookup dict
    instrument_map = {}
    for inst in all_instruments:
        if inst["instrument_type"] != "EQ":
            continue
        symbol  = inst["tradingsymbol"]
        name    = inst["name"] if inst["name"] else symbol
        anchor  = strip_suffixes(name)
        instrument_map[symbol] = {
            "symbol":        symbol,
            "name":          name,
            "search_anchor": anchor,
            "instrument_token": inst["instrument_token"],
            "exchange":      inst["exchange"]
        }

    # Save cache
    with open(cache_path, "w") as f:
        json.dump(instrument_map, f, indent=2)

    logger.info(f"Saved {len(instrument_map)} EQ instruments to {cache_path}")
    return instrument_map


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    instruments = load_instruments(force_refresh=True)
    total = len(instruments)
    print(f"\n✅ Loaded {total} NSE EQ instruments")
    print("\nFirst 10 instruments:")
    print(f"{'Symbol':<20} {'Name':<40} {'Search Anchor'}")
    print("-" * 80)
    for i, (symbol, data) in enumerate(list(instruments.items())[:10]):
        print(f"{symbol:<20} {data['name']:<40} {data['search_anchor']}")
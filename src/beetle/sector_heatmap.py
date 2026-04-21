import os
import logging
from datetime import datetime
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv("config/.env")
logger = logging.getLogger(__name__)

# 12 Nifty sector indices per Blueprint
SECTOR_INDICES = {
    "NIFTY BANK":               "NSE:NIFTY BANK",
    "NIFTY IT":                 "NSE:NIFTY IT",
    "NIFTY FMCG":               "NSE:NIFTY FMCG",
    "NIFTY AUTO":               "NSE:NIFTY AUTO",
    "NIFTY PHARMA":             "NSE:NIFTY PHARMA",
    "NIFTY METAL":              "NSE:NIFTY METAL",
    "NIFTY ENERGY":             "NSE:NIFTY ENERGY",
    "NIFTY REALTY":             "NSE:NIFTY REALTY",
    "NIFTY MEDIA":              "NSE:NIFTY MEDIA",
    "NIFTY CONSUMER DURABLES":  "NSE:NIFTY CONSR DURBL",
    "NIFTY HEALTHCARE":         "NSE:NIFTY HEALTHCARE",
    "NIFTY PSU BANK":           "NSE:NIFTY PSU BANK",
}

# Bias thresholds per Blueprint
BULLISH_THRESHOLD =  0.3   # Change% > +0.3% = BULLISH
BEARISH_THRESHOLD = -0.3   # Change% < -0.3% = BEARISH


def _classify(change_pct: float) -> str:
    if change_pct > BULLISH_THRESHOLD:
        return "BULLISH"
    elif change_pct < BEARISH_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


def _mock_heatmap() -> dict:
    """
    Returns mock sector data for testing outside market hours.
    Based on realistic sector moves.
    """
    logger.warning("Market closed or API unavailable — using mock heatmap data.")
    mock_data = {
        "NIFTY BANK":              +0.82,
        "NIFTY IT":                +0.45,
        "NIFTY FMCG":              +0.61,
        "NIFTY AUTO":              -0.15,
        "NIFTY PHARMA":            +0.33,
        "NIFTY METAL":             -0.55,
        "NIFTY ENERGY":            +0.12,
        "NIFTY REALTY":            -0.41,
        "NIFTY MEDIA":             -0.08,
        "NIFTY CONSUMER DURABLES": +0.28,
        "NIFTY HEALTHCARE":        +0.19,
        "NIFTY PSU BANK":          +0.67,
    }
    result = {}
    for sector, change_pct in mock_data.items():
        result[sector] = {
            "change_pct": change_pct,
            "bias":       _classify(change_pct),
            "mock":       True
        }
    return result


def get_heatmap(use_mock_if_closed: bool = True) -> dict:
    """
    Fetch live sector heatmap from Kite API.
    Falls back to mock data if market is closed or API fails.
    Returns dict keyed by sector name.
    """
    api_key      = os.getenv("ZERODHA_API_KEY")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

    if not api_key or not access_token:
        logger.warning("Kite credentials missing — using mock heatmap.")
        return _mock_heatmap()

    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Fetch quotes for all 12 indices
        symbols = list(SECTOR_INDICES.values())
        quotes  = kite.quote(symbols)

        result = {}
        for sector_name, symbol in SECTOR_INDICES.items():
            if symbol not in quotes:
                logger.warning(f"  {sector_name}: not in quote response")
                continue
            q          = quotes[symbol]
            ltp        = q["last_price"]
            change_pct = q["net_change"] / q["ohlc"]["close"] * 100 if q["ohlc"]["close"] else 0.0

            result[sector_name] = {
                "ltp":        ltp,
                "change_pct": round(change_pct, 3),
                "bias":       _classify(change_pct),
                "mock":       False
            }
            logger.info(f"  {sector_name}: {change_pct:+.2f}% → {result[sector_name]['bias']}")

        return result

    except Exception as e:
        logger.warning(f"Kite API error: {e}")
        if use_mock_if_closed:
            return _mock_heatmap()
        raise


def print_heatmap(heatmap: dict):
    """Pretty print the sector heatmap."""
    is_mock = any(v.get("mock") for v in heatmap.values())
    source  = "MOCK DATA" if is_mock else "LIVE DATA"

    print(f"\n── Sector Heatmap ({source}) ──")
    print(f"{'Sector':<28} {'Change%':<10} {'Bias'}")
    print("-" * 55)

    for sector, data in heatmap.items():
        change = data["change_pct"]
        bias   = data["bias"]
        icon   = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"
        print(f"{sector:<28} {change:>+7.2f}%   {icon} {bias}")

    bullish = sum(1 for v in heatmap.values() if v["bias"] == "BULLISH")
    bearish = sum(1 for v in heatmap.values() if v["bias"] == "BEARISH")
    neutral = sum(1 for v in heatmap.values() if v["bias"] == "NEUTRAL")
    print(f"\n  🟢 BULLISH: {bullish}  ⚪ NEUTRAL: {neutral}  🔴 BEARISH: {bearish}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    heatmap = get_heatmap(use_mock_if_closed=True)
    print_heatmap(heatmap)
    print("\n✅ SectorHeatmap ready.")
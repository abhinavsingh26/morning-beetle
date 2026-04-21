import logging
from transformers import pipeline

logger = logging.getLogger(__name__)

# Thresholds per Blueprint
BULLISH_THRESHOLD =  0.15
BEARISH_THRESHOLD = -0.15

_pipeline = None  # Lazy load — only initialised once


def _get_pipeline():
    """Load FinBERT pipeline once and cache in memory."""
    global _pipeline
    if _pipeline is None:
        logger.info("Loading FinBERT model into memory...")
        _pipeline = pipeline(
            "text-classification",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert"
        )
        logger.info("FinBERT ready.")
    return _pipeline


def score_headline(headline: str) -> dict:
    """
    Score a single headline with FinBERT.
    Returns:
        {
            score:  float  — signed score in [-1, +1]
            label:  str    — BULLISH / BEARISH / NEUTRAL
            raw:    dict   — raw FinBERT output
        }
    """
    nlp = _get_pipeline()

    # Truncate to 512 chars — FinBERT token limit
    headline = headline[:512]

    result = nlp(headline)[0]
    label_raw = result["label"].lower()   # positive / negative / neutral
    conf      = result["score"]           # 0.0 – 1.0

    # Convert to signed score
    if label_raw == "positive":
        signed_score = +conf
    elif label_raw == "negative":
        signed_score = -conf
    else:
        signed_score = 0.0

    # Classify per Blueprint thresholds
    if signed_score > BULLISH_THRESHOLD:
        label = "BULLISH"
    elif signed_score < BEARISH_THRESHOLD:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    return {
        "score": round(signed_score, 4),
        "label": label,
        "raw":   result
    }


def score_batch(headlines: list[str]) -> list[dict]:
    """Score a list of headlines. Returns list of score dicts."""
    return [score_headline(h) for h in headlines]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    test_headlines = [
        "HDFC Bank Q3 beats estimates, NII up 15%",
        "RBI maintains hawkish stance on inflation",
        "Company announces rights issue at 20% discount",
        "Tata Motors receives large order from defence ministry",
        "Infosys raises revenue guidance after strong Q3",
        "Vedanta share price slips ahead of dividend announcement",
        "Markets close flat amid global uncertainty",
        "Persistent Systems Q4 results: IT firm posts 33% rise in PAT",
    ]

    print("\n── FinBERT Scorer Test ──")
    print(f"{'Score':<10} {'Label':<10} {'Headline'}")
    print("-" * 80)

    for headline in test_headlines:
        result = score_headline(headline)
        score  = result["score"]
        label  = result["label"]
        icon   = "🟢" if label == "BULLISH" else "🔴" if label == "BEARISH" else "⚪"
        print(f"{score:<10} {icon} {label:<8} {headline[:60]}")

    print("\n✅ FinBERT scorer ready.")
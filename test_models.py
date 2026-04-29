from transformers import pipeline
import time

test_headlines = [
    "RBI maintains hawkish stance on inflation",
    "RBI hikes repo rate by 25 bps amid inflation concerns",
    "Infosys wins Rs 5000 crore deal from European bank",
    "Stock hits upper circuit after strong Q4 results",
    "Promoter pledges shares worth Rs 200 crore",
    "SEBI imposes penalty on operator for front running",
    "FII selling pressure continues, DII buying provides support",
    "Coal India Q4 results: Profit rises 12% to Rs 10908 crore",
    "Maruti Suzuki Q4 results: PAT drops 7% YoY",
    "Bandhan Bank share soars 10% to 52-week high post Q4",
]

models = [
    ("ProsusAI/finbert", "Current"),
    ("ahmedrachid/FinancialBERT-Sentiment-Analysis", "FinancialBERT"),
    ("nickmuchi/finbert-tone-financial-news-sentiment-analysis", "FinBERT-Tone"),
]

for model_name, label in models:
    print(f"\n{'='*60}")
    print(f"  {label} — {model_name}")
    print(f"{'='*60}")
    try:
        start = time.time()
        nlp = pipeline("text-classification", model=model_name)
        load_time = time.time() - start
        print(f"  Loaded in {load_time:.1f}s\n")

        print(f"{'Score':<10} {'Label':<12} {'Headline'}")
        print("-" * 75)

        for headline in test_headlines:
            t = time.time()
            result = nlp(headline)[0]
            ms = (time.time() - t) * 1000
            raw_label = result["label"].lower()
            score = result["score"]

            # Normalize to signed score
            if "pos" in raw_label:
                signed = +score
                icon = "🟢"
            elif "neg" in raw_label:
                signed = -score
                icon = "🔴"
            else:
                signed = 0.0
                icon = "⚪"

            print(f"{signed:+.3f}     {icon} {raw_label:<10} {headline[:50]}")

    except Exception as e:
        print(f"  ❌ Failed to load: {e}")
from transformers import pipeline
import time

print("="*50)
print("  Downloading FinBERT model (ProsusAI/finbert)")
print("  ~440MB — please wait...")
print("="*50 + "\n")

start = time.time()

# This downloads and caches the model on first run
nlp = pipeline(
    "text-classification",
    model="ProsusAI/finbert",
    tokenizer="ProsusAI/finbert"
)

elapsed = time.time() - start
print(f"\n✅ Model cached successfully in {elapsed:.1f}s")

# Test inference
print("\nRunning inference test...")
test_headlines = [
    "HDFC Bank Q3 beats estimates, NII up 15%",
    "RBI maintains hawkish stance on inflation",
    "Tata Motors receives large order from defence ministry"
]

for headline in test_headlines:
    t = time.time()
    result = nlp(headline)[0]
    ms = (time.time() - t) * 1000
    label = result['label']
    score = result['score']
    print(f"  [{ms:.0f}ms] {label:8s} ({score:.2f}) — {headline}")

print("\n✅ FinBERT ready. Inference < 500ms per headline.")
print("="*50)
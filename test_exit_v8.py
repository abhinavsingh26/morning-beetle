from src.core.exit_manager import ExitManager, Position, STRATEGY_TRAIL_PCT
from src.strategies.sector_pullback import SectorLeaderPullback
from src.strategies.breakout import MorningBreakout

# Test that per-strategy exit params flow through
s1 = MorningBreakout(None, "TEST")
s3 = SectorLeaderPullback(None, "TEST")

print("Strategy exit profiles:")
print(f"  S1 MorningBreakout:  SL={s1.sl_pct*100:.1f}%  Target={s1.target_pct*100:.1f}%  Trail={STRATEGY_TRAIL_PCT[s1.name]*100:.1f}%")
print(f"  S3 SectorPullback:   SL={s3.sl_pct*100:.1f}%  Target={s3.target_pct*100:.1f}%  Trail={STRATEGY_TRAIL_PCT[s3.name]*100:.1f}%")

# Test Position uses strategy params
pos1 = Position(
    trade_id=1, symbol="INFY", direction="BUY",
    entry_price=1000.0, quantity=10, sentiment_score=0.5,
    strategy_name="morning_breakout",
    sl_pct=0.008, target_pct=0.015, trail_pct=0.005,
    loss_bucket="morning"
)
print(f"\nS1 Position: SL={pos1.sl_price} Target={pos1.target_price}")
print(f"  Expected:  SL=992.00  Target=1015.00")

pos3 = Position(
    trade_id=2, symbol="INFY", direction="BUY",
    entry_price=1000.0, quantity=10, sentiment_score=0.5,
    strategy_name="sector_pullback",
    sl_pct=0.005, target_pct=0.010, trail_pct=0.003,
    loss_bucket="post_morning"
)
print(f"\nS3 Position: SL={pos3.sl_price} Target={pos3.target_price}")
print(f"  Expected:  SL=995.00  Target=1010.00")
from src.strategies.breakout import MorningBreakout
from src.strategies.rsi_momentum import RSIMomentum
from src.strategies.sector_pullback import SectorLeaderPullback
from src.strategies.vol_breakout import VolatilityContractionBreakout
from src.strategies.vol_spike import VolumeSpikeWithSentiment
from src.core.strategy_registry import StrategyRegistry
from datetime import datetime, time

reg = StrategyRegistry()
reg.register(MorningBreakout(None, "TEST"))
reg.register(RSIMomentum(None, "TEST"))
reg.register(SectorLeaderPullback(None, "TEST"))
reg.register(VolatilityContractionBreakout(None, "TEST"))
reg.register(VolumeSpikeWithSentiment(None, "TEST"))

print("\n" + reg.summary())
print(f"\nTotal strategies: {len(reg.all_strategies())}")

# Test active window logic
test_times = [
    time(9, 25),
    time(9, 45),
    time(11, 0),
    time(13, 0),
    time(14, 0),
    time(14, 50),
]

for t in test_times:
    now = datetime.now().replace(hour=t.hour, minute=t.minute, second=0)
    active = reg.active_strategies(now)
    print(f"\n  {t.strftime('%H:%M')}: {len(active)} active")
    for s in active:
        print(f"    - {s.name}")
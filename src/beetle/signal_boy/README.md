# 🤖 Signal Boy

Intelligence layer for Morning Beetle. Generates ranked signals every 15 minutes
throughout the trading day. Lives as a background thread inside the main engine
(not a separate program).

See `docs/SIGNAL_BOY_DESIGN.md` for full design specification.

## Build Status (Phase 6D)

| Task | Module | Status |
|------|--------|--------|
| 6D.1 | `ingestion_cache.py`  | ✅ Built (May 12, 2026) |
| 6D.2 | `ranker.py`           | ⏳ Pending (Day 8 EOD) |
| 6D.3 | `signal_boy.py` + `queue_writer.py` | ⏳ Pending (Day 9 EOD) |
| 6D.4 | engine integration (`main.py` wiring) | ⏳ Pending (Day 12 EOD) |

## How to Test What's Built

### 6D.1 — IngestionCache
```powershell
python -m src.beetle.signal_boy.ingestion_cache
```
Expected: 8/8 tests pass.

### Module Imports
```powershell
python -c "from src.beetle.signal_boy.ingestion_cache import IngestionCache, SOURCE_REGISTRY; print(list(SOURCE_REGISTRY.keys()))"
```
Expected: list of 10 source IDs.

## Source Registry

Signal Boy v1 fetches from 9 sources via the IngestionCache:

| Source | TTL | Category |
|--------|-----|----------|
| google_business | 300s | aggregator |
| google_earnings | 300s | aggregator |
| google_corporate | 300s | aggregator |
| livemint_markets | 300s | aggregator |
| livemint_companies | 300s | aggregator |
| hindu_business | 600s | aggregator |
| ndtv_profit | 600s | aggregator |
| nse_filings | 90s | official_filings |
| pulse_zerodha | 120s | curated |
| pib_defence | 300s | official_press |

Lower TTL = more frequent fetch. NSE filings and Pulse have the lowest latency
because they carry the highest signal-to-noise ratio.

## Anti-Goals (Won't Do)

- ❌ Place orders
- ❌ Manage risk
- ❌ Exit positions
- ❌ Run as separate process
- ❌ Add Twitter scraping in v1
- ❌ Run after 14:30

— Abhinav, Phase 6D, May 2026

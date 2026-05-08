# 🤖 Signal Boy — Design Document

**Module:** `src/beetle/signal_boy/` (lives inside Morning Beetle, not separate)
**Owner:** Abhinav
**Status:** Designed, pending build (Phase 6D)
**Build trigger:** After Stage 5a extension passes (Mon–Wed next week)
**Created:** 2026-05-08 (Fri evening, post Day 5 gate review)
**Last Updated:** 2026-05-08 (added §2.1, §4.5, §13.1)

---

## 1. Single Purpose

> **Signal Boy generates ranked, evidence-backed signals for Morning Beetle's
> strategies to consume. Continuously, throughout the trading day. That's it.**

It does not place orders. It does not manage risk. It does not exit positions.
It just produces the highest-quality watchlist possible at any given moment
and writes it to a JSON queue that the engine reads.

---

## 2. Why It Exists (Problems Solved)

| Current Problem | Signal Boy's Fix |
|-----------------|------------------|
| Morning watchlist becomes stale by 11 AM | 15-min refresh keeps signals fresh until 14:30 |
| S3/S4/S5 idle without fresh tickers | Continuous signal stream feeds them |
| Bad signals (false positives) trade all day | Bad signals expire after 1–2 scans if not validated |
| Each strategy refetches news independently | One Shared Ingestion Cache — all readers |
| No structured way to add new signal sources | Plug new fetcher into cache, done |
| Stage 5b/5c can't unlock without mid-day discovery | Signal Boy IS the mid-day discovery |

---

## 2.1 Execution Timing  ★ NEW

Three distinct events happen each trading day. Signal Boy does **not** change
the boot time or the first-trade time — it changes the *frequency* of scans.

| Event | Time | Notes |
|-------|------|-------|
| **Manual auth step** | 08:45 AM | User runs `auth_test.py` — generates fresh Zerodha access token (Zerodha 2FA, non-automatable). Must complete before 09:00 boot. |
| **Engine boot** | 09:00 AM | Windows Task Scheduler triggers `main.py`. Engine lock acquired, Kite REST initialised, instruments loaded, Signal Boy thread starts. |
| **First scan (Scan #1)** | **09:01 AM** | Signal Boy's first run. Replaces current `intelligence.py` pre-market flow. Fetches news, scores sentiment, applies EntityShield, writes initial `signals/queue.json`. |
| **WebSocket subscription** | 09:01 → 09:14 | Engine subscribes to top tickers from Scan #1 results. |
| **Pre-market Telegram report** | 09:12 AM | Sent after Scan #1 completes, before market opens. |
| **Market opens** | 09:15 AM | NSE opens. WebSocket starts streaming live ticks. Reference candles begin building (S1 needs first 15 min). |
| **First strategy signal possible** | **09:30 AM** | S1/S2 active windows open. First trade can fire after indicators warm up (~09:32). |
| **Last fresh signal** | 14:30 PM | Final Signal Boy scan. After this, no new signals — engine focuses on exits only. |
| **Hard no-entry cutoff** | 14:45 PM | RiskManager rejects all SignalEvents past this time. |
| **Kill switch** | 15:15 PM | All open positions force-closed. |

**Key answer to "when does first execution happen?"**

The first **trade** can execute around 09:30–09:32 — same as today. Signal Boy
doesn't change this. What changes is that instead of one stale watchlist used
all day, **22 fresh scans** keep the universe live throughout the trading day:

```
09:01  Scan #1   ← Pre-market, replaces current intelligence.py
09:15  Scan #2   ← Just before market open
09:30  Scan #3   ← First post-open scan; S1/S2 strategies start firing
09:45  Scan #4
10:00  Scan #5
10:15  Scan #6
10:30  Scan #7   ← Regime transition (S3 activates in Stage 5b+)
10:45  Scan #8
... continues every 15 min ...
13:30  Scan #18  ← S4 activates in Stage 5d
14:30  Scan #22  ← FINAL scan
14:45+ ─────────  No fresh signals; exit logic only
```

**Will Signal Boy run earlier than 09:00?** Not in v1. Pushing earlier (e.g.,
08:30 to catch pre-open news) would require moving the manual `auth_test.py`
step too — adds friction without proven payoff. Defer to a future phase if
the data shows pre-09:01 news has actionable value for intraday.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                  MORNING BEETLE (single process)                 │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  🤖 SIGNAL BOY (background thread, 15-min cadence)       │   │
│  │                                                            │   │
│  │     ┌─────────────────────────────────┐                  │   │
│  │     │     Shared Ingestion Cache      │                  │   │
│  │     │     (SQLite, TTL-based)         │                  │   │
│  │     │                                  │                  │   │
│  │     │  • RSS feeds (7 sources)        │                  │   │
│  │     │  • NSE corporate filings  ★ NEW │                  │   │
│  │     │  • Pulse RSS (Zerodha)    ★ NEW │                  │   │
│  │     │  • Sector heatmap (5-min TTL)   │                  │   │
│  │     │  • F&O ban list (15-min TTL)    │                  │   │
│  │     │  • India VIX (5-min TTL)        │                  │   │
│  │     └────────────────┬────────────────┘                  │   │
│  │                      │                                    │   │
│  │              fetch every 15 min                           │   │
│  │                      ↓                                    │   │
│  │     ┌────────────────────────────────┐                   │   │
│  │     │     Scoring Pipeline           │                   │   │
│  │     │  1. EntityShield → ticker      │                   │   │
│  │     │  2. FinBERT → sentiment        │                   │   │
│  │     │  3. Sector convergence gate    │                   │   │
│  │     │  4. Catalyst strength          │                   │   │
│  │     │  5. Composite ranking          │                   │   │
│  │     └────────────────┬───────────────┘                   │   │
│  │                      ↓                                    │   │
│  │     ┌────────────────────────────────┐                   │   │
│  │     │     Queue Writer (atomic)      │                   │   │
│  │     │     → signals/queue.json       │                   │   │
│  │     └────────────────────────────────┘                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              ↓                                    │
│              signals/queue.json (always written)                  │
│                              ↓                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │            Engine Universe Manager (in main.py)          │   │
│  │  • Reads queue.json on each scan tick                    │   │
│  │  • Subscribes to NEW tickers via WebSocket               │   │
│  │  • Drops EXPIRED tickers (with grace period)             │   │
│  │  • Provides universe to StrategyRegistry                 │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              ↓                                    │
│       Strategies (S1–S5) evaluate ticks normally                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Module Structure

```
src/beetle/signal_boy/
├── __init__.py
├── signal_boy.py              # Main orchestrator (background thread)
├── ingestion_cache.py         # Shared SQLite cache, TTL-based readers
├── ranker.py                  # Composite scoring (sentiment × catalyst × sector)
├── queue_writer.py            # Atomic JSON write to signals/queue.json
└── README.md                  # Module docs
```

**Files modified (not created):**
- `src/main.py` — adds `SignalBoy.start()`, replaces watchlist polling
- `src/core/data_handler.py` — adds dynamic subscribe/unsubscribe methods
- `src/beetle/news_fetcher.py` — `fetch_for_ticker()` migrates to read from cache

**Files retired:**
- `watchlist.json` (replaced by `signals/queue.json`)
- Dynamic universe monitor logic in `main.py` (merged into Signal Boy)

---

## 4.5 Source Coverage  ★ NEW

Signal Boy v1 ships with **9 sources** in the Shared Ingestion Cache (current
7 RSS feeds + 2 new high-priority sources). Twitter and MoneyControl scrapers
are explicitly excluded from v1 — see §13 Anti-Goals.

### Current 7 sources (already wired)

```
google_business      Google News (NSE India query)         RSS, ~5–10 min latency
google_earnings      Google News (Q4 earnings query)       RSS, ~5–10 min latency
google_corporate     Google News (corporate actions query) RSS, ~5–10 min latency
livemint_markets     LiveMint Markets RSS                  RSS, ~5–15 min latency
livemint_companies   LiveMint Companies RSS                RSS, ~5–15 min latency
hindu_business       The Hindu Business Line RSS           RSS, ~10–20 min latency
ndtv_profit          NDTV Profit (FeedBurner)              RSS, ~10–20 min latency
```

### Two NEW sources for Signal Boy v1

| Source | URL | TTL | Why It Matters |
|--------|-----|-----|----------------|
| **NSE Corporate Filings** | `https://www.nseindia.com/api/corporate-announcements` | 90s | Official exchange filings (results, dividends, mergers, regulatory). Lowest latency (~2 min). Highest signal-to-noise ratio. Catches catalysts *before* news aggregators pick them up. |
| **Pulse RSS (Zerodha)** | `https://pulse.zerodha.com/feed` | 120s | Curated by Zerodha specifically for Indian retail traders. Built-in noise filtering. No false foreign-market headlines. |

### Why these specifically (and not others)

**Added because:**
- NSE filings are the *primary source* — every other RSS feed is downstream of these. Going direct cuts latency by 5–10 minutes.
- Pulse RSS has the best signal-to-noise of any free India-focused feed.
- Both have TTLs that reduce duplicate fetches (Shared Ingestion Cache ensures
  one poll, many readers).

**NSE-specific implementation notes:**
- Requires User-Agent rotation (NSE blocks repeated UAs)
- Requires session cookies (first call hits homepage to seed cookies)
- Polled every 60s by the writer; cache TTL of 90s prevents stampede

**NOT added in v1** (deferred or rejected):
- **Twitter via snscrape** — fragile, breaks when X changes anti-scrape rules
- **MoneyControl scraper** — aggressive bot detection, content already in Google News
- **BSE filings** — duplicates 95% of NSE filings; defer to Phase 6E if NSE coverage gaps appear
- **Earnings calendar APIs** — paid services; defer to v3 when budget allows

### Ingestion Cache TTL Table

| Source | TTL | Rationale |
|--------|-----|-----------|
| `nse_filings` | 90s | Official, fast-moving, primary catalyst source |
| `pulse_rss` | 120s | Curated, slightly less time-critical |
| `google_business / earnings / corporate` | 300s | Aggregated, already 5+ min behind source |
| `livemint_markets / companies` | 300s | Same — aggregated content |
| `hindu_business / ndtv_profit` | 600s | Slowest sources, longest cache window |
| `sector_heatmap` (Kite quote) | 300s | Sector indices change slowly |
| `india_vix` | 300s | Same as sector |
| `fno_ban_list` | 900s | Updates only at end of day; 15-min TTL is generous |

### Future expansion path (not v1)

If Signal Boy v1 proves stable and Stage 5d signals show gaps, add in this order:

1. **Phase 6E:** BSE filings (fills NSE-only gap for BSE-exclusive stocks)
2. **Phase 6F:** Curated Twitter via official X API (if budget allows)
3. **v3:** Paid earnings calendar API (Trendlyne or Screener)
4. **v3:** PDF parsing for earnings call transcripts

---

## 5. Output Contract

### `signals/queue.json` Schema (v1.0)

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-05-08T11:30:00+05:30",
  "scan_id": 11,
  "trading_date": "2026-05-08",
  "active_signals": [
    {
      "symbol": "POLYCAB",
      "rank": 1,
      "sentiment_score": 0.93,
      "sentiment_label": "BULLISH",
      "sector": "NIFTY IT",
      "sector_bias": "BULLISH",
      "catalyst_strength": 0.88,
      "composite_score": 2.74,
      "headline": "Polycab India Q4 PAT 32%",
      "headline_source": "google_earnings",
      "first_seen_at": "2026-05-08T09:01:00+05:30",
      "last_validated_at": "2026-05-08T11:30:00+05:30",
      "scans_validated": 11,
      "stale": false,
      "instrument_token": 2455041
    }
  ],
  "expired_signals": [
    {
      "symbol": "BIOCON",
      "expired_at": "2026-05-08T10:30:00+05:30",
      "reason": "no_fresh_news"
    }
  ],
  "metadata": {
    "scans_today": 11,
    "next_scan_at": "2026-05-08T11:45:00+05:30",
    "cache_hit_rate": 0.82,
    "total_active": 8,
    "total_expired_today": 3
  }
}
```

### Atomic Write Pattern
- Write to `signals/queue.json.tmp`
- `os.replace()` to `signals/queue.json` (atomic on Windows)
- Engine never reads partial file

### Failure Mode
If queue.json is missing or stale (>2 scans old), engine logs warning but
**does not abort**. Existing subscribed tickers continue to be monitored.

---

## 6. Scan Schedule

| Time | Scan # | Notes |
|------|--------|-------|
| 09:01 | 1 | Pre-market scan (replaces current `intelligence.py` flow) |
| 09:15 | 2 | Just before market open |
| 09:30 | 3 | First post-open scan |
| 09:45 | 4 | |
| 10:00 | 5 | |
| 10:15 | 6 | |
| 10:30 | 7 | Regime transition (S3 activates) |
| 10:45 | 8 | |
| 11:00 | 9 | |
| 11:15 | 10 | |
| 11:30 | 11 | |
| 11:45 | 12 | |
| 12:00 | 13 | Lunch lull begins |
| 12:15 | 14 | Light activity expected |
| 12:30–13:30 | 15–17 | Lunch lull (still scan, fewer matches) |
| 13:30 | 18 | S4 activates (afternoon re-engagement) |
| 13:45 | 19 | |
| 14:00 | 20 | |
| 14:15 | 21 | |
| 14:30 | 22 | **Final scan** — after this, no new signals |
| 14:45+ | — | No-entry zone, exits only |

**Total: 22 scans per trading day.**

---

## 7. Composite Scoring

```python
composite_score = (
    abs(sentiment_score) * sentiment_weight +     # 0.40
    catalyst_strength    * catalyst_weight +      # 0.35
    sector_alignment     * sector_weight          # 0.25
)
```

Where:
- `sentiment_score`: existing FinBERT signed score (−1 to +1)
- `catalyst_strength`: 0–1 score from BOOST_KEYWORDS hit count
  - Q4 RESULTS BEAT → 1.0
  - DIVIDEND ANNOUNCED → 0.7
  - GENERIC NEWS → 0.3
- `sector_alignment`: 1.0 if sentiment direction matches sector_bias, else 0.5

Tickers below `min_composite_score = 0.60` are dropped.

Top N (default 15) make it into the active queue. Cap is configurable.

---

## 8. Signal Lifecycle

| Stage | Trigger | Action |
|-------|---------|--------|
| **Born** | First scan that finds the ticker | Added to queue with `first_seen_at` |
| **Validated** | Subsequent scans still find ticker | `scans_validated` increments |
| **Stale** | 2 consecutive scans don't find ticker | `stale = true`, but stays in queue |
| **Expired** | Stale + 1 more miss (3 scans absent) | Moves to `expired_signals[]` |
| **Re-born** | Reappears after expiry | New entry, fresh `first_seen_at` |

This prevents a ticker from being "removed" mid-trade due to one missed scan.

---

## 9. Engine Integration

### main.py — Boot Sequence (modified)

```python
# Existing
db = TradeDB()
engine = TradingEngine(...)
data_handler = DataHandler(engine, instruments)

# NEW
signal_boy = SignalBoy(
    instruments=instruments,
    sector_cache=sector_cache,
    sector_cache_lock=sector_cache_lock,
    db=db,
    config={
        "scan_interval_minutes": 15,
        "max_active_signals": 15,
        "min_composite_score": 0.60,
        "cache_path": "signals/ingestion_cache.db",
        "queue_path": "signals/queue.json",
    }
)
signal_boy.start()  # background thread
logger.info("🤖 Signal Boy started")

# Existing
risk = RiskManager(...)
exits = ExitManager(..., kite=kite_rest)
```

### Universe Manager (replaces dynamic universe monitor)

```python
def universe_loop(signal_boy, data_handler, registry, ...):
    """Reads signals/queue.json every 30s, syncs WebSocket subscriptions."""
    last_seen = set()
    while not stop_event.is_set():
        try:
            queue = signal_boy.read_queue()
            current = {s["symbol"] for s in queue["active_signals"]}

            # NEW signals → subscribe + register strategies
            for symbol in current - last_seen:
                data_handler.subscribe([symbol])
                for strategy_class in ENABLED_STRATEGIES:
                    strategy = build_strategy(strategy_class, symbol, queue)
                    registry.register(strategy)

            # EXPIRED signals → unsubscribe (with grace period)
            for symbol in last_seen - current:
                if has_open_position(symbol):
                    continue  # don't drop active trades
                data_handler.unsubscribe([symbol])
                registry.unregister(symbol)

            last_seen = current
            time.sleep(30)
        except Exception as e:
            logger.error(f"Universe loop error: {e}")
            time.sleep(60)
```

---

## 10. Build Plan — 4 Tasks

| Task | Effort | Files Touched | Risk |
|------|--------|---------------|------|
| **6D.1** Build `IngestionCache` (SQLite, TTL readers) | 2 hrs | `signal_boy/ingestion_cache.py` | Low — standalone |
| **6D.2** Build `Ranker` (composite scoring) | 1 hr | `signal_boy/ranker.py` | Low — pure function |
| **6D.3** Build `SignalBoy` orchestrator + `QueueWriter` | 3 hrs | `signal_boy/signal_boy.py`, `queue_writer.py` | Medium — threading |
| **6D.4** Wire into `main.py` (universe manager) | 1 hr | `main.py`, `data_handler.py` | High — touches running engine |

**Total: ~7 hours.** Spread over 2–3 evenings.

### Build Order (Safest)

1. **6D.1 first** — IngestionCache standalone, no engine touch
2. **6D.2 second** — Ranker is pure function, easy to test
3. **6D.3 third** — Build SignalBoy that writes JSON. Run it standalone to validate output.
4. **6D.4 last** — Only after 1–3 are validated. This is the one that touches live engine.

### Test Strategy

- Run Signal Boy in **shadow mode** for 2 days first:
  - It runs and writes `signals/queue_shadow.json`
  - Engine still uses old `watchlist.json`
  - Compare side-by-side: which has fewer false positives?
- Once shadow validates → flip to production mode (engine reads `queue.json`)

---

## 11. What Gets Replaced / Kept

| Current | Status After Signal Boy |
|---------|------------------------|
| `intelligence.py` 09:01 run | → Becomes Signal Boy's scan #1 |
| Static `watchlist.json` | → Replaced by `signals/queue.json` |
| Dynamic universe monitor | → Merged into Signal Boy |
| `news_fetcher.fetch_for_ticker()` | → Reads from Shared Ingestion Cache |
| EntityShield + FinBERT + Sector heatmap | → Kept exactly, called by Signal Boy |
| Strategy active windows (S1–S5) | → Kept exactly |
| RiskManager + ExitManager | → Kept exactly |
| Telegram notifier | → Kept (Signal Boy can optionally send digest) |

---

## 12. Open Questions / Decisions

- [ ] Should Signal Boy also write to `signals/log.db` for historical analysis?
      (Probably yes — useful for "which signals would have won" backtests)
- [ ] Should expired signals be unsubscribed immediately or after grace period?
      (Current plan: 30s grace, but never drop if position open)
- [ ] What's `max_active_signals`? Currently 15. Stage 5b might want lower (10).
- [ ] Should Signal Boy emit Telegram digest at every scan? Or only at regime
      transitions (10:30, 13:30, 14:30)? (Recommendation: regime transitions only)
- [ ] When Signal Boy is in shadow mode, should it use a separate cache DB or
      share with production reads? (Recommendation: separate `_shadow.db`)

---

## 13. Anti-Goals (What Signal Boy Will NOT Do)

To prevent scope creep:

- ❌ Will not place orders (that's strategies' job)
- ❌ Will not manage risk (that's RiskManager)
- ❌ Will not exit positions (that's ExitManager)
- ❌ Will not call FinBERT directly — uses existing scorer module
- ❌ Will not invent new sentiment logic — uses existing pipeline
- ❌ Will not become a separate process (lives in same engine)
- ❌ Will not require any new external dependencies (stays on existing stack)
- ❌ Will not change strategy active windows (S1–S5 unchanged)
- ❌ Will not run after 14:30 (no fresh signals during exit-only window)
- ❌ Will not change the 09:00 boot time (auth_test.py still runs at 08:45)

### 13.1 Sources NOT Added in v1  ★ NEW

Twitter and MoneyControl scrapers are explicitly **excluded** from Signal Boy v1.

| Source | Why Excluded |
|--------|--------------|
| **Twitter (via snscrape)** | Fragile dependency. Breaks repeatedly when X changes anti-scrape rules. Most useful Indian financial Twitter (CNBC TV18, ET NOW, MoneycontrolCom) already shows up in Google News and LiveMint feeds. Defer to Phase 6F if budget allows official X API. |
| **MoneyControl scraper** | Aggressive bot detection. Unreliable HTML parsing — DOM changes break the scraper without warning. Their content shows up in Google News anyway. Not worth the maintenance burden. |
| **BSE filings (independent fetch)** | Duplicates ~95% of NSE filings content. Adds rate-limit pressure with little marginal value. Defer to Phase 6E only if NSE coverage gaps appear in production. |
| **Paid earnings calendar APIs** | Trendlyne / Screener APIs cost money. Defer to v3 when budget allows and there's a proven gap. |
| **PDF parsing of earnings transcripts** | Heavy dependency, slow. Defer to v3. Out of scope for intraday signals. |

The principle: **Signal Boy v1 stays on the existing stack with two well-chosen
additions (NSE direct + Pulse). No new external dependencies, no fragile
scrapers, no paid APIs.**

---

## 14. Success Metrics

After 5 trading days with Signal Boy live:

- ✅ **Watchlist quality:** ≥ 80% of queue.json signals match real news (vs current 67–80%)
- ✅ **Mid-day refresh proven:** S3 fires on tickers added after 10:30 scan
- ✅ **No regressions:** Stage 5a win rate maintained or improved
- ✅ **Cache hit rate:** ≥ 70% on RSS fetches (proves dedup works)
- ✅ **Engine stability:** Zero crashes attributable to Signal Boy
- ✅ **Latency:** Each scan completes in < 90 seconds

---

## 15. Timeline

| Date | Event |
|------|-------|
| Mon 11 May | Stage 5a Day 6 (extension day 1) — engine runs as-is |
| Tue 12 May | Stage 5a Day 7 — evening: build 6D.1 (IngestionCache) |
| Wed 13 May | Stage 5a Day 8 (final extension) — evening: build 6D.2 (Ranker) |
| Thu 14 May | Stage 5a gate review #2 — evening: build 6D.3 (SignalBoy) |
| Fri 15 May | If gate passes → start Signal Boy SHADOW mode (parallel to live engine) |
| Mon 18 May | Day 1 of Signal Boy shadow validation |
| Tue 19 May | Day 2 of shadow validation, build 6D.4 |
| Wed 20 May | Promote Signal Boy to PRODUCTION mode |
| Thu 21 May | Stage 5b begins — S3 activates, fed by Signal Boy queue |

---

## 16. The Big Picture

Morning Beetle has three layers:

1. **Signal Boy** (intelligence) — generates the universe of opportunities
2. **Strategy Registry** (decision) — applies S1–S5 logic to the universe
3. **Risk + Execution** (action) — gates and executes approved signals

V1 spec called these "Scanner + Scout + Star". You're calling Signal Boy what
V1 split into Scanner + Scout. The unification is correct — they share too
much logic (FinBERT, EntityShield, sector heatmap) to justify separation.

**Signal Boy is the missing piece.** Once it's built, Phase 6D is complete and
Stage 5b/5c/5d can unlock cleanly.

---

**End of Design Doc**

*Pin this in `docs/SIGNAL_BOY_DESIGN.md` next to the v9 Blueprint. Reference
when starting Phase 6D build next week.*

# Morning Beetle 
import os
import sys
import json
import logging
import threading
import time
import time
from datetime import datetime
from datetime import datetime, time as dtime
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv("config/.env")

# ── Core imports ─────────────────────────────────────────────────────
from src.core.events      import MarketEvent, SignalEvent, OrderEvent, FillEvent
from src.core.engine      import TradingEngine
from src.core.trade_db    import TradeDB
from src.core.data_handler import DataHandler
from src.core.risk        import RiskManager
from src.core.exit_manager import ExitManager
from src.core.execution   import ExecutionHandler

# ── Telegram Notification ─────────────────────────────────────────────────
from src.notifications.telegram_bot import TelegramNotifier

# ── Strategy imports ─────────────────────────────────────────────────
from src.strategies.breakout         import MorningBreakout
from src.strategies.rsi_momentum     import RSIMomentum
from src.strategies.sector_pullback  import SectorLeaderPullback
from src.strategies.vol_breakout     import VolatilityContractionBreakout
from src.strategies.vol_spike        import VolumeSpikeWithSentiment
from src.core.strategy_registry      import StrategyRegistry
from src.core.historical_seeder import HistoricalSeeder

# ── Intelligence imports ──────────────────────────────────────────────
from src.beetle.instrument_master import load_instruments
from src.beetle.intelligence      import run_pipeline, save_watchlist

LOCK_FILE = "engine.lock"



# Shared sector cache — updated every 5 minutes
sector_cache = {}
sector_cache_lock = threading.Lock()

def acquire_lock() -> bool:
    """
    Create a lock file to prevent duplicate engine instances.
    Returns True if lock acquired, False if another instance is running.
    """
    if os.path.exists(LOCK_FILE):
        # Check if the lock is stale (older than 24 hours)
        lock_age = datetime.now().timestamp() - os.path.getmtime(LOCK_FILE)
        if lock_age > 86400:  # 24 hours
            logger.warning("Stale lock file found — removing.")
            os.remove(LOCK_FILE)
        else:
            logger.error(f"❌ Engine already running! Lock file exists: {LOCK_FILE}")
            logger.error("   If this is wrong, delete engine.lock and restart.")
            return False

    with open(LOCK_FILE, "w") as f:
        f.write(f"{datetime.now().isoformat()}\n")
        f.write(f"PID: {os.getpid()}\n")
    logger.info(f"✅ Engine lock acquired (PID: {os.getpid()})")
    return True

def release_lock():
    """Remove lock file on clean shutdown."""
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.info("✅ Engine lock released.")


# ── Logging setup ────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    handlers= [
        logging.FileHandler("live_logs.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

IS_PAPER_TRADING = os.getenv("IS_PAPER_TRADING", "True") == "True"

MIN_ACTIVE_CANDIDATES = 3    # Trigger refresh if below this
MAX_SUBSCRIPTIONS     = 10   # Never subscribe more than this

# ── v8 Phase 5 Staged Rollout ────────────────────────────────────────
# Edit this list to control which strategies are active.
# Stage 5a — start here:  ["morning_breakout", "rsi_momentum"]
# Stage 5b — add S3:      + "sector_pullback"
# Stage 5c — add S5:      + "vol_spike_sentiment"
# Stage 5d — all 5:       + "vol_contraction"
ENABLED_STRATEGIES = [
    "morning_breakout",
    "rsi_momentum",
]

# Strategy class registry — keyed by strategy.name
STRATEGY_CLASSES = {
    "morning_breakout":    MorningBreakout,
    "rsi_momentum":        RSIMomentum,
    "sector_pullback":     SectorLeaderPullback,
    "vol_contraction":     VolatilityContractionBreakout,
    "vol_spike_sentiment": VolumeSpikeWithSentiment,
}


def load_watchlist() -> list:
    """Load watchlist.json produced by Morning Beetle."""
    try:
        with open("watchlist.json") as f:
            data = json.load(f)
        tickers = data.get("tickers", [])
        max_pos = data.get("max_positions", 3)
        logger.info(f"Watchlist loaded: {[t['symbol'] for t in tickers]} "
                   f"(max {max_pos} positions)")
        return tickers
    except Exception as e:
        logger.error(f"Failed to load watchlist.json: {e}")
        return []

def cleanup_stale_trades(db: TradeDB):
    """
    On engine startup, auto-close any trades left OPEN from previous days.
    Prevents stale positions from blocking new signals.
    """
    from sqlalchemy.orm import Session
    from src.core.trade_db import Trade
    from datetime import date

    today = datetime.now().date()

    with Session(db.engine) as session:
        stale = session.query(Trade).filter(
            Trade.status == "OPEN"
        ).all()

        cleaned = 0
        for trade in stale:
            trade_date = trade.entry_time.date() if trade.entry_time else None
            if trade_date and trade_date < today:
                trade.status      = "CLOSED"
                trade.exit_reason = "STALE_CLEANUP"
                trade.exit_price  = trade.entry_price  # No P&L — unknown exit
                trade.pnl         = 0.0
                cleaned += 1
                logger.info(f"  🧹 Stale trade cleaned: {trade.symbol} "
                           f"[ID:{trade.id}] from {trade_date}")

        if cleaned > 0:
            session.commit()
            logger.info(f"  ✅ {cleaned} stale trades closed.")
        else:
            logger.info("  ✅ No stale trades found.")

def run_morning_beetle() -> list:
    """Run Morning Beetle pre-market pipeline."""
    logger.info("Running Morning Beetle intelligence pipeline...")
    try:
        watchlist = run_pipeline()
        save_watchlist(watchlist)
        return watchlist
    except Exception as e:
        logger.error(f"Morning Beetle pipeline failed: {e}")
        return []


def start_heatmap_refresher(interval_seconds: int = 300):
    """
    Background thread — refreshes sector heatmap every 5 minutes.
    Updates shared sector_cache dict.
    """
    from src.beetle.sector_heatmap import get_heatmap

    def _refresh():
        while True:
            try:
                now = datetime.now().time()
                # Only refresh during market hours
                if dtime(9, 12) <= now <= dtime(15, 30):
                    heatmap = get_heatmap(use_mock_if_closed=False)
                    with sector_cache_lock:
                        sector_cache.update(heatmap)
                    bullish = sum(1 for v in heatmap.values() if v["bias"] == "BULLISH")
                    bearish = sum(1 for v in heatmap.values() if v["bias"] == "BEARISH")
                    logger.info(f"  🌡️  Heatmap refreshed — "
                               f"🟢{bullish} BULLISH | 🔴{bearish} BEARISH")
            except Exception as e:
                logger.warning(f"  Heatmap refresh failed: {e}")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_refresh, daemon=True, name="HeatmapRefresher")
    t.start()
    logger.info("✅ Heatmap refresher started — updates every 5 minutes.")
    return t

def start_dynamic_universe(engine, data_handler, instruments,
                            registry, watchlist, notifier,
                            sector_cache, sector_cache_lock, db):
    """
    Background thread — monitors active candidates.
    If fewer than MIN_ACTIVE_CANDIDATES remain viable,
    re-runs pipeline and subscribes fresh tickers.
    """
    from src.beetle.intelligence import run_pipeline_fresh

    subscribed_symbols = [t["symbol"] for t in watchlist]
    total_subscribed   = len(subscribed_symbols)
    db_ref             = db

    def _monitor():
        nonlocal subscribed_symbols, total_subscribed

        while True:
            time.sleep(300)   # Check every 5 minutes

            now = datetime.now().time()

            # Only refresh during entry window
            if not (dtime(9, 15) <= now <= dtime(10, 15)):
                continue

            # Count candidates that haven't traded or signalled today
            from sqlalchemy.orm import Session
            from src.core.trade_db import Trade, Signal

            today = datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0)
            with Session(db_ref.engine) as session:
                # Symbols with any trade today
                traded = [
                    t.symbol for t in session.query(Trade).filter(
                        Trade.entry_time >= today
                    ).all()
                ]
                # Symbols that fired any signal today
                signalled = [
                    s.symbol for s in session.query(Signal).filter(
                        Signal.timestamp >= today
                    ).all()
                ]

            # Active = subscribed but not yet traded AND not yet signalled
            exhausted = set(traded + signalled)
            active = [s for s in subscribed_symbols if s not in exhausted]

            logger.info(f"🔄 Universe check — active: {len(active)}/{len(subscribed_symbols)} "
                       f"(traded: {len(set(traded))}, signalled: {len(set(signalled))})")

            if len(active) >= MIN_ACTIVE_CANDIDATES:
                continue

            # Check cap
            if total_subscribed >= MAX_SUBSCRIPTIONS:
                logger.info("🔄 Dynamic refresh: max subscriptions reached.")
                continue

            logger.info(f"🔄 Only {len(active)} active candidates — "
                       f"triggering universe refresh...")

            try:
                fresh = run_pipeline_fresh(
                    exclude_symbols=subscribed_symbols
                )

                if not fresh:
                    logger.info("🔄 No fresh candidates found.")
                    continue

                # Subscribe new tickers
                slots_available = MAX_SUBSCRIPTIONS - total_subscribed
                new_tickers = fresh[:slots_available]

                for t in new_tickers:
                    symbol = t["symbol"]
                    if symbol in subscribed_symbols:
                        continue

                    # Add to WebSocket
                    new_tokens = data_handler.subscribe([symbol])
                    if new_tokens:
                        data_handler.ticker.subscribe(new_tokens)
                        data_handler.ticker.set_mode(
                            data_handler.ticker.MODE_FULL, new_tokens
                        )

                    # Add strategies via registry — respects ENABLED_STRATEGIES
                    sentiment = t.get("sentiment_score", 0.0)
                    for strat_name in ENABLED_STRATEGIES:
                        if strat_name not in STRATEGY_CLASSES:
                            continue
                        cls = STRATEGY_CLASSES[strat_name]
                        new_strategy = cls(engine, symbol, sentiment)
                        registry.register(new_strategy)

                    subscribed_symbols.append(symbol)
                    total_subscribed += 1

                    logger.info(f"  🆕 Added to universe: {symbol} "
                               f"({t['sentiment_label']} "
                               f"{t['sentiment_score']:+.2f})")

                    # Telegram alert
                    if notifier:
                        notifier.send(
                            f"🔄 <b>Universe Refresh</b>\n"
                            f"Added {symbol} — {t['name']}\n"
                            f"Sentiment: {t['sentiment_score']:+.2f} "
                            f"{t['sentiment_label']}\n"
                            f"Sector: {t['sector']} → {t['sector_bias']}"
                        )

            except Exception as e:
                logger.error(f"🔄 Dynamic refresh error: {e}")

    t = threading.Thread(target=_monitor, daemon=True,
                        name="DynamicUniverse")
    t.start()
    logger.info("✅ Dynamic universe monitor started.")
    return t

def main():
    logger.info("=" * 60)
    logger.info("  MORNING BEETLE ENGINE — STARTING")
    logger.info(f"  Mode: {'PAPER TRADING' if IS_PAPER_TRADING else '⚠️  LIVE TRADING'}")
    logger.info(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  Strategies enabled: {ENABLED_STRATEGIES}")
    logger.info("=" * 60)

     # ── Engine lock — prevent duplicate instances ─────────────────
    if not acquire_lock():
        return
    
    # ── Kite REST client for ExitManager kill switch fallback ─────────
    from kiteconnect import KiteConnect
    api_key = os.getenv("ZERODHA_API_KEY")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN")
    if not api_key or not access_token:
        raise ValueError("ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN missing")
    kite_rest = KiteConnect(api_key=api_key)
    kite_rest.set_access_token(access_token)
    logger.info("Kite REST client initialised (for kill switch fallback).")

    # ── Telegram notifier ─────────────────────────────────────────────
    try:
        notifier = TelegramNotifier()
    except Exception as e:
        logger.warning(f"Telegram not available: {e}")
        notifier = None

    # ── Step 1: Run Morning Beetle (or load existing watchlist) ───────
    now = datetime.now().time()
    if now < dtime(9, 14):
        logger.info("Pre-market window — running Morning Beetle...")
        watchlist = run_morning_beetle()
        if notifier and watchlist:
            notifier.send_premarket_report(watchlist, is_paper=IS_PAPER_TRADING)
    else:
        logger.info("Loading existing watchlist.json...")
        watchlist = load_watchlist()

    if not watchlist:
        logger.error("Empty watchlist — engine cannot start. Run auth_test.py first.")
        return

    symbols = [t["symbol"] for t in watchlist]
    sentiment_map = {t["symbol"]: t.get("sentiment_score", 0.0)
                     for t in watchlist}

    # ── Step 2: Initialise core modules ──────────────────────────────
    engine    = TradingEngine(timeout=3.0, is_paper_trading=IS_PAPER_TRADING)
    db        = TradeDB(db_path="trades.db")
    cleanup_stale_trades(db)

    # ── Step 3: Load instruments + set up DataHandler ─────────────────
    # (Moved up: ExitManager needs data_handler.kite for live LTP queries.)
    instruments = load_instruments()
    data_handler = DataHandler(engine=engine, instrument_map=instruments)
    data_handler.subscribe(symbols)

    risk      = RiskManager(engine=engine, trade_db=db)
    risk.set_sector_cache(sector_cache, sector_cache_lock)
    exits = ExitManager(engine=engine, trade_db=db,
                    notifier=notifier, risk_manager=risk,
                    kite=kite_rest)   # ← use the REST instance
    execution = ExecutionHandler(engine=engine, trade_db=db,
                                 is_paper_trading=IS_PAPER_TRADING)

    db.log_system("INFO", "ENGINE_START",
                  f"Symbols: {symbols} | Paper: {IS_PAPER_TRADING}")

    # ── Step 3.5 (v9.5): Pre-load historical 5-min candles for warm-up ──
    # Strategy needs 30+ candles for RSI(14)+ADX(14)+Supertrend(10) to
    # become meaningful. Fetching last 30 bars from Kite REST means the
    # engine is fully armed and ready to fire signals at 09:30 AM, not
    # 10:25 AM (which would happen if we had to wait for 30 live 5-min
    # bars after market open).
    seed_by_symbol = {}
    try:
        seeder = HistoricalSeeder(kite_rest=kite_rest)
        instrument_map_for_seed = {}
        for symbol in symbols:
            if symbol in instruments:
                instrument_map_for_seed[symbol] = instruments[symbol]["instrument_token"]
            else:
                logger.warning(f"  Symbol {symbol} not in instruments — cannot seed")
        seed_by_symbol = seeder.fetch_for_symbols(
            instrument_map=instrument_map_for_seed,
            num_candles=30,
            interval_minutes=5,
        )
        seeded_count = sum(1 for v in seed_by_symbol.values() if v)
        logger.info(f"  Historical seed complete: {seeded_count}/{len(symbols)} symbols armed")
    except Exception as e:
        logger.error(f"  Historical seeding failed entirely: {e}. "
                     f"Strategies will arm via live ticks (slower).")
        seed_by_symbol = {}

    # ── Step 4: Initialise strategies via StrategyRegistry ────────────
    registry = StrategyRegistry()
    for symbol in symbols:
        sentiment = sentiment_map.get(symbol, 0.0)
        seed = seed_by_symbol.get(symbol, [])    # ← v9.5 NEW
        for strat_name in ENABLED_STRATEGIES:
            if strat_name not in STRATEGY_CLASSES:
                logger.warning(f"  Unknown strategy in ENABLED_STRATEGIES: {strat_name}")
                continue
            cls = STRATEGY_CLASSES[strat_name]
            # v9.5: pass seed_candles only to strategies that accept it.
            # RSIMomentum signature: (engine, symbol, sentiment_score, seed_candles=None)
            # Older strategies that don't accept it: gracefully degrade
            try:
                strategy = cls(engine, symbol, sentiment, seed_candles=seed)
            except TypeError:
                # Strategy doesn't accept seed_candles param (e.g. S1 Breakout)
                # → fall back to old-style instantiation
                strategy = cls(engine, symbol, sentiment)
            registry.register(strategy)

        logger.info(f"  Strategies initialised for {symbol} "
                   f"(sentiment={sentiment:+.3f}) — "
                   f"{len(ENABLED_STRATEGIES)} strategies")

    logger.info(f"  Registry total: {len(registry.all_strategies())} "
               f"strategies across {len(symbols)} symbols")

    # ── Step 5: Register EventBus handlers ───────────────────────────
    # Map signal events to their originating strategy (for risk + exit profiles)
    signal_to_strategy = {}   # signal_id -> Strategy object

    def on_market_event(event: MarketEvent):
        """Route ticks via StrategyRegistry (active-window filtered)."""
        registry.dispatch(event)
        exits.on_tick(event)

    def on_signal(event: SignalEvent):
        """
        Route signal to RiskManager with originating strategy.
        Lookup strategy by (symbol, name) — strategy_name in SignalEvent
        may differ from strategy.name due to legacy code.
        """
        # Find originating strategy
        matched_strategy = None
        for s in registry.all_strategies():
            if s.symbol != event.symbol:
                continue
            if s.name == event.strategy_name or \
               getattr(s, "strategy_name", None) == event.strategy_name:
                matched_strategy = s
                break

        # Track for fill handler
        signal_id = (event.symbol, event.timestamp)
        if matched_strategy:
            signal_to_strategy[signal_id] = matched_strategy

        # Validate with strategy context
        risk.validate(event, strategy=matched_strategy)

    def on_fill_event(event: FillEvent):
        """Register new position with ExitManager using strategy profile."""
        # Find the most recent strategy for this symbol
        matched_strategy = None
        for sig_id, s in signal_to_strategy.items():
            if sig_id[0] == event.symbol:
                matched_strategy = s

        exits.add_position(
            trade_id    = event.trade_id,
            symbol      = event.symbol,
            direction   = event.direction,
            entry_price = event.fill_price,
            quantity    = event.quantity,
            strategy    = matched_strategy   # v8 — per-strategy exit profile
        )
        logger.info(f"  Position registered with ExitManager: "
                   f"{event.direction} {event.symbol} @ {event.fill_price} "
                   f"[{matched_strategy.name if matched_strategy else 'default'}]")

        # Telegram trade alert
        if notifier:
            sentiment = matched_strategy.sentiment_score if matched_strategy else 0.0
            strategy_name = matched_strategy.name if matched_strategy else "Engine"
            notifier.send_trade_alert(
                direction  = event.direction,
                symbol     = event.symbol,
                price      = event.fill_price,
                quantity   = event.quantity,
                strategy   = strategy_name,
                sentiment  = sentiment,
                sector     = "—",
                is_paper   = IS_PAPER_TRADING
            )

    engine.register_handler("MARKET", on_market_event)
    engine.register_handler("SIGNAL", on_signal)
    engine.register_handler("ORDER",  execution.on_order)
    engine.register_handler("FILL",   on_fill_event)

    # ── Step 6: Start EventBus ────────────────────────────────────────
    engine.run_in_thread()

    # ── Step 6b: Start heatmap refresher ─────────────────────────────
    start_heatmap_refresher(interval_seconds=300)

    # ── Step 6c: Start dynamic universe monitor ───────────────────────
    start_dynamic_universe(
        engine, data_handler, instruments,
        registry, watchlist, notifier,
        sector_cache, sector_cache_lock, db
    )

    # ── Step 7: Start WebSocket ───────────────────────────────────────
    data_handler.start()
    logger.info("WebSocket streaming started.")
    logger.info(f"Watching: {symbols}")
    logger.info("Engine running. Press Ctrl+C to stop.\n")

    # ── Step 8: Main loop — runs until kill switch or Ctrl+C ──────────
    try:
        while True:
            now = datetime.now().time()

            # Kill switch check — 15:15 (only during market hours)
            from datetime import time as _dtime
            if datetime.now().time() >= _dtime(15, 15):
                logger.info("⚡ 15:15 Kill switch — stopping engine.")
                exits.force_kill_now()
                exits.wait_for_all_closed(timeout=30)
                break

            # Status heartbeat every 5 minutes
            if now.second == 0 and now.minute % 5 == 0:
                open_pos = exits.get_open_positions()
                daily_pnl = db.get_daily_pnl()
                logger.info(f"  ❤️  Heartbeat | Open: {list(open_pos.keys())} "
                           f"| Daily P&L: ₹{daily_pnl:.2f}")

            time.sleep(1)

            # 15:00 — Tighten trail stops
            if now.hour == 15 and now.minute == 0 and now.second < 2:
                logger.info("🔧 15:00 — Tightening trail stops...")
                exits.tighten_trails()

            # 15:10 — Move all SLs to breakeven
            if now.hour == 15 and now.minute == 10 and now.second < 2:
                logger.info("🔧 15:10 — Moving SLs to breakeven...")
                exits.move_to_breakeven()

            # EOD Summary at 15:30
            if now.hour == 15 and now.minute == 30 and now.second < 2:
                if notifier:
                    from sqlalchemy.orm import Session
                    from src.core.trade_db import Trade
                    with Session(db.engine) as session:
                        today_trades = session.query(Trade).filter(
                            Trade.entry_time >= datetime.now().replace(
                                hour=0, minute=0, second=0)
                        ).all()
                    notifier.send_eod_summary(
                        today_trades,
                        db.get_daily_pnl(),
                        is_paper=IS_PAPER_TRADING
                    )

    except KeyboardInterrupt:
        logger.info("\nCtrl+C received — shutting down.")

    finally:
        # v9.1 — close any open positions BEFORE engine teardown.
        # Fixes the shutdown race that left SASKEN open on Day 6 (11 May 2026).
        try:
            open_positions = exits.get_open_positions()
            if open_positions:
                logger.warning(
                    f"  Shutdown: {len(open_positions)} positions still open — "
                    f"forcing kill switch synchronously: {list(open_positions.keys())}"
                )
                exits.force_kill_now()
                exits.wait_for_all_closed(timeout=30)
        except Exception as e:
            logger.error(f"  Shutdown kill switch error: {e}")

        # Now safe to tear down the rest
        data_handler.stop()
        engine.stop()
        exits.stop()
        if notifier:
            notifier.shutdown()
        release_lock()

        # Final P&L
        daily_pnl = db.get_daily_pnl()
        open_pos  = exits.get_open_positions()

        logger.info("\n" + "=" * 60)
        logger.info("  ENGINE SHUTDOWN")
        logger.info(f"  Daily P&L    : ₹{daily_pnl:.2f}")
        logger.info(f"  Open positions: {list(open_pos.keys())}")
        logger.info("=" * 60)

        db.log_system("INFO", "ENGINE_STOP",
                      f"Daily P&L: ₹{daily_pnl:.2f}")


if __name__ == "__main__":
    main()
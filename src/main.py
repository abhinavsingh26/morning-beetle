# Morning Beetle 
import os
import sys
import json
import logging
import threading
import time
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
from src.strategies.breakout     import MorningBreakout
from src.strategies.rsi_momentum import RSIMomentum

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
                            strategies, watchlist, notifier,
                            sector_cache, sector_cache_lock):
    """
    Background thread — monitors active candidates.
    If fewer than MIN_ACTIVE_CANDIDATES remain viable,
    re-runs pipeline and subscribes fresh tickers.
    """
    from src.beetle.intelligence import run_pipeline_fresh
    from src.strategies.breakout import MorningBreakout
    from src.strategies.rsi_momentum import RSIMomentum

    subscribed_symbols = [t["symbol"] for t in watchlist]
    total_subscribed   = len(subscribed_symbols)

    def _monitor():
        nonlocal subscribed_symbols, total_subscribed

        while True:
            time.sleep(300)   # Check every 5 minutes

            now = datetime.now().time()

            # Only refresh during entry window
            if not (dtime(9, 15) <= now <= dtime(10, 15)):
                continue

            # Count viable candidates (not yet traded, not blocked)
            open_trades  = [t.symbol for t in data_handler.engine
                           .__dict__.get('_open', [])]
            active = [s for s in subscribed_symbols
                     if s not in open_trades]

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

                    # Add strategies
                    strategies[symbol] = {
                        "breakout": MorningBreakout(
                            engine, symbol, t.get("sentiment_score", 0.0)
                        ),
                        "rsi": RSIMomentum(
                            engine, symbol, t.get("sentiment_score", 0.0)
                        )
                    }

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
    logger.info("=" * 60)

     # ── Engine lock — prevent duplicate instances ─────────────────
    if not acquire_lock():
        return
    
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
    risk      = RiskManager(engine=engine, trade_db=db)
    risk.set_sector_cache(sector_cache, sector_cache_lock)
    exits     = ExitManager(engine=engine, trade_db=db, notifier=notifier)
    execution = ExecutionHandler(engine=engine, trade_db=db,
                                 is_paper_trading=IS_PAPER_TRADING)
    

    db.log_system("INFO", "ENGINE_START",
                  f"Symbols: {symbols} | Paper: {IS_PAPER_TRADING}")

    # ── Step 3: Load instruments + set up DataHandler ─────────────────
    instruments = load_instruments()
    data_handler = DataHandler(engine=engine, instrument_map=instruments)
    data_handler.subscribe(symbols)

    # ── Step 4: Initialise strategies for each symbol ─────────────────
    strategies = {}
    for symbol in symbols:
        sentiment = sentiment_map.get(symbol, 0.0)
        strategies[symbol] = {
            "breakout": MorningBreakout(engine, symbol, sentiment),
            "rsi":      RSIMomentum(engine, symbol, sentiment)
        }
        logger.info(f"  Strategies initialised for {symbol} "
                   f"(sentiment={sentiment:+.3f})")

    # ── Step 5: Register EventBus handlers ───────────────────────────
    def on_market_event(event: MarketEvent):
        """Route ticks to strategies and exit manager."""
        symbol = event.symbol
        if symbol in strategies:
            strategies[symbol]["breakout"].on_tick(event)
            strategies[symbol]["rsi"].on_tick(event)
        exits.on_tick(event)

    def on_fill_event(event: FillEvent):
        """Register new position with ExitManager."""
        exits.add_position(
            trade_id    = event.trade_id,
            symbol      = event.symbol,
            direction   = event.direction,
            entry_price = event.fill_price,
            quantity    = event.quantity
        )
        logger.info(f"  Position registered with ExitManager: "
                   f"{event.direction} {event.symbol} @ {event.fill_price}")
        
        # Telegram trade alert
        if notifier:
            notifier.send_trade_alert(
                direction  = event.direction,
                symbol     = event.symbol,
                price      = event.fill_price,
                quantity   = event.quantity,
                strategy   = "Engine",
                sentiment  = 0.0,
                sector     = "—",
                is_paper   = IS_PAPER_TRADING
            )

    engine.register_handler("MARKET", on_market_event)
    engine.register_handler("SIGNAL", risk.on_signal)
    engine.register_handler("ORDER",  execution.on_order)
    engine.register_handler("FILL",   on_fill_event)

    # ── Step 6: Start EventBus ────────────────────────────────────────
    engine.run_in_thread()

    # ── Step 6b: Start heatmap refresher ─────────────────────────────
    start_heatmap_refresher(interval_seconds=300)

    # ── Step 6c: Start dynamic universe monitor ───────────────────────
    start_dynamic_universe(
        engine, data_handler, instruments,
        strategies, watchlist, notifier,
        sector_cache, sector_cache_lock
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
            if dtime(15, 15) <= now <= dtime(15, 30):
                logger.info("⚡ 15:15 Kill switch — stopping engine.")
                db.log_system("INFO", "ENGINE_STOP", "15:15 kill switch")
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
        # ── Shutdown ─────────────────────────────────────────────────
        data_handler.stop()
        engine.stop()
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
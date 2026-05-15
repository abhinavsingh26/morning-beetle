"""
DataHandler — Kite WebSocket manager.

v9.3 (Day 10 EOD) — TICK-STREAM DIAGNOSTIC LOGGING
====================================================

Day 9 (RITES) and Day 10 (BHARTIARTL) both showed the same pattern:
  - Position opens, gets initial tick
  - 5+ hours of frozen heartbeat P&L
  - Kill switch at 15:15 needs REST fallback because last_known_ltp is stale

The kill-switch v9.2 fix made this survivable (REST quote tier saves us),
but the underlying problem — silent tick-stream halts for individual
symbols — is unobserved.

This patch adds:
  - Per-symbol tick counter (thread-safe)
  - Background reporter thread:
      * Every 60s logs "📡 Tick rates" summary
      * Warns when subscribed symbols got 0 ticks
      * Cumulative-halt tracking: increases warning urgency for prolonged halts
  - Clean shutdown (reporter thread joined on stop)

The diagnostic does NOT auto-fix anything. It gives us evidence.
Monday EOD review will tell us: when, which symbols, how long.
With that data, next week we design the actual fix.

Design notes:
  - Hot path (_on_ticks) gets ONE atomic counter increment per tick.
    No locks held during the increment to avoid impacting tick throughput.
  - Reporter runs as daemon thread; market-hours only (09:15–15:30 IST)
    to avoid noise during boot/shutdown.
"""
import os
import time
import logging
import threading
from collections import defaultdict
from datetime import datetime, time as dt_time
from kiteconnect import KiteTicker
from dotenv import load_dotenv
from src.core.events import MarketEvent

load_dotenv("config/.env")
logger = logging.getLogger(__name__)

# Reporter cadence — once per minute, low log noise
REPORT_INTERVAL_SECONDS = 60

# Tick-stream halt thresholds (consecutive 60s windows with zero ticks)
HALT_WARN_WINDOWS    = 1   # 1 minute → WARN
HALT_ESCALATE_WINDOWS = 3   # 3 minutes → ERROR-level (still won't break anything)

# Only run reporter during market hours
MARKET_OPEN_IST  = dt_time(9, 15)
MARKET_CLOSE_IST = dt_time(15, 30)


class DataHandler:
    """
    Kite WebSocket manager.
    Subscribes to instrument tokens from watchlist.
    Emits MarketEvent on every tick received.

    v9.3 adds per-symbol tick-rate diagnostic logging.
    """

    def __init__(self, engine, instrument_map: dict):
        """
        engine         — TradingEngine instance (for emit_event)
        instrument_map — dict of {symbol: instrument_data} from InstrumentMaster
        """
        self.engine          = engine
        self.instrument_map  = instrument_map
        self.ticker          = None
        self.subscribed      = {}   # token -> symbol
        self.is_connected    = False
        self._lock           = threading.Lock()

        # ── v9.3 — Tick-rate diagnostic state ──────────────────
        self._tick_counts   = defaultdict(int)  # symbol -> count this window
        self._halt_streaks  = defaultdict(int)  # symbol -> consecutive zero-windows
        self._counter_lock  = threading.Lock()
        self._reporter_stop = threading.Event()
        self._reporter_thread = None
        # ───────────────────────────────────────────────────────

        api_key      = os.getenv("ZERODHA_API_KEY")
        access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

        if not api_key or not access_token:
            raise ValueError("ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN missing")

        self.ticker = KiteTicker(api_key, access_token)
        self._register_callbacks()
        logger.info("DataHandler initialised (v9.3 with tick-rate diagnostic).")

    def _register_callbacks(self):
        """Wire Kite WebSocket callbacks."""
        self.ticker.on_connect   = self._on_connect
        self.ticker.on_ticks     = self._on_ticks
        self.ticker.on_close     = self._on_close
        self.ticker.on_error     = self._on_error
        self.ticker.on_reconnect = self._on_reconnect

    def subscribe(self, symbols: list):
        """
        Subscribe to a list of ticker symbols.
        Looks up instrument tokens from instrument_map.
        """
        tokens = []
        for symbol in symbols:
            if symbol in self.instrument_map:
                token = self.instrument_map[symbol]["instrument_token"]
                self.subscribed[token] = symbol
                tokens.append(token)
                logger.info(f"  Subscribed: {symbol} (token={token})")
            else:
                logger.warning(f"  Symbol not found in instrument map: {symbol}")
        return tokens

    def _on_connect(self, ws, response):
        """Called when WebSocket connects."""
        self.is_connected = True
        tokens = list(self.subscribed.keys())
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info(f"WebSocket connected. Subscribed to {len(tokens)} tokens.")
        else:
            logger.warning("WebSocket connected but no tokens to subscribe.")

    def _on_ticks(self, ws, ticks):
        """
        Called on every tick. Creates MarketEvent and pushes to EventBus.
        Hot path — keep it fast.

        v9.3: increment per-symbol tick counter. Lock is short and contention
        is one-writer-many-readers (reporter only reads at 60s cadence), so
        impact on throughput is negligible.
        """
        for tick in ticks:
            token  = tick.get("instrument_token", 0)
            symbol = self.subscribed.get(token, "UNKNOWN")

            # v9.3 — counter increment (short critical section)
            with self._counter_lock:
                self._tick_counts[symbol] += 1

            event = MarketEvent(
                symbol           = symbol,
                instrument_token = token,
                ltp              = tick.get("last_price", 0.0),
                open             = tick.get("ohlc", {}).get("open", 0.0),
                high             = tick.get("ohlc", {}).get("high", 0.0),
                low              = tick.get("ohlc", {}).get("low", 0.0),
                close            = tick.get("ohlc", {}).get("close", 0.0),
                volume           = tick.get("volume", 0),
                change           = tick.get("change", 0.0)
            )
            self.engine.emit_event(event)

    def _on_close(self, ws, code, reason):
        self.is_connected = False
        logger.warning(f"WebSocket closed: {code} — {reason}")

    def _on_error(self, ws, code, reason):
        logger.error(f"WebSocket error: {code} — {reason}")

    def _on_reconnect(self, ws, attempts_count):
        logger.info(f"WebSocket reconnecting... attempt {attempts_count}")

    # ── v9.3 — Reporter Thread ─────────────────────────────────

    def _is_market_hours(self) -> bool:
        """True during NSE intraday window."""
        now = datetime.now().time()
        return MARKET_OPEN_IST <= now <= MARKET_CLOSE_IST

    def _drain_counters(self) -> dict:
        """Atomically read + zero the per-symbol counters."""
        with self._counter_lock:
            snapshot = dict(self._tick_counts)
            self._tick_counts.clear()
        return snapshot

    def _format_rate_line(self, counts: dict) -> str:
        """Render rate summary, sorted by symbol for readable output."""
        if not counts:
            return "(no ticks received)"
        # Sort by symbol; include 0-counts so we can SEE the halt
        ordered = sorted(counts.items())
        return "  ".join(f"{sym}={n}" for sym, n in ordered)

    def _check_for_halts(self, counts: dict) -> None:
        """
        Detect symbols with zero ticks this window.
        Maintain a per-symbol consecutive-halt streak.
        Escalate log level on prolonged halts.
        """
        subscribed_symbols = set(self.subscribed.values())
        for symbol in subscribed_symbols:
            count = counts.get(symbol, 0)
            if count == 0:
                self._halt_streaks[symbol] += 1
                streak = self._halt_streaks[symbol]
                if streak >= HALT_ESCALATE_WINDOWS:
                    logger.error(
                        f"  ⚠️  TICK HALT: {symbol} received 0 ticks for "
                        f"{streak} consecutive 60s windows "
                        f"(~{streak} min). Possible WebSocket subscription drop."
                    )
                elif streak >= HALT_WARN_WINDOWS:
                    logger.warning(
                        f"  ⚠️  TICK HALT: {symbol} received 0 ticks in last 60s "
                        f"(streak: {streak})"
                    )
            else:
                # Reset streak on any tick activity
                if self._halt_streaks[symbol] > 0:
                    logger.info(
                        f"  ✅ TICK RECOVERY: {symbol} resumed "
                        f"({count} ticks in 60s after "
                        f"{self._halt_streaks[symbol]}-window halt)"
                    )
                self._halt_streaks[symbol] = 0

    def _reporter_loop(self):
        """
        Reports per-symbol tick rates every 60s during market hours.
        Daemon thread; gracefully exits when _reporter_stop event is set.
        """
        logger.info("📡 Tick-rate reporter started (60s cadence, market hours).")
        # Align first report ~60s after start; wait in 1s chunks so stop is responsive
        for _ in range(REPORT_INTERVAL_SECONDS):
            if self._reporter_stop.is_set():
                logger.info("📡 Tick-rate reporter exiting (pre-market stop).")
                return
            time.sleep(1)

        while not self._reporter_stop.is_set():
            try:
                counts = self._drain_counters()
                if self._is_market_hours() and self.subscribed:
                    rates = self._format_rate_line(counts)
                    logger.info(f"  📡 Tick rates (last 60s): {rates}")
                    self._check_for_halts(counts)
                # else: pre/post-market silence — no log noise
            except Exception as e:
                logger.error(f"Tick-rate reporter error: {e}", exc_info=True)

            # Wait next 60s in 1s chunks so we respond promptly to stop()
            for _ in range(REPORT_INTERVAL_SECONDS):
                if self._reporter_stop.is_set():
                    break
                time.sleep(1)

        logger.info("📡 Tick-rate reporter stopped.")

    # ───────────────────────────────────────────────────────────

    def start(self):
        """Start WebSocket in background thread + tick-rate reporter."""
        logger.info("Starting Kite WebSocket...")
        self.ticker.connect(threaded=True)
        # v9.3 — start reporter thread
        self._reporter_stop.clear()
        self._reporter_thread = threading.Thread(
            target=self._reporter_loop,
            name="TickRateReporter",
            daemon=True
        )
        self._reporter_thread.start()

    def stop(self):
        """Stop WebSocket + reporter thread cleanly."""
        # v9.3 — signal reporter to exit
        self._reporter_stop.set()
        if self._reporter_thread and self._reporter_thread.is_alive():
            self._reporter_thread.join(timeout=2.0)
            if self._reporter_thread.is_alive():
                logger.warning("Tick-rate reporter did not exit within 2s.")
        if self.ticker:
            self.ticker.close()
            self.is_connected = False
            logger.info("WebSocket stopped.")


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(levelname)s — %(message)s")

    from src.core.engine import TradingEngine
    from src.beetle.instrument_master import load_instruments

    # Load watchlist
    try:
        with open("watchlist.json") as f:
            watchlist = json.load(f)
        symbols = [t["symbol"] for t in watchlist["tickers"]]
    except Exception as e:
        logger.warning(f"Could not load watchlist.json: {e}")
        symbols = ["INFY", "RELIANCE"]

    logger.info(f"Watchlist symbols: {symbols}")

    # Set up engine
    engine = TradingEngine(is_paper_trading=True)

    tick_count = 0

    def on_market_event(event: MarketEvent):
        global tick_count
        tick_count += 1
        if tick_count <= 10:
            logger.info(f"  TICK [{tick_count}] {event.symbol} "
                       f"LTP={event.ltp} H={event.high} L={event.low} V={event.volume}")

    engine.register_handler("MARKET", on_market_event)
    engine.run_in_thread()

    # Load instruments and start DataHandler
    instruments = load_instruments()
    handler = DataHandler(engine=engine, instrument_map=instruments)
    handler.subscribe(symbols)
    handler.start()

    # Run for 130 seconds so we see at least two reporter cycles
    print(f"\n⏳ Streaming ticks for 130s — watch for 'Tick rates' lines every 60s...\n")
    time.sleep(130)

    handler.stop()
    engine.stop()
    print(f"\n── DataHandler Test Results ──")
    print(f"  Ticks received: {tick_count}")
    if tick_count > 0:
        print(f"✅ WebSocket streaming working.")
    else:
        print(f"⚠️  No ticks received — market may be closed or token expired.")
        print(f"   This is expected outside market hours (09:15–15:30).")
        print(f"   Re-run during market hours to verify live ticks.")
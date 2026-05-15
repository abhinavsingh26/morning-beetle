import logging
import threading
import time as time_module
from datetime import datetime, time
from src.core.events import MarketEvent

logger = logging.getLogger(__name__)

# Default exit parameters (fallback if strategy not provided)
DEFAULT_SL_PCT     = 0.008
DEFAULT_TARGET_PCT = 0.015
DEFAULT_TRAIL_PCT  = 0.005

PARTIAL_EXIT       = 0.5
KILL_SWITCH        = time(15, 15)        # Force-close all at 15:15
KILL_TIMER_INTERVAL = 5                  # Background timer check interval (seconds)
KILL_TIMER_START    = time(15, 14, 50)   # Start active polling 10s before kill

# Per-strategy trail percentages (Blueprint v8)
STRATEGY_TRAIL_PCT = {
    "morning_breakout":    0.005,
    "rsi_momentum":        0.005,
    "sector_pullback":     0.003,
    "vol_contraction":     0.004,
    "vol_spike_sentiment": 0.004,
}

SENTIMENT_REVERSAL_THRESHOLD = 0.3


class Position:
    """
    Tracks a single open position.
    v9: stores last_known_ltp for tick-less kill switch.
    """

    def __init__(self, trade_id: int, symbol: str, direction: str,
                 entry_price: float, quantity: int,
                 sentiment_score: float,
                 strategy_name: str = "default",
                 sl_pct: float = DEFAULT_SL_PCT,
                 target_pct: float = DEFAULT_TARGET_PCT,
                 trail_pct: float = DEFAULT_TRAIL_PCT,
                 loss_bucket: str = "morning"):
        self.trade_id        = trade_id
        self.symbol          = symbol
        self.direction       = direction
        self.entry_price     = entry_price
        self.quantity        = quantity
        self.remaining_qty   = quantity
        self.sentiment_score = sentiment_score

        self.strategy_name   = strategy_name
        self.sl_pct          = sl_pct
        self.target_pct      = target_pct
        self.trail_pct       = trail_pct
        self.loss_bucket     = loss_bucket

        if direction == "BUY":
            self.sl_price     = round(entry_price * (1 - sl_pct), 2)
            self.target_price = round(entry_price * (1 + target_pct), 2)
        else:
            self.sl_price     = round(entry_price * (1 + sl_pct), 2)
            self.target_price = round(entry_price * (1 - target_pct), 2)

        self.peak_price       = entry_price
        self.last_known_ltp   = entry_price   # v9 — fallback for kill switch
        self.last_tick_at     = datetime.now()
        self.trail_active     = False
        self.trail_sl         = None
        self.partial_exited   = False

        logger.info(f"  Position opened: {direction} {symbol} @ {entry_price:.2f} "
                   f"[{strategy_name}] SL={self.sl_price:.2f} "
                   f"Target={self.target_price:.2f} "
                   f"(SL={sl_pct*100:.1f}% Target={target_pct*100:.1f}%)")


class ExitManager:
    """
    Monitors all open positions and triggers exits.

    v9.1 Exit types:
    1. Stop Loss        — per-strategy SL%
    2. Target           — per-strategy target%, exit 50%, trail rest
    3. Trailing SL      — per-strategy trail%
    4. Sentiment        — FinBERT re-scan flips bias
    5. Kill Switch      — 15:15 IST force-close (event-driven via on_tick)
    6. Time-Based Kill  — 15:15 IST force-close (background timer, fires
                          even when ticks stop — uses last_known_ltp or
                          REST API quote as fallback)
    7. Synchronous Kill — force_kill_now() called from main shutdown to
                          guarantee close-before-shutdown (NEW v9.1) ★
    8. Daily Loss       — handled by RiskManager STOP_ALL
    9. Bucket Halt      — RiskManager halts bucket on loss limit
    """

    def __init__(self, engine, trade_db, notifier=None,
                 risk_manager=None, kite=None):
        self.engine       = engine
        self.trade_db     = trade_db
        self.notifier     = notifier
        self.risk_manager = risk_manager
        self.kite         = kite
        self.positions    = {}
        self._lock        = threading.Lock()
        self.kill_fired   = False

        # v9 — background time-based kill switch thread
        self._stop_event  = threading.Event()
        self._kill_thread = threading.Thread(
            target=self._kill_switch_timer,
            daemon=True,
            name="KillSwitchTimer"
        )
        self._kill_thread.start()

        logger.info("ExitManager initialised (with time-based kill switch).")

    # ── v9 Time-Based Kill Switch Timer ──────────────────────────────
    def _kill_switch_timer(self):
        """
        Background thread that polls every 5s.
        Fires kill switch at 15:15 even if no ticks are arriving.
        """
        logger.info("Kill switch timer thread started.")
        while not self._stop_event.is_set():
            try:
                now = datetime.now().time()

                if now >= KILL_TIMER_START and now < KILL_SWITCH:
                    time_module.sleep(0.5)
                    continue

                if now >= KILL_SWITCH and not self.kill_fired:
                    self._fire_time_based_kill_switch()
                    return

                time_module.sleep(KILL_TIMER_INTERVAL)

            except Exception as e:
                logger.error(f"Kill switch timer error: {e}")
                time_module.sleep(KILL_TIMER_INTERVAL)

        logger.info("Kill switch timer thread stopped.")

    def _fire_time_based_kill_switch(self):
        """
        Force-close all open positions using fallback price strategy:
        1. Try fresh quote from Kite REST API (most accurate).
        2. Fall back to last_known_ltp from position.
        3. Final fallback: entry_price (zero P&L recorded).
        """
        if self.kill_fired:
            return

        self.kill_fired = True

        with self._lock:
            symbols = list(self.positions.keys())

        if not symbols:
            logger.info("⚡ KILL SWITCH (timer): no open positions to close.")
            self.trade_db.log_system(
                "INFO", "KILL_SWITCH_TIMER",
                "Kill switch fired at 15:15 — no open positions"
            )
            return

        logger.warning(f"⚡ KILL SWITCH (timer-based): closing "
                      f"{len(symbols)} positions at 15:15")
        self.trade_db.log_system(
            "WARNING", "KILL_SWITCH_TIMER",
            f"Time-based kill switch firing for {len(symbols)} positions"
        )

        rest_prices = self._fetch_rest_prices(symbols)

        for symbol in symbols:
            with self._lock:
                pos = self.positions.get(symbol)
            if not pos:
                continue

            if rest_prices.get(symbol):
                exit_price = rest_prices[symbol]
                source = "REST"
            elif pos.last_known_ltp:
                exit_price = pos.last_known_ltp
                source = "last_tick"
            else:
                exit_price = pos.entry_price
                source = "entry_fallback"

            # ── v9.2 SANITY GUARD (Day 9 bug protection) ──────────────
            # If exit_price differs from entry by more than 8%, the price
            # is almost certainly a stale tick from a different symbol
            # leaking through. Refuse the booking and fall back to
            # entry_price (records zero P&L instead of garbage).
            #
            # Real intraday moves on stable stocks rarely exceed 8% in a
            # single session. This is conservative — even circuit-limit
            # stocks (5%/20%) won't trigger false positives because the
            # broker's MIS would have force-squared earlier on margin.
            if exit_price and pos.entry_price:
                drift_pct = abs(exit_price - pos.entry_price) / pos.entry_price
                if drift_pct > 0.08:
                    logger.error(
                        f"  ⚠️  SANITY GUARD: {symbol} exit_price {exit_price:.2f} "
                        f"differs from entry {pos.entry_price:.2f} by "
                        f"{drift_pct*100:.1f}% via {source} — refusing booking, "
                        f"using entry_price (zero P&L recorded)"
                    )
                    self.trade_db.log_system(
                        "ERROR", "KILL_SWITCH_SANITY_GUARD",
                        f"{symbol} exit drift {drift_pct*100:.1f}% via {source}; "
                        f"refused {exit_price:.2f}, used entry {pos.entry_price:.2f}"
                    )
                    exit_price = pos.entry_price
                    source = "sanity_guard_entry"
            # ─────────────────────────────────────────────────────────

            logger.warning(f"  ⚡ Force-closing {symbol} @ {exit_price:.2f} "
                          f"(source: {source})")
            self._close_position(symbol, exit_price, "KILL_SWITCH_TIMER")

    # ── v9.1 Synchronous shutdown kill switch ★ NEW ──────────────────
    def force_kill_now(self):
        """
        Synchronously fire kill switch on demand. Called by main shutdown
        handler at 15:15 to guarantee positions close BEFORE engine teardown.

        This is the fix for the shutdown race condition: previously the
        timer thread could be racing with main.py's shutdown sequence and
        lose. Now main calls this synchronously and waits for completion.

        Safe to call even if positions are already closed (no-op).
        Safe to call multiple times (idempotent via kill_fired flag).
        """
        with self._lock:
            symbols = list(self.positions.keys())

        if not symbols:
            logger.info("  force_kill_now(): no open positions, nothing to do.")
            return

        logger.warning(f"  🔒 SYNCHRONOUS KILL: closing {len(symbols)} positions "
                      f"before shutdown")
        self.trade_db.log_system(
            "WARNING", "KILL_SWITCH_SYNC",
            f"Synchronous kill from shutdown handler: {symbols}"
        )

        # Reuse the same fire logic (handles REST + fallback)
        self._fire_time_based_kill_switch()

    def wait_for_all_closed(self, timeout: int = 30) -> bool:
        """
        Block until all positions are closed, or timeout.
        Returns True if all closed, False if timeout hit with positions still open.

        Called from main shutdown after force_kill_now() to ensure DB writes
        complete before engine tear-down.
        """
        start = time_module.time()
        while True:
            with self._lock:
                still_open = list(self.positions.keys())

            if not still_open:
                logger.info("  ✅ All positions closed.")
                return True

            elapsed = time_module.time() - start
            if elapsed >= timeout:
                logger.error(f"  ⚠️  wait_for_all_closed timeout: "
                            f"{len(still_open)} positions still open: {still_open}")
                self.trade_db.log_system(
                    "ERROR", "SHUTDOWN_TIMEOUT",
                    f"Positions still open after {timeout}s timeout: {still_open}"
                )
                return False

            time_module.sleep(0.5)

    def _fetch_rest_prices(self, symbols: list) -> dict:
        """Fetch live quotes via Kite REST API."""
        if self.kite is None:
            logger.warning("  No Kite instance — skipping REST quote fallback")
            return {}

        try:
            kite_keys = [f"NSE:{s}" for s in symbols]
            quotes = self.kite.quote(kite_keys)
            result = {}
            for s in symbols:
                key = f"NSE:{s}"
                if key in quotes and "last_price" in quotes[key]:
                    result[s] = quotes[key]["last_price"]
            logger.info(f"  REST quote fetch: {len(result)}/{len(symbols)} succeeded")
            return result
        except Exception as e:
            logger.warning(f"  REST quote fetch failed: {e}")
            return {}

    def stop(self):
        """Signal the kill switch timer thread to exit."""
        self._stop_event.set()
        logger.info("ExitManager stop signal sent.")

    # ── Position management ──────────────────────────────────────────
    def add_position(self, trade_id: int, symbol: str, direction: str,
                     entry_price: float, quantity: int,
                     sentiment_score: float = 0.0,
                     strategy=None):
        """Register a new open position."""
        if strategy is not None:
            strategy_name = getattr(strategy, "name", "default")
            sl_pct        = getattr(strategy, "sl_pct", DEFAULT_SL_PCT)
            target_pct    = getattr(strategy, "target_pct", DEFAULT_TARGET_PCT)
            trail_pct     = STRATEGY_TRAIL_PCT.get(strategy_name, DEFAULT_TRAIL_PCT)
            loss_bucket   = getattr(strategy, "loss_bucket", "morning")
        else:
            strategy_name = "default"
            sl_pct        = DEFAULT_SL_PCT
            target_pct    = DEFAULT_TARGET_PCT
            trail_pct     = DEFAULT_TRAIL_PCT
            loss_bucket   = "morning"

        with self._lock:
            self.positions[symbol] = Position(
                trade_id=trade_id, symbol=symbol, direction=direction,
                entry_price=entry_price, quantity=quantity,
                sentiment_score=sentiment_score,
                strategy_name=strategy_name,
                sl_pct=sl_pct, target_pct=target_pct,
                trail_pct=trail_pct, loss_bucket=loss_bucket
            )

    def _close_position(self, symbol: str, exit_price: float,
                        reason: str) -> float:
        """Close position, log to DB, return P&L."""
        pos = self.positions.pop(symbol, None)
        if not pos:
            return 0.0
        pnl = self.trade_db.close_trade(pos.trade_id, exit_price, reason)
        logger.info(f"  🔴 EXIT {reason}: {pos.direction} {symbol} "
                   f"[{pos.strategy_name}] @ {exit_price:.2f} | P&L: ₹{pnl:.2f}")

        if self.risk_manager is not None:
            try:
                self.risk_manager.update_bucket_loss(pos.loss_bucket, pnl)
            except Exception as e:
                logger.warning(f"  Bucket loss update failed: {e}")

        if self.notifier:
            try:
                self.notifier.send_exit_alert(
                    symbol=symbol, exit_price=exit_price,
                    reason=reason, pnl=pnl, is_paper=True
                )
            except Exception as e:
                logger.warning(f"  Telegram exit alert failed: {e}")

        return pnl

    def tighten_trails(self):
        """At 15:00 — tighten trail stop to 0.2%."""
        with self._lock:
            for symbol, pos in self.positions.items():
                if pos.trail_active and pos.trail_sl:
                    if pos.direction == "BUY":
                        new_trail = round(pos.peak_price * (1 - 0.002), 2)
                        pos.trail_sl = max(pos.trail_sl, new_trail)
                    else:
                        new_trail = round(pos.peak_price * (1 + 0.002), 2)
                        pos.trail_sl = min(pos.trail_sl, new_trail)
                    logger.info(f"  🔧 Trail tightened: {symbol} → {pos.trail_sl:.2f}")

    def move_to_breakeven(self):
        """At 15:10 — move all SLs to breakeven."""
        with self._lock:
            for symbol, pos in self.positions.items():
                if pos.direction == "BUY":
                    if pos.entry_price > pos.sl_price:
                        pos.sl_price = pos.entry_price
                        logger.info(f"  🔧 SL moved to breakeven: {symbol} @ {pos.entry_price:.2f}")
                else:
                    if pos.entry_price < pos.sl_price:
                        pos.sl_price = pos.entry_price
                        logger.info(f"  🔧 SL moved to breakeven: {symbol} @ {pos.entry_price:.2f}")

    def on_tick(self, event: MarketEvent):
        """Called on every MarketEvent. Evaluates all exit conditions."""
        symbol = event.symbol
        ltp    = event.ltp

        with self._lock:
            if symbol not in self.positions:
                return
            pos = self.positions[symbol]
            pos.last_known_ltp = ltp
            pos.last_tick_at   = datetime.now()

        if self._check_kill_switch(ltp):
            return

        if pos.direction == "BUY":
            pos.peak_price = max(pos.peak_price, ltp)
        else:
            pos.peak_price = min(pos.peak_price, ltp)

        if self._check_stop_loss(pos, ltp):
            return
        if self._check_target(pos, ltp):
            return
        if self._check_trail(pos, ltp):
            return

    def _check_stop_loss(self, pos: Position, ltp: float) -> bool:
        hit = (pos.direction == "BUY"  and ltp <= pos.sl_price) or \
              (pos.direction == "SELL" and ltp >= pos.sl_price)
        if hit:
            self._close_position(pos.symbol, ltp, "SL")
            return True
        return False

    def _check_target(self, pos: Position, ltp: float) -> bool:
        if pos.partial_exited:
            return False
        hit = (pos.direction == "BUY"  and ltp >= pos.target_price) or \
              (pos.direction == "SELL" and ltp <= pos.target_price)
        if hit:
            pos.partial_exited = True
            pos.trail_active   = True
            if pos.direction == "BUY":
                pos.trail_sl = round(ltp * (1 - pos.trail_pct), 2)
            else:
                pos.trail_sl = round(ltp * (1 + pos.trail_pct), 2)
            logger.info(f"  🎯 TARGET HIT: {pos.symbol} @ {ltp:.2f} "
                       f"[{pos.strategy_name}] — 50% exited, "
                       f"trail SL set @ {pos.trail_sl:.2f} "
                       f"(trail={pos.trail_pct*100:.1f}%)")
            self.trade_db.log_system(
                "INFO", "PARTIAL_EXIT",
                f"{pos.symbol} target hit @ {ltp:.2f}, trail activated"
            )
        return False

    def _check_trail(self, pos: Position, ltp: float) -> bool:
        if not pos.trail_active or pos.trail_sl is None:
            return False
        if pos.direction == "BUY":
            new_trail = round(ltp * (1 - pos.trail_pct), 2)
            pos.trail_sl = max(pos.trail_sl, new_trail)
            hit = ltp <= pos.trail_sl
        else:
            new_trail = round(ltp * (1 + pos.trail_pct), 2)
            pos.trail_sl = min(pos.trail_sl, new_trail)
            hit = ltp >= pos.trail_sl
        if hit:
            self._close_position(pos.symbol, ltp, "TRAIL")
            return True
        return False

    def _check_kill_switch(self, ltp: float) -> bool:
        """
        Event-driven kill switch (only fires if ticks are flowing).
        Time-based fallback handled by _kill_switch_timer thread.

        v9.2 FIX (Day 9 bug): previously called _close_position(symbol, ltp,...)
        in a loop using the SINGLE ltp from the triggering tick. That caused
        all open positions to be booked at the same (wrong) price.
        Example: at 15:15:00.161 a ZYDUSLIFE tick @ 991.90 fired the kill
        switch, and RITES was also closed at 991.90 (real price ~213.85)
        — bogus +₹54,588 P&L.

        Fix: delegate to _fire_time_based_kill_switch(), which already does
        the correct per-position price resolution (REST quote → last_known_ltp
        → entry_price fallback chain), AND adds a sanity guard against
        wildly-off prices.
        """
        now = datetime.now().time()
        if now >= KILL_SWITCH and not self.kill_fired:
            with self._lock:
                symbols = list(self.positions.keys())
            logger.warning(f"⚡ KILL SWITCH (event-driven): closing {len(symbols)} positions")
            self.trade_db.log_system(
                "WARNING", "KILL_SWITCH",
                f"Event-driven kill switch firing at 15:15"
            )
            # Reuse the per-position fallback logic. It sets kill_fired internally.
            self._fire_time_based_kill_switch()
            return True
        return False

    def check_sentiment_reversal(self, symbol: str,
                                  new_sentiment: float) -> bool:
        with self._lock:
            if symbol not in self.positions:
                return False
            pos = self.positions[symbol]
        original = pos.sentiment_score
        flipped  = (original > 0 and new_sentiment < -SENTIMENT_REVERSAL_THRESHOLD) or \
                   (original < 0 and new_sentiment >  SENTIMENT_REVERSAL_THRESHOLD)
        if flipped:
            ltp = pos.last_known_ltp or pos.entry_price
            logger.info(f"  🔄 SENTIMENT REVERSAL: {symbol} "
                       f"score {original:+.2f} → {new_sentiment:+.2f}")
            self._close_position(symbol, ltp, "SENTIMENT_REVERSAL")
            return True
        return False

    def get_open_positions(self) -> dict:
        with self._lock:
            return dict(self.positions)
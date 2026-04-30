import logging
import threading
from datetime import datetime, time
from src.core.events import MarketEvent

logger = logging.getLogger(__name__)

# Default exit parameters (used as fallback if strategy not provided)
DEFAULT_SL_PCT     = 0.008    # 0.8% stop loss
DEFAULT_TARGET_PCT = 0.015    # 1.5% target
DEFAULT_TRAIL_PCT  = 0.005    # 0.5% trailing stop from peak

PARTIAL_EXIT       = 0.5      # Exit 50% at target, trail remainder
KILL_SWITCH        = time(15, 15)   # Force-close all at 15:15

# Per-strategy trail percentages (Blueprint v8)
STRATEGY_TRAIL_PCT = {
    "morning_breakout":    0.005,   # 0.5%
    "rsi_momentum":        0.005,   # 0.5%
    "sector_pullback":     0.003,   # 0.3% aggressive
    "vol_contraction":     0.004,   # 0.4%
    "vol_spike_sentiment": 0.004,   # 0.4%
}

# Sentiment reversal threshold
SENTIMENT_REVERSAL_THRESHOLD = 0.3


class Position:
    """
    Tracks a single open position.
    v8: SL/Target/Trail percentages now per-strategy.
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

        # v8 — per-strategy exit params
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

        self.peak_price     = entry_price
        self.trail_active   = False
        self.trail_sl       = None
        self.partial_exited = False

        logger.info(f"  Position opened: {direction} {symbol} @ {entry_price:.2f} "
                   f"[{strategy_name}] SL={self.sl_price:.2f} "
                   f"Target={self.target_price:.2f} "
                   f"(SL={sl_pct*100:.1f}% Target={target_pct*100:.1f}%)")


class ExitManager:
    """
    Monitors all open positions and triggers exits.

    v8 Exit types per Blueprint:
    1. Stop Loss        — per-strategy SL%
    2. Target           — per-strategy target%, exit 50%, trail rest
    3. Trailing SL      — per-strategy trail%
    4. Sentiment        — FinBERT re-scan flips bias
    5. Kill Switch      — 15:15 IST force-close all
    6. Daily Loss       — handled by RiskManager STOP_ALL
    7. Bucket Halt      — RiskManager halts bucket on loss limit
    """

    def __init__(self, engine, trade_db, notifier=None,
                 risk_manager=None):
        self.engine       = engine
        self.trade_db     = trade_db
        self.notifier     = notifier
        self.risk_manager = risk_manager
        self.positions    = {}
        self._lock        = threading.Lock()
        self.kill_fired   = False
        logger.info("ExitManager initialised.")

    def add_position(self, trade_id: int, symbol: str, direction: str,
                     entry_price: float, quantity: int,
                     sentiment_score: float = 0.0,
                     strategy=None):
        """
        Register a new open position to monitor.
        v8: accepts optional strategy object for per-strategy exit profile.
        """
        if strategy is not None:
            strategy_name = getattr(strategy, "name", "default")
            sl_pct        = getattr(strategy, "sl_pct", DEFAULT_SL_PCT)
            target_pct    = getattr(strategy, "target_pct", DEFAULT_TARGET_PCT)
            trail_pct     = STRATEGY_TRAIL_PCT.get(
                strategy_name, DEFAULT_TRAIL_PCT
            )
            loss_bucket   = getattr(strategy, "loss_bucket", "morning")
        else:
            strategy_name = "default"
            sl_pct        = DEFAULT_SL_PCT
            target_pct    = DEFAULT_TARGET_PCT
            trail_pct     = DEFAULT_TRAIL_PCT
            loss_bucket   = "morning"

        with self._lock:
            self.positions[symbol] = Position(
                trade_id=trade_id,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                sentiment_score=sentiment_score,
                strategy_name=strategy_name,
                sl_pct=sl_pct,
                target_pct=target_pct,
                trail_pct=trail_pct,
                loss_bucket=loss_bucket
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

        # v8 — Report P&L to RiskManager bucket tracker
        if self.risk_manager is not None:
            try:
                self.risk_manager.update_bucket_loss(pos.loss_bucket, pnl)
            except Exception as e:
                logger.warning(f"  Bucket loss update failed: {e}")

        # Send Telegram exit alert
        if self.notifier:
            try:
                self.notifier.send_exit_alert(
                    symbol     = symbol,
                    exit_price = exit_price,
                    reason     = reason,
                    pnl        = pnl,
                    is_paper   = True
                )
            except Exception as e:
                logger.warning(f"  Telegram exit alert failed: {e}")

        return pnl

    def tighten_trails(self):
        """At 15:00 — tighten trail stop to 0.2% to lock more profit."""
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
        """At 15:10 — move all SLs to breakeven to protect capital."""
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
            # v8 — use strategy's trail_pct
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
        # v8 — use strategy's trail_pct
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
        now = datetime.now().time()
        if now >= KILL_SWITCH and not self.kill_fired:
            self.kill_fired = True
            symbols = list(self.positions.keys())
            logger.warning(f"⚡ KILL SWITCH: closing {len(symbols)} positions")
            self.trade_db.log_system("WARNING", "KILL_SWITCH",
                                     f"Force-closing {len(symbols)} positions at 15:15")
            for symbol in symbols:
                self._close_position(symbol, ltp, "KILL_SWITCH")
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
            ltp = 0.0
            logger.info(f"  🔄 SENTIMENT REVERSAL: {symbol} "
                       f"score {original:+.2f} → {new_sentiment:+.2f}")
            self._close_position(symbol, ltp, "SENTIMENT_REVERSAL")
            return True
        return False

    def get_open_positions(self) -> dict:
        with self._lock:
            return dict(self.positions)
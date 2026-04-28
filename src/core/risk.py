import os
import logging
import threading
from datetime import datetime, time
from dotenv import load_dotenv
from src.core.events import SignalEvent, OrderEvent

load_dotenv("config/.env")
logger = logging.getLogger(__name__)

# Risk parameters per Blueprint
DAILY_LOSS_LIMIT  = -2000.0   # ₹2,000 max daily loss
ENTRY_CUTOFF      = time(10, 30)   # No new positions after 10:30 AM
COOLDOWN_SECONDS  = 1.0            # Min 1s between consecutive orders
MAX_SIMULTANEOUS_POSITIONS = 3   # Never more than 3 open at once

CAPITAL_PER_TRADE_PCT = 0.30   # 30% of capital per trade

# Sentiment gate thresholds per Blueprint
SENTIMENT_BLOCK_BULL = 0.4    # Block SELL if FinBERT > +0.4
SENTIMENT_BLOCK_BEAR = -0.4   # Block BUY  if FinBERT < -0.4


class RiskManager:
    """
    Validates every SignalEvent before allowing order placement.

    Checks (in order):
    1. Daily loss limit not breached
    2. No duplicate open position in same ticker
    3. Current time < 10:30 AM entry cutoff
    4. 1 second cooldown between orders
    5. Sentiment gate — signal must align with FinBERT bias

    Returns APPROVED or BLOCKED with reason.
    """

    def __init__(self, engine, trade_db):
        self.engine      = engine
        self.trade_db    = trade_db
        self._lock       = threading.Lock()
        self._last_order_time = None
        self.stop_all    = False 
        self._bypass_time_gate = False  # Fires when daily loss limit hit

        logger.info("RiskManager initialised.")

    def _check_daily_loss(self) -> tuple[bool, str]:
        """Block if daily P&L < -₹2,000."""
        if self.stop_all:
            return False, "STOP_ALL active — daily loss limit previously breached"
        daily_pnl = self.trade_db.get_daily_pnl()
        if daily_pnl <= DAILY_LOSS_LIMIT:
            self.stop_all = True
            logger.warning(f"🛑 STOP_ALL: Daily P&L ₹{daily_pnl:.2f} ≤ limit ₹{DAILY_LOSS_LIMIT:.2f}")
            self.trade_db.log_system(
                "WARNING", "STOP_ALL",
                f"Daily loss limit breached: ₹{daily_pnl:.2f}"
            )
            return False, f"Daily loss limit breached: ₹{daily_pnl:.2f}"
        return True, ""
    
    def _check_max_positions(self) -> tuple[bool, str]:
        """Block if already at maximum simultaneous positions."""
        open_trades = self.trade_db.get_open_trades()
        if len(open_trades) >= MAX_SIMULTANEOUS_POSITIONS:
            return False, f"Max positions reached: {len(open_trades)}/{MAX_SIMULTANEOUS_POSITIONS}"
        return True, ""

    def _check_duplicate_position(self, symbol: str) -> tuple[bool, str]:
        """Block if already have open position in this ticker."""
        open_trades = self.trade_db.get_open_trades()
        for trade in open_trades:
            if trade.symbol == symbol:
                return False, f"Duplicate position: {symbol} already open"
        return True, ""

    def _check_time_gate(self) -> tuple[bool, str]:
        """Block new entries after 10:30 AM."""
        if self._bypass_time_gate:
            return True, ""
        now = datetime.now().time()
        if now >= ENTRY_CUTOFF:
            return False, f"Time gate: past entry cutoff {ENTRY_CUTOFF}"
        return True, ""

    def _check_cooldown(self) -> tuple[bool, str]:
        """Block if last order was less than 1 second ago."""
        if self._last_order_time is None:
            return True, ""
        elapsed = (datetime.now() - self._last_order_time).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            return False, f"Cooldown: {elapsed:.2f}s since last order (min {COOLDOWN_SECONDS}s)"
        return True, ""

    def _check_sentiment_gate(self, signal: SignalEvent) -> tuple[bool, str]:
        """
        Block signal if it contradicts strong FinBERT sentiment.
        SELL blocked if FinBERT strongly BULLISH (score > +0.4)
        BUY  blocked if FinBERT strongly BEARISH (score < -0.4)
        """
        score = signal.sentiment_score
        if signal.direction == "SELL" and score > SENTIMENT_BLOCK_BULL:
            return False, f"Sentiment gate: SELL blocked — FinBERT strongly BULLISH ({score:+.2f})"
        if signal.direction == "BUY" and score < SENTIMENT_BLOCK_BEAR:
            return False, f"Sentiment gate: BUY blocked — FinBERT strongly BEARISH ({score:+.2f})"
        return True, ""

    '''def _calculate_quantity(self, symbol: str, ltp: float) -> int:
        """Simple fixed quantity for now. Phase 5 will add position sizing."""
        return MAX_POSITION_SIZE'''
    
    def _calculate_quantity(self, symbol: str, ltp: float) -> int:
        """
        Capital-aware position sizing.
        Each trade = 30% of TOTAL_CAPITAL.
        Never more than MAX_SIMULTANEOUS_POSITIONS open.
        """
        total_capital = float(os.getenv("TOTAL_CAPITAL", "50000"))
        capital_per_trade = total_capital * CAPITAL_PER_TRADE_PCT
        if ltp <= 0:
            return 1
        qty = int(capital_per_trade / ltp)
        return max(1, qty)

    def validate(self, signal: SignalEvent) -> tuple[bool, str]:
        """
        Run all risk checks on a SignalEvent.
        Returns (approved: bool, reason: str)
        """
        with self._lock:
            checks = [
                self._check_daily_loss(),
                self._check_max_positions(),
                self._check_duplicate_position(signal.symbol),
                self._check_time_gate(),
                self._check_cooldown(),
                self._check_sentiment_gate(signal),
            ]
            for passed, reason in checks:
                if not passed:
                    logger.info(f"  ❌ BLOCKED {signal.symbol}: {reason}")
                    self.trade_db.log_signal(
                        symbol=signal.symbol,
                        direction=signal.direction,
                        strategy=signal.strategy_name,
                        sentiment=signal.sentiment_score,
                        ltp=signal.ltp,
                        status="BLOCKED",
                        block_reason=reason
                    )
                    return False, reason

            # All checks passed — create OrderEvent
            quantity    = self._calculate_quantity(signal.symbol, signal.ltp)
            buffer      = 1.0005 if signal.direction == "BUY" else 0.9995
            limit_price = round(signal.ltp * buffer, 2)

            order = OrderEvent(
                symbol          = signal.symbol,
                direction       = signal.direction,
                quantity        = quantity,
                limit_price     = limit_price,
                strategy_name   = signal.strategy_name,
                signal_timestamp = signal.timestamp
            )

            self._last_order_time = datetime.now()
            self.trade_db.log_signal(
                symbol=signal.symbol,
                direction=signal.direction,
                strategy=signal.strategy_name,
                sentiment=signal.sentiment_score,
                ltp=signal.ltp,
                status="APPROVED"
            )

            logger.info(f"  ✅ APPROVED {signal.direction} {signal.symbol} "
                       f"@ {limit_price} x{quantity}")
            self.engine.emit_event(order)
            return True, "APPROVED"

    def on_signal(self, event: SignalEvent):
        """EventBus handler — called on every SignalEvent."""
        self.validate(event)


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    from src.core.engine import TradingEngine
    from src.core.trade_db import TradeDB

    engine   = TradingEngine(is_paper_trading=True)
    db       = TradeDB(db_path="test_risk.db")
    risk     = RiskManager(engine=engine, trade_db=db)
    risk._bypass_time_gate = True   # Test runs outside market hours
    orders   = []

    def on_order(event: OrderEvent):
        orders.append(event)

    engine.register_handler("SIGNAL", risk.on_signal)
    engine.register_handler("ORDER",  on_order)
    engine.run_in_thread()

    print("── RiskManager Test — 6 Scenarios ──\n")

    def make_signal(symbol="INFY", direction="BUY",
                    sentiment=0.5, ltp=1842.50,
                    strategy="MorningBreakout") -> SignalEvent:
        return SignalEvent(
            symbol=symbol, direction=direction,
            strategy_name=strategy,
            sentiment_score=sentiment, ltp=ltp
        )

    # Scenario 1 — All clear, should APPROVE
    print("[1] All clear — expect APPROVED")
    engine.emit_event(make_signal("INFY", "BUY", sentiment=0.5))
    time.sleep(0.2)

    # Scenario 2 — Duplicate position (INFY already open)
    db.open_trade("INFY", "BUY", 100, 1842.50,
                  "MorningBreakout", 0.5, "NIFTY IT",
                  "PAPER_001", True)
    print("[2] Duplicate position — expect BLOCKED")
    engine.emit_event(make_signal("INFY", "BUY", sentiment=0.5))
    time.sleep(0.2)

    # Scenario 3 — Cooldown (too fast after scenario 1)
    print("[3] Cooldown — expect BLOCKED (sent immediately after #1)")
    engine.emit_event(make_signal("HDFCBANK", "BUY", sentiment=0.6))
    time.sleep(0.2)

    # Scenario 4 — Sentiment gate: BUY with strongly BEARISH sentiment
    print("[4] Sentiment gate — BUY blocked by BEARISH FinBERT")
    engine.emit_event(make_signal("RELIANCE", "BUY", sentiment=-0.96))
    time.sleep(0.2)

    # Scenario 5 — Daily loss limit
    print("[5] Daily loss limit — expect BLOCKED")
    risk.stop_all = True
    engine.emit_event(make_signal("TCS", "BUY", sentiment=0.7))
    time.sleep(0.2)
    risk.stop_all = False

    # Scenario 6 — Wait for cooldown, clean signal
    print("[6] After cooldown — expect APPROVED")
    time.sleep(1.1)
    engine.emit_event(make_signal("HDFCBANK", "BUY", sentiment=0.6))
    time.sleep(0.2)

    engine.stop()
    time.sleep(0.3)

    print(f"\n── Results ──")
    print(f"  Orders approved : {len(orders)}")
    print(f"  Expected        : 2 (scenarios 1 and 6)")

    import os
    db.engine.dispose()
    os.remove("test_risk.db")

    if len(orders) == 2:
        print(f"\n✅ RiskManager all 6 scenarios correct.")
    else:
        print(f"\n⚠️  Expected 2 orders, got {len(orders)}")
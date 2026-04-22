import os
import sys
import time
import pytest
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.events       import MarketEvent, SignalEvent, OrderEvent, FillEvent
from src.core.engine       import TradingEngine
from src.core.trade_db     import TradeDB
from src.core.risk         import RiskManager
from src.core.exit_manager import ExitManager
from src.core.execution    import ExecutionHandler
from src.strategies.breakout     import MorningBreakout
from src.strategies.rsi_momentum import RSIMomentum


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    e = TradingEngine(is_paper_trading=True)
    e.run_in_thread()
    yield e
    e.stop()
    time.sleep(0.2)

@pytest.fixture
def db(tmp_path):
    d = TradeDB(db_path=str(tmp_path / "test.db"))
    yield d
    d.engine.dispose()

@pytest.fixture
def risk(engine, db):
    r = RiskManager(engine=engine, trade_db=db)
    r._bypass_time_gate = True
    return r

@pytest.fixture
def exits(engine, db):
    return ExitManager(engine=engine, trade_db=db)

@pytest.fixture
def execution(engine, db):
    return ExecutionHandler(engine=engine, trade_db=db,
                            is_paper_trading=True)


# ── EventBus tests ────────────────────────────────────────────────────

class TestEventBus:

    def test_market_event_routed(self, engine):
        received = []
        engine.register_handler("MARKET",
                                 lambda e: received.append(e))
        engine.emit_event(MarketEvent(symbol="INFY", ltp=1842.0))
        time.sleep(0.1)
        assert len(received) == 1
        assert received[0].symbol == "INFY"

    def test_multiple_handlers_same_event(self, engine):
        results = []
        engine.register_handler("MARKET", lambda e: results.append("h1"))
        engine.register_handler("MARKET", lambda e: results.append("h2"))
        engine.emit_event(MarketEvent(symbol="INFY", ltp=1842.0))
        time.sleep(0.1)
        assert "h1" in results
        assert "h2" in results

    def test_100_events_no_drops(self, engine):
        received = []
        engine.register_handler("MARKET", lambda e: received.append(e))
        for i in range(100):
            engine.emit_event(MarketEvent(symbol="INFY", ltp=1840.0 + i))
        time.sleep(0.5)
        assert len(received) == 100


# ── RiskManager tests ─────────────────────────────────────────────────

class TestRiskManager:

    def test_approved_signal_creates_order(self, engine, db, risk):
        orders = []
        engine.register_handler("SIGNAL", risk.on_signal)
        engine.register_handler("ORDER",  lambda e: orders.append(e))
        engine.emit_event(SignalEvent(
            symbol="INFY", direction="BUY",
            strategy_name="Test", sentiment_score=0.5, ltp=1842.0
        ))
        time.sleep(0.3)
        assert len(orders) == 1
        assert orders[0].symbol == "INFY"

    def test_duplicate_position_blocked(self, engine, db, risk):
        orders = []
        engine.register_handler("SIGNAL", risk.on_signal)
        engine.register_handler("ORDER",  lambda e: orders.append(e))
        # Open a position manually
        db.open_trade("INFY", "BUY", 100, 1842.0,
                      "Test", 0.5, "IT", "PAPER_001", True)
        engine.emit_event(SignalEvent(
            symbol="INFY", direction="BUY",
            strategy_name="Test", sentiment_score=0.5, ltp=1842.0
        ))
        time.sleep(0.3)
        assert len(orders) == 0

    def test_sentiment_gate_blocks_buy_on_bearish(self, engine, db, risk):
        orders = []
        engine.register_handler("SIGNAL", risk.on_signal)
        engine.register_handler("ORDER",  lambda e: orders.append(e))
        engine.emit_event(SignalEvent(
            symbol="RELIANCE", direction="BUY",
            strategy_name="Test", sentiment_score=-0.96, ltp=2800.0
        ))
        time.sleep(0.3)
        assert len(orders) == 0

    def test_stop_all_blocks_all_orders(self, engine, db, risk):
        orders = []
        risk.stop_all = True
        engine.register_handler("SIGNAL", risk.on_signal)
        engine.register_handler("ORDER",  lambda e: orders.append(e))
        engine.emit_event(SignalEvent(
            symbol="TCS", direction="BUY",
            strategy_name="Test", sentiment_score=0.7, ltp=3500.0
        ))
        time.sleep(0.3)
        assert len(orders) == 0


# ── ExitManager tests ─────────────────────────────────────────────────

class TestExitManager:

    def _open_position(self, db, exits, symbol="INFY",
                       direction="BUY", entry=1842.50):
        trade_id = db.open_trade(symbol, direction, 100, entry,
                                 "Test", 0.5, "IT", "PAPER_TEST", True)
        exits.add_position(trade_id, symbol, direction, entry, 100)
        return trade_id

    def _tick(self, symbol, ltp):
        return MarketEvent(symbol=symbol, ltp=ltp,
                          open=ltp, high=ltp+2, low=ltp-2, close=ltp)

    def test_sl_closes_position(self, engine, db, exits):
        self._open_position(db, exits, "INFY", "BUY", 1842.50)
        sl = exits.positions["INFY"].sl_price
        exits.on_tick(self._tick("INFY", sl - 1))
        assert "INFY" not in exits.positions

    def test_target_activates_trail(self, engine, db, exits):
        self._open_position(db, exits, "HDFCBANK", "BUY", 1600.0)
        target = exits.positions["HDFCBANK"].target_price
        exits.on_tick(self._tick("HDFCBANK", target + 1))
        assert exits.positions["HDFCBANK"].trail_active is True

    def test_trail_closes_after_target(self, engine, db, exits):
        self._open_position(db, exits, "RELIANCE", "BUY", 2800.0)
        target = exits.positions["RELIANCE"].target_price
        exits.on_tick(self._tick("RELIANCE", target + 1))
        trail_sl = exits.positions["RELIANCE"].trail_sl
        exits.on_tick(self._tick("RELIANCE", trail_sl - 1))
        assert "RELIANCE" not in exits.positions

    def test_sl_price_correct_for_buy(self, engine, db, exits):
        self._open_position(db, exits, "TCS", "BUY", 3500.0)
        expected_sl = round(3500.0 * 0.992, 2)
        assert exits.positions["TCS"].sl_price == expected_sl

    def test_target_price_correct_for_buy(self, engine, db, exits):
        self._open_position(db, exits, "WIPRO", "BUY", 500.0)
        expected_target = round(500.0 * 1.015, 2)
        assert exits.positions["WIPRO"].target_price == expected_target


# ── ExecutionHandler tests ────────────────────────────────────────────

class TestExecutionHandler:

    def test_paper_order_emits_fill(self, engine, db, execution):
        fills = []
        engine.register_handler("ORDER", execution.on_order)
        engine.register_handler("FILL",  lambda e: fills.append(e))
        engine.emit_event(OrderEvent(
            symbol="INFY", direction="BUY",
            quantity=100, limit_price=1843.42,
            strategy_name="Test"
        ))
        time.sleep(0.3)
        assert len(fills) == 1
        assert fills[0].symbol == "INFY"
        assert fills[0].is_paper is True

    def test_paper_order_logged_to_db(self, engine, db, execution):
        engine.register_handler("ORDER", execution.on_order)
        engine.emit_event(OrderEvent(
            symbol="HDFCBANK", direction="BUY",
            quantity=50, limit_price=1600.0,
            strategy_name="Test"
        ))
        time.sleep(0.3)
        trades = db.get_open_trades()
        symbols = [t.symbol for t in trades]
        assert "HDFCBANK" in symbols

    def test_buy_limit_price_has_buffer(self, engine, db, execution):
        fills = []
        engine.register_handler("ORDER", execution.on_order)
        engine.register_handler("FILL",  lambda e: fills.append(e))
        engine.emit_event(OrderEvent(
            symbol="TCS", direction="BUY",
            quantity=10, limit_price=round(3500.0 * 1.0005, 2),
            strategy_name="Test"
        ))
        time.sleep(0.3)
        assert len(fills) == 1
        assert fills[0].fill_price == round(3500.0 * 1.0005, 2)


# ── Full trade lifecycle test ─────────────────────────────────────────

class TestFullTradeLifecycle:

    def test_signal_to_fill_to_exit(self, engine, db, risk,
                                     exits, execution):
        """
        Full lifecycle:
        Signal → RiskManager APPROVED → OrderEvent →
        ExecutionHandler → FillEvent → ExitManager →
        SL hit → Trade closed in DB
        """
        fills  = []
        orders = []

        def on_fill(event: FillEvent):
            fills.append(event)
            # Register with ExitManager
            exits.add_position(
                trade_id    = event.trade_id,
                symbol      = event.symbol,
                direction   = event.direction,
                entry_price = event.fill_price,
                quantity    = event.quantity
            )

        engine.register_handler("SIGNAL", risk.on_signal)
        engine.register_handler("ORDER",  execution.on_order)
        engine.register_handler("ORDER",  lambda e: orders.append(e))
        engine.register_handler("FILL",   on_fill)

        # Emit signal
        engine.emit_event(SignalEvent(
            symbol="INFY", direction="BUY",
            strategy_name="MorningBreakout",
            sentiment_score=0.5, ltp=1842.50
        ))
        time.sleep(0.5)

        # Verify order and fill
        assert len(orders) == 1
        assert len(fills)  == 1
        assert fills[0].symbol == "INFY"

        # Trigger SL
        sl_price = exits.positions["INFY"].sl_price
        exits.on_tick(MarketEvent(
            symbol="INFY", ltp=sl_price - 1,
            open=sl_price, high=sl_price+2,
            low=sl_price-2, close=sl_price-1
        ))
        time.sleep(0.2)

        # Position should be closed
        assert "INFY" not in exits.positions

        # DB should show closed trade
        from sqlalchemy.orm import Session
        from src.core.trade_db import Trade
        with Session(db.engine) as session:
            trade = session.query(Trade).filter_by(symbol="INFY").first()
            assert trade is not None
            assert trade.status == "CLOSED"
            assert trade.exit_reason == "SL"

        print("\n✅ Full trade lifecycle: Signal → Order → Fill → SL → Closed")
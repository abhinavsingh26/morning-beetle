from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    """Base class for all events in the Morning Beetle engine."""
    timestamp: datetime = field(default_factory=datetime.now)
    event_type: str = "BASE"


@dataclass
class MarketEvent(Event):
    """
    Carries live tick data from Kite WebSocket.
    Emitted by DataHandler on every tick received.
    """
    event_type: str = "MARKET"
    symbol: str = ""
    instrument_token: int = 0
    ltp: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    change: float = 0.0


@dataclass
class SignalEvent(Event):
    """
    BUY or SELL signal emitted by a Strategy.
    Carries ticker, direction, and originating strategy name.
    """
    event_type: str = "SIGNAL"
    symbol: str = ""
    direction: str = ""        # "BUY" or "SELL"
    strategy_name: str = ""
    sentiment_score: float = 0.0
    confidence: float = 0.0
    ltp: float = 0.0


@dataclass
class OrderEvent(Event):
    """
    Final order parameters passed to ExecutionHandler.
    Created by RiskManager after all checks pass.
    """
    event_type: str = "ORDER"
    symbol: str = ""
    direction: str = ""        # "BUY" or "SELL"
    quantity: int = 0
    order_type: str = "LIMIT"  # Always LIMIT per Blueprint
    limit_price: float = 0.0
    strategy_name: str = ""
    signal_timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class FillEvent(Event):
    """
    Confirms an order has been filled.
    Created by ExecutionHandler after Kite confirms order.
    """
    event_type: str = "FILL"
    symbol: str = ""
    direction: str = ""
    quantity: int = 0
    fill_price: float = 0.0
    order_id: str = ""
    is_paper: bool = True


if __name__ == "__main__":
    from datetime import datetime

    print("── Event Classes Test ──\n")

    # Test MarketEvent
    me = MarketEvent(
        symbol="INFY",
        instrument_token=408065,
        ltp=1842.50,
        open=1830.00,
        high=1850.00,
        low=1825.00,
        close=1835.00,
        volume=125000
    )
    print(f"MarketEvent : {me.symbol} LTP={me.ltp} @ {me.timestamp.strftime('%H:%M:%S')}")

    # Test SignalEvent
    se = SignalEvent(
        symbol="INFY",
        direction="BUY",
        strategy_name="MorningBreakout",
        sentiment_score=-0.963,
        ltp=1842.50
    )
    print(f"SignalEvent : {se.direction} {se.symbol} via {se.strategy_name}")

    # Test OrderEvent
    oe = OrderEvent(
        symbol="INFY",
        direction="BUY",
        quantity=100,
        limit_price=1843.42,
        strategy_name="MorningBreakout"
    )
    print(f"OrderEvent  : {oe.direction} {oe.quantity} {oe.symbol} @ {oe.limit_price} LIMIT")

    # Test FillEvent
    fe = FillEvent(
        symbol="INFY",
        direction="BUY",
        quantity=100,
        fill_price=1843.42,
        order_id="KITE123456",
        is_paper=True
    )
    print(f"FillEvent   : {fe.direction} {fe.quantity} {fe.symbol} @ {fe.fill_price} [PAPER={fe.is_paper}]")

    print("\n✅ All 4 event types created successfully.")
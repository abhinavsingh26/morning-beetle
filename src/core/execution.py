import os
import logging
from datetime import datetime
from kiteconnect import KiteConnect
from dotenv import load_dotenv
from src.core.events import OrderEvent, FillEvent

load_dotenv("config/.env")
logger = logging.getLogger(__name__)

# Order buffer per Blueprint
BUY_BUFFER  = 1.0005   # LTP × 1.0005 for BUY limit
SELL_BUFFER = 0.9995   # LTP × 0.9995 for SELL limit


class ExecutionHandler:
    """
    Places LIMIT orders via Kite API.
    MARKET orders are code-disabled per Blueprint.

    IS_PAPER_TRADING = True  → prints order, no real placement
    IS_PAPER_TRADING = False → places real Kite MIS limit order
    """

    def __init__(self, engine, trade_db, is_paper_trading: bool = True):
        self.engine           = engine
        self.trade_db         = trade_db
        self.is_paper_trading = is_paper_trading
        self.kite             = None
        self._paper_order_counter = 1000

        if not is_paper_trading:
            api_key      = os.getenv("ZERODHA_API_KEY")
            access_token = os.getenv("ZERODHA_ACCESS_TOKEN")
            if not api_key or not access_token:
                raise ValueError("Kite credentials missing for live trading")
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)
            logger.info("ExecutionHandler initialised — LIVE MODE")
        else:
            logger.info("ExecutionHandler initialised — PAPER MODE")

    def _place_paper_order(self, event: OrderEvent) -> str:
        """Simulate order placement. Returns fake order ID."""
        self._paper_order_counter += 1
        order_id = f"PAPER_{self._paper_order_counter}"

        print(f"\n  📋 PAPER ORDER: {event.direction} {event.symbol} "
              f"{event.quantity} @ ₹{event.limit_price:.2f} LIMIT MIS"
              f"  [{event.strategy_name}]")

        logger.info(f"  PAPER ORDER: {event.direction} {event.symbol} "
                   f"{event.quantity} @ {event.limit_price:.2f} "
                   f"[{order_id}]")
        return order_id

    def _place_live_order(self, event: OrderEvent) -> str:
        """Place real LIMIT MIS order via Kite API."""
        if not self.kite:
            raise RuntimeError("Kite not initialised for live trading")

        # MARKET orders are code-disabled per Blueprint
        order_id = self.kite.place_order(
            variety    = self.kite.VARIETY_REGULAR,
            exchange   = self.kite.EXCHANGE_NSE,
            tradingsymbol = event.symbol,
            transaction_type = (
                self.kite.TRANSACTION_TYPE_BUY
                if event.direction == "BUY"
                else self.kite.TRANSACTION_TYPE_SELL
            ),
            quantity   = event.quantity,
            product    = self.kite.PRODUCT_MIS,
            order_type = self.kite.ORDER_TYPE_LIMIT,
            price      = event.limit_price
        )
        logger.info(f"  LIVE ORDER placed: {event.direction} {event.symbol} "
                   f"@ {event.limit_price:.2f} [{order_id}]")
        return str(order_id)

    def on_order(self, event: OrderEvent):
        """
        EventBus handler — called on every OrderEvent.
        Places order and emits FillEvent.
        """
        try:
            if self.is_paper_trading:
                order_id = self._place_paper_order(event)
            else:
                order_id = self._place_live_order(event)

            # Log to DB
            trade_id = self.trade_db.open_trade(
                symbol       = event.symbol,
                direction    = event.direction,
                quantity     = event.quantity,
                entry_price  = event.limit_price,
                strategy     = event.strategy_name,
                sentiment    = 0.0,
                sector       = "UNKNOWN",
                order_id     = order_id,
                is_paper     = self.is_paper_trading
            )

            # Emit FillEvent for ExitManager to start monitoring
            fill = FillEvent(
                symbol      = event.symbol,
                direction   = event.direction,
                quantity    = event.quantity,
                fill_price  = event.limit_price,
                order_id    = order_id,
                is_paper    = self.is_paper_trading
            )
            fill.trade_id = trade_id   # Attach DB ID for ExitManager
            self.engine.emit_event(fill)

            logger.info(f"  ✅ Order logged to DB — trade_id={trade_id}")

        except Exception as e:
            logger.error(f"  ❌ ExecutionHandler error: {e}")
            self.trade_db.log_system("ERROR", "EXECUTION_FAIL", str(e))

    def cancel_order(self, order_id: str):
        """Cancel a pending order."""
        if self.is_paper_trading:
            logger.info(f"  PAPER: Cancel order {order_id} (simulated)")
            return
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id
            )
            logger.info(f"  Order cancelled: {order_id}")
        except Exception as e:
            logger.error(f"  Cancel failed: {e}")


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    from src.core.engine import TradingEngine
    from src.core.trade_db import TradeDB

    engine    = TradingEngine(is_paper_trading=True)
    db        = TradeDB(db_path="test_exec.db")
    execution = ExecutionHandler(engine=engine, trade_db=db,
                                 is_paper_trading=True)
    fills     = []

    def on_fill(event: FillEvent):
        fills.append(event)
        logger.info(f"  FillEvent received: {event.direction} "
                   f"{event.symbol} @ {event.fill_price}")

    engine.register_handler("ORDER", execution.on_order)
    engine.register_handler("FILL",  on_fill)
    engine.run_in_thread()

    print("── ExecutionHandler Test ──\n")

    # Test 1 — BUY paper order
    print("[1] BUY INFY — paper order")
    engine.emit_event(OrderEvent(
        symbol="INFY", direction="BUY",
        quantity=100, limit_price=1843.42,
        strategy_name="MorningBreakout"
    ))
    time.sleep(0.3)

    # Test 2 — SELL paper order
    print("\n[2] SELL HDFCBANK — paper order")
    engine.emit_event(OrderEvent(
        symbol="HDFCBANK", direction="SELL",
        quantity=50, limit_price=1598.75,
        strategy_name="RSIMomentum"
    ))
    time.sleep(0.3)

    engine.stop()
    time.sleep(0.2)

    print(f"\n── Results ──")
    print(f"  Orders placed : 2")
    print(f"  Fills received: {len(fills)}")

    # Verify DB
    open_trades = db.get_open_trades()
    print(f"  DB open trades: {len(open_trades)}")
    for t in open_trades:
        print(f"    {t.direction} {t.symbol} @ {t.entry_price} "
              f"[{t.order_id}] paper={t.is_paper}")

    import os
    db.engine.dispose()
    os.remove("test_exec.db")

    if len(fills) == 2 and len(open_trades) == 2:
        print(f"\n✅ ExecutionHandler working. MARKET orders disabled.")
    else:
        print(f"\n⚠️  Check fills and DB entries.")
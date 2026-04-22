import queue
import logging
import threading
from datetime import datetime
from src.core.events import Event

logger = logging.getLogger(__name__)


class TradingEngine:
    """
    Event-driven trading engine with a shared EventBus.
    All modules communicate exclusively through events.
    No polling loops — every action is triggered by an event.
    """

    def __init__(self, timeout: float = 3.0, is_paper_trading: bool = True):
        self.event_queue     = queue.Queue()
        self.handlers        = {}        # event_type -> list of handler functions
        self.timeout         = timeout   # 3.0s per Blueprint for WiFi stability
        self.is_paper_trading = is_paper_trading
        self.is_running      = False
        self._lock           = threading.Lock()

        logger.info(f"TradingEngine initialised — "
                    f"PAPER={is_paper_trading} | timeout={timeout}s")

    def register_handler(self, event_type: str, handler):
        """
        Register a handler function for a specific event type.
        Multiple handlers can be registered for the same event type.
        """
        with self._lock:
            if event_type not in self.handlers:
                self.handlers[event_type] = []
            self.handlers[event_type].append(handler)
            logger.debug(f"Registered handler {handler.__name__} for {event_type}")

    def emit_event(self, event: Event):
        """Push an event onto the queue."""
        self.event_queue.put(event)

    def _dispatch(self, event: Event):
        """Route event to all registered handlers."""
        event_type = event.event_type
        if event_type not in self.handlers:
            logger.debug(f"No handlers for event type: {event_type}")
            return
        for handler in self.handlers[event_type]:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Handler {handler.__name__} failed on {event_type}: {e}")

    def run(self):
        """
        Main event loop. Blocks until stop() is called.
        Processes events from the queue with timeout for WiFi stability.
        """
        self.is_running = True
        logger.info("EventBus started — waiting for events...")

        while self.is_running:
            try:
                event = self.event_queue.get(timeout=self.timeout)
                self._dispatch(event)
                self.event_queue.task_done()
            except queue.Empty:
                continue  # Timeout — loop back and check is_running
            except Exception as e:
                logger.error(f"EventBus error: {e}")

        logger.info("EventBus stopped.")

    def run_in_thread(self) -> threading.Thread:
        """Start the event loop in a background thread."""
        t = threading.Thread(target=self.run, daemon=True, name="EventBus")
        t.start()
        logger.info("EventBus running in background thread.")
        return t

    def stop(self):
        """Signal the event loop to stop."""
        self.is_running = False
        logger.info("TradingEngine stop signal sent.")

    def queue_size(self) -> int:
        return self.event_queue.qsize()


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    from src.core.events import MarketEvent, SignalEvent

    engine = TradingEngine(timeout=3.0, is_paper_trading=True)

    # Track received events
    received = []

    def on_market_event(event: MarketEvent):
        received.append(event)
        logger.info(f"  Handler received: {event.symbol} LTP={event.ltp}")

    def on_signal_event(event: SignalEvent):
        received.append(event)
        logger.info(f"  Signal received: {event.direction} {event.symbol}")

    # Register handlers
    engine.register_handler("MARKET", on_market_event)
    engine.register_handler("SIGNAL", on_signal_event)

    # Start engine in background thread
    thread = engine.run_in_thread()

    # Emit 100 mock MarketEvents
    print("\nEmitting 100 mock MarketEvents...")
    for i in range(100):
        engine.emit_event(MarketEvent(
            symbol="INFY",
            instrument_token=408065,
            ltp=1840.0 + i * 0.1
        ))

    # Emit 5 mock SignalEvents
    print("Emitting 5 mock SignalEvents...")
    for i in range(5):
        engine.emit_event(SignalEvent(
            symbol="INFY",
            direction="BUY",
            strategy_name="TestStrategy",
            ltp=1842.50
        ))

    # Wait for queue to drain
    time.sleep(2)
    engine.stop()
    time.sleep(0.5)

    print(f"\n── EventBus Test Results ──")
    print(f"  Events emitted  : 105")
    print(f"  Events received : {len(received)}")
    print(f"  MarketEvents    : {sum(1 for e in received if e.event_type == 'MARKET')}")
    print(f"  SignalEvents    : {sum(1 for e in received if e.event_type == 'SIGNAL')}")

    assert len(received) == 105, f"Expected 105, got {len(received)}"
    print("\n✅ All 105 events received. No timeouts. EventBus working.")
import os
import logging
import threading
from datetime import datetime
from kiteconnect import KiteTicker
from dotenv import load_dotenv
from src.core.events import MarketEvent

load_dotenv("config/.env")
logger = logging.getLogger(__name__)


class DataHandler:
    """
    Kite WebSocket manager.
    Subscribes to instrument tokens from watchlist.
    Emits MarketEvent on every tick received.
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

        api_key      = os.getenv("ZERODHA_API_KEY")
        access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

        if not api_key or not access_token:
            raise ValueError("ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN missing")

        self.ticker = KiteTicker(api_key, access_token)
        self._register_callbacks()
        logger.info("DataHandler initialised.")

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
        This is the hot path — keep it fast.
        """
        for tick in ticks:
            token  = tick.get("instrument_token", 0)
            symbol = self.subscribed.get(token, "UNKNOWN")

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

    def start(self):
        """Start WebSocket in background thread."""
        logger.info("Starting Kite WebSocket...")
        self.ticker.connect(threaded=True)

    def stop(self):
        """Stop WebSocket connection."""
        if self.ticker:
            self.ticker.close()
            self.is_connected = False
            logger.info("WebSocket stopped.")


if __name__ == "__main__":
    import time
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

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

    print(f"\n⏳ Streaming ticks for 30 seconds — watch for live data...\n")
    time.sleep(30)

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
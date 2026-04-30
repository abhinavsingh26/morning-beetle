import logging
from datetime import time, datetime
from collections import deque
from src.strategies.base import Strategy
from src.core.events import MarketEvent, SignalEvent

logger = logging.getLogger(__name__)

# S5 Parameters per Blueprint v8
VOLUME_SPIKE_MULT      = 3.0     # 5-min volume > 3x trailing 30-min avg
PRICE_MOVE_PCT         = 0.005   # Price moves > 0.5% in spike direction
SENTIMENT_CONFIRM_BULL = 0.30    # FinBERT > +0.30 for BUY
SENTIMENT_CONFIRM_BEAR = -0.30   # FinBERT < -0.30 for SELL
COOLDOWN_MINUTES       = 90      # No re-entry on same symbol for 90 min
MIN_VOLUME_TICKS       = 15      # Min ticks for reliable volume avg
SL_PCT                 = 0.006   # 0.6% stop loss
TARGET_PCT             = 0.012   # 1.2% target


class VolumeSpikeWithSentiment(Strategy):
    """
    Strategy S5 — Volume Spike + Sentiment Re-confirmation

    Thesis: A sudden volume spike on a watchlist stock often signals
    fresh news flow. Catch the move but only after FinBERT confirms
    the directional thesis still holds with current headlines.

    Active window: 10:30 AM – 14:45 PM (broadest window)
    Loss bucket:   post_morning
    """

    name          = "vol_spike_sentiment"
    active_window = (time(10, 30), time(14, 45))
    loss_bucket   = "post_morning"
    sl_pct        = SL_PCT
    target_pct    = TARGET_PCT

    def __init__(self, engine, symbol: str,
                 sentiment_score: float = 0.0):
        super().__init__(engine, symbol, sentiment_score)

        self._volumes         = deque(maxlen=30)   # 30-min trailing window
        self._prices          = deque(maxlen=10)
        self._tick_count      = 0
        self._last_signal_time = None
        self._signal_fired    = False

        logger.info(f"  VolumeSpikeWithSentiment initialised for {symbol}")

    def _is_in_cooldown(self) -> bool:
        """90-minute cooldown per ticker after signal."""
        if self._last_signal_time is None:
            return False
        elapsed = (datetime.now() - self._last_signal_time).total_seconds()
        return elapsed < (COOLDOWN_MINUTES * 60)

    def _get_fresh_sentiment(self) -> float:
        """
        On-demand FinBERT re-scan for this ticker.
        Uses fetch_for_ticker() if available, falls back to stored score.
        """
        try:
            from src.beetle.news_fetcher import fetch_for_ticker
            from src.beetle.finbert_scorer import score_headline
            headlines = fetch_for_ticker(self.symbol, since_minutes=120)
            if headlines:
                scores = [score_headline(h["title"])["score"]
                          for h in headlines[:3]]
                return sum(scores) / len(scores)
        except Exception as e:
            logger.debug(f"  S5 fresh sentiment fetch failed: {e}")
        return self.sentiment_score   # Fall back to pre-market score

    def on_tick(self, event: MarketEvent) -> None:
        """Process tick — detect volume spike + confirm sentiment."""
        ltp    = event.ltp
        volume = event.volume if hasattr(event, 'volume') and event.volume else 0

        self._prices.append(ltp)
        if volume > 0:
            self._volumes.append(volume)
        self._tick_count += 1

        # Need minimum volume history
        if self._tick_count < MIN_VOLUME_TICKS or not self._volumes:
            return

        # Skip if in cooldown
        if self._is_in_cooldown():
            return

        avg_volume = sum(self._volumes) / len(self._volumes)

        # Volume spike check
        if volume < avg_volume * VOLUME_SPIKE_MULT:
            return

        # Price move check — determine direction
        if len(self._prices) < 5:
            return
        price_start = self._prices[0]
        price_move  = (ltp - price_start) / price_start

        if abs(price_move) < PRICE_MOVE_PCT:
            return

        direction = "BUY" if price_move > 0 else "SELL"

        # Fresh sentiment confirmation
        fresh_score = self._get_fresh_sentiment()

        if direction == "BUY" and fresh_score < SENTIMENT_CONFIRM_BULL:
            logger.debug(f"  S5 {self.symbol}: BUY spike but sentiment weak "
                        f"({fresh_score:+.2f} < {SENTIMENT_CONFIRM_BULL})")
            return

        if direction == "SELL" and fresh_score > SENTIMENT_CONFIRM_BEAR:
            logger.debug(f"  S5 {self.symbol}: SELL spike but sentiment weak "
                        f"({fresh_score:+.2f} > {SENTIMENT_CONFIRM_BEAR})")
            return

        # ✅ All conditions met — emit signal
        self._last_signal_time = datetime.now()
        logger.info(f"  🎯 S5 SIGNAL: {direction} {self.symbol} @ {ltp:.2f} "
                   f"[vol={volume:.0f} vs avg={avg_volume:.0f} "
                   f"({volume/avg_volume:.1f}x), sentiment={fresh_score:+.2f}]")

        signal = SignalEvent(
            symbol         = self.symbol,
            direction      = direction,
            strategy_name  = self.name,
            sentiment_score= fresh_score,
            ltp            = ltp
        )
        self.engine.emit_event(signal)

    def reset(self) -> None:
        self._volumes.clear()
        self._prices.clear()
        self._tick_count       = 0
        self._last_signal_time = None
        self._signal_fired     = False
import os
import logging
import asyncio
import threading
import queue
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

load_dotenv("config/.env")
logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Async Telegram bot for Morning Beetle alerts.

    v2 Architecture (fixes pool timeout + event loop errors):
    - Single persistent asyncio event loop on a dedicated background thread
    - Thread-safe message queue — any thread can call send()
    - HTTPX connection pool sized for burst traffic (10 connections)
    - 30s timeouts to avoid pool starvation on slow Telegram responses

    3 message templates per Blueprint:
    1. Pre-Market Report  — 09:14 AM
    2. Trade Alert        — every order
    3. EOD Summary        — 15:30 PM
    """

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not self.token or not self.chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from .env"
            )

        # Configure HTTPX with a larger pool to handle burst alerts
        request = HTTPXRequest(
            connection_pool_size = 10,
            pool_timeout         = 10.0,
            connect_timeout      = 10.0,
            read_timeout         = 30.0,
            write_timeout        = 30.0,
        )
        self.bot = Bot(token=self.token, request=request)

        # Dedicated event loop on a background thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="TelegramLoop"
        )
        self._thread.start()
        self._shutdown = False

        logger.info("TelegramNotifier initialised.")

    def _run_loop(self):
        """Run asyncio loop forever in background thread."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"Telegram loop crashed: {e}")

    async def _send_async(self, message: str):
        """Async send — runs on dedicated loop."""
        try:
            await self.bot.send_message(
                chat_id    = self.chat_id,
                text       = message,
                parse_mode = "HTML"
            )
            logger.info(f"Telegram sent: {message[:60]}...")
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
        except Exception as e:
            logger.error(f"Telegram unexpected error: {e}")

    def send(self, message: str):
        """
        Thread-safe send. Schedules message on the dedicated loop.
        Returns immediately — does not block caller.
        """
        if self._shutdown:
            logger.warning("Telegram shutting down — message dropped.")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_async(message),
                self._loop
            )
        except Exception as e:
            logger.error(f"Failed to schedule Telegram message: {e}")

    def shutdown(self):
        """Stop the background loop on engine shutdown."""
        self._shutdown = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2.0)
        except Exception as e:
            logger.warning(f"Telegram shutdown error: {e}")

    # ── Message Templates ─────────────────────────────────────────────

    def send_premarket_report(self, watchlist: list,
                               is_paper: bool = True):
        """Template 1 — Pre-Market Report at 09:14 AM."""
        mode = "📋 PAPER" if is_paper else "🔴 LIVE"
        now  = datetime.now().strftime("%d %b %Y")

        lines = [
            f"🐞 <b>Morning Beetle — Pre-Market Report</b>",
            f"📅 {now}  |  {mode}",
            f"━━━━━━━━━━━━━━━━━━━━━",
        ]

        if not watchlist:
            lines.append("⚠️ No high-conviction tickers today.")
        else:
            for i, t in enumerate(watchlist, 1):
                score  = t.get("sentiment_score", 0)
                icon   = "🟢" if score > 0 else "🔴"
                sector = t.get("sector", "UNKNOWN")
                s_bias = t.get("sector_bias", "NEUTRAL")
                lines.append(
                    f"\n{i}. <b>{t['symbol']}</b> — {t.get('name', '')}"
                    f"\n   {icon} Sentiment: {score:+.2f}"
                    f"\n   📊 Sector: {sector} → {s_bias}"
                    f"\n   📰 {t.get('headline', '')[:60]}..."
                )

        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⏰ Engine active. Entry window: 09:15–10:30")
        self.send("\n".join(lines))

    def send_trade_alert(self, direction: str, symbol: str,
                          price: float, quantity: int,
                          strategy: str, sentiment: float,
                          sector: str, is_paper: bool = True):
        """Template 2 — Trade Alert on every order."""
        mode    = "📋 PAPER" if is_paper else "🔴 LIVE"
        icon    = "🚀" if direction == "BUY" else "🔻"
        d_label = "Bought" if direction == "BUY" else "Sold"
        value   = price * quantity

        msg = (
            f"{icon} <b>{mode} {d_label} {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Price    : ₹{price:,.2f}\n"
            f"📦 Quantity : {quantity}\n"
            f"💵 Value    : ₹{value:,.0f}\n"
            f"📈 Strategy : {strategy}\n"
            f"🧠 Sentiment: {sentiment:+.2f}\n"
            f"🏭 Sector   : {sector}\n"
            f"⏰ Time     : {datetime.now().strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def send_exit_alert(self, symbol: str, exit_price: float,
                         reason: str, pnl: float,
                         is_paper: bool = True):
        """Template 2b — Exit Alert."""
        mode  = "📋 PAPER" if is_paper else "🔴 LIVE"
        icon  = "✅" if pnl >= 0 else "❌"
        emoji = {"SL": "🛑", "TARGET": "🎯",
                 "TRAIL": "📉", "KILL_SWITCH": "⚡",
                 "SENTIMENT_REVERSAL": "🔄"}.get(reason, "🔴")

        msg = (
            f"{emoji} <b>{mode} Exit — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Exit Price: ₹{exit_price:,.2f}\n"
            f"📋 Reason    : {reason}\n"
            f"{icon} P&amp;L      : ₹{pnl:,.0f}\n"
            f"⏰ Time      : {datetime.now().strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def send_eod_summary(self, trades: list, daily_pnl: float,
                          is_paper: bool = True):
        """Template 3 — EOD Summary at 15:30 PM."""
        mode    = "📋 PAPER" if is_paper else "🔴 LIVE"
        total   = len(trades)
        wins    = sum(1 for t in trades if (t.pnl or 0) > 0)
        losses  = sum(1 for t in trades if (t.pnl or 0) < 0)
        win_rate = (wins / total * 100) if total > 0 else 0
        pnl_icon = "✅" if daily_pnl >= 0 else "❌"

        msg = (
            f"📊 <b>Morning Beetle — EOD Summary</b>\n"
            f"📅 {datetime.now().strftime('%d %b %Y')}  |  {mode}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Trades    : {total}\n"
            f"✅ Wins      : {wins}\n"
            f"❌ Losses    : {losses}\n"
            f"🎯 Win Rate  : {win_rate:.0f}%\n"
            f"{pnl_icon} Daily P&amp;L : ₹{daily_pnl:,.0f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Kill switch fired. All positions closed.\n"
            f"💾 Data archived to I: drive."
        )
        self.send(msg)

    def send_stop_all_alert(self, daily_pnl: float,
                             is_paper: bool = True):
        """Alert when daily loss limit is breached."""
        mode = "📋 PAPER" if is_paper else "🔴 LIVE"
        msg = (
            f"🛑 <b>{mode} STOP ALL FIRED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Daily loss limit breached!\n"
            f"❌ P&amp;L: ₹{daily_pnl:,.0f}\n"
            f"🔒 No new trades for today.\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self.send(msg)


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    notifier = TelegramNotifier()

    print("Sending burst of 6 test messages to Telegram...\n")

    # Test 1 — Pre-Market Report
    mock_watchlist = [
        {
            "symbol": "INFY",
            "name": "INFOSYS",
            "sentiment_score": -0.963,
            "sector": "NIFTY IT",
            "sector_bias": "BEARISH",
            "headline": "Infosys Q4 preview: PAT may slip 2% QoQ"
        },
        {
            "symbol": "PERSISTENT",
            "name": "PERSISTENT SYSTEMS",
            "sentiment_score": +0.946,
            "sector": "NIFTY IT",
            "sector_bias": "NEUTRAL",
            "headline": "Persistent Systems Q4 results: PAT rises 33%"
        }
    ]
    notifier.send_premarket_report(mock_watchlist, is_paper=True)
    print("✅ Pre-Market Report queued")

    # Test 2 — Burst of trade alerts (this is what failed yesterday)
    for i, sym in enumerate(["NETWEB", "ASIANENE", "HAL", "BEL"]):
        notifier.send_trade_alert(
            direction="BUY", symbol=sym,
            price=1000.0 + i*100, quantity=5,
            strategy="RSIMomentum", sentiment=+0.85,
            sector="NIFTY IT", is_paper=True
        )
        print(f"✅ Trade alert queued: {sym}")

    # Test 3 — Exit alert
    notifier.send_exit_alert(
        symbol="NETWEB", exit_price=1050.0,
        reason="TRAIL", pnl=140.13, is_paper=True
    )
    print("✅ Exit alert queued")

    # Wait for background thread to flush all messages
    print("\nWaiting 10s for delivery...")
    time.sleep(10)

    notifier.shutdown()
    print("\n✅ All 6 messages should have arrived in Telegram.")
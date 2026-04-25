import os
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

load_dotenv("config/.env")
logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Async Telegram bot for Morning Beetle alerts.
    3 message templates per Blueprint:
    1. Pre-Market Report  — sent at 09:14 AM
    2. Trade Alert        — sent on every order
    3. EOD Summary        — sent at 15:30 PM
    """

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing from .env")

        self.bot = Bot(token=self.token)
        logger.info("TelegramNotifier initialised.")

    async def _send(self, message: str):
        """Send a message. Handles errors gracefully."""
        try:
            await self.bot.send_message(
                chat_id    = self.chat_id,
                text       = message,
                parse_mode = "HTML"
            )
            logger.info(f"Telegram sent: {message[:60]}...")
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")

    def send(self, message: str):
        """Synchronous wrapper — safe to call from any thread."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._send(message))
            else:
                loop.run_until_complete(self._send(message))
        except RuntimeError:
            asyncio.run(self._send(message))

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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s — %(message)s")

    notifier = TelegramNotifier()

    print("Sending 3 test messages to Telegram...\n")

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
    print("✅ Pre-Market Report sent")

    import time
    time.sleep(1)

    # Test 2 — Trade Alert
    notifier.send_trade_alert(
        direction="BUY", symbol="PERSISTENT",
        price=5073.14, quantity=9,
        strategy="RSIMomentum", sentiment=+0.946,
        sector="NIFTY IT", is_paper=True
    )
    print("✅ Trade Alert sent")

    time.sleep(1)

    # Test 3 — EOD Summary
    from src.core.trade_db import TradeDB
    db = TradeDB()
    trades = db.get_open_trades()
    notifier.send_eod_summary(trades, daily_pnl=-4264.0, is_paper=True)
    print("✅ EOD Summary sent")

    print("\nCheck your Telegram — 3 messages should have arrived.")
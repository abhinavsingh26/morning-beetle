import os
import sys
import psutil
import pandas as pd
import streamlit as st
from datetime import datetime
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.core.trade_db import TradeDB, Trade, Signal, SystemLog
from src.beetle.sector_heatmap import get_heatmap

# ── Page config ───────────────────────────────────────────────────────
st.set_page_config(
    page_title = "Morning Beetle",
    page_icon  = "🐞",
    layout     = "wide",
    initial_sidebar_state = "collapsed"
)

# ── Custom CSS ────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .metric-card {
        background: #1e2130;
        border-radius: 10px;
        padding: 15px;
        margin: 5px;
        border-left: 4px solid #00d4aa;
    }
    .bearish { border-left-color: #ff4444; }
    .bullish { border-left-color: #00d4aa; }
    .neutral { border-left-color: #888888; }
    h1 { color: #00d4aa; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_db():
    return TradeDB(db_path="trades.db")


def get_today_trades(db: TradeDB) -> list:
    with Session(db.engine) as session:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return session.query(Trade).filter(
            Trade.entry_time >= today
        ).all()


def get_recent_signals(db: TradeDB, limit: int = 10) -> list:
    with Session(db.engine) as session:
        return session.query(Signal).order_by(
            Signal.timestamp.desc()
        ).limit(limit).all()


def get_system_logs(db: TradeDB, limit: int = 5) -> list:
    with Session(db.engine) as session:
        return session.query(SystemLog).order_by(
            SystemLog.timestamp.desc()
        ).limit(limit).all()


# ── Header ────────────────────────────────────────────────────────────
st.markdown("# 🐞 Morning Beetle — Trading Dashboard")
st.markdown(f"*Last updated: {datetime.now().strftime('%d %b %Y %H:%M:%S')}*")
st.divider()

db = get_db()

# ── Row 1: Key Metrics ────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns(5)

today_trades = get_today_trades(db)
daily_pnl    = sum(t.pnl or 0 for t in today_trades)
open_trades  = [t for t in today_trades if t.status == "OPEN"]
closed_trades = [t for t in today_trades if t.status == "CLOSED"]
wins  = sum(1 for t in closed_trades if (t.pnl or 0) > 0)
total_closed = len(closed_trades)
win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

with col1:
    st.metric("Daily P&L", f"₹{daily_pnl:,.0f}",
              delta=f"{'▲' if daily_pnl >= 0 else '▼'} Today")

with col2:
    st.metric("Open Positions", len(open_trades))

with col3:
    st.metric("Trades Today", len(today_trades))

with col4:
    st.metric("Win Rate", f"{win_rate:.0f}%",
              delta=f"{wins}W / {total_closed - wins}L")

with col5:
    # System info
    cpu  = psutil.cpu_percent(interval=0.1)
    ram  = psutil.virtual_memory().percent
    st.metric("System", f"CPU {cpu:.0f}%", delta=f"RAM {ram:.0f}%")

st.divider()

# ── Row 2: Open Positions + Sector Heatmap ────────────────────────────
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📋 Open Positions")
    if open_trades:
        data = []
        for t in open_trades:
            data.append({
                "Symbol":    t.symbol,
                "Direction": t.direction,
                "Entry":     f"₹{t.entry_price:,.2f}",
                "Qty":       t.quantity,
                "Strategy":  t.strategy_name,
                "Time":      t.entry_time.strftime("%H:%M:%S") if t.entry_time else "—"
            })
        st.dataframe(pd.DataFrame(data), use_container_width=True,
                    hide_index=True)
    else:
        st.info("No open positions.")

with col_right:
    st.subheader("🌡️ Sector Heatmap")
    try:
        heatmap = get_heatmap(use_mock_if_closed=True)
        data = []
        for sector, info in heatmap.items():
            bias  = info["bias"]
            icon  = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"
            change = info["change_pct"]
            data.append({
                "Sector": sector,
                "Change%": f"{change:+.2f}%",
                "Bias": f"{icon} {bias}"
            })
        st.dataframe(pd.DataFrame(data), use_container_width=True,
                    hide_index=True)
    except Exception as e:
        st.error(f"Heatmap error: {e}")

st.divider()

# ── Row 3: Today's Trade Log ──────────────────────────────────────────
st.subheader("📊 Today's Trade Log")
if today_trades:
    data = []
    for t in today_trades:
        pnl_str = f"₹{t.pnl:,.0f}" if t.pnl else "—"
        data.append({
            "Symbol":    t.symbol,
            "Direction": t.direction,
            "Entry":     f"₹{t.entry_price:,.2f}",
            "Exit":      f"₹{t.exit_price:,.2f}" if t.exit_price else "—",
            "P&L":       pnl_str,
            "Reason":    t.exit_reason or "OPEN",
            "Strategy":  t.strategy_name,
            "Paper":     "✅" if t.is_paper else "🔴"
        })
    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("No trades today.")

st.divider()

# ── Row 4: Recent Signals + System Log ───────────────────────────────
col_sig, col_log = st.columns([1, 1])

with col_sig:
    st.subheader("⚡ Recent Signals")
    signals = get_recent_signals(db)
    if signals:
        data = []
        for s in signals:
            icon = "✅" if s.status == "APPROVED" else "❌"
            data.append({
                "Time":      s.timestamp.strftime("%H:%M:%S") if s.timestamp else "—",
                "Symbol":    s.symbol,
                "Direction": s.direction,
                "Status":    f"{icon} {s.status}",
                "Reason":    s.block_reason or "—",
                "Strategy":  s.strategy_name
            })
        st.dataframe(pd.DataFrame(data), use_container_width=True,
                    hide_index=True)
    else:
        st.info("No signals yet today.")

with col_log:
    st.subheader("🖥️ System Log")
    logs = get_system_logs(db)
    if logs:
        data = []
        for log in logs:
            data.append({
                "Time":   log.timestamp.strftime("%H:%M:%S") if log.timestamp else "—",
                "Level":  log.level,
                "Event":  log.event,
                "Detail": (log.detail or "")[:50]
            })
        st.dataframe(pd.DataFrame(data), use_container_width=True,
                    hide_index=True)
    else:
        st.info("No system events.")

st.divider()

# ── Auto-refresh ──────────────────────────────────────────────────────
st.markdown("*Dashboard auto-refreshes every 10 seconds*")
st.markdown(
    """
    <meta http-equiv="refresh" content="10">
    """,
    unsafe_allow_html=True
)
# save as recover_sasken.py
from src.core.trade_db import TradeDB

db = TradeDB()
pnl = db.close_trade(
    trade_id    = 22,
    exit_price  = 1634.40,    # Upper circuit price at 14:53
    exit_reason = "MANUAL_RECOVERY_SHUTDOWN_RACE"
)
print(f"Trade #22 closed: SASKEN @ ₹1634.40")
print(f"P&L: ₹{pnl:.2f}")
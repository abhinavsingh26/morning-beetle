import sqlite3
conn = sqlite3.connect('trades.db')
cur = conn.cursor()

# 1. Stuck positions check
open_trades = cur.execute(
    "SELECT id, symbol, status, entry_time FROM trades WHERE status='OPEN'"
).fetchall()
print('OPEN positions:', open_trades if open_trades else 'NONE (clean)')

# 2. Day 10 (May 15) trades
print()
print('=== Day 10 (2026-05-15) trades ===')
rows = cur.execute(
    "SELECT id, symbol, direction, quantity, entry_price, exit_price, pnl, exit_reason, strategy_name, entry_time, exit_time "
    "FROM trades WHERE date(entry_time)='2026-05-15' ORDER BY id"
).fetchall()
for r in rows:
    print(r)
print(f'\nTotal Day 10 trades: {len(rows)}')

if rows:
    closed = [r for r in rows if r[6] is not None]
    wins   = sum(1 for r in closed if r[6] > 0)
    losses = sum(1 for r in closed if r[6] < 0)
    total  = sum(r[6] for r in closed)
    print(f'Wins: {wins}, Losses: {losses}, Net P&L: Rs{total:.2f}')

# 3. Day 10 signals (for context — what was blocked, what fired)
print()
print('=== Day 10 signals (first 20) ===')
sigs = cur.execute(
    "SELECT symbol, direction, strategy_name, sentiment_score, status, block_reason, timestamp "
    "FROM signals WHERE date(timestamp)='2026-05-15' ORDER BY timestamp LIMIT 20"
).fetchall()
for s in sigs:
    print(s)
print(f'\nTotal Day 10 signals: {len(sigs)}')

conn.close()

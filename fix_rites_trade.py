import sqlite3
conn = sqlite3.connect('trades.db')
cur = conn.cursor()

print('BEFORE:')
print(cur.execute('SELECT id, symbol, entry_price, exit_price, pnl FROM trades WHERE id=28').fetchone())

new_pnl = round((213.85 - 212.07) * 70, 2)
cur.execute('UPDATE trades SET exit_price=?, pnl=? WHERE id=28', (213.85, new_pnl))
conn.commit()

print('AFTER:')
print(cur.execute('SELECT id, symbol, entry_price, exit_price, pnl FROM trades WHERE id=28').fetchone())

cur.execute(
    "INSERT INTO system_log (level, event, details, timestamp) "
    "VALUES (?, ?, ?, datetime('now'))",
    (
        'WARNING',
        'MANUAL_CORRECTION',
        'Day 9 RITES trade #28: corrected exit_price 991.90 to 213.85 (kill-switch shared-LTP bug). Old P&L 54588.10, new 124.60.'
    )
)
conn.commit()
conn.close()
print('SUCCESS: trades.db row 28 corrected. Audit log written.')

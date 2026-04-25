import os
import sys
import logging
from datetime import datetime
from kiteconnect import KiteConnect
from dotenv import load_dotenv
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv("config/.env")

from src.core.trade_db import TradeDB, Trade

logger = logging.getLogger(__name__)


def reconcile(db: TradeDB) -> dict:
    """
    Cross-checks trades.db against Kite order history.
    Flags any missing or mismatched orders.
    Returns reconciliation report.
    """
    api_key      = os.getenv("ZERODHA_API_KEY")
    access_token = os.getenv("ZERODHA_ACCESS_TOKEN")

    if not api_key or not access_token:
        raise ValueError("Kite credentials missing")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Fetch today's orders from Kite
    logger.info("Fetching orders from Kite API...")
    try:
        kite_orders = kite.orders()
        logger.info(f"  Kite orders fetched: {len(kite_orders)}")
    except Exception as e:
        logger.error(f"  Kite API error: {e}")
        kite_orders = []

    # Fetch today's trades from DB
    with Session(db.engine) as session:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        db_trades = session.query(Trade).filter(
            Trade.entry_time >= today,
            Trade.is_paper == False   # Only reconcile live trades
        ).all()
        db_trade_list = [
            {
                "order_id": t.order_id,
                "symbol":   t.symbol,
                "direction": t.direction,
                "quantity": t.quantity,
                "price":    t.entry_price
            }
            for t in db_trades
        ]

    logger.info(f"  DB live trades today: {len(db_trade_list)}")

    # Build Kite order lookup
    kite_order_ids = {str(o["order_id"]): o for o in kite_orders}
    db_order_ids   = {t["order_id"]: t for t in db_trade_list}

    matched      = []
    missing_kite = []   # In DB but not in Kite
    missing_db   = []   # In Kite but not in DB

    # Check DB trades against Kite
    for order_id, trade in db_order_ids.items():
        if order_id in kite_order_ids:
            matched.append(order_id)
        else:
            missing_kite.append(trade)
            logger.warning(f"  ⚠️  DB trade not in Kite: {order_id} "
                          f"({trade['symbol']} {trade['direction']})")

    # Check Kite orders against DB
    for order_id, order in kite_order_ids.items():
        if order_id not in db_order_ids:
            if order.get("status") == "COMPLETE":
                missing_db.append(order)
                logger.warning(f"  ⚠️  Kite order not in DB: {order_id} "
                              f"({order.get('tradingsymbol')} "
                              f"{order.get('transaction_type')})")

    report = {
        "date":         datetime.now().strftime("%Y-%m-%d"),
        "db_trades":    len(db_trade_list),
        "kite_orders":  len(kite_orders),
        "matched":      len(matched),
        "missing_kite": missing_kite,
        "missing_db":   missing_db,
        "clean":        len(missing_kite) == 0 and len(missing_db) == 0
    }

    return report


if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s — %(message)s"
    )

    print("── Morning Beetle Trade Reconciler ──\n")

    db     = TradeDB(db_path="trades.db")
    report = reconcile(db)

    print(f"\n── Reconciliation Report ──")
    print(f"  Date         : {report['date']}")
    print(f"  DB trades    : {report['db_trades']}")
    print(f"  Kite orders  : {report['kite_orders']}")
    print(f"  Matched      : {report['matched']}")

    if report["missing_kite"]:
        print(f"\n  ⚠️  In DB but missing from Kite ({len(report['missing_kite'])}):")
        for t in report["missing_kite"]:
            print(f"    {t['order_id']} — {t['symbol']} {t['direction']}")

    if report["missing_db"]:
        print(f"\n  ⚠️  In Kite but missing from DB ({len(report['missing_db'])}):")
        for o in report["missing_db"]:
            print(f"    {o['order_id']} — {o.get('tradingsymbol')} "
                  f"{o.get('transaction_type')}")

    if report["clean"]:
        total = report['matched']
        print(f"\n✅ All {total} orders reconciled. 0 discrepancies.")
    else:
        issues = len(report["missing_kite"]) + len(report["missing_db"])
        print(f"\n⚠️  {issues} discrepancies found. Review above.")

    db.engine.dispose()
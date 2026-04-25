import os
import sys
import shutil
import logging
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
load_dotenv("config/.env")

logger = logging.getLogger(__name__)

# Paths
SSD_BASE  = "C:/Users/Abhinav/MorningBeetle_Dev"
HDD_BASE  = "I:/01_Active_Projects/The_Morning_Star/03_output"


def archive_daily():
    """
    Post-market archiver — runs at 16:00 PM daily.
    Copies trades.db snapshot + live_logs.log to I: drive.
    Clears live_logs.log for next day.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    dest_dir  = os.path.join(HDD_BASE, today)
    timestamp = datetime.now().strftime("%H:%M:%S")

    logger.info(f"Archiver starting — {today}")

    # Create dated folder on I: drive
    os.makedirs(dest_dir, exist_ok=True)
    logger.info(f"  Archive folder: {dest_dir}")

    archived = []
    failed   = []

    # ── Archive trades.db ─────────────────────────────────────────────
    db_src  = os.path.join(SSD_BASE, "trades.db")
    db_dest = os.path.join(dest_dir, f"trades_{today}.db")
    if os.path.exists(db_src):
        shutil.copy2(db_src, db_dest)
        size = os.path.getsize(db_dest) / 1024
        archived.append(f"trades.db → {db_dest} ({size:.1f} KB)")
        logger.info(f"  ✅ trades.db archived ({size:.1f} KB)")
    else:
        failed.append("trades.db not found")
        logger.warning("  ⚠️  trades.db not found")

    # ── Archive live_logs.log ─────────────────────────────────────────
    log_src  = os.path.join(SSD_BASE, "live_logs.log")
    log_dest = os.path.join(dest_dir, f"live_logs_{today}.log")
    if os.path.exists(log_src):
        shutil.copy2(log_src, log_dest)
        size = os.path.getsize(log_dest) / 1024
        archived.append(f"live_logs.log → {log_dest} ({size:.1f} KB)")
        logger.info(f"  ✅ live_logs.log archived ({size:.1f} KB)")

        # Clear live_logs.log for next day
        with open(log_src, "w") as f:
            f.write(f"# Log cleared by archiver at {timestamp} on {today}\n")
        logger.info("  ✅ live_logs.log cleared for next day")
    else:
        failed.append("live_logs.log not found")
        logger.warning("  ⚠️  live_logs.log not found")

    # ── Archive watchlist.json ────────────────────────────────────────
    wl_src  = os.path.join(SSD_BASE, "watchlist.json")
    wl_dest = os.path.join(dest_dir, f"watchlist_{today}.json")
    if os.path.exists(wl_src):
        shutil.copy2(wl_src, wl_dest)
        archived.append(f"watchlist.json → {wl_dest}")
        logger.info(f"  ✅ watchlist.json archived")

    # ── Summary ───────────────────────────────────────────────────────
    logger.info(f"\nArchiver complete — {len(archived)} files archived, "
               f"{len(failed)} failed")

    return {
        "date":     today,
        "dest":     dest_dir,
        "archived": archived,
        "failed":   failed
    }


if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s — %(message)s"
    )

    print("── Morning Beetle Archiver ──\n")
    result = archive_daily()

    print(f"\n── Results ──")
    print(f"  Date     : {result['date']}")
    print(f"  Dest     : {result['dest']}")
    print(f"\n  Archived:")
    for f in result["archived"]:
        print(f"    ✅ {f}")
    if result["failed"]:
        print(f"\n  Failed:")
        for f in result["failed"]:
            print(f"    ❌ {f}")

    if not result["failed"]:
        print(f"\n✅ Archive complete. Check I: drive.")
    else:
        print(f"\n⚠️  Archive completed with warnings.")
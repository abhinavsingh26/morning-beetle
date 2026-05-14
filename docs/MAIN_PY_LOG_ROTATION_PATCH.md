────────────────────────────────────────────────────────────────────
  MAIN.PY LOG ROTATION PATCH — APPLY THIS WEEKEND, NOT TONIGHT
────────────────────────────────────────────────────────────────────

CURRENT (in main.py — somewhere near the top, before any logger calls):

  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s [%(levelname)s] %(message)s",
      handlers=[
          logging.FileHandler("live_logs.log"),
          logging.StreamHandler(),
      ]
  )

REPLACE WITH:

  from src.utils.logging_setup import setup_daily_rotating_logger
  setup_daily_rotating_logger(
      component="engine",
      log_root="logs",
      retention_days=90,
  )

After change, logs land in:
  logs/engine/2026-05-14.log
  logs/engine/2026-05-15.log
  ... (rotated automatically at midnight)
  ... (older than 90 days auto-deleted)

────────────────────────────────────────────────────────────────────
  WHY NOT TONIGHT
────────────────────────────────────────────────────────────────────

The engine isn't running right now (you terminated for Day 8).
This patch is safe to apply tomorrow morning before 09:00, OR over
the weekend. Don't apply tonight — bedtime is for things that don't
need to work tomorrow.

────────────────────────────────────────────────────────────────────
  HOUSEKEEPING
────────────────────────────────────────────────────────────────────

After applying the patch and confirming logs appear in logs/engine/,
you can optionally archive the old live_logs.log:

  Move-Item live_logs.log archive\live_logs_pre_rotation.log

But don't delete it — useful historical reference for the Day 1-8
debugging trail.

────────────────────────────────────────────────────────────────────

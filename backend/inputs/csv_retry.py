"""CSV bulk retry: filenames listed in a CSV in data/csv-retry/ are re-queued."""
import csv
import logging
import os
import shutil
import time

from sqlalchemy import select

from database import async_session
from models import Job
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.inputs")


CSV_RETRY_DIR = os.path.join(
    os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")),
    "csv-retry",
)


async def scan_csv_retry():
    """Scan the csv-retry/ folder for CSV files containing filenames to retry.

    Each CSV should have a column named 'filename' (or be a single-column CSV
    without header). For every filename listed, all matching jobs in status
    'done', 'error', 'review', or 'duplicate' are reset to 'queued' so the
    pipeline worker picks them up.

    Processed CSVs are moved to csv-retry/done/ so they don't get re-read.
    This is a generic, multifunctional retry mechanism — not tied to any
    specific use case (ghost-tags, sidecar repair, etc.).
    """
    if not os.path.isdir(CSV_RETRY_DIR):
        return

    csv_files = [f for f in os.listdir(CSV_RETRY_DIR)
                 if f.lower().endswith(".csv") and os.path.isfile(os.path.join(CSV_RETRY_DIR, f))]
    if not csv_files:
        return

    from pipeline import reset_job_for_retry

    for csv_name in csv_files:
        csv_path = os.path.join(CSV_RETRY_DIR, csv_name)
        try:
            # Read filenames from CSV
            filenames = set()
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if "filename" in (reader.fieldnames or []):
                    # Has header with 'filename' column
                    for row in reader:
                        fn = (row.get("filename") or "").strip()
                        if fn:
                            filenames.add(fn)
                else:
                    # Fallback: single-column CSV without header, or
                    # header not named 'filename' — treat first column as filename
                    f.seek(0)
                    for line in f:
                        fn = line.strip().strip('"').strip("'")
                        if fn and not fn.lower().startswith("filename"):
                            filenames.add(fn)

            if not filenames:
                await log_warning("csv-retry", f"CSV '{csv_name}' contains no filenames, skipping")
                continue

            # Find matching jobs
            total_queued = 0
            total_not_found = 0
            ELIGIBLE = ("done", "error", "review", "duplicate")

            async with async_session() as session:
                for fn in filenames:
                    result = await session.execute(
                        select(Job.id, Job.debug_key, Job.status).where(
                            Job.filename == fn,
                            Job.status.in_(ELIGIBLE),
                        )
                    )
                    rows = result.all()
                    if not rows:
                        total_not_found += 1
                        continue
                    for row in rows:
                        try:
                            ok = await reset_job_for_retry(row.id)
                            if ok:
                                total_queued += 1
                        except Exception as e:
                            logger.warning("csv-retry: reset failed for %s: %s", row.debug_key, e)

            await log_info(
                "csv-retry",
                f"CSV '{csv_name}': {len(filenames)} filenames → "
                f"{total_queued} jobs queued, {total_not_found} not found",
            )

            # Move processed CSV to done/ subfolder
            done_dir = os.path.join(CSV_RETRY_DIR, "done")
            os.makedirs(done_dir, exist_ok=True)
            done_path = os.path.join(done_dir, csv_name)
            if os.path.exists(done_path):
                # Add timestamp to avoid overwriting
                name, ext = os.path.splitext(csv_name)
                done_path = os.path.join(done_dir, f"{name}_{int(time.time())}{ext}")
            shutil.move(csv_path, done_path)

        except Exception as e:
            await log_error("csv-retry", f"Error processing CSV '{csv_name}': {e}")
            logger.error("csv-retry: error processing %s: %s", csv_name, e)

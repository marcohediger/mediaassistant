"""CSV bulk retry: jobs listed in a CSV in data/csv-retry/ are re-queued."""
import asyncio
import csv
import logging
import os

from sqlalchemy import select

import fileops
from database import async_session
from models import Job
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.inputs")


CSV_RETRY_DIR = os.path.join(
    os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")),
    "csv-retry",
)

ELIGIBLE = ("done", "error", "review", "duplicate")


def _read_entries(csv_path: str) -> list[tuple[str | None, str | None]]:
    """(debug_key, filename) per row; either may be None.

    A `debug_key` column names a job exactly. A `filename` column (or a
    single column without header) is only a hint: a name is acted on only
    if exactly one job carries it (R-13).
    """
    entries: dict[tuple, None] = {}
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if "debug_key" in fields or "filename" in fields:
            for row in reader:
                key = (row.get("debug_key") or "").strip() or None
                name = (row.get("filename") or "").strip() or None
                if key or name:
                    entries[(key, name)] = None
        else:
            # Single-column CSV without header, or a header not named
            # 'filename' — the first column is the filename.
            f.seek(0)
            for line in f:
                name = line.strip().strip('"').strip("'")
                if name and not name.lower().startswith("filename"):
                    entries[(None, name)] = None
    return list(entries)


async def _match(debug_key: str | None, filename: str | None) -> list[tuple[int, str]]:
    """(id, debug_key) of the retryable jobs a CSV row names."""
    query = select(Job.id, Job.debug_key).where(Job.status.in_(ELIGIBLE))
    query = query.where(Job.debug_key == debug_key) if debug_key else query.where(Job.filename == filename)
    async with async_session() as session:
        return [tuple(r) for r in (await session.execute(query.limit(20))).all()]


async def scan_csv_retry():
    """Scan the csv-retry/ folder for CSV files naming jobs to retry.

    Each CSV has a column `debug_key` (exact) and/or `filename`, or is a
    single-column CSV of filenames without header. A row is acted on only
    when it names exactly one job in status 'done', 'error', 'review' or
    'duplicate': by its debug key, or by a filename no other such job
    carries. A filename shared by several jobs is skipped with a warning —
    resetting all of them would retry jobs nobody meant (R-13), and a retry
    by name could pick up another file of that name.

    Processed CSVs are moved to csv-retry/done/ so they don't get re-read.
    This is a generic retry mechanism — not tied to any specific use case
    (ghost-tags, sidecar repair, etc.).
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
            entries = await asyncio.to_thread(_read_entries, csv_path)
            if not entries:
                await log_warning("csv-retry", f"CSV '{csv_name}' enthält keine Einträge, übersprungen")
            queued, refused, not_found, ambiguous = 0, 0, [], []
            for debug_key, filename in entries:
                label = debug_key or filename
                jobs = await _match(debug_key, filename)
                if not jobs:
                    not_found.append(label)
                    continue
                if len(jobs) > 1:
                    ambiguous.append(f"{label} ({', '.join(k for _, k in jobs)})")
                    continue
                job_id, key = jobs[0]
                try:
                    if await reset_job_for_retry(job_id, actor="system:csv-retry"):
                        queued += 1
                    else:
                        refused += 1
                except Exception as e:
                    refused += 1
                    logger.warning("csv-retry: reset failed for %s: %s", key, e)
                    await log_warning("csv-retry", f"{key} Retry aus CSV '{csv_name}' fehlgeschlagen",
                                      f"{type(e).__name__}: {e}")

            await log_info(
                "csv-retry",
                f"CSV '{csv_name}': {len(entries)} Einträge → {queued} Jobs eingereiht, "
                f"{refused} abgelehnt, {len(not_found)} nicht gefunden, {len(ambiguous)} mehrdeutig",
                ("nicht gefunden: " + ", ".join(not_found[:50])) if not_found else None,
            )
            if ambiguous:
                await log_warning(
                    "csv-retry",
                    f"CSV '{csv_name}': {len(ambiguous)} Dateinamen mehrdeutig — übersprungen",
                    "Mehrere Jobs tragen denselben Dateinamen; ohne Spalte debug_key ist nicht "
                    "eindeutig, welcher gemeint ist: " + "; ".join(ambiguous[:50]),
                )

            # Move the processed CSV to done/ (never over an older one).
            await fileops.move(None, csv_path, os.path.join(CSV_RETRY_DIR, "done") + os.sep,
                               actor="system:csv-retry")

        except Exception as e:
            await log_error("csv-retry", f"Fehler beim Verarbeiten der CSV '{csv_name}': {e}")
            logger.error("csv-retry: error processing %s: %s", csv_name, e)

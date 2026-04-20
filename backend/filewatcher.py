import asyncio
import concurrent.futures
import csv
import gc
import hashlib
import logging
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, update as sql_update
from sqlalchemy.exc import IntegrityError
from config import config_manager
from database import async_session
from models import Job, InboxDirectory
from system_logger import log_error, log_info, log_warning
from pipeline import run_pipeline

logger = logging.getLogger("mediaassistant.filewatcher")

# Track whether we already logged "outside schedule" to avoid log spam
_schedule_logged = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif", ".bmp", ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".vob", ".asf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


from file_operations import sha256 as _sha256  # shared implementation


# In-memory counter — initialized from DB on first use, then only incremented in memory.
# Eliminates race conditions entirely: no two coroutines can ever get the same counter.
_key_counter: int | None = None
_key_counter_lock = asyncio.Lock()


async def _next_debug_key() -> str:
    """Generate the next debug_key using an in-memory counter (no race conditions)."""
    global _key_counter
    async with _key_counter_lock:
        year = datetime.now().year
        prefix = f"MA-{year}-"
        if _key_counter is None:
            # Initialize from DB — use CAST to numeric for correct MAX with 5+ digit keys
            # (string MAX fails: "9999" > "10000" alphabetically)
            from sqlalchemy import literal_column
            async with async_session() as session:
                result = await session.execute(
                    select(
                        func.max(
                            literal_column("CAST(SUBSTR(debug_key, " + str(len(prefix) + 1) + ") AS INTEGER)")
                        )
                    ).where(Job.debug_key.like(f"{prefix}%"))
                )
                max_num = result.scalar()
                _key_counter = int(max_num) if max_num else 0
        _key_counter += 1
        return f"{prefix}{_key_counter:04d}"


async def _create_job_safe(*, filename, original_path, source_label, source_inbox_path=None,
                           folder_tags=False, dry_run=False, use_immich=False,
                           immich_asset_id=None, immich_user_id=None, file_hash=None) -> Job | None:
    """Create a Job with collision-free debug_key from in-memory counter."""
    debug_key = await _next_debug_key()
    try:
        async with async_session() as session:
            job = Job(
                filename=filename,
                original_path=original_path,
                debug_key=debug_key,
                status="queued",
                source_label=source_label,
                source_inbox_path=source_inbox_path,
                folder_tags=folder_tags,
                dry_run=dry_run,
                use_immich=use_immich,
                immich_asset_id=immich_asset_id,
                immich_user_id=immich_user_id,
                file_hash=file_hash,
            )
            session.add(job)
            await session.commit()
            return job
    except IntegrityError:
        logger.error(f"debug_key collision for {filename} key={debug_key} — resetting counter")
        # Reset counter so next call re-reads from DB
        async with _key_counter_lock:
            global _key_counter
            _key_counter = None
    return None


_SKIP_DIRS = {"@eadir", ".synology", "#recycle"}


def _scan_directory(path: str, min_age: float) -> list[str]:
    """Scan directory for supported media files that are stable (not being written).

    Skips symlinks (both files and dirs) to prevent unbounded recursion
    on symlink loops and to avoid processing files outside the inbox tree.
    """
    files = []
    now = time.time()
    try:
        for entry in os.scandir(path):
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                if ".tmp." in entry.name:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    stat = entry.stat(follow_symlinks=False)
                    if now - stat.st_mtime > min_age:
                        files.append((entry.path, stat.st_size))
            elif entry.is_dir(follow_symlinks=False):
                if entry.name.lower() in _SKIP_DIRS:
                    continue
                files.extend(_scan_directory(entry.path, min_age))
    except PermissionError:
        pass
    return files


# Maximum file size: 10 GB — prevents OOM from excessively large files
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024


def _is_file_stable(filepath: str, expected_size: int) -> bool:
    """Check if file size is stable (not still being copied)."""
    try:
        current_size = os.path.getsize(filepath)
        if current_size == expected_size and current_size > 0:
            return True
        # Size mismatch — file may still be copying, wait briefly and re-check
        time.sleep(1.0)
        current_size = os.path.getsize(filepath)
        return current_size == expected_size and current_size > 0
    except OSError:
        return False


async def _scan_inbox():
    """Scan inboxes, hash files, create queued jobs. Does NOT run pipelines."""
    async with async_session() as session:
        result = await session.execute(
            select(InboxDirectory).where(InboxDirectory.active == True)
        )
        inboxes = result.scalars().all()

    if not inboxes:
        return

    interval = await config_manager.get("filewatcher.interval", 5)
    min_age = max(float(interval), 2.0)

    for inbox in inboxes:
        if not os.path.isdir(inbox.path):
            continue

        # Get paths+hashes of all known jobs for this inbox
        async with async_session() as session:
            existing = await session.execute(
                select(Job.original_path, Job.status, Job.file_hash, Job.error_message, Job.target_path, Job.dry_run).where(
                    Job.original_path.like(f"{inbox.path}%"),
                )
            )
            rows = existing.all()
            active_paths = {r[0] for r in rows if r[1] in ("queued", "processing")}
            done_hashes = set()
            for r in rows:
                path, status, fhash, err, target, dry_run = r
                if not fhash:
                    continue
                if not dry_run and status != "skipped" and os.path.exists(path):
                    continue
                if dry_run and status in ("done", "duplicate"):
                    done_hashes.add((path, fhash))
                    continue
                if status == "skipped":
                    done_hashes.add((path, fhash))
                    continue
                if status == "duplicate" and fhash:
                    done_hashes.add((path, fhash))
                    continue
                if status == "done" and not err:
                    if target and target.startswith("immich:"):
                        done_hashes.add((path, fhash))
                    elif target and os.path.exists(target):
                        done_hashes.add((path, fhash))
                    elif not target:
                        done_hashes.add((path, fhash))

        # Scan for new files
        found_files = await asyncio.to_thread(_scan_directory, inbox.path, min_age)

        # Filter candidates
        candidates = []
        for filepath, file_size in found_files:
            if filepath in active_paths:
                continue
            if file_size > MAX_FILE_SIZE:
                size_gb = file_size / (1024 ** 3)
                await log_warning("filewatcher", f"Skipped (too large): {os.path.basename(filepath)}", f"Size: {size_gb:.1f} GB, limit: {MAX_FILE_SIZE / (1024 ** 3):.0f} GB")
                continue
            candidates.append((filepath, file_size))

        if not candidates:
            continue

        # Hash + stability check (runs in thread)
        def _hash_and_check(filepath, file_size):
            if not _is_file_stable(filepath, file_size):
                return filepath, file_size, None
            return filepath, file_size, _sha256(filepath)

        # Process all candidates: hash in parallel (4 threads), create jobs as each completes
        use_folder_tags = inbox.folder_tags and await config_manager.is_module_enabled("ordner_tags")
        total_queued = 0
        sem = asyncio.Semaphore(4)

        async def _hash_one(fp, fs):
            async with sem:
                return await asyncio.to_thread(_hash_and_check, fp, fs)

        tasks = [_hash_one(fp, fs) for fp, fs in candidates]
        for coro in asyncio.as_completed(tasks):
            try:
                filepath, file_size, file_hash = await coro
                if file_hash is None:
                    continue
                if (filepath, file_hash) in done_hashes:
                    continue

                filename = os.path.basename(filepath)
                job = await _create_job_safe(
                    filename=filename,
                    original_path=filepath,
                    source_label=inbox.label,
                    source_inbox_path=inbox.path,
                    folder_tags=use_folder_tags,
                    dry_run=inbox.dry_run,
                    use_immich=inbox.use_immich,
                    immich_user_id=inbox.immich_user_id,
                    file_hash=file_hash,
                )
                if job:
                    total_queued += 1
                    logger.info(f"New file: {filename} → {job.debug_key} (Inbox: {inbox.label})")
            except Exception as e:
                logger.error(f"Error queuing file: {e}")

        if total_queued:
            try:
                await log_info("filewatcher", f"{total_queued} new files queued from {inbox.label}")
            except Exception:
                logger.info(f"{total_queued} new files queued from {inbox.label} (log_info failed, DB busy)")


async def _run_job(job_id: int, filename: str, debug_key: str):
    """Run a single pipeline job (used as asyncio task)."""
    logger.info(f"Pipeline start: {debug_key} ({filename})")
    try:
        await run_pipeline(job_id)
    except Exception as e:
        # Crash outside run_pipeline's own error handling. Without this
        # fallback the job would remain 'processing' until the 15-min
        # stale-recovery kicks in, blocking a worker slot silently.
        tb = traceback.format_exc()
        logger.error(f"Pipeline error {debug_key}: {e}\n{tb}")
        try:
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job and job.status == "processing":
                    job.status = "error"
                    job.error_message = f"Pipeline-Crash: {type(e).__name__}: {e}"
                    await session.commit()
                    await log_error(
                        "filewatcher",
                        f"{debug_key} Pipeline-Crash — Status auf error gesetzt",
                        f"{type(e).__name__}: {e}\n\n{tb}",
                    )
        except Exception as inner:
            logger.error(f"Failed to mark {debug_key} as error after crash: {inner}")
    logger.info(f"Pipeline done: {debug_key} ({filename})")


async def _poll_immich():
    """Poll Immich for new assets and process them through the pipeline."""
    import shutil
    from immich_client import get_recent_assets, download_asset, get_immich_config, get_user_api_key
    from models import ImmichUser

    # Get last poll timestamp
    last_poll = await config_manager.get("immich.last_poll", None)

    # First activation: set timestamp to now, skip existing assets
    now = datetime.now(timezone.utc).isoformat()
    if not last_poll:
        await config_manager.set("immich.last_poll", now)
        await log_info("immich_poll", "First activation — skipping existing assets, polling starts from now")
        return

    # Build list of (user_id | None, label, api_key) to poll
    async with async_session() as session:
        result = await session.execute(
            select(ImmichUser).where(ImmichUser.active == True)
        )
        immich_users = result.scalars().all()

    user_keys: list[tuple[int | None, str, str]] = []
    # Always poll with global API key
    _, global_key = await get_immich_config()
    if global_key:
        user_keys.append((None, "Global", global_key))
    # Additionally poll each configured Immich user
    for u in immich_users:
        key = await get_user_api_key(u.id)
        if key:
            user_keys.append((u.id, u.label, key))

    if not user_keys:
        return

    # Pre-load already processed asset IDs and file hashes
    async with async_session() as session:
        existing_by_id_result = await session.execute(
            select(Job.immich_asset_id).where(Job.immich_asset_id.isnot(None))
        )
        already_by_id = {row[0] for row in existing_by_id_result.all()}

        existing_hashes = await session.execute(
            select(Job.file_hash, Job.immich_user_id).where(
                Job.source_label == "Immich",
                Job.file_hash.isnot(None),
            )
        )
        processed_hashes_by_user: dict[int | None, set[str]] = {}
        for row in existing_hashes.all():
            processed_hashes_by_user.setdefault(row[1], set()).add(row[0])

    # Poll each user
    for user_id, user_label, api_key in user_keys:
        try:
            assets = await get_recent_assets(since=last_poll, api_key=api_key)
        except Exception as e:
            await log_error("immich_poll", f"Failed for user {user_label}", str(e))
            continue

        new_assets = [
            a for a in assets
            if a["id"] not in already_by_id
            and a.get("deviceId") != "MediaAssistant"
        ]
        if not new_assets:
            continue

        await log_info("immich_poll", f"{len(new_assets)} new assets for {user_label}")

        for asset in new_assets:
            asset_id = asset["id"]
            filename = asset.get("originalFileName", f"{asset_id}.jpg")

            ext = os.path.splitext(filename)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            tmp_dir = tempfile.mkdtemp(prefix="ma_immich_")
            try:
                file_path = await download_asset(asset_id, tmp_dir, api_key=api_key)
            except Exception as e:
                await log_error("immich_poll", f"Download failed: {filename}", str(e))
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue

            file_hash = await asyncio.to_thread(_sha256, file_path)

            user_hashes = processed_hashes_by_user.get(user_id, set())
            if file_hash in user_hashes:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue

            # Track this asset to avoid duplicates within the same poll cycle
            already_by_id.add(asset_id)
            processed_hashes_by_user.setdefault(user_id, set()).add(file_hash)

            job = await _create_job_safe(
                filename=filename,
                original_path=file_path,
                source_label="Immich",
                use_immich=True,
                immich_asset_id=asset_id,
                immich_user_id=user_id,
                file_hash=file_hash,
            )
            if not job:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue

            await log_info("immich_poll", f"Processing: {filename}", f"User: {user_label}, Asset: {asset_id}, Key: {job.debug_key}")
            await run_pipeline(job.id)

    # Persist poll cursor with a 5-minute overlap buffer. Dedup via
    # `already_by_id` (asset-id) and `processed_hashes_by_user` (sha256)
    # makes the overlap safe — each asset is guaranteed to be processed
    # at most once. The buffer protects against clock skew between
    # MediaAssistant and Immich, and against assets arriving in the
    # interval between `now = ...` (line 277) and the HTTP request.
    overlap_cursor = (
        datetime.fromisoformat(now) - timedelta(minutes=5)
    ).isoformat()
    await config_manager.set("immich.last_poll", overlap_cursor)


async def _is_within_schedule() -> bool:
    """Check if the current time is within the configured schedule.

    Returns True if processing should happen now, False otherwise.
    Schedule modes:
    - continuous: always True
    - manual: always False (only via manual trigger)
    - window: True if current time is between window_start and window_end
    - scheduled: True if current weekday is in scheduled_days AND current time matches scheduled_time (within interval)
    """
    global _schedule_logged
    mode = await config_manager.get("filewatcher.schedule_mode", "continuous")

    if mode == "continuous":
        _schedule_logged = False
        return True

    if mode == "manual":
        if not _schedule_logged:
            logger.info("Schedule mode: manual — skipping automatic processing")
            _schedule_logged = True
        return False

    now = datetime.now()

    if mode == "window":
        start_str = await config_manager.get("filewatcher.window_start", "22:00")
        end_str = await config_manager.get("filewatcher.window_end", "06:00")
        try:
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
        except (ValueError, AttributeError):
            return True  # invalid config → fallback to continuous

        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            # Same-day window (e.g. 08:00–18:00)
            in_window = start_minutes <= current_minutes < end_minutes
        else:
            # Overnight window (e.g. 22:00–06:00)
            in_window = current_minutes >= start_minutes or current_minutes < end_minutes

        if not in_window and not _schedule_logged:
            logger.info(f"Outside time window ({start_str}–{end_str}), pausing")
            _schedule_logged = True
        elif in_window:
            _schedule_logged = False
        return in_window

    if mode == "scheduled":
        days_str = await config_manager.get("filewatcher.scheduled_days", "0,1,2,3,4")
        time_str = await config_manager.get("filewatcher.scheduled_time", "23:00")
        try:
            allowed_days = {int(d.strip()) for d in days_str.split(",")}
        except (ValueError, AttributeError):
            allowed_days = set(range(7))
        try:
            sched_h, sched_m = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            return True

        if now.weekday() not in allowed_days:
            if not _schedule_logged:
                logger.info(f"Scheduled mode: today (weekday {now.weekday()}) not in schedule")
                _schedule_logged = True
            return False

        # Allow processing within a 1-hour window starting at scheduled_time
        interval = await config_manager.get("filewatcher.interval", 5)
        sched_minutes = sched_h * 60 + sched_m
        current_minutes = now.hour * 60 + now.minute
        window = max(int(interval), 60)  # at least 60 minutes
        in_window = sched_minutes <= current_minutes < sched_minutes + window

        if not in_window and not _schedule_logged:
            logger.info(f"Scheduled mode: outside window ({time_str}+{window}min)")
            _schedule_logged = True
        elif in_window:
            _schedule_logged = False
        return in_window

    return True  # unknown mode → fallback to continuous


# Flag for manual trigger (set via API, consumed by filewatcher loop)
_manual_trigger = asyncio.Event()


def trigger_manual_scan():
    """Trigger a single scan cycle from the API (for manual mode)."""
    _manual_trigger.set()


CSV_RETRY_DIR = os.path.join(
    os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")),
    "csv-retry",
)


async def _scan_csv_retry():
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


async def start_filewatcher(shutdown_event: asyncio.Event):
    """Main filewatcher loop, runs as background task."""
    logger.info("Filewatcher started")
    # Resume interrupted jobs on startup (max 3 retries to prevent infinite loops)
    MAX_RETRIES = 3
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "processing")
        )
        interrupted = result.scalars().all()
        for job in interrupted:
            retry = (job.retry_count or 0) + 1
            if retry > MAX_RETRIES:
                job.status = "error"
                job.error_message = f"Max retries ({MAX_RETRIES}) exceeded — job keeps crashing"
                await session.commit()
                await log_error("filewatcher", f"{job.debug_key} abandoned after {MAX_RETRIES} retries", job.filename)
                logger.error(f"Job {job.debug_key} abandoned after {MAX_RETRIES} retries")
                continue
            job.retry_count = retry
            # Reset to queued so run_pipeline's atomic claim accepts it.
            job.status = "queued"
            await session.commit()
            await log_info("filewatcher", f"Job resumed (attempt {retry}/{MAX_RETRIES})", f"{job.debug_key} ({job.filename})")
            logger.info(f"Resuming {job.debug_key} (attempt {retry}/{MAX_RETRIES})")
            await run_pipeline(job.id)

    # Start pipeline worker as separate background task
    worker_task = asyncio.create_task(_pipeline_worker(shutdown_event))

    while not shutdown_event.is_set():
        try:
            if await config_manager.is_module_enabled("filewatcher"):
                should_scan = await _is_within_schedule()
                if _manual_trigger.is_set():
                    _manual_trigger.clear()
                    should_scan = True
                    try:
                        await log_info("filewatcher", "Manual scan triggered")
                    except Exception:
                        logger.info("Manual scan triggered (log_info failed, DB busy)")

                if should_scan:
                    await _scan_inbox()
        except Exception as e:
            await log_error("filewatcher", f"Scan error: {e}")
            logger.error(f"Scan error: {e}")

        try:
            paused = await config_manager.get("pipeline.paused", False)
            if not paused and await config_manager.is_module_enabled("immich"):
                poll_enabled = await config_manager.get("immich.poll_enabled", False)
                if poll_enabled:
                    if await _is_within_schedule() or _manual_trigger.is_set():
                        await _poll_immich()
        except Exception as e:
            await log_error("immich_poll", f"Poll error: {e}")
            logger.error(f"Immich poll error: {e}")

        # CSV-Retry: scan /app/data/csv-retry/ for bulk-retry CSVs
        try:
            await _scan_csv_retry()
        except Exception as e:
            logger.error(f"csv-retry scan error: {e}")

        interval = await config_manager.get("filewatcher.interval", 5)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass

    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


async def _pipeline_worker(shutdown_event: asyncio.Event):
    """Background worker: continuously fills free slots with queued jobs.

    Instead of batch processing (start N, wait for all, repeat), this
    worker checks every second for free slots and immediately starts
    new jobs — keeping all slots busy with even load distribution.
    """
    from ai_backends import get_total_slots

    STALE_TIMEOUT_S = 15 * 60  # 15 minutes without progress → stale
    STALE_MAX_RETRIES = 3
    STALE_CHECK_INTERVAL_S = 60

    logger.info("Pipeline worker started")
    running: set[asyncio.Task] = set()
    # Map job_id → asyncio.Task so stale recovery can cancel hung tasks
    running_jobs: dict[int, asyncio.Task] = {}
    jobs_since_gc = 0
    last_pause_log = 0  # rate-limit "paused" log to once per 60s
    last_stale_check = 0.0

    while not shutdown_event.is_set():
        try:
            # Clean up finished tasks
            done = {t for t in running if t.done()}
            for t in done:
                running.discard(t)
                # Remove from running_jobs map
                running_jobs = {jid: task for jid, task in running_jobs.items() if task is not t}
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.error(f"Pipeline task failed: {exc}")
                jobs_since_gc += 1

            if jobs_since_gc >= 10:
                gc.collect()
                jobs_since_gc = 0

            # ── Stale processing recovery ─────────────────────────────
            # Detect jobs stuck in 'processing' for longer than
            # STALE_TIMEOUT_S without any DB update (updated_at stale).
            # Increment retry_count and requeue, or mark error after
            # STALE_MAX_RETRIES attempts.
            now_mono = time.monotonic()
            if now_mono - last_stale_check >= STALE_CHECK_INTERVAL_S:
                last_stale_check = now_mono
                from datetime import timedelta as _td
                cutoff = datetime.now() - _td(seconds=STALE_TIMEOUT_S)
                async with async_session() as session:
                    result = await session.execute(
                        select(Job).where(
                            Job.status == "processing",
                            Job.updated_at < cutoff,
                        )
                    )
                    stale_jobs = result.scalars().all()
                    for job in stale_jobs:
                        retry = (job.retry_count or 0) + 1
                        if retry > STALE_MAX_RETRIES:
                            job.status = "error"
                            job.error_message = (
                                f"Job hängt wiederholt bei {job.current_step} "
                                f"({STALE_MAX_RETRIES}x Timeout) — aufgegeben"
                            )
                            await session.commit()
                            await log_error(
                                "pipeline",
                                f"{job.debug_key} abandoned — stale {STALE_MAX_RETRIES}x",
                                f"{job.filename} hing wiederholt bei {job.current_step}",
                            )
                            logger.error(
                                "Job %s abandoned after %d stale timeouts at %s",
                                job.debug_key, STALE_MAX_RETRIES, job.current_step,
                            )
                        elif not os.path.exists(job.original_path):
                            # Source file gone (e.g. temp inbox file cleaned up
                            # while the job was stuck) — requeueing would just
                            # fail again at IA-01 or IA-08.
                            job.status = "error"
                            job.error_message = (
                                f"Stale bei {job.current_step} — Quelldatei "
                                f"nicht mehr vorhanden: {job.original_path}"
                            )
                            await session.commit()
                            await log_error(
                                "pipeline",
                                f"{job.debug_key} stale + Datei fehlt",
                                f"{job.filename}: {job.original_path} existiert nicht mehr",
                            )
                            logger.error(
                                "Stale job %s — source file missing: %s",
                                job.debug_key, job.original_path,
                            )
                        else:
                            job.status = "queued"
                            job.retry_count = retry
                            # Drop the stuck step so it re-runs from scratch
                            sr = dict(job.step_result or {})
                            sr.pop(job.current_step, None)
                            job.step_result = sr
                            from sqlalchemy.orm.attributes import flag_modified
                            flag_modified(job, "step_result")
                            await session.commit()
                            await log_warning(
                                "pipeline",
                                f"{job.debug_key} stale recovery (attempt {retry}/{STALE_MAX_RETRIES})",
                                f"{job.filename} hing >={STALE_TIMEOUT_S // 60} Min bei {job.current_step}",
                            )
                            logger.warning(
                                "Stale job %s at %s — requeued (attempt %d/%d)",
                                job.debug_key, job.current_step, retry, STALE_MAX_RETRIES,
                            )
                        # Cancel the hung asyncio task if we still track it
                        hung_task = running_jobs.pop(job.id, None)
                        if hung_task and not hung_task.done():
                            hung_task.cancel()
                            running.discard(hung_task)

            # Drain & pause: if pipeline.paused config is set, the worker
            # finishes its currently running tasks but does NOT pull new
            # queued jobs. Used by the "Pause Pipeline" UI button so the
            # user can stop the worker cleanly before docker stop, even
            # when there are already jobs in 'queued' state.
            paused = await config_manager.get("pipeline.paused", False)
            if paused:
                now = time.time()
                if now - last_pause_log > 60:
                    logger.info(
                        "Pipeline paused — %d running, drainage in progress",
                        len(running),
                    )
                    last_pause_log = now
                # Don't pull new jobs, but keep the loop alive so running
                # tasks can finish and be cleaned up next iteration
                await asyncio.sleep(1)
                continue

            # Fill free slots with queued jobs
            max_concurrent = await get_total_slots()
            free_slots = max_concurrent - len(running)

            if free_slots > 0:
                async with async_session() as session:
                    result = await session.execute(
                        select(Job).where(Job.status == "queued")
                        .order_by(Job.created_at).limit(free_slots)
                    )
                    new_jobs = result.scalars().all()

                for job in new_jobs:
                    task = asyncio.create_task(
                        _run_job(job.id, job.filename, job.debug_key)
                    )
                    running.add(task)
                    running_jobs[job.id] = task
                    # Staggered start: 0.3s apart so DB writes / atomic
                    # claims don't all fire in the same microsecond but
                    # parallelism still ramps up quickly. Was 2s in
                    # earlier versions which made effective concurrency
                    # very low for jobs that complete in <10s.
                    if len(new_jobs) > 1:
                        await asyncio.sleep(0.3)

        except Exception as e:
            try:
                await log_error("pipeline_worker", f"Pipeline error: {e}")
            except Exception:
                pass
            logger.error(f"Pipeline worker error: {e}")

        await asyncio.sleep(1)

    # Graceful shutdown: wait for running tasks
    if running:
        await asyncio.gather(*running, return_exceptions=True)

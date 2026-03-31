import asyncio
import hashlib
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, InboxDirectory
from system_logger import log_error, log_info, log_warning
from pipeline import run_pipeline

logger = logging.getLogger("mediaassistant.filewatcher")

# Track whether we already logged "outside schedule" to avoid log spam
_schedule_logged = False

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif", ".bmp", ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def _generate_debug_key(session) -> str:
    year = datetime.now().year
    prefix = f"MA-{year}-"
    result = await session.execute(
        select(func.max(Job.debug_key)).where(Job.debug_key.like(f"{prefix}%"))
    )
    max_key = result.scalar()
    if max_key:
        counter = int(max_key.split("-")[-1]) + 1
    else:
        counter = 1
    return f"{prefix}{counter:04d}"


def _scan_directory(path: str, min_age: float) -> list[str]:
    """Scan directory for supported media files that are stable (not being written)."""
    files = []
    now = time.time()
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                if ".tmp." in entry.name:
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    stat = entry.stat()
                    if now - stat.st_mtime > min_age:
                        files.append((entry.path, stat.st_size))
            elif entry.is_dir():
                files.extend(_scan_directory(entry.path, min_age))
    except PermissionError:
        pass
    return files


# Maximum file size: 10 GB — prevents OOM from excessively large files
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024


def _is_file_stable(filepath: str, expected_size: int, wait: float = 2.0) -> bool:
    """Check if file size is stable (not still being copied)."""
    try:
        time.sleep(wait)
        current_size = os.path.getsize(filepath)
        return current_size == expected_size and current_size > 0
    except OSError:
        return False


async def _scan_and_process():
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
            # Active jobs: never re-process
            active_paths = {r[0] for r in rows if r[1] in ("queued", "processing")}
            # Successful jobs (done + no errors) OR duplicate jobs: skip same file
            # Bug 1 fix: Include dry_run jobs to prevent endless re-processing
            # Bug 2 fix: Only skip if target file still exists
            done_hashes = set()
            for r in rows:
                path, status, fhash, err, target, dry_run = r
                if not fhash:
                    continue

                # Core rule: if the source file is still at its original
                # inbox path, it was never moved — always re-process it
                # (IA-02 will handle duplicate detection).
                # Exception: dry-run jobs intentionally leave the file in place.
                if not dry_run and os.path.exists(path):
                    continue

                # Dry-run jobs: always skip (file stays in inbox by design)
                if dry_run and status in ("done", "duplicate"):
                    done_hashes.add((path, fhash))
                    continue
                # Duplicate jobs: file was moved to duplicates/ → skip
                if status == "duplicate" and fhash:
                    done_hashes.add((path, fhash))
                    continue
                # Done jobs without errors: skip if target exists
                if status == "done" and not err:
                    if target and target.startswith("immich:"):
                        done_hashes.add((path, fhash))
                    elif target and os.path.exists(target):
                        done_hashes.add((path, fhash))
                    elif not target:
                        done_hashes.add((path, fhash))
                    # else: target file missing → allow re-import

        # Scan for new files (returns list of (path, size) tuples)
        found_files = await asyncio.to_thread(_scan_directory, inbox.path, min_age)

        # Phase 1: Create all jobs as "queued" first
        new_job_ids = []
        for filepath, file_size in found_files:
            if filepath in active_paths:
                continue

            # Stability check
            stable = await asyncio.to_thread(_is_file_stable, filepath, file_size)
            if not stable:
                size_mb = file_size / (1024 * 1024)
                await log_info("filewatcher", f"Skipped (unstable): {os.path.basename(filepath)}", f"Size: {size_mb:.1f} MB")
                logger.info(f"Skipped (unstable): {os.path.basename(filepath)} ({size_mb:.1f} MB)")
                continue

            # File size limit — prevent OOM from excessively large files
            if file_size > MAX_FILE_SIZE:
                size_gb = file_size / (1024 ** 3)
                await log_warning("filewatcher", f"Skipped (too large): {os.path.basename(filepath)}", f"Size: {size_gb:.1f} GB, limit: {MAX_FILE_SIZE / (1024 ** 3):.0f} GB")
                logger.warning(f"Skipped (too large): {os.path.basename(filepath)} ({size_gb:.1f} GB)")
                continue

            filename = os.path.basename(filepath)
            file_hash = await asyncio.to_thread(_sha256, filepath)

            # Skip if same file (same path + hash) was already processed successfully
            if (filepath, file_hash) in done_hashes:
                continue

            async with async_session() as session:
                debug_key = await _generate_debug_key(session)
                job = Job(
                    filename=filename,
                    original_path=filepath,
                    debug_key=debug_key,
                    status="queued",
                    source_label=inbox.label,
                    source_inbox_path=inbox.path if inbox.folder_tags else None,
                    dry_run=inbox.dry_run,
                    use_immich=inbox.use_immich,
                    file_hash=file_hash,
                )
                session.add(job)
                await session.commit()
                new_job_ids.append((job.id, filename, debug_key))

            await log_info("filewatcher", f"New file detected: {filename}", f"Inbox: {inbox.label}, Key: {debug_key}")
            logger.info(f"New file: {filename} → {debug_key} (Inbox: {inbox.label})")

        # Phase 2: Process queued jobs one by one
        for job_id, filename, debug_key in new_job_ids:
            logger.info(f"Pipeline start: {debug_key} ({filename})")
            await run_pipeline(job_id)
            logger.info(f"Pipeline done: {debug_key} ({filename})")


async def _poll_immich():
    """Poll Immich for new assets and process them through the pipeline."""
    from immich_client import get_recent_assets, download_asset

    # Get last poll timestamp
    last_poll = await config_manager.get("immich.last_poll", None)

    # First activation: set timestamp to now, skip existing assets
    now = datetime.now(timezone.utc).isoformat()
    if not last_poll:
        await config_manager.set("immich.last_poll", now)
        await log_info("immich_poll", "First activation — skipping existing assets, polling starts from now")
        return

    try:
        assets = await get_recent_assets(since=last_poll)
    except Exception as e:
        await log_error("immich_poll", f"Failed to fetch assets from Immich", str(e))
        return

    if not assets:
        await config_manager.set("immich.last_poll", now)
        return

    # Filter out assets already processed
    # Check by immich_asset_id AND by file_hash (catches replaced assets with new IDs)
    asset_ids = [a["id"] for a in assets]
    async with async_session() as session:
        existing_by_id = await session.execute(
            select(Job.immich_asset_id).where(
                Job.immich_asset_id.in_(asset_ids)
            )
        )
        already_by_id = {row[0] for row in existing_by_id.all()}

        # Get file hashes from all Immich-sourced done jobs
        existing_hashes = await session.execute(
            select(Job.file_hash).where(
                Job.source_label == "Immich",
                Job.file_hash.isnot(None),
                Job.status.in_(("done", "duplicate")),
            )
        )
        processed_hashes = {row[0] for row in existing_hashes.all()}

        # Also get file hashes from inbox-uploaded jobs (uploaded via use_immich)
        uploaded_hashes = await session.execute(
            select(Job.file_hash).where(
                Job.use_immich == True,
                Job.file_hash.isnot(None),
                Job.status.in_(("done", "duplicate")),
            )
        )
        processed_hashes.update(row[0] for row in uploaded_hashes.all())

    new_assets = [a for a in assets if a["id"] not in already_by_id]
    if not new_assets:
        await config_manager.set("immich.last_poll", now)
        return

    await log_info("immich_poll", f"{len(new_assets)} new assets found in Immich")

    # Process each new asset
    for asset in new_assets:
        asset_id = asset["id"]
        filename = asset.get("originalFileName", f"{asset_id}.jpg")

        # Check file extension
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue

        # Download to temp directory
        tmp_dir = tempfile.mkdtemp(prefix="ma_immich_")
        try:
            file_path = await download_asset(asset_id, tmp_dir)
        except Exception as e:
            await log_error("immich_poll", f"Download failed: {filename}", str(e))
            # Clean up temp dir including any partial downloads
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue

        file_hash = await asyncio.to_thread(_sha256, file_path)

        # Skip if this file was already processed (catches replaced assets with new IDs)
        if file_hash in processed_hashes:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            continue

        async with async_session() as session:
            debug_key = await _generate_debug_key(session)
            job = Job(
                filename=filename,
                original_path=file_path,
                debug_key=debug_key,
                status="queued",
                source_label="Immich",
                use_immich=True,
                immich_asset_id=asset_id,
                file_hash=file_hash,
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        await log_info("immich_poll", f"Processing: {filename}", f"Asset: {asset_id}, Key: {debug_key}")
        await run_pipeline(job_id)

    await config_manager.set("immich.last_poll", now)


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
            await session.commit()
            await log_info("filewatcher", f"Job resumed (attempt {retry}/{MAX_RETRIES})", f"{job.debug_key} ({job.filename})")
            logger.info(f"Resuming {job.debug_key} (attempt {retry}/{MAX_RETRIES})")
            await run_pipeline(job.id)

    while not shutdown_event.is_set():
        try:
            if await config_manager.is_module_enabled("filewatcher"):
                should_scan = await _is_within_schedule()
                # Manual trigger overrides schedule
                if _manual_trigger.is_set():
                    _manual_trigger.clear()
                    should_scan = True
                    await log_info("filewatcher", "Manual scan triggered")

                if should_scan:
                    await _scan_and_process()
        except Exception as e:
            await log_error("filewatcher", f"Scan error: {e}")
            logger.error(f"Scan error: {e}")

        try:
            if await config_manager.is_module_enabled("immich"):
                poll_enabled = await config_manager.get("immich.poll_enabled", False)
                if poll_enabled:
                    # Immich polling follows same schedule
                    if await _is_within_schedule() or _manual_trigger.is_set():
                        await _poll_immich()
        except Exception as e:
            await log_error("immich_poll", f"Poll error: {e}")
            logger.error(f"Immich poll error: {e}")

        interval = await config_manager.get("filewatcher.interval", 5)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass

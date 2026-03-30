import asyncio
import hashlib
import os
import tempfile
import time
from datetime import datetime, timezone
from sqlalchemy import select, func
from config import config_manager
from database import async_session
from models import Job, InboxDirectory
from system_logger import log_error, log_info
from pipeline import run_pipeline

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
                select(Job.original_path, Job.status, Job.file_hash, Job.error_message).where(
                    Job.original_path.like(f"{inbox.path}%"),
                )
            )
            rows = existing.all()
            # Active jobs: never re-process
            active_paths = {r[0] for r in rows if r[1] in ("queued", "processing")}
            # Only truly successful jobs (done + no errors): skip same file
            done_hashes = {(r[0], r[2]) for r in rows if r[1] == "done" and r[2] and not r[3]}

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

        # Phase 2: Process queued jobs one by one
        for job_id, filename, debug_key in new_job_ids:
            await run_pipeline(job_id)


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

    # Filter out assets already processed (by immich_asset_id in jobs table)
    asset_ids = [a["id"] for a in assets]
    async with async_session() as session:
        existing = await session.execute(
            select(Job.immich_asset_id).where(
                Job.immich_asset_id.in_(asset_ids)
            )
        )
        already_processed = {row[0] for row in existing.all()}

    new_assets = [a for a in assets if a["id"] not in already_processed]
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
            # Clean up temp dir
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
            continue

        file_hash = await asyncio.to_thread(_sha256, file_path)

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


async def start_filewatcher(shutdown_event: asyncio.Event):
    """Main filewatcher loop, runs as background task."""
    # Resume interrupted jobs on startup
    async with async_session() as session:
        result = await session.execute(
            select(Job.id).where(Job.status == "processing")
        )
        interrupted = result.scalars().all()
        for job_id in interrupted:
            await log_info("filewatcher", f"Job resumed", f"Job-ID: {job_id}")
            await run_pipeline(job_id)

    while not shutdown_event.is_set():
        try:
            if await config_manager.is_module_enabled("filewatcher"):
                await _scan_and_process()
        except Exception as e:
            await log_error("filewatcher", f"Scan error: {e}")

        try:
            if await config_manager.is_module_enabled("immich"):
                poll_enabled = await config_manager.get("immich.poll_enabled", False)
                if poll_enabled:
                    await _poll_immich()
        except Exception as e:
            await log_error("immich_poll", f"Poll error: {e}")

        interval = await config_manager.get("filewatcher.interval", 5)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass

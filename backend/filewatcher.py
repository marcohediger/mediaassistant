import asyncio
import hashlib
import os
import time
from datetime import datetime
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
                    if now - entry.stat().st_mtime > min_age:
                        files.append(entry.path)
            elif entry.is_dir():
                files.extend(_scan_directory(entry.path, min_age))
    except PermissionError:
        pass
    return files


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

        # Get paths of jobs that are still active (queued/processing)
        async with async_session() as session:
            existing = await session.execute(
                select(Job.original_path).where(
                    Job.original_path.like(f"{inbox.path}%"),
                    Job.status.in_(("queued", "processing")),
                )
            )
            known_paths = {row[0] for row in existing.all()}

        # Scan for new files
        found_files = await asyncio.to_thread(_scan_directory, inbox.path, min_age)

        for filepath in found_files:
            if filepath in known_paths:
                continue

            filename = os.path.basename(filepath)
            file_hash = await asyncio.to_thread(_sha256, filepath)

            async with async_session() as session:
                debug_key = await _generate_debug_key(session)
                job = Job(
                    filename=filename,
                    original_path=filepath,
                    debug_key=debug_key,
                    status="queued",
                    source_label=inbox.label,
                    source_inbox_path=inbox.path if inbox.folder_tags else None,
                    file_hash=file_hash,
                )
                session.add(job)
                await session.commit()
                job_id = job.id

            await log_info("filewatcher", f"New file detected: {filename}", f"Inbox: {inbox.label}, Key: {debug_key}")
            await run_pipeline(job_id)


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

        interval = await config_manager.get("filewatcher.interval", 5)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass

"""Shared pieces of all inputs: file types, directory walking, job creation."""
import asyncio
import logging
import os
import time
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from database import async_session
from models import Job

logger = logging.getLogger("mediaassistant.inputs")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif", ".bmp", ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".vob", ".asf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

_SKIP_DIRS = {"@eadir", ".synology", "#recycle"}

# Maximum file size: 10 GB — prevents OOM from excessively large files
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024

# An Immich job is created before its file exists: the worker downloads the
# original when it starts the job. Until then original_path carries this
# marker plus the asset id.
IMMICH_PENDING = "immich-pending:"


def needs_immich_download(job) -> bool:
    """True for an Immich job whose original is not (or no longer) on disk.

    Covers both a freshly polled job and one whose container temp dir was
    wiped by a restart — in either case the worker fetches it again.
    """
    if not job.immich_asset_id or job.source_label != "Immich":
        return False
    path = job.original_path or ""
    return path.startswith(IMMICH_PENDING) or not os.path.exists(path)


def scan_directory(path: str, min_age: float) -> list[tuple[str, int, int]]:
    """Supported media files that are stable (not being written).

    Returns (path, size, mtime_ns). Skips symlinks (both files and dirs) to
    prevent unbounded recursion on symlink loops and to avoid processing
    files outside the inbox tree.
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
                        files.append((entry.path, stat.st_size, stat.st_mtime_ns))
            elif entry.is_dir(follow_symlinks=False):
                if entry.name.lower() in _SKIP_DIRS:
                    continue
                files.extend(scan_directory(entry.path, min_age))
    except PermissionError:
        pass
    return files


def is_file_stable(filepath: str, expected_size: int) -> bool:
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


# In-memory counter — initialized from DB on first use, then only incremented in memory.
# Eliminates race conditions entirely: no two coroutines can ever get the same counter.
_key_counter: int | None = None
_key_counter_lock = asyncio.Lock()


async def next_debug_key() -> str:
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


async def create_job_safe(*, filename, original_path, source_label, set_id,
                          source_inbox_path=None, folder_tags=False, dry_run=False,
                          use_immich=False, immich_asset_id=None, immich_user_id=None,
                          file_hash=None, immich_checksum=None) -> Job | None:
    """Create a queued Job with collision-free debug_key from the in-memory counter.

    The job's source is recorded in the copy register right away (see
    register_job_source); `immich_checksum` is the asset's checksum from
    Immich's search answer, if known.
    """
    global _key_counter
    debug_key = await next_debug_key()
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
                set_id=set_id,
                file_hash=file_hash,
            )
            session.add(job)
            await session.commit()
    except IntegrityError:
        logger.error(f"debug_key collision for {filename} key={debug_key} — resetting counter")
        # Reset counter so next call re-reads from DB
        async with _key_counter_lock:
            _key_counter = None
        return None
    await register_job_source(job, file_hash=file_hash, immich_checksum=immich_checksum)
    return job


async def register_job_source(job, *, file_hash=None, immich_checksum=None) -> None:
    """Record where a new job's content is, in the copy register (fileops).

    Inbox file: a local copy owned by the job; if the register still lists
    an older job's file with the same content at that path, that entry is
    stale and the new job takes the path over. Immich asset: an unverified
    copy of kind immich — the worker confirms its content when it downloads
    the original. Only registers, nothing is moved; the ledger gets the job's
    intake row ("register", shown as "Erfasst" in the job's history). A
    failure is logged and does not stop the job; fileops registers an
    unknown source on first use.
    """
    import fileops
    from system_logger import log_warning

    path = job.original_path or ""
    try:
        if job.immich_asset_id and path.startswith(IMMICH_PENDING):
            await fileops.register_existing(job, "immich", job.immich_asset_id, checksum=immich_checksum,
                                            intake="input:immich")
        elif path and os.path.isfile(path):
            await fileops.register_existing(job, "local", path, sha256=file_hash, take_over=True,
                                            intake="input:inbox")
    except Exception as e:
        logger.warning("register %s failed: %s", job.debug_key, e)
        await log_warning("filewatcher", f"{job.debug_key} Eintrag im Exemplar-Register fehlgeschlagen",
                          f"{path}: {type(e).__name__}: {e}")

"""Shared file operation helpers — single source of truth.

Every file operation that appears in more than one module belongs here.
Do NOT reimplement these with raw os/shutil calls elsewhere.
"""

import hashlib
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

def sha256(path: str) -> str:
    """Return the hex SHA-256 digest of a file, reading in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Filename conflict resolution
# ---------------------------------------------------------------------------

def resolve_filename_conflict(
    directory: str,
    filename: str,
    separator: str = "_",
) -> str:
    """Return a non-conflicting file path in *directory*.

    If ``directory/filename`` does not exist, returns it as-is.
    Otherwise appends ``{separator}1``, ``{separator}2``, ... before the
    extension until a free name is found.

    Args:
        directory: Target directory (must already exist).
        filename:  Base filename including extension.
        separator: Character(s) inserted between stem and counter.
                   Default ``"_"``; use ``"+"`` for library conflicts.

    Returns:
        Absolute path that does not yet exist on disk.
    """
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    name, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{name}{separator}{counter}{ext}")
        counter += 1
    return path


# ---------------------------------------------------------------------------
# Safe file removal
# ---------------------------------------------------------------------------

def safe_remove(path: str, *, missing_ok: bool = True) -> bool:
    """Remove a file without raising on common errors.

    Args:
        path:       File path to remove.  None and empty string are no-ops.
        missing_ok: If True (default), FileNotFoundError is silently ignored.

    Returns:
        True if the file was actually removed, False otherwise.
    """
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return not missing_ok  # False when missing_ok, would have raised otherwise
    except (OSError, PermissionError) as exc:
        logger.warning("Failed to remove %s: %s", path, exc)
        return False


def safe_remove_with_log(path: str) -> list[str]:
    """Remove a file and its companion ``.log`` sidecar.

    Skips None, empty, and ``immich:`` prefixed paths.

    Returns:
        List of paths that were actually removed (0–2 entries).
    """
    removed = []
    if not path or path.startswith("immich:"):
        return removed
    if safe_remove(path):
        removed.append(path)
    log_path = path + ".log"
    if os.path.exists(log_path):
        if safe_remove(log_path):
            removed.append(log_path)
    return removed


# ---------------------------------------------------------------------------
# Duplicate directory (reads from config)
# ---------------------------------------------------------------------------

async def get_duplicate_dir(job) -> str:
    """Return the duplicate-files directory of the job's set, creating it if needed.

    The base comes from the job's set, the relative part from
    ``library.path_duplicate`` (default ``error/duplicates/``).

    Returns:
        Absolute path to the duplicates directory (guaranteed to exist).
    """
    import asyncio
    from config import config_manager
    from database import async_session
    from sets import base_path_for

    async with async_session() as session:
        base_path = await base_path_for(session, job)
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)
    return dup_dir


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_filepath(job) -> str:
    """Find the actual file path, checking target, original, and temp paths.

    Prefers target_path, then original_path (only if the file exists there),
    then falls back to the IA-04 converted temp file.
    """
    if job.target_path and os.path.exists(job.target_path):
        return job.target_path
    if job.original_path and os.path.exists(job.original_path):
        return job.original_path
    # Fallback: IA-04 converted temp file
    convert_result = (job.step_result or {}).get("IA-04", {})
    temp_path = convert_result.get("temp_path")
    if temp_path and os.path.exists(temp_path):
        return temp_path
    return job.target_path or job.original_path


# ---------------------------------------------------------------------------
# Path sanitisation & validation
# ---------------------------------------------------------------------------

def sanitize_path_component(value: str) -> str:
    """Remove dangerous characters from path components to prevent path traversal."""
    if not value:
        return "unknown"
    # Remove .. / \ and null bytes — prevents directory escape
    value = value.replace("..", "").replace("/", "_").replace("\\", "_")
    value = re.sub(r'[\x00-\x1f]', '', value)
    return value.strip() or "unknown"


def validate_target_path(target_dir: str, base_path: str) -> str:
    """Ensure target directory is within base_path (defense in depth).

    Returns the validated real path or raises ValueError.
    """
    target_real = os.path.realpath(target_dir)
    base_real = os.path.realpath(base_path)
    if not target_real.startswith(base_real + os.sep) and target_real != base_real:
        raise ValueError(
            f"Security: target path escapes library boundary "
            f"(target={target_dir}, base={base_path})"
        )
    return target_real


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> datetime | None:
    """Parse EXIF/video date string into datetime.

    Handles timezone suffixes (Z, +00:00, etc.) and sub-second precision
    by stripping them before matching.
    """
    if not date_str:
        return None
    # Strip timezone suffix (Z, +00:00, +02:00 etc.) for naive datetime
    cleaned = re.sub(r"[+-]\d{2}:\d{2}$", "", date_str)
    cleaned = cleaned.rstrip("Z")
    # Strip sub-second precision (.000000)
    cleaned = re.sub(r"\.\d+$", "", cleaned)
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Folder-tag check
# ---------------------------------------------------------------------------

async def is_folder_tags_active(job) -> bool:
    """Folder tags apply when the job's set has them on — re-read at runtime.

    Only files that came from a directory have a folder structure to read.
    """
    if not job.source_inbox_path:
        return False
    from database import async_session
    from sets import get_set
    async with async_session() as session:
        processing_set = await get_set(session, job.set_id)
    return bool(processing_set and processing_set.folder_tags)

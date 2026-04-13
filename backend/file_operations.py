"""Shared file operation helpers — single source of truth.

Every file operation that appears in more than one module belongs here.
Do NOT reimplement these with raw os/shutil calls elsewhere.
"""

import hashlib
import logging
import os

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

async def get_duplicate_dir() -> str:
    """Return the duplicate-files directory, creating it if needed.

    Reads ``library.base_path`` and ``library.path_duplicate`` from config.

    Returns:
        Absolute path to the duplicates directory (guaranteed to exist).
    """
    import asyncio
    from config import config_manager

    base_path = await config_manager.get("library.base_path", "/library")
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)
    return dup_dir

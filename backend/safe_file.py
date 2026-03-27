"""Safe file operations — copy first, verify, then delete original.

Every move is a three-step process:
  1. Copy source → destination (shutil.copy2 preserves metadata)
  2. Verify: compare file sizes (and optionally hashes)
  3. Delete source only after successful verification

All operations are logged to the system log.
"""

import hashlib
import os
import shutil

from system_logger import log_info, log_error


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_move(src: str, dst: str, context: str = "") -> str:
    """Move a file safely: copy → verify → delete original.

    Args:
        src: Source file path.
        dst: Destination file path (parent dir must exist).
        context: Label for log messages (e.g. debug_key or step name).

    Returns:
        The destination path.

    Raises:
        RuntimeError: If copy verification fails (original is NOT deleted).
    """
    src_size = os.path.getsize(src)

    # Step 1: Copy (preserves metadata)
    shutil.copy2(src, dst)

    # Step 2: Verify size
    dst_size = os.path.getsize(dst)
    if src_size != dst_size:
        # Remove broken copy, keep original safe
        os.remove(dst)
        msg = f"Kopie fehlgeschlagen: Grösse {src_size} ≠ {dst_size}"
        _log_error_sync(context, msg, f"{src} → {dst}")
        raise RuntimeError(msg)

    # Step 3: Verify hash
    src_hash = _sha256(src)
    dst_hash = _sha256(dst)
    if src_hash != dst_hash:
        os.remove(dst)
        msg = f"Kopie fehlgeschlagen: Hash {src_hash[:16]}… ≠ {dst_hash[:16]}…"
        _log_error_sync(context, msg, f"{src} → {dst}")
        raise RuntimeError(msg)

    # Step 4: Delete original
    os.remove(src)

    _log_info_sync(
        context,
        f"Datei verschoben: {os.path.basename(src)} ({src_size} Bytes, SHA256 {src_hash[:16]}…)",
        f"{src} → {dst}",
    )
    return dst


# Synchronous log wrappers (safe_move runs in asyncio.to_thread)
import asyncio


def _log_info_sync(source: str, message: str, detail: str = ""):
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(log_info(source, message, detail))
        )
    except RuntimeError:
        pass


def _log_error_sync(source: str, message: str, detail: str = ""):
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(log_error(source, message, detail))
        )
    except RuntimeError:
        pass

"""Safe file operations — copy first, verify, then delete original.

Every move is a three-step process:
  1. Copy source → destination while computing hash (single read)
  2. Verify: compare destination hash (single read of destination)
  3. Delete source only after successful verification

Optimized: source file is read only ONCE (hash computed during copy).
"""

import hashlib
import os
import shutil

from system_logger import log_sync


from file_operations import sha256 as _sha256  # shared implementation


def safe_move(src: str, dst: str, context: str = "") -> str:
    """Move a file safely: copy+hash → verify → delete original.

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

    # Step 1: Copy while computing source hash in one pass
    src_hash = hashlib.sha256()
    with open(src, "rb") as f_in, open(dst, "wb") as f_out:
        # Preserve metadata after content copy
        while True:
            chunk = f_in.read(65536)
            if not chunk:
                break
            f_out.write(chunk)
            src_hash.update(chunk)
    src_hash_hex = src_hash.hexdigest()

    # Copy metadata (timestamps, permissions)
    shutil.copystat(src, dst)

    # Step 2: Verify size
    dst_size = os.path.getsize(dst)
    if src_size != dst_size:
        os.remove(dst)
        msg = f"Copy failed: Size {src_size} ≠ {dst_size}"
        _log_error_sync(context, msg, f"{src} → {dst}")
        raise RuntimeError(msg)

    # Step 3: Verify hash (only reads destination once — source hash from step 1)
    dst_hash_hex = _sha256(dst)
    if src_hash_hex != dst_hash_hex:
        os.remove(dst)
        msg = f"Copy failed: Hash {src_hash_hex[:16]}… ≠ {dst_hash_hex[:16]}…"
        _log_error_sync(context, msg, f"{src} → {dst}")
        raise RuntimeError(msg)

    # Step 4: Delete original
    os.remove(src)

    _log_info_sync(
        context,
        f"File moved: {os.path.basename(src)} ({src_size} Bytes, SHA256 {src_hash_hex[:16]}…)",
        f"{src} → {dst}",
    )
    return dst


# Synchronous log wrappers. safe_move runs in asyncio.to_thread, where the
# event loop is out of reach; log_sync writes the row from the thread itself.

def _log_info_sync(source: str, message: str, detail: str = ""):
    log_sync("INFO", source or "safe_move", message, detail)


def _log_error_sync(source: str, message: str, detail: str = ""):
    log_sync("ERROR", source or "safe_move", message, detail)

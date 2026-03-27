import asyncio
import os
from datetime import datetime

import imagehash
from PIL import Image
from sqlalchemy import select

from config import config_manager
from models import Job
from safe_file import safe_move
from system_logger import log_info, log_warning


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif", ".bmp", ".dng", ".cr2", ".nef", ".arw"}


def _compute_phash(filepath: str) -> str | None:
    """Compute perceptual hash for an image file."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return None
    try:
        img = Image.open(filepath)
        return str(imagehash.phash(img))
    except Exception:
        return None


def _file_exists(job_entry) -> bool:
    """Check if the file referenced by a job still exists on disk."""
    path = job_entry.target_path or job_entry.original_path
    return path and os.path.exists(path)


async def execute(job, session) -> dict:
    """IA-03: Duplikat-Erkennung — SHA256 (exakt) + pHash (ähnlich)."""
    if not await config_manager.is_module_enabled("duplikat_erkennung"):
        return {"status": "skipped", "reason": "module disabled"}

    # Use temp file from IA-02 if available (for pHash of converted formats)
    convert_result = (job.step_result or {}).get("IA-02", {})
    image_path = convert_result.get("temp_path") or job.original_path

    # --- Stage 1: SHA256 exact match ---
    file_hash = job.file_hash
    if file_hash:
        result = await session.execute(
            select(Job).where(
                Job.file_hash == file_hash,
                Job.id != job.id,
                Job.status.in_(("done", "duplicate", "review", "processing")),
            )
        )
        candidates = result.scalars().all()
        for existing in candidates:
            if await asyncio.to_thread(_file_exists, existing):
                await _handle_duplicate(job, session, existing, "exact", 0)
                return {
                    "status": "duplicate",
                    "match_type": "exact",
                    "original_debug_key": existing.debug_key,
                    "original_path": existing.target_path or existing.original_path,
                }
            else:
                await log_warning("IA-03", f"Orphaned job {existing.debug_key}: file missing, skipping duplicate match")

    # --- Stage 2: Perceptual hash (pHash) similarity ---
    phash_str = await asyncio.to_thread(_compute_phash, image_path)
    if phash_str:
        job.phash = phash_str
        await session.commit()

        threshold = int(await config_manager.get("duplikat.phash_threshold", 5))
        current_hash = imagehash.hex_to_hash(phash_str)

        # Query all jobs with a phash set (done or processing)
        result = await session.execute(
            select(Job).where(
                Job.phash.isnot(None),
                Job.id != job.id,
                Job.status.in_(("done", "duplicate", "review", "processing")),
            )
        )
        candidates = result.scalars().all()

        for candidate in candidates:
            try:
                candidate_hash = imagehash.hex_to_hash(candidate.phash)
                distance = int(current_hash - candidate_hash)
            except Exception:
                continue

            if distance <= threshold:
                if await asyncio.to_thread(_file_exists, candidate):
                    await _handle_duplicate(job, session, candidate, "similar", distance)
                    return {
                        "status": "duplicate",
                        "match_type": "similar",
                        "phash_distance": distance,
                        "original_debug_key": candidate.debug_key,
                        "original_path": candidate.target_path or candidate.original_path,
                    }
                else:
                    await log_warning("IA-03", f"Orphaned job {candidate.debug_key}: file missing, skipping duplicate match")

    # --- Stage 3: Immich duplicate check ---
    if job.use_immich:
        try:
            from immich_client import check_duplicate, get_immich_config
            dup_result = await check_duplicate(job.original_path)
            if dup_result:
                immich_url, _ = await get_immich_config()
                asset_id = dup_result.get("assetId", "")
                immich_link = f"{immich_url}/photos/{asset_id}" if asset_id else ""
                await _handle_duplicate_immich(job, session, asset_id, immich_link)
                return {
                    "status": "duplicate",
                    "match_type": "immich",
                    "immich_asset_id": asset_id,
                    "immich_link": immich_link,
                    "original_path": f"immich:{asset_id}",
                }
        except Exception:
            pass

    return {"status": "ok", "phash": phash_str}


async def _handle_duplicate(job, session, original, match_type: str, distance: int):
    """Move duplicate file to duplicates/ directory and write .log file."""
    original_path = original.target_path or original.original_path
    if match_type == "exact":
        desc = f"Exact duplicate of: {original_path} ({original.debug_key})"
    else:
        desc = f"Similar to: {original_path} ({original.debug_key}, pHash distance: {distance})"

    # Dry-run: detect but don't move
    if job.dry_run:
        job.status = "duplicate"
        await log_info("IA-03", f"{job.debug_key} [dry-run] {desc}")
        return

    base_path = await config_manager.get("library.base_path", "/bibliothek")
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)

    filename = os.path.basename(job.original_path)
    dup_path = os.path.join(dup_dir, filename)

    # Handle name conflicts
    if os.path.exists(dup_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dup_path):
            dup_path = os.path.join(dup_dir, f"{name}_{counter}{ext}")
            counter += 1

    await asyncio.to_thread(safe_move, job.original_path, dup_path, job.debug_key)

    # Write .log file
    log_lines = [
        f"Debug-Key: {job.debug_key}",
        f"File: {job.filename}",
        f"Original: {job.original_path}",
        f"Duplicate type: {match_type}",
        f"Reference: {desc}",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    log_path = dup_path + ".log"
    await asyncio.to_thread(_write_log, log_path, "\n".join(log_lines))

    # Update job status
    job.status = "duplicate"
    job.target_path = dup_path

    await log_info("IA-03", f"{job.debug_key} {desc}")


async def _handle_duplicate_immich(job, session, asset_id: str, immich_link: str):
    """Move duplicate file to duplicates/ directory for review against Immich."""
    desc = f"Already exists in Immich (Asset: {asset_id})"

    if job.dry_run:
        job.status = "duplicate"
        await log_info("IA-03", f"{job.debug_key} [dry-run] {desc}")
        return

    base_path = await config_manager.get("library.base_path", "/bibliothek")
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)

    filename = os.path.basename(job.original_path)
    dup_path = os.path.join(dup_dir, filename)

    if os.path.exists(dup_path):
        name, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dup_path):
            dup_path = os.path.join(dup_dir, f"{name}_{counter}{ext}")
            counter += 1

    await asyncio.to_thread(safe_move, job.original_path, dup_path, job.debug_key)

    # Write .log file with Immich reference
    log_lines = [
        f"Debug-Key: {job.debug_key}",
        f"File: {job.filename}",
        f"Original: {job.original_path}",
        f"Duplicate type: immich",
        f"Reference: {desc}",
        f"Immich Link: {immich_link}",
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    log_path = dup_path + ".log"
    await asyncio.to_thread(_write_log, log_path, "\n".join(log_lines))

    job.status = "duplicate"
    job.target_path = dup_path

    await log_info("IA-03", f"{job.debug_key} {desc}")


def _write_log(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

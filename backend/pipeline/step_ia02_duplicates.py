import asyncio
import io
import os
import subprocess
from datetime import datetime

import imagehash
from PIL import Image
from pillow_heif import register_heif_opener
from sqlalchemy import select

register_heif_opener()

import logging

from config import config_manager
from models import Job
from safe_file import safe_move
from system_logger import log_info, log_warning

_orphan_logger = logging.getLogger("mediaassistant.pipeline.ia02.orphan")


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif", ".webp", ".gif", ".bmp", ".dng", ".cr2", ".nef", ".arw"}
RAW_EXTENSIONS = {".dng", ".cr2", ".nef", ".arw"}


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}


# Format quality preference for duplicate resolution.
# Higher = preferred as original. RAW > HEIC > TIFF > JPEG > others.
_FORMAT_SCORE = {
    ".dng": 5, ".cr2": 5, ".nef": 5, ".arw": 5,   # RAW
    ".heic": 4, ".heif": 4,                          # Apple native, less lossy
    ".tiff": 3, ".tif": 3,                           # lossless
    ".jpg": 2, ".jpeg": 2,                           # lossy but standard
    ".png": 1, ".webp": 1, ".gif": 1, ".bmp": 1,    # often screenshots
}


def _quality_score(job) -> tuple:
    """Compute a comparable quality score from IA-01 data.

    Returns a tuple that can be compared with > to determine which file
    is "better". Higher values win. The tuple elements are compared
    left-to-right (most important first):

      1. format_score   — RAW > HEIC > TIFF > JPEG > PNG/WebP
      2. pixel_count    — width × height (resolution)
      3. file_size      — larger = less compressed (at same format)
      4. metadata_count — more EXIF/GPS/date/camera info = preferred
                          (tiebreaker when quality is equal)

    Available at IA-02 time because IA-01 runs first.
    """
    ia01 = (job.step_result or {}).get("IA-01", {})
    w = ia01.get("width") or 0
    h = ia01.get("height") or 0
    pixels = w * h
    size = ia01.get("file_size") or 0
    ext = os.path.splitext(job.filename)[1].lower() if job.filename else ""
    fmt = _FORMAT_SCORE.get(ext, 0)

    # Count available metadata fields — more metadata = richer file,
    # preferred as original when image quality is otherwise equal.
    meta_count = 0
    if ia01.get("has_exif"):
        meta_count += 1
    if ia01.get("gps"):
        meta_count += 1
    if ia01.get("date"):
        meta_count += 1
    if ia01.get("make") or ia01.get("model"):
        meta_count += 1
    if ia01.get("software"):
        meta_count += 1

    return (fmt, pixels, size, meta_count)


def _compute_phash(filepath: str) -> str | None:
    """Compute perceptual hash for an image file.

    HEIC/HEIF is supported via pillow-heif. For RAW formats (DNG, CR2, NEF, ARW)
    the embedded PreviewImage is extracted via ExifTool as fallback.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return None
    try:
        img = Image.open(filepath)
        return str(imagehash.phash(img))
    except Exception:
        # Fallback for RAW formats: extract preview via ExifTool
        if ext in RAW_EXTENSIONS:
            return _phash_from_preview(filepath)
        return None


def _compute_video_phash(frame_paths: list[str]) -> str | None:
    """Compute average perceptual hash from video frame images.

    Each frame gets its own pHash (8x8 bool matrix). The average is computed
    by majority vote across all frames: a bit is set if >50% of frames have it set.
    """
    import numpy as np
    if not frame_paths:
        return None
    hash_arrays = []
    for path in frame_paths:
        try:
            img = Image.open(path)
            h = imagehash.phash(img)
            hash_arrays.append(h.hash.astype(float))
        except Exception:
            continue
    if not hash_arrays:
        return None
    avg = np.mean(hash_arrays, axis=0) > 0.5
    return str(imagehash.ImageHash(avg))


def _phash_from_preview(filepath: str) -> str | None:
    """Extract embedded PreviewImage via ExifTool and compute pHash from it."""
    try:
        result = subprocess.run(
            ["exiftool", "-b", "-PreviewImage", filepath],
            capture_output=True, timeout=15,
        )
        if result.stdout:
            img = Image.open(io.BytesIO(result.stdout))
            return str(imagehash.phash(img))
    except Exception:
        pass
    return None


async def _file_exists(job_entry) -> bool:
    """Check if the file referenced by a job still exists (on disk or in Immich)."""
    path = job_entry.target_path or job_entry.original_path
    if path and path.startswith("immich:"):
        from immich_client import asset_exists
        asset_id = path[7:]
        return await asset_exists(asset_id)
    return path and os.path.exists(path)


async def execute(job, session) -> dict:
    """IA-02: Duplikat-Erkennung — SHA256 (exakt) + pHash (ähnlich)."""
    if not await config_manager.is_module_enabled("duplikat_erkennung"):
        return {"status": "skipped", "reason": "module disabled"}

    image_path = job.original_path

    # --- Stage 1: SHA256 exact match ---
    # Auch gegen fehlerhafte Jobs matchen → landet im Duplikat-Review
    file_hash = job.file_hash
    if file_hash:
        # Use indexed file_hash column — fast lookup even with 150k+ jobs
        result = await session.execute(
            select(Job).where(
                Job.file_hash == file_hash,
                Job.id != job.id,
                # A job in 'duplicate' status is itself a copy — never use
                # it as the "original" for dedup. This prevents circular
                # references on retry: job A was done, poller created
                # duplicate B of A, then retrying A would match B and
                # mark A as duplicate of its own duplicate.
                Job.status.in_(("done", "review", "processing", "error")),  # excludes 'duplicate', 'orphan', 'queued', 'deleted', 'skipped'
            ).limit(10)
        )
        candidates = result.scalars().all()
        for existing in candidates:
            if await _file_exists(existing):
                # Quality-aware: if the current file is better quality
                # than the existing one, swap roles — demote the existing
                # to duplicate and let the current continue as original.
                if _quality_score(job) > _quality_score(existing):
                    await _swap_duplicate(job, session, existing, "exact", 0)
                    return {
                        "status": "ok",
                        "quality_swap": True,
                        "demoted_debug_key": existing.debug_key,
                        "reason": "current file has better quality, swapped roles",
                    }
                await _handle_duplicate(job, session, existing, "exact", 0)
                return {
                    "status": "duplicate",
                    "match_type": "exact",
                    "original_debug_key": existing.debug_key,
                    "original_path": existing.target_path or existing.original_path,
                }
            # File missing — likely because it's currently being retried
            # (path in transition from /library/error to /app/data/reprocess)
            # or the user manually deleted it. Operational noise, not a real
            # issue → DEBUG-level only, not the system_logs WARNING level
            # that surfaces in the UI.
            _orphan_logger.debug(
                "Orphan candidate %s for job %s: file missing at %s",
                existing.debug_key, job.debug_key,
                existing.target_path or existing.original_path,
            )

    # --- Stage 1.5: JPG+RAW Paar-Erkennung ---
    # Wenn raw_jpg_keep_both=True → beide Dateien unabhängig verarbeiten (kein Duplikat)
    # Wenn raw_jpg_keep_both=False → Paar als Duplikat erkennen → landet im Review
    raw_jpg_keep_both = await config_manager.get("duplikat.raw_jpg_pair", True)
    if not raw_jpg_keep_both:
        basename = os.path.splitext(job.filename)[0]
        ext = os.path.splitext(job.filename)[1].lower()
        jpg_exts = {".jpg", ".jpeg"}
        is_raw = ext in RAW_EXTENSIONS
        is_jpg = ext in jpg_exts

        if is_raw or is_jpg:
            # Suche nach Job mit gleichem Basisnamen aber anderer Endung (JPG↔RAW)
            result = await session.execute(
                select(Job).where(
                    Job.id != job.id,
                    Job.status.in_(("done", "review", "processing", "error")),  # excludes 'duplicate', 'orphan', 'queued', 'deleted', 'skipped'
                    Job.filename.like(f"{basename}.%"),
                )
            )
            for candidate in result.scalars().all():
                cand_ext = os.path.splitext(candidate.filename)[1].lower()
                # JPG sucht RAW-Partner und umgekehrt
                if (is_jpg and cand_ext in RAW_EXTENSIONS) or (is_raw and cand_ext in jpg_exts):
                    if await _file_exists(candidate):
                        await _handle_duplicate(job, session, candidate, "raw_jpg_pair", 0)
                        return {
                            "status": "duplicate",
                            "match_type": "raw_jpg_pair",
                            "original_debug_key": candidate.debug_key,
                            "original_path": candidate.target_path or candidate.original_path,
                        }

    # --- Stage 2: Perceptual hash (pHash) similarity ---
    phash_str = await asyncio.to_thread(_compute_phash, image_path)
    if phash_str:
        job.phash = phash_str
        await session.commit()

        threshold = int(await config_manager.get("duplikat.phash_threshold", 3))
        current_hash = imagehash.hex_to_hash(phash_str)

        # Only compare within the same media type: images vs images,
        # videos vs videos. A video frame thumbnail and a photo can
        # have similar pHash values but are fundamentally different files.
        current_ext = os.path.splitext(job.filename)[1].lower() if job.filename else ""
        current_is_video = current_ext in VIDEO_EXTENSIONS

        # Query only necessary columns for pHash comparison (not full Job objects)
        # Process in batches to avoid loading 150k+ rows into memory at once
        BATCH_SIZE = 5000
        offset = 0
        found_duplicate = None

        while not found_duplicate:
            result = await session.execute(
                select(Job.id, Job.phash, Job.debug_key, Job.target_path, Job.original_path, Job.immich_asset_id, Job.filename).where(
                    Job.phash.isnot(None),
                    Job.id != job.id,
                    Job.status.in_(("done", "review", "processing", "error")),  # excludes 'duplicate', 'orphan', 'queued', 'deleted', 'skipped'
                ).offset(offset).limit(BATCH_SIZE)
            )
            rows = result.all()
            if not rows:
                break

            for row in rows:
                try:
                    candidate_hash = imagehash.hex_to_hash(row.phash)
                    distance = int(current_hash - candidate_hash)
                except Exception:
                    continue

                if distance <= threshold:
                    # Skip cross-media-type matches (image vs video)
                    cand_ext = os.path.splitext(row.filename or "")[1].lower()
                    cand_is_video = cand_ext in VIDEO_EXTENSIONS
                    if current_is_video != cand_is_video:
                        continue

                    # Load full Job object only for the match
                    candidate = await session.get(Job, row.id)
                    if candidate and await _file_exists(candidate):
                        # Quality-aware: prefer the better file as original
                        if _quality_score(job) > _quality_score(candidate):
                            await _swap_duplicate(job, session, candidate, "similar", distance)
                            found_duplicate = {
                                "status": "ok",
                                "quality_swap": True,
                                "demoted_debug_key": candidate.debug_key,
                                "phash_distance": distance,
                                "reason": "current file has better quality, swapped roles",
                            }
                            break
                        await _handle_duplicate(job, session, candidate, "similar", distance)
                        found_duplicate = {
                            "status": "duplicate",
                            "match_type": "similar",
                            "phash_distance": distance,
                            "original_debug_key": candidate.debug_key,
                            "original_path": candidate.target_path or candidate.original_path,
                        }
                        break
                    elif candidate:
                        _orphan_logger.debug(
                            "Orphan pHash candidate %s for job %s: file missing at %s",
                            candidate.debug_key, job.debug_key,
                            candidate.target_path or candidate.original_path,
                        )

            offset += BATCH_SIZE

        if found_duplicate:
            return found_duplicate


    return {"status": "ok", "phash": phash_str}


async def execute_video_phash(job, session) -> dict | None:
    """Post-IA-04 video pHash check: compute average pHash from extracted frames
    and check for similar duplicates. Returns duplicate result or None."""
    if not await config_manager.is_module_enabled("duplikat_erkennung"):
        return None

    # Only for videos that don't have a pHash yet
    ext = os.path.splitext(job.filename)[1].lower()
    if ext not in VIDEO_EXTENSIONS or job.phash:
        return None

    # Get frame paths from IA-04 result
    step_results = job.step_result or {}
    ia04 = step_results.get("IA-04", {})
    frame_paths = ia04.get("temp_paths") or []
    if not frame_paths:
        single = ia04.get("temp_path")
        if single:
            frame_paths = [single]
    if not frame_paths:
        return None

    # Compute average pHash from frames
    phash_str = await asyncio.to_thread(_compute_video_phash, frame_paths)
    if not phash_str:
        return None

    job.phash = phash_str
    await session.commit()

    # Check against existing pHashes — only compare videos with videos
    threshold = int(await config_manager.get("duplikat.phash_threshold", 3))
    current_hash = imagehash.hex_to_hash(phash_str)

    BATCH_SIZE = 5000
    offset = 0

    while True:
        result = await session.execute(
            select(Job.id, Job.phash, Job.debug_key, Job.target_path, Job.original_path, Job.immich_asset_id, Job.filename).where(
                Job.phash.isnot(None),
                Job.id != job.id,
                Job.status.in_(("done", "review", "processing", "error")),  # excludes 'duplicate', 'orphan', 'queued', 'deleted', 'skipped'
            ).offset(offset).limit(BATCH_SIZE)
        )
        rows = result.all()
        if not rows:
            break

        for row in rows:
            try:
                candidate_hash = imagehash.hex_to_hash(row.phash)
                distance = int(current_hash - candidate_hash)
            except Exception:
                continue

            if distance <= threshold:
                # Only compare videos with videos (not with images)
                cand_ext = os.path.splitext(row.filename or "")[1].lower()
                if cand_ext not in VIDEO_EXTENSIONS:
                    continue

                candidate = await session.get(Job, row.id)
                if candidate and await _file_exists(candidate):
                    await _handle_duplicate(job, session, candidate, "similar", distance)
                    return {
                        "status": "duplicate",
                        "match_type": "similar",
                        "phash_distance": distance,
                        "original_debug_key": candidate.debug_key,
                        "original_path": candidate.target_path or candidate.original_path,
                    }

        offset += BATCH_SIZE

    return None


async def _swap_duplicate(job, session, existing, match_type: str, distance: int):
    """The current job has better quality than the existing one.

    Demote the existing job to duplicate (move its file to duplicates/),
    and let the current job continue through the pipeline as the new
    original. The existing job's step_result['IA-02'] is updated to
    reference the current job as the new original.
    """
    existing_path = existing.target_path or existing.original_path

    if match_type == "exact":
        desc_existing = f"Demoted: better quality duplicate arrived ({job.debug_key})"
    else:
        desc_existing = (f"Demoted: better quality similar file arrived "
                         f"({job.debug_key}, pHash distance: {distance})")

    cur_score = _quality_score(job)
    ext_score = _quality_score(existing)

    # Dry-run: only log, don't move
    if job.dry_run:
        await log_info(
            "IA-02",
            f"{job.debug_key} quality swap: keeping current "
            f"(score={cur_score}) over {existing.debug_key} "
            f"(score={ext_score})",
        )
        return

    # Move the existing job's file to duplicates/
    base_path = await config_manager.get("library.base_path", "/library")
    dup_rel = await config_manager.get("library.path_duplicate", "error/duplicates/")
    dup_dir = os.path.join(base_path, dup_rel)
    await asyncio.to_thread(os.makedirs, dup_dir, exist_ok=True)

    # Only move if the existing file is a local path (not immich:)
    if existing_path and not existing_path.startswith("immich:") and os.path.exists(existing_path):
        filename = os.path.basename(existing_path)
        dup_path = os.path.join(dup_dir, filename)
        if os.path.exists(dup_path):
            name, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(dup_path):
                dup_path = os.path.join(dup_dir, f"{name}_{counter}{ext}")
                counter += 1
        await asyncio.to_thread(safe_move, existing_path, dup_path, existing.debug_key)
        existing.target_path = dup_path
    elif existing_path and existing_path.startswith("immich:"):
        # Immich asset: keep the reference, just mark as duplicate.
        # The file stays in Immich (user can clean up via Immich UI).
        dup_path = existing_path

    # Write log for the demoted file
    if existing.target_path and not existing.target_path.startswith("immich:"):
        log_lines = [
            f"Debug-Key: {existing.debug_key}",
            f"File: {existing.filename}",
            f"Demoted by: {job.debug_key} (better quality)",
            f"Quality scores: demoted={ext_score} winner={cur_score}",
            f"Match type: {match_type}",
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        log_path = existing.target_path + ".log"
        await asyncio.to_thread(_write_log, log_path, "\n".join(log_lines))

    # Update the existing job's status and step_result
    existing.status = "duplicate"
    existing.error_message = None
    sr = existing.step_result or {}
    sr["IA-02"] = {
        "status": "duplicate",
        "match_type": match_type,
        "original_debug_key": job.debug_key,
        "original_path": job.original_path,
        "quality_swap": True,
        "quality_score_self": list(ext_score),
        "quality_score_winner": list(cur_score),
    }
    if match_type == "similar" and distance > 0:
        sr["IA-02"]["phash_distance"] = distance
    existing.step_result = sr
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(existing, "step_result")
    await session.commit()

    await log_info(
        "IA-02",
        f"{existing.debug_key} {desc_existing}",
        f"Scores: demoted={ext_score} winner={cur_score}",
    )


def _extract_folder_tags(job) -> list[str]:
    """Extract folder-based tags from the job's inbox path.

    Same logic as step_ia07_exif_write.py:74-87, but called at IA-02 time
    so the tags are preserved in step_result even after the file moves to
    /library/error/duplicates/ (which breaks the relative-path calculation).

    Stored in step_result['IA-02']['folder_tags'] and picked up by IA-07
    when the duplicate is later kept via the review UI and re-processed.
    """
    if not job.source_inbox_path or not job.original_path:
        return []
    try:
        rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
    except ValueError:
        return []
    if not rel or rel == ".":
        return []
    folder_parts = [p for p in rel.split(os.sep) if p and p != "."]
    if not folder_parts:
        return []

    tags = []
    for part in folder_parts:
        for word in part.split():
            if word and word not in tags:
                tags.append(word)
    combined = " ".join(folder_parts)
    if combined not in tags:
        tags.append(combined)
    return tags


async def _handle_duplicate(job, session, original, match_type: str, distance: int):
    """Move duplicate file to duplicates/ directory and write .log file."""
    original_path = original.target_path or original.original_path
    if match_type == "exact":
        desc = f"Exact duplicate of: {original_path} ({original.debug_key})"
    else:
        desc = f"Similar to: {original_path} ({original.debug_key}, pHash distance: {distance})"

    # Preserve folder tags before the file moves (path breaks after move)
    folder_tags = _extract_folder_tags(job)

    # Dry-run: detect but don't move
    if job.dry_run:
        job.status = "duplicate"
        # Clear any pre-existing warning message left over from a prior
        # run (e.g. retry where the previous run had a soft warning) —
        # a duplicate aborts the pipeline before the steps that produce
        # warnings, so the message is no longer truthful.
        job.error_message = None
        await log_info("IA-02", f"{job.debug_key} [dry-run] {desc}")
        return

    base_path = await config_manager.get("library.base_path", "/library")
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
    # Clear any pre-existing warning message — a duplicate aborts the
    # pipeline before the steps that produce warnings, so the message
    # would otherwise outlive its referent.
    job.error_message = None

    # Store folder tags in step_result so they survive the file move
    # and can be picked up by IA-07 when this job is later kept via
    # the duplicate review UI and re-processed.
    if folder_tags:
        sr = job.step_result or {}
        ia02 = sr.get("IA-02", {})
        if not isinstance(ia02, dict):
            ia02 = {}
        ia02["folder_tags"] = folder_tags
        sr["IA-02"] = ia02
        job.step_result = sr
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(job, "step_result")

    await log_info("IA-02", f"{job.debug_key} {desc}"
                   + (f" [folder_tags: {', '.join(folder_tags)}]" if folder_tags else ""))

    # Clean up empty parent directories in inbox (best-effort, file is already moved)
    try:
        if job.source_inbox_path:
            from pipeline.step_ia08_sort import _cleanup_empty_dirs
            source_dir = os.path.dirname(job.original_path)
            await asyncio.to_thread(_cleanup_empty_dirs, source_dir, job.source_inbox_path)
    except Exception as e:
        await log_warning("IA-02", f"{job.debug_key} Cleanup nach Duplikat-Move fehlgeschlagen: {e}")



def _write_log(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

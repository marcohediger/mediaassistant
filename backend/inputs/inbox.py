"""Inbox directories of a set: find new files and enqueue them."""
import asyncio
import logging
import os

from sqlalchemy import select

import services
import storage
from config import config_manager
from database import async_session
from file_operations import sha256 as _sha256
from models import InboxDirectory, Job
from system_logger import log_info, log_warning
from inputs.common import MAX_FILE_SIZE, create_job_safe, is_file_stable, scan_directory

logger = logging.getLogger("mediaassistant.inputs")

# path -> (size, mtime_ns, sha256). A file that sits in an inbox (skipped by a
# rule, or in a dry-run set) used to be re-hashed on every scan — every few
# seconds, large videos included. Unchanged size and mtime mean unchanged
# content, so the hash is reused.
_hash_cache: dict[str, tuple[int, int, str]] = {}

_QUERY_CHUNK = 500


async def _known_state(paths: list[str]) -> tuple[set[str], set[tuple[str, str]]]:
    """Which of these paths are in flight, and which (path, hash) pairs are done.

    Asks only about the files actually lying in the inbox now. The old scan
    loaded every job that ever came from the directory — tens of thousands of
    rows per minute on a long-running instance, to answer a question about a
    handful of files.
    """
    active: set[str] = set()
    done: set[tuple[str, str]] = set()
    async with async_session() as session:
        for start in range(0, len(paths), _QUERY_CHUNK):
            chunk = paths[start:start + _QUERY_CHUNK]
            rows = (await session.execute(
                select(Job.original_path, Job.status, Job.file_hash, Job.error_message,
                       Job.target_path, Job.dry_run)
                .where(Job.original_path.in_(chunk))
            )).all()
            for path, status, fhash, err, target, dry_run in rows:
                if status in ("queued", "processing"):
                    active.add(path)
                if not fhash:
                    continue
                if not dry_run and status != "skipped" and os.path.exists(path):
                    continue
                if dry_run and status in ("done", "duplicate"):
                    done.add((path, fhash))
                elif status in ("skipped", "duplicate"):
                    done.add((path, fhash))
                elif status == "done" and not err:
                    if target and target.startswith("immich:"):
                        done.add((path, fhash))
                    elif target and os.path.exists(target):
                        done.add((path, fhash))
                    elif not target:
                        done.add((path, fhash))
    return active, done


def _hash_and_check(filepath: str, file_size: int, mtime_ns: int) -> tuple[str, str | None]:
    cached = _hash_cache.get(filepath)
    if cached and cached[0] == file_size and cached[1] == mtime_ns:
        return filepath, cached[2]
    if not is_file_stable(filepath, file_size):
        return filepath, None
    digest = _sha256(filepath)
    _hash_cache[filepath] = (file_size, mtime_ns, digest)
    return filepath, digest


async def scan_set(processing_set) -> int:
    """Scan the active directories of one set and enqueue new files.

    Returns the number of jobs created.
    """
    ps = processing_set
    async with async_session() as session:
        inboxes = (await session.execute(
            select(InboxDirectory).where(InboxDirectory.set_id == ps.id,
                                         InboxDirectory.active == True)  # noqa: E712
        )).scalars().all()
    if not inboxes:
        return 0

    min_age = max(float(ps.interval_s or 5), 2.0)
    queued_total = 0

    for inbox in inboxes:
        # An inbox drive that is not mounted looks like an empty inbox.
        found = await storage.unavailable([inbox.path])
        if found is not None:
            if await config_manager.get("pipeline.auto_paused_reason", "") != services.STORAGE:
                await storage.pause(found, where=f"Eingang {inbox.label} (Set {ps.name})",
                                    error="Eingang nicht verfügbar — nicht eingelesen")
            continue
        if not os.path.isdir(inbox.path):
            continue

        found = await asyncio.to_thread(scan_directory, inbox.path, min_age)

        # Forget cache entries of files that left this inbox
        prefix = inbox.path.rstrip(os.sep) + os.sep
        present = {f[0] for f in found}
        for stale in [p for p in _hash_cache if p.startswith(prefix) and p not in present]:
            del _hash_cache[stale]

        if not found:
            continue
        active, done = await _known_state([f[0] for f in found])

        candidates = []
        for filepath, file_size, mtime_ns in found:
            if filepath in active:
                continue
            if file_size > MAX_FILE_SIZE:
                size_gb = file_size / (1024 ** 3)
                await log_warning("filewatcher", f"Skipped (too large): {os.path.basename(filepath)}",
                                  f"Size: {size_gb:.1f} GB, limit: {MAX_FILE_SIZE / (1024 ** 3):.0f} GB")
                continue
            candidates.append((filepath, file_size, mtime_ns))
        if not candidates:
            continue

        # Hash in parallel (4 threads), create jobs as each completes
        sem = asyncio.Semaphore(4)

        async def _hash_one(fp, fs, mt):
            async with sem:
                return await asyncio.to_thread(_hash_and_check, fp, fs, mt)

        queued = 0
        for coro in asyncio.as_completed([_hash_one(*c) for c in candidates]):
            try:
                filepath, file_hash = await coro
                if file_hash is None or (filepath, file_hash) in done:
                    continue
                filename = os.path.basename(filepath)
                job = await create_job_safe(
                    filename=filename,
                    original_path=filepath,
                    source_label=inbox.label,
                    source_inbox_path=inbox.path,
                    set_id=ps.id,
                    folder_tags=(ps.folder_tag_mode or "none") != "none" or bool(ps.folder_albums),
                    dry_run=bool(ps.dry_run),
                    use_immich=ps.target_mode == "immich",
                    immich_user_id=ps.immich_user_id,
                    file_hash=file_hash,
                )
                if job:
                    queued += 1
                    logger.info(f"New file: {filename} → {job.debug_key} (Set: {ps.name}, Inbox: {inbox.label})")
            except Exception as e:
                logger.error(f"Error queuing file: {e}")

        if queued:
            queued_total += queued
            try:
                await log_info("filewatcher", f"{queued} new files queued from {inbox.label}",
                               f"Set: {ps.name}")
            except Exception:
                logger.info(f"{queued} new files queued from {inbox.label} (log_info failed, DB busy)")

    return queued_total

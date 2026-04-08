"""Shared building block for re-queueing a job through the pipeline.

Several flows need to put a previously processed job back into the pipeline
worker's queue:

- Retry of error / warning jobs (`pipeline.reset_job_for_retry`)
- Manual review in the duplicates UI (`routers/duplicates.py`)
- Wartungs-Tools wie tag_cleanup (issue #42)

All of these share the same three-step dance:

1. Move the job's current file (`target_path` if present, else `original_path`)
   into the internal `/app/data/reprocess/` directory so the worker doesn't
   pick it up from the library while it's mid-edit. The companion `.xmp`
   sidecar is moved with the file so EXIF/sidecar metadata stays attached
   wherever IA-07 originally wrote it. The `.log` file is removed.
2. Reset `step_result` according to one of three policies (keep specific
   steps, drop steps with given statuses, inject synthetic results).
3. Flip the job to `status='queued'`, clear `target_path` and
   `error_message`, and commit. The pipeline worker / `run_pipeline()`
   takes over from here.

IA-07 (`step_ia07_exif_write`) honours the global `metadata.write_mode`
setting at runtime, so as long as that setting hasn't changed between the
original processing and the reprocess, the data will land in EXIF or sidecar
exactly as it did the first time around. This helper deliberately preserves
the `.xmp` sidecar (instead of orphaning it) so IA-07 in sidecar mode picks
up cleanly at the new location.
"""

import asyncio
import os

from sqlalchemy.orm.attributes import flag_modified

from safe_file import safe_move


REPROCESS_DIR = "/app/data/reprocess"


def _resolve_reprocess_path(filename: str, debug_key: str) -> str:
    """Build a non-colliding path inside REPROCESS_DIR for `filename`.

    If a file with the same name already exists (from a prior reprocess
    of a different job), suffix it with the debug_key to keep them apart.
    """
    candidate = os.path.join(REPROCESS_DIR, filename)
    if os.path.exists(candidate):
        name, ext = os.path.splitext(filename)
        candidate = os.path.join(REPROCESS_DIR, f"{name}_{debug_key}{ext}")
    return candidate


def _is_immich_target(target_path: str | None) -> bool:
    """True if `target_path` is an `immich:<asset_id>` reference (not a disk path)."""
    return bool(target_path and target_path.startswith("immich:"))


async def _move_file_for_reprocess(job) -> bool:
    """Move the job's current file (+ sidecar, - log) into REPROCESS_DIR.

    Source resolution order:
      1. `target_path` (if it's a real disk path that exists)
      2. `original_path` (if it exists on disk)
      3. `target_path` if it's an `immich:<asset_id>` reference — in
         that case the asset is downloaded from Immich into the
         reprocess dir. This is the path used for jobs whose only
         remaining copy lives in Immich (the inbox file was already
         deleted by IA-08 after the successful upload).

    Updates `job.original_path` to the new local location. Clears
    `job.target_path` **only when it pointed to a now-stale local file
    path** — an `immich:<asset_id>` reference is preserved so the job
    stays linked to its Immich data.

    Returns True if the file is now available in REPROCESS_DIR (either
    via local move or Immich download), False if no source could be
    located anywhere.
    """
    src = None
    if job.target_path and not _is_immich_target(job.target_path) and os.path.exists(job.target_path):
        src = job.target_path
    elif job.original_path and os.path.exists(job.original_path):
        src = job.original_path

    if not src:
        # Last-resort: if target_path is an immich:<asset_id> reference,
        # download the original from Immich into REPROCESS_DIR. This
        # makes retry work for jobs whose only surviving copy lives in
        # Immich (typical state for any job whose inbox file IA-08
        # already removed after upload).
        if _is_immich_target(job.target_path):
            asset_id = job.target_path[len("immich:"):]
            try:
                # Lazy import to avoid an immich_client → reprocess
                # circular at module-load time and to keep this module
                # importable in tests that don't need Immich at all.
                from immich_client import download_asset, get_user_api_key
                user_api_key = None
                if job.immich_user_id:
                    user_api_key = await get_user_api_key(job.immich_user_id)
                await asyncio.to_thread(os.makedirs, REPROCESS_DIR, exist_ok=True)
                downloaded = await download_asset(
                    asset_id, REPROCESS_DIR, api_key=user_api_key,
                )
            except Exception:
                # Asset gone from Immich too — no source anywhere.
                return False

            # Move the just-downloaded file to a debug-key-suffixed name
            # so concurrent reprocesses of different jobs that happen to
            # have the same Immich filename don't collide.
            dst = _resolve_reprocess_path(os.path.basename(downloaded), job.debug_key)
            if downloaded != dst:
                await asyncio.to_thread(os.rename, downloaded, dst)
            job.original_path = dst
            # Keep target_path = "immich:<id>" so IA-08's webhook branch
            # (cached in step_result) still recognises the asset.
            return True

        # Nothing to move — caller decides whether to fail or continue.
        # Clear a stale local target_path; preserve immich: references.
        if not _is_immich_target(job.target_path):
            job.target_path = None
        return False

    await asyncio.to_thread(os.makedirs, REPROCESS_DIR, exist_ok=True)
    dst = _resolve_reprocess_path(os.path.basename(src), job.debug_key)
    await asyncio.to_thread(safe_move, src, dst, job.debug_key)

    # Companion .xmp sidecar — move it alongside so IA-07 in sidecar mode
    # finds the metadata at the new location instead of orphaning it.
    sidecar_src = src + ".xmp"
    if os.path.exists(sidecar_src):
        sidecar_dst = dst + ".xmp"
        # Avoid clobbering a leftover sidecar at the destination from
        # an interrupted prior reprocess of the same job.
        if os.path.exists(sidecar_dst):
            try:
                os.remove(sidecar_dst)
            except OSError:
                pass
        await asyncio.to_thread(safe_move, sidecar_src, sidecar_dst, job.debug_key)

        # The cached IA-07 step result still points at the old sidecar
        # location. Update it so any step that re-runs after the move
        # (e.g. IA-08 picking up the sidecar to upload alongside the
        # asset) sees the new path instead of looking at a stale one.
        sr = job.step_result or {}
        ia07 = sr.get("IA-07")
        if isinstance(ia07, dict) and ia07.get("sidecar_path"):
            ia07["sidecar_path"] = sidecar_dst
            sr["IA-07"] = ia07
            job.step_result = sr
            flag_modified(job, "step_result")

    # Defensive: drop any leftover `.xmp.{debug_key}.tmp` from an interrupted
    # ExifTool run at the new location.
    stale_tmp = f"{dst}.xmp.{job.debug_key}.tmp"
    if os.path.exists(stale_tmp):
        try:
            os.remove(stale_tmp)
        except OSError:
            pass

    # Remove .log file at the source location (it described the previous
    # run; the next run will produce a fresh one if needed).
    log_src = src + ".log"
    if os.path.exists(log_src):
        try:
            os.remove(log_src)
        except OSError:
            pass

    job.original_path = dst
    # Only clear target_path if it pointed to a now-stale local file
    # location. An `immich:<asset_id>` reference still points to a valid
    # asset that survives the retry — keep it so the job remains linked
    # to its Immich data even when IA-08's cached step result skips
    # re-running on retry.
    if not _is_immich_target(job.target_path):
        job.target_path = None
    return True


def _reset_step_results(
    job,
    *,
    keep_steps: set[str] | None,
    drop_step_statuses: set[str] | None,
    inject_steps: dict[str, dict] | None,
) -> None:
    """Apply the chosen step_result reset policy in-place on `job`.

    Policies (combinable, applied in order):
    - keep_steps: if set, keep ONLY these step codes (drop everything else)
    - drop_step_statuses: drop step codes whose result has one of these
      statuses (e.g. {"error", "warning"} for retry of soft failures)
    - inject_steps: merge these synthetic step results in last (e.g.
      {"IA-02": {"status": "skipped", "reason": "..."}})
    """
    current = dict(job.step_result or {})

    if keep_steps is not None:
        current = {k: v for k, v in current.items() if k in keep_steps}

    if drop_step_statuses:
        for step_code in list(current.keys()):
            r = current[step_code]
            if isinstance(r, dict) and r.get("status") in drop_step_statuses:
                del current[step_code]

    if inject_steps:
        current.update(inject_steps)

    job.step_result = current
    flag_modified(job, "step_result")


async def prepare_job_for_reprocess(
    session,
    job,
    *,
    keep_steps: set[str] | None = None,
    drop_step_statuses: set[str] | None = None,
    inject_steps: dict[str, dict] | None = None,
    move_file: bool = True,
    commit: bool = True,
) -> bool:
    """Reset `job` for a fresh pipeline run and (optionally) commit.

    The caller is responsible for any locking / status claim that must
    happen *before* this helper runs (e.g. the atomic `error → processing`
    transition in `reset_job_for_retry()`).

    Args:
        session: active SQLAlchemy AsyncSession the job is bound to.
        job: ORM Job instance to reset.
        keep_steps: if given, only these step codes survive the reset.
        drop_step_statuses: drop step results with these statuses.
        inject_steps: synthetic step results merged in last.
        move_file: when True (default), move the file + sidecar to the
            internal reprocess dir. Set False for in-place reprocessing
            (e.g. tag_cleanup, where the file stays at target_path and
            only its EXIF was wiped beforehand).
        commit: when True (default), commit the session at the end.

    Returns:
        True if a file was moved (or move_file was False),
        False if move_file was True but no source file could be located.
    """
    moved_or_skipped = True
    if move_file:
        moved_or_skipped = await _move_file_for_reprocess(job)

    _reset_step_results(
        job,
        keep_steps=keep_steps,
        drop_step_statuses=drop_step_statuses,
        inject_steps=inject_steps,
    )

    job.status = "queued"
    job.error_message = None
    if not move_file:
        # In-place reprocess: target_path stays so IA-08 knows where the
        # file already lives. Caller is responsible for the consistency
        # of that path.
        pass
    else:
        # Only clear target_path if it was a now-stale local file path.
        # An `immich:<asset_id>` reference still points to a valid asset
        # — preserve it so the job stays linked to its Immich data after
        # IA-08's cached step result skips re-execution on retry.
        if not _is_immich_target(job.target_path):
            job.target_path = None

    if commit:
        await session.commit()

    return moved_or_skipped

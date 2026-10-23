import asyncio
import logging
import os

from system_logger import log_info, log_warning

logger = logging.getLogger("mediaassistant.pipeline.ia10")

# The Immich poller (worker.fetch_immich_source) downloads into
# tempfile.mkdtemp(prefix="ma_immich_").
POLLER_TMP_PREFIX = "/tmp/ma_immich_"


async def execute(job, session) -> dict:
    """IA-10: Aufräumen — temporäre Dateien entfernen.

    Finalizer: runs after every run, also after a critical failure. Every
    part is guarded on its own, so one failure never skips the others, and
    every removal goes through fileops (ledger, register, trash). Content
    that may still be needed (a sidecar next to its file, a Takeout JSON in
    a dry run) is left where it is.
    """
    step_results = job.step_result or {}
    result = {"removed": [], "trashed": [], "kept": [], "failed": []}

    for part in (_cleanup_temp_files, _cleanup_poller_copy, _cleanup_sidecar, _cleanup_takeout_json):
        try:
            await part(job, step_results, result)
        except Exception as exc:
            logger.exception("%s IA-10 %s failed", job.debug_key, part.__name__)
            result["failed"].append(f"{part.__name__}: {type(exc).__name__}: {exc}")

    try:
        await _remove_empty_poller_dirs(job, result)
    except Exception as exc:
        result["failed"].append(f"poller dir: {type(exc).__name__}: {exc}")

    result["count"] = len(result["removed"]) + len(result["trashed"])
    for key in ("trashed", "kept", "failed"):
        if not result[key]:
            del result[key]
    return result


async def _cleanup_temp_files(job, step_results, result):
    """S16: the IA-04 temp JPEG(s) — only this job's own (register or name).

    A kept duplicate carries the IA-04 result of its analysis donor; those
    paths belong to the donor and are never touched here.
    """
    from pipeline.step_ia04_convert import discard_temp_files
    convert_result = step_results.get("IA-04") or {}
    temp_paths = list(convert_result.get("temp_paths") or [])
    if not temp_paths and convert_result.get("temp_path"):
        temp_paths = [convert_result["temp_path"]]
    if not temp_paths:
        return
    removed, kept, failed = await discard_temp_files(
        job, temp_paths, actor="IA-10", reason="temporäre Konvertierung nach der Verarbeitung")
    result["removed"].extend(removed)
    result["kept"].extend(f"{p} (gehört einem anderen Job)" for p in kept)
    result["failed"].extend(failed)


def _is_poller_job(job) -> bool:
    return bool(job.immich_asset_id) and job.source_label == "Immich"


async def _cleanup_poller_copy(job, step_results, result):
    """S19: the original the Immich poller downloaded into its temp dir.

    Three-part guard (v2.28.28, live incident MA-2026-28123): the job comes
    from the poller, and the path lies in a poller temp dir. Never any file
    just because the job has an immich_asset_id — an inbox job carries one
    too after its first upload. fileops.discard additionally requires that
    the register lists the copy as this job's work copy and that another
    copy of the same content (the Immich asset) checks out right now.
    """
    import fileops
    path = job.original_path or ""
    if not (_is_poller_job(job) and path.startswith(POLLER_TMP_PREFIX)):
        return
    if not await asyncio.to_thread(os.path.isfile, path):
        return
    own = {c.location: c for c in await fileops.copies(job)}
    row = own.get(os.path.abspath(path))
    try:
        if row is None:
            # Downloaded before the register existed, or its entry failed.
            row = await fileops.register_existing(job, "work", path)
        await fileops.discard(job, row.id, actor="IA-10",
                              reason="Download des Immich-Pollers nach der Verarbeitung")
        result["removed"].append(path)
    except fileops.FileOpRefused as exc:
        # E.g. Immich unreachable, so the asset cannot be checked: the file stays.
        result["kept"].append(f"{path} ({exc})")


async def _cleanup_sidecar(job, step_results, result):
    """S17: the XMP sidecar IA-07 wrote — only once nothing needs it locally.

    It stays wherever it lies next to its file (library, error/, reprocess/,
    an inbox), and always after a critical failure or in a dry run: then the
    sidecar is the only place the generated metadata is (R-17b). It goes to
    the trash (never deleted outright) only when the job's content reached
    Immich — a sidecar IA-07 created (MediaAssistant's data only, also in
    Immich via upload or API), a merged user sidecar only once its file is
    gone too (uploaded with it) — or when IA-07 created it and the job was
    skipped. Library jobs need nothing here: IA-08 moved the sidecar
    together with the file.
    """
    import fileops
    ia07 = step_results.get("IA-07") or {}
    sidecar_path = ia07.get("sidecar_path")
    if not sidecar_path:
        return
    sidecar_path = os.path.abspath(sidecar_path)
    own = {c.location: c for c in await fileops.copies(job) if c.kind == "sidecar"}
    row = own.get(sidecar_path)
    if row is None or not await asyncio.to_thread(os.path.isfile, sidecar_path):
        return  # moved on (IA-08, _move_to_error) or not this job's
    if job.dry_run or job.status == "error":
        result["kept"].append(f"{sidecar_path} (Job {job.status}, Sidecar bleibt)")
        return
    image = ia07.get("sidecar_for") or os.path.splitext(sidecar_path)[0]
    beside = await asyncio.to_thread(os.path.exists, image)
    created = ia07.get("sidecar_origin", "created") == "created"
    in_immich = (job.target_path or "").startswith("immich:")
    if in_immich and (created or not beside):
        reason = "Sidecar nach Übergabe an Immich"
    elif job.status == "skipped" and created:
        reason = "von MediaAssistant erzeugte Sidecar, Datei übersprungen"
    else:
        result["kept"].append(f"{sidecar_path} (liegt bei ihrer Datei)")
        return
    try:
        res = await fileops.trash(job, row.id, actor="IA-10", reason=reason, allow_last_copy=True)
        result["trashed"].append(res.location or sidecar_path)
    except fileops.FileOpRefused as exc:
        result["kept"].append(f"{sidecar_path} ({exc})")


async def _cleanup_takeout_json(job, step_results, result):
    """S18 / R-16: the Google Takeout JSON goes to the trash, never deleted.

    IA-07 has written its GPS and date into the file or sidecar where they
    were missing. Left alone in a dry run (R-07) and while the media file
    still lies beside it (e.g. skipped by a rule), because then it is still
    the file's companion.
    """
    import fileops
    ia01 = step_results.get("IA-01") or {}
    json_path = ia01.get("google_json_path")
    if not json_path or job.dry_run:
        return
    json_path = os.path.abspath(json_path)
    if not await asyncio.to_thread(os.path.isfile, json_path):
        return
    media = job.original_path or ""
    if (os.path.dirname(os.path.abspath(media)) == os.path.dirname(json_path)
            and await asyncio.to_thread(os.path.exists, media)):
        result["kept"].append(f"{json_path} (Datei liegt noch daneben)")
        return
    written = (step_results.get("IA-07") or {}).get("takeout_written")
    reason = ("Google-Takeout-JSON, Angaben in die Datei übernommen" if written is not None
              else "Google-Takeout-JSON nach der Verarbeitung")
    try:
        row = await fileops.register_existing(job, "takeout_json", json_path, take_over=True)
        res = await fileops.trash(job, row.id, actor="IA-10", reason=reason, allow_last_copy=True)
    except fileops.FileOpRefused as exc:
        result["kept"].append(f"{json_path} ({exc})")
        return
    result["trashed"].append(res.location or json_path)
    if written is None:
        await log_info("IA-10", f"{job.debug_key} Takeout-JSON im Papierkorb, Angaben nicht in die Datei geschrieben",
                       f"{json_path} → {res.location} — IA-07 lief nicht oder schrieb keine Takeout-Angaben "
                       f"(Status {job.status}); aus dem Papierkorb wiederherstellbar")


async def _remove_empty_poller_dirs(job, result):
    """Remove the poller's temp dir once it is empty (only ever an empty dir)."""
    if not _is_poller_job(job):
        return
    import fileops
    dirs = set()
    if (job.original_path or "").startswith(POLLER_TMP_PREFIX):
        dirs.add(os.path.dirname(job.original_path))
    # The download may have moved on (IA-02 duplicate, error/): the register
    # still knows where it came from.
    for copy in await fileops.copies(job, states=("present", "moved", "deleted")):
        if copy.location.startswith(POLLER_TMP_PREFIX):
            dirs.add(os.path.dirname(copy.location))
    for d in sorted(dirs):
        if not d.startswith(POLLER_TMP_PREFIX) or not await asyncio.to_thread(os.path.isdir, d):
            continue
        try:
            if not await asyncio.to_thread(os.listdir, d):
                await asyncio.to_thread(os.rmdir, d)
                result["removed"].append(d)
        except OSError as e:
            await log_warning("IA-10", f"{job.debug_key} Immich-Temp-Ordner nicht entfernt", f"{d} — {e}")

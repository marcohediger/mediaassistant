"""Shared building block for re-queueing a job through the pipeline.

Several flows put a previously processed job back into the worker's queue:

- Retry of error / warning jobs (`pipeline.reset_job_for_retry`)
- Manual review in the duplicates UI (`routers/duplicates.py`)
- Maintenance tools (stuck duplicate winners, tag cleanup)

All of them share the same steps:

1. Find the job's own content (`find_source`). The source comes from the
   copy register (fileops): a local copy this job owns whose hash checks
   out right now. Without one, the old job fields are tried, but a file
   there is taken only if its hash is one this job's content ever had —
   a different photo that landed at a stale `original_path` (R-01) or a
   file another job owns is never touched. Last comes the job's own (or
   shared) Immich asset.
2. Bring it into the internal `REPROCESS_DIR` (`materialize`): a local
   copy is moved there with fileops (never overwriting anything; its
   `.xmp` sidecar travels along, the `.log` of the previous run is
   dropped), an Immich asset is downloaded into a work copy of its own
   (R-03).
3. Reset the module results (keep / drop / inject policies; the run
   parameters of the previous run are always dropped): the runs of the
   dropped modules are superseded, injected results become runs of their
   own (runs.py), and the job goes to `status='queued'`. The pipeline
   worker takes over from there.

Run parameters are control values for exactly one pipeline run (e.g.
`_force_reupload` from the duplicate review, `_retry_previous_immich_tags`
from a retry). They are job values (jobs.job_data, shown in `step_result`)
under keys starting with an underscore, read by steps through
`pipeline.run_params(job)`, survive a crash of that run (the resumed run
gets them again) and are removed by run_pipeline when the run ends. Every
reset here drops the old ones.
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select

import fileops
import runs
from database import async_session
from file_operations import sha256 as _sha256
from models import AssetRef, FileCopy, FileOp, Job
from pipeline.registry import MAIN
from system_logger import log_info, log_warning


REPROCESS_DIR = "/app/data/reprocess"
ACTOR = "system:reprocess"

# Marker of a retry whose old results are already dropped but whose source is
# not prepared yet (see pipeline.reset_job_for_retry, R-10b). run_pipeline
# finishes the preparation when it finds it.
PREPARE_SOURCE = "_prepare_source"

# Register kinds that hold the job's content itself (not a sidecar, a temp
# JPEG or a Takeout JSON).
CONTENT_KINDS = ("local", "work", "immich")
_LOCAL_CONTENT_KINDS = ("local", "work")

# run_pipeline hands the parameters of the run in progress to the steps
# through this attribute of the Job object (not persisted).
RUN_PARAMS_ATTR = "_ma_run_params"


def is_run_param(key) -> bool:
    return isinstance(key, str) and key.startswith("_")


def run_params(job) -> dict:
    """Parameters of the pipeline run in progress for `job`, e.g.
    {"_force_reupload": True} after a duplicate keep or
    {"_retry_previous_immich_tags": [...]} after a retry.

    Set by run_pipeline for exactly one run; empty outside a run, so a step
    called on its own never acts on a leftover value (R-12). Steps read
    them here instead of from step_result and need not remove them —
    run_pipeline does that when the run ends.
    """
    return dict(getattr(job, RUN_PARAMS_ATTR, None) or {})


@dataclass
class Source:
    """Where a job's content can be taken from for a new run."""
    kind: str                 # "local" (a file) or "immich" (an asset id)
    location: str
    sha256: str | None = None
    copy_id: int | None = None  # file_copies row, None for a copy only the old job fields know
    checked: bool = True      # False: a file from the old fields whose content could not be compared


def _is_immich_target(target_path: str | None) -> bool:
    """True if `target_path` is an `immich:<asset_id>` reference (not a disk path)."""
    return bool(target_path and target_path.startswith("immich:"))


def _local_path(path: str | None) -> str | None:
    if not path or path.startswith(("immich:", "immich-pending:")):
        return None
    return os.path.abspath(os.path.normpath(path))


def _inside(path: str, directory: str) -> bool:
    path = os.path.abspath(os.path.normpath(path))
    directory = os.path.abspath(os.path.normpath(directory))
    return path.startswith(directory.rstrip(os.sep) + os.sep)


def _is_file(path: str) -> bool:
    return os.path.isfile(path) and not os.path.islink(path)


def _free_name(directory: str, filename: str, debug_key: str) -> str:
    """A name that is free in `directory` together with its .xmp companion.

    A taken name gets the debug key, as before. Only a wish: fileops
    reserves the name atomically and adds a counter if it got taken since.
    """
    stem, ext = os.path.splitext(filename)
    names = [filename, f"{stem}_{debug_key}{ext}"]
    names += [f"{stem}_{debug_key}_{n}{ext}" for n in range(1, 100)]
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.lexists(path) and not os.path.lexists(path + ".xmp"):
            return name
    return filename


def _is_own_log(path: str, debug_key: str) -> bool:
    """The .log next to an error file is MediaAssistant's own if it starts
    with this job's debug key (pipeline._move_to_error writes it so)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readline().strip() == f"Debug-Key: {debug_key}"
    except OSError:
        return False


def _field_hashes(job) -> set[str]:
    """Content hashes the job's own fields know (scan hash, hash after a
    direct metadata write, hashes a cut-short retry noted)."""
    sr = job.step_result or {}
    ia07 = sr.get("IA-07") if isinstance(sr.get("IA-07"), dict) else {}
    marker = sr.get(PREPARE_SOURCE) if isinstance(sr.get(PREPARE_SOURCE), dict) else {}
    hashes = {job.file_hash, ia07.get("file_hash"), *(marker.get("sha256") or [])}
    return {h for h in hashes if h}


async def own_copies(job, states=None, kinds=CONTENT_KINDS) -> list[FileCopy]:
    """Register rows this job owns — this job, not an earlier one with the
    same id.

    SQLite hands the id of the newest job to the next one once that job row
    is deleted, and the register keeps the rows of the deleted job (the
    ledger may not lose history). Such a row is older than the job and none
    of the job's own operations touched it: it is skipped. A row this job
    created, or one its operations moved, downloaded or took over
    (`transfer`) after the job existed, counts.
    """
    since = job.created_at
    query = select(FileCopy).where(FileCopy.job_id == job.id, FileCopy.kind.in_(kinds))
    if states:
        query = query.where(FileCopy.state.in_(states))
    async with async_session() as session:
        rows = list((await session.execute(query.order_by(FileCopy.id))).scalars().all())
        if since is None or not rows:
            return rows
        old = [r.id for r in rows if r.created_at is None or r.created_at < since]
        touched = set()
        if old:
            for copy_id, dst_copy_id in (await session.execute(
                    select(FileOp.copy_id, FileOp.dst_copy_id).where(
                        FileOp.job_id == job.id, FileOp.created_at >= since,
                        or_(FileOp.copy_id.in_(old), FileOp.dst_copy_id.in_(old))))).all():
                touched.update((copy_id, dst_copy_id))
    return [r for r in rows if r.id not in old or r.id in touched]


async def known_hashes(job) -> set[str]:
    """Every content hash this job's file ever had: its fields, its copies in
    the register (and the copies it shares) and its ledger (e.g. the hash
    before a metadata write)."""
    hashes = _field_hashes(job)
    own = await own_copies(job)
    hashes.update(r.sha256 for r in own if r.sha256)
    own_ids = [r.id for r in own]
    since = job.created_at or datetime.min
    async with async_session() as session:
        hashes.update((await session.execute(
            select(FileCopy.sha256).join(AssetRef, AssetRef.location == FileCopy.location)
            .where(AssetRef.job_id == job.id, AssetRef.created_at >= since,
                   FileCopy.kind.in_(CONTENT_KINDS), FileCopy.sha256.isnot(None))
        )).scalars().all())
        if own_ids:
            for before, after in (await session.execute(
                    select(FileOp.sha256_before, FileOp.sha256_after).where(
                        FileOp.job_id == job.id, FileOp.created_at >= since,
                        or_(FileOp.copy_id.in_(own_ids), FileOp.dst_copy_id.in_(own_ids))))).all():
                hashes.update(h for h in (before, after) if h)
    return hashes


async def register_row_at(path: str) -> FileCopy | None:
    """The present register row at a local path, if any."""
    return await _register_row_at(os.path.abspath(os.path.normpath(path)))


async def _register_row_at(path: str) -> FileCopy | None:
    async with async_session() as session:
        return (await session.execute(
            select(FileCopy).where(FileCopy.location == path, FileCopy.state == "present")
        )).scalar()


async def _debug_key_of(job_id: int | None) -> str:
    if job_id is None:
        return "keinem Job"
    async with async_session() as session:
        key = (await session.execute(select(Job.debug_key).where(Job.id == job_id))).scalar()
    return key or f"Job #{job_id}"


async def _other_jobs_at(path: str, job_id: int, *, statuses=None) -> list[str]:
    """Debug keys of other jobs whose original_path or target_path is `path`."""
    query = select(Job.debug_key).where(
        Job.id != job_id, or_(Job.original_path == path, Job.target_path == path))
    if statuses:
        query = query.where(Job.status.in_(statuses))
    async with async_session() as session:
        return list((await session.execute(query.limit(5))).scalars().all())


async def job_api_key(job) -> str | None:
    """API key for the job's Immich user (None = the global key). A job whose
    user is gone gets no key at all: the global key sees another account."""
    if not job.immich_user_id:
        return None
    from immich_client import get_user_api_key
    key = await get_user_api_key(job.immich_user_id)
    if not key:
        raise fileops.FileOpRefused(f"Immich-Zugang #{job.immich_user_id} des Jobs ist nicht mehr hinterlegt")
    return key


async def own_local_copy(job) -> FileCopy | None:
    """A local copy this job owns that verifies right now."""
    target, original = _local_path(job.target_path), _local_path(job.original_path)
    rows = await own_copies(job, states=("present",), kinds=_LOCAL_CONTENT_KINDS)
    rows.sort(key=lambda r: (r.location != target, r.location != original, -r.id))
    for row in rows:
        checked = await fileops.verify(row, actor=ACTOR)
        if checked.state == "present":
            return checked
    return None


async def _local_from_fields(job, known: set[str], notes: list[str]) -> Source | None:
    """A file named by target_path / original_path, if it is this job's content."""
    seen = set()
    for label, path in (("target_path", _local_path(job.target_path)),
                        ("original_path", _local_path(job.original_path))):
        if not path or path in seen:
            continue
        seen.add(path)
        if not await asyncio.to_thread(_is_file, path):
            continue
        row = await _register_row_at(path)
        if row is not None and row.job_id != job.id:
            notes.append(f"{path} gehört {await _debug_key_of(row.job_id)} — nicht übernommen")
            continue
        busy = await _other_jobs_at(path, job.id, statuses=("queued", "processing"))
        if busy:
            notes.append(f"{path} wird von {', '.join(busy)} verarbeitet — nicht übernommen")
            continue
        digest = await asyncio.to_thread(_sha256, path)
        if known:
            if digest in known:
                return Source("local", path, digest, row.id if row else None)
            notes.append(f"{path} hat anderen Inhalt als dieser Job (SHA256 {digest[:12]}…) — "
                         "fremde Datei, nicht übernommen")
            continue
        # Nothing records this job's content (old job without a hash). Only a
        # file no other job names is taken, and the log says it was unchecked.
        others = await _other_jobs_at(path, job.id)
        if others:
            notes.append(f"{path}: Inhalt nicht prüfbar (kein Hash bekannt) und auch von "
                         f"{', '.join(others)} verwendet — nicht übernommen")
            continue
        return Source("local", path, digest, row.id if row else None, checked=False)
    return None


async def _immich_source(job, notes: list[str]) -> Source | None:
    """The job's own Immich asset, or one it shares with another job."""
    import immich_client

    try:
        key = await job_api_key(job)
    except fileops.FileOpRefused as exc:
        notes.append(str(exc))
        return None
    rows = list(reversed(await own_copies(job, states=("present",), kinds=("immich",))))
    async with async_session() as session:
        rows += list((await session.execute(select(FileCopy).where(
            FileCopy.kind == "immich", FileCopy.state == "present", FileCopy.job_id != job.id,
            FileCopy.location.in_(select(AssetRef.location).where(
                AssetRef.job_id == job.id, AssetRef.kind == "immich",
                AssetRef.created_at >= (job.created_at or datetime.min))),
        ).order_by(FileCopy.id.desc()))).scalars().all())
    tried = set()
    for row in rows:
        tried.add(row.location)
        try:
            checked = await fileops.verify(row, actor=ACTOR)
        except fileops.FileOpError as exc:
            notes.append(f"Asset {row.location}: {exc}")
            continue
        if checked.state == "present":
            return Source("immich", checked.location, checked.sha256, checked.id)
        notes.append(f"Asset {row.location}: {checked.state}")

    legacy = []
    if _is_immich_target(job.target_path):
        legacy.append(job.target_path[len("immich:"):])
    if job.immich_asset_id:
        legacy.append(job.immich_asset_id)
    for asset_id in dict.fromkeys(legacy):
        if asset_id in tried:
            continue
        try:
            state = await immich_client.get_asset_state(asset_id, api_key=key)
        except Exception as exc:
            notes.append(f"Asset {asset_id}: Zustand in Immich unbekannt ({type(exc).__name__}: {exc})")
            continue
        if state == immich_client.ASSET_PRESENT:
            return Source("immich", asset_id)
        notes.append(f"Asset {asset_id}: {state}")
    return None


async def find_source(job, known: set[str] | None = None) -> tuple[Source | None, list[str]]:
    """Where this job's content can be taken from; (None, reasons) if nowhere.

    Order: a local copy the job owns (register, hash verified now); a file
    named by target_path / original_path whose hash is one of the job's
    (`known_hashes`); the job's Immich asset. Reads only — apart from the
    register noting a copy as missing when its check fails. Call it without
    flushed writes in your session (fileops writes in sessions of its own).
    """
    notes: list[str] = []
    if known is None:
        known = await known_hashes(job)
    row = await own_local_copy(job)
    if row is not None:
        return Source("local", row.location, row.sha256, row.id), notes
    source = await _local_from_fields(job, known, notes)
    if source is None:
        source = await _immich_source(job, notes)
    return source, notes


async def _carry_companions(job, src: str, dst: str, actor: str) -> str | None:
    """After the move of src → dst: move the .xmp sidecar along (returns its
    new place) and drop this job's .log of the previous run. A failure here
    is logged and does not stop the reprocess."""
    key = job.debug_key
    moved_sidecar = None
    sidecar = src + ".xmp"
    if await asyncio.to_thread(_is_file, sidecar):
        try:
            res = await fileops.move(job, sidecar, dst + ".xmp", actor=actor, kind="sidecar")
            moved_sidecar = res.location
            if moved_sidecar != dst + ".xmp":
                await log_warning("reprocess", f"{key} Sidecar unter anderem Namen abgelegt",
                                  f"{sidecar} → {moved_sidecar} (Name {dst}.xmp war belegt)")
        except fileops.FileOpError as exc:
            await log_warning("reprocess", f"{key} Sidecar nicht mitverschoben: {os.path.basename(sidecar)}",
                              f"{sidecar}: {exc}")
    for log_path in (src + ".log", f"{src}.{key}.log"):
        if not await asyncio.to_thread(_is_file, log_path):
            continue
        if not await asyncio.to_thread(_is_own_log, log_path, key):
            continue
        try:
            await fileops.register_existing(job, "temp", log_path)
            await fileops.discard(job, log_path, actor=actor, reason="Fehlerprotokoll des vorherigen Laufs")
        except Exception as exc:
            await log_warning("reprocess", f"{key} Fehlerprotokoll nicht entfernt: {os.path.basename(log_path)}",
                              f"{log_path}: {type(exc).__name__}: {exc}")
    return moved_sidecar


async def materialize(job, source: Source, *, actor: str = ACTOR) -> tuple[str, str | None]:
    """Bring the source into REPROCESS_DIR; returns (file, moved sidecar or None).

    A local copy already inside REPROCESS_DIR stays where it is (a resumed
    retry). Others are moved there by fileops (copy, verify, then remove the
    source; never over another file). An Immich asset is downloaded into a
    work copy of its own. Raises fileops.FileOpError; nothing is lost then.
    """
    reprocess_dir = REPROCESS_DIR
    if source.kind == "local":
        if _inside(source.location, reprocess_dir):
            return source.location, None
        if not source.checked:
            await log_warning("reprocess", f"{job.debug_key} Quelle ohne Inhaltsprüfung übernommen",
                              f"{source.location}: für diesen Job ist kein Hash bekannt")
        await asyncio.to_thread(os.makedirs, reprocess_dir, exist_ok=True)
        name = await asyncio.to_thread(_free_name, reprocess_dir, os.path.basename(source.location),
                                       job.debug_key)
        res = await fileops.move(job, source.location, os.path.join(reprocess_dir, name), actor=actor,
                                 kind="work", expected_sha256=source.sha256)
        sidecar = await _carry_companions(job, source.location, res.location, actor)
        return res.location, sidecar

    key = await job_api_key(job)
    res = await fileops.download(job, source.location, reprocess_dir, actor=actor, kind="work", api_key=key)
    if source.copy_id is None:
        # The asset was known only from the job fields; its content is known now.
        try:
            await fileops.register_existing(job, "immich", source.location, sha256=res.sha256,
                                            size=res.size, verified=True)
        except Exception as exc:
            await log_warning("reprocess", f"{job.debug_key} Immich-Asset nicht im Register eingetragen",
                              f"Asset {source.location}: {type(exc).__name__}: {exc}")
    await log_info("reprocess", f"{job.debug_key} Original aus Immich geladen: {os.path.basename(res.location)}",
                   f"Asset {source.location} → {res.location}")
    return res.location, None


def point_job_at(job, path: str, sidecar: str | None) -> None:
    """The job's file now lies at `path` (in REPROCESS_DIR). A local
    target_path is stale now; an `immich:<id>` reference stays so the job
    remains linked to its Immich data. (A kept IA-07 that names the moved
    sidecar is updated by prepare_job_for_reprocess.)"""
    job.original_path = path
    if not _is_immich_target(job.target_path):
        job.target_path = None


async def _follow_sidecar(session, job, sidecar: str | None) -> None:
    """A kept (not re-run) IA-07 still names the sidecar's old place: its
    result is carried on in a new run that names the new one."""
    if not sidecar:
        return
    ia07 = await runs.current(session, job.id, "IA-07")
    if ia07 is None or not runs.is_done(ia07) or not isinstance(ia07.result, dict) \
            or not ia07.result.get("sidecar_path"):
        return
    await runs.record(session, job, "IA-07", dict(ia07.result, sidecar_path=sidecar), trigger="retry",
                      copied_from_run_id=ia07.id, error=ia07.error, params=ia07.params,
                      started_at=ia07.started_at, finished_at=ia07.finished_at)


@dataclass
class DuplicateOriginal:
    """The job IA-02 named as this job's original, with a copy that checks out now."""
    debug_key: str
    job_id: int
    match_type: str  # IA-02's match type: "exact", "similar", "raw_jpg_pair"
    where: str       # "immich" or "local"


def duplicate_record(job) -> dict | None:
    """IA-02's result if it recorded this job as a duplicate of another job."""
    ia02 = (job.step_result or {}).get("IA-02")
    if not isinstance(ia02, dict) or ia02.get("status") != "duplicate" or not ia02.get("original_debug_key"):
        return None
    return ia02


async def duplicate_original(job) -> DuplicateOriginal | None:
    """Where the content IA-02 matched this job with verifiably lives now.

    Only the original's own copies count, found like a retry finds a job's
    source (find_source on the original): a local copy whose hash checks out
    now, or its Immich asset present (not in the trash). None when IA-02
    named no original, the original is deleted or busy, its only file could
    not be compared, or nothing checks out. Reads only, like find_source.
    What is found is never a source for `job` itself: another job's copy is
    never taken (R-01) — it only tells whether anything was lost.
    """
    ia02 = duplicate_record(job)
    if ia02 is None:
        return None
    key = ia02["original_debug_key"]
    async with async_session() as session:
        original = (await session.execute(
            select(Job).where(Job.debug_key == key, Job.id != job.id).order_by(Job.id.desc()).limit(1)
        )).scalar()
    if original is None or original.status in ("deleted", "queued", "processing"):
        return None
    try:
        source, _ = await find_source(original)
        if source is not None and not source.checked:
            # A local file whose content cannot be compared (old job without
            # a hash) proves nothing; the original's Immich asset may.
            source = await _immich_source(original, [])
    except Exception as exc:  # Immich unreachable and the like: unknown is not "present"
        await log_warning("reprocess", f"{job.debug_key} Original {key} nicht prüfbar",
                          f"{type(exc).__name__}: {exc}")
        return None
    if source is None or not source.checked:
        return None
    return DuplicateOriginal(key, original.id, str(ia02.get("match_type") or "exact"), source.kind)


def _duplicate_hint(original: DuplicateOriginal) -> str:
    where = "in Immich" if original.where == "immich" else "lokal"
    if original.match_type == "exact":
        return (f"Kein eigenes Exemplar. Derselbe Inhalt liegt als {original.debug_key} vor "
                f"(exaktes Duplikat, {where}) — nichts verloren, der Job kann gelöscht werden.")
    if original.match_type == "raw_jpg_pair":
        return (f"Kein eigenes Exemplar. Das zugehörige RAW/JPG liegt als {original.debug_key} vor "
                f"(RAW+JPG-Paar, {where}) — die Datei dieses Jobs selbst fehlt.")
    return (f"Kein eigenes Exemplar. Ein ähnliches Bild liegt als {original.debug_key} vor "
            f"(ähnliches Duplikat, {where}) — die Datei dieses Jobs selbst fehlt; "
            "vor dem Löschen vergleichen.")


def missing_source_message(notes: list[str], original: DuplicateOriginal | None = None) -> str:
    """Error of a retry that found no verified copy of the job's own content.
    `original`: the duplicate's original that holds the matched content now
    (duplicate_original), named so nobody searches for a lost file."""
    message = "Datei nicht auffindbar — Retry abgebrochen. "
    if original is not None:
        message += _duplicate_hint(original)
    else:
        message += "Kein geprüftes Exemplar dieses Jobs gefunden (weder lokal noch in Immich)."
    if notes:
        message += " " + "; ".join(notes)
    return message[:2000]


async def resolved_donor(job) -> bool:
    """A donor whose duplicate group was resolved: its content lives on in
    the kept job, the Immich asset it still names belongs to that job now.
    Only an own local copy that still verifies would make it retryable."""
    if job.status != "done" or not (job.error_message or "").startswith("Duplicate deleted"):
        return False
    return await own_local_copy(job) is None


async def resume_prepare_source(session, job, *, actor: str = ACTOR) -> tuple[bool, list[str]]:
    """Finish a retry that was cut short after it dropped the old results
    (R-10b): find the source again, bring it into REPROCESS_DIR, drop the
    marker. `job` belongs to `session`, which must hold no flushed writes.
    Commits. Returns (ok, reasons)."""
    source, notes = await find_source(job)
    if source is None:
        return False, notes
    try:
        path, sidecar = await materialize(job, source, actor=actor)
    except fileops.FileOpError as exc:
        return False, notes + [str(exc)]
    point_job_at(job, path, sidecar)
    await runs.set_values(session, job, {PREPARE_SOURCE: None})
    await session.commit()
    await log_info("reprocess", f"{job.debug_key} unterbrochenen Retry fortgesetzt",
                   f"Quelle {source.location} → {path}")
    return True, notes


async def _reset_step_results(
    session,
    job,
    *,
    keep_steps: set[str] | None,
    drop_step_statuses: set[str] | None,
    drop_step_codes: set[str] | None = None,
    inject_steps: dict[str, dict] | None,
    run_params: dict | None = None,
    inject_trigger: str = "rerun",
    inject_sources: dict[str, int] | None = None,
) -> list:
    """Apply the chosen reset policy to `job`'s module runs (runs.py).

    Policies (combinable, applied in order):
    - The previous run's parameters (keys starting with "_") are always
      dropped: a reset starts a new run, which gets only what the caller
      passes (R-12: a leaked _force_reupload used to survive every retry).
    - keep_steps: if set, keep ONLY these step codes (and job values) —
      everything else is dropped
    - drop_step_statuses: drop step codes whose result has one of these
      statuses (e.g. {"error", "warning"} for retry of soft failures).
    - drop_step_codes: drop these step codes regardless of status — used
      to act on `error_message="Warnungen in: IA-XX"` even when the cached
      step_result no longer carries the warning status. Live-Vorfall
      MA-2026-28121 vor v2.28.35.
    - inject_steps: these results are set last (e.g.
      {"IA-02": {"status": "skipped", "reason": "..."}}), each as a run of
      its own with `inject_trigger` ('rerun' = set by a user action,
      'copied' = taken over from another job) and the source run from
      `inject_sources`; keys starting with "_" are run parameters, like
      run_params, other keys job values.
    - run_params: parameters for the next run (e.g. {"_force_reupload": True}).

    A dropped module's current run is superseded (it stays in the history);
    a kept one stays current.

    **Cascade**: when ANY step is dropped (via either status or explicit
    code), every step that runs *after* it in the pipeline order is
    dropped too. Otherwise the cached downstream results would still
    reflect the OLD output of the dropped step. Live-Vorfälle MA-2026-28111
    (v2.28.33), MA-2026-28121 (v2.28.35).

    Does not commit. Returns snapshots of the runs that were current before
    and are not any more (for runs.restore).
    """
    before = {m: runs.snapshot(r) for m, r in (await runs.current_runs(session, job.id)).items()}
    current = {k: v for k, v in (job.step_result or {}).items() if not is_run_param(k)}

    if keep_steps is not None:
        current = {k: v for k, v in current.items() if k in keep_steps}

    matched: set[str] = set()
    if drop_step_statuses:
        for step_code, r in current.items():
            if isinstance(r, dict) and r.get("status") in drop_step_statuses:
                matched.add(step_code)
    if drop_step_codes:
        for step_code in drop_step_codes:
            if step_code in current:
                matched.add(step_code)

    if matched:
        to_drop = set(matched)
        for matched_code in matched:
            if matched_code in MAIN:  # the main modules in pipeline order (registry)
                idx = MAIN.index(matched_code)
                for downstream in MAIN[idx + 1:]:
                    to_drop.add(downstream)

        for step_code in to_drop:
            current.pop(step_code, None)

    if inject_steps:
        current.update(inject_steps)
    if run_params:
        current.update(run_params)

    await runs.apply(session, job, current, trigger=inject_trigger, sources=inject_sources, keep_open=False)
    after = await runs.current_runs(session, job.id)
    return [run for module, run in before.items() if module not in after or after[module].id != run.id]


async def prepare_job_for_reprocess(
    session,
    job,
    *,
    keep_steps: set[str] | None = None,
    drop_step_statuses: set[str] | None = None,
    drop_step_codes: set[str] | None = None,
    inject_steps: dict[str, dict] | None = None,
    run_params: dict | None = None,
    move_file: bool = True,
    commit: bool = True,
    actor: str = ACTOR,
    inject_trigger: str = "rerun",
    inject_sources: dict[str, int] | None = None,
) -> bool:
    """Reset `job` for a fresh pipeline run and (optionally) commit.

    The caller is responsible for any locking / status claim that must
    happen *before* this helper runs (e.g. the atomic `error → processing`
    transition in `reset_job_for_retry()`).

    With move_file the job's own content is located (`find_source`) and
    brought into REPROCESS_DIR (`materialize`). The file operations commit
    in sessions of their own, so the caller's session must not hold
    flushed writes at that point (SQLite has one write lock). The module
    runs are written afterwards: from then on the session holds the lock
    until the caller commits.

    Args:
        session: active SQLAlchemy AsyncSession the job is bound to.
        job: ORM Job instance to reset.
        keep_steps: if given, only these step codes survive the reset.
        drop_step_statuses: drop step results with these statuses.
        inject_steps: results set last, each a run of its own (see _reset_step_results).
        run_params: parameters for exactly the next run (keys start with "_").
        inject_trigger, inject_sources: trigger and source runs of the injected results.
        move_file: when True (default), bring the file + sidecar into the
            internal reprocess dir. Set False for in-place reprocessing.
        commit: when True (default), commit the session at the end.

    Returns:
        True if the file is in place (or move_file was False), False if no
        verified copy of the job's content could be found or moved; the job
        is then left as it was (apart from a stale local target_path).
    """
    if move_file:
        source, notes = await find_source(job)
        if source is None:
            await log_warning("reprocess", f"{job.debug_key} Kein Exemplar für die Neuverarbeitung gefunden",
                              "; ".join(notes) or "weder lokal noch in Immich")
            if not _is_immich_target(job.target_path):
                job.target_path = None
            return False
        try:
            path, sidecar = await materialize(job, source, actor=actor)
        except fileops.FileOpError as exc:
            await log_warning("reprocess", f"{job.debug_key} Neuverarbeitung: Datei nicht bereitgestellt",
                              f"{source.location}: {exc}")
            return False

    await _reset_step_results(
        session,
        job,
        keep_steps=keep_steps,
        drop_step_statuses=drop_step_statuses,
        drop_step_codes=drop_step_codes,
        inject_steps=inject_steps,
        run_params=run_params,
        inject_trigger=inject_trigger,
        inject_sources=inject_sources,
    )
    if move_file:
        point_job_at(job, path, sidecar)
        await _follow_sidecar(session, job, sidecar)

    job.status = "queued"
    job.error_message = None

    if commit:
        await session.commit()

    return True

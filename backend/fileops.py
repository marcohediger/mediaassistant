"""The single path for file operations: ledger, copy register, trash.

Every operation that moves, copies, rewrites or removes a file is meant to
go through this module (callers are migrated step by step, see
docs/UMBAU_MODULE.md). Three tables back it (models.py):

- ``file_ops``     append-only ledger. A row is written as ``intent`` before
                   the disk is touched and set exactly once to ``done``,
                   ``failed`` or ``reconciled``. Triggers forbid deleting rows
                   and changing finished ones.
- ``file_copies``  where the copies of a job's content are (kind, location,
                   sha256, state). One ``present`` row per location at most.
- ``asset_refs``   jobs that use a copy they do not own (shared Immich assets,
                   identical content another job already had in place).

Rules every operation follows:

- Nothing is overwritten. Content is written into a private part file
  (``<dir>/.fops-<op id>.part``), verified, and hard-linked to the first free
  name; ``link()`` fails instead of replacing. A name that already holds the
  identical content (same sha256) is reused instead of copying again.
- A source is removed only after its content is verified at the destination
  and the destination is recorded in the ledger.
- Deleting means moving into the set's trash
  (``<base>/.papierkorb/<YYYY-MM-DD>/<debug_key>/<file>``). Only
  ``purge_trash`` removes for good, after the retention period.
- Immich: upload/replace succeed only once the asset is present in Immich
  with the source's checksum. Assets go to Immich's own trash
  (``force=false``), never deleted for good; Immich empties its trash
  itself, and the register notes when an asset is gone from it. A shared
  asset (asset_refs, or the old job fields until the migration) is never
  trashed or replaced away.
- Blocking I/O runs in worker threads. The database is used in short sessions
  of this module's own, never held open across a file copy or an HTTP call.

Callers must not hold uncommitted writes in their own session when calling
in: fileops commits in sessions of its own and SQLite has one write lock (the
reason IA-08 runs with ``no_autoflush``).
"""
import asyncio
import base64
import contextvars
import errno
import hashlib
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from database import async_session
from file_operations import sanitize_path_component
from file_operations import sha256 as _sha256
from models import AssetRef, FileCopy, FileOp, Job
from sets import base_path_for
from system_logger import log_error, log_info, log_warning

logger = logging.getLogger("mediaassistant.fileops")

LOG_SOURCE = "fileops"
# The module run an operation belongs to, when the caller names none: the
# executor sets it around every module (pipeline/executor.py), so each
# ledger row of a step carries its run (file_ops.run_id).
CURRENT_RUN_ID: contextvars.ContextVar = contextvars.ContextVar("fileops_current_run_id", default=None)
LOCAL_KINDS = frozenset({"local", "work", "temp", "sidecar", "takeout_json"})
KINDS = LOCAL_KINDS | {"immich"}
TRASH_DIRNAME = ".papierkorb"
TRASH_RETENTION_DAYS = 30
_TRANSFER_OPS = frozenset({"move", "copy", "trash", "restore", "download"})
_CHUNK = 1024 * 1024
# link() errors that mean "this filesystem has no hard links", not "name taken"
_NO_HARDLINK = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}


# ---------------------------------------------------------------------------
# Results and errors
# ---------------------------------------------------------------------------

@dataclass
class OpResult:
    op_id: int
    copy_id: int | None  # file_copies row that holds the content afterwards
    location: str | None  # where that copy is (for trash: the trash path; Immich: the asset id)
    sha256: str | None
    size: int | None
    identical: bool = False  # the destination already held this content; nothing was copied
    # Immich operations only
    asset_id: str | None = None
    immich_status: str | None = None  # upload: created / duplicate / restored
    owned: bool = True  # False: the asset belongs to another job (or none); the job holds a ref
    albums_added: list = field(default_factory=list)
    albums_created: list = field(default_factory=list)  # albums that did not exist and were created
    tags_written: list = field(default_factory=list)
    tags_failed: list = field(default_factory=list)
    old_asset_id: str | None = None  # replace: the asset that was replaced
    old_trashed: bool = False  # replace: the old asset is in Immich's trash now
    old_kept: str | None = None  # replace: why the old asset stays (another job uses it, …)
    notes: list = field(default_factory=list)


class FileOpError(Exception):
    """Base class. ``op_id`` names the ledger row, if one was written."""

    def __init__(self, message: str, op_id: int | None = None, result: OpResult | None = None):
        super().__init__(message)
        self.op_id = op_id
        self.result = result


class FileOpRefused(FileOpError):
    """A rule forbids the operation; nothing on disk was touched."""


class FileOpBusy(FileOpRefused):
    """Another operation on the same source is still open."""


class FileOpFailed(FileOpError):
    """The operation was attempted and failed. The source is unchanged; if a
    destination was already placed, ``result`` describes it."""


class _ContentMismatch(Exception):
    pass


class _SourceChanged(Exception):
    pass


# Fault injection for test_fileops.py: a crash point raises a BaseException,
# which skips every cleanup handler, like a killed process would.
class _SimulatedCrash(BaseException):
    pass


_CRASH_AT: str | None = None


def _crashpoint(name: str) -> None:
    if _CRASH_AT == name:
        raise _SimulatedCrash(name)


# ---------------------------------------------------------------------------
# Blocking helpers (run in worker threads)
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _stat_key(st) -> tuple:
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _hash_regular(path: str) -> str | None:
    """sha256 of a regular file (not a symlink), None if there is none."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return _sha256(path)


def _same_content(path: str, sha: str, size: int) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != size:
        return False
    return _sha256(path) == sha


def _candidates(filename: str, sep: str):
    stem, ext = os.path.splitext(filename)
    yield filename
    n = 1
    while True:
        yield f"{stem}{sep}{n}{ext}"
        n += 1


def _write_part(src: str, part: str, expected_sha: str | None) -> tuple[str, int, tuple]:
    """Copy src into the private part file, hashing on the way; verify it."""
    st = os.stat(src)
    h = hashlib.sha256()
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        first = True
        while True:
            chunk = inp.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
            if first:
                first = False
                _crashpoint("copying")
        out.flush()
        os.fsync(out.fileno())
    shutil.copystat(src, part)
    sha = h.hexdigest()
    size = os.path.getsize(part)
    if size != st.st_size:
        raise OSError(f"Kopie unvollständig: {size} von {st.st_size} Bytes")
    if expected_sha and sha != expected_sha:
        raise _ContentMismatch(
            f"Inhalt der Quelle weicht vom Register ab (SHA256 {sha[:16]}… statt {expected_sha[:16]}…) "
            "— fremde Datei am Ort?")
    if _sha256(part) != sha:
        raise OSError("Prüfsumme der Kopie weicht ab")
    return sha, size, _stat_key(st)


def _reserve_and_replace(part: str, cand: str) -> bool:
    """Fallback without hard links: reserve the name, then move the part onto it."""
    try:
        fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    os.replace(part, cand)
    return True


def _link_free(part: str, src: str | None, dst_dir: str, filename: str, sha: str, size: int,
               sep: str, merge_identical: bool) -> tuple[str, bool]:
    """Give the verified part file the first free name. Returns (path, identical)."""
    src_real = os.path.realpath(src) if src else None
    for name in _candidates(filename, sep):
        cand = os.path.join(dst_dir, name)
        if src_real and os.path.realpath(cand) == src_real:
            continue  # the source itself is never "the identical destination"
        try:
            os.link(part, cand)
        except FileExistsError:
            if merge_identical and _same_content(cand, sha, size):
                return cand, True
            continue
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK:
                raise
            if not _reserve_and_replace(part, cand):
                if merge_identical and _same_content(cand, sha, size):
                    return cand, True
                continue
            return cand, False
        _crashpoint("linked")
        return cand, False
    raise AssertionError("unreachable")


@dataclass
class _Placed:
    path: str
    sha256: str
    size: int
    identical: bool
    src_stat: tuple | None


def _place_blocking(src, dst_dir, filename, part, expected_sha, sep, merge_identical) -> _Placed:
    os.makedirs(dst_dir, exist_ok=True)
    try:
        sha, size, st_key = _write_part(src, part, expected_sha)
        final, identical = _link_free(part, src, dst_dir, filename, sha, size, sep, merge_identical)
    except Exception:
        _unlink_quiet(part)
        raise
    if identical:
        _unlink_quiet(part)
    _fsync_dir(dst_dir)
    return _Placed(final, sha, size, identical, st_key)


def _drop_source_blocking(src: str | None, st_key: tuple | None, part: str | None) -> None:
    """Remove the part file (an extra link by now) and, for moves, the source."""
    _unlink_quiet(part)
    if src is None:
        return
    st = os.stat(src)
    if st_key is not None and _stat_key(st) != st_key:
        raise _SourceChanged("Quelle wurde während des Kopierens verändert — nicht entfernt")
    os.remove(src)
    _fsync_dir(os.path.dirname(src))


def _adopt_blocking(path: str, dst_dir: str, filename: str, sep: str) -> _Placed:
    """Hash a file written into a private dir and link it to a free name."""
    with open(path, "rb") as f:
        os.fsync(f.fileno())
    sha = _sha256(path)
    size = os.path.getsize(path)
    final, _ = _link_free(path, None, dst_dir, filename, sha, size, sep, merge_identical=False)
    _fsync_dir(dst_dir)
    return _Placed(final, sha, size, False, None)


def _remove_tree_quiet(path: str | None) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _remove_blocking(path: str, expected_size: int | None) -> None:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError("keine reguläre Datei")
    if expected_size is not None and st.st_size != expected_size:
        raise OSError(f"Grösse {st.st_size} statt {expected_size} Bytes — nicht gelöscht")
    os.remove(path)
    _fsync_dir(os.path.dirname(path))


def _rewrite_blocking(path: str, part: str, sha_before: str | None, writer) -> tuple[str, int]:
    """Let writer change a private copy, then swap it in atomically."""
    try:
        _sha, _size, st_key = _write_part(path, part, sha_before)
        writer(part)
        new_sha = _sha256(part)
        new_size = os.path.getsize(part)
        if _stat_key(os.stat(path)) != st_key:
            raise _SourceChanged("Datei wurde während des Schreibens verändert — nicht ersetzt")
    except Exception:
        _unlink_quiet(part)
        raise
    _crashpoint("rewritten")
    os.replace(part, path)
    _fsync_dir(os.path.dirname(path))
    return new_sha, new_size


def _prune_trash_dirs(path: str) -> None:
    """Remove empty <date>/<debug_key> folders above a trash file, never the trash root."""
    current = os.path.dirname(path)
    while os.path.basename(current) != TRASH_DIRNAME:
        parent = os.path.dirname(current)
        if parent == current or TRASH_DIRNAME not in current.split(os.sep):
            return
        try:
            os.rmdir(current)
        except OSError:
            return
        current = parent


def _find_sibling(dst_dir: str, st) -> str | None:
    """Another name of the same inode in dst_dir (a part file already linked)."""
    try:
        entries = list(os.scandir(dst_dir))
    except FileNotFoundError:
        return None
    for entry in entries:
        if entry.name.startswith(".fops-"):
            continue
        try:
            est = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if est.st_ino == st.st_ino and est.st_dev == st.st_dev:
            return entry.path
    return None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"Datei nicht gefunden: {exc.filename or exc}"
    return f"{exc}" if isinstance(exc, (_ContentMismatch, _SourceChanged)) else f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Database helpers (short sessions of our own)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _JobRef:
    id: int | None
    debug_key: str
    set_id: int | None
    immich_user_id: int | None = None


async def _job_ref(job) -> _JobRef:
    """Who an operation is for. Reads only the identity of a Job object, so
    the caller's session is never touched (no lazy load, no autoflush)."""
    if isinstance(job, _JobRef):
        return job
    if job is None:
        return _JobRef(None, "system", None)
    if isinstance(job, int):
        job_id = job
    else:
        state = sa_inspect(job, raiseerr=False)
        identity = getattr(state, "identity", None)
        job_id = identity[0] if identity else vars(job).get("id")
    if job_id is None:
        raise ValueError("fileops: job has no id (not flushed yet?)")
    async with async_session() as session:
        row = (await session.execute(
            select(Job.debug_key, Job.set_id, Job.immich_user_id).where(Job.id == job_id))).first()
    if row is None:
        return _JobRef(job_id, f"job-{job_id}", None)
    return _JobRef(job_id, row.debug_key or f"job-{job_id}", row.set_id, row.immich_user_id)


async def _debug_keys(session, job_ids) -> list[str]:
    ids = [j for j in job_ids if j is not None]
    if not ids:
        return []
    found = dict((await session.execute(
        select(Job.id, Job.debug_key).where(Job.id.in_(ids)))).all())
    return [found.get(j) or f"job-{j}" for j in ids]


async def _owner_label(session, job_id) -> str:
    if job_id is None:
        return "keinem Job (nicht von MediaAssistant angelegt)"
    return (await _debug_keys(session, [job_id]))[0]


async def _present_at(session, location: str) -> FileCopy | None:
    return (await session.execute(
        select(FileCopy).where(FileCopy.location == location, FileCopy.state == "present")
    )).scalar()


async def _foreign_refs(session, location: str, job_id: int | None) -> list[int]:
    return list((await session.execute(
        select(AssetRef.job_id).where(AssetRef.location == location, AssetRef.job_id != job_id)
    )).scalars().all())


async def _add_ref_in(session, kind: str, location: str, job_id: int) -> None:
    exists = (await session.execute(select(AssetRef.id).where(
        AssetRef.kind == kind, AssetRef.location == location, AssetRef.job_id == job_id))).first()
    if not exists:
        session.add(AssetRef(kind=kind, location=location, job_id=job_id))
        await session.flush()


async def _repoint_refs(session, old_location: str, dst: FileCopy) -> None:
    refs = (await session.execute(
        select(AssetRef).where(AssetRef.location == old_location))).scalars().all()
    for ref in refs:
        taken = (await session.execute(select(AssetRef.id).where(
            AssetRef.kind == dst.kind, AssetRef.location == dst.location,
            AssetRef.job_id == ref.job_id))).first()
        if ref.job_id == dst.job_id or taken:
            await session.delete(ref)
        else:
            ref.kind, ref.location = dst.kind, dst.location
    await session.flush()


async def _claim(session, job_id, kind, location, sha, size, notes: list, *,
                 verified: bool = True) -> FileCopy:
    """The present row for content `sha` at `location`, created if needed.

    A present row with other content is stale (its file is no longer there)
    and becomes 'missing'. A row owned by another job gets a reference.
    """
    now = datetime.now()
    existing = await _present_at(session, location)
    if existing is not None:
        if existing.sha256 is None or sha is None or existing.sha256 == sha:
            existing.sha256 = sha or existing.sha256
            existing.size = size if size is not None else existing.size
            if verified:
                existing.verified_at = now
            if job_id is not None and existing.job_id != job_id:
                await _add_ref_in(session, existing.kind, location, job_id)
            return existing
        existing.state = "missing"
        notes.append(f"Exemplar #{existing.id} an {location} hatte anderen Inhalt, jetzt als fehlend markiert")
        await session.flush()
    row = FileCopy(job_id=job_id, kind=kind, location=location, sha256=sha, size=size,
                   state="present", verified_at=now if verified else None)
    session.add(row)
    await session.flush()
    return row


async def _begin(jref: _JobRef, *, op, actor, src_kind, src, dst_kind=None, dst_dir=None,
                 filename=None, copy_id=None, sha256_before=None, size=None, reason=None,
                 run_id=None, register_src=None, part_suffix=None, tmp_dir=False,
                 detail: dict | None = None) -> tuple[int, dict]:
    """Write the intent row; returns (op id, detail). Raises FileOpBusy."""
    if run_id is None:
        run_id = CURRENT_RUN_ID.get()
    async with async_session() as session:
        try:
            if register_src is not None:
                # An unregistered source is registered in the same transaction.
                row = FileCopy(job_id=jref.id, kind=register_src, location=src, state="present",
                               sha256=sha256_before)
                session.add(row)
                await session.flush()
                copy_id = row.id
            entry = FileOp(job_id=jref.id, run_id=run_id, op=op, src_kind=src_kind, src=src,
                           dst_kind=dst_kind, copy_id=copy_id, sha256_before=sha256_before,
                           size=size, actor=actor, reason=reason, state="intent")
            session.add(entry)
            await session.flush()
            detail = dict(detail or {})
            if dst_dir:
                detail.update(dst_dir=dst_dir, filename=filename)
            if part_suffix is not None:
                base_dir = dst_dir or os.path.dirname(src)
                detail["part"] = os.path.join(base_dir, f".fops-{entry.id}{part_suffix}")
            if tmp_dir:
                detail["tmp_dir"] = os.path.join(dst_dir, f".fops-{entry.id}.dl")
            entry.detail = detail
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise FileOpBusy(f"Für {src} ist bereits eine Operation offen") from exc
        return entry.id, detail


async def _finish(session, op_id: int, state: str, **values) -> None:
    """Set the end state; exactly once. The caller commits."""
    res = await session.execute(
        update(FileOp).where(FileOp.id == op_id, FileOp.state == "intent")
        .values(state=state, finished_at=datetime.now(), **values)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise FileOpError(f"Protokollzeile #{op_id} ist nicht mehr offen", op_id)


async def _fail(op_id: int, error: str, **values) -> None:
    async with async_session() as session:
        try:
            await _finish(session, op_id, "failed", error=error, **values)
            await session.commit()
        except FileOpError:
            await session.rollback()


async def _refused(jref, op, actor, src_kind, src, copy_id, problem, reason=None) -> int:
    """Record a refusal: a ledger row that was never an intent."""
    async with async_session() as session:
        entry = FileOp(job_id=jref.id, run_id=CURRENT_RUN_ID.get(), op=op, src_kind=src_kind, src=src,
                       copy_id=copy_id, actor=actor, reason=reason, state="failed", error=problem,
                       finished_at=datetime.now())
        session.add(entry)
        await session.commit()
        return entry.id


async def _record_placement(op_id: int, placed: _Placed) -> None:
    async with async_session() as session:
        res = await session.execute(
            update(FileOp).where(FileOp.id == op_id, FileOp.state == "intent").values(
                dst=placed.path, sha256_after=placed.sha256, size=placed.size,
                sha256_before=func.coalesce(FileOp.sha256_before, placed.sha256))
            .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            raise FileOpError(f"Protokollzeile #{op_id} ist nicht mehr offen", op_id)
        await session.commit()


async def _settle(session, op: FileOp, final: str, sha: str, size: int,
                  src_gone: bool) -> tuple[int | None, list[str]]:
    """Bring file_copies in line with a placed transfer. Used by the normal
    path, by a failed source removal and by reconcile."""
    notes: list[str] = []
    now = datetime.now()
    src_row = await session.get(FileCopy, op.copy_id) if op.copy_id else None

    if op.op in ("move", "copy", "download"):
        dst = await _claim(session, op.job_id, op.dst_kind or "local", final, sha, size, notes)
        if src_row is not None and src_row.sha256 is None and op.op != "download":
            src_row.sha256, src_row.size, src_row.verified_at = sha, size, now
        if op.op == "move":
            if src_gone:
                if src_row is not None and src_row.state == "present":
                    src_row.state = "moved"
                await _repoint_refs(session, op.src, dst)
            else:
                notes.append("Quelle noch vorhanden")
        return dst.id, notes

    if op.op == "trash":
        if src_gone:
            src_row.state = "trashed"
            src_row.trash_location = final
            src_row.trashed_at = now
            src_row.sha256, src_row.size, src_row.verified_at = sha, size, now
            return src_row.id, notes
        extra = FileCopy(job_id=src_row.job_id, kind=src_row.kind, location=src_row.location,
                         sha256=sha, size=size, state="trashed", trash_location=final,
                         trashed_at=op.created_at or now, verified_at=now)
        session.add(extra)
        await session.flush()
        notes.append("Original noch vorhanden; zusätzliche Kopie im Papierkorb")
        return extra.id, notes

    if op.op == "restore":
        existing = await _present_at(session, final)
        if existing is not None and existing.id != src_row.id and existing.sha256 in (None, sha):
            existing.sha256, existing.size, existing.verified_at = sha, size, now
            if existing.job_id != src_row.job_id:
                await _add_ref_in(session, existing.kind, final, src_row.job_id)
            if src_gone:
                src_row.state = "moved"
            notes.append("derselbe Inhalt lag bereits am Ziel")
            return existing.id, notes
        if existing is not None and existing.id != src_row.id:
            existing.state = "missing"
            notes.append(f"Exemplar #{existing.id} an {final} hatte anderen Inhalt, jetzt als fehlend markiert")
            await session.flush()
        if src_gone:
            src_row.state = "present"
            src_row.location = final
            src_row.trash_location = None
            src_row.trashed_at = None
            src_row.sha256, src_row.size, src_row.verified_at = sha, size, now
            await session.flush()
            return src_row.id, notes
        restored = FileCopy(job_id=src_row.job_id, kind=src_row.kind, location=final, sha256=sha,
                            size=size, state="present", verified_at=now)
        session.add(restored)
        await session.flush()
        notes.append("Kopie bleibt zusätzlich im Papierkorb")
        return restored.id, notes

    raise ValueError(f"fileops: no settle rule for {op.op}")


# ---------------------------------------------------------------------------
# The transfer engine: intent → place → record → drop source → done
# ---------------------------------------------------------------------------

@dataclass
class _Plan:
    jref: _JobRef
    op: str
    actor: str
    src_kind: str
    src: str
    dst_kind: str
    dst_dir: str
    filename: str
    remove_source: bool
    merge_identical: bool = True
    expected_sha: str | None = None
    copy_id: int | None = None
    sep: str = "_"
    reason: str | None = None
    run_id: int | None = None
    register_src: str | None = None
    texts: dict = field(default_factory=dict)  # before / after / verb (German, for system logs)


async def _run(plan: _Plan, recheck=None) -> OpResult:
    key = plan.jref.debug_key
    name = os.path.basename(plan.src)
    verb = plan.texts.get("verb", plan.op)
    op_id, detail = await _begin(
        plan.jref, op=plan.op, actor=plan.actor, src_kind=plan.src_kind, src=plan.src,
        dst_kind=plan.dst_kind, dst_dir=plan.dst_dir, filename=plan.filename,
        copy_id=plan.copy_id, sha256_before=plan.expected_sha, reason=plan.reason,
        run_id=plan.run_id, register_src=plan.register_src, part_suffix=".part")
    part = detail["part"]
    if plan.texts.get("before"):
        await log_info(LOG_SOURCE, f"{key} {plan.texts['before']}: {name}",
                       f"#{op_id} {plan.src} → {plan.dst_dir}")
    _crashpoint("intent")

    if recheck is not None:
        problem = await recheck()
        if problem:
            await _fail(op_id, problem)
            await log_warning(LOG_SOURCE, f"{key} {verb} verweigert: {name}", f"#{op_id} {problem}")
            raise FileOpRefused(problem, op_id)

    try:
        placed = await asyncio.to_thread(
            _place_blocking, plan.src, plan.dst_dir, plan.filename, part,
            plan.expected_sha, plan.sep, plan.merge_identical)
    except Exception as exc:
        error = _describe(exc)
        await _fail(op_id, error)
        await log_warning(LOG_SOURCE, f"{key} {verb} fehlgeschlagen: {name}", f"#{op_id} {error}")
        if isinstance(exc, (_ContentMismatch, FileNotFoundError)) and plan.copy_id:
            await verify(plan.copy_id)  # marks the registered copy missing if it is not there
        raise FileOpFailed(error, op_id) from exc

    await _record_placement(op_id, placed)
    _crashpoint("placed")

    src_error = None
    try:
        await asyncio.to_thread(_drop_source_blocking,
                                plan.src if plan.remove_source else None, placed.src_stat, part)
    except Exception as exc:
        src_error = _describe(exc)
    if src_error is None:
        _crashpoint("source_removed")

    async with async_session() as session:
        op = await session.get(FileOp, op_id)
        dst_copy_id, notes = await _settle(session, op, placed.path, placed.sha256, placed.size,
                                           src_gone=plan.remove_source and src_error is None)
        new_detail = dict(op.detail or {}, identical=placed.identical)
        if notes:
            new_detail["notes"] = notes
        await _finish(session, op_id, "failed" if src_error else "done",
                      dst_copy_id=dst_copy_id, detail=new_detail, error=src_error)
        await session.commit()

    result = OpResult(op_id, dst_copy_id, placed.path, placed.sha256, placed.size, placed.identical)
    if src_error:
        await log_warning(LOG_SOURCE, f"{key} {verb}: Ziel abgelegt, Quelle nicht entfernt: {name}",
                          f"#{op_id} {placed.path} — {src_error}")
        raise FileOpFailed(src_error, op_id, result)
    if plan.texts.get("after"):
        same = ", derselbe Inhalt lag bereits dort" if placed.identical else ""
        await log_info(LOG_SOURCE, f"{key} {plan.texts['after']}: {name}",
                       f"#{op_id} {plan.src} → {placed.path} (SHA256 {placed.sha256[:16]}…{same})")
    return result


def _split_dst(src: str, dst: str) -> tuple[str, str]:
    """dst is a directory if it ends with a separator or exists as one."""
    if dst.endswith(os.sep) or os.path.isdir(dst):
        return _norm(dst), os.path.basename(src)
    dst = _norm(dst)
    return os.path.dirname(dst), os.path.basename(dst)


# ---------------------------------------------------------------------------
# Public API: register, verify, move, copy, download
# ---------------------------------------------------------------------------

async def register_existing(job, kind: str, location: str, sha256: str | None = None,
                            size: int | None = None, *, verified: bool | None = None,
                            checksum: str | None = None, take_over: bool = False,
                            intake: str | None = None) -> FileCopy:
    """Record a copy that came into the system without fileops (inbox scan,
    an asset found in Immich). Nothing on disk changes.

    Local kinds: without sha256 the file is hashed now (verified). With a
    given sha256 it is trusted but unverified, unless verified=True. If the
    location already belongs to another job with different content, the file
    is hashed to decide which row is stale.

    ``take_over`` is for a file that just arrived where an older job's file
    used to be (inbox scan): if the register still lists the same content
    there for another job that is not queued or processing, that row is
    stale — the older file left through a path the register did not see —
    and becomes 'moved'; the new job owns the file. Without it the new job
    only gets a reference.

    ``intake`` is given only by the input that created the job (actor, e.g.
    ``input:inbox``, ``input:immich``). The first such call for a job writes
    the ledger row ``register`` — done at once, nothing was moved: where the
    job's content was taken in and its hash (or Immich checksum) then. Later
    calls never write one, so each job has at most one.
    """
    if kind not in KINDS:
        raise ValueError(f"fileops: unknown kind {kind!r}")
    jref = await _job_ref(job)
    notes: list[str] = []

    async def record_intake(session, row: FileCopy) -> None:
        if not intake or jref.id is None:
            return
        if (await session.execute(select(FileOp.id).where(
                FileOp.job_id == jref.id, FileOp.op == "register").limit(1))).first():
            return
        session.add(FileOp(job_id=jref.id, op="register", dst_kind=kind, dst=row.location,
                           dst_copy_id=row.id, sha256_after=row.sha256, size=row.size, actor=intake,
                           state="done", finished_at=datetime.now(),
                           detail={"checksum": checksum} if checksum else None))
        await session.flush()

    if kind == "immich":
        async with async_session() as session:
            row = await _claim(session, jref.id, kind, location, sha256, size, notes,
                               verified=bool(verified))
            if checksum:
                row.checksum = checksum
            await record_intake(session, row)
            await session.commit()
        return row

    location = _norm(location)
    st = await asyncio.to_thread(os.stat, location)
    if sha256 is None:
        sha256 = await asyncio.to_thread(_sha256, location)
        verified = True
    async with async_session() as session:
        existing = await _present_at(session, location)
    if existing is not None and existing.sha256 and existing.sha256 != sha256 and not verified:
        sha256 = await asyncio.to_thread(_sha256, location)
        verified = True
    async with async_session() as session:
        existing = await _present_at(session, location)
        if (take_over and existing is not None and existing.job_id != jref.id
                and existing.sha256 in (None, sha256)):
            status = (await session.execute(
                select(Job.status).where(Job.id == existing.job_id))).scalar() \
                if existing.job_id is not None else None
            if status not in ("queued", "processing"):
                existing.state = "moved"
                owner = await _owner_label(session, existing.job_id)
                notes.append(f"Exemplar #{existing.id} von {owner} an {location} war veraltet "
                             f"(Datei neu eingelesen), jetzt 'moved'")
                await session.flush()
        row = await _claim(session, jref.id, kind, location, sha256, st.st_size, notes,
                           verified=bool(verified))
        await record_intake(session, row)
        await session.commit()
    for note in notes:
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Register korrigiert", note)
    return row


async def verify(copy, *, actor: str = "system:verify") -> FileCopy:
    """Re-hash a copy against the register; mark it missing when it is not there."""
    copy_id = copy.id if isinstance(copy, FileCopy) else int(copy)
    async with async_session() as session:
        row = await session.get(FileCopy, copy_id)
    if row is None:
        raise FileOpRefused(f"Exemplar #{copy_id} nicht gefunden")
    if row.kind == "immich":
        return await _verify_immich(row, actor)
    if row.state not in ("present", "trashed", "missing"):
        return row
    path = row.trash_location if row.state == "trashed" else row.location
    actual = await asyncio.to_thread(_hash_regular, path) if path else None
    ok = actual is not None and (row.sha256 is None or actual == row.sha256)
    key = (await _job_ref(row.job_id)).debug_key if row.job_id else "system"
    async with async_session() as session:
        row = await session.get(FileCopy, copy_id)
        if ok:
            row.sha256 = actual
            row.verified_at = datetime.now()
            if row.state == "missing" and await _present_at(session, row.location) is None:
                row.state = "present"
        elif row.state != "missing":
            row.state = "missing"
        await session.commit()
    if not ok:
        why = "fehlt" if actual is None else "hat anderen Inhalt"
        await log_warning(LOG_SOURCE, f"{key} Exemplar {why}: {os.path.basename(path or '')}",
                          f"Exemplar #{copy_id} {path} ({actor})")
    return row


async def move(job, src: str, dst: str, *, actor: str, kind: str | None = None,
               expected_sha256: str | None = None, sep: str = "_",
               run_id: int | None = None) -> OpResult:
    """Move src to dst (a directory, or a path whose name is a wish).

    Never overwrites: a taken name gets a counter (``name<sep>1.ext``), a name
    holding the identical content is reused. The source must hold the
    content the register (or ``expected_sha256``) expects and must not belong
    to another job. Raises FileOpRefused / FileOpBusy / FileOpFailed.
    """
    jref = await _job_ref(job)
    src = _norm(src)
    dst_dir, filename = _split_dst(src, dst)
    if os.path.join(dst_dir, filename) == src:
        raise FileOpRefused("Quelle und Ziel sind derselbe Ort")
    async with async_session() as session:
        row = await _present_at(session, src)
        owner = (await _debug_keys(session, [row.job_id]))[0] \
            if row is not None and row.job_id != jref.id else None
    if owner:
        problem = f"Exemplar gehört {owner}"
        op_id = await _refused(jref, "move", actor, row.kind, src, row.id, problem)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Verschieben verweigert: {os.path.basename(src)}",
                          f"#{op_id} {problem}")
        raise FileOpRefused(problem, op_id)
    plan = _Plan(
        jref=jref, op="move", actor=actor, src_kind=row.kind if row else "local", src=src,
        dst_kind=kind or (row.kind if row else "local"), dst_dir=dst_dir, filename=filename,
        remove_source=True, expected_sha=expected_sha256 or (row.sha256 if row else None),
        copy_id=row.id if row else None, sep=sep, run_id=run_id,
        register_src=None if row else "local",
        texts={"before": "Verschieben beginnt", "after": "Verschoben", "verb": "Verschieben"})
    return await _run(plan)


async def copy(job, src: str, dst: str, *, actor: str, kind: str = "work",
               expected_sha256: str | None = None, sep: str = "_",
               run_id: int | None = None) -> OpResult:
    """Copy src to dst; the new copy belongs to `job`. Same naming rules as move."""
    jref = await _job_ref(job)
    src = _norm(src)
    dst_dir, filename = _split_dst(src, dst)
    if os.path.join(dst_dir, filename) == src:
        raise FileOpRefused("Quelle und Ziel sind derselbe Ort")
    async with async_session() as session:
        row = await _present_at(session, src)
    plan = _Plan(
        jref=jref, op="copy", actor=actor, src_kind=row.kind if row else "local", src=src,
        dst_kind=kind, dst_dir=dst_dir, filename=filename, remove_source=False,
        expected_sha=expected_sha256 or (row.sha256 if row else None),
        copy_id=row.id if row else None, sep=sep, run_id=run_id,
        register_src=None if row else "local", texts={"verb": "Kopieren"})
    return await _run(plan)


async def download(job, asset_id: str, dst_dir: str, *, actor: str, filename: str | None = None,
                   kind: str = "work", api_key: str | None = None, sep: str = "_",
                   run_id: int | None = None) -> OpResult:
    """Download an Immich original into a copy of its own in dst_dir (R-03).

    immich_client.download_asset writes with "wb" into the directory it is
    given, so it gets a private directory; the result is linked to a free
    name. Two jobs downloading the same name never touch each other's file.
    """
    import immich_client

    jref = await _job_ref(job)
    dst_dir = _norm(dst_dir)
    op_id, detail = await _begin(jref, op="download", actor=actor, src_kind="immich", src=asset_id,
                                 dst_kind=kind, dst_dir=dst_dir, filename=filename,
                                 run_id=run_id, tmp_dir=True)
    tmp_dir = detail["tmp_dir"]
    try:
        await asyncio.to_thread(os.makedirs, tmp_dir)
        path = await immich_client.download_asset(asset_id, tmp_dir, api_key=api_key)
        placed = await asyncio.to_thread(_adopt_blocking, path, dst_dir,
                                         filename or os.path.basename(path), sep)
    except Exception as exc:
        error = _describe(exc)
        await asyncio.to_thread(_remove_tree_quiet, tmp_dir)
        await _fail(op_id, error)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Download fehlgeschlagen: {asset_id}",
                          f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    await _record_placement(op_id, placed)
    _crashpoint("placed")
    await asyncio.to_thread(_remove_tree_quiet, tmp_dir)
    async with async_session() as session:
        op = await session.get(FileOp, op_id)
        dst_copy_id, notes = await _settle(session, op, placed.path, placed.sha256, placed.size, True)
        await _finish(session, op_id, "done", dst_copy_id=dst_copy_id,
                      detail=dict(op.detail or {}, **({"notes": notes} if notes else {})))
        await session.commit()
    return OpResult(op_id, dst_copy_id, placed.path, placed.sha256, placed.size)


async def write_metadata(job, target, writer, *, actor: str,
                         expected_sha256: str | None = None, run_id: int | None = None) -> OpResult:
    """Change a file's content in place, crash-safe (R-10f).

    ``writer(path)`` is a blocking callable (e.g. an exiftool run) that edits
    the file at `path`. It gets a private copy; the result replaces the file
    atomically, so after a crash the file is either old or new, never torn.
    """
    jref = await _job_ref(job)
    row = await _resolve_copy(jref, target, expected_sha256, register=True)
    name = os.path.basename(row.location)
    async with async_session() as session:
        refs = await _foreign_refs(session, row.location, jref.id)
        problem = None
        if row.job_id != jref.id:
            problem = f"Exemplar gehört {(await _debug_keys(session, [row.job_id]))[0]}"
        elif refs:
            problem = f"wird verwendet von {', '.join(await _debug_keys(session, refs))}"
    if problem:
        op_id = await _refused(jref, "write_metadata", actor, row.kind, row.location, row.id, problem)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Metadaten schreiben verweigert: {name}",
                          f"#{op_id} {problem}")
        raise FileOpRefused(problem, op_id)
    ext = os.path.splitext(row.location)[1]
    op_id, detail = await _begin(jref, op="write_metadata", actor=actor, src_kind=row.kind,
                                 src=row.location, copy_id=row.id, sha256_before=row.sha256,
                                 size=row.size, run_id=run_id, part_suffix=f".tmp{ext}")
    await log_info(LOG_SOURCE, f"{jref.debug_key} Metadaten werden geschrieben: {name}",
                   f"#{op_id} {row.location}")
    try:
        new_sha, new_size = await asyncio.to_thread(_rewrite_blocking, row.location, detail["part"],
                                                    row.sha256, writer)
    except Exception as exc:
        error = _describe(exc)
        await _fail(op_id, error)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Metadaten schreiben fehlgeschlagen: {name}",
                          f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    async with async_session() as session:
        current = await session.get(FileCopy, row.id)
        current.sha256, current.size, current.verified_at = new_sha, new_size, datetime.now()
        await _finish(session, op_id, "done", dst_kind=row.kind, dst=row.location,
                      dst_copy_id=row.id, sha256_after=new_sha, size=new_size)
        await session.commit()
    await log_info(LOG_SOURCE, f"{jref.debug_key} Metadaten geschrieben: {name}",
                   f"#{op_id} SHA256 {(row.sha256 or '?')[:16]}… → {new_sha[:16]}…")
    return OpResult(op_id, row.id, row.location, new_sha, new_size)


# ---------------------------------------------------------------------------
# Public API: trash, restore, purge, discard
# ---------------------------------------------------------------------------

async def _resolve_copy(jref: _JobRef, target, expected_sha256: str | None, *,
                        register: bool) -> FileCopy:
    """The present row for target (FileCopy, id or path), with a known sha256.

    An unregistered path is registered for `jref` only with ``register``;
    the file is hashed and must match ``expected_sha256`` when one is given.
    trash() registers only when the caller supplied an expected hash.
    """
    path = None
    async with async_session() as session:
        if isinstance(target, FileCopy):
            row = await session.get(FileCopy, target.id)
        elif isinstance(target, int):
            row = await session.get(FileCopy, target)
        else:
            path = _norm(target)
            row = await _present_at(session, path)
    if row is None and path is None:
        raise FileOpRefused("Exemplar nicht gefunden")
    if row is None:
        if not register:
            raise FileOpRefused(f"{path} ist nicht registriert — ohne erwarteten Hash wird nichts gelöscht")
        actual = await asyncio.to_thread(_hash_regular, path)
        if actual is None:
            raise FileOpRefused(f"Datei fehlt: {path}")
        if expected_sha256 and actual != expected_sha256:
            raise FileOpRefused(f"{path} hat anderen Inhalt als erwartet — fremde Datei?")
        size = await asyncio.to_thread(os.path.getsize, path)
        async with async_session() as session:
            row = await _claim(session, jref.id, "local", path, actual, size, [])
            await session.commit()
        return row
    if row.state != "present":
        raise FileOpRefused(f"Exemplar #{row.id} ist nicht vorhanden (Zustand {row.state})")
    if row.sha256 is None and row.kind in LOCAL_KINDS:
        row = await verify(row)
        if row.state != "present":
            raise FileOpRefused(f"Exemplar #{row.id} fehlt am Ort {row.location}")
    return row


async def _verified_twin(row: FileCopy) -> FileCopy | None:
    """Another present copy of the same content that checks out right now."""
    if not row.sha256:
        return None
    async with async_session() as session:
        twins = (await session.execute(select(FileCopy).where(
            FileCopy.sha256 == row.sha256, FileCopy.state.in_(("present", "locked")),
            FileCopy.id != row.id, FileCopy.location != row.location,
        ).order_by(FileCopy.verified_at.desc()))).scalars().all()
    for twin in twins:
        if twin.state == "locked" and twin.kind != "immich":
            continue
        try:
            checked = await verify(twin)
        except Exception as exc:
            # Immich unreachable: a copy that cannot be checked does not count.
            logger.info("twin #%s not verifiable: %s", twin.id, exc)
            continue
        if checked.state == "present":
            return checked
        # Locked by MediaAssistant after a verified upload: the content is in
        # Immich's locked folder, only hidden from the API key. A trashed
        # asset never counts (R-22: Immich empties its trash).
        if checked.kind == "immich" and checked.state == "locked" and checked.verified_at:
            return checked
    return None


async def trash_blockers(job, target, *, allow_last_copy: bool = False) -> list[str]:
    """Why `job` may not trash `target` (a FileCopy, its id, or an Immich
    asset id); empty = allowed."""
    jref = await _job_ref(job)
    try:
        if _is_asset_id(target):
            row = await _resolve_asset(jref, target)
            if row.state != "present":
                raise FileOpRefused(f"Exemplar #{row.id} ist nicht vorhanden (Zustand {row.state})")
        else:
            row = await _resolve_copy(jref, target, None, register=False)
    except FileOpRefused as exc:
        return [str(exc)]
    return await _blockers(jref, row, allow_last_copy)


async def _blockers(jref: _JobRef, row: FileCopy, allow_last_copy: bool) -> list[str]:
    reasons = []
    async with async_session() as session:
        if row.job_id != jref.id:
            reasons.append(f"Exemplar gehört {await _owner_label(session, row.job_id)}")
        refs = await _foreign_refs(session, row.location, jref.id)
        if row.kind == "immich":
            refs += await _adopt_legacy_refs(session, row, jref.id, refs)
        if refs:
            reasons.append(f"wird verwendet von {', '.join(await _debug_keys(session, refs))}")
        await session.commit()
    if not allow_last_copy and not reasons and await _verified_twin(row) is None:
        reasons.append("letztes Exemplar dieses Inhalts (kein anderes geprüftes Exemplar)")
    return reasons


async def _is_immich_target(target) -> bool:
    if isinstance(target, str):
        return _is_asset_id(target)
    if isinstance(target, FileCopy):
        return target.kind == "immich"
    if isinstance(target, int) and not isinstance(target, bool):
        async with async_session() as session:
            row = await session.get(FileCopy, target)
        return row is not None and row.kind == "immich"
    return False


async def trash(job, target, *, actor: str, reason: str, allow_last_copy: bool = False,
                expected_sha256: str | None = None, run_id: int | None = None) -> OpResult:
    """Move a local copy into the set's trash instead of deleting it.

    Allowed only if (a) another present copy of the same content verifies
    right now — unless ``allow_last_copy`` (a delete the user asked for; the
    trash itself is then the fallback) — and (b) the copy is owned by `job`
    and no other job references it. `target` is a FileCopy, its id, or a
    path; an unregistered path needs ``expected_sha256`` and must match it.
    An Immich asset (its id, or its FileCopy) goes to trash_asset().
    """
    if await _is_immich_target(target):
        return await trash_asset(job, target, actor=actor, reason=reason,
                                 allow_last_copy=allow_last_copy, run_id=run_id)
    jref = await _job_ref(job)
    try:
        row = await _resolve_copy(jref, target, expected_sha256,
                                  register=expected_sha256 is not None)
    except FileOpRefused as exc:
        src = target if isinstance(target, str) else None
        op_id = await _refused(jref, "trash", actor, "local", src, None, str(exc), reason)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Löschen verweigert", f"#{op_id} {exc}")
        raise FileOpRefused(str(exc), op_id) from None
    name = os.path.basename(row.location)
    blockers = await _blockers(jref, row, allow_last_copy)
    if blockers:
        problem = "; ".join(blockers)
        op_id = await _refused(jref, "trash", actor, row.kind, row.location, row.id, problem, reason)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Löschen verweigert: {name}", f"#{op_id} {problem}")
        raise FileOpRefused(problem, op_id)

    async with async_session() as session:
        base = await base_path_for(session, jref)
    trash_dir = os.path.join(base, TRASH_DIRNAME, datetime.now().strftime("%Y-%m-%d"),
                             sanitize_path_component(jref.debug_key))

    async def recheck() -> str | None:
        # The rules were checked before the intent existed; a reference added
        # in between must still stop the trash.
        async with async_session() as session:
            current = await session.get(FileCopy, row.id)
            if current is None or current.state != "present":
                return "Exemplar inzwischen nicht mehr vorhanden"
            refs = await _foreign_refs(session, current.location, jref.id)
            if refs:
                return f"wird verwendet von {', '.join(await _debug_keys(session, refs))}"
        return None

    plan = _Plan(
        jref=jref, op="trash", actor=actor, src_kind=row.kind, src=row.location, dst_kind="trash",
        dst_dir=trash_dir, filename=name, remove_source=True, merge_identical=False,
        expected_sha=row.sha256, copy_id=row.id, reason=reason, run_id=run_id,
        texts={"before": "Wird in den Papierkorb verschoben", "after": "Im Papierkorb",
               "verb": "Löschen"})
    return await _run(plan, recheck=recheck)


async def restore(copy_id: int | None = None, *, op_id: int | None = None,
                  dst_dir: str | None = None, actor: str = "user:restore") -> OpResult:
    """Bring a trashed copy back: to its old place, or a free name next to it.

    Takes the copy id or the id of the trash operation. ``dst_dir`` restores
    somewhere else (e.g. not back into an inbox, which would re-import it).
    An Immich copy is restored in Immich (restore_asset).
    """
    async with async_session() as session:
        if copy_id is None:
            entry = await session.get(FileOp, op_id) if op_id is not None else None
            if entry is None or entry.op != "trash":
                raise FileOpRefused(f"#{op_id} ist keine Papierkorb-Operation")
            copy_id = entry.dst_copy_id or entry.copy_id
        row = await session.get(FileCopy, copy_id)
    if row is not None and row.kind == "immich":
        return await restore_asset(row.id, actor=actor)
    if row is None or row.state != "trashed" or not row.trash_location:
        raise FileOpRefused(f"Exemplar #{copy_id} liegt nicht im Papierkorb")
    jref = await _job_ref(row.job_id)
    plan = _Plan(
        jref=jref, op="restore", actor=actor, src_kind="trash", src=row.trash_location,
        dst_kind=row.kind, dst_dir=_norm(dst_dir) if dst_dir else os.path.dirname(row.location),
        filename=os.path.basename(row.location), remove_source=True, merge_identical=True,
        expected_sha=row.sha256, copy_id=row.id,
        texts={"before": "Wird aus dem Papierkorb wiederhergestellt", "after": "Wiederhergestellt",
               "verb": "Wiederherstellen"})
    result = await _run(plan)
    await asyncio.to_thread(_prune_trash_dirs, row.trash_location)
    return result


async def _remove_registered(jref: _JobRef, row: FileCopy, path: str, *, src_kind: str,
                             actor: str, reason: str, check_size: bool) -> int:
    """Permanently remove one file with a ledger row (op purge)."""
    name = os.path.basename(path)
    op_id, _ = await _begin(jref, op="purge", actor=actor, src_kind=src_kind, src=path,
                            copy_id=row.id, sha256_before=row.sha256, size=row.size, reason=reason)
    await log_info(LOG_SOURCE, f"{jref.debug_key} Wird endgültig gelöscht: {name}",
                   f"#{op_id} {path} ({reason})")
    try:
        await asyncio.to_thread(_remove_blocking, path, row.size if check_size else None)
    except Exception as exc:
        error = _describe(exc)
        missing = isinstance(exc, FileNotFoundError)
        await _fail(op_id, error)
        if missing:
            async with async_session() as session:
                current = await session.get(FileCopy, row.id)
                current.state = "missing"
                await session.commit()
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Endgültig löschen fehlgeschlagen: {name}",
                          f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    async with async_session() as session:
        current = await session.get(FileCopy, row.id)
        current.state = "deleted"
        await _finish(session, op_id, "done")
        await session.commit()
    await log_info(LOG_SOURCE, f"{jref.debug_key} Endgültig gelöscht: {name}", f"#{op_id} {path}")
    return op_id


async def purge_trash(older_than_days: int = TRASH_RETENTION_DAYS, *,
                      actor: str = "system:papierkorb", immich: bool = True) -> dict:
    """Empty the local trash of entries older than the retention period.

    Immich empties its own trash (nightly, after its own retention); MA
    never force-deletes there. With ``immich`` every Immich copy the register
    lists as trashed is checked, and one that is gone is recorded as
    'deleted' with a ledger row (see _verify_immich).
    """
    counts = await _purge_local(older_than_days, actor)
    if immich:
        counts.update(await _sweep_immich_trash(actor))
    return counts


async def _sweep_immich_trash(actor: str) -> dict:
    async with async_session() as session:
        rows = (await session.execute(select(FileCopy).where(
            FileCopy.kind == "immich", FileCopy.state == "trashed").order_by(FileCopy.id)
        )).scalars().all()
    counts = {"immich_checked": 0, "immich_gone": 0, "immich_unchecked": 0}
    failures_in_row = 0
    for i, row in enumerate(rows):
        try:
            checked = await _verify_immich(row, actor)
        except Exception as exc:
            counts["immich_unchecked"] += 1
            failures_in_row += 1
            logger.info("Immich trash check of #%s failed: %s", row.id, exc)
            if failures_in_row >= 3:  # Immich is down; try again tomorrow
                counts["immich_unchecked"] += len(rows) - i - 1
                break
            continue
        failures_in_row = 0
        counts["immich_checked"] += 1
        if checked.state == "deleted":
            counts["immich_gone"] += 1
    return counts


async def _purge_local(older_than_days: int, actor: str) -> dict:
    cutoff = datetime.now() - timedelta(days=older_than_days)
    async with async_session() as session:
        rows = (await session.execute(select(FileCopy).where(
            FileCopy.state == "trashed", FileCopy.trash_location.isnot(None),
            FileCopy.kind != "immich", FileCopy.trashed_at < cutoff,
        ).order_by(FileCopy.id))).scalars().all()
    counts = {"purged": 0, "failed": 0, "busy": 0}
    for row in rows:
        jref = await _job_ref(row.job_id)
        try:
            await _remove_registered(jref, row, row.trash_location, src_kind="trash", actor=actor,
                                     reason=f"Aufbewahrung von {older_than_days} Tagen abgelaufen",
                                     check_size=True)
            counts["purged"] += 1
            await asyncio.to_thread(_prune_trash_dirs, row.trash_location)
        except FileOpBusy:
            counts["busy"] += 1
        except FileOpError:
            counts["failed"] += 1
    return counts


async def discard(job, target, *, actor: str, reason: str) -> int:
    """Remove a derived or work file for good, without the trash.

    Kind 'temp' (e.g. the IA-04 JPEG) always. Kind 'work' (a download into
    reprocess/ or a poller temp dir) only while another copy of the same
    content verifies right now — typically the Immich asset it came from or
    was uploaded to — and no other job uses it. Everything else, and a work
    copy that is the last one, goes through trash().
    """
    jref = await _job_ref(job)
    row = await _resolve_copy(jref, target, None, register=False)
    problem = None
    if row.kind not in ("temp", "work"):
        problem = f"Nur temporäre Dateien und Arbeitskopien werden direkt gelöscht, nicht {row.kind}"
    elif row.job_id != jref.id:
        problem = "Datei gehört einem anderen Job"
    elif row.kind == "work":
        async with async_session() as session:
            refs = await _foreign_refs(session, row.location, jref.id)
            users = ", ".join(await _debug_keys(session, refs)) if refs else ""
        if refs:
            problem = f"wird verwendet von {users}"
        elif await _verified_twin(row) is None:
            problem = "Arbeitskopie ist das letzte geprüfte Exemplar dieses Inhalts — trash() verwenden"
    if problem:
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Direktes Löschen verweigert: "
                                      f"{os.path.basename(row.location)}", problem)
        raise FileOpRefused(problem)
    return await _remove_registered(jref, row, row.location, src_kind=row.kind, actor=actor,
                                    reason=reason, check_size=False)


# ---------------------------------------------------------------------------
# Public API: queries and references
# ---------------------------------------------------------------------------

async def copies(job, states=("present",)) -> list[FileCopy]:
    """Copies a job owns, in the given states."""
    jref = await _job_ref(job)
    async with async_session() as session:
        return list((await session.execute(select(FileCopy).where(
            FileCopy.job_id == jref.id, FileCopy.state.in_(states)).order_by(FileCopy.id)
        )).scalars().all())


async def add_ref(job, kind: str, location: str) -> None:
    """Record that `job` uses a copy it does not own (e.g. a shared Immich asset)."""
    jref = await _job_ref(job)
    location = location if kind == "immich" else _norm(location)
    async with async_session() as session:
        await _add_ref_in(session, kind, location, jref.id)
        await session.commit()


async def drop_ref(job, kind: str, location: str) -> None:
    jref = await _job_ref(job)
    location = location if kind == "immich" else _norm(location)
    async with async_session() as session:
        for ref in (await session.execute(select(AssetRef).where(
                AssetRef.kind == kind, AssetRef.location == location,
                AssetRef.job_id == jref.id))).scalars().all():
            await session.delete(ref)
        await session.commit()


# ---------------------------------------------------------------------------
# Immich. The same guarantees as for local copies: an intent row before the
# call to Immich, exactly one end state, register and references updated in
# the transaction that ends the row, German system logs before and after.
# What these functions rely on is measured on Immich 3.1 in
# docs/IMMICH_3_VERHALTEN.md.
#
# States of a copy of kind 'immich': present; trashed (in Immich's trash);
# locked (moved to the locked folder — the API key can no longer see it,
# which is not a loss); missing (not visible, reason unknown); deleted (gone
# from Immich's trash: Immich emptied it); moved (another row took over).
# MediaAssistant never force-deletes an asset.
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_ASSET_STATE_ORDER = ("present", "locked", "trashed", "missing", "deleted", "moved")
_TAG_WRITE_DELAY_S = 0.1  # Immich drops a tag write that follows another on the same asset too closely (IA-08)
_VERIFY_RETRY_S = (1, 3)  # the check right after an upload is retried on transient errors


def _ic():
    import immich_client  # late: keeps httpx and the client out of fileops' import
    return immich_client


def _is_asset_id(value) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _tag_value(name: str) -> str:
    """The value Immich stores for a tag that tag_asset() created from `name`."""
    return name.replace("/", " - ")


def _hash_for_upload(path: str) -> tuple[str, str, int]:
    """sha256 (hex), Immich's checksum (SHA-1, base64) and size, in one read."""
    h256, h1 = hashlib.sha256(), hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h256.update(chunk)
            h1.update(chunk)
        size = os.fstat(f.fileno()).st_size
    return h256.hexdigest(), base64.b64encode(h1.digest()).decode("ascii"), size


def _created_since(info: dict | None, since: datetime | None) -> bool:
    """Whether Immich created the asset after `since` (naive local time)."""
    raw = (info or {}).get("createdAt")
    if not raw or since is None:
        return False
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return created >= since.astimezone(timezone.utc) - timedelta(seconds=5)


async def _immich_key(jref: _JobRef, override: str | None = None) -> str | None:
    """API key for a job's assets: the override, the key of the job's Immich
    user, or None (immich_client then uses the global key)."""
    if override:
        return override
    if jref.immich_user_id:
        key = await _ic().get_user_api_key(jref.immich_user_id)
        if not key:
            # The global key sees another account and would report the
            # job's assets as missing.
            raise FileOpRefused(f"Immich-Zugang #{jref.immich_user_id} des Jobs ist nicht mehr hinterlegt")
        return key
    return None


async def _asset_live(asset_id: str, key: str | None, retries=()) -> tuple[str, dict | None]:
    """(state, asset json) as Immich sees it now; the json only while the
    asset is visible. Raises when Immich cannot tell (timeout, 5xx, …)."""
    ic = _ic()
    for delay in (*retries, None):
        try:
            state = await ic.get_asset_state(asset_id, api_key=key)
            info = None
            if state != ic.ASSET_MISSING:
                info, why = await ic.fetch_asset_info(asset_id, api_key=key)
                if info is None:
                    raise RuntimeError(f"Asset {asset_id} nicht lesbar: {why}")
            return state, info
        except Exception:
            if delay is None:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _asset_row(session, asset_id: str) -> FileCopy | None:
    """The register row that speaks for an asset: present before locked
    before trashed …, the newest of each."""
    rows = (await session.execute(select(FileCopy).where(
        FileCopy.kind == "immich", FileCopy.location == asset_id).order_by(FileCopy.id.desc())
    )).scalars().all()
    for state in _ASSET_STATE_ORDER:
        for row in rows:
            if row.state == state:
                return row
    return rows[0] if rows else None


async def _legacy_users(session, asset_id: str) -> list[int]:
    """Jobs pointing at an asset through jobs.immich_asset_id or an immich:
    target — the only record of sharing from before the register, until the
    migration builds asset_refs from them (live: 3,840 shared assets)."""
    return sorted(set((await session.execute(select(Job.id).where(or_(
        Job.immich_asset_id == asset_id, Job.target_path == f"immich:{asset_id}"))
    )).scalars().all()))


async def _adopt_legacy_refs(session, row: FileCopy, job_id, known: list[int]) -> list[int]:
    """Add references for jobs that use the asset only through the old job
    fields; returns their ids. Only ever adds protection, never removes it."""
    added = []
    for user in await _legacy_users(session, row.location):
        if user in (job_id, row.job_id) or user in known:
            continue
        await _add_ref_in(session, "immich", row.location, user)
        added.append(user)
    return added


async def _resolve_asset(jref: _JobRef, target) -> FileCopy:
    """The register row of an Immich asset (FileCopy, its id, or the asset id).

    An asset id the register does not know yet is registered, unverified,
    from the jobs that point at it: the calling job owns it if it is one of
    them, otherwise the first of them, and the others get references. An
    asset no job points at belongs to no job, so MediaAssistant never trashes
    or replaces it.
    """
    if isinstance(target, (FileCopy, int)) and not isinstance(target, bool):
        copy_id = target.id if isinstance(target, FileCopy) else target
        async with async_session() as session:
            row = await session.get(FileCopy, copy_id)
        if row is None or row.kind != "immich":
            raise FileOpRefused(f"Exemplar #{copy_id} ist kein Immich-Asset")
        return row
    if not _is_asset_id(target):
        raise FileOpRefused(f"{target!r} ist keine Immich-Asset-ID")
    for _attempt in range(2):
        async with async_session() as session:
            row = await _asset_row(session, target)
            if row is not None:
                return row
            users = await _legacy_users(session, target)
            owner = jref.id if jref.id in users else (users[0] if users else None)
            row = FileCopy(job_id=owner, kind="immich", location=target, state="present")
            session.add(row)
            try:
                await session.flush()
                for user in users:
                    if user != owner:
                        await _add_ref_in(session, "immich", target, user)
                owner_text = await _owner_label(session, owner)
                others = [u for u in users if u != owner]
                other_keys = ", ".join(await _debug_keys(session, others)) if others else "keine"
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue  # registered at the same moment elsewhere: read it again
        await log_info(LOG_SOURCE, f"{jref.debug_key} Immich-Asset nachregistriert: {target}",
                       f"Exemplar #{row.id}, gehört {owner_text}; Verweise: {other_keys}")
        return row
    raise FileOpBusy(f"Asset {target} wird gerade registriert")


async def _set_asset_state(row: FileCopy, state: str) -> FileCopy:
    """Bring the register in line with a state just read from Immich."""
    async with async_session() as session:
        cur = await session.get(FileCopy, row.id)
        if cur.state == state:
            return cur
        if state == "present":
            other = await _present_at(session, cur.location)
            if other is not None:
                return other
            cur.trashed_at = None
        elif state == "trashed":
            cur.trashed_at = cur.trashed_at or datetime.now()
        before, cur.state = cur.state, state
        await session.commit()
    jref = await _job_ref(cur.job_id)
    await log_warning(LOG_SOURCE, f"{jref.debug_key} Register korrigiert",
                      f"Exemplar #{cur.id} Asset {cur.location}: {before} → {state} (laut Immich)")
    return cur


async def _locked_by_ma(session, row: FileCopy) -> bool:
    """IA-08 locked this asset (NSFW) before locks went through lock_asset()."""
    if row.job_id is None:
        return False
    result = (await session.execute(select(Job.step_result).where(Job.id == row.job_id))).scalar()
    ia08 = result.get("IA-08") if isinstance(result, dict) else None
    if not isinstance(ia08, dict) or not ia08.get("immich_locked"):
        return False
    return row.location in (ia08.get("immich_asset_id"), ia08.get("immich_id"))


async def _adopt_asset(session, jref: _JobRef, asset_id: str, sha: str | None, size: int | None,
                       checksum: str | None, *, created: bool, notes: list) -> tuple[FileCopy, bool]:
    """Register a verified asset after an upload; returns (row, owned).

    An asset the upload created belongs to the job. A duplicate answer means
    Immich already had the content: the job owns the asset only if the
    register or its own job fields say so; otherwise it gets a reference.
    """
    row = await _asset_row(session, asset_id)  # a present row comes first
    if row is not None and row.state == "moved":
        row = None
    elif row is not None and row.state != "present":
        notes.append(f"Exemplar #{row.id} war '{row.state}', ist wieder vorhanden")
        row.state, row.trashed_at = "present", None
    if row is None:
        users = await _legacy_users(session, asset_id)
        others = [u for u in users if u != jref.id]
        owner = jref.id if created or jref.id in users else (others[0] if others else None)
        row = FileCopy(job_id=owner, kind="immich", location=asset_id, state="present")
        session.add(row)
        await session.flush()
        for user in others:
            if user != owner:
                await _add_ref_in(session, "immich", asset_id, user)
        if owner is None:
            notes.append("Immich kannte den Inhalt schon (Asset nicht von MediaAssistant angelegt)")
    elif created and row.job_id is None:
        row.job_id = jref.id  # this very upload created it
    if row.sha256 and sha and row.sha256 != sha:
        notes.append(f"Hash von Exemplar #{row.id} korrigiert")
    row.sha256 = sha or row.sha256
    row.size = size if size is not None else row.size
    row.checksum = checksum or row.checksum
    row.verified_at = datetime.now()
    owned = row.job_id == jref.id
    if not owned:
        await _add_ref_in(session, "immich", asset_id, jref.id)
        notes.append(f"Asset gehört {await _owner_label(session, row.job_id)}; Verweis eingetragen")
    await session.flush()
    return row, owned


async def _note(op_id: int, *, detail: dict | None = None, **values) -> None:
    """Fill in what an open operation learned on the way (asset id, checksum)."""
    async with async_session() as session:
        entry = await session.get(FileOp, op_id)
        if entry is None or entry.state != "intent":
            raise FileOpError(f"Protokollzeile #{op_id} ist nicht mehr offen", op_id)
        if detail:
            entry.detail = dict(entry.detail or {}, **detail)
        for name, value in values.items():
            setattr(entry, name, value)
        await session.commit()


async def _close(op_id: int, state: str, *, detail: dict | None = None, error: str | None = None,
                 **values) -> None:
    """End an operation; `detail` is merged into what it recorded."""
    async with async_session() as session:
        entry = await session.get(FileOp, op_id)
        merged = dict(entry.detail or {}, **(detail or {}))
        await _finish(session, op_id, state, detail=merged, error=error, **values)
        await session.commit()


async def _observed(jref: _JobRef, op: str, actor: str, src: str, copy_id: int | None, note: str, *,
                    dst_kind: str | None = None, reason: str | None = None) -> int:
    """Record that the wanted state was already there: a ledger row that
    was never an intent, because nothing had to be done."""
    async with async_session() as session:
        entry = FileOp(job_id=jref.id, op=op, src_kind="immich", src=src, dst_kind=dst_kind, dst=src,
                       copy_id=copy_id, dst_copy_id=copy_id, actor=actor, reason=reason, state="done",
                       finished_at=datetime.now(), detail={"observed": note})
        session.add(entry)
        await session.commit()
        return entry.id


async def _upload_source(jref: _JobRef, src, expected_sha256: str | None, actor: str, op: str) -> FileCopy:
    """The local copy to upload: the job's own, or one it references (R-01)."""
    try:
        row = await _resolve_copy(jref, src, expected_sha256, register=True)
        problem = None
        if row.kind == "immich":
            problem = "Quelle ist ein Immich-Asset, keine Datei"
        elif expected_sha256 and row.sha256 and row.sha256 != expected_sha256:
            problem = f"{row.location} hat anderen Inhalt als erwartet — fremde Datei?"
        elif row.job_id != jref.id:
            async with async_session() as session:
                ref = (await session.execute(select(AssetRef.id).where(
                    AssetRef.location == row.location, AssetRef.job_id == jref.id))).first()
                if ref is None:
                    problem = f"Exemplar gehört {await _owner_label(session, row.job_id)}"
        if problem:
            raise FileOpRefused(problem)
    except FileOpRefused as exc:
        where = src if isinstance(src, str) else None
        op_id = await _refused(jref, op, actor, "local", where, None, str(exc))
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Upload verweigert", f"#{op_id} {exc}")
        raise FileOpRefused(str(exc), op_id) from None
    return row


async def upload(job, src, *, actor: str, albums=None, sidecar: str | None = None,
                 api_key: str | None = None, restore_trashed: bool = False,
                 expected_sha256: str | None = None, run_id: int | None = None) -> OpResult:
    """Upload a local copy to Immich, check it there and register the asset.

    `src` is a FileCopy, its id, or a path (an unregistered path is hashed and
    registered for `job` and must match ``expected_sha256`` if given). It has
    to be the job's own copy or one it references. The source is never
    removed here: after success the caller trashes it, or discards a work
    copy — the verified asset is the second copy that allows that.

    Success means the asset is present in Immich with the source's checksum
    and size. Immich's answer is taken as follows:

    - ``created``: the job owns the new asset.
    - ``duplicate`` of a present asset: Immich already had the content. If
      the register (or the old job fields) names another owner, the job only
      gets a reference and ``owned`` is False (R-08).
    - ``duplicate`` of an asset in Immich's trash: no success (R-22) — the
      only copy would go with the trash. With ``restore_trashed`` it is
      restored first (``immich_status`` 'restored'), otherwise FileOpFailed
      and the source stays.
    - ``duplicate`` of an asset the key cannot see (locked folder, other
      account): FileOpFailed, the source stays.

    Albums are added after the check. The API key is that of the job's Immich
    user unless ``api_key`` overrides it.
    """
    jref = await _job_ref(job)
    key = await _immich_key(jref, api_key)
    row = await _upload_source(jref, src, expected_sha256, actor, "upload")
    return await _upload_row(jref, row, key, actor=actor, albums=albums, sidecar=sidecar,
                             restore_trashed=restore_trashed, run_id=run_id)


async def _upload_row(jref: _JobRef, row: FileCopy, key: str | None, *, actor: str, albums,
                      sidecar: str | None, restore_trashed: bool, run_id: int | None,
                      parent_op: int | None = None) -> OpResult:
    ic = _ic()
    k = jref.debug_key
    name = os.path.basename(row.location)
    albums = [a for a in (albums or []) if a]
    detail = {"albums": albums, "sidecar": sidecar, "immich_user_id": jref.immich_user_id}
    if parent_op:
        detail["parent_op"] = parent_op
    op_id, _ = await _begin(jref, op="upload", actor=actor, src_kind=row.kind, src=row.location,
                            dst_kind="immich", copy_id=row.id, sha256_before=row.sha256,
                            size=row.size, run_id=run_id, detail=detail)
    await log_info(LOG_SOURCE, f"{k} Upload nach Immich beginnt: {name}", f"#{op_id} {row.location}")
    _crashpoint("intent")

    async def failed(error: str) -> FileOpFailed:
        await _close(op_id, "failed", error=error)
        await log_warning(LOG_SOURCE, f"{k} Upload fehlgeschlagen: {name}", f"#{op_id} {error}")
        return FileOpFailed(error, op_id)

    try:
        sha, checksum, size = await asyncio.to_thread(_hash_for_upload, row.location)
        if row.sha256 and sha != row.sha256:
            raise _ContentMismatch(
                f"Inhalt der Quelle weicht vom Register ab (SHA256 {sha[:16]}… statt "
                f"{row.sha256[:16]}…) — fremde Datei am Ort?")
        # Recorded before the request: after a crash the asset is found by it.
        await _note(op_id, sha256_before=sha, size=size, detail={"checksum": checksum})
        resp = await ic.upload_asset(row.location, None, sidecar_path=sidecar, api_key=key)
    except Exception as exc:
        err = await failed(_describe(exc))
        if isinstance(exc, (_ContentMismatch, FileNotFoundError)):
            await verify(row.id)  # marks the registered copy missing
        raise err from exc

    asset_id = resp.get("id")
    status = str(resp.get("status") or "created").lower()
    _crashpoint("uploaded")
    if not asset_id:
        raise await failed(f"Immich lieferte keine Asset-ID: {str(resp)[:200]}")
    await _note(op_id, dst_kind="immich", dst=asset_id, detail={"status": status})
    try:
        state, info = await _asset_live(asset_id, key, retries=_VERIFY_RETRY_S)
    except Exception as exc:
        error = f"Prüfung in Immich nicht möglich: {_describe(exc)}"
        if status == "created":
            # Ours for sure: registered unchecked so it is not lost track of.
            async with async_session() as session:
                created_row = await _claim(session, jref.id, "immich", asset_id, sha, size, [],
                                           verified=False)
                created_row.checksum = checksum
                await session.commit()
            error += f" — Asset {asset_id} ungeprüft registriert"
        raise await failed(error) from exc

    actual = (info or {}).get("checksum")
    actual_size = ((info or {}).get("exifInfo") or {}).get("fileSizeInByte")
    same = actual == checksum and (not actual_size or actual_size == size)
    notes: list[str] = []

    if status == "created" and not (state == ic.ASSET_PRESENT and same):
        if state != ic.ASSET_PRESENT:
            raise await failed(f"Asset {asset_id} ist nach dem Upload nicht vorhanden (Zustand {state})")
        # Immich holds other bytes than the source: a damaged upload of ours.
        await log_info(LOG_SOURCE, f"{k} Fehlerhaftes Asset wird in den Immich-Papierkorb verschoben: {asset_id}",
                       f"#{op_id} Prüfsumme {actual} statt {checksum}, Grösse {actual_size} statt {size}")
        try:
            await ic.trash_asset(asset_id, api_key=key)
            async with async_session() as session:
                session.add(FileCopy(job_id=jref.id, kind="immich", location=asset_id, size=actual_size,
                                     checksum=actual, state="trashed", trashed_at=datetime.now()))
                await session.commit()
            await log_info(LOG_SOURCE, f"{k} Fehlerhaftes Asset im Immich-Papierkorb: {asset_id}", f"#{op_id}")
        except Exception as exc:
            await log_warning(LOG_SOURCE, f"{k} Fehlerhaftes Asset nicht in den Papierkorb verschoben: {asset_id}",
                              f"#{op_id} {_describe(exc)}")
        raise await failed(f"Asset {asset_id} weicht von der Quelle ab (Prüfsumme/Grösse) — "
                           "nicht als hochgeladen gewertet")

    if status != "created":
        if state == ic.ASSET_TRASHED and same and restore_trashed:
            await log_info(LOG_SOURCE, f"{k} Asset {asset_id} mit gleichem Inhalt liegt im Immich-Papierkorb "
                                       f"— wird wiederhergestellt", f"#{op_id} {name}")
            try:
                await ic.restore_asset(asset_id, api_key=key)
            except Exception as exc:
                raise await failed(f"Wiederherstellen von Asset {asset_id} aus dem Immich-Papierkorb "
                                   f"fehlgeschlagen: {_describe(exc)}") from exc
            await log_info(LOG_SOURCE, f"{k} Asset {asset_id} aus dem Immich-Papierkorb wiederhergestellt",
                           f"#{op_id}")
            status, state = "restored", ic.ASSET_PRESENT
            notes.append("aus dem Immich-Papierkorb wiederhergestellt")
        elif state == ic.ASSET_TRASHED:
            async with async_session() as session:
                known = await _asset_row(session, asset_id)
            if known is not None and known.state == "present":
                await _set_asset_state(known, "trashed")
            raise await failed(
                f"Immich kennt diesen Inhalt als Asset {asset_id}, das im Papierkorb liegt — nicht als "
                "hochgeladen gewertet, die Quelle bleibt erhalten (Immich löscht das Asset mit dem "
                "Papierkorb endgültig). Asset in Immich wiederherstellen oder den Papierkorb leeren, "
                "dann erneut versuchen.")
        elif state == ic.ASSET_MISSING:
            async with async_session() as session:
                known = await _asset_row(session, asset_id)
            why = ("im gesperrten Ordner (Locked Folder)" if known is not None and known.state == "locked"
                   else "für diesen Zugang nicht sichtbar (gesperrt, gelöscht oder in einem anderen Konto)")
            raise await failed(f"Immich meldet ein Duplikat von Asset {asset_id}, das {why} ist — nicht als "
                               "hochgeladen gewertet, die Quelle bleibt erhalten")
        elif not same:
            raise await failed(f"Immich meldet ein Duplikat von Asset {asset_id}, aber Prüfsumme oder "
                               "Grösse weichen von der Quelle ab")

    albums_added: list[str] = []
    albums_created: list[str] = []
    if albums:
        try:
            albums_added = await ic.add_asset_to_albums(asset_id, albums, api_key=key, created=albums_created)
        except Exception as exc:
            logger.warning("albums for %s: %s", asset_id, exc)
        missing_albums = [a for a in albums if a not in albums_added]
        if missing_albums:
            notes.append(f"Alben nicht gesetzt: {', '.join(missing_albums)}")

    for attempt in range(2):
        adopt_notes = list(notes)
        try:
            async with async_session() as session:
                copy_row, owned = await _adopt_asset(session, jref, asset_id, sha, size, checksum,
                                                     created=status == "created", notes=adopt_notes)
                entry = await session.get(FileOp, op_id)
                new_detail = dict(entry.detail or {}, status=status, owned=owned, albums_added=albums_added)
                if albums_created:
                    new_detail["albums_created"] = albums_created
                if adopt_notes:
                    new_detail["notes"] = adopt_notes
                await _finish(session, op_id, "done", dst_kind="immich", dst=asset_id,
                              dst_copy_id=copy_row.id, sha256_after=sha, size=size, detail=new_detail)
                await session.commit()
            break
        except IntegrityError as exc:
            if attempt:
                raise await failed(f"Register-Eintrag für Asset {asset_id} fehlgeschlagen: {exc}") from exc
    notes = adopt_notes

    how = {"created": "neu angelegt", "duplicate": "Inhalt war schon in Immich",
           "restored": "aus dem Papierkorb wiederhergestellt"}.get(status, status)
    shared = "" if owned else "; gehört einem anderen Job, Verweis eingetragen"
    await log_info(LOG_SOURCE, f"{k} In Immich hochgeladen: {name}",
                   f"#{op_id} → Asset {asset_id} ({how}; Prüfsumme und Grösse geprüft{shared})")
    return OpResult(op_id, copy_row.id, asset_id, sha, size, identical=status != "created",
                    asset_id=asset_id, immich_status=status, owned=owned, albums_added=albums_added,
                    albums_created=albums_created, notes=notes)


async def _apply_tags(asset_id: str, names: list[str], carry: list[dict], key: str | None,
                      notes: list) -> tuple[list[str], list[str]]:
    """Tag an asset: carried tags by id (hierarchical tags stay intact), MA's
    tags by name (created if needed); then one check and one retry of the
    writes Immich lost. Returns (written, failed): MA's tags by the names
    given, carried tags by their Immich value."""
    if not names and not carry:
        return [], []
    ic = _ic()

    async def write(tag_names, tags):
        for tag in tags:
            try:
                await ic.tag_assets(tag["id"], [asset_id], api_key=key)
            except Exception as exc:
                logger.warning("tag %s on %s: %s", tag.get("value"), asset_id, exc)
            await asyncio.sleep(_TAG_WRITE_DELAY_S)
        for tag_name in tag_names:
            try:
                await ic.tag_asset(asset_id, tag_name, api_key=key)
            except Exception as exc:
                logger.warning("tag %s on %s: %s", tag_name, asset_id, exc)
            await asyncio.sleep(_TAG_WRITE_DELAY_S)

    await write(names, carry)
    have: set = set()
    for attempt in range(2):
        info, _why = await ic.fetch_asset_info(asset_id, api_key=key)
        have = {t.get("value") for t in ((info or {}).get("tags") or []) if isinstance(t, dict)}
        lost_names = [n for n in names if _tag_value(n) not in have]
        lost_carry = [t for t in carry if t.get("value") not in have]
        if attempt or not (lost_names or lost_carry):
            break
        await write(lost_names, lost_carry)
    wanted = {n: _tag_value(n) for n in names}
    wanted.update({t.get("value"): t.get("value") for t in carry})
    written = [label for label, value in wanted.items() if value in have]
    failed = [label for label, value in wanted.items() if value not in have]
    if failed:
        notes.append(f"Tags nicht gesetzt: {', '.join(str(v) for v in failed)}")
    return written, failed


async def replace(job, old, new_src, *, actor: str, tags=None, description: str | None = None,
                  drop_tags=(), albums=None, sidecar: str | None = None, api_key: str | None = None,
                  restore_trashed: bool = False, expected_sha256: str | None = None,
                  run_id: int | None = None) -> OpResult:
    """Replace an Immich asset by a new version of the content (S7, R-18, R-08).

    1. The new content is uploaded and checked exactly as by upload(); until
       then the old asset is untouched, and on failure nothing else happens.
    2. PUT /assets/copy old → new with ``sidecar: false``: albums, favorite,
       shared links and stack move over, the new asset keeps its own XMP.
       Immich's default ``sidecar: true`` would replace the new asset's tags,
       description, rating and date by the old ones (R-18).
    3. What /assets/copy does not carry is set here: the old asset's tags (by
       id) except ``drop_tags`` (tags MA wrote earlier and no longer wants),
       MA's ``tags``, the ``description`` (None keeps the old asset's) and
       the old rating.
    4. The old asset goes to Immich's trash, never force-deleted — only if
       the job owns it, no other job references it and step 2 succeeded
       (otherwise albums, favorite, shared links and stack would exist only
       on the trashed asset, R-18a); else it stays and ``old_kept`` says
       why. The verified new version is the second copy that allows the
       trash.

    `old` is a FileCopy, its id or the asset id; `new_src` as for upload().
    If Immich answers that the new content is the old asset (same checksum),
    nothing is replaced; tags and description are still applied. The caller
    then points the job at ``result.asset_id``.
    """
    ic = _ic()
    jref = await _job_ref(job)
    k = jref.debug_key
    try:
        old_row = await _resolve_asset(jref, old)
        key = await _immich_key(jref, api_key)
    except FileOpRefused as exc:
        op_id = await _refused(jref, "replace", actor, "immich", old if isinstance(old, str) else None,
                               None, str(exc))
        await log_warning(LOG_SOURCE, f"{k} Ersetzen verweigert", f"#{op_id} {exc}")
        raise FileOpRefused(str(exc), op_id) from None
    old_id = old_row.location
    src_row = await _upload_source(jref, new_src, expected_sha256, actor, "replace")
    name = os.path.basename(src_row.location)
    try:
        old_state, old_info = await _asset_live(old_id, key)
    except Exception as exc:
        problem = f"Zustand des alten Assets {old_id} unbekannt: {_describe(exc)}"
        op_id = await _refused(jref, "replace", actor, src_row.kind, src_row.location, old_row.id, problem)
        await log_warning(LOG_SOURCE, f"{k} Ersetzen verweigert: {name}", f"#{op_id} {problem}")
        raise FileOpFailed(problem, op_id) from exc

    op_id, _ = await _begin(jref, op="replace", actor=actor, src_kind=src_row.kind, src=src_row.location,
                            dst_kind="immich", copy_id=old_row.id, run_id=run_id,
                            detail={"old_asset": old_id, "old_state": old_state,
                                    "immich_user_id": jref.immich_user_id})
    await log_info(LOG_SOURCE, f"{k} Ersetzen in Immich beginnt: {name}",
                   f"#{op_id} altes Asset {old_id} ({old_state})")
    _crashpoint("intent")
    try:
        up = await _upload_row(jref, src_row, key, actor=actor, albums=albums, sidecar=sidecar,
                               restore_trashed=restore_trashed, run_id=run_id, parent_op=op_id)
    except FileOpError as exc:
        error = f"neue Version nicht hochgeladen, altes Asset unverändert: {exc}"
        await _close(op_id, "failed", error=error, detail={"upload_op": exc.op_id})
        await log_warning(LOG_SOURCE, f"{k} Ersetzen fehlgeschlagen: {name}", f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    new_id = up.asset_id
    await _note(op_id, dst=new_id, detail={"upload_op": up.op_id})
    _crashpoint("replace_uploaded")

    notes = list(up.notes)
    exif = (old_info or {}).get("exifInfo") or {}
    carry, desc, rating = [], description, None
    links_error = None
    if new_id == old_id:
        notes.append("gleicher Inhalt wie das alte Asset — es bleibt, Tags und Beschreibung werden gesetzt")
    else:
        if old_state != ic.ASSET_MISSING:
            try:
                await ic.copy_asset_links(old_id, new_id, sidecar=False, api_key=key)
            except Exception as exc:
                links_error = _describe(exc)
                notes.append(f"Alben/Favorit/Stack nicht übernommen: {links_error}")
        dropped = {_tag_value(t) for t in (drop_tags or ()) if t}
        carry = [t for t in ((old_info or {}).get("tags") or [])
                 if isinstance(t, dict) and t.get("id") and t.get("value") not in dropped]
        if desc is None:
            desc = exif.get("description") or None
        rating = exif.get("rating")
    written, tags_failed = await _apply_tags(new_id, [t for t in (tags or []) if t], carry, key, notes)
    if desc:
        try:
            await ic.update_asset_description(new_id, desc, api_key=key)
        except Exception as exc:
            notes.append(f"Beschreibung nicht gesetzt: {_describe(exc)}")
    if rating:
        try:
            await ic.set_asset_rating(new_id, rating, api_key=key)
        except Exception as exc:
            notes.append(f"Bewertung nicht übernommen: {_describe(exc)}")

    old_trashed, old_kept, trash_op = False, None, None
    if new_id != old_id:
        if old_state == ic.ASSET_PRESENT and links_error:
            # Albums, favorite, shared links and stack live only on the old
            # asset now: trashing it would lose them (R-18a). Both stay.
            old_kept = (f"Alben, Favorit, Shared Links und Stack nicht auf das neue Asset übertragen "
                        f"({links_error}) — altes Asset bleibt, damit nichts verloren geht")
            await log_warning(LOG_SOURCE, f"{k} Altes Asset bleibt in Immich: {old_id}",
                              f"#{op_id} neues Asset {new_id}; {old_kept}")
        elif old_state == ic.ASSET_PRESENT:
            try:
                current = await _set_asset_state(old_row, "present")
                trashed = await _trash_asset_row(jref, current, key, actor=actor,
                                                 reason=f"ersetzt durch Asset {new_id}",
                                                 allow_last_copy=True, run_id=run_id, parent_op=op_id)
                old_trashed, trash_op = True, trashed.op_id
            except FileOpError as exc:
                old_kept = str(exc)
        elif old_state == ic.ASSET_TRASHED:
            await _set_asset_state(old_row, "trashed")
            old_trashed = True
            notes.append("altes Asset lag bereits im Immich-Papierkorb")
        else:
            try:
                await verify(old_row)
            except FileOpError as exc:
                logger.info("verify of old asset %s: %s", old_id, exc)
            notes.append("altes Asset ist für diesen Zugang nicht mehr sichtbar")

    result_detail = {"tags_written": written, "tags_failed": tags_failed, "trash_op": trash_op,
                     "old_trashed": old_trashed}
    if old_kept:
        result_detail["old_kept"] = old_kept
    if notes:
        result_detail["notes"] = notes
    await _close(op_id, "done", dst_kind="immich", dst=new_id, dst_copy_id=up.copy_id,
                 sha256_after=up.sha256, size=up.size, detail=result_detail)
    if new_id == old_id:
        after = "gleiches Asset, Tags und Beschreibung gesetzt"
    elif old_trashed:
        after = "altes Asset im Immich-Papierkorb"
    else:
        after = f"altes Asset bleibt: {old_kept}" if old_kept else "altes Asset nicht mehr sichtbar"
    await log_info(LOG_SOURCE, f"{k} In Immich ersetzt: {name}", f"#{op_id} Asset {old_id} → {new_id}; {after}")
    return OpResult(op_id, up.copy_id, new_id, up.sha256, up.size, identical=new_id == old_id,
                    asset_id=new_id, immich_status=up.immich_status, owned=up.owned,
                    albums_added=up.albums_added, albums_created=up.albums_created,
                    tags_written=written, tags_failed=tags_failed,
                    old_asset_id=old_id, old_trashed=old_trashed, old_kept=old_kept, notes=notes)


async def _trash_asset_row(jref: _JobRef, row: FileCopy, key: str | None, *, actor: str, reason: str,
                           allow_last_copy: bool, run_id: int | None = None,
                           parent_op: int | None = None) -> OpResult:
    """Trash an asset the caller just saw present in Immich."""
    ic = _ic()
    k = jref.debug_key
    asset_id = row.location
    blockers = ([f"Exemplar #{row.id} ist nicht vorhanden (Zustand {row.state})"]
                if row.state != "present" else await _blockers(jref, row, allow_last_copy))
    if blockers:
        problem = "; ".join(blockers)
        op_id = await _refused(jref, "trash", actor, "immich", asset_id, row.id, problem, reason)
        await log_warning(LOG_SOURCE, f"{k} Immich-Papierkorb verweigert: Asset {asset_id}", f"#{op_id} {problem}")
        raise FileOpRefused(problem, op_id)
    detail = {"immich_user_id": jref.immich_user_id}
    if parent_op:
        detail["parent_op"] = parent_op
    op_id, _ = await _begin(jref, op="trash", actor=actor, src_kind="immich", src=asset_id,
                            dst_kind="immich_trash", copy_id=row.id, sha256_before=row.sha256,
                            size=row.size, reason=reason, run_id=run_id, detail=detail)
    await log_info(LOG_SOURCE, f"{k} Wird in den Immich-Papierkorb verschoben: Asset {asset_id}",
                   f"#{op_id} {reason}")
    _crashpoint("intent")
    # The rules were checked before the intent existed; a reference added
    # in between must still stop the trash.
    async with async_session() as session:
        refs = await _foreign_refs(session, asset_id, jref.id)
        refs += await _adopt_legacy_refs(session, row, jref.id, refs)
        users = ", ".join(await _debug_keys(session, refs)) if refs else ""
        await session.commit()
    if refs:
        problem = f"wird verwendet von {users}"
        await _close(op_id, "failed", error=problem)
        await log_warning(LOG_SOURCE, f"{k} Immich-Papierkorb verweigert: Asset {asset_id}", f"#{op_id} {problem}")
        raise FileOpRefused(problem, op_id)
    try:
        await ic.trash_asset(asset_id, api_key=key)
    except Exception as exc:
        error = _describe(exc)
        await _close(op_id, "failed", error=error)
        await log_warning(LOG_SOURCE, f"{k} Immich-Papierkorb fehlgeschlagen: Asset {asset_id}",
                          f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    _crashpoint("trashed")
    async with async_session() as session:
        current = await session.get(FileCopy, row.id)
        current.state, current.trashed_at = "trashed", datetime.now()
        await _finish(session, op_id, "done", dst_kind="immich_trash", dst=asset_id, dst_copy_id=row.id)
        await session.commit()
    await log_info(LOG_SOURCE, f"{k} Im Immich-Papierkorb: Asset {asset_id}",
                   f"#{op_id} wiederherstellbar, bis Immich seinen Papierkorb leert")
    return OpResult(op_id, row.id, asset_id, row.sha256, row.size, asset_id=asset_id)


async def trash_asset(job, target, *, actor: str, reason: str, allow_last_copy: bool = False,
                      api_key: str | None = None, run_id: int | None = None) -> OpResult:
    """Move an Immich asset into Immich's trash (DELETE with force=false).

    The rules of trash(): the job owns the asset; no other job references
    it (asset_refs, and until the migration also jobs.immich_asset_id /
    an immich: target); another copy of the content verifies right now —
    unless ``allow_last_copy`` (a delete the user asked for; Immich's trash,
    30 days by default, is then the fallback). `target` is a FileCopy, its
    id or the asset id. An asset already in the trash counts as done
    (``identical``); one the key cannot see (locked, gone) is refused.
    """
    ic = _ic()
    jref = await _job_ref(job)
    k = jref.debug_key

    async def refuse(problem: str, row: FileCopy | None = None, exc_type=FileOpRefused):
        where = row.location if row is not None else (target if isinstance(target, str) else None)
        op_id = await _refused(jref, "trash", actor, "immich", where, row.id if row else None, problem, reason)
        await log_warning(LOG_SOURCE, f"{k} Immich-Papierkorb verweigert" + (f": Asset {where}" if where else ""),
                          f"#{op_id} {problem}")
        return exc_type(problem, op_id)

    try:
        row = await _resolve_asset(jref, target)
        key = await _immich_key(jref, api_key)
    except FileOpRefused as exc:
        raise await refuse(str(exc)) from None
    try:
        state, _info = await _asset_live(row.location, key)
    except Exception as exc:
        raise await refuse(f"Zustand in Immich unbekannt: {_describe(exc)}", row, FileOpFailed) from exc
    if state == ic.ASSET_TRASHED:
        row = await _set_asset_state(row, "trashed")
        op_id = await _observed(jref, "trash", actor, row.location, row.id, "lag bereits im Immich-Papierkorb",
                                dst_kind="immich_trash", reason=reason)
        await log_info(LOG_SOURCE, f"{k} Asset liegt bereits im Immich-Papierkorb: {row.location}", f"#{op_id} {reason}")
        return OpResult(op_id, row.id, row.location, row.sha256, row.size, identical=True,
                        asset_id=row.location)
    if state == ic.ASSET_MISSING:
        try:
            checked = (await verify(row)).state
        except FileOpError:
            checked = row.state
        raise await refuse(f"Asset in Immich nicht sichtbar (Register: {checked})", row)
    row = await _set_asset_state(row, "present")
    return await _trash_asset_row(jref, row, key, actor=actor, reason=reason,
                                  allow_last_copy=allow_last_copy, run_id=run_id)


async def restore_asset(copy_id, *, actor: str = "user:restore", api_key: str | None = None) -> OpResult:
    """Bring an asset back from Immich's trash and check that it is back:
    Immich's answer counts the ids sent, not the assets restored. `copy_id`
    is the FileCopy or its id; restore() of an Immich copy lands here."""
    ic = _ic()
    copy_id = copy_id.id if isinstance(copy_id, FileCopy) else int(copy_id)
    async with async_session() as session:
        row = await session.get(FileCopy, copy_id)
    if row is None or row.kind != "immich":
        raise FileOpRefused(f"Exemplar #{copy_id} ist kein Immich-Asset")
    jref = await _job_ref(row.job_id)
    k = jref.debug_key
    key = await _immich_key(jref, api_key)
    asset_id = row.location
    try:
        state, _info = await _asset_live(asset_id, key)
    except Exception as exc:
        raise FileOpFailed(f"Zustand von Asset {asset_id} in Immich unbekannt: {_describe(exc)}") from exc
    if state == ic.ASSET_PRESENT:
        row = await _set_asset_state(row, "present")
        op_id = await _observed(jref, "restore", actor, asset_id, row.id, "war bereits wiederhergestellt",
                                dst_kind="immich")
        await log_info(LOG_SOURCE, f"{k} Asset ist bereits wiederhergestellt: {asset_id}", f"#{op_id}")
        return OpResult(op_id, row.id, asset_id, row.sha256, row.size, identical=True, asset_id=asset_id)
    if state == ic.ASSET_MISSING:
        try:
            checked = (await verify(row)).state
        except FileOpError:
            checked = row.state
        raise FileOpRefused(f"Asset {asset_id} liegt nicht mehr im Immich-Papierkorb (Register: {checked}) "
                            "— Immich hat es endgültig gelöscht, oder es ist gesperrt")
    row = await _set_asset_state(row, "trashed")
    op_id, _ = await _begin(jref, op="restore", actor=actor, src_kind="immich", src=asset_id,
                            dst_kind="immich", copy_id=row.id, sha256_before=row.sha256, size=row.size,
                            detail={"immich_user_id": jref.immich_user_id})
    await log_info(LOG_SOURCE, f"{k} Wird aus dem Immich-Papierkorb wiederhergestellt: Asset {asset_id}", f"#{op_id}")
    _crashpoint("intent")
    try:
        await ic.restore_asset(asset_id, api_key=key)
    except Exception as exc:
        error = _describe(exc)
        await _close(op_id, "failed", error=error)
        await log_warning(LOG_SOURCE, f"{k} Wiederherstellen aus dem Immich-Papierkorb fehlgeschlagen: "
                                      f"Asset {asset_id}", f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    notes = []
    async with async_session() as session:
        current = await session.get(FileCopy, row.id)
        other = await _present_at(session, asset_id)
        if other is not None and other.id != current.id:
            current.state = "moved"
            result_id = other.id
            notes.append(f"Exemplar #{other.id} führt das Asset bereits")
            if current.job_id is not None and current.job_id != other.job_id:
                await _add_ref_in(session, "immich", asset_id, current.job_id)
        else:
            current.state, current.trashed_at, current.verified_at = "present", None, datetime.now()
            result_id = current.id
        await _finish(session, op_id, "done", dst_kind="immich", dst=asset_id, dst_copy_id=result_id,
                      **({"detail": {"immich_user_id": jref.immich_user_id, "notes": notes}} if notes else {}))
        await session.commit()
    await log_info(LOG_SOURCE, f"{k} Aus dem Immich-Papierkorb wiederhergestellt: Asset {asset_id}", f"#{op_id}")
    return OpResult(op_id, result_id, asset_id, row.sha256, row.size, asset_id=asset_id, notes=notes)


async def lock_asset(job, target, *, actor: str, api_key: str | None = None,
                     run_id: int | None = None) -> OpResult:
    """Move an asset into Immich's locked folder (NSFW) and record it.

    Afterwards the API key cannot see the asset (GET and DELETE answer 400).
    The register keeps it as 'locked': verify() does not report it as lost,
    it still counts as a copy of its content, and MediaAssistant cannot
    trash it any more.
    """
    ic = _ic()
    jref = await _job_ref(job)
    k = jref.debug_key
    row = await _resolve_asset(jref, target)
    key = await _immich_key(jref, api_key)
    asset_id = row.location
    if row.state == "locked":
        op_id = await _observed(jref, "lock", actor, asset_id, row.id, "war bereits gesperrt",
                                dst_kind="immich_locked")
        return OpResult(op_id, row.id, asset_id, row.sha256, row.size, identical=True, asset_id=asset_id)
    op_id, _ = await _begin(jref, op="lock", actor=actor, src_kind="immich", src=asset_id,
                            dst_kind="immich_locked", copy_id=row.id, run_id=run_id,
                            detail={"immich_user_id": jref.immich_user_id})
    await log_info(LOG_SOURCE, f"{k} Wird in Immich gesperrt (Locked Folder): Asset {asset_id}", f"#{op_id}")
    _crashpoint("intent")
    try:
        await ic.lock_asset(asset_id, api_key=key)
    except Exception as exc:
        error = _describe(exc)
        await _close(op_id, "failed", error=error)
        await log_warning(LOG_SOURCE, f"{k} Sperren in Immich fehlgeschlagen: Asset {asset_id}", f"#{op_id} {error}")
        raise FileOpFailed(error, op_id) from exc
    async with async_session() as session:
        current = await session.get(FileCopy, row.id)
        current.state = "locked"
        await _finish(session, op_id, "done", dst_kind="immich_locked", dst=asset_id, dst_copy_id=row.id)
        await session.commit()
    await log_info(LOG_SOURCE, f"{k} In Immich gesperrt: Asset {asset_id}",
                   f"#{op_id} für den API-Key danach unsichtbar — im Register 'locked', nicht verloren")
    return OpResult(op_id, row.id, asset_id, row.sha256, row.size, asset_id=asset_id)


async def _verify_immich(row: FileCopy, actor: str) -> FileCopy:
    """Check an Immich copy against Immich: present with the same checksum,
    in the trash, locked, or gone. Raises FileOpFailed when Immich cannot
    tell — a transient failure must never read as a lost asset."""
    ic = _ic()
    jref = await _job_ref(row.job_id)
    key = await _immich_key(jref)
    try:
        state, info = await _asset_live(row.location, key)
    except Exception as exc:
        raise FileOpFailed(f"Zustand von Asset {row.location} in Immich unbekannt: {_describe(exc)}") from exc
    checksum = (info or {}).get("checksum")
    now = datetime.now()
    message, level = None, log_warning
    async with async_session() as session:
        cur = await session.get(FileCopy, row.id)
        before = cur.state
        if before == "moved":
            return cur
        same = not (checksum and cur.checksum and checksum != cur.checksum)
        if state == ic.ASSET_PRESENT and same:
            if before != "present" and await _present_at(session, cur.location) is None:
                cur.state, cur.trashed_at = "present", None
                message, level = f"wieder vorhanden (vorher {before})", log_info
            cur.verified_at = now
            cur.checksum = cur.checksum or checksum
        elif state == ic.ASSET_TRASHED and same:
            if before != "trashed":
                cur.state, cur.trashed_at = "trashed", cur.trashed_at or now
                message = "liegt im Immich-Papierkorb"
            cur.verified_at = now
            cur.checksum = cur.checksum or checksum
        elif state != ic.ASSET_MISSING:
            if before != "missing":
                cur.state = "missing"
                message = f"hat in Immich anderen Inhalt (Prüfsumme {checksum})"
        elif before == "trashed":
            # Gone from the trash: Immich emptied it after its retention.
            cur.state = "deleted"
            message, level = "von Immich aus dem Papierkorb endgültig gelöscht", log_info
            session.add(FileOp(job_id=cur.job_id, op="purge", src_kind="immich", src=cur.location,
                               copy_id=cur.id, sha256_before=cur.sha256, size=cur.size, actor=actor,
                               reason="Immich hat seinen Papierkorb geleert", state="done",
                               finished_at=now, detail={"observed": "Asset nicht mehr in Immich"}))
        elif before == "locked" or await _locked_by_ma(session, cur):
            if before != "locked":
                cur.state = "locked"
                message, level = "gesperrt (Locked Folder) — für den API-Key unsichtbar, nicht verloren", log_info
        elif before not in ("missing", "deleted"):
            cur.state = "missing"
            message = "fehlt in Immich (für diesen Zugang nicht sichtbar)"
        await session.commit()
    if message:
        await level(LOG_SOURCE, f"{jref.debug_key} Immich-Exemplar {message}: Asset {cur.location}",
                    f"Exemplar #{cur.id} ({actor})")
    return cur


# ---------------------------------------------------------------------------
# Background duty
# ---------------------------------------------------------------------------

MAINTENANCE_FIRST_DELAY_S = 600
MAINTENANCE_INTERVAL_S = 24 * 3600


async def trash_maintenance(shutdown_event: asyncio.Event, *,
                            first_delay_s: float = MAINTENANCE_FIRST_DELAY_S,
                            interval_s: float = MAINTENANCE_INTERVAL_S) -> None:
    """Daily, starting ten minutes after startup: empty the local trash after
    the retention period, record Immich assets Immich purged from its own
    trash, and settle operations whose check failed at startup (e.g. while
    Immich was unreachable)."""
    delay = first_delay_s
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        delay = interval_s
        try:
            settled = await reconcile_open_intents(older_than_s=interval_s)
            counts = await purge_trash(TRASH_RETENTION_DAYS)
            still_open = sum(1 for r in settled if r["state"] == "intent")
            detail = (f"lokal endgültig gelöscht {counts['purged']}, fehlgeschlagen {counts['failed']}, "
                      f"belegt {counts['busy']}; Immich-Papierkorb geprüft {counts.get('immich_checked', 0)}, "
                      f"von Immich geleert {counts.get('immich_gone', 0)}, nicht prüfbar "
                      f"{counts.get('immich_unchecked', 0)}; offene Operationen abgeglichen "
                      f"{len(settled) - still_open}, weiter offen {still_open}")
            log = log_warning if counts["failed"] or still_open or counts.get("immich_unchecked") else log_info
            await log(LOG_SOURCE, "Papierkorb-Wartung erledigt", detail)
        except Exception as exc:
            logger.exception("trash maintenance failed")
            await log_error(LOG_SOURCE, "Papierkorb-Wartung fehlgeschlagen", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def _probe_transfer(op_src, src_is_file, final, part, tmp_dir, dst_dir, expected_sha) -> dict:
    """Look at the disk for one open transfer. Removes only this operation's
    own part files, and only when a verified copy or the source remains
    (an Immich source always remains)."""
    leftovers = []
    if part and os.path.lexists(part):
        leftovers.append(part)
    if tmp_dir and os.path.isdir(tmp_dir):
        leftovers.extend(e.path for e in os.scandir(tmp_dir) if e.is_file(follow_symlinks=False))
    found_via_link = False
    if final is None and dst_dir:
        for p in leftovers:
            st = os.lstat(p)
            if st.st_nlink > 1:
                sibling = _find_sibling(dst_dir, st)
                if sibling:
                    final, found_via_link = sibling, True
                    break
    src_exists = bool(src_is_file and op_src and os.path.isfile(op_src))
    final_sha = _hash_regular(final) if final else None
    # A name found through the part's inode holds the part's verified content.
    final_ok = final_sha is not None and (found_via_link or not expected_sha or final_sha == expected_sha)
    size = os.path.getsize(final) if final_ok else None
    kept = []
    if final_ok or src_exists or not src_is_file:
        for p in leftovers:
            _unlink_quiet(p)
        _remove_tree_quiet(tmp_dir)
    else:
        kept = leftovers  # the only trace of the content: never removed here
    return {"final": final, "final_sha": final_sha, "final_ok": final_ok, "size": size,
            "src_exists": src_exists, "kept": kept, "found_via_link": found_via_link}


async def _end(op_id: int, state: str, *, notes=None, **values) -> None:
    async with async_session() as session:
        entry = await session.get(FileOp, op_id)
        detail = dict(entry.detail or {})
        if notes:
            detail["reconcile"] = notes
        await _finish(session, op_id, state, detail=detail, **values)
        await session.commit()


async def _mark_copy(copy_id: int | None, state: str) -> None:
    if not copy_id:
        return
    async with async_session() as session:
        row = await session.get(FileCopy, copy_id)
        if row is not None:
            row.state = state
            await session.commit()


async def _reconcile_transfer(entry: FileOp) -> tuple[str, str]:
    detail = entry.detail or {}
    src_is_file = entry.src_kind != "immich"
    probe = await asyncio.to_thread(
        _probe_transfer, entry.src, src_is_file, entry.dst, detail.get("part"),
        detail.get("tmp_dir"), detail.get("dst_dir"), entry.sha256_after)
    if not src_is_file:
        src_state = "Quelle liegt in Immich"
    else:
        src_state = "Quelle unverändert" if probe["src_exists"] else "Quelle fehlt"

    if probe["final"] is None:
        note = f"abgebrochen, bevor das Ziel abgelegt war; {src_state}"
        if probe["kept"]:
            note += f"; Inhalt liegt noch in {', '.join(probe['kept'])}"
        if src_is_file and not probe["src_exists"]:
            await _mark_copy(entry.copy_id, "missing")
            await log_error(LOG_SOURCE, f"Abgleich #{entry.id}: Quelle fehlt, kein Ziel",
                            f"{entry.op} {entry.src} — {note}")
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note

    if not probe["final_ok"]:
        note = f"Ziel {probe['final']} fehlt oder weicht ab; {src_state}"
        if probe["kept"]:
            note += f"; Inhalt liegt noch in {', '.join(probe['kept'])}"
        if not probe["src_exists"]:
            await log_error(LOG_SOURCE, f"Abgleich #{entry.id}: Inhalt nicht am Ziel", note)
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note

    async with async_session() as session:
        op_row = await session.get(FileOp, entry.id)
        dst_copy_id, notes = await _settle(session, op_row, probe["final"], probe["final_sha"],
                                           probe["size"], src_gone=not probe["src_exists"])
        notes = [f"Ziel geprüft: {probe['final']}; {src_state}"] + notes
        detail = dict(op_row.detail or {}, reconcile=notes)
        await _finish(session, entry.id, "reconciled", dst=probe["final"], dst_copy_id=dst_copy_id,
                      sha256_after=probe["final_sha"], size=probe["size"], detail=detail)
        await session.commit()
    return "reconciled", "; ".join(notes)


async def _reconcile_write(entry: FileOp) -> tuple[str, str]:
    part = (entry.detail or {}).get("part")
    await asyncio.to_thread(_unlink_quiet, part)
    actual = await asyncio.to_thread(_hash_regular, entry.src)
    if actual is None:
        note = "Datei fehlt"
        await _mark_copy(entry.copy_id, "missing")
        await log_error(LOG_SOURCE, f"Abgleich #{entry.id}: Datei fehlt", entry.src)
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note
    if actual == entry.sha256_before:
        note = "nicht angewendet, Datei unverändert"
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note
    size = await asyncio.to_thread(os.path.getsize, entry.src)
    async with async_session() as session:
        row = await session.get(FileCopy, entry.copy_id) if entry.copy_id else None
        if row is not None:
            row.sha256, row.size, row.verified_at = actual, size, datetime.now()
        await _finish(session, entry.id, "reconciled", sha256_after=actual, size=size,
                      dst_kind=entry.src_kind, dst=entry.src, dst_copy_id=entry.copy_id,
                      detail=dict(entry.detail or {}, reconcile=["angewendet, neuer Inhalt registriert"]))
        await session.commit()
    return "reconciled", "angewendet"


async def _reconcile_purge(entry: FileOp) -> tuple[str, str]:
    if await asyncio.to_thread(os.path.lexists, entry.src):
        note = "nicht gelöscht, Datei noch vorhanden"
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note
    await _mark_copy(entry.copy_id, "deleted")
    await _end(entry.id, "reconciled", notes=["Datei entfernt"])
    return "reconciled", "Datei entfernt"


async def _reconcile_upload(entry: FileOp) -> tuple[str, str]:
    """An upload cut short: find the asset by the recorded id or, if the
    crash came before Immich's answer was recorded, by the checksum."""
    ic = _ic()
    detail = entry.detail or {}
    jref = await _job_ref(entry.job_id)
    key = await _immich_key(jref)
    checksum = detail.get("checksum")
    asset_id, via = entry.dst, "Protokoll"
    if not asset_id:
        hit = await ic.find_asset_by_checksum(checksum, api_key=key) if checksum else None
        if hit is None:
            note = ("abgebrochen vor dem Upload" if not checksum
                    else "nicht in Immich angekommen (Prüfsumme unbekannt)") + "; Quelle unverändert"
            await _end(entry.id, "failed", error=note, notes=[note])
            return "failed", note
        asset_id, via = hit["id"], "Prüfsumme"
    state, info = await _asset_live(asset_id, key)
    if state != ic.ASSET_PRESENT or (info or {}).get("checksum") != checksum:
        note = f"Asset {asset_id} ist {state} — nicht als hochgeladen gewertet; Quelle unverändert"
        await _end(entry.id, "failed", error=note, notes=[note], dst=asset_id)
        return "failed", note
    status = detail.get("status")
    created = status == "created" if status else _created_since(info, entry.created_at)
    notes = [f"Asset {asset_id} gefunden ({via}) und geprüft"]
    async with async_session() as session:
        row, owned = await _adopt_asset(session, jref, asset_id, entry.sha256_before, entry.size,
                                        checksum, created=created, notes=notes)
        op_row = await session.get(FileOp, entry.id)
        await _finish(session, entry.id, "reconciled", dst_kind="immich", dst=asset_id,
                      dst_copy_id=row.id, sha256_after=entry.sha256_before,
                      detail=dict(op_row.detail or {}, owned=owned, reconcile=notes,
                                  status=status or ("created" if created else "duplicate")))
        await session.commit()
    return "reconciled", "; ".join(notes)


_ASSET_OP_EFFECT = {"trash": ("trashed", "trashed", "immich_trash"),
                    "restore": ("present", "present", "immich"),
                    "lock": ("missing", "locked", "immich_locked")}


async def _reconcile_asset_op(entry: FileOp) -> tuple[str, str]:
    """trash / restore / lock of an asset: did Immich apply it?"""
    ic = _ic()
    jref = await _job_ref(entry.job_id)
    key = await _immich_key(jref)
    wanted, register_state, dst_kind = _ASSET_OP_EFFECT[entry.op]
    state = await ic.get_asset_state(entry.src, api_key=key)
    if state != wanted:
        note = f"nicht angewendet: Asset ist {state}"
        await _end(entry.id, "failed", error=note, notes=[note])
        if entry.copy_id:
            try:
                await verify(entry.copy_id)
            except FileOpError as exc:
                logger.info("verify after reconcile #%s: %s", entry.id, exc)
        return "failed", note
    async with async_session() as session:
        current = await session.get(FileCopy, entry.copy_id) if entry.copy_id else None
        if current is not None and current.state != register_state and (
                register_state != "present" or await _present_at(session, current.location) is None):
            current.state = register_state
            if register_state == "trashed":
                current.trashed_at = current.trashed_at or entry.created_at
            elif register_state == "present":
                current.trashed_at = None
        op_row = await session.get(FileOp, entry.id)
        await _finish(session, entry.id, "reconciled", dst_kind=dst_kind, dst=entry.src,
                      dst_copy_id=entry.copy_id,
                      detail=dict(op_row.detail or {}, reconcile=["angewendet, Register nachgeführt"]))
        await session.commit()
    return "reconciled", "angewendet, Register nachgeführt"


async def _reconcile_replace(entry: FileOp) -> tuple[str, str]:
    """A replace cut short. Runs after its upload and trash rows were settled."""
    ic = _ic()
    old_id = (entry.detail or {}).get("old_asset")
    new_id = entry.dst
    if not new_id:
        async with async_session() as session:
            children = (await session.execute(select(FileOp).where(
                FileOp.op == "upload", FileOp.job_id == entry.job_id, FileOp.id > entry.id)
            )).scalars().all()
        child = next((c for c in children if (c.detail or {}).get("parent_op") == entry.id), None)
        if child is not None and child.state in ("done", "reconciled"):
            new_id = child.dst
    if not new_id:
        note = f"keine neue Version in Immich; altes Asset {old_id} unverändert"
        await _end(entry.id, "failed", error=note, notes=[note])
        return "failed", note
    jref = await _job_ref(entry.job_id)
    key = await _immich_key(jref)
    old_state = await ic.get_asset_state(old_id, api_key=key) if old_id and old_id != new_id else None
    if old_state == ic.ASSET_PRESENT:
        note = (f"unvollständig: neue Version Asset {new_id} ist registriert, das alte Asset {old_id} "
                "ist noch da, Tags/Beschreibung evtl. nicht übertragen — Ersetzen beim nächsten Lauf wiederholen")
        await _end(entry.id, "failed", error=note, notes=[note], dst=new_id)
        return "failed", note
    note = f"ersetzt durch Asset {new_id}" + (f"; altes Asset {old_id} ist {old_state}" if old_state else "") \
        + "; Übertragung von Tags/Beschreibung nicht bestätigt"
    await _end(entry.id, "reconciled", notes=[note], dst=new_id)
    return "reconciled", note


async def reconcile_open_intents(*, older_than_s: float = 0.0,
                                 job_ids: list[int] | None = None) -> list[dict]:
    """Settle ledger rows left open by a crash, against what is on disk and
    in Immich.

    Meant for startup, before the worker runs (rows of operations still
    running would be settled under their feet). Non-destructive: it never
    removes a source or a placed destination, only the operation's own part
    files, and those only while a verified copy remains; in Immich it only
    reads. Each open row ends as 'reconciled' (the effect is there and
    registered) or 'failed'. A row whose check raised (Immich unreachable)
    stays open and is reported with state 'intent'. ``job_ids`` limits it
    to some jobs.
    """
    cutoff = datetime.now() - timedelta(seconds=older_than_s)
    query = select(FileOp).where(FileOp.state == "intent", FileOp.created_at <= cutoff)
    if job_ids is not None:
        query = query.where(FileOp.job_id.in_(job_ids))
    async with async_session() as session:
        entries = (await session.execute(query.order_by(FileOp.id))).scalars().all()
    # A replace is judged by its upload and trash rows, so those go first.
    entries = sorted(entries, key=lambda e: (e.op == "replace", e.id))
    results = []
    for entry in entries:
        try:
            if entry.src_kind == "immich" and entry.op in _ASSET_OP_EFFECT:
                state, note = await _reconcile_asset_op(entry)
            elif entry.op in _TRANSFER_OPS:
                state, note = await _reconcile_transfer(entry)
            elif entry.op == "write_metadata":
                state, note = await _reconcile_write(entry)
            elif entry.op == "purge":
                state, note = await _reconcile_purge(entry)
            elif entry.op == "upload":
                state, note = await _reconcile_upload(entry)
            elif entry.op == "replace":
                state, note = await _reconcile_replace(entry)
            else:
                state, note = "failed", "kein Abgleich für diese Operation — bitte prüfen"
                await _end(entry.id, state, error=note, notes=[note])
        except Exception as exc:
            logger.exception("reconcile of file_ops #%s failed", entry.id)
            await log_error(LOG_SOURCE, f"Abgleich #{entry.id} fehlgeschlagen", f"{type(exc).__name__}: {exc}")
            results.append({"op_id": entry.id, "op": entry.op, "state": "intent", "note": str(exc)})
            continue
        key = (await _job_ref(entry.job_id)).debug_key if entry.job_id else "system"
        log = log_info if state == "reconciled" else log_warning
        await log(LOG_SOURCE, f"{key} Offene Operation abgeglichen: {entry.op} #{entry.id} → {state}", note)
        results.append({"op_id": entry.id, "op": entry.op, "state": state, "note": note})
    return results


# ---------------------------------------------------------------------------
# Duplicate resolution: look an asset up, hand a copy over
# ---------------------------------------------------------------------------

async def immich_copy(job, asset_id: str, *, check: bool = True) -> FileCopy:
    """The register row of an Immich asset `job` points at.

    An asset the register does not know yet is registered from the job
    fields exactly as trash_asset() and replace() would do it (the calling
    job owns it if its own fields name it, otherwise the first job that
    does; the others get references). With ``check`` the row is checked
    against Immich now (verify()); FileOpFailed when Immich cannot tell.
    """
    jref = await _job_ref(job)
    row = await _resolve_asset(jref, asset_id)
    if check:
        row = await verify(row)
    return row


async def transfer(target, to_job, *, actor: str, reason: str) -> FileCopy:
    """Make `to_job` the owner of a copy (a FileCopy, its id, or an asset id).

    For the duplicate resolution: the kept job takes over the asset of a
    donor that is resolved, so the later replace() may move it to Immich's
    trash, and a shared asset does not stay owned by a job that no longer
    exists. A reference of `to_job` to the copy becomes redundant and is
    dropped; the old owner keeps none. Nothing on disk or in Immich changes:
    one ledger row (op 'transfer'), written in the transaction of the change.
    """
    jref = await _job_ref(to_job)
    if _is_asset_id(target):
        row = await _resolve_asset(jref, target)
    else:
        copy_id = target.id if isinstance(target, FileCopy) else int(target)
        async with async_session() as session:
            row = await session.get(FileCopy, copy_id)
        if row is None:
            raise FileOpRefused(f"Exemplar #{copy_id} nicht gefunden")
    async with async_session() as session:
        cur = await session.get(FileCopy, row.id)
        before = cur.job_id
        if before == jref.id:
            return cur
        cur.job_id = jref.id
        for ref in (await session.execute(select(AssetRef).where(
                AssetRef.location == cur.location, AssetRef.job_id.in_([jref.id, before])
        ))).scalars().all():
            await session.delete(ref)
        old_owner = await _owner_label(session, before)
        session.add(FileOp(job_id=jref.id, op="transfer", src_kind=cur.kind, src=cur.location,
                           dst_kind=cur.kind, dst=cur.location, copy_id=cur.id, dst_copy_id=cur.id,
                           actor=actor, reason=reason, state="done", finished_at=datetime.now(),
                           detail={"from_job": before}))
        await session.commit()
    await log_info(LOG_SOURCE, f"{jref.debug_key} Exemplar übernommen: {os.path.basename(cur.location)}",
                   f"Exemplar #{cur.id} ({cur.kind}) von {old_owner} — {reason}")
    return cur


# ---------------------------------------------------------------------------
# Helpers for IA-08 (appended): resume after a crash, empty inbox folders
# ---------------------------------------------------------------------------

async def last_result(job, ops, *, src: str | None = None, since: datetime | None = None) -> str | None:
    """Where the newest finished operation of `job` among `ops` left its
    content — a path or an asset id — if that copy is still present (or
    locked); None otherwise.

    For a step that runs again after a crash (R-10c): fileops commits the
    ledger and the register itself, the step's job fields are committed
    only after the step returned. ``src`` limits the search to operations
    from that path, ``since`` to operations started after that time (e.g.
    the job's last completed run, so an earlier attempt's result is not
    taken for this one's).
    """
    jref = await _job_ref(job)
    query = select(FileOp.dst_copy_id).where(
        FileOp.job_id == jref.id, FileOp.op.in_(tuple(ops)),
        FileOp.state.in_(("done", "reconciled")), FileOp.dst_copy_id.isnot(None))
    if src is not None:
        query = query.where(FileOp.src == _norm(src))
    if since is not None:
        query = query.where(FileOp.created_at > since)
    async with async_session() as session:
        copy_id = (await session.execute(query.order_by(FileOp.id.desc()).limit(1))).scalar()
        row = await session.get(FileCopy, copy_id) if copy_id else None
    if row is None or row.state not in ("present", "locked"):
        return None
    return row.location


# System files that do not make a folder "non-empty" (Synology, macOS, Windows)
_JUNK_FILES = frozenset({".ds_store", "thumbs.db", ".thumbs", "desktop.ini"})
_JUNK_DIRS = frozenset({"@eadir", ".synology"})


def _junk_entries(path: str) -> list | None:
    """The entries of a folder if all of them are system junk, else None."""
    entries = list(os.scandir(path))
    for entry in entries:
        name = entry.name.lower()
        if entry.is_dir(follow_symlinks=False):
            if name not in _JUNK_DIRS:
                return None
        elif name not in _JUNK_FILES:
            return None
    return entries


def prune_empty_dirs_blocking(start_dir: str, stop_at: str) -> list[str]:
    """Remove folders from start_dir upwards that hold nothing but system
    junk, up to (not including) stop_at. Blocking: run it in a thread.

    Only folders inside stop_at are touched, decided by path containment,
    not by string length (R-23): a start_dir outside stop_at (e.g. a work
    copy in reprocess/ below an /inbox) removes nothing. Only junk names
    are deleted; a folder that is gone or holds anything else stops the
    climb, so parallel jobs and files arriving meanwhile are safe. The
    junk carries no content, so there is no ledger row; the caller logs
    the returned folders.
    """
    if not start_dir or not stop_at:
        return []
    stop = os.path.realpath(stop_at)
    current = os.path.realpath(start_dir)
    removed: list[str] = []
    while current != stop and os.path.commonpath([current, stop]) == stop:
        try:
            if not os.path.isdir(current):
                break
            entries = _junk_entries(current)
            if entries is None:
                break
            for entry in entries:
                name = entry.name.lower()
                if entry.is_dir(follow_symlinks=False) and name in _JUNK_DIRS:
                    shutil.rmtree(entry.path, ignore_errors=True)
                elif not entry.is_dir(follow_symlinks=False) and name in _JUNK_FILES:
                    os.remove(entry.path)
            os.rmdir(current)  # fails if anything arrived meanwhile
        except OSError:
            break
        removed.append(current)
        current = os.path.dirname(current)
    return removed

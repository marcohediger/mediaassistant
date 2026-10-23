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
- Blocking I/O runs in worker threads. The database is used in short sessions
  of this module's own, never held open across a file copy.

Callers must not hold uncommitted writes in their own session when calling
in: fileops commits in sessions of its own and SQLite has one write lock (the
reason IA-08 runs with ``no_autoflush``).
"""
import asyncio
import errno
import hashlib
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
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
    location: str | None  # where that copy is (for trash: the trash path)
    sha256: str | None
    size: int | None
    identical: bool = False  # the destination already held this content; nothing was copied


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
            select(Job.debug_key, Job.set_id).where(Job.id == job_id))).first()
    if row is None:
        return _JobRef(job_id, f"job-{job_id}", None)
    return _JobRef(job_id, row.debug_key or f"job-{job_id}", row.set_id)


async def _debug_keys(session, job_ids) -> list[str]:
    ids = [j for j in job_ids if j is not None]
    if not ids:
        return []
    found = dict((await session.execute(
        select(Job.id, Job.debug_key).where(Job.id.in_(ids)))).all())
    return [found.get(j) or f"job-{j}" for j in ids]


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
        if existing.sha256 in (None, sha):
            existing.sha256 = sha
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
                 run_id=None, register_src=None, part_suffix=None, tmp_dir=False) -> tuple[int, dict]:
    """Write the intent row; returns (op id, detail). Raises FileOpBusy."""
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
            detail: dict = {}
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
        entry = FileOp(job_id=jref.id, op=op, src_kind=src_kind, src=src, copy_id=copy_id,
                       actor=actor, reason=reason, state="failed", error=problem,
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
                            checksum: str | None = None) -> FileCopy:
    """Record a copy that came into the system without fileops (inbox scan,
    an asset found in Immich). No ledger row: nothing on disk changes.

    Local kinds: without sha256 the file is hashed now (verified). With a
    given sha256 it is trusted but unverified, unless verified=True. If the
    location already belongs to another job with different content, the file
    is hashed to decide which row is stale.
    """
    if kind not in KINDS:
        raise ValueError(f"fileops: unknown kind {kind!r}")
    jref = await _job_ref(job)
    notes: list[str] = []
    if kind == "immich":
        async with async_session() as session:
            row = await _claim(session, jref.id, kind, location, sha256, size, notes,
                               verified=bool(verified))
            if checksum:
                row.checksum = checksum
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
        row = await _claim(session, jref.id, kind, location, sha256, st.st_size, notes,
                           verified=bool(verified))
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
        # IMMICH-STUB: needs the asset check of the Immich rebuild.
        raise NotImplementedError("fileops.verify: Immich-Prüfung folgt mit dem Immich-Umbau")
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
            FileCopy.sha256 == row.sha256, FileCopy.state == "present",
            FileCopy.id != row.id, FileCopy.location != row.location,
        ).order_by(FileCopy.verified_at.desc()))).scalars().all()
    for twin in twins:
        if twin.kind == "immich":
            # Immich copies count once verified at upload; a live check comes
            # with the Immich rebuild.
            if twin.verified_at is not None:
                return twin
            continue
        checked = await verify(twin)
        if checked.state == "present":
            return checked
    return None


async def trash_blockers(job, target, *, allow_last_copy: bool = False) -> list[str]:
    """Why `job` may not trash `target` (a FileCopy or its id); empty = allowed."""
    jref = await _job_ref(job)
    try:
        row = await _resolve_copy(jref, target, None, register=False)
    except FileOpRefused as exc:
        return [str(exc)]
    return await _blockers(jref, row, allow_last_copy)


async def _blockers(jref: _JobRef, row: FileCopy, allow_last_copy: bool) -> list[str]:
    reasons = []
    async with async_session() as session:
        if row.job_id != jref.id:
            reasons.append(f"Exemplar gehört {(await _debug_keys(session, [row.job_id]))[0]}")
        refs = await _foreign_refs(session, row.location, jref.id)
        if refs:
            reasons.append(f"wird verwendet von {', '.join(await _debug_keys(session, refs))}")
    if not allow_last_copy and not reasons and await _verified_twin(row) is None:
        reasons.append("letztes Exemplar dieses Inhalts (kein anderes geprüftes Exemplar)")
    return reasons


async def trash(job, target, *, actor: str, reason: str, allow_last_copy: bool = False,
                expected_sha256: str | None = None, run_id: int | None = None) -> OpResult:
    """Move a local copy into the set's trash instead of deleting it.

    Allowed only if (a) another present copy of the same content verifies
    right now — unless ``allow_last_copy`` (a delete the user asked for; the
    trash itself is then the fallback) — and (b) the copy is owned by `job`
    and no other job references it. `target` is a FileCopy, its id, or a
    path; an unregistered path needs ``expected_sha256`` and must match it.
    """
    jref = await _job_ref(job)
    try:
        row = await _resolve_copy(jref, target, expected_sha256,
                                  register=expected_sha256 is not None)
    except FileOpRefused as exc:
        src = target if isinstance(target, str) else None
        op_id = await _refused(jref, "trash", actor, "local", src, None, str(exc), reason)
        await log_warning(LOG_SOURCE, f"{jref.debug_key} Löschen verweigert", f"#{op_id} {exc}")
        raise FileOpRefused(str(exc), op_id) from None
    if row.kind == "immich":
        raise FileOpRefused("Immich-Exemplare gehen über trash_asset()")
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
    """
    async with async_session() as session:
        if copy_id is None:
            entry = await session.get(FileOp, op_id) if op_id is not None else None
            if entry is None or entry.op != "trash":
                raise FileOpRefused(f"#{op_id} ist keine Papierkorb-Operation")
            copy_id = entry.dst_copy_id or entry.copy_id
        row = await session.get(FileCopy, copy_id)
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
                      actor: str = "system:papierkorb") -> dict:
    """Empty the local trash of entries older than the retention period."""
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
    """Remove a derived temporary file for good (kind 'temp' only, e.g. the
    IA-04 JPEG). Everything else goes through trash()."""
    jref = await _job_ref(job)
    row = await _resolve_copy(jref, target, None, register=False)
    if row.kind != "temp":
        raise FileOpRefused(f"Nur temporäre Dateien werden direkt gelöscht, nicht {row.kind}")
    if row.job_id != jref.id:
        raise FileOpRefused("Temporäre Datei gehört einem anderen Job")
    return await _remove_registered(jref, row, row.location, src_kind="temp", actor=actor,
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
# Immich: interfaces only. IMMICH-STUB — wired up once immich_client has
# trash_asset/restore_asset (Immich 3.1.0 rebuild). The rules are the same as
# for local copies: trash_blockers() decides, asset_refs protect shared assets.
# ---------------------------------------------------------------------------

async def upload(job, copy, *, actor: str, albums=None, sidecar=None,
                 api_key: str | None = None) -> OpResult:
    """Upload a verified local copy; registers the asset (kind immich, verified)."""
    raise NotImplementedError("fileops.upload: Immich-Anbindung folgt mit dem Immich-Umbau")


async def replace(job, old_asset_id: str, copy, *, actor: str,
                  api_key: str | None = None) -> OpResult:
    """Upload a new version, then move the old asset to Immich's trash —
    only if no other job references it."""
    raise NotImplementedError("fileops.replace: Immich-Anbindung folgt mit dem Immich-Umbau")


async def trash_asset(job, asset_id: str, *, actor: str, reason: str,
                      allow_last_copy: bool = False, api_key: str | None = None) -> OpResult:
    """Move an asset into Immich's trash (delete with force=False)."""
    raise NotImplementedError("fileops.trash_asset: Immich-Anbindung folgt mit dem Immich-Umbau")


async def restore_asset(copy_id: int, *, actor: str = "user:restore",
                        api_key: str | None = None) -> OpResult:
    raise NotImplementedError("fileops.restore_asset: Immich-Anbindung folgt mit dem Immich-Umbau")


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


async def reconcile_open_intents(*, older_than_s: float = 0.0,
                                 job_ids: list[int] | None = None) -> list[dict]:
    """Settle ledger rows left open by a crash, against what is on disk.

    Meant for startup, before the worker runs (rows of operations still
    running would be settled under their feet). Non-destructive: it never
    removes a source or a placed destination, only the operation's own part
    files, and those only while a verified copy remains. Each open row ends
    as 'reconciled' (the effect is there and registered) or 'failed'.
    ``job_ids`` limits it to some jobs.
    """
    cutoff = datetime.now() - timedelta(seconds=older_than_s)
    query = select(FileOp).where(FileOp.state == "intent", FileOp.created_at <= cutoff)
    if job_ids is not None:
        query = query.where(FileOp.job_id.in_(job_ids))
    async with async_session() as session:
        entries = (await session.execute(query.order_by(FileOp.id))).scalars().all()
    results = []
    for entry in entries:
        try:
            if entry.op in _TRANSFER_OPS:
                state, note = await _reconcile_transfer(entry)
            elif entry.op == "write_metadata":
                state, note = await _reconcile_write(entry)
            elif entry.op == "purge":
                state, note = await _reconcile_purge(entry)
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

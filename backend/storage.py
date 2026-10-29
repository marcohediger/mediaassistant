"""The storage the pipeline works on: the inbox folders and the library base
paths of the sets.

A drive that is not there is a problem of the infrastructure, never of a
job (services.py). Inside a container it rarely shows as an error: the
mount point stays behind as an empty folder of the host, and whatever is
written there lands on the wrong disk and vanishes from view once the
drive is back. So MediaAssistant marks every root it works in with a file
``.mediaassistant`` (as Immich does with ``.immich``) and remembers the
roots it marked (config ``storage.marked``). A marked root without its
marker is a drive that is not mounted.

A root is unusable (``Problem``) when it

- cannot be read (I/O error, stale handle, no answer within CHECK_TIMEOUT_S),
- is not writable (read-only file system),
- was marked and has lost its marker, or is gone altogether,
- is missing or empty although the register knows files in it (a drive
  that was not mounted when the root was first seen).

Otherwise a root that exists is marked on its first check; one that does
not exist yet (a new set's base path) is fine and marked once it exists.
Paths in the database's folder (/app/data) are never checked: without that
volume there is no database either.

    problem(root, kind)     why a root is unusable, or None (marks a usable root)
    describe(problem, i18n) the problem as the user reads it
    check(i18n)             (ok, detail) of the roots in use — the health watcher
    down_for_job(job)       Problem of the first unusable root of a job, or None
    unavailable(paths)      Problem of the first unusable root among paths, or None
    pause(problem, ...)     pause the pipeline for it (services.pause)
    adopt()                 mark roots anew whose marker the user says is gone on purpose
"""
import asyncio
import errno
import logging
import os
import stat
from dataclasses import dataclass

from sqlalchemy import select

from config import config_manager
from database import DATABASE_PATH, async_session
from models import FileCopy, InboxDirectory, ProcessingSet

logger = logging.getLogger("mediaassistant.storage")

MARKER = ".mediaassistant"
MARKED_KEY = "storage.marked"
CHECK_TIMEOUT_S = 15
# The problems the user may settle by marking the root anew: the folder is
# there and usable, only the evidence that it is the right drive is missing.
ADOPTABLE = frozenset({"no_marker", "empty_known"})

_MARKER_TEXT = (
    "MediaAssistant works in this folder. Please do not delete this file:\n"
    "MediaAssistant checks it to know that the drive is mounted. Without it\n"
    "the pipeline stops and waits.\n"
)

# A check of a root that has not answered yet: a hung mount gets no second thread.
_pending: dict[str, asyncio.Future] = {}
_marked_lock = asyncio.Lock()


@dataclass(frozen=True)
class Problem:
    code: str       # absent / no_marker / empty_known / unreadable / timeout / read_only
    kind: str       # inbox / library
    root: str
    error: str = ""


def norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


_DB_DIR = norm(os.path.dirname(DATABASE_PATH))


def _checked(root: str) -> bool:
    return not _inside(root, _DB_DIR)


async def _roots(*, active_only: bool) -> dict[str, str]:
    """root -> kind: the inbox folders and library base paths of the sets."""
    async with async_session() as session:
        sets = (await session.execute(select(ProcessingSet))).scalars().all()
        inboxes = (await session.execute(select(InboxDirectory))).scalars().all()
    active = {s.id for s in sets if s.active}
    found: dict[str, str] = {}
    for d in inboxes:
        if d.path and (not active_only or (d.active and d.set_id in active)):
            found.setdefault(norm(d.path), "inbox")
    for s in sets:
        if not active_only or s.active:
            found.setdefault(norm(s.library_base_path or "/library"), "library")
    return {root: kind for root, kind in found.items() if _checked(root)}


def _root_of(path: str, roots: dict[str, str]) -> str | None:
    """The innermost root that holds path."""
    path = norm(path)
    best = None
    for root in roots:
        if _inside(path, root) and (best is None or len(root) > len(best)):
            best = root
    return best


def _why(exc: OSError) -> str:
    name = errno.errorcode.get(exc.errno or 0, "")
    return f"{exc.strerror or type(exc).__name__}{f' ({name})' if name else ''}"


def _look(root: str) -> dict:
    """What the disk says about a root (blocking)."""
    try:
        st = os.stat(root)
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        return {"error": _why(exc)}
    if not stat.S_ISDIR(st.st_mode):
        return {"error": "kein Ordner"}
    try:
        with os.scandir(root) as entries:
            empty = not any(e.name != MARKER for e in entries)
    except OSError as exc:
        return {"error": _why(exc)}
    try:
        marker = stat.S_ISREG(os.stat(os.path.join(root, MARKER)).st_mode)
    except FileNotFoundError:
        marker = False
    except OSError as exc:
        return {"error": _why(exc)}
    return {"exists": True, "empty": empty, "marker": marker, "writable": os.access(root, os.W_OK)}


async def _look_async(root: str) -> dict:
    fut = _pending.get(root)
    if fut is None:
        fut = asyncio.ensure_future(asyncio.to_thread(_look, root))
        _pending[root] = fut
    try:
        seen = await asyncio.wait_for(asyncio.shield(fut), CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"timeout": True}
    if _pending.get(root) is fut:
        del _pending[root]
    return seen


def _write_marker(root: str) -> None:
    path = os.path.join(root, MARKER)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(_MARKER_TEXT)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        if not os.path.isfile(path):  # e.g. a dangling link in its place
            raise OSError(errno.EEXIST, f"{MARKER} ist keine Datei") from None


async def _marked() -> set[str]:
    return set(await config_manager.get(MARKED_KEY, []) or [])


async def _remember(root: str) -> None:
    """Note the root as marked; roots no set uses any more are dropped."""
    async with _marked_lock:
        known = await _roots(active_only=False)
        marked = {r for r in await _marked() if r in known} | {root}
        await config_manager.set(MARKED_KEY, sorted(marked))


async def _mark(root: str, kind: str) -> "Problem | None":
    try:
        await asyncio.wait_for(asyncio.to_thread(_write_marker, root), CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return Problem("timeout", kind, root)
    except OSError as exc:
        return Problem("read_only" if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM) else "unreadable",
                       kind, root, _why(exc))
    await _remember(root)
    logger.info("storage root marked: %s", root)
    return None


async def _registered_in(root: str) -> bool:
    """Does the register know a present file in the root?"""
    low = root.rstrip(os.sep) + os.sep
    high = low[:-1] + chr(ord(os.sep) + 1)
    async with async_session() as session:
        found = (await session.execute(select(FileCopy.id).where(
            FileCopy.state == "present", FileCopy.kind != "immich",
            FileCopy.location >= low, FileCopy.location < high).limit(1))).first()
    return found is not None


async def problem(root: str, kind: str) -> Problem | None:
    """Why the root is unusable, or None. A usable root is marked."""
    seen = await _look_async(root)
    if seen.get("timeout"):
        return Problem("timeout", kind, root)
    if "error" in seen:
        return Problem("unreadable", kind, root, seen["error"])
    marked = root in await _marked()
    if not seen["exists"]:
        if marked or await _registered_in(root):
            return Problem("absent", kind, root)
        return None  # a new set's base path: created on first use
    if not seen["marker"]:
        if marked:
            return Problem("no_marker", kind, root)
        if seen["empty"] and await _registered_in(root):
            return Problem("empty_known", kind, root)
    if not seen["writable"]:
        return Problem("read_only", kind, root)
    if not seen["marker"]:
        return await _mark(root, kind)
    if not marked:
        await _remember(root)  # marked before (e.g. a restored database)
    return None


def describe(p: Problem, i18n: dict | None = None) -> str:
    texts = (i18n or {}).get("dashboard", {})
    where = texts.get(f"storage_kind_{p.kind}", p.kind)
    text = texts.get(f"storage_{p.code}", "{where} {path}: {error}")
    return text.format(where=where, path=p.root, error=p.error, marker=MARKER)


async def _first_problem(roots: dict[str, str]) -> Problem | None:
    for root, kind in sorted(roots.items()):
        found = await problem(root, kind)
        if found is not None:
            return found
    return None


async def _watched() -> dict[str, str]:
    """The roots of the active sets and those a pause names."""
    roots = await _roots(active_only=True)
    known = await _roots(active_only=False)
    for root in await config_manager.get("pipeline.auto_paused_paths", []) or []:
        if root in known:
            roots.setdefault(root, known[root])
    return roots


async def check(i18n: dict | None = None) -> tuple[bool, str]:
    """(ok, detail) of the roots in use and those the pause names."""
    roots = await _watched()
    found = await _first_problem(roots)
    if found is not None:
        return False, describe(found, i18n)
    text = (i18n or {}).get("dashboard", {}).get("storage_ok", "{count}")
    return True, text.format(count=len(roots))


async def down_for_job(job) -> Problem | None:
    """The first unusable root of a job: its set's base path and the roots
    its file lies in (inbox, library)."""
    from sets import get_set
    async with async_session() as session:
        ps = await get_set(session, getattr(job, "set_id", None))
        base = norm(ps.library_base_path or "/library") if ps else "/library"
    known = await _roots(active_only=False)
    roots = {base: "library"} if _checked(base) else {}
    for path in (job.original_path, job.target_path):
        root = _root_of(path, known) if path and path.startswith(os.sep) else None
        if root:
            roots.setdefault(root, known[root])
    return await _first_problem(roots)


async def unavailable(paths) -> Problem | None:
    """The first unusable root among the roots the paths lie in."""
    paths = [p for p in paths if p and p.startswith(os.sep)]
    if not paths:
        return None
    known = await _roots(active_only=False)
    roots = {}
    for path in paths:
        root = _root_of(path, known)
        if root:
            roots.setdefault(root, known[root])
    return await _first_problem(roots)


async def pause(p: Problem, *, where: str, error: str, tb: str = "") -> None:
    import services
    await services.pause(services.STORAGE, describe(p, await services.ui_texts()), where=where, error=error,
                         tb=tb, paths=[p.root])


async def adopt() -> list[str]:
    """Mark the roots anew whose marker is missing or that look empty
    against the register — the user confirmed the drive is the right one.
    Returns the roots marked."""
    adopted = []
    for root, kind in sorted((await _watched()).items()):
        found = await problem(root, kind)
        if found is not None and found.code in ADOPTABLE and await _mark(root, kind) is None:
            adopted.append(root)
    return adopted

"""Folder structure of a file below its inbox — cleaned for tags and albums.

The one place that turns "where the file lay in its inbox" into what Immich
shows. It used to be derived four times (IA-02, IA-07, IA-08, the duplicate
merge). A job keeps the raw relative folder (``step_result['IA-02']
['folder_path']``); tags and albums are derived from it when they are
written, with the rules below, so changed rules can be applied again later.

Two options per processing set (docs/IMMICH_ORDNERSTRUKTUR.md, decisions of
2026-10-23), independent of each other:

* ``folder_tag_mode`` — what the folders become as tags:
  ``none``; ``flat``: one flat tag per level (``Kategorie``, ``Ereignis``,
  ``2019``), the way the old folder-tag switch tagged them, but cleaned;
  ``nested``: one tag ``Ordner/<level>/<level>/…`` that Immich shows as a
  tree — a parent tag lists every picture below it.
* ``folder_albums`` — one album per folder, named by its levels joined with
  spaces (``Kategorie Ereignis 2019``), as the old folder-tag switch named
  them.

Rules, the same for both options:

* meaningless levels are dropped: month or day (``07``), a date
  (``2019-07``, ``2019-07-14``), ``DCIM``, camera folders (``104_PANA``,
  ``100APPLE``) and Google Takeout's ``Photos from 2019``;
* a level equal to the one above collapses (``X 2007/X 2007`` -> ``X 2007``),
  and so does a pure year below a level that already names that year;
* a pure year (``2019``) stays a level of its own in the tag tree
  (``Kategorie/Ereignis/2019`` — the event across all years) and is appended
  to the level above in an album name (``Ereignis 2019`` — one album per
  year);
* at most ``MAX_LEVELS`` levels; deeper ones are cut.
"""
import os
import re
from dataclasses import dataclass, field

ROOT_TAG = "Ordner"
MAX_LEVELS = 4

_DROP = (
    re.compile(r"^\d{1,2}$"),                                # month or day
    re.compile(r"^\d{4}[-_.]\d{1,2}(?:[-_.]\d{1,2})?$"),     # 2019-07, 2019-07-14
    re.compile(r"^dcim$", re.IGNORECASE),
    re.compile(r"^\d{3}[A-Z_][A-Z0-9_]{4}$"),               # DCF camera folder: 104_PANA, 100APPLE
    re.compile(r"^(?:photos from|fotos von) \d{4}$", re.IGNORECASE),  # Google Takeout
)
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


TAG_MODES = ("none", "flat", "nested")


@dataclass
class FolderOptions:
    tag_mode: str = "none"  # none / flat / nested
    albums: bool = False    # one album per folder

    @property
    def flat(self) -> bool:
        return self.tag_mode == "flat"

    @property
    def nested(self) -> bool:
        return self.tag_mode == "nested"

    @property
    def any(self) -> bool:
        return self.flat or self.nested or self.albums


@dataclass
class FolderTargets:
    tag_paths: list = field(default_factory=list)   # nested tag values, e.g. "Ordner/Kat/Ereignis"
    flat_tags: list = field(default_factory=list)   # flat tags, one per level
    albums: list = field(default_factory=list)      # album names


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def relative_folder(path: str | None, inbox: str | None) -> str | None:
    """The folders between the inbox and the file, joined with "/".

    None when the file lies directly in the inbox or outside of it (e.g.
    after a move to reprocess/ or the duplicate folder).
    """
    if not path or not inbox:
        return None
    try:
        rel = os.path.relpath(os.path.dirname(os.path.abspath(path)), os.path.abspath(inbox))
    except ValueError:
        return None
    if not rel or rel == "." or rel == ".." or rel.startswith(".." + os.sep):
        return None
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    return "/".join(parts) or None


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _names_year(level: str, year: str) -> bool:
    return re.search(rf"(?<!\d){year}(?!\d)", level) is not None


def levels(rel: str | None) -> list[str]:
    """Meaningful levels of a relative folder, years as levels of their own,
    not yet cut to MAX_LEVELS."""
    out: list[str] = []
    for raw in (rel or "").replace("\\", "/").split("/"):
        level = " ".join(raw.split())
        if not level or level in (".", "..") or any(rx.match(level) for rx in _DROP):
            continue
        if out and _same(out[-1], level):
            continue
        if out and _YEAR.match(level) and _names_year(out[-1], level):
            continue
        out.append(level)
    return out


def tag_levels(rel: str | None) -> list[str]:
    return levels(rel)[:MAX_LEVELS]


def album_levels(rel: str | None) -> list[str]:
    """Levels for an album name: a pure year is appended to the level above."""
    out: list[str] = []
    for level in levels(rel):
        if out and _YEAR.match(level):
            merged = f"{out[-1]} {level}"
            if len(out) > 1 and _same(out[-2], merged):
                out.pop()
            else:
                out[-1] = merged
            continue
        if out and _same(out[-1], level):
            continue
        out.append(level)
    return out[:MAX_LEVELS]


def tag_path(rel: str | None) -> str | None:
    """Nested tag value for a relative folder, e.g. "Ordner/Kategorie/Ereignis"."""
    parts = tag_levels(rel)
    return "/".join([ROOT_TAG, *parts]) if parts else None


def album_name(rel: str | None) -> str | None:
    """Album name for a relative folder, e.g. "Kategorie Ereignis 2019"."""
    parts = album_levels(rel)
    return " ".join(parts) if parts else None


def is_tag_path(value) -> bool:
    """A nested folder tag (as opposed to a flat keyword)."""
    return isinstance(value, str) and value.startswith(ROOT_TAG + "/")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _ia02(job, ia02: dict | None = None) -> dict:
    if ia02 is not None:
        return ia02
    found = (getattr(job, "step_result", None) or {}).get("IA-02")
    return found if isinstance(found, dict) else {}


async def _from_ledger(job) -> str | None:
    """The relative folder from the first recorded operation that took the
    file from its inbox — for a job whose step results were reset (retry)
    after the file had left the inbox."""
    if not getattr(job, "id", None) or not getattr(job, "source_inbox_path", None):
        return None
    from sqlalchemy import select
    from database import async_session
    from models import FileOp
    inbox = os.path.abspath(job.source_inbox_path).rstrip(os.sep) + os.sep
    async with async_session() as session:
        sources = (await session.execute(
            select(FileOp.src).where(FileOp.job_id == job.id, FileOp.src.like(inbox + "%"))
            .order_by(FileOp.id).limit(5))).scalars().all()
    for src in sources:
        rel = relative_folder(src, job.source_inbox_path)
        if rel:
            return rel
    return None


async def folder_path_of(job, ia02: dict | None = None) -> str | None:
    """Raw relative folder of a job: IA-02's record, else where the file lies
    now, else the ledger."""
    recorded = _ia02(job, ia02).get("folder_path")
    if recorded:
        return recorded
    rel = relative_folder(getattr(job, "original_path", None), getattr(job, "source_inbox_path", None))
    if rel:
        return rel
    return await _from_ledger(job)


async def folder_options(job) -> FolderOptions:
    """The folder options of the job's set, read at runtime. Only a file
    that came from a directory has a folder structure."""
    if not getattr(job, "source_inbox_path", None):
        return FolderOptions()
    from database import async_session
    from sets import get_set
    async with async_session() as session:
        processing_set = await get_set(session, getattr(job, "set_id", None))
    if not processing_set:
        return FolderOptions()
    mode = processing_set.folder_tag_mode if processing_set.folder_tag_mode in TAG_MODES else "none"
    return FolderOptions(tag_mode=mode, albums=bool(processing_set.folder_albums))


def _add(items: list, value) -> None:
    if value and value not in items:
        items.append(value)


async def folder_targets(job, *, ia02: dict | None = None,
                         options: FolderOptions | None = None) -> FolderTargets:
    """Tags (flat or nested) and albums the job's file should carry.

    The job's own folder plus the folders of duplicates merged into it
    (``donor_folder_paths``), each per the set's options. Albums the merged
    duplicates' assets were in (``donor_albums``) come along whatever the
    options say: they are the user's albums, not derived from folders.
    Jobs from before the raw folder was recorded fall back to what the old
    code stored: its flat tags (``folder_tags``) and album name
    (``own_album``, else the last entry of ``folder_tags``).
    """
    record = _ia02(job, ia02)
    opts = options or await folder_options(job)
    out = FolderTargets()
    if opts.any:
        own = await folder_path_of(job, record)
        paths = [own, *(record.get("donor_folder_paths") or [])]
        for rel in paths:
            if opts.nested:
                _add(out.tag_paths, tag_path(rel))
            if opts.flat:
                for level in tag_levels(rel):
                    _add(out.flat_tags, level)
            if opts.albums:
                _add(out.albums, album_name(rel))
        if not own:
            legacy = record.get("folder_tags") or []
            if opts.flat:
                for word in legacy:
                    _add(out.flat_tags, word)
            if opts.albums:
                _add(out.albums, record.get("own_album") or (legacy[-1] if legacy else None))
    for name in record.get("donor_albums") or []:
        _add(out.albums, name)
    return out

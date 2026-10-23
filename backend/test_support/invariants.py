"""Reusable "no file lost" invariant for data-safety tests and E2E stories.

The invariant has two halves:

1. **Every job of a story still has at least one verified copy of its
   content.** A copy is a local file whose SHA-256 matches one of the job's
   known content hashes, or an Immich asset that exists, is not in the trash
   and, where a SHA-1 of the content is known, carries the matching checksum.
   A copy is *linked* when the job's own fields (``target_path``,
   ``original_path``, ``immich_asset_id`` / ``immich:<id>``) point at it.
   A copy that only turns up when scanning the story's directories is
   *unlinked*: the content survived, but MediaAssistant no longer knows where.

2. **Nothing outside the story's own paths was touched.** ``FileGuard``
   snapshots the watched roots before the story and diffs them afterwards
   (created / deleted / modified, directories included).

Known content hashes of a job, in this order of precedence:
``expected[job_id]`` passed by the caller, else ``job.file_hash`` (hash at
scan time) plus ``step_result["IA-07"]["file_hash"]`` (hash after a direct
metadata write) plus whatever ``FileGuard.register()`` recorded.

Example (an E2E story)::

    from test_support.invariants import FileGuard, check_no_file_lost

    guard = FileGuard(roots=["/library", "/inbox"], own=[inbox_file],
                      may_create=["/library"]).snapshot()
    guard.register(job_id, inbox_file)       # optional: sha256/sha1/size now
    ...                                      # run the story
    res = await check_no_file_lost([job_id], guard=guard)
    check("US-2 Invariante: keine Datei verloren", res.ok, res.summary())

The helper only reads. It opens one short DB session per call and closes it
before touching the disk or Immich, so it never holds a session while the app
or the worker might be waiting for the database.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
from dataclasses import dataclass, field

TRASH_DIRNAME = ".papierkorb"  # mirrors fileops.TRASH_DIRNAME (no app import at module load)


# ---------------------------------------------------------------------------
# Hashing and path helpers
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha1_b64_file(path: str) -> str:
    """Base64 SHA-1, the format of Immich's ``checksum`` field."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


def is_under(path: str, prefix: str) -> bool:
    """True if ``path`` is ``prefix`` itself or lies inside it (by containment, not by string length)."""
    path = os.path.normpath(path)
    prefix = os.path.normpath(prefix)
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


def _local(path: str | None) -> str | None:
    """A job path that names a local file, or None for immich:/immich-pending: references."""
    if not path or path.startswith("immich:") or path.startswith("immich-pending:"):
        return None
    return path


# ---------------------------------------------------------------------------
# FileGuard: what changed on disk during a story
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    kind: str      # "created" / "deleted" / "modified"
    path: str
    is_dir: bool

    def __str__(self) -> str:
        label = {"created": "neu", "deleted": "entfernt", "modified": "verändert"}[self.kind]
        return f"{label}: {self.path}{os.sep if self.is_dir else ''}"


class FileGuard:
    """Snapshot of the watched roots; reports changes outside the story's paths.

    ``own``        paths the story may change in any way (a file, or a
                   directory and everything below it).
    ``may_create`` paths below which the story may create new entries. An
                   entry that existed before and is changed or removed there
                   still counts as foreign — that is how an overwrite of
                   somebody else's library file shows up.
    ``ignore``     fnmatch patterns on the full path that are never reported.

    Other processes writing into the same roots show up as foreign changes
    too; watch roots the story owns where possible.
    """

    def __init__(self, roots, *, own=(), may_create=(), ignore=()):
        self.roots = [os.path.normpath(r) for r in roots]
        self.own = [os.path.normpath(p) for p in own]
        self.may_create = [os.path.normpath(p) for p in may_create]
        self.ignore = list(ignore)
        self.registered: dict[int, dict] = {}
        self._before: dict[str, tuple] | None = None

    def allow(self, *paths: str) -> "FileGuard":
        self.own.extend(os.path.normpath(p) for p in paths if p)
        return self

    def allow_create(self, *paths: str) -> "FileGuard":
        self.may_create.extend(os.path.normpath(p) for p in paths if p)
        return self

    def register(self, job_id: int, path: str) -> dict:
        """Remember the content of a job's file now (before the story moves it)."""
        info = {"path": path, "size": os.path.getsize(path),
                "sha256": sha256_file(path), "sha1": sha1_b64_file(path)}
        self.registered[job_id] = info
        return info

    @staticmethod
    def _scan(roots) -> dict[str, tuple]:
        entries: dict[str, tuple] = {}
        for root in roots:
            if os.path.isfile(root):
                st = os.lstat(root)
                entries[root] = ("f", st.st_size, st.st_mtime_ns, st.st_ino)
                continue
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                entries[dirpath] = ("d",)
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(p)
                    except FileNotFoundError:
                        continue
                    entries[p] = ("f", st.st_size, st.st_mtime_ns, st.st_ino)
        return entries

    def snapshot(self) -> "FileGuard":
        self._before = self._scan(self.roots)
        return self

    def changes(self) -> list[Change]:
        if self._before is None:
            raise RuntimeError("FileGuard.snapshot() must run before the story")
        after = self._scan(self.roots)
        out = []
        for p, e in self._before.items():
            if p not in after:
                out.append(Change("deleted", p, e[0] == "d"))
            elif after[p] != e:
                out.append(Change("modified", p, after[p][0] == "d"))
        for p, e in after.items():
            if p not in self._before:
                out.append(Change("created", p, e[0] == "d"))
        return sorted(out, key=lambda c: (c.path, c.kind))

    def foreign_changes(self) -> list[Change]:
        foreign = []
        for c in self.changes():
            if any(fnmatch.fnmatch(c.path, pat) for pat in self.ignore):
                continue
            if any(is_under(c.path, o) for o in self.own):
                continue
            if c.kind == "created" and any(is_under(c.path, m) for m in self.may_create):
                continue
            # fileops' trash (<base>/.papierkorb/…) is the sanctioned fallback:
            # a new entry there loses nothing. A foreign file moved into it
            # still shows up as "deleted" at the place it came from.
            if c.kind == "created" and TRASH_DIRNAME in c.path.split(os.sep):
                continue
            foreign.append(c)
        return foreign


# ---------------------------------------------------------------------------
# Per-job copy check
# ---------------------------------------------------------------------------

@dataclass
class JobCopies:
    job_id: int
    debug_key: str | None = None
    status: str | None = None
    expected: set = field(default_factory=set)
    linked: list = field(default_factory=list)
    unlinked: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    verdict: str = "lost"   # ok / unlinked / lost / row_missing / deleted_by_user

    def line(self) -> str:
        who = self.debug_key or f"job#{self.job_id}"
        text = {
            "ok": "verifizierte Kopie",
            "unlinked": "Inhalt nur noch UNVERKNÜPFT vorhanden",
            "lost": "KEINE verifizierte Kopie mehr",
            "row_missing": "Job-Zeile fehlt",
            "deleted_by_user": "vom Benutzer gelöscht (erlaubt)",
        }[self.verdict]
        parts = [f"{who} [{self.status}]: {text}"]
        if self.linked:
            parts.append("bei " + ", ".join(self.linked))
        if self.unlinked:
            parts.append("gefunden unter " + ", ".join(self.unlinked))
        if self.notes and self.verdict != "ok":
            parts.append("(" + "; ".join(self.notes) + ")")
        return " ".join(parts)


@dataclass
class InvariantResult:
    jobs: list
    foreign: list
    require_linked: bool = True

    def job(self, job_id: int) -> JobCopies | None:
        return next((j for j in self.jobs if j.job_id == job_id), None)

    @property
    def lost(self) -> list:
        allowed = {"ok", "deleted_by_user"} | (set() if self.require_linked else {"unlinked"})
        return [j for j in self.jobs if j.verdict not in allowed]

    @property
    def ok(self) -> bool:
        return not self.lost and not self.foreign

    def problems(self) -> list[str]:
        out = [j.line() for j in self.lost]
        out += [f"fremd {c}" for c in self.foreign]
        return out

    def summary(self) -> str:
        if self.ok:
            return "; ".join(j.line() for j in self.jobs) + "; nichts Fremdes angefasst"
        return "; ".join(self.problems())


class _HashIndex:
    """sha256 → paths for every file under the search roots, built lazily once."""

    def __init__(self, roots):
        self.roots = list(roots)
        self._index: dict[str, list[str]] | None = None

    def lookup(self, digest: str) -> list[str]:
        if self._index is None:
            self._index = {}
            for root in self.roots:
                for dirpath, _d, filenames in os.walk(root):
                    for name in filenames:
                        p = os.path.join(dirpath, name)
                        try:
                            self._index.setdefault(sha256_file(p), []).append(p)
                        except OSError:
                            continue
        return self._index.get(digest, [])


async def _immich_copy(job, asset_id: str, sha1s: set, direct_mode: bool) -> tuple[str | None, str | None]:
    """(linked label, note) for one Immich reference of a job."""
    from immich_client import fetch_asset_info, get_user_api_key
    api_key = await get_user_api_key(job["immich_user_id"]) if job["immich_user_id"] else None
    info, err = await fetch_asset_info(asset_id, api_key=api_key)
    if info is None:
        return None, f"immich:{asset_id}: {err or 'nicht gefunden'}"
    if info.get("isTrashed"):
        return None, f"immich:{asset_id}: liegt im Papierkorb"
    checksum = info.get("checksum")
    if sha1s and checksum in sha1s:
        return f"immich:{asset_id}", None
    if sha1s and not direct_mode:
        return None, f"immich:{asset_id}: Prüfsumme {checksum} passt nicht"
    # Direct mode rewrites the file before the upload, so the uploaded bytes
    # have a SHA-1 nobody recorded. Existence is all that can be checked.
    return f"immich:{asset_id} (Prüfsumme nicht prüfbar)", None


async def check_no_file_lost(job_ids, *, guard: FileGuard | None = None, search_roots=(),
                             expected: dict | None = None, require_linked: bool = True,
                             check_immich: bool = True, deleted_ok=()) -> InvariantResult:
    """Check both halves of the invariant for the given jobs.

    ``search_roots``   directories scanned for unlinked copies (by sha256).
    ``expected``       {job_id: {sha256, ...}} overrides the known hashes.
    ``require_linked`` an unlinked copy is a failure (default) or acceptable.
    ``check_immich``   verify immich:<id> / immich_asset_id against Immich.
    ``deleted_ok``     job ids the user deliberately deleted in the story.
    """
    from sqlalchemy import select
    from database import async_session
    from models import Job

    job_ids = list(job_ids)
    async with async_session() as session:
        rows = (await session.execute(select(Job).where(Job.id.in_(job_ids)))).scalars().all()
        views = {
            j.id: {
                "debug_key": j.debug_key, "status": j.status, "file_hash": j.file_hash,
                "target_path": j.target_path, "original_path": j.original_path,
                "immich_asset_id": j.immich_asset_id, "immich_user_id": j.immich_user_id,
                "step_result": dict(j.step_result or {}),
            }
            for j in rows
        }

    index = _HashIndex(search_roots)
    reports = []
    for jid in job_ids:
        view = views.get(jid)
        if view is None:
            reports.append(JobCopies(jid, verdict="deleted_by_user" if jid in deleted_ok else "row_missing"))
            continue
        ia07 = view["step_result"].get("IA-07") or {}
        reg = guard.registered.get(jid, {}) if guard else {}
        if expected and jid in expected:
            exp = set(expected[jid])
        else:
            exp = {view["file_hash"], ia07.get("file_hash"), reg.get("sha256")}
        exp.discard(None)
        exp.discard("")
        sha1s = {reg["sha1"]} if reg.get("sha1") else set()
        rep = JobCopies(jid, view["debug_key"], view["status"], exp)
        if not exp:
            rep.notes.append("kein Inhalts-Hash bekannt")

        seen = set()
        for p in (_local(view["target_path"]), _local(view["original_path"])):
            if not p or p in seen:
                continue
            seen.add(p)
            if not os.path.isfile(p):
                rep.notes.append(f"{p} fehlt")
                continue
            digest = sha256_file(p)
            if digest in exp:
                rep.linked.append(p)
            else:
                rep.notes.append(f"{p} hat fremden Inhalt (sha256 {digest[:12]}…)")

        asset_ids = []
        tp = view["target_path"] or ""
        if tp.startswith("immich:"):
            asset_ids.append(tp[len("immich:"):])
        if view["immich_asset_id"] and view["immich_asset_id"] not in asset_ids:
            asset_ids.append(view["immich_asset_id"])
        if check_immich:
            direct = ia07.get("write_mode") == "direct"
            for aid in asset_ids:
                label, note = await _immich_copy(view, aid, sha1s, direct)
                if label:
                    rep.linked.append(label)
                if note:
                    rep.notes.append(note)
        elif asset_ids:
            rep.notes.append("Immich nicht geprüft")

        if not rep.linked and search_roots:
            for digest in exp:
                rep.unlinked.extend(p for p in index.lookup(digest) if p not in rep.unlinked)

        if rep.linked:
            rep.verdict = "ok"
        elif jid in deleted_ok:
            rep.verdict = "deleted_by_user"
        elif rep.unlinked:
            rep.verdict = "unlinked"
        else:
            rep.verdict = "lost"
        reports.append(rep)

    foreign = guard.foreign_changes() if guard else []
    return InvariantResult(reports, foreign, require_linked)

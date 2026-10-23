"""Read-only view of a job for the web UI: module blocks, ledger, register.

Every template and script that shows a job's modules reads ``module_blocks()``
(directly, or through the JSON endpoints in routers/history.py) and never
``jobs.step_result`` itself. Step 3 of the rebuild (docs/UMBAU_MODULE.md,
table ``module_runs``) switches only this module from step_result to the runs;
the output stays the same:

    module_blocks(job, t) -> list of dicts, in module order (IA-09 only when it
    mailed something or failed, see step_visible):
        module        "IA-01" … "IA-11"
        label         translated module name (i18n "steps")
        status        ok | running | warning | error | skipped | duplicate |
                      dry_run | not_run   (later also queued, superseded)
        status_label  translated status
        tone, icon    design-system tone class (is-success …) and sprite icon
        detail        one short line (reason, target, counts), may be ""
        duration_s    seconds, None while unknown (step_result has no timing)
        attempts      number of runs, None while unknown

The ledger (file_ops) and register (file_copies, asset_refs) are only read
here; every change goes through fileops.py.
"""
import asyncio
import math
import os
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from database import async_session
from models import AssetRef, FileCopy, FileOp, Job

MODULES = tuple(f"IA-{n:02d}" for n in range(1, 12))
# A failure in these modules fails the job; elsewhere it only warns
# (pipeline/__init__.py: non_critical steps and the finalizers).
CRITICAL = frozenset({"IA-01", "IA-07", "IA-08"})
TRASH_RETENTION_DAYS = 30  # fileops.TRASH_RETENTION_DAYS; not imported to keep this module light

STATUS_TONES = {
    "ok": ("is-success", "check-circle"),
    "running": ("is-info", "loader"),
    "warning": ("is-warning", "alert-triangle"),
    "error": ("is-error", "alert-circle"),
    "skipped": ("is-neutral", "pause-circle"),
    "duplicate": ("is-warning", "copy"),
    "dry_run": ("is-info", "eye"),
    "not_run": ("is-neutral", "circle"),
    "queued": ("is-neutral", "clock"),
    "superseded": ("is-neutral", "circle"),
}

OP_ICONS = {
    "move": "arrow-right", "copy": "copy", "download": "cloud-down", "upload": "cloud-up", "transfer": "merge",
    "replace": "refresh", "write_metadata": "pencil", "trash": "trash", "restore": "retry",
    "purge": "x-circle", "lock": "lock",
}
OP_STATE_TONES = {"done": "is-success", "failed": "is-error", "intent": "is-info", "reconciled": "is-warning"}
COPY_STATE_TONES = {
    "present": "is-success", "trashed": "is-warning", "missing": "is-error",
    "deleted": "is-neutral", "moved": "is-neutral", "locked": "is-violet",
}
# Operations whose content must arrive unchanged; write_metadata and
# replace change it on purpose.
_SAME_CONTENT_OPS = frozenset({"move", "copy", "download", "upload", "trash", "restore"})
_DETAIL_MAX = 160


def _h(t: dict) -> dict:
    return (t or {}).get("history", {})


def _fmt(text: str, **values) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _first_line(value, limit: int = _DETAIL_MAX) -> str:
    if value is None:
        return ""
    text = str(value).strip().split("\n", 1)[0]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stamp(value: datetime | None, with_year: bool = True) -> str | None:
    if value is None:
        return None
    return value.strftime("%d.%m.%Y %H:%M:%S" if with_year else "%d.%m. %H:%M")


def short_location(kind: str | None, location: str | None) -> str:
    """Asset ids and paths as people read them: the asset id, or the path."""
    if not location:
        return ""
    if location.startswith("immich:"):
        return location[len("immich:"):]
    return location


# ---------------------------------------------------------------------------
# Module blocks
# ---------------------------------------------------------------------------

def _status_of(job, code: str, results: dict) -> str:
    if job.status == "processing" and job.current_step == code:
        return "running"
    if code not in results:
        return "not_run"
    result = results[code]
    if not isinstance(result, dict):
        return "ok"
    st = result.get("status")
    if st == "error":
        return "error" if code in CRITICAL else "warning"
    if st in ("warning", "skipped", "duplicate", "dry_run"):
        return st
    if result.get("_skipped"):
        return "skipped"
    return "ok"


def _ok_detail(code: str, r: dict, h: dict) -> str:
    if code == "IA-01":
        parts = [r.get("file_type") or ""]
        if r.get("width") and r.get("height"):
            parts.append(f"{r['width']} × {r['height']}")
        if r.get("gps"):
            parts.append("GPS")
        return " · ".join(p for p in parts if p)
    if code == "IA-02":
        return h.get("detail_no_duplicate", "")
    if code == "IA-03":
        place = ", ".join(p for p in (r.get("city"), r.get("country")) if p)
        return place or _first_line(r.get("display_name"))
    if code == "IA-04":
        if r.get("converted"):
            return h.get("detail_converted", "")
        return _first_line(r.get("reason"))
    if code == "IA-05":
        tags = r.get("tags") if isinstance(r.get("tags"), list) else []
        parts = [r.get("type") or ""]
        if tags:
            parts.append(_fmt(h.get("detail_tags", "{n}"), n=len(tags)))
        return " · ".join(p for p in parts if p)
    if code == "IA-06":
        if "has_text" in r or "text" in r:
            return h.get("detail_text_found" if (r.get("has_text") or r.get("text")) else "detail_no_text", "")
        return ""
    if code == "IA-07":
        n = r.get("tags_count")
        if n is None and isinstance(r.get("keywords_written"), list):
            n = len(r["keywords_written"])
        return _fmt(h.get("detail_tags_written", "{n}"), n=n) if n is not None else ""
    if code == "IA-08":
        target = r.get("target_path") or ""
        if target.startswith("immich:"):
            return _fmt(h.get("detail_immich", "{id}"), id=target[len("immich:"):][:8] + "…")
        return target
    if code == "IA-10":
        n = r.get("count")
        if n is None and isinstance(r.get("removed"), list):
            n = len(r["removed"])
        if not n:
            return h.get("detail_nothing", "")
        return h.get("detail_removed_one", "") if n == 1 else _fmt(h.get("detail_removed", "{n}"), n=n)
    if code == "IA-11":
        return _first_line(r.get("summary"))
    return _first_line(r.get("reason"))


def _detail_of(code: str, status: str, result, h: dict) -> str:
    if status == "running":
        return h.get("detail_running", "")
    if status == "not_run" or not isinstance(result, dict):
        return ""
    if status in ("error", "warning", "skipped"):
        return _first_line(result.get("reason") or result.get("_reason") or result.get("error")
                           or result.get("note"))
    if status == "duplicate":
        key = result.get("original_debug_key")
        return _fmt(h.get("detail_duplicate_of", "{key}"), key=key) if key else _first_line(result.get("note"))
    if status == "dry_run":
        target = result.get("target_path")
        if target:
            return _fmt(h.get("detail_planned", "{target}"), target=target)
        planned = result.get("keywords_planned")
        if isinstance(planned, list):
            return _fmt(h.get("detail_tags", "{n}"), n=len(planned))
        return ""
    return _ok_detail(code, result, h)


def step_visible(code: str, result) -> bool:
    """Whether a module belongs in the workflow view of a job.

    IA-09 only mails when a job failed; in every other run it reports
    "module disabled" / "no errors to report", which is noise in the
    workflow. It shows up only when it sent something or failed itself.
    """
    if code != "IA-09":
        return True
    return isinstance(result, dict) and (bool(result.get("sent")) or result.get("status") == "error")


def module_blocks(job, t: dict | None = None) -> list[dict]:
    """The modules of `job` as blocks (see the module docstring)."""
    t = t or {}
    h, steps = _h(t), t.get("steps", {})
    results = job.step_result if isinstance(job.step_result, dict) else {}
    blocks = []
    for code in MODULES:
        if not step_visible(code, results.get(code)):
            continue
        status = _status_of(job, code, results)
        tone, icon = STATUS_TONES[status]
        blocks.append({
            "module": code,
            "label": steps.get(code, code),
            "status": status,
            "status_label": h.get(f"status_{status}", status),
            "tone": tone,
            "icon": icon,
            "detail": _detail_of(code, status, results.get(code), h),
            "duration_s": None,
            "attempts": None,
        })
    return blocks


def strip_summary(blocks: list[dict], t: dict | None = None) -> str:
    """One line for screen readers: "Module: 8 OK, 1 Warnung, 2 nicht gelaufen"."""
    h = _h(t)
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["status_label"]] = counts.get(b["status_label"], 0) + 1
    summary = ", ".join(f"{n} {label}" for label, n in counts.items())
    return _fmt(h.get("strip_label", "{summary}"), summary=summary)


def job_signature(job) -> str:
    """Changes whenever the blocks of a job can change (live refresh cache)."""
    return f"{job.status}|{job.current_step}|{job.updated_at.isoformat() if job.updated_at else ''}"


# ---------------------------------------------------------------------------
# Ledger and register
# ---------------------------------------------------------------------------

def _actor_label(actor: str | None, t: dict) -> str:
    if not actor:
        return ""
    h, steps = _h(t), t.get("steps", {})
    if actor in steps:
        return f"{actor} {steps[actor]}"
    kind, _, name = actor.partition(":")
    if name:
        known = h.get(f"actor_{kind}_{name.replace('-', '_')}")
        if known:
            return known
        return f"{h.get(f'actor_{kind}', kind)} · {name}"
    return actor


def _kind_label(kind: str | None, t: dict) -> str:
    return _h(t).get(f"kind_{kind}", kind or "") if kind else ""


def _hash_state(op: FileOp) -> str | None:
    if op.state != "done":
        return None
    if op.op in _SAME_CONTENT_OPS:
        if op.sha256_after and (op.sha256_before is None or op.sha256_before == op.sha256_after):
            return "verified"
        if op.op in ("upload", "restore", "trash") and op.dst:
            # Immich operations check the asset's checksum, not a local hash
            return "verified" if op.dst_kind in ("immich", "immich_trash") else None
        return None
    if op.op in ("write_metadata", "replace") and op.sha256_before and op.sha256_after \
            and op.sha256_before != op.sha256_after:
        return "changed"
    return None


def _op_note(op: FileOp) -> str:
    detail = op.detail if isinstance(op.detail, dict) else {}
    parts = []
    if detail.get("observed"):
        parts.append(str(detail["observed"]))
    notes = detail.get("notes")
    if isinstance(notes, list):
        parts.extend(str(n) for n in notes)
    return _first_line("; ".join(parts), 240)


def op_entry(op: FileOp, t: dict) -> dict:
    h = _h(t)
    hash_state = _hash_state(op)
    return {
        "id": op.id,
        "op": op.op,
        "op_label": h.get(f"op_{op.op}", op.op),
        "icon": OP_ICONS.get(op.op, "file"),
        "state": op.state,
        "state_label": h.get(f"state_{op.state}", op.state),
        "tone": OP_STATE_TONES.get(op.state, "is-neutral"),
        "src": short_location(op.src_kind, op.src),
        "src_kind": op.src_kind,
        "src_kind_label": _kind_label(op.src_kind, t),
        "dst": short_location(op.dst_kind, op.dst),
        "dst_kind": op.dst_kind,
        "dst_kind_label": _kind_label(op.dst_kind, t),
        "hash": hash_state,
        "hash_label": h.get(f"hash_{hash_state}", "") if hash_state else "",
        "sha256": (op.sha256_after or op.sha256_before or "")[:12],
        "actor": op.actor,
        "actor_label": _actor_label(op.actor, t),
        "reason": _first_line(op.reason, 240),
        "error": _first_line(op.error, 400),
        "note": _op_note(op),
        "created_at": stamp(op.created_at),
        "finished_at": stamp(op.finished_at),
    }


def remaining_days(trashed_at: datetime | None, retention_days: int = TRASH_RETENTION_DAYS,
                   now: datetime | None = None) -> int | None:
    """Whole days until a trashed copy is emptied; 0 = at the next run."""
    if trashed_at is None:
        return None
    left = (trashed_at + timedelta(days=retention_days) - (now or datetime.now())).total_seconds()
    return math.ceil(left / 86400) if left > 0 else 0


def remaining_label(days: int | None, t: dict) -> str:
    h = _h(t)
    if days is None:
        return ""
    if days <= 0:
        return h.get("remaining_today", "")
    if days == 1:
        return h.get("remaining_one", "")
    return _fmt(h.get("remaining_days", "{n}"), n=days)


def copy_entry(row: FileCopy, t: dict, *, owner_key: str | None = None, shared: bool = False,
               immich_trash_days: int = TRASH_RETENTION_DAYS) -> dict:
    h = _h(t)
    days = None
    if row.state == "trashed":
        days = remaining_days(row.trashed_at, immich_trash_days if row.kind == "immich" else TRASH_RETENTION_DAYS)
    return {
        "id": row.id,
        "kind": row.kind,
        "kind_label": _kind_label(row.kind, t),
        "location": row.location,
        "name": os.path.basename(row.location) if row.kind != "immich" else row.location,
        "trash_location": row.trash_location,
        "state": row.state,
        "state_label": h.get(f"copy_{row.state}", row.state),
        "tone": COPY_STATE_TONES.get(row.state, "is-neutral"),
        "sha256": (row.sha256 or "")[:12],
        "size": row.size,
        "verified_at": stamp(row.verified_at),
        "trashed_at": stamp(row.trashed_at),
        "remaining_days": days,
        "remaining_label": remaining_label(days, t) if row.state == "trashed" else "",
        "can_restore": row.state == "trashed",
        "shared": shared,
        "owner_key": owner_key,
    }


async def job_ledger(job_id: int, t: dict, limit: int = 200) -> list[dict]:
    """The job's ledger rows, newest first."""
    async with async_session() as session:
        rows = (await session.execute(
            select(FileOp).where(FileOp.job_id == job_id).order_by(FileOp.id.desc()).limit(limit)
        )).scalars().all()
    return [op_entry(op, t) for op in rows]


async def job_copies(job_id: int, t: dict, immich_trash_days: int = TRASH_RETENTION_DAYS) -> list[dict]:
    """Copies the job owns plus the shared ones it references; present first."""
    async with async_session() as session:
        own = (await session.execute(
            select(FileCopy).where(FileCopy.job_id == job_id).order_by(FileCopy.id)
        )).scalars().all()
        refs = (await session.execute(
            select(AssetRef.location).where(AssetRef.job_id == job_id))).scalars().all()
        shared = []
        if refs:
            shared = (await session.execute(
                select(FileCopy).where(FileCopy.location.in_(refs), FileCopy.state == "present",
                                       or_(FileCopy.job_id.is_(None), FileCopy.job_id != job_id))
            )).scalars().all()
        owners = {}
        owner_ids = {c.job_id for c in shared if c.job_id is not None}
        if owner_ids:
            owners = dict((await session.execute(
                select(Job.id, Job.debug_key).where(Job.id.in_(owner_ids)))).all())
    order = {"present": 0, "locked": 1, "trashed": 2, "missing": 3, "moved": 4, "deleted": 5}
    entries = [copy_entry(c, t, immich_trash_days=immich_trash_days) for c in own]
    entries += [copy_entry(c, t, owner_key=owners.get(c.job_id), shared=True,
                           immich_trash_days=immich_trash_days) for c in shared]
    entries.sort(key=lambda e: (order.get(e["state"], 9), e["id"]))
    return entries


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

async def trash_counts() -> dict:
    async with async_session() as session:
        rows = (await session.execute(
            select(FileCopy.kind == "immich", func.count()).where(FileCopy.state == "trashed")
            .group_by(FileCopy.kind == "immich"))).all()
    counts = {bool(is_immich): n for is_immich, n in rows}
    local, immich = counts.get(False, 0), counts.get(True, 0)
    return {"local": local, "immich": immich, "total": local + immich}


async def trash_entries(t: dict, *, kind: str | None = None, offset: int = 0, limit: int = 50,
                        immich_trash_days: int = TRASH_RETENTION_DAYS) -> tuple[list[dict], int]:
    """Trashed copies, newest first, with job and reason (from the ledger)."""
    h = _h(t)
    query = select(FileCopy).where(FileCopy.state == "trashed")
    if kind == "immich":
        query = query.where(FileCopy.kind == "immich")
    elif kind == "local":
        query = query.where(FileCopy.kind != "immich")
    async with async_session() as session:
        total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
        rows = (await session.execute(
            query.order_by(FileCopy.trashed_at.desc(), FileCopy.id.desc()).offset(offset).limit(limit)
        )).scalars().all()
        ids = [r.id for r in rows]
        job_ids = {r.job_id for r in rows if r.job_id is not None}
        jobs = {}
        if job_ids:
            jobs = {j.id: j for j in (await session.execute(
                select(Job.id, Job.debug_key, Job.filename).where(Job.id.in_(job_ids)))).all()}
        reasons: dict[int, FileOp] = {}
        if ids:
            ops = (await session.execute(
                select(FileOp).where(FileOp.op == "trash", FileOp.state == "done",
                                     or_(FileOp.dst_copy_id.in_(ids), FileOp.copy_id.in_(ids)))
                .order_by(FileOp.id)
            )).scalars().all()
            for op in ops:
                for cid in (op.dst_copy_id, op.copy_id):
                    if cid in ids:
                        reasons[cid] = op
    entries = []
    for row in rows:
        entry = copy_entry(row, t, immich_trash_days=immich_trash_days)
        job = jobs.get(row.job_id)
        op = reasons.get(row.id)
        entry.update({
            "debug_key": job.debug_key if job else None,
            "filename": (job.filename if job else None) or entry["name"],
            "reason": _first_line(op.reason, 240) if op else h.get("trash_reason_unknown", ""),
            "actor_label": _actor_label(op.actor, t) if op else "",
            "op_id": op.id if op else None,
        })
        entries.append(entry)
    return entries, total


# ---------------------------------------------------------------------------
# Inventory check helpers (read only)
# ---------------------------------------------------------------------------

async def copies_to_check(copy_ids: list[int] | None = None) -> list[dict]:
    """Copies the inventory check verifies: present and missing ones (a
    missing copy may have come back), with the state of their job."""
    query = (select(FileCopy.id, FileCopy.kind, FileCopy.location, FileCopy.state,
                    FileCopy.trash_location, FileCopy.job_id, Job.status, Job.debug_key, Job.filename)
             .outerjoin(Job, Job.id == FileCopy.job_id)
             .where(FileCopy.state.in_(("present", "missing")))
             .order_by(FileCopy.id))
    if copy_ids is not None:
        query = query.where(FileCopy.id.in_(copy_ids))
    async with async_session() as session:
        rows = (await session.execute(query)).all()
    return [dict(id=r.id, kind=r.kind, location=r.location, state=r.state, job_id=r.job_id,
                 job_status=r.status, debug_key=r.debug_key, filename=r.filename) for r in rows]


async def open_intent_sources() -> set[str]:
    """Locations with an operation in flight; the check leaves them alone."""
    async with async_session() as session:
        rows = (await session.execute(
            select(FileOp.src, FileOp.dst).where(FileOp.state == "intent"))).all()
    return {v for row in rows for v in row if v}


async def copy_state(copy_id: int) -> str | None:
    async with async_session() as session:
        row = await session.get(FileCopy, copy_id)
        return row.state if row else None


async def unregistered_files(directory: str, limit: int = 500) -> tuple[list[dict], int, int]:
    """Files in `directory` (reprocess/) that the register does not list as
    present. Each is matched to a job by original_path where possible.
    Returns (entries up to limit, number without a job, number with a job)."""
    def _scan():
        found = []
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith(".fops-"):
                    continue
                found.append(os.path.join(root, name))
        return found

    if not os.path.isdir(directory):
        return [], 0, 0
    paths = await asyncio.to_thread(_scan)
    if not paths:
        return [], 0, 0
    async with async_session() as session:
        registered = set((await session.execute(
            select(FileCopy.location).where(FileCopy.state == "present",
                                            FileCopy.location.like(directory.rstrip("/") + "/%"))
        )).scalars().all())
        loose = [p for p in paths if p not in registered]
        owners: dict[str, tuple] = {}
        for i in range(0, len(loose), 500):
            chunk = loose[i:i + 500]
            for path, key, filename in (await session.execute(
                    select(Job.original_path, Job.debug_key, Job.filename)
                    .where(Job.original_path.in_(chunk)))).all():
                owners.setdefault(path, (key, filename))
    entries, orphans, assigned = [], 0, 0
    for path in sorted(loose):
        owner = owners.get(path)
        if owner:
            assigned += 1
        else:
            orphans += 1
        if len(entries) < limit:
            entries.append({"location": path, "debug_key": owner[0] if owner else None,
                            "filename": owner[1] if owner else os.path.basename(path)})
    return entries, orphans, assigned

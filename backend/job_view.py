"""Read-only view of a job for the web UI: module blocks, ledger, register.

Every template and script that shows a job's modules reads ``module_blocks()``
(directly, or through the JSON endpoints in routers/history.py) and never
``jobs.step_result`` itself. With the job's module runs (``job_runs()``,
table ``module_runs``) the blocks come from the runs; without them (the
server-rendered strip and bar) from the derived view step_result, which
gives the same status and detail:

    module_blocks(job, t, runs=None) -> list of dicts, in module order (IA-09
    only when it mailed something or failed, see step_visible):
        module        "IA-01" … "IA-11"
        label         translated module name (i18n "steps")
        status        ok | running | warning | error | skipped | duplicate |
                      dry_run | not_run | queued
        status_label  translated status
        tone, icon    design-system tone class (is-success …) and sprite icon
        detail        one short line (reason, target, counts), may be ""
        duration_s    seconds of the current run, None while unknown
        attempts      number of runs, None without runs
        trigger, trigger_label   how the current run came about (None without runs)
        earlier       the earlier attempts, oldest first (empty without runs):
                      attempt, trigger(_label), status(_label), tone, icon —
                      the outcome the attempt had —, started_at, finished_at,
                      duration_s, error (first line), copied_from_run_id

The ledger (file_ops) and register (file_copies, asset_refs) are only read
here; every change goes through fileops.py.

The timeline of a job (expanded rows, job page) starts with job_intake():
where and when the content was taken in, from the ledger row 'register'
the inputs write since it exists, reconstructed from the job for older
jobs. Each ledger entry says whether it is housekeeping (is_housekeeping:
automatic tidying up that the timeline folds away). job_labels() lists the
tags and albums MA wrote.
"""
import asyncio
import math
import os
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from sqlalchemy import func, or_, select

from database import async_session
from models import AssetRef, FileCopy, FileOp, Job, ModuleRun, ProcessingSet, RUN_RESULT_STATUSES

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
    "interrupted": ("is-neutral", "x-circle"),
}

OP_ICONS = {
    "move": "arrow-right", "copy": "copy", "download": "cloud-down", "upload": "cloud-up", "transfer": "merge",
    "replace": "refresh", "write_metadata": "pencil", "trash": "trash", "restore": "retry",
    "purge": "x-circle", "lock": "lock", "register": "inbox",
}
# Housekeeping: what modules and the system tidy up on their own once the
# content is safe elsewhere — the source after a verified upload, discarded
# work copies and temp files, the sidecar handed over to Immich, old error
# logs, the trash emptied after its retention. The timeline folds it away.
# Everything a user triggered (user:…), the maintenance buttons
# (system:cleanup_…) and every failed or reconciled operation stay in view.
_HOUSEKEEPING_OPS = frozenset({"trash", "purge"})
_HOUSEKEEPING_SYSTEM = frozenset({"system:immich-input", "system:papierkorb", "system:reprocess",
                                  "system:recovery", "system:resume", "system:csv-retry"})
INTAKE_WINDOW_S = 120  # a register row this close to the job's creation was written at intake
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


def _result_tone(code: str, run_status: str) -> str:
    """Block status of a finished run: a failure outside the critical
    modules only warns, like in the pipeline."""
    if run_status == "error":
        return "error" if code in CRITICAL else "warning"
    return run_status if run_status in STATUS_TONES else "ok"


def _duration(run) -> float | None:
    if run is None or run.started_at is None or run.finished_at is None:
        return None
    return round(max((run.finished_at - run.started_at).total_seconds(), 0.0), 1)


def _outcome(code: str, run) -> str:
    """What an attempt came to: the status its result stands for, also once
    superseded; 'interrupted' for one superseded while it ran."""
    if run.status in RUN_RESULT_STATUSES:
        return _result_tone(code, run.status)
    if run.status != "superseded":
        return run.status  # queued, running
    if run.result is None:
        return "interrupted" if run.started_at is not None else "not_run"
    import runs as _runs  # runs.py writes; only its status rule is used here
    return _result_tone(code, _runs.result_status(run.result))


def _attempt_entry(code: str, run, h: dict) -> dict:
    status = _outcome(code, run)
    tone, icon = STATUS_TONES[status]
    return {
        "id": run.id,
        "attempt": run.attempt,
        "trigger": run.trigger,
        "trigger_label": h.get(f"trigger_{run.trigger}", run.trigger),
        "status": status,
        "status_label": h.get(f"status_{status}", status),
        "tone": tone,
        "icon": icon,
        "superseded": run.status == "superseded",
        "started_at": stamp(run.started_at),
        "finished_at": stamp(run.finished_at),
        "duration_s": _duration(run),
        "error": _first_line(run.error, 240),
        "copied_from_run_id": run.copied_from_run_id,
    }


def _block(code: str, status: str, result, h: dict, steps: dict) -> dict:
    tone, icon = STATUS_TONES[status]
    return {
        "module": code,
        "label": steps.get(code, code),
        "status": status,
        "status_label": h.get(f"status_{status}", status),
        "tone": tone,
        "icon": icon,
        "detail": _detail_of(code, status, result, h),
        "duration_s": None,
        "attempts": None,
        "trigger": None,
        "trigger_label": None,
        "earlier": [],
    }


def module_blocks(job, t: dict | None = None, runs=None) -> list[dict]:
    """The modules of `job` as blocks (see the module docstring); `runs`:
    all module runs of the job (job_runs), else the step_result view."""
    t = t or {}
    h, steps = _h(t), t.get("steps", {})
    if runs is None:
        results = job.step_result if isinstance(job.step_result, dict) else {}
        return [_block(code, _status_of(job, code, results), results.get(code), h, steps)
                for code in MODULES if step_visible(code, results.get(code))]

    by_module: dict[str, list] = {}
    for run in sorted(runs, key=lambda r: r.id):
        by_module.setdefault(run.module, []).append(run)
    blocks = []
    for code in MODULES:
        mod_runs = by_module.get(code, [])
        current = next((r for r in reversed(mod_runs) if r.status != "superseded"), None)
        finished = current is not None and current.status in RUN_RESULT_STATUSES
        result = current.result if finished else None
        if not step_visible(code, result):
            continue
        if current is None:
            status = "running" if job.status == "processing" and job.current_step == code else "not_run"
        elif finished:
            status = _result_tone(code, current.status)
        else:
            status = current.status  # running or queued
        block = _block(code, status, result, h, steps)
        block.update(
            duration_s=_duration(current) if finished else None,
            attempts=len(mod_runs),
            trigger=current.trigger if current is not None else None,
            trigger_label=h.get(f"trigger_{current.trigger}", current.trigger) if current is not None else None,
            earlier=[_attempt_entry(code, r, h) for r in mod_runs if r is not current],
        )
        blocks.append(block)
    return blocks


async def job_runs(job_ids) -> dict[int, list]:
    """{job id: its module runs, oldest first} — read only."""
    ids = list(dict.fromkeys(job_ids))
    out: dict[int, list] = {job_id: [] for job_id in ids}
    if not ids:
        return out
    async with async_session() as session:
        for i in range(0, len(ids), 500):
            rows = (await session.execute(
                select(ModuleRun).where(ModuleRun.job_id.in_(ids[i:i + 500])).order_by(ModuleRun.id)
            )).scalars().all()
            for run in rows:
                out[run.job_id].append(run)
    return out


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


def is_housekeeping(op: FileOp) -> bool:
    """Automatic tidying up by a module or the system (see _HOUSEKEEPING_OPS)."""
    if op.state != "done" or op.op not in _HOUSEKEEPING_OPS:
        return False
    actor = op.actor or ""
    return actor in MODULES or actor in _HOUSEKEEPING_SYSTEM


def op_entry(op: FileOp, t: dict) -> dict:
    h = _h(t)
    hash_state = _hash_state(op)
    return {
        "id": op.id,
        "op": op.op,
        "housekeeping": is_housekeeping(op),
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
    """The job's ledger rows, newest first. The intake row ('register') is
    not among them: job_intake() shows it at the start of the timeline."""
    async with async_session() as session:
        rows = (await session.execute(
            select(FileOp).where(FileOp.job_id == job_id, FileOp.op != "register")
            .order_by(FileOp.id.desc()).limit(limit)
        )).scalars().all()
    return [op_entry(op, t) for op in rows]


def _intake_source(job, actor: str | None) -> str:
    if actor == "input:immich" or (actor is None and job.source_label == "Immich"):
        return "immich"
    if actor == "input:inbox" or (actor is None and job.source_inbox_path):
        return "inbox"
    return "other"


def _under(path: str | None, root: str | None) -> bool:
    if not path or not root:
        return False
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


async def job_intake(job, t: dict) -> dict:
    """The first entry of the timeline: when, from which input and set, where
    and with which hash the job's content was taken in.

    From the ledger row 'register' the inputs write (fileops.register_existing
    with intake=…). A job older than that row is reconstructed from its own
    fields: created_at, source_label, set, and the earliest register row of
    its content where it came in (a file in its inbox, its Immich asset),
    else the first operation on it; ``reconstructed`` says so."""
    h = _h(t)
    async with async_session() as session:
        op = (await session.execute(
            select(FileOp).where(FileOp.job_id == job.id, FileOp.op == "register")
            .order_by(FileOp.id).limit(1))).scalar()
        set_name = (await session.execute(
            select(ProcessingSet.name).where(ProcessingSet.id == job.set_id))).scalar() if job.set_id else None
        copies, ops = [], []
        if op is None:
            copies = (await session.execute(
                select(FileCopy).where(FileCopy.job_id == job.id, FileCopy.kind.in_(("local", "immich")))
                .order_by(FileCopy.id).limit(50))).scalars().all()
            ops = (await session.execute(
                select(FileOp).where(FileOp.job_id == job.id, FileOp.src.isnot(None))
                .order_by(FileOp.id).limit(50))).scalars().all()

    source = _intake_source(job, op.actor if op else None)
    kind = location = sha = checksum = None
    if op is not None:
        kind, location, sha = op.dst_kind, op.dst, op.sha256_after
        checksum = (op.detail or {}).get("checksum") if isinstance(op.detail, dict) else None
        when = op.created_at
    else:
        when = job.created_at

        def taken_in(kind_, location_, created=None) -> bool:
            if source == "immich":
                return kind_ == "immich"
            if source == "inbox":
                return kind_ == "local" and _under(location_, job.source_inbox_path)
            return (kind_ in ("local", "immich") and created is not None and job.created_at is not None
                    and abs((created - job.created_at).total_seconds()) <= INTAKE_WINDOW_S)

        first_copy = next((c for c in copies if taken_in(c.kind, c.location, c.created_at)), None)
        first_op = next((o for o in ops if taken_in(o.src_kind, o.src)), None)
        if first_copy is not None:
            kind, location, sha, checksum = first_copy.kind, first_copy.location, first_copy.sha256, first_copy.checksum
        elif first_op is not None:
            kind, location, sha = first_op.src_kind, first_op.src, first_op.sha256_before
        elif source == "inbox" and _under(job.original_path, job.source_inbox_path):
            kind, location = "local", job.original_path
        elif source == "immich":
            r8 = (job.step_result or {}).get("IA-08") if isinstance(job.step_result, dict) else None
            replaced = r8.get("immich_replaced_asset") if isinstance(r8, dict) else None
            if replaced or job.immich_asset_id:
                kind, location = "immich", replaced or job.immich_asset_id
        sha = sha or job.file_hash
    if kind == "immich" and not sha and checksum:
        hash_label, hash_value = h.get("intake_checksum", ""), checksum[:12]
    else:
        hash_label, hash_value = "SHA256", (sha or "")[:12]
    source_label = h.get(f"intake_source_{source}", "") if source != "other" else (job.source_label or "")
    return {
        "op": "register",
        "title": h.get("op_register", "register"),
        "source": source,
        "source_label": source_label,
        "icon": {"immich": "cloud", "inbox": "inbox"}.get(source, "file"),
        "input_name": job.source_label if source == "inbox" else None,
        "set_name": set_name,
        "location": short_location(kind, location),
        "location_kind": kind,
        "location_kind_label": _kind_label(kind, t),
        "hash": hash_value,
        "hash_label": hash_label if hash_value else "",
        "actor_label": _actor_label(op.actor, t) if op else "",
        "created_at": stamp(when),
        "reconstructed": op is None,
        "reconstructed_label": h.get("intake_reconstructed", "") if op is None else "",
        "reconstructed_note": h.get("intake_reconstructed_note", "") if op is None else "",
    }


def job_labels(job, t: dict | None = None) -> dict:
    """Tags and albums MA wrote for the job.

    Immich target: the tags IA-08 set on the asset (immich_tags_written) and
    the albums it added the asset to; ``created`` marks an album IA-08 had to
    create. Otherwise the keywords IA-07 wrote into the file or sidecar. A
    dry run shows the planned keywords (``planned``)."""
    h = _h(t)
    results = job.step_result if isinstance(job.step_result, dict) else {}
    r7 = results.get("IA-07") if isinstance(results.get("IA-07"), dict) else {}
    r8 = results.get("IA-08") if isinstance(results.get("IA-08"), dict) else {}
    immich = (job.target_path or "").startswith("immich:") or "immich_tags_written" in r8
    tags = r8.get("immich_tags_written") if immich else r7.get("keywords_written")
    planned = False
    if not tags and isinstance(r7.get("keywords_planned"), list):
        tags, planned = r7["keywords_planned"], True
    tags = [str(x) for x in (tags if isinstance(tags, list) else []) if x]
    created = set(r8.get("immich_albums_created") or []) if isinstance(r8.get("immich_albums_created"), list) else set()
    added = r8.get("immich_albums_added") if isinstance(r8.get("immich_albums_added"), list) else []
    albums = [{"name": str(a), "created": a in created} for a in added if a]
    where = "immich" if immich else "file"
    return {
        "where": where,
        "tags": tags,
        "tags_title": h.get(f"labels_tags_{where}", ""),
        "planned": planned,
        "planned_label": h.get("labels_planned", "") if planned else "",
        "albums": albums,
    }


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


# ---------------------------------------------------------------------------
# Back link of the job page
# ---------------------------------------------------------------------------

BACK_MAX = 1500
_JOB_PAGE = re.compile(r"^/logs/job/([A-Za-z0-9_.-]+)$")
_LOGS_TABS = {"system": "system_log", "jobs": "job_log", "dryrun": "dryrun_tab"}
_TOOL_PAGES = {"/tools/papierkorb": ("history", "trash_title"), "/tools/bestand": ("history", "inv_title"),
               "/tools": ("nav", "tools")}
_NAV_PAGES = {"/": "dashboard", "/duplicates": "duplicates", "/review": "review"}


def _back_label(path: str, query: str, t: dict) -> str | None:
    """Label of a page the job page may lead back to; None for any other."""
    t = t or {}
    if path in _NAV_PAGES:
        return t.get("nav", {}).get(_NAV_PAGES[path], "")
    if path in _TOOL_PAGES:
        section, key = _TOOL_PAGES[path]
        return t.get(section, {}).get(key, "")
    if path == "/logs":
        tab = (parse_qs(query).get("tab") or ["system"])[0]
        return t.get("logs", {}).get(_LOGS_TABS[tab], "") if tab in _LOGS_TABS else None
    match = _JOB_PAGE.match(path)
    return match.group(1) if match else None


def safe_back(value: str | None, debug_key: str | None = None, t: dict | None = None) -> str | None:
    """`value` as the local path+query the job page of `debug_key` may link
    back to, or None. Only pages of this app that lead to jobs qualify, and
    never the job's own page: no scheme, no host, no protocol-relative
    "//host" or "/\\host", nothing else."""
    if not value or len(value) > BACK_MAX or not value.startswith("/") or value.startswith("//"):
        return None
    if any(c in value for c in "\\\r\n\t\0"):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    if debug_key and path == f"/logs/job/{debug_key}":
        return None
    if _back_label(path, parts.query, t or {}) is None:
        return None
    return parts.path + ("?" + parts.query if parts.query else "")


def back_link(job_key: str, *, back: str | None, referer: str | None, host: str | None,
              list_params: dict | None = None, t: dict | None = None) -> dict:
    """Where the back link of a job page goes, and its label.

    1. ``back`` (query parameter or form field), 2. the Referer — same host
    only, path and query; a Referer that is this job page itself passes on
    its own ``back``. 3. Otherwise the job log with the list filters it was
    opened with (``list_params``: page, status, q)."""
    t = t or {}
    target = safe_back(back, job_key, t)
    if target is None and referer:
        ref = urlsplit(referer)
        if ref.scheme in ("http", "https") and host and ref.netloc.lower() == host.lower():
            if unquote(ref.path) == f"/logs/job/{job_key}":
                target = safe_back((parse_qs(ref.query).get("back") or [None])[0], job_key, t)
            else:
                target = safe_back(ref.path + ("?" + ref.query if ref.query else ""), job_key, t)
    if target is not None:
        parts = urlsplit(target)
        return {"url": target, "label": _back_label(unquote(parts.path), parts.query, t), "explicit": True}
    params = {"tab": "jobs"}
    for key in ("page", "status", "q"):
        value = (list_params or {}).get(key)
        if value:
            params[key] = value
    return {"url": "/logs?" + urlencode(params), "label": t.get("logs", {}).get("job_log", ""),
            "explicit": False}


def job_page_url(job_key: str, back: str | None = None) -> str:
    """The job page, carrying a validated back target along."""
    target = safe_back(back, job_key)
    return f"/logs/job/{job_key}" + (f"?back={quote(target, safe='')}" if target else "")

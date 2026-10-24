"""Run single modules again: checks, plan, preview and preparation
(docs/UMBAU_MODULE_AUSFUEHRUNG.md, sections 2 and 4).

A rerun never bypasses run_pipeline. The preparation claims the job like a
retry (``done|review → processing``), checks it, makes its file available
(2.3), supersedes the current runs of the chosen modules and of the
finalizers and queues new ones (trigger ``rerun`` / ``cascade``), stores
the run parameters (``_rerun`` and friends, 2.8) and sets the job to
``queued`` — runs, parameters, status and the request item in one
transaction, after every fileops call. The worker then runs the job like
any other; the executor (pipeline/executor.py) decides the cascade.

Checks happen twice: in the preview (SQL and the register, cheap) and
after the claim in the preparation (binding: files hashed, assets checked
in Immich).

Reasons, skip and refusal codes leave this module as keys (section 4.7),
never as text; the UI translates them.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import func, or_, select, update

import fileops
import runs
from database import async_session
from models import AssetRef, FileCopy, Job, ModuleRun, ProcessingSet, Rerun, RerunItem, RERUN_ITEM_OPEN
from pipeline import registry
from pipeline.registry import FINALIZER_CODES, MAIN
from system_logger import log_info, log_warning

logger = logging.getLogger("mediaassistant.rerun")

ACTOR = "system:rerun"
DEFAULT_OPTIONS = {"cascade": "auto", "hints": False, "immich": "api"}
CASCADES = ("auto", "all", "off")
IMMICH_MODES = ("api", "replace")
MAX_KEYS = 5000
MAX_PREVIEW_JOBS = 200
SAMPLE = 5
_CHUNK = 500

# Job status -> refusal key (2.5); done and review may run again.
_STATUS_REFUSED = {"error": "status_error", "duplicate": "status_duplicate", "skipped": "status_skipped",
                   "orphan": "status_orphan", "deleted": "status_deleted", "queued": "busy",
                   "processing": "busy"}
RERUNNABLE = ("done", "review")
# Order in which refusals are reported when several apply.
REFUSAL_KEYS = ("busy", "status_error", "status_duplicate", "status_skipped", "status_orphan",
                "status_deleted", "already_pending", "resolved_donor", "set_missing", "asset_missing",
                "no_copy", "shared_copy")
# Volatile result keys that do not make a result "changed".
_VOLATILE = frozenset({"cached", "file_op", "notes", "resumed"})


class RerunError(Exception):
    """A request the API answers with an error key."""

    def __init__(self, key: str, status: int = 400, detail: str = "", **extra):
        super().__init__(key)
        self.key, self.status, self.detail, self.extra = key, status, detail, extra


# ---------------------------------------------------------------------------
# Request validation and target resolution
# ---------------------------------------------------------------------------

def validate(modules, options) -> tuple[list[str], dict]:
    """The chosen modules in pipeline order and the full options."""
    if not isinstance(modules, list) or not modules:
        raise RerunError("no_modules")
    for code in modules:
        if code not in registry.SPECS:
            raise RerunError("unknown_module", detail=str(code))
        if not registry.standalone(code):
            raise RerunError("not_standalone", detail=code)
    opts = dict(DEFAULT_OPTIONS)
    if options is not None and not isinstance(options, dict):
        raise RerunError("bad_option")
    for key, value in (options or {}).items():
        if key == "cascade" and value in CASCADES:
            opts[key] = value
        elif key == "hints" and isinstance(value, bool):
            opts[key] = value
        elif key == "immich" and value in IMMICH_MODES:
            opts[key] = value
        elif key == "tags_only" and isinstance(value, bool):
            # IA-08 changes tags only (the tool that switches a set to tag groups)
            opts[key] = value
        else:
            raise RerunError("bad_option", detail=f"{key}={value!r}")
    return [c for c in MAIN if c in modules], opts


def _date(value, end: bool = False):
    if value in (None, ""):
        return None
    try:
        day = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        raise RerunError("bad_target", detail=f"date {value!r}")
    return day.replace(hour=23, minute=59, second=59) if end else day


async def resolve_target(target) -> tuple[list[int], dict]:
    """(job ids in order, scope as stored in reruns.scope)."""
    if not isinstance(target, dict):
        raise RerunError("bad_target")
    kind = target.get("kind")
    async with async_session() as session:
        if kind == "jobs":
            keys = target.get("keys")
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                raise RerunError("bad_target")
            keys = list(dict.fromkeys(keys))
            if len(keys) > MAX_KEYS:
                raise RerunError("too_many_keys", status=413, detail=str(len(keys)))
            found = {}
            for i in range(0, len(keys), _CHUNK):
                found.update((await session.execute(select(Job.debug_key, Job.id).where(
                    Job.debug_key.in_(keys[i:i + _CHUNK])))).all())
            ids = [found[k] for k in keys if k in found]
            if not ids:
                raise RerunError("bad_target", detail="no such job")
            return ids, {"kind": "jobs", "keys": keys[:20], "count": len(ids)}
        if kind == "query":
            status, q = target.get("status") or "", target.get("q") or ""
            if not isinstance(status, str) or not isinstance(q, str):
                raise RerunError("bad_target")
            query = select(Job.id)
            if status == "warning":
                # The processing log's pseudo status (routers/logs.py).
                query = query.where(Job.status == "done", Job.error_message.like("Warnungen in:%"))
            elif status:
                query = query.where(Job.status == status)
            if q:
                query = query.where(Job.filename.contains(q) | Job.debug_key.contains(q))
            ids = list((await session.execute(query.order_by(Job.updated_at.desc()))).scalars().all())
            return ids, {"kind": "query", "status": status, "q": q, "count": len(ids)}
        if kind == "set":
            set_id = target.get("set_id")
            if not isinstance(set_id, int) or isinstance(set_id, bool):
                raise RerunError("bad_target")
            ps = await session.get(ProcessingSet, set_id)
            if ps is None:
                raise RerunError("set_not_found", status=404)
            flt = target.get("filter") or {}
            if not isinstance(flt, dict):
                raise RerunError("bad_target")
            statuses = flt.get("status") or list(RERUNNABLE)
            if not isinstance(statuses, list) or not all(isinstance(s, str) for s in statuses):
                raise RerunError("bad_target")
            query = select(Job.id).where(Job.set_id == set_id, Job.status.in_(statuses))
            start, end = _date(flt.get("created_from")), _date(flt.get("created_to"), end=True)
            if start:
                query = query.where(Job.created_at >= start)
            if end:
                query = query.where(Job.created_at <= end)
            ids = list((await session.execute(query.order_by(Job.created_at, Job.id))).scalars().all())
            clean = {"status": statuses, "only_stale": bool(flt.get("only_stale")),
                     "created_from": flt.get("created_from"), "created_to": flt.get("created_to")}
            return ids, {"kind": "set", "set_id": set_id, "filter": clean, "count": len(ids)}
    raise RerunError("bad_target")


# ---------------------------------------------------------------------------
# Loading jobs and their context
# ---------------------------------------------------------------------------

_JOB_COLUMNS = (Job.id, Job.debug_key, Job.filename, Job.status, Job.set_id, Job.use_immich, Job.dry_run,
                Job.target_path, Job.original_path, Job.immich_asset_id, Job.immich_user_id,
                Job.error_message, Job.file_hash, Job.source_label, Job.step_result, Job.created_at)


@dataclass
class Context:
    """What planning many jobs needs, read once."""
    global_cfg: dict
    sets: dict = field(default_factory=dict)        # set_id -> set_settings
    set_ids: set = field(default_factory=set)       # existing set ids
    ai: bool = False                                # an AI backend is configured and on
    geocoding: bool = False


async def load_context(session) -> Context:
    from ai_backends import _load_backends
    g = await registry.global_settings(session)
    ids = set((await session.execute(select(ProcessingSet.id))).scalars().all())
    return Context(global_cfg=g, set_ids=ids, ai=bool(await _load_backends()),
                   geocoding=g["modules"]["geocoding"])


async def _set_snap(session, ctx: Context, set_id) -> dict:
    if set_id not in ctx.sets:
        ctx.sets[set_id] = await registry.set_settings(session, set_id)
    return {**ctx.global_cfg, **ctx.sets[set_id]}


async def load_rows(ids: list[int]) -> list[SimpleNamespace]:
    """The jobs as plain rows (columns needed here plus the view), in the
    order of `ids`, with the register facts the checks need."""
    rows: dict[int, SimpleNamespace] = {}
    async with async_session() as session:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            for r in (await session.execute(select(*_JOB_COLUMNS).where(Job.id.in_(chunk)))).all():
                rows[r.id] = SimpleNamespace(**r._asdict(), local_copies=[], foreign_local=False,
                                             shares_other=False, asset_state=None, pending=False)
            for job_id, kind, location, state in (await session.execute(
                    select(FileCopy.job_id, FileCopy.kind, FileCopy.location, FileCopy.state)
                    .where(FileCopy.job_id.in_(chunk), FileCopy.kind.in_(("local", "work", "immich")))
                    .order_by(FileCopy.id))).all():
                row = rows.get(job_id)
                if row is None:
                    continue
                if kind in ("local", "work") and state == "present":
                    row.local_copies.append(location)
                elif kind == "immich" and location == _asset_id(row):
                    row.asset_state = state  # the newest row of the asset counts
            for job_id in (await session.execute(select(RerunItem.job_id).where(
                    RerunItem.job_id.in_(chunk), RerunItem.state.in_(RERUN_ITEM_OPEN)))).scalars().all():
                if job_id in rows:
                    rows[job_id].pending = True
            # Shared local copies: someone else's file this job uses, or this
            # job's file other jobs use (fileops.write_metadata refuses both).
            for job_id, location in (await session.execute(select(AssetRef.job_id, AssetRef.location).where(
                    AssetRef.job_id.in_(chunk), AssetRef.kind != "immich"))).all():
                if job_id in rows:
                    rows[job_id].shares_other = True
            locs = {loc: r.id for r in rows.values() if r.id in chunk for loc in r.local_copies}
            if locs:
                for location, job_id in (await session.execute(select(AssetRef.location, AssetRef.job_id).where(
                        AssetRef.location.in_(list(locs)), AssetRef.kind != "immich"))).all():
                    if job_id != locs.get(location):
                        rows[locs[location]].foreign_local = True
    out = []
    for job_id in ids:
        row = rows.get(job_id)
        if row is not None:
            row.view = row.step_result if isinstance(row.step_result, dict) else {}
            out.append(row)
    return out


def _asset_id(row) -> str | None:
    target = row.target_path or ""
    if target.startswith("immich:"):
        return target[len("immich:"):]
    return row.immich_asset_id


# ---------------------------------------------------------------------------
# The plan of one job
# ---------------------------------------------------------------------------

@dataclass
class ModulePlan:
    action: str          # run | maybe | keep | skip
    why: str             # selected | cascade | prerequisite | finalizer | hint_not_followed | cascade_off
    sources: list = field(default_factory=list)
    skip: str | None = None


@dataclass
class JobPlan:
    modules: dict        # code -> ModulePlan, pipeline order
    needs_file: bool
    ai_calls: int
    writes_file: bool
    may_move: bool
    immich_api: bool
    immich_replace: bool

    def runs(self) -> list[str]:
        return [c for c, p in self.modules.items() if p.action in ("run", "maybe")]


def _ext(row) -> str:
    path = row.target_path if row.target_path and not row.target_path.startswith("immich:") else row.filename
    return os.path.splitext(path or row.filename or "")[1].lower()


def _ocr_relevant(ia05: dict) -> bool:
    ai_type = (ia05.get("type") or "")
    source = (ia05.get("source") or "").lower()
    return ai_type.lower() == "screenshot" or "dokument" in source or "document" in source or "quittung" in source


def predict_skip(code: str, row, view: dict, snap: dict, ctx: Context, options: dict,
                 upstream_runs: set) -> str | None:
    """Why `code` would come back without a new result for this job (a
    skip key, 4.7), judged from what is known before it runs."""
    s = snap.get("set") or {}
    ia01 = view.get("IA-01") if isinstance(view.get("IA-01"), dict) else {}
    if code == "IA-03":
        if not ctx.geocoding:
            return "module_disabled"
        if "IA-01" not in upstream_runs and (ia01.get("gps_lat") is None or ia01.get("gps_lon") is None):
            return "no_gps"
    elif code == "IA-05":
        if not ctx.ai:
            return "no_ai"
        w, h = ia01.get("width") or 0, ia01.get("height") or 0
        if 0 < w < 16 and 0 < h < 16:
            return "too_small"
    elif code == "IA-06":
        if not s.get("ocr_enabled"):
            return "ocr_off"
        if not ctx.ai:
            return "no_ai"
        ia05 = view.get("IA-05") if isinstance(view.get("IA-05"), dict) else {}
        if (s.get("ocr_mode") or "smart") == "smart" and "IA-05" not in upstream_runs \
                and ia05.get("type") and not _ocr_relevant(ia05) and not ia05.get("parse_error"):
            return "ocr_not_needed"
    elif code == "IA-07":
        from pipeline.step_ia07_exif_write import WRITABLE_EXTENSIONS
        if registry.SPECS["IA-07"].needs_file(row, options) and _ext(row) not in WRITABLE_EXTENSIONS:
            return "not_writable"
    return None


def _template_inputs(template: str) -> set:
    """Modules whose results feed placeholders of a path template."""
    out = set()
    if "{CITY}" in template or "{COUNTRY}" in template:
        out.add("IA-03")
    if any(p in template for p in ("{YYYY}", "{MM}", "{DD}", "{YYYY-MM}", "{CAMERA}")):
        out.add("IA-01")
    return out


def _may_move(row, view: dict, snap: dict, active: set) -> bool:
    """Could IA-08 put this library file into another folder?"""
    if row.use_immich or row.dry_run:
        return False
    target = row.target_path or ""
    if not target or target.startswith("immich:"):
        return True
    ia08 = view.get("IA-08") if isinstance(view.get("IA-08"), dict) else {}
    category = ia08.get("category")
    cats = {c[0]: c for c in snap.get("categories") or []}
    if category not in cats or {"IA-05", "IA-01"} & active:
        return True  # the category itself may change
    template = cats[category][2] or ""
    if _template_inputs(template) & active:
        return True
    # Unchanged inputs: a move only if the folder the rules give now is
    # another one (e.g. the template or the base path changed since).
    try:
        from file_operations import parse_date, validate_target_path
        from pipeline.step_ia08_sort import _resolve_path
        exif = dict(view.get("IA-01") or {})
        geo = view.get("IA-03") if isinstance(view.get("IA-03"), dict) else {}
        if geo.get("country"):
            exif.update(country=geo["country"], city=geo.get("city", ""))
        base = (snap.get("set") or {}).get("base") or "/library"
        want = validate_target_path(os.path.join(base, _resolve_path(template, exif, parse_date(exif.get("date")))),
                                    base)
        return os.path.realpath(want.rstrip("/")) != os.path.realpath(os.path.dirname(target))
    except Exception:
        return True


def plan_job(row, view: dict, modules: list[str], options: dict, snap: dict, ctx: Context) -> JobPlan:
    """What a rerun of `modules` does with this job (section 2.2)."""
    cascade, hints = options["cascade"], options["hints"]
    ocr_mode = (snap.get("set") or {}).get("ocr_mode") or "smart"
    present = {c for c in MAIN if c in view}
    plans: dict[str, ModulePlan] = {}
    active: set = set()          # modules that run or may run (and give a new result)

    for code in MAIN:
        if code == "IA-02":
            continue  # never runs on its own (2.2)
        if code in modules:
            reason = predict_skip(code, row, view, snap, ctx, options, active)
            plans[code] = ModulePlan("skip" if reason else "run", "selected", skip=reason)
            if not reason:
                active.add(code)
            continue
        if code not in present:
            continue  # never ran: does not run in a rerun (2.2)
        strong = [u for u in registry.edges(code, ocr_mode=ocr_mode) if u in active]
        weak = [u for u in registry.SPECS[code].hints if u in active and u not in strong]
        if strong or (hints and weak):
            sources = strong + (weak if hints else [])
            if cascade == "off":
                plans[code] = ModulePlan("keep", "cascade_off", sources)
                continue
            reason = predict_skip(code, row, view, snap, ctx, options, active)
            if reason:
                plans[code] = ModulePlan("skip", "cascade", sources, skip=reason)
                continue
            plans[code] = ModulePlan("run" if cascade == "all" else "maybe", "cascade", sources)
            active.add(code)
        elif weak:
            plans[code] = ModulePlan("keep", "hint_not_followed", weak)

    # IA-04 as the prerequisite of IA-05 / IA-06 (its output is gone after
    # every run; only a converted file needs it).
    ia04 = view.get("IA-04") if isinstance(view.get("IA-04"), dict) else {}
    users = [c for c in ("IA-05", "IA-06") if plans.get(c) and plans[c].action in ("run", "maybe")]
    if users and "IA-04" in present and ia04.get("converted") and "IA-04" not in active:
        definite = any(plans[c].action == "run" for c in users)
        plans["IA-04"] = ModulePlan("run" if definite else "maybe", "prerequisite", users)
        active.add("IA-04")

    for code in FINALIZER_CODES:
        plans[code] = ModulePlan("run", "finalizer")
    ordered = {c: plans[c] for c in registry.ORDER if c in plans}

    runs_ = {c for c, p in ordered.items() if p.action in ("run", "maybe") and c in MAIN}
    needs_file = any(registry.SPECS[c].needs_file(row, options) for c in runs_)
    if row.dry_run:
        needs_file = True
    ai_calls = sum(1 for c in ("IA-05", "IA-06") if c in runs_)
    writes = "IA-07" in runs_ and registry.SPECS["IA-07"].needs_file(row, options) and not row.dry_run
    ia08 = "IA-08" in runs_ and not row.dry_run
    return JobPlan(
        modules=ordered, needs_file=needs_file, ai_calls=ai_calls, writes_file=writes,
        may_move=ia08 and not row.use_immich and _may_move(row, view, snap, active),
        immich_api=ia08 and bool(row.use_immich) and options["immich"] == "api",
        immich_replace=ia08 and bool(row.use_immich) and options["immich"] == "replace")


# ---------------------------------------------------------------------------
# Refusals (2.5), cheap form for the preview
# ---------------------------------------------------------------------------

def refusal(row, plan: JobPlan | None, ctx: Context) -> str | None:
    if row.status not in RERUNNABLE:
        return _STATUS_REFUSED.get(row.status, "busy")
    if row.pending:
        return "already_pending"
    if row.status == "done" and (row.error_message or "").startswith("Duplicate deleted") \
            and not row.local_copies:
        return "resolved_donor"
    if row.set_id is not None and row.set_id not in ctx.set_ids:
        return "set_missing"
    if row.use_immich and not row.dry_run:
        if not _asset_id(row) or row.asset_state in ("trashed", "missing", "deleted"):
            return "asset_missing"
    if row.dry_run:
        if not _isfile(row.original_path):
            return "no_copy"
    elif not row.use_immich:
        target = row.target_path if row.target_path and not row.target_path.startswith("immich:") else None
        # The register, and a look at the disk (a stat, no hashing — the
        # preparation hashes).
        if not any(_isfile(p) for p in row.local_copies) and not (target and _isfile(target)):
            return "no_copy"
        if plan is not None and plan.writes_file and (row.shares_other or row.foreign_local):
            return "shared_copy"
    return None


def _isfile(path) -> bool:
    return bool(path) and not str(path).startswith(("immich:", "immich-pending:")) and os.path.isfile(path)


def source_kind(row, plan: JobPlan) -> str:
    """Where the file comes from (preview): local, work (download), none."""
    if row.dry_run or not row.use_immich:
        return "local"
    return "work" if plan.needs_file else "none"


# ---------------------------------------------------------------------------
# Preview (4.2) and selection for a request (4.3)
# ---------------------------------------------------------------------------

def _add(bucket: dict, key: str, debug_key: str) -> None:
    entry = bucket.setdefault(key, {"count": 0, "sample": []})
    entry["count"] += 1
    if len(entry["sample"]) < SAMPLE:
        entry["sample"].append(debug_key)


async def evaluate(ids: list[int], modules: list[str], options: dict, *, only_stale: bool = False):
    """Plan and refusal for every job: [(row, plan or None, refused key or None)]."""
    rows = await load_rows(ids)
    async with async_session() as session:
        ctx = await load_context(session)
        snaps = {}
        for set_id in {r.set_id for r in rows}:
            snaps[set_id] = await _set_snap(session, ctx, set_id)
        stale_runs = {}
        if only_stale:
            for i in range(0, len(rows), _CHUNK):
                chunk = [r.id for r in rows[i:i + _CHUNK]]
                for run in (await session.execute(select(ModuleRun).where(
                        ModuleRun.job_id.in_(chunk), ModuleRun.module.in_(modules),
                        ModuleRun.status != "superseded"))).scalars().all():
                    stale_runs.setdefault(run.job_id, []).append(run)
    out = []
    for row in rows:
        snap = snaps[row.set_id]
        if only_stale and not any(registry.is_stale(r, row, row.view, snap) for r in stale_runs.get(row.id, [])):
            continue
        plan = plan_job(row, row.view, modules, options, snap, ctx) if row.status in RERUNNABLE else None
        out.append((row, plan, refusal(row, plan, ctx)))
    return out


async def preview(body: dict) -> dict:
    started = time.monotonic()
    modules, options = validate(body.get("modules"), body.get("options"))
    ids, scope = await resolve_target(body.get("target"))
    detail = body.get("detail", "summary")
    if detail not in ("summary", "jobs"):
        raise RerunError("bad_option", detail=f"detail={detail!r}")
    only_stale = bool((scope.get("filter") or {}).get("only_stale"))
    evaluated = await evaluate(ids, modules, options, only_stale=only_stale)

    refused: dict = {}
    per: dict = {}
    effects = {"downloads": 0, "ai_calls_max": 0, "file_writes_max": 0, "library_moves_max": 0,
               "immich_api": 0, "immich_replace": 0}
    eligible = 0
    targets = {"immich": 0, "library": 0}
    jobs = []
    for row, plan, why in evaluated:
        if why:
            _add(refused, why, row.debug_key)
        else:
            eligible += 1
            targets["immich" if row.use_immich else "library"] += 1
            for code, mp in plan.modules.items():
                agg = per.setdefault(code, {"actions": {}, "why": {}, "from": [], "skip": {}})
                agg["actions"][mp.action] = agg["actions"].get(mp.action, 0) + 1
                agg["why"].setdefault(mp.action, mp.why)
                for s_ in mp.sources:
                    if s_ not in agg["from"]:
                        agg["from"].append(s_)
                if mp.skip:
                    _add(agg["skip"], mp.skip, row.debug_key)
            if plan.needs_file and source_kind(row, plan) == "work":
                effects["downloads"] += 1
            effects["ai_calls_max"] += plan.ai_calls
            effects["file_writes_max"] += int(plan.writes_file)
            effects["library_moves_max"] += int(plan.may_move)
            effects["immich_api"] += int(plan.immich_api)
            effects["immich_replace"] += int(plan.immich_replace)
        if detail == "jobs" and len(jobs) < MAX_PREVIEW_JOBS:
            jobs.append({
                "debug_key": row.debug_key, "filename": row.filename, "status": row.status,
                "target": "immich" if row.use_immich else "library", "refused": why,
                "source": None if why or plan is None else source_kind(row, plan),
                "modules": [] if why or plan is None else [
                    {"module": c, "action": p.action, "why": p.why, **({"skip": p.skip} if p.skip else {})}
                    for c, p in plan.modules.items()],
            })

    # One entry per module that the rerun touches: the most active action
    # any job has (run > maybe > skip > keep) with its count; "skip" lists
    # the jobs where a module comes back without a new result.
    per_module = {}
    for code in registry.ORDER:
        agg = per.get(code)
        if not agg:
            continue
        acts = agg["actions"]
        action = next((a for a in ("run", "maybe", "skip", "keep") if acts.get(a)), "keep")
        entry = {"action": action, "count": acts.get(action, 0) if action != "skip" else 0,
                 "why": agg["why"][action]}
        if agg["from"]:
            entry["from"] = agg["from"]
        if agg["skip"]:
            entry["skip"] = agg["skip"]
        per_module[code] = entry
    out = {"ok": True, "modules": modules, "options": options, "total": len(evaluated), "eligible": eligible,
           "refused": refused, "per_module": per_module, "effects": effects, "targets": targets,
           "elapsed_ms": int((time.monotonic() - started) * 1000)}
    if detail == "jobs":
        out["jobs"] = jobs
        out["jobs_truncated"] = len(evaluated) > len(jobs)
    out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return out


async def job_info(job) -> dict:
    """The "rerun" block of GET /api/history/job/{key} (4.6): may the job
    run again, and which modules would give a result."""
    info = {"eligible": False, "refused": None, "modules": {}, "active": None}
    rows = await load_rows([job.id])
    if rows:
        row = rows[0]
        async with async_session() as session:
            ctx = await load_context(session)
            snap = await _set_snap(session, ctx, row.set_id)
        why = refusal(row, None, ctx)
        info["eligible"], info["refused"] = why is None, why
        for code in MAIN:
            if registry.standalone(code):
                skip = predict_skip(code, row, row.view, snap, ctx, DEFAULT_OPTIONS, set())
                info["modules"][code] = {"possible": why is None and skip is None, "skip": skip}
    async with async_session() as session:
        item = (await session.execute(select(RerunItem.rerun_id, RerunItem.state).where(
            RerunItem.job_id == job.id, RerunItem.state.in_(RERUN_ITEM_OPEN + ("deferred",)))
            .order_by(RerunItem.id.desc()).limit(1))).first()
    if item:
        info["active"] = {"rerun_id": item.rerun_id, "state": item.state}
    return info


async def stale_modules(jobs, runs_by_job: dict) -> dict:
    """{job id: set of modules whose current finished run is stale}."""
    out = {}
    if not jobs:
        return out
    async with async_session() as session:
        g = await registry.global_settings(session)
        cache = {}
        for job in jobs:
            snap = await registry.settings_snapshot(session, job, global_cfg=g, set_cache=cache)
            view = job.step_result if isinstance(job.step_result, dict) else {}
            cur = {}
            for run in runs_by_job.get(job.id, []):
                if run.status != "superseded":
                    cur[run.module] = run
            out[job.id] = {m for m, r in cur.items() if registry.is_stale(r, job, view, snap)}
    return out


# ---------------------------------------------------------------------------
# Preparation (binding checks, source, queue) — called by the feeder
# ---------------------------------------------------------------------------

@dataclass
class Prepared:
    state: str                  # queued | refused | failed
    reason: str | None = None
    detail: str | None = None


async def _set_item(item_id: int, frm, **values) -> bool:
    async with async_session() as session:
        query = update(RerunItem).where(RerunItem.id == item_id)
        if frm:
            query = query.where(RerunItem.state.in_(frm if isinstance(frm, tuple) else (frm,)))
        res = await session.execute(query.values(**values))
        await session.commit()
        return res.rowcount == 1


async def _release(job_id: int, prev_status: str) -> None:
    """Hand a claimed job back unchanged."""
    async with async_session() as session:
        await session.execute(update(Job).where(Job.id == job_id, Job.status == "processing")
                              .values(status=prev_status))
        await session.commit()


async def release_cut_short(session, job, *, cutoff: datetime | None = None) -> bool:
    """A job left 'processing' by a rerun preparation that was cut short
    goes back to the status it had, its item back to 'pending' (the feeder
    prepares it again). True if it was one. Called at startup
    (recovery.requeue_interrupted_jobs) and by the stale watchdog
    (worker._requeue_or_fail, with its `cutoff`: a job touched since is
    left alone); a preparation still running heartbeats and is never stale.

    The preparation commits its claim (with the status from before, in the
    item) first and everything else in one transaction at the end (runs,
    run parameters, status, item). Cut short in between, the job has no run
    parameters and its item is still 'preparing': nothing of the rerun was
    kept, and a work copy it may have downloaded is the job's own
    (registered), found again by the next preparation. Resumed like an
    interrupted pipeline run instead, the job ran with nothing open and its
    status was aggregated anew — a review job came back 'done' — and the
    item met a queued job and was refused as busy. retry_count stays (2.6).
    """
    if job.status != "processing" or "_rerun" in runs.params_of(job):
        return False
    item = (await session.execute(select(RerunItem.id, RerunItem.prev_status).where(
        RerunItem.job_id == job.id, RerunItem.state == "preparing"))).first()
    if item is None or item.prev_status not in RERUNNABLE:
        return False
    query = update(Job).where(Job.id == job.id, Job.status == "processing")
    if cutoff is not None:
        query = query.where(Job.updated_at < cutoff)
    res = await session.execute(query.values(status=item.prev_status))
    if res.rowcount != 1:
        return False  # nothing changed; the caller goes on as without
    await session.execute(update(RerunItem).where(RerunItem.id == item.id, RerunItem.state == "preparing")
                          .values(state="pending"))
    await session.commit()
    await log_info("rerun", f"{job.debug_key} Vorbereitung für Neu ausführen unterbrochen — "
                            f"Job zurück auf '{item.prev_status}'",
                   f"{job.filename}: nichts vom Neu ausführen übernommen; der Auftrag wird neu vorbereitet")
    return True


async def _asset_ok(job) -> tuple[bool, str]:
    """The job's asset is present in Immich (not trashed). (ok, note)."""
    import immich_client
    from pipeline import reprocess
    asset_id = job.target_path[len("immich:"):] if (job.target_path or "").startswith("immich:") \
        else job.immich_asset_id
    if not asset_id:
        return False, "kein Asset"
    async with async_session() as session:
        row = (await session.execute(select(FileCopy).where(
            FileCopy.kind == "immich", FileCopy.location == asset_id)
            .order_by(FileCopy.id.desc()).limit(1))).scalar()
    try:
        if row is not None:
            checked = await fileops.verify(row, actor=ACTOR)
            return checked.state == "present", f"Asset {asset_id}: {checked.state}"
        key = await reprocess.job_api_key(job)
        state = await immich_client.get_asset_state(asset_id, api_key=key)
        return state == immich_client.ASSET_PRESENT, f"Asset {asset_id}: {state}"
    except Exception as exc:
        return False, f"Asset {asset_id}: {type(exc).__name__}: {exc}"


async def _local_source(job) -> tuple[str | None, int | None, str]:
    """The job's own file in its library (or inbox, dry run): (path, copy
    id, note). The register first; an unregistered file at target_path only
    if its content is one the job ever had."""
    from pipeline import reprocess
    if job.dry_run:
        path = reprocess._local_path(job.original_path)
        if path and await asyncio.to_thread(os.path.isfile, path):
            return path, None, ""
        return None, None, f"{job.original_path} fehlt"
    row = await reprocess.own_local_copy(job)
    if row is not None:
        return row.location, row.id, ""
    known = await reprocess.known_hashes(job)
    notes: list[str] = []
    found = await reprocess._local_from_fields(job, known, notes)
    if found is not None and found.checked and found.location == reprocess._local_path(job.target_path):
        try:
            reg = await fileops.register_existing(job, "local", found.location, sha256=found.sha256,
                                                  verified=True)
            return found.location, reg.id, ""
        except Exception as exc:
            notes.append(f"{type(exc).__name__}: {exc}")
    return None, None, "; ".join(notes) or "kein geprüftes Exemplar am Ablageort"


def _shared(job_id: int, path: str):
    """(owned by another job, used by other jobs) for a local path."""
    async def check():
        async with async_session() as session:
            row = (await session.execute(select(FileCopy).where(
                FileCopy.location == os.path.abspath(path), FileCopy.state == "present"))).scalar()
            refs = (await session.execute(select(func.count()).select_from(AssetRef).where(
                AssetRef.location == os.path.abspath(path), AssetRef.job_id != job_id))).scalar()
        return (row is not None and row.job_id != job_id), bool(refs)
    return check()


async def prepare(item_id: int) -> Prepared:
    """Prepare one item (state 'preparing', set by the feeder)."""
    import pipeline
    async with async_session() as session:
        item = await session.get(RerunItem, item_id)
        request = await session.get(Rerun, item.rerun_id) if item else None
    if item is None or request is None:
        return Prepared("failed", "prepare_failed", "item missing")
    modules, options = list(request.modules or []), {**DEFAULT_OPTIONS, **(request.options or {})}
    job_id = item.job_id

    async def refuse(reason, detail=""):
        await _set_item(item_id, "preparing", state="refused", reason=reason, detail=detail[:2000] or None,
                        finished_at=datetime.now())
        return Prepared("refused", reason, detail)

    if job_id in pipeline._ACTIVE_RUNS:
        return await refuse("busy")
    async with async_session() as session:
        prev = (await session.execute(select(Job.status).where(Job.id == job_id))).scalar()
        if prev not in RERUNNABLE:
            return await refuse(_STATUS_REFUSED.get(prev, "busy"))
        # Atomic claim, like a retry: only one caller takes a finished job.
        # The status it had goes into the item in the same commit: a restart
        # that cuts the preparation short hands it back from there
        # (release_cut_short).
        claim = await session.execute(update(Job).where(Job.id == job_id, Job.status == prev)
                                      .values(status="processing"))
        if claim.rowcount == 1:
            await session.execute(update(RerunItem).where(RerunItem.id == item_id,
                                                          RerunItem.state == "preparing")
                                  .values(prev_status=prev))
        await session.commit()
    if claim.rowcount != 1:
        return await refuse("busy")

    async with pipeline._running(job_id, "rerun"):
        try:
            result = await _prepare_claimed(item_id, request, job_id, prev, modules, options)
        except Exception as exc:
            logger.exception("rerun item %s: preparation failed", item_id)
            await _release(job_id, prev)
            await _set_item(item_id, ("preparing",), state="failed", reason="prepare_failed",
                            detail=f"{type(exc).__name__}: {exc}"[:2000], finished_at=datetime.now())
            return Prepared("failed", "prepare_failed", str(exc))
    if result.state != "queued":
        await _release(job_id, prev)
        if result.state == "refused":
            return await refuse(result.reason, result.detail or "")
        await _set_item(item_id, ("preparing",), state="failed", reason=result.reason,
                        detail=(result.detail or "")[:2000] or None, finished_at=datetime.now())
    return result


async def _prepare_claimed(item_id, request, job_id, prev, modules, options) -> Prepared:
    from pipeline import reprocess
    async with async_session() as session:
        job = await session.get(Job, job_id)
    key = job.debug_key

    # Binding checks (2.5)
    if job.set_id is not None:
        async with async_session() as session:
            if await session.get(ProcessingSet, job.set_id) is None:
                return Prepared("refused", "set_missing")
    if await reprocess.resolved_donor(job):
        return Prepared("refused", "resolved_donor")
    rows = await load_rows([job_id])
    row = rows[0]
    row.pending = False
    row.status = prev
    async with async_session() as session:
        ctx = await load_context(session)
        snap = await _set_snap(session, ctx, job.set_id)
    plan = plan_job(row, row.view, modules, options, snap, ctx)

    # Source (2.3)
    source = {"kind": "immich", "location": None, "copy_id": None}
    path = None
    if job.dry_run or not job.use_immich:
        path, copy_id, note = await _local_source(job)
        if path is None:
            return Prepared("refused", "no_copy", note)
        if plan.writes_file and not job.dry_run:
            other_owner, used_by_others = await _shared(job_id, path)
            if other_owner or used_by_others:
                return Prepared("refused", "shared_copy", path)
        source = {"kind": "local", "location": path, "copy_id": copy_id}
    else:
        ok, note = await _asset_ok(job)
        if not ok:
            return Prepared("refused", "asset_missing", note)
        asset_id = job.target_path[len("immich:"):] if (job.target_path or "").startswith("immich:") \
            else job.immich_asset_id
        source = {"kind": "immich", "location": asset_id, "copy_id": None}
        if plan.needs_file:
            # An own local copy only if it holds what the asset holds now.
            local = await reprocess.own_local_copy(job)
            async with async_session() as session:
                asset_sha = (await session.execute(select(FileCopy.sha256).where(
                    FileCopy.kind == "immich", FileCopy.location == asset_id, FileCopy.state == "present")
                    .order_by(FileCopy.id.desc()).limit(1))).scalar()
            if local is not None and asset_sha and local.sha256 == asset_sha:
                path = local.location
                source = {"kind": "local", "location": path, "copy_id": local.id}
            else:
                api_key = await reprocess.job_api_key(job)
                await asyncio.to_thread(os.makedirs, reprocess.REPROCESS_DIR, exist_ok=True)
                await log_info("rerun", f"{key} Original wird für Neu ausführen geladen",
                               f"Asset {asset_id} → {reprocess.REPROCESS_DIR}/")
                try:
                    import resources
                    async with resources.use("immich", priority=1):
                        res = await fileops.download(job, asset_id, reprocess.REPROCESS_DIR, actor=ACTOR,
                                                     kind="work", api_key=api_key)
                except fileops.FileOpError as exc:
                    return Prepared("failed", "asset_missing", str(exc))
                path = res.location
                source = {"kind": "work", "location": path, "copy_id": res.copy_id}
                await log_info("rerun", f"{key} Original für Neu ausführen geladen",
                               f"Asset {asset_id} → {path}")

    # Parameters from the current runs (2.8), then runs, values, status and
    # item in one transaction — fileops is done by now.
    async with async_session() as session:
        job = await session.get(Job, job_id)
        cur = await runs.current_runs(session, job_id)
        ia07 = cur["IA-07"].result if "IA-07" in cur and isinstance(cur["IA-07"].result, dict) else {}
        ia08 = cur["IA-08"].result if "IA-08" in cur and isinstance(cur["IA-08"].result, dict) else {}
        rr = {"rerun": request.id, "item": item_id, "modules": modules, "cascade": options["cascade"],
              "hints": options["hints"], "immich": options["immich"],
              "tags_only": bool(options.get("tags_only")), "prev_status": prev,
              "prev_error": job.error_message, "started_at": datetime.now().isoformat(timespec="seconds"),
              "source": source}
        values = {"_rerun": rr,
                  "_retry_previous_immich_tags": list(ia08.get("immich_tags_written") or []) or None,
                  "_retry_previous_immich_description":
                      ia08.get("description_written") or ia07.get("description_written") or None,
                  "_previous_keywords_written": list(ia07.get("keywords_written") or []) or None,
                  "_previous_tag_paths_written": list(ia07.get("tag_paths_written") or []) or None,
                  "_previous_description_written": ia07.get("description_written") or None,
                  "_manual_category": ia08.get("category") if ia08.get("manual_review") else None}
        # Every run of this job with a higher id belongs to this rerun.
        rr["base"] = int((await session.execute(select(func.coalesce(func.max(ModuleRun.id), 0)).where(
            ModuleRun.job_id == job_id))).scalar() or 0)
        superseded = {}
        for code in [*modules, *FINALIZER_CODES]:
            _run, old = await runs.enqueue(session, job, code, trigger="rerun" if code in modules else "cascade")
            if old is not None:
                superseded[code] = old.id
        rr["superseded"] = superseded
        await runs.set_values(session, job, {k: v for k, v in values.items()}, clear_params=True)
        job.original_path = path if path else f"immich:{source['location']}"
        job.status = "queued"
        res = await session.execute(update(RerunItem).where(RerunItem.id == item_id,
                                                            RerunItem.state == "preparing")
                                    .values(state="queued"))
        if res.rowcount != 1:
            # Cancelled meanwhile: nothing of this is kept.
            await session.rollback()
            return Prepared("refused", "busy", "Auftrag inzwischen abgebrochen")
        await session.commit()
    await log_info("rerun", f"{key} Neu ausführen vorbereitet: {', '.join(modules)}",
                   f"Quelle {source['kind']} {source.get('location') or ''}; Kaskade {options['cascade']}, "
                   f"KI {'ja' if options['hints'] else 'nein'}, Immich {options['immich']}")
    return Prepared("queued")


async def abort(job_id: int, error: str) -> None:
    """A rerun run crashed outside the executor's own handling: the job
    goes back to the status it had, never to error (2.6)."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        rr = runs.params_of(job).get("_rerun")
        if not rr or job.status != "processing":
            return
        # Modules caught mid-run or not reached get their old runs back.
        from pipeline import executor
        await runs.supersede_running(session, job, reason="abgebrochen (Absturz beim Neu ausführen)")
        await session.commit()
        ctx = executor.Ctx(session=session, job=job, params=runs.params_of(job), snap=None, current={}, rerun=rr)
        await executor._give_back_open(ctx)
        await runs.set_values(session, job, clear_params=True)
        job.status = rr.get("prev_status") or "done"
        job.completed_at = datetime.now()
        await session.execute(update(RerunItem).where(RerunItem.id == rr.get("item"),
                                                      RerunItem.state.in_(("queued", "running")))
                              .values(state="failed", reason="module_error", detail=error[:2000],
                                      finished_at=datetime.now()))
        await session.commit()
    await log_warning("rerun", f"{job.debug_key} Neu ausführen abgebrochen", error[:2000])

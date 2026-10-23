"""History, trash and inventory check: what the data-safety rebuild shows.

- /api/history/job/{key}     modules, ledger and register of one job (expanded
                             rows, job detail page)
- /api/history/modules       module blocks of several jobs (dashboard refresh)
- /api/history/copies/{id}/restore   brings a trashed copy back (fileops.restore)
- /tools/papierkorb          trash page, filled from /api/history/trash
- /tools/bestand             inventory check page; the check runs
                             fileops.verify() over the register in the
                             background and only reads: it never moves or
                             deletes anything (verify only records what it finds)

All reading goes through job_view.py; all changes through fileops.py.
"""
import asyncio
import logging
import os
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

import fileops
import job_view
from config import config_manager
from database import async_session
from i18n import DEFAULT_LANGUAGE, load_lang
from models import Job
from system_logger import log_error, log_info, log_warning
from template_engine import render, templates

logger = logging.getLogger("mediaassistant.history")
router = APIRouter()

# Templates read the module blocks through these, never step_result.
templates.env.globals.update(module_blocks=job_view.module_blocks,
                             step_visible=job_view.step_visible,
                             strip_summary=job_view.strip_summary,
                             job_signature=job_view.job_signature)

LOG_SOURCE = "bestand"
TRASH_PAGE_SIZE = 50
MAX_KEYS = 100
INVENTORY_KEY = "inventory.last_run"
MAX_FINDINGS = 500
REPROCESS_DIR = "/app/data/reprocess"  # pipeline/reprocess.py REPROCESS_DIR
IMMICH_FAILURES_BEFORE_SKIP = 3
DISCREPANCY_TYPES = ("missing", "mismatch", "immich_missing", "immich_trashed", "reprocess_orphan")


async def _t() -> dict:
    return load_lang(await config_manager.get("ui.language", DEFAULT_LANGUAGE))


# Immich's own trash retention (server setting trashDays), for "noch n Tage".
_trash_days_cache: dict = {"value": job_view.TRASH_RETENTION_DAYS, "at": 0.0}
TRASH_DAYS_TTL_S = 600


async def immich_trash_days() -> int:
    if time.monotonic() - _trash_days_cache["at"] < TRASH_DAYS_TTL_S:
        return _trash_days_cache["value"]
    _trash_days_cache["at"] = time.monotonic()
    url = (await config_manager.get("immich.url", "") or "").rstrip("/")
    if not url:
        return _trash_days_cache["value"]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/api/server/config")
        days = resp.json().get("trashDays") if resp.status_code == 200 else None
        if isinstance(days, int) and days >= 0:
            _trash_days_cache["value"] = days
    except Exception as exc:  # unreachable Immich: keep the last known value
        logger.info("Immich trashDays not readable: %s", exc)
    return _trash_days_cache["value"]


async def _job_by_key(debug_key: str) -> Job | None:
    async with async_session() as session:
        return (await session.execute(select(Job).where(Job.debug_key == debug_key))).scalar()


# ---------------------------------------------------------------------------
# Job history
# ---------------------------------------------------------------------------

@router.get("/api/history/job/{debug_key}")
async def job_history(debug_key: str):
    job = await _job_by_key(debug_key)
    if job is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    t = await _t()
    blocks = job_view.module_blocks(job, t)
    return {
        "debug_key": job.debug_key,
        "filename": job.filename,
        "status": job.status,
        "signature": job_view.job_signature(job),
        "modules": blocks,
        "summary": job_view.strip_summary(blocks, t),
        "ops": await job_view.job_ledger(job.id, t),
        "copies": await job_view.job_copies(job.id, t, await immich_trash_days()),
    }


@router.get("/api/history/modules")
async def modules_of(keys: str = ""):
    """Module blocks of several jobs at once (the dashboard's live refresh)."""
    wanted = [k for k in dict.fromkeys(keys.split(",")) if k][:MAX_KEYS]
    if not wanted:
        return {"jobs": {}}
    t = await _t()
    async with async_session() as session:
        jobs = (await session.execute(select(Job).where(Job.debug_key.in_(wanted)))).scalars().all()
    out = {}
    for job in jobs:
        blocks = job_view.module_blocks(job, t)
        out[job.debug_key] = {"signature": job_view.job_signature(job), "modules": blocks,
                              "summary": job_view.strip_summary(blocks, t)}
    return {"jobs": out}


@router.post("/api/history/copies/{copy_id}/restore")
async def restore_copy(copy_id: int):
    """Bring one trashed copy back. fileops logs before and after and refuses
    what its rules forbid; a refusal is answered with 409 and its reason."""
    try:
        result = await fileops.restore(copy_id, actor="user:restore")
    except fileops.FileOpRefused as exc:
        return JSONResponse({"ok": False, "refused": True, "error": str(exc)}, status_code=409)
    except fileops.FileOpError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    except Exception as exc:
        logger.exception("restore of copy %s failed", copy_id)
        await log_error(LOG_SOURCE, f"Wiederherstellen von Exemplar #{copy_id} fehlgeschlagen",
                        f"{type(exc).__name__}: {exc}")
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return {"ok": True, "location": result.location, "identical": result.identical,
            "notes": list(result.notes or [])}


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

@router.get("/tools/papierkorb")
async def trash_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)
    return await render(request, "tools_trash.html", {"retention_days": job_view.TRASH_RETENTION_DAYS})


@router.get("/api/history/trash")
async def trash_list(kind: str = "", page: int = 1):
    kind = kind if kind in ("local", "immich") else ""
    page = max(1, page)
    t = await _t()
    days = await immich_trash_days()
    entries, total = await job_view.trash_entries(
        t, kind=kind or None, offset=(page - 1) * TRASH_PAGE_SIZE, limit=TRASH_PAGE_SIZE,
        immich_trash_days=days)
    return {
        "entries": entries,
        "total": total,
        "page": page,
        "pages": max(1, (total + TRASH_PAGE_SIZE - 1) // TRASH_PAGE_SIZE),
        "kind": kind,
        "counts": await job_view.trash_counts(),
        "retention_days": job_view.TRASH_RETENTION_DAYS,
        "immich_trash_days": days,
    }


# ---------------------------------------------------------------------------
# Inventory check
# ---------------------------------------------------------------------------

# Progress of the running check; the result of the last finished one is
# kept in the config (INVENTORY_KEY) so it survives a restart.
_progress: dict = {"running": False, "phase": None, "current": 0, "total": 0, "started_at": None,
                   "cancel": False, "error": None}


def _finding(ftype: str, row: dict | None = None, **values) -> dict:
    entry = {"type": ftype, "severity": "discrepancy" if ftype in DISCREPANCY_TYPES else "info"}
    if row is not None:
        # The copy's own name (a sidecar is not the photo); Immich: the job's file
        name = row.get("filename") if row["kind"] == "immich" else os.path.basename(row["location"])
        entry.update(copy_id=row["id"], kind=row["kind"], location=row["location"],
                     debug_key=row.get("debug_key"), filename=name)
    entry.update(values)
    return entry


async def run_inventory(copy_ids: list[int] | None = None, *, scan_reprocess: bool = True,
                        persist: bool = True) -> dict:
    """Verify every present (and missing) copy in the register, then look for
    files in reprocess/ the register does not know. Reads only: verify()
    re-hashes and records what it finds, nothing is moved or deleted.

    ``copy_ids`` limits the check to these copies (tests)."""
    started = datetime.now()
    rows = await job_view.copies_to_check(copy_ids)
    _progress.update(running=True, phase="copies", current=0, total=len(rows), started_at=started.isoformat(),
                     cancel=False, error=None)
    await log_info(LOG_SOURCE, "Bestandsprüfung gestartet",
                   f"{len(rows)} Exemplare im Register" + (" (Auswahl)" if copy_ids is not None else ""))
    counts = {"checked": 0, "ok": 0, "unchecked": 0, "skipped": 0}
    findings: list[dict] = []
    busy = await job_view.open_intent_sources()
    immich_failures = 0
    cancelled = False
    for i, row in enumerate(rows):
        if _progress["cancel"]:
            cancelled = True
            break
        _progress["current"] = i + 1
        # Files in flight would read as missing: a job being processed,
        # or an operation still open on this location.
        if row["job_status"] == "processing" or row["location"] in busy:
            counts["skipped"] += 1
            continue
        if row["kind"] == "immich" and immich_failures >= IMMICH_FAILURES_BEFORE_SKIP:
            counts["unchecked"] += 1
            continue
        before = row["state"]
        try:
            checked = await fileops.verify(row["id"], actor="system:verify")
        except Exception as exc:
            counts["unchecked"] += 1
            if row["kind"] == "immich":
                immich_failures += 1
            if len(findings) < MAX_FINDINGS:
                findings.append(_finding("unchecked", row, detail=str(exc)[:300]))
            continue
        if row["kind"] == "immich":
            immich_failures = 0
        counts["checked"] += 1
        after = checked.state
        found = None
        if after == "present":
            counts["ok"] += 1
            if before == "missing":
                found = _finding("recovered", row)
        elif row["kind"] == "immich":
            found = {"trashed": _finding("immich_trashed", row), "locked": _finding("locked", row),
                     "missing": _finding("immich_missing", row)}.get(after)
            if after == "locked":
                counts["ok"] += 1
        elif after == "missing":
            exists = await asyncio.to_thread(os.path.lexists, row["location"])
            found = _finding("mismatch" if exists else "missing", row)
        if found and len(findings) < MAX_FINDINGS:
            findings.append(found)
        await asyncio.sleep(0)

    orphans = assigned = 0
    if scan_reprocess and not cancelled:
        _progress.update(phase="reprocess", current=0, total=0)
        entries, orphans, assigned = await job_view.unregistered_files(REPROCESS_DIR, limit=MAX_FINDINGS)
        for e in entries:
            if len(findings) >= MAX_FINDINGS:
                break
            findings.append(_finding("reprocess_unregistered" if e["debug_key"] else "reprocess_orphan",
                                     None, kind="work", location=e["location"],
                                     debug_key=e["debug_key"], filename=e["filename"]))

    finished = datetime.now()
    by_type: dict[str, int] = {}
    for f in findings:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    # reprocess/ can hold far more files than the list keeps
    if orphans:
        by_type["reprocess_orphan"] = orphans
    if assigned:
        by_type["reprocess_unregistered"] = assigned
    discrepancies = sum(n for k, n in by_type.items() if k in DISCREPANCY_TYPES)
    result = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "total": len(rows),
        "cancelled": cancelled,
        "partial": copy_ids is not None,
        **counts,
        "discrepancies": discrepancies,
        "by_type": by_type,
        "findings": findings,
        "truncated": len(findings) >= MAX_FINDINGS,
    }
    if persist:
        await config_manager.set(INVENTORY_KEY, result)
    log = log_warning if discrepancies or counts["unchecked"] else log_info
    await log(LOG_SOURCE, "Bestandsprüfung abgebrochen" if cancelled else "Bestandsprüfung beendet",
              f"geprüft {counts['checked']}, in Ordnung {counts['ok']}, Abweichungen {discrepancies}, "
              f"nicht prüfbar {counts['unchecked']}, übersprungen (in Arbeit) {counts['skipped']}; "
              f"reprocess/ ohne Job {orphans}, ohne Register-Eintrag {assigned}")
    return result


async def _inventory_task():
    try:
        await run_inventory()
    except Exception as exc:
        logger.exception("inventory check failed")
        _progress["error"] = f"{type(exc).__name__}: {exc}"
        await log_error(LOG_SOURCE, "Bestandsprüfung fehlgeschlagen", _progress["error"])
    finally:
        _progress["running"] = False


@router.get("/tools/bestand")
async def inventory_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)
    return await render(request, "tools_inventory.html", {})


@router.post("/api/history/inventory/start")
async def inventory_start():
    if _progress["running"]:
        return JSONResponse({"ok": False, "busy": True}, status_code=409)
    _progress.update(running=True, phase="starting", current=0, total=0, cancel=False, error=None)
    asyncio.create_task(_inventory_task())
    return {"ok": True}


@router.post("/api/history/inventory/cancel")
async def inventory_cancel():
    if _progress["running"]:
        _progress["cancel"] = True
    return {"ok": True}


@router.get("/api/history/inventory/status")
async def inventory_status():
    return {
        "running": _progress["running"],
        "phase": _progress["phase"],
        "current": _progress["current"],
        "total": _progress["total"],
        "error": _progress["error"],
        "last": await config_manager.get(INVENTORY_KEY),
    }


async def inventory_hint(t: dict) -> dict | None:
    """Row for the navbar status menu while the last check found discrepancies."""
    last = await config_manager.get(INVENTORY_KEY)
    n = (last or {}).get("discrepancies") or 0
    if not n:
        return None
    h = t.get("history", {})
    detail = h.get("inv_hint_one", "") if n == 1 else h.get("inv_hint_many", "{n}").replace("{n}", str(n))
    return {"label": h.get("inv_title", ""), "detail": detail, "href": "/tools/bestand", "count": n}


@router.get("/api/history/overview")
async def overview():
    """Counts for the entries on the Werkzeuge page."""
    last = await config_manager.get(INVENTORY_KEY) or {}
    return {
        "trash": await job_view.trash_counts(),
        "inventory": {k: last.get(k) for k in ("finished_at", "discrepancies", "checked", "cancelled")} if last else None,
        "inventory_running": _progress["running"],
    }

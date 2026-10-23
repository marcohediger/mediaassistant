import asyncio
import logging
import os
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import flag_modified

import fileops
from config import config_manager
from database import async_session
from file_operations import (
    job_asset_ids, job_copies, parse_date, resolve_filepath, sanitize_path_component,
    validate_target_path,
)
from models import Job
from system_logger import log_info, log_warning
from thumbnail_utils import generate_thumbnail, THUMB_SIZE, PREVIEW_SIZE

from template_engine import render

logger = logging.getLogger("mediaassistant.routers.review")

router = APIRouter()

# A dry-run job never reaches a real file action (R-07): it is not listed, and
# classify / delete refuse it.
_NOT_DRY_RUN = or_(Job.dry_run.is_(None), Job.dry_run.is_(False))
_VIDEO_TYPES = ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")


def _is_video(exif: dict) -> bool:
    file_type = (exif.get("file_type") or "").upper()
    return (exif.get("mime_type") or "").startswith("video/") or file_type in _VIDEO_TYPES


async def _build_review_items() -> list[dict]:
    """Load all jobs with status='review' and build display items."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "review", _NOT_DRY_RUN).order_by(Job.created_at.desc())
        )
        jobs = result.scalars().all()

    items = []
    for job in jobs:
        filepath = await resolve_filepath(job)
        exists = bool(filepath) and os.path.isfile(filepath)
        step_results = job.step_result or {}
        exif = step_results.get("IA-01", {})
        ai = step_results.get("IA-05", {})
        geo = step_results.get("IA-03", {})
        sort = step_results.get("IA-08", {})

        asset_ids = job_asset_ids(job)
        immich_asset_id = asset_ids[0] if asset_ids else ""

        # File size: the job's file → IA-01 → Immich API
        file_size_kb = 0
        if exists:
            file_size_kb = os.path.getsize(filepath) / 1024
        elif exif.get("file_size"):
            file_size_kb = exif["file_size"] / 1024
        elif immich_asset_id:
            try:
                from immich_client import get_asset_info, get_user_api_key
                _ukey = await get_user_api_key(job.immich_user_id) if job.immich_user_id else None
                info = await get_asset_info(immich_asset_id, api_key=_ukey)
                if info:
                    exif_info = info.get("exifInfo", {})
                    file_size_kb = (exif_info.get("fileSizeInByte", 0) or 0) / 1024
            except Exception as exc:
                logger.info("size of %s: %s", immich_asset_id, exc)

        location = ""
        if geo.get("city") or geo.get("country"):
            location = ", ".join(p for p in [geo.get("city", ""), geo.get("country", "")] if p)

        items.append({
            "job_id": job.id,
            "set_id": job.set_id,
            "debug_key": job.debug_key,
            "filename": job.filename,
            "filepath": filepath,
            "exists": exists,
            "is_video": _is_video(exif),
            "immich_asset_id": immich_asset_id,
            "file_size_kb": round(file_size_kb),
            "ai_type": ai.get("type", ""),
            "ai_confidence": ai.get("confidence", 0.0),
            "ai_description": ai.get("description", ""),
            "ai_tags": ai.get("tags", []),
            "ai_quality": ai.get("quality", ""),
            "ai_people": ai.get("people_count", 0),
            "exif_date": exif.get("date") or (job.created_at.strftime("%Y-%m-%d %H:%M") if job.created_at else ""),
            "exif_camera": " ".join(p for p in [exif.get("make"), exif.get("model")] if p and p != "None").strip(),
            "dimensions": f"{exif['width']} × {exif['height']}" if exif.get("width") and exif.get("height") else "",
            "has_exif": exif.get("has_exif", False),
            "has_gps": exif.get("gps", False),
            "location": location,
            "sort_reason": sort.get("category", "unknown"),
        })

    return items


def _redirect(code: str | None = None, *, tone: str = "error", **params) -> RedirectResponse:
    """Back to the review page, with a message (translated there: t.review.msg_<code>)."""
    url = "/review"
    if code:
        query = {"msg": code, "tone": tone, **{k: str(v)[:500] for k, v in params.items() if v is not None}}
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/review")
async def review_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    items = await _build_review_items()

    # Every item gets the classify buttons of its own set — categories are
    # per set, so one shared row of buttons would offer keys a job's set
    # may not have.
    from sets import categories_of
    categories_by_set: dict = {}
    async with async_session() as session:
        for set_id in {item["set_id"] for item in items}:
            categories_by_set[set_id] = [c for c in await categories_of(session, set_id)
                                         if not c.fixed]

    notice = None
    if request.query_params.get("msg"):
        tone = request.query_params.get("tone", "error")
        notice = {"code": request.query_params["msg"],
                  "tone": tone if tone in ("error", "warning", "success", "info") else "error",
                  "name": request.query_params.get("name", ""),
                  "detail": request.query_params.get("detail", ""),
                  "n": request.query_params.get("n", "")}

    return await render(request, "review.html", {
        "items": items,
        "total": len(items),
        "categories_by_set": categories_by_set,
        "notice": notice,
    })


@router.get("/api/review/thumbnail/{job_id}")
async def review_thumbnail(job_id: int, size: str = "thumbnail"):
    """Serve a JPEG thumbnail for a review item. size=thumbnail|preview"""
    async with async_session() as session:
        job = await session.get(Job, job_id)
    if not job:
        return Response(status_code=404)

    filepath = await resolve_filepath(job)
    if not filepath or not os.path.isfile(filepath):
        return Response(status_code=404)

    max_size = PREVIEW_SIZE if size == "preview" else THUMB_SIZE
    data = await asyncio.to_thread(generate_thumbnail, filepath, max_size)
    if not data:
        return Response(status_code=404)

    return Response(content=data, media_type="image/jpeg")


def _category_dir(path_template: str, base_path: str, exif: dict, geo: dict) -> str:
    """The category's folder for this file (raises ValueError if it leaves the base)."""
    if geo.get("country"):
        exif = {**exif, "country": geo["country"], "city": geo.get("city", "")}
    date = parse_date(exif.get("date")) or datetime.now()
    make, model = exif.get("make", ""), exif.get("model", "")
    camera = f"{make}-{model}".strip("-") if (make or model) else "unknown"
    camera = camera.replace(" ", "_").replace("/", "_")
    replacements = {
        "{YYYY}": date.strftime("%Y"),
        "{MM}": date.strftime("%m"),
        "{DD}": date.strftime("%d"),
        "{YYYY-MM}": date.strftime("%Y-%m"),
        "{CAMERA}": sanitize_path_component(camera),
        "{TYPE}": sanitize_path_component(exif.get("type", "unknown")),
        "{COUNTRY}": sanitize_path_component(exif.get("country", "")),
        "{CITY}": sanitize_path_component(exif.get("city", "")),
    }
    relative_dir = path_template
    for key, value in replacements.items():
        relative_dir = relative_dir.replace(key, value)
    return validate_target_path(os.path.join(base_path, relative_dir), base_path)


async def _classify_one(job_id: int, category: str, *, actor: str) -> tuple[str | None, str]:
    """File one review job under a category of its own set.

    Immich: archive the asset if the category says so. Local: move the job's
    own file — a copy it owns whose hash checks out now, never a stale
    original_path or the IA-04 JPEG (V2, R-07) — into the category folder
    through fileops. The job's row is committed right after its file moved
    (R-10g). Returns (problem code or None, detail).
    """
    from sets import base_path_for, find_category

    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != "review":
            return "not_found", ""
        if job.dry_run:
            return "dry_run", job.debug_key
        key = category
        if key == "sourceless":
            # The batch button sends "sourceless"; the sets have a photo and a video variant.
            key = "sourceless_video" if _is_video((job.step_result or {}).get("IA-01", {})) else "sourceless_foto"
        lib_cat = await find_category(session, job.set_id, key=key)
        if not lib_cat:
            return "no_category", key
        base_path = await base_path_for(session, job)
        should_archive = bool(lib_cat.immich_archive)
        path_template = lib_cat.path_template

    step_results = dict(job.step_result or {})
    sort_result = dict(step_results.get("IA-08", {}), category=key, manual_review=True)
    asset_ids = job_asset_ids(job)

    if asset_ids:
        asset_id = asset_ids[0]
        if should_archive:
            from immich_client import archive_asset, get_user_api_key
            api_key = await get_user_api_key(job.immich_user_id) if job.immich_user_id else None
            await log_info("review", f"{job.debug_key} Asset wird archiviert (Review)", f"Asset {asset_id} → {key}")
            try:
                await archive_asset(asset_id, api_key=api_key)
            except Exception as exc:
                await log_warning("review", f"{job.debug_key} Archivieren fehlgeschlagen",
                                  f"Asset {asset_id}: {type(exc).__name__}: {exc}")
                return "archive_failed", str(exc)
            await log_info("review", f"{job.debug_key} Asset archiviert (Review)", f"Asset {asset_id}")
        sort_result["immich_archived"] = should_archive
        async with async_session() as session:
            row = await session.get(Job, job_id)
            row.status = "done"
            row.completed_at = datetime.now()
            row.step_result = dict(row.step_result or {}, **{"IA-08": sort_result})
            flag_modified(row, "step_result")
            await session.commit()
        await log_info("review", f"Manuell klassifiziert (Immich): {job.debug_key} → {key}")
        return None, ""

    rows, notes = await job_copies(job)
    own = [r for r in rows if r.job_id == job.id and r.kind in ("local", "work")]
    if not own:
        detail = "; ".join(notes) or "kein geprüftes Exemplar des Jobs"
        async with async_session() as session:
            row = await session.get(Job, job_id)
            if row.status == "review":
                row.status = "error"
                row.error_message = f"Review: Datei nicht gefunden ({detail})"[:2000]
                await session.commit()
        await log_warning("review", f"{job.debug_key} Review: Datei nicht gefunden", detail)
        return "missing", detail
    target = os.path.abspath(job.target_path) if job.target_path and not job.target_path.startswith("immich") else None
    src = next((r for r in own if r.location == target), own[-1]).location

    try:
        target_dir = _category_dir(path_template, base_path, step_results.get("IA-01", {}),
                                   step_results.get("IA-03", {}))
    except ValueError as exc:
        await log_warning("review", f"Path traversal blocked: {job.debug_key}", str(exc))
        return "failed", str(exc)

    try:
        moved = await fileops.move(job, src, os.path.join(target_dir, os.path.basename(src)), actor=actor)
    except fileops.FileOpError as exc:
        return "failed", str(exc)
    sidecar = src + ".xmp"
    if await asyncio.to_thread(os.path.isfile, sidecar):
        try:
            await fileops.move(job, sidecar, moved.location + ".xmp", actor=actor, kind="sidecar")
        except fileops.FileOpError as exc:
            await log_warning("review", f"{job.debug_key} Sidecar nicht mitverschoben",
                              f"{sidecar}: {exc}")

    sort_result.update(target_path=moved.location, target_dir=os.path.dirname(moved.location), moved=True)
    async with async_session() as session:
        row = await session.get(Job, job_id)
        row.target_path = moved.location
        row.status = "done"
        row.completed_at = datetime.now()
        row.step_result = dict(row.step_result or {}, **{"IA-08": sort_result})
        flag_modified(row, "step_result")
        await session.commit()
    await log_info("review", f"Manuell klassifiziert: {job.debug_key} → {key}", f"→ {moved.location}")
    return None, ""


async def _review_job(debug_key: str):
    async with async_session() as session:
        return (await session.execute(
            select(Job).where(Job.debug_key == debug_key, Job.status == "review"))).scalars().first()


@router.post("/api/review/classify")
async def classify_file(request: Request):
    """Classify a review file: move locally or handle in Immich."""
    form = await request.form()
    debug_key = form.get("debug_key")
    category = form.get("category")

    if not debug_key or not category:
        return RedirectResponse(url="/review", status_code=303)

    job = await _review_job(debug_key)
    if not job:
        return RedirectResponse(url="/review", status_code=303)
    problem, detail = await _classify_one(job.id, category, actor="user:review-classify")
    if problem in (None, "not_found", "no_category"):
        return RedirectResponse(url="/review", status_code=303)
    return _redirect(f"classify_{problem}", name=job.filename, detail=detail)


@router.post("/api/review/delete")
async def delete_file(request: Request):
    """Delete a review file: its own copies go to the trash (local trash / Immich trash).

    Only copies the job owns are touched — never what lies at a stale
    original_path (R-02). A copy another job uses stops the delete as a
    whole, and the user is told why (R-08); nothing falls back to a hard
    delete.
    """
    form = await request.form()
    debug_key = form.get("debug_key")
    if not debug_key:
        return RedirectResponse(url="/review", status_code=303)

    job = await _review_job(debug_key)
    if not job:
        return RedirectResponse(url="/review", status_code=303)
    if job.dry_run:
        return _redirect("delete_dry_run", name=job.filename)

    actor, reason = "user:review-delete", "Im Review gelöscht"
    rows, notes = await job_copies(job, check=False)
    own = [r for r in rows if r.job_id == job.id]
    blocked = []
    for row in own:
        if row.kind == "temp":
            continue
        reasons = await fileops.trash_blockers(job, row, allow_last_copy=True)
        if reasons:
            blocked.append(f"{os.path.basename(row.location)}: {'; '.join(reasons)}")
    if blocked:
        detail = " · ".join(blocked)
        await log_warning("review", f"{job.debug_key} Löschen verweigert", detail)
        return _redirect("delete_refused", name=job.filename, detail=detail)

    await log_info("review", f"{job.debug_key} Löschen beginnt (Papierkorb)",
                   f"{len(own)} Exemplar(e): {[r.location for r in own]}" + (f"; {'; '.join(notes)}" if notes else ""))
    failed, trashed, local_gone = [], 0, False
    for row in own:
        try:
            if row.kind == "temp":
                await fileops.discard(job, row, actor=actor, reason=reason)
                continue
            if row.kind == "immich":
                await fileops.trash_asset(job, row, actor=actor, reason=reason, allow_last_copy=True)
            else:
                await fileops.trash(job, row, actor=actor, reason=reason, allow_last_copy=True)
                local_gone = local_gone or row.location == os.path.abspath(job.target_path or "")
            trashed += 1
        except fileops.FileOpError as exc:
            failed.append(f"{os.path.basename(row.location)}: {exc}")
    for row in rows:
        if row.job_id != job.id:
            await fileops.drop_ref(job, row.kind, row.location)

    # The IA-04 JPEG, if IA-10 left one behind (names built from the debug key only)
    ia04 = (job.step_result or {}).get("IA-04") or {}
    temps = [p for p in [ia04.get("temp_path"), *(ia04.get("temp_paths") or [])] if p]
    if temps:
        from pipeline.step_ia04_convert import discard_temp_files
        _removed, _kept, temp_failed = await discard_temp_files(job, temps, actor=actor, reason=reason)
        for line in temp_failed:
            logger.warning("%s temp file: %s", job.debug_key, line)

    if failed:
        await log_warning("review", f"{job.debug_key} Löschen unvollständig", "; ".join(failed))
        async with async_session() as session:
            row = await session.get(Job, job.id)
            if local_gone and row.target_path and not row.target_path.startswith("immich"):
                row.target_path = None
                await session.commit()
        return _redirect("delete_failed", name=job.filename, detail="; ".join(failed))

    async with async_session() as session:
        row = await session.get(Job, job.id)
        row.status = "deleted"
        row.completed_at = datetime.now()
        row.error_message = "Im Review gelöscht (Papierkorb)"
        if row.target_path and not row.target_path.startswith("immich"):
            row.target_path = None
        await session.commit()

    await log_info("review", f"Gelöscht: {debug_key}", f"{trashed} Exemplar(e) im Papierkorb")
    return RedirectResponse(url="/review", status_code=303)


@router.post("/api/review/classify-all")
async def classify_all(request: Request):
    """Classify all review items as sourceless (batch action), one job and one commit at a time."""
    form = await request.form()
    category = form.get("category")

    if not category:
        return RedirectResponse(url="/review", status_code=303)

    async with async_session() as session:
        ids = list((await session.execute(
            select(Job.id).where(Job.status == "review", _NOT_DRY_RUN))).scalars().all())

    count, skipped, failed = 0, 0, []
    for job_id in ids:
        try:
            problem, detail = await _classify_one(job_id, category, actor="user:review-classify-all")
        except Exception as exc:
            logger.exception("classify-all: job %s", job_id)
            problem, detail = "failed", f"{type(exc).__name__}: {exc}"
        if problem is None:
            count += 1
        elif problem == "no_category":
            skipped += 1
        elif problem != "not_found":
            failed.append(f"#{job_id}: {detail or problem}")

    await log_info("review", f"Batch: {count} Dateien → {category}",
                   "; ".join(filter(None, [
                       f"{skipped} übersprungen — Kategorie fehlt in ihrem Set" if skipped else "",
                       f"{len(failed)} nicht abgelegt: {'; '.join(failed[:10])}" if failed else "",
                   ])))
    if failed:
        return _redirect("batch_partial", tone="warning", n=len(failed), detail="; ".join(failed[:5]))
    return RedirectResponse(url="/review", status_code=303)

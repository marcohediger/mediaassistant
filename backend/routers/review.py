import asyncio
import os
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from config import config_manager
from database import async_session
from file_operations import (
    resolve_filepath, sanitize_path_component, validate_target_path, parse_date,
)
from models import Job
from safe_file import safe_move
from system_logger import log_info, log_warning
from thumbnail_utils import generate_thumbnail, THUMB_SIZE, PREVIEW_SIZE

from template_engine import render

router = APIRouter()


async def _build_review_items() -> list[dict]:
    """Load all jobs with status='review' and build display items."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "review").order_by(Job.created_at.desc())
        )
        jobs = result.scalars().all()

        items = []
        for job in jobs:
            filepath = resolve_filepath(job)
            exists = os.path.exists(filepath)
            step_results = job.step_result or {}
            exif = step_results.get("IA-01", {})
            ai = step_results.get("IA-05", {})
            geo = step_results.get("IA-03", {})
            sort = step_results.get("IA-08", {})

            # Immich asset info
            immich_asset_id = ""
            target = job.target_path or ""
            if target.startswith("immich:"):
                immich_asset_id = target[7:]
            elif job.immich_asset_id:
                immich_asset_id = job.immich_asset_id

            # File size: local file → IA-01 → Immich API
            file_size_kb = 0
            if exists:
                file_size_kb = os.path.getsize(filepath) / 1024
            elif exif.get("file_size"):
                file_size_kb = exif["file_size"] / 1024
            elif job.original_path and os.path.exists(job.original_path):
                file_size_kb = os.path.getsize(job.original_path) / 1024
            elif immich_asset_id:
                try:
                    from immich_client import get_asset_info, get_user_api_key
                    _ukey = await get_user_api_key(job.immich_user_id) if job.immich_user_id else None
                    info = await get_asset_info(immich_asset_id, api_key=_ukey)
                    if info:
                        exif_info = info.get("exifInfo", {})
                        file_size_kb = (exif_info.get("fileSizeInByte", 0) or 0) / 1024
                except Exception:
                    pass

            # Location string
            location = ""
            if geo.get("city") or geo.get("country"):
                parts = [p for p in [geo.get("city", ""), geo.get("country", "")] if p]
                location = ", ".join(parts)

            # Determine if video
            mime = exif.get("mime_type", "")
            file_type = (exif.get("file_type") or "").upper()
            is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")

            items.append({
                "job_id": job.id,
                "debug_key": job.debug_key,
                "filename": job.filename,
                "filepath": filepath,
                "exists": exists,
                "is_video": is_video,
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


@router.get("/review")
async def review_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    items = await _build_review_items()

    # Load categories for classify buttons
    from models import LibraryCategory
    async with async_session() as session:
        cat_result = await session.execute(
            select(LibraryCategory)
            .where(LibraryCategory.fixed == False)
            .order_by(LibraryCategory.position)
        )
        review_categories = cat_result.scalars().all()

    return await render(request, "review.html", {
        "items": items,
        "total": len(items),
        "review_categories": review_categories,
    })


@router.get("/api/review/thumbnail/{job_id}")
async def review_thumbnail(job_id: int, size: str = "thumbnail"):
    """Serve a JPEG thumbnail for a review item. size=thumbnail|preview"""
    async with async_session() as session:
        job = await session.get(Job, job_id)
    if not job:
        return Response(status_code=404)

    filepath = resolve_filepath(job)
    if not os.path.exists(filepath):
        return Response(status_code=404)

    max_size = PREVIEW_SIZE if size == "preview" else THUMB_SIZE
    data = await asyncio.to_thread(generate_thumbnail, filepath, max_size)
    if not data:
        return Response(status_code=404)

    return Response(content=data, media_type="image/jpeg")


@router.post("/api/review/classify")
async def classify_file(request: Request):
    """Classify a review file: move locally or handle in Immich."""
    from sqlalchemy.orm.attributes import flag_modified
    from immich_client import archive_asset

    form = await request.form()
    debug_key = form.get("debug_key")
    category = form.get("category")

    if not debug_key or not category:
        return RedirectResponse(url="/review", status_code=303)

    # Validate category exists in DB
    from models import LibraryCategory
    async with async_session() as session_check:
        valid = (await session_check.execute(
            select(LibraryCategory).where(LibraryCategory.key == category)
        )).scalar()
    if not valid:
        return RedirectResponse(url="/review", status_code=303)

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key == debug_key, Job.status == "review")
        )
        job = result.scalars().first()
        if not job:
            return RedirectResponse(url="/review", status_code=303)

        step_results = job.step_result or {}
        exif = step_results.get("IA-01", {})
        geo = step_results.get("IA-03", {})
        immich_asset_id = job.immich_asset_id or ""
        target = job.target_path or ""
        is_immich = target.startswith("immich:") or bool(immich_asset_id)

        # ── Immich-Modus ──────────────────────────────────────────
        if is_immich:
            asset_id = immich_asset_id or target.replace("immich:", "")

            # Check if category should be archived in Immich
            from models import LibraryCategory
            lib_cat = (await session.execute(
                select(LibraryCategory).where(LibraryCategory.key == category)
            )).scalar()
            should_archive = lib_cat.immich_archive if lib_cat else False

            if should_archive and asset_id:
                _ukey = None
                if job.immich_user_id:
                    from immich_client import get_user_api_key
                    _ukey = await get_user_api_key(job.immich_user_id)
                await archive_asset(asset_id, api_key=_ukey)

            job.status = "done"
            job.completed_at = datetime.now()

            sort_result = dict(step_results.get("IA-08", {}))
            sort_result["category"] = category
            sort_result["manual_review"] = True
            sort_result["immich_archived"] = should_archive
            step_results["IA-08"] = sort_result
            job.step_result = step_results
            flag_modified(job, "step_result")

            await session.commit()
            await log_info("review", f"Manuell klassifiziert (Immich): {debug_key} → {category}")
            return RedirectResponse(url="/review", status_code=303)

        # ── Lokale Ablage ─────────────────────────────────────────
        filepath = resolve_filepath(job)
        if not os.path.exists(filepath):
            job.status = "error"
            job.error_message = "Review: Datei nicht gefunden"
            await session.commit()
            return RedirectResponse(url="/review", status_code=303)

        # Merge geo into exif for path resolution
        if geo.get("country"):
            exif = {**exif, "country": geo["country"], "city": geo.get("city", "")}

        # Get path template from library_categories table
        from models import LibraryCategory
        cat_result = await session.execute(
            select(LibraryCategory).where(LibraryCategory.key == category)
        )
        lib_cat = cat_result.scalar()
        if lib_cat:
            path_template = lib_cat.path_template
        else:
            path_template = "unknown/review/"
        base_path = await config_manager.get("library.base_path", "/library")

        # Parse date
        date = parse_date(exif.get("date"))
        if not date:
            date = datetime.now()

        # Resolve path template
        make = exif.get("make", "")
        model = exif.get("model", "")
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

        target_dir = os.path.join(base_path, relative_dir)

        # Security: ensure target stays within library
        try:
            target_dir = validate_target_path(target_dir, base_path)
        except ValueError as e:
            await log_warning("review", f"Path traversal blocked: {debug_key}", str(e))
            return RedirectResponse(url="/review", status_code=303)

        filename = os.path.basename(filepath)
        target_path = os.path.join(target_dir, filename)

        # Handle name conflicts
        from file_operations import resolve_filename_conflict
        await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
        target_path = resolve_filename_conflict(target_dir, filename)

        # Move file
        await asyncio.to_thread(safe_move, filepath, target_path, job.debug_key)

        # Update job
        job.target_path = target_path
        job.status = "done"
        job.completed_at = datetime.now()

        sort_result = dict(step_results.get("IA-08", {}))
        sort_result["category"] = category
        sort_result["target_path"] = target_path
        sort_result["target_dir"] = target_dir
        sort_result["moved"] = True
        sort_result["manual_review"] = True
        step_results["IA-08"] = sort_result
        job.step_result = step_results
        flag_modified(job, "step_result")

        await session.commit()

    await log_info("review", f"Manuell klassifiziert: {debug_key} → {category}")
    return RedirectResponse(url="/review", status_code=303)


@router.post("/api/review/delete")
async def delete_file(request: Request):
    """Delete a review file — removes from Immich and/or local filesystem."""
    from immich_client import get_immich_config

    form = await request.form()
    debug_key = form.get("debug_key")
    if not debug_key:
        return RedirectResponse(url="/review", status_code=303)

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key == debug_key, Job.status == "review")
        )
        job = result.scalars().first()
        if not job:
            return RedirectResponse(url="/review", status_code=303)

        # Delete from Immich if applicable
        immich_asset_id = job.immich_asset_id or ""
        target = job.target_path or ""
        if target.startswith("immich:"):
            immich_asset_id = target.replace("immich:", "")

        if immich_asset_id:
            try:
                from immich_client import delete_asset
                await delete_asset(immich_asset_id)
            except Exception:
                pass

        # Delete local files if they exist
        from file_operations import safe_remove
        for path in [job.original_path, job.target_path]:
            if path and not path.startswith("immich:") and os.path.exists(path):
                await asyncio.to_thread(safe_remove, path)

        # Delete temp converted file
        convert_result = (job.step_result or {}).get("IA-04", {})
        temp_path = convert_result.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            await asyncio.to_thread(safe_remove, temp_path)

        # Mark job as deleted
        job.status = "deleted"
        job.completed_at = datetime.now()
        await session.commit()

    await log_info("review", f"Gelöscht: {debug_key}")
    return RedirectResponse(url="/review", status_code=303)


@router.post("/api/review/classify-all")
async def classify_all(request: Request):
    """Classify all review items as sourceless (batch action)."""
    from immich_client import archive_asset

    form = await request.form()
    category = form.get("category")

    if not category:
        return RedirectResponse(url="/review", status_code=303)
    # Validate category exists
    from models import LibraryCategory
    async with async_session() as session_check:
        valid = (await session_check.execute(
            select(LibraryCategory).where(LibraryCategory.key == category)
        )).scalar()
    if not valid:
        return RedirectResponse(url="/review", status_code=303)

    count = 0
    async with async_session() as session:
        # Load target category from DB
        lib_cat = (await session.execute(
            select(LibraryCategory).where(LibraryCategory.key == category)
        )).scalar()
        should_archive = lib_cat.immich_archive if lib_cat else False

        result = await session.execute(
            select(Job).where(Job.status == "review")
        )
        jobs = result.scalars().all()

        for job in jobs:
            target = job.target_path or ""
            is_immich = target.startswith("immich:") or bool(job.immich_asset_id)

            # ── Immich: Archivieren wenn konfiguriert ────────
            if is_immich:
                asset_id = job.immich_asset_id or target.replace("immich:", "")
                if should_archive and asset_id:
                    try:
                        _ukey = None
                        if job.immich_user_id:
                            from immich_client import get_user_api_key
                            _ukey = await get_user_api_key(job.immich_user_id)
                        await archive_asset(asset_id, api_key=_ukey)
                    except Exception:
                        continue
                job.status = "done"
                job.completed_at = datetime.now()
                count += 1
                continue

            # ── Lokal: Verschieben ────────────────────────────
            filepath = resolve_filepath(job)
            if not os.path.exists(filepath):
                continue

            step_results = job.step_result or {}
            exif = step_results.get("IA-01", {})

            base_path = await config_manager.get("library.base_path", "/library")
            path_template = lib_cat.path_template if lib_cat else "unknown/review/"

            date = parse_date(exif.get("date")) or datetime.now()
            relative_dir = path_template.replace("{YYYY}", date.strftime("%Y")).replace("{MM}", date.strftime("%m"))
            target_dir = os.path.join(base_path, relative_dir)

            # Security: ensure target stays within library
            try:
                target_dir = validate_target_path(target_dir, base_path)
            except ValueError as e:
                await log_warning("review", f"Path traversal blocked: {job.debug_key}", str(e))
                continue

            filename = os.path.basename(filepath)
            from file_operations import resolve_filename_conflict
            await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
            target_path = resolve_filename_conflict(target_dir, filename)
            await asyncio.to_thread(safe_move, filepath, target_path, job.debug_key)

            job.target_path = target_path
            job.status = "done"
            job.completed_at = datetime.now()
            count += 1

        await session.commit()

    await log_info("review", f"Batch: {count} Dateien → {category}")
    return RedirectResponse(url="/review", status_code=303)

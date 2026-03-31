import asyncio
import io
import os
import re
import subprocess
import tempfile
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from PIL import Image
from sqlalchemy import select

from config import config_manager
from database import async_session
from models import Job
from safe_file import safe_move
from system_logger import log_info, log_warning

from template_engine import render


def _sanitize_path_component(value: str) -> str:
    """Remove dangerous characters from path components to prevent path traversal."""
    if not value:
        return "unknown"
    value = value.replace("..", "").replace("/", "_").replace("\\", "_")
    value = re.sub(r'[\x00-\x1f]', '', value)
    return value.strip() or "unknown"


def _validate_target_path(target_dir: str, base_path: str) -> str:
    """Ensure target directory is within base_path (defense in depth)."""
    target_real = os.path.realpath(target_dir)
    base_real = os.path.realpath(base_path)
    if not target_real.startswith(base_real + os.sep) and target_real != base_real:
        raise ValueError(
            f"Security: target path escapes library boundary "
            f"(target={target_dir}, base={base_path})"
        )
    return target_real

router = APIRouter()

THUMB_SIZE = (400, 400)
PREVIEW_SIZE = (1600, 1600)
HEIC_EXTENSIONS = {".heic", ".heif"}


def _heic_to_jpeg(filepath: str) -> bytes | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=True) as tmp:
            subprocess.run(
                ["heif-convert", "-q", "80", filepath, tmp.name],
                capture_output=True, timeout=15, check=True,
            )
            with open(tmp.name, "rb") as f:
                return f.read()
    except Exception:
        return None


def _generate_thumbnail(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in HEIC_EXTENSIONS:
        jpeg_data = _heic_to_jpeg(filepath)
        if not jpeg_data:
            return None
        img = Image.open(io.BytesIO(jpeg_data))
    else:
        try:
            img = Image.open(filepath)
        except Exception:
            return None

    img.thumbnail(max_size, Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _resolve_filepath(job) -> str:
    for path in [job.target_path, job.original_path]:
        if path and os.path.exists(path):
            return path
    convert_result = (job.step_result or {}).get("IA-04", {})
    temp_path = convert_result.get("temp_path")
    if temp_path and os.path.exists(temp_path):
        return temp_path
    return job.target_path or job.original_path


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except (ValueError, TypeError):
            continue
    return None


async def _build_review_items() -> list[dict]:
    """Load all jobs with status='review' and build display items."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "review").order_by(Job.created_at.desc())
        )
        jobs = result.scalars().all()

        items = []
        for job in jobs:
            filepath = _resolve_filepath(job)
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
                    from immich_client import get_asset_info
                    info = await get_asset_info(immich_asset_id)
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

    filepath = _resolve_filepath(job)
    if not os.path.exists(filepath):
        return Response(status_code=404)

    max_size = PREVIEW_SIZE if size == "preview" else THUMB_SIZE
    data = await asyncio.to_thread(_generate_thumbnail, filepath, max_size)
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
                await archive_asset(asset_id)

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
        filepath = _resolve_filepath(job)
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
        date = _parse_date(exif.get("date"))
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
            "{CAMERA}": _sanitize_path_component(camera),
            "{TYPE}": _sanitize_path_component(exif.get("type", "unknown")),
            "{COUNTRY}": _sanitize_path_component(exif.get("country", "")),
            "{CITY}": _sanitize_path_component(exif.get("city", "")),
        }
        relative_dir = path_template
        for key, value in replacements.items():
            relative_dir = relative_dir.replace(key, value)

        target_dir = os.path.join(base_path, relative_dir)

        # Security: ensure target stays within library
        try:
            target_dir = _validate_target_path(target_dir, base_path)
        except ValueError as e:
            await log_warning("review", f"Path traversal blocked: {debug_key}", str(e))
            return RedirectResponse(url="/review", status_code=303)

        filename = os.path.basename(filepath)
        target_path = os.path.join(target_dir, filename)

        # Handle name conflicts
        if os.path.exists(target_path):
            name, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(target_path):
                target_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
                counter += 1

        # Move file
        await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
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
                url, api_key = await get_immich_config()
                if url and api_key:
                    import httpx, json as _json
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.request(
                            "DELETE",
                            f"{url}/api/assets",
                            headers={"x-api-key": api_key, "Content-Type": "application/json"},
                            content=_json.dumps({"ids": [immich_asset_id]}),
                        )
            except Exception:
                pass

        # Delete local files if they exist
        for path in [job.original_path, job.target_path]:
            if path and not path.startswith("immich:") and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        # Delete temp converted file
        convert_result = (job.step_result or {}).get("IA-04", {})
        temp_path = convert_result.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

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
                        await archive_asset(asset_id)
                    except Exception:
                        continue
                job.status = "done"
                job.completed_at = datetime.now()
                count += 1
                continue

            # ── Lokal: Verschieben ────────────────────────────
            filepath = _resolve_filepath(job)
            if not os.path.exists(filepath):
                continue

            step_results = job.step_result or {}
            exif = step_results.get("IA-01", {})

            base_path = await config_manager.get("library.base_path", "/library")
            path_template = lib_cat.path_template if lib_cat else "unknown/review/"

            date = _parse_date(exif.get("date")) or datetime.now()
            relative_dir = path_template.replace("{YYYY}", date.strftime("%Y")).replace("{MM}", date.strftime("%m"))
            target_dir = os.path.join(base_path, relative_dir)

            # Security: ensure target stays within library
            try:
                target_dir = _validate_target_path(target_dir, base_path)
            except ValueError as e:
                await log_warning("review", f"Path traversal blocked: {job.debug_key}", str(e))
                continue

            filename = os.path.basename(filepath)
            target_path = os.path.join(target_dir, filename)

            if os.path.exists(target_path):
                name, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(target_path):
                    target_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
                    counter += 1

            await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
            await asyncio.to_thread(safe_move, filepath, target_path, job.debug_key)

            job.target_path = target_path
            job.status = "done"
            job.completed_at = datetime.now()
            count += 1

        await session.commit()

    await log_info("review", f"Batch: {count} Dateien → {category}")
    return RedirectResponse(url="/review", status_code=303)

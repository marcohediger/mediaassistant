import asyncio
import io
import os
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
from system_logger import log_info

from template_engine import render

router = APIRouter()

THUMB_SIZE = (400, 400)
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


def _generate_thumbnail(filepath: str) -> bytes | None:
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

    img.thumbnail(THUMB_SIZE, Image.LANCZOS)
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

            file_size_kb = 0
            if exists:
                file_size_kb = os.path.getsize(filepath) / 1024

            # Location string
            location = ""
            if geo.get("city") or geo.get("country"):
                parts = [p for p in [geo.get("city", ""), geo.get("country", "")] if p]
                location = ", ".join(parts)

            # Determine if video
            mime = exif.get("mime_type", "")
            file_type = (exif.get("file_type") or "").upper()
            is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")

            # Immich asset info
            immich_asset_id = ""
            target = job.target_path or ""
            if target.startswith("immich:"):
                immich_asset_id = target[7:]
            elif job.immich_asset_id:
                immich_asset_id = job.immich_asset_id

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
                "exif_date": exif.get("date", ""),
                "exif_camera": f"{exif.get('make', '')} {exif.get('model', '')}".strip(),
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
    return await render(request, "review.html", {
        "items": items,
        "total": len(items),
    })


@router.get("/api/review/thumbnail/{job_id}")
async def review_thumbnail(job_id: int):
    """Serve a JPEG thumbnail for a review item."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
    if not job:
        return Response(status_code=404)

    filepath = _resolve_filepath(job)
    if not os.path.exists(filepath):
        return Response(status_code=404)

    data = await asyncio.to_thread(_generate_thumbnail, filepath)
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

    if not debug_key or category not in ("photo", "video", "screenshot", "sourceless"):
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

            if category == "sourceless" and asset_id:
                # Sourceless → in Immich archivieren
                await archive_asset(asset_id)

            # Foto/Video/Screenshot → bleibt in Timeline (nichts zu tun)

            job.status = "done"
            job.completed_at = datetime.now()

            sort_result = dict(step_results.get("IA-08", {}))
            sort_result["category"] = category
            sort_result["manual_review"] = True
            sort_result["immich_archived"] = category == "sourceless"
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

        # Get path template
        category_key_map = {
            "photo": "library.path_photo",
            "sourceless": "library.path_sourceless",
            "screenshot": "library.path_screenshot",
            "video": "library.path_video",
        }
        defaults = {
            "photo": "photos/{YYYY}/{YYYY-MM}/",
            "sourceless": "sourceless/{YYYY}/",
            "screenshot": "screenshots/{YYYY}/",
            "video": "videos/{YYYY}/{YYYY-MM}/",
        }

        config_key = category_key_map[category]
        default_path = defaults[category]
        path_template = await config_manager.get(config_key, default_path)
        base_path = await config_manager.get("library.base_path", "/bibliothek")

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
            "{CAMERA}": camera,
            "{TYPE}": exif.get("type", "unknown"),
            "{COUNTRY}": exif.get("country", ""),
            "{CITY}": exif.get("city", ""),
        }
        relative_dir = path_template
        for key, value in replacements.items():
            relative_dir = relative_dir.replace(key, value)

        target_dir = os.path.join(base_path, relative_dir)
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


@router.post("/api/review/classify-all")
async def classify_all(request: Request):
    """Classify all review items as sourceless (batch action)."""
    from immich_client import archive_asset

    form = await request.form()
    category = form.get("category")

    if category not in ("sourceless",):
        return RedirectResponse(url="/review", status_code=303)

    count = 0
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "review")
        )
        jobs = result.scalars().all()

        for job in jobs:
            target = job.target_path or ""
            is_immich = target.startswith("immich:") or bool(job.immich_asset_id)

            # ── Immich: Archivieren ───────────────────────────
            if is_immich:
                asset_id = job.immich_asset_id or target.replace("immich:", "")
                if asset_id:
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

            base_path = await config_manager.get("library.base_path", "/bibliothek")
            path_template = await config_manager.get("library.path_sourceless", "sourceless/{YYYY}/")

            date = _parse_date(exif.get("date")) or datetime.now()
            relative_dir = path_template.replace("{YYYY}", date.strftime("%Y")).replace("{MM}", date.strftime("%m"))
            target_dir = os.path.join(base_path, relative_dir)
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

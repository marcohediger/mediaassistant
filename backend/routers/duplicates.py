import asyncio
import io
import os
import subprocess
import tempfile
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy import select, func

from config import config_manager
from database import async_session
from models import Job
from system_logger import log_info

router = APIRouter()
templates = Jinja2Templates(directory="templates")

THUMB_SIZE = (400, 400)
HEIC_EXTENSIONS = {".heic", ".heif"}


def _heic_to_jpeg(filepath: str) -> bytes | None:
    """Convert HEIC to JPEG bytes using heif-convert."""
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
    """Generate a JPEG thumbnail from an image file."""
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


def _get_image_info(filepath: str) -> dict:
    """Get image dimensions and file size via exiftool (works for all formats)."""
    info = {"file_size": 0, "width": 0, "height": 0, "megapixel": 0.0}
    try:
        info["file_size"] = os.path.getsize(filepath)
        result = subprocess.run(
            ["exiftool", "-ImageWidth", "-ImageHeight", "-s3", filepath],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            info["width"] = int(lines[0])
            info["height"] = int(lines[1])
            info["megapixel"] = round(info["width"] * info["height"] / 1_000_000, 1)
    except Exception:
        pass
    return info


async def _build_duplicate_groups() -> list[dict]:
    """Build groups of duplicate files linked to their originals."""
    async with async_session() as session:
        # Get all duplicate jobs
        result = await session.execute(
            select(Job).where(Job.status == "duplicate").order_by(Job.created_at.desc())
        )
        dup_jobs = result.scalars().all()

        if not dup_jobs:
            return []

        # Group by original_debug_key
        groups_map = defaultdict(list)
        for job in dup_jobs:
            dup_info = (job.step_result or {}).get("IA-03", {})
            original_key = dup_info.get("original_debug_key", "unknown")
            groups_map[original_key].append(job)

        # Build groups with original job info
        groups = []
        for original_key, duplicates in groups_map.items():
            result = await session.execute(
                select(Job).where(Job.debug_key == original_key)
            )
            original = result.scalars().first()

            members = []

            # Add original as first member
            if original:
                filepath = original.target_path or original.original_path
                ai_result = (original.step_result or {}).get("IA-04", {})
                exif = (original.step_result or {}).get("IA-01", {})
                img_info = await asyncio.to_thread(_get_image_info, filepath) if os.path.exists(filepath) else {}
                members.append({
                    "job_id": original.id,
                    "debug_key": original.debug_key,
                    "filename": original.filename,
                    "filepath": filepath,
                    "exists": os.path.exists(filepath),
                    "is_original": True,
                    "match_type": "original",
                    "phash_distance": 0,
                    "quality": ai_result.get("quality", "—"),
                    "confidence": ai_result.get("confidence", 0),
                    "has_exif": exif.get("has_exif", False),
                    "has_gps": bool(exif.get("gps")),
                    **img_info,
                })

            # Add duplicates
            for dup in duplicates:
                filepath = dup.target_path or dup.original_path
                dup_info = (dup.step_result or {}).get("IA-03", {})
                ai_result = (dup.step_result or {}).get("IA-04", {}) or ((original.step_result or {}).get("IA-04", {}) if original else {})
                exif = (dup.step_result or {}).get("IA-01", {})
                img_info = await asyncio.to_thread(_get_image_info, filepath) if os.path.exists(filepath) else {}
                members.append({
                    "job_id": dup.id,
                    "debug_key": dup.debug_key,
                    "filename": dup.filename,
                    "filepath": filepath,
                    "exists": os.path.exists(filepath),
                    "is_original": False,
                    "match_type": dup_info.get("match_type", "unknown"),
                    "phash_distance": dup_info.get("phash_distance", 0),
                    "quality": ai_result.get("quality", "—"),
                    "confidence": ai_result.get("confidence", 0),
                    "has_exif": exif.get("has_exif", False),
                    "has_gps": bool(exif.get("gps")),
                    **img_info,
                })

            groups.append({
                "original_key": original_key,
                "members": members,
                "count": len(members),
                "all_exact": all(
                    m["match_type"] in ("exact", "original") for m in members
                ),
            })

    return groups


@router.get("/duplicates")
async def duplicates_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    groups = await _build_duplicate_groups()
    return templates.TemplateResponse(request, "duplicates.html", {
        "groups": groups,
        "total_groups": len(groups),
        "exact_groups": sum(1 for g in groups if g["all_exact"]),
    })


@router.get("/api/thumbnail/{job_id}")
async def thumbnail(job_id: int):
    """Serve a JPEG thumbnail for a job's image."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
    if not job:
        return Response(status_code=404)

    filepath = job.target_path or job.original_path

    # For HEIC/RAW, check if there's a converted temp file
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".heic", ".heif", ".dng", ".cr2", ".nef", ".arw"):
        convert_result = (job.step_result or {}).get("IA-02", {})
        temp_path = convert_result.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            filepath = temp_path

    if not os.path.exists(filepath):
        return Response(status_code=404)

    data = await asyncio.to_thread(_generate_thumbnail, filepath)
    if not data:
        return Response(status_code=404)

    return Response(content=data, media_type="image/jpeg")


@router.post("/api/duplicates/keep")
async def keep_file(request: Request):
    """Keep one file from a group, delete the rest."""
    form = await request.form()
    keep_key = form.get("keep_key")
    group_key = form.get("group_key")

    if not keep_key or not group_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        # Get all duplicates in this group
        result = await session.execute(
            select(Job).where(Job.status == "duplicate")
        )
        all_dups = result.scalars().all()

        for dup in all_dups:
            dup_info = (dup.step_result or {}).get("IA-03", {})
            if dup_info.get("original_debug_key") != group_key:
                continue
            if dup.debug_key == keep_key:
                continue

            # Delete the duplicate file
            filepath = dup.target_path or dup.original_path
            if os.path.exists(filepath):
                await asyncio.to_thread(os.remove, filepath)
                # Remove .log file
                log_path = filepath + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

            dup.status = "done"
            dup.error_message = f"Duplikat gelöscht (behalten: {keep_key})"

        # If the kept file is a duplicate (not the original), update its status
        result = await session.execute(
            select(Job).where(Job.debug_key == keep_key)
        )
        kept_job = result.scalars().first()
        if kept_job and kept_job.status == "duplicate":
            # Move the kept duplicate to the original's location
            original_result = await session.execute(
                select(Job).where(Job.debug_key == group_key)
            )
            original_job = original_result.scalars().first()
            if original_job:
                kept_job.status = "done"
                kept_job.error_message = f"Duplikat-Review: als beste Version behalten"

        await session.commit()

    await log_info("duplicates", f"Review abgeschlossen für Gruppe {group_key}, behalten: {keep_key}")
    return RedirectResponse(url="/duplicates", status_code=303)


@router.post("/api/duplicates/skip")
async def skip_group(request: Request):
    """Skip a group for now (no action)."""
    return RedirectResponse(url="/duplicates", status_code=303)


@router.post("/api/duplicates/delete-all")
async def delete_duplicate(request: Request):
    """Delete a single duplicate file."""
    form = await request.form()
    debug_key = form.get("debug_key")

    if not debug_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key == debug_key, Job.status == "duplicate")
        )
        job = result.scalars().first()
        if job:
            filepath = job.target_path or job.original_path
            if os.path.exists(filepath):
                await asyncio.to_thread(os.remove, filepath)
                log_path = filepath + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

            job.status = "done"
            job.error_message = "Duplikat gelöscht (manuell)"
            await session.commit()

    await log_info("duplicates", f"Duplikat gelöscht: {debug_key}")
    return RedirectResponse(url="/duplicates", status_code=303)


@router.post("/api/duplicates/batch-clean")
async def batch_clean():
    """Auto-delete all exact SHA256 duplicates (keep original)."""
    deleted = 0
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "duplicate")
        )
        dups = result.scalars().all()

        for dup in dups:
            dup_info = (dup.step_result or {}).get("IA-03", {})
            if dup_info.get("match_type") != "exact":
                continue

            filepath = dup.target_path or dup.original_path
            if os.path.exists(filepath):
                await asyncio.to_thread(os.remove, filepath)
                log_path = filepath + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

            dup.status = "done"
            dup.error_message = "Duplikat gelöscht (Batch-Clean, SHA256 exakt)"
            deleted += 1

        await session.commit()

    await log_info("duplicates", f"Batch-Clean: {deleted} exakte Duplikate gelöscht")
    return RedirectResponse(url="/duplicates", status_code=303)

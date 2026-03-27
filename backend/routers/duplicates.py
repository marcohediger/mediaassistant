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


def _union_find_groups(links: list[tuple[str, str]]) -> dict[str, set[str]]:
    """Transitively merge linked keys into groups using union-find."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in links:
        union(a, b)

    groups = defaultdict(set)
    for key in parent:
        groups[find(key)].add(key)
    return groups


async def _build_member(job, session) -> dict:
    """Build a member dict for a single job."""
    filepath = job.target_path or job.original_path
    dup_info = (job.step_result or {}).get("IA-03", {})
    ai_result = (job.step_result or {}).get("IA-04", {})
    exif = (job.step_result or {}).get("IA-01", {})
    is_dup = dup_info.get("status") == "duplicate"
    img_info = await asyncio.to_thread(_get_image_info, filepath) if os.path.exists(filepath) else {}

    return {
        "job_id": job.id,
        "debug_key": job.debug_key,
        "filename": job.filename,
        "filepath": filepath,
        "exists": os.path.exists(filepath),
        "is_original": not is_dup,
        "match_type": dup_info.get("match_type", "original") if is_dup else "original",
        "phash_distance": dup_info.get("phash_distance", 0),
        "quality": ai_result.get("quality", "—"),
        "confidence": ai_result.get("confidence", 0),
        "has_exif": exif.get("has_exif", False),
        "has_gps": bool(exif.get("gps")),
        **img_info,
    }


async def _build_duplicate_groups() -> list[dict]:
    """Build transitively merged groups of duplicate files."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "duplicate")
        )
        dup_jobs = result.scalars().all()

        if not dup_jobs:
            return []

        # Build links: each duplicate is linked to its original
        links = []
        for job in dup_jobs:
            dup_info = (job.step_result or {}).get("IA-03", {})
            original_key = dup_info.get("original_debug_key")
            if original_key:
                links.append((job.debug_key, original_key))

        # Transitively merge into groups
        merged = _union_find_groups(links)

        # Collect all debug_keys we need
        all_keys = set()
        for members in merged.values():
            all_keys.update(members)

        # Fetch all relevant jobs
        result = await session.execute(
            select(Job).where(Job.debug_key.in_(all_keys))
        )
        jobs_by_key = {j.debug_key: j for j in result.scalars().all()}

        # Build groups
        groups = []
        for root_key, member_keys in merged.items():
            members = []
            # Sort: originals first, then by debug_key
            sorted_keys = sorted(member_keys, key=lambda k: (
                1 if jobs_by_key.get(k) and jobs_by_key[k].status == "duplicate" else 0,
                k,
            ))

            for key in sorted_keys:
                job = jobs_by_key.get(key)
                if not job:
                    continue
                members.append(await _build_member(job, session))

            if len(members) < 2:
                continue

            # Use the first original's key as group key
            group_key = next(
                (m["debug_key"] for m in members if m["is_original"]),
                members[0]["debug_key"],
            )

            groups.append({
                "original_key": group_key,
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
    """Keep one file from a group, delete all others (including original if needed)."""
    form = await request.form()
    keep_key = form.get("keep_key")
    group_key = form.get("group_key")

    if not keep_key or not group_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        # Find all members of this group using transitive merging
        result = await session.execute(
            select(Job).where(Job.status == "duplicate")
        )
        all_dups = result.scalars().all()

        links = []
        for dup in all_dups:
            dup_info = (dup.step_result or {}).get("IA-03", {})
            orig_key = dup_info.get("original_debug_key")
            if orig_key:
                links.append((dup.debug_key, orig_key))

        merged = _union_find_groups(links)

        # Find which group contains group_key
        group_keys = set()
        for members in merged.values():
            if group_key in members:
                group_keys = members
                break

        if not group_keys:
            group_keys = {group_key}

        # Fetch all jobs in the group
        result = await session.execute(
            select(Job).where(Job.debug_key.in_(group_keys))
        )
        group_jobs = result.scalars().all()

        # Delete all except the kept one
        for job in group_jobs:
            if job.debug_key == keep_key:
                continue

            filepath = job.target_path or job.original_path
            if os.path.exists(filepath):
                await asyncio.to_thread(os.remove, filepath)
                log_path = filepath + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

            if job.status == "duplicate":
                job.status = "done"
            job.error_message = f"Duplikat-Review: gelöscht (behalten: {keep_key})"

        # Mark the kept job as resolved
        result = await session.execute(
            select(Job).where(Job.debug_key == keep_key)
        )
        kept_job = result.scalars().first()
        if kept_job and kept_job.status == "duplicate":
            kept_job.status = "done"
            kept_job.error_message = "Duplikat-Review: als beste Version behalten"

        await session.commit()

    await log_info("duplicates", f"Review: Gruppe {group_key}, behalten: {keep_key}")
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

import asyncio
import io
import os
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from PIL import Image
from sqlalchemy import select, func

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
    """Get image dimensions, file size, EXIF details and keywords via exiftool."""
    info = {
        "file_size": 0, "width": 0, "height": 0, "megapixel": 0.0,
        "exif_date": "", "exif_camera": "", "exif_iso": "", "exif_aperture": "",
        "exif_shutter": "", "exif_focal": "", "exif_keywords": [], "exif_description": "",
        "exif_has_gps": False, "exif_has_exif": False,
    }
    try:
        info["file_size"] = os.path.getsize(filepath)
        result = subprocess.run(
            ["exiftool", "-j",
             "-ImageWidth", "-ImageHeight",
             "-DateTimeOriginal", "-CreateDate",
             "-Make", "-Model",
             "-ISO", "-FNumber", "-ExposureTime", "-FocalLength",
             "-Keywords", "-Subject",
             "-ImageDescription",
             "-GPSLatitude", "-GPSLongitude",
             filepath],
            capture_output=True, text=True, timeout=10,
        )
        import json as _json
        data = _json.loads(result.stdout)[0] if result.stdout.strip() else {}

        w = data.get("ImageWidth", 0)
        h = data.get("ImageHeight", 0)
        if w and h:
            info["width"] = int(w)
            info["height"] = int(h)
            info["megapixel"] = round(int(w) * int(h) / 1_000_000, 1)

        date = data.get("DateTimeOriginal") or data.get("CreateDate") or ""
        info["exif_date"] = date
        make = data.get("Make", "")
        model = data.get("Model", "")
        info["exif_camera"] = f"{make} {model}".strip() if (make or model) else ""
        info["exif_iso"] = str(data.get("ISO", ""))
        info["exif_aperture"] = str(data.get("FNumber", ""))
        info["exif_shutter"] = str(data.get("ExposureTime", ""))
        info["exif_focal"] = str(data.get("FocalLength", ""))

        # Keywords can be string or list
        kw = data.get("Keywords") or data.get("Subject") or []
        if isinstance(kw, str):
            kw = [kw]
        info["exif_keywords"] = kw

        info["exif_description"] = data.get("ImageDescription", "")
        info["exif_has_gps"] = bool(data.get("GPSLatitude"))
        info["exif_has_exif"] = bool(date or make or model)
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


def _resolve_filepath(job) -> str:
    """Find the actual file path, checking target, original, and temp paths."""
    for path in [job.target_path, job.original_path]:
        if path and os.path.exists(path):
            return path
    # Fallback: IA-02 converted temp file
    convert_result = (job.step_result or {}).get("IA-02", {})
    temp_path = convert_result.get("temp_path")
    if temp_path and os.path.exists(temp_path):
        return temp_path
    return job.target_path or job.original_path


async def _build_member(job, session) -> dict:
    """Build a member dict for a single job — all info read directly from the file."""
    filepath = _resolve_filepath(job)
    dup_info = (job.step_result or {}).get("IA-03", {})
    is_dup = dup_info.get("status") == "duplicate"
    exists = os.path.exists(filepath)
    img_info = await asyncio.to_thread(_get_image_info, filepath) if exists else {}

    return {
        "job_id": job.id,
        "debug_key": job.debug_key,
        "filename": job.filename,
        "filepath": filepath,
        "exists": exists,
        "is_original": not is_dup,
        "match_type": dup_info.get("match_type", "original") if is_dup else "original",
        "phash_distance": dup_info.get("phash_distance", 0),
        "immich_link": dup_info.get("immich_link", ""),
        "immich_asset_id": dup_info.get("immich_asset_id", ""),
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

        # Separate Immich duplicates (no original_debug_key) from local duplicates
        immich_dups = []
        links = []
        for job in dup_jobs:
            dup_info = (job.step_result or {}).get("IA-03", {})
            original_key = dup_info.get("original_debug_key")
            if dup_info.get("match_type") == "immich":
                immich_dups.append(job)
            elif original_key:
                links.append((job.debug_key, original_key))

        # Transitively merge local duplicates into groups
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

        # Build groups for local duplicates
        groups = []
        for root_key, member_keys in merged.items():
            members = []
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

        # Build groups for Immich duplicates (each as its own group)
        for job in immich_dups:
            member = await _build_member(job, session)
            groups.append({
                "original_key": job.debug_key,
                "members": [member],
                "count": 1,
                "all_exact": False,
                "is_immich_duplicate": True,
            })

    return groups


@router.get("/duplicates")
async def duplicates_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    groups = await _build_duplicate_groups()
    return await render(request, "duplicates.html", {
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

    filepath = _resolve_filepath(job)

    if not os.path.exists(filepath):
        return Response(status_code=404)

    data = await asyncio.to_thread(_generate_thumbnail, filepath)
    if not data:
        return Response(status_code=404)

    return Response(content=data, media_type="image/jpeg")


@router.get("/api/thumbnail/immich/{asset_id}")
async def immich_thumbnail(asset_id: str):
    """Serve a thumbnail fetched from Immich."""
    from immich_client import get_asset_thumbnail
    data = await get_asset_thumbnail(asset_id)
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

        # Find the kept job and a target path in the library
        kept_job = None
        library_path = None
        for job in group_jobs:
            if job.debug_key == keep_key:
                kept_job = job
            # Find a target_path from an original (non-duplicate) in the library
            if job.status != "duplicate" and job.target_path:
                library_path = job.target_path

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
            job.error_message = f"Duplicate review: deleted (kept: {keep_key})"
            job.target_path = None

        # Move the kept file into the library if it's not already there
        if kept_job:
            kept_filepath = kept_job.target_path or kept_job.original_path

            if kept_job.status == "duplicate" and os.path.exists(kept_filepath):
                # Determine target: use the original's library path directory
                if library_path:
                    target_dir = os.path.dirname(library_path)
                else:
                    # Fallback: sort into library based on date
                    base_path = await config_manager.get("library.base_path", "/bibliothek")
                    exif = (kept_job.step_result or {}).get("IA-01", {})
                    date_str = exif.get("date", "")
                    if date_str:
                        try:
                            dt = datetime.strptime(date_str.split(" ")[0], "%Y:%m:%d")
                        except ValueError:
                            dt = datetime.now()
                    else:
                        dt = datetime.now()
                    target_dir = os.path.join(base_path, "photos", dt.strftime("%Y"), dt.strftime("%Y-%m"))

                await asyncio.to_thread(os.makedirs, target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, kept_job.filename)

                # Handle name conflicts
                if os.path.exists(target_path):
                    name, ext = os.path.splitext(kept_job.filename)
                    counter = 1
                    while os.path.exists(target_path):
                        target_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
                        counter += 1

                await asyncio.to_thread(safe_move, kept_filepath, target_path, kept_job.debug_key)
                kept_job.target_path = target_path

            kept_job.status = "done"
            kept_job.error_message = "Duplicate review: kept as best version"

        await session.commit()

    await log_info("duplicates", f"Review: Group {group_key}, kept: {keep_key}")
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
            job.error_message = "Duplicate deleted (manually)"
            await session.commit()

    await log_info("duplicates", f"Duplicate deleted: {debug_key}")
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
            dup.error_message = "Duplicate deleted (Batch-Clean, SHA256 exact)"
            deleted += 1

        await session.commit()

    await log_info("duplicates", f"Batch-Clean: {deleted} exact duplicates deleted")
    return RedirectResponse(url="/duplicates", status_code=303)

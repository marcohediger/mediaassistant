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
from sqlalchemy.orm.attributes import flag_modified

from config import config_manager
from database import async_session
from models import Job
from safe_file import safe_move
from system_logger import log_info

from template_engine import render

router = APIRouter()

THUMB_SIZE = (400, 400)
PREVIEW_SIZE = (1600, 1600)
HEIC_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {".dng", ".cr2", ".nef", ".arw"}


def _raw_to_jpeg(filepath: str) -> bytes | None:
    """Extract embedded PreviewImage from RAW file via ExifTool."""
    try:
        result = subprocess.run(
            ["exiftool", "-b", "-PreviewImage", filepath],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 1000:
            return result.stdout
        # Fallback: try JpgFromRaw
        result = subprocess.run(
            ["exiftool", "-b", "-JpgFromRaw", filepath],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 1000:
            return result.stdout
    except Exception:
        pass
    return None


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


def _generate_thumbnail(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
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

    img.thumbnail(max_size, Image.LANCZOS)
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
    """Find the actual file path, checking target, original, and temp paths.

    Bevorzugt target_path — original_path nur wenn Datei dort noch existiert.
    """
    if job.target_path and os.path.exists(job.target_path):
        return job.target_path
    if job.original_path and os.path.exists(job.original_path):
        return job.original_path
    # Fallback: IA-04 converted temp file
    convert_result = (job.step_result or {}).get("IA-04", {})
    temp_path = convert_result.get("temp_path")
    if temp_path and os.path.exists(temp_path):
        return temp_path
    return job.target_path or job.original_path


def _display_path(job) -> str:
    """Pfad für die Anzeige — nie Inbox-Pfade zeigen."""
    if job.target_path:
        return job.target_path
    return job.original_path or "—"


async def _build_member(job, session) -> dict:
    """Build a member dict for a single job — all info read directly from the file."""
    filepath = _resolve_filepath(job)
    dup_info = (job.step_result or {}).get("IA-02", {})
    is_dup = dup_info.get("status") == "duplicate"
    exists = os.path.exists(filepath)

    # Check if this job's target is in Immich
    immich_asset_id = job.immich_asset_id or ""
    immich_link = ""
    target = job.target_path or ""
    if target.startswith("immich:"):
        immich_asset_id = immich_asset_id or target[7:]
    if immich_asset_id:
        from immich_client import get_immich_config
        immich_url, _ = await get_immich_config()
        if immich_url:
            immich_link = f"{immich_url}/photos/{immich_asset_id}"

    if exists:
        img_info = await asyncio.to_thread(_get_image_info, filepath)
    elif immich_asset_id:
        img_info = await _img_info_from_immich(immich_asset_id)
    else:
        img_info = _empty_img_info()

    return {
        "job_id": job.id,
        "debug_key": job.debug_key,
        "filename": job.filename,
        "filepath": _display_path(job),
        "exists": exists,
        "is_original": not is_dup,
        "match_type": dup_info.get("match_type", "original") if is_dup else "original",
        "phash_distance": dup_info.get("phash_distance", 0),
        "immich_link": immich_link,
        "immich_asset_id": immich_asset_id,
        **img_info,
    }


def _empty_img_info() -> dict:
    return {
        "file_size": 0, "width": 0, "height": 0, "megapixel": 0.0,
        "exif_date": "", "exif_camera": "", "exif_iso": "", "exif_aperture": "",
        "exif_shutter": "", "exif_focal": "", "exif_keywords": [], "exif_description": "",
        "exif_has_gps": False, "exif_has_exif": False,
    }


async def _img_info_from_immich(asset_id: str) -> dict:
    """Fetch EXIF data from Immich API for an asset."""
    info = _empty_img_info()
    try:
        from immich_client import get_asset_info
        data = await get_asset_info(asset_id)
        if not data:
            return info

        exif = data.get("exifInfo", {})
        info["file_size"] = exif.get("fileSizeInByte", 0) or 0
        w = exif.get("exifImageWidth", 0) or 0
        h = exif.get("exifImageHeight", 0) or 0
        if w and h:
            info["width"] = int(w)
            info["height"] = int(h)
            info["megapixel"] = round(int(w) * int(h) / 1_000_000, 1)

        info["exif_date"] = exif.get("dateTimeOriginal", "")
        make = exif.get("make") or ""
        model = exif.get("model") or ""
        info["exif_camera"] = f"{make} {model}".strip() if (make or model) else ""
        info["exif_iso"] = str(exif.get("iso", "")) if exif.get("iso") else ""
        info["exif_aperture"] = str(exif.get("fNumber", "")) if exif.get("fNumber") else ""
        shutter = exif.get("exposureTime")
        info["exif_shutter"] = str(shutter) if shutter else ""
        focal = exif.get("focalLength")
        info["exif_focal"] = f"{focal} mm" if focal else ""
        info["exif_has_gps"] = bool(exif.get("latitude"))
        info["exif_has_exif"] = bool(info["exif_date"] or make or model)

        # Tags from Immich
        tags = data.get("tags", [])
        info["exif_keywords"] = [t.get("value") or t.get("name", "") for t in tags if t.get("value") or t.get("name")]

        info["exif_description"] = exif.get("description", "")
    except Exception:
        pass
    return info


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
            dup_info = (job.step_result or {}).get("IA-02", {})
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

            # Check if original is in Immich
            has_immich = any(m.get("immich_asset_id") for m in members)

            group_key = next(
                (m["debug_key"] for m in members if m["is_original"]),
                members[0]["debug_key"],
            )

            groups.append({
                "original_key": group_key,
                "members": members,
                "count": len(members),
                "all_exact": all(
                    m["match_type"] in ("exact", "original", "raw_jpg_pair") for m in members
                ),
                "is_immich_duplicate": has_immich,
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
async def thumbnail(job_id: int, size: str = "thumbnail"):
    """Serve a JPEG thumbnail for a job's image. size=thumbnail|preview"""
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


@router.get("/api/thumbnail/immich/{asset_id}")
async def immich_thumbnail(asset_id: str, size: str = "thumbnail"):
    """Serve a thumbnail fetched from Immich. size=thumbnail|preview"""
    from immich_client import get_asset_thumbnail
    data = await get_asset_thumbnail(asset_id, size=size)
    if not data:
        return Response(status_code=404)
    return Response(content=data, media_type="image/jpeg")


@router.get("/api/original/immich/{asset_id}")
async def immich_original(asset_id: str):
    """Proxy the original image from Immich. For RAW formats, returns the preview JPEG."""
    from immich_client import get_immich_config, get_asset_info, get_asset_thumbnail
    url, api_key = await get_immich_config()
    if not url or not api_key:
        return Response(status_code=404)

    # Check if the asset is a RAW format — use preview instead of original
    info = await get_asset_info(asset_id)
    if info:
        filename = info.get("originalFileName", "")
        ext = os.path.splitext(filename)[1].lower()
        if ext in RAW_EXTENSIONS:
            data = await get_asset_thumbnail(asset_id, size="preview")
            if data:
                return Response(content=data, media_type="image/jpeg")
            return Response(status_code=404)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{url}/api/assets/{asset_id}/original",
                headers={"x-api-key": api_key},
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return Response(status_code=404)
            content_type = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=content_type)
    except Exception:
        return Response(status_code=404)


@router.get("/api/original/local/{job_id}")
async def local_original(job_id: int):
    """Serve the original image file for a job."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
    if not job:
        return Response(status_code=404)

    filepath = _resolve_filepath(job)
    if not os.path.exists(filepath):
        return Response(status_code=404)

    import mimetypes
    content_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

    ext = os.path.splitext(filepath)[1].lower()

    # For HEIC, convert to JPEG for browser compatibility
    if ext in HEIC_EXTENSIONS:
        data = await asyncio.to_thread(_heic_to_jpeg, filepath)
        if data:
            return Response(content=data, media_type="image/jpeg")
        return Response(status_code=404)

    # For RAW, extract PreviewImage via ExifTool
    if ext in RAW_EXTENSIONS:
        data = await asyncio.to_thread(_raw_to_jpeg, filepath)
        if data:
            return Response(content=data, media_type="image/jpeg")
        return Response(status_code=404)

    with open(filepath, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=content_type)


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
            dup_info = (dup.step_result or {}).get("IA-02", {})
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
        group_is_immich = False
        for job in group_jobs:
            if job.debug_key == keep_key:
                kept_job = job
            # Check if any job in the group uses Immich
            if job.use_immich or (job.target_path or "").startswith("immich:") or job.immich_asset_id:
                group_is_immich = True
            # Find a target_path from an original (non-duplicate) in the library
            if job.status != "duplicate" and job.target_path and not job.target_path.startswith("immich:"):
                library_path = job.target_path

        # Delete all except the kept one
        for job in group_jobs:
            if job.debug_key == keep_key:
                continue

            # Delete local file if exists
            filepath = job.target_path or job.original_path
            if filepath and not filepath.startswith("immich:") and os.path.exists(filepath):
                await asyncio.to_thread(os.remove, filepath)
                log_path = filepath + ".log"
                if os.path.exists(log_path):
                    await asyncio.to_thread(os.remove, log_path)

            # Delete from Immich if applicable
            asset_id = job.immich_asset_id or ""
            target = job.target_path or ""
            if target.startswith("immich:"):
                asset_id = asset_id or target[7:]
            if asset_id:
                try:
                    from immich_client import get_immich_config
                    import httpx
                    i_url, i_key = await get_immich_config()
                    if i_url and i_key:
                        import json as _json
                        async with httpx.AsyncClient(timeout=10) as client:
                            await client.request(
                                "DELETE",
                                f"{i_url}/api/assets",
                                headers={"x-api-key": i_key, "Content-Type": "application/json"},
                                content=_json.dumps({"ids": [asset_id]}),
                            )
                except Exception:
                    pass

            if job.status == "duplicate":
                job.status = "done"
            job.error_message = None
            job.target_path = None
            # Clear hash so IA-02 won't match against this deleted job
            job.file_hash = None
            job.phash = None

        # Re-run pipeline for the kept file (AI analysis, tag writing, sorting)
        if kept_job:
            kept_filepath = kept_job.target_path or kept_job.original_path
            is_already_done = kept_job.status == "done"

            if is_already_done:
                # Original that was already fully processed — nothing to do
                pass
            elif kept_job.status == "duplicate" and kept_filepath and os.path.exists(kept_filepath):
                # Move file back to original inbox path for re-processing
                original_dir = os.path.dirname(kept_job.original_path)
                if os.path.exists(original_dir) and kept_filepath != kept_job.original_path:
                    await asyncio.to_thread(safe_move, kept_filepath, kept_job.original_path, kept_job.debug_key)
                    # Remove .log file
                    log_path = kept_filepath + ".log"
                    if os.path.exists(log_path):
                        await asyncio.to_thread(os.remove, log_path)

                # Reset job for re-processing: keep IA-01 (EXIF), clear everything else
                step_results = kept_job.step_result or {}
                ia01 = step_results.get("IA-01")
                kept_job.step_result = {"IA-01": ia01} if ia01 else {}
                flag_modified(kept_job, "step_result")
                kept_job.status = "queued"
                kept_job.target_path = None
                kept_job.error_message = None
                await session.commit()

                # Re-run pipeline in background
                from pipeline import run_pipeline
                asyncio.create_task(run_pipeline(kept_job.id))
                await log_info("duplicates", f"Review: Group {group_key}, kept: {keep_key} → re-processing")
                return RedirectResponse(url="/duplicates", status_code=303)
            else:
                kept_job.status = "done"
                kept_job.error_message = None

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
            dup_info = (dup.step_result or {}).get("IA-02", {})
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

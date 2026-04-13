import asyncio
import hashlib
import io
import os
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from PIL import Image
from sqlalchemy import select, func
from sqlalchemy.orm.attributes import flag_modified

from config import config_manager
from database import async_session
from models import Job
from system_logger import log_info

from template_engine import render

router = APIRouter()

THUMB_SIZE = (400, 400)
PREVIEW_SIZE = (1600, 1600)
HEIC_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts"}


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


def _video_to_jpeg(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
    """Extract a frame from a video file via ffmpeg and return as JPEG bytes."""
    try:
        w, h = max_size
        result = subprocess.run(
            ["ffmpeg", "-ss", "1", "-i", filepath,
             "-frames:v", "1", "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease",
             "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
            capture_output=True, timeout=15,
        )
        if result.stdout and len(result.stdout) > 500:
            return result.stdout
    except Exception:
        pass
    return None


def _generate_thumbnail(filepath: str, max_size=THUMB_SIZE) -> bytes | None:
    """Generate a JPEG thumbnail from an image or video file."""
    ext = os.path.splitext(filepath)[1].lower()

    if ext in VIDEO_EXTENSIONS:
        return _video_to_jpeg(filepath, max_size)

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


def _parse_exiftool_entry(data: dict, filepath: str) -> dict:
    """Parse a single exiftool JSON entry into our info dict."""
    info = _empty_img_info()
    try:
        info["file_size"] = os.path.getsize(filepath)
    except OSError:
        pass

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

    kw = data.get("Keywords") or data.get("Subject") or []
    if isinstance(kw, str):
        kw = [kw]
    info["exif_keywords"] = kw

    info["exif_description"] = data.get("ImageDescription", "")
    info["exif_has_gps"] = bool(data.get("GPSLatitude"))
    info["exif_has_exif"] = bool(date or make or model)
    return info


def _get_image_info(filepath: str) -> dict:
    """Get image dimensions, file size, EXIF details and keywords via exiftool."""
    try:
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
            capture_output=True, timeout=10,
        )
        import json as _json
        stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ''
        data = _json.loads(stdout)[0] if stdout.strip() else {}
        return _parse_exiftool_entry(data, filepath)
    except Exception:
        return _empty_img_info()


_EXIFTOOL_BATCH_SIZE = 100


def _get_image_info_batch(filepaths: list[str]) -> dict[str, dict]:
    """Get image info for multiple files in batched exiftool calls (max 100 per call)."""
    existing = [fp for fp in filepaths if os.path.exists(fp)]
    if not existing:
        return {fp: _empty_img_info() for fp in filepaths}

    result_map = {fp: _empty_img_info() for fp in filepaths}
    import json as _json
    exif_args = [
        "-j",
        "-ImageWidth", "-ImageHeight",
        "-DateTimeOriginal", "-CreateDate",
        "-Make", "-Model",
        "-ISO", "-FNumber", "-ExposureTime", "-FocalLength",
        "-Keywords", "-Subject",
        "-ImageDescription",
        "-GPSLatitude", "-GPSLongitude",
    ]
    for i in range(0, len(existing), _EXIFTOOL_BATCH_SIZE):
        batch = existing[i:i + _EXIFTOOL_BATCH_SIZE]
        try:
            result = subprocess.run(
                ["exiftool"] + exif_args + batch,
                capture_output=True,
                timeout=max(30, len(batch) * 2),
            )
            stdout = result.stdout.decode('utf-8', errors='replace') if result.stdout else ''
            entries = _json.loads(stdout) if stdout.strip() else []
            for entry in entries:
                src = entry.get("SourceFile", "")
                if src in result_map:
                    result_map[src] = _parse_exiftool_entry(entry, src)
        except Exception:
            pass
    return result_map


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


async def _build_member(job, session, *, prefetched_info: dict | None = None) -> dict:
    """Build a member dict for a single job — all info read directly from the file."""
    filepath = _resolve_filepath(job)
    dup_info = (job.step_result or {}).get("IA-02", {})
    # is_original is determined later in _build_group_detail by
    # comparing job IDs — the oldest job (lowest ID) in the group is
    # the original (= was in the library first). This placeholder is
    # overridden below.
    is_dup = job.status == "duplicate"
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

    if prefetched_info is not None:
        img_info = prefetched_info
    elif exists:
        img_info = await asyncio.to_thread(_get_image_info, filepath)
    elif immich_asset_id:
        from immich_client import get_user_api_key as _guk
        _ukey = await _guk(job.immich_user_id) if job.immich_user_id else None
        img_info = await _img_info_from_immich(immich_asset_id, api_key=_ukey)
    else:
        img_info = _empty_img_info()

    # Quality score for ⭐ badge (best-quality recommendation)
    from pipeline.step_ia02_duplicates import _quality_score
    q_score = _quality_score(job)

    # Folder tags (album name) — shown in the duplicate comparison UI.
    # Priority: 1) IA-02 folder_tags  2) IA-08 immich_albums_added  3) Immich API
    folder_tags = dup_info.get("folder_tags") or []
    folder_album = folder_tags[-1] if folder_tags else ""

    if not folder_album:
        # Fallback: IA-08 recorded which albums the asset was added to
        ia08 = (job.step_result or {}).get("IA-08", {})
        ia08_albums = ia08.get("immich_albums_added") or []
        if ia08_albums:
            folder_album = ia08_albums[0]

    if not folder_album and immich_asset_id:
        # Last resort: query Immich API for album membership
        try:
            from immich_client import get_asset_albums, get_user_api_key
            _akey = await get_user_api_key(job.immich_user_id) if job.immich_user_id else None
            album_names = await get_asset_albums(immich_asset_id, api_key=_akey)
            if album_names:
                folder_album = album_names[0]
        except Exception:
            pass

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
        "quality_score": q_score,
        "folder_tags": folder_tags,
        "folder_album": folder_album,
        **img_info,
    }


def _empty_img_info() -> dict:
    return {
        "file_size": 0, "width": 0, "height": 0, "megapixel": 0.0,
        "exif_date": "", "exif_camera": "", "exif_iso": "", "exif_aperture": "",
        "exif_shutter": "", "exif_focal": "", "exif_keywords": [], "exif_description": "",
        "exif_has_gps": False, "exif_has_exif": False,
    }


async def _img_info_from_immich(asset_id: str, *, api_key: str | None = None) -> dict:
    """Fetch EXIF data from Immich API for an asset."""
    info = _empty_img_info()
    try:
        from immich_client import get_asset_info
        data = await get_asset_info(asset_id, api_key=api_key)
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


async def _build_group_index() -> tuple[list[dict], dict[str, "Job"]]:
    """Build lightweight group index without EXIF data (fast)."""
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.status == "duplicate")
        )
        dup_jobs = result.scalars().all()

        if not dup_jobs:
            return [], {}

        links = []
        for job in dup_jobs:
            dup_info = (job.step_result or {}).get("IA-02", {})
            original_key = dup_info.get("original_debug_key")
            if original_key:
                links.append((job.debug_key, original_key))

        merged = _union_find_groups(links)

        all_keys = set()
        for members in merged.values():
            all_keys.update(members)

        result = await session.execute(
            select(Job).where(Job.debug_key.in_(all_keys))
        )
        jobs_by_key = {j.debug_key: j for j in result.scalars().all()}

        groups = []
        for root_key, member_keys in merged.items():
            # Sort by job ID (oldest first = original first in the UI)
            sorted_keys = sorted(member_keys, key=lambda k: (
                jobs_by_key[k].id if k in jobs_by_key else float("inf"),
            ))
            valid_keys = [k for k in sorted_keys if k in jobs_by_key]
            if len(valid_keys) < 2:
                continue

            group_key = next(
                (k for k in valid_keys if jobs_by_key[k].status != "duplicate"),
                valid_keys[0],
            )

            # Determine group flags from job data (no EXIF needed)
            has_immich = any(
                jobs_by_key[k].immich_asset_id or (jobs_by_key[k].target_path or "").startswith("immich:")
                for k in valid_keys
            )
            all_exact = all(
                (jobs_by_key[k].step_result or {}).get("IA-02", {}).get("match_type") in ("exact", "raw_jpg_pair", None)
                for k in valid_keys
            )
            # Safe for batch-clean: exact SHA256, RAW+JPG pair, OR pHash
            # with distance=0 (100% visually identical).
            # Default phash_distance=0 matches the UI display (which also
            # defaults to 0 when the field is missing from old IA-02 results).
            safe_for_batch = all_exact or all(
                (jobs_by_key[k].step_result or {}).get("IA-02", {}).get("match_type") in ("exact", "raw_jpg_pair", None)
                or (jobs_by_key[k].step_result or {}).get("IA-02", {}).get("phash_distance", 0) == 0
                for k in valid_keys
            )

            groups.append({
                "original_key": group_key,
                "member_keys": valid_keys,
                "count": len(valid_keys),
                "all_exact": all_exact,
                "safe_for_batch": safe_for_batch,
                "is_immich_duplicate": has_immich,
            })

    return groups, jobs_by_key


async def _build_group_detail(member_keys: list[str], jobs_by_key: dict) -> list[dict]:
    """Build full member details for one group, using batch exiftool."""
    # Collect local file paths for batch exiftool
    local_files = {}
    immich_jobs = {}
    for key in member_keys:
        job = jobs_by_key.get(key)
        if not job:
            continue
        filepath = _resolve_filepath(job)
        asset_id = job.immich_asset_id or ""
        target = job.target_path or ""
        if target.startswith("immich:"):
            asset_id = asset_id or target[7:]
        if os.path.exists(filepath):
            local_files[key] = filepath
        elif asset_id:
            immich_jobs[key] = (job, asset_id)

    # Batch exiftool for all local files at once
    batch_info = {}
    if local_files:
        batch_info = await asyncio.to_thread(
            _get_image_info_batch, list(local_files.values())
        )

    # Fetch Immich info concurrently
    immich_info = {}
    if immich_jobs:
        async def _fetch_immich(key, job, asset_id):
            from immich_client import get_user_api_key as _guk
            _ukey = await _guk(job.immich_user_id) if job.immich_user_id else None
            return key, await _img_info_from_immich(asset_id, api_key=_ukey)

        tasks = [_fetch_immich(k, j, aid) for k, (j, aid) in immich_jobs.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, tuple):
                immich_info[r[0]] = r[1]

    # Build members
    members = []
    async with async_session() as session:
        for key in member_keys:
            job = jobs_by_key.get(key)
            if not job:
                continue
            prefetched = None
            if key in local_files:
                prefetched = batch_info.get(local_files[key])
            elif key in immich_info:
                prefetched = immich_info[key]
            members.append(await _build_member(job, session, prefetched_info=prefetched))

    # Mark the oldest member (lowest job_id) as the true original —
    # this is the file that was in the library first, regardless of
    # any status swaps from re-evaluate operations.
    if members:
        oldest_idx = min(range(len(members)), key=lambda i: members[i].get("job_id", float("inf")))
        for i, m in enumerate(members):
            m["is_original"] = (i == oldest_idx)

    # Mark the member with the best quality score with ⭐
    if members:
        best_idx = max(range(len(members)), key=lambda i: members[i].get("quality_score", ()))
        for i, m in enumerate(members):
            m["is_quality_best"] = (i == best_idx)

    return members


async def _build_duplicate_groups() -> list[dict]:
    """Build transitively merged groups of duplicate files with full EXIF data."""
    group_index, jobs_by_key = await _build_group_index()
    if not group_index:
        return []

    groups = []
    for g in group_index:
        members = await _build_group_detail(g["member_keys"], jobs_by_key)
        if len(members) < 2:
            continue
        groups.append({
            "original_key": g["original_key"],
            "members": members,
            "count": g["count"],
            "all_exact": g["all_exact"],
            "is_immich_duplicate": g["is_immich_duplicate"],
        })

    return groups


@router.get("/api/duplicates/groups")
async def api_duplicate_groups(request: Request):
    """Paginated API: return duplicate groups with full EXIF, loaded in batches."""
    try:
        page = int(request.query_params.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = min(int(request.query_params.get("per_page", 10)), 50)
    except ValueError:
        per_page = 10

    group_index, jobs_by_key = await _build_group_index()
    total = len(group_index)
    exact_count = sum(1 for g in group_index if g.get("safe_for_batch", g.get("all_exact")))

    start = (page - 1) * per_page
    page_groups = group_index[start:start + per_page]

    groups = []
    for g in page_groups:
        members = await _build_group_detail(g["member_keys"], jobs_by_key)
        if len(members) < 2:
            continue
        groups.append({
            "original_key": g["original_key"],
            "members": members,
            "count": g["count"],
            "all_exact": g["all_exact"],
            "is_immich_duplicate": g["is_immich_duplicate"],
        })

    return JSONResponse({
        "groups": groups,
        "total_groups": total,
        "exact_groups": exact_count,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
    })


@router.get("/duplicates")
async def duplicates_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    skip_confirm = await config_manager.get("duplikat.skip_confirm", False)

    # Pagination
    try:
        page = int(request.query_params.get("page", 1))
    except ValueError:
        page = 1
    per_page = 10

    group_index, jobs_by_key = await _build_group_index()
    total = len(group_index)
    exact_count = sum(1 for g in group_index if g.get("safe_for_batch", g.get("all_exact")))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    page_groups = group_index[start:start + per_page]

    groups = []
    for g in page_groups:
        members = await _build_group_detail(g["member_keys"], jobs_by_key)
        if len(members) < 2:
            continue
        groups.append({
            "original_key": g["original_key"],
            "members": members,
            "count": g["count"],
            "all_exact": g["all_exact"],
            "is_immich_duplicate": g["is_immich_duplicate"],
        })

    # Build pagination page numbers (efficient — only the visible ones)
    page_numbers = []
    for p in range(1, total_pages + 1):
        if p <= 2 or p > total_pages - 1 or abs(p - page) <= 2:
            page_numbers.append(p)
        elif page_numbers and page_numbers[-1] != "...":
            page_numbers.append("...")

    return await render(request, "duplicates.html", {
        "groups": groups,
        "total_groups": total,
        "exact_groups": exact_count,
        "skip_confirm": skip_confirm,
        "current_page": page,
        "total_pages": total_pages,
        "per_page": per_page,
        "page_numbers": page_numbers,
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
        from immich_client import get_asset_original
        result = await get_asset_original(asset_id, api_key=api_key)
        if result:
            data, content_type = result
            return Response(content=data, media_type=content_type)
        return Response(status_code=404)
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

        # Merge metadata from all other members into the kept one
        # before deleting them (GPS, date, keywords, description,
        # folder_tags from IA-02).
        if kept_job:
            kept_sr = kept_job.step_result or {}
            kept_ia01 = kept_sr.get("IA-01") or {}
            kept_ia02 = kept_sr.get("IA-02") or {}
            kept_ia07 = kept_sr.get("IA-07") or {}
            kept_folder_tags = list(kept_ia02.get("folder_tags") or [])
            merge_notes = []

            for donor in group_jobs:
                if donor.debug_key == keep_key:
                    continue
                d_sr = donor.step_result or {}
                d_ia01 = d_sr.get("IA-01") or {}
                d_ia02 = d_sr.get("IA-02") or {}
                d_ia03 = d_sr.get("IA-03") or {}
                d_ia07 = d_sr.get("IA-07") or {}

                if not kept_ia01.get("gps") and d_ia01.get("gps"):
                    kept_ia01["gps"] = True
                    kept_ia01["gps_lat"] = d_ia01.get("gps_lat")
                    kept_ia01["gps_lon"] = d_ia01.get("gps_lon")
                    if d_ia03 and d_ia03.get("status") != "skipped":
                        kept_sr["IA-03"] = d_ia03
                    merge_notes.append("GPS")

                if not kept_ia01.get("date") and d_ia01.get("date"):
                    kept_ia01["date"] = d_ia01["date"]
                    merge_notes.append("date")

                kept_kw = kept_ia07.get("keywords_written") or []
                donor_kw = d_ia07.get("keywords_written") or []
                new_kw = [k for k in donor_kw if k and k not in kept_kw]
                if new_kw:
                    kept_kw.extend(new_kw)
                    kept_ia07["keywords_written"] = kept_kw
                    kept_ia07["tags_count"] = len(kept_kw)
                    merge_notes.append(f"keywords(+{len(new_kw)})")

                # Merge donor folder_tags from IA-02 (duplicates never ran
                # IA-07, so their folder tags only exist in IA-02)
                donor_ft = d_ia02.get("folder_tags") or []
                new_ft = [t for t in donor_ft if t and t not in kept_folder_tags]
                if new_ft:
                    kept_folder_tags.extend(new_ft)
                    merge_notes.append(f"folder_tags(+{len(new_ft)})")

                kept_desc = kept_ia07.get("description_written") or ""
                donor_desc = d_ia07.get("description_written") or ""
                if not kept_desc and donor_desc:
                    kept_ia07["description_written"] = donor_desc
                    merge_notes.append("description")

            # Persist merged folder_tags back into IA-02
            if kept_folder_tags:
                if not isinstance(kept_ia02, dict):
                    kept_ia02 = {}
                kept_ia02["folder_tags"] = kept_folder_tags
                kept_sr["IA-02"] = kept_ia02

            if merge_notes or kept_folder_tags:
                kept_sr["IA-01"] = kept_ia01
                kept_sr["IA-07"] = kept_ia07
                kept_job.step_result = kept_sr
                flag_modified(kept_job, "step_result")

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

            # Delete from Immich using the shared helper (force=True by
            # default, so the asset is permanently gone — not just trashed).
            # Without force, a re-upload of the kept file gets "duplicate"
            # from Immich (matching the trashed asset) and fails silently.
            asset_id = job.immich_asset_id or ""
            target = job.target_path or ""
            if target.startswith("immich:"):
                asset_id = asset_id or target[7:]
            if asset_id:
                try:
                    from immich_client import delete_asset
                    await delete_asset(asset_id)
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
                # Save folder_tags BEFORE prepare_job_for_reprocess wipes
                # all steps except IA-01.
                pre_ia02 = (kept_job.step_result or {}).get("IA-02") or {}
                saved_folder_tags = pre_ia02.get("folder_tags") or kept_folder_tags or []

                # File move (incl. .xmp sidecar) + step_result reset (keep
                # only IA-01 EXIF) + status flip is delegated to the shared
                # reprocess helper.
                from pipeline.reprocess import prepare_job_for_reprocess
                await prepare_job_for_reprocess(
                    session,
                    kept_job,
                    keep_steps={"IA-01"},
                    move_file=True,
                )

                # Inject IA-02 as skipped so the pipeline does NOT re-run
                # duplicate detection. The user explicitly chose to keep
                # this file — re-flagging it as duplicate would undo their
                # decision (especially when other jobs with matching pHash
                # still exist in the DB).
                sr = kept_job.step_result or {}
                sr["IA-02"] = {"status": "skipped", "reason": "kept via duplicate review"}
                if saved_folder_tags:
                    sr["IA-02"]["folder_tags"] = saved_folder_tags
                kept_job.step_result = sr
                flag_modified(kept_job, "step_result")
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


@router.post("/api/duplicates/not-duplicate")
async def not_duplicate(request: Request):
    """Mark a single file as 'not a duplicate' and re-process it through the pipeline."""
    form = await request.form()
    debug_key = form.get("debug_key")

    if not debug_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        # Accept "duplicate" (normal) and "done" (after Batch-Clean
        # promoted the job but it still shows in the duplicates view).
        result = await session.execute(
            select(Job).where(
                Job.debug_key == debug_key,
                Job.status.in_(("duplicate", "done")),
            )
        )
        job = result.scalars().first()
        if not job:
            return RedirectResponse(url="/duplicates", status_code=303)

        # ── Clear IA-02 duplicate flag on this job ────────────────
        skip_result = {
            "status": "skipped",
            "reason": "manually marked as not a duplicate",
        }

        # Preserve folder_tags before IA-02 is overwritten
        old_ia02 = (job.step_result or {}).get("IA-02") or {}
        if old_ia02.get("folder_tags"):
            skip_result["folder_tags"] = old_ia02["folder_tags"]

        filepath = job.target_path or job.original_path
        if not filepath or not os.path.exists(filepath) or (filepath and filepath.startswith("immich:")):
            # File doesn't exist locally or is an Immich asset — just
            # clear the duplicate flag so it disappears from the view.
            sr = dict(job.step_result or {})
            sr["IA-02"] = skip_result
            job.step_result = sr
            flag_modified(job, "step_result")
            if job.status == "duplicate":
                job.status = "done"
        else:
            # File exists locally — reprocess through the pipeline.
            from pipeline.reprocess import prepare_job_for_reprocess
            await prepare_job_for_reprocess(
                session,
                job,
                keep_steps={"IA-01"},
                inject_steps={"IA-02": skip_result},
                move_file=True,
                commit=False,
            )
            # Re-run pipeline in background
            from pipeline import run_pipeline
            asyncio.create_task(run_pipeline(job.id))

        # ── Dissolve orphaned group members ───────────────────────
        # When this job is removed from its duplicate group, any other
        # member that referenced it (or was referenced by it) may now
        # be the sole remaining member.  A group with only 1 member
        # should not exist — dissolve those orphans too.
        ia02 = (job.step_result or {}).get("IA-02", {})
        partner_key = ia02.get("original_debug_key")

        # Also find jobs that reference THIS job as their original
        referencing = await session.execute(
            select(Job).where(
                Job.status == "duplicate",
                Job.step_result.like(f'%"original_debug_key": "{debug_key}"%'),
            )
        )
        referencing_jobs = referencing.scalars().all()

        # Collect all partner keys in this group
        partner_keys = set()
        if partner_key:
            partner_keys.add(partner_key)
        for rj in referencing_jobs:
            partner_keys.add(rj.debug_key)

        # For each partner: check if they still have other duplicate
        # links.  If not, they are orphaned → dissolve.
        for pk in partner_keys:
            # Count remaining duplicate jobs referencing this partner
            remaining = await session.execute(
                select(func.count(Job.id)).where(
                    Job.status == "duplicate",
                    Job.debug_key != debug_key,
                    Job.step_result.like(f'%"original_debug_key": "{pk}"%'),
                )
            )
            remaining_count = remaining.scalar()

            # Also count if this partner itself references another
            # duplicate that is still active
            partner_result = await session.execute(
                select(Job).where(Job.debug_key == pk)
            )
            partner_job = partner_result.scalars().first()

            if remaining_count == 0 and partner_job and partner_job.status == "duplicate":
                # This partner is now alone — dissolve it
                psr = dict(partner_job.step_result or {})
                psr["IA-02"] = skip_result
                partner_job.step_result = psr
                flag_modified(partner_job, "step_result")
                partner_job.status = "done"

        await session.commit()

    await log_info("duplicates", f"Not a duplicate: {debug_key}")
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


@router.post("/api/duplicates/merge-metadata")
async def merge_metadata(request: Request):
    """Merge missing metadata from one or more source files into target file.

    Accepts either a single ``source_key`` or a comma-separated
    ``source_keys`` parameter so that metadata from an entire duplicate
    group can be merged in one request.
    """
    form = await request.form()
    target_key = form.get("target_key")
    # Support single source_key or comma-separated source_keys
    source_keys_raw = form.get("source_keys") or form.get("source_key") or ""
    source_keys = [k.strip() for k in source_keys_raw.split(",") if k.strip()]

    if not target_key or not source_keys:
        return JSONResponse({"error": "missing keys"}, status_code=400)

    all_keys = [target_key] + source_keys
    async with async_session() as session:
        result = await session.execute(
            select(Job).where(Job.debug_key.in_(all_keys))
        )
        jobs = {j.debug_key: j for j in result.scalars().all()}
        target_job = jobs.get(target_key)
        if not target_job:
            return JSONResponse({"error": "target job not found"}, status_code=404)

        target_path = _resolve_filepath(target_job)
        if not os.path.exists(target_path):
            return JSONResponse({"error": "target file not found"}, status_code=404)

        # Resolve sources — local files AND Immich assets
        # source_infos: list of (info_dict, local_path_or_None)
        source_infos: list[tuple[dict, str | None]] = []
        local_paths = []
        for sk in source_keys:
            sj = jobs.get(sk)
            if not sj:
                continue
            sp = _resolve_filepath(sj)
            if os.path.exists(sp):
                local_paths.append(sp)
                source_infos.append((None, sp))  # info filled via batch below
            else:
                # Try Immich
                asset_id = sj.immich_asset_id or ""
                target_str = sj.target_path or ""
                if target_str.startswith("immich:"):
                    asset_id = asset_id or target_str[7:]
                if asset_id:
                    from immich_client import get_user_api_key as _guk
                    _ukey = await _guk(sj.immich_user_id) if sj.immich_user_id else None
                    immich_info = await _img_info_from_immich(asset_id, api_key=_ukey)
                    source_infos.append((immich_info, None))

        if not source_infos:
            return JSONResponse({"error": "no source files found"}, status_code=404)

        # Batch-read EXIF for all local files (target + local sources)
        all_local = [target_path] + local_paths
        batch = await asyncio.to_thread(_get_image_info_batch, all_local)
        target_info = batch.get(target_path, _empty_img_info())

        # Fill in batch info for local sources
        for i, (info, lp) in enumerate(source_infos):
            if lp and info is None:
                source_infos[i] = (batch.get(lp, _empty_img_info()), lp)

        from pipeline.step_ia07_exif_write import _IPTC_FORMATS, _NO_XPCOMMENT
        ext = os.path.splitext(target_path)[1].lower()

        cmd = ["exiftool", "-overwrite_original_in_place", "-P", "-m"]
        merged_fields = []

        # GPS — take from first source that has it
        if not target_info["exif_has_gps"]:
            for si, lp in source_infos:
                if si["exif_has_gps"]:
                    if lp:
                        # Local file: copy GPS tags directly
                        cmd += ["-TagsFromFile", lp,
                                "-GPSLatitude", "-GPSLongitude",
                                "-GPSLatitudeRef", "-GPSLongitudeRef"]
                    else:
                        # Immich source: GPS data already in info but not as raw tags.
                        # Read raw GPS string from exif_date-style fields won't work.
                        # Skip — GPS from Immich-only sources not supported yet.
                        continue
                    merged_fields.append("GPS")
                    break

        # Date — take from first source that has it
        if not target_info["exif_date"]:
            for si, lp in source_infos:
                if si["exif_date"]:
                    cmd.append(f"-DateTimeOriginal={si['exif_date']}")
                    merged_fields.append("DateTimeOriginal")
                    break

        # Camera — take from first source that has it
        if not target_info["exif_camera"]:
            for si, lp in source_infos:
                if si["exif_camera"]:
                    if lp:
                        import json as _json
                        src_exif = subprocess.run(
                            ["exiftool", "-j", "-Make", "-Model", lp],
                            capture_output=True, timeout=10,
                        )
                        src_stdout = src_exif.stdout.decode('utf-8', errors='replace') if src_exif.stdout else ''
                        src_data = _json.loads(src_stdout)[0] if src_stdout.strip() else {}
                        if src_data.get("Make"):
                            cmd.append(f"-Make={src_data['Make']}")
                        if src_data.get("Model"):
                            cmd.append(f"-Model={src_data['Model']}")
                    else:
                        # Immich source: camera info is combined string, split heuristic
                        parts = si["exif_camera"].split(" ", 1)
                        cmd.append(f"-Make={parts[0]}")
                        if len(parts) > 1:
                            cmd.append(f"-Model={parts[1]}")
                    merged_fields.append("Camera")
                    break

        # Description — take from first source that has it
        if not target_info["exif_description"]:
            for si, lp in source_infos:
                if si["exif_description"]:
                    cmd.append(f"-ImageDescription={si['exif_description']}")
                    if ext not in _NO_XPCOMMENT:
                        cmd.append(f"-XPComment={si['exif_description']}")
                    merged_fields.append("Description")
                    break

        # Keywords — union from ALL sources (local + Immich)
        target_kw = set(target_info["exif_keywords"])
        new_kw = set()
        for si, lp in source_infos:
            new_kw |= set(si["exif_keywords"])
        new_kw -= target_kw
        if new_kw:
            if ext in _IPTC_FORMATS:
                for kw in sorted(new_kw):
                    cmd.append(f"-Keywords+={kw}")
            else:
                for kw in sorted(new_kw):
                    cmd.append(f"-Subject+={kw}")
            merged_fields.append(f"Keywords (+{len(new_kw)})")

        if not merged_fields:
            return JSONResponse({"status": "nothing_to_merge", "merged": []})

        cmd.append(target_path)

        result = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
            return JSONResponse(
                {"error": f"ExifTool error: {stderr.strip()}"},
                status_code=500,
            )

        # Update file hash in job
        def _sha256(path):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()

        new_hash = await asyncio.to_thread(_sha256, target_path)
        target_job.file_hash = new_hash
        flag_modified(target_job, "step_result")
        await session.commit()

    src_label = ", ".join(source_keys)
    await log_info("duplicates", f"Metadata merged: [{src_label}] → {target_key} ({', '.join(merged_fields)})")
    return JSONResponse({"status": "ok", "merged": merged_fields})


@router.post("/api/duplicates/re-evaluate-quality")
async def re_evaluate_quality():
    """Informational: count how many duplicate groups have a better-quality
    duplicate than their current original. Does NOT swap any statuses —
    the quality badge in the UI shows the recommendation.
    """
    from pipeline.step_ia02_duplicates import _quality_score

    would_swap = 0
    already_best = 0

    group_index, jobs_by_key = await _build_group_index()

    for g in group_index:
        member_keys = g["member_keys"]
        members = [(k, jobs_by_key[k]) for k in member_keys if k in jobs_by_key]
        if len(members) < 2:
            continue
        orig = next(((k, j) for k, j in members if j.status != "duplicate"), members[0])
        best = max(members, key=lambda kj: _quality_score(kj[1]))
        if best[0] != orig[0]:
            would_swap += 1
        else:
            already_best += 1

    summary = (f"Quality Re-Evaluate: {would_swap} Gruppen mit besserem Duplikat, "
               f"{already_best} bereits korrekt")
    await log_info("duplicates", summary)
    return RedirectResponse(url="/duplicates", status_code=303)


@router.post("/api/duplicates/batch-clean-quality")
async def batch_clean_quality(request: Request):
    """Quality-aware batch clean for the current page only.

    Reads the 'page' form field to determine which groups to clean.
    Only exact-match groups on that page are processed. The best
    quality member per group is kept, the rest is deleted.

    After cleaning, redirects back to the same page (or page 1 if
    the current page is now empty).
    """
    from pipeline.step_ia02_duplicates import _quality_score

    # Read page from form data (page=0 means "all pages")
    try:
        form = await request.form()
        page = int(form.get("page", 1))
    except (ValueError, Exception):
        page = 1
    per_page = 10

    kept = 0
    deleted = 0
    errors = 0

    group_index, jobs_by_key = await _build_group_index()

    # page=0 → all groups; page>0 → only that page
    if page == 0:
        page_groups = group_index
    else:
        start = (page - 1) * per_page
        page_groups = group_index[start:start + per_page]

    async with async_session() as session:
        for g in page_groups:
            if not g.get("safe_for_batch", g.get("all_exact")):
                continue  # only exact or pHash-100% for auto-clean

            # Load all members and compute quality scores
            member_jobs = []
            for key in g["member_keys"]:
                job = jobs_by_key.get(key)
                if job:
                    result = await session.execute(select(Job).where(Job.id == job.id))
                    fresh = result.scalar()
                    if fresh:
                        member_jobs.append(fresh)

            if len(member_jobs) < 2:
                continue

            # Find the best quality member
            best = max(member_jobs, key=lambda j: _quality_score(j))
            kept += 1

            # Merge metadata from all others into the best before deleting.
            # Collects GPS, date, keywords, description, folder_tags from
            # worse members and fills gaps in the best member's step_result.
            best_sr = best.step_result or {}
            best_ia01 = best_sr.get("IA-01") or {}
            best_ia02 = best_sr.get("IA-02") or {}
            best_ia07 = best_sr.get("IA-07") or {}
            best_folder_tags = list((best_ia02 if isinstance(best_ia02, dict) else {}).get("folder_tags") or [])
            merged_fields = []

            for donor in member_jobs:
                if donor.id == best.id:
                    continue
                donor_sr = donor.step_result or {}
                donor_ia01 = donor_sr.get("IA-01") or {}
                donor_ia02 = donor_sr.get("IA-02") or {}
                donor_ia03 = donor_sr.get("IA-03") or {}
                donor_ia07 = donor_sr.get("IA-07") or {}

                # GPS: if best has none but donor does
                if not best_ia01.get("gps") and donor_ia01.get("gps"):
                    best_ia01["gps"] = True
                    best_ia01["gps_lat"] = donor_ia01.get("gps_lat")
                    best_ia01["gps_lon"] = donor_ia01.get("gps_lon")
                    # Also copy geocoding result
                    if donor_ia03 and donor_ia03.get("status") != "skipped":
                        best_sr["IA-03"] = donor_ia03
                    merged_fields.append("GPS")

                # Date: if best has none but donor does
                if not best_ia01.get("date") and donor_ia01.get("date"):
                    best_ia01["date"] = donor_ia01["date"]
                    merged_fields.append("date")

                # Keywords: merge unique keywords from donor
                best_kw = best_ia07.get("keywords_written") or []
                donor_kw = donor_ia07.get("keywords_written") or []
                new_kw = [k for k in donor_kw if k and k not in best_kw]
                if new_kw:
                    best_kw.extend(new_kw)
                    best_ia07["keywords_written"] = best_kw
                    best_ia07["tags_count"] = len(best_kw)
                    merged_fields.append(f"keywords(+{len(new_kw)})")

                # Merge donor folder_tags from IA-02 (duplicates never ran
                # IA-07, so their folder tags only exist in IA-02)
                donor_ft = (donor_ia02 if isinstance(donor_ia02, dict) else {}).get("folder_tags") or []
                new_ft = [t for t in donor_ft if t and t not in best_folder_tags]
                if new_ft:
                    best_folder_tags.extend(new_ft)
                    merged_fields.append(f"folder_tags(+{len(new_ft)})")

                # Description: if best has none but donor does
                best_desc = best_ia07.get("description_written") or ""
                donor_desc = donor_ia07.get("description_written") or ""
                if not best_desc and donor_desc:
                    best_ia07["description_written"] = donor_desc
                    merged_fields.append("description")

            # Persist merged folder_tags back into IA-02
            if best_folder_tags:
                if not isinstance(best_ia02, dict):
                    best_ia02 = {}
                best_ia02["folder_tags"] = best_folder_tags
                best_sr["IA-02"] = best_ia02

            if merged_fields or best_folder_tags:
                best_sr["IA-01"] = best_ia01
                best_sr["IA-07"] = best_ia07
                best.step_result = best_sr
                flag_modified(best, "step_result")

            # Delete all others
            for job in member_jobs:
                if job.id == best.id:
                    continue

                filepath = job.target_path or job.original_path
                if filepath and not filepath.startswith("immich:") and os.path.exists(filepath):
                    try:
                        await asyncio.to_thread(os.remove, filepath)
                        log_path = filepath + ".log"
                        if os.path.exists(log_path):
                            await asyncio.to_thread(os.remove, log_path)
                    except OSError:
                        errors += 1
                        continue

                job.status = "done"
                job.error_message = (
                    f"Duplicate deleted (Batch-Clean Quality). "
                    f"Kept: {best.debug_key} (better quality)"
                )
                deleted += 1

            # If the best was a duplicate, it never ran IA-03..IA-08.
            # Copy the analysis results (IA-03 geocoding, IA-05 AI tags,
            # IA-06 OCR) from the original so we don't need to re-run
            # the slow AI step. Then queue for re-processing — only
            # IA-07 (tag write) and IA-08 (sort/upload) need to run.
            if best.status == "duplicate":
                # Transfer Immich asset ID from deleted members to the
                # promoted job. When the worse member was already uploaded
                # to Immich, the promoted job needs its asset_id so IA-08
                # does the Upload→Copy→Delete replace workflow instead of
                # a bare new upload.  Without this, the promoted file
                # stayed in /library/error/duplicates/ and Immich kept
                # the lower-quality version.  (Fix for v2.28.69)
                for donor_job in member_jobs:
                    if donor_job.id == best.id:
                        continue
                    donor_immich_id = donor_job.immich_asset_id
                    if not donor_immich_id:
                        donor_target = donor_job.target_path or ""
                        if donor_target.startswith("immich:"):
                            donor_immich_id = donor_target[len("immich:"):]
                    if donor_immich_id and not best.immich_asset_id:
                        best.immich_asset_id = donor_immich_id
                        break

                # Find the original (the done member with the most complete step_result)
                original = next(
                    (j for j in member_jobs if j.id != best.id and j.status != "duplicate"
                     and (j.step_result or {}).get("IA-05")),
                    None,
                )
                if original:
                    orig_sr = original.step_result or {}
                    # Copy analysis steps from original
                    for step in ("IA-03", "IA-04", "IA-05", "IA-06"):
                        if orig_sr.get(step):
                            best_sr[step] = orig_sr[step]
                    # Skip IA-02 — preserve folder_tags
                    old_ia02 = best_sr.get("IA-02") or {}
                    new_ia02 = {"status": "skipped", "reason": "kept via batch-clean"}
                    if old_ia02.get("folder_tags"):
                        new_ia02["folder_tags"] = old_ia02["folder_tags"]
                    best_sr["IA-02"] = new_ia02
                    best.step_result = best_sr
                    flag_modified(best, "step_result")

                    # Move file from /library/error/duplicates/ to reprocess/
                    # so the pipeline can sort it into the library or upload
                    # to Immich via IA-08.
                    from pipeline.reprocess import prepare_job_for_reprocess
                    kept_filepath = best.target_path or best.original_path
                    if kept_filepath and not kept_filepath.startswith("immich:") and os.path.exists(kept_filepath):
                        await prepare_job_for_reprocess(
                            session,
                            best,
                            keep_steps={"IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06"},
                            move_file=True,
                            commit=False,
                        )

                    best.status = "queued"
                    best.error_message = (
                        f"Promoted (best quality). Analysis copied from {original.debug_key}."
                        + (f" Merged: {', '.join(merged_fields)}" if merged_fields else "")
                    )
                else:
                    # No original with analysis data found — queue for full
                    # re-processing (AI will run)
                    from pipeline.reprocess import prepare_job_for_reprocess
                    kept_filepath = best.target_path or best.original_path
                    if kept_filepath and not kept_filepath.startswith("immich:") and os.path.exists(kept_filepath):
                        await prepare_job_for_reprocess(
                            session,
                            best,
                            keep_steps={"IA-01"},
                            move_file=True,
                            commit=False,
                        )
                        # Skip IA-02 — preserve folder_tags
                        sr = best.step_result or {}
                        old_ia02 = sr.get("IA-02") or {}
                        sr["IA-02"] = {"status": "skipped", "reason": "kept via batch-clean"}
                        if old_ia02.get("folder_tags"):
                            sr["IA-02"]["folder_tags"] = old_ia02["folder_tags"]
                        best.step_result = sr
                        flag_modified(best, "step_result")
                    best.status = "queued"
                    best.error_message = "Promoted (best quality, full re-processing)"
                    if merged_fields:
                        best.error_message += f" + merged: {', '.join(merged_fields)}"
            elif merged_fields:
                best.error_message = (best.error_message or "") + f" + merged: {', '.join(merged_fields)}"

        await session.commit()

    page_label = "all" if page == 0 else f"page {page}"
    summary = (f"Batch-Clean Quality ({page_label}): "
               f"{kept} groups, {deleted} deleted, {errors} errors")
    await log_info("duplicates", summary)
    redirect_url = "/duplicates" if page == 0 else f"/duplicates?page={page}"
    return RedirectResponse(url=redirect_url, status_code=303)


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

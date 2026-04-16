import asyncio
import os
import subprocess
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select, func
from sqlalchemy.orm.attributes import flag_modified

from config import config_manager
from database import async_session
from file_operations import resolve_filepath
from models import Job
import logging

from system_logger import log_info

logger = logging.getLogger("mediaassistant.routers.duplicates")
from thumbnail_utils import (
    generate_thumbnail, raw_to_jpeg, heic_to_jpeg, video_to_jpeg,
    THUMB_SIZE, PREVIEW_SIZE, HEIC_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS,
)

from template_engine import render

router = APIRouter()



# Thumbnail functions (raw_to_jpeg, heic_to_jpeg, video_to_jpeg,
# generate_thumbnail) consolidated into thumbnail_utils.py


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



# _resolve_filepath consolidated into file_operations.resolve_filepath


def _display_path(job) -> str:
    """Pfad für die Anzeige — nie Inbox-Pfade zeigen."""
    if job.target_path:
        return job.target_path
    return job.original_path or "—"


async def _build_member(job, session, *, prefetched_info: dict | None = None) -> dict:
    """Build a member dict for a single job — all info read directly from the file."""
    filepath = resolve_filepath(job)
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
            # Sort: files already in target (done/review) first, then by job ID.
            # This ensures the file that's in Immich/Library shows on the left.
            sorted_keys = sorted(member_keys, key=lambda k: (
                0 if k in jobs_by_key and jobs_by_key[k].status != "duplicate" else 1,
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
        filepath = resolve_filepath(job)
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

    # Mark the member that is already in the target (not duplicate) as original.
    # Fallback: oldest job if all are duplicates.
    if members:
        orig_idx = next(
            (i for i, m in enumerate(members) if m.get("match_type") == "original"),
            min(range(len(members)), key=lambda i: members[i].get("job_id", float("inf"))),
        )
        for i, m in enumerate(members):
            m["is_original"] = (i == orig_idx)

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

    filepath = resolve_filepath(job)

    if not os.path.exists(filepath):
        return Response(status_code=404)

    max_size = PREVIEW_SIZE if size == "preview" else THUMB_SIZE
    data = await asyncio.to_thread(generate_thumbnail, filepath, max_size)
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

    filepath = resolve_filepath(job)
    if not os.path.exists(filepath):
        return Response(status_code=404)

    import mimetypes
    content_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

    ext = os.path.splitext(filepath)[1].lower()

    # For HEIC, convert to JPEG for browser compatibility
    if ext in HEIC_EXTENSIONS:
        data = await asyncio.to_thread(heic_to_jpeg, filepath)
        if data:
            return Response(content=data, media_type="image/jpeg")
        return Response(status_code=404)

    # For RAW, extract PreviewImage via ExifTool
    if ext in RAW_EXTENSIONS:
        data = await asyncio.to_thread(raw_to_jpeg, filepath)
        if data:
            return Response(content=data, media_type="image/jpeg")
        return Response(status_code=404)

    with open(filepath, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=content_type)


async def _resolve_duplicate_group(
    session,
    best: Job,
    member_jobs: list[Job],
    *,
    source: str = "review",
    user_kept: bool = False,
) -> tuple[list[str], int, int]:
    """Resolve a duplicate group: keep *best*, merge metadata, clean up donors.

    Shared core for keep_file (manual pick) and batch_clean_quality (auto pick).

    1. Merges GPS, date, keywords, folder_tags, description, albums from donors.
    2. Deletes donor local files + Immich assets (guards same-asset deletion).
    3. Clears donor job fields (status, hashes, paths).
    4. Handles best: if "done" → apply merged data to Immich directly;
       if "duplicate" → copy analysis from original, prepare for reprocess.

    Returns (merge_notes, deleted_count, error_count).
    Caller must commit the session after this returns.
    """
    from pipeline.step_ia02_duplicates import _extract_folder_tags

    donors = [j for j in member_jobs if j.id != best.id]

    # ── Pre-collect donor data needed after cleanup ─────────────
    donor_immich_map: dict[int, str] = {}
    for d in donors:
        did = d.immich_asset_id or ""
        if not did and (d.target_path or "").startswith("immich:"):
            did = (d.target_path or "")[7:]
        if did:
            donor_immich_map[d.id] = did

    # Best donor with analysis data (for copying IA-03..06, saves AI costs)
    analysis_donor = next(
        (d for d in donors if d.status != "duplicate"
         and (d.step_result or {}).get("IA-05")),
        None,
    )

    # ── 1. Metadata merge (donors → best) ──────────────────────
    best_sr = best.step_result or {}
    best_ia01 = best_sr.get("IA-01") or {}
    best_ia02 = best_sr.get("IA-02") or {}
    if not isinstance(best_ia02, dict):
        best_ia02 = {}
    best_ia07 = best_sr.get("IA-07") or {}
    best_folder_tags = list(best_ia02.get("folder_tags") or [])
    own_album = best_folder_tags[-1] if best_folder_tags else ""
    donor_immich_albums: list[str] = []
    merge_notes: list[str] = []

    for donor in donors:
        d_sr = donor.step_result or {}
        d_ia01 = d_sr.get("IA-01") or {}
        d_ia02 = d_sr.get("IA-02") or {}
        d_ia03 = d_sr.get("IA-03") or {}
        d_ia07 = d_sr.get("IA-07") or {}

        # GPS
        if not best_ia01.get("gps") and d_ia01.get("gps"):
            best_ia01["gps"] = True
            best_ia01["gps_lat"] = d_ia01.get("gps_lat")
            best_ia01["gps_lon"] = d_ia01.get("gps_lon")
            if d_ia03 and d_ia03.get("status") != "skipped":
                best_sr["IA-03"] = d_ia03
            merge_notes.append("GPS")

        # Date
        if not best_ia01.get("date") and d_ia01.get("date"):
            best_ia01["date"] = d_ia01["date"]
            merge_notes.append("date")

        # Keywords
        best_kw = best_ia07.get("keywords_written") or []
        donor_kw = d_ia07.get("keywords_written") or []
        new_kw = [k for k in donor_kw if k and k not in best_kw]
        if new_kw:
            best_kw.extend(new_kw)
            best_ia07["keywords_written"] = best_kw
            best_ia07["tags_count"] = len(best_kw)
            merge_notes.append(f"keywords(+{len(new_kw)})")

        # Folder tags
        donor_ft = (d_ia02 if isinstance(d_ia02, dict) else {}).get("folder_tags") or []
        if not donor_ft and donor.folder_tags:
            donor_ft = _extract_folder_tags(donor)
        new_ft = [t for t in donor_ft if t and t not in best_folder_tags]
        if new_ft:
            best_folder_tags.extend(new_ft)
            merge_notes.append(f"folder_tags(+{len(new_ft)})")

        # Donor albums (Immich API → IA-08 result → folder_tags fallback)
        donor_albums_found: list[str] = []
        donor_asset = donor_immich_map.get(donor.id, "")
        if donor_asset:
            try:
                from immich_client import get_asset_albums
                donor_albums_found = await get_asset_albums(donor_asset)
            except Exception:
                pass
        if not donor_albums_found:
            d_ia08 = d_sr.get("IA-08") or {}
            donor_albums_found = d_ia08.get("immich_albums_added") or []
        if not donor_albums_found and donor_ft:
            donor_albums_found = [donor_ft[-1]]
        for a in donor_albums_found:
            if a and a not in donor_immich_albums:
                donor_immich_albums.append(a)
            if a and a not in best_folder_tags:
                best_folder_tags.append(a)
            for word in (a or "").split():
                if word and word not in best_folder_tags:
                    best_folder_tags.append(word)

        # Description
        best_desc = best_ia07.get("description_written") or ""
        donor_desc = d_ia07.get("description_written") or ""
        if not best_desc and donor_desc:
            best_ia07["description_written"] = donor_desc
            merge_notes.append("description")

    # Ensure all folder_tags are in keywords
    best_kw = best_ia07.get("keywords_written") or []
    for ft in best_folder_tags:
        if ft and ft not in best_kw:
            best_kw.append(ft)
    if best_kw:
        best_ia07["keywords_written"] = best_kw
        best_ia07["tags_count"] = len(best_kw)

    # Persist merged data into step_result
    if best_folder_tags:
        best_ia02["folder_tags"] = best_folder_tags
    if own_album:
        best_ia02["own_album"] = own_album
    if donor_immich_albums:
        best_ia02["donor_albums"] = donor_immich_albums
        merge_notes.append(f"donor_albums({', '.join(donor_immich_albums)})")
    best_sr["IA-02"] = best_ia02

    if merge_notes or best_folder_tags or donor_immich_albums:
        best_sr["IA-01"] = best_ia01
        best_sr["IA-07"] = best_ia07
        best.step_result = best_sr
        flag_modified(best, "step_result")

    # ── 2. Resolve kept Immich asset ID (same-asset guard) ──────
    kept_immich_id = best.immich_asset_id or ""
    if not kept_immich_id and (best.target_path or "").startswith("immich:"):
        kept_immich_id = (best.target_path or "")[7:]

    # When best is a duplicate without its own Immich asset, it will
    # inherit one donor's asset_id (Schritt 4, line ~912). IA-08's
    # `safe_replace_asset` will then upload the kept file as a new
    # asset, copy metadata over, and delete the old (donor) asset
    # AFTER the new asset is verified — the safe_move-Garantie für
    # Immich. Therefore we MUST NOT eager-delete that one donor's
    # Immich asset here, otherwise IA-08 hits a 404 on copy_metadata.
    # Pre-v2.31.0, this pre-deletion was the root cause of the
    # "Bild verloren bei Keep This"-Vorfälle: donor asset gelöscht →
    # IA-08 versucht Replace gegen Geist → upload OK, copy 404 →
    # alter Rollback-Pfad löscht NEUES Asset → keine Datei in Immich.
    promoted_donor_asset = ""
    if (best.status == "duplicate" and not best.immich_asset_id
            and donor_immich_map):
        promoted_donor_asset = next(iter(donor_immich_map.values()))

    # ── 3. Delete donors ────────────────────────────────────────
    # NOTE: no log_info() calls inside this section — they open a
    # separate DB session which causes "database is locked" when the
    # caller's session has an open write lock. All log messages are
    # collected and written after the caller commits.
    _pending_logs: list[tuple[str, str]] = []

    deleted = 0
    errors = 0
    for donor in donors:
        # Local file
        filepath = donor.target_path or donor.original_path
        if filepath and not filepath.startswith("immich:") and os.path.exists(filepath):
            from file_operations import safe_remove_with_log
            removed = await asyncio.to_thread(safe_remove_with_log, filepath)
            if filepath not in removed:
                errors += 1
                continue  # Don't clear job if file couldn't be deleted

        # Immich asset — guard: never delete the same asset as the kept job
        donor_asset = donor_immich_map.get(donor.id, "")
        if donor_asset and donor_asset == kept_immich_id:
            _pending_logs.append(
                (f"{donor.debug_key} Immich-Asset übersprungen (gleich wie kept)",
                 f"asset={donor_asset}"))
        elif donor_asset and donor_asset == promoted_donor_asset:
            # Will be replaced by IA-08's safe_replace_asset against the
            # uploaded kept file. Eager delete here would break the
            # safe_move-Garantie (no copy verified before old destroyed).
            _pending_logs.append(
                (f"{donor.debug_key} Immich-Asset übersprungen (wird via IA-08 safe_replace ersetzt)",
                 f"asset={donor_asset}, kept={best.debug_key}"))
        elif donor_asset:
            _pending_logs.append(
                (f"{donor.debug_key} Immich-Asset wird gelöscht",
                 f"asset={donor_asset}, kept={best.debug_key}"))
            try:
                from immich_client import delete_asset
                await delete_asset(donor_asset)
                _pending_logs.append(
                    (f"{donor.debug_key} Immich-Asset gelöscht OK",
                     f"asset={donor_asset}"))
            except Exception as exc:
                _pending_logs.append(
                    (f"{donor.debug_key} Immich-Asset Löschung fehlgeschlagen",
                     f"asset={donor_asset}, error={exc}"))

        # Clear donor job
        if donor.status == "duplicate":
            donor.status = "done"
        donor.error_message = f"Duplicate deleted ({source}). Kept: {best.debug_key}"
        donor.target_path = None
        donor.file_hash = None
        donor.phash = None
        deleted += 1

    # ── 4. Handle kept job ──────────────────────────────────────
    if user_kept:
        sr = best.step_result or {}
        if not isinstance(sr.get("IA-02"), dict):
            sr["IA-02"] = sr.get("IA-02") or {}
        sr["IA-02"]["user_kept"] = True
        best.step_result = sr
        flag_modified(best, "step_result")

    if best.status == "done":
        # Already processed — apply merged data directly to Immich
        if kept_immich_id:
            from immich_client import add_asset_to_albums, tag_asset, update_asset_description

            existing_tags = set((best_sr.get("IA-08") or {}).get("immich_tags_written") or [])
            all_tags = set(best_ia07.get("keywords_written") or [])
            new_tags = [t for t in all_tags if t not in existing_tags]
            for tag in new_tags:
                try:
                    await tag_asset(kept_immich_id, tag)
                except Exception:
                    pass
            if new_tags:
                merge_notes.append(f"tags(+{len(new_tags)})")

            if donor_immich_albums:
                added = await add_asset_to_albums(kept_immich_id, donor_immich_albums)
                if added:
                    merge_notes.append(f"albums({', '.join(added)})")

            merged_desc = best_ia07.get("description_written") or ""
            existing_desc = (best_sr.get("IA-08") or {}).get("description_written") or ""
            if not existing_desc and merged_desc:
                try:
                    await update_asset_description(kept_immich_id, merged_desc)
                    merge_notes.append("description→immich")
                except Exception:
                    pass

        best.error_message = None

    elif best.status == "duplicate":
        # Transfer Immich asset ID from donor if best doesn't have one.
        if not best.immich_asset_id and donor_immich_map:
            best.immich_asset_id = next(iter(donor_immich_map.values()))

        # Force safe_replace whenever best=duplicate has any asset_id —
        # whether freshly inherited from donor OR pre-existing (e.g. job
        # was previously done, then flipped back to duplicate when a
        # later arrival was preferred). The kept file's bytes may
        # differ from whatever the asset currently holds; without this
        # signal IA-08 in sidecar mode + retry_count=0 would just
        # re-tag the existing asset and the local file content would
        # never reach Immich. Same root cause as the v2.31.0 fix, just
        # a different trigger path.
        force_reupload_after_inherit = bool(best.immich_asset_id)

        # Copy analysis steps from original (saves AI re-run costs)
        keep_steps = {"IA-01"}
        if analysis_donor:
            orig_sr = analysis_donor.step_result or {}
            for step in ("IA-03", "IA-04", "IA-05", "IA-06"):
                if orig_sr.get(step):
                    best_sr[step] = orig_sr[step]
            keep_steps = {"IA-01", "IA-02", "IA-03", "IA-04", "IA-05", "IA-06"}

        # Inject IA-02 skip (before prepare_job_for_reprocess so keep_steps preserves it)
        ia02_data = best_sr.get("IA-02") or {}
        new_ia02: dict = {"status": "skipped", "reason": f"kept via {source}"}
        if user_kept:
            new_ia02["user_kept"] = True
        if ia02_data.get("folder_tags") or best_folder_tags:
            new_ia02["folder_tags"] = ia02_data.get("folder_tags") or best_folder_tags
        if ia02_data.get("own_album") or own_album:
            new_ia02["own_album"] = ia02_data.get("own_album") or own_album
        if ia02_data.get("donor_albums") or donor_immich_albums:
            new_ia02["donor_albums"] = ia02_data.get("donor_albums") or donor_immich_albums
        best_sr["IA-02"] = new_ia02
        best.step_result = best_sr
        flag_modified(best, "step_result")

        # Move file from duplicates/ to reprocess/ and reset pipeline state
        kept_filepath = best.target_path or best.original_path
        if kept_filepath and not kept_filepath.startswith("immich:") and os.path.exists(kept_filepath):
            from pipeline.reprocess import prepare_job_for_reprocess
            inject = None
            if force_reupload_after_inherit:
                # IA-08 reads this sentinel and forces a safe_replace_asset
                # against the inherited donor asset_id. Pops itself at end
                # of IA-08 so it never persists in the final step_result.
                inject = {"_force_reupload": True}
            await prepare_job_for_reprocess(
                session, best,
                keep_steps=keep_steps,
                inject_steps=inject,
                move_file=True,
                commit=False,
            )

        best.status = "queued"
        best.error_message = f"Promoted ({source})"
        if analysis_donor:
            best.error_message += f". Analysis copied from {analysis_donor.debug_key}"
        if merge_notes:
            best.error_message += f" + merged: {', '.join(merge_notes)}"

    else:
        # Safety net (unexpected status)
        best.status = "done"
        best.error_message = None

    # Store pending logs on the function return — caller writes them
    # AFTER session.commit() to avoid "database is locked".
    async def flush_logs():
        try:
            await log_info("duplicates",
                           f"{source}: Start Duplikat-Auflösung",
                           f"kept={best.debug_key} asset={best.immich_asset_id or 'local'} "
                           f"donors={[d.debug_key for d in donors]}")
            for msg, detail in _pending_logs:
                await log_info("duplicates", msg, detail)
            await log_info("duplicates",
                           f"{source}: kept={best.debug_key} deleted={deleted} errors={errors}",
                           f"merged={merge_notes}" if merge_notes else "")
        except Exception:
            pass

    return merge_notes, deleted, errors, flush_logs


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

        group_keys = set()
        for members in merged.values():
            if group_key in members:
                group_keys = members
                break
        if not group_keys:
            group_keys = {group_key}

        result = await session.execute(
            select(Job).where(Job.debug_key.in_(group_keys))
        )
        group_jobs = result.scalars().all()

        kept_job = next((j for j in group_jobs if j.debug_key == keep_key), None)
        if not kept_job:
            return RedirectResponse(url="/duplicates", status_code=303)

        _, _, _, flush_logs = await _resolve_duplicate_group(
            session, kept_job, group_jobs,
            source="duplicate review",
            user_kept=True,
        )
        await session.commit()

    await flush_logs()
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
            "user_kept": True,
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
                from file_operations import safe_remove_with_log
                await asyncio.to_thread(safe_remove_with_log, filepath)

            job.status = "done"
            job.error_message = "Duplicate deleted (manually)"
            await session.commit()

    await log_info("duplicates", f"Duplicate deleted: {debug_key}")
    return RedirectResponse(url="/duplicates", status_code=303)
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


# Batch-clean progress tracking (in-memory, single instance)
_batch_progress: dict = {"running": False, "kept": 0, "deleted": 0, "errors": 0,
                         "total": 0, "current": 0, "done": False, "page": 0}


@router.get("/api/duplicates/batch-clean-status")
async def batch_clean_status():
    """Poll endpoint for batch-clean progress."""
    return JSONResponse(_batch_progress)


@router.post("/api/duplicates/batch-clean-quality")
async def batch_clean_quality(request: Request):
    """Quality-aware batch clean — runs as background task with progress tracking."""
    global _batch_progress

    if _batch_progress["running"]:
        return JSONResponse({"error": "Batch-Clean läuft bereits"}, status_code=409)

    try:
        form = await request.form()
        page = int(form.get("page", 1))
    except (ValueError, Exception):
        page = 1

    _batch_progress = {"running": True, "kept": 0, "deleted": 0, "errors": 0,
                       "total": 0, "current": 0, "done": False, "page": page}

    import asyncio
    asyncio.create_task(_run_batch_clean(page))

    return JSONResponse({"status": "started", "page": page})


async def _run_batch_clean(page: int):
    """Background task: process batch-clean with progress updates."""
    global _batch_progress
    logger.info("Batch-clean background task started for page=%s", page)
    from pipeline.step_ia02_duplicates import _quality_score

    per_page = 10
    BATCH_SIZE = 50

    try:
        group_index, jobs_by_key = await _build_group_index()

        if page == 0:
            page_groups = group_index
        else:
            start = (page - 1) * per_page
            page_groups = group_index[start:start + per_page]

        # Count safe groups for progress
        safe_groups = [g for g in page_groups
                       if g.get("safe_for_batch", g.get("all_exact"))]
        _batch_progress["total"] = len(safe_groups)

        idx = 0
        for batch_start in range(0, len(safe_groups), BATCH_SIZE):
            batch = safe_groups[batch_start:batch_start + BATCH_SIZE]
            batch_log_fns = []

            async with async_session() as session:
                for g in batch:
                    member_jobs = []
                    for key in g["member_keys"]:
                        job = jobs_by_key.get(key)
                        if job:
                            result = await session.execute(
                                select(Job).where(Job.id == job.id))
                            fresh = result.scalar()
                            if fresh:
                                member_jobs.append(fresh)

                    if len(member_jobs) < 2:
                        idx += 1
                        _batch_progress["current"] = idx
                        continue

                    best = max(member_jobs, key=lambda j: _quality_score(j))

                    try:
                        _, d, e, flush_fn = await _resolve_duplicate_group(
                            session, best, member_jobs,
                            source="batch-clean",
                        )
                        _batch_progress["kept"] += 1
                        _batch_progress["deleted"] += d
                        _batch_progress["errors"] += e
                        batch_log_fns.append(flush_fn)
                    except Exception as exc:
                        logger.error("Batch-clean error for group %s: %s",
                                     g.get("original_key"), exc)
                        _batch_progress["errors"] += 1

                    idx += 1
                    _batch_progress["current"] = idx

                await session.commit()

            # Write logs AFTER commit (avoids "database is locked")
            for fn in batch_log_fns:
                await fn()

        page_label = "all" if page == 0 else f"page {page}"
        summary = (f"Batch-Clean Quality ({page_label}): "
                   f"{_batch_progress['kept']} groups, "
                   f"{_batch_progress['deleted']} deleted, "
                   f"{_batch_progress['errors']} errors")
        await log_info("duplicates", summary)

    except Exception as exc:
        import traceback
        logger.error("Batch-clean crashed: %s\n%s", exc, traceback.format_exc())
        try:
            await log_info("duplicates", f"Batch-Clean abgestürzt: {exc}")
        except Exception:
            pass
    finally:
        _batch_progress["done"] = True
        _batch_progress["running"] = False
        logger.info("Batch-clean background task finished: %s", _batch_progress)

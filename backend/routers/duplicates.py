import asyncio
import copy
import logging
import os
import re
import subprocess
import traceback
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import or_, select, update
from sqlalchemy.orm.attributes import flag_modified

import fileops
from config import config_manager
from database import async_session
from file_operations import (
    is_folder_tags_active, job_asset_ids, job_copies, present_owners, resolve_filepath,
)
from models import AssetRef, Job
from pipeline.reprocess import prepare_job_for_reprocess
from system_logger import log_error, log_info, log_warning
from template_engine import render
from thumbnail_utils import (
    generate_thumbnail, raw_to_jpeg, heic_to_jpeg,
    THUMB_SIZE, PREVIEW_SIZE, HEIC_EXTENSIONS, RAW_EXTENSIONS,
)

logger = logging.getLogger("mediaassistant.routers.duplicates")

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
    filepath = await resolve_filepath(job)
    dup_info = (job.step_result or {}).get("IA-02", {})
    # is_original is determined later in _build_group_detail by
    # comparing job IDs — the oldest job (lowest ID) in the group is
    # the original (= was in the library first). This placeholder is
    # overridden below.
    is_dup = job.status == "duplicate"
    exists = bool(filepath) and os.path.isfile(filepath)

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

        # Deleted jobs (resolved donors, review deletes) and dry-run jobs are no
        # group members: a deleted original must not rejoin its group and be
        # picked as the copy to keep (R-04), a dry-run file is not in the
        # library at all (R-07).
        result = await session.execute(
            select(Job).where(Job.debug_key.in_(all_keys), Job.status != "deleted",
                              or_(Job.dry_run.is_(None), Job.dry_run.is_(False)))
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
            # with a recorded distance of 0 (100% visually identical). A
            # missing distance does not count as 0 (D9).
            safe_for_batch = all_exact or all(
                (jobs_by_key[k].step_result or {}).get("IA-02", {}).get("match_type") in ("exact", "raw_jpg_pair", None)
                or (jobs_by_key[k].step_result or {}).get("IA-02", {}).get("phash_distance") == 0
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
        filepath = await resolve_filepath(job)
        asset_id = job.immich_asset_id or ""
        target = job.target_path or ""
        if target.startswith("immich:"):
            asset_id = asset_id or target[7:]
        if filepath and os.path.isfile(filepath):
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


def _size_label(size: int) -> str:
    """Human-readable file size for the comparison tiles."""
    if not size:
        return ""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _date_label(value) -> str:
    """'2026:04:15 10:52:37' or ISO '2026-04-15T10:52:37.338+00:00' -> '2026-04-15 10:52'."""
    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})(?:[T ](\d{2}):(\d{2}))?", str(value or ""))
    if not m:
        return str(value or "")
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return f"{date} {m.group(4)}:{m.group(5)}" if m.group(4) else date


# Metadata chips of the comparison tiles: chip key -> member field
_DISPLAY_META_FIELD = {"gps": "exif_has_gps", "exif": "exif_has_exif",
                       "description": "exif_description", "keywords": "exif_keywords"}


def _annotate_for_display(members: list[dict]) -> None:
    """Presentation-only facts for the comparison tiles.

    Marks the largest resolution / file size of a group (only when the
    members differ) and which metadata chips a member has that another
    one lacks. Nothing here influences keep, batch clean or the quality score.
    """
    def pixels(m):
        return (m.get("width") or 0) * (m.get("height") or 0)

    all_px = {pixels(m) for m in members}
    all_size = {m.get("file_size") or 0 for m in members}
    for m in members:
        m["ext"] = os.path.splitext(m.get("filename") or "")[1].lstrip(".").upper()
        m["size_label"] = _size_label(m.get("file_size") or 0)
        m["date_label"] = _date_label(m.get("exif_date"))
        m["is_top_px"] = len(all_px) > 1 and pixels(m) == max(all_px) > 0
        m["is_top_size"] = len(all_size) > 1 and (m.get("file_size") or 0) == max(all_size) > 0
        m["meta_have"] = [k for k, field in _DISPLAY_META_FIELD.items() if m.get(field)]
        m["meta_extra"] = [
            k for k in m["meta_have"]
            if any(not o.get(_DISPLAY_META_FIELD[k]) for o in members if o is not m)
        ]


@router.get("/duplicates")
async def duplicates_page(request: Request):
    if not await config_manager.is_setup_complete():
        return RedirectResponse(url="/setup", status_code=302)

    skip_confirm = await config_manager.get("duplikat.skip_confirm", False)
    # A resolution a crash left half done is finished first (in the background).
    _resume_in_background()

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
        _annotate_for_display(members)
        groups.append({
            "original_key": g["original_key"],
            "members": members,
            "count": g["count"],
            "all_exact": g["all_exact"],
            "is_immich_duplicate": g["is_immich_duplicate"],
        })
    page_exact_groups = sum(1 for g in page_groups if g.get("safe_for_batch", g.get("all_exact")))

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
        "page_exact_groups": page_exact_groups,
        "skip_confirm": skip_confirm,
        "current_page": page,
        "total_pages": total_pages,
        "per_page": per_page,
        "page_numbers": page_numbers,
        "notice": _notice(request),
    })


@router.get("/api/thumbnail/{job_id}")
async def thumbnail(job_id: int, size: str = "thumbnail"):
    """Serve a JPEG thumbnail for a job's image. size=thumbnail|preview"""
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

    filepath = await resolve_filepath(job)
    if not filepath or not os.path.isfile(filepath):
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


# ═══ Duplicate resolution ═══════════════════════════════════════════════
#
# Resolving a group is a plan carried out in a fixed order (R-04, R-09,
# R-10a):
#
# 1. Claim. Every member goes atomically from the status it was seen in to
#    'resolving' (UPDATE … WHERE status = seen) and carries the plan — kept
#    job, members, their previous statuses — in step_result. A member that
#    is queued, processing or already being resolved stops the whole group
#    before anything is touched. The start is logged before any change.
# 2. The kept copy is checked: a copy of the kept job (register, or its old
#    fields registered on first touch) that verifies right now. Without one
#    nothing is deleted and the claim is released.
# 3. Donors: their own copies go to the trash through fileops (the set's
#    trash folder, Immich's trash), never deleted for good, each with intent
#    and outcome in the ledger. An asset the kept job uses, or a job outside
#    the group, stays.
# 4. The kept job: a kept duplicate is brought into reprocess/ and queued; a
#    kept original gets the merged tags, albums and — only where the user
#    has not written one — the description in Immich.
# 5. The final state of all members in one commit; the plan is removed.
#
# A crash in between leaves the members in 'resolving' with their plan, and
# the ledger shows what is in the trash already. resume_interrupted_
# resolutions() carries such a plan out again; every step may run twice.

RESOLVING = "resolving"
PLAN_KEY = "resolving"    # step_result: the plan while the group is being resolved
MERGE_KEY = "dup_merge"   # step_result: what the kept job took over from its donors (R-20)
_KEEPABLE = ("duplicate", "done", "review", "error")
_STALE_CLAIM = timedelta(minutes=10)
_ACTIVE: set[int] = set()  # job ids being resolved in this process
_RESUME_LOCK = asyncio.Lock()


class Resolution(NamedTuple):
    merge_notes: list
    deleted: int    # donors whose copies are in the trash now
    errors: int     # donors that could not be resolved
    problems: list  # {"code", "text", "fatal", …}: why something was refused or left

    @property
    def refused(self) -> bool:
        """Nothing was done: the group stays as it was."""
        return any(p.get("fatal") for p in self.problems)


def _job_id(job) -> int:
    return job if isinstance(job, int) else job.id


def _problem(code: str, text: str, *, fatal: bool = False, **params) -> dict:
    return {"code": code, "text": text, "fatal": fatal, **params}


def _without_plan(step_result) -> dict:
    sr = dict(step_result or {})
    sr.pop(PLAN_KEY, None)
    return sr


def _drop_plan(job) -> None:
    job.step_result = _without_plan(job.step_result)
    flag_modified(job, "step_result")


async def _claim_group(kept_id: int, member_ids: list[int], *, source: str, user_kept: bool,
                       resume: dict | None) -> tuple[dict, dict | None, dict | None]:
    """Claim every member for the resolution (R-09).

    Returns (members by id, plan, problem). The members are snapshots read
    in the claim transaction; nothing may write them back.
    """
    async with async_session() as session:
        jobs = {j.id: j for j in (await session.execute(
            select(Job).where(Job.id.in_(member_ids)))).scalars().all()}
    kept = jobs.get(kept_id)
    if kept is None:
        return {}, None, _problem("not_found", "Die Datei gibt es nicht mehr", fatal=True)
    busy, dry, seen = [], [], {}
    for jid, job in jobs.items():
        plan = (job.step_result or {}).get(PLAN_KEY)
        if job.status == RESOLVING:
            if (resume and isinstance(plan, dict) and plan.get("id") == resume["id"]
                    and jid not in _ACTIVE):
                seen[jid] = RESOLVING
            else:
                busy.append(job.debug_key)
        elif job.status in ("queued", "processing") or jid in _ACTIVE:
            busy.append(job.debug_key)
        elif job.dry_run:
            dry.append(job.debug_key)
        else:
            seen[jid] = job.status
    if busy:
        return {}, None, _problem("busy", f"Dateien der Gruppe werden gerade verarbeitet: {', '.join(busy)}",
                                  fatal=True, jobs=", ".join(busy))
    if dry:
        return {}, None, _problem("dry_run", f"Testlauf-Jobs werden nicht aufgelöst: {', '.join(dry)}",
                                  fatal=True, jobs=", ".join(dry))
    prev = dict(resume["prev"]) if resume else {str(jid): status for jid, status in seen.items()}
    kept_prev = prev.get(str(kept_id))
    if kept_prev not in _KEEPABLE:
        return {}, None, _problem("kept_status", f"Eine Datei im Status '{kept_prev}' kann nicht behalten werden",
                                  fatal=True, status=str(kept_prev))
    plan = resume or {
        "id": uuid.uuid4().hex, "kept": kept_id, "members": sorted(seen), "source": source,
        "user_kept": user_kept, "since": datetime.now().isoformat(timespec="seconds"), "prev": prev,
    }
    async with async_session() as session:
        for jid, status in seen.items():
            res = await session.execute(
                update(Job).where(Job.id == jid, Job.status == status).values(status=RESOLVING)
                .execution_options(synchronize_session=False))
            if res.rowcount != 1:
                await session.rollback()
                return {}, None, _problem("busy", f"{jobs[jid].debug_key} hat inzwischen den Status gewechselt",
                                          fatal=True, jobs=jobs[jid].debug_key)
        claimed = (await session.execute(select(Job).where(Job.id.in_(list(seen)))
                                         .execution_options(populate_existing=True))).scalars().all()
        for job in claimed:
            sr = dict(job.step_result or {})
            sr[PLAN_KEY] = plan
            job.step_result = sr
            flag_modified(job, "step_result")
        await session.commit()
    _ACTIVE.update(seen)
    return {j.id: j for j in claimed}, plan, None


async def _release(plan: dict, job_ids) -> None:
    """Give claimed members their previous status back; nothing was done to them."""
    async with async_session() as session:
        for job in (await session.execute(select(Job).where(Job.id.in_(list(job_ids))))).scalars().all():
            if job.status != RESOLVING or ((job.step_result or {}).get(PLAN_KEY) or {}).get("id") != plan["id"]:
                continue
            job.status = plan["prev"].get(str(job.id), "duplicate")
            _drop_plan(job)
        await session.commit()


async def _api_key(job) -> str | None:
    if not job.immich_user_id:
        return None
    from immich_client import get_user_api_key
    return await get_user_api_key(job.immich_user_id)


@dataclass
class _Merge:
    step_result: dict        # kept job's step_result with the donors' data, plan removed
    notes: list              # short labels for logs and the batch summary
    keywords: list           # all keywords the kept job should carry
    taken_keywords: list     # keywords that came from donors (not the kept job's own)
    description: str         # the description the kept job should carry
    own_description: str     # what MediaAssistant wrote for the kept job itself
    donor_albums: list
    analysis_donor: object   # donor whose IA-03…IA-06 a kept duplicate takes over
    ia02_skip: dict          # IA-02 result of a kept duplicate's next run


async def _merge(kept, donors, prev: dict, *, source: str, user_kept: bool) -> _Merge:
    """GPS, date, keywords, folder tags, description and albums of the donors,
    merged into the kept job (v2.29.5). Reads only (Immich: album lookups)."""
    from immich_client import get_asset_albums
    from pipeline.step_ia02_duplicates import _extract_folder_tags

    sr = copy.deepcopy(_without_plan(kept.step_result))
    ia01 = dict(sr.get("IA-01") or {})
    ia02 = dict(sr["IA-02"]) if isinstance(sr.get("IA-02"), dict) else {}
    ia07 = dict(sr["IA-07"]) if isinstance(sr.get("IA-07"), dict) else {}
    own_keywords = [k for k in (ia07.get("keywords_written") or []) if k]
    own_description = ia07.get("description_written") or ""
    folder_tags = list(ia02.get("folder_tags") or [])
    own_album = ia02.get("own_album") or (folder_tags[-1] if folder_tags else "")
    keywords = list(own_keywords)
    description = own_description
    donor_albums: list[str] = []
    notes: list[str] = []
    # Folder tags count only while the kept job's set has them on — a donor
    # processed back when they were on must not carry them over (they would
    # become keywords and Immich tags the user switched off).
    folder_tags_on = await is_folder_tags_active(kept)
    folder_words: set[str] = set()

    for donor in donors:
        dsr = donor.step_result or {}
        d01 = dsr.get("IA-01") or {}
        d02 = dsr.get("IA-02") if isinstance(dsr.get("IA-02"), dict) else {}
        d03 = dsr.get("IA-03") or {}
        d07 = dsr.get("IA-07") if isinstance(dsr.get("IA-07"), dict) else {}

        if not ia01.get("gps") and d01.get("gps"):
            ia01.update(gps=True, gps_lat=d01.get("gps_lat"), gps_lon=d01.get("gps_lon"))
            if d03 and d03.get("status") != "skipped":
                sr["IA-03"] = dict(d03, copied_from=donor.debug_key)
            notes.append("GPS")
        if not ia01.get("date") and d01.get("date"):
            ia01["date"] = d01["date"]
            notes.append("date")

        new_keywords = [k for k in (d07.get("keywords_written") or []) if k and k not in keywords]
        if new_keywords:
            keywords.extend(new_keywords)
            notes.append(f"keywords(+{len(new_keywords)})")

        donor_ft: list[str] = []
        donor_ft_raw = d02.get("folder_tags") or (_extract_folder_tags(donor) if donor.folder_tags else [])
        folder_words.update(donor_ft_raw)
        if folder_tags_on:
            donor_ft = donor_ft_raw
            new_ft = [t for t in donor_ft if t and t not in folder_tags]
            if new_ft:
                folder_tags.extend(new_ft)
                notes.append(f"folder_tags(+{len(new_ft)})")

        # Albums of the donor: Immich → what IA-08 recorded → its folder
        found: list[str] = []
        for asset_id in job_asset_ids(donor):
            try:
                found = await get_asset_albums(asset_id, api_key=await _api_key(donor))
            except Exception as exc:
                logger.info("albums of %s (%s): %s", asset_id, donor.debug_key, exc)
            if found:
                break
        if not found:
            found = list((dsr.get("IA-08") or {}).get("immich_albums_added") or [])
        if not found and donor_ft:
            found = [donor_ft[-1]]
        for album in found:
            if album and album not in donor_albums:
                donor_albums.append(album)
            folder_words.add(album)
            folder_words.update((album or "").split())
            # Album names become keywords only with folder tags on; the album
            # itself is added in Immich either way.
            if folder_tags_on:
                for name in [album, *(album or "").split()]:
                    if name and name not in folder_tags:
                        folder_tags.append(name)

        if not description and d07.get("description_written"):
            description = d07["description_written"]
            notes.append("description")

    for tag in folder_tags:
        if tag and tag not in keywords:
            keywords.append(tag)
    if donor_albums:
        notes.append(f"donor_albums({', '.join(donor_albums)})")

    if folder_tags:
        ia02["folder_tags"] = folder_tags
    if own_album:
        ia02["own_album"] = own_album
    if donor_albums:
        ia02["donor_albums"] = donor_albums
    if user_kept:
        ia02["user_kept"] = True
    sr["IA-01"] = ia01
    sr["IA-02"] = ia02
    if keywords:
        ia07["keywords_written"] = keywords
        ia07["tags_count"] = len(keywords)
    if description:
        ia07["description_written"] = description
    if ia07:
        sr["IA-07"] = ia07

    taken = [k for k in keywords if k not in own_keywords]
    if not folder_tags_on:
        taken = [k for k in taken if k not in folder_words]
    donor_keys = [d.debug_key for d in donors]
    if taken or description != own_description or donor_albums:
        sr[MERGE_KEY] = {
            "keywords": taken, "description": description if description != own_description else None,
            "albums": donor_albums, "from": donor_keys, "source": source,
            "at": datetime.now().isoformat(timespec="seconds"),
        }

    analysis_donor = next((d for d in donors if prev.get(str(d.id)) != "duplicate"
                           and (d.step_result or {}).get("IA-05")), None)
    ia02_skip = {"status": "skipped", "reason": f"kept via {source}"}
    for key in ("user_kept", "folder_tags", "own_album", "donor_albums"):
        if ia02.get(key):
            ia02_skip[key] = ia02[key]
    return _Merge(sr, notes, keywords, taken, description, own_description, donor_albums,
                  analysis_donor, ia02_skip)


def _promotion_step_result(merge: _Merge) -> tuple[dict, set]:
    """step_result and keep_steps for the next run of a kept duplicate.

    IA-03…IA-06 come from the analysis donor (saves the AI run), marked with
    their origin; the donor's IA-04 work files stay the donor's (its temp
    paths are not taken over). IA-07 runs again and writes the file: the
    keywords taken over from donors go in with the AI tags, so the run
    writes them into the file and into Immich (R-20); MERGE_KEY records
    them. IA-02 becomes 'skipped' so the run does not flag the kept file as
    a duplicate again.
    """
    sr = dict(merge.step_result)
    keep = {"IA-01", "IA-02", MERGE_KEY}
    donor = merge.analysis_donor
    if donor is not None:
        dsr = donor.step_result or {}
        for code in ("IA-03", "IA-04", "IA-05", "IA-06"):
            if isinstance(dsr.get(code), dict):
                result = dict(dsr[code], copied_from=donor.debug_key)
                if code == "IA-04":
                    for field_name in ("temp_path", "temp_paths", "video_frames"):
                        result.pop(field_name, None)
                sr[code] = result
        keep |= {"IA-03", "IA-04", "IA-05", "IA-06"}
    ia05 = sr.get("IA-05")
    if merge.taken_keywords and isinstance(ia05, dict) and "IA-05" in keep:
        tags = list(ia05.get("tags") or [])
        added = [k for k in merge.taken_keywords if k not in tags]
        if added:
            sr["IA-05"] = dict(ia05, tags=tags + added, merged_tags=added)
        if not ia05.get("description") and merge.description:
            sr["IA-05"] = dict(sr["IA-05"], description=merge.description)
    sr["IA-02"] = merge.ia02_skip
    return sr, keep


async def _users_outside(asset_id: str, group_ids) -> list[str]:
    """Debug keys of jobs outside the group that point at or reference the asset."""
    async with async_session() as session:
        keys = list((await session.execute(select(Job.debug_key).where(
            Job.id.notin_(list(group_ids)),
            or_(Job.immich_asset_id == asset_id, Job.target_path == f"immich:{asset_id}")))).scalars().all())
        ref_jobs = list((await session.execute(select(AssetRef.job_id).where(
            AssetRef.location == asset_id, AssetRef.job_id.notin_(list(group_ids))))).scalars().all())
        if ref_jobs:
            keys += list((await session.execute(select(Job.debug_key).where(Job.id.in_(ref_jobs)))).scalars().all())
    return sorted(set(k or "?" for k in keys))


async def _let_go_of_asset(asset_id: str, group_ids, except_id: int) -> None:
    """Other members of the group stop pointing at an asset that is about to
    go to the trash — fileops would count them as its users otherwise."""
    async with async_session() as session:
        for job in (await session.execute(select(Job).where(
                Job.id.in_(list(group_ids)), Job.id != except_id,
                or_(Job.immich_asset_id == asset_id, Job.target_path == f"immich:{asset_id}")))).scalars().all():
            if job.immich_asset_id == asset_id:
                job.immich_asset_id = None
            if job.target_path == f"immich:{asset_id}":
                job.target_path = None
        for ref in (await session.execute(select(AssetRef).where(
                AssetRef.location == asset_id, AssetRef.job_id.in_(list(group_ids)),
                AssetRef.job_id != except_id))).scalars().all():
            await session.delete(ref)
        await session.commit()


def _is_own_log(path: str, debug_key: str) -> bool:
    """MediaAssistant's .log next to a duplicate or error file starts with the job's debug key."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readline().strip() == f"Debug-Key: {debug_key}"
    except OSError:
        return False


async def _trash_companions(donor, path: str, *, actor: str, reason: str, out: dict) -> None:
    """The .xmp sidecar and MediaAssistant's .log next to a donor file that went to the trash."""
    sidecar = path + ".xmp"
    if os.path.isfile(sidecar):
        try:
            row = await fileops.register_existing(donor, "sidecar", sidecar)
            if row.job_id == donor.id:
                await fileops.trash(donor, row, actor=actor, reason=reason, allow_last_copy=True)
                out["trashed"] += 1
        except fileops.FileOpError as exc:
            out["notes"].append(f"{os.path.basename(sidecar)}: bleibt ({exc})")
    log_path = path + ".log"
    if os.path.isfile(log_path) and await asyncio.to_thread(_is_own_log, log_path, donor.debug_key):
        try:
            row = await fileops.register_existing(donor, "temp", log_path)
            if row.job_id == donor.id:
                await fileops.discard(donor, row, actor=actor, reason="Protokoll der Duplikat-Erkennung")
        except fileops.FileOpError as exc:
            out["notes"].append(f"{os.path.basename(log_path)}: bleibt ({exc})")


async def _retire_donor(donor, kept, group_ids, kept_assets: set, promoted: str | None, *,
                        actor: str, reason: str) -> dict:
    """Move a donor's own copies to the trash; returns what happened.

    ok=False: a copy that should go is still there because fileops could not
    move it (it says why) — the donor is not resolved then.
    """
    out = {"ok": True, "notes": [], "trashed": 0, "local_gone": False, "asset_gone": False}
    rows, notes = await job_copies(donor, check=False)
    out["notes"] += notes
    target = os.path.abspath(donor.target_path) if donor.target_path and \
        not donor.target_path.startswith("immich") else None
    if target and target not in {r.location for r in rows} and os.path.isfile(target) \
            and (await present_owners([target])).get(target) is None:
        # A file at the donor's place that is not its content (or cannot be
        # checked): nothing of this donor is touched, it stays visible with it.
        out["ok"] = False
        return out
    for row in rows:
        name = os.path.basename(row.location)
        if row.kind == "immich":
            await _release_asset(donor, row, kept, group_ids, kept_assets, promoted,
                                 actor=actor, reason=reason, out=out)
            continue
        if row.job_id != donor.id:
            await fileops.drop_ref(donor, row.kind, row.location)
            out["notes"].append(f"{name}: gehört einem anderen Job, bleibt")
            continue
        try:
            if row.kind == "temp":
                await fileops.discard(donor, row, actor=actor, reason=reason)
            else:
                await fileops.trash(donor, row, actor=actor, reason=reason, allow_last_copy=True)
                out["trashed"] += 1
        except fileops.FileOpBusy as exc:
            out["ok"] = False
            out["notes"].append(f"{name}: {exc}")
            continue
        except fileops.FileOpRefused as exc:
            # Another job uses this very file: it stays, for that job.
            out["notes"].append(f"{name}: bleibt ({exc})")
            continue
        except fileops.FileOpError as exc:
            out["ok"] = False
            out["notes"].append(f"{name}: nicht in den Papierkorb verschoben ({exc})")
            continue
        if row.location == target:
            out["local_gone"] = True
        if row.kind in ("local", "work"):
            await _trash_companions(donor, row.location, actor=actor, reason=reason, out=out)
    return out


async def _release_asset(donor, row, kept, group_ids, kept_assets: set, promoted: str | None, *,
                         actor: str, reason: str, out: dict) -> None:
    asset_id = row.location
    if asset_id in kept_assets or asset_id == promoted:
        # The kept job uses this asset (shared, or it takes it over so that
        # IA-08 replaces it): it stays, and belongs to the kept job now.
        why = "vom behaltenen Job verwendet" if asset_id in kept_assets else "geht an den behaltenen Job"
        if row.job_id == donor.id:
            await fileops.transfer(row, kept, actor=actor,
                                   reason=f"Duplikat aufgelöst: {kept.debug_key} übernimmt von {donor.debug_key}")
        else:
            await fileops.drop_ref(donor, "immich", asset_id)
        out["notes"].append(f"Asset {asset_id} bleibt ({why})")
        out["asset_gone"] = True
        return
    if row.job_id != donor.id:
        await fileops.drop_ref(donor, "immich", asset_id)
        out["notes"].append(f"Asset {asset_id} gehört einem anderen Job, bleibt")
        out["asset_gone"] = True
        return
    outside = await _users_outside(asset_id, group_ids)
    if outside:
        out["notes"].append(f"Asset {asset_id} bleibt (verwendet von {', '.join(outside)})")
        out["asset_gone"] = True
        return
    await _let_go_of_asset(asset_id, group_ids, donor.id)
    try:
        await fileops.trash_asset(donor, row, actor=actor, reason=reason, allow_last_copy=True)
        out["trashed"] += 1
        out["asset_gone"] = True
    except fileops.FileOpBusy as exc:
        out["ok"] = False
        out["notes"].append(f"Asset {asset_id}: {exc}")
    except fileops.FileOpRefused as exc:
        # Not visible to the key any more (gone, locked) or used by another
        # job after all: nothing of the donor's to move.
        out["notes"].append(f"Asset {asset_id} bleibt ({exc})")
        out["asset_gone"] = True
    except fileops.FileOpError as exc:
        out["ok"] = False
        out["notes"].append(f"Asset {asset_id}: nicht in den Immich-Papierkorb verschoben ({exc})")


async def _promoted_asset(kept, donors, prev: dict, kept_assets: set, group_ids) -> str | None:
    """A donor's asset the kept duplicate takes over, so that IA-08 replaces
    it — its albums, favorite and shared links move to the new version —
    instead of uploading next to it. Only for a kept duplicate bound for
    Immich that has no asset of its own, and only an asset that is present
    now (R-04) and that no job outside the group uses."""
    if prev.get(str(kept.id)) != "duplicate" or not kept.use_immich or job_asset_ids(kept):
        return None
    for donor in sorted(donors, key=lambda d: (prev.get(str(d.id)) == "duplicate", d.id)):
        for asset_id in job_asset_ids(donor):
            if asset_id in kept_assets or await _users_outside(asset_id, group_ids):
                continue
            try:
                row = await fileops.immich_copy(donor, asset_id)
            except fileops.FileOpError as exc:
                logger.info("asset %s of %s not checkable: %s", asset_id, donor.debug_key, exc)
                continue
            if row.state == "present":
                return asset_id
    return None


async def _apply_merge_in_immich(kept, asset_id: str, merge: _Merge, notes: list) -> None:
    """A kept original that is already in Immich: add the donors' tags and
    albums to its asset. The description is written only where the asset
    has none or still has MediaAssistant's own one — a text the user wrote
    in Immich stays (R-19b)."""
    from immich_client import add_asset_to_albums, get_asset_info, tag_asset, update_asset_description

    key = await _api_key(kept)
    info = await get_asset_info(asset_id, api_key=key) or {}
    on_asset = {t.get("value") or t.get("name") for t in (info.get("tags") or []) if isinstance(t, dict)}
    written = set(((kept.step_result or {}).get("IA-08") or {}).get("immich_tags_written") or [])
    new_tags = [t for t in merge.keywords if t not in written and t.replace("/", " - ") not in on_asset]
    if not await is_folder_tags_active(kept):
        new_tags = [t for t in new_tags if t in merge.taken_keywords or t in written]
    added = 0
    for tag in new_tags:
        try:
            await tag_asset(asset_id, tag, api_key=key)
            added += 1
        except Exception as exc:
            logger.warning("tag %s on %s: %s", tag, asset_id, exc)
    if added:
        notes.append(f"tags(+{added})")
    if merge.donor_albums:
        albums = await add_asset_to_albums(asset_id, merge.donor_albums, api_key=key)
        if albums:
            notes.append(f"albums({', '.join(albums)})")
    current = ((info.get("exifInfo") or {}).get("description") or "").strip()
    wanted = (merge.description or "").strip()
    if not wanted or wanted == current:
        return
    if current and current != merge.own_description.strip():
        notes.append("Beschreibung des Benutzers bleibt")
        return
    await log_info("duplicates", f"{kept.debug_key} Beschreibung in Immich wird gesetzt",
                   f"Asset {asset_id}: bisher {current!r}")
    try:
        await update_asset_description(asset_id, wanted, api_key=key)
    except Exception as exc:
        await log_warning("duplicates", f"{kept.debug_key} Beschreibung in Immich nicht gesetzt",
                          f"Asset {asset_id}: {type(exc).__name__}: {exc}")
        return
    await log_info("duplicates", f"{kept.debug_key} Beschreibung in Immich gesetzt", f"Asset {asset_id}")
    notes.append("description→immich")


async def _resolve_duplicate_group(best, member_jobs, *, source: str = "review",
                                   user_kept: bool = False, resume: dict | None = None) -> Resolution:
    """Resolve a duplicate group: keep `best`, merge the donors' metadata into
    it, move the donors' own copies to the trash. Shared core of "Dieses
    behalten" and the batch clean.

    `best` and `member_jobs` are Job objects or ids. They are only read;
    every change is written in sessions of this function's own, so a caller
    must not hold uncommitted writes to these jobs.
    """
    kept_id = _job_id(best)
    member_ids = list(dict.fromkeys([kept_id, *(_job_id(m) for m in member_jobs)]))
    members, plan, problem = await _claim_group(kept_id, member_ids, source=source,
                                                user_kept=user_kept, resume=resume)
    if problem:
        who = getattr(best, "debug_key", None) or f"Job #{kept_id}"
        await log_warning("duplicates", f"{source}: Duplikat-Auflösung nicht begonnen ({who})", problem["text"])
        return Resolution([], 0, 0, [problem])
    try:
        return await _carry_out(members, plan, source=source, user_kept=user_kept)
    except Exception as exc:
        # The members stay claimed with their plan; resume_interrupted_resolutions
        # takes it up again later. Nothing here deleted anything for good.
        logger.exception("duplicate resolution %s failed", plan["id"])
        await log_error("duplicates", f"{source}: Duplikat-Auflösung abgebrochen",
                        f"Plan {plan['id'][:8]}, behalten #{kept_id}: {type(exc).__name__}: {exc}")
        return Resolution([], 0, 0, [_problem("failed", f"{type(exc).__name__}: {exc}", fatal=True,
                                              detail=str(exc))])
    finally:
        _ACTIVE.difference_update(members)


async def _carry_out(members: dict, plan: dict, *, source: str, user_kept: bool) -> Resolution:
    kept = members[plan["kept"]]
    donors = [job for jid, job in members.items() if jid != kept.id]
    prev = plan["prev"]
    kept_prev = prev[str(kept.id)]
    group_ids = set(members)
    actor = "user:keep" if user_kept else "user:batch-clean"
    reason = f"Duplikat aufgelöst ({source}), behalten: {kept.debug_key}"
    await log_info("duplicates", f"{source}: Start Duplikat-Auflösung",
                   f"behalten={kept.debug_key} ({kept_prev}), Spender="
                   f"{[f'{d.debug_key} ({prev.get(str(d.id))})' for d in donors]}, Plan {plan['id'][:8]}")

    # 2. The kept copy exists, checked now (R-04)
    kept_rows, kept_notes = await job_copies(kept)
    if not kept_rows:
        detail = "; ".join(kept_notes) or "kein Exemplar im Register, an target_path oder in Immich"
        await log_warning("duplicates", f"{kept.debug_key} Duplikat-Auflösung abgebrochen: behaltene Datei fehlt",
                          f"{detail} — nichts gelöscht")
        await _release(plan, members)
        return Resolution([], 0, 0, [_problem(
            "kept_missing", f"Die behaltene Datei ist nicht auffindbar oder lässt sich nicht prüfen: {detail}",
            fatal=True, name=kept.filename, detail=detail)])
    kept_assets = {r.location for r in kept_rows if r.kind == "immich"} | set(job_asset_ids(kept))

    # 3. What the kept job takes over, and the asset a kept duplicate inherits
    merge = await _merge(kept, donors, prev, source=source, user_kept=user_kept)
    promoted = await _promoted_asset(kept, donors, prev, kept_assets, group_ids)

    # 4. Donors: their own copies go to the trash
    outcomes = {}
    for donor in donors:
        outcomes[donor.id] = await _retire_donor(donor, kept, group_ids, kept_assets, promoted,
                                                 actor=actor, reason=reason)
        out = outcomes[donor.id]
        log = log_info if out["ok"] else log_warning
        await log("duplicates", f"{donor.debug_key} Spender {'aufgelöst' if out['ok'] else 'nicht aufgelöst'}"
                                f" (behalten: {kept.debug_key})",
                  f"{out['trashed']} Exemplar(e) im Papierkorb" + (f"; {'; '.join(out['notes'])}" if out["notes"] else ""))

    # 5. The kept job, then the final state of everyone in one commit
    merge_notes = list(merge.notes)
    if kept_prev in ("done", "review"):
        present = [r.location for r in kept_rows if r.kind == "immich" and r.state == "present"]
        if present:
            try:
                await _apply_merge_in_immich(kept, present[0], merge, merge_notes)
            except Exception as exc:
                await log_warning("duplicates", f"{kept.debug_key} Übernommene Daten nicht in Immich gesetzt",
                                  f"Asset {present[0]}: {type(exc).__name__}: {exc}")

    problems = []
    async with async_session() as session:
        rows = {j.id: j for j in (await session.execute(
            select(Job).where(Job.id.in_(list(group_ids))))).scalars().all()}
        job = rows[kept.id]
        promoted_ok = True
        if kept_prev == "duplicate":
            step_result, keep_steps = _promotion_step_result(merge)
            job.step_result = step_result
            flag_modified(job, "step_result")
            if promoted:
                job.immich_asset_id = promoted
            run_params = {"_force_reupload": True} if job.immich_asset_id else None
            # fileops writes in sessions of its own: nothing may be flushed here yet.
            with session.no_autoflush:
                promoted_ok = await prepare_job_for_reprocess(
                    session, job, keep_steps=keep_steps, inject_steps={"IA-02": merge.ia02_skip},
                    run_params=run_params, move_file=True, commit=False, actor=actor)
            if promoted_ok:
                job.error_message = f"Promoted ({source})"
                if merge.analysis_donor is not None:
                    job.error_message += f". Analysis copied from {merge.analysis_donor.debug_key}"
                if merge_notes:
                    job.error_message += f" + merged: {', '.join(merge_notes)}"
            else:
                # Nothing moved: the job stays a visible duplicate, ready for another try.
                job.status = "duplicate"
                job.step_result = _without_plan(kept.step_result)
                job.target_path = kept.target_path
                job.original_path = kept.original_path
                job.error_message = ("Behalten, aber die Datei konnte nicht für die Verarbeitung bereitgestellt "
                                     "werden — bitte erneut behalten")
                problems.append(_problem("kept_not_moved", job.error_message, name=kept.filename))
        else:
            job.step_result = merge.step_result
            flag_modified(job, "step_result")
            job.status = kept_prev
            if kept_prev == "error":
                job.error_message = ((kept.error_message or "").rstrip()
                                     + f"\nBehalten bei der Duplikat-Auflösung ({source}).").strip()

        deleted = errors = 0
        now = datetime.now()
        for donor in donors:
            out = outcomes[donor.id]
            row = rows.get(donor.id)
            if row is None:
                continue
            _drop_plan(row)
            if out["ok"]:
                row.status = "deleted"
                row.error_message = f"Duplicate deleted ({source}). Kept: {kept.debug_key}"
                row.target_path = None
                row.immich_asset_id = None
                row.file_hash = None
                row.phash = None
                row.completed_at = now
                deleted += 1
            else:
                row.status = prev.get(str(donor.id), "duplicate")
                if out["local_gone"]:
                    row.target_path = None
                if out["asset_gone"]:
                    if row.target_path and row.target_path.startswith("immich:"):
                        row.target_path = None
                    row.immich_asset_id = None
                errors += 1
                problems.append(_problem("donor_failed", "; ".join(out["notes"]), name=donor.filename,
                                         detail="; ".join(out["notes"])))
        await session.commit()

    await log_info("duplicates", f"{source}: kept={kept.debug_key} deleted={deleted} errors={errors}",
                   f"merged={merge_notes}" if merge_notes else "")
    return Resolution(merge_notes, deleted, errors, problems)


async def resume_interrupted_resolutions() -> int:
    """Carry out resolutions a crash left half done (members in 'resolving').

    Meant for startup and for the duplicates page. A claim younger than
    _STALE_CLAIM may belong to a resolution still running in another
    process (a test script) and is left alone. Returns how many plans ran.
    """
    if _RESUME_LOCK.locked():
        return 0
    async with _RESUME_LOCK:
        cutoff = datetime.now() - _STALE_CLAIM
        async with async_session() as session:
            rows = (await session.execute(select(Job).where(Job.status == RESOLVING))).scalars().all()
        plans: dict[str, dict] = {}
        for job in rows:
            if job.id in _ACTIVE:
                continue
            plan = (job.step_result or {}).get(PLAN_KEY)
            if not isinstance(plan, dict) or not plan.get("id"):
                continue
            try:
                since = datetime.fromisoformat(plan.get("since") or "")
            except ValueError:
                since = cutoff
            if since <= cutoff:
                plans.setdefault(plan["id"], plan)
        for plan in plans.values():
            await log_warning("duplicates", "Unterbrochene Duplikat-Auflösung wird fortgesetzt",
                              f"Plan {plan['id'][:8]} ({plan.get('source')}), behalten #{plan.get('kept')}, "
                              f"Mitglieder {plan.get('members')}, seit {plan.get('since')}")
            if plan.get("action") == "single":
                # A not-duplicate / delete that stopped half way: give the job back.
                await _release(plan, plan.get("members") or [])
                continue
            res = await _resolve_duplicate_group(plan["kept"], plan.get("members") or [],
                                                 source=plan.get("source") or "resume",
                                                 user_kept=bool(plan.get("user_kept")), resume=plan)
            if res.refused:
                await _release(plan, plan.get("members") or [])
        return len(plans)


def _resume_in_background() -> None:
    if not _RESUME_LOCK.locked():
        asyncio.create_task(resume_interrupted_resolutions())


def _redirect(url: str, code: str | None = None, *, tone: str = "error", **params) -> RedirectResponse:
    """Back to a page, with a message for the user (translated there: t.duplicates.msg_<code>)."""
    if code:
        query = {"msg": code, "tone": tone, **{k: str(v)[:500] for k, v in params.items() if v is not None}}
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url=url, status_code=303)


def _notice(request: Request) -> dict | None:
    code = request.query_params.get("msg")
    if not code:
        return None
    tone = request.query_params.get("tone", "error")
    return {"code": code, "tone": tone if tone in ("error", "warning", "success", "info") else "error",
            "name": request.query_params.get("name", ""), "detail": request.query_params.get("detail", ""),
            "n": request.query_params.get("n", "")}


def _resolution_redirect(res: Resolution, name: str) -> RedirectResponse:
    if res.refused:
        problem = next(p for p in res.problems if p.get("fatal"))
        return _redirect("/duplicates", f"keep_{problem['code']}", name=name,
                         detail=problem.get("detail") or problem.get("jobs") or problem.get("status") or problem["text"])
    if res.problems:
        return _redirect("/duplicates", "keep_partial", tone="warning", name=name, n=res.errors or len(res.problems),
                         detail="; ".join(p["text"] for p in res.problems))
    return _redirect("/duplicates")


async def _group_keys(group_key: str, keep_key: str) -> set[str]:
    """Debug keys of the group of `group_key` (transitive over the duplicate links)."""
    async with async_session() as session:
        rows = (await session.execute(select(Job.debug_key, Job.step_result).where(
            Job.status == "duplicate"))).all()
    links = []
    for key, sr in rows:
        original = ((sr or {}).get("IA-02") or {}).get("original_debug_key") if isinstance(sr, dict) else None
        if original:
            links.append((key, original))
    for members in _union_find_groups(links).values():
        if group_key in members:
            return set(members)
    return {group_key, keep_key}


@router.post("/api/duplicates/keep")
async def keep_file(request: Request):
    """Keep one file of a group; the others go to the trash, their metadata to the kept one."""
    form = await request.form()
    keep_key = form.get("keep_key")
    group_key = form.get("group_key")

    if not keep_key or not group_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    keys = await _group_keys(group_key, keep_key)
    async with async_session() as session:
        # The same members the page shows: no deleted and no dry-run jobs (_build_group_index).
        group_jobs = (await session.execute(select(Job).where(
            Job.debug_key.in_(keys), Job.status != "deleted",
            or_(Job.dry_run.is_(None), Job.dry_run.is_(False))))).scalars().all()
    kept_job = next((j for j in group_jobs if j.debug_key == keep_key), None)
    if not kept_job:
        return _redirect("/duplicates", "keep_not_found", name=keep_key)

    res = await _resolve_duplicate_group(kept_job, group_jobs, source="duplicate review", user_kept=True)
    await log_info("duplicates", f"Review: Group {group_key}, kept: {keep_key}",
                   "abgelehnt: " + res.problems[0]["text"] if res.refused else "")
    return _resolution_redirect(res, kept_job.filename)


async def _claim_single(job, action: str) -> dict | None:
    """Claim one duplicate (duplicate → resolving) with a plan; None if it changed meanwhile."""
    plan = {"id": uuid.uuid4().hex, "kept": job.id, "members": [job.id], "source": action,
            "action": "single", "since": datetime.now().isoformat(timespec="seconds"),
            "prev": {str(job.id): "duplicate"}}
    async with async_session() as session:
        res = await session.execute(update(Job).where(Job.id == job.id, Job.status == "duplicate")
                                    .values(status=RESOLVING).execution_options(synchronize_session=False))
        if res.rowcount != 1:
            await session.rollback()
            return None
        row = await session.get(Job, job.id)
        row.step_result = dict(row.step_result or {}, **{PLAN_KEY: plan})
        flag_modified(row, "step_result")
        await session.commit()
    _ACTIVE.add(job.id)
    return plan


@router.post("/api/duplicates/not-duplicate")
async def not_duplicate(request: Request):
    """Take a file out of its group and process it like any other file."""
    form = await request.form()
    debug_key = form.get("debug_key")

    if not debug_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        job = (await session.execute(select(Job).where(Job.debug_key == debug_key))).scalars().first()
    if not job or job.status != "duplicate":
        return _redirect("/duplicates", "not_dup_status", name=debug_key,
                         detail=job.status if job else "—")
    if job.dry_run:
        return _redirect("/duplicates", "keep_dry_run", name=job.filename, detail=debug_key)
    plan = await _claim_single(job, "not-duplicate")
    if plan is None:
        return _redirect("/duplicates", "keep_busy", name=job.filename, detail=debug_key)

    old_ia02 = (job.step_result or {}).get("IA-02") or {}
    skip_result = {"status": "skipped", "reason": "manually marked as not a duplicate", "user_kept": True}
    if isinstance(old_ia02, dict) and old_ia02.get("folder_tags"):
        skip_result["folder_tags"] = old_ia02["folder_tags"]
    try:
        await log_info("duplicates", f"{debug_key} Kein Duplikat: wird neu verarbeitet",
                       f"bisher Duplikat von {old_ia02.get('original_debug_key') if isinstance(old_ia02, dict) else '?'}")
        async with async_session() as session:
            row = await session.get(Job, job.id)
            # The file comes along through fileops; nothing may be flushed here yet.
            with session.no_autoflush:
                ok = await prepare_job_for_reprocess(
                    session, row, keep_steps={"IA-01"}, inject_steps={"IA-02": skip_result},
                    move_file=True, commit=False, actor="user:not-duplicate")
            if not ok:
                # No copy of the file anywhere: it stays a visible duplicate (D7).
                row.status = "duplicate"
                row.target_path = job.target_path
                row.step_result = _without_plan(job.step_result)
                flag_modified(row, "step_result")
                row.error_message = "Kein Duplikat: Datei nicht auffindbar — nichts geändert"
            await session.commit()
    finally:
        _ACTIVE.discard(job.id)

    if not ok:
        await log_warning("duplicates", f"{debug_key} Kein Duplikat: Datei nicht auffindbar", "bleibt Duplikat")
        return _redirect("/duplicates", "not_dup_missing", name=job.filename)
    await log_info("duplicates", f"Not a duplicate: {debug_key}", "eingereiht")
    return RedirectResponse(url="/duplicates", status_code=303)


@router.post("/api/duplicates/delete-all")
async def delete_duplicate(request: Request):
    """Move a single duplicate's own file to the trash (API only, no button)."""
    form = await request.form()
    debug_key = form.get("debug_key")

    if not debug_key:
        return RedirectResponse(url="/duplicates", status_code=303)

    async with async_session() as session:
        job = (await session.execute(
            select(Job).where(Job.debug_key == debug_key, Job.status == "duplicate"))).scalars().first()
    if not job:
        return RedirectResponse(url="/duplicates", status_code=303)
    if job.dry_run:
        return _redirect("/duplicates", "keep_dry_run", name=job.filename, detail=debug_key)
    plan = await _claim_single(job, "delete")
    if plan is None:
        return _redirect("/duplicates", "keep_busy", name=job.filename, detail=debug_key)

    out = {"ok": True, "notes": [], "trashed": 0}
    try:
        await log_info("duplicates", f"{debug_key} Duplikat wird gelöscht (Papierkorb)", f"Datei {job.target_path}")
        rows, notes = await job_copies(job, check=False)
        out["notes"] += notes
        for row in rows:
            if row.kind == "immich" or row.job_id != job.id:
                continue  # the Immich asset is not part of this action
            try:
                await fileops.trash(job, row, actor="user:delete-duplicate", reason="Duplikat gelöscht",
                                    allow_last_copy=True)
                out["trashed"] += 1
                await _trash_companions(job, row.location, actor="user:delete-duplicate",
                                        reason="Duplikat gelöscht", out=out)
            except fileops.FileOpError as exc:
                out["ok"] = False
                out["notes"].append(f"{os.path.basename(row.location)}: {exc}")
        async with async_session() as session:
            row = await session.get(Job, job.id)
            _drop_plan(row)
            if out["ok"]:
                row.status = "deleted"
                row.error_message = "Duplicate deleted (manually)"
                row.target_path = None
                row.completed_at = datetime.now()
            else:
                row.status = "duplicate"
            await session.commit()
    finally:
        _ACTIVE.discard(job.id)

    log = log_info if out["ok"] else log_warning
    await log("duplicates", f"Duplicate deleted: {debug_key}" if out["ok"] else f"{debug_key} Duplikat nicht gelöscht",
              f"{out['trashed']} Exemplar(e) im Papierkorb" + (f"; {'; '.join(out['notes'])}" if out["notes"] else ""))
    if not out["ok"]:
        return _redirect("/duplicates", "delete_failed", name=job.filename, detail="; ".join(out["notes"]))
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
_batch_progress: dict = {"running": False, "kept": 0, "deleted": 0, "errors": 0, "skipped": 0,
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

    _batch_progress = {"running": True, "kept": 0, "deleted": 0, "errors": 0, "skipped": 0,
                       "total": 0, "current": 0, "done": False, "page": page}

    asyncio.create_task(_run_batch_clean(page))

    return JSONResponse({"status": "started", "page": page})


async def _run_batch_clean(page: int):
    """Background task: resolve the safe groups of a page (or all), one group at a time.

    The kept job is the one with the best quality score; a group whose best
    copy cannot be verified right now is skipped, nothing of it deleted
    (R-04). Each group is claimed, resolved and committed on its own.
    """
    logger.info("Batch-clean background task started for page=%s", page)
    from pipeline.step_ia02_duplicates import _quality_score

    per_page = 10
    skipped_reasons: list[str] = []
    _batch_progress.setdefault("skipped", 0)

    try:
        await resume_interrupted_resolutions()
        group_index, jobs_by_key = await _build_group_index()

        if page == 0:
            page_groups = group_index
        else:
            start = (page - 1) * per_page
            page_groups = group_index[start:start + per_page]

        safe_groups = [g for g in page_groups if g.get("safe_for_batch", g.get("all_exact"))]
        _batch_progress["total"] = len(safe_groups)

        for idx, g in enumerate(safe_groups, start=1):
            ids = [jobs_by_key[k].id for k in g["member_keys"] if k in jobs_by_key]
            async with async_session() as session:
                member_jobs = (await session.execute(select(Job).where(Job.id.in_(ids)))).scalars().all()
            if len(member_jobs) >= 2:
                best = max(member_jobs, key=_quality_score)
                try:
                    res = await _resolve_duplicate_group(best, member_jobs, source="batch-clean")
                except Exception as exc:
                    logger.error("Batch-clean error for group %s: %s", g.get("original_key"), exc)
                    res = Resolution([], 0, 0, [_problem("failed", str(exc), fatal=True)])
                if res.refused:
                    _batch_progress["skipped"] += 1
                    if len(skipped_reasons) < 20:
                        skipped_reasons.append(f"{best.debug_key}: {res.problems[0]['text']}")
                else:
                    _batch_progress["kept"] += 1
                    _batch_progress["deleted"] += res.deleted
                    _batch_progress["errors"] += res.errors
            _batch_progress["current"] = idx

        page_label = "all" if page == 0 else f"page {page}"
        summary = (f"Batch-Clean Quality ({page_label}): "
                   f"{_batch_progress['kept']} groups, "
                   f"{_batch_progress['deleted']} deleted, "
                   f"{_batch_progress['skipped']} skipped, "
                   f"{_batch_progress['errors']} errors")
        log = log_warning if _batch_progress["skipped"] or _batch_progress["errors"] else log_info
        await log("duplicates", summary, "; ".join(skipped_reasons))

    except Exception as exc:
        logger.error("Batch-clean crashed: %s\n%s", exc, traceback.format_exc())
        await log_error("duplicates", f"Batch-Clean abgestürzt: {exc}", traceback.format_exc()[-2000:])
    finally:
        _batch_progress["done"] = True
        _batch_progress["running"] = False
        logger.info("Batch-clean background task finished: %s", _batch_progress)

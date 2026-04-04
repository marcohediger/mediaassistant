import asyncio
import hashlib
import os
import subprocess

from config import config_manager
from database import async_session


async def _is_folder_tags_active(job) -> bool:
    """Check if folder tags should be applied — re-reads module AND inbox setting at runtime."""
    if not await config_manager.is_module_enabled("ordner_tags"):
        return False
    if not job.source_inbox_path:
        return False
    # Re-read current inbox setting from DB (not the stale job.folder_tags)
    from models import InboxDirectory
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(InboxDirectory.folder_tags).where(InboxDirectory.path == job.source_inbox_path)
        )
        inbox_folder_tags = result.scalar()
    return bool(inbox_folder_tags)


WRITABLE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp",
    ".heic", ".heif", ".dng",
    ".mp4", ".mov",
}

# Formats that support IPTC Keywords natively
_IPTC_FORMATS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".dng"}
# Formats that only support XMP (no IPTC) — use Subject (dc:subject)
_XMP_ONLY_FORMATS = {".heic", ".heif", ".webp", ".mp4", ".mov"}
# Formats that don't support XPComment
_NO_XPCOMMENT = {".mp4", ".mov"}


async def execute(job, session) -> dict:
    """IA-07: EXIF-Tags (Keywords, Description) zurück in die Datei schreiben."""
    ext = os.path.splitext(job.original_path)[1].lower()
    if ext not in WRITABLE_EXTENSIONS:
        return {"status": "skipped", "reason": f"format {ext} not supported for in-place tag writing"}

    # Detect format/extension mismatch (e.g. JPG named as .png)
    step_results = job.step_result or {}
    exif_data = step_results.get("IA-01", {})
    actual_type = (exif_data.get("file_type") or "").upper()
    _EXT_TO_TYPES = {
        ".jpg": {"JPEG"}, ".jpeg": {"JPEG"},
        ".png": {"PNG"}, ".webp": {"WEBP"},
        ".tiff": {"TIFF"}, ".tif": {"TIFF"},
        ".heic": {"HEIC"}, ".heif": {"HEIF"},
        ".dng": {"DNG"},
        ".mp4": {"MP4"}, ".mov": {"MOV", "MP4"},
    }
    expected_types = _EXT_TO_TYPES.get(ext, set())
    if actual_type and expected_types and actual_type not in expected_types:
        return {
            "status": "skipped",
            "reason": f"format mismatch: file is {actual_type} but extension is {ext} — ExifTool cannot write safely",
        }

    ai_result = step_results.get("IA-05", {})
    ocr_result = step_results.get("IA-06", {})
    geo_result = step_results.get("IA-03", {})

    # Collect keywords
    keywords = []

    # From folder structure (re-check module + inbox setting at runtime)
    folder_tags_active = await _is_folder_tags_active(job)
    if folder_tags_active and job.source_inbox_path:
        rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
        if rel and rel != ".":
            folder_parts = [p for p in rel.split(os.sep) if p and p != "."]
            # Split all folder names into individual words as tags
            for part in folder_parts:
                for word in part.split():
                    if word and word not in keywords:
                        keywords.append(word)
            # Add combined tag from all folder parts (e.g. "Ferien Spanien 2024")
            combined = " ".join(folder_parts)
            if combined not in keywords:
                keywords.append(combined)

    # From AI analysis (type + tags + source)
    if ai_result.get("type"):
        keywords.append(ai_result["type"])
    if ai_result.get("tags"):
        keywords.extend(ai_result["tags"])
    if ai_result.get("source"):
        keywords.append(ai_result["source"])
    if ai_result.get("quality") == "blurry":
        keywords.append("blurry")

    # From geocoding (all available fields)
    for geo_field in ("country", "state", "city", "suburb"):
        val = geo_result.get(geo_field)
        if val and val not in keywords:
            keywords.append(val)

    # From OCR — just flag that text was detected (actual text is in UserComment)
    if ocr_result.get("has_text"):
        keywords.append("OCR")

    # Build description
    description_parts = []
    if ai_result.get("description"):
        description_parts.append(ai_result["description"])
    # Google Takeout JSON description as fallback (only if no AI description)
    if not ai_result.get("description") and exif_data.get("google_json_description"):
        description_parts.append(exif_data["google_json_description"])
    if geo_result.get("city") and geo_result.get("country"):
        location = geo_result["city"]
        if geo_result.get("suburb"):
            location = f"{geo_result['suburb']}, {location}"
        description_parts.append(f"Aufgenommen in {location}, {geo_result['country']}.")

    description = " ".join(description_parts)

    if not keywords and not description:
        return {"status": "skipped", "reason": "no tags to write"}

    write_mode = await config_manager.get("metadata.write_mode", "direct")

    # Dry-run: report what would be written, but don't modify file
    if job.dry_run:
        return {
            "status": "dry_run",
            "keywords_planned": keywords,
            "description_planned": description,
            "tags_count": len(keywords),
            "write_mode": write_mode,
        }

    # Collect OCR text early (used by both modes)
    ocr_text = ""
    if ocr_result.get("has_text") and ocr_result.get("text"):
        ocr_text = ocr_result["text"].strip()

    if write_mode == "sidecar":
        return await _write_sidecar(job, keywords, description, ocr_text, ext)

    return await _write_direct(job, keywords, description, ocr_text, ext)


async def _write_direct(job, keywords, description, ocr_text, ext):
    """Write metadata directly into the file (original behavior)."""
    cmd = ["exiftool", "-overwrite_original_in_place", "-P", "-m"]

    # Write keywords — format-aware tag field selection
    if ext in _IPTC_FORMATS:
        for kw in keywords:
            cmd.append(f"-Keywords+={kw}")
    else:
        for kw in keywords:
            cmd.append(f"-Subject+={kw}")

    if description:
        cmd.append(f"-ImageDescription={description}")
        if ext not in _NO_XPCOMMENT:
            cmd.append(f"-XPComment={description}")

    if ocr_text:
        cmd.append(f"-UserComment=OCR: {ocr_text}")

    cmd.append(job.original_path)

    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        raise RuntimeError(f"ExifTool Write Fehler: {result.stderr.strip()}")

    new_hash = await asyncio.to_thread(_sha256, job.original_path)
    new_size = os.path.getsize(job.original_path)

    return {
        "keywords_written": keywords,
        "description_written": description,
        "ocr_text_written": ocr_text,
        "tags_count": len(keywords),
        "file_size": new_size,
        "file_hash": new_hash,
        "write_mode": "direct",
    }


async def _write_sidecar(job, keywords, description, ocr_text, ext):
    """Write metadata to an XMP sidecar file, leaving the original untouched."""
    sidecar_path = job.original_path + ".xmp"

    # ExifTool -o file.xmp creates an XMP sidecar from the source file
    cmd = ["exiftool", "-o", sidecar_path, "-P", "-m"]

    # Always use XMP Subject for sidecar files (universal XMP format)
    for kw in keywords:
        cmd.append(f"-Subject+={kw}")

    if description:
        cmd.append(f"-ImageDescription={description}")
        cmd.append(f"-XPComment={description}")

    if ocr_text:
        cmd.append(f"-UserComment=OCR: {ocr_text}")

    cmd.append(job.original_path)

    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        raise RuntimeError(f"ExifTool Sidecar Fehler: {result.stderr.strip()}")

    return {
        "keywords_written": keywords,
        "description_written": description,
        "ocr_text_written": ocr_text,
        "tags_count": len(keywords),
        "sidecar_path": sidecar_path,
        "write_mode": "sidecar",
    }


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

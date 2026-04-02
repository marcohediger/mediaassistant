import asyncio
import hashlib
import os
import subprocess


WRITABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp", ".heic", ".heif", ".dng"}

# Formats that support IPTC Keywords natively
_IPTC_FORMATS = {".jpg", ".jpeg", ".tiff", ".tif", ".dng"}
# Formats that only support XMP (no IPTC) — use Subject (dc:subject)
_XMP_ONLY_FORMATS = {".heic", ".heif", ".png", ".webp"}


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

    # From folder structure (if inbox has folder_tags enabled)
    if job.folder_tags and job.source_inbox_path:
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
    if geo_result.get("city") and geo_result.get("country"):
        location = geo_result["city"]
        if geo_result.get("suburb"):
            location = f"{geo_result['suburb']}, {location}"
        description_parts.append(f"Aufgenommen in {location}, {geo_result['country']}.")

    description = " ".join(description_parts)

    if not keywords and not description:
        return {"status": "skipped", "reason": "no tags to write"}

    # Dry-run: report what would be written, but don't modify file
    if job.dry_run:
        return {
            "status": "dry_run",
            "keywords_planned": keywords,
            "description_planned": description,
            "tags_count": len(keywords),
        }

    # Build ExifTool command
    cmd = ["exiftool", "-overwrite_original_in_place", "-P", "-m"]

    # Write keywords — format-aware tag field selection
    if ext in _IPTC_FORMATS:
        # IPTC Keywords: supported by JPEG, TIFF, DNG — read by Immich, Lightroom, digiKam
        for kw in keywords:
            cmd.append(f"-Keywords+={kw}")
    else:
        # XMP Subject (dc:subject): for HEIC, PNG, WebP — these formats don't support IPTC
        for kw in keywords:
            cmd.append(f"-Subject+={kw}")

    # Write description
    if description:
        cmd.append(f"-ImageDescription={description}")
        cmd.append(f"-XPComment={description}")

    # Write OCR text
    ocr_text = ""
    if ocr_result.get("has_text") and ocr_result.get("text"):
        ocr_text = ocr_result["text"].strip()
        cmd.append(f"-UserComment=OCR: {ocr_text}")

    cmd.append(job.original_path)

    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        raise RuntimeError(f"ExifTool Write Fehler: {result.stderr.strip()}")

    # Compute new hash for result (don't overwrite job.file_hash — it must
    # stay as the original hash so IA-02 duplicate detection works correctly)
    new_hash = await asyncio.to_thread(_sha256, job.original_path)
    new_size = os.path.getsize(job.original_path)

    return {
        "keywords_written": keywords,
        "description_written": description,
        "ocr_text_written": ocr_text,
        "tags_count": len(keywords),
        "file_size": new_size,
        "file_hash": new_hash,
    }


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

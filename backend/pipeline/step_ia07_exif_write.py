import asyncio
import hashlib
import os
import subprocess


async def execute(job, session) -> dict:
    """IA-07: EXIF-Tags (Keywords, Description) zurück in die Datei schreiben."""
    step_results = job.step_result or {}
    ai_result = step_results.get("IA-04", {})
    ocr_result = step_results.get("IA-05", {})
    geo_result = step_results.get("IA-06", {})

    # Collect keywords
    keywords = []

    # From folder structure (if inbox has folder_tags enabled)
    if job.source_inbox_path:
        rel = os.path.relpath(os.path.dirname(job.original_path), job.source_inbox_path)
        if rel and rel != ".":
            folder_parts = [p for p in rel.split(os.sep) if p and p != "."]
            keywords.extend(folder_parts)

    # From AI analysis
    if ai_result.get("tags"):
        keywords.extend(ai_result["tags"])
    if ai_result.get("type") and ai_result["type"] != "unknown":
        keywords.append(ai_result["type"])
    if ai_result.get("mood"):
        keywords.append(ai_result["mood"])
    if ai_result.get("quality"):
        keywords.append(f"quality:{ai_result['quality']}")

    # From geocoding (all available fields)
    for geo_field in ("country", "state", "city", "suburb"):
        val = geo_result.get(geo_field)
        if val and val not in keywords:
            keywords.append(val)

    # From OCR
    if ocr_result.get("has_text") and ocr_result.get("text_type"):
        keywords.append(f"text:{ocr_result['text_type']}")

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

    # Build ExifTool command
    cmd = ["exiftool", "-overwrite_original"]

    # Write keywords
    for kw in keywords:
        cmd.append(f"-Keywords+={kw}")
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
    # stay as the original hash so IA-03 duplicate detection works correctly)
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

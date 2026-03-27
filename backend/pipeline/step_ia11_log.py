from system_logger import log_info


async def execute(job, session) -> dict:
    """IA-11: SQLite log entry — log processing summary."""
    step_results = job.step_result or {}
    parts = []

    # Category from AI analysis
    ai_result = step_results.get("IA-04", {})
    if ai_result.get("type"):
        parts.append(ai_result["type"])

    # Number of written tags
    tags_result = step_results.get("IA-07", {})
    if tags_result.get("tags_count"):
        parts.append(f"{tags_result['tags_count']} Tags")

    # OCR
    ocr_result = step_results.get("IA-05", {})
    if ocr_result.get("has_text"):
        parts.append("OCR")

    # Geocoding
    geo_result = step_results.get("IA-06", {})
    if geo_result.get("city") and geo_result.get("country"):
        parts.append(f"{geo_result['city']}/{geo_result['country']}")

    # Duplicate
    dup_result = step_results.get("IA-03", {})
    if dup_result.get("duplicate"):
        parts.append("Duplicate")

    # Target folder
    sort_result = step_results.get("IA-08", {})
    target = sort_result.get("target_path", "")

    summary = ", ".join(parts) if parts else "processed"
    if target:
        summary += f" -> {target}"

    await log_info("pipeline", f"{job.debug_key} {summary}", job.filename)

    return {"logged": True, "summary": summary}

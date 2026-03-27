from system_logger import log_info


async def execute(job, session) -> dict:
    """IA-11: SQLite Log-Eintrag — Zusammenfassung der Verarbeitung loggen."""
    step_results = job.step_result or {}
    parts = []

    # Kategorie aus KI-Analyse
    ai_result = step_results.get("IA-03", {})
    if ai_result.get("type"):
        parts.append(ai_result["type"])

    # Anzahl geschriebene Tags
    tags_result = step_results.get("IA-07", {})
    if tags_result.get("tags_count"):
        parts.append(f"{tags_result['tags_count']} Tags")

    # OCR
    ocr_result = step_results.get("IA-04", {})
    if ocr_result.get("has_text"):
        parts.append("OCR")

    # Geocoding
    geo_result = step_results.get("IA-06", {})
    if geo_result.get("city") and geo_result.get("country"):
        parts.append(f"{geo_result['city']}/{geo_result['country']}")

    # Duplikat
    dup_result = step_results.get("IA-05", {})
    if dup_result.get("duplicate"):
        parts.append("Duplikat")

    # Zielordner
    sort_result = step_results.get("IA-08", {})
    target = sort_result.get("target_path", "")

    summary = ", ".join(parts) if parts else "verarbeitet"
    if target:
        summary += f" -> {target}"

    await log_info("pipeline", f"{job.debug_key} {summary}", job.filename)

    return {"logged": True, "summary": summary}

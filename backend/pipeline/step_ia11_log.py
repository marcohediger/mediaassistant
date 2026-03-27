from system_logger import log_info


async def execute(job, session) -> dict:
    """IA-11: SQLite Log-Eintrag — Zusammenfassung der Verarbeitung loggen."""
    step_results = job.step_result or {}

    # Collect summary parts
    parts = []

    ai_result = step_results.get("IA-03", {})
    if ai_result.get("type"):
        parts.append(ai_result["type"])

    tags_result = step_results.get("IA-07", {})
    if tags_result.get("tags_count"):
        parts.append(f"{tags_result['tags_count']} Tags")

    geo_result = step_results.get("IA-06", {})
    if geo_result.get("city") and geo_result.get("country"):
        parts.append(f"{geo_result['city']}/{geo_result['country']}")

    sort_result = step_results.get("IA-08", {})
    target = sort_result.get("target_path", "")

    summary = ", ".join(parts) if parts else "verarbeitet"
    if target:
        summary += f" -> {target}"

    await log_info("pipeline", f"{job.debug_key} {summary}", job.filename)

    return {"logged": True, "summary": summary}

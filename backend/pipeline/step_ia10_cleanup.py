import os


async def execute(job, session) -> dict:
    """IA-10: Aufräumen — temporäre Dateien entfernen."""
    step_results = job.step_result or {}
    removed = []

    # Remove temp JPEG from IA-02 (Formatkonvertierung)
    convert_result = step_results.get("IA-02", {})
    temp_path = convert_result.get("temp_path")
    if temp_path and os.path.exists(temp_path):
        os.remove(temp_path)
        removed.append(temp_path)

    return {"removed": removed, "count": len(removed)}

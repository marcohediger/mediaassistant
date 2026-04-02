import os


async def execute(job, session) -> dict:
    """IA-10: Aufräumen — temporäre Dateien entfernen."""
    step_results = job.step_result or {}
    removed = []

    # Remove temp JPEG(s) from IA-04 (Temp. Konvertierung für KI)
    convert_result = step_results.get("IA-04", {})
    temp_paths = convert_result.get("temp_paths") or []
    if not temp_paths:
        single = convert_result.get("temp_path")
        if single:
            temp_paths = [single]
    for temp_path in temp_paths:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            removed.append(temp_path)

    # Remove XMP sidecar file from IA-07 (if sidecar mode was used and file still exists)
    ia07_result = step_results.get("IA-07", {})
    sidecar_path = ia07_result.get("sidecar_path")
    if sidecar_path and os.path.exists(sidecar_path):
        os.remove(sidecar_path)
        removed.append(sidecar_path)

    # Remove downloaded file and temp dir from Immich webhook
    if job.immich_asset_id and job.original_path and os.path.exists(job.original_path):
        os.remove(job.original_path)
        removed.append(job.original_path)
        # Remove temp directory if empty
        parent = os.path.dirname(job.original_path)
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
            removed.append(parent)

    return {"removed": removed, "count": len(removed)}

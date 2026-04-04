import logging
import os

logger = logging.getLogger("mediaassistant.pipeline.ia10")


async def execute(job, session) -> dict:
    """IA-10: Aufräumen — temporäre Dateien entfernen."""
    step_results = job.step_result or {}
    removed = []
    failed = []

    def _safe_remove(path: str, label: str):
        """Remove a file with error handling — never crash the pipeline."""
        try:
            if path and os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except (OSError, PermissionError) as e:
            logger.warning(f"Failed to remove {label}: {path} — {e}")
            failed.append(path)

    # Remove temp JPEG(s) from IA-04 (Temp. Konvertierung für KI)
    convert_result = step_results.get("IA-04", {})
    temp_paths = convert_result.get("temp_paths") or []
    if not temp_paths:
        single = convert_result.get("temp_path")
        if single:
            temp_paths = [single]
    for temp_path in temp_paths:
        _safe_remove(temp_path, "IA-04 temp")

    # Remove XMP sidecar file from IA-07 (if sidecar mode was used and file still exists)
    ia07_result = step_results.get("IA-07", {})
    sidecar_path = ia07_result.get("sidecar_path")
    _safe_remove(sidecar_path, "IA-07 sidecar")

    # Remove Google Takeout JSON sidecar (if used and file was moved/processed)
    ia01_result = step_results.get("IA-01", {})
    google_json_path = ia01_result.get("google_json_path")
    _safe_remove(google_json_path, "Google JSON")

    # Remove downloaded file and temp dir from Immich webhook
    if job.immich_asset_id and job.original_path:
        _safe_remove(job.original_path, "Immich download")
        # Remove temp directory if empty
        parent = os.path.dirname(job.original_path)
        if parent and os.path.isdir(parent):
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    removed.append(parent)
            except (OSError, PermissionError) as e:
                logger.warning(f"Failed to remove Immich temp dir: {parent} — {e}")

    result = {"removed": removed, "count": len(removed)}
    if failed:
        result["failed"] = failed
    return result

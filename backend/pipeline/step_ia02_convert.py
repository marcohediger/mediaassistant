import asyncio
import os
import subprocess

TEMP_DIR = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), "tmp")


async def execute(job, session) -> dict:
    """IA-02: Formatkonvertierung — HEIC/DNG/RAW in temp JPEG für KI-Analyse."""
    filepath = job.original_path
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return {"converted": False, "reason": "format natively supported"}

    await asyncio.to_thread(os.makedirs, TEMP_DIR, exist_ok=True)
    temp_path = os.path.join(TEMP_DIR, f"{job.debug_key}.jpg")

    try:
        if ext in (".heic", ".heif"):
            await asyncio.to_thread(
                subprocess.run,
                ["heif-convert", filepath, temp_path],
                capture_output=True, timeout=30, check=True
            )
        elif ext in (".dng", ".cr2", ".nef", ".arw", ".tiff", ".tif"):
            # exiftool -w writes next to source, so extract to stdout and redirect
            result = await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-b", "-PreviewImage", filepath],
                capture_output=True, timeout=30
            )
            if result.stdout:
                await asyncio.to_thread(_write_bytes, temp_path, result.stdout)
        elif ext == ".gif":
            await asyncio.to_thread(
                subprocess.run,
                ["convert", f"{filepath}[0]", temp_path],
                capture_output=True, timeout=30, check=True
            )
        else:
            return {"converted": False, "reason": f"no conversion for {ext}"}

        if os.path.exists(temp_path):
            return {"converted": True, "temp_path": temp_path}
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise RuntimeError(f"Formatkonvertierung fehlgeschlagen: {e}")

    return {"converted": False, "reason": "conversion produced no output"}


def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)

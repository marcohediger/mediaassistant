import asyncio
import base64
import json
import os
import subprocess
import httpx
from config import config_manager


SYSTEM_PROMPT = """Du bist ein Bildanalyse-Assistent. Analysiere das Bild und antworte ausschliesslich mit validem JSON (kein Markdown, kein Text drumherum).

Analysiere folgende Aspekte:
{
  "type": "personal_photo|whatsapp|screenshot|internet_image|document|meme",
  "tags": ["tag1", "tag2", ...],
  "description": "Kurze Beschreibung in 1-2 Sätzen",
  "mood": "indoor|outdoor|nacht|gegenlicht|studio",
  "people_count": 0,
  "quality": "unscharf|durchschnitt|gut|sehr_gut",
  "confidence": 0.0-1.0
}

Regeln:
- type: Wähle den passendsten Typ
- tags: 3-8 relevante Tags auf Deutsch (z.B. Landschaft, Essen, Tier, Selfie, Gruppe, Stadt, Natur, Sport, Feier)
- description: Deutsch, sachlich, 1-2 Sätze
- people_count: Anzahl sichtbarer Personen (0 wenn keine)
- quality: Technische Bildqualität bewerten
- confidence: Wie sicher bist du bei der Typ-Klassifizierung (0.0-1.0)"""


async def _convert_to_jpeg(filepath: str) -> str | None:
    """Convert HEIC/DNG to temp JPEG for AI analysis."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return None  # No conversion needed

    temp_path = filepath + ".tmp.jpg"
    try:
        if ext in (".heic", ".heif"):
            await asyncio.to_thread(
                subprocess.run,
                ["heif-convert", filepath, temp_path],
                capture_output=True, timeout=30, check=True
            )
        elif ext in (".dng", ".cr2", ".nef", ".arw", ".tiff", ".tif"):
            await asyncio.to_thread(
                subprocess.run,
                ["exiftool", "-b", "-PreviewImage", "-w", ".tmp.jpg", filepath],
                capture_output=True, timeout=30
            )
            # exiftool writes to filename.tmp.jpg
            expected = os.path.splitext(filepath)[0] + ".tmp.jpg"
            if os.path.exists(expected) and expected != temp_path:
                os.rename(expected, temp_path)
        else:
            return None

        if os.path.exists(temp_path):
            return temp_path
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return None


async def execute(job, session) -> dict:
    """IA-02: KI-Analyse via OpenAI-kompatiblem Endpunkt."""
    if not await config_manager.is_module_enabled("ki_analyse"):
        return {"status": "skipped", "reason": "module disabled"}

    url = await config_manager.get("ai.backend_url")
    model = await config_manager.get("ai.model")
    if not url or not model:
        return {"status": "skipped", "reason": "not configured"}

    api_key = await config_manager.get("ai.api_key", "not-needed")

    # Read and encode image
    filepath = job.original_path
    temp_file = await _convert_to_jpeg(filepath)
    image_path = temp_file or filepath

    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime_type = mime_map.get(ext, "image/jpeg")

    # Build EXIF context
    exif = (job.step_result or {}).get("IA-01", {})
    exif_context = ""
    if exif.get("has_exif"):
        parts = []
        if exif.get("make"):
            parts.append(f"Kamera: {exif['make']} {exif.get('model', '')}")
        if exif.get("date"):
            parts.append(f"Datum: {exif['date']}")
        if exif.get("gps"):
            parts.append(f"GPS: {exif['gps_lat']}, {exif['gps_lon']}")
        if parts:
            exif_context = "\n\nEXIF-Daten: " + ", ".join(parts)

    # Call AI API
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Analysiere dieses Bild.{exif_context}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}}
            ]}
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }

    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"KI-API Fehler: HTTP {resp.status_code} — {resp.text[:200]}")

    response_data = resp.json()
    content = response_data["choices"][0]["message"]["content"]

    # Parse JSON from response (handle markdown code blocks)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        content = content.rsplit("```", 1)[0]
    content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"raw_response": content, "parse_error": True}

    return result

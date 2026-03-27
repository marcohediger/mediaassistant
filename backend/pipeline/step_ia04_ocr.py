import base64
import json
import os
import httpx
from config import config_manager


OCR_PROMPT = """Untersuche dieses Bild auf sichtbaren Text. Antworte ausschliesslich mit validem JSON (kein Markdown).

{
  "has_text": true/false,
  "text": "Der gesamte erkannte Text",
  "text_type": "schild|dokument|screenshot|whiteboard|handschrift|sonstiges|keiner",
  "language": "de|en|fr|it|..."
}

Regeln:
- Wenn kein Text sichtbar ist: has_text=false, text="", text_type="keiner"
- Erfasse ALLEN sichtbaren Text (Schilder, Beschriftungen, Bildschirminhalte, Dokumente)
- Behalte die originale Formatierung bei wo sinnvoll"""


async def execute(job, session) -> dict:
    """IA-04: OCR — Texterkennung via KI."""
    if not await config_manager.is_module_enabled("ocr"):
        return {"status": "skipped", "reason": "module disabled"}

    url = await config_manager.get("ai.backend_url")
    model = await config_manager.get("ai.model")
    if not url or not model:
        return {"status": "skipped", "reason": "not configured"}

    # Skip if AI analysis already found no text-relevant content
    ai_result = (job.step_result or {}).get("IA-03", {})
    ai_type = ai_result.get("type", "")
    # Only run OCR for screenshots, documents, or if AI detected text-like content
    if ai_type not in ("screenshot", "document", "") and not ai_result.get("parse_error"):
        return {"status": "skipped", "reason": f"type={ai_type}, OCR nicht nötig"}

    api_key = await config_manager.get("ai.api_key", "not-needed")

    # Use pre-converted temp file from IA-02 if available
    filepath = job.original_path
    convert_result = (job.step_result or {}).get("IA-02", {})
    image_path = convert_result.get("temp_path") or filepath

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime_type = mime_map.get(os.path.splitext(image_path)[1].lower(), "image/jpeg")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": OCR_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Erkenne allen sichtbaren Text in diesem Bild."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}}
            ]}
        ],
        "temperature": 0.1,
        "max_tokens": 1000,
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
        raise RuntimeError(f"OCR API Fehler: HTTP {resp.status_code}")

    content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        content = content.rsplit("```", 1)[0]
    content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"has_text": False, "raw_response": content, "parse_error": True}

    return result

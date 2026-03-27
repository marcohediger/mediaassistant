import asyncio
import base64
import json
import os
import httpx
from config import config_manager


DEFAULT_SYSTEM_PROMPT = """Du bist ein Bildanalyse-Assistent. Analysiere das Bild und antworte ausschliesslich mit validem JSON (kein Markdown, kein Text drumherum).

Analysiere folgende Aspekte:
{
  "type": "personal|screenshot|internet_image|document|meme",
  "tags": ["tag1", "tag2", ...],
  "description": "Kurze Beschreibung in 1-2 Sätzen",
  "mood": "indoor|outdoor|nacht|gegenlicht|studio",
  "people_count": 0,
  "quality": "unscharf|durchschnitt|gut|sehr_gut",
  "confidence": 0.0-1.0
}

Regeln:
- type: Wähle den passendsten Typ
  - "personal": Echte Fotos von Personen, Landschaften, Tieren, Essen, Events, Reisen — alles was mit einer Kamera oder Handy aufgenommen wurde
  - "screenshot": NUR echte Bildschirmfotos (Statusleiste, App-UI, Browser sichtbar). NICHT verwechseln mit: abfotografierten Bildschirmen, Fotos mit Text/Schildern, Grafiken oder Memes
  - "internet_image": Heruntergeladene Bilder, Stockfotos, Grafiken ohne persönlichen Bezug
  - "document": Gescannte Dokumente, Quittungen, Briefe
  - "meme": Internet-Memes, Witze, Social-Media-Bilder mit Text-Overlay
- tags: 3-8 relevante Tags auf Deutsch (z.B. Landschaft, Essen, Tier, Selfie, Gruppe, Stadt, Natur, Sport, Feier)
- description: Deutsch, sachlich, 1-2 Sätze
- people_count: Anzahl sichtbarer Personen (0 wenn keine)
- quality: Technische Bildqualität bewerten
- confidence: Wie sicher bist du bei der Typ-Klassifizierung (0.0-1.0)"""


async def execute(job, session) -> dict:
    """IA-04: KI-Analyse via OpenAI-kompatiblem Endpunkt."""
    if not await config_manager.is_module_enabled("ki_analyse"):
        return {"status": "skipped", "reason": "module disabled"}

    url = await config_manager.get("ai.backend_url")
    model = await config_manager.get("ai.model")
    if not url or not model:
        return {"status": "skipped", "reason": "not configured"}

    api_key = await config_manager.get("ai.api_key", "not-needed")
    system_prompt = await config_manager.get("ai.prompt", DEFAULT_SYSTEM_PROMPT)

    # Use pre-converted temp file from IA-02 if available
    filepath = job.original_path
    convert_result = (job.step_result or {}).get("IA-02", {})
    image_path = convert_result.get("temp_path") or filepath

    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

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
            {"role": "system", "content": system_prompt},
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

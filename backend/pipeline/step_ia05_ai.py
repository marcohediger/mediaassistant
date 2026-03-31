import asyncio
import base64
import json
import os
import httpx
from config import config_manager


DEFAULT_SYSTEM_PROMPT = """Du bist ein Bildanalyse-Assistent für eine Foto-Mediathek. Statische Regeln sortieren Dateien anhand von Dateinamen, Endungen und EXIF-Daten in Kategorien. Deine Aufgabe ist es, den Bildinhalt zu analysieren und eine eigene Klassifikation zu liefern, damit fehlerhafte Regelentscheidungen korrigiert werden können.

Die verfügbaren Kategorien und die Vorklassifikation der statischen Regel werden dir mit den Metadaten übergeben.

Antworte NUR mit validem JSON (kein Markdown, kein umgebender Text):
{
  "type": "<kategorie_key>",
  "source": "Quelle des Bildes",
  "tags": ["Tag1", "Tag2", ...],
  "description": "Kurze Beschreibung in 1-2 Sätzen",
  "mood": "indoor|outdoor|night|backlit|studio",
  "people_count": 0,
  "quality": "blurry|average|good|excellent",
  "confidence": 0.0-1.0
}

## 1) type — Kategorie (Ziel-Ablage)
Verwende einen der Kategorie-Keys aus der übergebenen Liste.
Prüfe ob die Vorklassifikation der statischen Regel plausibel ist. Korrigiere wenn nötig.
WICHTIG: Im Zweifel lieber persönliches Foto — besser ein Meme behalten als ein echtes Foto aussortieren.

## 2) source — Quelle / Herkunft
Beschreibt woher das Bild stammt. Grossbuchstabe am Anfang, menschenlesbar.
Beispiele: Kamerafoto, Selfie, Meme, Internet Bild, Werbung, Infografik, Dokument, Quittung, Screenshot App, Screenshot Web, Sticker, Comic, Weitergeleitetes Bild
Du kannst auch eigene passende Quellen definieren wenn keine der Beispiele passt.

## 3) tags — Allgemeine beschreibende Tags
3-8 Tags die den Bildinhalt beschreiben. Auf DEUTSCH, Grossbuchstabe am Anfang, menschenlesbar.
Beispiele: Landschaft, Essen, Tier, Hund, Katze, Gruppe, Stadt, Natur, Sport, Feier, Strand, Berge, Haus, Boot, Auto, Blumen, Sonnenuntergang, Familie, Kind

## Entscheidungshilfe für type:
- EXIF mit Kamera-Info oder GPS → stark persönlich
- Dateigrösse >500 KB ohne EXIF → wahrscheinlich persönlich via Messenger
- Dateigrösse <100 KB ohne EXIF → wahrscheinlich Meme/Internet Bild
- Text-Overlay, Meme-Templates → sourceless
- Wasserzeichen, Stock-Foto, Werbung → sourceless
- Echte Statusleiste/App-UI sichtbar → screenshot
- Natürlich/spontan → persönlich

## Weitere Felder:
- description: Auf DEUTSCH, sachlich, 1-2 Sätze
- people_count: Anzahl sichtbarer Personen (0 wenn keine)
- quality: Technische Bildqualität
- confidence: Wie sicher bei der type-Klassifikation (0.0-1.0)"""


async def execute(job, session) -> dict:
    """IA-05: KI-Analyse via OpenAI-kompatiblem Endpunkt.

    Nutzt alle bisher gesammelten Metadaten (EXIF, Geocoding, Dateigrösse)
    für eine bestmögliche Klassifikation.
    """
    if not await config_manager.is_module_enabled("ki_analyse"):
        return {"status": "skipped", "reason": "module disabled"}

    url = await config_manager.get("ai.backend_url")
    model = await config_manager.get("ai.model")
    if not url or not model:
        return {"status": "skipped", "reason": "not configured"}

    api_key = await config_manager.get("ai.api_key", "not-needed")
    system_prompt = await config_manager.get("ai.prompt", DEFAULT_SYSTEM_PROMPT)

    # Mindestgrösse prüfen — zu kleine Bilder erzeugen AI-Halluzinationen
    exif = (job.step_result or {}).get("IA-01", {})
    img_width = exif.get("width", 0) or 0
    img_height = exif.get("height", 0) or 0
    if img_width > 0 and img_height > 0 and img_width < 16 and img_height < 16:
        return {
            "type": "unknown",
            "tags": [],
            "description": f"Bild zu klein für Analyse ({img_width}x{img_height} px)",
            "mood": "",
            "people_count": 0,
            "quality": "unbekannt",
            "confidence": 0.0,
            "_skipped": True,
            "_reason": f"Mindestgrösse unterschritten ({img_width}x{img_height} px)",
        }

    # Use pre-converted temp file(s) from IA-04 if available
    filepath = job.original_path
    convert_result = (job.step_result or {}).get("IA-04", {})

    # Multi-frame support for videos
    image_paths = convert_result.get("temp_paths") or []
    if not image_paths:
        single = convert_result.get("temp_path") or filepath
        image_paths = [single]

    # Filter to existing files
    image_paths = [p for p in image_paths if os.path.exists(p)]
    if not image_paths:
        image_paths = [filepath]

    # Encode all images
    image_data_list = []
    for img_path in image_paths:
        with open(img_path, "rb") as f:
            image_data_list.append(base64.b64encode(f.read()).decode("utf-8"))

    mime_type = "image/jpeg"

    # ── Kategorien aus DB laden ────────────────────────────────────
    from models import LibraryCategory
    from sqlalchemy import select as sa_select
    cat_result = await session.execute(
        sa_select(LibraryCategory).where(LibraryCategory.key != "error", LibraryCategory.key != "duplicate").order_by(LibraryCategory.position)
    )
    categories = cat_result.scalars().all()
    cat_list = " | ".join(f"{c.key} ({c.label})" for c in categories)

    # ── Alle Metadaten sammeln ──────────────────────────────────────
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    geo = step_results.get("IA-03", {})
    filename = os.path.basename(job.original_path)
    file_size_kb = os.path.getsize(filepath) / 1024

    # Statische Regel vorab auswerten für Kontext
    from pipeline.step_ia08_sort import _match_sorting_rules
    rule_category = await _match_sorting_rules(filename, exif, session)
    rule_cat_label = ""
    if rule_category:
        for c in categories:
            if c.key == rule_category:
                rule_cat_label = c.label
                break

    context_parts = []

    # Verfügbare Kategorien
    context_parts.append(f"Verfügbare Kategorien: {cat_list}")

    # Vorklassifikation der statischen Regel
    if rule_category:
        context_parts.append(f"Vorklassifikation (statische Regel): {rule_category} ({rule_cat_label})")
    else:
        context_parts.append("Vorklassifikation (statische Regel): keine Regel hat gegriffen")

    # Dateiinfo (immer)
    context_parts.append(f"Dateiname: {filename}")
    context_parts.append(f"Dateigrösse: {file_size_kb:.0f} KB")

    # EXIF-Daten
    if exif.get("has_exif"):
        if exif.get("make"):
            context_parts.append(f"Kamera: {exif['make']} {exif.get('model', '')}")
        if exif.get("date"):
            context_parts.append(f"Aufnahmedatum: {exif['date']}")
        if exif.get("gps"):
            context_parts.append(f"GPS: {exif['gps_lat']}, {exif['gps_lon']}")
        if exif.get("width") and exif.get("height"):
            context_parts.append(f"Auflösung: {exif['width']}x{exif['height']}")
        if exif.get("software"):
            context_parts.append(f"Software: {exif['software']}")
    else:
        context_parts.append("Keine EXIF-Daten vorhanden (typisch für Messenger-Bilder)")

    # Video-spezifische Metadaten
    if exif.get("duration"):
        context_parts.append(f"Medientyp: Video")
        if exif.get("duration_formatted"):
            context_parts.append(f"Dauer: {exif['duration_formatted']}")
        if exif.get("video_codec"):
            context_parts.append(f"Codec: {exif['video_codec']}")
        if exif.get("video_frame_rate"):
            context_parts.append(f"Framerate: {exif['video_frame_rate']} fps")
        if exif.get("video_bitrate_kbps"):
            context_parts.append(f"Bitrate: {exif['video_bitrate_kbps']} kbps")

    # Geocoding-Daten (IA-03 lief vor uns)
    if geo.get("country"):
        location_parts = []
        if geo.get("suburb"):
            location_parts.append(geo["suburb"])
        if geo.get("city"):
            location_parts.append(geo["city"])
        if geo.get("state"):
            location_parts.append(geo["state"])
        if geo.get("country"):
            location_parts.append(geo["country"])
        context_parts.append(f"Aufnahmeort: {', '.join(location_parts)}")

    # Messenger-Hinweise aus Dateiname
    if "-WA" in filename.upper():
        context_parts.append("Herkunft: WhatsApp (Dateiname enthält -WA)")
    elif filename.startswith("signal-"):
        context_parts.append("Herkunft: Signal")
    elif filename.startswith("telegram-"):
        context_parts.append("Herkunft: Telegram")

    # UUID-Dateiname erkennen
    import re
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$", re.IGNORECASE)
    if uuid_re.match(filename):
        context_parts.append("Dateiname ist eine UUID (typisch für Messenger-Weiterleitungen)")

    metadata_context = "\n".join(context_parts)

    # ── API-Aufruf ──────────────────────────────────────────────────
    is_video = len(image_data_list) > 1
    if is_video:
        user_message = f"""Analysiere dieses Video anhand von {len(image_data_list)} extrahierten Frames.

Gesammelte Metadaten:
{metadata_context}

Nutze diese Informationen zusammen mit den Frames für deine Klassifikation.
Beachte: Die Frames sind gleichmässig über die Videodauer verteilt."""
    else:
        user_message = f"""Analysiere dieses Bild.

Gesammelte Metadaten:
{metadata_context}

Nutze diese Informationen zusammen mit dem Bildinhalt für deine Klassifikation."""

    # Build content array with text + image(s)
    content_parts = [{"type": "text", "text": user_message}]
    for img_data in image_data_list:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{img_data}"}
        })

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts}
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

    # KI-Kontext für Anzeige im Log-Detail speichern
    result["_context"] = metadata_context
    result["_images"] = len(image_data_list)
    result["_model"] = model

    return result

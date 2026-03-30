import asyncio
import base64
import json
import os
import httpx
from config import config_manager


DEFAULT_SYSTEM_PROMPT = """Du bist ein Bildanalyse-Assistent für eine Foto-Mediathek. Deine Hauptaufgabe ist es, persönliche Fotos von Chat-App-Schrott (Memes, Internet-Bilder) zu unterscheiden.

Antworte NUR mit validem JSON (kein Markdown, kein umgebender Text):
{
  "type": "personal|screenshot|internet_image|document|meme",
  "tags": ["tag1", "tag2", ...],
  "description": "Kurze Beschreibung in 1-2 Sätzen",
  "mood": "indoor|outdoor|night|backlit|studio",
  "people_count": 0,
  "quality": "blurry|average|good|excellent",
  "confidence": 0.0-1.0
}

## Klassifikationsregeln:

### "personal" — Persönliche Fotos/Videos
Echte Aufnahmen mit Kamera oder Handy: Selfies, Familienfotos, Landschaften, Essen, Tiere, Events, Reisen, Alltag.
WICHTIG: Ein persönliches Foto bleibt persönlich, auch wenn es per WhatsApp/Telegram/Signal gesendet wurde (niedrigere Qualität, kein EXIF).
Erkennungsmerkmale:
- Natürliche Imperfektionen (Verwacklung, ungünstige Belichtung, spontaner Moment)
- Persönlicher Kontext (Wohnung, Garten, Arbeitsplatz, Reiseziel)
- Echte Menschen in natürlichen Situationen
- Typische Handykamera-Perspektive
- GPS-Daten oder Kamera-Informationen in den Metadaten
- Dateigrösse meist >200 KB (auch nach Messenger-Kompression)

### "screenshot" — Bildschirmfotos
NUR echte Screenshots mit sichtbarer Statusleiste, App-UI oder Browser.
NICHT: Abfotografierte Bildschirme, Fotos mit Text, Grafiken.

### "internet_image" — Aus dem Internet heruntergeladen
Stock-Fotos, Social-Media-Reposts, Infografiken, Werbung, virale Bilder.
Erkennungsmerkmale:
- Wasserzeichen, perfekte Komposition, professionelle Bearbeitung
- Corporate Branding, Logos
- Viraler Content, motivierende Sprüche auf Landschaftsbildern
- Sehr kleine Dateigrösse (<100 KB) ohne EXIF → stark komprimiert, typisch für weitergeleitete Internet-Bilder

### "meme" — Internet-Memes und Witze
Bilder mit Text-Overlay, Reaction-Bilder, Comic-Panels, Witz-Bilder.
Erkennungsmerkmale:
- Impact-Font oder ähnliche Schrift oben/unten
- Bekannte Meme-Templates
- Billig zusammengeschnittene Collagen
- Sehr kleine Dateigrösse (<100 KB)

### "document" — Dokumente
Gescannte Dokumente, Quittungen, Briefe, Formulare.

## Entscheidungshilfe für schwierige Fälle:
1. Hat das Bild EXIF-Daten mit Kamera-Info? → Stark Richtung "personal"
2. Hat das Bild GPS-Koordinaten / Ortsdaten? → Stark Richtung "personal"
3. Dateigrösse >500 KB ohne EXIF? → Wahrscheinlich persönliches Foto via Messenger
4. Dateigrösse <100 KB ohne EXIF? → Wahrscheinlich Meme/Internet-Bild
5. Zeigt das Bild Text-Overlay/Meme-Format? → "meme"
6. Sieht das Bild professionell/perfekt aus? → "internet_image"
7. Sieht das Bild natürlich/spontan aus? → "personal"

## Zusatzregeln:
- tags: 3-8 relevante Tags auf DEUTSCH (z.B. Landschaft, Essen, Tier, Selfie, Gruppe, Stadt, Natur, Sport, Feier)
- description: Auf DEUTSCH, sachlich, 1-2 Sätze
- people_count: Anzahl sichtbarer Personen (0 wenn keine)
- quality: Technische Bildqualität bewerten
- confidence: Wie sicher bist du bei der Typ-Klassifikation (0.0-1.0). Bei Unsicherheit lieber 0.5 und "personal" statt falsch aussortieren."""


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

    # ── Alle Metadaten sammeln ──────────────────────────────────────
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    geo = step_results.get("IA-03", {})
    filename = os.path.basename(job.original_path)
    file_size_kb = os.path.getsize(filepath) / 1024

    context_parts = []

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

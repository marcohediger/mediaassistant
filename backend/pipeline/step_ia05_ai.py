import asyncio
import base64
import json
import os
import httpx
from config import config_manager


DEFAULT_SYSTEM_PROMPT = """You are an image analysis assistant for a photo media library. Static rules pre-classify files based on filename, extension, and EXIF metadata. Your job is to verify the pre-classification and correct it ONLY when the image content clearly contradicts it.

The available categories and the static rule pre-classification are provided with the metadata.

Respond ONLY with valid JSON (no markdown, no surrounding text):
{
  "type": "<category_label>",
  "source": "Origin of the image",
  "tags": ["Tag1", "Tag2", ...],
  "description": "Short description in 1-2 sentences",
  "mood": "indoor|outdoor|night|backlit|studio",
  "people_count": 0,
  "quality": "blurry|average|good|excellent",
  "confidence": 0.0-1.0
}

## 1) type — Category (target library folder)
Use one of the category labels EXACTLY as provided in the list (e.g. "Persönliches Foto", "Screenshot").

CRITICAL RULES for pre-classification handling:
- If a static rule assigned a category AND the metadata supports it (e.g. camera make/model present, GPS coordinates, real EXIF date), you MUST keep that classification. The metadata is objective evidence — do NOT override it based on image appearance alone.
- Only override the pre-classification when the image content CLEARLY contradicts it. Example: a meme with text overlay was classified as personal photo because it had EXIF from a forwarding app — override to sourceless.
- When NO static rule matched, classify based on image content and metadata combined.
- When in doubt, prefer personal photo — it is better to keep a meme in personal photos than to lose a real photo to sourceless.

Strong signals that CONFIRM personal photo:
- EXIF with camera make/model (e.g. Apple, Samsung, Canon, Nikon, DJI, Casio, Panasonic, Sony)
- GPS coordinates present
- File size > 500 KB with EXIF data
- Natural/spontaneous scene, real-world photo

Strong signals for sourceless (override personal ONLY if no camera EXIF):
- Text overlay, meme templates, watermarks
- Stock photos, ads, infographics
- Very small file size (< 100 KB) without EXIF
- Obviously generated or downloaded content

Strong signals for screenshot (use Screenshot category!):
- OS status bar, navigation bar, or app UI elements visible
- Device frame or notification area
- Chat conversations, messaging apps, social media interfaces (WhatsApp, Instagram, Facebook, Snapchat, TikTok, Twitter/X, Telegram, Signal)
- Social media posts, profiles, stories, feeds — if you can see the app interface around the content, it IS a screenshot
- App settings, menus, or dialog boxes
- Browser windows showing web content
- Any image that was clearly captured from a device screen
- Note: screenshots often have NO EXIF data and come via messenger with UUID filenames
- IMPORTANT: If the description mentions a social media post (Facebook, Instagram, etc.) or an app interface, classify as Screenshot, NOT as personal photo

## 2) source — Origin / provenance
Describes where the image came from. Capitalize first letter, human-readable, in GERMAN.
Examples: Kamerafoto, Selfie, Drohnenfoto, Meme, Internetbild, Werbung, Infografik, Dokument, Quittung, Screenshot App, Screenshot Web, Sticker, Comic, Weitergeleitetes Bild
You may define new fitting sources if none of the examples match.

## 3) tags — Descriptive content tags
3-8 tags describing the image content. In GERMAN, capitalize first letter, human-readable.
Examples: Landschaft, Essen, Tier, Hund, Katze, Gruppe, Stadt, Natur, Sport, Feier, Strand, Berge, Haus, Boot, Auto, Blumen, Sonnenuntergang, Familie, Kind

## Other fields:
- description: In GERMAN, factual, 1-2 sentences
- people_count: Number of visible people (0 if none)
- quality: Technical image quality
- confidence: How certain about the type classification (0.0-1.0)"""


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
    cat_list = " | ".join(c.label for c in categories)

    # ── Alle Metadaten sammeln ──────────────────────────────────────
    step_results = job.step_result or {}
    exif = step_results.get("IA-01", {})
    geo = step_results.get("IA-03", {})
    filename = os.path.basename(job.original_path)
    file_size_kb = os.path.getsize(filepath) / 1024

    # Statische Regel vorab auswerten für Kontext
    from pipeline.step_ia08_sort import _match_sorting_rules
    file_type = (exif.get("file_type") or "").upper()
    mime = exif.get("mime_type", "")
    is_video = mime.startswith("video/") or file_type in ("MP4", "MOV", "AVI", "MKV", "M4V", "3GP")
    rule_category = await _match_sorting_rules(filename, exif, session, is_video=is_video)
    rule_cat_label = ""
    if rule_category:
        for c in categories:
            if c.key == rule_category:
                rule_cat_label = c.label
                break

    context_parts = []

    # Available categories
    context_parts.append(f"Available categories: {cat_list}")

    # Static rule pre-classification
    if rule_category:
        context_parts.append(f"Pre-classification (static rule): {rule_cat_label}")
    else:
        context_parts.append("Pre-classification (static rule): no rule matched")

    # File info (always)
    context_parts.append(f"Filename: {filename}")
    context_parts.append(f"File size: {file_size_kb:.0f} KB")

    # EXIF data
    if exif.get("has_exif"):
        if exif.get("make"):
            context_parts.append(f"Camera: {exif['make']} {exif.get('model', '')}")
        if exif.get("date"):
            context_parts.append(f"Capture date: {exif['date']}")
        if exif.get("gps"):
            context_parts.append(f"GPS: {exif['gps_lat']}, {exif['gps_lon']}")
        if exif.get("width") and exif.get("height"):
            context_parts.append(f"Resolution: {exif['width']}x{exif['height']}")
        if exif.get("software"):
            context_parts.append(f"Software: {exif['software']}")
    else:
        context_parts.append("No EXIF data present (typical for messenger images)")

    # Video-specific metadata
    if exif.get("duration"):
        context_parts.append(f"Media type: Video")
        if exif.get("duration_formatted"):
            context_parts.append(f"Duration: {exif['duration_formatted']}")
        if exif.get("video_codec"):
            context_parts.append(f"Codec: {exif['video_codec']}")
        if exif.get("video_frame_rate"):
            context_parts.append(f"Framerate: {exif['video_frame_rate']} fps")
        if exif.get("video_bitrate_kbps"):
            context_parts.append(f"Bitrate: {exif['video_bitrate_kbps']} kbps")

    # Geocoding data
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
        context_parts.append(f"Location: {', '.join(location_parts)}")

    # Messenger hints from filename
    if "-WA" in filename.upper():
        context_parts.append("Origin: WhatsApp (filename contains -WA)")
    elif filename.startswith("signal-"):
        context_parts.append("Origin: Signal")
    elif filename.startswith("telegram-"):
        context_parts.append("Origin: Telegram")

    # UUID filename detection
    import re
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$", re.IGNORECASE)
    if uuid_re.match(filename):
        context_parts.append("Filename is a UUID (typical for messenger forwarded images)")

    metadata_context = "\n".join(context_parts)

    # ── API-Aufruf ──────────────────────────────────────────────────
    is_video = len(image_data_list) > 1
    if is_video:
        user_message = f"""Analyze this video based on {len(image_data_list)} extracted frames.

Collected metadata:
{metadata_context}

Use this information together with the frames for your classification.
Note: Frames are evenly distributed across the video duration."""
    else:
        user_message = f"""Analyze this image.

Collected metadata:
{metadata_context}

Use this information together with the image content for your classification."""

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

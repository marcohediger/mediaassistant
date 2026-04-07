import asyncio
import base64
import io
import json
import os
import httpx
from ai_backends import acquire_ai_backend
from config import config_manager
from PIL import Image


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
  "confidence": 0.0-1.0,
  "nsfw": false
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
Examples for images: Kamerafoto, Selfie, Drohnenfoto, Meme, Internetbild, Werbung, Infografik, Dokument, Quittung, Screenshot App, Screenshot Web, Sticker, Comic, Weitergeleitetes Bild
Examples for videos: Kameravideo, Selfie-Video, Drohnenvideo, Internetvideo, Weitergeleitetes Video, Bildschirmaufnahme
You may define new fitting sources if none of the examples match.

## 3) tags — Descriptive content tags
3-8 tags describing the image content. In GERMAN, capitalize first letter, human-readable.
IMPORTANT: Only assign tags for things that are CLEARLY VISIBLE in the image. Never invent or guess tags. If you are unsure about an object, do NOT tag it.
Tags should cover broad categories like: scene type (e.g. Landschaft, Innenraum), main subjects actually visible, activity, location type, notable objects, atmosphere.
Do NOT use a fixed vocabulary — derive tags strictly from what you actually see.

## Other fields:
- description: In GERMAN, factual, 1-2 sentences
- people_count: Number of visible people (0 if none)
- quality: Technical image quality
- confidence: How certain about the type classification (0.0-1.0)
- nsfw: true if the image contains nudity, explicit sexual content, or other not-safe-for-work material. Always false for landscapes, food, animals, buildings, etc."""


async def execute(job, session) -> dict:
    """IA-05: KI-Analyse via OpenAI-kompatiblem Endpunkt.

    Nutzt alle bisher gesammelten Metadaten (EXIF, Geocoding, Dateigrösse)
    für eine bestmögliche Klassifikation.
    """
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
    ext = os.path.splitext(filepath)[1].lower()

    # Skip AI if format not supported and conversion failed
    ai_native_formats = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    if ext not in ai_native_formats and not convert_result.get("converted"):
        return {
            "type": "unknown",
            "tags": [],
            "description": f"Format {ext} nicht konvertierbar, KI-Analyse übersprungen",
            "mood": "",
            "people_count": 0,
            "quality": "unbekannt",
            "confidence": 0.0,
            "_skipped": True,
            "_reason": f"IA-04 conversion failed for {ext}",
        }

    # Multi-frame support for videos
    image_paths = convert_result.get("temp_paths") or []
    if not image_paths:
        single = convert_result.get("temp_path") or filepath
        image_paths = [single]

    # Filter to existing files
    image_paths = [p for p in image_paths if os.path.exists(p)]
    if not image_paths:
        image_paths = [filepath]

    # Optionally resize images before sending to AI
    ai_resize_enabled = await config_manager.get("ai.image_resize", False)
    image_data_list = []
    if ai_resize_enabled:
        ai_max_px = int(await config_manager.get("ai.image_max_px", 1024))
        ai_max_px = max(256, ai_max_px)
        for img_path in image_paths:
            resized = await asyncio.to_thread(_resize_for_ai, img_path, ai_max_px)
            image_data_list.append(base64.b64encode(resized).decode("utf-8"))
    else:
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

    # ── Acquire idle AI backend and make API call ───────────────────
    async with acquire_ai_backend() as backend:
        if not backend:
            return {"status": "skipped", "reason": "not configured"}

        url = backend["url"]
        model = backend["model"]
        api_key = backend["api_key"]

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

        ai_timeout = int(await config_manager.get("ai.timeout", 120))
        max_retries = 2
        resp = None
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=ai_timeout) as client:
                    resp = await client.post(
                        f"{url.rstrip('/')}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                if resp.status_code < 500:
                    break
                # Server error — retry
                last_exc = RuntimeError(f"KI-API Fehler: HTTP {resp.status_code} — {resp.text[:200]}")
            except httpx.ReadTimeout as e:
                last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(5 * (attempt + 1))

    # Backend released — free large base64 data
    num_images = len(image_data_list)
    del image_data_list, content_parts, payload

    if resp is None:
        raise RuntimeError(f"KI-API Timeout nach {max_retries + 1} Versuchen") from last_exc
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
    result["_images"] = num_images
    result["_model"] = model

    return result


def _resize_for_ai(image_path: str, max_size: int = 768) -> bytes:
    """Resize image so longest side <= max_size, return JPEG bytes.

    Dramatically reduces payload for local AI models (e.g. 3MB → 40KB).
    Falls back to raw file bytes if PIL cannot open the image.
    """
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            w, h = img.size
            if w <= max_size and h <= max_size:
                # Already small enough — just re-encode as JPEG
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
            if w >= h:
                new_w, new_h = max_size, int(h * max_size / w)
            else:
                new_w, new_h = int(w * max_size / h), max_size
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
    except Exception:
        # Fallback: send raw file
        with open(image_path, "rb") as f:
            return f.read()

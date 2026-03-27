# MediaAssistant

Automated media processing: Photos and videos are detected, analyzed, tagged, and sorted into a library.

## How it works

```
Inbox  →  EXIF  →  Convert  →  Duplicates  →  AI  →  OCR  →  Geocoding  →  Tags  →  Sort  →  Notify  →  Library
```

New files in inbox directories are automatically detected and processed through an 11-step pipeline:

| Step | Name | Description |
|------|------|-------------|
| IA-01 | Read EXIF | Extract metadata via ExifTool |
| IA-02 | Format Conversion | HEIC/DNG/RAW/GIF → temp JPEG for AI analysis |
| IA-03 | Duplicate Detection | SHA256 (exact) + pHash (similar) |
| IA-04 | AI Analysis | Analyze image (type, tags, description, mood) |
| IA-05 | OCR | Text recognition (screenshots, documents) |
| IA-06 | Geocoding | GPS coordinates → place names (country, state, city, suburb) |
| IA-07 | Write EXIF Tags | Write tags, description, geocoding and folder-tags back to file |
| IA-08 | Sort | Move file to library by type/date, clean up empty source folders |
| IA-09 | Notification | Email on errors (SMTP, Office 365 / Gmail) |
| IA-10 | Cleanup | Remove temporary files |
| IA-11 | SQLite Log | Log processing summary |

IA-09 to IA-11 are **finalizers** — they always run, even if a critical step fails.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- (Optional) LM Studio or compatible OpenAI API server for AI analysis

### Installation

```bash
git clone https://git.marcohediger.ch/MediaAssistant/ma-core.git
cd ma-core
cp .env.example .env
```

Edit `.env`:

```env
# AI Backend (LM Studio etc.)
AI_BACKEND_URL=http://192.168.0.100:1234/v1
AI_MODEL=qwen/qwen3-vl-4b

# Paths
INBOX_PATH=/volume1/inbox
LIBRARY_PATH=/volume1/bibliothek

# SMTP (e.g. Office 365)
SMTP_SERVER=smtp.office365.com
SMTP_PORT=587
SMTP_SSL=false
SMTP_USER=user@example.com
SMTP_PASSWORD=
SMTP_RECIPIENT=user@example.com

# Timezone
TZ=Europe/Zurich
```

Environment variables are imported into the database on first start. After that, all settings can be changed via the web interface.

### Start (Production)

```bash
docker compose up -d
```

Web interface: **http://localhost:8000**

On first start, the setup wizard is displayed.

### Start (Development)

```bash
docker compose -f docker-compose.dev.yml up -d
```

Hot-reload active — code changes are applied immediately.

## Web Interface

### Internationalization (i18n)
The web interface supports multiple languages (currently German and English). Language and theme (dark/light) can be configured in **Settings → Appearance**.

All system log messages are always written in English, regardless of the UI language.

### Dashboard
- Processing statistics (total, done, errors, queue, duplicates)
- Module status with health checks (AI backend, geocoding, SMTP, file watcher)
- Recently processed files with live auto-refresh

### Settings
- Enable/disable modules individually
- AI backend, geocoding, SMTP configuration
- Editable AI system prompt (stored in database, default fallback)
- Manage inbox directories (with dry-run, folder-tags, active toggle per inbox)
- Library target structure with placeholders
- Duplicate detection threshold (pHash)
- OCR mode (smart / all images)
- File watcher schedule mode (continuous / time window / scheduled / manual)
- Appearance: Language (DE/EN) and Theme (dark/light)

### Duplicate Review
- All files of a group side-by-side (transitive grouping via Union-Find)
- Per file: thumbnail, file size, resolution, megapixels
- Per file: all EXIF data read directly from file (date, camera, ISO, aperture, shutter speed, focal length, GPS)
- Per file: all keywords/tags and description from file
- Per file: similarity score (SHA256 exact / pHash %)
- Actions: "Keep this" (moves to library, deletes all others)
- Batch-Clean: auto-delete all exact SHA256 duplicates
- Orphaned entries: if a referenced original file no longer exists on disk, the match is skipped and the new file is treated as a fresh original

### Log Viewer
- System log (errors, warnings, info)
- Processing log (jobs with status and step details)
- Filter, search, pagination
- Job detail page with step results, paths, timestamps, hashes
- Live auto-refresh on job detail page

## AI Analysis

The AI prompt is fully editable in **Settings → AI Analysis**. The prompt is written in English and instructs the AI to classify images into types:

| Type | Description |
|------|-------------|
| `personal` | Personal photos (people, selfies, pets, food, travel, events) |
| `screenshot` | Device screenshots (must have OS UI elements like status bar, navigation) |
| `internet_image` | Downloaded images, memes, ads, stock photos |
| `document` | Scanned documents, receipts, forms, handwritten notes |
| `meme` | Memes with text overlay on images |

The AI returns JSON with: type, tags (German), description (German), mood, people_count, quality, confidence.

## Library Structure

Target structure is configurable per category with placeholders:

| Placeholder | Example |
|-------------|---------|
| `{YYYY}` | 2026 |
| `{MM}` | 03 |
| `{DD}` | 27 |
| `{YYYY-MM}` | 2026-03 |
| `{CAMERA}` | iPhone_15_Pro |
| `{TYPE}` | personal |
| `{COUNTRY}` | Schweiz |
| `{CITY}` | Ehrendingen |

Default structure:

```
/bibliothek/
├── photos/{YYYY}/{YYYY-MM}/       ← personal photos, chronological
├── sourceless/{YYYY}/             ← images without EXIF (messenger, apps)
├── screenshots/{YYYY}/            ← screenshots
├── videos/{YYYY}/{YYYY-MM}/       ← videos
├── unknown/review/                ← AI uncertain, manual review
├── error/                         ← failed files
└── error/duplicates/              ← detected duplicates
```

## Features

### Dry-Run Mode
Each inbox directory has a dry-run toggle. When enabled:
- All analysis steps run normally (EXIF, AI, OCR, Geocoding, Duplicates)
- **IA-07**: Tags/description are calculated but **not written** to the file
- **IA-08**: Target path is calculated but the file is **not moved**
- Step results show `status: "dry_run"` with the planned values
- The file stays untouched in the inbox

Useful for testing the pipeline on an existing photo library before committing changes.

### Folder Tags
Inbox subdirectory names can be automatically added as EXIF keywords. Configurable per inbox directory. Example: a file in `/inbox/manual/vacation/italy/` gets keywords `["vacation", "italy"]`.

### Geocoding Keywords
All geocoding fields (country, state, city, suburb) are written as EXIF keywords, with deduplication.

### Safe File Move
Every file move is a three-step process to prevent data loss:
1. **Copy** — `shutil.copy2` (preserves metadata)
2. **Verify** — compare file size + SHA256 hash
3. **Delete** — original is only deleted after successful verification

### Empty Folder Cleanup
After moving a file from an inbox, empty parent directories are automatically cleaned up (up to the inbox root).

## Supported Formats

**Images:** JPG, JPEG, PNG, HEIC, HEIF, TIFF, WebP, GIF, BMP, DNG, CR2, NEF, ARW

**Videos:** MP4, MOV, AVI, MKV, M4V, 3GP

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async), aiosqlite
- **Database:** SQLite
- **Container:** Docker with ExifTool, FFmpeg, libheif
- **AI:** Any OpenAI-compatible Vision API server (e.g. LM Studio, Ollama)
- **Geocoding:** Nominatim, Photon, or Google Maps API
- **i18n:** JSON language files (DE/EN), centralized template rendering
- **Theme:** Dark (default) / Light, conditional CSS loading

## Encrypted Configuration

API keys and passwords are encrypted with Fernet (AES-128-CBC) in the database. The key is stored in `/app/data/.secret_key`.

## License

Private project by Marco Hediger.

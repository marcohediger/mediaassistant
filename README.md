![MediaAssistant](logos/mediaassistant_logo_dark.svg)

Drop your photos and videos into a folder. MediaAssistant takes care of the rest: metadata extraction, AI-powered tagging, duplicate detection, geocoding, and automatic sorting into your library or Immich.

![Dashboard](docs/screenshots/Screenshot%202026-04-15%20133554.png)

## What it does

- **Automatic processing** — New files are detected and run through an 11-step pipeline
- **AI-powered tagging** — Every photo and video gets descriptive keywords, a category, and a description
- **Duplicate detection** — SHA256 (exact) and perceptual hash (similar) matching across images and videos
- **Geocoding** — GPS coordinates are resolved to place names and written as EXIF keywords
- **Immich integration** — Upload to Immich with albums from folder structure, or auto-tag mobile uploads
- **OCR** — Text recognition for screenshots and documents
- **Sorting** — Configurable rules and AI-verified classification sort files into the right folders

## Pipeline

```
Inbox  →  EXIF  →  Duplicates  →  Geocoding  →  Convert  →  AI  →  OCR  →  Tags  →  Sort  →  Notify  →  Library
```

| Step | Name | What it does |
|------|------|-------------|
| IA-01 | Read EXIF | Extract metadata via ExifTool; videos via ffprobe (GPS, date, duration, resolution, codec) |
| IA-02 | Duplicates | SHA256 + pHash matching. Duplicates go to a review folder. Videos use averaged frame hashes |
| IA-03 | Geocoding | GPS coordinates to place names (country, city, suburb) via Nominatim, Photon, or Google |
| IA-04 | Convert | HEIC/DNG/RAW/GIF to temp JPEG for AI; video frame extraction via ffmpeg |
| IA-05 | AI Analysis | Vision AI classifies type, source, tags, description. Categories from database |
| IA-06 | OCR | Text recognition for screenshots and documents |
| IA-07 | Write Tags | AI tags, geocoding, folder tags, description written to EXIF (direct or sidecar mode) |
| IA-08 | Sort | Static rules + AI verification. Move to library or upload to Immich with albums |
| IA-09 | Notify | Email on errors (SMTP) |
| IA-10 | Cleanup | Remove temporary files |
| IA-11 | Log | Processing summary to database |

## Screenshots

### Review — Manual Classification
![Review](docs/screenshots/Screenshot%202026-04-15%20133653.png)

Files the AI is uncertain about land in the review queue. Classify them manually with one click.

### Settings — Inbox Directories
![Inbox Settings](docs/screenshots/Screenshot%202026-04-15%20133709.png)

Multiple inbox directories with individual settings: Immich upload, folder tags, dry-run mode.

### Settings — Sorting Rules
![Sorting Rules](docs/screenshots/Screenshot%202026-04-15%20133728.png)

Flexible rules based on filename, regex, EXIF expressions, or file extension. First match wins. AI verifies all files.

### Settings — Target Storage
![Target Storage](docs/screenshots/Screenshot%202026-04-15%20133741.png)

Configurable library structure with placeholders ({YYYY}, {COUNTRY}, {CAMERA}, ...). Per-category paths.

### Duplicate Review — Side-by-Side Comparison
![Duplicate Review](docs/screenshots/Screenshot%202026-04-15%20150132.png)

Compare duplicates with thumbnails, metadata, and quality scores at a glance. Keep the best file, batch-clean entire groups, or mark false positives — with live progress tracking.

### Authentication — SSO Login
![SSO Login](docs/screenshots/Screenshot%202026-04-15%20135538.png)

Secure access via OIDC/OAuth2. Works with Authentik, Keycloak, Authelia, or any compatible provider — one click to sign in.

## Quick Start

```bash
mkdir mediaassistant && cd mediaassistant
curl -O https://raw.githubusercontent.com/marcohediger/mediaassistant/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/marcohediger/mediaassistant/main/.env.example
cp .env.example .env
```

Edit `.env`:

```env
# AI Backend (LM Studio, Ollama, etc.)
AI_BACKEND_URL=http://192.168.0.100:1234/v1
AI_MODEL=qwen/qwen3-vl-4b

# Paths
INBOX_PATH=/volume1/inbox
LIBRARY_PATH=/volume1/library

# Timezone
TZ=Europe/Zurich
```

Start:

```bash
docker compose up -d
```

Web interface: **http://localhost:8000**

On first start, a setup wizard guides you through the configuration.

## Immich Integration

MediaAssistant works bidirectionally with Immich:

**Inbox to Immich** — Toggle per inbox directory. Files are analyzed, tagged, and uploaded with albums created from folder structure. The original is deleted from the inbox after upload.

**Immich to Pipeline** — Enable polling to auto-process new mobile uploads. Tags, descriptions, and geocoding are added. Assets uploaded by MediaAssistant are automatically skipped (no double processing).

**Archiving** — Categories like screenshots or sourceless images are automatically archived in Immich (hidden from timeline). NSFW content is moved to the locked folder.

## Duplicate Review

Duplicates are detected automatically and placed in a review queue:

- Side-by-side comparison with thumbnails, EXIF data, keywords, and quality scores
- **Keep this** — triggers full pipeline re-run with merged metadata from all group members
- **Batch-Clean** — automatically keeps the best quality file per group with live progress bar
- **Not a duplicate** — re-processes the file through the full pipeline
- GPS, date, keywords, description, and folder tags are merged automatically

## Folder Tags

Inbox subdirectory names become EXIF keywords and Immich albums:

```
/inbox/vacation/italy/photo.jpg
  → Keywords: vacation, italy
  → Immich Album: vacation italy
```

## Sorting Rules

Configurable rules evaluated before AI classification:

| Condition | Example |
|-----------|---------|
| Filename contains | `-WA` (WhatsApp) |
| Filename pattern | `^IMG_\d+` (regex) |
| EXIF expression | `make != "" & date != ""` |
| File extension | `.png` |

Each rule maps to a category and has a media type filter (All / Images / Videos).

## Library Structure

Target paths with placeholders, configurable per category:

```
/library/
├── photos/{YYYY}/{YYYY-MM}/          ← personal photos
├── videos/{YYYY}/{YYYY-MM}/          ← personal videos
├── sourceless/foto/{YYYY}/           ← memes, forwarded images
├── screenshots/{YYYY}/               ← screenshots
└── error/duplicates/                 ← detected duplicates
```

## Supported Formats

**Images:** JPG, JPEG, PNG, HEIC, HEIF, TIFF, WebP, GIF, BMP, DNG, CR2, NEF, ARW

**Videos:** MP4, MOV, AVI, MKV, M4V, 3GP

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async), aiosqlite
- **Database:** SQLite with WAL mode
- **Container:** Docker with ExifTool, FFmpeg, libheif, ImageMagick
- **AI:** Any OpenAI-compatible Vision API (LM Studio, Ollama, etc.)
- **Geocoding:** Nominatim, Photon, or Google Maps
- **i18n:** German and English
- **Theme:** Dark and Light mode

## Authentication (SSO)

OIDC/OAuth2 authentication via Authentik, Keycloak, Authelia, or any OIDC-compliant provider.

```yaml
environment:
  - AUTH_MODE=oidc
  - OIDC_ISSUER=https://auth.example.com/application/o/mediaassistant/
  - OIDC_CLIENT_ID=mediaassistant
  - OIDC_CLIENT_SECRET=your-secret-here
```

Set `AUTH_MODE=disabled` (default) for no authentication.

## Environment Variables

Settings are imported into the database on first start. After that, use the web interface.

| Variable | Description |
|----------|-------------|
| `AI_BACKEND_URL` | AI backend URL |
| `AI_MODEL` | AI model name |
| `AI_API_KEY` | AI backend API key (if required) |
| `IMMICH_URL` | Immich server URL |
| `IMMICH_API_KEY` | Immich API key |
| `GEOCODING_PROVIDER` | nominatim / photon / google |
| `SMTP_SERVER` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Email notifications |
| `LIBRARY_BASE_PATH` | Library base path |
| `INBOX_PATH` | Default inbox path |
| `UI_LANGUAGE` | de / en |
| `UI_THEME` | dark / light |

## Safe File Operations

Every file move uses copy → verify hash → delete. No data loss even on interrupted operations.

## Encrypted Configuration

API keys and passwords are encrypted with Fernet (AES-128-CBC) in the database.

## Companion Tools

| Tool | Purpose |
|------|---------|
| [ma-sidecar-repair](https://github.com/marcohediger/ma-sidecar-repair) | Repair broken XMP sidecars |
| [ma-ghost-tag-detect](https://github.com/marcohediger/ma-ghost-tag-detect) | Find hallucinated AI tags |

## License

Private project by Marco Hediger.

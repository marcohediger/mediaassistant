# MediaAssistant

Automated media processing: Photos and videos are detected, analyzed, tagged, and sorted into a library.

## How it works

```
Inbox  →  EXIF  →  Duplicates  →  Geocoding  →  Convert  →  AI  →  OCR  →  Tags  →  Sort  →  Notify  →  Library
```

New files in inbox directories are automatically detected and processed through an 11-step pipeline:

| Step | Name | Description |
|------|------|-------------|
| IA-01 | Read EXIF | Extract metadata via ExifTool; videos additionally via ffprobe (date, GPS with ISO 6709 parser, duration + formatted, resolution, megapixels, codec, framerate, bitrate, rotation) |
| IA-02 | Duplicate Detection | SHA256 (exact) + pHash (similar) for images and videos, incl. Immich-uploaded files. Video pHash is computed as average from IA-04 frames (post-IA-04 check) |
| IA-03 | Geocoding | GPS coordinates → place names (country, state, city, suburb) |
| IA-04 | Temp. Conversion for AI | HEIC/DNG/RAW/GIF → temp JPEG for AI analysis; video thumbnail extraction via ffmpeg (configurable number of frames evenly distributed across video duration) |
| IA-05 | AI Analysis | Classify image (type from DB categories, source, tags, description) with all collected metadata + static rule pre-classification |
| IA-06 | OCR | Text recognition (screenshots, documents) |
| IA-07 | Write EXIF Tags | Write AI tags, source, description, geocoding and folder-tags back to file |
| IA-08 | Sort | Static rules + AI verification → category; write category tag; move to library or upload to Immich |
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
mkdir mediaassistant && cd mediaassistant
curl -O https://raw.githubusercontent.com/marcohediger/mediaassistant/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/marcohediger/mediaassistant/main/.env.example
cp .env.example .env
```

Or clone the full repository:

```bash
git clone https://github.com/marcohediger/mediaassistant.git
cd mediaassistant
cp .env.example .env
```

Edit `.env`:

```env
# AI Backend (LM Studio etc.)
AI_BACKEND_URL=http://192.168.0.100:1234/v1
AI_MODEL=qwen/qwen3-vl-4b

# Paths
INBOX_PATH=/volume1/inbox
LIBRARY_PATH=/volume1/library

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

**All supported environment variables:**

| Variable | Description |
|----------|-------------|
| `AI_BACKEND_URL` | AI backend URL (LM Studio, Ollama etc.) |
| `AI_MODEL` | AI model name |
| `AI_API_KEY` | AI backend API key (if required) |
| `IMMICH_URL` | Immich server URL |
| `IMMICH_API_KEY` | Immich API key |
| `GEOCODING_PROVIDER` | Geocoding provider (nominatim/photon/google) |
| `GEOCODING_URL` | Custom geocoding URL |
| `GOOGLE_MAPS_API_KEY` | Google Maps API key |
| `SMTP_SERVER` | SMTP server hostname |
| `SMTP_PORT` | SMTP port |
| `SMTP_SSL` | SMTP SSL (true/false) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASSWORD` | SMTP password |
| `SMTP_RECIPIENT` | Notification email recipient |
| `LIBRARY_BASE_PATH` | Library base path |
| `INBOX_PATH` | Default inbox path (creates inbox on first start) |
| `INBOX_LABEL` | Default inbox label (default: "Default Inbox") |
| `UI_LANGUAGE` | Interface language (de/en) |
| `UI_THEME` | Theme (dark/light) |
| `FILEWATCHER_INTERVAL` | File watcher interval in seconds |
| `FILEWATCHER_SCHEDULE_MODE` | Schedule mode (continuous/window/scheduled/manual) |
| `OCR_MODE` | OCR mode (smart/all) |
| `PHASH_THRESHOLD` | Duplicate detection pHash threshold |
| `SETUP_COMPLETE` | Skip setup wizard (true/false) |
| `AUTH_MODE` | Authentication mode: `disabled` or `oidc` (see [Authentication](#authentication-sso)) |
| `OIDC_ISSUER` | SSO server URL |
| `OIDC_CLIENT_ID` | Application/Client ID |
| `OIDC_CLIENT_SECRET` | Client secret |
| `OIDC_REDIRECT_URI` | Callback URL (auto-derived if omitted) |
| `OIDC_SCOPES` | OIDC scopes (default: openid profile email) |
| `SESSION_SECRET` | Cookie signing secret (auto-generated if omitted) |
| `SESSION_LIFETIME_HOURS` | Session duration in hours (default: 8) |

### Start (Production)

```bash
docker compose up -d
```

This pulls the image from `ghcr.io/marcohediger/mediaassistant:latest`.

Web interface: **http://localhost:8000**

On first start, the setup wizard is displayed.

### Start (Local Build)

If you want to build the image locally instead of pulling from ghcr.io:

```bash
docker compose -f docker-compose.build.yml up -d
```

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
- Manage inbox directories (with dry-run, folder-tags, Immich toggle, active toggle per inbox)
- Immich integration (URL, API key, polling toggle)
- Library target structure with placeholders
- Duplicate detection threshold (pHash)
- OCR mode (smart / all images)
- File watcher schedule mode (continuous / time window / scheduled / manual) with enforcement
- Manual scan trigger button on dashboard
- Appearance: Language (DE/EN) and Theme (dark/light)

### Duplicate Review
- All files of a group side-by-side (transitive grouping via Union-Find)
- Per file: thumbnail with lightbox (click to view original in full size), file size, resolution, megapixels
- Per file: all EXIF data — read from file (local) or fetched via Immich API (Immich assets)
- Per file: all keywords/tags and description from file
- Per file: similarity score (SHA256 exact / pHash %)
- Per file: badge (ORIGINAL/EXACT) is a clickable link — Immich assets open in Immich, local files download
- **Immich duplicates**: Thumbnail fetched from Immich, "View in Immich" button, "Delete local copy" for the local file
- Actions: "Keep this" on all group members — triggers full pipeline re-run (AI analysis, tag writing, sorting/Immich upload) so the kept file gets all metadata before being filed
- Batch-Clean: auto-delete all exact SHA256 duplicates
- Orphaned entries: if a referenced original file no longer exists on disk (or was deleted from Immich), the match is skipped and the new file is treated as a fresh original

### Review
- Manual classification of unclear files (AI uncertain, no EXIF, messenger files)
- Thumbnail preview with lightbox (click to view original in full size)
- AI description, tags, metadata displayed
- File size (with Immich API fallback), dimensions, date (fallback to FileModifyDate/job.created_at)
- Metadata fields shown conditionally (date/camera only when present)
- Category buttons loaded dynamically from database (all non-fixed categories)
- Delete button to remove review files directly
- Immich: archiving per category (configurable via `immich_archive` flag in library categories)
- Batch action: classify all as sourceless

### Log Viewer
- System log with full traceback on errors
- Processing log with duration, status and step details
- Filter, search, pagination
- Job detail page with step results, paths, timestamps, hashes, full error traceback
- Job detail page with lightbox (click thumbnail to view original in full size)
- Live auto-refresh on job detail page
- Immich thumbnail in job detail page

## AI Analysis

The AI prompt is fully editable in **Settings → AI Analysis**. Available categories are loaded dynamically from the database (`library_categories` table) and passed to the AI prompt as context. Static sorting rule pre-classification is also provided so the AI can verify or correct it.

The AI returns JSON with three main fields:

| Field | Description | Example |
|-------|-------------|---------|
| `type` | Category key from database | `personliches_foto`, `personliches_video`, `screenshot`, `sourceless_foto`, `sourceless_video` |
| `source` | Origin/source of the image/video | Images: `Kamerafoto`, `Meme`, `Internetbild`. Videos: `Kameravideo`, `Drohnenvideo`, `Internetvideo` |
| `tags` | Descriptive content tags | `Landschaft`, `Tier`, `Haus`, `Boot` |

Additional fields: `description` (German), `mood`, `people_count`, `quality`, `confidence`.

Categories are fully configurable in **Settings → Library Categories** — no hardcoded types in the code.

### EXIF Tag Strategy

Tags are written in two pipeline steps to keep them clean and organized:

**IA-07 (EXIF Write)** — AI-generated tags:

| Source | Example Tags | Notes |
|--------|-------------|-------|
| AI descriptive tags | `Landschaft`, `Tier`, `Essen`, `Selfie` | From AI `tags` field |
| AI source | `Kamerafoto`, `Meme`, `Internetbild` | From AI `source` field |
| Geocoding | `Schweiz`, `Zürich`, `Altstadt` | Country, state, city, suburb |
| Folder tags | `vacation`, `italy`, `album:vacation italy` | From inbox subdirectories |
| OCR flag | `OCR` | Set when text was detected (actual text in EXIF UserComment) |
| Quality | `blurry` | Only written when image is blurry |

**IA-08 (Sort)** — Category tags:

| Source | Example Tags | Notes |
|--------|-------------|-------|
| Category label | `Persönliches Foto`, `Screenshot` | From library_categories DB |
| AI source | `Kamerafoto`, `Meme` | Repeated for category context |

**Not written as tags:** mood (indoor/outdoor), quality levels other than blurry, OCR text type.

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

Default structure (categories configurable in Settings → Library Categories):

```
/library/
├── photos/{YYYY}/{YYYY-MM}/          ← personal photos, chronological
├── videos/{YYYY}/{YYYY-MM}/          ← personal videos, chronological
├── sourceless/foto/{YYYY}/           ← images without source (messenger, memes)
├── sourceless/video/{YYYY}/          ← videos without source (messenger, forwarded)
├── screenshots/{YYYY}/               ← screenshots
├── unknown/review/                   ← AI uncertain, manual review
├── error/                            ← failed files
└── error/duplicates/                 ← detected duplicates
```

## Features

### Dry-Run Mode
Each inbox directory has a dry-run toggle. When enabled:
- All analysis steps run normally (EXIF, AI, OCR, Geocoding, Duplicates)
- **IA-07**: Tags/description are calculated but **not written** to the file
- **IA-08**: Target path is calculated but the file is **not moved**
- Step results show `status: "dry_run"` with the planned values
- The file stays untouched in the inbox
- **HTML Report**: After a dry-run, view a summary report under **Logs → Dry-Run Report** with category counts, per-inbox breakdown, file list, and errors

Useful for testing the pipeline on an existing photo library before committing changes.

### Sorting Rules
Configurable sorting rules in **Settings → Sorting Rules**. Static rules are evaluated first — the AI then verifies ALL files and can correct the classification if needed (e.g. rescue a personal photo from "Sourceless"). First matching rule wins.

| Condition | Example | Description |
|-----------|---------|-------------|
| Filename contains | `-WA` | Match if filename contains the value |
| Filename pattern | `^IMG_\d+` | Match by regex pattern |
| EXIF expression | `make != "" & date != ""` | Match with operators (`==`, `!=`, `~`, `!~`) and logic (`&` AND, `\|` OR) |
| File extension | `.png` | Match by file extension |

Each rule maps to a target category from the database and has a **media type filter** (All / Images / Videos). This allows separate rules for images and videos — e.g. UUID filenames route to `sourceless_foto` for images and `sourceless_video` for videos. Rules can be reordered with up/down buttons and individually enabled/disabled.

### Folder Tags
Inbox subdirectory names can be automatically added as EXIF keywords. Configurable per inbox directory. Example: a file in `/inbox/manual/vacation/italy/` gets keywords `["vacation", "italy"]` and an `album:vacation italy` tag.

When combined with Immich upload, folder tags also create an **Immich album** with the combined name (e.g. "vacation italy").

### Geocoding Keywords
All geocoding fields (country, state, city, suburb) are written as EXIF keywords, with deduplication.

### Safe File Move
Every file move is a three-step process to prevent data loss:
1. **Copy** — `shutil.copy2` (preserves metadata)
2. **Verify** — compare file size + SHA256 hash
3. **Delete** — original is only deleted after successful verification

### Immich Integration
MediaAssistant integrates with Immich in two directions:

**Inbox → Immich (Upload)**
Each inbox directory can optionally upload files to Immich instead of moving them to the local library. Configurable per inbox directory via the "Immich" toggle.

When enabled for an inbox:
- **IA-07**: All EXIF tags (AI, geocoding, folder tags, `album:` tag) are written before upload
- **IA-08**: File is uploaded to Immich via API, then deleted from inbox
- The file is **not** copied to the local target directory
- **Albums**: If folder tags are active, an Immich album is created from the subfolder names (e.g. `Ferien/Nänikon 2026/` → album "Ferien Nänikon 2026")

**Immich → Pipeline (Polling)**
New uploads in Immich (e.g. from the mobile app) can be automatically processed through the full pipeline. Enable "Immich Polling" in **Settings → Immich**.

When enabled:
- MediaAssistant polls for new assets on the same interval as the file watcher
- On first activation, the timestamp is set to "now" — existing assets are not processed
- New assets are downloaded, processed (AI, OCR, Geocoding), tags are written to the file via EXIF, and the asset is replaced in Immich with the tagged version
- Assets uploaded from an inbox are automatically skipped (no double processing)

**Archiving & Locked Folder:**
- Each library category has a configurable `immich_archive` flag (Settings → Library Categories)
- Categories with archiving enabled (e.g. Sourceless, Screenshot) are automatically archived in Immich (hidden from timeline, accessible via Archive)
- Categories without archiving (e.g. personal photos, videos) stay in the main timeline
- **NSFW detection**: The AI flags explicit/not-safe-for-work content (`nsfw: true`). These assets are automatically moved to the **locked folder** in Immich (`visibility: locked`). Locked takes priority over archiving. Works for both upload and polling paths.

**Shared features:**
- **Duplicate detection**: Previously uploaded files are tracked in the local database — re-uploading the same file triggers duplicate review with side-by-side comparison (Immich thumbnail vs. local file)
- **Orphaned assets**: If a referenced Immich asset is deleted, the duplicate match is skipped and the new file is treated as a fresh original
- Requires Immich URL and API key configured in **Settings → Immich**
- Dashboard shows Immich connection status in module health checks

### Empty Folder Cleanup
After moving a file from an inbox, empty parent directories are automatically cleaned up (up to the inbox root).

## Supported Formats

**Images:** JPG, JPEG, PNG, HEIC, HEIF, TIFF, WebP, GIF, BMP, DNG, CR2, NEF, ARW

**Videos:** MP4, MOV, AVI, MKV, M4V, 3GP

## Architecture

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async), aiosqlite
- **Database:** SQLite
- **Container:** Docker with ExifTool, FFmpeg/ffprobe, libheif
- **AI:** Any OpenAI-compatible Vision API server (e.g. LM Studio, Ollama)
- **Immich:** Bidirectional — upload from inbox + polling for new mobile uploads (REST API)
- **Geocoding:** Nominatim, Photon, or Google Maps API
- **i18n:** JSON language files (DE/EN), centralized template rendering
- **Theme:** Dark (default) / Light, conditional CSS loading

## Authentication (SSO)

MediaAssistant supports OIDC/OAuth2 authentication via SSO server, configured via the `AUTH_MODE` environment variable:

| `AUTH_MODE` | Description |
|-------------|-------------|
| `disabled` | No authentication (default) |
| `oidc` | Full OIDC/OAuth2 login via SSO server |

MediaAssistant redirects to your SSO server for login and receives identity claims via secure token exchange.

**Supported providers:** Authentik, Keycloak, Authelia, any OIDC-compliant provider.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_MODE` | `disabled` | Set to `oidc` |
| `OIDC_ISSUER` | *(required)* | SSO server URL (e.g. `https://auth.example.com/application/o/mediaassistant/`) |
| `OIDC_CLIENT_ID` | *(required)* | Application/Client ID from SSO provider |
| `OIDC_CLIENT_SECRET` | *(required)* | Client secret from SSO provider |
| `OIDC_REDIRECT_URI` | *(auto)* | Callback URL (e.g. `https://media.example.com/auth/callback`) |
| `OIDC_SCOPES` | `openid profile email` | OIDC scopes to request |
| `SESSION_SECRET` | *(auto-generated)* | Secret for signing session cookies |
| `SESSION_LIFETIME_HOURS` | `8` | Session duration in hours |

#### Example: Authentik

1. In Authentik, create a new **OAuth2/OpenID Provider**:
   - Name: `MediaAssistant`
   - Client ID: `mediaassistant` (or auto-generated)
   - Redirect URI: `https://media.example.com/auth/callback`
   - Scopes: `openid`, `profile`, `email`

2. Create an **Application** linked to the provider

3. Configure MediaAssistant:

```yaml
services:
  mediaassistant:
    image: ghcr.io/marcohediger/mediaassistant:latest
    environment:
      - AUTH_MODE=oidc
      - OIDC_ISSUER=https://auth.example.com/application/o/mediaassistant/
      - OIDC_CLIENT_ID=mediaassistant
      - OIDC_CLIENT_SECRET=your-secret-here
      - OIDC_REDIRECT_URI=https://media.example.com/auth/callback
```

#### Example: Keycloak

```yaml
environment:
  - AUTH_MODE=oidc
  - OIDC_ISSUER=https://keycloak.example.com/realms/myrealm
  - OIDC_CLIENT_ID=mediaassistant
  - OIDC_CLIENT_SECRET=your-secret-here
  - OIDC_REDIRECT_URI=https://media.example.com/auth/callback
```

### Login Flow

1. User opens MediaAssistant → redirected to `/auth/login`
2. `/auth/login` redirects to SSO provider login page
3. User authenticates at SSO provider
4. SSO provider redirects back to `/auth/callback` with auth code
5. MediaAssistant exchanges code for tokens, extracts username/email
6. Session cookie is set, user is redirected to the app
7. Logout via ✕ button in navbar → clears session, redirects to SSO logout

### Exempt paths

The following paths are accessible without authentication:
- `/static/*` — CSS, JS, images
- `/auth/*` — Login, callback, logout endpoints
- `/api/health` — Health check endpoint
- `/setup` — Setup wizard (first-time configuration)

### Disabling authentication

Set `AUTH_MODE=disabled` (default) or remove all AUTH variables. All routes are then publicly accessible — only use this behind a trusted network or VPN.

## Encrypted Configuration

API keys and passwords are encrypted with Fernet (AES-128-CBC) in the database. The key is stored in `/app/data/.secret_key`.

## License

Private project by Marco Hediger.

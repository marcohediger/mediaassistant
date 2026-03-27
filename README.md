# MediaAssistant

Automatisierte Medienverarbeitung: Fotos und Videos werden erkannt, analysiert, getaggt und in eine Bibliothek einsortiert.

## Funktionsweise

```
Inbox  →  EXIF lesen  →  Konvertierung  →  KI-Analyse  →  Geocoding  →  Tags schreiben  →  Sortieren  →  Log  →  Bibliothek
```

Neue Dateien im Eingangsverzeichnis werden automatisch erkannt und durchlaufen eine 11-stufige Pipeline:

| Step | Name | Beschreibung |
|------|------|-------------|
| IA-01 | EXIF auslesen | Metadaten via ExifTool extrahieren |
| IA-02 | Formatkonvertierung | HEIC/DNG/RAW/GIF → temp JPEG für KI-Analyse |
| IA-03 | KI-Analyse | Bild analysieren (Typ, Tags, Beschreibung, Stimmung) |
| IA-04 | OCR | Texterkennung (Screenshots, Dokumente) |
| IA-05 | Duplikaterkennung | Perceptual Hash Vergleich |
| IA-06 | Geocoding | GPS-Koordinaten → Ort, Land, Stadt |
| IA-07 | EXIF Tags schreiben | Tags und Beschreibung in Datei zurückschreiben |
| IA-08 | Sortieren | Datei in Bibliothek nach Typ/Datum einsortieren |
| IA-09 | Benachrichtigung | E-Mail bei Fehlern (SMTP, Office 365 / Gmail) |
| IA-10 | Cleanup | Temporäre Dateien entfernen |
| IA-11 | SQLite Log-Eintrag | Verarbeitungszusammenfassung loggen |

IA-09 bis IA-11 sind **Finalizer** — sie laufen immer, auch wenn ein kritischer Schritt fehlschlägt.

## Schnellstart

### Voraussetzungen

- Docker & Docker Compose
- (Optional) LM Studio oder kompatibler OpenAI-API-Server für KI-Analyse

### Installation

```bash
git clone https://git.marcohediger.ch/MediaAssistant/ma-core.git
cd ma-core
cp .env.example .env
```

`.env` anpassen:

```env
# KI-Backend (LM Studio o.ä.)
AI_BACKEND_URL=http://192.168.0.100:1234/v1
AI_MODEL=qwen/qwen3-vl-4b

# Pfade (Produktion / NAS)
INBOX_PATH=/volume1/inbox
LIBRARY_PATH=/volume1/bibliothek

# SMTP (z.B. Office 365)
SMTP_SERVER=smtp.office365.com
SMTP_PORT=587
SMTP_SSL=false
SMTP_USER=user@example.com
SMTP_PASSWORD=
SMTP_RECIPIENT=user@example.com

# Zeitzone
TZ=Europe/Zurich
```

ENV-Variablen werden beim ersten Start automatisch in die Datenbank übernommen. Danach können alle Einstellungen über das Web-Interface geändert werden.

### Starten (Produktion)

```bash
docker compose up -d
```

Web-Interface: **http://localhost:8000**

Beim ersten Start wird der Setup-Wizard angezeigt.

### Starten (Entwicklung)

```bash
docker compose -f docker-compose.dev.yml up -d
```

Hot-Reload aktiv — Code-Änderungen werden sofort übernommen.

## Web-Interface

### Dashboard
- Verarbeitungsstatistiken (Total, Erledigt, Fehler, Warteschlange)
- Modul-Status mit Health-Checks (KI-Backend, Geocoding, SMTP, Filewatcher)
- Letzte verarbeitete Dateien

### Einstellungen
- Module aktivieren/deaktivieren
- KI-Backend, Geocoding, SMTP konfigurieren
- Eingangsverzeichnisse verwalten (mit Dry-Run und Ordner-Tags)
- Bibliothek-Zielstruktur definieren

### Log-Viewer
- System-Log (Fehler, Warnungen, Info)
- Verarbeitungs-Log (Jobs mit Status und Schritt-Details)
- Filter und Suche

## Bibliothek-Struktur

Die Zielstruktur ist pro Kategorie konfigurierbar mit Platzhaltern:

| Platzhalter | Beispiel |
|-------------|----------|
| `{YYYY}` | 2026 |
| `{MM}` | 03 |
| `{DD}` | 26 |
| `{YYYY-MM}` | 2026-03 |
| `{CAMERA}` | iPhone_15_Pro |
| `{TYPE}` | personal_photo |
| `{COUNTRY}` | Schweiz |
| `{CITY}` | Ehrendingen |

Standard-Struktur:

```
/bibliothek/
├── photos/{YYYY}/{YYYY-MM}/
├── screenshots/{YYYY}/
├── whatsapp/{YYYY}/{YYYY-MM}/
├── videos/{YYYY}/{YYYY-MM}/
├── unknown/{YYYY}/
├── error/
└── duplicates/
```

## Unterstützte Formate

**Bilder:** JPG, JPEG, PNG, HEIC, HEIF, TIFF, WebP, GIF, BMP, DNG, CR2, NEF, ARW

**Videos:** MP4, MOV, AVI, MKV, M4V, 3GP

## Architektur

- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async), aiosqlite
- **Datenbank:** SQLite
- **Container:** Docker mit ExifTool, FFmpeg, libheif
- **KI:** Beliebiger OpenAI-kompatibler Vision-API-Server (z.B. LM Studio)
- **Geocoding:** Nominatim, Photon oder Google Maps API

## Konfiguration verschlüsselt

API-Keys und Passwörter werden mit Fernet (AES-128-CBC) verschlüsselt in der Datenbank gespeichert. Der Schlüssel liegt in `/app/data/.secret_key`.

## Lizenz

Privates Projekt von Marco Hediger.

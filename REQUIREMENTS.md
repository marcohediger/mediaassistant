# REQUIREMENTS — ImageAssistant

## Projektbeschreibung
Dauerhafter Docker-Service der Fotos/Videos automatisch verarbeitet:
EXIF auslesen → KI-Analyse → Tags schreiben → in Zielstruktur ablegen.
Eine Pipeline, mehrere konfigurierbare Eingangsverzeichnisse.

## Architektur

### Ordnerstruktur NAS
```
/volume1/inbox/                  ← Eingangsverzeichnisse (konfigurierbar, beliebig viele)
├── mobile/                      ← Beispiel: Handy-Sync
├── manual/                      ← Beispiel: Kamera, alte Bestände
└── [weitere beliebige Ordner]   ← frei konfigurierbar

/volume1/inbox/error/
├── IMG_1234.jpg     ← fehlgeschlagene Dateien
└── IMG_1234.log     ← Fehlergrund

/volume1/bibliothek/
├── photos/
│   └── 2025/2025-03/   ← echte Fotos, chronologisch (Jahr/Monat)
├── whatsapp/
│   └── 2025/           ← WhatsApp-Bilder separiert
├── screenshots/
│   └── 2025/           ← Screenshots separiert
└── unknown/
    └── review/         ← KI unsicher, manuell prüfen
```

### Eingangsverzeichnisse (konfigurierbar im Webinterface)
Beliebig viele Verzeichnisse konfigurierbar, pro Verzeichnis:
- Pfad (absolut)
- Name/Label (z.B. "Handy", "Kamera", "Archiv 2010")
- Ordner-Tags ja/nein (Unterordner als EXIF-Keywords übernehmen)
- Aktiv/inaktiv Toggle
- Eigene Verarbeitungszeiten (oder globale Einstellung übernehmen)
- Dry-Run Toggle (nur Report, keine Dateien verschieben)

Beispiel:
| Pfad | Label | Ordner-Tags | Dry-Run | Aktiv |
|---|---|---|---|---|
| /inbox/mobile/ | Handy | Nein | ❌ | ✅ |
| /inbox/manual/ | Kamera & Archiv | Ja | ❌ | ✅ |
| /photos/old/ | Migration | Ja | ✅ | ✅ |

### Komponenten
- **Python + Watchdog** — Filewatcher auf konfigurierbaren Eingangsverzeichnissen
- **ExifTool** (via subprocess) — EXIF lesen und Tags/Keywords schreiben
- **LM Studio API** (OpenAI-kompatibler Endpunkt, konfigurierbar) — Vision-Modell für Bildanalyse
- **FastAPI** — Webinterface für Status, Logs, manuelle Auslösung
- **SQLite** — Verarbeitungs-Log (Datei, Status, Zeitstempel, Tags)
- **SMTP** — Fehlerbenachrichtigung per Mail

### Deployment
- Docker Container auf NAS/Server (Produktion)
- Windows Docker Desktop (Entwicklung + Test)
- Container muss in beiden Umgebungen identisch laufen
- Pfade im Container immer mit `/` (Linux-Style) — Windows Volume-Mounts via Docker Desktop kompatibel
- Zwei docker-compose Varianten:
  - `docker-compose.yml` → Produktion (NAS/Server, absolute Pfade)
  - `docker-compose.dev.yml` → Entwicklung (Windows, relative Pfade)
- Alle Pfade über Umgebungsvariablen konfigurierbar — kein hardcodierter Pfad im Code
- Entwicklung auf Windows, Deploy auf Synology ohne Code-Änderung

## Modi

### Modus 1 — Pipeline (Filewatcher)
Dauerhafter Service, watched alle konfigurierten Eingangsverzeichnisse, verarbeitet neue Dateien automatisch.

**Migration** läuft über ein normales Eingangsverzeichnis das auf die bestehende Bibliothek zeigt:
- Bestehende Fotobibliothek als Eingangsverzeichnis konfigurieren (z.B. Label: "Migration")
- Dry-Run Modus: nur Report, keine Dateien verschieben (pro Verzeichnis auslösbar)
- HTML-Report nach Dry-Run: Anzahl Dateien, Kategorien, erkannte Duplikate, Fehler
- Nach Review: produktiv laufen lassen

### Modus 2 — Immich Integration
Bidirektionale Verbindung mit Immich via REST API:

**3a — Trigger-basierte Anreicherung:**
```
Tag "ia-process" in Immich vergeben
     ↓
ImageAssistant pollt Immich API (konfigurierbar, z.B. alle 5 Min)
→ Asset herunterladen
→ Kompletter Flow: EXIF, KI, OCR, Geocoding
→ Ergebnisse via ExifTool direkt in Originaldatei schreiben
→ Immich API: Asset rescan triggern → DB aktualisiert
→ Tag "ia-process" → "ia-done"
```

**3b — Metadaten-Sync (Immich DB → Originaldateien):**
```
Alle Assets in Immich die Tags/Keywords in DB haben aber nicht in Datei
     ↓
ImageAssistant holt Metadaten via Immich API
→ ExifTool schreibt direkt in Originaldatei
→ Immich rescan triggern
→ Originaldateien sind nun selbst-beschreibend (unabhängig von Immich DB)
```

Verfügbare Trigger-Tags in Immich:
| Tag | Aktion |
|---|---|
| `ia-process` | Kompletter Flow (KI, OCR, Geocoding, EXIF schreiben) |
| `ia-location` | Nur Ortschätzung (Google Vision / GeoCLIP) |
| `ia-ocr` | Nur OCR nochmal laufen lassen |
| `ia-sync` | Immich DB Metadaten → Originaldatei schreiben |

- Immich API URL + API-Key konfigurierbar im Webinterface
- Poll-Intervall konfigurierbar
- Kein Verschieben der Dateien (Immich behält Datei-Hoheit)
- Nach ExifTool-Write: Immich rescan automatisch getriggert



```
Neue Datei in /inbox/mobile/ oder /inbox/manual/
        ↓
 1. EXIF auslesen (ExifTool)
    - Make, Model, DateTimeOriginal, GPS, Software
        ↓
 2. Duplikaterkennung (vor KI — spart teure Analyse)
    - SHA256 Hash (exakt) + pHash (ähnlich)
    - Duplikat → Pipeline stoppt, Datei → /error/duplicates/
        ↓
 3. Geocoding
    - GPS-Koordinaten → Ort, Land, Stadt
        ↓
 4. Temp. Konvertierung für KI
    - HEIC/DNG/RAW/GIF → temp JPEG für KI-Analyse
    - JPG/PNG/WebP → direkt, keine Konvertierung
        ↓
 5. KI-Analyse (LM Studio Vision)
    - Typ: personal / screenshot / internet_image / document / meme
    - Inhalt: Personen, Landschaft, Essen, Dokument, Tier, etc.
    - Stimmung: indoor / outdoor / nacht / gegenlicht
    - Personenanzahl (keine Namen)
    - Qualität: unscharf / gut / sehr gut
    - Beschreibung: Freitext (1-2 Sätze)
        ↓
 6. OCR — Texterkennung
    - Screenshots, Dokumente, Schilder, Whiteboards
        ↓
 7. EXIF Tags schreiben (ExifTool, overwrite_original)
    - Keywords: Typ, Inhalt-Tags, Qualität, Ort, Ordner-Tags
    - ImageDescription: KI-Freitext + Ort
        ↓
 8. Sortieren — Zielordner bestimmen + Datei sicher verschieben
    - photos/YYYY/YYYY-MM/    (personal, Datum aus EXIF)
    - sourceless/YYYY/        (Bilder ohne EXIF / Messenger)
    - screenshots/YYYY/       (screenshot)
    - unknown/review/         (KI-Konfidenz < Schwellwert oder unklar)
    - Sichere Verschiebung: Kopie → SHA256-Verifikation → Original löschen
    - Leere Quellordner nach Verschiebung automatisch aufräumen
        ↓
 9. Benachrichtigung
    - E-Mail bei Fehlern (SMTP, zusammengefasst)
        ↓
10. Cleanup
    - Temporäre Dateien (temp JPEG etc.) entfernen
        ↓
11. SQLite Log-Eintrag
    - Verarbeitungszusammenfassung loggen
```

## Job-System (SQLite)

Jede Datei erhält so früh wie möglich einen SQLite-Eintrag — bereits beim Erkennen durch den Filewatcher oder API-Polling. So ist jederzeit nachvollziehbar in welchem Schritt eine Datei ist, und bei Fehler kann ab dem fehlgeschlagenen Step wiedereingestiegen werden.

### Schema
```sql
id              INTEGER PRIMARY KEY
filename        TEXT
original_path   TEXT
target_path     TEXT        -- wird gesetzt wenn bekannt
debug_key       TEXT        -- IA-YYYY-NNNN
status          TEXT        -- queued / processing / done / error / duplicate / review
current_step    TEXT        -- IA-01 bis IA-11, null wenn queued
step_result     JSON        -- Ergebnisse pro Step
error_message   TEXT        -- Fehlermeldung wenn status=error
source_label    TEXT        -- Label des Eingangsverzeichnisses
source_inbox_path TEXT      -- Inbox-Basispfad (für Ordner-Tags)
file_hash       TEXT        -- SHA256 Hash (Original, wird nie überschrieben)
phash           TEXT        -- Perceptual Hash (für Ähnlichkeitserkennung)
created_at      DATETIME
updated_at      DATETIME
completed_at    DATETIME
```

### step_result JSON
```json
{
  "IA-01": {"make": "Apple", "model": "iPhone15", "date": "2024-06-12", "gps": true, "has_exif": true},
  "IA-02": {"status": "ok", "phash": "b38e33e05c686733"},  // oder {"status": "duplicate", "match_type": "exact|similar", "original_debug_key": "MA-2026-0001"}
  "IA-03": {"country": "Schweiz", "city": "Zürich", "suburb": "Altstadt", "provider": "nominatim"},
  "IA-04": {"converted": true, "temp_path": "/tmp/IA-2025-0342.tmp.jpg"},
  "IA-05": {"type": "personal_photo", "tags": ["Zürich", "outdoor"], "quality": "gut", "confidence": 0.95},
  "IA-06": {"has_text": false, "text": "", "text_type": "keiner"},
  "IA-07": {"keywords_written": ["Zürich", "outdoor", "personal_photo"], "tags_count": 3, "file_size": 2458901, "file_hash": "a1b2c3..."},
  "IA-08": {"category": "photo", "target_path": "/bibliothek/photos/2024/2024-06/IMG_1234.jpg", "moved": true},
  "IA-09": {"sent": true, "recipient": "user@example.com", "errors_reported": 0},
  "IA-10": {"removed": ["/tmp/IA-2025-0342.tmp.jpg"], "count": 1},
  "IA-11": {"logged": true, "summary": "personal_photo, 3 Tags, Zürich/Schweiz"}
}
```

### Status-Werte
| Status | Bedeutung |
|---|---|
| `queued` | Erkannt, wartet auf Verarbeitung |
| `processing` | Läuft gerade (current_step zeigt wo) |
| `done` | Komplett fertig, in Bibliothek |
| `error` | Fehlgeschlagen, in /inbox/error/ |
| `duplicate` | Duplikat erkannt, wartet auf Review |
| `review` | KI unsicher, wartet auf manuelle Prüfung |

### Resume-Logik
```python
# Beim Start / nach Absturz:
# Alle Jobs mit status="processing" oder status="error" prüfen
# step_result JSON → welche Steps haben Ergebnis? → überspringen
# Ab erstem fehlendem Step weitermachen
```

- Bei Migration: Fortschritt bleibt erhalten auch nach Neustart
- Bei LM Studio Timeout: nur IA-05 wiederholen, nicht von vorne
- Im Webinterface: "Ab diesem Step wiederholen" Button pro Job
- Live-Ansicht: aktueller Step aller laufenden Jobs


Zweistufige Erkennung, läuft vor der KI-Analyse:

```
Neue Datei
     ↓
1. SHA256 Hash → exakter Treffer in SQLite?
   ja → Duplikat, sofort aussortieren
     ↓
2. Perceptual Hash (imagehash) → ähnliches Bild (Schwellwert konfigurierbar)?
   ja → "ähnliches Bild" flaggen (z.B. WA-Komprimat vom Original)
     ↓
   nein → normal weiterverarbeiten
```

Duplikate landen in:
```
/inbox/error/duplicates/
├── IMG_1234.jpg       ← Duplikat
└── IMG_1234.log       ← "Exaktes Duplikat von: /bibliothek/photos/2023/..."
                          oder "Ähnlich zu: /bibliothek/photos/2023/... (Score: 0.97)"
```

- SHA256 Hash wird beim ersten Import in SQLite gespeichert
- Perceptual Hash (pHash) ebenfalls in SQLite gespeichert
- Schwellwert für "ähnlich" konfigurierbar in config.yml
- Bibliothek: `imagehash` (Python, kein KI nötig)

### Duplikat-Review Webinterface
Eigene Seite im Webinterface zum Reviewen und Löschen von Duplikaten:

- Alle Dateien einer Gruppe gleichwertig nebeneinander (transitive Gruppierung via Union-Find)
- Pro Datei: Vorschaubild (HEIC via heif-convert), Dateigrösse, Auflösung, Megapixel
- Pro Datei: Alle EXIF-Daten direkt aus Datei gelesen (Datum, Kamera, ISO, Blende, Verschlusszeit, Brennweite, GPS)
- Pro Datei: Alle Keywords/Tags aus Datei angezeigt (AI-Tags, Geo, OCR, Folder-Tags)
- Pro Datei: Beschreibung aus Datei angezeigt
- Aktionen pro Datei: "Dieses behalten" (verschiebt in Bibliothek, löscht alle anderen)
- Ähnlichkeits-Score pro Datei anzeigen (SHA256 exakt / pHash %)
- Batch-Clean: alle exakten SHA256 Duplikate ohne Review automatisch löschen
- Anzahl offener Duplikat-Gruppen im Dashboard anzeigen
- Dateinamen-Kollision: automatischer Index (_1, _2, ...) bei gleichem Namen im Zielordner

## Logging & Debug

### Debugschlüssel
Jede Dateiverarbeitung erhält einen eindeutigen Schlüssel: `IA-YYYY-NNNN` (fortlaufend).
Jeder Verarbeitungsschritt wird mit einem Step-Code geloggt:

| Code | Schritt |
|---|---|
| IA-01 | EXIF auslesen |
| IA-02 | Duplikaterkennung (SHA256 + pHash) |
| IA-03 | Geocoding |
| IA-04 | Temp. Konvertierung für KI (HEIC/DNG/RAW/GIF → JPEG) |
| IA-05 | KI-Analyse |
| IA-06 | OCR (Texterkennung) |
| IA-07 | EXIF Tags schreiben |
| IA-08 | Sortieren (Zielordner + verschieben) |
| IA-09 | Benachrichtigung |
| IA-10 | Cleanup (temp Dateien) |
| IA-11 | SQLite Log-Eintrag |

Log-Format pro Datei:
```
2025-03-20 14:32:01 | IA-2025-0342 | IMG_1234.heic
  [IA-01] EXIF auslesen        ✓ Make=Apple, DateTimeOriginal=2024-06-12
  [IA-02] Duplikaterkennung    ✓ OK
  [IA-03] Geocoding            ✓ Zürich, Schweiz
  [IA-04] Konvertierung für KI ✓ temp JPEG erstellt
  [IA-05] KI-Analyse           ✗ FEHLER: LM Studio Timeout nach 30s
  [IA-10] Cleanup temp JPEG    ✓
  → Datei nach /inbox/error/ verschoben
  → Fehlermail: "IA-2025-0342 Fehler bei [IA-05] KI-Analyse: Timeout"
```

- Debugschlüssel wird in SQLite gespeichert → im Webinterface suchbar
- Fehlermail enthält immer den Schlüssel + betroffenen Step-Code
- Bei Open-Source: Nutzer können Schlüssel + Step-Code im GitHub Issue angeben
- Log-Level konfigurierbar in config.yml (DEBUG / INFO / ERROR)


### Sichere Dateiverschiebung (safe_move)
Dateien dürfen **niemals** verloren gehen. Jede Verschiebung im System (Sortierung, Error-Ordner, Retry) ist ein dreistufiger Prozess:

1. **Kopieren** — `shutil.copy2` (mit Metadaten)
2. **Verifizieren** — Dateigrösse + SHA256-Hash der Kopie mit Original vergleichen
3. **Löschen** — Original wird erst nach erfolgreicher Verifikation gelöscht

Bei fehlgeschlagener Verifikation:
- Defekte Kopie wird entfernt
- Original bleibt unangetastet
- Fehler wird im System-Log dokumentiert
- Pipeline bricht mit Fehler ab

Jede Dateiverschiebung wird im System-Log dokumentiert mit:
- Dateiname, Dateigrösse, SHA256-Hash (gekürzt)
- Quell- und Zielpfad

Anwendungsorte:
- IA-08: Sortierung (Inbox → Bibliothek)
- Error-Handling: fehlgeschlagene Dateien → /error/
- Retry: Dateien aus /error/ zurück in Inbox

### Fehlerbehandlung
- Bei Fehler → Datei sofort nach /inbox/error/ verschieben (vor Finalizern)
- Logfile (gleiches Verzeichnis, gleicher Name + .log) mit Fehlerdetails
- SMTP Mail mit Fehlerübersicht (max. 1 Mail pro Stunde zusammengefasst)
- Retry-Button im Webinterface (Job-Detail Seite)
- Löschen-Button im Webinterface (Job + Datei endgültig entfernen)

## Migration (Einmalig)
- Gleiche Pipeline wie oben, aber Quelle: /volume1/photo/ (Synology Photos)
- Dry-Run Modus: nur Report, keine Dateien verschieben
- Batch-Modus: Ordner für Ordner verarbeitbar
- HTML-Report pro Lauf: Anzahl Dateien, Kategorien, Fehler

## Geocoding
GPS-Koordinaten aus EXIF werden in lesbare Ortsnamen umgewandelt und als Keywords gespeichert.

### Provider (wählbar im Webinterface)
Einheitliche interne Schnittstelle — Output immer gleich egal welcher Provider:

| Provider | Typ | Rate-Limit | API-Key |
|---|---|---|---|
| Nominatim (OpenStreetMap) | Public / Self-hosted | 1 req/s public, keins self-hosted | Nein |
| Photon (OpenStreetMap) | Self-hosted | Keins | Nein |
| Google Maps Geocoding API | Cloud | Hoch (kostenpflichtig ab Volumen) | Ja |

- URL konfigurierbar (für eigene Nominatim/Photon Instanzen)
- API-Key konfigurierbar (nur Google)
- Test-Button im Webinterface (Testkoordinate → Ergebnis anzeigen)
- Empfehlung für Migration: eigener Photon-Container (schlank, kein Rate-Limit)
- Photon-Container optional in docker-compose.yml mitgeliefert

### Output pro Foto (falls GPS vorhanden):
```
EXIF GPS: 47.3769° N, 8.5417° E
     ↓
Keywords: ["Schweiz", "Zürich", "Altstadt"]
EXIF ImageDescription: "... aufgenommen in Altstadt, Zürich, Schweiz"
```

Felder die extrahiert werden:
- `country` → Land (z.B. "Schweiz")
- `state` → Kanton/Bundesland (z.B. "Zürich")
- `city` → Stadt (z.B. "Zürich")
- `suburb` → Quartier/Ortsteil (z.B. "Altstadt") falls verfügbar

- Geocoding-Ergebnis wird in SQLite gecacht → gleiche Koordinaten werden nicht doppelt abgefragt
- Fotos ohne GPS → Geocoding wird übersprungen

### Ablage-Ordnerstruktur Erweiterung
Geocoding-Platzhalter zusätzlich verfügbar:
- `{COUNTRY}` — Land (z.B. "Schweiz")
- `{CITY}` — Stadt (z.B. "Zuerich")

Beispiel: `photos/{YYYY}/{CITY}/` → `photos/2024/Zuerich/`

## Webinterface (FastAPI)
- Dashboard: Anzahl verarbeitete Dateien heute/total, Fehler
- Live-Log: letzte Verarbeitungen
- Queue-Status: wie viele Dateien warten noch
- Manueller Trigger: einzelne Datei oder ganzen Ordner verarbeiten
- Migration starten/stoppen mit Dry-Run Option

### Setup-Wizard (erster Start)
Beim ersten Start wird automatisch auf `/setup` weitergeleitet — kein manuelles config.yml nötig.
Schritte:
1. KI Backend (URL, API-Key, Modell, Test)
2. SMTP (Server, Port, SSL, User, Passwort, Test)
3. Pfade (Inbox Mobile, Inbox Manual, Bibliothek)
4. Fertig → Dashboard

Nach abgeschlossenem Setup wird `/setup` gesperrt (nur via Reset wieder zugänglich).
Onboarding analog paperless-ai: `docker run -d -p 3000:3000 imageassistant` → `http://your-instance/setup`

### AI Playground
Eigene Seite zum live Testen von Prompts:
- Bild hochladen oder aus Bibliothek wählen
- Klassifizierungs-Prompt und Inhalts-Prompt editieren
- KI-Antwort direkt anzeigen (Typ, Tags, Beschreibung, Qualität)
- "Prompt übernehmen" Button → speichert direkt in Einstellungen
- Hilfreich beim Finetuning des Prompts für die eigene Bibliothek

### Einstellungen — Module
Alle Module einzeln aktivierbar/deaktivierbar im Webinterface:

| Modul | Standard | Wenn deaktiviert |
|---|---|---|
| KI-Analyse | ✅ an | Bilder ohne EXIF-Match → /unknown/review/ |
| Geocoding | ✅ an | Kein Orts-Tag, GPS bleibt als Koordinate |
| Duplikat-Erkennung | ✅ an | Alle Dateien durchlassen |
| OCR | ✅ an | Kein Text-Tag |
| SMTP Benachrichtigung | ✅ an | Nur Logfile, keine Mail |
| Filewatcher | ✅ an | Nur manueller Trigger |

Ordner-Tags werden pro Eingangsverzeichnis konfiguriert (nicht als globales Modul).

- Toggle pro Modul im Webinterface (Einstellungen → Module)
- Status aller Module im Dashboard sichtbar
- Nützlich zum schrittweisen Testen: erst ohne KI, dann Module einzeln zuschalten

### Einstellungen — Verarbeitungszeiten
Konfigurierbar im Webinterface, gespeichert in SQLite:

| Modus | Beschreibung | Beispiel |
|---|---|---|
| Kontinuierlich | 24/7, alle X Minuten | alle 5 Min |
| Zeitfenster | Nur zwischen Uhrzeit A und B | 22:00 - 06:00 |
| Geplant | Bestimmte Tage + Uhrzeit | Mo-Fr 23:00 |
| Manuell | Nur auf Knopfdruck im Webinterface | — |

- Separate Einstellung pro Eingangskanal (mobile, manual, Synology, Immich)
- Zeitzone konfigurierbar
- "Jetzt ausführen" Button im Dashboard unabhängig vom Zeitplan
- Bei aktivem Zeitfenster: eingehende Dateien werden gequeued und beim nächsten Fenster verarbeitet
- Nächste geplante Ausführung im Dashboard anzeigen

### Einstellungen — Ablage-Ordnerstruktur
Zielordner-Schema konfigurierbar im Webinterface pro Kategorie:

| Kategorie | Standard-Schema | Beispiel |
|---|---|---|
| personal | `photos/{YYYY}/{YYYY-MM}/` | `photos/2024/2024-06/` |
| sourceless | `sourceless/{YYYY}/` | `sourceless/2024/` |
| screenshot | `screenshots/{YYYY}/` | `screenshots/2024/` |
| video | `videos/{YYYY}/{YYYY-MM}/` | `videos/2024/2024-06/` |
| unknown | `unknown/review/` | `unknown/review/` |
| error | `error/` | `error/` |
| duplicate | `error/duplicates/` | `error/duplicates/` |

Verfügbare Platzhalter:
- `{YYYY}` — Jahr (aus EXIF DateTimeOriginal)
- `{MM}` — Monat (zweistellig)
- `{DD}` — Tag (zweistellig)
- `{CAMERA}` — Kamera/Gerät (aus EXIF Make+Model, z.B. "Apple-iPhone15")
- `{TYPE}` — KI-Klassifizierung (personal, screenshot, internet_image, document, meme)
- `{COUNTRY}` — Land (z.B. "Schweiz")
- `{CITY}` — Stadt (z.B. "Zuerich")
- `{YEAR-MONTH}` — kombiniert, z.B. "2024-06"

Beispiel-Schemas:
```
Nach Jahr/Monat:     photos/{YYYY}/{YYYY-MM}/
Nach Kamera:         photos/{YYYY}/{CAMERA}/
Nach Tag:            photos/{YYYY}/{MM}/{DD}/
Flach:               photos/{YYYY}/
```

- Schema wird in SQLite gespeichert
- Vorschau im Webinterface zeigt Beispielpfad live beim Tippen
- Änderung gilt nur für neue Dateien, bestehende Bibliothek bleibt unverändert

### Einstellungen — SMTP
- SMTP Server, Port, SSL/TLS, Benutzername, Passwort editierbar im Webinterface
- Empfänger-Adresse konfigurierbar
- Gespeichert in SQLite (verschlüsselt für Passwort)
- Test-Button: Test-Mail verschicken zur Verifikation
- Zusammenfassung konfigurierbar: max. 1 Mail pro X Minuten (Standard: 60)

### Einstellungen — KI Backend
- Backend-URL konfigurierbar im Webinterface
- API-Key konfigurierbar (optional, für OpenAI oder andere Cloud-Dienste)
- Modell-Name konfigurierbar (z.B. "gpt-4o", "llava", "qwen2-vl")
- **KI-Analyse deaktivierbar** (Toggle im Webinterface)
  - Wenn deaktiviert: nur EXIF, Regelklassifizierung, Ordner-Tags, Geocoding, Duplikat-Erkennung
  - Bilder die normalerweise zur KI gehen → landen in /unknown/review/
  - Nützlich zum Testen der Pipeline ohne KI-Abhängigkeit
- Kompatibel mit allen OpenAI-kompatiblen Endpunkten:
  - LM Studio (lokal)
  - Ollama (lokal)
  - OpenAI API (Cloud)
  - Anthropic Claude API (Cloud)
  - Groq, Together AI, etc.
- Test-Button: Verbindung prüfen + Modell-Liste abrufen falls verfügbar
- Gespeichert in SQLite (API-Key verschlüsselt)

### Einstellungen — KI Prompts
- System-Prompt editierbar im Webinterface (Settings → AI Analysis)
- Prompt in Englisch, KI-Antwort (Tags/Description) auf Deutsch
- Typen: personal / screenshot / internet_image / document / meme
- Prompt wird in SQLite gespeichert, Default-Prompt als Fallback wenn leer
- Striktere Screenshot-Erkennung (muss OS-UI-Elemente wie Statusbar haben)

### Einstellungen — Sortier-Regeln
- Regel-Liste editierbar im Webinterface
- Reihenfolge per Drag-and-Drop änderbar (erste Regel die matcht gewinnt)
- Pro Regel konfigurierbar:
  - Bedingung: Dateiname enthält / EXIF-Feld leer / EXIF-Feld enthält / Dateierweiterung
  - Wert: z.B. "-WA", "Screenshot", "Apple"
  - Aktion: → whatsapp / screenshot / photo / unknown
- Standard-Regeln beim ersten Start:
  1. Dateiname enthält "-WA" → whatsapp
  2. Dateiname enthält "Screenshot" → screenshot
  3. EXIF Make + DateTimeOriginal vorhanden → photo
  4. EXIF komplett leer → unknown (→ KI-Analyse)
- Regeln werden in SQLite gespeichert

## Tasks

### Erledigt ✅
- [x] SETUP: Projektstruktur anlegen (backend, docker-compose, volumes)
- [x] DOCKER: docker-compose.yml mit Volumes und Umgebungsvariablen
- [x] FEAT: Dateiformat-Handling (HEIC/DNG/GIF → temp JPEG vor KI-Analyse, IA-02)
- [x] FEAT: EXIF-Auslesen via ExifTool subprocess (IA-01)
- [x] FEAT: Duplikat-Erkennung SHA256 (exakt) + pHash (ähnlich) via imagehash (IA-03)
- [x] FEAT: Duplikate → /error/duplicates/ + .log mit Verweis auf Original
- [x] FEAT: Duplikat-Review Webinterface (gruppiert, Side-by-Side, Batch-Löschen, EXIF/Tags direkt aus Datei)
- [x] FEAT: LM Studio Vision API Integration (IA-05)
- [x] FEAT: KI-Prompt für Typ + Inhalt + Qualität + Beschreibung (IA-05)
- [x] FEAT: EXIF-Tags schreiben via ExifTool (IA-07)
- [x] FEAT: Manuelle Imports — Ordnerstruktur als Tags (jede Ebene = ein EXIF-Keyword, pro Verzeichnis konfigurierbar)
- [x] FEAT: Zielstruktur-Logik (Ordner bestimmen, Datei verschieben, leere Ordner aufräumen, IA-08)
- [x] FEAT: Geocoding Provider-Schnittstelle (Nominatim / Photon / Google Maps, einheitlicher Output, IA-04)
- [x] FEAT: Alle Geocoding-Felder als EXIF-Keywords (country, state, city, suburb)
- [x] FEAT: SMTP Fehlerbenachrichtigung (IA-09, STARTTLS/Office 365 Support)
- [x] FEAT: Fehlerbehandlung → /error/ + .log Datei + Retry-Button + Löschen-Button
- [x] FEAT: Sichere Dateiverschiebung (safe_move: Copy → SHA256-Verify → Delete, kein Datenverlust)
- [x] FEAT: Debugschlüssel (MA-YYYY-NNNN) + Step-Codes (IA-01 bis IA-11) pro Verarbeitung
- [x] FEAT: Job-System (SQLite, Eintrag bei Erkennung, Status + current_step + step_result JSON)
- [x] FEAT: Resume-Logik (nach Absturz ab fehlendem Step weitermachen)
- [x] FEAT: SQLite Logging (System-Log + Verarbeitungs-Log)
- [x] FEAT: Eingangsverzeichnisse konfigurierbar im Webinterface (Pfad, Label, Ordner-Tags, Dry-Run, Aktiv)
- [x] FEAT: Filewatcher (Polling) auf allen konfigurierten Eingangsverzeichnissen
- [x] FEAT: Setup-Wizard beim ersten Start (/setup, 4 Schritte, danach gesperrt)
- [x] FEAT: FastAPI Webinterface (Dashboard, Live-Log, Queue, Job-Detail mit Auto-Refresh)
- [x] FEAT: Alle Module einzeln ein/ausschaltbar im Webinterface (KI, Geocoding, Duplikat, OCR, SMTP, Filewatcher)
- [x] FEAT: Webinterface — KI Backend konfigurierbar (URL, API-Key, Modell)
- [x] FEAT: Webinterface — SMTP Konfiguration (Server, Port, SSL, User, Passwort)
- [x] FEAT: Webinterface — KI Prompt editierbar (gespeichert in SQLite, Standard-Prompt als Fallback)
- [x] FEAT: Webinterface — Ablage-Ordnerstruktur konfigurierbar (Schema pro Kategorie, Platzhalter)
- [x] FEAT: Webinterface — OCR-Modus konfigurierbar (smart / always)
- [x] FEAT: Webinterface — Filewatcher-Modus konfigurierbar (continuous / window / scheduled / manual)
- [x] FEAT: Webinterface — pHash Schwellwert konfigurierbar
- [x] FEAT: KI-Prompt auf Englisch (bessere LLM-Verarbeitung), Tags/Description auf Deutsch
- [x] FEAT: KI-Typen überarbeitet (personal statt personal_photo, kein whatsapp, striktere Screenshot-Erkennung)
- [x] FEAT: i18n — Vollständige Mehrsprachigkeit (DE/EN) via JSON-Sprachdateien
- [x] FEAT: Theme — Dark/Light-Modus umschaltbar (CSS-Variablen, conditional CSS)
- [x] FEAT: Sprache und Theme konfigurierbar in Einstellungen → Darstellung
- [x] FEAT: Alle System-Log-Meldungen immer auf Englisch (unabhängig von UI-Sprache)
- [x] FEAT: Geocoding-Platzhalter in Ordnerstruktur ({COUNTRY}, {CITY})
- [x] FEAT: Leere Quellordner nach Import automatisch aufräumen (bis Inbox-Root)
- [x] FEAT: Dry-Run Modus pro Inbox (Pipeline analysiert aber verschiebt/schreibt nicht, zeigt geplanten Zielpfad)
- [x] FEAT: Filewatcher queued korrekt alle Dateien vor Verarbeitung (Phase 1: create, Phase 2: process)
- [x] FEAT: Verwaiste DB-Einträge bei Duplikaterkennung — wenn Original-Datei gelöscht wurde, wird Match übersprungen und neue Datei als Original behandelt

### Offen
- [x] FEAT: Lightbox — Klick auf Thumbnail öffnet Originalbild als Fullscreen-Overlay (Review, Duplikate, Log-Detail); RAW/DNG via ExifTool PreviewImage oder Immich Preview, HEIC → JPEG
- [x] FEAT: Review-Seite — Löschen-Button, Dateigrösse (Immich API Fallback), Datum-Fallback (FileModifyDate/created_at), Dimensionen, bedingte Metadatenfelder
- [x] FEAT: Duplikat-Review — EXIF via Immich API, "Dieses behalten" auf allen Mitgliedern, Badge als klickbarer Link, Keep → Immich Upload, httpx DELETE Fix
- [x] FEAT: Filewatcher Stabilitätscheck — Dateigrösse nach 2s Wartezeit prüfen (halbkopierte Dateien)
- [x] FEAT: IA-07 ExifTool `-m` Flag für Minor Warnings (DJI DNG "Maker notes")
- [x] FEAT: IA-01 speichert file_size, Fallback auf FileModifyDate
- [x] FIX: httpx DELETE — `client.request` mit `content=` statt `json=`
- [ ] FEAT: Video-Metadaten auslesen via ffprobe (Datum, GPS, Dauer, Auflösung)
- [ ] FEAT: Video-Thumbnail Extraktion via ffmpeg für KI-Analyse (vorbereiten, deaktiviert)
- [ ] FEAT: AI Playground (Bild hochladen, Prompt testen, live Antwort, übernehmen)
- [ ] DOCKER: Photon-Container optional in docker-compose.yml

### Optional (v2)
- [ ] SSO Login via OIDC (fastapi-sso + sso.marcohediger.ch)
- [ ] Video KI-Analyse (Thumbnail → LM Studio Vision)
- [ ] KI-basierte Ortschätzung für Bilder ohne GPS (Vision → "estimated_location" Tag)
- [ ] GeoCLIP für präzise GPS-Schätzung ohne GPS-EXIF
- [ ] Google Vision API Landmark Detection (für alte Fotos ohne GPS)
- [ ] Geocoding-Cache in SQLite (Koordinaten-Lookup cachen, relevant bei Migration/Google API)
- [ ] HTML-Report nach Dry-Run (Anzahl Dateien, Kategorien, Duplikate, Fehler)
- [ ] config.example.yml (alle Optionen mit Kommentaren)
- [ ] docs/ (installation.md, configuration.md, migration.md)
- [ ] GitHub Actions Workflow → automatischer Docker Hub Build

## Dateiformat-Unterstützung

LM Studio Vision erwartet JPEG/PNG — andere Formate werden vor der KI-Analyse
temporär konvertiert. Original bleibt immer erhalten.
Alle Dateiverschiebungen nutzen safe_move (Copy → SHA256-Verify → Delete) — kein Datenverlust möglich.

| Format | Quelle | Handling |
|---|---|---|
| `.jpg/.jpeg` | Alle Kameras | Direkt |
| `.heic/.heif` | iPhone (modern) | → temp JPEG via libheif/ImageMagick |
| `.png` | Screenshots | Direkt |
| `.dng` | iPhone RAW | → temp JPEG via ImageMagick |
| `.webp` | Internet/WhatsApp | Direkt |
| `.gif` | Internet/WhatsApp | Erstes Frame → temp JPEG |
| `.mp4/.mov` | Videos | Metadaten via ffprobe, Thumbnail optional (v2) |
| `.m4v` | iTunes/Apple Videos | Metadaten via ffprobe |
| `.avi/.mkv` | ältere Kameras | Metadaten via ffprobe |
| `.3gp` | ältere Handys/WhatsApp | Metadaten via ffprobe |
| `.mts/.m2ts` | Videokameras | Metadaten via ffprobe |

- Temporäre JPEG-Dateien werden nach KI-Analyse sofort gelöscht
- Videos: Metadaten via ffprobe (Datum, GPS, Dauer, Auflösung, Gerät)
- Videos: kein KI-Analyse in v1 — nur sortieren nach Metadaten
- Videos: Thumbnail-Extraktion via ffmpeg vorbereitet aber deaktiviert (v2)
- ExifTool liest alle Formate nativ (keine Konvertierung für EXIF-Analyse nötig)
- Nicht unterstützte Formate → /inbox/error/ mit Hinweis im Log
- Unterstützte Formate konfigurierbar in config.yml

## Nicht umsetzen (v1)
- Keine Authentifizierung im Webinterface (siehe Optional)
- Keine Cloud-Anbindung
- Keine Video-Analyse (nur Thumbnails/Metadaten)
- Kein n8n-Workflow
- ~~Keine Immich Integration~~ → **umgesetzt in v1.1.0–v1.5.0**

## Optional (v2)
### ~~Immich Integration (Modus 2)~~ ✅ Umgesetzt
Bidirektionale Verbindung mit Immich via REST API.
- ✅ Upload: Inbox-Dateien nach Immich (pro Inbox konfigurierbar)
- ✅ Polling: Neue Immich-Uploads (z.B. Mobile App) automatisch verarbeiten
- ✅ Tags/Beschreibung/Geodaten via EXIF in Datei schreiben, Asset in Immich ersetzen
- ✅ Album-Erstellung aus Ordner-Tags
- ✅ Duplikaterkennung mit Immich (Side-by-Side Review, Thumbnail aus Immich API)
- ✅ Orphaned Asset Handling (gelöschte Immich-Assets werden übersprungen)
- ✅ API-Key + URL im Webinterface konfigurierbar
- ✅ Poll-Toggle im Webinterface, Intervall = Filewatcher-Intervall
- ❌ Trigger-Tags (ia-process, ia-location, ia-ocr, ia-sync) — nicht umgesetzt
- ❌ Immich DB Metadaten → Originaldateien zurückschreiben — nicht umgesetzt

### SSO Login (OIDC)
- Provider: sso.marcohediger.ch (Synology SSO Server)
- Bibliothek: `fastapi-sso` (OIDC fertig eingebaut)
- OIDC Client in Synology SSO registrieren (analog Paperless-ngx, Open WebUI etc.)
- Alle FastAPI Endpoints hinter Login absichern
- Nur sinnvoll wenn Webinterface von aussen erreichbar gemacht wird

### Open-Source Veröffentlichung (GitHub)
Repository-Struktur:
```
imageassistant/
├── README.md                  ← Projektbeschreibung, Features, Screenshots
├── REQUIREMENTS.md            ← dieses Dokument
├── docker-compose.yml         ← production-ready
├── docker-compose.dev.yml     ← development setup
├── config.example.yml         ← Vorlage mit allen Optionen + Kommentaren
├── backend/
│   ├── Dockerfile
│   └── ...
├── docs/
│   ├── installation.md
│   ├── configuration.md
│   └── migration.md
└── .github/
    └── workflows/
        └── docker-publish.yml ← automatisch Docker Hub Image bauen
```

README.md Inhalt:
- Was ist ImageAssistant (1 Satz)
- Features Liste
- Screenshots Webinterface
- Quick Start (docker-compose up in 5 Minuten)
- Kompatible LLM Backends (LM Studio, Ollama, OpenAI)
- Kompatible Foto-Manager (Immich, Photoprism, etc.)
- Configuration Reference

Docker Hub:
- Image: `marcohediger/imageassistant:latest`
- Automatischer Build via GitHub Actions bei neuem Release

## Technologie
- Python 3.12 (Docker Image)
- FastAPI + Uvicorn
- SQLAlchemy (async) + aiosqlite
- ExifTool (perl, via apk)
- SQLite (Datenbank + Config + Logging)
- SMTP (via Python smtplib, STARTTLS Support)
- imagehash (Perceptual Hashing für Duplikat-Erkennung)
- ImageMagick + libheif (HEIC/DNG → JPEG Konvertierung)
- ffmpeg (Video-Thumbnail Extraktion)
- OpenAI-kompatibler Endpunkt (konfigurierbar: LM Studio, Ollama, OpenAI, etc.)
- Fernet (AES-128-CBC) für verschlüsselte API-Keys/Passwörter
- i18n via JSON-Sprachdateien (DE/EN), Jinja2 Templates

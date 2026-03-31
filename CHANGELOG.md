# Changelog

## v2.6.0 — 2026-03-31

### Schedule-Modus Enforcement
- **Zeitfenster-Modus**: Filewatcher verarbeitet nur innerhalb des konfigurierten Zeitfensters (z.B. 22:00–06:00), unterstützt Overnight-Fenster
- **Geplanter Modus**: Verarbeitung nur an bestimmten Wochentagen zu einer festen Uhrzeit (z.B. Mo–Fr 23:00)
- **Manueller Modus**: Keine automatische Verarbeitung — nur über "Jetzt scannen" Button im Dashboard
- **Kontinuierlich**: Wie bisher, 24/7 Verarbeitung
- Neuer API-Endpoint `POST /api/trigger-scan` für manuellen Scan-Trigger
- "Jetzt scannen" Button im Dashboard (funktioniert unabhängig vom Modus)

### Sortier-Regeln
- Editierbare Sortier-Regeln im Webinterface (Einstellungen → Sortier-Regeln)
- Bedingungen: Dateiname enthält, EXIF leer, EXIF enthält, Dateiendung
- Jede Regel mappt auf eine Zielkategorie (Foto, Video, Screenshot, Sourceless, Review)
- Reihenfolge per Pfeil-Buttons änderbar (erste Regel die matcht gewinnt)
- KI-Klassifikation hat immer Vorrang — Regeln greifen nur ohne KI-Ergebnis
- Standard-Regeln werden beim ersten Start geseedet

### HTML-Report nach Dry-Run
- Neuer Report unter Logs → "Dry-Run Report"
- Übersicht: Anzahl Dateien, Kategorien, Fehler, Duplikate, Review
- Aufschlüsselung nach Eingangsverzeichnis
- Vollständige Dateiliste mit Zielpfad und Status
- Fehlerübersicht mit Details

### Geocoding
- Photon und Google Maps API aus Auswahlmenü entfernt (verschoben auf v2)
- Nur noch Nominatim (OpenStreetMap) als Provider wählbar
- Backend-Code für Photon/Google bleibt erhalten für spätere Aktivierung

## v2.5.0 — 2026-03-30

### Performance-Optimierung für NAS-Betrieb (150k+ Dateien)
- **R1: Immich Streaming-Upload** — Dateien werden direkt von Disk gestreamt statt komplett in RAM geladen. Spart bei 500MB Video → 500MB RAM
- **R2: Dashboard 1 Query statt 6** — `GROUP BY status` statt 6 separate `COUNT`-Queries. Dashboard-JSON in ~22ms
- **R3: Duplikat-Erkennung optimiert** — pHash-Vergleich in Batches à 5000 statt ganze Tabelle (150k Rows) in RAM. Nur leichte Spalten geladen (`id`, `phash`, `debug_key`)
- **R4: Database-Indexes** — 7 Indexes auf `status`, `file_hash`, `phash`, `original_path`, `created_at`, `updated_at`, `system_logs.created_at`. Beschleunigt alle Queries massiv
- **R5: Docker Memory/CPU Limit** — `mem_limit: 2g`, `cpus: 2.0` in docker-compose.yml. NAS wird nicht mehr ausgelastet
- **R6: Temp-Cleanup** — `shutil.rmtree()` statt `os.rmdir()` bei fehlgeschlagenen Immich-Downloads. Keine Dateileichen mehr
- **R7: Log-Rotation** — System-Logs älter als 90 Tage werden automatisch gelöscht (stündliche Prüfung). DB wächst nicht mehr unbegrenzt
- **R8: safe_move optimiert** — Source-Datei wird nur noch 1× gelesen (Hash während Kopieren berechnet) statt 3× (Copy + Hash-src + Hash-dst). Spart 33% Disk-I/O

## v2.4.5 — 2026-03-30

### Security
- **Fix S1: Path Traversal Schutz** in `step_ia08_sort.py`, `review.py`
  - Neue Funktion `_sanitize_path_component()`: Entfernt `..`, `/`, `\` und Steuerzeichen aus EXIF-Werten (Country, City, Camera, Type) bevor sie in Pfade eingesetzt werden
  - Neue Funktion `_validate_target_path()`: Prüft mit `os.path.realpath()` dass der Zielpfad innerhalb der Bibliothek bleibt (Defense in Depth)
  - Geschützte Stellen: Pipeline IA-08 Sort, Review Classify, Review Classify-All
- **Fix S7: Dateigrössenlimit** in `filewatcher.py`
  - `MAX_FILE_SIZE = 10 GB` — Dateien über 10 GB werden übersprungen und geloggt
  - Verhindert Out-of-Memory bei extrem grossen Dateien
- **Fix S8: Immich Filename Sanitisierung** in `immich_client.py`
  - Neue Funktion `_sanitize_filename()`: Entfernt Path-Traversal-Muster (`../`, absolute Pfade) aus Immich-API-Dateinamen
  - Schützt `download_asset()` vor manipulierten `originalFileName`-Werten

## v2.4.3 — 2026-03-30

### Bugfix
- **Fix B10: Review-Status überschrieben**: Pipeline hat `job.status = "review"` (gesetzt von IA-08 für unklare Dateien) mit `"done"` überschrieben — UUID-Dateien ohne EXIF landeten im richtigen Verzeichnis (`unknown/review/`), aber mit Status "done" statt "review"

### Umfassendes E2E-Testing
- **Format-Tests**: PNG, HEIC, WebP, GIF, TIFF, MOV — alle Formate durch Pipeline verifiziert
- **Edge Cases**: Leere Dateien (abgewiesen), nicht unterstützte Formate (.txt abgewiesen), Dateinamenkollisionen (_1 Suffix), Screenshots (AI-Erkennung), kurze Videos (<1s, bekannte Limitation)
- **Modul-Disable**: AI, Geocoding, OCR einzeln deaktiviert — Pipeline läuft korrekt weiter mit Fallback-Werten
- **Job Retry/Delete**: Fehlgeschlagene Jobs wiederholt, gelöschte Jobs korrekt bereinigt
- **Review-System**: Einzelklassifikation und Batch-Classify-All verifiziert
- **Immich**: Ordner-Tags → Album-Erstellung, Sourceless-Archivierung bestätigt
- **Geocoding-Fehler**: Ungültige URL → nicht-kritischer Fehler, Pipeline fährt fort
- **Dry-Run**: Tags werden berechnet aber nicht geschrieben, Datei bleibt im Inbox
- **OCR**: Smart-Modus erkennt Screenshots, All-Modus verarbeitet alle Bilder
- **Blurry-Erkennung**: Unscharfe Bilder erhalten `blurry` Tag und Quality-Flag
- **Messenger-Dateien**: UUID-Dateinamen werden als sourceless erkannt, gehen in Review

## v2.4.2 — 2026-03-30

### Bugfixes aus E2E-Testing (DJI DNG, MP4, JPG+DNG Paare)
- **Fix: Video-Datumsformate**: ISO 8601 mit Mikrosekunden (`.000000`) und Timezone (`Z`, `+02:00`) werden jetzt korrekt geparst — Videos werden ins richtige Jahres-Verzeichnis sortiert statt ins aktuelle Datum
- **Fix: Filewatcher done_hashes**: Erkennt bereits verarbeitete Dateien zuverlässig — prüft dry_run-Jobs, Immich-Assets und Target-Existenz auf Dateisystem
- **Fix: Logging**: `logging.basicConfig()` in main.py — alle Pipeline-Logs erscheinen in Docker stdout (`docker logs`)
- **Fix: Pipeline-Fehler**: werden geloggt statt verschluckt (logger in `pipeline/__init__.py`)
- **Fix: ExifTool-Fehlermeldungen**: Bessere Fehlermeldungen bei korrupten/unlesbare Dateien
- **Fix: Kleine Bilder**: Bilder unter 16×16 Pixel werden von der KI-Analyse übersprungen (verhindert API-Fehler)
- **Fix: pHash-Threshold**: von 5 auf 3 gesenkt (weniger False Positives bei Duplikaterkennung)
- **Fix: Batch-Clean Label**: verdeutlicht, dass nur exakte SHA256-Duplikate automatisch bereinigt werden
- **UI: Preview-Badge**: Dry-Run-Jobs zeigen "Preview"-Badge in Log-Übersicht und Job-Detail

### Getestete Szenarien (DJI-Daten)
- DNG RAW-Dateien (25MB–97MB): EXIF, pHash aus Preview, Konvertierung, KI, Geocoding ✓
- MP4-Videos (57MB–304MB): ffprobe, Thumbnails, KI, Immich-Upload ✓
- JPG+DNG Paare: Paar-Erkennung (keep_both true/false) ✓
- Sonderzeichen in Dateinamen: Leerzeichen, Klammern ✓
- Alle Modi: Dateiablage, Immich, Dry-Run ✓
- Duplikat-Szenarien: SHA256, Cross-Mode, Keep/Delete, Batch-Clean ✓

## v2.4.0 — 2026-03-30

### JPG+RAW Paar-Erkennung
- **Konfigurierbares Verhalten**: Schalter in Einstellungen unter Duplikaterkennung
- **AN** (Standard): JPG + RAW werden beide unabhängig verarbeitet und übernommen
- **AUS**: Paare werden als Duplikat erkannt und landen im Review zur manuellen Auswahl
- Eigener "JPG+RAW" Badge in der Duplikat-Review-Seite

### Duplikat-Erkennung Verbesserungen
- **Fehlerhafte Jobs einbezogen**: SHA256- und pHash-Vergleich matcht jetzt auch gegen Jobs mit Status "error" — diese landen im Duplikat-Review statt automatisch verarbeitet zu werden
- **error_message Bereinigung**: Duplikat-Review setzt error_message korrekt auf NULL (verhindert doppelte Verarbeitung im Filewatcher)

### Filewatcher Stabilisierung
- **Hash-basierte Deduplizierung**: Nur noch erfolgreich abgeschlossene Jobs (done + kein Fehler) blockieren erneute Verarbeitung — fehlerhafte Dateien können erneut eingefügt werden
- **Vereinfachter Stabilitätscheck**: Einfache Dateigrössen-Prüfung nach 2s Wartezeit (robust bei Docker/SMB)

### Immich Upload Stabilität
- **Grosse Dateien**: Upload/Replace liest Datei komplett in Memory vor dem Senden (verhindert halbfertige DNG/RAW Uploads)
- **Separate Timeouts**: connect=10s, read=120s, write=300s für grosse Dateien (bis 10GB Videos)

### KI-Kontext im Log
- **IA-05 Detail-Ansicht**: Zeigt Modell, Anzahl Bilder, Metadaten-Kontext und KI-Antwort separat an
- Auto-Refresh erhält die formatierte Darstellung bei

### UI-Verbesserungen
- **Inbox-Pfade versteckt**: Temporäre Inbox-Pfade werden nie als Referenz angezeigt, "(Inbox — temporär)" Markierung im Job-Detail
- **Video-Thumbnails konfigurierbar**: Anzahl Frames (1–50) und Skalierung (25/50/75/100%) in Einstellungen
- Cache-Busting für JavaScript (v3)

## v2.3.0 — 2026-03-29

### Lightbox
- **Bild-Vollansicht**: Klick auf Thumbnail öffnet Originalbild als Fullscreen-Overlay (Review, Duplikate, Log-Detail)
- RAW/DNG: PreviewImage wird via ExifTool oder Immich-Preview extrahiert
- HEIC: wird zu JPEG konvertiert für Anzeige
- Schliessen mit ESC oder Klick auf Overlay

### Review-Seite
- **Löschen-Button** zum direkten Entfernen von Review-Dateien
- Dateigrösse wird via Immich API abgefragt (Fallback)
- Datum-Fallback auf FileModifyDate bzw. job.created_at
- Bildabmessungen (Auflösung) angezeigt
- Metadatenfelder bedingt angezeigt (Datum/Kamera nur wenn vorhanden)

### Duplikat-Review
- EXIF-Daten werden via Immich API geholt für Immich-Assets
- "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale Dateien)
- Badge (ORIGINAL/EXAKT) ist jetzt klickbarer Link (Immich → öffnet Immich, lokal → lädt Datei herunter)
- Keep-Aktion lädt Datei zu Immich hoch wenn Gruppe im Immich-Modus ist
- Immich-Delete repariert (httpx DELETE mit Request Body)

### Video-Verarbeitung
- **IA-01**: Video-Metadaten via ffprobe ergänzt ExifTool — Datum, GPS (ISO 6709 Parser), Dauer (roh + formatiert), Auflösung, Megapixel, Codec, Framerate, Bitrate, Rotation
- **IA-04**: Video-Thumbnail Extraktion via ffmpeg bei 10% der Dauer (vorbereitet, `VIDEO_THUMBNAIL_ENABLED = False`)

### Pipeline-Stabilität
- **Filewatcher**: Dateigrössen-Check nach 2s Wartezeit verhindert Verarbeitung halbkopierter Dateien
- **IA-07**: ExifTool `-m` Flag ignoriert kleinere Warnungen (z.B. DJI DNG "Maker notes")
- **IA-01**: Speichert file_size, Fallback auf FileModifyDate für Datum
- **httpx DELETE**: Korrektur — `json=` nicht unterstützt, stattdessen `client.request` mit `content=`

## v2.1.0 — 2026-03-29

### Pipeline-Optimierung
- **Neue Reihenfolge**: IA-01 EXIF → IA-02 Duplikate → IA-03 Geocoding → IA-04 Temp. Konvertierung → IA-05 KI → IA-06 OCR → IA-07–11
- Duplikaterkennung direkt nach EXIF (spart KI-Kosten bei Duplikaten)
- Geocoding vor KI-Analyse (Ortsdaten verbessern Klassifikation)
- Formatkonvertierung nur noch direkt vor KI (wird nur bei Bedarf ausgeführt)
- pHash-Fallback für RAW-Formate (DNG, CR2, NEF, ARW) via ExifTool PreviewImage

### Tags
- Mood-Tags (indoor/outdoor) entfernt — kein Nutzen als Keyword
- Quality nur noch bei `blurry` geschrieben
- OCR: einfaches `OCR` Flag statt `text:screenshot` etc.
- AI-Type bleibt als Tag erhalten

### Fehler-Logging
- Voller Python-Traceback bei allen Pipeline-Fehlern (kritisch, nicht-kritisch, Finalizer)
- Job-Detail zeigt Traceback in Monospace an
- System-Log Detail-Spalte mit Traceback

### Verarbeitungs-Log
- Zeit-Spalte direkt nach Debug-Key
- Verarbeitungsdauer angezeigt (z.B. "1m 23s")
- Datei und Ziel mit natürlichem Zeilenumbruch
- Fehler-Spalte kompakt mit Tooltip für Details

## v2.0.0 — 2026-03-29

### Review-System
- Neue Review-Seite für manuelle Klassifikation unklarer Dateien
- Kategorien: Foto, Video, Screenshot, Sourceless
- Immich-Integration: Sourceless → archiviert, andere bleiben in Timeline
- Batch-Aktion: alle Review-Dateien als Sourceless klassifizieren
- Alle unklaren Dateien gehen zu Review (keine automatische Sourceless-Zuordnung)

### Immich-Archivierung
- Screenshots und Sourceless werden automatisch in Immich archiviert (aus Timeline ausgeblendet)
- Fallback für ältere Immich-Versionen (isArchived vs. visibility API)

### KI-Optimierung
- Optimierter KI-Prompt mit allen gesammelten Metadaten (EXIF, Geocoding, Dateigrösse, Dateiname)
- Messenger-Erkennung (WhatsApp, Signal, Telegram) aus Dateiname
- UUID-Dateinamen als Messenger-Hinweis erkannt
- Dateigrösse als Klassifikations-Signal

### UI-Verbesserungen
- Review-Link in Navigation
- Zurück-Button in Job-Detail geht zu Verarbeitungs-Log (nicht System-Log)
- Immich-Thumbnail in Job-Detail-Seite

## v1.5.0

### Features
- Immich bidirektionale Integration (Upload + Polling)
- Immich-Alben aus Ordner-Tags
- Duplikat-Review mit Immich-Thumbnails
- OCR-Texterkennung (Smart/Alle Modi)
- Dry-Run Modus pro Inbox
- Ordner-Tags als EXIF-Keywords
- i18n (DE/EN)
- Dark/Light Theme

## v1.0.0

- Initial release
- 11-Step Pipeline
- Web Interface (Dashboard, Settings, Logs)
- EXIF, KI-Analyse, Geocoding, Duplikaterkennung
- SMTP Benachrichtigung
- Docker Deployment

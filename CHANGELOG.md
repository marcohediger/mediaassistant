# Changelog

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

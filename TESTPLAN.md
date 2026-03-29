# Testplan — MediaAssistant

## 1. Pipeline-Steps

### IA-01: EXIF auslesen
- [ ] JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt extrahiert
- [ ] HEIC mit EXIF → korrekt gelesen
- [ ] Datei ohne EXIF (z.B. Messenger-Bild) → `has_exif: false`
- [ ] Video (MP4/MOV) → Mime-Type und Dateityp korrekt erkannt
- [ ] Beschädigte Datei → Fehler wird gefangen, Pipeline bricht nicht ab
- [ ] file_size wird korrekt gespeichert
- [ ] Datum-Fallback auf FileModifyDate wenn DateTimeOriginal fehlt
- [ ] Video: ffprobe extrahiert Datum (creation_time) korrekt
- [ ] Video: ffprobe extrahiert GPS-Koordinaten aus ISO 6709 String (z.B. `+47.3769+008.5417/`)
- [ ] Video: ISO 6709 Parser verarbeitet verschiedene Formate korrekt (mit/ohne Höhe, mit/ohne Vorzeichen)
- [ ] Video: GPS aus ISO 6709 wird als lat/lon in Metadaten gespeichert
- [ ] Video: Dauer (duration) wird als Rohwert und formatiert gespeichert (z.B. `125.4` → `2m 05s`)
- [ ] Video: Auflösung (width x height) korrekt extrahiert
- [ ] Video: Megapixel aus Auflösung berechnet
- [ ] Video: Codec (z.B. h264, hevc) korrekt extrahiert
- [ ] Video: Framerate (z.B. 30, 60) korrekt extrahiert
- [ ] Video: Bitrate korrekt extrahiert
- [ ] Video: Rotation korrekt extrahiert (z.B. 0, 90, 180, 270)
- [ ] Video: ffprobe nicht verfügbar → Fehler gefangen, Fallback auf ExifTool-Daten
- [ ] Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, fehlende ignoriert

### IA-02: Duplikat-Erkennung
- [ ] Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate"
- [ ] Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert
- [ ] Unterschiedliches Bild → kein Match, `status: ok`
- [ ] RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet
- [ ] Modul deaktiviert → `status: skipped`
- [ ] Duplikat eines Immich-Assets → korrekt erkannt
- [ ] Orphaned Job (Original-Datei gelöscht) → Match wird übersprungen

### IA-03: Geocoding
- [ ] Bild mit GPS-Koordinaten → Land, Stadt, Stadtteil aufgelöst
- [ ] Bild ohne GPS → `status: skipped`
- [ ] Nominatim-Provider → korrekte Ergebnisse
- [ ] Photon-Provider → korrekte Ergebnisse
- [ ] Modul deaktiviert → `status: skipped`
- [ ] Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen

### IA-04: Temp. Konvertierung für KI
- [ ] JPG/PNG/WebP → keine Konvertierung, `converted: false`
- [ ] HEIC → temp JPEG erstellt
- [ ] DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG
- [ ] GIF → erster Frame als JPEG
- [ ] Nicht unterstütztes Format → `converted: false`
- [ ] Konvertierung fehlgeschlagen → Fehler gefangen
- [ ] Video mit VIDEO_THUMBNAIL_ENABLED = False → kein Thumbnail extrahiert, `converted: false`
- [ ] Video mit VIDEO_THUMBNAIL_ENABLED = True → Thumbnail bei 10% der Dauer extrahiert als temp JPEG
- [ ] Video-Thumbnail: Dauer korrekt ermittelt, Frame bei 10% Position extrahiert
- [ ] Video-Thumbnail: sehr kurzes Video (< 1s) → Thumbnail trotzdem extrahiert (Fallback auf erstes Frame)
- [ ] Video-Thumbnail: ffmpeg nicht verfügbar → Fehler gefangen, `converted: false`

### IA-05: KI-Analyse
- [ ] Persönliches Foto → `type: personal`, sinnvolle Tags
- [ ] Screenshot → `type: screenshot`
- [ ] Meme mit Text-Overlay → `type: meme`
- [ ] Internet-Bild → `type: internet_image`
- [ ] Dokument/Quittung → `type: document`
- [ ] KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt
- [ ] Modul deaktiviert → `status: skipped`
- [ ] Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben

### IA-06: OCR
- [ ] Screenshot mit Text → Text erkannt, `has_text: true`
- [ ] Foto ohne Text → `has_text: false`
- [ ] Smart-Modus: normales Foto → OCR übersprungen
- [ ] Smart-Modus: Screenshot → OCR ausgeführt
- [ ] Always-Modus → OCR immer ausgeführt
- [ ] Modul deaktiviert → `status: skipped`

### IA-07: EXIF-Tags schreiben
- [ ] AI-Tags werden als Keywords geschrieben
- [ ] AI-Type wird als Keyword geschrieben
- [ ] Geocoding-Daten (Land, Stadt etc.) als Keywords
- [ ] Ordner-Tags als Keywords + `album:` Tag
- [ ] `OCR` Flag bei erkanntem Text
- [ ] `blurry` Tag bei schlechter Qualität
- [ ] Kein mood-Tag (indoor/outdoor) geschrieben
- [ ] Kein quality-Tag ausser bei blurry
- [ ] Description aus AI + Geocoding zusammengebaut
- [ ] OCR-Text in UserComment geschrieben
- [ ] Dry-Run → Tags berechnet aber nicht geschrieben
- [ ] Datei-Hash nach Schreiben neu berechnet
- [ ] `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschrieben

### IA-08: Sortierung
- [ ] `personal` → photos/{YYYY}/{YYYY-MM}/
- [ ] `screenshot` → screenshots/{YYYY}/
- [ ] `meme`/`internet_image`/`document` → sourceless/{YYYY}/
- [ ] Video → videos/{YYYY}/{YYYY-MM}/
- [ ] Unklar (kein EXIF, KI unsicher) → Status "review"
- [ ] Immich Upload → Datei hochgeladen, Quelle gelöscht
- [ ] Immich Replace (Polling) → Asset ersetzt
- [ ] Immich: sourceless/screenshot → Asset archiviert
- [ ] Namenskollision → automatischer Counter (_1, _2, ...)
- [ ] Dry-Run → Zielpfad berechnet, nicht verschoben
- [ ] Leere Quellordner aufgeräumt

### IA-09: Benachrichtigung
- [ ] Fehler vorhanden → E-Mail gesendet
- [ ] Kein Fehler → keine E-Mail
- [ ] SMTP nicht konfiguriert → übersprungen

### IA-10: Cleanup
- [ ] Temp JPEG aus IA-04 gelöscht
- [ ] Immich-Webhook: heruntergeladene Datei gelöscht
- [ ] Keine temp Dateien → nichts zu tun

### IA-11: SQLite Log
- [ ] Zusammenfassung korrekt (Typ, Tags, Ort, Ziel)
- [ ] Log-Eintrag in system_log Tabelle erstellt

## 2. Pipeline-Fehlerbehandlung

- [ ] Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuft weiter
- [ ] Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finalizer laufen trotzdem
- [ ] Fehler-Datei nach error/ verschoben mit .log Datei
- [ ] Voller Traceback in error_message, step_result und System-Log
- [ ] Retry: fehlgeschlagener Job kann erneut verarbeitet werden
- [ ] Duplikat erkannt → Pipeline stoppt nach IA-02, Finalizer laufen

## 3. Web Interface

### Dashboard
- [ ] Statistiken korrekt (Total, Done, Errors, Queue, Duplicates, Review)
- [ ] Modul-Status mit Health-Checks (KI, Geocoding, SMTP, Filewatcher, Immich)
- [ ] Letzte Verarbeitungen mit Auto-Refresh

### Einstellungen
- [ ] Module einzeln aktivieren/deaktivieren
- [ ] KI-Backend URL, Modell, API-Key konfigurierbar
- [ ] AI System-Prompt editierbar (Default-Fallback)
- [ ] Geocoding Provider (Nominatim/Photon/Google) + URL
- [ ] Inbox-Verzeichnisse: hinzufügen, bearbeiten, löschen
- [ ] Pro Inbox: Pfad, Label, Ordner-Tags, Dry-Run, Immich, Aktiv
- [ ] Immich URL + API-Key + Polling-Toggle
- [ ] Bibliothek-Pfade mit Platzhaltern
- [ ] pHash-Schwellwert konfigurierbar
- [ ] OCR-Modus (Smart/Alle)
- [ ] Filewatcher Schedule (Kontinuierlich/Zeitfenster/Geplant/Manuell)
- [ ] Sprache (DE/EN) und Theme (Dark/Light)
- [ ] API-Keys verschlüsselt gespeichert

### Duplikat-Review
- [ ] Gruppen transitive zusammengeführt (Union-Find)
- [ ] Dateien nebeneinander mit Thumbnail, EXIF, Keywords
- [ ] Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay
- [ ] Lightbox: RAW/DNG zeigt PreviewImage (ExifTool oder Immich Preview)
- [ ] Lightbox: HEIC wird zu JPEG konvertiert für Anzeige
- [ ] Lightbox: ESC oder Klick schliesst Overlay
- [ ] EXIF-Daten für Immich-Assets via Immich API geholt
- [ ] "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale)
- [ ] "Dieses behalten" bei Immich-Gruppe → Upload zu Immich
- [ ] Badge (ORIGINAL/EXAKT) ist klickbarer Link (Immich → öffnet Immich, lokal → Download)
- [ ] Batch-Clean → alle exakten SHA256-Duplikate gelöscht
- [ ] Immich-Duplikate: Thumbnail aus Immich, "In Immich ansehen"
- [ ] Immich-Delete funktioniert korrekt (httpx DELETE mit request body)

### Review (Manuelle Klassifikation)
- [ ] Alle Jobs mit Status "review" angezeigt
- [ ] Thumbnail (lokal oder Immich)
- [ ] Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay
- [ ] Lightbox: RAW/DNG zeigt PreviewImage, HEIC → JPEG
- [ ] AI-Beschreibung, Tags, Metadaten angezeigt
- [ ] Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar)
- [ ] Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at
- [ ] Bildabmessungen (Auflösung) angezeigt
- [ ] Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden)
- [ ] Kategorie-Buttons: Foto, Video, Screenshot, Sourceless
- [ ] Löschen-Button entfernt Review-Datei
- [ ] Lokal: Datei in richtigen Zielordner verschoben
- [ ] Immich: Sourceless → archiviert
- [ ] Batch: "Alle → Sourceless" funktioniert

### Log Viewer
- [ ] System-Log mit Level-Filter (Info/Warning/Error)
- [ ] System-Log Detail mit vollem Traceback
- [ ] Verarbeitungs-Log mit Status-Filter
- [ ] Verarbeitungs-Log zeigt Dauer an
- [ ] Suche nach Dateiname und Debug-Key
- [ ] Pagination funktioniert
- [ ] Job-Detail: alle Step-Results, Pfade, Timestamps, Hashes
- [ ] Job-Detail: voller Traceback bei Fehlern
- [ ] Job-Detail: Immich-Thumbnail bei Immich-Assets
- [ ] Job-Detail: Lightbox — Klick auf Thumbnail öffnet Originalbild
- [ ] Job-Detail: Zurück-Button geht zu Verarbeitungs-Log
- [ ] Job löschen und Retry funktioniert

## 4. Filewatcher-Stabilität

- [ ] Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet
- [ ] Nach 2s Wartezeit: Dateigrösse wird erneut geprüft
- [ ] Dateigrösse stabil → Verarbeitung startet
- [ ] Dateigrösse geändert → erneute Wartezeit

## 5. Immich-Integration

- [ ] Upload: Datei wird hochgeladen, Asset-ID gespeichert
- [ ] Upload: Album aus Ordner-Tags erstellt
- [ ] Upload: Sourceless/Screenshots werden archiviert
- [ ] Polling: neue Assets werden erkannt und verarbeitet
- [ ] Polling: bereits verarbeitete Assets werden übersprungen
- [ ] Replace: Asset in Immich mit getaggter Version ersetzt
- [ ] Duplikat-Erkennung über Immich-Assets hinweg
- [ ] Immich nicht erreichbar → Fehler geloggt

## 6. Dateiformate

- [ ] JPG/JPEG — Verarbeitung + KI + Tags schreiben
- [ ] PNG — Verarbeitung + KI + Tags schreiben
- [ ] HEIC/HEIF — Konvertierung + KI + Tags schreiben
- [ ] WebP — Verarbeitung + KI
- [ ] GIF — erster Frame konvertiert für KI
- [ ] TIFF — Verarbeitung + Tags schreiben
- [ ] DNG/CR2/NEF/ARW — PreviewImage für KI + pHash
- [ ] MP4/MOV — Video erkannt, ffprobe-Metadaten extrahiert, korrekt sortiert
- [ ] Nicht unterstütztes Format → sauber übersprungen

## 7. Edge Cases

- [ ] Leere Datei → Fehler gefangen
- [ ] Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert
- [ ] Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet
- [ ] Dateiname mit Leerzeichen → korrekt verarbeitet
- [ ] Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust
- [ ] Container-Neustart während Verarbeitung → Resume ab letztem Step
- [ ] Verschlüsselte Config-Werte → korrekt entschlüsselt

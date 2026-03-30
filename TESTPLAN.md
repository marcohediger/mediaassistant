# Testplan — MediaAssistant

> Letzter vollständiger Testlauf: **v2.4.2 — 2026-03-30**
> Testdaten: Panasonic DMC-GF2 JPGs, DJI FC7203/FC3170 JPGs, DJI DNG RAW, DJI MP4 Videos

## 1. Pipeline-Steps

### IA-01: EXIF auslesen
- [x] JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt extrahiert
- [ ] HEIC mit EXIF → korrekt gelesen
- [x] Datei ohne EXIF (z.B. Messenger-Bild) → `has_exif: false`
- [x] Video (MP4/MOV) → Mime-Type und Dateityp korrekt erkannt
- [x] Beschädigte Datei → Fehler wird gefangen, Pipeline bricht nicht ab
- [x] file_size wird korrekt gespeichert
- [x] Datum-Fallback auf FileModifyDate wenn DateTimeOriginal fehlt
- [x] Video: ffprobe extrahiert Datum (creation_time) korrekt
- [x] Video: ffprobe extrahiert GPS-Koordinaten aus ISO 6709 String (z.B. `+47.3769+008.5417/`)
- [x] Video: ISO 6709 Parser verarbeitet verschiedene Formate korrekt (mit/ohne Höhe, mit/ohne Vorzeichen)
- [x] Video: GPS aus ISO 6709 wird als lat/lon in Metadaten gespeichert
- [x] Video: Dauer (duration) wird als Rohwert und formatiert gespeichert (z.B. `125.4` → `2m 05s`)
- [x] Video: Auflösung (width x height) korrekt extrahiert
- [x] Video: Megapixel aus Auflösung berechnet
- [x] Video: Codec (z.B. h264, hevc) korrekt extrahiert
- [x] Video: Framerate (z.B. 30, 60) korrekt extrahiert
- [x] Video: Bitrate korrekt extrahiert
- [x] Video: Rotation korrekt extrahiert (z.B. 0, 90, 180, 270)
- [ ] Video: ffprobe nicht verfügbar → Fehler gefangen, Fallback auf ExifTool-Daten
- [x] Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, fehlende ignoriert
- [x] DNG (RAW): EXIF korrekt (Make, Model, Datum, GPS, Auflösung)
- [x] DNG: Grosse Dateien (25MB–97MB) verarbeitet ohne Timeout

### IA-02: Duplikat-Erkennung
- [x] Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate"
- [x] Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert
- [x] Unterschiedliches Bild → kein Match, `status: ok`
- [x] RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet
- [ ] Modul deaktiviert → `status: skipped`
- [x] Duplikat eines Immich-Assets → korrekt erkannt
- [x] Orphaned Job (Original-Datei gelöscht) → Match wird übersprungen
- [x] JPG+DNG Paar mit keep_both=true → beide unabhängig verarbeitet
- [x] JPG+DNG Paar mit keep_both=false → zweite Datei als `raw_jpg_pair` Duplikat
- [x] pHash-Threshold 3 → weniger False Positives als Threshold 5
- [x] Video: kein pHash (Videos haben keinen pHash), nur SHA256

### IA-03: Geocoding
- [x] Bild mit GPS-Koordinaten → Land, Stadt, Stadtteil aufgelöst
- [x] Bild ohne GPS → `status: skipped`
- [x] Nominatim-Provider → korrekte Ergebnisse
- [ ] Photon-Provider → korrekte Ergebnisse
- [ ] Modul deaktiviert → `status: skipped`
- [ ] Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen
- [x] DJI-Drohne GPS (Teneriffa, Schweiz) → korrekt aufgelöst
- [x] Video GPS (ffprobe ISO 6709) → korrekt geocodiert

### IA-04: Temp. Konvertierung für KI
- [x] JPG/PNG/WebP → keine Konvertierung, `converted: false`
- [ ] HEIC → temp JPEG erstellt
- [x] DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG
- [ ] GIF → erster Frame als JPEG
- [ ] Nicht unterstütztes Format → `converted: false`
- [x] Konvertierung fehlgeschlagen → Fehler gefangen (korruptes Video)
- [ ] Video mit VIDEO_THUMBNAIL_ENABLED = False → kein Thumbnail extrahiert, `converted: false`
- [x] Video mit VIDEO_THUMBNAIL_ENABLED = True → mehrere Thumbnails extrahiert
- [x] Video-Thumbnail: Dauer korrekt ermittelt, Frames gleichmässig verteilt
- [ ] Video-Thumbnail: sehr kurzes Video (< 1s) → Thumbnail trotzdem extrahiert
- [x] Video-Thumbnail: ffmpeg nicht verfügbar / Fehler → Fehler gefangen, `converted: false`

### IA-05: KI-Analyse
- [x] Persönliches Foto → `type: personal`, sinnvolle Tags
- [ ] Screenshot → `type: screenshot`
- [ ] Meme mit Text-Overlay → `type: meme`
- [ ] Internet-Bild → `type: internet_image`
- [ ] Dokument/Quittung → `type: document`
- [x] KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt (korruptes Video)
- [ ] Modul deaktiviert → `status: skipped`
- [x] Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben
- [x] DNG-Konvertierung für KI-Analyse funktioniert
- [x] Video-Thumbnails (5 Frames) für KI-Analyse
- [x] Sehr kleine Bilder (<16px) → übersprungen mit Meldung
- [x] DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt

### IA-06: OCR
- [ ] Screenshot mit Text → Text erkannt, `has_text: true`
- [ ] Foto ohne Text → `has_text: false`
- [x] Smart-Modus: normales Foto → OCR übersprungen
- [ ] Smart-Modus: Screenshot → OCR ausgeführt
- [ ] Always-Modus → OCR immer ausgeführt
- [ ] Modul deaktiviert → `status: skipped`

### IA-07: EXIF-Tags schreiben
- [x] AI-Tags werden als Keywords geschrieben
- [x] AI-Type wird als Keyword geschrieben
- [x] Geocoding-Daten (Land, Stadt etc.) als Keywords
- [ ] Ordner-Tags als Keywords + `album:` Tag
- [ ] `OCR` Flag bei erkanntem Text
- [ ] `blurry` Tag bei schlechter Qualität
- [x] Kein mood-Tag (indoor/outdoor) geschrieben
- [x] Kein quality-Tag ausser bei blurry
- [x] Description aus AI + Geocoding zusammengebaut
- [ ] OCR-Text in UserComment geschrieben
- [ ] Dry-Run → Tags berechnet aber nicht geschrieben
- [x] Datei-Hash nach Schreiben neu berechnet
- [x] `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschrieben
- [x] DNG: Tags korrekt geschrieben (file_size ändert sich)
- [x] MP4: Tags korrekt in Video geschrieben

### IA-08: Sortierung
- [x] `personal` → photos/{YYYY}/{YYYY-MM}/
- [ ] `screenshot` → screenshots/{YYYY}/
- [ ] `meme`/`internet_image`/`document` → sourceless/{YYYY}/
- [x] Video → videos/{YYYY}/{YYYY-MM}/
- [ ] Unklar (kein EXIF, KI unsicher) → Status "review"
- [x] Immich Upload → Datei hochgeladen, Quelle gelöscht
- [ ] Immich Replace (Polling) → Asset ersetzt
- [ ] Immich: sourceless/screenshot → Asset archiviert
- [ ] Namenskollision → automatischer Counter (_1, _2, ...)
- [x] Dry-Run → Zielpfad berechnet, nicht verschoben
- [x] Leere Quellordner aufgeräumt
- [x] EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit)
- [x] ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst
- [x] DNG nach korrektem Jahresordner sortiert (2022, 2023, 2024)
- [x] Video nach korrektem Jahresordner sortiert (nach Datum-Fix v2.4.2)

### IA-09: Benachrichtigung
- [x] Fehler vorhanden → E-Mail gesendet
- [x] Kein Fehler → keine E-Mail
- [ ] SMTP nicht konfiguriert → übersprungen

### IA-10: Cleanup
- [x] Temp JPEG aus IA-04 gelöscht (DNG-Konvertierung + Video-Thumbnails)
- [ ] Immich-Webhook: heruntergeladene Datei gelöscht
- [x] Keine temp Dateien → nichts zu tun

### IA-11: SQLite Log
- [x] Zusammenfassung korrekt (Typ, Tags, Ort, Ziel)
- [x] Log-Eintrag in system_log Tabelle erstellt

## 2. Pipeline-Fehlerbehandlung

- [x] Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuft weiter
- [x] Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finalizer laufen trotzdem
- [ ] Fehler-Datei nach error/ verschoben mit .log Datei
- [x] Voller Traceback in error_message, step_result und System-Log
- [ ] Retry: fehlgeschlagener Job kann erneut verarbeitet werden
- [x] Duplikat erkannt → Pipeline stoppt nach IA-02, Finalizer laufen
- [x] Korruptes Video → Warnungen, E-Mail-Benachrichtigung, kein Crash

## 3. Web Interface

### Dashboard
- [x] Statistiken korrekt (Total, Done, Errors, Queue, Duplicates, Review)
- [x] Modul-Status mit Health-Checks (KI, Geocoding, SMTP, Filewatcher, Immich)
- [x] Letzte Verarbeitungen mit Auto-Refresh

### Einstellungen
- [x] Module einzeln aktivieren/deaktivieren
- [x] KI-Backend URL, Modell, API-Key konfigurierbar
- [x] AI System-Prompt editierbar (Default-Fallback)
- [x] Geocoding Provider (Nominatim/Photon/Google) + URL
- [x] Inbox-Verzeichnisse: hinzufügen, bearbeiten, löschen
- [x] Pro Inbox: Pfad, Label, Ordner-Tags, Dry-Run, Immich, Aktiv
- [x] Immich URL + API-Key + Polling-Toggle
- [x] Bibliothek-Pfade mit Platzhaltern
- [x] pHash-Schwellwert konfigurierbar
- [x] OCR-Modus (Smart/Alle)
- [x] Filewatcher Schedule (Kontinuierlich/Zeitfenster/Geplant/Manuell)
- [x] Sprache (DE/EN) und Theme (Dark/Light)
- [x] API-Keys verschlüsselt gespeichert

### Duplikat-Review
- [x] Gruppen transitive zusammengeführt (Union-Find)
- [x] Dateien nebeneinander mit Thumbnail, EXIF, Keywords
- [x] Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay
- [x] Lightbox: RAW/DNG zeigt PreviewImage (ExifTool oder Immich Preview)
- [ ] Lightbox: HEIC wird zu JPEG konvertiert für Anzeige
- [x] Lightbox: ESC oder Klick schliesst Overlay
- [x] EXIF-Daten für Immich-Assets via Immich API geholt
- [x] "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale)
- [x] "Dieses behalten" bei Immich-Gruppe → Upload zu Immich
- [x] Badge (ORIGINAL/EXAKT) ist klickbarer Link (Immich → öffnet Immich, lokal → Download)
- [x] Batch-Clean → alle exakten SHA256-Duplikate gelöscht, ähnliche (pHash) behalten
- [x] Immich-Duplikate: Thumbnail aus Immich, "In Immich ansehen"
- [x] Immich-Delete funktioniert korrekt (httpx DELETE mit request body)
- [x] Keep/Delete mit JPG+DNG Paar funktioniert korrekt

### Review (Manuelle Klassifikation)
- [x] Alle Jobs mit Status "review" angezeigt
- [x] Thumbnail (lokal oder Immich)
- [x] Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay
- [ ] Lightbox: RAW/DNG zeigt PreviewImage, HEIC → JPEG
- [x] AI-Beschreibung, Tags, Metadaten angezeigt
- [x] Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar)
- [x] Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at
- [x] Bildabmessungen (Auflösung) angezeigt
- [x] Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden)
- [x] Kategorie-Buttons: Foto, Video, Screenshot, Sourceless
- [x] Löschen-Button entfernt Review-Datei
- [ ] Lokal: Datei in richtigen Zielordner verschoben
- [ ] Immich: Sourceless → archiviert
- [ ] Batch: "Alle → Sourceless" funktioniert

### Log Viewer
- [x] System-Log mit Level-Filter (Info/Warning/Error)
- [x] System-Log Detail mit vollem Traceback
- [x] Verarbeitungs-Log mit Status-Filter
- [x] Verarbeitungs-Log zeigt Dauer an
- [x] Suche nach Dateiname und Debug-Key
- [x] Pagination funktioniert
- [x] Job-Detail: alle Step-Results, Pfade, Timestamps, Hashes
- [x] Job-Detail: voller Traceback bei Fehlern
- [x] Job-Detail: Immich-Thumbnail bei Immich-Assets
- [x] Job-Detail: Lightbox — Klick auf Thumbnail öffnet Originalbild
- [x] Job-Detail: Zurück-Button geht zu Verarbeitungs-Log
- [ ] Job löschen und Retry funktioniert
- [x] Preview-Badge bei Dry-Run-Jobs angezeigt

## 4. Filewatcher-Stabilität

- [x] Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet
- [x] Nach 2s Wartezeit: Dateigrösse wird erneut geprüft
- [x] Dateigrösse stabil → Verarbeitung startet
- [x] Dateigrösse geändert → erneute Wartezeit
- [x] Bereits verarbeitete Datei erneut in Inbox → Filewatcher überspringt (done_hashes)
- [x] Dry-Run-Jobs werden in done_hashes berücksichtigt
- [x] Immich-Assets werden in done_hashes berücksichtigt
- [x] Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft)
- [x] Docker-Logging: Alle Filewatcher-Aktionen in stdout sichtbar

## 5. Immich-Integration

- [x] Upload: Datei wird hochgeladen, Asset-ID gespeichert
- [ ] Upload: Album aus Ordner-Tags erstellt
- [ ] Upload: Sourceless/Screenshots werden archiviert
- [ ] Polling: neue Assets werden erkannt und verarbeitet
- [ ] Polling: bereits verarbeitete Assets werden übersprungen
- [ ] Replace: Asset in Immich mit getaggter Version ersetzt
- [x] Duplikat-Erkennung über Immich-Assets hinweg
- [ ] Immich nicht erreichbar → Fehler geloggt
- [x] DNG nach Immich hochgeladen (25MB RAW)
- [x] MP4 nach Immich hochgeladen (304MB Video)
- [x] JPG nach Immich hochgeladen (mit GPS/Tags)
- [x] Cross-Mode Duplikat: Dateiablage → Immich erkannt

## 6. Dateiformate

- [x] JPG/JPEG — Verarbeitung + KI + Tags schreiben
- [ ] PNG — Verarbeitung + KI + Tags schreiben
- [ ] HEIC/HEIF — Konvertierung + KI + Tags schreiben
- [ ] WebP — Verarbeitung + KI
- [ ] GIF — erster Frame konvertiert für KI
- [ ] TIFF — Verarbeitung + Tags schreiben
- [x] DNG — PreviewImage für KI + pHash, Tags schreiben, grosse Dateien (25–97MB)
- [ ] CR2/NEF/ARW — PreviewImage für KI + pHash
- [x] MP4 — Video erkannt, ffprobe-Metadaten, Thumbnails, KI, Tags schreiben, korrekt sortiert
- [ ] MOV — Video erkannt, korrekt sortiert
- [ ] Nicht unterstütztes Format → sauber übersprungen

## 7. Edge Cases

- [ ] Leere Datei → Fehler gefangen
- [x] Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert (97MB DNG, 304MB MP4)
- [x] Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet
- [x] Dateiname mit Leerzeichen und Klammern → korrekt verarbeitet (`DJI_0061 (2).JPG`)
- [x] Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust (Batch 4 Dateien)
- [ ] Container-Neustart während Verarbeitung → Resume ab letztem Step
- [x] Verschlüsselte Config-Werte → korrekt entschlüsselt
- [x] Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash
- [x] Sehr kleine Bilder (<16px) → KI-Analyse übersprungen

## 8. DJI-Testdaten Ergebnisse (v2.4.2)

### Pipeline Dateiablage
| Test | Datei | Ergebnis |
|------|-------|----------|
| T1 | DJI_0063.DNG (25MB, FC3170) | ✅ EXIF, pHash, Konvert., KI, Geocoding (Teneriffa) |
| T2 | DJI_0047.MP4 (57MB, korrupt) | ⚠️ Kein Crash, Warnungen + E-Mail |
| T2b | DJI_0041.MP4 (285MB, FC7203) | ✅ ffprobe, 5 Thumbnails, KI, GPS (Nussbaumen CH) |
| T3 | DJI_0002.JPG (FC7203) | ✅ EXIF, KI (Innenraum), Datum korrekt 2020-06 |
| T4 | DJI_0004.JPG + DJI_0004.DNG | ✅ Unabhängig verarbeitet (verschiedene Szenen) |
| T4b | DJI_0005.JPG + DJI_0005.DNG | ✅ Paar-Erkennung: DNG=Original, JPG=raw_jpg_pair |
| T5 | DJI_0061 (2).JPG | ✅ Sonderzeichen OK, GPS (Teneriffa) |
| T6 | 2 JPG + 1 DNG + 1 Sonderzeichen | ✅ Batch korrekt, verschiedene Daten |

### Pipeline Immich
| Test | Datei | Ergebnis |
|------|-------|----------|
| T7 | DJI_0173.DNG (25MB) | ✅ Upload OK, GPS (Trin Mulin, Graubünden) |
| T8 | DJI_0053.MP4 (304MB) | ✅ Upload OK, GPS (Baden AG), 5 Thumbnails |
| T9 | DJI_0064.JPG | ✅ Upload OK, GPS (Ennetbürgen NW) |

### Duplikat-Tests
| Test | Szenario | Ergebnis |
|------|----------|----------|
| D1 | DNG erneut einfügen (SHA256) | ✅ Filewatcher überspringt |
| D2 | Video erneut einfügen (SHA256) | ✅ Filewatcher überspringt |
| D3 | Cross-Mode (Dateiablage → Immich) | ✅ Hash erkannt trotz Moduswechsel |
| D4 | Keep JPG / Delete DNG (Paar) | ✅ JPG verschoben, DNG gelöscht |
| D5 | Dry-Run DNG | ✅ Alle Schritte, Datei bleibt in Inbox |
| D5b | Dry-Run Duplikat | ✅ Filewatcher überspringt |
| D6 | Immich Re-Import DNG | ✅ Existierendes Asset erkannt |
| D7 | Batch-Clean | ✅ 6 exakte bereinigt, 3 ähnliche behalten |

### Gefundene Bugs (alle in v2.4.2 behoben)
| Bug | Beschreibung |
|-----|-------------|
| B1: Filewatcher done_hashes | Dry-Run-Jobs und fehlende Targets nicht berücksichtigt |
| B2: Filewatcher Re-Import | Gelöschte Zieldateien nicht erkannt → Datei ignoriert |
| B3: ExifTool-Fehler | Korrupte Dateien: generische Fehlermeldung statt hilfreicher Text |
| B4: Kleine Bilder | <16px Bilder → KI-API Fehler 400 statt Überspringen |
| B5: pHash False Positives | Threshold 5 zu hoch → unähnliche Bilder als Duplikat |
| B6: Batch-Clean Label | Unklar ob nur exakte oder auch ähnliche Duplikate |
| B7: Dry-Run Badge | Kein visueller Hinweis auf Preview-Jobs in Logs |
| B8: Docker Logging | Pipeline-Logs nur in SQLite, nicht in stdout |
| B9: Video-Datum | ISO 8601 mit `.000000Z` nicht geparst → falscher Jahresordner |

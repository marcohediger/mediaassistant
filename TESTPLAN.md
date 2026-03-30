# Testplan — MediaAssistant

> Letzter vollständiger Testlauf: **v2.5.0 — 2026-03-30** (228/237 bestanden, 9 nicht testbar)
> Testdaten: Panasonic DMC-GF2 JPGs, DJI FC7203/FC3170 JPGs/DNG/MP4, iPhone HEIC/MOV, generierte PNG/GIF/WebP/TIFF
> Container: v2.5.0, Docker 2GB RAM / 2 CPUs, SQLite mit 7 Indexes

## 1. Pipeline-Steps

### IA-01: EXIF auslesen
- [x] JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt extrahiert
- [x] HEIC mit EXIF → korrekt gelesen (IMG_1005.heic: iPhone, Strand/Portugal)
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
- [x] Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, fehlende ignoriert
- [x] DNG (RAW): EXIF korrekt (Make, Model, Datum, GPS, Auflösung)
- [x] DNG: Grosse Dateien (25MB–97MB) verarbeitet ohne Timeout
- [x] PNG: file_type=PNG, mime=image/png korrekt
- [x] WebP: file_type=WEBP, mime=image/webp korrekt
- [x] GIF: file_type=GIF, mime=image/gif korrekt
- [x] TIFF: file_type=TIFF, mime=image/tiff korrekt
- [x] MOV: file_type=MOV, mime=video/quicktime, ffprobe-Metadaten korrekt

### IA-02: Duplikat-Erkennung
- [x] Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate"
- [x] Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert
- [x] Unterschiedliches Bild → kein Match, `status: ok`
- [x] RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`
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
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`
- [x] Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen, Pipeline läuft weiter
- [x] DJI-Drohne GPS (Teneriffa, Schweiz) → korrekt aufgelöst
- [x] Video GPS (ffprobe ISO 6709) → korrekt geocodiert

### IA-04: Temp. Konvertierung für KI
- [x] JPG/PNG/WebP → keine Konvertierung, `converted: false`
- [x] HEIC → temp JPEG erstellt, KI-Analyse erfolgreich
- [x] DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG
- [x] GIF → Konvertierung versucht (convert nicht verfügbar), KI analysiert trotzdem direkt
- [x] TIFF → keine Konvertierung nötig, direkt analysierbar
- [x] Konvertierung fehlgeschlagen → Fehler gefangen (korruptes Video, fehlender convert)
- [x] Video mit VIDEO_THUMBNAIL_ENABLED = True → mehrere Thumbnails extrahiert
- [x] Video-Thumbnail: Dauer korrekt ermittelt, Frames gleichmässig verteilt
- [x] Video-Thumbnail: ffmpeg nicht verfügbar / Fehler → Fehler gefangen, `converted: false`
- [x] MOV Video → 5 Thumbnails extrahiert, KI-Analyse erfolgreich

### IA-05: KI-Analyse
- [x] Persönliches Foto → `type: personal`, sinnvolle Tags
- [x] Screenshot → `type: screenshot` (Statusleiste, Navigationsbar erkannt)
- [x] Internet-Bild → `type: internet_image` (generierte PNG/WebP/TIFF)
- [x] KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`
- [x] Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben
- [x] DNG-Konvertierung für KI-Analyse funktioniert
- [x] Video-Thumbnails (5 Frames) für KI-Analyse
- [x] Sehr kleine Bilder (<16px) → übersprungen mit Meldung
- [x] DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt
- [x] Unscharfes Foto → `quality: blurry`

### IA-06: OCR
- [x] Screenshot mit Text → `has_text: true`, Text korrekt erkannt
- [x] Foto ohne Text (Smart-Modus) → OCR übersprungen (`type=personal, OCR nicht nötig`)
- [x] Smart-Modus: Screenshot → OCR ausgeführt
- [x] Always-Modus → OCR wird immer ausgeführt (auch für normale Fotos)
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`

### IA-07: EXIF-Tags schreiben
- [x] AI-Tags werden als Keywords geschrieben
- [x] AI-Type wird als Keyword geschrieben
- [x] Geocoding-Daten (Land, Stadt etc.) als Keywords
- [x] Ordner-Tags als Keywords + `album:` Tag (z.B. `Ferien`, `Spanien`, `album:Ferien Spanien`)
- [x] `OCR` Flag bei erkanntem Text (screenshot_test.png)
- [x] `blurry` Tag bei schlechter Qualität
- [x] Kein mood-Tag (indoor/outdoor) geschrieben
- [x] Kein quality-Tag ausser bei blurry
- [x] Description aus AI + Geocoding zusammengebaut
- [x] OCR-Text in UserComment geschrieben
- [x] Dry-Run → Tags berechnet (`keywords_planned`) aber nicht geschrieben
- [x] Datei-Hash nach Schreiben neu berechnet
- [x] `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschrieben
- [x] DNG: Tags korrekt geschrieben (file_size ändert sich)
- [x] MP4: Tags korrekt in Video geschrieben
- [x] Modul deaktiviert / keine Tags → `status: skipped, reason: no tags to write`

### IA-08: Sortierung
- [x] `personal` → photos/{YYYY}/{YYYY-MM}/
- [x] `screenshot` → screenshots/{YYYY}/
- [x] `internet_image` → sourceless/{YYYY}/
- [x] Video → videos/{YYYY}/{YYYY-MM}/
- [x] Unklar (kein EXIF, KI unsicher) → Status "review", Datei in unknown/review/ (Bug B10 behoben in v2.4.3)
- [x] Immich Upload → Datei hochgeladen, Quelle gelöscht
- [x] Immich: screenshot → Asset archiviert (`immich_archived: true`)
- [x] Namenskollision → automatischer Counter (_1, _2, ...) (screenshot_test → screenshot_test_1)
- [x] Dry-Run → Zielpfad berechnet, nicht verschoben
- [x] Leere Quellordner aufgeräumt (wenn folder_tags aktiv)
- [x] EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit)
- [x] ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst
- [x] DNG nach korrektem Jahresordner sortiert (2022, 2023, 2024)
- [x] Video nach korrektem Jahresordner sortiert (nach Datum-Fix v2.4.2)

### IA-09: Benachrichtigung
- [x] Fehler vorhanden → E-Mail gesendet
- [x] Kein Fehler → keine E-Mail
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`

### IA-10: Cleanup
- [x] Temp JPEG aus IA-04 gelöscht (DNG-Konvertierung + Video-Thumbnails)
- [x] Keine temp Dateien → nichts zu tun

### IA-11: SQLite Log
- [x] Zusammenfassung korrekt (Typ, Tags, Ort, Ziel)
- [x] Log-Eintrag in system_log Tabelle erstellt

## 2. Pipeline-Fehlerbehandlung

- [x] Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuft weiter
- [x] Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finalizer laufen trotzdem
- [x] Fehler-Datei nach error/ verschoben mit .log Datei (Traceback, Debug-Key, Zeitpunkt)
- [x] Voller Traceback in error_message, step_result und System-Log
- [x] Retry: fehlgeschlagener Job kann erneut verarbeitet werden (POST /api/job/{key}/retry)
- [x] Job Delete: Job aus DB gelöscht, Datei aus error/ entfernt (POST /api/job/{key}/delete)
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
- [x] AI-Beschreibung, Tags, Metadaten angezeigt
- [x] Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar)
- [x] Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at
- [x] Bildabmessungen (Auflösung) angezeigt
- [x] Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden)
- [x] Kategorie-Buttons: Foto, Video, Screenshot, Sourceless
- [x] Löschen-Button entfernt Review-Datei
- [x] Lokal: Datei in richtigen Zielordner verschoben (Review → Photo)
- [x] Immich: Review-Items werden über classify-all archiviert
- [x] Batch: "Alle → Sourceless" funktioniert (beide lokale und Immich-Items)

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
- [x] Job löschen und Retry funktioniert (API-Endpunkte getestet)
- [x] Preview-Badge bei Dry-Run-Jobs angezeigt

## 4. Filewatcher-Stabilität

- [x] Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet
- [x] Nach 2s Wartezeit: Dateigrösse wird erneut geprüft
- [x] Dateigrösse stabil → Verarbeitung startet
- [x] Dateigrösse geändert → erneute Wartezeit
- [x] Leere Datei (0 Bytes) → wird als "unstable" übersprungen (current_size > 0 Check)
- [x] Nicht unterstütztes Format (.txt) → wird vom Filewatcher ignoriert
- [x] Bereits verarbeitete Datei erneut in Inbox → Filewatcher überspringt (done_hashes)
- [x] Dry-Run-Jobs werden in done_hashes berücksichtigt
- [x] Immich-Assets werden in done_hashes berücksichtigt
- [x] Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft)
- [x] Docker-Logging: Alle Filewatcher-Aktionen in stdout sichtbar
- [x] Unterordner in Inbox → Dateien werden rekursiv gefunden und verarbeitet

## 5. Immich-Integration

- [x] Upload: Datei wird hochgeladen, Asset-ID gespeichert
- [x] Upload: Album aus Ordner-Tags erstellt (Ferien/Spanien → "Ferien Spanien")
- [x] Upload: Screenshots werden archiviert (`immich_archived: true`)
- [x] Duplikat-Erkennung über Immich-Assets hinweg
- [x] Immich nicht erreichbar → Fehler geloggt, Status error, E-Mail gesendet
- [x] DNG nach Immich hochgeladen (25MB RAW)
- [x] MP4 nach Immich hochgeladen (304MB Video)
- [x] JPG nach Immich hochgeladen (mit GPS/Tags)
- [x] Cross-Mode Duplikat: Dateiablage → Immich erkannt

## 6. Dateiformate

- [x] JPG/JPEG — Verarbeitung + KI + Tags schreiben
- [x] PNG — Verarbeitung + KI + Tags schreiben (test_landscape.png → internet_image/sourceless)
- [x] HEIC — Konvertierung + KI + Tags schreiben (IMG_1005.heic → personal/photos, 11 Tags)
- [x] WebP — Verarbeitung + KI (test_image.webp → internet_image/sourceless)
- [x] GIF — KI direkt analysiert (convert nicht verfügbar, aber Pipeline läuft weiter)
- [x] TIFF — Verarbeitung + KI + Tags schreiben (test_image.tiff → internet_image/sourceless)
- [x] DNG — PreviewImage für KI + pHash, Tags schreiben, grosse Dateien (25–97MB)
- [x] MP4 — Video erkannt, ffprobe-Metadaten, Thumbnails, KI, Tags schreiben, korrekt sortiert
- [x] MOV — Video erkannt, ffprobe, 5 Thumbnails, KI, Tags, korrekt sortiert (IMG_7267.mov)
- [x] Nicht unterstütztes Format (.txt) → vom Filewatcher ignoriert (SUPPORTED_EXTENSIONS Filter)

## 7. Edge Cases

- [x] Leere Datei (0 Bytes) → Filewatcher überspringt als "unstable"
- [x] Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert (97MB DNG, 304MB MP4)
- [x] Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet
- [x] Dateiname mit Leerzeichen und Klammern → korrekt verarbeitet (`DJI_0061 (2).JPG`)
- [x] Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust (Batch 4+ Dateien)
- [x] Verschlüsselte Config-Werte → korrekt entschlüsselt
- [x] Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash
- [x] Sehr kleine Bilder (<16px) → KI-Analyse übersprungen
- [x] Unscharfes Foto → KI erkennt `quality: blurry`, Tag geschrieben
- [x] Namenskollision → Counter _1, _2 angehängt (screenshot_test → screenshot_test_1)
- [x] Dateien in Unterordnern → rekursiv erkannt und verarbeitet
- [x] UUID-Dateiname (WhatsApp-Format) ohne EXIF + keine KI → Status "review"

## 8. Security (v2.4.4–v2.4.5)

- [x] Path Traversal: EXIF country `../../etc` → sanitisiert zu `__etc`, bleibt in Bibliothek
- [x] Path Traversal: `_validate_target_path()` blockiert `/bibliothek/../etc` mit ValueError
- [x] Path Traversal: Normaler EXIF-Wert (Schweiz/Zürich) wird durchgelassen
- [x] Immich Filename: `../../etc/passwd` → `os.path.basename()` → `passwd`
- [x] Immich Filename: Leerer Name → Fallback auf `asset_id.jpg`
- [x] Dateigrössenlimit: `MAX_FILE_SIZE = 10 GB` korrekt gesetzt
- [ ] Dateigrössenlimit: Datei > 10 GB wird im Filewatcher übersprungen (nicht testbar ohne 10GB Datei)

## 9. Performance (v2.5.0)

- [x] DB-Indexes: 7/7 Indexes auf jobs + system_logs erstellt
- [x] Dashboard: 1 GROUP BY Query statt 6 COUNT Queries
- [x] Dashboard JSON-Endpoint Antwortzeit: **7ms** (< 100ms Limit)
- [x] Duplikat pHash: Batched Query (BATCH_SIZE=5000, nur leichte Spalten)
- [x] safe_move: Datei wird nur 1× gelesen — 100KB Random-Daten Integrität verifiziert
- [x] Immich Upload: Streaming von Disk (kein `f.read()`)
- [x] Log-Rotation: `LOG_RETENTION_DAYS = 90`, stündliche Prüfung
- [x] Temp-Cleanup: `shutil.rmtree()` bei fehlgeschlagenen Immich-Downloads
- [x] Docker: Memory-Limit 2 GB und CPU-Limit 2.0 aktiv (cgroup verifiziert)

## 10. Nicht getestet (erfordern spezifische Infrastruktur)

- [ ] Photon-Provider (erfordert Photon-Server)
- [ ] CR2/NEF/ARW Formate (keine Testdateien vorhanden)
- [ ] Immich Polling (erfordert Upload via Immich Mobile App)
- [ ] Immich Replace (erfordert Polling-Aktivierung + neues Asset)
- [ ] Container-Neustart während Verarbeitung (risikobehaftet)
- [ ] HEIC Lightbox (erfordert Browser-Test)
- [ ] ffprobe nicht verfügbar (fest im Container installiert)
- [ ] Video < 1s Thumbnail (Seek-Position > Videolänge, bekanntes Limit)

## 11. DJI-Testdaten Ergebnisse (v2.4.2)

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

### Dateiformat-Tests (v2.4.3)
| Format | Datei | EXIF | Konvertierung | KI | Tags | Sortierung |
|--------|-------|------|--------------|-----|------|-----------|
| PNG | test_landscape.png | ✅ | Nicht nötig | ✅ internet_image | ✅ 8 | ✅ sourceless |
| HEIC | IMG_1005.heic | ✅ | ✅ temp JPEG | ✅ personal | ✅ 11 | ✅ photos/2026-03 |
| WebP | test_image.webp | ✅ | Nicht nötig | ✅ internet_image | ✅ 7 | ✅ sourceless |
| GIF | test_image.gif | ✅ | ⚠️ convert fehlt | ✅ direkt | ✅ 7 | ✅ sourceless |
| TIFF | test_image.tiff | ✅ | Nicht nötig | ✅ internet_image | ✅ 7 | ✅ sourceless |
| MOV | IMG_7267.mov | ✅ | ✅ 5 Frames | ✅ personal | ✅ 11 | ✅ videos/2025-04 |

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

### Modul-Deaktivierungs-Tests (v2.4.3)
| Modul | Ergebnis |
|-------|----------|
| IA-02 Duplikat-Erkennung | ✅ `skipped, module disabled` |
| IA-03 Geocoding | ✅ `skipped, module disabled` |
| IA-05 KI-Analyse | ✅ `skipped, module disabled` |
| IA-06 OCR | ✅ `skipped, module disabled` |
| IA-07 Tags (indirekt) | ✅ `skipped, no tags to write` |
| IA-09 SMTP | ✅ `skipped, module disabled` |

### Gefundene Bugs (alle behoben)
| Bug | Version | Beschreibung |
|-----|---------|-------------|
| B1: Filewatcher done_hashes | v2.4.1 | Dry-Run-Jobs und fehlende Targets nicht berücksichtigt |
| B2: Filewatcher Re-Import | v2.4.1 | Gelöschte Zieldateien nicht erkannt → Datei ignoriert |
| B3: ExifTool-Fehler | v2.4.1 | Korrupte Dateien: generische Fehlermeldung statt hilfreicher Text |
| B4: Kleine Bilder | v2.4.1 | <16px Bilder → KI-API Fehler 400 statt Überspringen |
| B5: pHash False Positives | v2.4.1 | Threshold 5 zu hoch → unähnliche Bilder als Duplikat |
| B6: Batch-Clean Label | v2.4.1 | Unklar ob nur exakte oder auch ähnliche Duplikate |
| B7: Dry-Run Badge | v2.4.1 | Kein visueller Hinweis auf Preview-Jobs in Logs |
| B8: Docker Logging | v2.4.1 | Pipeline-Logs nur in SQLite, nicht in stdout |
| B9: Video-Datum | v2.4.2 | ISO 8601 mit `.000000Z` nicht geparst → falscher Jahresordner |
| B10: Review-Status | v2.4.3 | Pipeline überschrieb "review" mit "done" → unklare Dateien nicht in Review |

### E2E Regressiontest v2.5.0

| Datei | Format | EXIF | Duplikat | Geocoding | Konvert. | KI | Tags | Sortierung | Ziel |
|-------|--------|------|----------|-----------|----------|-----|------|-----------|------|
| test_v250_panasonic.JPG | JPEG | ✅ date=2012 | ✅ ok | skip (kein GPS) | nicht nötig | ✅ personal | ✅ 8 | ✅ photos/2012/2012-02 | ✅ |
| test_v250_iphone.heic | HEIC | ✅ date=2026 | ✅ ok | ✅ Cascais, PT | ✅ temp JPEG | ✅ personal | ✅ 11 | ✅ photos/2026/2026-03 | ✅ |
| test_v250_dji.DNG | DNG | ✅ date=2022 | ✅ pHash | ✅ Churwalden, CH | ✅ preview | ✅ personal | ✅ 10 | ✅ photos/2022/2022-02 | ✅ |
| test_v250_video.mov | MOV | ✅ date=2025 | ✅ ok | ✅ Baden, CH | ✅ 5 frames | ✅ personal | ✅ 11 | ✅ videos/2025/2025-04 | ✅ |
| test_v250_landscape.png | PNG | ✅ no EXIF | ✅ pHash | skip | nicht nötig | ✅ internet_image | ✅ 8 | ✅ sourceless/2026 | ✅ |
| test_v250_abstract.webp | WebP | ✅ no EXIF | ✅ pHash | skip | nicht nötig | ✅ internet_image | ✅ 8 | ✅ sourceless/2026 | ✅ |
| test_v250_warm.tiff | TIFF | ✅ no EXIF | ✅ pHash | skip | nicht nötig | ✅ internet_image | ✅ 8 | ✅ sourceless/2026 | ✅ |
| test_v250_green.gif | GIF | ✅ no EXIF | ✅ pHash | skip | ⚠️ convert fehlt | ✅ internet_image | ✅ 7 | ✅ sourceless/2026 | ✅ |
| UUID messenger file | JPEG | ✅ no EXIF | ✅ pHash | skip | nicht nötig | ✅ personal | ✅ 8 | ✅ photos/2026/2026-03 | ✅ |
| test_v250_panasonic_dup.JPG | JPEG | — | ✅ SHA256 exact | — | — | — | — | ✅ duplicate | ✅ |
| test_v250_noai.jpg (AI off) | JPEG | ✅ no EXIF | ✅ ok | skip | nicht nötig | ⏭️ skipped | — | ✅ unknown/review | ✅ |

**Ergebnis: 11/11 Tests bestanden** — alle Formate, Duplikate, Modul-Disable korrekt verarbeitet.

### Vollständiger Regressionstest v2.5.0 — 30.03.2026

19 Jobs verarbeitet, 228/237 Tests bestanden (9 nicht testbar wegen fehlender Infrastruktur).

#### Funktionale Tests

| Test | Beschreibung | Ergebnis |
|------|-------------|----------|
| Dry-Run (Duplikat) | `T50_dryrun_test.JPG` → SHA256-Match, `dry_run=True`, Datei bleibt in Inbox | ✅ |
| Dry-Run (Unique) | `T51_dryrun_unique.jpg` → Pipeline komplett, Target berechnet (`photos/2026/2026-03/`), Datei bleibt in Inbox | ✅ |
| Subfolder Rekursion | `vacation/spain/T52_subfolder.jpg` → aus verschachteltem Unterordner verarbeitet | ✅ |
| Namenskollision | `screenshot_test.png` → `screenshot_test_1.png` (Counter-Suffix) | ✅ |
| Geocoding Non-Critical | IA-03 in `non_critical` Set → Fehler gefangen, Pipeline fährt fort | ✅ |
| Leere Datei | `T10_empty.jpg` (0 Bytes) → Filewatcher ignoriert als "unstable" | ✅ |
| Nicht-unterstützt | `T11_document.txt` → Filewatcher ignoriert (SUPPORTED_EXTENSIONS) | ✅ |
| SMTP: kein Fehler | `IA-09: {"status": "skipped", "reason": "no errors to report"}` | ✅ |
| SMTP: mit Fehler | `IA-09: {"sent": true, "recipient": "ds@marcohediger.ch", "errors_reported": 1}` | ✅ |
| Modul-Disable: AI | `T30_noai.jpg` → AI skipped, Status review → classify als sourceless | ✅ |
| Modul-Disable: Geo | `T31_nogeo.jpg` → Geocoding skipped | ✅ |
| Modul-Disable: OCR | `T32_noocr.jpg` → OCR skipped | ✅ |
| Modul-Disable: Dup | `T33_nodup.jpg` → Duplikat-Erkennung skipped | ✅ |
| Sonderzeichen | `T12_Ferien Foto (2026) #1.jpg` → korrekt verarbeitet | ✅ |
| UUID-Dateiname | `b2c3d4e5-f6a7-8901-bcde-f12345678901.jpg` → screenshots/2026/ | ✅ |
| Duplikat Exact | `T01_panasonic.JPG` (SHA256-Match) → duplicate, Datei in error/duplicates/ | ✅ |

#### Security-Tests

| Test | Beschreibung | Ergebnis |
|------|-------------|----------|
| S1-1 | `_sanitize_path_component("../../etc/passwd")` → `"__etc_passwd"` | ✅ |
| S1-2 | `_sanitize_path_component("Zürich")` → `"Zürich"` (normaler Wert durchgelassen) | ✅ |
| S1-3 | `_validate_target_path("/etc/passwd", "/bibliothek")` → `ValueError` raised | ✅ |
| S1-4 | `_validate_target_path("/bibliothek/photos/2026", "/bibliothek")` → akzeptiert | ✅ |
| S1-5 | Control-Characters (`\x00\x01\x1f`) → entfernt | ✅ |
| S7 | `MAX_FILE_SIZE = 10737418240` (10 GB) korrekt gesetzt | ✅ |
| S8-1 | `_sanitize_filename("../../etc/passwd")` → `"passwd"` | ✅ |
| S8-2 | `_sanitize_filename("/etc/passwd")` → `"passwd"` | ✅ |
| S8-3 | `_sanitize_filename("")` → `"asset.jpg"` (Fallback) | ✅ |
| S8-4 | `_sanitize_filename(None)` → `"asset.jpg"` (Fallback) | ✅ |
| S8-5 | `_sanitize_filename("photo_2026.jpg")` → `"photo_2026.jpg"` (normaler Wert) | ✅ |

#### Performance-Tests

| Test | Beschreibung | Ergebnis |
|------|-------------|----------|
| R1 | Immich `upload_asset()`: Streaming mit `files=` (kein `f.read()`) | ✅ |
| R2 | Dashboard JSON: 17ms avg (3 Runs: 14ms, 15ms, 22ms) — unter 100ms Limit | ✅ |
| R3 | pHash Batching: `BATCH_SIZE=5000`, `.offset()` + `.limit()`, SHA256 `.limit(10)` | ✅ |
| R4 | 7/7 DB-Indexes vorhanden: `idx_job_status`, `idx_job_file_hash`, `idx_job_phash`, `idx_job_original_path`, `idx_job_created_at`, `idx_job_updated_at`, `idx_syslog_created_at` | ✅ |
| R5 | Docker Limits: Memory=2147483648 (2GB), NanoCPUs=2000000000 (2.0) | ✅ |
| R6 | `shutil.rmtree()` in filewatcher.py (Zeile 251) | ✅ |
| R7 | `LOG_RETENTION_DAYS=90`, `_CLEANUP_INTERVAL=3600s` (1h) | ✅ |
| R8 | `safe_move`: Streaming Hash (`f_out.write(chunk)` + `src_hash.update(chunk)`), Source 1× gelesen | ✅ |

#### Endpoint-Performance v2.5.0

| Endpoint | Status | Antwortzeit |
|----------|--------|-------------|
| `/` (Dashboard) | 200 | 744ms (initial) |
| `/api/dashboard` (JSON) | 200 | 15ms |
| `/review` | 200 | 36ms |
| `/logs` | 200 | 19ms |
| `/settings` | 200 | 100ms |
| `/duplicates` | 200 | 40ms |

#### Bibliothek-Struktur nach Test

```
/bibliothek/
├── photos/
│   ├── 2012/2012-02/  T01_panasonic.JPG, test_v250_panasonic.JPG
│   ├── 2022/2022-02/  T03_dji_raw.DNG, test_v250_dji.DNG
│   └── 2026/2026-03/  T02_iphone.heic, T52_subfolder.jpg, UUID-messenger
├── videos/
│   └── 2025/2025-04/  T04_video.mov, test_v250_video.mov
├── sourceless/
│   └── 2026/          T05-T08 (PNG/WebP/TIFF/GIF), T30-T33 (Modul-Tests)
├── screenshots/
│   └── 2026/          screenshot_test.png, screenshot_test_1.png (Kollision)
└── error/
    └── duplicates/    T01_panasonic.JPG (SHA256 exact duplicate)
```

### Bekannte Einschränkungen
| Thema | Beschreibung |
|-------|-------------|
| GIF-Konvertierung | `convert` (ImageMagick) nicht im Container → GIF wird direkt an KI gesendet |
| Video < 1s | Thumbnail-Extraktion scheitert (Seek-Position > Videolänge) |
| Leere Ordner | Werden nur aufgeräumt wenn `folder_tags` aktiv ist |
| SMTP leerer Wert | JSON-encoded leerer String `""` wird nicht als "nicht konfiguriert" erkannt |

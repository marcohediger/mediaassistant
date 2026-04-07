# Testplan — MediaAssistant

> Letzter Race-Condition-Testlauf: **v2.28.3 — 2026-04-07** (26/26 bestanden, alle Tests in `backend/test_duplicate_fix.py`)
> Letzter vollständiger Testlauf: **v2.17.1 — 2026-04-02** (296/305 bestanden, 9 nicht testbar)
> Exotische Tests: 42 Zusatztests, 3 Bugs gefunden und behoben
> Slow-System Test: 0.5 CPU / 512MB RAM (Synology-Simulation) bestanden
> Testdaten: Panasonic DMC-GF2 JPGs, DJI FC7203 JPGs, iPhone 12 Pro HEIC/MOV, Casio EX-S600 JPGs, generierte PNG/GIF/WebP/TIFF, UUID Messenger-Dateien
> Container: v2.9.0, Docker 2GB RAM / 2 CPUs, SQLite mit 7 Indexes
> Testlauf: 19 Jobs verarbeitet (16 done, 3 duplicate, 0 error, 0 review), Inbox leer nach Abschluss
>
> **v2.8.0 Änderungen**: Kategorien sind dynamisch aus DB (library_categories). Statische Regeln primär, KI verifies/korrigiert. AI gibt type (DB-Key), source (Herkunft), tags (beschreibend) zurück. Review-Buttons dynamisch. EXIF-Tags: IA-07 schreibt AI-Tags+Source, IA-08 schreibt Kategorie-Label+Source. Noch nicht regressionsgetestet.
>
> **v2.9.0 Änderungen**: Sorting Rules mit media_type Filter (Bilder/Videos/Alle). Video-pHash Duplikaterkennung (Durchschnitt aus IA-04 Frames). Separate Kategorien sourceless_foto/sourceless_video/personliches_video. Duplikat-"Behalten" startet volle Pipeline nach. Inbox-Garantie: keine Datei bleibt unbeachtet. Retry-Counter (max 3) gegen Crash-Loops. Immich Tag-Fix (HTTP 400). Config JSON-Crash Resilience.
>
> **v2.28.2 Änderungen**: Atomarer Claim am Anfang von `run_pipeline` via `UPDATE jobs SET status='processing' WHERE id=? AND status='queued'`. Verhindert die Race, dass derselbe Job von zwei Pipeline-Instanzen parallel verarbeitet wird (Worker + retry_job + duplicates_router + immich_poll + startup-resume sind 5 Aufrufer). Symptom-Pflaster aus älteren Releases (XMP pre-delete in IA-07, exists-Check vor Upload in IA-08) wurden entfernt, weil nicht mehr nötig. Startup-Resume setzt Status nun auf `queued` vor `run_pipeline`-Aufruf.
>
> **v2.28.3 Änderungen**: `retry_job` hatte ein Folge-TOCTOU-Window zwischen seinen zwei Commits (1. status=queued, 2. step_result aufgeräumt). Fix: transienter Lock-State `error → processing → queued` nach Cleanup. 4 neue Race-Condition-Tests (Test 5–8) zu `backend/test_duplicate_fix.py` hinzugefügt — alle 26 Tests grün.

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
- [x] Video: pHash aus Durchschnitt der IA-04 Frames berechnet (post-IA-04 Check)
- [x] Video: Re-encoded Video (anderer Codec/Bitrate) → pHash-Match, als "similar" Duplikat erkannt (MA-2026-0053, Distanz 0)
- [x] Video: Exakte Kopie eines Videos → SHA256-Match, als "exact" Duplikat erkannt (MA-2026-0052)

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
- [x] Persönliches Foto → `type: personliches_foto`, sinnvolle Tags (v2.8.0: type = DB-Key)
- [x] Screenshot → `type: screenshot` (Statusleiste, Navigationsbar erkannt)
- [x] Internet-Bild → `type: sourceless` (generierte PNG/WebP/TIFF, v2.8.0: kein internet_image mehr)
- [x] KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`
- [x] Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben
- [x] Kategorien aus DB werden im Prompt übergeben (v2.8.0, verifiziert: "Available categories: Persönliches Foto | Persönliches Video | ...")
- [x] Statische Regel-Vorklassifikation wird der KI als Kontext mitgegeben (v2.8.0, verifiziert: "Pre-classification (static rule): Persönliches Video")
- [x] KI gibt `source` (Herkunft) und `tags` (beschreibend) separat zurück (v2.8.0, verifiziert: source=Kamerafoto, tags=[Essen, Restaurant, ...])
- [x] DNG-Konvertierung für KI-Analyse funktioniert
- [x] Video-Thumbnails (5 Frames) für KI-Analyse
- [x] Sehr kleine Bilder (<16px) → übersprungen mit Meldung
- [x] DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt
- [x] Unscharfes Foto → `quality: blurry`
- [x] NSFW-Erkennung: KI gibt `nsfw: true` für nicht-jugendfreie Inhalte zurück
- [x] NSFW-Erkennung: `nsfw: false` für normale Bilder (Landschaft, Essen, etc.)

### IA-06: OCR
- [x] Screenshot mit Text → `has_text: true`, Text korrekt erkannt
- [x] Foto ohne Text (Smart-Modus) → OCR übersprungen (`type=personal, OCR nicht nötig`)
- [x] Smart-Modus: Screenshot → OCR ausgeführt
- [x] Always-Modus → OCR wird immer ausgeführt (auch für normale Fotos)
- [x] Modul deaktiviert → `status: skipped, reason: module disabled`

### IA-07: EXIF-Tags schreiben
- [x] AI-Tags werden als Keywords geschrieben
- [x] AI-Source (Herkunft) wird als Keyword geschrieben (v2.8.0, verifiziert: "Kamerafoto" in keywords_written)
- [x] Geocoding-Daten (Land, Stadt etc.) als Keywords
- [x] Ordner-Tags: Einzelwörter + zusammengesetzter Tag (z.B. `Ferien/Mallorca 2025/` → `Ferien`, `Mallorca`, `2025`, `Ferien Mallorca 2025`)
- [x] Ordner-Tags: Einfacher Ordner → nur Ordnername als Tag (z.B. `Geburtstag/` → `Geburtstag`)
- [x] Ordner-Tags: Tief verschachtelt mit Umlauten (z.B. `Ferien/Nänikon 2026/Tag 3/` → 6 Tags)
- [x] Ordner-Tags: Gemischter Inhalt (JPG + MOV + UUID im gleichen Ordner) → alle bekommen gleiche Tags
- [x] Ordner-Tags: Immich-Tags werden aus IA-07 Keywords übernommen (identisch zu EXIF-Tags)
- [x] Ordner-Tags: Immich-Album wird aus zusammengesetztem Pfad erstellt (z.B. "Ferien Mallorca 2025")
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
- [x] Statische Regeln werden immer zuerst ausgewertet (v2.8.0, verifiziert: rule_category vor AI)
- [x] KI verifies/korrigiert Kategorie gegen DB (v2.8.0, verifiziert: AI type → DB lookup)
- [x] Kategorie-Label + Source als EXIF-Keywords geschrieben (v2.8.0, verifiziert: "Persönliches Video" + "Kamerafoto" in keywords)
- [x] Pfad-Template aus library_categories DB geladen (v2.8.0, verifiziert: 8 Kategorien mit Templates)
- [x] `personliches_foto` → persoenliche_fotos/{YYYY}/{YYYY-MM}/ (v2.8.0: Key geändert)
- [x] `screenshot` → screenshots/{YYYY}/
- [x] `sourceless_foto` → sourceless/foto/{YYYY}/
- [x] `sourceless_video` → sourceless/video/{YYYY}/
- [x] `personliches_video` → videos/{YYYY}/{YYYY-MM}/
- [x] Sorting Rule media_type=image → Regel wird nur auf Bilder angewendet, Videos übersprungen
- [x] Sorting Rule media_type=video → Regel wird nur auf Videos angewendet, Bilder übersprungen
- [x] iPhone MOV (make=Apple) → Pre-Classification "Persönliches Video", Kategorie personliches_video (MA-2026-0049)
- [x] UUID MP4 ohne EXIF → Pre-Classification "Sourceless Video", Kategorie sourceless_video (MA-2026-0050)
- [x] WhatsApp Video (-WA im Namen) → Kategorie sourceless_video (Regeltest verifiziert)
- [x] KI-Prompt enthält korrekte Pre-Classification für Videos (nicht "Persönliches Foto") (MA-2026-0049)
- [x] KI gibt "Kameravideo" statt "Kamerafoto" als Source zurück bei Videos (Prompt aktualisiert, Beispiele vorhanden)
- [x] Unklar (kein EXIF, KI unsicher) → Status "review", Datei in unknown/review/
- [x] Immich Upload → Datei hochgeladen, Quelle gelöscht
- [x] Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB (verifiziert: screenshot+sourceless archived=True, personal archived=False)
- [x] Immich: NSFW-Bild → gesperrter Ordner (`visibility: locked`), nicht archiviert (locked hat Vorrang)
- [x] Immich: NSFW-Lock funktioniert im Upload-Pfad (Inbox → Immich)
- [x] Immich: NSFW-Lock funktioniert im Replace-Pfad (Polling → Immich)
- [x] Namenskollision → automatischer Counter (_1, _2, ...)
- [x] Dry-Run → Zielpfad berechnet, nicht verschoben
- [x] Leere Quellordner aufgeräumt (wenn folder_tags aktiv)
- [x] EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit)
- [x] ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst
- [x] DNG nach korrektem Jahresordner sortiert
- [x] Video nach korrektem Jahresordner sortiert

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
- [x] Job in "processing" nach Crash → max. 3 Retry-Versuche, danach Status "error" (MAX_RETRIES=3, retry_count in DB)
- [x] Retry-Counter wird bei jedem Neustart-Versuch hochgezählt und geloggt
- [x] **Atomic Claim (v2.28.2)**: `run_pipeline` weigert sich, einen Job zu verarbeiten, der nicht im Status `queued` ist — verhindert Doppel-Ausführung
- [x] **Atomic Claim (v2.28.2)**: 10 parallele `run_pipeline(same_id)`-Aufrufe → 9 brechen mit Log-Eintrag `already claimed by another caller — skipping` ab
- [x] **Atomic Claim (v2.28.2)**: `run_pipeline` auf Job mit Status `done`/`processing`/`error` → No-op, keine Status-Änderung
- [x] **Startup-Resume (v2.28.2)**: Resume setzt Status auf `queued` bevor `run_pipeline` aufgerufen wird, damit der atomare Claim greift
- [x] **retry_job (v2.28.3)**: Atomarer Claim `error → processing` (transienter Lock-State während Cleanup), dann `queued` für `run_pipeline`
- [x] **retry_job (v2.28.3)**: 5 parallele `retry_job(same_id)`-Aufrufe → exakt 1× True, 4× False (Doppelklick-/Multi-Tab-Schutz)
- [x] **retry_job (v2.28.3)**: `retry_job` parallel zu Worker-`run_pipeline` → kein stale step_result, IA-01 wird tatsächlich frisch ausgeführt

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
- [x] Ziel-Ablagen (library_categories): Key, Label, Pfad-Template, Immich-Archiv, Position (8 Kategorien verifiziert)
- [x] Sorting Rules: Medientyp-Filter (Alle/Bilder/Videos) in UI und Logik (8 Regeln mit media_type verifiziert)
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
- [x] "Dieses behalten" → volle Pipeline wird nachgeholt (KI, Tags, Sortierung/Immich) (MA-2026-0073: IA-05+07+08 nachgeholt)
- [x] "Dieses behalten" bei Immich-Gruppe → KI + Tags + Upload zu Immich (MA-2026-0073 → immich:5866e694...)
- [x] "Dieses behalten" bei lokaler Gruppe → KI + Tags + lokale Ablage
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
- [x] Kategorie-Buttons dynamisch aus DB geladen (v2.8.0, review.py referenziert library_categories)
- [x] Löschen-Button entfernt Review-Datei
- [x] Lokal: Datei in richtigen Zielordner verschoben (Review → Photo)
- [x] Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB (verifiziert: screenshot+sourceless archived)
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
- [x] Bereits verarbeitete Datei erneut in Inbox → wird erneut verarbeitet, IA-02 erkennt Duplikat (MA-2026-0056/0057)
- [x] Datei liegt nach Verarbeitung noch in Inbox (Move fehlgeschlagen) → wird erneut verarbeitet
- [x] Dry-Run-Jobs werden in done_hashes berücksichtigt (Datei bleibt absichtlich in Inbox)
- [x] Immich-Assets werden in done_hashes berücksichtigt
- [x] Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft)
- [x] Keine Datei bleibt dauerhaft unbeachtet in der Inbox liegen (ausser Dry-Run)
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
- [x] Immich: Alle Tags korrekt zugewiesen (auch bereits existierende Tags, HTTP 400 Handling) (MA-2026-0039: 7/7 Tags)
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
- [x] Ungültiges JSON in Config-Wert → kein Crash, Rohwert zurückgegeben (getestet: "not valid json {" → Rohstring)
- [x] Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash
- [x] Sehr kleine Bilder (<16px) → KI-Analyse übersprungen
- [x] Unscharfes Foto → KI erkennt `quality: blurry`, Tag geschrieben
- [x] Namenskollision → Counter _1, _2 angehängt (screenshot_test → screenshot_test_1)
- [x] Dateien in Unterordnern → rekursiv erkannt und verarbeitet
- [x] UUID-Dateiname (WhatsApp-Format) ohne EXIF + keine KI → Status "review"

## 8. Security (v2.4.4–v2.4.5)

- [x] Path Traversal: EXIF country `../../etc` → sanitisiert zu `__etc`, bleibt in Bibliothek
- [x] Path Traversal: `_validate_target_path()` blockiert `/library/../etc` mit ValueError
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
| D1 | DNG erneut einfügen (SHA256) | ✅ Filewatcher verarbeitet, IA-02 erkennt Duplikat |
| D2 | Video erneut einfügen (SHA256) | ✅ Filewatcher verarbeitet, IA-02 erkennt Duplikat |
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
| SMTP: mit Fehler | `IA-09: {"sent": true, "recipient": "user@example.com", "errors_reported": 1}` | ✅ |
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
| S1-3 | `_validate_target_path("/etc/passwd", "/library")` → `ValueError` raised | ✅ |
| S1-4 | `_validate_target_path("/library/photos/2026", "/library")` → akzeptiert | ✅ |
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
/library/
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

## 12. Exotische Tests (v2.17.1)

> Testlauf: **v2.17.1 — 2026-04-02**, Container 0.5 CPU / 512MB RAM (Synology-Simulation)

### Format/Extension-Mismatch

- [x] JPG mit .png Extension → IA-01 erkennt `file_type=JPEG`, IA-07 überspringt mit "format mismatch" statt Crash
- [x] PNG mit .jpg Extension → IA-07 überspringt mit "format mismatch"
- [x] MP4 als .mov umbenannt → Pipeline verarbeitet korrekt (ffprobe erkennt Format)
- [x] Zufällige Binärdaten als .jpg → IA-01 Fehler "konnte Datei nicht lesen", kein Crash

### Extreme Dateinamen

- [x] 200+ Zeichen Dateiname → korrekt verarbeitet
- [x] Emoji im Dateinamen (🏔️_Berge_🌅.jpg) → korrekt verarbeitet, Immich-Upload OK
- [x] Chinesisch/Japanisch (测试照片_テスト.jpg) → korrekt verarbeitet, Immich-Upload OK
- [x] Nur Punkte (`...jpg`) → korrekt ignoriert (kein Extension-Match)
- [x] Leerzeichen-Name (`   .jpg`) → korrekt verarbeitet
- [x] Doppelte Extension (`photo.jpg.jpg`) → korrekt verarbeitet
- [x] Uppercase Extension (`PHOTO.JPEG`) → `.lower()` normalisiert korrekt

### Extreme Bilddimensionen

- [x] 1x1 Pixel Bild → pHash berechnet, korrekt verarbeitet
- [x] 10000x100 Panorama → korrekt verarbeitet
- [x] 16x16 Pixel (an KI-Schwelle) → korrekt verarbeitet
- [x] 15x15 Pixel (unter KI-Schwelle) → KI übersprungen "Bild zu klein"
- [x] Solid Black / Solid White → pHash `0000...` / `8000...`, korrekt verarbeitet

### EXIF Edge Cases

- [x] Zukunftsdatum (2030-01-01) → Datum korrekt gelesen, Sortierung in 2030/
- [x] Sehr altes Datum (1900-01-01) → korrekt verarbeitet
- [x] GPS Longitude=0 (Greenwich-Meridian) → Geocoding korrekt "Vereinigtes Königreich / Groß-London" (Bug in v2.17.0 behoben)
- [x] GPS Latitude=0 (Äquator) → gps=true, Geocoding ausgeführt (Bug in v2.17.0 behoben)
- [x] Ungültige GPS (999,999) → "skipped, invalid GPS coordinates" (Validierung in v2.17.1 hinzugefügt)
- [x] GPS Null Island (0,0) → Geocoding wird ausgeführt (Bug in v2.17.0 behoben)
- [x] 10KB EXIF Description → ExifTool verarbeitet ohne Probleme
- [x] XSS in EXIF Keywords (`<script>alert(1)</script>`) → wird nicht in KI-Tags übernommen

### Synology-spezifisch

- [x] `@eaDir` Verzeichnis → korrekt ignoriert (`_SKIP_DIRS` in filewatcher.py)
- [x] `.DS_Store` Datei → ignoriert (keine unterstützte Extension)
- [x] `Thumbs.db` Datei → ignoriert (keine unterstützte Extension)
- [x] Versteckte Datei (`.hidden_photo.jpg`) → wird verarbeitet (korrekt, versteckte Dateien mit gültiger Extension sind gültige Eingaben)

### Stress / Concurrent

- [x] 10 Dateien gleichzeitig → alle korrekt verarbeitet, sequentielle Abarbeitung
- [x] Gleiche Datei 5x mit verschiedenen Namen → 1 done + 4 SHA256-Duplikate
- [x] Datei vor Filewatcher-Pickup gelöscht → kein Crash, kein Job erstellt
- [x] 15 Dateien in Queue auf langsamem System → alle verarbeitet, kein OOM

### Grosse Dateien auf langsamem System

- [x] 97MB DNG → korrekt verarbeitet, Memory ~260MB
- [x] 273MB MP4 Video → korrekt verarbeitet, Memory unter 260MB
- [x] 8MB PNG → korrekt verarbeitet

### API Edge Cases

- [x] Ungültiger Job-Key für Retry → `{"status":"error","message":"Job nicht gefunden"}`
- [x] Nicht-existenter Job löschen → Redirect ohne Fehlerseite
- [x] Dashboard mit 0 Jobs → korrekte Antwort, alle Werte 0

### Settings Security (v2.17.1)

- [x] Partieller POST ohne `_form_token` → abgelehnt mit "invalid_form" Fehler
- [x] Vollständiger POST mit `_form_token` → akzeptiert
- [x] XSS-Payload in Textfeldern → HTML-escaped gespeichert (`&lt;script&gt;`)
- [x] Module-Checkboxen nur aktualisiert wenn `_form_token` vorhanden

### Gefundene und behobene Bugs (v2.17.1)

| Bug | Beschreibung | Fix |
|-----|-------------|-----|
| GPS lon=0 / lat=0 | `bool(0)` ist False → GPS am Äquator/Greenwich ignoriert | `is not None` Check |
| GPS Validierung | GPS lat=999, lon=999 akzeptiert | Range-Check -90..90 / -180..180 |
| Format-Mismatch | JPG als .png → ExifTool Write crasht | Mismatch-Erkennung vor Write |
| Settings partieller POST | Wiped alle Module + Config | `_form_token` Guard |
| Settings XSS | Ungefilterte Eingabe in Config gespeichert | `html.escape()` Sanitisierung |

### Bekannte Einschränkungen
| Thema | Beschreibung |
|-------|-------------|
| GIF-Konvertierung | `convert` (ImageMagick) nicht im Container → GIF wird direkt an KI gesendet |
| Video < 1s | Thumbnail-Extraktion scheitert (Seek-Position > Videolänge) |
| Leere Ordner | Werden nur aufgeräumt wenn `folder_tags` aktiv ist |
| SMTP leerer Wert | JSON-encoded leerer String `""` wird nicht als "nicht konfiguriert" erkannt |
| `...jpg` Dateiname | `os.path.splitext("...jpg")` gibt keine Extension → still ignoriert |
| Max-Retry nur bei Start | `retry_count > MAX_RETRIES` Check nur beim Container-Start, nicht im laufenden Betrieb |

## 13. Race-Condition-Tests (v2.28.2 / v2.28.3)

> Testlauf: **v2.28.3 — 2026-04-07**, Dev-Container `mediaassistant-dev`, 26/26 Tests bestanden
> Testdatei: `backend/test_duplicate_fix.py` (kombiniert die alten Duplikat-Tests mit den neuen Race-Tests)
> Ausführung: `docker exec mediaassistant-dev python3 /app/test_duplicate_fix.py`

### Hintergrund

In `v2.28.0`/`v2.28.1` traten ~30 Jobs/Tag mit doppelten Pipeline-Logs auf
(verschiedene Tag-Counts für dieselbe `debug_key`), 120 Jobs mit
inkonsistenten `error_message`-Feldern, sowie die wiederkehrenden Fehler
`ExifTool Sidecar already exists`, `File disappeared before upload` und
`ExifTool File not found`. Die Live-DB-Analyse bewies, dass `run_pipeline`
von **5 verschiedenen Stellen** aufgerufen wird:

| # | Aufrufer | Datei:Zeile |
|---|---|---|
| 1 | `_pipeline_worker` | `filewatcher.py:264` |
| 2 | `_poll_immich` | `filewatcher.py:379` |
| 3 | Startup-Resume | `filewatcher.py:503` |
| 4 | `retry_job` | `pipeline/__init__.py:304` |
| 5 | Duplikate-Router (Keep / Not-a-duplicate) | `routers/duplicates.py:815, 882` |

Ohne Schutz konnten zwei Aufrufer denselben Job parallel verarbeiten und
schrieben gleichzeitig in dieselben Dateien (`.xmp`, Quelldatei, Immich).

### Fix-Architektur

**v2.28.2 — `run_pipeline` atomarer Claim:**
```python
claim = await session.execute(
    update(Job)
    .where(Job.id == job_id, Job.status == "queued")
    .values(status="processing", started_at=datetime.now())
)
await session.commit()
if claim.rowcount == 0:
    return  # someone else already claimed
```

**v2.28.3 — `retry_job` atomarer Claim mit transientem Lock:**
```python
claim = await session.execute(
    update(Job)
    .where(Job.id == job_id, Job.status == "error")
    .values(status="processing")  # transient lock during cleanup
)
# ... cleanup file move + step_result reset ...
job.status = "queued"  # only now is the job claimable by run_pipeline
await session.commit()
```

### Tests

#### Test 1: `_handle_duplicate` Cleanup-Fehler abgefangen (Fix #38)
| Assertion | Resultat |
|---|---|
| `_handle_duplicate` wirft keine Exception bei Cleanup-Fehler | ✅ |
| `job.status == "duplicate"` (auch nach Cleanup-Fehler) | ✅ |
| `job.target_path` korrekt gesetzt | ✅ |
| Original-Datei in `error/duplicates/` verschoben | ✅ |

#### Test 2: Pipeline-Fallback erkennt `job.status == "duplicate"` (Fix #38)
| Assertion | Resultat |
|---|---|
| `job.status == "duplicate"` | ✅ |
| `job.status != "error"` | ✅ |
| IA-02 result enthält `note: detected but cleanup failed` | ✅ |
| IA-08 wurde NICHT ausgeführt (Pipeline brach korrekt nach IA-02 ab) | ✅ |

#### Test 3: Normaler Duplikat-Flow ohne Fehler
| Assertion | Resultat |
|---|---|
| `job.status == "duplicate"` | ✅ |
| `IA-02.match_type == "exact"` | ✅ |
| IA-08 wurde NICHT ausgeführt | ✅ |
| Datei aus Original-Ort verschoben | ✅ |

#### Test 4: Nicht-Duplikat läuft normal weiter bis IA-08
| Assertion | Resultat |
|---|---|
| `job.status != "duplicate"` | ✅ |
| `IA-02.status != "duplicate"` | ✅ |
| Pipeline lief über IA-02 hinaus weiter (IA-03+) | ✅ |

#### Test 5 (NEU v2.28.2): Atomic claim blockiert 10 parallele `run_pipeline`-Aufrufe
**Setup:** Job in `queued` Status, dann `asyncio.gather(*[run_pipeline(jid) for _ in range(10)])`.

| Assertion | Erwartet | Resultat |
|---|---|---|
| 9/10 Aufrufer blockiert mit `already claimed`-Log | 9 | ✅ 9 |
| `step_result` enthält IA-01 (genau eine Ausführung) | True | ✅ True |
| `system_logs`-Einträge `Error at IA-01` für diesen Job | 1 | ✅ 1 |

**Beweis ohne den Fix:** vor dem Fix wären die step_results von 10 parallelen Runs überlagert worden, mehrere Tag-Counts in `system_logs` aufgetreten, und das `error_message`-Feld hätte einen Traceback aus einem RUN, während `step_result` Daten aus einem anderen RUN enthielt.

#### Test 6 (NEU v2.28.2): `run_pipeline` auf nicht-queued Job ist No-op
**Setup:** Job in Status `done`, dann `await run_pipeline(jid)`.

| Assertion | Erwartet | Resultat |
|---|---|---|
| Status unverändert (`done`) | done | ✅ done |
| Kein step_result hinzugefügt | leer | ✅ leer |

**Bedeutung:** Jobs, die bereits abgeschlossen sind, werden niemals versehentlich neu verarbeitet — auch nicht durch falsche API-Calls oder Race-bedingte Doppel-Aufrufe.

#### Test 7 (NEU v2.28.3): `retry_job` parallel zu 5× `run_pipeline`
**Setup:** Job in `error`, dann `asyncio.gather(retry_job(jid), run_pipeline(jid)*5)`.

| Assertion | Erwartet | Resultat |
|---|---|---|
| `retry_job` returned `True` | 1× True | ✅ |
| 5× `run_pipeline` returned `None` (alle blockiert) | 5× None | ✅ |
| IA-01 wurde frisch ausgeführt (kein stale `reason: stale`) | reason startswith "ExifTool" | ✅ |
| `system_logs`-Einträge ≤ 2 (kein Doppel-Processing) | ≤ 2 | ✅ 2 |

**Beweis für den Fix-Wert:** Vor dem v2.28.3-Fix konnte `retry_job` zwischen seinen zwei Commits einen Worker reinrutschen lassen, der mit dem alten `step_result` (`{IA-01: {status: error, reason: stale}}`) gestartet ist und IA-01 übersprungen hat.

#### Test 8 (NEU v2.28.3): 5 parallele `retry_job`-Aufrufe (Doppelklick-Schutz)
**Setup:** Job in `error`, dann `asyncio.gather(*[retry_job(jid) for _ in range(5)])`.

| Assertion | Erwartet | Resultat |
|---|---|---|
| `retry_job` returned True genau 1× | 1 | ✅ 1 |
| `retry_job` returned False 4× | 4 | ✅ 4 |

**Bedeutung:** Schutz gegen Doppelklick im UI, mehrere Browser-Tabs, oder API-Spam. Nur der erste Aufrufer flippt den Status atomar von `error` zu `processing`.

### Live-Datenbank-Analyse vor dem Fix (07.04.2026)

**Smoking guns aus der Produktions-DB:**

```
MA-2026-19689 (drei Pipeline-Ausführungen für denselben job_id):
  14:03:17.411 INFO  unknown, 5 Tags, ... -> immich:62f46271-...   ← Run A erfolgreich
  14:03:29.596 ERROR Error at IA-07                                  ← Run B parallel zu A
  14:03:29.664 ERROR Error at IA-08                                  ← Run B
  14:03:32.915 INFO  Persönliches Foto, ...                          ← Run B notify
  14:03:33.194 INFO  Persönliches Foto, 11 Tags, ...                 ← Run B final
  15:51:31.241 INFO  Persönliches Foto, 11 Tags, ... -> immich:...   ← Run C (~2h später, Retry)

MA-2026-23090 (zwei Pipeline-Ausführungen mit unterschiedlichen Tag-Counts):
  00:04:47.641 INFO  Persönliches Foto, 11 Tags, ...                 ← Run A
  00:04:55.271 ERROR Error at IA-08                                  ← Run B
  00:04:59.233 INFO  Persönliches Foto, 10 Tags                      ← Run B (anderes Tag-Count!)
```

**Aggregierte Schäden in 2 Tagen:**
- 30+ Jobs mit doppelten `INFO pipeline` Log-Einträgen pro `debug_key`
- 120 Jobs mit `error_message LIKE '%already exists%'` aber Status `done`
- 71 Jobs mit `File disappeared` Fehler

### Reproduktionsschritte (für künftige Regression-Tests)

```bash
# Im Dev-Container ausführen:
docker exec mediaassistant-dev python3 /app/test_duplicate_fix.py

# Erwartete Ausgabe:
#   Ergebnis: 26/26 Tests bestanden
#   🎉 Alle Tests bestanden!
```

Bei Code-Änderungen an:
- `backend/pipeline/__init__.py` (`run_pipeline` oder `retry_job`)
- `backend/filewatcher.py` (`_pipeline_worker` oder Startup-Resume)
- `backend/routers/duplicates.py` (Keep/Not-a-duplicate Endpoints)

→ **Pflicht-Durchlauf** der vollen Suite vor dem Commit, inkl. der Race-Tests 5–8.

### Symptom-Pflaster, die mit v2.28.2 entfernt wurden

Diese beiden Workarounds aus älteren Releases waren nach dem echten Fix
nicht mehr nötig und wurden bewusst entfernt — sie hätten neue Race-Bugs
maskiert:

| Pflaster | Ursprung | Status |
|---|---|---|
| `step_ia07_exif_write.py`: `os.path.exists(sidecar_path) → os.remove()` vor ExifTool | `00b1d5b` | ❌ entfernt |
| `step_ia08_sort.py`: `os.path.exists(job.original_path)` vor Upload UND vor Move | `4a149f4` | ❌ entfernt |

Falls echte Filesystem-Probleme auftreten (User löscht Datei manuell, NFS-Glitch), werden die Fehler aus `upload_asset()` oder `safe_move()` direkt durchgereicht — mit präziseren Meldungen als die irreführende `file may still be copying or was moved by another process`.

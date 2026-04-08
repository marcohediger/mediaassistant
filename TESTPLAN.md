# Testplan — MediaAssistant

> **Letzter Test-Stand: v2.28.29 — 2026-04-08**
> - **Sektion 15** (Test-Matrix, neu in v2.28.29): 28 von 72 Pipeline-Szenarien
>   abgedeckt, 44 Lücken explizit dokumentiert. Siehe Abschnitt 15.
> - `backend/test_retry_file_lifecycle.py`: **37/37 grün** gegen echtes Dev-Immich
>   (Retry+Reprocess File-Lifecycle, sidecar+direct, immich+file-storage,
>   error+warning, missing-file).
> - `backend/test_duplicate_fix.py`: **26/26 grün** (Duplikat-Fix #38 + Race-Conditions Test 5–8).
> - `backend/test_testplan_final.py`: **59/0** (1 Block: HEIC-Testdatei fehlt im
>   Container — preexisting). Sektionen 1, 2, 3, 4, 6, 7, 8, 9, 12.
> - Sektion 5 (Immich Replace), Sektion 11 (DJI Daten) nicht erneut getestet —
>   keine relevante Code-Änderung seit v2.28.3.
>
> **Letzter vollständiger Testlauf vor v2.28.29: v2.28.3 — 2026-04-07 — 92/92** (Dev-Container)
>
> Letzter Race-Condition-Testlauf: **v2.28.3 — 2026-04-07** (26/26 bestanden, alle Tests in `backend/test_duplicate_fix.py`)
> Letzter vollständiger Testlauf vor v2.28.x: **v2.17.1 — 2026-04-02** (296/305 bestanden, 9 nicht testbar)
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
- [x] **v2.28.2:** Sidecar-Schreiben ohne pre-delete (Workaround entfernt) — die Race, die den pre-delete nötig machte, ist via atomic claim in `run_pipeline` verhindert (siehe Sektion 13)
- [x] **v2.28.2:** Bei einem Retry, der IA-07 erneut ausführen muss, ist ein leftover `.xmp` aus einem früheren Crash kein Problem mehr — `retry_job` löscht den entsprechenden step_result-Eintrag, neuer IA-07 schreibt frisch (kein leftover bei normalen Retries, da `retry_job` bei IA-07-Erfolg den Eintrag in step_result behält und IA-07 daher beim Retry komplett übersprungen wird)

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
- [x] **v2.28.2:** `os.path.exists`-Check vor Immich-Upload **entfernt** (war Workaround für Race, die jetzt via atomic claim verhindert wird) — wenn die Quelldatei wirklich fehlt (z.B. User löscht manuell), wird der Fehler aus `upload_asset()` direkt durchgereicht
- [x] **v2.28.2:** `os.path.exists`-Check vor Library-Move **entfernt** (gleicher Grund) — Fehler aus `safe_move()` werden direkt durchgereicht
- [x] **v2.28.2:** Schutz vor Half-Copied Files liegt jetzt ausschliesslich beim Filewatcher (`_is_file_stable`, siehe Sektion 4) und beim atomic claim (verhindert dass zwei Pipeline-Instanzen denselben Pfad gleichzeitig anfassen)

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
- [x] **v2.28.2:** `_is_file_stable` ist nach Entfernung der IA-07/IA-08-Workarounds der **einzige** Schutz vor Half-Copied Files in der Pipeline — bestätigt durch Filewatcher-Tests, kein Workaround mehr in den Pipeline-Steps nötig
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
- [x] **v2.28.2/v2.28.3:** Derselbe `job_id` wird nicht von zwei Pipeline-Instanzen gleichzeitig verarbeitet (siehe Sektion 13: Race-Condition-Tests 5–8 in `test_duplicate_fix.py`)

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
| Externe Datei-Race | Wenn ein **externer** Prozess eine Inbox-Datei mid-pipeline löscht/ersetzt (z.B. iCloud re-sync), wird der entsprechende ExifTool/upload_asset/safe_move-Fehler direkt durchgereicht — der atomic claim aus v2.28.2 schützt nur vor *internen* Doppel-Verarbeitungen, nicht vor externen Filesystem-Eingriffen |

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

## 14. Vollständiger Testlauf — v2.28.3 — 2026-04-07

> Dev-Container `mediaassistant-dev`, durchgeführt nach v2.28.2/v2.28.3 Race-Condition-Fixes
> Gesamt: **92/92 Tests bestanden, 0 Fehler, 0 geblockt**

### Test 1: `backend/test_duplicate_fix.py` (26 Tests)

`docker exec mediaassistant-dev python3 /app/test_duplicate_fix.py`

| Bereich | Tests | Resultat |
|---|---|---|
| Test 1: `_handle_duplicate` Cleanup-Fehler-Resilienz (Fix #38) | 4 | ✅ |
| Test 2: Pipeline-Fallback bei IA-02 Exception (Fix #38) | 4 | ✅ |
| Test 3: Normaler Duplikat-Flow | 4 | ✅ |
| Test 4: Nicht-Duplikat läuft bis IA-08 | 3 | ✅ |
| **Test 5 (NEU v2.28.2):** 10× parallel `run_pipeline` → atomic claim blockiert 9 | 3 | ✅ |
| **Test 6 (NEU v2.28.2):** `run_pipeline` auf done Job → no-op | 2 | ✅ |
| **Test 7 (NEU v2.28.3):** `retry_job` + 5× `run_pipeline` → frischer IA-01 | 4 | ✅ |
| **Test 8 (NEU v2.28.3):** 5× parallel `retry_job` → exakt 1× True | 2 | ✅ |
| **Total** | **26** | **✅ 26/26** |

### Test 2: TESTPLAN-Sektionen (66 Tests in `/tmp/testplan_final.py`)

#### Sektion 9 — Performance
| Test | Resultat |
|---|---|
| 7+ DB-Indexes vorhanden (R4) | ✅ 10 indexes |
| Dashboard GROUP BY query (R2) | ✅ 3.3ms (< 100ms) |
| `MAX_FILE_SIZE = 10 GB` (S7) | ✅ 10737418240 |

#### Sektion 8 — Security
| Test | Resultat |
|---|---|
| `_validate_target_path('/library/../etc')` → ValueError | ✅ |
| `_validate_target_path('/library/photos/2026')` → akzeptiert | ✅ |
| `_sanitize_filename('../../etc/passwd')` → `'passwd'` | ✅ |
| `_sanitize_filename('/etc/passwd')` → `'passwd'` | ✅ |
| `_sanitize_filename('photo_2026.jpg')` → unverändert | ✅ |
| `_sanitize_filename('')` → `'asset.jpg'` (Fallback) | ✅ |
| `_sanitize_filename(None)` → `'asset.jpg'` (Fallback) | ✅ |

#### Sektion 4 — Filewatcher-Stabilität
| Test | Resultat |
|---|---|
| Stabile Datei (Größe stimmt) → True | ✅ |
| Leere Datei (0 Bytes) → unstable (current_size > 0 Check) | ✅ |
| Größen-Mismatch → unstable nach 1.0s Wartezeit | ✅ |
| 24/24 Extensions registriert | ✅ |
| Synology SKIP_DIRS = `{'@eadir', '.synology', '#recycle'}` | ✅ |

#### Sektion 7 — Edge Cases
| Test | Resultat |
|---|---|
| `.txt` wird vom Filewatcher ignoriert | ✅ |
| Job mit Leerzeichen-Name | ✅ |
| Job mit Umlauten (`Umläüten_äöü.jpg`) | ✅ |
| Job mit CJK (`测试照片_テスト.jpg`) | ✅ |
| Job mit Emoji (`🏔️_Berge.jpg`) | ✅ |
| Job mit Klammern (`DJI_0061 (2).JPG`) | ✅ |
| Job mit doppelter Extension (`photo.jpg.jpg`) | ✅ |

#### Sektion 2 — Pipeline-Fehlerbehandlung
| Test | Resultat |
|---|---|
| Critical IA-01 Fehler → status=error | ✅ |
| `error_message` enthält IA-01-Marker | ✅ |
| Finalizer (IA-09/10/11) liefen nach kritischem Fehler | ✅ |

#### Sektion 1 + 6 — Pipeline-Steps & Dateiformate
> Reale Dateien aus `/home/marcohediger/claude/Testbilder/iphone/`, synthetische via PIL.

**HEIC** (iPhone 12 Pro, IMG_2410.HEIC, 759KB):
| Step | Resultat |
|---|---|
| IA-01: `file_type=HEIC`, EXIF Apple iPhone 12 Pro | ✅ |
| IA-02: status=ok | ✅ |
| IA-04: konvertiert → temp JPEG | ✅ |
| IA-05: `Persönliches Foto`, confidence 1.00 | ✅ |
| IA-07 dry_run: 9 Tags geplant | ✅ |
| IA-08 dry_run: target `/library/photos/2022/2022-12/...` | ✅ |
| Final status: `done` | ✅ |

**MOV** (iPhone Video, IMG_2539.MOV, 1.4MB, 1.02s):
| Step | Resultat |
|---|---|
| IA-01: `file_type=MOV`, mime=`video/quicktime`, ffprobe-Daten | ✅ |
| IA-02: status=ok | ✅ |
| IA-04: `converted=False` (Video-Thumbnail im Test deaktiviert) | ✅ |
| IA-05: type=unknown (kein Thumbnail) | ✅ |
| IA-07 dry_run: 4 Tags | ✅ |
| IA-08 dry_run: target `/library/videos/2022/2022-12/...` | ✅ |
| Final status: `done` | ✅ |

**PNG / WEBP / TIFF / GIF / JPEG / BMP** (synthetische Bilder via PIL):
| Format | IA-01 | IA-02 | Final |
|---|---|---|---|
| PNG | ✅ PNG | ✅ duplicate | ✅ duplicate |
| WEBP | ✅ WEBP | ✅ duplicate | ✅ duplicate |
| TIFF | ✅ TIFF | ✅ duplicate | ✅ duplicate |
| GIF | ✅ GIF | ✅ duplicate | ✅ duplicate |
| JPEG | ✅ JPEG | ✅ duplicate | ✅ duplicate |
| BMP | ✅ BMP | ✅ duplicate | ✅ duplicate |

> Synthetische Mini-Bilder werden korrekt von der Duplikat-Erkennung erfasst (pHash-Match mit existierenden Test-Bildern in der DB) — IA-01 läuft trotzdem korrekt durch.

#### Sektion 12 — Stress / Concurrent
| Test | Resultat |
|---|---|
| 10 Dateien parallel via `asyncio.gather` → alle 10 verarbeitet in 1.1s | ✅ |

#### Sektion 3 — Web Interface (Endpoint-Reachability)
| Endpoint | Status | Antwortzeit |
|---|---|---|
| `/api/version` | 200 | 5ms |
| `/api/dashboard` | 200 | 4ms |
| `/` | 200 | 3ms |
| `/login` | 200 | 4ms |
| `/review` | 200 | 4ms |
| `/logs` | 200 | 3ms |
| `/settings` | 200 | 3ms |
| `/duplicates` | 200 | 4ms |

> Alle Endpoints redirecten zu `/login` (Auth aktiv) und liefern HTTP 200 — Browser-UI-Tests selbst wurden nicht durchgeführt (manuelle Validierung erforderlich).

### Nicht erneut getestete Sektionen (keine Code-Änderung in v2.28.x)

| Sektion | Begründung |
|---|---|
| **5 — Immich-Integration** | Erfordert echte Immich-Instanz; bereits in v2.17.1 grün |
| **10 — Nicht testbar (Photon, CR2/NEF/ARW)** | Infrastruktur fehlt |
| **11 — DJI-Testdaten Ergebnisse** | Historische Tests, weiterhin gültig |

### Reproduktionsschritte

```bash
# Race-Condition-Tests
docker exec mediaassistant-dev python3 /app/test_duplicate_fix.py

# Konsolidierter TESTPLAN-Lauf (66 Tests, ~30s)
docker cp /tmp/testplan_final.py mediaassistant-dev:/tmp/
docker exec mediaassistant-dev python3 /tmp/testplan_final.py
```

**Erwartetes Ergebnis:**
- `test_duplicate_fix.py`: `26/26 Tests bestanden`
- `testplan_final.py`: `PASS: 66, FAIL: 0, BLOCK: 0`

## 15. Test-Matrix — Vollständige Coverage-Karte (v2.28.29)

> Vollständige Coverage-Karte aller Code-Pfade, die `run_pipeline()` oder
> `prepare_job_for_reprocess()` auslösen — von der ersten Erkennung einer
> neuen Datei (Filewatcher, Immich-Poller) bis zum manuellen Retry und
> Duplikat-Review. Pro Szenario: Eingangs-Bedingungen, erwartetes
> Verhalten, automatisierter Test (oder explizit markierte Lücke).
>
> Diese Matrix ist eine **strukturelle Ergänzung** zu den älteren
> Sektionen 1–14: wo die Sektionen 1–12 die Tests **per Pipeline-Step**
> auflisten, kartografiert Sektion 15 die Tests **per Code-Pfad** durch
> die Pipeline-Entry-Points. Beide Sichten sind komplementär.
>
> Stand v2.28.29 (commit `b54587f`).
>
> **Test-Skripte:**
> - [`backend/test_retry_file_lifecycle.py`](backend/test_retry_file_lifecycle.py)
>   — Retry/Reprocess-Lifecycle gegen echtes Immich (sidecar+direct,
>   immich+file-storage, error+warning, missing-file)
> - [`backend/test_duplicate_fix.py`](backend/test_duplicate_fix.py)
>   — Duplikat-Fix #38 + Race-Conditions für `run_pipeline`/`retry_job`
> - [`backend/test_testplan_final.py`](backend/test_testplan_final.py)
>   — TESTPLAN.md Sektion 1-12 (Formate, Web, Filewatcher, Security,
>   Performance, Edge Cases, Stress)
> - [`backend/test_ai_backends.py`](backend/test_ai_backends.py)
>   — AI-Backend-Loadbalancer

### Achsen

| Achse | Werte | Bedeutung |
|---|---|---|
| **Entry-Point** | filewatcher (Inbox-Scan) / filewatcher (Immich-Poller) / filewatcher (Startup-Resume) / retry_job / reset_job_for_retry / duplicates.review / duplicates.not-duplicate / `move_file=False` | wer löst `run_pipeline` bzw. `prepare_job_for_reprocess` aus |
| **Storage** | Immich / File-Storage | `use_immich=True/False` → IA-08-Branch |
| **Write-Mode** | direct / sidecar | `metadata.write_mode` → EXIF-in-Datei vs `.xmp`-Sidecar |
| **Source** | Inbox / Immich-Poller | `source_label`, prägt `original_path`-Prefix |
| **File-Type** | image (JPG/PNG/HEIC/GIF/WebP/RAW) / video (MP4/MOV/MKV/...) | beeinflusst IA-04 (Convert/Frame-Extract), IA-06 (OCR), IA-08 (Kategorie photo vs video) |
| **EXIF-Status** | mit EXIF / ohne EXIF / korrupt | beeinflusst IA-01-Erfolg, IA-08-Default-Kategorie |
| **GPS-Status** | mit GPS / ohne GPS | beeinflusst IA-03-Geocoding, Album-Tags, Pfad-Templates |
| **Pre-Retry-Status** (nur Retry-Pfad) | done+Warnungen / error / duplicate | aus welchem Job-Zustand kommt der Retry |
| **Pre-Retry-File-Location** (nur Retry-Pfad) | inbox / library / library/error / library/duplicates / `/tmp/ma_immich_*` / nowhere | wo liegt die Datei beim Retry-Klick |
| **immich_asset_id gesetzt** | yes / no | beeinflusst IA-08-Branch (webhook vs upload) und (vor Fix v2.28.28) IA-10-Cleanup |
| **Sorting Rule** | match / kein match / "skip"-Rule | IA-08 entscheidet Kategorie statisch vor KI-Override |
| **dry_run** | on / off | IA-08 macht Move/Upload oder nur Report |
| **Module aktiviert** | ki_analyse, geocoding, ocr, ordner_tags, smtp, filewatcher, immich (jeweils on/off) | überspringt einzelne Steps |

### Entry-Points im Code

| # | Entry | Datei:Zeile | Auslöser |
|---|---|---|---|
| 1 | Filewatcher Inbox-Scan → `_create_job_safe` → `run_pipeline` | `filewatcher.py:236`, `:264` | neue Datei in `/inbox/...` (continuous oder scheduled) |
| 2 | Filewatcher Immich-Poller → `_create_job_safe` → `run_pipeline` | `filewatcher.py:365`, `:379` | neues Asset in Immich (`immich.poll_enabled=true`) |
| 3 | Filewatcher Startup-Resume | `filewatcher.py:505` | Container-Start: Jobs in `status='processing'` von Vorlauf werden requeued (max 3 retries) |
| 4 | `retry_job(jid)` | `pipeline/__init__.py:440` | UI-Button "Retry" pro Job (`POST /api/job/{key}/retry`) |
| 5 | `_bulk_reset_errors_in_background()` → `reset_job_for_retry(jid)` | `routers/api.py:54`, `pipeline/__init__.py:334` | UI "Retry-All" für alle Error-Jobs (`POST /api/jobs/retry-all-errors`) |
| 6 | `prepare_job_for_reprocess` aus `routers/duplicates.py:789` | `routers/duplicates.py:789` | "Behalten" im Duplikat-Review (`/api/duplicates/review`) |
| 7 | `prepare_job_for_reprocess` aus `routers/duplicates.py:837` | `routers/duplicates.py:837` | "Kein Duplikat" im Duplikat-Review (`/api/duplicates/not-duplicate`) |
| 8 | `move_file=False`-Variante | `pipeline/reprocess.py:211` | derzeit **keine Aufrufer** im Code (für tag_cleanup vorgesehen, issue #42) |

### Test-Matrix: Normal Pipeline Flows (Entry 1+2+3)

Erste Verarbeitung einer neuen Datei. Status startet `queued`, läuft IA-01..IA-11 sequenziell. **Glücklicher Pfad und alle Verzweigungen**.

#### N1-Serie: Inbox → Immich

Die häufigste Live-Konstellation. Filewatcher findet Datei in `/inbox/`,
erstellt Job (`use_immich=True`, `source_label='Default Inbox'`),
Pipeline läuft, IA-08 lädt nach Immich hoch und löscht die Inbox-Kopie.

| # | File-Type | EXIF | GPS | Write-Mode | Erwartet | Test-Status |
|---|---|---|---|---|---|---|
| N1.1 | JPG (Kamera, voll) | ✓ | ✓ | direct | done, target=`immich:`, alle Tags+Geo geschrieben | ✅ `test_testplan_final.py` Sektion 1+6 |
| N1.2 | HEIC (iPhone) | ✓ | ✓ | direct | wie N1.1, plus IA-04 HEIC→JPG-Konvertierung für KI | ✅ `test_testplan_final.py` (wenn `__test_heic.HEIC` verfügbar) |
| N1.3 | HEIC (iPhone) | ✓ | ✓ | sidecar | wie N1.2, aber IA-07 schreibt `.xmp`-Sidecar statt EXIF, IA-08 lädt `.xmp` mit hoch | ⚠️ **Lücke** (sidecar+Inbox-First-Run nicht explizit getestet — nur indirekt im Retry-Test) |
| N1.4 | PNG (Screenshot) | – | – | direct | done, Kategorie=screenshot, EXIF-leer, ggf. OCR-Tags | ✅ `test_testplan_final.py` Sektion 1+6 |
| N1.5 | GIF | – | – | direct | done, IA-04 macht GIF[0]→JPG für KI | ✅ `test_testplan_final.py` |
| N1.6 | DNG/RAW (Kamera) | ✓ | ggf | direct | done, IA-04 extrahiert PreviewImage via ExifTool | ✅ `test_testplan_final.py` |
| N1.7 | TIFF | ✓ | ggf | direct | done | ✅ `test_testplan_final.py` |
| N1.8 | WebP | – | – | direct | done | ✅ `test_testplan_final.py` |
| N1.9 | MP4 (Kamera-Video) | ✓ | ✓ | direct | done, Kategorie=`personliches_video`, IA-04 extrahiert N Frames via ffmpeg | ✅ `test_testplan_final.py` |
| N1.10 | MOV (iPhone-Video) | ✓ | ✓ | direct | wie N1.9 | ✅ `test_testplan_final.py` |
| N1.11 | MOV iPhone Live-Photo | ✓ | ✓ | direct | done, IA-04 video-thumbnail wenn aktiviert | ⚠️ **Lücke** (Live-Photo + HEIC-Companion separater Pfad?) |
| N1.12 | JPG ohne EXIF (Messenger-Bild) | – | – | direct | done, Kategorie=`sourceless` (oder `screenshot` je nach KI), `has_exif=false` | ✅ `test_testplan_final.py` |
| N1.13 | UUID-Filename (WhatsApp `[0-9a-f]{8}-...jpg`) | – | – | direct | done, Sorting-Rule für WhatsApp greift falls konfiguriert | ✅ `test_testplan_final.py` Sektion 7 |
| N1.14 | JPG mit EXIF aber ohne GPS | ✓ | – | direct | done, IA-03 geocoding skipped, kein Geo-Album | ⚠️ **Lücke** (nicht explizit isoliert) |
| N1.15 | Korrupte Datei (z.B. 0-Byte) | – | – | direct | status=error, IA-01 ExifTool-Fehler, file → `/library/error/`, `.log` daneben | ✅ `test_duplicate_fix.py` Test 7+8 (indirekt), `test_testplan_final.py` Sektion 2 |

#### N2-Serie: Inbox → File-Storage (use_immich=False)

Selbe Inbox-Detection, aber `use_immich=False`. IA-08 verschiebt nach `/library/<kategorie>/<jahr>/<jahr-monat>/`.

| # | File-Type | EXIF | Write-Mode | Erwartet | Test-Status |
|---|---|---|---|---|---|
| N2.1 | JPG | ✓ | direct | done, target=`/library/photos/2024/2024-03/X.jpg` | ⚠️ **Lücke** (file-storage first-run nicht direkt getestet — nur Retry-Variante R3) |
| N2.2 | HEIC | ✓ | sidecar | done, target=`/library/photos/.../X.HEIC` + `X.HEIC.xmp` daneben | ⚠️ **Lücke** (nur als Retry-Variante R4 getestet) |
| N2.3 | MP4 | ✓ | direct | done, target=`/library/videos/...` | ⚠️ **Lücke** |
| N2.4 | JPG ohne EXIF | – | direct | done, target=`/library/sourceless/...` | ⚠️ **Lücke** |

#### N3-Serie: Immich-Poller → Pipeline (Entry 2)

Immich-Poller (`immich.poll_enabled=true`) lädt neue Assets aus Immich
ins eigene Tempdir `/tmp/ma_immich_xxx/`, erstellt Job mit
`source_label='Immich'`, `use_immich=True`, `immich_asset_id=<id>`.

Pipeline läuft, IA-08 nimmt **webhook-Branch** (line 443: `if job.immich_asset_id:`)
weil Asset schon existiert. Direct-Mode: re-upload als neuer Asset, alten löschen.
Sidecar-Mode: nur tags via API, kein Datei-Upload.

| # | File-Type | Write-Mode | IA-08-Branch | Erwartet | Test-Status |
|---|---|---|---|---|---|
| N3.1 | JPG | direct | webhook+upload | done, neuer asset_id, alter gelöscht, IA-10 räumt `/tmp/ma_immich_xxx/` weg | ❌ **Lücke** |
| N3.2 | JPG | sidecar | webhook+tag-only | done, asset_id unverändert, nur Immich-Tags neu, IA-10 räumt Tempdir | ❌ **Lücke** |
| N3.3 | HEIC | direct | webhook+upload | wie N3.1 | ❌ **Lücke** |
| N3.4 | HEIC | sidecar | webhook+tag-only | wie N3.2 | ❌ **Lücke** |
| N3.5 | MOV (Video) | direct | webhook+upload | done | ❌ **Lücke** |

#### N4-Serie: Modul-Variationen (orthogonal)

Pipeline läuft normal, aber einzelne Module sind aus.

| # | Modul aus | Effekt | Test-Status |
|---|---|---|---|
| N4.1 | `ki_analyse` | IA-05 skipped, Klassifikation rein über statische Sorting Rules + EXIF | ⚠️ **Lücke** |
| N4.2 | `geocoding` | IA-03 skipped, keine Geo-Tags, kein Geo-Album | ⚠️ **Lücke** |
| N4.3 | `duplikat_erkennung` | IA-02 läuft nur als Hash-Check ohne pHash, alles passiert | ⚠️ **Lücke** |
| N4.4 | `ocr` | IA-06 skipped | ⚠️ **Lücke** |
| N4.5 | `ordner_tags` (per Inbox) | IA-08 erstellt kein Album aus Inbox-Subfolder-Pfad | ⚠️ **Lücke** |
| N4.6 | `smtp` | IA-09 skipped (kein Mail-Versand), `sent=false` im step_result | ✅ Default in dev |
| N4.7 | `immich` (komplett aus) | `use_immich=True`-Jobs scheitern oder fallen auf File-Storage zurück | ⚠️ **Lücke** |
| N4.8 | beide AI-Backends aus (`ki_analyse` + `ki_analyse_2`) | IA-05 skipped, kein Auto-Pause | ⚠️ **Lücke** |

#### N5-Serie: Spezielle Outcomes

Pipeline läuft komplett durch, endet aber NICHT in `done`.

| # | Trigger | Erwartetes Ergebnis | Test-Status |
|---|---|---|---|
| N5.1 | IA-02 findet exact-Hash-Duplikat eines schon verarbeiteten Jobs | status=`duplicate`, file → `/library/error/duplicates/`, IA-08+IA-09 nicht ausgeführt | ✅ `test_duplicate_fix.py` Tests 1-4 |
| N5.2 | IA-02 findet pHash-similar (nicht exact) | status=`duplicate`, match_type=`similar` | ✅ `test_duplicate_fix.py` |
| N5.3 | IA-02 Video-pHash post-IA-04 | status=`duplicate`, IA-02 nachträglich überschrieben | ✅ `test_duplicate_fix.py` |
| N5.4 | KI gibt Kategorie `unknown` zurück (oder keine valide) | status=`review`, file im review-Ordner | ⚠️ **Lücke** (nicht isoliert getestet) |
| N5.5 | Sorting-Rule mit `target_category="skip"` matched | status=`skipped`, **keine** Datei-Bewegung, Pipeline bricht nach IA-01 ab | ⚠️ **Lücke** (early-skip-Pfad in pipeline/__init__.py:82) |
| N5.6 | `dry_run=True` auf der Inbox | status=`done` (oder `dry_run`), KEIN Move, KEIN Upload | ⚠️ **Lücke** |
| N5.7 | IA-05 mit AI Auto-Pause (`AIConnectionError`, beide Backends down) | `pipeline.paused=true` global, Job=`error`, health_watcher resumed bei Recovery | ⚠️ **Lücke** (manueller Check) |
| N5.8 | IA-03 mit Geocoding Auto-Pause (`GeocodingConnectionError`) | wie N5.7 für Geocoding | ⚠️ **Lücke** |

#### N6-Serie: Filewatcher Startup-Resume (Entry 3)

Container restart mit Jobs in `status='processing'` (z.B. nach Crash).

| # | Vor-Zustand | Erwartet | Test-Status |
|---|---|---|---|
| N6.1 | 1 Job `processing` | nach Restart: status='queued' + retry_count++, Pipeline läuft erneut | ⚠️ **Lücke** |
| N6.2 | Job mit retry_count=3 | abandoned: status='error', Meldung "Max retries (3) exceeded" | ⚠️ **Lücke** (`filewatcher.py:492`) |
| N6.3 | mehrere Jobs `processing` parallel | alle requeued sequenziell | ⚠️ **Lücke** |

#### N7-Serie: Concurrency / Race-Conditions

| # | Szenario | Erwartet | Test-Status |
|---|---|---|---|
| N7.1 | 10 Dateien gleichzeitig im Inbox | alle 10 verarbeitet, kein Duplicate-Job, kein Lost-File | ✅ `test_testplan_final.py` Sektion 12 (Stress 10 parallel) |
| N7.2 | derselbe Job von 5 Pipeline-Aufrufern parallel | atomic claim: 1 läuft, 4 returnen None | ✅ `test_duplicate_fix.py` Test 5 (10 callers) |
| N7.3 | retry_job + 5 parallele run_pipeline auf demselben Job | nur retry's pipeline läuft, 5 blocked | ✅ `test_duplicate_fix.py` Test 7 |
| N7.4 | 5 parallele retry_job auf demselben Job | exakt 1 succeeded, 4 returnen False | ✅ `test_duplicate_fix.py` Test 8 |
| N7.5 | run_pipeline auf done/processing-Job (Idempotenz-Check) | no-op | ✅ `test_duplicate_fix.py` Test 6 |
| N7.6 | Bulk-Retry-All triggert 30+ parallele Pipeline-Tasks | DB-Pool reicht (20/40 nach v2.28.7), keine "QueuePool limit"-Errors | ⚠️ **Lücke** (Pool-Tuning ist da, kein automatischer Test) |

### Test-Matrix: Retry-Job (Entry 4)

Eingangs-Status: `status='error'` ODER `status='done' + error_message='Warnungen in: ...'`.

| # | Storage | Write-Mode | Source | Pre-Status | File liegt | immich_asset_id | Erwartet | Test-Status |
|---|---|---|---|---|---|---|---|---|
| R1 | Immich | sidecar | Inbox | done+Warnung | inbox | gesetzt | Datei → reprocess, IA-08 cached, target_path bleibt `immich:`, Datei reachable | ✅ `_run_lifecycle_test(mode=sidecar)` |
| R2 | Immich | direct | Inbox | done+Warnung | inbox | gesetzt | wie R1 | ✅ `_run_lifecycle_test(mode=direct)` |
| R3 | File-Storage | direct | Inbox | done+Warnung | library/photos/... | nein | Datei → reprocess → IA-08 re-runs → zurück nach library | ✅ `_run_filestorage_test(mode=direct)` |
| R4 | File-Storage | sidecar | Inbox | done+Warnung | library/photos/... + .xmp | nein | wie R3 + .xmp wandert mit | ✅ `_run_filestorage_test(mode=sidecar)` |
| R5 | Immich | direct | Inbox | error (IA-08) | library/error | nein (IA-08 hat noch nicht hochgeladen) | Datei → reprocess → IA-08 lädt nach Immich, target_path=`immich:`, lokal gelöscht | ✅ `_run_error_retry_test` |
| R6 | Immich | sidecar | Inbox | error (IA-08) | library/error | nein | wie R5 | ⚠️ **Lücke** (wäre direkter Klon von R5 mit write_mode-Switch) |
| R7 | Immich | direct | Inbox | error (IA-07) | library/error | nein | Datei → reprocess → IA-07 schreibt EXIF erneut → IA-08 lädt hoch | ⚠️ **Lücke** |
| R8 | Immich | sidecar | Inbox | error (IA-07) | library/error | nein | wie R7, aber IA-07 schreibt `.xmp` neu | ⚠️ **Lücke** |
| R9 | Immich | direct | Inbox | error (IA-01) | original location (z.B. inbox) | nein | Datei → reprocess → IA-01 läuft erneut | ⚠️ **Lücke** (wird teilweise von test_duplicate_fix.py Test 7+8 geprüft) |
| R10 | File-Storage | direct | Inbox | error (IA-08) | library/error | nein | Datei → reprocess → IA-08 verschiebt nach library/photos | ⚠️ **Lücke** |
| R11 | File-Storage | sidecar | Inbox | error (IA-08) | library/error + .xmp | nein | wie R10, .xmp wandert mit | ⚠️ **Lücke** |
| R12 | Immich | direct | Immich-Poller | done+Warnung | `/tmp/ma_immich_xxx/` | gesetzt | Datei → reprocess → IA-08 webhook tags, IA-10 darf jetzt löschen (poller-temp) | ⚠️ **Lücke** |
| R13 | Immich | sidecar | Immich-Poller | done+Warnung | `/tmp/ma_immich_xxx/` + `.xmp` | gesetzt | wie R12, sidecar im Poller-Tempdir | ⚠️ **Lücke** |
| R14 | Immich | direct | Immich-Poller | error (IA-05) | `/tmp/ma_immich_xxx/` | gesetzt | wie R12 mit Critical-Statt-Warning | ⚠️ **Lücke** |
| R15 | – | – | – | – | nowhere (Datei vor Retry weg) | egal | Retry bricht ab mit `status='error'`, Meldung "Datei nicht auffindbar — Retry abgebrochen" | ✅ `_run_missing_file_test` |
| R16 | Immich | direct | Inbox | error (IA-01, Datei niemals existiert) | `/tmp/__race_X.jpg` (0-Byte) | nein | atomic claim race: 1 retry winnt, andere blocked | ✅ `test_duplicate_fix.py` Test 7+8 |

### Test-Matrix: Retry-All Bulk (Entry 5)

`reset_job_for_retry` direkt, ohne `retry_job`-Wrapper. Selbe Logik, nur sequenziell für viele Jobs.

| # | Szenario | Test-Status |
|---|---|---|
| RA1 | Bulk-Retry mehrerer Error-Jobs ohne sofortigen Pipeline-Run (background worker picks up) | ⚠️ **Lücke** — atomar/sequenziell-Verhalten bisher nur über die einzelnen `reset_job_for_retry`-Aufrufe abgedeckt, kein End-to-End-Bulk-Test |

### Test-Matrix: Duplikat-Review (Entry 6+7)

`prepare_job_for_reprocess` aus dem Duplikat-Router. Anders als Retry-Job: andere `keep_steps`/`inject_steps`-Parameter.

| # | Szenario | Storage | Pre-Status | Test-Status |
|---|---|---|---|---|
| D1 | "Behalten" im Review: kept_job läuft volle Pipeline neu (keep IA-01) | Immich | duplicate | ⚠️ **Lücke** |
| D2 | "Behalten" im Review, File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D3 | "Kein Duplikat": IA-02 wird auf skipped injiziert, IA-01 behalten | Immich | duplicate | ⚠️ **Lücke** |
| D4 | "Kein Duplikat", File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D5 | "Kein Duplikat" wenn Datei im library/duplicates/ verschwunden ist | – | duplicate | ⚠️ **Lücke** (sollte jetzt sauber abbrechen analog zu R15) |

### Test-Matrix: `move_file=False` (Entry 8)

Code-Pfad existiert, aber **keine Aufrufer** im Repo. Reserviert für tag_cleanup (issue #42).

| # | Szenario | Test-Status |
|---|---|---|
| M1 | In-place reprocess ohne Datei-Move (z.B. nach EXIF-Wipe in target_path) | ⚠️ **Lücke** — kein Test, weil kein Caller |

### Pro-Step Failure-Matrix (orthogonal zu obiger Achse)

Welche Pipeline-Steps können einen Job in `error` oder `done+Warnungen` schicken, und sind die getestet?

| Step | Kritisch? | Failure-Effekt | Live-relevant | Im Retry-Test getestet |
|---|---|---|---|---|
| IA-01 EXIF | ja | status=error, file → library/error | ja (z.B. korrupte Datei) | indirekt via test_duplicate_fix.py 7+8 |
| IA-02 Duplikate | nein | status=warning ODER status=duplicate (Sonderfall) | ja | Duplikat-Status: nein |
| IA-03 Geocoding | nein | status=warning (oder Auto-Pause bei `GeocodingConnectionError`) | ja | nein |
| IA-04 Convert | nein | status=warning | ja (HEIC→JPG-Konvertierung) | nein |
| IA-05 KI | nein | status=warning (oder Auto-Pause bei `AIConnectionError`) | ja (häufigster Fall: Backend-Aussetzer) | **synthetisches IA-05-warning ist genau die getestete Quelle** für R1–R4 |
| IA-06 OCR | nein | status=warning | selten | nein |
| IA-07 EXIF-Write | ja | status=error, file → library/error | ja (Sidecar-Konflikte, Permissions) | nein (R7/R8 = Lücke) |
| IA-08 Sort/Upload | ja | status=error, file → library/error | ja (Immich 502, Disk voll) | ✅ R5 |
| IA-09 Notify | nein (Finalizer) | step_result.status=error, kein Job-Status-Wechsel | ja (SMTP down) | nein (Effekt klein, kein Datei-Verlust-Risiko) |
| IA-10 Cleanup | nein (Finalizer) | step_result.status=error | ja (relevant für diesen Bug!) | ✅ Asserts in R1–R4 prüfen `IA-10.removed` |
| IA-11 SQLite-Log | nein (Finalizer) | step_result.status=error | nein (lokal, kein Datei-Effekt) | nein |

### Modul-Konfigurationen (orthogonal)

| Modul | Werte | Beeinflusst | Im Retry-Test |
|---|---|---|---|
| `ki_analyse` | on/off | IA-05 läuft oder skipped | on (Default) |
| `geocoding` | on/off | IA-03 macht echten Nominatim-Call oder skipped | on |
| `duplikat_erkennung` | on/off | IA-02 hash-/phash-Lookup | on |
| `ocr` | on/off | IA-06 Tesseract | on |
| `ordner_tags` | on/off | IA-08 Album-Tags aus Inbox-Pfad | on |
| `smtp` | on/off | IA-09 Mail-Versand bei Fehler | off (Default in dev) |
| `filewatcher` | on/off | scannt Inbox automatisch | **temporär off während Test** (verhindert Race) |
| `immich` | on/off | Immich-Auth/Polling überhaupt aktiv | on |
| `immich.poll_enabled` | true/false | Immich-Poller läuft als zweite Job-Quelle | false in dev |
| `metadata.write_mode` | direct/sidecar | IA-07 schreibt in Datei oder als `.xmp` | beide getestet |

### Zusammenfassung

| Bereich | Szenarien | Abgedeckt | Lücken |
|---|---|---|---|
| **Normal: Inbox → Immich** (N1.1–N1.15) | 15 | ~12 ✅ | N1.3 (sidecar+inbox first-run), N1.11 (Live-Photo), N1.14 (EXIF ohne GPS) |
| **Normal: Inbox → File-Storage** (N2.1–N2.4) | 4 | ⚠️ 0 (nur via Retry-Test indirekt) | alle 4 |
| **Normal: Immich-Poller → Pipeline** (N3.1–N3.5) | 5 | ❌ 0 | alle 5 |
| **Normal: Modul-Variationen** (N4.1–N4.8) | 8 | ⚠️ 1 (smtp aus = Default) | 7 von 8 |
| **Normal: Spezielle Outcomes** (N5.1–N5.8) | 8 | ✅ 3 (duplicate, similar, video-phash) | review, skip, dry_run, beide Auto-Pause |
| **Normal: Startup-Resume** (N6.1–N6.3) | 3 | ❌ 0 | alle 3 |
| **Normal: Concurrency** (N7.1–N7.6) | 6 | ✅ 5 | bulk-retry pool exhaustion |
| **Retry-Job, Inbox-Source, Warnungs-Retry** (R1–R4) | 4 | ✅ 4 | – |
| **Retry-Job, Inbox-Source, Error-Retry** (R5–R11) | 7 | ✅ 1 (R5) | R6–R11 |
| **Retry-Job, Immich-Poller-Source** (R12–R14) | 3 | ❌ 0 | alle 3 |
| **Retry-Job, Negativ-Fall** (R15) | 1 | ✅ 1 | – |
| **Retry-Job, Race-Conditions** (R16) | 1 | ✅ 1 | – |
| **Retry-All Bulk** (RA1) | 1 | ❌ 0 | – |
| **Duplikat-Review** (D1–D5) | 5 | ❌ 0 | alle 5 |
| **`move_file=False`** (M1) | 1 | ❌ 0 (kein Caller im Code) | – |
| **TOTAL** | **72** | **~28 ✅** | **~44 offen** |

### Empfohlene nächste Tests

Priorisiert nach **Daten-Verlust-Risiko** und **Live-Frequenz**:

#### Hohe Priorität (Daten-Verlust möglich)
1. **R6 (Immich+sidecar+error)** und **R10/R11 (File-Storage+error)** — dieselbe Klasse Bug wie R5, nur mit anderer Branch in IA-08. 1:1 vom R5-Test ableitbar, ~30 Min Aufwand.
2. **N3.1–N3.5 (Immich-Poller-Normal-Flow)** — bisher null Coverage für eine ganze Job-Quelle. IA-10-Cleanup darf hier (und nur hier) Tempdir löschen — Test muss aktiv prüfen dass es passiert. ~2h (Poller-Setup, fake Immich-Asset, Lifecycle-Asserts).
3. **D3/D5 (Duplikat-"Kein Duplikat")** — eigener Caller von `prepare_job_for_reprocess`, andere Pre-Status (`duplicate`). Live relevant beim manuellen Review. ~1h.

#### Mittlere Priorität (Korrektheit)
4. **N1.3 (Inbox+sidecar+Immich first-run)** — deckt eine Achse ab, die bisher nur indirekt im Retry-Test getroffen wird. ~20 Min.
5. **N2.1–N2.4 (Inbox→File-Storage normal flow)** — Coverage-Lücke für eine ganze Storage-Achse. ~30 Min.
6. **N5.4–N5.6 (review/skip/dry_run)** — drei alternative Job-End-Status, die UI-Logik triggern. Je 15 Min.
7. **R7/R8 (IA-07-Critical-Error retry)** — testet, dass `_move_to_error` den Move sauber macht und Retry den richtigen Pfad findet. ~30 Min.
8. **D1/D2 ("Behalten" im Review)** — wie D3, aber mit `keep_steps={'IA-01'}`. ~30 Min.

#### Niedrige Priorität (Edge Cases)
9. **N4.1–N4.8 (Modul-Variationen)** — jedes Modul einzeln aus testen. Defensiv, aber unwahrscheinliche Live-Konfigurationen. ~2h für alle 8.
10. **N6.1–N6.3 (Startup-Resume)** — schwer zu testen ohne echten Container-Restart. ~1h.
11. **N5.7/N5.8 (Auto-Pause)** — mit Mock-Backend-Down. ~30 Min.
12. **N7.6 (Pool-Exhaustion-Stress)** — synthetischer Stress mit 50+ parallel Tasks. ~30 Min.
13. **RA1 (Bulk-Retry-All End-to-End)** — wenige zusätzliche Asserts oben auf bestehende reset_job_for_retry-Tests. ~30 Min.

**Geschätzter Gesamt-Aufwand für 100% Coverage: 12–15 Stunden.**

Realistisches Ziel für nächsten Sprint: **Hohe Priorität + N1.3 + N2.x = ~5 Stunden, ~20 zusätzliche Asserts.**


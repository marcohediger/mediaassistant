# Testplan — MediaAssistant

> Dieses Dokument beschreibt **was** zu testen ist (Szenarien, erwartete
> Ergebnisse, Test-Skripte). Konkrete Test-**Resultate** eines bestimmten
> Releases gehören in den jeweiligen Commit / `CHANGELOG.md` — nicht hierhin,
> weil sie sonst beim nächsten Release sofort veraltet sind.
>
> **Wie ausführen:**
> ```bash
> docker exec mediaassistant-dev python /app/test_duplicate_fix.py
> docker exec mediaassistant-dev python /app/test_retry_file_lifecycle.py
> docker exec mediaassistant-dev python /app/test_testplan_final.py
> docker exec mediaassistant-dev python /app/test_ai_backends.py
> docker exec mediaassistant-dev python /app/test_keep_flow.py
> docker exec mediaassistant-dev python /app/test_ftag_immich.py
> docker exec mediaassistant-dev python /app/test_v29_stress.py
> docker exec mediaassistant-dev python /app/test_no_file_loss.py
> ```
>
> Test-Daten: Panasonic DMC-GF2 JPGs, DJI FC7203 JPGs, iPhone 12 Pro
> HEIC/MOV, Casio EX-S600 JPGs, generierte PNG/GIF/WebP/TIFF, UUID
> Messenger-Dateien.
>
> **Versions-Änderungen, die Tests betreffen**, gehören in `CHANGELOG.md` —
> nicht in dieses Dokument.

## 1. Pipeline-Steps

### IA-01: EXIF auslesen
- **IA01-01:** JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt extrahiert
- **IA01-02:** HEIC mit EXIF → korrekt gelesen
- **IA01-03:** Datei ohne EXIF (z.B. Messenger-Bild) → `has_exif: false`
- **IA01-04:** Video (MP4/MOV) → Mime-Type und Dateityp korrekt erkannt
- **IA01-05:** Beschädigte Datei → Fehler wird gefangen, Pipeline bricht nicht ab
- **IA01-06:** file_size wird korrekt gespeichert
- **IA01-07:** Datum-Fallback auf FileModifyDate wenn DateTimeOriginal fehlt
- **IA01-08:** Video: ffprobe extrahiert Datum (creation_time) korrekt
- **IA01-09:** Video: ffprobe extrahiert GPS-Koordinaten aus ISO 6709 String
- **IA01-10:** Video: ISO 6709 Parser verarbeitet verschiedene Formate korrekt (mit/ohne Höhe, mit/ohne Vorzeichen)
- **IA01-11:** Video: GPS aus ISO 6709 wird als lat/lon in Metadaten gespeichert
- **IA01-12:** Video: Dauer (duration) wird als Rohwert und formatiert gespeichert (z.B. `125.4` → `2m 05s`)
- **IA01-13:** Video: Auflösung (width x height) korrekt extrahiert
- **IA01-14:** Video: Megapixel aus Auflösung berechnet
- **IA01-15:** Video: Codec (z.B. h264, hevc) korrekt extrahiert
- **IA01-16:** Video: Framerate (z.B. 30, 60) korrekt extrahiert
- **IA01-17:** Video: Bitrate korrekt extrahiert
- **IA01-18:** Video: Rotation korrekt extrahiert (z.B. 0, 90, 180, 270)
- **IA01-19:** Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, fehlende ignoriert
- **IA01-20:** DNG (RAW): EXIF korrekt (Make, Model, Datum, GPS, Auflösung)
- **IA01-21:** DNG: Grosse Dateien (25MB–97MB) verarbeitet ohne Timeout
- **IA01-22:** PNG: file_type=PNG, mime=image/png korrekt
- **IA01-23:** WebP: file_type=WEBP, mime=image/webp korrekt
- **IA01-24:** GIF: file_type=GIF, mime=image/gif korrekt
- **IA01-25:** TIFF: file_type=TIFF, mime=image/tiff korrekt
- **IA01-26:** MOV: file_type=MOV, mime=video/quicktime, ffprobe-Metadaten korrekt

### IA-02: Duplikat-Erkennung
- **IA02-01:** Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate"
- **IA02-02:** Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert
- **IA02-03:** Unterschiedliches Bild → kein Match, `status: ok`
- **IA02-04:** RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet
- **IA02-05:** Modul `duplikat_erkennung` deaktiviert → IA-02 `status: skipped, reason: module disabled`
- **IA02-06:** Duplikat eines Immich-Assets → korrekt erkannt
- **IA02-07:** Orphaned Job (Original-Datei gelöscht) → Match wird übersprungen
- **IA02-08:** JPG+DNG Paar mit keep_both=true → beide unabhängig verarbeitet
- **IA02-09:** JPG+DNG Paar mit keep_both=false → zweite Datei als `raw_jpg_pair` Duplikat
- **IA02-10:** pHash-Threshold 3 → weniger False Positives als Threshold 5
- **IA02-11:** Video: pHash aus Durchschnitt der IA-04 Frames berechnet (post-IA-04 Check)
- **IA02-12:** Video: Re-encoded Video (anderer Codec/Bitrate) → pHash-Match, als "similar" Duplikat erkannt
- **IA02-13:** Video: Exakte Kopie eines Videos → SHA256-Match, als "exact" Duplikat erkannt

### IA-03: Geocoding
- **IA03-01:** Bild mit GPS-Koordinaten → Land, Stadt, Stadtteil aufgelöst
- **IA03-02:** Bild ohne GPS → `status: skipped`
- **IA03-03:** Nominatim-Provider → korrekte Ergebnisse
- **IA03-04:** Modul `geocoding` deaktiviert → IA-03 `status: skipped, reason: module disabled`
- **IA03-05:** Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen, Pipeline läuft weiter
- **IA03-06:** DJI-Drohne GPS → korrekt aufgelöst
- **IA03-07:** Video GPS (ffprobe ISO 6709) → korrekt geocodiert

### IA-04: Temp. Konvertierung für KI
- **IA04-01:** JPG/PNG/WebP → keine Konvertierung, `converted: false`
- **IA04-02:** HEIC → temp JPEG erstellt, KI-Analyse erfolgreich
- **IA04-03:** DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG
- **IA04-04:** GIF → Konvertierung versucht (convert nicht verfügbar), KI analysiert trotzdem direkt
- **IA04-05:** TIFF → keine Konvertierung nötig, direkt analysierbar
- **IA04-06:** Konvertierung fehlgeschlagen → Fehler gefangen (korruptes Video, fehlender convert)
- **IA04-07:** Video mit VIDEO_THUMBNAIL_ENABLED = True → mehrere Thumbnails extrahiert
- **IA04-08:** Video-Thumbnail: Dauer korrekt ermittelt, Frames gleichmässig verteilt
- **IA04-09:** Video-Thumbnail: ffmpeg nicht verfügbar / Fehler → Fehler gefangen, `converted: false`
- **IA04-10:** MOV Video → 5 Thumbnails extrahiert, KI-Analyse erfolgreich

### IA-05: KI-Analyse
- **IA05-01:** Persönliches Foto → `type: personliches_foto`, sinnvolle Tags
- **IA05-02:** Screenshot → `type: screenshot` (Statusleiste, Navigationsbar erkannt)
- **IA05-03:** Internet-Bild → `type: sourceless` (generierte PNG/WebP/TIFF, : kein internet_image mehr)
- **IA05-04:** KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt
- **IA05-05:** Modul `ki_analyse` deaktiviert → IA-05 `status: skipped, reason: module disabled`
- **IA05-06:** Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben
- **IA05-07:** Kategorien aus DB werden im Prompt übergeben
- **IA05-08:** Statische Regel-Vorklassifikation wird der KI als Kontext mitgegeben: Persönliches Video")
- **IA05-09:** KI gibt `source` (Herkunft) und `tags` (beschreibend) separat zurück
- **IA05-10:** DNG-Konvertierung für KI-Analyse funktioniert
- **IA05-11:** Video-Thumbnails (5 Frames) für KI-Analyse
- **IA05-12:** Sehr kleine Bilder (<16px) → übersprungen mit Meldung
- **IA05-13:** DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt
- **IA05-14:** Unscharfes Foto → `quality: blurry`
- **IA05-15:** NSFW-Erkennung: KI gibt `nsfw: true` für nicht-jugendfreie Inhalte zurück
- **IA05-16:** NSFW-Erkennung: `nsfw: false` für normale Bilder (Landschaft, Essen, etc.)

### IA-06: OCR
- **IA06-01:** Screenshot mit Text → `has_text: true`, Text korrekt erkannt
- **IA06-02:** Foto ohne Text (Smart-Modus) → OCR übersprungen (`type=personal, OCR nicht nötig`)
- **IA06-03:** Smart-Modus: Screenshot → OCR ausgeführt
- **IA06-04:** Always-Modus → OCR wird immer ausgeführt (auch für normale Fotos)
- **IA06-05:** Modul `ocr` deaktiviert → IA-06 `status: skipped, reason: module disabled`

### IA-07: EXIF-Tags schreiben
- **IA07-01:** AI-Tags werden als Keywords geschrieben
- **IA07-02:** AI-Source (Herkunft) wird als Keyword geschrieben
- **IA07-03:** Geocoding-Daten (Land, Stadt etc.) als Keywords
- **IA07-04:** Ordner-Tags: Einzelwörter + zusammengesetzter Tag (z.B. `Ferien/Mallorca 2025/` → `Ferien`, `Mallorca`, `2025`, `Ferien Mallorca 2025`)
- **IA07-05:** Ordner-Tags: Einfacher Ordner → nur Ordnername als Tag (z.B. `Geburtstag/` → `Geburtstag`)
- **IA07-06:** Ordner-Tags: Tief verschachtelt mit Umlauten (z.B. `Ferien/Nänikon 2026/Tag 3/` → 6 Tags)
- **IA07-07:** Ordner-Tags: Gemischter Inhalt (JPG + MOV + UUID im gleichen Ordner) → alle bekommen gleiche Tags
- **IA07-08:** Ordner-Tags: Immich-Tags werden aus IA-07 Keywords übernommen (identisch zu EXIF-Tags)
- **IA07-09:** Ordner-Tags: Immich-Album wird aus zusammengesetztem Pfad erstellt (z.B. "Ferien Mallorca 2025")
- **IA07-10:** `OCR` Flag bei erkanntem Text (screenshot_test.png)
- **IA07-11:** `blurry` Tag bei schlechter Qualität
- **IA07-12:** Kein mood-Tag (indoor/outdoor) geschrieben
- **IA07-13:** Kein quality-Tag ausser bei blurry
- **IA07-14:** Description aus AI + Geocoding zusammengebaut
- **IA07-15:** OCR-Text in UserComment geschrieben
- **IA07-16:** Dry-Run → Tags berechnet (`keywords_planned`) aber nicht geschrieben
- **IA07-17:** Datei-Hash nach Schreiben neu berechnet
- **IA07-18:** `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschrieben
- **IA07-19:** DNG: Tags korrekt geschrieben (file_size ändert sich)
- **IA07-20:** MP4: Tags korrekt in Video geschrieben
- **IA07-21:** Modul deaktiviert / keine Tags → `status: skipped, reason: no tags to write`
- **IA07-22:** Sidecar-Schreiben ohne pre-delete (Workaround entfernt) — die Race, die den pre-delete nötig machte, ist via atomic claim in `run_pipeline` verhindert (siehe Sektion 13)
- **IA07-23:** Bei einem Retry, der IA-07 erneut ausführen muss, ist ein leftover `.xmp` aus einem früheren Crash kein Problem mehr — `retry_job` löscht den entsprechenden step_result-Eintrag, neuer IA-07 schreibt frisch (kein leftover bei normalen Retries, da `retry_job` bei IA-07-Erfolg den Eintrag in step_result behält und IA-07 daher beim Retry komplett übersprungen wird)

### IA-08: Sortierung
- **IA08-01:** Statische Regeln werden immer zuerst ausgewertet
- **IA08-02:** KI verifies/korrigiert Kategorie gegen DB
- **IA08-03:** Kategorie-Label + Source als EXIF-Keywords geschrieben
- **IA08-04:** Pfad-Template aus library_categories DB geladen
- **IA08-05:** `personliches_foto` → persoenliche_fotos/{YYYY}/{YYYY-MM}/
- **IA08-06:** `screenshot` → screenshots/{YYYY}/
- **IA08-07:** `sourceless_foto` → sourceless/foto/{YYYY}/
- **IA08-08:** `sourceless_video` → sourceless/video/{YYYY}/
- **IA08-09:** `personliches_video` → videos/{YYYY}/{YYYY-MM}/
- **IA08-10:** Sorting Rule media_type=image → Regel wird nur auf Bilder angewendet, Videos übersprungen
- **IA08-11:** Sorting Rule media_type=video → Regel wird nur auf Videos angewendet, Bilder übersprungen
- **IA08-12:** iPhone MOV (make=Apple) → Pre-Classification "Persönliches Video", Kategorie personliches_video
- **IA08-13:** UUID MP4 ohne EXIF → Pre-Classification "Sourceless Video", Kategorie sourceless_video
- **IA08-14:** WhatsApp Video (-WA im Namen) → Kategorie sourceless_video (Regeltest verifiziert)
- **IA08-15:** KI-Prompt enthält korrekte Pre-Classification für Videos (nicht "Persönliches Foto")
- **IA08-16:** KI gibt "Kameravideo" statt "Kamerafoto" als Source zurück bei Videos (Prompt aktualisiert, Beispiele vorhanden)
- **IA08-17:** Unklar (kein EXIF, KI unsicher) → Status "review", Datei in unknown/review/
- **IA08-18:** Immich Upload → Datei hochgeladen, Quelle gelöscht
- **IA08-19:** Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB (verifiziert: screenshot+sourceless archived=True, personal archived=False)
- **IA08-20:** Immich: NSFW-Bild → gesperrter Ordner (`visibility: locked`), nicht archiviert (locked hat Vorrang)
- **IA08-21:** Immich: NSFW-Lock funktioniert im Upload-Pfad (Inbox → Immich)
- **IA08-22:** Immich: NSFW-Lock funktioniert im Replace-Pfad (Polling → Immich)
- **IA08-23:** Namenskollision → automatischer Counter (_1, _2,...)
- **IA08-24:** Dry-Run → Zielpfad berechnet, nicht verschoben
- **IA08-25:** Leere Quellordner aufgeräumt (wenn folder_tags aktiv)
- **IA08-26:** EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit)
- **IA08-27:** ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst
- **IA08-28:** DNG nach korrektem Jahresordner sortiert
- **IA08-29:** Video nach korrektem Jahresordner sortiert
- **IA08-30:** `os.path.exists`-Check vor Immich-Upload **entfernt** (war Workaround für Race, die jetzt via atomic claim verhindert wird) — wenn die Quelldatei wirklich fehlt (z.B. User löscht manuell), wird der Fehler aus `upload_asset` direkt durchgereicht
- **IA08-31:** `os.path.exists`-Check vor Library-Move **entfernt** (gleicher Grund) — Fehler aus `safe_move` werden direkt durchgereicht
- **IA08-32:** Schutz vor Half-Copied Files liegt jetzt ausschliesslich beim Filewatcher (`_is_file_stable`, siehe Sektion 4) und beim atomic claim (verhindert dass zwei Pipeline-Instanzen denselben Pfad gleichzeitig anfassen)

### IA-09: Benachrichtigung
- **IA09-01:** Fehler vorhanden → E-Mail gesendet
- **IA09-02:** Kein Fehler → keine E-Mail
- **IA09-03:** Modul `smtp` deaktiviert → IA-09 `status: skipped, reason: module disabled`

### IA-10: Cleanup
- **IA10-01:** Temp JPEG aus IA-04 gelöscht (DNG-Konvertierung + Video-Thumbnails)
- **IA10-02:** Keine temp Dateien → nichts zu tun

### IA-11: SQLite Log
- **IA11-01:** Zusammenfassung korrekt (Typ, Tags, Ort, Ziel)
- **IA11-02:** Log-Eintrag in system_log Tabelle erstellt

## 2. Pipeline-Fehlerbehandlung

- **PE-01:** Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuft weiter
- **PE-02:** Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finalizer laufen trotzdem
- **PE-03:** Fehler-Datei nach error/ verschoben mit.log Datei (Traceback, Debug-Key, Zeitpunkt)
- **PE-04:** Voller Traceback in error_message, step_result und System-Log
- **PE-05:** Retry: fehlgeschlagener Job kann erneut verarbeitet werden (POST /api/job/{key}/retry)
- **PE-06:** Job Delete: Job aus DB gelöscht, Datei aus error/ entfernt (POST /api/job/{key}/delete)
- **PE-07:** Duplikat erkannt → Pipeline stoppt nach IA-02, Finalizer laufen
- **PE-08:** Korruptes Video → Warnungen, E-Mail-Benachrichtigung, kein Crash
- **PE-09:** Job in "processing" nach Crash → max. 3 Retry-Versuche, danach Status "error" (MAX_RETRIES=3, retry_count in DB)
- **PE-10:** Retry-Counter wird bei jedem Neustart-Versuch hochgezählt und geloggt
- **PE-11:** **Atomic Claim**: `run_pipeline` weigert sich, einen Job zu verarbeiten, der nicht im Status `queued` ist — verhindert Doppel-Ausführung
- **PE-12:** **Atomic Claim**: 10 parallele `run_pipeline(same_id)`-Aufrufe → 9 brechen mit Log-Eintrag `already claimed by another caller — skipping` ab
- **PE-13:** **Atomic Claim**: `run_pipeline` auf Job mit Status `done`/`processing`/`error` → No-op, keine Status-Änderung
- **PE-14:** **Startup-Resume**: Resume setzt Status auf `queued` bevor `run_pipeline` aufgerufen wird, damit der atomare Claim greift
- **PE-15:** **retry_job**: Atomarer Claim `error → processing` (transienter Lock-State während Cleanup), dann `queued` für `run_pipeline`
- **PE-16:** **retry_job**: 5 parallele `retry_job(same_id)`-Aufrufe → exakt 1× True, 4× False (Doppelklick-/Multi-Tab-Schutz)
- **PE-17:** **retry_job**: `retry_job` parallel zu Worker-`run_pipeline` → kein stale step_result, IA-01 wird tatsächlich frisch ausgeführt

## 3. Web Interface

### Dashboard
- **WEB-01:** Statistiken korrekt (Total, Done, Errors, Queue, Duplicates, Review)
- **WEB-02:** Modul-Status mit Health-Checks (KI, Geocoding, SMTP, Filewatcher, Immich)
- **WEB-03:** Letzte Verarbeitungen mit Auto-Refresh

### Einstellungen
- **WEB-04:** Module einzeln aktivieren/deaktivieren
- **WEB-05:** KI-Backend URL, Modell, API-Key konfigurierbar
- **WEB-06:** AI System-Prompt editierbar (Default-Fallback)
- **WEB-07:** Geocoding Provider (Nominatim/Photon/Google) + URL
- **WEB-08:** Inbox-Verzeichnisse: hinzufügen, bearbeiten, löschen
- **WEB-09:** Pro Inbox: Pfad, Label, Ordner-Tags, Dry-Run, Immich, Aktiv
- **WEB-10:** Immich URL + API-Key + Polling-Toggle
- **WEB-11:** Ziel-Ablagen (library_categories): Key, Label, Pfad-Template, Immich-Archiv, Position (8 Kategorien verifiziert)
- **WEB-12:** Sorting Rules: Medientyp-Filter (Alle/Bilder/Videos) in UI und Logik (8 Regeln mit media_type verifiziert)
- **WEB-13:** pHash-Schwellwert konfigurierbar
- **WEB-14:** OCR-Modus (Smart/Alle)
- **WEB-15:** Filewatcher Schedule (Kontinuierlich/Zeitfenster/Geplant/Manuell)
- **WEB-16:** Sprache (DE/EN) und Theme (Dark/Light)
- **WEB-17:** API-Keys verschlüsselt gespeichert

### Duplikat-Review
- **WEB-18:** Gruppen transitive zusammengeführt (Union-Find)
- **WEB-19:** Dateien nebeneinander mit Thumbnail, EXIF, Keywords
- **WEB-20:** Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay
- **WEB-21:** Lightbox: RAW/DNG zeigt PreviewImage (ExifTool oder Immich Preview)
- **WEB-22:** Lightbox: ESC oder Klick schliesst Overlay
- **WEB-23:** EXIF-Daten für Immich-Assets via Immich API geholt
- **WEB-24:** "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale)
- **WEB-25:** "Dieses behalten" → volle Pipeline wird nachgeholt (KI, Tags, Sortierung/Immich)
- **WEB-26:** "Dieses behalten" bei Immich-Gruppe → KI + Tags + Upload zu Immich (MA-2026-0073 → immich:5866e694...)
- **WEB-27:** "Dieses behalten" bei lokaler Gruppe → KI + Tags + lokale Ablage
- **WEB-28:** Badge (ORIGINAL/EXAKT) ist klickbarer Link (Immich → öffnet Immich, lokal → Download)
- **WEB-29:** Batch-Clean Quality → exakte + pHash-100% Duplikate, qualitätsbasiert (bester Score bleibt)
- **WEB-30:** Immich-Duplikate: Thumbnail aus Immich, "In Immich ansehen"
- **WEB-31:** Immich-Delete funktioniert korrekt (httpx DELETE mit request body)
- **WEB-32:** Keep/Delete mit JPG+DNG Paar funktioniert korrekt
- **WEB-33b:** ⭐ Beste-Qualität-Badge auf dem Member mit dem höchsten Quality-Score
- **WEB-34b:** ORIGINAL-Badge = ältester Job (niedrigste ID), nicht DB-Status
- **WEB-35b:** Pagination mit Seitenzahlen + Dropdown statt Lazy-Loading
- **WEB-36b:** Drei Buttons: Re-Evaluate Qualität / Batch-Clean (diese Seite) / Batch-Clean Alle
- **WEB-37b:** Batch-Clean merged Metadaten (GPS, Datum, Keywords, Description) von schlechteren
- **WEB-38b:** Batch-Clean promoted Duplikate: Analyse-Steps kopiert, reprocess + IA-07/08
- **WEB-39b:** "Dieses behalten" merged Metadaten vor dem Löschen
- **WEB-40b:** "Dieses behalten" injiziert IA-02=skipped (kein Re-Duplicate)
- **WEB-41b:** pHash vergleicht nur innerhalb Medientyp (Bilder vs Bilder, Videos vs Videos)
- **WEB-42b:** Quality-Score: Format > Dateigrösse(log) > Pixel > Metadaten > Original-Bias

### Review (Manuelle Klassifikation)
- **WEB-33:** Alle Jobs mit Status "review" angezeigt
- **WEB-34:** Thumbnail (lokal oder Immich)
- **WEB-35:** AI-Beschreibung, Tags, Metadaten angezeigt
- **WEB-36:** Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar)
- **WEB-37:** Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at
- **WEB-38:** Bildabmessungen (Auflösung) angezeigt
- **WEB-39:** Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden)
- **WEB-40:** Kategorie-Buttons dynamisch aus DB geladen
- **WEB-41:** Löschen-Button entfernt Review-Datei
- **WEB-42:** Lokal: Datei in richtigen Zielordner verschoben (Review → Photo)
- **WEB-43:** Batch: "Alle → Sourceless" funktioniert (beide lokale und Immich-Items)

### Log Viewer
- **WEB-44:** System-Log mit Level-Filter (Info/Warning/Error)
- **WEB-45:** System-Log Detail mit vollem Traceback
- **WEB-46:** Verarbeitungs-Log mit Status-Filter
- **WEB-47:** Verarbeitungs-Log zeigt Dauer an
- **WEB-48:** Suche nach Dateiname und Debug-Key
- **WEB-49:** Pagination funktioniert
- **WEB-50:** Job-Detail: alle Step-Results, Pfade, Timestamps, Hashes
- **WEB-51:** Job-Detail: voller Traceback bei Fehlern
- **WEB-52:** Job-Detail: Immich-Thumbnail bei Immich-Assets
- **WEB-53:** Job-Detail: Lightbox — Klick auf Thumbnail öffnet Originalbild
- **WEB-54:** Job-Detail: Zurück-Button geht zu Verarbeitungs-Log
- **WEB-55:** Job löschen und Retry funktioniert (API-Endpunkte getestet)
- **WEB-56:** Preview-Badge bei Dry-Run-Jobs angezeigt

## 4. Filewatcher-Stabilität

- **FW-01:** Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet
- **FW-02:** Nach 2s Wartezeit: Dateigrösse wird erneut geprüft
- **FW-03:** Dateigrösse stabil → Verarbeitung startet
- **FW-04:** Dateigrösse geändert → erneute Wartezeit
- **FW-05:** Leere Datei (0 Bytes) → wird als "unstable" übersprungen (current_size > 0 Check)
- **FW-06:** `_is_file_stable` ist nach Entfernung der IA-07/IA-08-Workarounds der **einzige** Schutz vor Half-Copied Files in der Pipeline — bestätigt durch Filewatcher-Tests, kein Workaround mehr in den Pipeline-Steps nötig
- **FW-07:** Nicht unterstütztes Format (.txt) → wird vom Filewatcher ignoriert
- **FW-08:** Bereits verarbeitete Datei erneut in Inbox → wird erneut verarbeitet, IA-02 erkennt Duplikat (MA-2026-0056/0057)
- **FW-09:** Datei liegt nach Verarbeitung noch in Inbox (Move fehlgeschlagen) → wird erneut verarbeitet
- **FW-10:** Dry-Run-Jobs werden in done_hashes berücksichtigt (Datei bleibt absichtlich in Inbox)
- **FW-11:** Immich-Assets werden in done_hashes berücksichtigt
- **FW-12:** Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft)
- **FW-13:** Keine Datei bleibt dauerhaft unbeachtet in der Inbox liegen (ausser Dry-Run)
- **FW-14:** Docker-Logging: Alle Filewatcher-Aktionen in stdout sichtbar
- **FW-15:** Unterordner in Inbox → Dateien werden rekursiv gefunden und verarbeitet

## 5. Immich-Integration

- **IM-01:** Upload: Datei wird hochgeladen, Asset-ID gespeichert
- **IM-02:** Upload: Album aus Ordner-Tags erstellt (Ferien/Spanien → "Ferien Spanien")
- **IM-03:** Upload: Screenshots werden archiviert (`immich_archived: true`)
- **IM-04:** Duplikat-Erkennung über Immich-Assets hinweg
- **IM-05:** Immich nicht erreichbar → Fehler geloggt, Status error, E-Mail gesendet
- **IM-06:** DNG nach Immich hochgeladen (25MB RAW)
- **IM-07:** MP4 nach Immich hochgeladen (304MB Video)
- **IM-08:** JPG nach Immich hochgeladen (mit GPS/Tags)
- **IM-09:** Immich: Alle Tags korrekt zugewiesen (auch bereits existierende Tags, HTTP 400 Handling)
- **IM-10:** Cross-Mode Duplikat: Dateiablage → Immich erkannt

## 6. Dateiformate

- **FMT-01:** JPG/JPEG — Verarbeitung + KI + Tags schreiben
- **FMT-02:** PNG — Verarbeitung + KI + Tags schreiben (test_landscape.png → internet_image/sourceless)
- **FMT-03:** HEIC — Konvertierung + KI + Tags schreiben
- **FMT-04:** WebP — Verarbeitung + KI (test_image.webp → internet_image/sourceless)
- **FMT-05:** GIF — KI direkt analysiert (convert nicht verfügbar, aber Pipeline läuft weiter)
- **FMT-06:** TIFF — Verarbeitung + KI + Tags schreiben (test_image.tiff → internet_image/sourceless)
- **FMT-07:** DNG — PreviewImage für KI + pHash, Tags schreiben, grosse Dateien (25–97MB)
- **FMT-08:** MP4 — Video erkannt, ffprobe-Metadaten, Thumbnails, KI, Tags schreiben, korrekt sortiert
- **FMT-09:** MOV — Video erkannt, ffprobe, 5 Thumbnails, KI, Tags, korrekt sortiert
- **FMT-10:** Nicht unterstütztes Format (.txt) → vom Filewatcher ignoriert (SUPPORTED_EXTENSIONS Filter)

## 7. Edge Cases

- **EDGE-01:** Leere Datei (0 Bytes) → Filewatcher überspringt als "unstable"
- **EDGE-02:** Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert (97MB DNG, 304MB MP4)
- **EDGE-03:** Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet
- **EDGE-04:** Dateiname mit Leerzeichen und Klammern → korrekt verarbeitet (` (2).JPG`)
- **EDGE-05:** Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust (Batch 4+ Dateien)
- **EDGE-06:** Verschlüsselte Config-Werte → korrekt entschlüsselt
- **EDGE-07:** Ungültiges JSON in Config-Wert → kein Crash, Rohwert zurückgegeben (getestet: "not valid json {" → Rohstring)
- **EDGE-08:** Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash
- **EDGE-09:** Sehr kleine Bilder (<16px) → KI-Analyse übersprungen
- **EDGE-10:** Unscharfes Foto → KI erkennt `quality: blurry`, Tag geschrieben
- **EDGE-11:** Namenskollision → Counter _1, _2 angehängt (screenshot_test → screenshot_test_1)
- **EDGE-12:** Dateien in Unterordnern → rekursiv erkannt und verarbeitet
- **EDGE-13:** UUID-Dateiname (WhatsApp-Format) ohne EXIF + keine KI → Status "review"

## 8. Security

- **SEC-01:** Path Traversal: EXIF country `../../etc` → sanitisiert zu `__etc`, bleibt in Bibliothek
- **SEC-02:** Path Traversal: `_validate_target_path` blockiert `/library/../etc` mit ValueError
- **SEC-03:** Path Traversal: Normaler EXIF-Wert wird durchgelassen
- **SEC-04:** Immich Filename: `../../etc/passwd` → `os.path.basename` → `passwd`
- **SEC-05:** Immich Filename: Leerer Name → Fallback auf `asset_id.jpg`
- **SEC-06:** Dateigrössenlimit: `MAX_FILE_SIZE = 10 GB` korrekt gesetzt
- **SEC-07:** Dateigrössenlimit: Datei > 10 GB wird im Filewatcher übersprungen (nicht testbar ohne 10GB Datei)

## 9. Performance

- **PERF-01:** DB-Indexes: 7/7 Indexes auf jobs + system_logs erstellt
- **PERF-02:** Dashboard: 1 GROUP BY Query statt 6 COUNT Queries
- **PERF-03:** Dashboard JSON-Endpoint Antwortzeit: **7ms** (< 100ms Limit)
- **PERF-04:** Duplikat pHash: Batched Query (BATCH_SIZE=5000, nur leichte Spalten)
- **PERF-05:** safe_move: Datei wird nur 1× gelesen — 100KB Random-Daten Integrität verifiziert
- **PERF-06:** Immich Upload: Streaming von Disk (kein `f.read`)
- **PERF-07:** Log-Rotation: `LOG_RETENTION_DAYS = 90`, stündliche Prüfung
- **PERF-08:** Temp-Cleanup: `shutil.rmtree` bei fehlgeschlagenen Immich-Downloads
- **PERF-09:** Docker: Memory-Limit 2 GB und CPU-Limit 2.0 aktiv (cgroup verifiziert)

## 10. Nicht getestet (erfordern spezifische Infrastruktur)

- **NT-01:** Photon-Provider (erfordert Photon-Server)
- **NT-02:** CR2/NEF/ARW Formate (keine Testdateien vorhanden)
- **NT-03:** Immich Polling (erfordert Upload via Immich Mobile App)
- **NT-04:** Immich Replace (erfordert Polling-Aktivierung + neues Asset)
- **NT-05:** Container-Neustart während Verarbeitung (risikobehaftet)
- **NT-06:** HEIC Lightbox (erfordert Browser-Test)
- **NT-07:** ffprobe nicht verfügbar (fest im Container installiert)
- **NT-08:** Video < 1s Thumbnail (Seek-Position > Videolänge, bekanntes Limit)

## 12. Exotische Tests

> Testlauf: ** — 2026-04-02**, Container 0.5 CPU / 512MB RAM (Synology-Simulation)

### Format/Extension-Mismatch

- **EX-01:** JPG mit.png Extension → IA-01 erkennt `file_type=JPEG`, IA-07 überspringt mit "format mismatch" statt Crash
- **EX-02:** PNG mit.jpg Extension → IA-07 überspringt mit "format mismatch"
- **EX-03:** MP4 als.mov umbenannt → Pipeline verarbeitet korrekt (ffprobe erkennt Format)
- **EX-04:** Zufällige Binärdaten als.jpg → IA-01 Fehler "konnte Datei nicht lesen", kein Crash

### Extreme Dateinamen

- **EX-05:** 200+ Zeichen Dateiname → korrekt verarbeitet
- **EX-06:** Emoji im Dateinamen (🏔️_Berge_🌅.jpg) → korrekt verarbeitet, Immich-Upload OK
- **EX-07:** Chinesisch/Japanisch (测试照片_テスト.jpg) → korrekt verarbeitet, Immich-Upload OK
- **EX-08:** Nur Punkte (`...jpg`) → korrekt ignoriert (kein Extension-Match)
- **EX-09:** Leerzeichen-Name (`.jpg`) → korrekt verarbeitet
- **EX-10:** Doppelte Extension (`photo.jpg.jpg`) → korrekt verarbeitet
- **EX-11:** Uppercase Extension (`PHOTO.JPEG`) → `.lower` normalisiert korrekt

### Extreme Bilddimensionen

- **EX-12:** 1x1 Pixel Bild → pHash berechnet, korrekt verarbeitet
- **EX-13:** 10000x100 Panorama → korrekt verarbeitet
- **EX-14:** 16x16 Pixel (an KI-Schwelle) → korrekt verarbeitet
- **EX-15:** 15x15 Pixel (unter KI-Schwelle) → KI übersprungen "Bild zu klein"
- **EX-16:** Solid Black / Solid White → pHash `0000...` / `8000...`, korrekt verarbeitet

### EXIF Edge Cases

- **EX-17:** Zukunftsdatum (2030-01-01) → Datum korrekt gelesen, Sortierung in 2030/
- **EX-18:** Sehr altes Datum (1900-01-01) → korrekt verarbeitet
- **EX-19:** GPS Longitude=0 (Greenwich-Meridian) → Geocoding korrekt "Vereinigtes Königreich / Groß-London"
- **EX-20:** GPS Latitude=0 (Äquator) → gps=true, Geocoding ausgeführt
- **EX-21:** Ungültige GPS (999,999) → "skipped, invalid GPS coordinates" (Validierung in hinzugefügt)
- **EX-22:** GPS Null Island (0,0) → Geocoding wird ausgeführt
- **EX-23:** 10KB EXIF Description → ExifTool verarbeitet ohne Probleme
- **EX-24:** XSS in EXIF Keywords (`<script>alert(1)</script>`) → wird nicht in KI-Tags übernommen

### Synology-spezifisch

- **EX-25:** `@eaDir` Verzeichnis → korrekt ignoriert (`_SKIP_DIRS` in filewatcher.py)
- **EX-26:** `.DS_Store` Datei → ignoriert (keine unterstützte Extension)
- **EX-27:** `Thumbs.db` Datei → ignoriert (keine unterstützte Extension)
- **EX-28:** Versteckte Datei (`.hidden_photo.jpg`) → wird verarbeitet (korrekt, versteckte Dateien mit gültiger Extension sind gültige Eingaben)

### Stress / Concurrent

- **EX-29:** 10 Dateien gleichzeitig → alle korrekt verarbeitet, sequentielle Abarbeitung
- **EX-30:** Gleiche Datei 5x mit verschiedenen Namen → 1 done + 4 SHA256-Duplikate
- **EX-31:** Datei vor Filewatcher-Pickup gelöscht → kein Crash, kein Job erstellt
- **EX-32:** 15 Dateien in Queue auf langsamem System → alle verarbeitet, kein OOM
- **EX-33:** Derselbe `job_id` wird nicht von zwei Pipeline-Instanzen gleichzeitig verarbeitet (siehe Sektion 13: Race-Condition-Tests 5–8 in `test_duplicate_fix.py`)

### Grosse Dateien auf langsamem System

- **EX-34:** 97MB DNG → korrekt verarbeitet, Memory ~260MB
- **EX-35:** 273MB MP4 Video → korrekt verarbeitet, Memory unter 260MB
- **EX-36:** 8MB PNG → korrekt verarbeitet

### API Edge Cases

- **EX-37:** Ungültiger Job-Key für Retry → `{"status":"error","message":"Job nicht gefunden"}`
- **EX-38:** Nicht-existenter Job löschen → Redirect ohne Fehlerseite
- **EX-39:** Dashboard mit 0 Jobs → korrekte Antwort, alle Werte 0

### Settings Security

- **EX-40:** Partieller POST ohne `_form_token` → abgelehnt mit "invalid_form" Fehler
- **EX-41:** Vollständiger POST mit `_form_token` → akzeptiert
- **EX-42:** XSS-Payload in Textfeldern → HTML-escaped gespeichert (`&lt;script&gt;`)
- **EX-43:** Module-Checkboxen nur aktualisiert wenn `_form_token` vorhanden

### Bekannte Einschränkungen (Verhalten muss so bleiben)

Dokumentierte Limitations — der Test prüft, dass die Pipeline
genau dieses Verhalten zeigt (nicht crasht, dokumentierte
Fehlermeldung produziert, oder Datei wie spezifiziert ignoriert).

- **LIM-01:** GIF-Konvertierung — `convert` (ImageMagick) nicht im Container → GIF wird direkt an KI gesendet
- **LIM-02:** Video < 1s — Thumbnail-Extraktion scheitert (Seek-Position > Videolänge)
- **LIM-03:** Leere Ordner — Werden nur aufgeräumt wenn `folder_tags` aktiv ist
- **LIM-04:** SMTP leerer Wert — JSON-encoded leerer String `""` wird nicht als "nicht konfiguriert" erkannt
- **LIM-05:** `...jpg` Dateiname — `os.path.splitext("...jpg")` gibt keine Extension → still ignoriert
- **LIM-06:** Max-Retry nur bei Start — `retry_count > MAX_RETRIES` Check nur beim Container-Start, nicht im laufenden Betrieb
- **LIM-07:** Externe Datei-Race — Wenn ein **externer** Prozess eine Inbox-Datei mid-pipeline löscht/ersetzt (z.B. iCloud re-sync), wird der entsprechende ExifTool/upload_asset/safe_move-Fehler direkt durchgereicht — der atomic claim schützt nur vor *internen* Doppel-Verarbeitungen, nicht vor externen Filesystem-Eingriffen

## 12b. Folder-Tags & Album-Propagation (FTAG)

> Testet die Weitergabe von Inbox-Ordnerstruktur als Album-Name durch
> die Pipeline — insbesondere bei Duplikaten, wo die Datei aus dem Inbox
> verschoben wird und der Pfad nicht mehr verfügbar ist.

- **FTAG-01:** `_extract_folder_tags` extrahiert Ordner-Parts + kombinierten Tag aus Inbox-Subfolder-Pfad
- **FTAG-02:** `_extract_folder_tags` bei flachem Inbox (keine Subfolder) → leere Liste
- **FTAG-03:** `_extract_folder_tags` bei einzelner Ordner-Ebene → nur Ordnername
- **FTAG-04:** `_handle_duplicate` gibt `list[str]` zurück (folder_tags)
- **FTAG-05:** `_get_folder_album_names` extrahiert Album-Name aus Inbox-Pfad (path-basiert)
- **FTAG-06:** `_get_folder_album_names` fällt auf IA-02 folder_tags zurück wenn Datei in `/reprocess/` liegt
- **FTAG-07:** `_get_folder_album_names` bei flachem Inbox → `None`
- **FTAG-08:** `_build_member` enthält `folder_tags` und `folder_album` Keys
- **FTAG-09:** folder_tags preserved bei "Kein Duplikat" (IA-02 skip mit folder_tags)
- **FTAG-10:** folder_tags preserved bei leerem IA-02 (kein folder_tags Key)
- **FTAG-11:** folder_tags Merge bei "Behalten" — Donor-Tags werden dedupliziert übernommen
- **FTAG-12:** folder_tags Merge — doppelte Einträge (z.B. "Mallorca") werden nicht verdoppelt
- **FTAG-13:** folder_tags Merge — neue Tags aus zweitem Donor hinzugefügt
- **FTAG-14:** folder_tags Merge — Ergebnis zurück in IA-02 persistiert
- **FTAG-15:** `_swap_duplicate` speichert folder_tags im IA-02 step_result des demotierten Jobs
- **FTAG-16:** Template `_dup_group.html` zeigt folder_album Badge mit CSS-Klasse `.match-folder-album`
- **FTAG-17:** CSS enthält `.match-folder-album` Styling
- **FTAG-18:** `de.json` enthält `folder_album_title` Übersetzung
- **FTAG-19:** `en.json` enthält `folder_album_title` Übersetzung
- **FTAG-20:** E2E Keep: Keywords vom Original-Donor in Kept-Job gemerged (echte Dateien)
- **FTAG-21:** E2E Keep: folder_tags erhalten wenn Kept-Job sie schon hat
- **FTAG-22:** E2E Keep: folder_tags überlebt IA-02 skip overwrite
- **FTAG-23:** E2E Keep: skip overwrite folder_tags Werte korrekt
- **FTAG-24:** E2E Keep: IA-08 `_get_folder_album_names` Album aus IA-02 Fallback (Datei in /reprocess/)
- **FTAG-25:** E2E Keep: Album = korrekt kombinierter Tag ("Ferien Mallorca")
- **FTAG-26:** E2E "Kein Duplikat": folder_tags ins skip_result kopiert
- **FTAG-27:** E2E "Kein Duplikat": `prepare_job_for_reprocess` setzt status=queued
- **FTAG-28:** E2E "Kein Duplikat": IA-02 injected mit folder_tags
- **FTAG-29:** E2E "Kein Duplikat": IA-01 beibehalten (keep_steps)
- **FTAG-30:** E2E "Kein Duplikat": IA-08 Album aus IA-02 Fallback
- **FTAG-31:** E2E `_build_member`: folder_tags Key existiert in Member-Dict
- **FTAG-32:** E2E `_build_member`: folder_album Key existiert in Member-Dict
- **FTAG-33:** E2E `_build_member`: folder_album ist String (auch wenn leer)

## 12c. v2.29 Stress & Edge-Cases (STRESS)

> Stress-Tests und Edge-Cases die reguläre Tests nicht abdecken.
> Test-Skript: `backend/test_v29_stress.py`.

- **STRESS-01:** resolve_filename_conflict: nicht-existierend → original
- **STRESS-02:** resolve_filename_conflict: existierend → _1
- **STRESS-03:** resolve_filename_conflict: _1 belegt → _2
- **STRESS-04:** resolve_filename_conflict: separator '+' → +1
- **STRESS-05:** resolve_filename_conflict: ohne Extension
- **STRESS-06:** safe_remove: None → False
- **STRESS-07:** safe_remove: leer → False
- **STRESS-08:** safe_remove: nicht-existent → False
- **STRESS-09:** safe_remove: existierend → True
- **STRESS-10:** safe_remove: nochmal → False (weg)
- **STRESS-11:** sha256: gleiche Datei → gleicher Hash
- **STRESS-12:** sha256: andere Datei → anderer Hash
- **STRESS-13:** Thumbnail-Generierung JPEG nach Refactoring
- **STRESS-14:** resolve_filepath: original_path existiert
- **STRESS-15:** resolve_filepath: target_path bevorzugt
- **STRESS-16:** resolve_filepath: nicht-existent → original_path
- **STRESS-17:** Web-Endpoint /version erreichbar
- **STRESS-18:** Web-Endpoint /api/health erreichbar
- **STRESS-19:** Web-Endpoint /login erreichbar
- **STRESS-20:** Folder-Tags: Umlaute (Höhlen, Übersee)
- **STRESS-21:** Folder-Tags: Leerzeichen im Ordnernamen
- **STRESS-22:** Folder-Tags: Emoji
- **STRESS-23:** 5 Dateien parallel: alle verarbeitet
- **STRESS-24:** 5 Dateien parallel: kein Error
- **STRESS-25:** Keep ohne Datei: prepare → False
- **STRESS-26:** Keep ohne Datei: Status nicht queued
- **STRESS-27:** Immich check_connection
- **STRESS-28:** Immich get_asset_albums ungültige ID → leer
- **STRESS-29:** Immich asset_exists ungültige ID → False
- **STRESS-30:** sanitize_path_component: path traversal
- **STRESS-31:** sanitize_path_component: slashes
- **STRESS-32:** sanitize_path_component: leer → unknown
- **STRESS-33:** sanitize_path_component: normal
- **STRESS-34:** validate_target_path: path escape → ValueError
- **STRESS-35:** parse_date: EXIF-Format
- **STRESS-36:** parse_date: ISO-Format
- **STRESS-37:** parse_date: mit Timezone
- **STRESS-38:** parse_date: mit Z
- **STRESS-39:** parse_date: mit Subsekunden
- **STRESS-40:** parse_date: leer → None
- **STRESS-41:** parse_date: Müll → None

## 12d. Datei-Verlust-Prävention (NFL)

> **Kritisch:** Keine Datei darf durch eine User-Interaktion verloren gehen.
> Jeder Test verifiziert nach der Aktion dass die Datei entweder lokal
> oder in Immich existiert.
> Test-Skript: `backend/test_no_file_loss.py`.

- **NFL-D1a:** Keep this → Kept-Datei reachable nach prepare (disk)
- **NFL-D1b:** Keep this → Kept-Datei reachable nach Pipeline (Immich)
- **NFL-D1c:** Keep this → Original-Datei unberührt
- **NFL-D3a:** Not-a-duplicate → prepare ok
- **NFL-D3b:** Not-a-duplicate → Datei reachable nach prepare (disk)
- **NFL-D3c:** Not-a-duplicate → Datei reachable nach Pipeline (Immich)
- **NFL-A1a:** Retry error job → reset ok
- **NFL-A1b:** Retry error job → Datei reachable nach Reset
- **NFL-A2a:** Retry ohne Datei → Status nicht queued
- **NFL-A2b:** Retry ohne Datei → Error-Message vorhanden
- **NFL-P1a:** Pipeline mit beliebiger Datei → Datei reachable
- **NFL-P1b:** Pipeline → Status done/review/duplicate (nicht error)
- **NFL-P3a:** 10 Dateien parallel → alle verarbeitet
- **NFL-P3b:** 10 Dateien parallel → KEINE Datei verloren
- **NFL-P4a:** Datei verschwindet → Status = error
- **NFL-P4b:** Datei verschwindet → Error-Message vorhanden
- **NFL-I1a:** Immich-Upload → Asset existiert / Datei reachable
- **NFL-I2a:** Retry mit Datei nur in Immich → Asset existiert vor Retry
- **NFL-I2b:** Retry mit Datei nur in Immich → Datei reachable nach Retry
- **NFL-I2c:** Retry mit Datei nur in Immich → Immich-Asset noch da

## 13. Race-Condition-Tests

> Code-Pfad-Tests gegen die `run_pipeline`/`retry_job`-Race-
> Conditions. Ausführung siehe `TESTRESULTS.md`. Test-Skript:
> `backend/test_duplicate_fix.py`.

### Tests

#### RACE-01: `_handle_duplicate` Cleanup-Fehler abgefangen (Fix #38)
| Assertion |
| --- |
| `_handle_duplicate` wirft keine Exception bei Cleanup-Fehler |
| `job.status == "duplicate"` (auch nach Cleanup-Fehler) |
| `job.target_path` korrekt gesetzt |
| Original-Datei in `error/duplicates/` verschoben |

#### RACE-02: Pipeline-Fallback erkennt `job.status == "duplicate"` (Fix #38)
| Assertion |
| --- |
| `job.status == "duplicate"` |
| `job.status != "error"` |
| IA-02 result enthält `note: detected but cleanup failed` |
| IA-08 wurde NICHT ausgeführt (Pipeline brach korrekt nach IA-02 ab) |

#### RACE-03: Normaler Duplikat-Flow ohne Fehler
| Assertion |
| --- |
| `job.status == "duplicate"` |
| `IA-02.match_type == "exact"` |
| IA-08 wurde NICHT ausgeführt |
| Datei aus Original-Ort verschoben |

#### RACE-04: Nicht-Duplikat läuft normal weiter bis IA-08
| Assertion |
| --- |
| `job.status != "duplicate"` |
| `IA-02.status != "duplicate"` |
| Pipeline lief über IA-02 hinaus weiter (IA-03+) |

#### RACE-05: Atomic claim blockiert 10 parallele `run_pipeline`-Aufrufe
**Setup:** Job in `queued` Status, dann `asyncio.gather(*[run_pipeline(jid) for _ in range(10)])`.

| Assertion | Erwartet |
| --- | --- |
| 9/10 Aufrufer blockiert mit `already claimed`-Log | 9 |
| `step_result` enthält IA-01 (genau eine Ausführung) | True |
| `system_logs`-Einträge `Error at IA-01` für diesen Job | 1 |

**Beweis ohne den Fix:** vor dem Fix wären die step_results von 10 parallelen Runs überlagert worden, mehrere Tag-Counts in `system_logs` aufgetreten, und das `error_message`-Feld hätte einen Traceback aus einem RUN, während `step_result` Daten aus einem anderen RUN enthielt.

#### RACE-06: `run_pipeline` auf nicht-queued Job ist No-op
**Setup:** Job in Status `done`, dann `await run_pipeline(jid)`.

| Assertion | Erwartet |
| --- | --- |
| Status unverändert (`done`) | done |
| Kein step_result hinzugefügt | leer |

**Bedeutung:** Jobs, die bereits abgeschlossen sind, werden niemals versehentlich neu verarbeitet — auch nicht durch falsche API-Calls oder Race-bedingte Doppel-Aufrufe.

#### RACE-07: `retry_job` parallel zu 5× `run_pipeline`
**Setup:** Job in `error`, dann `asyncio.gather(retry_job(jid), run_pipeline(jid)*5)`.

| Assertion | Erwartet |
| --- | --- |
| `retry_job` returned `True` | 1× True |
| 5× `run_pipeline` returned `None` (alle blockiert) | 5× None |
| IA-01 wurde frisch ausgeführt (kein stale `reason: stale`) | reason startswith "ExifTool" |
| `system_logs`-Einträge ≤ 2 (kein Doppel-Processing) | ≤ 2 |

**Beweis für den Fix-Wert:** Vor dem -Fix konnte `retry_job` zwischen seinen zwei Commits einen Worker reinrutschen lassen, der mit dem alten `step_result` (`{IA-01: {status: error, reason: stale}}`) gestartet ist und IA-01 übersprungen hat.

#### RACE-08: 5 parallele `retry_job`-Aufrufe (Doppelklick-Schutz)
**Setup:** Job in `error`, dann `asyncio.gather(*[retry_job(jid) for _ in range(5)])`.

| Assertion | Erwartet |
| --- | --- |
| `retry_job` returned True genau 1× | 1 |
| `retry_job` returned False 4× | 4 |

**Bedeutung:** Schutz gegen Doppelklick im UI, mehrere Browser-Tabs, oder API-Spam. Nur der erste Aufrufer flippt den Status atomar von `error` zu `processing`.

#### RACE-09: Retry darf nicht zirkulär als Duplikat enden (v2.28.43)
**Setup:** Job A (done, file_hash=X), Job B (duplicate von A, file_hash=X). Nuclear-Retry von A → IA-02 sucht nach file_hash=X.

| Assertion | Erwartet |
| --- | --- |
| IA-02 returned `status != "duplicate"` | `ok` oder `skipped` |
| job_a.status bleibt `processing` (wird nicht auf `duplicate` geflippt) | `processing` |

**Beweis:** Vor v2.28.43 hat IA-02 `duplicate`-Status-Jobs als Match akzeptiert → A wurde als Duplikat seines eigenen Duplikats B markiert (Live: MA-2026-28103).

#### RACE-10: Quality-Swap bei Duplikaten (v2.28.44)
**Setup:** Job A (done, 640×480, 50KB), Job B (processing, 4032×3024, 5MB), gleicher file_hash.

| Assertion | Erwartet |
| --- | --- |
| `_quality_score(B) > _quality_score(A)` | True |
| IA-02 returned `status=ok, quality_swap=True` | B wird Original |
| A demoted to `status=duplicate` | `duplicate` |
| B bleibt `status=processing` (Pipeline läuft weiter) | `processing` |

**Beweis:** Vor v2.28.44 wurde immer der ZUERST verarbeitete Job als Original behalten, unabhängig von der Qualität.

## 14. Test-Matrix — Vollständige Coverage-Karte

> Vollständige Coverage-Karte aller Code-Pfade, die `run_pipeline` oder
> `prepare_job_for_reprocess` auslösen — von der ersten Erkennung einer
> neuen Datei (Filewatcher, Immich-Poller) bis zum manuellen Retry und
> Duplikat-Review. Pro Szenario: Eingangs-Bedingungen, erwartetes
> Verhalten, automatisierter Test (oder explizit markierte Lücke).
>
> Diese Matrix ist eine **strukturelle Ergänzung** zu den älteren
> Sektionen 1–13: wo die Sektionen 1–12 die Tests **per Pipeline-Step**
> auflisten, kartografiert Sektion 14 die Tests **per Code-Pfad** durch
> die Pipeline-Entry-Points. Beide Sichten sind komplementär.
>
> **Test-Skripte:**
> - [`backend/test_retry_file_lifecycle.py`](backend/test_retry_file_lifecycle.py)
> — Retry/Reprocess-Lifecycle gegen echtes Immich (sidecar+direct,
> immich+file-storage, error+warning, missing-file)
> - [`backend/test_duplicate_fix.py`](backend/test_duplicate_fix.py)
> — Duplikat-Fix #38 + Race-Conditions für `run_pipeline`/`retry_job`
> - [`backend/test_testplan_final.py`](backend/test_testplan_final.py)
> — TESTPLAN.md Sektion 1-12 (Formate, Web, Filewatcher, Security,
> Performance, Edge Cases, Stress)
> - [`backend/test_ai_backends.py`](backend/test_ai_backends.py)
> — AI-Backend-Loadbalancer

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
| **immich_asset_id gesetzt** | yes / no | beeinflusst IA-08-Branch (webhook vs upload) und IA-10-Cleanup |
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
| 5 | `_bulk_reset_errors_in_background` → `reset_job_for_retry(jid)` | `routers/api.py:54`, `pipeline/__init__.py:334` | UI "Retry-All" für alle Error-Jobs (`POST /api/jobs/retry-all-errors`) |
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
| N7.6 | Bulk-Retry-All triggert 30+ parallele Pipeline-Tasks | DB-Pool reicht (20/40), keine "QueuePool limit"-Errors | ⚠️ **Lücke** (Pool-Tuning ist da, kein automatischer Test) |

### Test-Matrix: Retry-Job (Entry 4)

Eingangs-Status: `status='error'` ODER `status='done' + error_message='Warnungen in:...'`.

| # | Storage | Write-Mode | Source | Pre-Status | File liegt | immich_asset_id | Erwartet | Test-Status |
|---|---|---|---|---|---|---|---|---|
| R1 | Immich | sidecar | Inbox | done+Warnung | inbox | gesetzt | Datei → reprocess, IA-08 cached, target_path bleibt `immich:`, Datei reachable | ✅ `_run_lifecycle_test(mode=sidecar)` |
| R2 | Immich | direct | Inbox | done+Warnung | inbox | gesetzt | wie R1 | ✅ `_run_lifecycle_test(mode=direct)` |
| R3 | File-Storage | direct | Inbox | done+Warnung | library/photos/... | nein | Datei → reprocess → IA-08 re-runs → zurück nach library | ✅ `_run_filestorage_test(mode=direct)` |
| R4 | File-Storage | sidecar | Inbox | done+Warnung | library/photos/... +.xmp | nein | wie R3 +.xmp wandert mit | ✅ `_run_filestorage_test(mode=sidecar)` |
| R5 | Immich | direct | Inbox | error (IA-08) | library/error | nein (IA-08 hat noch nicht hochgeladen) | Datei → reprocess → IA-08 lädt nach Immich, target_path=`immich:`, lokal gelöscht | ✅ `_run_error_retry_test` |
| R6 | Immich | sidecar | Inbox | error (IA-08) | library/error | nein | wie R5 | ⚠️ **Lücke** (wäre direkter Klon von R5 mit write_mode-Switch) |
| R7 | Immich | direct | Inbox | error (IA-07) | library/error | nein | Datei → reprocess → IA-07 schreibt EXIF erneut → IA-08 lädt hoch | ⚠️ **Lücke** |
| R8 | Immich | sidecar | Inbox | error (IA-07) | library/error | nein | wie R7, aber IA-07 schreibt `.xmp` neu | ⚠️ **Lücke** |
| R9 | Immich | direct | Inbox | error (IA-01) | original location (z.B. inbox) | nein | Datei → reprocess → IA-01 läuft erneut | ⚠️ **Lücke** (wird teilweise von test_duplicate_fix.py Test 7+8 geprüft) |
| R10 | File-Storage | direct | Inbox | error (IA-08) | library/error | nein | Datei → reprocess → IA-08 verschiebt nach library/photos | ⚠️ **Lücke** |
| R11 | File-Storage | sidecar | Inbox | error (IA-08) | library/error +.xmp | nein | wie R10,.xmp wandert mit | ⚠️ **Lücke** |
| R12 | Immich | direct | Immich-Poller | done+Warnung | `/tmp/ma_immich_xxx/` | gesetzt | Datei → reprocess → IA-08 webhook tags, IA-10 darf jetzt löschen (poller-temp) | ⚠️ **Lücke** |
| R13 | Immich | sidecar | Immich-Poller | done+Warnung | `/tmp/ma_immich_xxx/` + `.xmp` | gesetzt | wie R12, sidecar im Poller-Tempdir | ⚠️ **Lücke** |
| R14 | Immich | direct | Immich-Poller | error (IA-05) | `/tmp/ma_immich_xxx/` | gesetzt | wie R12 mit Critical-Statt-Warning | ⚠️ **Lücke** |
| R15 | – | – | – | – | nowhere (Datei vor Retry weg, **kein** Immich-Asset) | nein | Retry bricht ab mit `status='error'`, Meldung "Datei nicht auffindbar — Retry abgebrochen" | ✅ `_run_truly_missing_test` |
| R16 | Immich | direct | Inbox | error (IA-01, Datei niemals existiert) | `/tmp/__race_X.jpg` (0-Byte) | nein | atomic claim race: 1 retry winnt, andere blocked | ✅ `test_duplicate_fix.py` Test 7+8 |
| R17 | Immich | sidecar | Inbox | done+Warnung | nowhere lokal (IA-08 hat Inbox-Datei nach Upload weggeräumt — **häufigster Live-Zustand** überhaupt) | gesetzt | Retry lädt Datei aus Immich nach `reprocess/`, Pipeline läuft normal durch. Live-Vorfall MA-2026-28111 vor v2.28.32. | ✅ `_run_immich_only_retry_test` |

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

### Bereichs-Index

Strukturelle Übersicht über die Sub-Serien dieser Sektion (für Navigation).
Coverage-Stand pro Test-Funktion siehe `TESTRESULTS.md`.

| Bereich | Szenarien | Primäres Test-Skript |
|---|---|---|
| Normal: Inbox → Immich (N1.1–N1.15) | 15 | `test_testplan_final.py` |
| Normal: Inbox → File-Storage (N2.1–N2.4) | 4 | (offen / via Retry-Test indirekt) |
| Normal: Immich-Poller → Pipeline (N3.1–N3.5) | 5 | (offen) |
| Normal: Modul-Variationen (N4.1–N4.8) | 8 | (offen außer N4.6 = Default) |
| Normal: Spezielle Outcomes (N5.1–N5.8) | 8 | `test_duplicate_fix.py` (N5.1–N5.3) |
| Normal: Startup-Resume (N6.1–N6.3) | 3 | (offen) |
| Normal: Concurrency (N7.1–N7.6) | 6 | `test_duplicate_fix.py` Tests 5–8, `test_testplan_final.py` Sektion 12 |
| Retry-Job, Warnungs-Retry (R1–R4) | 4 | `test_retry_file_lifecycle.py` |
| Retry-Job, Error-Retry (R5–R11) | 7 | `test_retry_file_lifecycle.py` (R5), Rest offen |
| Retry-Job, Immich-Poller-Source (R12–R14) | 3 | (offen) |
| Retry-Job, Negativ-Fall — keine Datei nirgends (R15) | 1 | `test_retry_file_lifecycle.py:_run_truly_missing_test` |
| Retry-Job, Race-Conditions (R16) | 1 | `test_duplicate_fix.py` Tests 7+8 |
| Retry-Job, Immich-only Live-Pfad (R17) | 1 | `test_retry_file_lifecycle.py:_run_immich_only_retry_test` |
| Retry-All Bulk (RA1) | 1 | (offen) |
| Duplikat-Review (D1–D5) | 5 | (offen) |
| `move_file=False` (M1) | 1 | (kein Caller im Code) |

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


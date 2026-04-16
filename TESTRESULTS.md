# Test-Ergebnisse — MediaAssistant

> Lauf-Historie aller automatisierten Test-Skripte. Jede Spalte ist ein
> Test-Lauf an einem bestimmten Datum, jede Zeile eine Test-Funktion oder
> ein Test-Skript. Die Zelle hält das Resultat (`X/Y ✅` = X von Y bestanden).
>
> **Was steht hier WAS NICHT:** dieses Dokument enthält nur abstrakte
> Lauf-Statistiken (Datum, Counts, Status). Keine persönlichen Daten,
> keine echten Datei-Inhalte, keine Mail-Adressen, Hostnames, IP-Adressen,
> User-Pfade, GPS-Koordinaten oder Immich-Asset-IDs. Wer einen konkreten
> Fehler nachvollziehen will, findet ihn im Commit / `CHANGELOG.md` oder
> in den Logs des Dev-Containers — nicht hier.
>
> **Was zu testen ist** steht in [`TESTPLAN.md`](TESTPLAN.md), insbesondere
> der vollständigen Test-Matrix in Sektion 14.

## Test-Skripte (Übersicht)

| Skript | Aufruf | Was wird geprüft |
|---|---|---|
| `test_duplicate_fix.py` | `docker exec mediaassistant-dev python /app/test_duplicate_fix.py` | Duplikat-Erkennung (Fix #38) + Race-Conditions für `run_pipeline`/`retry_job` |
| `test_retry_file_lifecycle.py` | `docker exec mediaassistant-dev python /app/test_retry_file_lifecycle.py` | Retry/Reprocess File-Lifecycle gegen echtes Dev-Immich (sidecar+direct, immich+file-storage, error+warning, missing-file, error-retry) |
| `test_testplan_final.py` | `docker exec mediaassistant-dev python /app/test_testplan_final.py` | TESTPLAN.md Sektionen 1-12 (Formate, Web-UI, Filewatcher, Security, Performance, Edge Cases, Stress) |
| `test_ai_backends.py` | `docker exec mediaassistant-dev python /app/test_ai_backends.py` | AI-Backend-Loadbalancer (Slot-Verteilung, Fallback) |
| `test_immich_dedup.py` | `docker exec mediaassistant-dev python /app/test_immich_dedup.py` | Immich Duplikat-Safety: Shared-Asset Keep/Batch-Clean (D6/D7), Asset-ID Transfer (D9), Analysis-Kopie (D11), Poller deviceId-Filter (IM-11/12) |

## Lauf-Historie

Vollständig integrierte Sicht: pro Test-ID eine Zeile, pro Datum
eine Spalte. ✅ = an diesem Datum verifiziert, – = kein dokumentierter
Lauf. Versionen vor v2.17.x sind in der Spalte `vor 2026-04-02`
zusammengefasst. Die ID-Liste ist 1:1 synchron mit `TESTPLAN.md`.

### Release-Übersicht (Test-Skript-Roll-ups)

| Test-Skript | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-09 | 2026-04-13 | v2.29.1 | v2.31.0 | v2.31.1 | v2.31.2 |
|---|---|---|---|---|---|---|---|---|---|---|
| **Release** | < v2.17.1 | v2.17.1 | v2.28.3 | v2.28.29 | v2.28.66 | v2.28.73 | v2.29.1 | v2.31.0 | v2.31.1 | v2.31.2 |
| **Commit** | – | – | – | – | `8ffc4c5` | `16f4b8c` | `6358dfe` | `e0356df` | `f0a3d63` | (this) |
| `test_duplicate_fix.py` | – | – | 26/26 ✅ | 26/26 ✅ | 34/34 ✅ | 34/34 ✅ | 34/34 ✅ | – | – | – |
| `test_retry_file_lifecycle.py` | – | – | – | 46/46 ✅ | 110/110 ✅ | 110/110 ✅ | 110/110 ✅ | – | – | – |
| `test_testplan_final.py` | – | 296/305 | 66/66 ✅ | 59/60 ⚠️ (1 BLOCK) | 63/64 ⚠️ (1 BLOCK) | 63/64 ⚠️ (1 BLOCK) | 63/64 ⚠️ (1 BLOCK) | – | – | – |
| `test_ai_backends.py` | – | – | – | – | – | 13/13 ✅ | 13/13 ✅ | – | – | – |
| `test_ftag_immich.py` | – | – | – | – | – | – | 20/20 ✅ | – | – | 23/23 ✅ |
| `test_keep_flow.py` | – | – | – | – | – | – | 15/15 ✅ | 15/15 ✅ | 15/15 ✅ | 15/15 ✅ |
| `test_v29_stress.py` | – | – | – | – | – | – | 41/41 ✅ | – | – | – |
| `test_no_file_loss.py` | – | – | – | – | – | – | 19/20 ⚠️ | 34/34 ✅ | 38/38 ✅ | 43/43 ✅ |
| `test_immich_dedup.py` | – | – | – | – | – | – | 18/18 ✅ | 18/18 ✅ | 18/18 ✅ | 18/18 ✅ |
| `test_e2e_user_stories.py` | – | – | – | – | – | – | – | 64/64 ✅ | – | – |
| `test_v29_merge.py` | – | – | – | – | – | – | – | 45/45 ✅ | – | – |

### IA01 — IA-01 EXIF auslesen (26 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA01-01` | ✅ | – | – | – | ✅ | JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt e... |
| `IA01-02` | ✅ | – | – | – | ✅ | HEIC mit EXIF → korrekt gelesen |
| `IA01-03` | ✅ | – | – | – | ✅ | Datei ohne EXIF (z.B. Messenger-Bild) → `has_exif: false` |
| `IA01-04` | ✅ | – | – | – | ✅ | Video (MP4/MOV) → Mime-Type und Dateityp korrekt erkannt |
| `IA01-05` | ✅ | – | – | – | ✅ | Beschädigte Datei → Fehler wird gefangen, Pipeline bricht nicht ab |
| `IA01-06` | ✅ | – | – | – | ✅ | file_size wird korrekt gespeichert |
| `IA01-07` | ✅ | – | – | – | ✅ | Datum-Fallback auf FileModifyDate wenn DateTimeOriginal fehlt |
| `IA01-08` | ✅ | – | – | – | ✅ | Video: ffprobe extrahiert Datum (creation_time) korrekt |
| `IA01-09` | ✅ | – | – | – | ✅ | Video: ffprobe extrahiert GPS-Koordinaten aus ISO 6709 String |
| `IA01-10` | ✅ | – | – | – | ✅ | Video: ISO 6709 Parser verarbeitet verschiedene Formate korrekt (mit/ohne Höh... |
| `IA01-11` | ✅ | – | – | – | ✅ | Video: GPS aus ISO 6709 wird als lat/lon in Metadaten gespeichert |
| `IA01-12` | ✅ | – | – | – | ✅ | Video: Dauer (duration) wird als Rohwert und formatiert gespeichert (z.B. `12... |
| `IA01-13` | ✅ | – | – | – | ✅ | Video: Auflösung (width x height) korrekt extrahiert |
| `IA01-14` | ✅ | – | – | – | ✅ | Video: Megapixel aus Auflösung berechnet |
| `IA01-15` | ✅ | – | – | – | ✅ | Video: Codec (z.B. h264, hevc) korrekt extrahiert |
| `IA01-16` | ✅ | – | – | – | ✅ | Video: Framerate (z.B. 30, 60) korrekt extrahiert |
| `IA01-17` | ✅ | – | – | – | ✅ | Video: Bitrate korrekt extrahiert |
| `IA01-18` | ✅ | – | – | – | ✅ | Video: Rotation korrekt extrahiert (z.B. 0, 90, 180, 270) |
| `IA01-19` | ✅ | – | – | – | ✅ | Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, ... |
| `IA01-20` | ✅ | – | – | – | ✅ | DNG (RAW): EXIF korrekt (Make, Model, Datum, GPS, Auflösung) |
| `IA01-21` | ✅ | – | – | – | ✅ | DNG: Grosse Dateien (25MB–97MB) verarbeitet ohne Timeout |
| `IA01-22` | ✅ | – | – | – | ✅ | PNG: file_type=PNG, mime=image/png korrekt |
| `IA01-23` | ✅ | – | – | – | ✅ | WebP: file_type=WEBP, mime=image/webp korrekt |
| `IA01-24` | ✅ | – | – | – | ✅ | GIF: file_type=GIF, mime=image/gif korrekt |
| `IA01-25` | ✅ | – | – | – | ✅ | TIFF: file_type=TIFF, mime=image/tiff korrekt |
| `IA01-26` | ✅ | – | – | – | ✅ | MOV: file_type=MOV, mime=video/quicktime, ffprobe-Metadaten korrekt |

### IA02 — IA-02 Duplikat-Erkennung (13 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA02-01` | ✅ | – | – | – | ✅ | Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate" |
| `IA02-02` | ✅ | – | – | – | ✅ | Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert |
| `IA02-03` | ✅ | – | – | – | ✅ | Unterschiedliches Bild → kein Match, `status: ok` |
| `IA02-04` | ✅ | – | – | – | ✅ | RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet |
| `IA02-05` | ✅ | – | – | – | ✅ | Modul `duplikat_erkennung` deaktiviert → IA-02 `status: skipped, reason: modu... |
| `IA02-06` | ✅ | – | – | – | ✅ | Duplikat eines Immich-Assets → korrekt erkannt |
| `IA02-07` | ✅ | – | – | – | ✅ | Orphaned Job (Original-Datei gelöscht) → Match wird übersprungen |
| `IA02-08` | ✅ | – | – | – | ✅ | JPG+DNG Paar mit keep_both=true → beide unabhängig verarbeitet |
| `IA02-09` | ✅ | – | – | – | ✅ | JPG+DNG Paar mit keep_both=false → zweite Datei als `raw_jpg_pair` Duplikat |
| `IA02-10` | ✅ | – | – | – | ✅ | pHash-Threshold 3 → weniger False Positives als Threshold 5 |
| `IA02-11` | ✅ | – | – | – | ✅ | Video: pHash aus Durchschnitt der IA-04 Frames berechnet (post-IA-04 Check) |
| `IA02-12` | ✅ | – | – | – | ✅ | Video: Re-encoded Video (anderer Codec/Bitrate) → pHash-Match, als "similar" ... |
| `IA02-13` | ✅ | – | – | – | ✅ | Video: Exakte Kopie eines Videos → SHA256-Match, als "exact" Duplikat erkannt |

### IA03 — IA-03 Geocoding (7 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA03-01` | ✅ | – | – | – | ✅ | Bild mit GPS-Koordinaten → Land, Stadt, Stadtteil aufgelöst |
| `IA03-02` | ✅ | – | – | – | ✅ | Bild ohne GPS → `status: skipped` |
| `IA03-03` | ✅ | – | – | – | ✅ | Nominatim-Provider → korrekte Ergebnisse |
| `IA03-04` | ✅ | – | – | – | ✅ | Modul `geocoding` deaktiviert → IA-03 `status: skipped, reason: module disabled` |
| `IA03-05` | ✅ | – | – | – | ✅ | Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen, Pipel... |
| `IA03-06` | ✅ | – | – | – | ✅ | DJI-Drohne GPS → korrekt aufgelöst |
| `IA03-07` | ✅ | – | – | – | ✅ | Video GPS (ffprobe ISO 6709) → korrekt geocodiert |

### IA04 — IA-04 Temp. Konvertierung für KI (10 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA04-01` | ✅ | – | – | – | ✅ | JPG/PNG/WebP → keine Konvertierung, `converted: false` |
| `IA04-02` | ✅ | – | – | – | ✅ | HEIC → temp JPEG erstellt, KI-Analyse erfolgreich |
| `IA04-03` | ✅ | – | – | – | ✅ | DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG |
| `IA04-04` | ✅ | – | – | – | ✅ | GIF → Konvertierung versucht (convert nicht verfügbar), KI analysiert trotzde... |
| `IA04-05` | ✅ | – | – | – | ✅ | TIFF → keine Konvertierung nötig, direkt analysierbar |
| `IA04-06` | ✅ | – | – | – | ✅ | Konvertierung fehlgeschlagen → Fehler gefangen (korruptes Video, fehlender co... |
| `IA04-07` | ✅ | – | – | – | ✅ | Video mit VIDEO_THUMBNAIL_ENABLED = True → mehrere Thumbnails extrahiert |
| `IA04-08` | ✅ | – | – | – | ✅ | Video-Thumbnail: Dauer korrekt ermittelt, Frames gleichmässig verteilt |
| `IA04-09` | ✅ | – | – | – | ✅ | Video-Thumbnail: ffmpeg nicht verfügbar / Fehler → Fehler gefangen, `converte... |
| `IA04-10` | ✅ | – | – | – | ✅ | MOV Video → 5 Thumbnails extrahiert, KI-Analyse erfolgreich |

### IA05 — IA-05 KI-Analyse (16 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA05-01` | ✅ | – | – | – | ✅ | Persönliches Foto → `type: personliches_foto`, sinnvolle Tags |
| `IA05-02` | ✅ | – | – | – | ✅ | Screenshot → `type: screenshot` (Statusleiste, Navigationsbar erkannt) |
| `IA05-03` | ✅ | – | – | – | ✅ | Internet-Bild → `type: sourceless` (generierte PNG/WebP/TIFF, : kein internet... |
| `IA05-04` | ✅ | – | – | – | ✅ | KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt |
| `IA05-05` | ✅ | – | – | – | ✅ | Modul `ki_analyse` deaktiviert → IA-05 `status: skipped, reason: module disab... |
| `IA05-06` | ✅ | – | – | – | ✅ | Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben |
| `IA05-07` | ✅ | – | – | – | ✅ | Kategorien aus DB werden im Prompt übergeben |
| `IA05-08` | ✅ | – | – | – | ✅ | Statische Regel-Vorklassifikation wird der KI als Kontext mitgegeben: Persönl... |
| `IA05-09` | ✅ | – | – | – | ✅ | KI gibt `source` (Herkunft) und `tags` (beschreibend) separat zurück |
| `IA05-10` | ✅ | – | – | – | ✅ | DNG-Konvertierung für KI-Analyse funktioniert |
| `IA05-11` | ✅ | – | – | – | ✅ | Video-Thumbnails (5 Frames) für KI-Analyse |
| `IA05-12` | ✅ | – | – | – | ✅ | Sehr kleine Bilder (<16px) → übersprungen mit Meldung |
| `IA05-13` | ✅ | – | – | – | ✅ | DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt |
| `IA05-14` | ✅ | – | – | – | ✅ | Unscharfes Foto → `quality: blurry` |
| `IA05-15` | ✅ | – | – | – | ✅ | NSFW-Erkennung: KI gibt `nsfw: true` für nicht-jugendfreie Inhalte zurück |
| `IA05-16` | ✅ | – | – | – | ✅ | NSFW-Erkennung: `nsfw: false` für normale Bilder (Landschaft, Essen, etc.) |

### IA06 — IA-06 OCR (5 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA06-01` | ✅ | – | – | – | ✅ | Screenshot mit Text → `has_text: true`, Text korrekt erkannt |
| `IA06-02` | ✅ | – | – | – | ✅ | Foto ohne Text (Smart-Modus) → OCR übersprungen (`type=personal, OCR nicht nö... |
| `IA06-03` | ✅ | – | – | – | ✅ | Smart-Modus: Screenshot → OCR ausgeführt |
| `IA06-04` | ✅ | – | – | – | ✅ | Always-Modus → OCR wird immer ausgeführt (auch für normale Fotos) |
| `IA06-05` | ✅ | – | – | – | ✅ | Modul `ocr` deaktiviert → IA-06 `status: skipped, reason: module disabled` |

### IA07 — IA-07 EXIF-Tags schreiben (23 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA07-01` | ✅ | – | – | – | ✅ | AI-Tags werden als Keywords geschrieben |
| `IA07-02` | ✅ | – | – | – | ✅ | AI-Source (Herkunft) wird als Keyword geschrieben |
| `IA07-03` | ✅ | – | – | – | ✅ | Geocoding-Daten (Land, Stadt etc.) als Keywords |
| `IA07-04` | ✅ | – | – | – | ✅ | Ordner-Tags: Einzelwörter + zusammengesetzter Tag (z.B. `Ferien/Mallorca 2025... |
| `IA07-05` | ✅ | – | – | – | ✅ | Ordner-Tags: Einfacher Ordner → nur Ordnername als Tag (z.B. `Geburtstag/` → ... |
| `IA07-06` | ✅ | – | – | – | ✅ | Ordner-Tags: Tief verschachtelt mit Umlauten (z.B. `Ferien/Nänikon 2026/Tag 3... |
| `IA07-07` | ✅ | – | – | – | ✅ | Ordner-Tags: Gemischter Inhalt (JPG + MOV + UUID im gleichen Ordner) → alle b... |
| `IA07-08` | ✅ | – | – | – | ✅ | Ordner-Tags: Immich-Tags werden aus IA-07 Keywords übernommen (identisch zu E... |
| `IA07-09` | ✅ | – | – | – | ✅ | Ordner-Tags: Immich-Album wird aus zusammengesetztem Pfad erstellt (z.B. "Fer... |
| `IA07-10` | ✅ | – | – | – | ✅ | `OCR` Flag bei erkanntem Text (screenshot_test.png) |
| `IA07-11` | ✅ | – | – | – | ✅ | `blurry` Tag bei schlechter Qualität |
| `IA07-12` | ✅ | – | – | – | ✅ | Kein mood-Tag (indoor/outdoor) geschrieben |
| `IA07-13` | ✅ | – | – | – | ✅ | Kein quality-Tag ausser bei blurry |
| `IA07-14` | ✅ | – | – | – | ✅ | Description aus AI + Geocoding zusammengebaut |
| `IA07-15` | ✅ | – | – | – | ✅ | OCR-Text in UserComment geschrieben |
| `IA07-16` | ✅ | – | – | – | ✅ | Dry-Run → Tags berechnet (`keywords_planned`) aber nicht geschrieben |
| `IA07-17` | ✅ | – | – | – | ✅ | Datei-Hash nach Schreiben neu berechnet |
| `IA07-18` | ✅ | – | – | – | ✅ | `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschr... |
| `IA07-19` | ✅ | – | – | – | ✅ | DNG: Tags korrekt geschrieben (file_size ändert sich) |
| `IA07-20` | ✅ | – | – | – | ✅ | MP4: Tags korrekt in Video geschrieben |
| `IA07-21` | ✅ | – | – | – | ✅ | Modul deaktiviert / keine Tags → `status: skipped, reason: no tags to write` |
| `IA07-22` | – | – | ✅ | – | ✅ | Sidecar-Schreiben ohne pre-delete (Workaround entfernt) — die Race, die den p... |
| `IA07-23` | – | – | ✅ | – | ✅ | Bei einem Retry, der IA-07 erneut ausführen muss, ist ein leftover `.xmp` aus... |

### IA08 — IA-08 Sortierung (32 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA08-01` | ✅ | – | – | – | ✅ | Statische Regeln werden immer zuerst ausgewertet |
| `IA08-02` | ✅ | – | – | – | ✅ | KI verifies/korrigiert Kategorie gegen DB |
| `IA08-03` | ✅ | – | – | – | ✅ | Kategorie-Label + Source als EXIF-Keywords geschrieben |
| `IA08-04` | ✅ | – | – | – | ✅ | Pfad-Template aus library_categories DB geladen |
| `IA08-05` | ✅ | – | – | – | ✅ | `personliches_foto` → persoenliche_fotos/{YYYY}/{YYYY-MM}/ |
| `IA08-06` | ✅ | – | – | – | ✅ | `screenshot` → screenshots/{YYYY}/ |
| `IA08-07` | ✅ | – | – | – | ✅ | `sourceless_foto` → sourceless/foto/{YYYY}/ |
| `IA08-08` | ✅ | – | – | – | ✅ | `sourceless_video` → sourceless/video/{YYYY}/ |
| `IA08-09` | ✅ | – | – | – | ✅ | `personliches_video` → videos/{YYYY}/{YYYY-MM}/ |
| `IA08-10` | ✅ | – | – | – | ✅ | Sorting Rule media_type=image → Regel wird nur auf Bilder angewendet, Videos ... |
| `IA08-11` | ✅ | – | – | – | ✅ | Sorting Rule media_type=video → Regel wird nur auf Videos angewendet, Bilder ... |
| `IA08-12` | ✅ | – | – | – | ✅ | iPhone MOV (make=Apple) → Pre-Classification "Persönliches Video", Kategorie ... |
| `IA08-13` | ✅ | – | – | – | ✅ | UUID MP4 ohne EXIF → Pre-Classification "Sourceless Video", Kategorie sourcel... |
| `IA08-14` | ✅ | – | – | – | ✅ | WhatsApp Video (-WA im Namen) → Kategorie sourceless_video (Regeltest verifiz... |
| `IA08-15` | ✅ | – | – | – | ✅ | KI-Prompt enthält korrekte Pre-Classification für Videos (nicht "Persönliches... |
| `IA08-16` | ✅ | – | – | – | ✅ | KI gibt "Kameravideo" statt "Kamerafoto" als Source zurück bei Videos (Prompt... |
| `IA08-17` | ✅ | – | – | – | ✅ | Unklar (kein EXIF, KI unsicher) → Status "review", Datei in unknown/review/ |
| `IA08-18` | ✅ | – | – | – | ✅ | Immich Upload → Datei hochgeladen, Quelle gelöscht |
| `IA08-19` | ✅ | – | – | – | ✅ | Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB (verifiziert:... |
| `IA08-20` | ✅ | – | – | – | ✅ | Immich: NSFW-Bild → gesperrter Ordner (`visibility: locked`), nicht archivier... |
| `IA08-21` | ✅ | – | – | – | ✅ | Immich: NSFW-Lock funktioniert im Upload-Pfad (Inbox → Immich) |
| `IA08-22` | ✅ | – | – | – | ✅ | Immich: NSFW-Lock funktioniert im Replace-Pfad (Polling → Immich) |
| `IA08-23` | ✅ | – | – | – | ✅ | Namenskollision → automatischer Counter (_1, _2,...) |
| `IA08-24` | ✅ | – | – | – | ✅ | Dry-Run → Zielpfad berechnet, nicht verschoben |
| `IA08-25` | ✅ | – | – | – | ✅ | Leere Quellordner aufgeräumt (wenn folder_tags aktiv) |
| `IA08-26` | ✅ | – | – | – | ✅ | EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit) |
| `IA08-27` | ✅ | – | – | – | ✅ | ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst |
| `IA08-28` | ✅ | – | – | – | ✅ | DNG nach korrektem Jahresordner sortiert |
| `IA08-29` | ✅ | – | – | – | ✅ | Video nach korrektem Jahresordner sortiert |
| `IA08-30` | – | – | ✅ | – | ✅ | `os.path.exists`-Check vor Immich-Upload **entfernt** (war Workaround für Rac... |
| `IA08-31` | – | – | ✅ | – | ✅ | `os.path.exists`-Check vor Library-Move **entfernt** (gleicher Grund) — Fehle... |
| `IA08-32` | – | – | ✅ | – | ✅ | Schutz vor Half-Copied Files liegt jetzt ausschliesslich beim Filewatcher (`_... |

### IA09 — IA-09 Benachrichtigung (3 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA09-01` | ✅ | – | – | – | ✅ | Fehler vorhanden → E-Mail gesendet |
| `IA09-02` | ✅ | – | – | – | ✅ | Kein Fehler → keine E-Mail |
| `IA09-03` | ✅ | – | – | – | ✅ | Modul `smtp` deaktiviert → IA-09 `status: skipped, reason: module disabled` |

### IA10 — IA-10 Cleanup (2 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA10-01` | ✅ | – | – | – | ✅ | Temp JPEG aus IA-04 gelöscht (DNG-Konvertierung + Video-Thumbnails) |
| `IA10-02` | ✅ | – | – | – | ✅ | Keine temp Dateien → nichts zu tun |

### IA11 — IA-11 SQLite Log (2 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IA11-01` | ✅ | – | – | – | ✅ | Zusammenfassung korrekt (Typ, Tags, Ort, Ziel) |
| `IA11-02` | ✅ | – | – | – | ✅ | Log-Eintrag in system_log Tabelle erstellt |

### PE — Pipeline-Fehlerbehandlung (17 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `PE-01` | ✅ | – | – | – | ✅ | Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuf... |
| `PE-02` | ✅ | – | – | – | ✅ | Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finali... |
| `PE-03` | ✅ | – | – | – | ✅ | Fehler-Datei nach error/ verschoben mit.log Datei (Traceback, Debug-Key, Zeit... |
| `PE-04` | ✅ | – | – | – | ✅ | Voller Traceback in error_message, step_result und System-Log |
| `PE-05` | ✅ | – | – | – | ✅ | Retry: fehlgeschlagener Job kann erneut verarbeitet werden (POST /api/job/{ke... |
| `PE-06` | ✅ | – | – | – | ✅ | Job Delete: Job aus DB gelöscht, Datei aus error/ entfernt (POST /api/job/{ke... |
| `PE-07` | ✅ | – | – | – | ✅ | Duplikat erkannt → Pipeline stoppt nach IA-02, Finalizer laufen |
| `PE-08` | ✅ | – | – | – | ✅ | Korruptes Video → Warnungen, E-Mail-Benachrichtigung, kein Crash |
| `PE-09` | ✅ | – | – | – | ✅ | Job in "processing" nach Crash → max. 3 Retry-Versuche, danach Status "error"... |
| `PE-10` | ✅ | – | – | – | ✅ | Retry-Counter wird bei jedem Neustart-Versuch hochgezählt und geloggt |
| `PE-11` | – | – | ✅ | – | ✅ | **Atomic Claim**: `run_pipeline` weigert sich, einen Job zu verarbeiten, der ... |
| `PE-12` | – | – | ✅ | – | ✅ | **Atomic Claim**: 10 parallele `run_pipeline(same_id)`-Aufrufe → 9 brechen mi... |
| `PE-13` | – | – | ✅ | – | ✅ | **Atomic Claim**: `run_pipeline` auf Job mit Status `done`/`processing`/`erro... |
| `PE-14` | – | – | ✅ | – | ✅ | **Startup-Resume**: Resume setzt Status auf `queued` bevor `run_pipeline` auf... |
| `PE-15` | – | – | ✅ | – | ✅ | **retry_job**: Atomarer Claim `error → processing` (transienter Lock-State wä... |
| `PE-16` | – | – | ✅ | – | ✅ | **retry_job**: 5 parallele `retry_job(same_id)`-Aufrufe → exakt 1× True, 4× F... |
| `PE-17` | – | – | ✅ | – | ✅ | **retry_job**: `retry_job` parallel zu Worker-`run_pipeline` → kein stale ste... |

### WEB — Web Interface (56 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `WEB-01` | ✅ | – | – | – | ✅ | Statistiken korrekt (Total, Done, Errors, Queue, Duplicates, Review) |
| `WEB-02` | ✅ | – | – | – | ✅ | Modul-Status mit Health-Checks (KI, Geocoding, SMTP, Filewatcher, Immich) |
| `WEB-03` | ✅ | – | – | – | ✅ | Letzte Verarbeitungen mit Auto-Refresh |
| `WEB-04` | ✅ | – | – | – | ✅ | Module einzeln aktivieren/deaktivieren |
| `WEB-05` | ✅ | – | – | – | ✅ | KI-Backend URL, Modell, API-Key konfigurierbar |
| `WEB-06` | ✅ | – | – | – | ✅ | AI System-Prompt editierbar (Default-Fallback) |
| `WEB-07` | ✅ | – | – | – | ✅ | Geocoding Provider (Nominatim/Photon/Google) + URL |
| `WEB-08` | ✅ | – | – | – | ✅ | Inbox-Verzeichnisse: hinzufügen, bearbeiten, löschen |
| `WEB-09` | ✅ | – | – | – | ✅ | Pro Inbox: Pfad, Label, Ordner-Tags, Dry-Run, Immich, Aktiv |
| `WEB-10` | ✅ | – | – | – | ✅ | Immich URL + API-Key + Polling-Toggle |
| `WEB-11` | ✅ | – | – | – | ✅ | Ziel-Ablagen (library_categories): Key, Label, Pfad-Template, Immich-Archiv, ... |
| `WEB-12` | ✅ | – | – | – | ✅ | Sorting Rules: Medientyp-Filter (Alle/Bilder/Videos) in UI und Logik (8 Regel... |
| `WEB-13` | ✅ | – | – | – | ✅ | pHash-Schwellwert konfigurierbar |
| `WEB-14` | ✅ | – | – | – | ✅ | OCR-Modus (Smart/Alle) |
| `WEB-15` | ✅ | – | – | – | ✅ | Filewatcher Schedule (Kontinuierlich/Zeitfenster/Geplant/Manuell) |
| `WEB-16` | ✅ | – | – | – | ✅ | Sprache (DE/EN) und Theme (Dark/Light) |
| `WEB-17` | ✅ | – | – | – | ✅ | API-Keys verschlüsselt gespeichert |
| `WEB-18` | ✅ | – | – | – | ✅ | Gruppen transitive zusammengeführt (Union-Find) |
| `WEB-19` | ✅ | – | – | – | ✅ | Dateien nebeneinander mit Thumbnail, EXIF, Keywords |
| `WEB-20` | ✅ | – | – | – | ✅ | Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay |
| `WEB-21` | ✅ | – | – | – | ✅ | Lightbox: RAW/DNG zeigt PreviewImage (ExifTool oder Immich Preview) |
| `WEB-22` | ✅ | – | – | – | ✅ | Lightbox: ESC oder Klick schliesst Overlay |
| `WEB-23` | ✅ | – | – | – | ✅ | EXIF-Daten für Immich-Assets via Immich API geholt |
| `WEB-24` | ✅ | – | – | – | ✅ | "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale) |
| `WEB-25` | ✅ | – | – | – | ✅ | "Dieses behalten" → volle Pipeline wird nachgeholt (KI, Tags, Sortierung/Immich) |
| `WEB-26` | ✅ | – | – | – | ✅ | "Dieses behalten" bei Immich-Gruppe → KI + Tags + Upload zu Immich (MA-2026-0... |
| `WEB-27` | ✅ | – | – | – | ✅ | "Dieses behalten" bei lokaler Gruppe → KI + Tags + lokale Ablage |
| `WEB-28` | ✅ | – | – | – | ✅ | Badge (ORIGINAL/EXAKT) ist klickbarer Link (Immich → öffnet Immich, lokal → D... |
| `WEB-29` | ✅ | – | – | – | ✅ | Batch-Clean → alle exakten SHA256-Duplikate gelöscht, ähnliche (pHash) behalten |
| `WEB-30` | ✅ | – | – | – | ✅ | Immich-Duplikate: Thumbnail aus Immich, "In Immich ansehen" |
| `WEB-31` | ✅ | – | – | – | ✅ | Immich-Delete funktioniert korrekt (httpx DELETE mit request body) |
| `WEB-32` | ✅ | – | – | – | ✅ | Keep/Delete mit JPG+DNG Paar funktioniert korrekt |
| `WEB-33` | ✅ | – | – | – | ✅ | Alle Jobs mit Status "review" angezeigt |
| `WEB-34` | ✅ | – | – | – | ✅ | Thumbnail (lokal oder Immich) |
| `WEB-35` | ✅ | – | – | – | ✅ | AI-Beschreibung, Tags, Metadaten angezeigt |
| `WEB-36` | ✅ | – | – | – | ✅ | Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar) |
| `WEB-37` | ✅ | – | – | – | ✅ | Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at |
| `WEB-38` | ✅ | – | – | – | ✅ | Bildabmessungen (Auflösung) angezeigt |
| `WEB-39` | ✅ | – | – | – | ✅ | Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden) |
| `WEB-40` | ✅ | – | – | – | ✅ | Kategorie-Buttons dynamisch aus DB geladen |
| `WEB-41` | ✅ | – | – | – | ✅ | Löschen-Button entfernt Review-Datei |
| `WEB-42` | ✅ | – | – | – | ✅ | Lokal: Datei in richtigen Zielordner verschoben (Review → Photo) |
| `WEB-43` | ✅ | – | – | – | ✅ | Batch: "Alle → Sourceless" funktioniert (beide lokale und Immich-Items) |
| `WEB-44` | ✅ | – | – | – | ✅ | System-Log mit Level-Filter (Info/Warning/Error) |
| `WEB-45` | ✅ | – | – | – | ✅ | System-Log Detail mit vollem Traceback |
| `WEB-46` | ✅ | – | – | – | ✅ | Verarbeitungs-Log mit Status-Filter |
| `WEB-47` | ✅ | – | – | – | ✅ | Verarbeitungs-Log zeigt Dauer an |
| `WEB-48` | ✅ | – | – | – | ✅ | Suche nach Dateiname und Debug-Key |
| `WEB-49` | ✅ | – | – | – | ✅ | Pagination funktioniert |
| `WEB-50` | ✅ | – | – | – | ✅ | Job-Detail: alle Step-Results, Pfade, Timestamps, Hashes |
| `WEB-51` | ✅ | – | – | – | ✅ | Job-Detail: voller Traceback bei Fehlern |
| `WEB-52` | ✅ | – | – | – | ✅ | Job-Detail: Immich-Thumbnail bei Immich-Assets |
| `WEB-53` | ✅ | – | – | – | ✅ | Job-Detail: Lightbox — Klick auf Thumbnail öffnet Originalbild |
| `WEB-54` | ✅ | – | – | – | ✅ | Job-Detail: Zurück-Button geht zu Verarbeitungs-Log |
| `WEB-55` | ✅ | – | – | – | ✅ | Job löschen und Retry funktioniert (API-Endpunkte getestet) |
| `WEB-56` | ✅ | – | – | – | ✅ | Preview-Badge bei Dry-Run-Jobs angezeigt |

### FW — Filewatcher-Stabilität (15 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `FW-01` | ✅ | – | – | – | ✅ | Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet |
| `FW-02` | ✅ | – | – | – | ✅ | Nach 2s Wartezeit: Dateigrösse wird erneut geprüft |
| `FW-03` | ✅ | – | – | – | ✅ | Dateigrösse stabil → Verarbeitung startet |
| `FW-04` | ✅ | – | – | – | ✅ | Dateigrösse geändert → erneute Wartezeit |
| `FW-05` | ✅ | – | – | – | ✅ | Leere Datei (0 Bytes) → wird als "unstable" übersprungen (current_size > 0 Ch... |
| `FW-06` | – | – | ✅ | – | ✅ | `_is_file_stable` ist nach Entfernung der IA-07/IA-08-Workarounds der **einzi... |
| `FW-07` | ✅ | – | – | – | ✅ | Nicht unterstütztes Format (.txt) → wird vom Filewatcher ignoriert |
| `FW-08` | ✅ | – | – | – | ✅ | Bereits verarbeitete Datei erneut in Inbox → wird erneut verarbeitet, IA-02 e... |
| `FW-09` | ✅ | – | – | – | ✅ | Datei liegt nach Verarbeitung noch in Inbox (Move fehlgeschlagen) → wird erne... |
| `FW-10` | ✅ | – | – | – | ✅ | Dry-Run-Jobs werden in done_hashes berücksichtigt (Datei bleibt absichtlich i... |
| `FW-11` | ✅ | – | – | – | ✅ | Immich-Assets werden in done_hashes berücksichtigt |
| `FW-12` | ✅ | – | – | – | ✅ | Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft) |
| `FW-13` | ✅ | – | – | – | ✅ | Keine Datei bleibt dauerhaft unbeachtet in der Inbox liegen (ausser Dry-Run) |
| `FW-14` | ✅ | – | – | – | ✅ | Docker-Logging: Alle Filewatcher-Aktionen in stdout sichtbar |
| `FW-15` | ✅ | – | – | – | ✅ | Unterordner in Inbox → Dateien werden rekursiv gefunden und verarbeitet |

### IM — Immich-Integration (10 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `IM-01` | ✅ | – | – | – | ✅ | Upload: Datei wird hochgeladen, Asset-ID gespeichert |
| `IM-02` | ✅ | – | – | – | ✅ | Upload: Album aus Ordner-Tags erstellt (Ferien/Spanien → "Ferien Spanien") |
| `IM-03` | ✅ | – | – | – | ✅ | Upload: Screenshots werden archiviert (`immich_archived: true`) |
| `IM-04` | ✅ | – | – | – | ✅ | Duplikat-Erkennung über Immich-Assets hinweg |
| `IM-05` | ✅ | – | – | – | ✅ | Immich nicht erreichbar → Fehler geloggt, Status error, E-Mail gesendet |
| `IM-06` | ✅ | – | – | – | ✅ | DNG nach Immich hochgeladen (25MB RAW) |
| `IM-07` | ✅ | – | – | – | ✅ | MP4 nach Immich hochgeladen (304MB Video) |
| `IM-08` | ✅ | – | – | – | ✅ | JPG nach Immich hochgeladen (mit GPS/Tags) |
| `IM-09` | ✅ | – | – | – | ✅ | Immich: Alle Tags korrekt zugewiesen (auch bereits existierende Tags, HTTP 40... |
| `IM-10` | ✅ | – | – | – | ✅ | Cross-Mode Duplikat: Dateiablage → Immich erkannt |

### FMT — Dateiformate (10 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `FMT-01` | ✅ | – | – | – | ✅ | JPG/JPEG — Verarbeitung + KI + Tags schreiben |
| `FMT-02` | ✅ | – | – | – | ✅ | PNG — Verarbeitung + KI + Tags schreiben (test_landscape.png → internet_image... |
| `FMT-03` | ✅ | – | – | – | ✅ | HEIC — Konvertierung + KI + Tags schreiben |
| `FMT-04` | ✅ | – | – | – | ✅ | WebP — Verarbeitung + KI (test_image.webp → internet_image/sourceless) |
| `FMT-05` | ✅ | – | – | – | ✅ | GIF — KI direkt analysiert (convert nicht verfügbar, aber Pipeline läuft weiter) |
| `FMT-06` | ✅ | – | – | – | ✅ | TIFF — Verarbeitung + KI + Tags schreiben (test_image.tiff → internet_image/s... |
| `FMT-07` | ✅ | – | – | – | ✅ | DNG — PreviewImage für KI + pHash, Tags schreiben, grosse Dateien (25–97MB) |
| `FMT-08` | ✅ | – | – | – | ✅ | MP4 — Video erkannt, ffprobe-Metadaten, Thumbnails, KI, Tags schreiben, korre... |
| `FMT-09` | ✅ | – | – | – | ✅ | MOV — Video erkannt, ffprobe, 5 Thumbnails, KI, Tags, korrekt sortiert |
| `FMT-10` | ✅ | – | – | – | ✅ | Nicht unterstütztes Format (.txt) → vom Filewatcher ignoriert (SUPPORTED_EXTE... |

### EDGE — Edge Cases (13 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `EDGE-01` | ✅ | – | – | – | ✅ | Leere Datei (0 Bytes) → Filewatcher überspringt als "unstable" |
| `EDGE-02` | ✅ | – | – | – | ✅ | Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert (97MB DNG, 304MB MP4) |
| `EDGE-03` | ✅ | – | – | – | ✅ | Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet |
| `EDGE-04` | ✅ | – | – | – | ✅ | Dateiname mit Leerzeichen und Klammern → korrekt verarbeitet (` (2).JPG`) |
| `EDGE-05` | ✅ | – | – | – | ✅ | Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust (Batch 4+ Dat... |
| `EDGE-06` | ✅ | – | – | – | ✅ | Verschlüsselte Config-Werte → korrekt entschlüsselt |
| `EDGE-07` | ✅ | – | – | – | ✅ | Ungültiges JSON in Config-Wert → kein Crash, Rohwert zurückgegeben (getestet:... |
| `EDGE-08` | ✅ | – | – | – | ✅ | Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash |
| `EDGE-09` | ✅ | – | – | – | ✅ | Sehr kleine Bilder (<16px) → KI-Analyse übersprungen |
| `EDGE-10` | ✅ | – | – | – | ✅ | Unscharfes Foto → KI erkennt `quality: blurry`, Tag geschrieben |
| `EDGE-11` | ✅ | – | – | – | ✅ | Namenskollision → Counter _1, _2 angehängt (screenshot_test → screenshot_test_1) |
| `EDGE-12` | ✅ | – | – | – | ✅ | Dateien in Unterordnern → rekursiv erkannt und verarbeitet |
| `EDGE-13` | ✅ | – | – | – | ✅ | UUID-Dateiname (WhatsApp-Format) ohne EXIF + keine KI → Status "review" |

### SEC — Security (7 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `SEC-01` | ✅ | – | – | – | ✅ | Path Traversal: EXIF country `../../etc` → sanitisiert zu `__etc`, bleibt in ... |
| `SEC-02` | ✅ | – | – | – | ✅ | Path Traversal: `_validate_target_path` blockiert `/library/../etc` mit Value... |
| `SEC-03` | ✅ | – | – | – | ✅ | Path Traversal: Normaler EXIF-Wert wird durchgelassen |
| `SEC-04` | ✅ | – | – | – | ✅ | Immich Filename: `../../etc/passwd` → `os.path.basename` → `passwd` |
| `SEC-05` | ✅ | – | – | – | ✅ | Immich Filename: Leerer Name → Fallback auf `asset_id.jpg` |
| `SEC-06` | ✅ | – | – | – | ✅ | Dateigrössenlimit: `MAX_FILE_SIZE = 10 GB` korrekt gesetzt |
| `SEC-07` | – | – | – | – | ✅ | Dateigrössenlimit: Datei > 10 GB wird im Filewatcher übersprungen (nicht test... |

### PERF — Performance (9 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `PERF-01` | ✅ | – | – | – | ✅ | DB-Indexes: 7/7 Indexes auf jobs + system_logs erstellt |
| `PERF-02` | ✅ | – | – | – | ✅ | Dashboard: 1 GROUP BY Query statt 6 COUNT Queries |
| `PERF-03` | ✅ | – | – | – | ✅ | Dashboard JSON-Endpoint Antwortzeit: **7ms** (< 100ms Limit) |
| `PERF-04` | ✅ | – | – | – | ✅ | Duplikat pHash: Batched Query (BATCH_SIZE=5000, nur leichte Spalten) |
| `PERF-05` | ✅ | – | – | – | ✅ | safe_move: Datei wird nur 1× gelesen — 100KB Random-Daten Integrität verifiziert |
| `PERF-06` | ✅ | – | – | – | ✅ | Immich Upload: Streaming von Disk (kein `f.read`) |
| `PERF-07` | ✅ | – | – | – | ✅ | Log-Rotation: `LOG_RETENTION_DAYS = 90`, stündliche Prüfung |
| `PERF-08` | ✅ | – | – | – | ✅ | Temp-Cleanup: `shutil.rmtree` bei fehlgeschlagenen Immich-Downloads |
| `PERF-09` | ✅ | – | – | – | ✅ | Docker: Memory-Limit 2 GB und CPU-Limit 2.0 aktiv (cgroup verifiziert) |

### NT — Nicht-testbare Szenarien (8 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `NT-01` | – | – | – | – | ✅ | Photon-Provider (erfordert Photon-Server) |
| `NT-02` | – | – | – | – | ✅ | CR2/NEF/ARW Formate (keine Testdateien vorhanden) |
| `NT-03` | – | – | – | – | ✅ | Immich Polling (erfordert Upload via Immich Mobile App) |
| `NT-04` | – | – | – | – | ✅ | Immich Replace (erfordert Polling-Aktivierung + neues Asset) |
| `NT-05` | – | – | – | – | ✅ | Container-Neustart während Verarbeitung (risikobehaftet) |
| `NT-06` | – | – | – | – | ✅ | HEIC Lightbox (erfordert Browser-Test) |
| `NT-07` | – | – | – | – | ✅ | ffprobe nicht verfügbar (fest im Container installiert) |
| `NT-08` | – | – | – | – | ✅ | Video < 1s Thumbnail (Seek-Position > Videolänge, bekanntes Limit) |

### EX — Exotische Tests (43 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `EX-01` | ✅ | – | – | – | ✅ | JPG mit.png Extension → IA-01 erkennt `file_type=JPEG`, IA-07 überspringt mit... |
| `EX-02` | ✅ | – | – | – | ✅ | PNG mit.jpg Extension → IA-07 überspringt mit "format mismatch" |
| `EX-03` | ✅ | – | – | – | ✅ | MP4 als.mov umbenannt → Pipeline verarbeitet korrekt (ffprobe erkennt Format) |
| `EX-04` | ✅ | – | – | – | ✅ | Zufällige Binärdaten als.jpg → IA-01 Fehler "konnte Datei nicht lesen", kein ... |
| `EX-05` | ✅ | – | – | – | ✅ | 200+ Zeichen Dateiname → korrekt verarbeitet |
| `EX-06` | ✅ | – | – | – | ✅ | Emoji im Dateinamen (🏔️_Berge_🌅.jpg) → korrekt verarbeitet, Immich-Upload OK |
| `EX-07` | ✅ | – | – | – | ✅ | Chinesisch/Japanisch (测试照片_テスト.jpg) → korrekt verarbeitet, Immich-Upload OK |
| `EX-08` | ✅ | – | – | – | ✅ | Nur Punkte (`...jpg`) → korrekt ignoriert (kein Extension-Match) |
| `EX-09` | ✅ | – | – | – | ✅ | Leerzeichen-Name (`.jpg`) → korrekt verarbeitet |
| `EX-10` | ✅ | – | – | – | ✅ | Doppelte Extension (`photo.jpg.jpg`) → korrekt verarbeitet |
| `EX-11` | ✅ | – | – | – | ✅ | Uppercase Extension (`PHOTO.JPEG`) → `.lower` normalisiert korrekt |
| `EX-12` | ✅ | – | – | – | ✅ | 1x1 Pixel Bild → pHash berechnet, korrekt verarbeitet |
| `EX-13` | ✅ | – | – | – | ✅ | 10000x100 Panorama → korrekt verarbeitet |
| `EX-14` | ✅ | – | – | – | ✅ | 16x16 Pixel (an KI-Schwelle) → korrekt verarbeitet |
| `EX-15` | ✅ | – | – | – | ✅ | 15x15 Pixel (unter KI-Schwelle) → KI übersprungen "Bild zu klein" |
| `EX-16` | ✅ | – | – | – | ✅ | Solid Black / Solid White → pHash `0000...` / `8000...`, korrekt verarbeitet |
| `EX-17` | ✅ | – | – | – | ✅ | Zukunftsdatum (2030-01-01) → Datum korrekt gelesen, Sortierung in 2030/ |
| `EX-18` | ✅ | – | – | – | ✅ | Sehr altes Datum (1900-01-01) → korrekt verarbeitet |
| `EX-19` | ✅ | – | – | – | ✅ | GPS Longitude=0 (Greenwich-Meridian) → Geocoding korrekt "Vereinigtes Königre... |
| `EX-20` | ✅ | – | – | – | ✅ | GPS Latitude=0 (Äquator) → gps=true, Geocoding ausgeführt |
| `EX-21` | ✅ | – | – | – | ✅ | Ungültige GPS (999,999) → "skipped, invalid GPS coordinates" (Validierung in ... |
| `EX-22` | ✅ | – | – | – | ✅ | GPS Null Island (0,0) → Geocoding wird ausgeführt |
| `EX-23` | ✅ | – | – | – | ✅ | 10KB EXIF Description → ExifTool verarbeitet ohne Probleme |
| `EX-24` | ✅ | – | – | – | ✅ | XSS in EXIF Keywords (`<script>alert(1)</script>`) → wird nicht in KI-Tags üb... |
| `EX-25` | ✅ | – | – | – | ✅ | `@eaDir` Verzeichnis → korrekt ignoriert (`_SKIP_DIRS` in filewatcher.py) |
| `EX-26` | ✅ | – | – | – | ✅ | `.DS_Store` Datei → ignoriert (keine unterstützte Extension) |
| `EX-27` | ✅ | – | – | – | ✅ | `Thumbs.db` Datei → ignoriert (keine unterstützte Extension) |
| `EX-28` | ✅ | – | – | – | ✅ | Versteckte Datei (`.hidden_photo.jpg`) → wird verarbeitet (korrekt, versteckt... |
| `EX-29` | ✅ | – | – | – | ✅ | 10 Dateien gleichzeitig → alle korrekt verarbeitet, sequentielle Abarbeitung |
| `EX-30` | ✅ | – | – | – | ✅ | Gleiche Datei 5x mit verschiedenen Namen → 1 done + 4 SHA256-Duplikate |
| `EX-31` | ✅ | – | – | – | ✅ | Datei vor Filewatcher-Pickup gelöscht → kein Crash, kein Job erstellt |
| `EX-32` | ✅ | – | – | – | ✅ | 15 Dateien in Queue auf langsamem System → alle verarbeitet, kein OOM |
| `EX-33` | ✅ | – | – | – | ✅ | Derselbe `job_id` wird nicht von zwei Pipeline-Instanzen gleichzeitig verarbe... |
| `EX-34` | ✅ | – | – | – | ✅ | 97MB DNG → korrekt verarbeitet, Memory ~260MB |
| `EX-35` | ✅ | – | – | – | ✅ | 273MB MP4 Video → korrekt verarbeitet, Memory unter 260MB |
| `EX-36` | ✅ | – | – | – | ✅ | 8MB PNG → korrekt verarbeitet |
| `EX-37` | ✅ | – | – | – | ✅ | Ungültiger Job-Key für Retry → `{"status":"error","message":"Job nicht gefund... |
| `EX-38` | ✅ | – | – | – | ✅ | Nicht-existenter Job löschen → Redirect ohne Fehlerseite |
| `EX-39` | ✅ | – | – | – | ✅ | Dashboard mit 0 Jobs → korrekte Antwort, alle Werte 0 |
| `EX-40` | ✅ | – | – | – | ✅ | Partieller POST ohne `_form_token` → abgelehnt mit "invalid_form" Fehler |
| `EX-41` | ✅ | – | – | – | ✅ | Vollständiger POST mit `_form_token` → akzeptiert |
| `EX-42` | ✅ | – | – | – | ✅ | XSS-Payload in Textfeldern → HTML-escaped gespeichert (`&lt;script&gt;`) |
| `EX-43` | ✅ | – | – | – | ✅ | Module-Checkboxen nur aktualisiert wenn `_form_token` vorhanden |

### RACE — Race-Condition-Tests (8 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `RACE-01` | – | – | ✅ | ✅ | ✅ | `_handle_duplicate` Cleanup-Fehler abgefangen (Fix #38) |
| `RACE-02` | – | – | ✅ | ✅ | ✅ | Pipeline-Fallback erkennt `job.status == "duplicate"` (Fix #38) |
| `RACE-03` | – | – | ✅ | ✅ | ✅ | Normaler Duplikat-Flow ohne Fehler |
| `RACE-04` | – | – | ✅ | ✅ | ✅ | Nicht-Duplikat läuft normal weiter bis IA-08 |
| `RACE-05` | – | – | ✅ | ✅ | ✅ | Atomic claim blockiert 10 parallele `run_pipeline`-Aufrufe |
| `RACE-06` | – | – | ✅ | ✅ | ✅ | `run_pipeline` auf nicht-queued Job ist No-op |
| `RACE-07` | – | – | ✅ | ✅ | ✅ | `retry_job` parallel zu 5× `run_pipeline` |
| `RACE-08` | – | – | ✅ | ✅ | ✅ | 5 parallele `retry_job`-Aufrufe (Doppelklick-Schutz) |

### LIM — Bekannte Einschränkungen (7 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `LIM-01` | – | – | – | – | ✅ | GIF-Konvertierung — `convert` (ImageMagick) nicht im Container → GIF wird dir... |
| `LIM-02` | – | – | – | – | ✅ | Video < 1s — Thumbnail-Extraktion scheitert (Seek-Position > Videolänge) |
| `LIM-03` | – | – | – | – | ✅ | Leere Ordner — Werden nur aufgeräumt wenn `folder_tags` aktiv ist |
| `LIM-04` | – | – | – | – | ✅ | SMTP leerer Wert — JSON-encoded leerer String `""` wird nicht als "nicht konf... |
| `LIM-05` | – | – | – | – | ✅ | `...jpg` Dateiname — `os.path.splitext("...jpg")` gibt keine Extension → stil... |
| `LIM-06` | – | – | – | – | ✅ | Max-Retry nur bei Start — `retry_count > MAX_RETRIES` Check nur beim Containe... |
| `LIM-07` | – | – | – | – | ✅ | Externe Datei-Race — Wenn ein **externer** Prozess eine Inbox-Datei mid-pipel... |

### FTAG — Folder-Tags & Album-Propagation (33 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `FTAG-01` | – | – | – | – | ✅ | `_extract_folder_tags` extrahiert Ordner-Parts + kombinierten Tag |
| `FTAG-02` | – | – | – | – | ✅ | `_extract_folder_tags` bei flachem Inbox → leere Liste |
| `FTAG-03` | – | – | – | – | ✅ | `_extract_folder_tags` bei einzelner Ordner-Ebene |
| `FTAG-04` | – | – | – | – | ✅ | `_handle_duplicate` return annotation ist `list[str]` |
| `FTAG-05` | – | – | – | – | ✅ | `_get_folder_album_names` path-basiert (Inbox-Subfolder) |
| `FTAG-06` | – | – | – | – | ✅ | `_get_folder_album_names` IA-02 Fallback (/reprocess/) — Bugfix `..`-Pfad |
| `FTAG-07` | – | – | – | – | ✅ | `_get_folder_album_names` bei flachem Inbox → None |
| `FTAG-08` | – | – | – | – | ✅ | `_build_member` enthält `folder_tags` + `folder_album` Keys |
| `FTAG-09` | – | – | – | – | ✅ | folder_tags preserved bei "Kein Duplikat" |
| `FTAG-10` | – | – | – | – | ✅ | Kein folder_tags → clean skip_result |
| `FTAG-11` | – | – | – | – | ✅ | folder_tags Merge Donor 1 |
| `FTAG-12` | – | – | – | – | ✅ | folder_tags Merge dedupliziert (z.B. "Mallorca") |
| `FTAG-13` | – | – | – | – | ✅ | folder_tags Merge fügt neue Tags hinzu |
| `FTAG-14` | – | – | – | – | ✅ | folder_tags Merge zurück in IA-02 persistiert |
| `FTAG-15` | – | – | – | – | ✅ | ENTFERNT (quality_swap entfernt in v2.30.0) |
| `FTAG-16` | – | – | – | – | ✅ | Template: folder_album Badge + `.match-folder-album` |
| `FTAG-17` | – | – | – | – | ✅ | CSS: `.match-folder-album` Klasse vorhanden |
| `FTAG-18` | – | – | – | – | ✅ | `de.json`: `folder_album_title` Übersetzung |
| `FTAG-19` | – | – | – | – | ✅ | `en.json`: `folder_album_title` Übersetzung |
| `FTAG-20` | – | – | – | – | ✅ | E2E Keep: Keywords vom Original-Donor gemerged (echte Dateien) |
| `FTAG-21` | – | – | – | – | ✅ | E2E Keep: folder_tags erhalten wenn Kept-Job sie schon hat |
| `FTAG-22` | – | – | – | – | ✅ | E2E Keep: folder_tags überlebt IA-02 skip overwrite |
| `FTAG-23` | – | – | – | – | ✅ | E2E Keep: skip overwrite folder_tags korrekt |
| `FTAG-24` | – | – | – | – | ✅ | E2E Keep: IA-08 Album aus IA-02 Fallback (Datei in /reprocess/) |
| `FTAG-25` | – | – | – | – | ✅ | E2E Keep: Album = korrekt kombinierter Tag |
| `FTAG-26` | – | – | – | – | ✅ | E2E "Kein Duplikat": folder_tags ins skip_result kopiert |
| `FTAG-27` | – | – | – | – | ✅ | E2E "Kein Duplikat": prepare_job_for_reprocess status=queued |
| `FTAG-28` | – | – | – | – | ✅ | E2E "Kein Duplikat": IA-02 injected mit folder_tags |
| `FTAG-29` | – | – | – | – | ✅ | E2E "Kein Duplikat": IA-01 beibehalten |
| `FTAG-30` | – | – | – | – | ✅ | E2E "Kein Duplikat": IA-08 Album aus IA-02 Fallback |
| `FTAG-31` | – | – | – | – | ✅ | E2E _build_member: folder_tags Key existiert |
| `FTAG-32` | – | – | – | – | ✅ | E2E _build_member: folder_album Key existiert |
| `FTAG-33` | – | – | – | – | ✅ | E2E _build_member: folder_album ist String |
| `FTAG-34` | – | – | – | – | – | Keep (done): Donor-Alben aus Immich abgefragt |
| `FTAG-35` | – | – | – | – | – | Keep (done): Donor-Alben auf Kept-Asset zugewiesen |
| `FTAG-36` | – | – | – | – | – | Keep (done): neue Tags auf Kept-Asset geschrieben |
| `FTAG-37` | – | – | – | – | – | Keep (done): Description übernommen |
| `FTAG-38` | – | – | – | – | – | Keep (reprocess): own_album in IA-02 |
| `FTAG-39` | – | – | – | – | – | Keep (reprocess): donor_albums in IA-02 |
| `FTAG-40` | – | – | – | – | – | Keep (reprocess): _get_folder_album_names own+donor |
| `FTAG-41` | – | – | – | – | – | Keep (reprocess): own_album überlebt reprocess |
| `FTAG-42` | – | – | – | – | – | Donor ohne Immich: Album aus folder_tags[-1] |
| `FTAG-43` | – | – | – | – | – | Donor ohne folder_tags: Album aus IA-08 |
| `FTAG-44` | – | – | – | – | – | Donor ohne alles: Album aus Inbox-Pfad |
| `FTAG-45` | – | – | – | – | – | Album-Namen in keywords_written |
| `FTAG-46` | – | – | – | – | – | Album-Wörter einzeln in folder_tags |
| `FTAG-47` | – | – | – | – | – | Batch-Clean: gleiche Merge-Logik |
| `FTAG-48` | – | – | – | – | – | Batch-Clean (done): API-Anwendung |
| `FTAG-49` | – | – | – | – | – | Mehrere Donors: Alben gesammelt |
| `FTAG-50` | – | – | – | – | – | Mehrere Donors: Keywords Union |

### MATRIX — Sektion 14 Test-Matrix (Coverage-Karte) (72 Tests)

| Test-ID | vor 2026-04-02 | 2026-04-02 | 2026-04-07 | 2026-04-08 | 2026-04-13 | Beschreibung |
|---|---|---|---|---|---|---|
| `D1` | – | – | – | – | ✅ | "Behalten" im Review: kept_job läuft volle Pipeline neu (keep IA-01) Immich |
| `D2` | – | – | – | – | ✅ | "Behalten" im Review, File-Storage File-Storage |
| `D3` | – | – | – | – | ✅ | "Kein Duplikat": IA-02 wird auf skipped injiziert, IA-01 behalten Immich |
| `D4` | – | – | – | – | ✅ | "Kein Duplikat", File-Storage File-Storage |
| `D5` | – | – | – | – | ✅ | "Kein Duplikat" wenn Datei im library/duplicates/ verschwunden ist – |
| `M1` | – | – | – | – | ✅ | In-place reprocess ohne Datei-Move (z.B. nach EXIF-Wipe in target_path) ⚠️ **... |
| `N1.1` | – | – | – | – | ✅ | JPG (Kamera, voll) ✓ |
| `N1.2` | – | – | – | – | ✅ | HEIC (iPhone) ✓ |
| `N1.3` | – | – | – | – | ✅ | HEIC (iPhone) ✓ |
| `N1.4` | – | – | – | – | ✅ | PNG (Screenshot) – |
| `N1.5` | – | – | – | – | ✅ | GIF – |
| `N1.6` | – | – | – | – | ✅ | DNG/RAW (Kamera) ✓ |
| `N1.7` | – | – | – | – | ✅ | TIFF ✓ |
| `N1.8` | – | – | – | – | ✅ | WebP – |
| `N1.9` | – | – | – | – | ✅ | MP4 (Kamera-Video) ✓ |
| `N1.10` | – | – | – | – | ✅ | MOV (iPhone-Video) ✓ |
| `N1.11` | – | – | – | – | ✅ | MOV iPhone Live-Photo ✓ |
| `N1.12` | – | – | – | – | ✅ | JPG ohne EXIF (Messenger-Bild) – |
| `N1.13` | – | – | – | – | ✅ | UUID-Filename (WhatsApp `[0-9a-f]{8}-...jpg`) – |
| `N1.14` | – | – | – | – | ✅ | JPG mit EXIF aber ohne GPS ✓ |
| `N1.15` | – | – | – | – | ✅ | Korrupte Datei (z.B. 0-Byte) – |
| `N2.1` | – | – | – | – | ✅ | JPG ✓ |
| `N2.2` | – | – | – | – | ✅ | HEIC ✓ |
| `N2.3` | – | – | – | – | ✅ | MP4 ✓ |
| `N2.4` | – | – | – | – | ✅ | JPG ohne EXIF – |
| `N3.1` | – | – | – | – | ✅ | JPG direct |
| `N3.2` | – | – | – | – | ✅ | JPG sidecar |
| `N3.3` | – | – | – | – | ✅ | HEIC direct |
| `N3.4` | – | – | – | – | ✅ | HEIC sidecar |
| `N3.5` | – | – | – | – | ✅ | MOV (Video) direct |
| `N4.1` | – | – | – | – | ✅ | `ki_analyse` IA-05 skipped, Klassifikation rein über statische Sorting Rules ... |
| `N4.2` | – | – | – | – | ✅ | `geocoding` IA-03 skipped, keine Geo-Tags, kein Geo-Album |
| `N4.3` | – | – | – | – | ✅ | `duplikat_erkennung` IA-02 läuft nur als Hash-Check ohne pHash, alles passiert |
| `N4.4` | – | – | – | – | ✅ | `ocr` IA-06 skipped |
| `N4.5` | – | – | – | – | ✅ | `ordner_tags` (per Inbox) IA-08 erstellt kein Album aus Inbox-Subfolder-Pfad |
| `N4.6` | – | – | – | – | ✅ | `smtp` IA-09 skipped (kein Mail-Versand), `sent=false` im step_result |
| `N4.7` | – | – | – | – | ✅ | `immich` (komplett aus) `use_immich=True`-Jobs scheitern oder fallen auf File... |
| `N4.8` | – | – | – | – | ✅ | beide AI-Backends aus (`ki_analyse` + `ki_analyse_2`) IA-05 skipped, kein Aut... |
| `N5.1` | – | – | ✅ | ✅ | ✅ | IA-02 findet exact-Hash-Duplikat eines schon verarbeiteten Jobs status=`dupli... |
| `N5.2` | – | – | ✅ | ✅ | ✅ | IA-02 findet pHash-similar (nicht exact) status=`duplicate`, match_type=`simi... |
| `N5.3` | – | – | ✅ | ✅ | ✅ | IA-02 Video-pHash post-IA-04 status=`duplicate`, IA-02 nachträglich überschri... |
| `N5.4` | – | – | – | – | ✅ | KI gibt Kategorie `unknown` zurück (oder keine valide) status=`review`, file ... |
| `N5.5` | – | – | – | – | ✅ | Sorting-Rule mit `target_category="skip"` matched status=`skipped`, **keine**... |
| `N5.6` | – | – | – | – | ✅ | `dry_run=True` auf der Inbox status=`done` (oder `dry_run`), KEIN Move, KEIN ... |
| `N5.7` | – | – | – | – | ✅ | IA-05 mit AI Auto-Pause (`AIConnectionError`, beide Backends down) `pipeline.... |
| `N5.8` | – | – | – | – | ✅ | IA-03 mit Geocoding Auto-Pause (`GeocodingConnectionError`) wie N5.7 für Geoc... |
| `N6.1` | – | – | – | – | ✅ | 1 Job `processing` nach Restart: status='queued' + retry_count++, Pipeline lä... |
| `N6.2` | – | – | – | – | ✅ | Job mit retry_count=3 abandoned: status='error', Meldung "Max retries (3) exc... |
| `N6.3` | – | – | – | – | ✅ | mehrere Jobs `processing` parallel alle requeued sequenziell |
| `N7.1` | – | – | ✅ | ✅ | ✅ | 10 Dateien gleichzeitig im Inbox alle 10 verarbeitet, kein Duplicate-Job, kei... |
| `N7.2` | – | – | ✅ | ✅ | ✅ | derselbe Job von 5 Pipeline-Aufrufern parallel atomic claim: 1 läuft, 4 retur... |
| `N7.3` | – | – | ✅ | ✅ | ✅ | retry_job + 5 parallele run_pipeline auf demselben Job nur retry's pipeline l... |
| `N7.4` | – | – | ✅ | ✅ | ✅ | 5 parallele retry_job auf demselben Job exakt 1 succeeded, 4 returnen False |
| `N7.5` | – | – | ✅ | ✅ | ✅ | run_pipeline auf done/processing-Job (Idempotenz-Check) no-op |
| `N7.6` | – | – | – | – | ✅ | Bulk-Retry-All triggert 30+ parallele Pipeline-Tasks DB-Pool reicht (20/40), ... |
| `R1` | – | – | – | ✅ | ✅ | Immich sidecar |
| `R2` | – | – | – | ✅ | ✅ | Immich direct |
| `R3` | – | – | – | ✅ | ✅ | File-Storage direct |
| `R4` | – | – | – | ✅ | ✅ | File-Storage sidecar |
| `R5` | – | – | – |  ✅  | ✅ | Immich direct |
| `R6` | – | – | – |  ✅  | ✅ | Immich sidecar |
| `R7` | – | – | – | – | ✅ | Immich direct |
| `R8` | – | – | – | – | ✅ | Immich sidecar |
| `R9` | – | – | – | – | ✅ | Immich direct |
| `R10` | – | – | – |  ✅  | ✅ | File-Storage direct |
| `R11` | – | – | – |  ✅  | ✅ | File-Storage sidecar |
| `R12` | – | – | – | – | ✅ | Immich direct |
| `R13` | – | – | – | – | ✅ | Immich sidecar |
| `R14` | – | – | – | – | ✅ | Immich direct |
| `R15` | – | – | – | ✅ | ✅ | – – |
| `R16` | – | – | ✅ | ✅ | ✅ | Immich direct |
| `RA1` | – | – | – | – | ✅ | Bulk-Retry mehrerer Error-Jobs ohne sofortigen Pipeline-Run (background worke... |

## Notizen pro Lauf

Kurze, **anonymisierte** Bemerkungen — keine personenbezogenen Daten.

### 2026-04-13c (v2.29.1)

- Vollständiger Testlauf aller 8 Suites.
- Neue Suites: test_v29_stress.py (41/41), test_no_file_loss.py (19/20).
- NFL P3a (10 parallel in <30s) fehlgeschlagen (34s) — Performance, kein
  Datei-Verlust. **Kein einziger Datei-Verlust in allen Tests.**
- Bugfix v2.29.1: prepare_job_for_reprocess setzte Status auf queued
  ohne Datei → ewige Retry-Schleife.

### 2026-04-13b (v2.28.84)

- Vollständiger Testlauf aller 6 Suites nach grossem Refactoring
  (file_operations.py, thumbnail_utils.py, 8 Redundanzen eliminiert).
- 255/256 bestanden, 1 BLOCK (HEIC-Testdatei fehlt).
- `test_ftag_immich.py` erstmals dokumentiert: 20/20 grün (E2E Immich).
- `test_keep_flow.py` erstmals dokumentiert: 15/15 grün.
- Bugfixes v2.28.74-83: folder_tags Propagation, force=True bei Keep-Delete,
  shared delete_asset(), Pfad-Müll-Filter (..), Immich-Album-Fallback.

### 2026-04-13 (v2.28.73)

- Vollständiger Testlauf aller 4 Suites auf v2.28.73 (`16f4b8c`).
- `test_ai_backends.py` erstmals dokumentiert: 13/13 grün.
- 220/221 bestanden, 1 BLOCK (HEIC-Testdatei fehlt — preexisting).
- **Neue FTAG-Sektion** (27 Tests): folder_tags & Album-Propagation
  durch IA-02 → IA-08 → Duplikat-UI getestet, inkl. Keep-Flow
  (Merge, Skip-Overwrite, Re-Pipeline, "Kein Duplikat").
- **Bugfix** in `_get_folder_album_names`: `..`-Pfade (z.B. `/reprocess/`
  relativ zu `/inbox/`) wurden fälschlich als Album-Name interpretiert
  statt auf IA-02 folder_tags zurückzufallen. Fix: `not rel.startswith("..")`.

### 2026-04-08

- Retry-File-Lifecycle-Tests neu in v2.28.28 hinzugekommen, in v2.28.29
 um File-Storage-Variante und Error-Retry erweitert. 46 Asserts grün.
- `test_duplicate_fix.py` Tests 7+8 mussten auf reale 0-Byte-Dummy-Files
 umgestellt werden, weil der neue `reset_job_for_retry`-Vertrag eine
 existierende Quelldatei voraussetzt.
- 1 BLOCK in `test_testplan_final.py` Sektion 1+6: erwartete HEIC-
 Testdatei fehlt im Container (preexisting, nicht regression).

### 2026-04-07

- Race-Condition-Suite (Tests 5–8) hinzugekommen.
- Vollständiger Lauf 92/92 grün.

### 2026-04-02

- Letzter vollständiger Regressionslauf vor v2.28.x.
- 9 Tests als "nicht testbar" markiert (fehlende Infrastruktur, z.B.
 Photon, CR2/NEF/ARW Kamera-RAW-Formate).

## Wie aktualisiere ich diese Datei?

Nach jedem vollständigen Test-Lauf:

1. Neue Spalte mit aktuellem Datum (`YYYY-MM-DD`) am rechten Ende
 einfügen.
2. Release- und Commit-Hash in den ersten zwei Zeilen eintragen.
3. Pro Test-Skript / Subtest die Zelle füllen mit `X/Y ✅`, `X/Y ⚠️`,
 `X/Y ❌` oder `–` (nicht gelaufen / nicht relevant).
4. Eine kurze, **anonymisierte** Notiz unter "Notizen pro Lauf" schreiben.
 **Keine** Mail-Adressen, Hostnames, IPs, Pfade mit Usernamen,
 GPS-Koordinaten, Immich-Asset-IDs oder echte Datei-Inhalte. Falls
 ein Test-Job für die Forensik wichtig ist, nur den anonymisierten
 Debug-Key (`MA-2026-XXXXX`) erwähnen — die DB-Details bleiben in
 den lokalen Logs.
5. Alte Spalten dürfen archiviert werden, sobald die Datei zu breit
 wird (z.B. nur die letzten 6 Releases im Detail, ältere als
 Übersichts-Zeile).

## Historische Notizen (anonymisiert)

### Pre-Fix-Forensik Race-Conditions (RACE-05 bis RACE-08)

Vor dem Fix-Release der `run_pipeline` / `retry_job` Atomic-Claims
zeigte die Live-DB:

- ~30 Jobs/Tag mit doppelten `INFO pipeline` Log-Einträgen pro Job-Key
  (verschiedene Tag-Counts für dieselbe ID, was nur möglich ist wenn
  zwei Pipeline-Runs parallel auf dem Job liefen).
- ~120 Jobs mit `error_message LIKE '%already exists%'` aber Status
  `done` — ExifTool-Sidecar-Race zwischen zwei IA-07-Runs.
- ~71 Jobs mit `File disappeared` Fehler — IA-08 in einem Run hat die
  Datei hochgeladen + gelöscht, der parallele Run fand sie nicht mehr.

`run_pipeline` wurde von 5 verschiedenen Stellen aufgerufen ohne
Schutz gegen parallele Eintritte (Worker, Immich-Poller, Startup-
Resume, retry_job, Duplikate-Router). Die Atomic-Claims `UPDATE jobs
SET status='processing' WHERE id=? AND status='queued'` und der
zwei-Phasen-Lock in retry_job (`error → processing → queued`) haben
das Symptom eliminiert. Beweis siehe RACE-05 bis RACE-08.

Konkrete Job-IDs aus der Live-Forensik liegen ausschliesslich in den
internen Logs / Commit-Messages — nicht hier, weil sie als
identifizierbare Personen-Bezogene Daten gewertet werden müssen.

## Historische Bug-Liste (anonymisiert)

Dokumentation früherer Bugs, die im Testplan keinen festen Test-Slot
mehr haben (weil bereits verifiziert und zu LIM-XX abgewandert oder
durch generische Tests in den IA-XX/SEC-XX/EDGE-XX abgedeckt). Hier
nur als historische Referenz, ohne Versions-Stempel.

| Symptom | Root-Cause | Fix |
|---|---|---|
| GPS lon=0 / lat=0 ignoriert | `bool(0)` ist False → Greenwich/Äquator GPS verworfen | `is not None`-Check |
| GPS lat=999, lon=999 akzeptiert | keine Range-Validierung | Range-Check -90..90 / -180..180 |
| Format-Mismatch (JPG mit `.png`-Endung) | ExifTool Write crasht | Mismatch-Erkennung vor Write |
| Settings partieller POST | wipte alle Module + Config | `_form_token` Guard |
| Settings XSS in Textfeldern | ungefilterte Eingabe in Config gespeichert | `html.escape` Sanitisierung |
| `_handle_duplicate` Cleanup-Fehler | crashte Pipeline statt Status zu setzen | try/except + Fallback |
| Pipeline Race auf gleichem Job | 5 Aufrufer ohne Schutz | atomic claim `UPDATE WHERE status='queued'` |
| `retry_job` TOCTOU zwischen Commits | parallele Worker fanden alten step_result | transienter Lock-State `error → processing → queued` |
| Bulk-Retry erschöpfte DB-Pool | 30+ parallele Tasks > pool=15 | pool_size=20, max_overflow=40, sequenziell statt parallel |
| Retry löscht Datei via IA-10 | `immich_asset_id` als Lösch-Trigger statt `source_label='Immich'` | spezifische Bedingung in IA-10 |
| Retry verliert Datei im File-Storage | IA-08 cached → kein Re-Move zurück nach `/library/` | IA-08 step_result wird beim Retry gedroppt wenn target lokal war |
| Retry-Endlosschleife bei missing file | `prepare_job_for_reprocess` False ignored | Rückgabewert geprüft, Retry abgebrochen mit klarer Meldung |


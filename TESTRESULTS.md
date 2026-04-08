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

## Lauf-Historie

Format: `bestanden/total ✅` oder `bestanden/total ⚠️` (mit Lücken) oder
`bestanden/total ❌` (Fehler) oder `–` (nicht gelaufen).

| Test-Skript / Subtest | 2026-04-02 | 2026-04-07 | 2026-04-08 |
|---|---|---|---|
| **Release** | v2.17.1 | v2.28.3 | v2.28.29 |
| **Commit** | – | – | `02d36a7` |
| `test_duplicate_fix.py` (gesamt) | – | 26/26 ✅ | 26/26 ✅ |
| ↳ Test 1 — `_handle_duplicate` cleanup-error | – | ✅ | ✅ |
| ↳ Test 2 — pipeline fallback erkennt duplicate | – | ✅ | ✅ |
| ↳ Test 3 — normaler Duplikat-Flow | – | ✅ | ✅ |
| ↳ Test 4 — Nicht-Duplikat läuft bis IA-08 | – | ✅ | ✅ |
| ↳ Test 5 — atomic claim blockiert 10 parallele `run_pipeline` | – | ✅ | ✅ |
| ↳ Test 6 — `run_pipeline` auf done-Job no-op | – | ✅ | ✅ |
| ↳ Test 7 — `retry_job` parallel zu 5× `run_pipeline` | – | ✅ | ✅ |
| ↳ Test 8 — 5 parallele `retry_job` | – | ✅ | ✅ |
| `test_retry_file_lifecycle.py` (gesamt) | – | – | 46/46 ✅ |
| ↳ R1 — sidecar mode, Immich, Inbox, Warnung | – | – | ✅ |
| ↳ R2 — direct mode, Immich, Inbox, Warnung | – | – | ✅ |
| ↳ R3 — direct mode, File-Storage, Inbox, Warnung | – | – | ✅ |
| ↳ R4 — sidecar mode, File-Storage, Inbox, Warnung | – | – | ✅ |
| ↳ R5 — direct mode, Immich, Inbox, Error (IA-08) | – | – | ✅ |
| ↳ R15 — Datei vor Retry verschwunden (negativ) | – | – | ✅ |
| `test_testplan_final.py` (gesamt) | 296/305 (9 nicht testbar) | 66/66 ✅ | 59/60 (1 BLOCK) |
| ↳ Sektion 1+6 — Pipeline-Steps + Dateiformate | – | ✅ | ✅ (1 BLOCK: HEIC-Testdatei fehlt im Container) |
| ↳ Sektion 2 — Pipeline-Fehlerbehandlung | – | ✅ | ✅ |
| ↳ Sektion 3 — Web-Interface Erreichbarkeit | – | ✅ | ✅ |
| ↳ Sektion 4 — Filewatcher-Stabilität | – | ✅ | ✅ |
| ↳ Sektion 7 — Edge Cases | – | ✅ | ✅ |
| ↳ Sektion 8 — Security | – | ✅ | ✅ |
| ↳ Sektion 9 — Performance | – | ✅ | ✅ |
| ↳ Sektion 12 — Stress / Concurrent (10 parallel) | – | ✅ | ✅ |
| `test_ai_backends.py` (gesamt) | – | – | – |

## Notizen pro Lauf

Kurze, **anonymisierte** Bemerkungen — keine personenbezogenen Daten.

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

## Verifikations-Historie pro Test-ID

> Tabelle aller im Testplan definierten Test-IDs mit dem Stand der
> historischen Verifikation. Eine ID gilt als 'verifiziert' wenn sie
> in irgendeinem früheren Release-Lauf manuell oder automatisiert
> bestätigt wurde — die konkrete Version steht in der zweiten Spalte.
> Vollkommen anonymisiert: keine echten Datei- oder Job-Referenzen.
>
> **Wann eine Zelle leer ist:** der Test ist im Plan vorhanden, aber
> es gibt keinen dokumentierten Verifikations-Lauf. Beim nächsten
> kompletten Regressions-Lauf hier nachtragen.


### EDGE-* (13 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `EDGE-01` | pre-v2.4 | Leere Datei (0 Bytes) → Filewatcher überspringt als "unstable" |
| `EDGE-02` | pre-v2.4 | Sehr grosse Datei (>100 MB) → Verarbeitung funktioniert (97MB DNG, 304MB MP4) |
| `EDGE-03` | pre-v2.4 | Dateiname mit Sonderzeichen/Umlauten → korrekt verarbeitet |
| `EDGE-04` | pre-v2.4 | Dateiname mit Leerzeichen und Klammern → korrekt verarbeitet (` (2).JPG`) |
| `EDGE-05` | pre-v2.4 | Gleichzeitige Verarbeitung mehrerer Dateien → kein Datenverlust (Batch 4+ Dat... |
| `EDGE-06` | pre-v2.4 | Verschlüsselte Config-Werte → korrekt entschlüsselt |
| `EDGE-07` | pre-v2.4 | Ungültiges JSON in Config-Wert → kein Crash, Rohwert zurückgegeben (getestet:... |
| `EDGE-08` | pre-v2.4 | Korruptes Video (moov atom fehlt) → Fehler gefangen, E-Mail gesendet, kein Crash |
| `EDGE-09` | pre-v2.4 | Sehr kleine Bilder (<16px) → KI-Analyse übersprungen |
| `EDGE-10` | pre-v2.4 | Unscharfes Foto → KI erkennt `quality: blurry`, Tag geschrieben |
| `EDGE-11` | pre-v2.4 | Namenskollision → Counter _1, _2 angehängt (screenshot_test → screenshot_test_1) |
| `EDGE-12` | pre-v2.4 | Dateien in Unterordnern → rekursiv erkannt und verarbeitet |
| `EDGE-13` | pre-v2.4 | UUID-Dateiname (WhatsApp-Format) ohne EXIF + keine KI → Status "review" |

### EX-* (43 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `EX-01` | pre-v2.4 | JPG mit.png Extension → IA-01 erkennt `file_type=JPEG`, IA-07 überspringt mit... |
| `EX-02` | pre-v2.4 | PNG mit.jpg Extension → IA-07 überspringt mit "format mismatch" |
| `EX-03` | pre-v2.4 | MP4 als.mov umbenannt → Pipeline verarbeitet korrekt (ffprobe erkennt Format) |
| `EX-04` | pre-v2.4 | Zufällige Binärdaten als.jpg → IA-01 Fehler "konnte Datei nicht lesen", kein ... |
| `EX-05` | pre-v2.4 | 200+ Zeichen Dateiname → korrekt verarbeitet |
| `EX-06` | pre-v2.4 | Emoji im Dateinamen (🏔️_Berge_🌅.jpg) → korrekt verarbeitet, Immich-Upload OK |
| `EX-07` | pre-v2.4 | Chinesisch/Japanisch (测试照片_テスト.jpg) → korrekt verarbeitet, Immich-Upload OK |
| `EX-08` | pre-v2.4 | Nur Punkte (`...jpg`) → korrekt ignoriert (kein Extension-Match) |
| `EX-09` | pre-v2.4 | Leerzeichen-Name (`.jpg`) → korrekt verarbeitet |
| `EX-10` | pre-v2.4 | Doppelte Extension (`photo.jpg.jpg`) → korrekt verarbeitet |
| `EX-11` | pre-v2.4 | Uppercase Extension (`PHOTO.JPEG`) → `.lower` normalisiert korrekt |
| `EX-12` | pre-v2.4 | 1x1 Pixel Bild → pHash berechnet, korrekt verarbeitet |
| `EX-13` | pre-v2.4 | 10000x100 Panorama → korrekt verarbeitet |
| `EX-14` | pre-v2.4 | 16x16 Pixel (an KI-Schwelle) → korrekt verarbeitet |
| `EX-15` | pre-v2.4 | 15x15 Pixel (unter KI-Schwelle) → KI übersprungen "Bild zu klein" |
| `EX-16` | pre-v2.4 | Solid Black / Solid White → pHash `0000...` / `8000...`, korrekt verarbeitet |
| `EX-17` | pre-v2.4 | Zukunftsdatum (2030-01-01) → Datum korrekt gelesen, Sortierung in 2030/ |
| `EX-18` | pre-v2.4 | Sehr altes Datum (1900-01-01) → korrekt verarbeitet |
| `EX-19` | pre-v2.4 | GPS Longitude=0 (Greenwich-Meridian) → Geocoding korrekt "Vereinigtes Königre... |
| `EX-20` | pre-v2.4 | GPS Latitude=0 (Äquator) → gps=true, Geocoding ausgeführt |
| `EX-21` | pre-v2.4 | Ungültige GPS (999,999) → "skipped, invalid GPS coordinates" (Validierung in ... |
| `EX-22` | pre-v2.4 | GPS Null Island (0,0) → Geocoding wird ausgeführt |
| `EX-23` | pre-v2.4 | 10KB EXIF Description → ExifTool verarbeitet ohne Probleme |
| `EX-24` | pre-v2.4 | XSS in EXIF Keywords (`<script>alert(1)</script>`) → wird nicht in KI-Tags üb... |
| `EX-25` | pre-v2.4 | `@eaDir` Verzeichnis → korrekt ignoriert (`_SKIP_DIRS` in filewatcher.py) |
| `EX-26` | pre-v2.4 | `.DS_Store` Datei → ignoriert (keine unterstützte Extension) |
| `EX-27` | pre-v2.4 | `Thumbs.db` Datei → ignoriert (keine unterstützte Extension) |
| `EX-28` | pre-v2.4 | Versteckte Datei (`.hidden_photo.jpg`) → wird verarbeitet (korrekt, versteckt... |
| `EX-29` | pre-v2.4 | 10 Dateien gleichzeitig → alle korrekt verarbeitet, sequentielle Abarbeitung |
| `EX-30` | pre-v2.4 | Gleiche Datei 5x mit verschiedenen Namen → 1 done + 4 SHA256-Duplikate |
| `EX-31` | pre-v2.4 | Datei vor Filewatcher-Pickup gelöscht → kein Crash, kein Job erstellt |
| `EX-32` | pre-v2.4 | 15 Dateien in Queue auf langsamem System → alle verarbeitet, kein OOM |
| `EX-33` | pre-v2.4 | **v2.28.2/v2.28.3:** Derselbe `job_id` wird nicht von zwei Pipeline-Instanzen... |
| `EX-34` | pre-v2.4 | 97MB DNG → korrekt verarbeitet, Memory ~260MB |
| `EX-35` | pre-v2.4 | 273MB MP4 Video → korrekt verarbeitet, Memory unter 260MB |
| `EX-36` | pre-v2.4 | 8MB PNG → korrekt verarbeitet |
| `EX-37` | pre-v2.4 | Ungültiger Job-Key für Retry → `{"status":"error","message":"Job nicht gefund... |
| `EX-38` | pre-v2.4 | Nicht-existenter Job löschen → Redirect ohne Fehlerseite |
| `EX-39` | pre-v2.4 | Dashboard mit 0 Jobs → korrekte Antwort, alle Werte 0 |
| `EX-40` | pre-v2.4 | Partieller POST ohne `_form_token` → abgelehnt mit "invalid_form" Fehler |
| `EX-41` | pre-v2.4 | Vollständiger POST mit `_form_token` → akzeptiert |
| `EX-42` | pre-v2.4 | XSS-Payload in Textfeldern → HTML-escaped gespeichert (`&lt;script&gt;`) |
| `EX-43` | pre-v2.4 | Module-Checkboxen nur aktualisiert wenn `_form_token` vorhanden |

### FMT-* (10 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `FMT-01` | pre-v2.4 | JPG/JPEG — Verarbeitung + KI + Tags schreiben |
| `FMT-02` | pre-v2.4 | PNG — Verarbeitung + KI + Tags schreiben (test_landscape.png → internet_image... |
| `FMT-03` | pre-v2.4 | HEIC — Konvertierung + KI + Tags schreiben |
| `FMT-04` | pre-v2.4 | WebP — Verarbeitung + KI (test_image.webp → internet_image/sourceless) |
| `FMT-05` | pre-v2.4 | GIF — KI direkt analysiert (convert nicht verfügbar, aber Pipeline läuft weiter) |
| `FMT-06` | pre-v2.4 | TIFF — Verarbeitung + KI + Tags schreiben (test_image.tiff → internet_image/s... |
| `FMT-07` | pre-v2.4 | DNG — PreviewImage für KI + pHash, Tags schreiben, grosse Dateien (25–97MB) |
| `FMT-08` | pre-v2.4 | MP4 — Video erkannt, ffprobe-Metadaten, Thumbnails, KI, Tags schreiben, korre... |
| `FMT-09` | pre-v2.4 | MOV — Video erkannt, ffprobe, 5 Thumbnails, KI, Tags, korrekt sortiert |
| `FMT-10` | pre-v2.4 | Nicht unterstütztes Format (.txt) → vom Filewatcher ignoriert (SUPPORTED_EXTE... |

### FW-* (15 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `FW-01` | pre-v2.4 | Halbkopierte Datei (Kopiervorgang läuft) → wird nicht sofort verarbeitet |
| `FW-02` | pre-v2.4 | Nach 2s Wartezeit: Dateigrösse wird erneut geprüft |
| `FW-03` | pre-v2.4 | Dateigrösse stabil → Verarbeitung startet |
| `FW-04` | pre-v2.4 | Dateigrösse geändert → erneute Wartezeit |
| `FW-05` | pre-v2.4 | Leere Datei (0 Bytes) → wird als "unstable" übersprungen (current_size > 0 Ch... |
| `FW-06` | v2.28.2 | `_is_file_stable` ist nach Entfernung der IA-07/IA-08-Workarounds der **einzi... |
| `FW-07` | pre-v2.4 | Nicht unterstütztes Format (.txt) → wird vom Filewatcher ignoriert |
| `FW-08` | pre-v2.4 | Bereits verarbeitete Datei erneut in Inbox → wird erneut verarbeitet, IA-02 e... |
| `FW-09` | pre-v2.4 | Datei liegt nach Verarbeitung noch in Inbox (Move fehlgeschlagen) → wird erne... |
| `FW-10` | pre-v2.4 | Dry-Run-Jobs werden in done_hashes berücksichtigt (Datei bleibt absichtlich i... |
| `FW-11` | pre-v2.4 | Immich-Assets werden in done_hashes berücksichtigt |
| `FW-12` | pre-v2.4 | Gelöschtes Ziel → Datei wird erneut verarbeitet (Target-Existenz geprüft) |
| `FW-13` | pre-v2.4 | Keine Datei bleibt dauerhaft unbeachtet in der Inbox liegen (ausser Dry-Run) |
| `FW-14` | pre-v2.4 | Docker-Logging: Alle Filewatcher-Aktionen in stdout sichtbar |
| `FW-15` | pre-v2.4 | Unterordner in Inbox → Dateien werden rekursiv gefunden und verarbeitet |

### IA01-* (26 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA01-01` | pre-v2.4 | JPG mit vollständigen EXIF-Daten (Kamera, Datum, GPS) → alle Felder korrekt e... |
| `IA01-02` | pre-v2.4 | HEIC mit EXIF → korrekt gelesen |
| `IA01-03` | pre-v2.4 | Datei ohne EXIF (z.B. Messenger-Bild) → `has_exif: false` |
| `IA01-04` | pre-v2.4 | Video (MP4/MOV) → Mime-Type und Dateityp korrekt erkannt |
| `IA01-05` | pre-v2.4 | Beschädigte Datei → Fehler wird gefangen, Pipeline bricht nicht ab |
| `IA01-06` | pre-v2.4 | file_size wird korrekt gespeichert |
| `IA01-07` | pre-v2.4 | Datum-Fallback auf FileModifyDate wenn DateTimeOriginal fehlt |
| `IA01-08` | pre-v2.4 | Video: ffprobe extrahiert Datum (creation_time) korrekt |
| `IA01-09` | pre-v2.4 | Video: ffprobe extrahiert GPS-Koordinaten aus ISO 6709 String |
| `IA01-10` | pre-v2.4 | Video: ISO 6709 Parser verarbeitet verschiedene Formate korrekt (mit/ohne Höh... |
| `IA01-11` | pre-v2.4 | Video: GPS aus ISO 6709 wird als lat/lon in Metadaten gespeichert |
| `IA01-12` | pre-v2.4 | Video: Dauer (duration) wird als Rohwert und formatiert gespeichert (z.B. `12... |
| `IA01-13` | pre-v2.4 | Video: Auflösung (width x height) korrekt extrahiert |
| `IA01-14` | pre-v2.4 | Video: Megapixel aus Auflösung berechnet |
| `IA01-15` | pre-v2.4 | Video: Codec (z.B. h264, hevc) korrekt extrahiert |
| `IA01-16` | pre-v2.4 | Video: Framerate (z.B. 30, 60) korrekt extrahiert |
| `IA01-17` | pre-v2.4 | Video: Bitrate korrekt extrahiert |
| `IA01-18` | pre-v2.4 | Video: Rotation korrekt extrahiert (z.B. 0, 90, 180, 270) |
| `IA01-19` | pre-v2.4 | Video: ffprobe liefert unvollständige Daten → vorhandene Felder gespeichert, ... |
| `IA01-20` | pre-v2.4 | DNG (RAW): EXIF korrekt (Make, Model, Datum, GPS, Auflösung) |
| `IA01-21` | pre-v2.4 | DNG: Grosse Dateien (25MB–97MB) verarbeitet ohne Timeout |
| `IA01-22` | pre-v2.4 | PNG: file_type=PNG, mime=image/png korrekt |
| `IA01-23` | pre-v2.4 | WebP: file_type=WEBP, mime=image/webp korrekt |
| `IA01-24` | pre-v2.4 | GIF: file_type=GIF, mime=image/gif korrekt |
| `IA01-25` | pre-v2.4 | TIFF: file_type=TIFF, mime=image/tiff korrekt |
| `IA01-26` | pre-v2.4 | MOV: file_type=MOV, mime=video/quicktime, ffprobe-Metadaten korrekt |

### IA02-* (13 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA02-01` | pre-v2.4 | Exaktes Duplikat (gleiche Datei nochmal) → SHA256-Match, Status "duplicate" |
| `IA02-02` | pre-v2.4 | Ähnliches Bild (z.B. leicht beschnitten) → pHash-Match unter Schwellwert |
| `IA02-03` | pre-v2.4 | Unterschiedliches Bild → kein Match, `status: ok` |
| `IA02-04` | pre-v2.4 | RAW-Format (DNG/CR2) → pHash via ExifTool PreviewImage berechnet |
| `IA02-05` | pre-v2.4 | Modul deaktiviert → `status: skipped, reason: module disabled` |
| `IA02-06` | pre-v2.4 | Duplikat eines Immich-Assets → korrekt erkannt |
| `IA02-07` | pre-v2.4 | Orphaned Job (Original-Datei gelöscht) → Match wird übersprungen |
| `IA02-08` | pre-v2.4 | JPG+DNG Paar mit keep_both=true → beide unabhängig verarbeitet |
| `IA02-09` | pre-v2.4 | JPG+DNG Paar mit keep_both=false → zweite Datei als `raw_jpg_pair` Duplikat |
| `IA02-10` | pre-v2.4 | pHash-Threshold 3 → weniger False Positives als Threshold 5 |
| `IA02-11` | pre-v2.4 | Video: pHash aus Durchschnitt der IA-04 Frames berechnet (post-IA-04 Check) |
| `IA02-12` | pre-v2.4 | Video: Re-encoded Video (anderer Codec/Bitrate) → pHash-Match, als "similar" ... |
| `IA02-13` | pre-v2.4 | Video: Exakte Kopie eines Videos → SHA256-Match, als "exact" Duplikat erkannt |

### IA03-* (7 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA03-01` | pre-v2.4 | Bild mit GPS-Koordinaten → Land, Stadt, Stadtteil aufgelöst |
| `IA03-02` | pre-v2.4 | Bild ohne GPS → `status: skipped` |
| `IA03-03` | pre-v2.4 | Nominatim-Provider → korrekte Ergebnisse |
| `IA03-04` | pre-v2.4 | Modul deaktiviert → `status: skipped, reason: module disabled` |
| `IA03-05` | pre-v2.4 | Geocoding-Server nicht erreichbar → Fehler gefangen, Step übersprungen, Pipel... |
| `IA03-06` | pre-v2.4 | DJI-Drohne GPS → korrekt aufgelöst |
| `IA03-07` | pre-v2.4 | Video GPS (ffprobe ISO 6709) → korrekt geocodiert |

### IA04-* (10 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA04-01` | pre-v2.4 | JPG/PNG/WebP → keine Konvertierung, `converted: false` |
| `IA04-02` | pre-v2.4 | HEIC → temp JPEG erstellt, KI-Analyse erfolgreich |
| `IA04-03` | pre-v2.4 | DNG/CR2/NEF/ARW → PreviewImage extrahiert als temp JPEG |
| `IA04-04` | pre-v2.4 | GIF → Konvertierung versucht (convert nicht verfügbar), KI analysiert trotzde... |
| `IA04-05` | pre-v2.4 | TIFF → keine Konvertierung nötig, direkt analysierbar |
| `IA04-06` | pre-v2.4 | Konvertierung fehlgeschlagen → Fehler gefangen (korruptes Video, fehlender co... |
| `IA04-07` | pre-v2.4 | Video mit VIDEO_THUMBNAIL_ENABLED = True → mehrere Thumbnails extrahiert |
| `IA04-08` | pre-v2.4 | Video-Thumbnail: Dauer korrekt ermittelt, Frames gleichmässig verteilt |
| `IA04-09` | pre-v2.4 | Video-Thumbnail: ffmpeg nicht verfügbar / Fehler → Fehler gefangen, `converte... |
| `IA04-10` | pre-v2.4 | MOV Video → 5 Thumbnails extrahiert, KI-Analyse erfolgreich |

### IA05-* (16 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA05-01` | v2.8.0 | Persönliches Foto → `type: personliches_foto`, sinnvolle Tags |
| `IA05-02` | pre-v2.4 | Screenshot → `type: screenshot` (Statusleiste, Navigationsbar erkannt) |
| `IA05-03` | pre-v2.4 | Internet-Bild → `type: sourceless` (generierte PNG/WebP/TIFF, v2.8.0: kein in... |
| `IA05-04` | pre-v2.4 | KI-Backend nicht erreichbar → Fehler gefangen, Fallback-Werte gesetzt |
| `IA05-05` | pre-v2.4 | Modul deaktiviert → `status: skipped, reason: module disabled` |
| `IA05-06` | pre-v2.4 | Metadata-Kontext (EXIF, Geo, Dateigrösse) wird an KI übergeben |
| `IA05-07` | v2.8.0 | Kategorien aus DB werden im Prompt übergeben |
| `IA05-08` | v2.8.0 | Statische Regel-Vorklassifikation wird der KI als Kontext mitgegeben: Persönl... |
| `IA05-09` | v2.8.0 | KI gibt `source` (Herkunft) und `tags` (beschreibend) separat zurück |
| `IA05-10` | pre-v2.4 | DNG-Konvertierung für KI-Analyse funktioniert |
| `IA05-11` | pre-v2.4 | Video-Thumbnails (5 Frames) für KI-Analyse |
| `IA05-12` | pre-v2.4 | Sehr kleine Bilder (<16px) → übersprungen mit Meldung |
| `IA05-13` | pre-v2.4 | DJI-Drohnenfotos → korrekt als personal/Luftaufnahme erkannt |
| `IA05-14` | pre-v2.4 | Unscharfes Foto → `quality: blurry` |
| `IA05-15` | pre-v2.4 | NSFW-Erkennung: KI gibt `nsfw: true` für nicht-jugendfreie Inhalte zurück |
| `IA05-16` | pre-v2.4 | NSFW-Erkennung: `nsfw: false` für normale Bilder (Landschaft, Essen, etc.) |

### IA06-* (5 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA06-01` | pre-v2.4 | Screenshot mit Text → `has_text: true`, Text korrekt erkannt |
| `IA06-02` | pre-v2.4 | Foto ohne Text (Smart-Modus) → OCR übersprungen (`type=personal, OCR nicht nö... |
| `IA06-03` | pre-v2.4 | Smart-Modus: Screenshot → OCR ausgeführt |
| `IA06-04` | pre-v2.4 | Always-Modus → OCR wird immer ausgeführt (auch für normale Fotos) |
| `IA06-05` | pre-v2.4 | Modul deaktiviert → `status: skipped, reason: module disabled` |

### IA07-* (23 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA07-01` | pre-v2.4 | AI-Tags werden als Keywords geschrieben |
| `IA07-02` | v2.8.0 | AI-Source (Herkunft) wird als Keyword geschrieben |
| `IA07-03` | pre-v2.4 | Geocoding-Daten (Land, Stadt etc.) als Keywords |
| `IA07-04` | pre-v2.4 | Ordner-Tags: Einzelwörter + zusammengesetzter Tag (z.B. `Ferien/Mallorca 2025... |
| `IA07-05` | pre-v2.4 | Ordner-Tags: Einfacher Ordner → nur Ordnername als Tag (z.B. `Geburtstag/` → ... |
| `IA07-06` | pre-v2.4 | Ordner-Tags: Tief verschachtelt mit Umlauten (z.B. `Ferien/Nänikon 2026/Tag 3... |
| `IA07-07` | pre-v2.4 | Ordner-Tags: Gemischter Inhalt (JPG + MOV + UUID im gleichen Ordner) → alle b... |
| `IA07-08` | pre-v2.4 | Ordner-Tags: Immich-Tags werden aus IA-07 Keywords übernommen (identisch zu E... |
| `IA07-09` | pre-v2.4 | Ordner-Tags: Immich-Album wird aus zusammengesetztem Pfad erstellt (z.B. "Fer... |
| `IA07-10` | pre-v2.4 | `OCR` Flag bei erkanntem Text (screenshot_test.png) |
| `IA07-11` | pre-v2.4 | `blurry` Tag bei schlechter Qualität |
| `IA07-12` | pre-v2.4 | Kein mood-Tag (indoor/outdoor) geschrieben |
| `IA07-13` | pre-v2.4 | Kein quality-Tag ausser bei blurry |
| `IA07-14` | pre-v2.4 | Description aus AI + Geocoding zusammengebaut |
| `IA07-15` | pre-v2.4 | OCR-Text in UserComment geschrieben |
| `IA07-16` | pre-v2.4 | Dry-Run → Tags berechnet (`keywords_planned`) aber nicht geschrieben |
| `IA07-17` | pre-v2.4 | Datei-Hash nach Schreiben neu berechnet |
| `IA07-18` | pre-v2.4 | `-m` Flag: DJI DNG "Maker notes" Warning wird ignoriert, Tags trotzdem geschr... |
| `IA07-19` | pre-v2.4 | DNG: Tags korrekt geschrieben (file_size ändert sich) |
| `IA07-20` | pre-v2.4 | MP4: Tags korrekt in Video geschrieben |
| `IA07-21` | pre-v2.4 | Modul deaktiviert / keine Tags → `status: skipped, reason: no tags to write` |
| `IA07-22` | v2.28.2 | Sidecar-Schreiben ohne pre-delete (Workaround entfernt) — die Race, die den p... |
| `IA07-23` | v2.28.2 | Bei einem Retry, der IA-07 erneut ausführen muss, ist ein leftover `.xmp` aus... |

### IA08-* (32 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA08-01` | v2.8.0 | Statische Regeln werden immer zuerst ausgewertet |
| `IA08-02` | v2.8.0 | KI verifies/korrigiert Kategorie gegen DB |
| `IA08-03` | v2.8.0 | Kategorie-Label + Source als EXIF-Keywords geschrieben |
| `IA08-04` | v2.8.0 | Pfad-Template aus library_categories DB geladen |
| `IA08-05` | v2.8.0 | `personliches_foto` → persoenliche_fotos/{YYYY}/{YYYY-MM}/ |
| `IA08-06` | pre-v2.4 | `screenshot` → screenshots/{YYYY}/ |
| `IA08-07` | pre-v2.4 | `sourceless_foto` → sourceless/foto/{YYYY}/ |
| `IA08-08` | pre-v2.4 | `sourceless_video` → sourceless/video/{YYYY}/ |
| `IA08-09` | pre-v2.4 | `personliches_video` → videos/{YYYY}/{YYYY-MM}/ |
| `IA08-10` | pre-v2.4 | Sorting Rule media_type=image → Regel wird nur auf Bilder angewendet, Videos ... |
| `IA08-11` | pre-v2.4 | Sorting Rule media_type=video → Regel wird nur auf Videos angewendet, Bilder ... |
| `IA08-12` | pre-v2.4 | iPhone MOV (make=Apple) → Pre-Classification "Persönliches Video", Kategorie ... |
| `IA08-13` | pre-v2.4 | UUID MP4 ohne EXIF → Pre-Classification "Sourceless Video", Kategorie sourcel... |
| `IA08-14` | pre-v2.4 | WhatsApp Video (-WA im Namen) → Kategorie sourceless_video (Regeltest verifiz... |
| `IA08-15` | pre-v2.4 | KI-Prompt enthält korrekte Pre-Classification für Videos (nicht "Persönliches... |
| `IA08-16` | pre-v2.4 | KI gibt "Kameravideo" statt "Kamerafoto" als Source zurück bei Videos (Prompt... |
| `IA08-17` | pre-v2.4 | Unklar (kein EXIF, KI unsicher) → Status "review", Datei in unknown/review/ |
| `IA08-18` | pre-v2.4 | Immich Upload → Datei hochgeladen, Quelle gelöscht |
| `IA08-19` | pre-v2.4 | Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB... |
| `IA08-20` | pre-v2.4 | Immich: NSFW-Bild → gesperrter Ordner (`visibility: locked`), nicht archivier... |
| `IA08-21` | pre-v2.4 | Immich: NSFW-Lock funktioniert im Upload-Pfad (Inbox → Immich) |
| `IA08-22` | pre-v2.4 | Immich: NSFW-Lock funktioniert im Replace-Pfad (Polling → Immich) |
| `IA08-23` | pre-v2.4 | Namenskollision → automatischer Counter (_1, _2,...) |
| `IA08-24` | pre-v2.4 | Dry-Run → Zielpfad berechnet, nicht verschoben |
| `IA08-25` | pre-v2.4 | Leere Quellordner aufgeräumt (wenn folder_tags aktiv) |
| `IA08-26` | pre-v2.4 | EXIF-Datum korrekt verwendet (nicht Datei-Modifikationszeit) |
| `IA08-27` | pre-v2.4 | ISO 8601 Datumsformate mit Timezone/Mikrosekunden korrekt geparst |
| `IA08-28` | pre-v2.4 | DNG nach korrektem Jahresordner sortiert |
| `IA08-29` | pre-v2.4 | Video nach korrektem Jahresordner sortiert |
| `IA08-30` | v2.28.2 | `os.path.exists`-Check vor Immich-Upload **entfernt** (war Workaround für Rac... |
| `IA08-31` | v2.28.2 | `os.path.exists`-Check vor Library-Move **entfernt** (gleicher Grund) — Fehle... |
| `IA08-32` | v2.28.2 | Schutz vor Half-Copied Files liegt jetzt ausschliesslich beim Filewatcher (`_... |

### IA09-* (3 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA09-01` | pre-v2.4 | Fehler vorhanden → E-Mail gesendet |
| `IA09-02` | pre-v2.4 | Kein Fehler → keine E-Mail |
| `IA09-03` | pre-v2.4 | Modul deaktiviert → `status: skipped, reason: module disabled` |

### IA10-* (2 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA10-01` | pre-v2.4 | Temp JPEG aus IA-04 gelöscht (DNG-Konvertierung + Video-Thumbnails) |
| `IA10-02` | pre-v2.4 | Keine temp Dateien → nichts zu tun |

### IA11-* (2 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IA11-01` | pre-v2.4 | Zusammenfassung korrekt (Typ, Tags, Ort, Ziel) |
| `IA11-02` | pre-v2.4 | Log-Eintrag in system_log Tabelle erstellt |

### IM-* (10 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `IM-01` | pre-v2.4 | Upload: Datei wird hochgeladen, Asset-ID gespeichert |
| `IM-02` | pre-v2.4 | Upload: Album aus Ordner-Tags erstellt (Ferien/Spanien → "Ferien Spanien") |
| `IM-03` | pre-v2.4 | Upload: Screenshots werden archiviert (`immich_archived: true`) |
| `IM-04` | pre-v2.4 | Duplikat-Erkennung über Immich-Assets hinweg |
| `IM-05` | pre-v2.4 | Immich nicht erreichbar → Fehler geloggt, Status error, E-Mail gesendet |
| `IM-06` | pre-v2.4 | DNG nach Immich hochgeladen (25MB RAW) |
| `IM-07` | pre-v2.4 | MP4 nach Immich hochgeladen (304MB Video) |
| `IM-08` | pre-v2.4 | JPG nach Immich hochgeladen (mit GPS/Tags) |
| `IM-09` | pre-v2.4 | Immich: Alle Tags korrekt zugewiesen (auch bereits existierende Tags, HTTP 40... |
| `IM-10` | pre-v2.4 | Cross-Mode Duplikat: Dateiablage → Immich erkannt |

### NT-* (8 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `NT-01` | – | Photon-Provider (erfordert Photon-Server) |
| `NT-02` | – | CR2/NEF/ARW Formate (keine Testdateien vorhanden) |
| `NT-03` | – | Immich Polling (erfordert Upload via Immich Mobile App) |
| `NT-04` | – | Immich Replace (erfordert Polling-Aktivierung + neues Asset) |
| `NT-05` | – | Container-Neustart während Verarbeitung (risikobehaftet) |
| `NT-06` | – | HEIC Lightbox (erfordert Browser-Test) |
| `NT-07` | – | ffprobe nicht verfügbar (fest im Container installiert) |
| `NT-08` | – | Video < 1s Thumbnail (Seek-Position > Videolänge, bekanntes Limit) |

### PE-* (17 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `PE-01` | pre-v2.4 | Nicht-kritischer Step (IA-02–06) fehlgeschlagen → übersprungen, Pipeline läuf... |
| `PE-02` | pre-v2.4 | Kritischer Step (IA-01, IA-07, IA-08) fehlgeschlagen → Status "error", Finali... |
| `PE-03` | pre-v2.4 | Fehler-Datei nach error/ verschoben mit.log Datei (Traceback, Debug-Key, Zeit... |
| `PE-04` | pre-v2.4 | Voller Traceback in error_message, step_result und System-Log |
| `PE-05` | pre-v2.4 | Retry: fehlgeschlagener Job kann erneut verarbeitet werden (POST /api/job/{ke... |
| `PE-06` | pre-v2.4 | Job Delete: Job aus DB gelöscht, Datei aus error/ entfernt (POST /api/job/{ke... |
| `PE-07` | pre-v2.4 | Duplikat erkannt → Pipeline stoppt nach IA-02, Finalizer laufen |
| `PE-08` | pre-v2.4 | Korruptes Video → Warnungen, E-Mail-Benachrichtigung, kein Crash |
| `PE-09` | pre-v2.4 | Job in "processing" nach Crash → max. 3 Retry-Versuche, danach Status "error"... |
| `PE-10` | pre-v2.4 | Retry-Counter wird bei jedem Neustart-Versuch hochgezählt und geloggt |
| `PE-11` | v2.28.2 | **Atomic Claim**: `run_pipeline` weigert sich, einen Job zu verarbeiten, der ... |
| `PE-12` | v2.28.2 | **Atomic Claim**: 10 parallele `run_pipeline(same_id)`-Aufrufe → 9 brechen mi... |
| `PE-13` | v2.28.2 | **Atomic Claim**: `run_pipeline` auf Job mit Status `done`/`processing`/`erro... |
| `PE-14` | v2.28.2 | **Startup-Resume**: Resume setzt Status auf `queued` bevor `run_pipeline` auf... |
| `PE-15` | v2.28.3 | **retry_job**: Atomarer Claim `error → processing` (transienter Lock-State wä... |
| `PE-16` | v2.28.3 | **retry_job**: 5 parallele `retry_job(same_id)`-Aufrufe → exakt 1× True, 4× F... |
| `PE-17` | v2.28.3 | **retry_job**: `retry_job` parallel zu Worker-`run_pipeline` → kein stale ste... |

### PERF-* (9 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `PERF-01` | pre-v2.4 | DB-Indexes: 7/7 Indexes auf jobs + system_logs erstellt |
| `PERF-02` | pre-v2.4 | Dashboard: 1 GROUP BY Query statt 6 COUNT Queries |
| `PERF-03` | pre-v2.4 | Dashboard JSON-Endpoint Antwortzeit: **7ms** (< 100ms Limit) |
| `PERF-04` | pre-v2.4 | Duplikat pHash: Batched Query (BATCH_SIZE=5000, nur leichte Spalten) |
| `PERF-05` | pre-v2.4 | safe_move: Datei wird nur 1× gelesen — 100KB Random-Daten Integrität verifiziert |
| `PERF-06` | pre-v2.4 | Immich Upload: Streaming von Disk (kein `f.read`) |
| `PERF-07` | pre-v2.4 | Log-Rotation: `LOG_RETENTION_DAYS = 90`, stündliche Prüfung |
| `PERF-08` | pre-v2.4 | Temp-Cleanup: `shutil.rmtree` bei fehlgeschlagenen Immich-Downloads |
| `PERF-09` | pre-v2.4 | Docker: Memory-Limit 2 GB und CPU-Limit 2.0 aktiv (cgroup verifiziert) |

### SEC-* (7 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `SEC-01` | pre-v2.4 | Path Traversal: EXIF country `../../etc` → sanitisiert zu `__etc`, bleibt in ... |
| `SEC-02` | pre-v2.4 | Path Traversal: `_validate_target_path` blockiert `/library/../etc` mit Value... |
| `SEC-03` | pre-v2.4 | Path Traversal: Normaler EXIF-Wert wird durchgelassen |
| `SEC-04` | pre-v2.4 | Immich Filename: `../../etc/passwd` → `os.path.basename` → `passwd` |
| `SEC-05` | pre-v2.4 | Immich Filename: Leerer Name → Fallback auf `asset_id.jpg` |
| `SEC-06` | pre-v2.4 | Dateigrössenlimit: `MAX_FILE_SIZE = 10 GB` korrekt gesetzt |
| `SEC-07` | – | Dateigrössenlimit: Datei > 10 GB wird im Filewatcher übersprungen (nicht test... |

### WEB-* (58 Tests)

| Test-ID | Verifiziert in | Beschreibung (Kurz) |
|---|---|---|
| `WEB-01` | pre-v2.4 | Statistiken korrekt (Total, Done, Errors, Queue, Duplicates, Review) |
| `WEB-02` | pre-v2.4 | Modul-Status mit Health-Checks (KI, Geocoding, SMTP, Filewatcher, Immich) |
| `WEB-03` | pre-v2.4 | Letzte Verarbeitungen mit Auto-Refresh |
| `WEB-04` | pre-v2.4 | Module einzeln aktivieren/deaktivieren |
| `WEB-05` | pre-v2.4 | KI-Backend URL, Modell, API-Key konfigurierbar |
| `WEB-06` | pre-v2.4 | AI System-Prompt editierbar (Default-Fallback) |
| `WEB-07` | pre-v2.4 | Geocoding Provider (Nominatim/Photon/Google) + URL |
| `WEB-08` | pre-v2.4 | Inbox-Verzeichnisse: hinzufügen, bearbeiten, löschen |
| `WEB-09` | pre-v2.4 | Pro Inbox: Pfad, Label, Ordner-Tags, Dry-Run, Immich, Aktiv |
| `WEB-10` | pre-v2.4 | Immich URL + API-Key + Polling-Toggle |
| `WEB-11` | pre-v2.4 | Ziel-Ablagen (library_categories): Key, Label, Pfad-Template, Immich-Archiv, ... |
| `WEB-12` | pre-v2.4 | Sorting Rules: Medientyp-Filter (Alle/Bilder/Videos) in UI und Logik (8 Regel... |
| `WEB-13` | pre-v2.4 | pHash-Schwellwert konfigurierbar |
| `WEB-14` | pre-v2.4 | OCR-Modus (Smart/Alle) |
| `WEB-15` | pre-v2.4 | Filewatcher Schedule (Kontinuierlich/Zeitfenster/Geplant/Manuell) |
| `WEB-16` | pre-v2.4 | Sprache (DE/EN) und Theme (Dark/Light) |
| `WEB-17` | pre-v2.4 | API-Keys verschlüsselt gespeichert |
| `WEB-18` | pre-v2.4 | Gruppen transitive zusammengeführt (Union-Find) |
| `WEB-19` | pre-v2.4 | Dateien nebeneinander mit Thumbnail, EXIF, Keywords |
| `WEB-20` | pre-v2.4 | Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay |
| `WEB-21` | pre-v2.4 | Lightbox: RAW/DNG zeigt PreviewImage (ExifTool oder Immich Preview) |
| `WEB-22` | pre-v2.4 | Lightbox: ESC oder Klick schliesst Overlay |
| `WEB-23` | pre-v2.4 | EXIF-Daten für Immich-Assets via Immich API geholt |
| `WEB-24` | pre-v2.4 | "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale) |
| `WEB-25` | pre-v2.4 | "Dieses behalten" → volle Pipeline wird nachgeholt (KI, Tags, Sortierung/Immich) |
| `WEB-26` | pre-v2.4 | "Dieses behalten" bei Immich-Gruppe → KI + Tags + Upload zu Immich (<job-id> ... |
| `WEB-27` | pre-v2.4 | "Dieses behalten" bei lokaler Gruppe → KI + Tags + lokale Ablage |
| `WEB-28` | pre-v2.4 | Badge (ORIGINAL/EXAKT) ist klickbarer Link (Immich → öffnet Immich, lokal → D... |
| `WEB-29` | pre-v2.4 | Batch-Clean → alle exakten SHA256-Duplikate gelöscht, ähnliche (pHash) behalten |
| `WEB-30` | pre-v2.4 | Immich-Duplikate: Thumbnail aus Immich, "In Immich ansehen" |
| `WEB-31` | pre-v2.4 | Immich-Delete funktioniert korrekt (httpx DELETE mit request body) |
| `WEB-32` | pre-v2.4 | Keep/Delete mit JPG+DNG Paar funktioniert korrekt |
| `WEB-33` | pre-v2.4 | Alle Jobs mit Status "review" angezeigt |
| `WEB-34` | pre-v2.4 | Thumbnail (lokal oder Immich) |
| `WEB-35` | pre-v2.4 | Lightbox: Klick auf Thumbnail öffnet Originalbild als Overlay |
| `WEB-36` | pre-v2.4 | AI-Beschreibung, Tags, Metadaten angezeigt |
| `WEB-37` | pre-v2.4 | Dateigrösse angezeigt (Immich API Fallback wenn lokal nicht verfügbar) |
| `WEB-38` | pre-v2.4 | Datum angezeigt mit Fallback auf FileModifyDate bzw. job.created_at |
| `WEB-39` | pre-v2.4 | Bildabmessungen (Auflösung) angezeigt |
| `WEB-40` | pre-v2.4 | Metadatenfelder bedingt (Datum/Kamera nur wenn vorhanden) |
| `WEB-41` | v2.8.0 | Kategorie-Buttons dynamisch aus DB geladen |
| `WEB-42` | pre-v2.4 | Löschen-Button entfernt Review-Datei |
| `WEB-43` | pre-v2.4 | Lokal: Datei in richtigen Zielordner verschoben (Review → Photo) |
| `WEB-44` | pre-v2.4 | Immich: Archivierung per Kategorie-Flag `immich_archive` aus DB... |
| `WEB-45` | pre-v2.4 | Batch: "Alle → Sourceless" funktioniert (beide lokale und Immich-Items) |
| `WEB-46` | pre-v2.4 | System-Log mit Level-Filter (Info/Warning/Error) |
| `WEB-47` | pre-v2.4 | System-Log Detail mit vollem Traceback |
| `WEB-48` | pre-v2.4 | Verarbeitungs-Log mit Status-Filter |
| `WEB-49` | pre-v2.4 | Verarbeitungs-Log zeigt Dauer an |
| `WEB-50` | pre-v2.4 | Suche nach Dateiname und Debug-Key |
| `WEB-51` | pre-v2.4 | Pagination funktioniert |
| `WEB-52` | pre-v2.4 | Job-Detail: alle Step-Results, Pfade, Timestamps, Hashes |
| `WEB-53` | pre-v2.4 | Job-Detail: voller Traceback bei Fehlern |
| `WEB-54` | pre-v2.4 | Job-Detail: Immich-Thumbnail bei Immich-Assets |
| `WEB-55` | pre-v2.4 | Job-Detail: Lightbox — Klick auf Thumbnail öffnet Originalbild |
| `WEB-56` | pre-v2.4 | Job-Detail: Zurück-Button geht zu Verarbeitungs-Log |
| `WEB-57` | pre-v2.4 | Job löschen und Retry funktioniert (API-Endpunkte getestet) |
| `WEB-58` | pre-v2.4 | Preview-Badge bei Dry-Run-Jobs angezeigt |

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

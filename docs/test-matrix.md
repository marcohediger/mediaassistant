# Test-Matrix — MediaAssistant Pipeline

> Vollständige Coverage-Karte aller Code-Pfade, die `run_pipeline()` oder
> `prepare_job_for_reprocess()` auslösen — von der ersten Erkennung einer
> neuen Datei (Filewatcher, Immich-Poller) bis zum manuellen Retry und
> Duplikat-Review. Pro Szenario: Eingangs-Bedingungen, erwartetes Verhalten,
> automatisierter Test (oder explizit markierte Lücke).
>
> Stand v2.28.29 (commit `b54587f`).
>
> **Test-Skripte:**
> - [`backend/test_retry_file_lifecycle.py`](../backend/test_retry_file_lifecycle.py)
>   — Retry/Reprocess-Lifecycle gegen echtes Immich (sidecar+direct,
>   immich+file-storage, error+warning, missing-file)
> - [`backend/test_duplicate_fix.py`](../backend/test_duplicate_fix.py)
>   — Duplikat-Fix #38 + Race-Conditions für `run_pipeline`/`retry_job`
> - [`backend/test_testplan_final.py`](../backend/test_testplan_final.py)
>   — TESTPLAN.md Sektion 1-12 (Formate, Web, Filewatcher, Security,
>   Performance, Edge Cases, Stress)
> - [`backend/test_ai_backends.py`](../backend/test_ai_backends.py)
>   — AI-Backend-Loadbalancer

## Achsen

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

## Entry-Points im Code

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

## Test-Matrix: Normal Pipeline Flows (Entry 1+2+3)

Erste Verarbeitung einer neuen Datei. Status startet `queued`, läuft IA-01..IA-11 sequenziell. **Glücklicher Pfad und alle Verzweigungen**.

### N1-Serie: Inbox → Immich

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

### N2-Serie: Inbox → File-Storage (use_immich=False)

Selbe Inbox-Detection, aber `use_immich=False`. IA-08 verschiebt nach `/library/<kategorie>/<jahr>/<jahr-monat>/`.

| # | File-Type | EXIF | Write-Mode | Erwartet | Test-Status |
|---|---|---|---|---|---|
| N2.1 | JPG | ✓ | direct | done, target=`/library/photos/2024/2024-03/X.jpg` | ⚠️ **Lücke** (file-storage first-run nicht direkt getestet — nur Retry-Variante R3) |
| N2.2 | HEIC | ✓ | sidecar | done, target=`/library/photos/.../X.HEIC` + `X.HEIC.xmp` daneben | ⚠️ **Lücke** (nur als Retry-Variante R4 getestet) |
| N2.3 | MP4 | ✓ | direct | done, target=`/library/videos/...` | ⚠️ **Lücke** |
| N2.4 | JPG ohne EXIF | – | direct | done, target=`/library/sourceless/...` | ⚠️ **Lücke** |

### N3-Serie: Immich-Poller → Pipeline (Entry 2)

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

### N4-Serie: Modul-Variationen (orthogonal)

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

### N5-Serie: Spezielle Outcomes

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

### N6-Serie: Filewatcher Startup-Resume (Entry 3)

Container restart mit Jobs in `status='processing'` (z.B. nach Crash).

| # | Vor-Zustand | Erwartet | Test-Status |
|---|---|---|---|
| N6.1 | 1 Job `processing` | nach Restart: status='queued' + retry_count++, Pipeline läuft erneut | ⚠️ **Lücke** |
| N6.2 | Job mit retry_count=3 | abandoned: status='error', Meldung "Max retries (3) exceeded" | ⚠️ **Lücke** (`filewatcher.py:492`) |
| N6.3 | mehrere Jobs `processing` parallel | alle requeued sequenziell | ⚠️ **Lücke** |

### N7-Serie: Concurrency / Race-Conditions

| # | Szenario | Erwartet | Test-Status |
|---|---|---|---|
| N7.1 | 10 Dateien gleichzeitig im Inbox | alle 10 verarbeitet, kein Duplicate-Job, kein Lost-File | ✅ `test_testplan_final.py` Sektion 12 (Stress 10 parallel) |
| N7.2 | derselbe Job von 5 Pipeline-Aufrufern parallel | atomic claim: 1 läuft, 4 returnen None | ✅ `test_duplicate_fix.py` Test 5 (10 callers) |
| N7.3 | retry_job + 5 parallele run_pipeline auf demselben Job | nur retry's pipeline läuft, 5 blocked | ✅ `test_duplicate_fix.py` Test 7 |
| N7.4 | 5 parallele retry_job auf demselben Job | exakt 1 succeeded, 4 returnen False | ✅ `test_duplicate_fix.py` Test 8 |
| N7.5 | run_pipeline auf done/processing-Job (Idempotenz-Check) | no-op | ✅ `test_duplicate_fix.py` Test 6 |
| N7.6 | Bulk-Retry-All triggert 30+ parallele Pipeline-Tasks | DB-Pool reicht (20/40 nach v2.28.7), keine "QueuePool limit"-Errors | ⚠️ **Lücke** (Pool-Tuning ist da, kein automatischer Test) |

## Test-Matrix: Retry-Job (Entry 4)

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

## Test-Matrix: Retry-All Bulk (Entry 5)

`reset_job_for_retry` direkt, ohne `retry_job`-Wrapper. Selbe Logik, nur sequenziell für viele Jobs.

| # | Szenario | Test-Status |
|---|---|---|
| RA1 | Bulk-Retry mehrerer Error-Jobs ohne sofortigen Pipeline-Run (background worker picks up) | ⚠️ **Lücke** — atomar/sequenziell-Verhalten bisher nur über die einzelnen `reset_job_for_retry`-Aufrufe abgedeckt, kein End-to-End-Bulk-Test |

## Test-Matrix: Duplikat-Review (Entry 6+7)

`prepare_job_for_reprocess` aus dem Duplikat-Router. Anders als Retry-Job: andere `keep_steps`/`inject_steps`-Parameter.

| # | Szenario | Storage | Pre-Status | Test-Status |
|---|---|---|---|---|
| D1 | "Behalten" im Review: kept_job läuft volle Pipeline neu (keep IA-01) | Immich | duplicate | ⚠️ **Lücke** |
| D2 | "Behalten" im Review, File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D3 | "Kein Duplikat": IA-02 wird auf skipped injiziert, IA-01 behalten | Immich | duplicate | ⚠️ **Lücke** |
| D4 | "Kein Duplikat", File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D5 | "Kein Duplikat" wenn Datei im library/duplicates/ verschwunden ist | – | duplicate | ⚠️ **Lücke** (sollte jetzt sauber abbrechen analog zu R15) |

## Test-Matrix: `move_file=False` (Entry 8)

Code-Pfad existiert, aber **keine Aufrufer** im Repo. Reserviert für tag_cleanup (issue #42).

| # | Szenario | Test-Status |
|---|---|---|
| M1 | In-place reprocess ohne Datei-Move (z.B. nach EXIF-Wipe in target_path) | ⚠️ **Lücke** — kein Test, weil kein Caller |

## Pro-Step Failure-Matrix (orthogonal zu obiger Achse)

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

## Modul-Konfigurationen (orthogonal)

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

## Zusammenfassung

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

## Empfohlene nächste Tests

Priorisiert nach **Daten-Verlust-Risiko** und **Live-Frequenz**:

### Hohe Priorität (Daten-Verlust möglich)
1. **R6 (Immich+sidecar+error)** und **R10/R11 (File-Storage+error)** — dieselbe Klasse Bug wie R5, nur mit anderer Branch in IA-08. 1:1 vom R5-Test ableitbar, ~30 Min Aufwand.
2. **N3.1–N3.5 (Immich-Poller-Normal-Flow)** — bisher null Coverage für eine ganze Job-Quelle. IA-10-Cleanup darf hier (und nur hier) Tempdir löschen — Test muss aktiv prüfen dass es passiert. ~2h (Poller-Setup, fake Immich-Asset, Lifecycle-Asserts).
3. **D3/D5 (Duplikat-"Kein Duplikat")** — eigener Caller von `prepare_job_for_reprocess`, andere Pre-Status (`duplicate`). Live relevant beim manuellen Review. ~1h.

### Mittlere Priorität (Korrektheit)
4. **N1.3 (Inbox+sidecar+Immich first-run)** — deckt eine Achse ab, die bisher nur indirekt im Retry-Test getroffen wird. ~20 Min.
5. **N2.1–N2.4 (Inbox→File-Storage normal flow)** — Coverage-Lücke für eine ganze Storage-Achse. ~30 Min.
6. **N5.4–N5.6 (review/skip/dry_run)** — drei alternative Job-End-Status, die UI-Logik triggern. Je 15 Min.
7. **R7/R8 (IA-07-Critical-Error retry)** — testet, dass `_move_to_error` den Move sauber macht und Retry den richtigen Pfad findet. ~30 Min.
8. **D1/D2 ("Behalten" im Review)** — wie D3, aber mit `keep_steps={'IA-01'}`. ~30 Min.

### Niedrige Priorität (Edge Cases)
9. **N4.1–N4.8 (Modul-Variationen)** — jedes Modul einzeln aus testen. Defensiv, aber unwahrscheinliche Live-Konfigurationen. ~2h für alle 8.
10. **N6.1–N6.3 (Startup-Resume)** — schwer zu testen ohne echten Container-Restart. ~1h.
11. **N5.7/N5.8 (Auto-Pause)** — mit Mock-Backend-Down. ~30 Min.
12. **N7.6 (Pool-Exhaustion-Stress)** — synthetischer Stress mit 50+ parallel Tasks. ~30 Min.
13. **RA1 (Bulk-Retry-All End-to-End)** — wenige zusätzliche Asserts oben auf bestehende reset_job_for_retry-Tests. ~30 Min.

**Geschätzter Gesamt-Aufwand für 100% Coverage: 12–15 Stunden.**

Realistisches Ziel für nächsten Sprint: **Hohe Priorität + N1.3 + N2.x = ~5 Stunden, ~20 zusätzliche Asserts.**

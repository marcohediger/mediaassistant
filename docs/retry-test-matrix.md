# Retry / Reprocess Test-Matrix

> Diese Matrix listet alle echten Code-Pfade durch die Retry- und
> Reprocess-Logik (`reset_job_for_retry`, `prepare_job_for_reprocess`)
> mit allen relevanten Einstellungs-Achsen, plus Status pro Kombination.
>
> Ziel: explizit machen welche Szenarien automatisiert getestet sind und
> wo Lücken offen sind. Stand v2.28.29 (commit 7abc4a6).
>
> Test-Datei: [`backend/test_retry_file_lifecycle.py`](../backend/test_retry_file_lifecycle.py)
> Race-Condition-Tests: [`backend/test_duplicate_fix.py`](../backend/test_duplicate_fix.py) (Test 7+8)

## Achsen

| Achse | Werte | Bedeutung |
|---|---|---|
| **Entry-Point** | retry_job / reset_job_for_retry / duplicates.review / duplicates.not-duplicate / `move_file=False` | wer löst den Reprocess aus |
| **Storage** | Immich / File-Storage | `use_immich=True/False` → IA-08-Branch |
| **Write-Mode** | direct / sidecar | `metadata.write_mode` → EXIF-in-Datei vs `.xmp`-Sidecar |
| **Source** | Inbox / Immich-Poller / Manual | `source_label`, prägt `original_path`-Prefix |
| **Pre-Retry-Status** | done+Warnungen / error / duplicate | aus welchem Job-Zustand kommt der Retry |
| **Pre-Retry-File-Location** | inbox / library / library/error / library/duplicates / `/tmp/ma_immich_*` / nowhere | wo liegt die Datei beim Retry-Klick |
| **immich_asset_id gesetzt** | yes / no | beeinflusst IA-08-Branch (webhook vs upload) und (vor Fix) IA-10-Cleanup |

## Entry-Points im Code

| # | Entry | Datei:Zeile | Auslöser |
|---|---|---|---|
| 1 | `retry_job(jid)` | `pipeline/__init__.py:440` | UI-Button "Retry" pro Job (`POST /api/job/{key}/retry`) |
| 2 | `_bulk_reset_errors_in_background()` → `reset_job_for_retry(jid)` | `routers/api.py:54`, `pipeline/__init__.py:334` | UI "Retry-All" für alle Error-Jobs (`POST /api/jobs/retry-all-errors`) |
| 3 | `prepare_job_for_reprocess` aus `routers/duplicates.py:789` | `routers/duplicates.py:789` | "Behalten" im Duplikat-Review (`/api/duplicates/review`) |
| 4 | `prepare_job_for_reprocess` aus `routers/duplicates.py:837` | `routers/duplicates.py:837` | "Kein Duplikat" im Duplikat-Review (`/api/duplicates/not-duplicate`) |
| 5 | `move_file=False`-Variante | `pipeline/reprocess.py:211` | derzeit **keine Aufrufer** im Code (für tag_cleanup vorgesehen, issue #42) |

## Test-Matrix: Retry-Job (Entry 1)

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

## Test-Matrix: Retry-All Bulk (Entry 2)

`reset_job_for_retry` direkt, ohne `retry_job`-Wrapper. Selbe Logik, nur sequenziell für viele Jobs.

| # | Szenario | Test-Status |
|---|---|---|
| RA1 | Bulk-Retry mehrerer Error-Jobs ohne sofortigen Pipeline-Run (background worker picks up) | ⚠️ **Lücke** — atomar/sequenziell-Verhalten bisher nur über die einzelnen `reset_job_for_retry`-Aufrufe abgedeckt, kein End-to-End-Bulk-Test |

## Test-Matrix: Duplikat-Review (Entry 3+4)

`prepare_job_for_reprocess` aus dem Duplikat-Router. Anders als Retry-Job: andere `keep_steps`/`inject_steps`-Parameter.

| # | Szenario | Storage | Pre-Status | Test-Status |
|---|---|---|---|---|
| D1 | "Behalten" im Review: kept_job läuft volle Pipeline neu (keep IA-01) | Immich | duplicate | ⚠️ **Lücke** |
| D2 | "Behalten" im Review, File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D3 | "Kein Duplikat": IA-02 wird auf skipped injiziert, IA-01 behalten | Immich | duplicate | ⚠️ **Lücke** |
| D4 | "Kein Duplikat", File-Storage | File-Storage | duplicate | ⚠️ **Lücke** |
| D5 | "Kein Duplikat" wenn Datei im library/duplicates/ verschwunden ist | – | duplicate | ⚠️ **Lücke** (sollte jetzt sauber abbrechen analog zu R15) |

## Test-Matrix: `move_file=False` (Entry 5)

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

| Kategorie | Abgedeckt | Lücke |
|---|---|---|
| Retry-Job, Inbox-Source, Warnungs-Retry | ✅ 4/4 (R1–R4) | – |
| Retry-Job, Inbox-Source, Error-Retry | ⚠️ 1/6 (R5; R6–R11 offen) | sidecar/file-storage error, IA-07/IA-01 errors |
| Retry-Job, Immich-Poller-Source | ❌ 0/3 (R12–R14) | komplett offen |
| Retry-Job, Negativ-Fall (file weg) | ✅ 1/1 (R15) | – |
| Retry-All Bulk | ❌ 0/1 (RA1) | komplett offen |
| Duplikat-Review (D1–D5) | ❌ 0/5 | komplett offen |
| `move_file=False` (M1) | ❌ 0/1 | kein Caller, kein Test |
| Race-Conditions (parallele retry_job) | ✅ 2/2 (test_duplicate_fix.py 7+8) | – |
| **TOTAL** | **8 abgedeckt + 2 Race** | **15 offen** |

## Empfohlene nächste Tests

Priorisiert nach **Daten-Verlust-Risiko**:

1. **R6 (Immich+sidecar+error)** und **R10/R11 (File-Storage+error)** — dieselbe Klasse Bug wie R5, nur mit anderer Branch in IA-08. 1:1 vom R5-Test ableitbar, ~30 Min Aufwand.
2. **R12/R13 (Immich-Poller+Warnung)** — dies ist der EINZIGE legitime Fall, in dem IA-10 die Datei löschen DARF (`source_label='Immich'` + `/tmp/ma_immich_*`). Test sollte aktiv prüfen, dass der Cleanup feuert. ~1h Aufwand (Immich-Poller-Setup nachbauen).
3. **D3/D5 (Duplikat-"Kein Duplikat")** — eigener Caller von `prepare_job_for_reprocess`, andere Parameter, andere Pre-Status (`duplicate`). Live relevant. ~1h.
4. **R7/R8 (IA-07-Critical-Error)** — testet, dass `_move_to_error` den Move sauber macht und Retry den richtigen Pfad findet. ~30 Min.
5. **D1/D2 ("Behalten" im Review)** — wie D3 aber mit `keep_steps={'IA-01'}`. ~30 Min.

Geschätzter Gesamt-Aufwand für volle Coverage: **3–4 Stunden**.

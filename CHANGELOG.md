# Changelog

## v2.28.6 — 2026-04-07

### Fix: "Alle Fehler retry"-Button — Höhe und Funktionalität

**Problem 1 (Höhe):** Der `<button>`-Tag im POST-Form hatte eine andere
Höhe als der `<a>`-Tag des "Dry-Run Report"-Buttons, weil `.btn` keine
expliziten `font-family`, `line-height` oder `box-sizing` Properties
hatte und Browser diese für `<button>` und `<a>` unterschiedlich
defaulten.

**Problem 2 (Funktionalität):** Das Form-POST war fragil — bei manchen
Browser/Auth-Konstellationen wurde das Submit nicht ausgeführt, oder die
Redirect-Kette mit Session-Cookie ging schief.

**Fix:**
- `style.css` `.btn`: explizit `font-family: inherit`, `line-height: 1.5`,
  `box-sizing: border-box`, `vertical-align: middle` gesetzt → identische
  Box-Maße für `<a>` und `<button>` (Cache-Buster v19 → v20)
- `logs.html`: Form durch `<a href="#">` mit `onclick="retryAllErrors()"`
  ersetzt. Der JS-Handler nutzt `fetch()` mit `credentials: 'same-origin'`,
  ruft den Endpoint async auf und reloadet danach die Seite mit
  preserved Filter-State
- Confirm-Dialog wird via `tojson` filter sicher in JS gerendert (mit
  korrektem Escaping für Sonderzeichen in der i18n-Übersetzung)

Dadurch:
- Beide Buttons sind exakt gleich hoch und visuell identisch
- Click ist robust auch bei Session-Edge-Cases
- Filter-State bleibt 100% erhalten (return_url wird via JS aus
  `current_query` gebaut und an POST + window.location übergeben)

## v2.28.5 — 2026-04-07

### Fix: Log-Filter bleiben erhalten beim Tab-Wechsel und Button-Klick

Bisher gingen die gesetzten Filter (Status, Level, Suchbegriff, Page) auf
der Logs-Seite verloren, sobald man:

- Zwischen den Tabs "System-Log" und "Verarbeitungs-Log" wechselte
  (Links zeigten hardcoded auf `/logs?tab=...` ohne Filter-Params)
- Den "Alle Fehler retry"-Button (v2.28.4) drückte (Redirect ging immer
  auf `/logs?tab=jobs&status=error`, ungeachtet der vorher gesetzten Filter)

**Fix:**
- Tab-Links übernehmen jetzt alle gesetzten Filter via `non_tab_query`
- Retry-All-Endpoint akzeptiert ein verstecktes `return_url`-Form-Field
  (gefüllt aus dem aktuellen Filter-State) und nutzt sonst den Referer-Header
  als Fallback. Open-Redirect ist via Whitelist (`return_url muss /logs...
  enthalten`) abgesichert
- Pagination, Detail-Navigation und Browser-Reload waren bereits
  filter-stable und bleiben unverändert

Tests im Dev-Container — alle 4 Redirect-Szenarien grün:
1. explizite `return_url` → preserved
2. nur `Referer` Header → preserved (mit `/logs`-Extraktion)
3. weder noch → Default `/logs?tab=jobs&status=error`
4. bösartige URL `https://evil.com/` → Default (Open-Redirect-Schutz)

## v2.28.4 — 2026-04-07

### Feature: "Alle Fehler retry" Button im Logs-View

Neuer Button neben "Dry-Run Report" oben rechts auf der Logs-Seite. Klick
ruft `POST /api/jobs/retry-all-errors` auf, das alle Jobs im Status `error`
parallel über `retry_job()` neu startet. Da `retry_job` einen atomaren
Claim (`error → processing`) verwendet, ist der Endpoint sicher gegen
Doppelklicks und kann beliebig oft aufgerufen werden — derselbe Job wird
nie zweimal parallel verarbeitet.

Nach dem Klick wird der User auf `/logs?tab=jobs&status=error` umgeleitet,
damit er den Fortschritt verfolgen kann. Confirm-Dialog vor dem Trigger.

i18n: `logs.retry_all_errors` + `logs.retry_all_confirm` für DE und EN.

## v2.28.3 — 2026-04-07

### Fix: retry_job hatte Folge-Race-Window

Beim Test des v2.28.2-Fixes im Dev-Container fiel auf, dass `retry_job`
zwischen seinen zwei Commits (1. status=queued, 2. step_result aufgeräumt)
ein TOCTOU-Window hat, in dem ein paralleler Aufrufer mit *stale* step_result
claimen konnte — Pipeline würde dann IA-01 überspringen, weil der alte
Error-Eintrag noch im step_result liegt.

**Fix:** `retry_job` claimt jetzt atomar `error → processing` (transienter
Lock-State), führt die Cleanup-Operationen durch (Datei-Move, step_result
bereinigen), und flippt erst danach auf `queued`. Erst dann darf
`run_pipeline` claimen. Das verhindert sowohl parallele `retry_job`-Aufrufe
(z.B. Doppelklick / mehrere Browser-Tabs) als auch die Race mit dem Worker.

### Test Suite: 4 neue Race-Condition-Tests in `test_duplicate_fix.py`

- **Test 5:** 10 parallele `run_pipeline()` für denselben queued Job → exakt
  1 Ausführung, 9 mit `already claimed` geblockt, exakt 1 IA-01 Error-Log
- **Test 6:** `run_pipeline()` auf Job mit Status `done` ist No-op
- **Test 7:** `retry_job()` parallel zu 5× `run_pipeline()` → nur retry_job
  läuft, IA-01 wird tatsächlich frisch ausgeführt (kein stale Reuse)
- **Test 8:** 5 parallele `retry_job()`-Aufrufe → exakt 1 erfolgreich, 4
  geben False zurück

Alle 26 Tests im Dev-Container grün (`docker exec mediaassistant-dev
python3 /app/test_duplicate_fix.py`).

## v2.28.2 — 2026-04-07

### Fix: Race-Condition — derselbe Job wurde von mehreren Pipeline-Instanzen parallel verarbeitet

**Root Cause:** `run_pipeline()` hatte keinen atomaren Schutz gegen den Übergang
`queued → processing`. Da `run_pipeline` von 5 Stellen aufgerufen wird (Worker,
`retry_job`, `_poll_immich`, Startup-Resume, Duplikate-Router), konnten zwei
Aufrufer denselben Job gleichzeitig starten — z.B. Worker selektiert einen
Job, der gleichzeitig per API-Retry wiederaufgenommen wird. Beide Pipelines
schrieben dann parallel in dieselben Dateien:

- IA-07 schlug mit `XMP Sidecar already exists` fehl, weil Run B die Datei
  vorfand, die Run A gerade geschrieben hatte
- IA-08 schlug mit `File disappeared before upload` fehl, weil Run A die
  Quelldatei nach Immich-Upload bereits gelöscht hatte
- IA-01 schlug mit `ExifTool File not found` fehl aus demselben Grund

In den Logs sichtbar als doppelte `Pipeline done`-Einträge mit
unterschiedlichen Tag-Counts für dieselbe debug_key, sowie Jobs mit Status
`done`, deren `error_message`-Feld trotzdem einen IA-07-Traceback enthielt.
**~30 betroffene Jobs / 120 inkonsistente error_messages über 2 Tage.**

**Fix:** Atomarer Claim am Anfang von `run_pipeline` via `UPDATE jobs SET
status='processing' WHERE id=? AND status='queued'`. Nur der Aufrufer mit
`rowcount == 1` läuft weiter; alle anderen brechen sofort ab.

### Fix: Symptom-Pflaster zurückgebaut

Mit dem echten Race-Fix sind die folgenden Workarounds aus v2.28.0/v2.27.x
nicht mehr nötig und wurden entfernt:

- `step_ia07_exif_write.py`: pre-delete des XMP-Sidecars vor dem ExifTool-Aufruf
  (war ein TOCTOU-Pflaster für die Race)
- `step_ia08_sort.py`: `os.path.exists`-Check vor Immich-Upload und Library-Move
  (war ebenfalls ein TOCTOU-Pflaster)

Falls echte Filesystem-Probleme auftreten, sollen Fehler aus `upload_asset`
oder `safe_move` direkt durchgereicht werden — die irreführende Meldung
"file disappeared … or was moved by another process" wurde ohnehin durch die
Race ausgelöst, nicht durch externe Prozesse.

### Hinweis

Das Startup-Resume in `filewatcher.py` setzt jetzt `status='queued'` bevor es
`run_pipeline` aufruft, damit der atomare Claim zugreift.

## v2.28.1 — 2026-04-05

### UI: Durchsatz als Grid-Karten statt Inline-Balken

- Durchsatz-Anzeige (Dateien/Min, /Std, /24h) als 3 einzelne Karten im Grid-Layout
- Konsistenter mit dem Stats-Grid darüber

## v2.28.0 — 2026-04-05

### Feature: Durchsatz-Anzeige & ETA auf dem Dashboard (#41)

- Neuer Throughput-Balken unter den Stats-Karten zeigt:
  - Dateien/Min (letzte 5 Minuten)
  - Dateien/Std (letzte Stunde)
  - Dateien gesamt (letzte 24 Stunden)
- **ETA bei wartenden Jobs:** Bei der "Wartend"-Karte wird die geschätzte Restzeit angezeigt (z.B. "~50 Min", "~5.6 Std"), berechnet aus aktuellem Durchsatz und Anzahl wartender Jobs
- Wird automatisch via Live-Polling aktualisiert
- i18n-Unterstützung für DE und EN

### Fix: UnicodeDecodeError in ExifTool-Aufrufen (#38)

- Alle `subprocess.run(..., text=True)` Aufrufe durch manuelle `decode('utf-8', errors='replace')` ersetzt
- Betrifft: IA-01 (EXIF Read), IA-07 (EXIF Write), Duplikat-Merge, API Health-Check
- Verhindert Crash bei Nicht-UTF-8-Bytes in ExifTool stderr

### Fix: Immich Upload Retry bei 5xx (#39)

- Upload versucht bei HTTP 5xx bis zu 3 Retries mit Backoff (30s, 60s, 120s)
- File-Handles werden pro Retry neu geöffnet
- Jeder Retry wird geloggt (System-Log + Python-Logger)

### Fix: Log-Filter geht verloren beim Zurücknavigieren (#40)

- Filter-Parameter (Status, Suche, Seite) werden als URL-Params an die Detail-Ansicht weitergereicht
- Zurück-Button in der Detail-Ansicht stellt den Filter wieder her

## v2.27.5 — 2026-04-05

### Fix: Duplikate fälschlicherweise bis IA-08 weitergeleitet (#38)

- **Ursache:** Wenn nach dem Verschieben einer Duplikat-Datei das Aufräumen leerer Ordner fehlschlug, wurde IA-02 als "unkritischer Fehler" übersprungen. Die Pipeline lief weiter bis IA-08, wo die bereits verschobene Datei nicht mehr gefunden wurde → "File disappeared before upload".
- **Fix 1:** Cleanup in `_handle_duplicate` ist jetzt in try-except gewrappt — ein Fehler beim Aufräumen kann nicht mehr die Duplikat-Erkennung sabotieren.
- **Fix 2:** Fallback in der Pipeline: Wenn IA-02 fehlschlägt aber `job.status == "duplicate"` bereits gesetzt ist, wird die Pipeline korrekt als Duplikat beendet.

## v2.27.2 — 2026-04-04

### Fix: XMP Sidecar "already exists" bei Retry

- Bestehende `.xmp` Sidecar-Datei wird vor dem Schreiben gelöscht (z.B. von einem früheren fehlgeschlagenen Lauf)

## v2.27.1 — 2026-04-04

### Fix: debug_key Kollision ab 10000 Jobs

- **Ursache:** `MAX(debug_key)` ist ein String-Vergleich in SQLite. `MA-2026-9999` ist alphabetisch grösser als `MA-2026-10000` (weil `9` > `1`). Der Counter las immer 9999, inkrementierte auf 10000, kollidierte mit dem existierenden Key.
- **Fix:** `CAST(SUBSTR(debug_key, N) AS INTEGER)` — numerischer MAX statt String-MAX.
- Betrifft nur Systeme mit >9999 Jobs pro Jahr.

## v2.27.0 — 2026-04-04

### Stabilität & Performance bei grossen Imports (#28-#35)

- **Immich Streaming Download** (#28/#21) — Downloads werden jetzt in 1 MB Chunks auf Disk geschrieben statt komplett in den RAM geladen. Verhindert OOM bei grossen Videos (1-4 GB).
- **SQLite Timeout 120s** (#29/#22) — Timeout von 30s auf 120s erhöht (connect_args + busy_timeout). Verhindert "Database is locked" bei vielen parallelen Pipeline-Workers.
- **Batch-ExifTool mit 100er-Limit** (#30/#23) — ExifTool-Aufrufe werden in Batches von max. 100 Dateien aufgeteilt. Verhindert Command-Line-Overflow bei 1000+ Dateien. Dynamischer Timeout (2s pro Datei).
- **ExifTool Timeout dynamisch** (#31/#24) — IA-01 ExifTool-Timeout basiert jetzt auf Dateigrösse (30s Base + 1s pro 10 MB). Grosse RAW-Dateien (>500 MB) scheitern nicht mehr.
- **Immich Temp-Dirs Cleanup** (#32/#25) — IA-10 Cleanup räumt leere `ma_immich_*` Temp-Verzeichnisse auf (war bereits teilweise implementiert, jetzt mit Error-Handling).
- **Composite-Indexes** (#33/#26) — Neue Indexes `(file_hash, status)`, `(phash, status)`, `(status, created_at)` für schnellere Queries bei 150k+ Jobs.
- **Cleanup Error-Handling** (#34/#27) — IA-10 Cleanup crasht nicht mehr bei gesperrten Dateien (try/except pro Datei, Warnung statt Fehler).
- **Sidecar File-Handle-Leak** (#35/#28) — `upload_asset()` nutzt jetzt `contextlib.ExitStack` für garantiertes Schliessen aller File-Handles bei Netzwerk-Timeouts.

## v2.26.3 — 2026-04-04

### Fix: debug_key Kollision trotz Lock bei hoher Last

- **Ursache:** `asyncio.Lock` schützte die MAX-Query + INSERT Sequenz, aber bei hoher DB-Last (200 GB Import + Immich-Poll gleichzeitig) konnte SQLite die Session nicht schnell genug committen. Die nächste Query sah den gleichen MAX-Wert.
- **Fix:** In-Memory-Counter ersetzt die DB-Query. Counter wird einmalig aus der DB initialisiert, danach nur noch im Speicher inkrementiert. Keine zwei Coroutines können jemals den gleichen Wert bekommen.
- Bei IntegrityError (z.B. nach Container-Restart mit veraltetem Counter) wird der Counter automatisch aus der DB re-initialisiert.

## v2.26.2 — 2026-04-04

### Fix: debug_key Kollision bei vielen gleichzeitigen Dateien

- **Race Condition** — wenn viele Dateien gleichzeitig eintreffen (z.B. Immich-Poll mit 60+ Assets), fragten alle Coroutines gleichzeitig `MAX(debug_key)` ab und erhielten denselben Wert. Das führte zu Endlos-Kollisionen und alle Jobs scheiterten nach 10 Versuchen.
- **Fix:** `asyncio.Lock` serialisiert die debug_key-Generierung. Key-Vergabe + INSERT erfolgen atomar — Kollisionen sind ausgeschlossen.
- Retry-Loop entfernt (nicht mehr nötig)

## v2.26.1 — 2026-04-04

### Video-Vorschau in Duplikat- und Review-Ansicht (#24)

- **Video-Thumbnails** — ffmpeg extrahiert ein Frame (bei Sekunde 1) und liefert es als JPEG-Thumbnail
- Funktioniert für MP4, MOV, AVI, MKV, WebM, M4V, MTS
- Gilt für beide Ansichten: Duplikat-Review und manuelle Review-Seite

## v2.26.0 — 2026-04-04

### Duplikat-Ansicht: Performance, Metadaten-Merge & Bestätigungsdialog (#25, #27)

- **Performance:** Batch-ExifTool — alle Dateien einer Gruppe werden in einem einzigen ExifTool-Aufruf gelesen statt einzeln (deutlich schneller bei vielen Duplikaten)
- **Performance:** Paginierte API (`GET /api/duplicates/groups?page=1&per_page=10`) — nur die ersten 10 Gruppen werden beim Laden der Seite abgefragt, weitere per "Mehr laden"-Button
- **Metadaten-Merge** — neuer Endpoint `POST /api/duplicates/merge-metadata` überträgt fehlende Metadaten (GPS, Datum, Kamera, Keywords, Beschreibung) vom Duplikat auf die behaltene Datei
- **Metadaten-Differenz** — in der Duplikat-Ansicht werden Felder visuell hervorgehoben (grün), die bei einem Mitglied vorhanden sind, beim anderen aber fehlen. Badge "+Mehr Metadaten" zeigt auf einen Blick, welche Datei reichere Daten hat
- **Merge-Button** — pro Karte ein "Metadaten übernehmen ← Dateiname"-Button, wenn die andere Datei fehlende Felder ergänzen kann
- **Bestätigungsdialog ausschaltbar** — neues Setting `duplikat.skip_confirm` in den Duplikat-Einstellungen: deaktiviert die Sicherheitsabfrage für Behalten, Löschen und "Kein Duplikat"
- Duplikat-Gruppen-Template als Partial (`_dup_group.html`) extrahiert
- i18n: DE + EN für alle neuen Texte

## v2.25.10 — 2026-04-04

### Optionen-Übersicht im Eingangsverzeichnisse-Bereich

- **Ausklapp-Info** unter den Eingangsverzeichnissen erklärt alle Inbox-Optionen (Immich, Ordner-Tags, Dry-Run, Aktiv) und wann Änderungen übernommen werden
- Ordner-Tags: sofort (Runtime-Prüfung), Immich/Dry-Run: bei Job-Erstellung, Aktiv: beim nächsten Scan
- i18n: DE + EN

## v2.25.9 — 2026-04-04

### Fix: Inbox folder_tags Einstellung wird live nachgelesen

- **Runtime-Prüfung** — IA-07 und IA-08 lesen die `folder_tags` Einstellung der Inbox jetzt direkt aus der Datenbank statt den bei Job-Erstellung gespeicherten Wert zu verwenden
- Umschalten der Inbox-Option greift sofort, auch für bereits erstellte Jobs in der Queue
- Prüft sowohl das globale Modul `ordner_tags` als auch die Inbox-Einstellung zur Laufzeit

## v2.25.8 — 2026-04-04

### Album-Logging im IA-08 Ergebnis

- **`immich_albums_added`** — neues Feld im IA-08 Step-Result zeigt welche Alben bei der Verarbeitung erstellt/zugewiesen wurden
- `upload_asset()` gibt jetzt die Namen der hinzugefügten Alben im Response zurück
- Betrifft beide Upload-Pfade: Webhook (Replace) und normaler Immich-Upload

## v2.25.7 — 2026-04-04

### Fix: Ordner-Tags werden trotz deaktiviertem Modul erstellt

- **Modul-Prüfung zur Laufzeit** — IA-07 (EXIF-Tags) und IA-08 (Immich-Alben) prüfen jetzt zusätzlich ob das Modul `ordner_tags` zur Pipeline-Laufzeit noch aktiv ist
- Zuvor wurde nur `job.folder_tags` geprüft (zum Zeitpunkt der Job-Erstellung gesetzt), sodass bei nachträglicher Deaktivierung des Moduls trotzdem Ordner-Tags und Immich-Alben erstellt wurden
- Betrifft: EXIF/XMP-Keywords aus Ordnernamen und Immich-Album-Erstellung aus Ordnerstruktur

## v2.25.6 — 2026-04-03

### Fix: Race Condition bei paralleler Verzeichnis-Bereinigung

- **_cleanup_empty_dirs absturzsicher** — wenn parallele Jobs Dateien im gleichen Verzeichnis verarbeiten, konnte ein Job das Verzeichnis löschen während ein anderer noch darauf zugriff
- Prüft jetzt ob das Verzeichnis noch existiert bevor es gelöscht wird
- FileNotFoundError wird sauber abgefangen statt die Pipeline zu unterbrechen

## v2.25.5 — 2026-04-03

### Fix: FileNotFoundError nach Immich-Upload

- **Quelldatei-Löschung absturzsicher** — wenn die Quelldatei bereits entfernt wurde (z.B. durch parallelen Job), wird dies sauber protokolliert statt einen Fehler auszulösen

## v2.25.4 — 2026-04-03

### Kontinuierlicher Worker-Pool

- **Gleichmässige Lastverteilung** — Jobs werden sofort nachgefüllt wenn ein Slot frei wird, statt auf den ganzen Batch zu warten
- Vorher: Start N → warte auf alle → Start N (Burst-Idle-Muster)
- Nachher: Slot frei → nächster Job startet sofort (kontinuierlich)

## v2.25.3 — 2026-04-03

### Fix: Multi-Slot Semaphore

- **Slot-Anzahl wird jetzt korrekt angewendet** — Semaphore wird bei Änderung der Slot-Konfiguration neu erstellt
- Zuvor blieb der Semaphore auf dem initialen Wert (1) stecken, unabhängig von der Einstellung

## v2.25.2 — 2026-04-03

### Stabilität & zeitversetzter Start

- **Zeitversetzter Job-Start** — parallele Jobs starten 2s versetzt statt alle gleichzeitig, reduziert Lastspitzen auf KI-Backend und SQLite
- **DB-Lock Recovery** — Filewatcher bleibt nicht mehr hängen wenn SQLite bei paralleler Verarbeitung kurzzeitig gesperrt ist
- **Immich Upload Fehlerbehandlung** — ungültige Upload-Ergebnisse werden sauber als Fehler gemeldet statt die Pipeline zu blockieren

## v2.25.1 — 2026-04-03

### Dashboard: KI-Status zusammengefasst

- **Dashboard zeigt einen einzelnen KI-Kasten** mit Verbindungsstatus `(X/Y)` statt zwei separate Module
  - `(0/0)` = keine KI aktiviert, `(1/1)` = 1 von 1 verbunden, `(1/2)` = 1 von 2 verbunden, etc.
- **Konfigurierbare Slots pro Backend** — `ai.slots` / `ai2.slots` (1–16) für parallele Verarbeitung
- **Pipeline verarbeitet mehrere Bilder gleichzeitig** entsprechend der verfügbaren Slots

## v2.25.0 — 2026-04-03

### Zweites KI-Backend für parallele Verarbeitung

- **Multi-Backend Load Balancing** — optional ein zweites OpenAI-kompatibles KI-Backend konfigurierbar
  - Bilder werden automatisch dem gerade freien Backend zugewiesen
  - Wenn beide Backends beschäftigt sind, wird auf das nächste freie gewartet
  - Funktioniert für KI-Analyse (IA-05) und OCR (IA-06)
  - Kein zweites Backend konfiguriert = Verhalten wie bisher (single backend)
- **Konfiguration über Setup-Wizard und Einstellungsseite** — URL, Modell und API Key für Backend 2
- **Umgebungsvariablen** — `AI2_BACKEND_URL`, `AI2_MODEL`, `AI2_API_KEY`

## v2.18.0 — 2026-04-02

### XMP-Sidecar-Modus + Einstellungen neu geordnet

- **Neuer Metadaten-Schreibmodus: XMP-Sidecar** — optionale Alternative zum direkten Schreiben in die Datei
  - Originaldatei bleibt komplett unverändert (Datei-Hash ändert sich nicht)
  - Separate `.xmp`-Sidecar-Datei wird neben dem Original erstellt (z.B. `foto.jpg` → `foto.jpg.xmp`)
  - Bei neuen Immich-Uploads wird die Sidecar-Datei als `sidecarData` mitgesendet
  - Bei bestehenden Immich-Assets (Polling/Webhook) wird **kein Re-Upload** durchgeführt — Tags werden nur via Immich-API gesetzt
  - Bei lokaler Dateiablage wird die Sidecar-Datei neben die Bilddatei in die Bibliothek verschoben
  - Ideal für Handy-App-Synchronisierung, da sich der Datei-Hash nicht ändert
  - Bestehender Modus (direkt in Datei schreiben) bleibt Standard und unverändert
- **Einstellungsseite neu geordnet** — logischer Aufbau entlang der Pipeline:
  - Eingang: Eingangsverzeichnisse → Filewatcher
  - Klassifikation: Sortier-Regeln → Ziel-Ablage
  - Verarbeitung: Duplikate → Geocoding → KI → Video-Thumbnails → OCR → Ordner-Tags
  - Ausgabe: Metadaten-Schreibmodus → Immich
  - System: SMTP → Darstellung
- **Detailliertere Beschreibungen** für alle Einstellungs-Sektionen — jede Option erklärt jetzt klar was sie bewirkt, wie sie wirkt und welche Abhängigkeiten bestehen
- Betroffene Dateien: `step_ia07_exif_write.py`, `immich_client.py`, `step_ia08_sort.py`, `step_ia10_cleanup.py`, `config.py`, `routers/settings.py`, `settings.html`, `de.json`, `en.json`

## v2.17.5 — 2026-04-02

### Video-Tags + vollständige Format-Kompatibilität

- MP4/MOV-Videos können jetzt auch Tags erhalten (XMP Subject)
- Format-aware Tag-Schreibung für alle unterstützten Formate:
  - JPEG/PNG/TIFF/DNG → `Keywords` (IPTC)
  - HEIC/HEIF/WebP/MP4/MOV → `Subject` (XMP dc:subject)
- XPComment wird bei MP4/MOV übersprungen (nicht unterstützt)
- Format-Mismatch-Erkennung um MP4/MOV erweitert
- Alle 8 Formate getestet: Tags + Description in Immich verifiziert ✓

## v2.17.4 — 2026-04-02

### HEIC-Tag-Schreibung repariert

- HEIC/HEIF/PNG/WebP unterstützen kein IPTC — `Keywords+=` hat bei diesen Formaten nichts geschrieben
- IA-07 erkennt jetzt das Format und wählt das passende Tag-Feld:
  - JPEG/TIFF/DNG → `Keywords` (IPTC)
  - HEIC/HEIF/PNG/WebP → `Subject` (XMP dc:subject)
- Immich liest beide Felder korrekt aus und erstellt Tags

## v2.17.3 — 2026-04-02

### EXIF-Tags: nur Keywords schreiben

- IA-07 schreibt Tags nur noch in `Keywords` (IPTC) statt in 4 Felder (Keywords, Subject, TagsList, HierarchicalSubject)
- Die zusätzlichen Felder wurden als vermeintlicher Immich-Fix in v2.16.4 hinzugefügt, der echte Fix war aber die Tag-Wait-Logik (v2.16.5)
- Reduziert Dateigrösse und vermeidet doppelte/vierfache Tag-Einträge in EXIF-Metadaten

## v2.17.2 — 2026-04-02

### Ordner-Tags als globales Modul

- Ordner-Tags fehlte als Modul im Dashboard und in den Einstellungen
- Neues Modul `ordner_tags` in `DEFAULT_MODULES`, Dashboard, Settings und Filewatcher
- Globaler Modul-Toggle deaktiviert Ordner-Tags auch wenn pro Inbox aktiviert
- Toggle in Einstellungen zwischen OCR und SMTP hinzugefügt
- i18n-Übersetzungen (DE/EN) für Modul-Beschreibung und Hinweistext

## v2.17.1 — 2026-04-02

### Bugfixes aus exotischen Tests

**GPS-Koordinaten bei Longitude/Latitude 0 ignoriert**
- `step_ia01_exif.py`: `bool(0)` war `False` → GPS am Äquator/Greenwich-Meridian wurde als "kein GPS" behandelt
- `step_ia03_geocoding.py`: `if not lat or not lon:` war falsy bei 0 → jetzt `if lat is None or lon is None:`
- GPS-Koordinaten werden jetzt validiert (lat: -90 bis 90, lon: -180 bis 180)

**Format/Extension-Mismatch verursacht Pipeline-Fehler**
- `step_ia07_exif_write.py`: Dateien mit falscher Extension (z.B. JPG als .png) werden jetzt erkannt
- ExifTool Write wird übersprungen statt mit Fehler abzubrechen
- Mismatch wird als "skipped" mit erklärender Meldung geloggt

**Settings-Save akzeptiert partielle/bösartige Formulardaten (kritisch)**
- Partielle POST-Requests konnten alle Module deaktivieren und Konfiguration löschen
- Neuer `_form_token` Guard: nur vollständige Formular-Submits werden verarbeitet
- Input-Sanitisierung gegen XSS (HTML-Escaping) für alle Text-Felder

## v2.17.0 — 2026-04-01

### Synology-Kompatibilität & neue Features

**Issue #11: Inbox-Ordner werden auf Synology nicht gelöscht**
- `@eaDir` (Synology Metadaten), `.DS_Store` (macOS), `Thumbs.db` (Windows) werden beim Aufräumen ignoriert
- Ordner mit nur diesen Systemdateien gelten als leer und werden gelöscht
- Auch nach Duplikat-Erkennung (IA-02) werden leere Inbox-Ordner jetzt aufgeräumt
- Filewatcher überspringt `@eaDir`, `.synology`, `#recycle` Verzeichnisse beim Scannen

**Issue #12: Ordnertag generiert kein Album in Immich**
- Album-Erstellung aus Inbox-Ordnerstruktur funktioniert jetzt auch im Webhook/Polling-Route
- Bisher wurde `upload_asset` im Webhook-Pfad ohne `album_names` aufgerufen

**Issue #13: Filter für Dateien die nicht verarbeitet werden sollen**
- Neuer Zieltyp "Überspringen" in den Sortier-Regeln
- Dateien die einer Skip-Regel entsprechen werden nicht verarbeitet und bleiben in der Inbox
- Übersprungene Dateien werden beim nächsten Scan nicht erneut aufgenommen
- Im UI als "⛔ Überspringen (nicht verarbeiten)" auswählbar (DE/EN)

**Issue #14: PNG-Bilder im Archiv nicht in Immich sichtbar**
- Fallback-Archiv-Logik für Kategorien ohne DB-Eintrag korrigiert: erkennt jetzt `sourceless_foto`/`sourceless_video` korrekt (vorher nur `sourceless`)

## v2.16.6 — 2026-04-01

### Bugfix: Immich-Tags auf Synology verloren (Wait-Logik verbessert)
- Wartet jetzt explizit bis Immich **Tags aus der Datei gelesen** hat (nicht nur Thumbnail/EXIF)
- Polling alle 3s, max 60s Timeout — reicht für langsame Systeme (Synology NAS)
- v2.16.5 wartete nur auf `thumbhash`+`make`, was auf Synology zu früh auslöste

## v2.16.5 — 2026-04-01

### Bugfix: Immich-Tags gehen nach Upload verloren
- IA-08 wartet jetzt bis Immich das Asset fertig verarbeitet hat (Thumbnail + EXIF), bevor Tags per API gesetzt werden
- Tags die Immich bereits aus der Datei (`TagsList`) gelesen hat, werden nicht nochmal per API gesetzt — keine Duplikate
- Ursache: Immich's Hintergrund-Verarbeitung überschrieb Tag-Zuordnungen die zu früh nach dem Upload gesetzt wurden

## v2.16.4 — 2026-04-01

### Bugfix: pHash-Berechnung für HEIC/HEIF
- HEIC/HEIF-Dateien erhalten jetzt einen pHash dank `pillow-heif` als Pillow-Plugin
- Bisher lieferte `Image.open()` einen Fehler für HEIC, und der ExifTool-Fallback war nur für RAW-Formate aktiv
- Neue Dependency: `pillow-heif>=0.18` in `requirements.txt`

## v2.16.3 — 2026-04-01

### EXIF: Zusätzliche XMP-Tag-Felder für Immich-Kompatibilität
- IA-07 schreibt Tags jetzt in **vier Felder**: `Keywords` (IPTC), `Subject` (XMP), `TagsList` (digiKam/Immich), `HierarchicalSubject` (Lightroom)
- Immich liest primär `TagsList` und `HierarchicalSubject` — diese fehlten bisher

## v2.16.2 — 2026-04-01

### Bugfix: Rollback bei fehlgeschlagenem Copy/Delete
- Wenn `copy_asset_metadata` oder `delete_asset` fehlschlägt, wird das neu hochgeladene Asset automatisch gelöscht — verhindert Duplikat-Loops im Polling-Mode
- Duplikat-Status von `upload_asset` wird geprüft bevor Copy/Delete ausgeführt wird

## v2.16.1 — 2026-04-01

### Bugfix: copy_asset_metadata API Felder
- Fix: Immich erwartet `sourceId`/`targetId` statt `from`/`to` im Copy-Endpoint

## v2.16.0 — 2026-04-01

### Immich: replace_asset durch Upload+Copy+Delete ersetzt
- **Deprecated `replace_asset()` entfernt**: `PUT /api/assets/{id}/original` wurde in Immich v1.142.0 deprecated und erzeugte `+1` Dateien auf Synology/btrfs
- **Neuer 3-Schritt-Workflow** für Polling-Mode (wie [lrc-immich-plugin PR #84](https://github.com/bmachek/lrc-immich-plugin/pull/84)):
  1. `upload_asset()` — getaggte Datei als neues Asset hochladen
  2. `copy_asset_metadata()` — Albums, Favoriten, Gesichter, Stacks vom alten auf neues Asset kopieren (`PUT /api/assets/copy`)
  3. `delete_asset()` — altes Asset löschen (`DELETE /api/assets` mit `force: true`)
- Kein `+1` Suffix mehr, keine verwaisten Dateien im Papierkorb

## v2.15.1 — 2026-04-01

### Bugfix: ExifTool auf Synology/btrfs
- **`-overwrite_original_in_place`** statt `-overwrite_original`: Bewahrt die Inode auf btrfs-Dateisystemen — verhindert dass Immich die Datei als neu erkennt (DELETE+CREATE → nur MODIFY Event)
- **`-P` Flag** hinzugefügt: Bewahrt Datei-Timestamps, reduziert unnötige Immich-Scan-Trigger
- Behebt Duplikat-Problem beim Betrieb auf Synology NAS

## v2.10.0 — 2026-03-31

### NSFW-Erkennung
- **KI erkennt nicht-jugendfreie Inhalte**: Neues `nsfw`-Feld in der KI-Antwort
- **Immich: Gesperrter Ordner**: NSFW-Assets werden automatisch in den gesperrten Ordner verschoben (`visibility: locked`)
- Funktioniert im Upload-Pfad (Inbox → Immich) und Polling-Pfad (Immich → Pipeline)
- Locked hat Vorrang vor Archivierung

### Ordner-Tags überarbeitet
- **Einzelwort-Tags**: Ordnernamen werden in Wörter aufgesplittet (`Ferien/Mallorca 2025/` → `Ferien`, `Mallorca`, `2025`)
- **Zusammengesetzter Tag**: Zusätzlich kombinierter Tag aus dem Gesamtpfad (`Ferien Mallorca 2025`)
- **`album:`-Prefix entfernt**: Kein `album:`-Prefix mehr in EXIF-Keywords

### Stabilität
- **Immich Polling-Loop behoben**: Nach `replace_asset` erhielt das Asset eine neue ID, was zu endloser Wiederverarbeitung führte. Jetzt wird SHA256-Hash nach Download geprüft
- **Reprocess-Verzeichnis**: Dateien werden nie zurück in die Inbox verschoben. Retry und Duplikat-Keep nutzen `/app/data/reprocess/`
- **Duplikat-Keep Fix**: file_hash wird auf gelöschten Group-Members genullt, damit IA-02 sie nicht erneut matcht

## v2.9.0 — 2026-03-31

### Video-Kategorien & Medientyp-Filter
- **Sorting Rules mit Medientyp-Filter**: Jede Regel kann auf Bilder, Videos oder Alle eingeschränkt werden — ermöglicht getrennte Regeln für Bilder und Videos
- **Separate Video-Kategorien**: `sourceless_foto`/`sourceless_video` und `personliches_foto`/`personliches_video` statt gemeinsamer Kategorien
- **Video Pre-Classification**: Videos erhalten korrekte Vorklassifikation (z.B. "Persönliches Video" statt "Persönliches Foto")
- **AI-Prompt für Videos**: Separate Beispiele für Bild- und Video-Quellen (Kameravideo, Drohnenvideo etc.)

### Video-Duplikaterkennung (pHash)
- **pHash aus Video-Frames**: Durchschnitts-pHash wird aus den IA-04 Thumbnail-Frames berechnet (kein zusätzlicher Rechenaufwand)
- **Re-encoded Videos erkannt**: Videos mit anderem Codec/Bitrate aber gleichem Inhalt werden als "similar" Duplikat erkannt
- **Post-IA-04 Check**: pHash-Duplikatprüfung läuft nach Frame-Extraktion als zweiter Check

### Duplikat-Review: Volle Pipeline beim Behalten
- **"Behalten" startet Pipeline nach**: Behaltene Duplikate durchlaufen die volle Pipeline (KI-Analyse, Tags schreiben, Sortierung/Immich-Upload) statt direkt verschoben zu werden
- **Funktioniert für alle Modi**: Lokale Ablage und Immich-Upload, Bilder und Videos

### Inbox-Garantie
- **Nichts bleibt unbeachtet**: Dateien die noch in der Inbox liegen werden immer verarbeitet — egal ob schon ein Done/Duplikat-Job existiert
- **Pipeline entscheidet**: Der Filewatcher ignoriert keine Dateien mehr; IA-02 erkennt Duplikate korrekt

### Stabilität
- **Retry-Counter**: Jobs die beim Container-Neustart in "processing" hängen, werden max. 3× versucht — danach Status "error" statt Endlosschleife
- **Config-Crash-Resilience**: Ungültiges JSON in Config-Werten führt nicht mehr zum Internal Server Error
- **Immich Tag-Fix**: HTTP 400 (statt nur 409) wird korrekt als "Tag existiert bereits" behandelt — alle Tags werden zugewiesen

### NSFW-Erkennung
- **KI-Prompt um `nsfw`-Feld erweitert**: Die KI erkennt nicht-jugendfreie Inhalte automatisch
- **Immich: Gesperrter Ordner**: NSFW-Bilder/Videos werden in den gesperrten Ordner verschoben (`visibility: locked`)
- **Locked hat Vorrang** vor Archivierung — ein NSFW-Bild wird nicht archiviert, sondern gesperrt
- Funktioniert sowohl im Upload-Pfad (Inbox → Immich) als auch im Polling-Pfad (Immich → Pipeline)

### Ordner-Tags
- **Einzelwort-Tags**: Ordnernamen werden in einzelne Wörter aufgesplittet (`Ferien/Spanien 2024/` → `Ferien`, `Spanien`, `2024`)
- **Zusammengesetzter Tag**: Zusätzlich wird ein kombinierter Tag aus dem gesamten Pfad erstellt (`Ferien Spanien 2024`)
- **`album:`-Prefix entfernt**: Tags enthalten kein `album:`-Prefix mehr

### Immich Polling Fix
- **Duplikat-Loop behoben**: Nach `replace_asset` erhält das Asset eine neue ID — der Poller erkannte es als "neues Asset" und verarbeitete es endlos. Jetzt wird auch der SHA256-Hash nach dem Download geprüft

### UI
- **"Jetzt scannen" und "Dry-Run Report" Buttons** nach oben neben Seitentitel verschoben

## v2.8.0 — 2026-03-31

### Dynamische KI-Klassifikation & DB-gesteuerte Kategorien
- **Statische Regeln primär, KI ergänzt**: Sortier-Regeln werden immer zuerst ausgewertet. Die KI prüft anschliessend ALLE Dateien und kann das Ergebnis korrigieren (z.B. ein persönliches Foto aus «Sourceless» retten)
- **Kategorien aus Datenbank**: Alle Kategorien (Ziel-Ablagen) kommen dynamisch aus der `library_categories`-Tabelle — keine hardcodierten Kategorie-Werte mehr im Code
- **KI-Prompt dynamisch**: Verfügbare Kategorien werden aus der DB geladen und dem AI-Prompt als Kontext übergeben, inkl. Vor-Klassifikation durch statische Regeln
- **Drei KI-Ausgabefelder**: `type` (Kategorie-Key aus DB), `source` (Herkunft wie Meme/Kamerafoto/Internetbild), `tags` (beschreibende Tags wie Landschaft, Tier, Haus)
- **Tag-Strategie überarbeitet**:
  - IA-07 schreibt AI-Tags + Source als EXIF-Keywords
  - IA-08 schreibt Kategorie-Label + Source als EXIF-Keywords
  - Keine doppelten Tags durch statische Regeln
- **Review-Seite dynamisch**: Klassifikations-Buttons werden aus der DB geladen statt hardcodiert
- **OCR Smart-Modus**: Verwendet AI `source`-Feld statt hardcodierter Typen für die Relevanzprüfung
- **Immich-Archivierung**: Pro Kategorie konfigurierbar in der Ziel-Ablage (DB-Feld `immich_archive`)
- **i18n aktualisiert**: Beschreibungen der Sortier-Regeln spiegeln den neuen Ablauf wider

## v2.7.0 — 2026-03-31

### Settings UI Redesign & EXIF Expression Engine
- **EXIF-Ausdrücke**: Neue Bedingung `exif_expression` für Sortier-Regeln mit Operatoren (`==`, `!=`, `~`, `!~`) und Verknüpfungen (`&` AND, `|` OR)
- **Nested-Form-Fix**: Delete/Add-Buttons in Einstellungen verwenden JavaScript statt verschachtelter HTML-Formulare
- **Immich-Archiv-Toggle**: Pro Ziel-Ablage konfigurierbar ob Dateien in Immich archiviert werden
- **Alte Bedingungen entfernt**: `exif_empty` und `exif_contains` durch `exif_expression` ersetzt

## v2.6.0 — 2026-03-31

### Schedule-Modus Enforcement
- **Zeitfenster-Modus**: Filewatcher verarbeitet nur innerhalb des konfigurierten Zeitfensters (z.B. 22:00–06:00), unterstützt Overnight-Fenster
- **Geplanter Modus**: Verarbeitung nur an bestimmten Wochentagen zu einer festen Uhrzeit (z.B. Mo–Fr 23:00)
- **Manueller Modus**: Keine automatische Verarbeitung — nur über "Jetzt scannen" Button im Dashboard
- **Kontinuierlich**: Wie bisher, 24/7 Verarbeitung
- Neuer API-Endpoint `POST /api/trigger-scan` für manuellen Scan-Trigger
- "Jetzt scannen" Button im Dashboard (funktioniert unabhängig vom Modus)

### Sortier-Regeln
- Editierbare Sortier-Regeln im Webinterface (Einstellungen → Sortier-Regeln)
- Bedingungen: Dateiname enthält, EXIF leer, EXIF enthält, Dateiendung
- Jede Regel mappt auf eine Zielkategorie (Foto, Video, Screenshot, Sourceless, Review)
- Reihenfolge per Pfeil-Buttons änderbar (erste Regel die matcht gewinnt)
- KI-Klassifikation hat immer Vorrang — Regeln greifen nur ohne KI-Ergebnis
- Standard-Regeln werden beim ersten Start geseedet

### HTML-Report nach Dry-Run
- Neuer Report unter Logs → "Dry-Run Report"
- Übersicht: Anzahl Dateien, Kategorien, Fehler, Duplikate, Review
- Aufschlüsselung nach Eingangsverzeichnis
- Vollständige Dateiliste mit Zielpfad und Status
- Fehlerübersicht mit Details

### Geocoding
- Photon und Google Maps API aus Auswahlmenü entfernt (verschoben auf v2)
- Nur noch Nominatim (OpenStreetMap) als Provider wählbar
- Backend-Code für Photon/Google bleibt erhalten für spätere Aktivierung

## v2.5.0 — 2026-03-30

### Performance-Optimierung für NAS-Betrieb (150k+ Dateien)
- **R1: Immich Streaming-Upload** — Dateien werden direkt von Disk gestreamt statt komplett in RAM geladen. Spart bei 500MB Video → 500MB RAM
- **R2: Dashboard 1 Query statt 6** — `GROUP BY status` statt 6 separate `COUNT`-Queries. Dashboard-JSON in ~22ms
- **R3: Duplikat-Erkennung optimiert** — pHash-Vergleich in Batches à 5000 statt ganze Tabelle (150k Rows) in RAM. Nur leichte Spalten geladen (`id`, `phash`, `debug_key`)
- **R4: Database-Indexes** — 7 Indexes auf `status`, `file_hash`, `phash`, `original_path`, `created_at`, `updated_at`, `system_logs.created_at`. Beschleunigt alle Queries massiv
- **R5: Docker Memory/CPU Limit** — `mem_limit: 2g`, `cpus: 2.0` in docker-compose.yml. NAS wird nicht mehr ausgelastet
- **R6: Temp-Cleanup** — `shutil.rmtree()` statt `os.rmdir()` bei fehlgeschlagenen Immich-Downloads. Keine Dateileichen mehr
- **R7: Log-Rotation** — System-Logs älter als 90 Tage werden automatisch gelöscht (stündliche Prüfung). DB wächst nicht mehr unbegrenzt
- **R8: safe_move optimiert** — Source-Datei wird nur noch 1× gelesen (Hash während Kopieren berechnet) statt 3× (Copy + Hash-src + Hash-dst). Spart 33% Disk-I/O

## v2.4.5 — 2026-03-30

### Security
- **Fix S1: Path Traversal Schutz** in `step_ia08_sort.py`, `review.py`
  - Neue Funktion `_sanitize_path_component()`: Entfernt `..`, `/`, `\` und Steuerzeichen aus EXIF-Werten (Country, City, Camera, Type) bevor sie in Pfade eingesetzt werden
  - Neue Funktion `_validate_target_path()`: Prüft mit `os.path.realpath()` dass der Zielpfad innerhalb der Bibliothek bleibt (Defense in Depth)
  - Geschützte Stellen: Pipeline IA-08 Sort, Review Classify, Review Classify-All
- **Fix S7: Dateigrössenlimit** in `filewatcher.py`
  - `MAX_FILE_SIZE = 10 GB` — Dateien über 10 GB werden übersprungen und geloggt
  - Verhindert Out-of-Memory bei extrem grossen Dateien
- **Fix S8: Immich Filename Sanitisierung** in `immich_client.py`
  - Neue Funktion `_sanitize_filename()`: Entfernt Path-Traversal-Muster (`../`, absolute Pfade) aus Immich-API-Dateinamen
  - Schützt `download_asset()` vor manipulierten `originalFileName`-Werten

## v2.4.3 — 2026-03-30

### Bugfix
- **Fix B10: Review-Status überschrieben**: Pipeline hat `job.status = "review"` (gesetzt von IA-08 für unklare Dateien) mit `"done"` überschrieben — UUID-Dateien ohne EXIF landeten im richtigen Verzeichnis (`unknown/review/`), aber mit Status "done" statt "review"

### Umfassendes E2E-Testing
- **Format-Tests**: PNG, HEIC, WebP, GIF, TIFF, MOV — alle Formate durch Pipeline verifiziert
- **Edge Cases**: Leere Dateien (abgewiesen), nicht unterstützte Formate (.txt abgewiesen), Dateinamenkollisionen (_1 Suffix), Screenshots (AI-Erkennung), kurze Videos (<1s, bekannte Limitation)
- **Modul-Disable**: AI, Geocoding, OCR einzeln deaktiviert — Pipeline läuft korrekt weiter mit Fallback-Werten
- **Job Retry/Delete**: Fehlgeschlagene Jobs wiederholt, gelöschte Jobs korrekt bereinigt
- **Review-System**: Einzelklassifikation und Batch-Classify-All verifiziert
- **Immich**: Ordner-Tags → Album-Erstellung, Sourceless-Archivierung bestätigt
- **Geocoding-Fehler**: Ungültige URL → nicht-kritischer Fehler, Pipeline fährt fort
- **Dry-Run**: Tags werden berechnet aber nicht geschrieben, Datei bleibt im Inbox
- **OCR**: Smart-Modus erkennt Screenshots, All-Modus verarbeitet alle Bilder
- **Blurry-Erkennung**: Unscharfe Bilder erhalten `blurry` Tag und Quality-Flag
- **Messenger-Dateien**: UUID-Dateinamen werden als sourceless erkannt, gehen in Review

## v2.4.2 — 2026-03-30

### Bugfixes aus E2E-Testing (DJI DNG, MP4, JPG+DNG Paare)
- **Fix: Video-Datumsformate**: ISO 8601 mit Mikrosekunden (`.000000`) und Timezone (`Z`, `+02:00`) werden jetzt korrekt geparst — Videos werden ins richtige Jahres-Verzeichnis sortiert statt ins aktuelle Datum
- **Fix: Filewatcher done_hashes**: Erkennt bereits verarbeitete Dateien zuverlässig — prüft dry_run-Jobs, Immich-Assets und Target-Existenz auf Dateisystem
- **Fix: Logging**: `logging.basicConfig()` in main.py — alle Pipeline-Logs erscheinen in Docker stdout (`docker logs`)
- **Fix: Pipeline-Fehler**: werden geloggt statt verschluckt (logger in `pipeline/__init__.py`)
- **Fix: ExifTool-Fehlermeldungen**: Bessere Fehlermeldungen bei korrupten/unlesbare Dateien
- **Fix: Kleine Bilder**: Bilder unter 16×16 Pixel werden von der KI-Analyse übersprungen (verhindert API-Fehler)
- **Fix: pHash-Threshold**: von 5 auf 3 gesenkt (weniger False Positives bei Duplikaterkennung)
- **Fix: Batch-Clean Label**: verdeutlicht, dass nur exakte SHA256-Duplikate automatisch bereinigt werden
- **UI: Preview-Badge**: Dry-Run-Jobs zeigen "Preview"-Badge in Log-Übersicht und Job-Detail

### Getestete Szenarien (DJI-Daten)
- DNG RAW-Dateien (25MB–97MB): EXIF, pHash aus Preview, Konvertierung, KI, Geocoding ✓
- MP4-Videos (57MB–304MB): ffprobe, Thumbnails, KI, Immich-Upload ✓
- JPG+DNG Paare: Paar-Erkennung (keep_both true/false) ✓
- Sonderzeichen in Dateinamen: Leerzeichen, Klammern ✓
- Alle Modi: Dateiablage, Immich, Dry-Run ✓
- Duplikat-Szenarien: SHA256, Cross-Mode, Keep/Delete, Batch-Clean ✓

## v2.4.0 — 2026-03-30

### JPG+RAW Paar-Erkennung
- **Konfigurierbares Verhalten**: Schalter in Einstellungen unter Duplikaterkennung
- **AN** (Standard): JPG + RAW werden beide unabhängig verarbeitet und übernommen
- **AUS**: Paare werden als Duplikat erkannt und landen im Review zur manuellen Auswahl
- Eigener "JPG+RAW" Badge in der Duplikat-Review-Seite

### Duplikat-Erkennung Verbesserungen
- **Fehlerhafte Jobs einbezogen**: SHA256- und pHash-Vergleich matcht jetzt auch gegen Jobs mit Status "error" — diese landen im Duplikat-Review statt automatisch verarbeitet zu werden
- **error_message Bereinigung**: Duplikat-Review setzt error_message korrekt auf NULL (verhindert doppelte Verarbeitung im Filewatcher)

### Filewatcher Stabilisierung
- **Hash-basierte Deduplizierung**: Nur noch erfolgreich abgeschlossene Jobs (done + kein Fehler) blockieren erneute Verarbeitung — fehlerhafte Dateien können erneut eingefügt werden
- **Vereinfachter Stabilitätscheck**: Einfache Dateigrössen-Prüfung nach 2s Wartezeit (robust bei Docker/SMB)

### Immich Upload Stabilität
- **Grosse Dateien**: Upload/Replace liest Datei komplett in Memory vor dem Senden (verhindert halbfertige DNG/RAW Uploads)
- **Separate Timeouts**: connect=10s, read=120s, write=300s für grosse Dateien (bis 10GB Videos)

### KI-Kontext im Log
- **IA-05 Detail-Ansicht**: Zeigt Modell, Anzahl Bilder, Metadaten-Kontext und KI-Antwort separat an
- Auto-Refresh erhält die formatierte Darstellung bei

### UI-Verbesserungen
- **Inbox-Pfade versteckt**: Temporäre Inbox-Pfade werden nie als Referenz angezeigt, "(Inbox — temporär)" Markierung im Job-Detail
- **Video-Thumbnails konfigurierbar**: Anzahl Frames (1–50) und Skalierung (25/50/75/100%) in Einstellungen
- Cache-Busting für JavaScript (v3)

## v2.3.0 — 2026-03-29

### Lightbox
- **Bild-Vollansicht**: Klick auf Thumbnail öffnet Originalbild als Fullscreen-Overlay (Review, Duplikate, Log-Detail)
- RAW/DNG: PreviewImage wird via ExifTool oder Immich-Preview extrahiert
- HEIC: wird zu JPEG konvertiert für Anzeige
- Schliessen mit ESC oder Klick auf Overlay

### Review-Seite
- **Löschen-Button** zum direkten Entfernen von Review-Dateien
- Dateigrösse wird via Immich API abgefragt (Fallback)
- Datum-Fallback auf FileModifyDate bzw. job.created_at
- Bildabmessungen (Auflösung) angezeigt
- Metadatenfelder bedingt angezeigt (Datum/Kamera nur wenn vorhanden)

### Duplikat-Review
- EXIF-Daten werden via Immich API geholt für Immich-Assets
- "Dieses behalten" Button auf allen Gruppenmitgliedern (nicht nur lokale Dateien)
- Badge (ORIGINAL/EXAKT) ist jetzt klickbarer Link (Immich → öffnet Immich, lokal → lädt Datei herunter)
- Keep-Aktion lädt Datei zu Immich hoch wenn Gruppe im Immich-Modus ist
- Immich-Delete repariert (httpx DELETE mit Request Body)

### Video-Verarbeitung
- **IA-01**: Video-Metadaten via ffprobe ergänzt ExifTool — Datum, GPS (ISO 6709 Parser), Dauer (roh + formatiert), Auflösung, Megapixel, Codec, Framerate, Bitrate, Rotation
- **IA-04**: Video-Thumbnail Extraktion via ffmpeg bei 10% der Dauer (vorbereitet, `VIDEO_THUMBNAIL_ENABLED = False`)

### Pipeline-Stabilität
- **Filewatcher**: Dateigrössen-Check nach 2s Wartezeit verhindert Verarbeitung halbkopierter Dateien
- **IA-07**: ExifTool `-m` Flag ignoriert kleinere Warnungen (z.B. DJI DNG "Maker notes")
- **IA-01**: Speichert file_size, Fallback auf FileModifyDate für Datum
- **httpx DELETE**: Korrektur — `json=` nicht unterstützt, stattdessen `client.request` mit `content=`

## v2.1.0 — 2026-03-29

### Pipeline-Optimierung
- **Neue Reihenfolge**: IA-01 EXIF → IA-02 Duplikate → IA-03 Geocoding → IA-04 Temp. Konvertierung → IA-05 KI → IA-06 OCR → IA-07–11
- Duplikaterkennung direkt nach EXIF (spart KI-Kosten bei Duplikaten)
- Geocoding vor KI-Analyse (Ortsdaten verbessern Klassifikation)
- Formatkonvertierung nur noch direkt vor KI (wird nur bei Bedarf ausgeführt)
- pHash-Fallback für RAW-Formate (DNG, CR2, NEF, ARW) via ExifTool PreviewImage

### Tags
- Mood-Tags (indoor/outdoor) entfernt — kein Nutzen als Keyword
- Quality nur noch bei `blurry` geschrieben
- OCR: einfaches `OCR` Flag statt `text:screenshot` etc.
- AI-Type bleibt als Tag erhalten

### Fehler-Logging
- Voller Python-Traceback bei allen Pipeline-Fehlern (kritisch, nicht-kritisch, Finalizer)
- Job-Detail zeigt Traceback in Monospace an
- System-Log Detail-Spalte mit Traceback

### Verarbeitungs-Log
- Zeit-Spalte direkt nach Debug-Key
- Verarbeitungsdauer angezeigt (z.B. "1m 23s")
- Datei und Ziel mit natürlichem Zeilenumbruch
- Fehler-Spalte kompakt mit Tooltip für Details

## v2.0.0 — 2026-03-29

### Review-System
- Neue Review-Seite für manuelle Klassifikation unklarer Dateien
- Kategorien: Foto, Video, Screenshot, Sourceless
- Immich-Integration: Sourceless → archiviert, andere bleiben in Timeline
- Batch-Aktion: alle Review-Dateien als Sourceless klassifizieren
- Alle unklaren Dateien gehen zu Review (keine automatische Sourceless-Zuordnung)

### Immich-Archivierung
- Screenshots und Sourceless werden automatisch in Immich archiviert (aus Timeline ausgeblendet)
- Fallback für ältere Immich-Versionen (isArchived vs. visibility API)

### KI-Optimierung
- Optimierter KI-Prompt mit allen gesammelten Metadaten (EXIF, Geocoding, Dateigrösse, Dateiname)
- Messenger-Erkennung (WhatsApp, Signal, Telegram) aus Dateiname
- UUID-Dateinamen als Messenger-Hinweis erkannt
- Dateigrösse als Klassifikations-Signal

### UI-Verbesserungen
- Review-Link in Navigation
- Zurück-Button in Job-Detail geht zu Verarbeitungs-Log (nicht System-Log)
- Immich-Thumbnail in Job-Detail-Seite

## v1.5.0

### Features
- Immich bidirektionale Integration (Upload + Polling)
- Immich-Alben aus Ordner-Tags
- Duplikat-Review mit Immich-Thumbnails
- OCR-Texterkennung (Smart/Alle Modi)
- Dry-Run Modus pro Inbox
- Ordner-Tags als EXIF-Keywords
- i18n (DE/EN)
- Dark/Light Theme

## v1.0.0

- Initial release
- 11-Step Pipeline
- Web Interface (Dashboard, Settings, Logs)
- EXIF, KI-Analyse, Geocoding, Duplikaterkennung
- SMTP Benachrichtigung
- Docker Deployment

# Known Risks

Ergebnis eines Logik- und Architektur-Reviews (Stand: 2026-04-20, durchgesehen 2026-09-02). Dokumentiert bekannte theoretische Risiken — **keine akuten Bugs**. Die meisten triggern nur in seltenen Crash-/Race-Szenarien. Kontext: Single-Admin-LAN-Setup, Server mit USV, kein Internet-Exposure.

Einträge, die inzwischen erledigt sind, bleiben mit ✅ und Fix-Version
stehen — die Begründung, warum es ein Risiko war, ist beim nächsten
ähnlichen Fall mehr wert als eine gelöschte Zeile.

**Grundsatz:** Nicht anfassen solange das System stabil läuft. Diese Liste dient als Orientierung, falls später gezielt refactorisiert wird.

---

## Kritisch (Datenverlust-Pfade)

### K-1 / K-2 — `safe_move` ohne fsync
**Datei:** `backend/safe_file.py:39-69`

Der Copy-Loop ruft vor `os.remove(src)` kein `f_out.flush()` + `os.fsync()` und kein Parent-Directory-fsync auf. Die Hash-Verifikation liest aus dem Page-Cache, nicht von Disk.

**Szenario:** Kernel-Panic / OOM-Kill / Container-Hard-Crash zwischen Copy und Writeback → Quelle gelöscht, Ziel leer/partiell.

**Entschärfung:** USV eliminiert Stromausfall. Realistisches Restrisiko: OOM-Kill / Docker-Crash.

**Fix (nicht priorisiert):** `f_out.flush(); os.fsync(f_out.fileno())` vor close, dann `os.fsync(dir_fd)` auf Parent-Dir, erst dann `os.remove(src)`.

---

### K-3 — `_handle_duplicate` ohne Rollback
**Datei:** `backend/pipeline/step_ia02_duplicates.py:485-509`

`safe_move` → `/duplicates/` passiert **vor** Commit. Crash dazwischen → Datei ist im Duplicates-Ordner, Job bleibt `processing`. Resume beim Startup setzt `status=queued` → IA-01 crasht mit FileNotFoundError.

**Fix (nicht priorisiert):** DB-Status zuerst auf `duplicate` setzen + committen, dann `safe_move`, dann `.log` schreiben. Oder Resume-Logik prüft Existenz von `original_path`.

---

### K-4 — Donor-Delete vor Commit in Duplicate-Resolution
**Datei:** `backend/routers/duplicates.py:861-908`

Donor-Files werden physisch via `safe_remove_with_log` + `delete_asset(force=True)` gelöscht **bevor** der Keep-Job-State persistiert wird. Commit-Fehler → Donor weg, Keep nicht korrekt verlinkt.

**Fix (nicht priorisiert):** Status-Änderungen committen, dann physische Löschung.

---

### K-5 — XMP-Sidecar-Überschreibung ohne Merge
**Datei:** `backend/pipeline/step_ia07_exif_write.py:264-269`

ExifTool `-o` überschreibt existierende `.xmp`-Sidecar ohne User-eingetragene Tags zu mergen. Widerspricht `keep-local-xmp`-Prinzip.

**Szenario:** User kuratiert Tags via `ma-sidecar-repair`, nächster MediaAssistant-Retry verwirft sie.

**Fix (nicht priorisiert):** `-tagsfromfile existing.xmp` vor Regenerierung oder Merge-Modus.

---

### K-7 — Size-Only-Overwrite in IA-08
**Datei:** `backend/pipeline/step_ia08_sort.py:470-487`

`overwrite_existing=True` wenn nur Dateigröße gleich — kein Hash-Vergleich. Zwei verschiedene Files mit zufällig identischer Byte-Größe am selben Zielpfad → eine wird silent gelöscht.

**Fix (nicht priorisiert):** Immer Hash vergleichen wenn target existiert. Die Optimierung "skip hash bei gleicher Grösse" spart wenige ms und riskiert Datenverlust.

---

### K-8 — `delete_asset(force=True)` hardcoded — ✅ schärfster Pfad entschärft (v2.32.5)
**Datei:** `backend/immich_client.py`

Umgeht Immich-Trash. Keine Recovery möglich. Insbesondere relevant in Kombination mit K-4.

**Was tatsächlich passiert ist:** `safe_upload_asset` behandelte einen
Upload, den Immich als Duplikat abwies, wie einen eigenen Fehlschlag und
rief `delete_asset(force=True)` auf die zurückgegebene ID — das ist aber
die ID des **bestehenden** Assets. Ein zweiter Upload eines Bildes, das
schon in Immich lag, konnte so das Original permanent löschen.

`AssetMediaResponseDto` hat ein Pflichtfeld `status` mit `created` oder
`duplicate`. Das wird jetzt gelesen; `orphan_id` wird nur bei
`status != "duplicate"` gesetzt.

**Rest-Risiko (nicht priorisiert):** `force=False` als Default, explizite
Opt-in-Pfade.

---

## Hoch (Inkonsistenter State)

### H-1 — Pipeline-Worker-Selektion ohne Lock
**Datei:** `backend/filewatcher.py:800-813`

`SELECT queued LIMIT N` ohne `FOR UPDATE SKIP LOCKED`. Der atomic claim in `pipeline/__init__.py:52` fängt Doppelvergabe ab, aber Worker startet leere asyncio-Tasks.

**Impact:** Nur Performance-Noise, kein Datenproblem.

---

### H-2 — `_poll_immich` blockt Filewatcher-Loop
**Datei:** `backend/filewatcher.py:339-381`

Serielle `await run_pipeline(job.id)`-Aufrufe im Poll-Loop. 100 neue Assets → ganzer Filewatcher steht bis alle durch sind. Inbox-Scan, CSV-Retry pausieren.

**Fix (nicht priorisiert):** Poller soll nur queuen, Pipeline-Worker nimmt auf.

---

### H-3 — Immich-Poll ohne Overlap-Buffer — ⚠️ ist eingetreten (v2.32.4)
**Datei:** `backend/filewatcher.py`

`last_poll = now` ohne Overlap. Clock-Skew zwischen MediaAssistant-Host und Immich-Server droppt Assets im Grenzbereich.

**Der teure Teil war ein anderer:** Der Cursor wurde **auch nach einem
gescheiterten Abruf** weitergestellt. Damit übersprang der Poller genau
die Assets, deren Abruf fehlgeschlagen war — in Produktion vier Wochen
Handy-Uploads, die nie in die Pipeline kamen. Ein `poll_failed`-Guard
verhindert das jetzt; die Lücke wurde per Cursor-Rückdrehung über die
ganze Historie aufgeholt.

**Rest-Risiko (gering):** Der Clock-Skew-Overlap fehlt weiterhin. Dedup
via `already_by_id` existiert.

---

### H-6 — LIKE-Query auf `step_result` JSON
**Datei:** `backend/routers/duplicates.py:1165-1168, 1186`

`Job.step_result.like(f'%"original_debug_key": "{debug_key}"%')`. Full-Table-Scan ohne Index, LIKE-Metazeichen nicht escaped.

**Fix (nicht priorisiert):** Separate normalisierte Tabelle `job_duplicate_links` oder SQLAlchemy JSON-Operatoren.

---

### H-7 — Folder-Tags splitten Whitespace
**Datei:** `backend/pipeline/step_ia02_duplicates.py:442-450`

Ordner `Ferien Spanien 2024` erzeugt Tags `["Ferien", "Spanien", "2024", "Ferien Spanien 2024"]`. Pure Zahlen als Tag sind rauschend.

**Fix (nicht priorisiert):** Explizit entscheiden (combined ODER split, nicht beides), Zahlen-only filtern.

---

### H-8 / H-9 — `retry_count` wird nie zurückgesetzt
**Datei:** `backend/pipeline/__init__.py:327-438`

Weder manueller Retry noch erfolgreicher Lauf setzen `retry_count` zurück. Ein einmal-staler Job wird beim zweiten Fehler früher abgebrochen als erwartet.

**Fix (nicht priorisiert):** Reset bei `done` / manual retry.

---

### H-11 — Geocoding ohne Provider-Fallback-Chain
**Datei:** `backend/pipeline/step_ia03_geocoding.py:222-236`

Nur ein konfigurierter Provider pro Job. README suggeriert Nominatim → Photon → Google Chain, Code hat das nicht.

**Fix (nicht priorisiert):** Provider-Chain in Config.

---

### H-12 — AI-Tags ohne Validierung — 🔶 teilweise entschärft (v2.32.x)
**Datei:** `backend/pipeline/step_ia05_ai.py` → IA-07

AI-Response-Tags wurden 1:1 in EXIF/Immich geschrieben. Keine Max-Länge, keine Whitelist, keine Profanity/Sprach-Filter. `ma-ghost-tag-detect` ist Post-hoc-Workaround.

**Was jetzt greift:** `is_unusable_keyword` verwirft an der Quelle, was
offensichtlich Ausschuss ist — leere Wörter, Wörter ohne einen einzigen
Buchstaben (`?????`, `---`, reine Zahlen) und Schrift-Mischungen wie
`Sunset夕日`. Verworfenes wird geloggt.

Für das, was schon in Sidecars und Immich liegt, gibt es das Register
**Werkzeuge**: Löschen nach Muster und Zusammenführen von Schreibweisen.

**Rest-Risiko (nicht priorisiert):** Max-Länge und inhaltliche Whitelist
fehlen weiterhin. Ein sinnvolles, aber falsches Wort erkennt kein Filter
— dafür bleibt `ma-ghost-tag-detect`.

Die KI-Stufe für Synonyme und Englisch → Deutsch ist
[Issue #45](https://git.marcohediger.ch/MediaAssistant/ma-core/issues/45).

---

## Mittel / Niedrig (Auswahl)

### N-6 — `_run_job` Exception-Fallback fehlt
**Datei:** `backend/filewatcher.py:257-264`

Exception in `_run_job` wird gelogt, aber Job-Status bleibt `processing` bis Stale-Recovery (15 min).

**Fix (gering):** Bei Exception `job.status="error"` setzen.

**Issue:** siehe Tracker.

---

### N-7 — `_scan_directory` folgt Symlinks
**Datei:** `backend/filewatcher.py:99-119`

`os.walk` default folgt Symlinks. Symlink-Loop in Inbox → Stack-Overflow möglich.

**Fix (gering):** `followlinks=False`.

**Issue:** siehe Tracker.

---

### M-3 — Zeitzone-Handling inkonsistent
**Datei:** `backend/file_operations.py:187-210`

`parse_date` stripped TZ-Info → nachgelagerte Pfad-Resolution verwendet lokale Container-TZ. Wenn Container-TZ von User-TZ abweicht, landen Grenzfälle in falschem Monat/Jahr.

**Fix (nicht priorisiert):** Container-TZ-Config `TZ=Europe/Zurich` sicherstellen (ist im README dokumentiert).

---

### M-5 — `done_hashes` als `(path, hash)`-Tupel
**Datei:** `backend/filewatcher.py:166-189`

Datei umbenannt und wieder in Inbox → Check greift nicht → unnötige AI-Kosten. Duplicate-Detection in IA-02 fängt's ab.

**Fix (nicht priorisiert):** Als `set[hash]` (Pfad egal für Entscheidung).

---

### M-7 — Sorting-Rules `&`/`|` ohne Klammern-Support
**Datei:** `backend/pipeline/step_ia08_sort.py:278-323`

Regel-Ausdrücke wie `a & b | c` werden implizit gruppiert. Admin-Config-Stolperfalle.

**Fix (nicht priorisiert):** Parser + Klammern oder klare Doku.

---

### N-5 — `STALE_TIMEOUT_S = 15*60` ohne Heartbeat
**Datei:** `backend/filewatcher.py:668`

Lange Video-Konvertierungen in IA-04 können als stale markiert werden obwohl sie laufen.

**Fix (nicht priorisiert):** Steps committen periodisch Heartbeat-Ticks.

---

### N-9 — Toter `orphan`-Status
**Datei:** `backend/models.py:18`

Status-Wert `orphan` in Enum, aber nirgends gesetzt. Nur in Filtern.

**Fix (gering, aufräumen):** Entfernen oder dokumentieren wann er gesetzt werden soll.

---

## Neu erkannt (2026-09-02)

### S-1 — Immich legt gelöschte Tags aus der Sidecar neu an
**Betrifft:** jede Tag-Operation gegen Immich

Immich schreibt Tag-Namen in die XMP-Sidecar des Assets. Wird ein Tag in
Immich gelöscht, steht der Name aber weiter in der Datei, legt der
nächste Einlesevorgang das Tag **neu an — mit neuer ID**. An einer
laufenden Instanz reproduziert, Datei im Zugriff:

    <rdf:li>Teststrand</rdf:li>
    <rdf:li>TESTSTRAND</rdf:li>

Das ist kein Bug in MediaAssistant und lässt sich von aussen nicht
abstellen. Konsequenz für jede künftige Tag-Funktion: **Immich allein
anzufassen genügt nie.** Aufräumen und Zusammenführen haben deshalb
beide Schalter, Sidecar-Hälfte voreingestellt an.

### S-2 — Hintergrund-Tasks starben lautlos
**Datei:** `backend/main.py` — ✅ behoben (v2.32.9)

Ein NameError in `start_filewatcher` legte Scannen, Pollen und
Verarbeiten still. Der Task starb beim Start, die App lief weiter, das
Dashboard zeigte nichts an — **drei Releases lang, ohne eine einzige
Meldung.** Gefunden wurde es erst über die Diagnose-API.

`_supervise` hüllt jetzt jeden Hintergrund-Task ein und schreibt seinen
Tod in die DB.

**Die eigentliche Lehre:** Die Migrationen waren unit-getestet, der
Startpfad selbst nie. Ein Test, der `start_filewatcher` wirklich
aufruft, hätte das in einer Sekunde gefunden.

### S-3 — Werkzeuge ändern die Library in grossem Umfang
**Datei:** `backend/routers/tools.py`

Löschen und Zusammenführen fassen tausende XMP-Dateien und Immich-Tags in
einem Lauf an. Es gibt kein Undo.

Abgesichert ist das durch: Pflicht-Vorschau, serverseitige Prüfung, dass
Muster und Schalter beim Ausführen exakt der Vorschau entsprechen (sonst
HTTP 409), Abbruch-Flag in jeder Schleife, und Protokoll vor und nach
jedem Lauf.

**Nicht abgesichert:** ein zu weit gefasster regulärer Ausdruck, den der
Nutzer in der Vorschau durchwinkt. Die Vorschau zeigt Trefferzahl und
Beispiele — sie zu lesen bleibt Teil der Bedienung.

### S-4 — Immichs Index hinkt Schreibvorgängen nach
**Betrifft:** Tests, nicht Produktion

Direkt nach `tag_assets` melden `count_tag_assets` und `list_tag_assets`
noch `0`; nach ~2 s stimmt es. Ein Test ohne Wartezeit misst sich selbst
und produziert Phantom-Fehler.

---

## Architektur-Schulden (strukturell, kein Bug)

- **A-1** Keine formale State-Machine für Job-Status — Transitions ad-hoc über Codebase verteilt.
- **A-2** `step_result` JSON-Spalte ohne Schema. Sentinel-Keys mit `_`-Präfix als Workaround.
- **A-3** Keine Pipeline-Parallelisierung. IA-02 / IA-03 / IA-04 hängen alle nur von IA-01 ab, könnten parallel laufen.
- **A-4** Kein exponentieller Backoff für Retries.
- **A-6** `filewatcher.py` ist 830-Zeilen-Gott-File (Scan + Poll + Worker + Recovery + Scheduler).
- **A-7** `_resolve_duplicate_group` ist 370+ Zeilen Business-Logic im Router.
- **A-8** SQLite Query-Plan für Duplicate-View ungetestet.
- **A-9** Kein globaler Filesystem-Healthcheck auf Inbox/Library-Mount.
- **A-10** E2E-Tests decken keine Failure-Injection (Kill während Pipeline-Run).

---

## Empfohlene Low-Risk-Fixes (Issues erstellt)

1. **N-6** — `_run_job` Exception-Fallback setzt `status="error"`
2. **N-7** — `_scan_directory` mit `followlinks=False`
3. **H-3** — Immich-Poll `last_poll` mit 5-min-Overlap-Buffer

Alle drei sind ~1-3 Zeilen, rein defensiv, isoliert testbar.

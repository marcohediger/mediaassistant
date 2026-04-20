# Known Risks

Ergebnis eines Logik- und Architektur-Reviews (Stand: 2026-04-20). Dokumentiert bekannte theoretische Risiken — **keine akuten Bugs**. Die meisten triggern nur in seltenen Crash-/Race-Szenarien. Kontext: Single-Admin-LAN-Setup, Server mit USV, kein Internet-Exposure.

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

### K-8 — `delete_asset(force=True)` hardcoded
**Datei:** `backend/immich_client.py:666-684`

Umgeht Immich-Trash. Keine Recovery möglich. Insbesondere relevant in Kombination mit K-4.

**Fix (nicht priorisiert):** `force=False` default, explizite Opt-in-Pfade.

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

### H-3 — Immich-Poll ohne Overlap-Buffer
**Datei:** `backend/filewatcher.py:382`

`last_poll = now` ohne Overlap. Clock-Skew zwischen MediaAssistant-Host und Immich-Server droppt Assets im Grenzbereich.

**Fix (gering):** `last_poll = now - timedelta(minutes=5)`. Dedup via `already_by_id` existiert.

**Issue:** siehe Tracker.

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

### H-12 — AI-Tags ohne Validierung
**Datei:** `backend/pipeline/step_ia05_ai.py:384-413` → IA-07

AI-Response-Tags werden 1:1 in EXIF/Immich geschrieben. Keine Max-Länge, keine Whitelist, keine Profanity/Sprach-Filter. `ma-ghost-tag-detect` ist Post-hoc-Workaround.

**Fix (nicht priorisiert):** Server-Side Tag-Validation (max-len, Regex-Whitelist).

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

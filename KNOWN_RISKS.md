# Known Risks

Ergebnis eines Logik- und Architektur-Reviews (Stand: 2026-04-20, durchgesehen 2026-09-02 und 2026-10-29 gegen v3.1.3). Dokumentiert bekannte theoretische Risiken — **keine akuten Bugs**. Die meisten triggern nur in seltenen Crash-/Race-Szenarien. Kontext: Single-Admin-LAN-Setup, Server mit USV, kein Internet-Exposure.

Einträge, die inzwischen erledigt sind, bleiben mit ✅ und Fix-Version
stehen — die Begründung, warum es ein Risiko war, ist beim nächsten
ähnlichen Fall mehr wert als eine gelöschte Zeile.

Die Datenverlust-Risiken des Umbaus „Module & Datensicherheit" (3.0) —
R-01 … R-26, je mit Reproducer in `test_data_safety_local.py` bzw.
`test_data_safety_immich.py` — stehen in
[`docs/UMBAU_MODULE.md`](docs/UMBAU_MODULE.md). Dieser Umbau hat die
Kritisch-Einträge unten geschlossen.

**Grundsatz:** Nicht anfassen solange das System stabil läuft. Diese Liste dient als Orientierung, falls später gezielt refactorisiert wird.

---

## Kritisch (Datenverlust-Pfade)

### K-1 / K-2 — `safe_move` ohne fsync — ✅ behoben (v3.0.0)
**Datei:** `backend/fileops.py`

Der Copy-Loop rief vor `os.remove(src)` kein `fsync` auf, weder für die
Datei noch für den Ordner. Bei einem Kernel-Panic, OOM-Kill oder
Container-Absturz zwischen Kopie und Writeback konnte die Quelle weg und
das Ziel leer sein.

**Jetzt:** Jede Verschiebung läuft über `fileops.move`: kopieren, Datei
und Zielordner per `fsync` festschreiben, SHA-256 am Ziel prüfen, erst
dann die Quelle entfernen (und deren Ordner festschreiben).

---

### K-3 — `_handle_duplicate` ohne Rollback — ✅ behoben (v3.0.0)
**Datei:** `backend/pipeline/step_ia02_duplicates.py`

Die Verschiebung in den Duplikat-Ordner lief **vor** dem Commit. Ein
Absturz dazwischen liess die Datei im Duplikat-Ordner und den Job auf
`processing`; die Wiederaufnahme scheiterte dann in IA-01 mit
FileNotFoundError.

**Jetzt:** IA-02 verschiebt über `fileops.move`, das die Absicht vor der
Operation ins Protokoll `file_ops` schreibt. Beim Start gleicht
`recovery.reconcile_file_operations()` offene Absichten gegen Platte und
Immich ab, bevor unterbrochene Jobs wieder eingereiht werden.

---

### K-4 — Donor-Delete vor Commit in Duplicate-Resolution — ✅ behoben (v3.0.0)
**Datei:** `backend/routers/duplicates.py`

Spender-Dateien wurden physisch gelöscht (`safe_remove_with_log`,
`delete_asset(force=True)`), **bevor** der Zustand des behaltenen Jobs
gespeichert war. Scheiterte der Commit, war der Spender weg und der
behaltene Job nicht richtig verknüpft.

**Jetzt (R-04, R-09, R-10a):** `_resolve_duplicate_group` beansprucht
zuerst alle Mitglieder atomar (`resolving`, Plan im Job), prüft das
behaltene Exemplar, schickt Spender nur in den Papierkorb und schreibt den
Endzustand in einem Commit. Nach einem Absturz führt
`resume_interrupted_resolutions()` den Plan erneut aus; jeder Schritt darf
zweimal laufen.

---

### K-5 — XMP-Sidecar-Überschreibung ohne Merge — ✅ behoben (v3.0.0)
**Datei:** `backend/pipeline/step_ia07_exif_write.py`

ExifTool `-o` überschrieb eine vorhandene `.xmp` und verwarf dabei vom
Nutzer eingetragene Tags.

**Jetzt (R-17a):** In eine vorhandene, lesbare Sidecar werden die
Stichwörter von MediaAssistant nur ergänzt; Bewertung, Label, eigene
Stichwörter und eine eigene Beschreibung bleiben. Nur eine Datei, die gar
kein XMP ist (die binären Bild-Kopien des v2.28.13-Bugs), kommt in den
Papierkorb und wird neu erzeugt.

---

### K-7 — Size-Only-Overwrite in IA-08 — ✅ behoben (v3.0.0)
**Datei:** `backend/pipeline/step_ia08_sort.py`, `backend/fileops.py`

Bei gleicher Dateigrösse am Zielpfad wurde ohne Hash-Vergleich
überschrieben. Zwei verschiedene Dateien mit zufällig gleicher Grösse —
eine ging still verloren.

**Jetzt (R-05, R-06):** `fileops` überschreibt nie; ein belegter Name
bekommt einen Zähler. Wo IA-08 den Inhalt eines Ziels nicht sehen kann,
wird nicht blind überschrieben.

---

### K-8 — `delete_asset(force=True)` hardcoded — ✅ behoben (v2.32.5, v3.0.0)
**Datei:** `backend/immich_client.py`, `backend/fileops.py`

`force=True` umgeht den Immich-Papierkorb, nichts ist wiederherstellbar.

**Was tatsächlich passiert ist (v2.32.5):** `safe_upload_asset` behandelte
einen Upload, den Immich als Duplikat abwies, wie einen eigenen
Fehlschlag und rief `delete_asset(force=True)` auf die zurückgegebene
ID — das ist aber die ID des **bestehenden** Assets. Seither wird das
Pflichtfeld `status` (`created` / `duplicate`) gelesen; `orphan_id` wird
nur bei `status != "duplicate"` gesetzt.

**Seit v3.0.0:** Weggeräumt wird nur über `fileops`, und das schickt
Immich-Assets ausschliesslich in den Papierkorb von Immich
(`force=false`). `test_no_direct_file_ops.py` meldet jeden direkten
Aufruf ausserhalb von `fileops`.

**Aufgeräumt (v3.2.0):** `safe_upload_asset` und `safe_replace_asset`,
die letzten Nutzer von `force=True`, sind entfernt — produktiv rief sie
niemand mehr auf. `delete_asset` hat keine Vorgabe mehr für `force`; wer
endgültig löschen will (nur Tests, für ihre eigenen Assets), muss es
ausschreiben.

---

## Hoch (Inkonsistenter State)

### H-1 — Pipeline-Worker-Selektion ohne Lock — ✅ erledigt (v3.0.0)
**Datei:** `backend/worker.py`

`SELECT queued LIMIT N` ohne Sperre; mehrere Aufrufer konnten dieselben
Jobs wählen, der atomic claim fing die Doppelvergabe ab.

**Jetzt:** `worker.py` ist der einzige Ort, der `run_pipeline` startet.
Er wählt nur Jobs, die nicht schon laufen (`notin_(busy)`), bis zu den
freien Plätzen; der atomic claim (`UPDATE … WHERE status='queued'`) bleibt
die Absicherung.

---

### H-2 — `_poll_immich` blockt Filewatcher-Loop — ✅ behoben (v3.0.0)

Der Poller lud jedes neue Asset herunter und liess die Pipeline darauf
laufen, eines nach dem anderen, innerhalb der Scan-Schleife — solange
wurde keine Inbox gescannt. Seit dem Umbau auf Verarbeitungs-Sets reiht
`inputs.immich.poll_set` nur noch Jobs ein (`original_path =
"immich-pending:<id>"`); der Worker lädt das Original beim Start des
Jobs (`worker.fetch_immich_source`). Allgemein gilt jetzt: Eingänge
reihen nur ein, verarbeitet wird ausschliesslich im Worker.

---

### H-3 — Immich-Poll ohne Overlap-Buffer — ✅ behoben (v2.32.4, v3.0.0)
**Datei:** `backend/inputs/immich.py`

`last_poll = now` ohne Overlap. Clock-Skew zwischen MediaAssistant-Host
und Immich-Server konnte Assets im Grenzbereich auslassen.

**Der teure Teil war ein anderer (v2.32.4):** Der Cursor wurde **auch
nach einem gescheiterten Abruf** weitergestellt. Damit übersprang der
Poller genau die Assets, deren Abruf fehlgeschlagen war — in Produktion
vier Wochen Handy-Uploads, die nie in die Pipeline kamen. Ein
`poll_failed`-Guard verhindert das; die Lücke wurde per
Cursor-Rückdrehung über die ganze Historie aufgeholt.

**Seit v3.0.0:** Der Cursor wird um `OVERLAP` (5 Minuten) zurückgesetzt;
schon bekannte Assets erkennt der Eingang über das Register.

---

### H-6 — LIKE-Query auf `step_result` JSON — ✅ erledigt (v3.0.0)
**Datei:** `backend/routers/duplicates.py`

`Job.step_result.like(f'%"original_debug_key": "{debug_key}"%')` —
Full-Table-Scan, LIKE-Metazeichen nicht escaped. Die Abfrage gibt es
nicht mehr; die Gruppen der Duplikat-Seite entstehen aus den
Duplikat-Beziehungen der Mitglieder.

---

### H-7 — Folder-Tags splitten Whitespace — ✅ erledigt (v3.0.0)
**Datei:** `backend/folder_path.py`

Ordner `Ferien Spanien 2024` erzeugte Tags `["Ferien", "Spanien", "2024",
"Ferien Spanien 2024"]`.

**Jetzt:** Die Ordner-Optionen sind pro Set wählbar (`none`, `flat`,
`nested`, `legacy`); siehe
[`docs/IMMICH_ORDNERSTRUKTUR.md`](docs/IMMICH_ORDNERSTRUKTUR.md). Nur
`legacy` teilt weiterhin in Wörter auf — bewusst, damit migrierte Sets
dieselben Tags schreiben wie vorher.

---

### H-8 / H-9 — `retry_count` wird nie zurückgesetzt — ✅ behoben (v2.31.3, v3.0.0)
**Datei:** `backend/pipeline/__init__.py`

Ein einmal hängengebliebener Job wurde beim nächsten Hänger früher
aufgegeben als erwartet. Jetzt setzt ein Lauf, der mit `done` oder
`review` endet, den Zähler zurück (v2.31.3), ebenso ein vom Nutzer
ausgelöster Retry (v3.0.0).

---

### H-11 — Geocoding ohne Provider-Fallback-Chain
**Datei:** `backend/pipeline/step_ia03_geocoding.py`

Pro Job wird genau der eine konfigurierte Anbieter gefragt (Nominatim,
Photon oder Google); eine Kette mit Ausweichanbieter gibt es nicht. Das
README verspricht inzwischen auch keine mehr.

**Entschärfung:** Fällt der Anbieter aus, pausiert
`GeocodingConnectionError` die Pipeline, und der Health-Watcher setzt sie
fort, sobald er wieder antwortet. Es gehen keine Jobs verloren, sie
warten.

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
Issue #45 (Gitea), auf GitHub #55.

---

## Mittel / Niedrig (Auswahl)

### N-6 — `_run_job` Exception-Fallback fehlt — ✅ behoben (v2.31.3)
**Datei:** `backend/worker.py`

Eine Exception ausserhalb der eigenen Fehlerbehandlung von `run_pipeline`
liess den Job bis zur Stale-Recovery auf `processing`. Jetzt setzt
`_run_job` ihn auf `error` und loggt den Absturz; ein „Neu ausführen"
kehrt stattdessen zu seinem vorherigen Status zurück.

---

### N-7 — `_scan_directory` folgt Symlinks — ✅ behoben (v2.31.3)
**Datei:** `backend/inputs/common.py`

`os.walk` folgte Symlinks; eine Symlink-Schleife in der Inbox hätte den
Scan endlos laufen lassen. Der Scan geht jetzt mit `os.scandir` und
überspringt Symlinks.

---

### M-3 — Zeitzone-Handling inkonsistent
**Datei:** `backend/file_operations.py` (`parse_date`)

`parse_date` entfernt die Zeitzone → die Pfad-Auflösung verwendet die
lokale Container-TZ. Weicht die Container-TZ von der des Nutzers ab,
landen Grenzfälle im falschen Monat/Jahr.

**Fix (nicht priorisiert):** Container-TZ-Config `TZ=Europe/Zurich` sicherstellen (ist im README dokumentiert).

---

### M-5 — Bekannte Dateien als `(Pfad, Hash)`-Paar
**Datei:** `backend/inputs/inbox.py`

Der Scan überspringt eine Datei nur, wenn Pfad **und** Hash schon einem
erledigten Job gehören. Eine umbenannte Datei, die wieder in die Inbox
kommt, wird neu eingereiht → unnötiger KI-Lauf. IA-02 erkennt sie als
exaktes Duplikat.

**Fix (nicht priorisiert):** Entscheidung nur nach Hash.

---

### M-7 — Sorting-Rules `&`/`|` ohne Klammern-Support
**Datei:** `backend/pipeline/step_ia08_sort.py` (`_eval_exif_expression`)

`|` bindet schwächer als `&` (`a & b | c` = `(a & b) | c`); Klammern gibt
es nicht. Admin-Config-Stolperfalle.

**Fix (nicht priorisiert):** Parser + Klammern oder klare Doku.

---

### N-5 — `STALE_TIMEOUT_S = 15*60` ohne Heartbeat — ✅ behoben (v3.0.0)
**Datei:** `backend/worker.py`, `backend/pipeline/__init__.py`

Lange Video-Konvertierungen konnten als hängend gelten, obwohl sie
liefen.

**Jetzt (R-11):** Jeder Lauf berührt seinen Job alle `HEARTBEAT_S`. Als
hängend gilt ein Job erst, wenn eines seiner laufenden Module länger als
15 Minuten läuft und keine Dateioperation von ihm offen ist; Warten auf
ein Betriebsmittel zählt nicht.

---

### N-9 — Toter `orphan`-Status — ✅ trifft nicht zu
**Datei:** `backend/routers/api.py`

Der Status wird seit v2.28.11 gesetzt: Die Wartung
`/api/jobs/cleanup-orphans` (`_scan_orphans_in_background`) markiert
erledigte Jobs (`done`, `duplicate`, `review`), deren Inhalt
weder lokal noch in Immich mehr existiert, und merkt sich den vorherigen
Status für die Rücknahme.

---

## Neu erkannt (2026-10-29)

### S-9 — Fehlt das KI-Modell, landet alles in Review, ohne Pause — ✅ behoben (v3.2.0)
**Datei:** `backend/services.py`, `backend/pipeline/executor.py`

Auf dem KI-Server wurde das Modell gewechselt; die Dev-Konfiguration
verlangte weiter das alte. Der Server antwortet dann mit HTTP 400
„model not found". Das ist ein `AIResponseError`, also eine weiche
Warnung in IA-05 — kein `AIConnectionError`, der die Pipeline pausieren
würde. Jedes Bild lief durch und landete als „Unbekannt / Review".
Aufgefallen ist es nur, weil die E2E-Suite rot wurde (13 von 147
Prüfungen).

In Produktion hiesse das: Nach einem Modellwechsel auf dem KI-Server
füllt sich still die Review-Liste, bis jemand die Einstellung anpasst.
Die Erreichbarkeitsprüfung sieht nur, ob der Server antwortet.

**Jetzt — allgemein für jeden Dienst der Pipeline:** Scheitert ein Modul,
das KI, Geocoding oder Immich braucht, fragt der Executor die Prüfung des
Dienstes. Die KI-Prüfung verlangt, dass das eingestellte Modell in `/models`
steht. Ist der Dienst nicht bereit, hält die Pipeline mit Grund an; der Job,
der es bemerkt hat, wartet wie alle anderen in der Warteschlange — ein
Problem der Infrastruktur wird nie einem Job angelastet. Davor landete
dieser Job als Fehler in `error/`, ein Immich-Ausfall pausierte gar nicht,
OCR und ein unbrauchbarer Geocoder liefen still mit Warnungen weiter.
E2E US-23 … US-25.

### S-10 — Immich 3.x stellt alte Tags wieder her — ✅ entschärft (v3.1.0)
**Betrifft:** IA-08 und „Tags in Gruppen umstellen" gegen Immich

Nach vielen Tag-Änderungen schreibt Immich 3.1/3.3 unter Last teils einen
veralteten Stand zurück — Tags, die MediaAssistant gerade entfernt hat,
hängen danach wieder am Asset. Gemessen an der Dev-Instanz, siehe
[`docs/IMMICH_ORDNERSTRUKTUR.md`](docs/IMMICH_ORDNERSTRUKTUR.md).

**Entschärfung:** IA-08 kontrolliert nach dem Schreiben, was am Asset
hängt, korrigiert und prüft erneut (höchstens 3 Runden). Das kostet
rund 1,8 s pro Job.

## Neu erkannt (2026-10-22)

### S-6 — Einrichtungs-Assistent nach der Einrichtung offen — ✅ behoben (v2.32.24)
**Datei:** `backend/routers/setup.py`

`/setup` ist von der Anmeldung ausgenommen. Nach der Einrichtung
leiteten nur die GET-Seiten weiter; die POST-Schritte nahmen ohne
Anmeldung Änderungen an KI-Backend-URL/-Schlüssel, SMTP und Ablagepfad
an. Jetzt sperrt eine Router-Abhängigkeit den ganzen Assistenten.

**Lehre:** Jede Ausnahme in `EXEMPT_PREFIXES` braucht eine eigene
Zugangsprüfung im Router — die Middleware schützt dort nichts mehr.

### S-7 — Umgebungsvariablen wirken nur noch beim ersten Start
**Datei:** `backend/config.py`

`FILEWATCHER_*`, `OCR_MODE`, `METADATA_WRITE_MODE`,
`METADATA_GOOGLE_JSON` und `LIBRARY_BASE_PATH` füllen weiterhin die
alten globalen Schlüssel. Seit den Verarbeitungs-Sets liest die Pipeline
diese aber nicht mehr; sie gehen nur bei der ersten Migration in das Set
„Standard" ein (`LIBRARY_BASE_PATH` zusätzlich als Vorgabe für neue
Sets). Wer sie danach ändert, sieht keine Wirkung — das steht im README.

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

### S-8 — Unfixierte Abhängigkeiten — ✅ festgenagelt (v2.32.25)
**Datei:** `backend/requirements.txt`, `backend/constraints.txt`

`requirements.txt` gab nur Untergrenzen vor; jeder Build zog die neuesten
Releases. v2.32.24 bekam SQLAlchemy 2.1 ohne `greenlet` und startete
nicht. Seit v2.32.25 legt `constraints.txt` jede Version fest. Aufgefallen
ist es am Startpfad-Test des veröffentlichten Images (S-5) — vor dem
Update der Produktion.

### S-5 — Ein Release kann ausgeliefert werden, ohne startfähig zu sein
**Datei:** `backend/version.py` — ✅ Wache eingebaut (v2.32.23)

v2.32.22 ging mit einer null Byte grossen `version.py` raus; der
Container stürzte beim Start endlos ab. Verursacht hat es das Hochzählen
der Version:

    open(v, "w").write(open(v).read().replace(alt, neu))

Python wertet das äussere `open(v, "w")` zuerst aus — die Datei ist auf
null gekürzt, bevor das innere `read()` läuft.

**Die Lücke war nicht der Einzeiler, sondern dass es niemandem auffiel.**
Alle Tests waren grün, weil kein einziger den Startpfad importiert.
Dasselbe Muster wie bei S-2. `test_startup_imports.py` schliesst das:
`version.py`, `main.py`, `pipeline`, `filewatcher` — mehr nicht.

**Regel:** in einem Ausdruck nie gleichzeitig lesen und schreiben.

### S-3 — Werkzeuge ändern die Library in grossem Umfang
**Datei:** `backend/routers/tools.py`

Löschen und Zusammenführen fassen tausende XMP-Dateien und Immich-Tags in
einem Lauf an. Seit v3.1.0 gehört „Tags in Gruppen umstellen" dazu, das
über alle Jobs eines Sets läuft. Es gibt kein Undo.

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

- **A-1** Keine formale State-Machine für Job-Status — Transitions ad-hoc über Codebase verteilt. Die kritischen Übergänge sind atomic claims (`UPDATE … WHERE status = …`).
- **A-2** 🔶 v3.0.0: Jeder Modul-Lauf ist eine Zeile in `module_runs`; `jobs.step_result` ist nur noch die abgeleitete Sicht, die allein `runs.py` schreibt (Wächter `test_no_direct_step_result.py`). Die Ergebnisse selbst sind weiterhin JSON ohne Schema, Sentinel-Keys mit `_`-Präfix bleiben.
- **A-3** 🔶 v3.0.0: Reihenfolge und Abhängigkeiten stehen nur in `pipeline/registry.py`; IA-03 läuft als Seiten-Task neben IA-04, jedes Modul hält nur sein Betriebsmittel (`resources.py`). Weitere Module laufen noch nacheinander.
- **A-4** Kein exponentieller Backoff für Pipeline-Retries (hängende Jobs werden bis zu dreimal neu eingereiht). Uploads nach Immich haben einen eigenen Backoff.
- ~~**A-6** `filewatcher.py` ist 830-Zeilen-Gott-File (Scan + Poll + Worker + Recovery + Scheduler).~~ ✅ v3.0.0: zerlegt in `inputs/`, `worker.py`, `recovery.py`; `filewatcher.py` ist nur noch der Taktgeber.
- **A-7** 🔶 `_resolve_duplicate_group` ist seit v3.0.0 in feste Schritte zerlegt (beanspruchen, prüfen, Papierkorb, behaltener Job, Endzustand), die Business-Logik liegt aber weiterhin im Router (`routers/duplicates.py`, rund 1950 Zeilen).
- **A-8** SQLite Query-Plan für Duplicate-View ungetestet.
- **A-9** Kein globaler Filesystem-Healthcheck auf Inbox/Library-Mount. Die Set-Liste zeigt nur an, ob der Basis-Pfad existiert.
- **A-10** 🔶 Die Reproducer in `test_data_safety_local.py` / `test_data_safety_immich.py` und US-18 simulieren Abstürze an festen Stellen (z. B. mitten im Ersetzen in Immich, R-10e) samt Wiederaufnahme. Ein echtes Abschiessen des Prozesses während eines Laufs fehlt weiterhin.

---

## Empfohlene Low-Risk-Fixes

Die drei Kandidaten von 2026-04-20 sind erledigt: N-6 und N-7 in v2.31.3,
der Overlap-Puffer von H-3 in v3.0.0; S-9 in v3.2.0.

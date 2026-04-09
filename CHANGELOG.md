# Changelog

## v2.28.44 — 2026-04-09

### Feature: Qualitaetsbasierte Duplikat-Erkennung (#46)

IA-02 bevorzugt bei Duplikaten jetzt die Datei mit der besten Qualitaet
als Original. Wenn ein Duplikat erkannt wird (SHA256-exakt oder pHash-
aehnlich) und die **neue** Datei bessere Qualitaet hat als die
existierende, werden die Rollen getauscht: die existierende wird zum
Duplikat degradiert, die neue laeuft weiter als Original.

Qualitaets-Score (absteigend nach Prioritaet):
1. **Format**: RAW (5) > HEIC (4) > TIFF (3) > JPEG (2) > PNG/WebP (1)
2. **Aufloesung**: width x height (mehr Pixel = besser)
3. **Dateigroesse**: groesser = weniger komprimiert
4. **Metadaten-Reichtum**: EXIF, GPS, Datum, Kamera, Software
   (Tiebreaker bei sonst gleicher Qualitaet)

Die degradierte Datei wird in `/library/error/duplicates/` verschoben
und referenziert den neuen Original-Job in ihrem `step_result['IA-02']`.

Bei Immich-Assets bleibt die Referenz (`immich:<asset_id>`) erhalten;
der Status wird auf `duplicate` gesetzt.

Tests: 34/34 Duplikat-Tests PASS (inkl. Test 10: Quality-Swap).

Refs #46

## v2.28.43 — 2026-04-09

### Fix: Zirkulaere Duplikat-Erkennung bei Retry

Wenn ein Job retried wurde, konnte IA-02 ihn als Duplikat seines
**eigenen** Duplikats markieren. Szenario:

1. Job A verarbeitet `IMG.HEIC` aus der Inbox → Upload nach Immich → done
2. Immich-Poller findet dasselbe Asset → Job B → IA-02 erkennt korrekt:
   "B ist Duplikat von A" → B landet in `/library/error/duplicates/`
3. User retried Job A → Nuclear Retry droppt alle Steps → IA-02 laeuft
   neu → findet Job B (Status `duplicate`, aber Datei existiert unter
   `/library/error/duplicates/`) → markiert A als Duplikat von B

**Problem:** IA-02 hat Jobs mit `status='duplicate'` als gueltige
"Originale" akzeptiert. Ein Duplikat ist per Definition selbst eine
Kopie und darf nie als Referenz fuer eine Duplikat-Erkennung dienen.

**Fix:** `duplicate` aus dem Status-Filter in allen 4 IA-02-Suchqueries
entfernt (SHA256-exakt, RAW+JPG-Paar, pHash exact, pHash near).
Nur `done`, `review`, `processing` und `error` Jobs werden als
potentielle Originale betrachtet.

Live-Vorfall: MA-2026-28103 (IMG_2499.HEIC) — nach Retry als Duplikat
von MA-2026-48417 markiert, obwohl 48417 selbst das Duplikat war.

## v2.28.42 — 2026-04-09

### Fix: Retry im Sidecar-Mode liefert frische XMP nach Immich

Beim Retry eines bereits in Immich vorhandenen Assets hat IA-08 im
Sidecar-Mode bisher den Re-Upload uebersprungen (`step_ia08_sort.py:529`),
weil die Annahme war: "Originaldatei unveraendert, nur API-Tags
aktualisieren." Die API-Tags wurden zwar korrekt gesetzt, aber die
`.xmp`-Sidecar-Datei im Immich-Storage blieb die alte/veraltete Version.

Da MediaAssistant keinen direkten Dateizugriff auf den Immich-Storage
hat und Immichs eigener SidecarWrite-Mechanismus `dc:subject` (Keywords)
nicht zurueckschreibt (nur `digiKam:TagsList`, getestet auf Immich v2.6.3),
gibt es keinen anderen Weg die frische Sidecar nach Immich zu bringen
als ein vollstaendiger Re-Upload.

**Loesung:** Bei einem Retry (`retry_count > 0`) macht IA-08 jetzt auch
im Sidecar-Mode den Upload+Copy+Delete-Workflow — identisch zum
Direct-Mode, aber mit `sidecar_path` im `upload_asset()`-Aufruf:

1. `upload_asset(original_path, sidecar_path=sidecar_path)` → neues
   Asset mit frischer Sidecar
2. `copy_asset_metadata(old, new)` → Albums, Favorites, Faces, Stacks
3. `delete_asset(old)` → altes Asset entfernen

**Konsequenz:** Die Asset-ID aendert sich beim Retry. Shared Links auf
das alte Asset brechen. Fuer einen Retry-Vorgang (Ausnahmefall, nicht
Massenoperation) ist das akzeptabel.

**Erste Verarbeitung** (kein Retry) bleibt unveraendert: Sidecar-Mode
taggt nur via API, kein Re-Upload.

Refs #44

## v2.28.41 — 2026-04-08

### Cleanup-Tool: praezise Container-Brand-Erkennung

`cleanup_broken_sidecars.py` hat den ISO-BMFF-Detection-Pfad
(`head[4:8] == b"ftyp"`) bisher pauschal als "HEIF container"
gemeldet, obwohl genau dieselben Bytes auch fuer MOV, MP4, AVIF
und Co. verwendet werden — der v2.28.13-Bug hat schliesslich auch
HEIC-/MOV-/MP4-Sources zu binaeren `.xmp`-Klonen gemacht.

Der Reason-String unterscheidet die Familien jetzt anhand des
Brand-Codes (Bytes 8..11):

- `heic`/`heix`/`heim`/`heis`/`hevc`/`hevx` → `HEIC binary`
- `mif1`/`msf1` → `HEIF binary`
- `qt  ` → `MOV (QuickTime) binary`
- `mp41`/`mp42`/`mp4 `/`isom`/`iso2`/`iso4`/`iso5`/`dash`/`M4V*`/`f4v ` → `MP4 binary`
- `avif`/`avis` → `AVIF binary`
- alles andere → `ISO-BMFF binary`

**Reine Kosmetik im Reporting** — die Detection-Entscheidung
(`return True` fuer alle ISO-BMFF-Brands) bleibt 1:1 identisch,
keine echten XMP-Dateien werden faelschlich getroffen. Verifiziert
mit 10 Test-Payloads (JPEG, alle ISO-BMFF-Brands, echte XMPs).

## v2.28.40 — 2026-04-08

### Fix: XMP-Sidecars waren seit v2.28.13 binäre JPEG/HEIC-Klone statt Text-XML

User: "@…01bb4f56-…jpg.xmp sind die daten so in ordnung?". Nach
Inspektion: nein — die Datei war 250 KB groß und begann mit
`\xff\xd8\xff\xe0` (JPEG SOI). Eine echte XMP-Sidecar ist Text-XML
(`<?xpacket … <x:xmpmeta …`) und ~2 KB groß.

**Root cause:** v2.28.13 (`dca6437`) führte das atomic-write-Pattern
für Sidecars ein:

```python
tmp_sidecar = f"{sidecar_path}.{job.debug_key}.tmp"  # FALSCH
exiftool -o {tmp_sidecar} … {original}
os.replace(tmp_sidecar, sidecar_path)
```

ExifTool entscheidet anhand der **Endung** des `-o`-Targets, was es
schreibt. `.xmp` → Text-XML-Sidecar. **Alles andere** (`.tmp`,
`.tmp.something`, …) → "kopiere die Source-Datei und betten XMP
ein", d.h. ein vollständiger JPEG/HEIC-Klon mit XMP-Block landet
unter dem Tempfile-Namen — wird dann atomic auf `foo.jpg.xmp`
verschoben. So entstanden 27 Monate lang riesige Binärdateien
mit `.xmp`-Endung statt echter Sidecars.

**Fix in `step_ia07_exif_write.py:228`:**

```python
# IMPORTANT: temp file MUST end in `.xmp`
tmp_sidecar = f"{job.original_path}.{job.debug_key}.tmp.xmp"
```

Plus ausführlicher Kommentar warum die Endung kritisch ist.

### Fix: Immich Tag-Asset Race — zweiter PUT verschwindet ~1s später

Beim Stuck-State-Test (`STUCK_STALE_GEO`) blieb der Tag hartnäckig
am Asset, obwohl `untag_asset()` "untagged" zurückgab. Isolierter
Test mit zwei rapid `PUT /api/tags/{id}/assets`-Calls hat die
Ursache identifiziert: Immich verarbeitet die Tag-Association
asynchron und der zweite PUT "isst" den ersten, wenn beide innerhalb
weniger Millisekunden eintreffen.

**Fix in `step_ia08_sort.py:_tag_immich_asset()`:**

1. `IMMICH_TAG_WRITE_DELAY_S = 0.1` Sekunden Pause zwischen
   sequentiellen Tag-Operationen.
2. **Verify-Retry:** nach allen PUTs/DELETEs wird das Asset noch
   einmal via `GET /api/assets/{id}` gelesen. Tags die "gegessen"
   wurden landen erneut im PUT, stale Tags die wieder aufgetaucht
   sind landen erneut im DELETE.

### Tooling: `cleanup_broken_sidecars.py`

Neues Standalone-Cleanup-Script (`backend/cleanup_broken_sidecars.py`)
um die durch den v2.28.13-Bug erzeugten korrupten `.xmp`-Dateien
aufzuspüren und zu löschen. Erkennungs-Heuristik: liest die ersten
32 Bytes jeder `.xmp`-Datei. Echte XMP beginnt mit `<?xpacket`,
`<x:xmpmeta`, `<?xml` oder `<rdf:`. JPEG (`\xff\xd8\xff`), HEIF
(`ftyp` an Offset 4), PNG und andere Binär-Formate werden als
"broken" markiert.

**Usage:**

```bash
# Dry-run (nur reporten)
docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py /library

# Wirklich löschen
docker exec mediaassistant-dev python /app/cleanup_broken_sidecars.py --delete /library
```

Nach dem Löschen die betroffenen Jobs retryen — IA-07 schreibt
dann eine korrekte Text-XMP.

### Test-Coverage

`test_retry_file_lifecycle.py` hat einen neuen Regression-Guard:
nach jedem Sidecar-Write wird verifiziert, dass die ersten Bytes
XML-Marker enthalten und die Datei <50 KB ist. So bleibt der
v2.28.13-Bug künftig unmöglich.

110/110 Lifecycle-Tests grün, 26/26 Duplicate-Tests grün.

## v2.28.39 — 2026-04-08

### Fix: Retry entfernt jetzt die alten Tags aus Immich bevor neue geschrieben werden

User: "das retry funktioniert jetzt aber es müssen zuerst noch die
alten tags entfernt werden". Stimmt — die `_tag_immich_asset()`
Funktion fügte bisher nur neue Tags **hinzu**, entfernte aber keine
stale Tags, die im vorherigen Lauf geschrieben wurden. Nach einem
Retry landeten die neuen Tags zusätzlich auf dem Asset, die alten
(z.B. `'unknown'` von einem Pre-Classification-Stall) blieben
kleben.

**Fix in drei Teilen:**

1. **`immich_client.untag_asset()`** — neuer Helper der via
   `DELETE /api/tags/{tag_id}/assets` die Tag-Asset-Beziehung
   entfernt. Nicht-existente Tags gelten als No-op.

2. **`pipeline/__init__.py:reset_job_for_retry()`** — vor dem
   Nuclear-Drop des step_result wird die bisherige
   `IA-08.immich_tags_written`-Liste aufgehoben und per
   `inject_steps={"_retry_previous_immich_tags": [...]}` in den
   frisch gestarteten Pipeline-Run mitgegeben. Der führende
   Underscore sorgt dafür dass der Pipeline-Main-Loop
   (`if step_code in existing_results: continue`) die Sentinel-
   Liste ignoriert — nur IA-08 liest sie explizit aus.

3. **`pipeline/step_ia08_sort.py`** — `_tag_immich_asset()` bekommt
   einen neuen `previous_tags`-Parameter und gibt jetzt ein
   3-Tupel zurück: `(tags_written, tags_failed, tags_removed)`.
   Nach dem normalen Tagging-Loop wird jeder Tag, der in
   `previous_tags` war aber NICHT im neuen `tag_keywords`-Set,
   via `untag_asset()` entfernt. **Wichtig:** nur Tags aus der
   previous-Liste werden angefasst — Tags die der User manuell in
   der Immich-UI hinzugefügt hat bleiben unangetastet.

   Beide Aufrufer in `step_ia08_sort.py:execute()` (webhook-Branch
   + first-upload-Branch) übergeben die Sentinel weiter und
   schreiben das Resultat in `IA-08.immich_tags_removed`. Der
   Sentinel wird am Ende aus dem step_result entfernt, damit er
   beim nächsten Lauf nicht als "from previous retry" wieder
   auftaucht.

**Test-Coverage:** `_run_stuck_state_retry_test` (existierend)
wurde erweitert. Vor dem Retry werden die stale Tags jetzt nicht
nur in der DB-step_result eingetragen, sondern **wirklich via
API** auf den Immich-Asset geschrieben. Die bestehenden
Immich-API-Asserts prüfen danach dass sie echt aus Immich
entfernt wurden — nicht nur aus der DB-Sicht.

Pre-Retry-Asserts (neu):
- "pre-retry, stale 'unknown' is really on the Immich asset" ✓
- "pre-retry, stale 'STUCK_STALE_GEO' is really on the Immich asset" ✓

Post-Retry-Asserts (bestehend, jetzt aussagekräftig):
- "Immich-API confirms 'unknown' is gone" ✓
- "Immich-API confirms 'STUCK_STALE_GEO' is gone" ✓

108/108 grün (`test_retry_file_lifecycle.py`), 26/26
(`test_duplicate_fix.py`).

**Was du jetzt tun solltest:** nach `docker compose pull` und
Restart (v2.28.39) einmal auf deine problematischen Jobs (MA-2026-
28111, -28115, -28121, ...) "Erneut verarbeiten" klicken. Diesmal:
- v2.28.37 Nuclear-Retry droppt IA-01..IA-11 und läuft alle Steps frisch
- v2.28.39 vergleicht die neuen Tags mit den alten und **entfernt** die stale
- In Immich bleiben nur die echten, neuen Tags über

## v2.28.38 — 2026-04-08

### Performance: `_tag_immich_asset` Wait-Loop komplett entfernt

User-Frage: "wenn man Tags direkt via API schreibt, braucht man den
Wait überhaupt?" Richtig — die Wait-Schleife war von Anfang an eine
Mikro-Optimierung: sie wartete darauf dass Immich die Tags aus der
XMP-Sidecar selbst extrahiert, um doppelte API-Calls zu vermeiden.

Die Rechnung war nie zugunsten der Optimierung:
- Gesparte API-Calls: ~10 pro Job × ~50ms = **~0.5s**
- Kosten im Worst-Case: **bis zu 120s pro Job** (Wait-Timeout wenn
  Immich die Tags nicht parst)

Zusätzlich ist die Immich Tag-API `POST /api/tags` auf Tag-Namen
**idempotent** — duplicate POSTs werden mit 400/409 abgewiesen und
vom bestehenden `tag_asset()`-Code abgefangen. Doppelte Calls sind
also harmlos, nicht einmal warnings-würdig.

**Fix:** `_wait_for_immich_tags()` ist weg. `_tag_immich_asset()`
macht jetzt:
1. Einen einzigen GET `get_asset_info()` (nur für Dedup-Reporting —
   unterscheiden zwischen "schon da" und "neu geschrieben")
2. Für jeden fehlenden Tag direkt POST via API

Kein Poll, kein Sleep, kein Timeout. IA-08 upload-to-tagged ist
jetzt von der Anzahl API-Calls bounded statt von einem Wait-Timer.

**Messung** (gegen echtes Dev-Immich, voller Test-Lauf):

| Version | Total | Avg pro Immich-Job |
|---|---|---|
| v2.28.32 (vor perf-work) | 401s | ~130s (R5/R6) |
| v2.28.36 (max_wait 120s → 15s) | 230s | ~22s (R5/R6) |
| **v2.28.38 (Wait raus)** | **180s** | **~8s (R5/R6)** |

Gesamt-Speedup v2.28.32 → v2.28.38: **55% schneller pro Job, 2.2×
höherer Durchsatz**. Live sollte der User von ~2-3 Files/Min (in
der Durchsatz-Regression ab 2026-04-08 ~16:00) zurück auf **6+
Files/Min** kommen.

**Test:** 106/106 grün (`test_retry_file_lifecycle.py`), 26/26
(`test_duplicate_fix.py`).

**Offene Architektur-Frage für v2.28.39+:** im Modus
`use_immich=True` könnte IA-07's lokale Sidecar-Erzeugung komplett
entfallen und alle Tags nur via Immich-API geschrieben werden.
Würde IA-07 vollständig skippen, pro Job nochmal ~10s sparen
(kein ExifTool-Call). Nachteil: Tags nur noch in Immichs DB, nicht
in einer lokalen .xmp. Bruch des v2.18.0-Versprechens "Sidecar-
Mode = file-hash preserving mit lokaler Tag-Preservation".
Zurückhalten bis User-Entscheidung.

## v2.28.37 — 2026-04-08

### Fix: Nuclear retry — drop ALL step results, period (15 stuck Live-Jobs)

User-Aussage: "es kann doch nicht so schwer sein, beim retry einfach
alle alten daten zu verwerfen und das einfach neu abzuarbeiten".
Hat komplett recht. Meine v2.28.33-36 cascade-Logik war zu schlau
und scheiterte an einem Live-Zustand mit 15 betroffenen Jobs:

- `status='done'`
- `error_message=None`  ← gecleart durch einen früheren partiellen Retry
- `step_result.IA-05` = success **ohne** status field (frische Klassifikation)
- `step_result.IA-07/IA-08` = stale, mit `'unknown'` aus der Pre-Fix-Zeit

In dem Zustand griff KEIN Cascade:
- by-status (v2.28.33): IA-05 hat keinen warning-Status
- by-error_message (v2.28.35): error_message ist None
- Selbst wenn der User auf "Erneut verarbeiten" klickte, refused
  das Retry-Endpoint die Annahme, weil `is_warning` nur für
  `error_message LIKE "Warnungen in:%"` matched. Auf der Detail-
  Page war der Button gar nicht sichtbar wenn `error_message=None`.
- Die Jobs hingen für immer fest. Nichts konnte sie reparieren.

**Drei zusammenhängende Fixes:**

1. **Nuclear drop in `pipeline/__init__.py:reset_job_for_retry()`**:
   `drop_step_codes = {IA-01, IA-02, ..., IA-11}`. Komplett alles.
   Keine Cleverness, keine Cascade-Versuche, keine Optimierungen.
   IA-01 (EXIF) ist deterministisch und schnell — kein Verlust.

2. **Atomic-Claim erweitert** in `reset_job_for_retry()`: akzeptiert
   jetzt jeden terminal-Status (`error`, `done`, `review`,
   `duplicate`, `skipped`, `orphan`). Nur `queued`/`processing`
   werden abgelehnt (Race-Schutz).

3. **Retry-Endpoint + Detail-Page-Button** in `routers/api.py` und
   `templates/log_detail.html`: der "Erneut verarbeiten"-Button
   ist jetzt für jeden non-running Job sichtbar und das Endpoint
   akzeptiert ihn. Damit können die 15 stuck-Live-Jobs gefixt
   werden ohne SQL-Migration — User klickt einmal auf jeden,
   fertig.

**Test-Coverage:** neuer Test `_run_stuck_state_retry_test`
reproduziert MA-2026-28115/28121 exakt:
- Job mit `status='done'`, `error_message=None`
- IA-05 success-without-status, IA-07/IA-08 mit stale `'unknown'`
- Trigger Retry → muss akzeptiert werden, IA-07/IA-08 müssen
  fresh sein, Immich-API-Verifikation der echten Tags

Pre-v2.28.37 wäre der Test rot (reset_job_for_retry refused).
Post-Fix: **106/106 grün** (`test_retry_file_lifecycle.py`),
26/26 (`test_duplicate_fix.py`).

**Was du jetzt tun solltest:**
1. `docker compose pull && docker compose up -d` auf live (v2.28.37)
2. Auf jede der 15 stuck-Jobs (MA-2026-28111, -28115, -28121, ...)
   "Erneut verarbeiten" klicken — diesmal wird ALLES neu verarbeitet
3. Neue Tags landen sauber in Immich

Falls du eine Liste der betroffenen Jobs willst, kann ich die aus
der Live-DB extrahieren — alle Jobs mit `IA-05.type != 'unknown'`
aber `'unknown' in IA-07.keywords_written`. Das ist die exakte
Smoking-Gun-Signatur.

## v2.28.36 — 2026-04-08

### Performance: `_wait_for_immich_tags` max_wait von 120s → 15s + konfigurierbar

User-Beobachtung in den Test-Läufen: trotz v2.28.34 (Skip-Wait wenn
IA-07 nichts schreibt) brauchten R5/R6-Tests immer noch **128-129
Sekunden pro Job** — exakt der `max_wait=120s`-Timeout aus
`_wait_for_immich_tags`.

**Ursache:** mein v2.28.34 Skip-Wait greift nur wenn
`keywords_written=[]`. Aber sobald IA-07 EGAL WAS schreibt — auch
ein einzelnes Wort wie "unknown" — läuft der Wait. Wenn Immich
diese Keywords aus irgendeinem Grund nicht extracten kann
(falsches XMP-Format, langsame Worker-Queue, große Datei,
unsupported Codec), läuft der Poll bis zum vollen Timeout. Im
Live-System passiert das genauso wie im Test, nur dass der User
es im einzelnen Job-Background nicht merkt.

**Fix:** `max_wait` Default von 120s auf **15s** reduziert. Plus
**konfigurierbar** via Config-Setting `immich.tag_wait_max_seconds`:

- `15` (Default): poll 5× im 3s-Intervall
- `0`: kompletter Skip — direkt zum API-Tagging
- größere Werte: für extrem langsame Setups (Synology mit ML-
  Worker-Queue), via Settings-UI oder DB änderbar

Begründung für 15s als Default: der Original-Commit-Kommentar
behauptete "30-90s, especially for PNG files" auf Synology — aber
in der Praxis (gemessen auf dev mit echtem Immich) extrahiert
Immich entweder innerhalb der ersten 3-6s oder gar nicht. Wenn es
nach 15s noch nichts hat, ist die Wahrscheinlichkeit dass es noch
passiert minimal — und der Fallback auf API-Tagging übernimmt es
sauber, weil die Immich-Tag-API auf Tag-Namen-Ebene idempotent ist
(duplicate POST → 400/409, vom Code abgefangen).

**Messung im Test-Lauf:**

| | Vor v2.28.36 | Nach v2.28.36 | Speedup |
|---|---|---|---|
| R5 (Immich+direct error retry) | 128s | 22s | 6× |
| R6 (Immich+sidecar error retry) | 129s | 22s | 6× |
| **Total `test_retry_file_lifecycle.py`** | **401s** | **230s** | **43%** |

99/99 grün, 26/26 grün im Regression-Suite.

## v2.28.35 — 2026-04-08

### Fix: Cascade-Drop griff nicht wenn IA-05 keinen `status: warning` mehr hatte (Live-Vorfall MA-2026-28121)

User-Bericht nach v2.28.34: Retry für MA-2026-28121 lief durch,
Detail-Page zeigte korrekt das frische KI-Resultat
(`type: 'Persönliches Foto'`, Tags Metallteil/Maschinenteil/...),
**aber IA-07.keywords_written und IA-08.immich_tags_written hatten
weiterhin die alten `["unknown", "Schweiz", "Aargau", "Lupfig"]`
drinstehen.** In Immich landeten also die alten stale Tags, nicht
die neue KI-Analyse.

**Ursache:** v2.28.33 cascade-droppt nur Steps deren `status` in
`{"error", "warning"}` ist. Aber ein erfolgreicher IA-05-Lauf
returnt ein dict OHNE `status` field — `IA-05.status` ist nur
gesetzt, wenn der Pipeline-Error-Handler den Step nach einer
Exception markiert hat.

Im Live-Fall MA-2026-28121: Vorgänger-Retries (vor v2.28.33) liefen
IA-05 frisch durch und überschrieben die "warning"-Markierung mit
einem erfolgreichen Result OHNE status field. IA-07/IA-08 blieben
aber stale (weil das pre-cascade-Code IA-07/IA-08 nicht droppte).
Die `error_message="Warnungen in: IA-05"` blieb stehen, weil sie
beim ersten warning-Run aggregiert wurde und kein späterer Lauf
sie korrigieren konnte.

Konsequenz: jeder spätere Retry mit v2.28.33 cascade fand bei
IA-05 keinen warning-Status mehr, droppte gar nichts, IA-05/06/07/08
blieben alle gecacht. Die alten Tags klebten für immer am Job.

**Fix in `pipeline/__init__.py:reset_job_for_retry()`:** parst
`error_message="Warnungen in: IA-XX, IA-YY"` und gibt die
gefundenen Step-Codes als neuen Parameter `drop_step_codes` an
`prepare_job_for_reprocess()`. `_reset_step_results()` matcht jetzt
auf BEIDE — Status-basiert UND explizit per Code — und triggert
den Cascade in beiden Fällen.

`error_message` ist die einzig verlässliche Quelle der Wahrheit
für "was muss repariert werden", weil sie nicht von späteren
partiellen Retries überschrieben wird.

### Test-Coverage: User-Vorschlag, direkte Immich-API-Verifikation

Bisher prüften die Tests nur das `step_result` in der DB — also
"was wir denken geschrieben zu haben". Auf User-Vorschlag fragt
der Test jetzt **direkt aus Immich via API** ab welche Tags
tatsächlich am Asset hängen. Das ist deutlich stärker, weil es
auch zwischen "DB sagt OK aber Immich hat's nie übernommen" und
"alles wirklich propagiert" unterscheidet.

Neue Asserts in `test_retry_file_lifecycle.py`:
- `_run_lifecycle_test`: "immich-API confirms 'unknown' is NOT
  on the live asset (sidecar/direct)"
- `_run_stale_warning_state_retry_test` (NEU, reproduziert
  MA-2026-28121): erstellt einen Job mit IA-05 success-without-status
  + stale IA-07/IA-08 + `error_message="Warnungen in: IA-05"`,
  triggered Retry, prüft per DB-Step-Result UND per Immich-API-Call
  dass IA-07/IA-08 frisch durchgelaufen sind und die stale 'unknown'
  und 'STALE_GEO_TAG' weg sind.

Vor v2.28.35 wäre der `_run_stale_warning_state_retry_test` rot.
Post-Fix: **99/99 grün** (`test_retry_file_lifecycle.py`),
26/26 (`test_duplicate_fix.py`).

**Was du jetzt tun solltest:** auf live nach `docker compose pull`
+ Restart (v2.28.35) MA-2026-28121 nochmal "Erneut verarbeiten"
klicken. Diesmal sollten die echten KI-Tags (Metallteil,
Maschinenteil, Nahaufnahme, Gestrichene Oberfläche, Geschliffene
Kanten) auch in Immich landen.

## v2.28.34 — 2026-04-08

### Performance: IA-08 wartet nicht mehr 120s auf Immich-Tag-Extraktion wenn IA-07 nichts geschrieben hat

User-Beobachtung: "wieso geht IA-08 im dev system so lange?" — Im
dev mit echtem Immich brauchten Pipeline-Läufe pro Job 60-130s,
verglichen mit 7-30s im File-Storage-Modus. Der Unterschied
kommt komplett von `_wait_for_immich_tags()` in
`pipeline/step_ia08_sort.py`, das nach jedem Upload alle 3s pollt
ob Immich die Tags aus dem File extrahiert hat — bis zu 120s
lang.

**Warum die Wartezeit überhaupt:** wir wollen wissen welche Tags
Immich aus dem hochgeladenen File schon selbst extrahiert hat,
damit wir sie nicht doppelt via API anhängen. Auf einem Synology
NAS dauert die Tag-Extraktion laut Original-Commit-Kommentar
"30-90s, especially for PNG files".

**Warum es jetzt erst auffällt:** der Code lebt seit 2026-04-01.
Im Live-Betrieb pro Job einmal 60-130s warten merkt der User
nicht — der Filewatcher arbeitet im Hintergrund. Erst die Test-
Suite ab v2.28.28+ macht 18+ Pipeline-Läufe in Folge → die
kumulierte Wartezeit wird sofort sichtbar (~36 Min Worst-Case).

**Fix (Option 3 vom User-Vote):** `_tag_immich_asset()` bekommt
einen neuen Parameter `ia07_wrote_tags: bool`. Nur wenn IA-07
tatsächlich Keywords ins File (oder die `.xmp`-Sidecar)
geschrieben hat, läuft die Wait-Schleife. Sonst hat Immich gar
nichts zum Extrahieren — wir machen einen einzigen GET um zu
sehen welche Tags der Asset eventuell aus einem früheren Import
hat, und schreiben alles via API. Die Immich-Tag-API ist auf
Tag-Namen-Ebene idempotent (duplicate POST → 400/409), das
fängt der bestehende Code schon ab.

Beide Aufrufer in `pipeline/step_ia08_sort.py` (webhook-Branch
+ first-upload-Branch) übergeben jetzt
`ia07_wrote_tags=bool(ia07_result.get("keywords_written"))`.

**Messung auf dev (echtes Immich, kein Mock):**
- Fast path (`ia07_wrote_tags=False`): **0.1s** (1 GET)
- Slow path (`ia07_wrote_tags=True`): **3.1s** (Immich extracts
  fast hier)
- Speedup: **41× schneller**
- Auf einem Synology-NAS wäre der Slow-Path im Worst Case 30-120s
  → Speedup zwischen **300× und 1200×**

**Test-Coverage:** neuer Test
`_run_immich_tag_wait_skip_test` in `test_retry_file_lifecycle.py`
lädt eine echte HEIC ins Dev-Immich hoch, ruft `_tag_immich_asset`
einmal mit `ia07_wrote_tags=False` und einmal mit `True` auf,
misst die Zeit und assertiert dass der Fast-Path in <10s
fertig ist. Beide Pfade müssen funktionieren (kein Crash, Tags
korrekt geschrieben).

Vor v2.28.34 wäre der Fast-Path-Test rot (würde wie der Slow-Path
auf den Wait warten). Post-Fix:
- 89/89 grün (`test_retry_file_lifecycle.py`)
- 26/26 grün (`test_duplicate_fix.py`)

## v2.28.33 — 2026-04-08

### Fix: Retry läuft, schreibt aber nichts in Immich (Live-Vorfall MA-2026-28111 Folge)

User-Bericht: nach v2.28.32 lief der Retry für MA-2026-28111 zwar
durch (Status `done`), aber **in Immich landete kein neuer Tag**.
Der KI-Lauf hatte korrekt "Persönliches Foto" mit Tags Metall,
Nahaufnahme, Gewinde, Seil, Maschine erkannt, die Detail-Seite
zeigte das auch — aber `IA-07.keywords_written = [unknown,
Schweiz, Aargau, Lupfig]` und `IA-08.immich_tags_written =
[unknown, Schweiz, Aargau, Lupfig, Persönliches Foto]`. Die alten
"unknown"-Tags vom Pre-Retry-Zustand sind in der Pipeline hängen
geblieben.

**Ursache:** `_reset_step_results()` in `pipeline/reprocess.py`
hat beim Retry nur die Steps mit `status='warning'`/`'error'` aus
dem `step_result` gedroppt. Dropping `IA-05` aber war zu wenig:
**IA-07 (schreibt Tags ins File) und IA-08 (lädt zu Immich +
schreibt Immich-Tags) konsumieren beide IA-05's Output.** Da
ihre Step-Results aber unverändert blieben, hat die Pipeline sie
beim erneuten Lauf als "schon erledigt" übersprungen, mit dem
ALTEN Output. Das frische IA-05-Resultat ging nirgendwo hin.

**Fix:** `_reset_step_results()` macht jetzt **Cascade-Drop**:
wenn ein Step nach Status gedroppt wird, werden alle Steps die
**nach** ihm in der Pipeline-Reihenfolge laufen ebenfalls
gedroppt. Konkret: bei `IA-05`-Warning droppt der Reset jetzt
auch `IA-06`, `IA-07`, `IA-08` — und alle vier laufen frisch
durch beim Retry. Die neue KI-Klassifikation propagiert über
IA-07 in die EXIF/Sidecar-Tags und über IA-08 in Immich.

Pipeline-Reihenfolge ist als `_PIPELINE_ORDER` in
`pipeline/reprocess.py` hartcodiert (parallel zu MAIN_STEPS in
`pipeline/__init__.py`, ohne Import-Cycle).

**Bonus-Cleanup:** der `target_was_local`-IA-08-Drop in
`reset_job_for_retry()` (aus v2.28.29) ist jetzt redundant —
der Cascade erledigt das automatisch in beiden Pfaden (warning
+ error retry, immich + file-storage). Entfernt.

**Test-Coverage:** `_run_lifecycle_test` hat drei neue Asserts
unter "F) Cascade-reset":
- IA-05 ran with non-'unknown' classification
- IA-07 keywords no longer carry the synthetic 'unknown'
- IA-08 immich_tags no longer carry the synthetic 'unknown'

Vor v2.28.33 wären alle drei rot. Post-Fix: 86/86 grün
(`test_retry_file_lifecycle.py`), 26/26 (`test_duplicate_fix.py`).

**Was du jetzt tun solltest:** auf live nach dem Pull und Restart
(v2.28.33) MA-2026-28111 nochmal "Erneut verarbeiten" klicken.
Diesmal landen die echten KI-Tags (Metall, Nahaufnahme, Gewinde,
Seil, Maschine) auch in Immich.

## v2.28.32 — 2026-04-08

### Fix: Retry bricht ab obwohl Datei in Immich noch lebt (MA-2026-28111)

User-Bericht: Job MA-2026-28111 (`IMG_2500.HEIC`), `target_path =
immich:154a3211-...`, `original_path = /inbox/iPhone/2022/12/IMG_2500.HEIC`,
Status `error` mit "Datei nicht auffindbar — Retry abgebrochen". Der
User bestätigte: "ich kann die datei aber manuel finden in immich".

**Was war kaputt:** Mein v2.28.28-Fix gegen die Endlos-Retry-Schleife
hat zu pauschal auf "weder target_path noch original_path existieren
auf der Disk → abort" geprüft. Wenn aber `target_path` ein
`immich:<asset_id>`-Reference ist, lebt die Datei nach wie vor in
Immich — sie ist nicht verloren, nur nicht lokal. Das ist sogar der
**häufigste Live-Zustand überhaupt**: jeder Inbox-Job, der erfolgreich
nach Immich hochgeladen wurde, hat ab dem Moment KEINE lokale Kopie
mehr (IA-08 räumt die Inbox-Datei nach Upload auf). Ein späterer Retry
wegen einer Soft-Warning hat dann immer abgebrochen.

**Fix in `pipeline/reprocess.py:_move_file_for_reprocess()`:**
Wenn die lokalen Quellen (target_path, original_path) leer sind UND
target_path eine `immich:`-Reference ist, wird die Datei jetzt via
`download_asset()` aus Immich nach `/app/data/reprocess/` runtergeladen
und der Pipeline-Lauf läuft normal weiter. Der debug_key wird ans
Filename suffigiert um Kollisionen zwischen parallelen Reprocesses
des gleichen Immich-Filenames zu vermeiden.

Wenn weder lokal noch in Immich was zu finden ist (echter "missing"-
Fall), bricht der Retry weiterhin sauber ab — wie bisher.

**Test-Matrix-Lücke geschlossen (Sektion 14):**
- Neuer Test `_run_immich_only_retry_test`: setzt das exakte
  MA-2026-28111-Szenario auf — erster Lauf hochladen, Inbox-Datei
  weg, Warning injizieren, Retry → muss erfolgreich sein. Vor
  v2.28.32 rot, nach v2.28.32 grün.
- Neuer Test `_run_truly_missing_test`: weder lokal noch in Immich.
  Retry muss abbrechen, kein Endlos-Loop.
- Sektion 14 R15 umformuliert (jetzt explizit: "weder Disk noch Immich")
- Sektion 14 R17 neu hinzugefügt für den Immich-only-Pfad.

Test-Lauf: 80/80 grün (`test_retry_file_lifecycle.py`).
Regressionen: 26/26 (`test_duplicate_fix.py`), 59/0/1-block
(`test_testplan_final.py`).

**Selbstkritik:** Die Test-Matrix in v2.28.30 hat genau diese Achse
übersehen. Sektion 14 hatte zwar "R12-R14: Immich-Poller-Source"-
Lücken aber nicht den **Inbox-Job-nach-erfolgreichem-Upload**-Pfad,
obwohl das der häufigste Job-Endzustand überhaupt ist. Das ist eine
ehrliche Lücke in meiner Coverage-Analyse, die Sektion 14 ab v2.28.32
mit R17 schliesst.

## v2.28.31 — 2026-04-08

### Fix: Doppelter Status-Badge auf der Job-Detail-Seite

User-Bericht: "Im Verarbeitungs-Log zeigt eine Datei zuerst den
einen Status (z.B. `review`), und nach ein paar Sekunden erscheint
ein zweiter Status (z.B. `duplicate`) daneben."

Ursache: `static/js/app.js:111` selektierte mit
`document.querySelectorAll(".status-badge")` ALLE Elemente mit
dieser Klasse und überschrieb deren Text mit `job.status`. Bei
`dry_run=True` rendert `templates/log_detail.html` aber ZWEI
Badges mit derselben Klasse: das normale Status-Badge plus ein
"Preview"-Badge. Beim Polling-Refresh wurde das Preview-Badge
also auch zum Status-Badge, und der User sah zwei identische
Badges nebeneinander.

Fix in zwei Stellen:
- `templates/log_detail.html`: das Status-Badge bekommt
  `data-field="status"`, das Preview-Badge eine eigene Klasse
  `status-preview`.
- `static/js/app.js:updateJobDetail()`: aktualisiert nur noch
  `[data-field="status"]`, lässt alle anderen `.status-badge`
  Elemente in Ruhe.

### Fix: IA-02 Duplicate-Handler löscht stale `error_message`

Beim Aufräumen der Test-DB fielen 8 Job-Rows mit intern
inkonsistentem Zustand auf: `status='duplicate'` UND
`error_message='Warnungen in: IA-05'`. Das ist semantisch
unmöglich (ein Duplikat bricht die Pipeline nach IA-02 ab,
kommt nie bis IA-05).

Wie der Zustand entstanden war: bei einem Retry, der zwischen
zwei Pipeline-Läufen einen Soft-Warning erbte, hat IA-02 im
zweiten Lauf das Duplikat erkannt und `job.status='duplicate'`
gesetzt — die alte `error_message="Warnungen in: ..."` aus dem
ersten Lauf blieb aber stehen. Live-DB ist davon nicht
betroffen (geprüft: 0 Treffer), nur die Dev-DB durch die
Test-Pollution.

Defensiver Fix in `pipeline/step_ia02_duplicates.py:_handle_duplicate()`:
beim Setzen von `status='duplicate'` wird `error_message` jetzt
explizit auf `None` zurückgesetzt — sowohl im Dry-Run-Pfad als
auch im normalen Pfad. Damit kann der inkonsistente Zustand
nicht mehr entstehen, egal woher der Job kommt.

Dev-DB Cleanup: 8 Test-Jobs hatten ihre stale `error_message`
geleert, Status bleibt `duplicate`.

## v2.28.30 — 2026-04-08

### Feature: "Alle Warnungen retry" Button im Verarbeitungs-Log

Analog zum bestehenden "Alle Fehler retry"-Button gibt es jetzt einen
zweiten Bulk-Action-Button im Logs-Header, der alle Jobs im Status
`done` mit `error_message='Warnungen in: ...'` (also Soft-Failures
aus IA-02..IA-06, häufigster Fall: kurzer KI-Backend-Aussetzer) in
einem Klick neu queued.

- Neuer Endpoint `POST /api/jobs/retry-all-warnings` (`routers/api.py`).
  Selbe Architektur wie `retry-all-errors`: ein einzelner sequentieller
  Background-Task, kein Pool-Exhaustion.
- Button im Logs-Header (`templates/logs.html`), JS-Handler in einen
  gemeinsamen `_bulkRetry()`-Helper refaktoriert (kein Copy-Paste).
- i18n: `retry_all_warnings` + `retry_all_warnings_confirm` in
  `i18n/de.json` und `i18n/en.json`.

### Fix: AI-Prompt fällt nach "Reset" nicht auf Source-Default zurück

`pipeline/step_ia05_ai.py:105` las den Prompt mit
`config_manager.get("ai.prompt", DEFAULT_SYSTEM_PROMPT)`. Der Default
greift aber **nur** wenn der Key komplett fehlt — wenn der Settings-UI
"Reset"-Button den Wert auf `""` setzte (so dass die Audit-Row in der
DB erhalten bleibt), bekam die KI einen leeren System-Prompt und
halluzinierte. Fix: zusätzlich `or DEFAULT_SYSTEM_PROMPT`, sodass auch
ein leerer String auf den Source-Default zurückfällt.

### Test-Coverage: Stufe 1 Retry-Matrix (R5/R6/R10/R11)

`test_retry_file_lifecycle.py` `_run_error_retry_test` parametrisiert
über die zwei Achsen `mode` ∈ {direct, sidecar} × `use_immich` ∈
{True, False}. Damit deckt der Test jetzt alle vier
Sektion-14-Szenarien für Error-Retries ab:

- R5  = Immich + direct + IA-08-Error retry  (war schon)
- R6  = Immich + sidecar + IA-08-Error retry  (NEU)
- R10 = File-Storage + direct + IA-08-Error retry  (NEU)
- R11 = File-Storage + sidecar + IA-08-Error retry  (NEU)

Sidecar-Varianten checken zusätzlich, dass die `.xmp` weder in
`/library/error/` noch in `reprocess/` strandet, sondern korrekt
zum finalen Target-Pfad wandert. File-Storage-Varianten checken,
dass das `target_path` aus `/library/error/` heraus in eine echte
Kategorie (`/library/photos/...`) wandert.

### Test-Verhalten: Tests laufen wie normale Files durch (kein Cleanup)

Die `_cleanup_job_artifacts()`-Funktion ist komplett entfernt. Test-
Files bleiben jetzt nach dem Lauf in `/library/`, in Immich, und als
Job-Rows in der DB — genau wie ein vom User reingelegter File. Damit
sind die Tests im Verarbeitungs-Log sichtbar (zuvor wurden die
Job-Rows gelöscht und waren weg).

Zwei Konsequenzen, die im `main()`-Setup adressiert sind:
- `duplikat_erkennung` wird zusätzlich zum `filewatcher` während der
  Tests **temporär deaktiviert**, weil die pHash-Detektion sonst die
  Test-Files vom n-ten Run als Duplikate des n-1-ten Runs flaggen
  würde (jeder Run nutzt zwar einen unique-content Source via
  `_make_unique_source()`, das ändert aber nur SHA256, nicht pHash).
- Beide Module + `metadata.write_mode` werden im `finally`-Block
  garantiert auf den Vor-Test-Stand zurückgesetzt.

Test-Resultat post-Stufe-1: **73/73 grün** gegen echtes Dev-Immich.

## v2.28.29 — 2026-04-08

### Fix: Retry verliert Datei auch im File-Storage-Modus (`use_immich=False`)

Nachzügler-Fix zu v2.28.28: der vorherige Patch hat das Datei-Verlust-
Problem für Immich-gestützte Jobs gefixt, aber im **reinen File-
Storage-Modus** (`use_immich=False`, IA-08 verschiebt nach
`/library/photos/...`) blieb derselbe Pathologie-Pfad offen.

**Was passierte:**

`_move_file_for_reprocess` verschiebt die Datei aus `target_path`
(z.B. `/library/photos/2024/.../X.jpg`) nach `/app/data/reprocess/`.
Da IA-08 sein Step-Result aus dem ersten erfolgreichen Lauf
gecacht hat, läuft IA-08 beim Retry **nicht** erneut — der Move
zurück nach `/library/` passiert nie. Die Datei strandet in
`reprocess/`, `target_path` ist `None`, und der User findet seine
Datei nicht mehr im erwarteten Library-Ordner.

**Fix in zwei Stellen:**

1. `pipeline/__init__.py:reset_job_for_retry` — vor dem
   Reprocess-Move wird das alte `target_path` festgehalten. War es
   ein lokaler Pfad (kein `immich:`-Ref), wird IA-08 aus dem
   Step-Result entfernt, sodass der nächste Pipeline-Pass den
   Move-to-Library erneut ausführt und `target_path` auf die neue,
   gültige Library-Location setzt. Für `immich:`-Refs bleibt
   IA-08 weiterhin gecacht (Asset überlebt, Re-Upload unnötig).

2. `pipeline/reprocess.py:_move_file_for_reprocess` — beim
   Verschieben des `.xmp`-Sidecars wird auch die gecachte
   `IA-07.sidecar_path` im Step-Result auf den neuen Pfad
   aktualisiert. Sonst würde IA-08 beim Re-Run die Sidecar-Datei
   am alten (jetzt leeren) Inbox-Pfad suchen und ein orphan
   `.xmp` in `reprocess/` zurücklassen.

**Test-Coverage:** `test_retry_file_lifecycle.py` um den
file-storage-Fall erweitert. Pre-Fix scheiterte die neue Assertion
"target_path points to an existing file post-retry", post-Fix
**31/31 grün** (Sidecar Immich, Direct Immich, File-Storage,
verschwundene Quelldatei).

## v2.28.28 — 2026-04-08

### Fix: Retry löscht die Datei (Live-Bug MA-2026-28123 / -15415)

Drei eng zusammenhängende Bugs im Retry-Pfad führten dazu, dass ein
Klick auf „Retry" auf einem Job mit `Warnungen in: IA-05` die Datei
permanent von der Disk gelöscht hat — obwohl sie kurz vor dem Klick
nachweislich noch vorhanden war.

**1) `pipeline/step_ia10_cleanup.py` — IA-10 löscht zu aggressiv**

IA-10 räumte `job.original_path` immer dann ab, wenn `immich_asset_id`
gesetzt war. Diese Bedingung sollte ursprünglich nur die Tempdirs des
Immich-Pollers (`/tmp/ma_immich_xxxxxxxx/`) treffen — feuerte aber
auch auf Inbox-Jobs, sobald der erste erfolgreiche IA-08-Upload eine
`immich_asset_id` gesetzt hatte. Folge: nach einem Retry verschob
`_move_file_for_reprocess` die Inbox-Datei nach
`/app/data/reprocess/`, IA-10 löschte sie dort weg, und nichts blieb
auf der Disk übrig. Die einzige Spur war die Immich-Kopie.

Fix: zusätzlich `source_label == "Immich"` UND
`original_path.startswith("/tmp/ma_immich_")` prüfen, sodass nur
echte Poller-Tempdirs geräumt werden.

**2) `pipeline/__init__.py:reset_job_for_retry` — Endlos-Retry bei
verschwundener Quelldatei**

`prepare_job_for_reprocess` gab `False` zurück, wenn weder
`target_path` noch `original_path` auf der Disk existierten — der
Aufrufer hat den Rückgabewert aber ignoriert und den Job trotzdem
auf `queued` gesetzt. Damit lief jeder Retry-Versuch in dieselbe
`FileNotFoundError`-Schleife (siehe MA-2026-15415, -23077, -22930
auf live).

Fix: Rückgabewert wird jetzt geprüft. Bei `False` wird der Job auf
`status='error'` gesetzt mit klarer Meldung „Datei nicht
auffindbar — Retry abgebrochen", statt blind requeued zu werden.

**3) `pipeline/reprocess.py:_move_file_for_reprocess` — `target_path`
wird beim Retry zerstört**

`_move_file_for_reprocess` und `prepare_job_for_reprocess` haben
`job.target_path` unbedingt auf `None` gesetzt. Bei Inbox-Jobs ist
`target_path` aber bereits beim Retry-Zeitpunkt eine
`immich:<asset_id>`-Referenz. Da IA-08 sein Step-Result aus dem
ersten Lauf gecacht hat und beim Retry NICHT erneut läuft, wurde
das gecleared `target_path` nie wieder gesetzt — der Job stand am
Ende mit `target_path=None` da, obwohl das Immich-Asset weiterhin
existierte.

Fix: ein neuer Helper `_is_immich_target()` erkennt
`immich:`-Referenzen, und das Cleanup erfolgt nur noch für lokale
Disk-Pfade. `immich:`-Referenzen werden über den Retry hinweg
erhalten.

**Test-Coverage:**

`backend/test_retry_file_lifecycle.py` reproduziert das Live-Szenario
1:1 gegen das echte Dev-Immich (Sidecar + Direct Mode + Negativ-Fall
für verschwundene Quelldatei). Vor dem Fix 8 Asserts rot, nach dem
Fix 24/24 grün. `test_duplicate_fix.py` Tests 7+8 (Race-Conditions
für `retry_job`) wurden auf reale Dummy-Files (0-Byte) umgestellt,
weil der neue `reset_job_for_retry`-Vertrag eine existierende
Quelldatei voraussetzt.

## v2.28.27 — 2026-04-07

### UI: Header-Buttons mit Title oben bündig statt vertikal zentriert

`align-items: center` auf den Headern von Dashboard und Logs hat die
Action-Buttons vertikal zur Mitte des H1 zentriert — das wirkte
optisch zu tief, weil das H1 mit `font-size: 1.75rem` deutlich höher
ist als die `btn-small`-Buttons. Auf `align-items: flex-start`
umgestellt, sodass die Buttons jetzt oben am H1 sitzen statt in der
Mitte zu schweben.

## v2.28.26 — 2026-04-07

### Fix: Dashboard-Header-Layout-Shift + Module-Grid auf 4 Spalten gedeckelt

**Layout-Shift bei Pipeline-Pause-Button:** Der `pipeline-toggle-btn`
startete bisher mit `style="display: none;"` und wurde erst per JS
sichtbar gemacht (`refreshPipelineStatus()` setzte `btn.style.display
= ''` nach dem ersten API-Call). Das ergab einen Layout-Shift kurz
nach Page-Load — der Button erschien plötzlich im Header und drückte
Title + Buttons in eine zweite Zeile.

Fix: Button von Anfang an sichtbar mit Default-Label „⏸ Pipeline
pausieren" und `data-state="running"`. Die JS aktualisiert nur noch
`textContent` und `dataset.state` wenn der API-Call wirklich `paused`
zurückgibt. Header bleibt damit von Anfang an stabil in einer Zeile,
wie auf der Logs-Seite.

**Module-Grid 4-Spalten-Cap auf Desktop:** `repeat(auto-fit, minmax(
180px, 1fr))` ließ die 8 Module-Karten auf sehr breiten Viewports zu
5+ Spalten ausufern. Eine Media-Query ab 920px Breite deckelt jetzt
auf `repeat(4, 1fr)` — ergibt das gewohnte 4×2-Layout am Desktop, ohne
das responsive Verhalten unter 920px (1–3 Spalten je nach Breite) zu
verlieren.

## v2.28.25 — 2026-04-07

### Fix: Cache-Buster für CSS/JS an die App-Version koppeln

`base.html` hatte hardcoded `?v=20` (style.css), `?v=4` (light.css) und
`?v=4` (app.js) als Cache-Buster — die wurden seit Ewigkeiten nicht
mehr von Hand gebumpt. Jeder Browser hatte deswegen die alten Asset-
Versionen unter dieser URL gecacht und hat die UI-Änderungen aus
v2.28.21–v2.28.24 (responsive Module-Karten, Header-Wrap, Scan-Button-
Refactor) gar nicht zu sehen bekommen.

Cache-Buster jetzt an `{{ version }}` gekoppelt — bei jedem Release
ändert sich die URL automatisch und der Browser holt frisch.

## v2.28.24 — 2026-04-07

### UI: Dashboard-Header analog zu Logs-Header + Module-Karten Text-Wrap

Auf der `/logs`-Seite sind alle drei Action-Buttons direkte `<a>`-Children
des Flex-Containers — auf dem Dashboard war einer davon (`Jetzt scannen`)
in einem `<form>` gewrappt, also war die Form das Flex-Item statt der
Button. Beim Wrappen auf Mobile wirkte das uneinheitlich.

Umgestellt: Der Scan-Button ist jetzt ebenfalls ein direkter Anchor
mit `onclick="triggerScan(event)"`-Handler nach dem Muster von
`retryAllErrors()` aus `logs.html`. Die POST-Semantik bleibt erhalten
(Fetch auf `/api/trigger-scan`), inklusive temporärem `⏳`-State und
Fehler-Alert wie bei den anderen Action-Buttons.

Zusätzlich darf `.module-detail` auf den Module-Karten jetzt umbrechen:
`white-space: nowrap; text-overflow: ellipsis;` ersetzt durch
`word-break: break-word; overflow-wrap: anywhere;`. Auf schmalen
Mobile-Karten wird Detail-Text damit über mehrere Zeilen umgebrochen
statt mit Ellipsis abgeschnitten.

## v2.28.23 — 2026-04-07

### UI: Logs-Header-Buttons brechen auf Mobile um

Gleicher Fix wie v2.28.22 für die `/logs`-Ansicht: Dryrun-Report,
Retry-All-Errors und Cleanup-Orphans bekommen `flex-wrap: wrap` und
brechen auf engen Viewports unter den Titel um.

## v2.28.22 — 2026-04-07

### UI: Dashboard-Header-Buttons brechen auf Mobile um

Die `Pipeline pausieren` / `Jetzt scannen` Buttons im Dashboard-Header
saßen in einem `flex`-Container ohne `flex-wrap` — auf engen Viewports
wurden sie aus der Zeile gedrängt oder überliefen den Container.
`flex-wrap: wrap` auf Header-Container und Button-Gruppe lässt sie
jetzt sauber unter den Titel umbrechen.

## v2.28.21 — 2026-04-07

### UI: Module-Karten auf dem Dashboard skalieren responsiv

`.modules-grid` hatte seit Tag 1 ein hardcoded `grid-template-columns:
repeat(4, 1fr)`, was die 8 Module-Karten auf dem Dashboard auf jedem
Viewport in 4 Spalten zwingt. Auf Mobile ergab das vier viel zu schmale
Spalten, die optisch wie zwei „Gruppen" zu je vier Karten wirkten.

Umgestellt auf `repeat(auto-fit, minmax(180px, 1fr))` — die Karten
fließen jetzt von 1 Spalte (kleines Mobile) bis 4+ Spalten (Desktop)
ohne starre Zeilen-Gruppierung. Zusätzlich `flex-wrap: wrap` auf der
`.module-legend` darunter, damit die Status-Legende auf engen Viewports
nicht mehr horizontal überläuft.

## v2.28.20 — 2026-04-07

### Refactor: gemeinsamer `prepare_job_for_reprocess()` Helper

Vier Codepfade haben bisher denselben „Job zurück in die Queue"-Tanz
inline implementiert: `pipeline.reset_job_for_retry()` (error/warning
Retry), `routers/duplicates.py` Review-Keep-One, `routers/duplicates.py`
Not-Duplicate, und das geplante Wartungs-Tool aus #42. Die Logik ist
jetzt als `pipeline.reprocess.prepare_job_for_reprocess()` zentralisiert.

**Bonus-Fix beim Refactor:** Der File-Move in den `reprocess/`-Ordner
verschiebt jetzt das `.xmp`-Sidecar **mit** der Datei. Vorher wurden
Sidecars im alten Library-Pfad orphaned, was bei `metadata.write_mode =
sidecar` dazu führen konnte, dass nach einem Reprocess Tags nicht mehr
am ursprünglichen Speicherort lagen. IA-07 schreibt jetzt im neuen
Reprocess-Pfad mit konsistentem Modus weiter — direct bleibt direct,
sidecar bleibt sidecar.

**Policies des Helpers:**
- `keep_steps={"IA-01"}` — nur diese Schritte behalten (Duplicates-Flow)
- `drop_step_statuses={"error","warning"}` — Schritte mit diesen Status
  verwerfen (Retry-Flow)
- `inject_steps={"IA-02": {...}}` — synthetische Step-Results einfügen
  (Not-Duplicate markiert IA-02 als skipped)
- `move_file=False` — In-Place Reprocess für Tools wie tag_cleanup, die
  die Datei am Library-Pfad lassen und nur das EXIF wipen

Issue #42 (tag_cleanup) wurde nachgeführt — Phase 3 (REQUEUE) ist jetzt
ein Einzeiler über `prepare_job_for_reprocess(move_file=False, ...)`.

## v2.28.19 — 2026-04-07

### Feature: Filter „Warnung" im Verarbeitungs-Log + Retry für Warning-Jobs (#43)

Ergänzt den Status-Filter im Verarbeitungs-Log (`/logs?tab=jobs`) um einen
neuen Eintrag **„Warnung"**. Da Jobs mit aggregierten Step-Warnungen technisch
weiterhin den Status `done` tragen (siehe v2.28.18), ist das ein Pseudo-Filter:
Backend matcht `status='done' AND error_message LIKE 'Warnungen in:%'`.

Zusätzlich kann ein einzelner Warning-Job jetzt über die Detail-Ansicht
nochmal durch die Pipeline geschickt werden — der Retry-Button war bisher nur
für `error`-Jobs sichtbar. `reset_job_for_retry()` akzeptiert jetzt beide
Zustände und verwirft sowohl `error`- als auch `warning`-Step-Results, damit
die betroffenen Schritte beim nächsten Lauf neu ausgeführt werden.

Schliesst marcohediger/mediaassistant#43.

## v2.28.18 — 2026-04-07

### Fix: Soft-Warnungen werden jetzt im Job sichtbar

User-Frage: „Werden jetzt alle Warnungen in den Job geschrieben?" —
Antwort: nicht ganz. Es gab drei Lücken die Warnungen still in
Sub-Feldern versteckt haben, ohne dass der Job-Status oder das
`error_message`-Feld irgendwas davon zeigte.

#### 1) IA-08: `immich_tags_failed` wird nicht mehr stillschweigend versteckt

Wenn beim Hochladen zu Immich ein oder mehrere Tags nicht gesetzt
werden konnten (`immich_tags_failed: ["tag1", "tag2"]`), war das
bisher nur in einem Sub-Feld des Step-Results sichtbar. Der Job
zeigte „done" ohne jegliche Warnung — wenn man nicht ins Detail
schaute, wusste man nicht dass Tags gefehlt haben.

**Fix:** `step_ia08_sort.py` setzt jetzt zusätzlich `status="warning"`
und ein `reason` mit der Liste der fehlgeschlagenen Tags. Beide
IA-08-Return-Pfade (Upload und Replace) sind angepasst.

#### 2) Pipeline aggregiert jetzt `status="warning"` zusätzlich zu `status="error"`

`pipeline/__init__.py` hat den `has_step_errors`-Check erweitert: er
prüft jetzt sowohl `status == "error"` als auch `status == "warning"`.
Das `error_message`-Feld zeigt entsprechend `Warnungen in: IA-08`
auch bei reinen Soft-Warnungen.

Nebenbei: das Feld heißt weiterhin `error_message`, beinhaltet aber
sowohl Errors als auch Warnungen — der Name ist historisch.

#### 3) Finalizer-Block: bessere Diagnose-Strings

`pipeline/__init__.py:238-244` (Finalizer-Catch-Block für IA-09/10/11)
benutzte noch `str(e)` für Exception-Reasons. Wenn die Exception einen
leeren `__str__` hatte (z.B. manche httpx-Fehler), landete `reason: ""`
in der DB und im System-Log. Jetzt: `f"{type(e).__name__}: {e}"`
konsistent mit dem main loop von v2.28.17.

### Was noch NICHT als Warnung erscheint (bewusst still)

- `_skipped: True`-Pfade in IA-05 (zu kleines Bild, Format nicht
  konvertierbar) — Skip ist semantisch kein Fehler, bleibt still wie
  bisher
- `IA-09.errors_reported: N` — das ist die Bestätigung dass eine
  Notification erfolgreich versendet wurde mit N Errors aus VORIGEN
  Steps. Die eigentlichen Warnungen kommen aus diesen vorigen Steps
  und werden separat aggregiert. Doppel-Tracking wäre Noise.

### Geänderte Dateien

- `backend/pipeline/__init__.py` (Finalizer-Diagnose, has_step_errors-Check)
- `backend/pipeline/step_ia08_sort.py` (immich_tags_failed → status=warning)

## v2.28.17 — 2026-04-07

### Fix: Pipeline Auto-Pause/Auto-Resume bei Service-Outages

User-Report: Das KI-Backend hatte Verbindungsfehler und Timeouts, **die
Pipeline lief aber stur weiter** und produzierte hunderte Files mit
leeren KI-Tags ("unknown") in der Library und in Immich. Pro File
wurden bis zu 6 Minuten verschwendet (3 Retries × 120s Timeout +
Backoff), bevor der Job stillschweigend als `done` markiert wurde.

**Ursache:** Die Pipeline kannte nur zwei Fehlerklassen — *critical*
(IA-01, IA-07, IA-08, IA-11) und *non-critical* (IA-02..IA-06).
Verbindungsfehler in non-critical Steps wurden behandelt wie inhaltliche
Fehler ("AI hat schlechte Antwort geliefert") → einzelner Step als
error markieren, weiter mit dem nächsten File. Keine Unterscheidung
zwischen *"AI ist offline"* und *"AI hat geantwortet, war aber Müll"*.

**Fix:** Drei Komponenten in einem zusammenhängenden Patch:

#### 1) Service-Outage-Klassifizierung

Neue Exception-Klassen die explizit „Backend ist tot" signalisieren:

- `pipeline.step_ia05_ai.AIConnectionError` — Backend nicht erreichbar
  (httpx.ConnectError/ConnectTimeout/ReadTimeout/NetworkError nach allen
  Retries, oder dauerhaft HTTP 5xx)
- `pipeline.step_ia05_ai.AIResponseError` — Backend hat geantwortet,
  Antwort aber unbrauchbar (HTTP 4xx etc.) → bleibt non-critical wie heute
- `pipeline.step_ia03_geocoding.GeocodingConnectionError` — Geo-Backend
  nach allen Retries unerreichbar oder dauerhaft HTTP 502/503/504. 429
  (Rate Limit) wird NICHT eskaliert — das ist ein Per-Request-Problem,
  kein Outage.

#### 2) Auto-Pause der Pipeline

`pipeline/__init__.py` fängt diese Connection-Errors gezielt ab und
setzt drei Config-Keys:

- `pipeline.paused = true`
- `pipeline.auto_paused_reason = "ai_unreachable" | "geo_unreachable"`
- `pipeline.auto_paused_at = ISO timestamp`

Der aktuelle Job wird als `error` markiert (nicht `done`!) und landet
in `error/` — keine Müll-Daten in der Library oder in Immich. Ein
prominenter `log_error("pipeline", ...)`-Eintrag erscheint im
System-Log mit Klartext-Hinweis dass die Pipeline pausiert wurde und
auf den health_watcher wartet.

#### 3) Health-Watcher mit Auto-Resume

Neuer Background-Task `backend/health_watcher.py`, registriert in der
`main.py` lifespan zusammen mit dem filewatcher. Pollt alle 30 Sekunden
(konfigurierbar via `health.check_interval`):

- Liest `pipeline.auto_paused_reason`
- Wenn gesetzt: ruft die **bestehenden Health-Check-Funktionen** aus
  `routers.dashboard` auf (`_check_ai_backend`, `_check_geocoding`) —
  selbe Checks die der User auch in der Modul-Liste sieht, kein
  duplizierter Code
- Wenn der Service wieder antwortet: setzt `pipeline.paused = false`,
  cleart die Auto-Pause-Keys, schreibt `log_info("pipeline", ...)`
  „Service wieder erreichbar — Pipeline AUTO-RESUMED"
- Pipeline-Worker übernimmt automatisch beim nächsten Loop-Iteration

**Wichtig — manuelle Pause-Trennung:**
- User klickt Pause-Button → setzt nur `pipeline.paused`, leeres
  `auto_paused_reason`. Health-Watcher fasst das **nicht** an. Auto-Resume
  erfolgt **nur** wenn die Pause auch automatisch war.
- User klickt manuell Resume während Auto-Pause → beide Keys werden
  gelöscht. Wenn Backend immer noch tot ist, wird der nächste Job die
  Auto-Pause sofort wieder triggern.

#### 4) Diagnose-Verbesserung

`pipeline/__init__.py` verwendet jetzt `f"{type(e).__name__}: {e}"`
statt `str(e)` an drei Stellen. Vorher landeten Exceptions mit leerem
`__str__` als `reason: ""` in der DB — z.B. der Job aus dem User-Report
hatte `IA-05.reason = ""` und niemand wusste warum. Jetzt sieht man
`"ConnectError: "` oder `"JSONDecodeError: ..."` und kann sofort
nachgehen.

#### 5) Sampling-Härtung gegen Repetition-Loops

In `step_ia05_ai.py` Payload:

```python
"frequency_penalty": 0.5,
"presence_penalty": 0.3,
```

Verhindert „Hügel Hügel Hügel..."-Token-Loops in kleinen Vision-Modellen
wie `qwen3-vl-4b`. Unbekannte Felder werden von OpenAI-kompatiblen
Backends ignoriert, kein Risiko. `response_format=json_object`
bewusst ausgelassen — wird nicht von allen Backends verstanden.

#### 6) "unknown"-Tag-Filter in IA-07

`step_ia07_exif_write.py` schrieb bisher den Wert von `ai_result["type"]`
literal als Keyword. Wenn IA-05 fehlgeschlagen ist und `type="unknown"`
default war, landete **literal „unknown"** als Tag im Sidecar und in
Immich (siehe User-Report `keywords_written: ["unknown"]`). Jetzt wird
„unknown" gezielt rausgefiltert.

### Geänderte Dateien

- `backend/pipeline/step_ia05_ai.py` (Exceptions, Klassifizierung, Sampling)
- `backend/pipeline/step_ia03_geocoding.py` (Exception, Retry-Eskalation)
- `backend/pipeline/__init__.py` (Auto-Pause + bessere Diagnose-Strings)
- `backend/pipeline/step_ia07_exif_write.py` (unknown-Filter)
- `backend/health_watcher.py` **NEU** (Background-Task)
- `backend/main.py` (Watcher-Registrierung in lifespan)

### Effekt im UI

- Pipeline pausiert sich selbst bei Service-Outages — keine Müll-Daten
  mehr in der Library oder in Immich während AI/Geo down sind
- System-Log zeigt rote Errors beim Auto-Pause und grüne Infos beim
  Auto-Resume
- Pause-Banner unter dem Header (existiert seit `v2.28.14`) bleibt
  weiterhin sichtbar während Auto-Pause aktiv ist
- Manuelle Pause/Resume-Workflows sind unverändert

### Konfiguration

- `health.check_interval` (Sekunden, default 30, minimum 5)

### Bekannte Einschränkungen

- Health-Check für `google` Geocoding-Provider triggert echte (kostenpflichtige)
  API-Calls. Workaround: Provider auf nominatim/photon umstellen oder
  bei Bedarf einen separaten "kein Ping für google"-Pfad einbauen.
- Reprocess der bereits kaputten Jobs (Issue #34/#42) wird durch diesen
  Fix NICHT mit erledigt — das kommt mit dem Cleanup-Tool.

## v2.28.16 — 2026-04-07

### Fix: Stille IA-05 parse_error landeten als 'done' im Log

User-Report: Eine HEIC-Datei wurde von `qwen3-vl-4b` mit einer
Repetition-Loop-Antwort verarbeitet (`"Hügel", "Hügel", "Hügel", ...`
bis `max_tokens=500` voll war), das JSON war abgeschnitten und
unparsbar. **Trotzdem** wurde der Job stillschweigend als `done`
markiert — keine Warnung im UI, keine `error_message`, keine
System-Log-Notification. IA-07 schrieb dann eine Datei mit leeren
KI-Tags in die Library.

**Ursache** in `pipeline/step_ia05_ai.py:340-343`: bei
`json.JSONDecodeError` wurde ein Result mit `parse_error: True`
zurückgegeben, aber **ohne** `status="error"`. Die Pipeline-Logik in
`pipeline/__init__.py:203-206` prüft ausschließlich auf
`status == "error"` — das `parse_error`-Flag wurde komplett ignoriert.
Konsequenz: `has_step_errors = False` → Job wird als `done` markiert,
Datei landet in der Library, Bug bleibt unsichtbar.

**Fix:**

1. parse_error-Pfad in IA-05 returniert jetzt ein vollständiges
   Error-Result mit `status="error"`, einem klaren `reason`, der
   `raw_response` (zur Diagnose) und sane Default-Feldern (`tags=[]`,
   `type="unknown"` etc.) damit IA-07 nicht crasht.
2. Zusätzlich wird via `log_warning("ai", ...)` ein prominenter
   System-Log-Eintrag erzeugt: Modell, debug_key, Parse-Fehler und
   die ersten 800 Zeichen der kaputten Antwort. Damit ist das Problem
   im Dashboard sichtbar und debuggbar.

**Effekt im UI:**
- Job-Status: `done` mit `error_message: "Warnungen in: IA-05"`
  (statt stillem `done` ohne Hinweis)
- System-Log-Eintrag mit Quelle `ai` und der vollständigen kaputten
  KI-Antwort

**Nicht im Scope dieses Fixes** (eigene Folge-Issues möglich):
- Repetition-Loop selbst verhindern (würde `frequency_penalty` /
  `presence_penalty` / `response_format=json_object` in der API-Payload
  brauchen)
- JSON-Reparatur bei abgeschnittenen Antworten
- Auto-Retry bei parse_error mit härteren Sampling-Parametern

## v2.28.15 — 2026-04-07

### Fix: AI-Tag-Halluzinationen ("Hund" ohne sichtbaren Hund)

User-Report: "die AI hat bei den Tags Halluzinationen, es kommt immer
wieder der Tag 'Hund' vor obwohl kein Hund sichtbar ist".

**Ursache:** Der DEFAULT_SYSTEM_PROMPT in `pipeline/step_ia05_ai.py`
listete im Tag-Abschnitt eine konkrete Beispiel-Vokabelliste auf
(`Landschaft, Essen, Tier, Hund, Katze, Gruppe, ...`). Gerade kleinere
lokale Vision-Modelle übernehmen solche Beispiele aus dem Prompt
häufig wörtlich als Output, auch wenn die Bildinhalte nichts damit zu
tun haben — klassisches In-Context-Bias / Priming. „Hund" stand früh
in der Liste und tauchte deshalb überdurchschnittlich oft auf.

**Fix:** Beispiel-Vokabelliste entfernt. Stattdessen explizite
Anweisung im Prompt:

- Tags **nur** für Dinge vergeben, die klar sichtbar sind
- Keine festen Vokabeln, Tags müssen aus dem tatsächlichen Bildinhalt
  abgeleitet werden
- Bei Unsicherheit lieber kein Tag

Hinweis: Wer in den Settings einen eigenen `ai.prompt` gespeichert
hat, muss diesen manuell aktualisieren — der Code-Default greift nur,
wenn kein Custom-Prompt in der DB liegt.

## v2.28.14 — 2026-04-07

### Feature: Pipeline-Pause für sauberen Container-Stop

User-Feedback: "filewatcher stoppen bringt nichts wenn die jobs schon
'wartend' sind". Korrekt — der Filewatcher-Modul-Toggle verhindert nur
neues Scannen, der Pipeline-Worker arbeitet bereits gequeuete Jobs
trotzdem weiter ab.

**Neuer "Drain & Pause"-Mechanismus:**

Der Pipeline-Worker checkt am Anfang jeder Loop-Iteration den
Config-Key `pipeline.paused`. Wenn `True`:
- Bereits laufende Jobs (`status=processing`) laufen zu Ende
- Worker pulled keine neuen Jobs aus der `queued`-Queue
- Filewatcher-Scanner läuft weiter und legt neue Jobs an (kein
  Datenverlust durch hängengebliebene Inbox-Files)
- Sobald alle laufenden Jobs fertig sind, ist der Worker komplett im
  Leerlauf — sicher für `docker stop`

**Neuer Button im Dashboard** (rechts oben neben "Jetzt scannen"):
- `⏸ Pipeline pausieren` / `▶ Pipeline fortsetzen` (Toggle)
- Confirm-Dialog vor Aktion
- Live-Status-Banner unter dem Header zeigt "Pipeline pausiert" mit
  Drain-Status: "(N aktiv, M wartend)"
- Auto-Refresh alle 5s via `/api/pipeline/status`

**Neue API-Endpoints:**
- `POST /api/pipeline/pause` → setzt `pipeline.paused=True`
- `POST /api/pipeline/resume` → setzt `pipeline.paused=False`
- `GET /api/pipeline/status` → `{paused, in_flight, queued}`

Alle Endpoints unterstützen `Accept: application/json` für Fetch-Calls
und 303-Redirect für klassische Form-POSTs.

**Empfohlener Workflow für sauberen Container-Stop:**

1. Dashboard → "⏸ Pipeline pausieren" klicken
2. Banner zeigt "(N aktiv, M wartend)" — warten bis N=0
3. `docker stop -t 60 mediaassistant`
4. Beim nächsten Start: Pause-Status persistiert via Config (auch
   nach Restart noch pausiert!) — Erst dann auf Resume klicken oder
   in Settings rücksetzen.

Wichtiger Hinweis: `pipeline.paused` ist persistent in der Config-DB.
Wenn der Container neu startet während Pause aktiv ist, bleibt die
Pipeline pausiert bis explizit ein Resume gesendet wird. Das ist
gewollt — verhindert dass eine vorherige Pause vergessen wird.

**Tests im Dev-Container — alle grün:**

| Test | Resultat |
|---|---|
| Pause via API setzt config | ✅ paused=True |
| 3 queued Jobs während Pause werden nicht gepulled | ✅ alle 3 bleiben queued |
| Status-Endpoint liefert in_flight + queued counts | ✅ |
| Resume → Worker startet sofort, processed alle 3 | ✅ |

## v2.28.13 — 2026-04-07

### Hotfix: IA-07 "ExifTool Sidecar already exists" — atomic write

**Symptom:**
```
[IA-07] ExifTool Sidecar Fehler: Error: '/app/data/reprocess/IMG_8484.HEIC.xmp'
already exists - /app/data/reprocess/IMG_8484.HEIC
```

**Ursache:** Sequenzielles Retry-Szenario:
1. Job A processed `/app/data/reprocess/IMG_8484.HEIC`, IA-07 schrieb
   `IMG_8484.HEIC.xmp` erfolgreich
2. Ein späterer Step (z.B. IA-08 Immich-Upload) failte
3. Sidecar bleibt am Ort liegen (kein Cleanup im Fehlerfall)
4. Bulk-Retry-Click → `reset_job_for_retry` cleared step_result, aber
   nicht das `.xmp`-File auf der Disk
5. Pipeline rennt erneut bis IA-07 → ExifTool refused mit "already
   exists"

In v2.28.2 hatte ich den `os.path.exists + os.remove`-Pre-Check entfernt
weil er TOCTOU-anfällig war. Das war zu radikal — es löste die Race,
nicht aber den sequenziellen Retry-Fall.

**Fix:** Atomic-Write-Pattern in `_write_sidecar`:
1. ExifTool schreibt zu `<sidecar>.{debug_key}.tmp` (eindeutiger Name)
2. Bei Erfolg: `os.replace(tmp, final)` → POSIX-atomar, überschreibt
   bestehendes File cleanly
3. Bei Fehler: tmp wird gelöscht, ursprüngliches Sidecar bleibt unberührt

Vorteile:
- **Sequenzielles Retry**: stale `.xmp` von vorherigem Run wird sauber
  überschrieben (anders als ExifTool-`-o` das refused)
- **Race-frei**: jeder Job hat eindeutigen tmp-Namen via `debug_key`
- **Atomar**: `os.replace` ist auf POSIX atomar, kein Half-State möglich
- **Kein TOCTOU**: keine Existenz-Checks, einfach replacen

**Defensive cleanup in `reset_job_for_retry`:**
Räumt jetzt zusätzlich leftover `.xmp` und `.xmp.<key>.tmp` Files
proaktiv weg, falls ein Job-Reset getriggert wird. Belt-and-suspenders
zur atomic write — schadet nicht, hilft bei interrupted ExifTool-Runs.

**Tests im Dev-Container — alle 3 grün:**

| Test | Resultat |
|---|---|
| Stale 43-byte sidecar wird durch 869KB XMP ersetzt | ✅ STALE_MARKER weg |
| First-time write ohne Leftover | ✅ Sidecar erstellt |
| Kein `.tmp`-File leftover nach Replace | ✅ aufgeräumt |

## v2.28.12 — 2026-04-07

### i18n: "Orphan" → "Verwaist" (Deutsch)

User-Feedback: Es gibt ein deutsches Wort dafür. Eingedeutschung der
Orphan-Strings im DE-Locale:

| Key | Vorher | Nachher |
|---|---|---|
| `orphan` | "Orphan" | "Verwaist" |
| `cleanup_orphans` | "Orphans aufräumen" | "Verwaiste aufräumen" |
| `cleanup_orphans_confirm` | "...als 'orphan' markieren?" | "...als 'verwaist' markieren?" |

Auch der hardcoded Alert-Text im JS-Handler eingedeutscht:
- "Orphan-Scan gestartet" → "Scan auf verwaiste Einträge gestartet"
- "Status orphan" → "Status Verwaist"
- "Lokale Pfade only" → "Nur lokale Pfade"

**Internal:** Der DB-Status-Wert bleibt `orphan` und URLs verwenden
weiter `?status=orphan` — das ist API-Contract und nicht
benutzersichtbar. Nur die i18n-Labels und User-Texte sind übersetzt.

`en.json` bleibt unverändert ("Orphan" / "Cleanup orphans").

## v2.28.11 — 2026-04-07

### Feature: Orphan-Cleanup (manueller Trigger im Logs-View)

Neuer Button **"Orphans aufräumen"** rechts oben neben "Alle Fehler retry".
Click triggert einen Background-Scan der alle Jobs im Status `done`,
`duplicate` und `review` durchgeht und diejenigen, deren `target_path`
oder `original_path` nicht mehr existiert, atomar als `status='orphan'`
markiert.

**Folgewirkung:**
- Orphan-Jobs werden aus IA-02 Candidate-Queries ausgeschlossen
  (`Job.status.in_(("done", "duplicate", "review", "processing", "error"))`
  enthält 'orphan' nicht)
- Daher keine Orphan-Logs (auch nicht auf DEBUG) mehr für diese Jobs
- Neue Filter-Option im Logs-View: `Status: Orphan` zeigt alle markierten

**Endpoint:** `POST /api/jobs/cleanup-orphans`
- `?check_immich=1` (optional) prüft auch Immich-Asset-Existenz via API
  (langsamer, default: nur lokale Pfade)
- JSON-Response mit `{scanning, check_immich}` für Fetch-Calls
- Background-Task läuft mit 50ms Pause zwischen Batches (200 Jobs/Batch)
- System-Log-Eintrag mit final count + reason wenn fertig

**JS-Handler im Logs-View:**
- Doppel-Confirm: erst Hauptaktion, dann ob Immich auch geprüft werden soll
- Button zeigt `⏳ ...` während des Calls (disabled)
- Alert mit Job-Anzahl und Hinweis auf Status-Filter
- Auto-redirect auf `/logs?tab=jobs&status=orphan` zur direkten Inspektion

**Recovery:** `error_message` dokumentiert den vorherigen Status:
`"Auto-orphaned from done (file gone) at 2026-04-07T...`. Falls die
Dateien wieder auftauchen, kann ein Operator manuell den Status
zurücksetzen.

### Performance: Pipeline-Worker Stagger reduziert

Worker-Stagger zwischen parallelen Job-Starts: **2.0s → 0.3s**.

In v2.28.x davor wartete der Worker 2 Sekunden zwischen jedem neuen Job
beim parallelen Start, um DB-Bursts zu vermeiden. Mit dem v2.28.8
Pool-Tuning (20/40 Connections) ist das nicht mehr nötig — und 2s
war zu lang: bei `slots=4` und Jobs mit ~5s Dauer war der erste Job
fertig bevor der vierte überhaupt startete, also effektiv nur 1–2
parallel statt 4. 0.3s ist genug Pause für DB-Atomic-Claims, lässt
aber alle 4 Slots in <1.5s vollaufen.

### i18n

Neue Strings in `de.json` und `en.json`:
- `logs.cleanup_orphans` / `logs.cleanup_orphans_confirm`
  / `logs.cleanup_orphans_immich` / `logs.orphan`

### Tests im Dev-Container

| Setup | Resultat |
|---|---|
| 5 done jobs mit existierenden lokalen Files | ✅ bleiben `done` |
| 5 done jobs mit fehlenden lokalen Files | 🗑️ alle 5 → `orphan` |
| 3 done jobs mit `target_path: immich:*` | ✓ bleiben `done` (kein API-Check) |
| Mit `check_immich=1`: fake immich:* IDs | 🗑️ würden via API als gone erkannt |

## v2.28.10 — 2026-04-07

### Fix: IA-02 Warnungen für orphan-Kandidaten in jeden Job-Detail

**Symptom:** Nach dem Bulk-Retry zeigte jeder neue Job mehrere Warnungen
in IA-02 vom Typ:
```
Orphaned job MA-2026-XXXX: file missing, skipping duplicate match
```

**Ursache:** Die Duplikat-Erkennung sucht im DB nach Jobs mit gleichem
file_hash oder ähnlichem pHash. Nach dem Retry-All wurden viele Files
von `/library/error/duplicates/` nach `/app/data/reprocess/` verschoben,
aber die alten DB-Einträge zeigen noch auf den ursprünglichen Pfad.
`_file_exists()` gibt False zurück, und die Pipeline loggt ein
**WARNING** für jeden Treffer — auch wenn der Orphan korrekt übersprungen
und die Pipeline normal weiterläuft.

**Fix:** Die Orphan-Meldungen werden jetzt nur noch auf **DEBUG-Level**
geloggt, nicht mehr als WARNING. Die Funktionalität ändert sich nicht
— Orphans werden weiter korrekt übersprungen, aber tauchen nicht mehr
in der system_logs Warning-Liste oder im Job-Detail auf.

Betrifft beide Stellen in `step_ia02_duplicates.py`:
- Stage 1: SHA256 exact-match Loop
- Stage 2: pHash similarity Loop

## v2.28.9 — 2026-04-07

### 🔥 Hotfix: Geocoding HTTP 429 (Nominatim Rate-Limit)

**Symptom:** Nach dem Bulk-Retry hagelte es HTTP 429 vom Nominatim-Server.
Geocoding-Step fiel reihenweise mit `RuntimeError: Nominatim HTTP 429`.

**Vier konkrete Probleme im alten Code (`step_ia03_geocoding.py`):**

1. **Kein User-Agent** — Nominatim Usage Policy verlangt einen
   identifizierenden User-Agent, sonst rate-limited oder blockiert
2. **Kein Retry bei 429** — wirft direkt RuntimeError, Job geht auf error
3. **Kein Client-Side Rate-Limit** — Nominatim erlaubt **max 1 req/s**
4. **Kein Cache** — gleiche Koordinaten (z.B. iCloud-Batch von einem Ort)
   wurden hunderte Male neu angefragt

**Fix:**

- **`USER_AGENT = "MediaAssistant/{VERSION} (self-hosted photo manager)"`**
  in allen drei Provider-Calls (Nominatim, Photon, Google)
- **Globaler asyncio-Throttle**: `_rate_lock` + `_last_request_ts`,
  enforce ≥ 1.1s zwischen Nominatim-Calls (gilt für alle parallelen
  Pipeline-Worker)
- **Retry-Helper `_http_get_with_retry()`**: max 4 Versuche bei
  HTTP 429 / 502 / 503 / 504, exponential backoff 5s → 10s → 20s.
  Wartet **mindestens** den exponential backoff, auch wenn der Server
  `Retry-After: 0` zurückgibt (das macht Nominatim bei abusive IPs)
- **In-Memory FIFO-Cache** mit ~11m Präzision (rounding auf 4 Dezimal-
  stellen), max 1024 Einträge. Bei iCloud-Batch-Imports trifft der
  Cache 90%+ der Anfragen
- **Non-fatal error handling**: Bei finalem Geocoding-Fail wird
  `{"status": "error", ...}` zurückgegeben statt RuntimeError → IA-03
  bleibt non-critical, Pipeline läuft weiter

**Tests im Dev-Container — alle grün:**

| Test | Resultat |
|---|---|
| Throttle 5× sequenziell | ✅ 4.40s (~1.1s/Call) |
| Retry-After=0 ignoriert + Backoff 5s+10s | ✅ 15.0s gesamt |
| Cache-Hit für gleiche Koordinaten | ✅ 12ms statt Network-Call |

## v2.28.8 — 2026-04-07

### 🔥 Hotfix: Retry-All erschöpfte den DB-Connection-Pool (v2.28.7 Folgebug)

**Symptome aus Production-Logs nach v2.28.7-Klick:**
```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00
```
- Pipeline-Jobs scheiterten reihenweise an IA-08
- Dashboard JSON gab HTTP 500
- `PendingRollbackError` Kaskaden in zerstörten Sessions

**Root Cause:** Der v2.28.4-Endpoint feuerte für jeden errored Job ein
`asyncio.create_task(retry_job(...))`. Bei 33 parallelen Retries hat
jeder Task zwei DB-Sessions geöffnet (atomic claim + run_pipeline-claim
+ Pipeline-Steps), was den Default-Pool von 5+10=15 sofort überlief.

**Fix in zwei Schritten:**

**1. Refactor `retry_job` → `reset_job_for_retry`:**
- Neue Helper-Funktion `reset_job_for_retry()` macht NUR die Vorbereitung:
  atomic claim (error → processing), File-Move, step_result Cleanup,
  flip auf `queued`. **Ruft `run_pipeline()` NICHT auf.**
- `retry_job()` (für Single-Retry per Detail-Button) bleibt: ruft
  `reset_job_for_retry()` + dann sofort `run_pipeline()` für instant
  feedback.
- `/api/jobs/retry-all-errors` nutzt jetzt nur `reset_job_for_retry()`
  in einem **einzigen** Background-Task, der die Jobs **sequenziell**
  mit 50ms Delay zwischen jedem reseted. Der normale Pipeline-Worker
  picked die Jobs danach an seiner konfigurierten Slot-Concurrency auf.

**2. DB Pool-Tuning:**
- `pool_size`: 5 → **20**
- `max_overflow`: 10 → **40**
- `pool_timeout`: default → **60s**
- `pool_pre_ping`: True (verhindert stale connections)

Damit ~60 max. Connections, ausreichend für Worker + Bulk-Reset-Task +
Dashboard-Polling auch unter Last.

**Test im Dev-Container:**
- 30 errored Test-Jobs erstellt + Endpoint aufgerufen
- Endpoint kehrte in **138ms** zurück (vorher: blockierender Burst)
- Nach 4s alle 30 Jobs in `queued`, kein einziger TimeoutError
- Dashboard parallel weiter erreichbar (kein 500)

## v2.28.7 — 2026-04-07

### UX: "Alle Fehler retry"-Button zeigt jetzt visuelles Feedback

User-Feedback: "ich weis nicht ob er wirklich alle auf retry setzt".

**Vorher:** Click → kurzer Page-Reload, keine Bestätigung. User wusste
nicht ob 0 oder 100 Jobs retried wurden.

**Jetzt:**
1. Click → Confirm-Dialog
2. Button zeigt `⏳ ...` während die Anfrage läuft (disabled)
3. Endpoint gibt JSON `{count, debug_keys[]}` zurück (vorher nur Redirect)
4. Alert: `✅ 13 Jobs für Retry vorgemerkt. Erste IDs: MA-2026-0003, ...`
5. Erst nach OK auf dem Alert wird die Seite geladen
6. Bei `count=0` (keine Error-Jobs gefunden): klare Meldung statt
   silent reload
7. Bei HTTP-Fehler: Alert mit Status-Code, Button kommt zurück

**Endpoint-Änderung (`/api/jobs/retry-all-errors`):**
- Detection via `Accept: application/json` oder `X-Requested-With: fetch`
- Fetch-Mode → JSONResponse mit `count`, `debug_keys[]` (max 20),
  `truncated`-Flag
- Klassischer Form-POST-Fallback → 303 Redirect (rückwärtskompatibel)
- System-Log-Eintrag enthält jetzt die ersten 20 Debug-Keys als Detail

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

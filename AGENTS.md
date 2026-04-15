# AGENTS.md — Hinweise für KI-Agenten an MediaAssistant

> Dieses Dokument richtet sich an LLM-Agenten (Claude Code, Codex, Aider,
> usw.), die in diesem Repo Code ändern. Es kondensiert die Konventionen,
> Architektur-Entscheidungen und Stolperfallen, die ein Mensch aus
> Erfahrung kennt — und die ohne diesen Hinweis erst durch Schaden
> gelernt würden.

## TL;DR — Was du wissen musst, bevor du Code anfasst

1. **Tests laufen IMMER auf dem Dev-Container, nicht lokal.**
   `docker exec mediaassistant-dev python /app/test_<name>.py`. Das
   Dev-Setup ist 1:1 identisch zum Live-System (echtes Immich, echtes
   AI-Backend, echtes SQLite, echte Pfade `/inbox`, `/library`,
   `/app/data`). Mocks sind die Ausnahme, nicht die Regel.
2. **Dev-Container nach Tests NICHT stoppen.** Er bleibt laufend.
3. **Bei Feature- oder Bug-Änderungen: Version bumpen, CHANGELOG
   schreiben, committen + pushen + git-tag + GitHub/Gitea-Release
   erstellen.** In genau dieser Reihenfolge, in einem Schub. **Beide
   Remotes** (origin/Gitea + github) bekommen Commit, Tag UND
   Release-Eintrag — sonst bleibt das `Latest`-Badge auf der alten
   Version stehen. Siehe "Release-Workflow" unten.
4. **Vor dem Coden recherchieren und verifizieren.** Lieber 5 Minuten
   Logs/DB lesen als einen Fix raten. Siehe "Vor jedem Fix" unten.
5. **Dev muss alle Live-Szenarien abdecken.** Wenn ein Bug auf live
   gefunden wird, MUSS er auf dev reproduzierbar sein, **bevor** der
   Fix gemacht wird. "Geht nur in production" ist kein gültiger Grund.

## Repo-Layout

```
MediaAssistant/
├── backend/                    # FastAPI app, Pipeline, alles Python
│   ├── main.py                 # App-Startup, FastAPI-Lifespan
│   ├── config.py               # ConfigManager (DB-backed key/value)
│   ├── database.py             # SQLAlchemy async session, pool tuning
│   ├── models.py               # ORM-Definitionen (Job, Config, Module, …)
│   ├── filewatcher.py          # Inbox-Scanner + Immich-Poller + Worker
│   ├── safe_file.py            # safe_move(): copy → verify hash → delete
│   ├── ai_backends.py          # Loadbalancer für mehrere OpenAI-kompat. Endpunkte
│   ├── immich_client.py        # REST-Client für Immich
│   ├── system_logger.py        # log_info/warning/error → system_logs Tabelle
│   ├── pipeline/               # Die 11 Pipeline-Steps + Helpers
│   │   ├── __init__.py         # run_pipeline, retry_job, reset_job_for_retry
│   │   ├── reprocess.py        # _move_file_for_reprocess, prepare_job_for_reprocess
│   │   ├── step_ia01_exif.py   # IA-01 ExifTool
│   │   ├── step_ia02_duplicates.py
│   │   ├── …                   # IA-03..IA-11
│   │   └── step_ia10_cleanup.py
│   ├── routers/                # FastAPI-Endpunkte (UI + API)
│   │   ├── api.py              # /api/health, /api/job/{key}/retry, retry-all, …
│   │   ├── dashboard.py
│   │   ├── duplicates.py       # Duplikat-Review-UI + Endpunkte
│   │   ├── logs.py
│   │   ├── settings.py
│   │   ├── setup.py
│   │   ├── review.py           # "Unknown" review queue
│   │   └── auth_oidc.py
│   ├── templates/              # Jinja2 HTML
│   ├── static/                 # CSS, JS, Logos
│   ├── i18n/                   # de.json / en.json
│   ├── version.py              # VERSION-String + DATE
│   ├── test_duplicate_fix.py   # Race-Conditions + Duplikat-Tests (26 Tests)
│   ├── test_retry_file_lifecycle.py  # Retry-File-Lifecycle (37+ Asserts, real Immich)
│   └── test_testplan_final.py  # Testplan-Smoketest (60 Asserts, UI-Erreichbarkeit)
├── data/                       # Mounts ./data → /app/data im Container
│   ├── mediaassistant.db       # Dev-DB (SQLite)
│   └── reprocess/              # Internes Arbeitsverzeichnis für Retry
├── dataLiveSystem/             # KOPIE der Prod-DB für Forensik (gitignored)
├── test_inbox/  / test_library/  # Mounts → /inbox / /library
├── docker-compose.dev.yml      # Entwicklung (uvicorn --reload)
├── docker-compose.yml          # Produktion (NAS)
├── docker-compose.synology.yml # Synology-spezifisch
├── docker-compose.build.yml    # Build-only
├── docker-compose.local.yml    # gitignored — lokale Overrides
├── docs/
│   └── sorting-flow.md
├── CHANGELOG.md                # Pflicht: jedem Release ein Eintrag
├── REQUIREMENTS.md             # Original-Spec, eher historisch
├── TESTPLAN.md                 # WAS getestet wird (Sektionen 1-13 per Step, Sektion 14 Test-Matrix per Code-Pfad)
├── TESTRESULTS.md              # WANN/wie ein Test-Lauf ausging (Tabelle: Funktion × Datum × Ergebnis, nur abstrakte Zahlen)
└── README.md
```

## Architektur in einem Absatz

FastAPI-App startet beim Boot 3 Hintergrund-Tasks: **Filewatcher**
(scannt Inbox-Verzeichnisse + pollt optional Immich), **Pipeline-Worker**
(zieht Jobs aus der DB-Queue), **Health-Watcher** (überwacht
AI-Backend + Geocoding und auto-pausiert die Pipeline bei Ausfall).
Jede neue Datei wird zu einer Zeile in `jobs` (`status='queued'`).
`run_pipeline(job_id)` läuft die 11 Steps sequentiell, jeder Step
schreibt sein Ergebnis als JSON in `job.step_result`. Failures unter
IA-02..IA-06 sind weich (Warning), IA-01/IA-07/IA-08 sind kritisch
(Job → `status='error'`, Datei wandert nach `/library/error/`).
Finalizer IA-09/10/11 laufen IMMER, auch nach Critical-Failure.

## Konventionen

### Sprache
- **Code & Code-Kommentare auf Englisch.** Keine Ausnahmen.
- **CHANGELOG, Commit-Messages, User-facing Strings (i18n)** auf Deutsch
  ODER Englisch, je nachdem was im jeweiligen File schon dominiert. Im
  CHANGELOG bisher Deutsch.
- **System-Logs** (`log_info`/`log_warning`/`log_error`) — User-facing,
  also Deutsch.

### Versionierung
- SemVer-ish: `2.<MAJOR>.<MINOR>` in `backend/version.py`.
- Bei Bug-Fix oder kleinem Feature: PATCH (letzte Stelle) bumpen.
- Bei größerem Feature: MINOR bumpen.
- Bei jedem Bump: `VERSION_DATE = "YYYY-MM-DD"` mit aktuellem Datum.
- **JEDE Code-Änderung, die User merken könnten, MUSS Version bumpen.**

### CHANGELOG-Format
```markdown
## v2.X.Y — YYYY-MM-DD

### <Kategorie>: <Kurzer Titel>

<Mehrere Absätze: was war kaputt, warum ist der Fix so, was ist
getestet, welche Live-Vorfälle sind dadurch erklärt.>
```

Kategorien (gelegentlich gemischt): `Fix`, `UI`, `Performance`,
`Refactor`, `Test`. Halte den ersten Satz konkret, kein Marketing.
Verweise auf Live-Vorfälle (Job-Debug-Keys wie `MA-2026-15415`) wenn
relevant.

### Tools-Nutzung
- Nutze die Read/Edit/Grep/Glob/Bash-Tools direkt; wenn ein
  spezialisierter dedizierter Tool existiert, **immer** den
  spezialisierten nehmen.
- Bash für Container-Operationen, sqlite-CLI-Aufrufe (via python3),
  und git.

## Dev-Container

Gestartet mit:
```bash
cd /home/marcohediger/claude/MediaAssistant
docker compose -f docker-compose.dev.yml up -d
```

Status prüfen:
```bash
docker ps --format '{{.Names}}\t{{.Status}}'
curl -s http://localhost:8000/api/health
```

Inside-Container shell:
```bash
docker exec -it mediaassistant-dev bash
```

Nach Code-Änderungen muss **NICHT** neu gebaut werden — `uvicorn
--reload` und der Volume-Mount `./backend:/app` führen Hot-Reload aus.
Test-Skripte werden direkt mit `docker exec ... python /app/test_X.py`
ausgeführt; sie öffnen ihre eigene DB-Session.

**WICHTIG: Den Dev-Container nach Tests NICHT stoppen.** Er bleibt
laufend, der nächste Test/Lauf erwartet ihn als ready.

### Dev-Umgebung — verfügbare Services

In der Dev-Umgebung sind neben dem MediaAssistant-Container auch eine
vollständige **Immich-Instanz** und alle Hilfsdienste verfügbar:

| Container | Zugang | Zweck |
|---|---|---|
| `mediaassistant-dev` | `http://localhost:8000` | MA Dev (hot-reload) |
| `immich_server` | `http://192.168.0.104:2283` | Immich API + Web-UI |
| `immich_machine_learning` | intern | Immich ML |
| `immich_redis` | intern | Redis für Immich |
| `immich_postgres` | intern | PostgreSQL für Immich |

**Immich API-Key:** In der MA-Dev-DB verschlüsselt gespeichert
(`config.immich.api_key`). Entschlüsselung:
```python
from cryptography.fernet import Fernet
import sqlite3, json
con = sqlite3.connect('data/mediaassistant.db')
val = con.execute("SELECT value FROM config WHERE key='immich.api_key'").fetchone()[0]
key = open('data/.secret_key', 'rb').read()
api_key = json.loads(Fernet(key).decrypt(val.encode()).decode())
```

**Immich Storage auf dem Host:**
```
Host-Pfad:       /home/marcohediger/claude/Bilder/
Im Container:    /data/  (immich_server)
Asset-Pfade:     /data/library/<User>/<Year>/<Year-Month>/<filename>
Sidecar-Pfade:   <asset_path>.xmp (falls vorhanden)
```

Dateien im Immich-Storage gehören `root:root` (Docker). Schreiben
in den Storage geht über `docker cp` oder `docker exec`:
```bash
docker cp /tmp/test.xmp "immich_server:/data/library/Marco Hediger/2026/..."
```

**Immich Jobs API** (getestet April 2026, Immich v2.6.3):
```bash
# Sidecar-Job triggern (Discover + Sync)
PUT /api/jobs/sidecar  {"command": "start"}              # nur neue/geänderte
PUT /api/jobs/sidecar  {"command": "start", "force": true}  # alle neu einlesen
# Metadata-Extraction
PUT /api/jobs/metadataExtraction  {"command": "start"}
```

## Tests

### Bestehende Test-Skripte

| File | Was | Aufruf |
|---|---|---|
| `backend/test_duplicate_fix.py` | 34 Tests: Duplikat-Fix #38 + Race-Conditions + Quality-Swap + zirkuläre Duplikate | `docker exec mediaassistant-dev python /app/test_duplicate_fix.py` |
| `backend/test_retry_file_lifecycle.py` | 110 Asserts: kompletter Retry-File-Lifecycle gegen echtes Immich (sidecar+direct, immich+file-storage, error+warning, missing-file, stale-warning, stuck-state) | `docker exec mediaassistant-dev python /app/test_retry_file_lifecycle.py` |
| `backend/test_testplan_final.py` | 68 Asserts: API/UI Smoke-Tests | `docker exec mediaassistant-dev python /app/test_testplan_final.py` |
| `backend/test_ai_backends.py` | AI-Backend-Loadbalancer | `docker exec mediaassistant-dev python /app/test_ai_backends.py` |

### Pflicht-E2E-Test: Release-Gate

> **Kein Release ohne grünen E2E-Test.** Unit-Tests allein reichen
> NICHT — die gravierendsten Bugs (v2.29.7 Datenverlust, Poller-
> Phantom-Duplikate) entstanden im Zusammenspiel von Features, nie
> in einzelnen Funktionen.

Das Test-Skript `test_e2e_user_stories.py` deckt alle User-Stories
end-to-end ab. Jede Story schickt eine **echte Datei** durch den
**ganzen Flow** gegen das Dev-System (echtes Immich, echte Pipeline,
echte DB) und verifiziert das Endergebnis:

| User-Story | Flow |
|---|---|
| **US-1: Inbox → Immich** | Datei in Inbox → Pipeline → Upload zu Immich → Asset existiert + Tags korrekt |
| **US-2: Inbox → Lokale Ablage** | Datei in Inbox → Pipeline → Datei in Library → EXIF-Tags geschrieben |
| **US-3: Immich-Poller** | Asset in Immich (vom Handy) → Poller erkennt → Pipeline → Tags in Immich |
| **US-4: Poller ignoriert eigene** | MA lädt Datei hoch → Poller überspringt (deviceId-Filter) → kein Duplikat |
| **US-5: Duplikat Keep** | Datei 2× einlesen → Duplikat erkannt → "Behalten" → 1 Asset in Immich, kein Verlust |
| **US-6: Duplikat Keep (Shared-Asset)** | Inbox + Poller = gleiche asset_id → "Behalten" → Asset bleibt in Immich |
| **US-7: Batch-Clean** | 2 Duplikate → Batch-Clean → Best bleibt, Donor weg, Asset in Immich OK |
| **US-8: Kein Duplikat** | "Kein Duplikat" → volle Pipeline → Upload zu Immich → Asset + Tags korrekt |
| **US-9: Retry nach Fehler** | Pipeline-Fehler → Retry → Datei landet korrekt in Immich |
| **US-10: Folder-Tags → Album** | Datei in Subfolder → Album in Immich erstellt → Asset zugeordnet |

**Pflicht vor jedem Release:**
```bash
docker exec mediaassistant-dev python /app/test_e2e_user_stories.py
```
Alle Stories müssen PASS sein. Ein FAIL blockiert den Release.

**Pflicht bei neuen Features/Bugfixes:**
- Wenn ein neues Feature eine User-Story betrifft → bestehenden
  E2E-Test erweitern oder neue Story hinzufügen.
- Wenn ein Bug im Live-System gefunden wird → **zuerst** prüfen
  ob eine E2E-Story diesen Fall abdeckt. Wenn nicht: Story ergänzen
  und sicherstellen dass sie den Bug **rot** zeigt, bevor der Fix
  geschrieben wird.

### Vor einem Bug-Fix: Reproducer auf dev bauen

Wenn ein Live-Bug gemeldet wird, ist die Pflicht-Reihenfolge:

1. Live-DB-Kopie nach `dataLiveSystem/mediaassistant.db` ziehen, mit
   sqlite analysieren (`python3 -c "import sqlite3; …"`). Verstehe
   den exakten Job-Verlauf.
2. **Prüfen ob eine E2E-User-Story den Fall abdeckt.** Wenn nicht:
   Story ergänzen die den Bug rot macht.
3. Reproducer auf dev schreiben, der den Bug rot macht. Ohne Reproducer
   kein Fix. Ohne rotem Reproducer kein Fix.
4. Code fixen, bis der Reproducer grün ist.
5. **E2E-Test + alle bestehenden Tests laufen lassen** — keine
   Regressionen erlaubt.
6. Erst dann Version bumpen + commit + push.

Siehe auch [`TESTPLAN.md` Sektion 14](TESTPLAN.md#14-test-matrix--vollständige-coverage-karte)
für die vollständig kartografierte Test-Matrix aller Pipeline-
Entry-Points (Filewatcher, Immich-Poller, Retry, Duplikat-Review)
mit explizit markierten Lücken.

Konkrete **Lauf-Resultate** (Datum × Test-Funktion × Pass/Fail) leben
in [`TESTRESULTS.md`](TESTRESULTS.md). Nach jedem vollständigen
Test-Lauf eine neue Spalte am rechten Ende einfügen. **Nur abstrakte
Zahlen** — keine personenbezogenen Daten, keine echten Datei-Inhalte.

### Test-Schreibung — Konventionen

- **Kein pytest.** Tests sind eigenständige `async def main()`-Skripte
  mit eigenem `report(name, ok, detail)`-Helper. Stil siehe
  `test_duplicate_fix.py` oder `test_retry_file_lifecycle.py`.
- **E2E first, Unit second.** Jeder neue Code-Pfad braucht zuerst
  eine E2E-User-Story die den vollen Flow testet. Unit-Tests sind
  Ergänzung, nicht Ersatz.
- **Test-Design vom Live-System her.** Bevor ein Test geschrieben
  wird: Live-DB abfragen, welche Daten-Konstellationen es wirklich
  gibt. Daraus Test-Szenarien ableiten — nicht aus dem Code.
- **Echte Datei-Operationen, echte HTTP-Calls, echte DB.** Mocks nur
  wenn ein Service real nicht erreichbar ist (z.B. SMTP).
- **Cleanup im finally-Block.** Tests müssen idempotent sein und nach
  einem Lauf den DB- und Disk-Zustand zurücklassen, wie sie ihn
  vorgefunden haben. Insbesondere: Immich-Assets, die vom Test
  hochgeladen wurden, via `delete_asset()` wieder löschen.
- **Test-Daten unter Pfaden, die der Filewatcher NICHT scannt** (z.B.
  `/app/data/__source_X.HEIC`), sonst Race mit Pipeline-Worker.
- Bei Tests, die den Filewatcher provozieren würden, das Modul
  temporär deaktivieren (`Module.enabled=False`) und am Ende
  restaurieren.

## Logging-Pflicht: Vor UND Nach jeder destruktiven Aktion

> Jede Operation die Daten verändert oder löscht MUSS **vor** und
> **nach** der Aktion geloggt werden. Wenn der Server zwischen den
> beiden Logs abstürzt, muss aus dem Log ersichtlich sein was
> passiert ist.

**Pattern:**
```python
await log_info("module", f"{key} Aktion wird ausgeführt", f"details...")
try:
    await destructive_action()
    await log_info("module", f"{key} Aktion OK", f"result...")
except Exception as exc:
    await log_info("module", f"{key} Aktion fehlgeschlagen", f"error={exc}")
```

**Gilt für:**
- Immich-Asset löschen (`delete_asset`)
- Immich-Asset hochladen/ersetzen (`upload_asset`, Upload→Copy→Delete)
- Dateien verschieben/löschen (`safe_move`, `safe_remove`)
- Job-Status-Änderungen bei Duplikat-Auflösung (Keep, Batch-Clean)
- Pipeline Duplikat-Erkennung (IA-02 Verschiebung in Duplikat-Ordner)

**Gilt NICHT für:** Reine Lese-Operationen, DB-Queries, Config-Reads.

Kein `except: pass` bei destruktiven Aktionen. Fehler müssen geloggt
werden — auch wenn die Aktion optional ist.

---

## Pipeline-Stolperfallen

### `original_path` vs. `target_path`
- `original_path` = wo die Datei AKTUELL liegt (mutiert über die
  Pipeline hinweg, z.B. nach `_move_file_for_reprocess`).
- `target_path` = wo IA-08 sie hingelegt hat. Entweder lokaler
  Library-Pfad ODER `immich:<asset_id>`-Referenz. Wird vom Pipeline-
  Error-Handler `_move_to_error()` auch auf `/library/error/...` gesetzt.
- **Niemals annehmen, dass `target_path` nach einem Retry gleich
  bleibt.** Cf. v2.28.28/29 — drei Bugs auf dieser Achse.

### Atomic Claim Pattern
Vor jeder kritischen Status-Transition (`queued → processing`,
`error → processing`) wird ein atomic SQL `UPDATE … WHERE status=?`
gemacht und `rowcount` geprüft. Genau ein Aufrufer gewinnt. Das
ist die einzige Race-Defense — nicht aushebeln.

### Finalizer (IA-09/10/11)
Laufen immer, auch nach Critical-Failure. Heißt: dein neuer
Pipeline-Code in IA-10 wird auch bei kaputten Jobs ausgeführt. **Defensiv
programmieren — Cleanup-Code darf nicht selbst crashen, sonst geht
ein folgender Finalizer-Step verloren.**

### Immich-Poller-Tempdirs
Nur Dateien, die der Immich-Poller heruntergeladen hat
(`source_label='Immich'`, `original_path` startet mit
`/tmp/ma_immich_`), dürfen von IA-10 gelöscht werden. Niemals beliebige
Dateien mit `immich_asset_id` als Lösch-Trigger nehmen — das war der
Live-Bug v2.28.28.

### `safe_move` ist nicht `os.rename`
`safe_move(src, dst)` macht copy → SHA256-verify → delete. Sicher gegen
Stromausfall und Filesystem-Glitches. Niemals durch `shutil.move()` oder
`os.rename` ersetzen.

### Pipeline-Auto-Pause
`AIConnectionError` und `GeocodingConnectionError` setzen
`config.pipeline.paused=True` global. Health-Watcher pollt alle 30s
und resumed automatisch. Wenn du einen Test schreibst, der absichtlich
das AI-Backend ausfallen lässt, wirst du die ganze Pipeline pausieren —
restaurieren im finally.

## Vor jedem Fix

Mantra:
1. **Was sagt die DB?** `python3 -c "import sqlite3; …"` auf
   `dataLiveSystem/mediaassistant.db` (für Live-Bugs) oder
   `data/mediaassistant.db` (für Dev). Suche relevante Job-Rows
   und ihren `step_result`.
2. **Was sagen die system_logs?** `SELECT … FROM system_logs WHERE
   message LIKE '%MA-2026-XXXX%'`. Zeitlinie rekonstruieren.
3. **Welcher Code-Pfad genau?** Nicht raten — `Read` und `Grep`
   bis du den exakten Branch siehst.
4. **Reproducer schreiben** — siehe oben.
5. **Erst dann fix.**

## Release-Workflow

1. `backend/version.py`: `VERSION` und `VERSION_DATE` bumpen.
2. `CHANGELOG.md`: neuen Eintrag oben einfügen, Format siehe oben.
3. **Wenn ein vollständiger Test-Lauf gemacht wurde:** neue Spalte in
   `TESTRESULTS.md` einfügen (Datum, Release, Commit, pro Test-Skript
   Pass/Fail-Counts). **Nur abstrakte Zahlen — keine personenbezogenen
   Daten, keine echten Datei-Inhalte, keine Mail-Adressen, Hostnames,
   IP-Adressen, GPS-Koordinaten oder Immich-Asset-IDs.**
4. Nur die geänderten Files stagen (kein `git add -A`!).
5. Commit-Message als HEREDOC, signiert mit
   `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`
   wenn der Code per AI-Agent kam.
6. **Beide Remotes pushen** — origin (Gitea) UND github:
   ```bash
   git push origin master
   git push github master
   ```
7. **Tag erstellen + auf BEIDE Remotes pushen**:
   ```bash
   git tag -a v2.X.Y -m "kurze Release-Notes hier"
   git push origin v2.X.Y
   git push github v2.X.Y
   ```
   Der GitHub-Action `docker-publish.yml` baut bei jedem `v*`-Tag-Push
   automatisch das Docker-Image und published es als `:2.X.Y` UND
   `:latest` auf `ghcr.io/marcohediger/mediaassistant`. Ohne Tag passiert
   nichts — git-Push allein reicht nicht.
8. **GitHub Release UND Gitea Release erstellen** (Pflicht — sonst bleibt
   das `Latest`-Badge auf der alten Version stehen). Ein Tag-Push allein
   reicht nicht; Releases sind eine separate API-Resource.

   GitHub via API (Token aus `git remote get-url github` parsen):
   ```bash
   TOKEN=$(git remote get-url github | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
   curl -X POST https://api.github.com/repos/marcohediger/mediaassistant/releases \
     -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.github+json" \
     -d '{"tag_name":"v2.X.Y","name":"v2.X.Y","body":"<changelog-text>",
          "draft":false,"prerelease":false,"make_latest":"true"}'
   ```

   Gitea via API (Credentials aus `git remote get-url origin`):
   ```bash
   CREDS=$(git remote get-url origin | sed -n 's|.*://\([^@]*\)@.*|\1|p')
   curl -X POST https://git.marcohediger.ch/api/v1/repos/MediaAssistant/ma-core/releases \
     -u "$CREDS" -H "Content-Type: application/json" \
     -d '{"tag_name":"v2.X.Y","name":"v2.X.Y","body":"<changelog-text>",
          "draft":false,"prerelease":false}'
   ```

   Tipp: lass dir vom CHANGELOG.md per Python den passenden Body
   extrahieren statt manuell zu kopieren. Dann sind beide Releases
   konsistent mit dem CHANGELOG.

9. **Niemals** `--no-verify`, `--force`, `--amend` ohne explizite
   User-Erlaubnis. Niemals interaktive git-Modi (`-i`).

## Was NICHT tun

- ❌ Tests mit Mocks schreiben, wenn dev real verfügbar ist.
- ❌ Fix vor Reproducer.
- ❌ Aggressive Fallbacks/Validierungen für Zustände, die nicht
  passieren können. Nur an System-Grenzen validieren (User-Input,
  externe APIs).
- ❌ Hilfsfunktionen für einmalige Operationen.
- ❌ Backwards-Compat-Shims für Code, den du sowieso ändern darfst.
- ❌ Force-push, --amend (außer auf User-Wunsch), --no-verify.
- ❌ Den Dev-Container nach Tests stoppen.
- ❌ Code-Kommentare auf Deutsch.
- ❌ Speculative Features ("falls der User später X braucht").
- ❌ User-facing Strings hardcoden — `i18n/de.json` + `i18n/en.json`
  ergänzen.

## Wichtige Live-Vorfälle als Referenz

| Job | Was war | Fix-Version |
|---|---|---|
| `MA-2026-28123` (IMG_3140.HEIC) | Retry hat die Datei via IA-10-Cleanup gelöscht | v2.28.28 |
| `MA-2026-15415`, `-23077`, `-22930` | Endlos-Retry-Loop nach verschwundener Inbox-Datei | v2.28.28 |
| `MA-2026-X` (file-storage) | Retry strandete Datei in `reprocess/`, kam nie zurück nach `library/` | v2.28.29 |
| Bulk-Retry exhausted DB-Pool (33 parallele tasks → pool=15) | Pool-Tuning auf 20/40, sequentiell statt parallel | v2.28.7 |
| Race: 5 Aufrufer von `run_pipeline` | atomic claim via `UPDATE … WHERE status='queued'` | v2.28.2 |
| Folge-TOCTOU bei `retry_job` zwei Commits | transienter Lock-State `error → processing → queued` | v2.28.3 |
| `MA-2026-28103` (IMG_2499.HEIC) | Zirkuläre Duplikat-Erkennung: Retry wurde Duplikat seines eigenen Duplikats | v2.28.43 |
| `MA-2026-0209` | "Dieses behalten" → IA-02 flaggte Job sofort wieder als Duplikat | v2.28.61 |
| 6554 defekte Sidecars | v2.28.13-Bug: ExifTool `.tmp` Extension → binäre Bild-Kopien statt XMP | Ext. Tool `ma-sidecar-repair` |

Weitere Details siehe `CHANGELOG.md`.

## Duplikat-System (v2.28.44–v2.28.66)

### Quality-Score (`_quality_score()` in `step_ia02_duplicates.py`)
Vergleichbarer Tuple-Score für Duplikat-Paare:
```
(format_score, file_size_log, pixel_count, metadata_score, -job_id)
```
- **Format:** RAW(5) > HEIC(4) > TIFF(3) > JPEG(2) > PNG/WebP(1)
- **Dateigrösse:** log2-skaliert (~7% Toleranz), grösser = besser
- **Pixel:** width × height
- **Metadaten:** GPS(+2), EXIF(+1), Datum(+1), Kamera(+1), Keywords(+1-5), Description(+2)
- **Job-ID:** negativ → älterer Job (Original) gewinnt bei Gleichstand

### Batch-Clean Quality (`POST /api/duplicates/batch-clean-quality`)
- Verarbeitet exakte SHA256 UND pHash-100% Matches
- Behält pro Gruppe den besten Quality-Score
- Merged Metadaten (GPS, Datum, Keywords, Description) von schlechteren
- Behaltene Duplikate: Analyse-Steps vom Original kopiert → nur IA-07/08 läuft
- `prepare_job_for_reprocess` verschiebt Datei korrekt
- IA-02 wird als `skipped` injiziert (folder_tags erhalten)

### Vollständiger Daten-Merge (v2.29.5)

Beim Auflösen einer Duplikatgruppe (Keep this / Batch-Clean) werden
**alle Informationen aller aufgelösten Assets** auf das Ziel übertragen.

**IA-02 step_result Felder:**
- `folder_tags` — gemergte Folder-Tags aller Members (für Keywords)
- `own_album` — Album-Name des behaltenen Jobs (vor Merge gesichert)
- `donor_albums` — Alben der gelöschten Donors (aus Immich abgefragt)

**Donor-Album Fallback-Kette:**
1. `get_asset_albums()` — Donor hat Immich-Asset
2. `IA-08.immich_albums_added` — Donor lief durch Pipeline
3. `folder_tags[-1]` — Donor war Duplikat (nie hochgeladen)

**Zwei Pfade:**
- **Reprocess (kept was duplicate):** Pipeline läuft IA-03..08.
  `_get_folder_album_names()` liest `own_album` + `donor_albums` aus IA-02.
- **Already-done (kept was original):** Kein Pipeline-Re-run. Tags,
  Alben, Description werden direkt via Immich API angewendet
  (`tag_asset`, `add_asset_to_albums`, `update_asset_description`).

**Wichtig:** Album-Namen fliessen auch in `keywords_written` (IA-07),
damit sie als Immich-Tags UND File-Keywords geschrieben werden.

### CSV-Retry Input (`/app/data/csv-retry/`)
- CSV mit `filename`-Spalte → Filewatcher erkennt → passende Jobs auf `queued`
- Verarbeitete CSVs werden nach `csv-retry/done/` verschoben
- Generischer Bulk-Retry-Mechanismus (nicht nur für Ghost-Tags)

### Externe Tools
| Tool | Repo | Zweck |
|---|---|---|
| `ma-sidecar-repair` | [GitHub](https://github.com/marcohediger/ma-sidecar-repair) / [Gitea](https://git.marcohediger.ch/MediaAssistant/ma-tools-sidecar-repair) | Defekte XMP-Sidecars reparieren (v2.28.13-Bug) |
| `ma-ghost-tag-detect` | [GitHub](https://github.com/marcohediger/ma-ghost-tag-detect) / [Gitea](https://git.marcohediger.ch/MediaAssistant/ma-tools-ghost-tag-detect) | Ghost-Tags erkennen → CSV für csv-retry |

### Immich Sidecar-Sync (getestet April 2026, Immich v2.6.3)
| Richtung | Verhalten |
|---|---|
| .xmp → Immich DB (Force Sync) | **ERSETZEND** — Tags aus .xmp ersetzen DB-Tags |
| Immich DB → .xmp (SidecarWrite) | Nur Description, NICHT dc:subject (Tags) |
| Unveränderte .xmp + Sync | Übersprungen (mtime-Check) |

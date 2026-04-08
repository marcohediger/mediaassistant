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

### 2026-04-08 (v2.28.29)

- Retry-File-Lifecycle-Tests neu in v2.28.28 hinzugekommen, in v2.28.29
  um File-Storage-Variante und Error-Retry erweitert. 46 Asserts grün.
- `test_duplicate_fix.py` Tests 7+8 mussten auf reale 0-Byte-Dummy-Files
  umgestellt werden, weil der neue `reset_job_for_retry`-Vertrag eine
  existierende Quelldatei voraussetzt.
- 1 BLOCK in `test_testplan_final.py` Sektion 1+6: erwartete HEIC-
  Testdatei fehlt im Container (preexisting, nicht regression).

### 2026-04-07 (v2.28.3)

- Race-Condition-Suite (Tests 5–8) hinzugekommen.
- Vollständiger Lauf 92/92 grün.

### 2026-04-02 (v2.17.1)

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

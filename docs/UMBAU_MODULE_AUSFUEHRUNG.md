# Umbau „Module & Datensicherheit": Module einzeln ausführen und Ausführung nach Abhängigkeiten (Schritte 5 und 6)

Branch `umbau/module-und-datensicherheit`. Grundlage: [`UMBAU_MODULE.md`](UMBAU_MODULE.md) (Ausführung, Reihenfolge, R-11, R-12), [`UMBAU_MODULE_INVENTUR.md`](UMBAU_MODULE_INVENTUR.md) Deliverable 3. Code-Stand: Arbeitsbaum vom 2026-10-23 mit Schritt 3 (`runs.py`, `module_runs`, Commit cbd6613). Zeilen relativ zu `backend/`; sie verschieben sich mit späteren Commits.

## 0. Entscheide auf einen Blick

1. **Neu ausführen geht über den Job-Claim wie der Retry:** `done|review → processing → queued`, die gewählten Module bekommen einen Lauf `queued` (Auslöser `rerun`), der Worker nimmt den Job wie jeden anderen. Kein Weg an `run_pipeline` vorbei.
2. **Kaskade nach Fingerabdruck, nicht vorab:** Ein abhängiges Modul wird erst ersetzt, wenn sein Vorgänger neu gelaufen ist und sich die gelesenen Felder geändert haben. „Geocoding neu" mit gleichem Ort ändert nur IA-03 und die Abschluss-Module.
3. **IA-02 läuft nie einzeln** (ein fertiger Job fände seinen Inbox-/Poller-Zwilling und würde verschoben), IA-04 nur als Voraussetzung, IA-09…11 immer.
4. **Ablage-Jobs an Ort und Stelle** (kein Umweg über `reprocess/`); IA-08 verschiebt nur bei anderem Zielordner.
5. **Immich-Jobs: Vorgabe „nur Tags und Beschreibung über die API"**, kein Download, kein Ersetzen; „Datei ersetzen" als Option. Download nur, wenn ein Modul die Datei liest, in eine eindeutige Arbeitskopie.
6. **Neu ausführen setzt nie `error`, schiebt nie nach `error/`.** Scheitert ein Modul, gilt sein alter Lauf wieder (ausser IA-08).
7. **Parallel nur IA-03 ∥ IA-04**, IA-03 in eigener Session (liest nur IA-01, schreibt keine Job-Spalte). Sonst wie heute nacheinander.
8. **Betriebsmittel statt Job-Slots:** globale Grenzen `ai`, `geo`, `immich`, `io`, `cpu`; mehr Jobs gleichzeitig (`2 × KI-Slots + 2`), die Job-Grenze schützt nur noch DB-Verbindungen und Speicher.
9. **Massenläufe über `reruns`/`rerun_items` und einen Zuführer**, der höchstens die Hälfte der Job-Plätze belegt; neue Dateien zuerst.

## 1. Modulregister

Neu `pipeline/registry.py`: nur Metadaten. Die Step-Funktionen holt der Ausführer bei jedem Lauf aus `pipeline.MAIN_STEPS`/`FINALIZERS` (Tests ersetzen sie: `test_duplicate_fix.py:154-164` per Zuweisung, `test_module_runs.py:689-702` per Index).

```
IA-01 ─> [Regel-Vorprüfung] ─> IA-02 (Schranke) ─┬─> IA-03 (geo, neben dem Hauptpfad) ─┐
                                                 └─> IA-04 (+ Video-pHash) ────────────┴─> IA-05 ─> IA-06 ─> IA-07 ─> IA-08
                                                       danach immer, nacheinander: IA-09, IA-10, IA-11
```

### 1.1 Module

| Modul | wartet auf | liest (Felder) | Schranke / Fehler | Betriebsmittel | destruktiv | einzeln | Datei nötig | Idempotenz |
|---|---|---|---|---|---|---|---|---|
| IA-01 | – | Datei, Set `google_json` (`ia01:14-107`) | kritisch | cpu | nein | ja | ja | lesend; liest nach IA-07 direkt die schon beschriebene Datei |
| IA-02 | IA-01, Regel-Vorprüfung (`__init__:196-216`) | `file_hash`, pHash, andere Jobs (`ia02:176-329`) | **Schranke**: Duplikat → Rest übersprungen; Fehler weich | cpu | **ja**: verschiebt nach `duplicates/` (`ia02:423-516`) | **nein** | ja | nicht idempotent auf fertigem Job (Zwillinge, live 3 724 Paare) |
| IA-03 | IA-02 | IA-01 `gps_lat`, `gps_lon` (`ia03:195-197`); `geo.provider`, `geo.url`, Modul `geocoding` | weich; `GeocodingConnectionError` → Auto-Pause | geo (`_throttle` 1,1 s, `ia03:43-51`) | nein | ja | nein | idempotent bis auf Anbieter |
| IA-04 | IA-02 | Datei; IA-01 `duration` nur Video (`ia04:156-158`); `video.*` | weich; danach Video-pHash, der IA-02 überstimmt (`__init__:250-257`) | cpu | nein | nein (Voraussetzung) | ja | räumt eigene Reste (`ia04:27-28`); **Ausgabe flüchtig**, IA-10 löscht sie (`ia10:44-61`) |
| IA-05 | IA-03, IA-04 | IA-01 Grösse/EXIF/Typ, IA-03 Ort (Kontext), IA-04 `converted`/`temp_path(s)`, Kategorien, Regeln, `ai.*` (`ia05:161-243`) | weich (Ersatzwerte `__init__:104-112`); `AIConnectionError` → Auto-Pause | ai | nein | ja | ja | nicht deterministisch |
| IA-06 | IA-05 | Set `ocr_enabled/ocr_mode`; IA-05 `type/source/parse_error` nur „smart"; IA-04 `temp_path` (`ia06:28-45`) | weich | ai | nein | ja | ja | nicht deterministisch |
| IA-07 | IA-06 | IA-01 `file_type`, `google_json*`; IA-02 Ordner-Felder (`folder_path.py:267-305`); IA-03 Ort; IA-05 `type,tags,source,quality,description`; IA-06 `has_text,text`; Set Schreibmodus, Ordner-Optionen; `dry_run` (`ia07:29-165`) | **kritisch** | cpu | **ja**: Datei (direkt) bzw. XMP | ja | ja, ausser Immich-API-Modus | **nicht idempotent**: `-Keywords+=` (`ia07:194-197`), Sidecar nur hinzu (`ia07:246-247`), vorhandene Beschreibung bleibt (`ia07:244`) |
| IA-08 | IA-07 | IA-01 `file_type,mime_type,has_exif,date,make,model`; IA-02 (Alben/Ordner-Tags); IA-03 `country,city` (Pfad); IA-05 `type,nsfw`; IA-07 `keywords_written,description_written,write_mode,sidecar_path`; Parameter `_retry_previous_immich_*`; Protokoll (`ia08:509-648`) | **kritisch**; Regel „skip" → übersprungen; `unknown` → review | immich (Immich-Ziel) / io (Ablage) | **ja** | ja | Ablage ja; Immich nur „ersetzen" | Ablage in place bei gleichem Ziel (`ia08:673-679`); Tags über Vorliste idempotent; Alben nur hinzu; Archiv nur an; Ersetzen = neue Asset-ID |
| IA-09 | alle | Status aller Module (`ia09:17-22`) | Abschluss | – | nein | immer | nein | schickt jedes Mal |
| IA-10 | alle | IA-01 `google_json_path`, IA-04 Temp, IA-07 `sidecar_*`, Job (`ia10:14-148`) | Abschluss | – | ja (über fileops) | immer | nein | idempotent |
| IA-11 | alle | IA-02, 03, 05, 06, 07, 08 | Abschluss | – | nein | immer | nein | Log-Zeile je Lauf |

### 1.2 Korrekturen an der Tabelle in UMBAU_MODULE.md

1. IA-03 und IA-04 lesen **nichts** von IA-02. IA-02 ist nur Reihenfolge und Schranke, keine Daten-Kante.
2. IA-04 liest IA-01 nur bei Videos (`duration`).
3. IA-07 liest IA-04 **nicht** („IA-01 … IA-06" heisst richtig IA-01, 02, 03, 05, 06).
4. IA-06 braucht IA-05 nur im OCR-Modus „smart"; „immer" braucht nur IA-04.
5. IA-05 nutzt IA-01 und IA-03 nur als Kontext für die KI → **Hinweis-Kanten**, Kaskade nur auf Wunsch.
6. IA-04 hat eine **flüchtige Ausgabe** (IA-10 löscht die Temp-Dateien): jeder Neulauf von IA-05/IA-06 braucht IA-04 erneut.
7. Nach IA-04 schreibt der Video-pHash **IA-02 neu** (zweiter Schreiber, Inventur 3.3.5) und kann den Job als Duplikat stoppen — ein paralleles IA-03 muss einen Stopp vertragen.
8. IA-02 ist **destruktiv** (verschiebt), nicht nur Schranke.
9. Die Regel-Vorprüfung vor IA-02 gehört zur Schranke (`__init__:196-216`).
10. IA-08 liest ausserdem Laufparameter und das Protokoll (`fileops.last_result`).

### 1.3 Kanten

| Art | Bedeutung | Kanten |
|---|---|---|
| Reihenfolge `requires` | muss beendet sein | IA-02←IA-01; IA-03, IA-04←IA-02; IA-05←IA-03, IA-04; IA-06←IA-05; IA-07←IA-06; IA-08←IA-07 |
| Daten `reads` (stark) | Änderung → Kaskade | IA-01→IA-03, 04, 07, 08, 10 · IA-02→IA-07, 08, 11 · IA-03→IA-07, 08, 11 · IA-04→IA-05, 06 · IA-05→IA-06 (smart), 07, 08, 11 · IA-06→IA-07, 11 · IA-07→IA-08, 10, 11 · IA-08→IA-10, 11 |
| Hinweis `hints` | nur KI-Kontext | IA-01→IA-05, IA-03→IA-05 |
| Voraussetzung `ephemeral` | Ausgabe nach dem Lauf weg | IA-04 für IA-05, IA-06 |

### 1.4 Form im Code

```python
@dataclass(frozen=True)
class ModuleSpec:
    code: str
    requires: tuple[str, ...]
    reads: dict[str, tuple[str, ...]]        # stark: Vorgänger -> gelesene Felder
    hints: dict[str, tuple[str, ...]]        # schwach
    resource: str | Callable                 # "cpu" | "ai" | "geo" | "immich" | "io" | "none"
    critical: bool
    destructive: bool
    standalone: bool
    needs_file: Callable                     # (job, options) -> bool
    ephemeral: bool = False
    side_session: bool = False               # nur IA-03
    fp_version: int = 1
```
Dazu `dependents(code, hints=False)`, `fingerprint(code, job, view, cfg)`, `settings_snapshot(session, job)`. `job_view.CRITICAL` (`job_view.py:52`) und `reprocess._PIPELINE_ORDER` (`reprocess.py:500-503`) lesen künftig hier.

## 2. Neu ausführen: Semantik

### 2.1 Ablauf „IA-03 neu" (Job `done`)

| Wann | Läufe | Job |
|---|---|---|
| Vorbereitung (`pipeline/rerun.py`, durch den Zuführer) | IA-03: aktueller Lauf `superseded`, neuer `queued`, Auslöser `rerun`. IA-09, 10, 11: ebenso, Auslöser `cascade`. Alles andere bleibt gültig. | Claim `done → processing`; Prüfungen (2.5); Quelle (2.3); Laufparameter (2.8); `original_path` = Quelle; `queued`. Läufe, Parameter, Status und Auftrag in **einer** Transaktion, nach allen fileops-Aufrufen |
| Worker | | Claim `queued → processing` (`__init__:138-149`) |
| IA-01, IA-02 | gültig, laufen nicht | |
| IA-03 | Claim `queued → running`, dann `ok` | |
| Kaskaden-Entscheid | IA-07 und IA-08 (IA-05 nur mit „KI auch neu"): Fingerabdruck jetzt ≠ gespeicherter → alter Lauf `superseded`, neuer `queued`, Auslöser `cascade`; gleich → bleibt | |
| IA-07 (falls) | ersetzt veraltete eigene Stichwörter (2.4) | Ablage an Ort und Stelle, Immich siehe 2.4 |
| IA-08 (falls) | Tags/Beschreibung nachführen, ggf. Ordner wechseln | |
| IA-09…11 | immer | |
| Ende | Parameter gelöscht (`__init__:356-358`) | Status neu aggregiert (done/review, „Warnungen in:"), `completed_at`; Auftrag `done`/`unchanged`/`failed` in derselben Transaktion |

IA-04, IA-05, IA-06 laufen nicht; IA-05 wird „veraltet" angezeigt, wenn sich der Ort änderte.

### 2.2 Kaskade

| `cascade` | Verhalten |
|---|---|
| `auto` (Vorgabe) | Abhängige laufen, wenn sich ihr Fingerabdruck geändert hat (2.7). Ohne gespeicherten Fingerabdruck (`migrated`, `copied`): Vergleich der gelesenen Felder des neuen mit dem ersetzten Vorgänger-Ergebnis. |
| `all` | alle stark Abhängigen laufen nach |
| `off` | nur die gewählten; Abhängige werden „veraltet" angezeigt |
| `hints: true` | folgt zusätzlich den Hinweis-Kanten (kostet KI-Aufrufe) |

- Ein Modul läuft pro Ausführung höchstens einmal. Entschieden wird nur, wenn ein Vorgänger in **dieser** Ausführung neu lief (`D.current.id < U.current.id`). Der Entscheid liest nur Läufe, nach einem Absturz kommt dasselbe heraus.
- Ein gewähltes Modul, dessen neues Ergebnis `skipped` oder `error` ist, liefert **kein** neues Ergebnis: alter Lauf zurück (2.6), keine Kaskade. Sonst würde z. B. ein ausgeschaltetes Geocoding die Orts-Tags löschen.
- Läuft IA-05 oder IA-06, läuft vorher IA-04, wenn seine Temp-Dateien fehlen (Auslöser `cascade`, Grund „Arbeitsdateien fehlen").
- Nie gelaufene, nicht gewählte Module laufen im Neu-Ausführen **nicht** (alte Jobs ohne IA-06 bekommen nicht plötzlich OCR). Die Steps vertragen fehlende Ergebnisse schon (`.get(…, {})`).
- **Pipeline-Lauf unverändert:** Wiederaufnahme nach „beendeter Lauf = erledigt" (`runs.is_done`, `runs.py:144-146`). Fingerabdrücke entscheiden dort nichts.

### 2.3 Quelle der Datei

| Ziel | Datei nötig? | Quelle | `original_path` während des Laufs |
|---|---|---|---|
| Ablage | immer (IA-07/IA-08) | eigenes Exemplar am Ablageort (`reprocess.own_local_copy`, `reprocess.py:251-260`): vorhanden, Grösse wie im Register; den Inhalt prüft fileops bei jeder Operation (`_write_part` mit `sha256_before`, `fileops.py:341-356`). Kein Umzug. Fehlt es → `no_copy` | Ablagepfad |
| Immich, kein Modul liest die Datei | nein | keine; Asset geprüft (`fileops.verify`: vorhanden, nicht im Papierkorb) → sonst `asset_missing` | `immich:<asset_id>` — sonst nähme IA-08 eine fremde Datei am alten Inbox-Pfad als Quelle und legte sie in den Papierkorb (`ia08:735`, `:777-780`; R-01) |
| Immich, Datei nötig (IA-01, IA-04/05/06, „ersetzen") | ja | eigenes lokales Exemplar mit dem SHA-256 des Assets, sonst `fileops.download` in eine **eindeutige** Arbeitskopie in `reprocess/` (`fileops.py:931-969`, R-03), registriert als `work` | Arbeitskopie |
| Probelauf (Datei in der Inbox) | ja | Inbox-Datei an Ort und Stelle | Inbox-Pfad |

„Datei nötig" wird vorsichtig bestimmt: gewählte Module plus alle, die die Kaskade erreichen *könnte*.
Aufräumen: IA-08 entfernt die Arbeitskopie nach geprüfter Übergabe wie heute (`ia08:436-463`); läuft IA-08 nicht, tut es IA-10 (neuer Teil `_cleanup_rerun_copy`, nur `_rerun.source.copy_id`, nur über `fileops.discard`).

### 2.4 Wie IA-07 und IA-08 die Änderung übernehmen

| Ziel | Fall | Verhalten |
|---|---|---|
| Ablage, direkt | IA-07 | `write_metadata` an Ort und Stelle (atomar); entfernt `_previous_keywords_written − neu` aus Keywords/Subject, TagsList, HierarchicalSubject; Beschreibung überschrieben |
| Ablage, Sidecar | IA-07 | Sidecar daneben ergänzt; alte eigene Stichwörter raus; Beschreibung ersetzt, wenn sie `_previous_description_written` ist, sonst bleibt sie (Benutzer) |
| Ablage | IA-08, Ordner gleich | keine Verschiebung (`ia08:673-679`), Sidecar bleibt |
| Ablage | IA-08, Ordner anders (`{CITY}`, `{COUNTRY}`, Datum, Kategorie, Basis-Pfad) | `fileops.move` (nie überschreiben, `+N`), Sidecar zieht mit |
| Ablage | Kategorie wird `unknown` | Ordner `unknown/…`, Status `review` |
| Immich, API (Vorgabe) | IA-07 | keine Datei; Ergebnis `write_mode: "immich_api"`, `keywords_written`, `description_written`, `file_unchanged: true` |
| Immich, API | IA-08 | Zweig „gleiche Bytes" (`ia08:868-890`): Tags hinzu, eigene alte weg (`_retry_previous_immich_tags`), Beschreibung nur über eigene alte (`ia08:378-397`), Alben nur hinzu; kein Upload, keine Quelle entfernen |
| Immich, ersetzen | IA-07 direkt | schreibt in die Arbeitskopie |
| Immich, ersetzen | IA-08 | Bytes anders → `fileops.replace` (neues Asset, Metadaten übernommen, altes in Immichs Papierkorb nur wenn eigenes und nicht geteilt), `immich_asset_id` neu. Sidecar-Modus: Bytes gleich → wie API |
| Probelauf | IA-07/IA-08 | `dry_run`, keine Änderung |

Nötige Code-Änderungen (Backend):
1. **IA-07:** Parameter `_previous_keywords_written`, `_previous_description_written`; API-Zweig **vor** der Endungsprüfung (`ia07:31-33`); veraltete flache Einträge auch aus TagsList (`_tags_list`, `ia07:364-399`).
2. **IA-08:** Regeln mit `job.filename` statt `basename(original_path)` (`ia08:527-532`, ebenso `ia05:234`, `__init__:204`), sonst werten `immich:<id>` oder `name_<debug_key>.jpg` falsch aus. `write_mode == "immich_api"` zählt als „Datei unverändert" (`ia08:846-864`). Regel „skip" im Neu-Ausführen = Warnung statt Status `skipped`. `_manual_category` (Review, `routers/review.py:226`) hat Vorrang vor Regel und KI. Sidecar-Rückfall `src + ".xmp"`, wenn das Register sie als Sidecar des Jobs führt (das IA-07-Ergebnis nennt nach der Ablage den alten Ort).
3. **IA-09:** nur Fehler der Module, die in diesem Lauf liefen.
4. **IA-10:** `_cleanup_rerun_copy`.
5. **Pipeline:** Video-pHash (`__init__:250-257`) und `_move_to_error` (`__init__:329-330`) nicht im Neu-Ausführen.
6. **Worker:** `fetch_immich_source` lässt Jobs mit `_rerun` in Ruhe (`worker.py:116-140` würde sonst einen Poller-Job mit gleichem Inhalt `skipped` setzen und alle Läufe ersetzen).

### 2.5 Regeln je Job-Zustand

| Zustand | Neu ausführen? | Schlüssel |
|---|---|---|
| done, review | ja | |
| done „Duplicate deleted…" (`reprocess.resolved_donor`, `reprocess.py:469-475`) | nein | `resolved_donor` |
| error | nein → „Erneut verarbeiten" | `status_error` |
| duplicate | nein → Duplikat-Prüfung | `status_duplicate` |
| skipped / orphan / deleted | nein | `status_skipped` / `status_orphan` / `status_deleted` |
| queued, processing | nein | `busy` |
| Probelauf in done/review | ja, ohne Wirkung | |
| Set gelöscht (`get_set` fiele aufs erste Set, `sets.py:89-97`) | nein | `set_missing` |
| lokales Exemplar mitbenutzt + IA-07 direkt im Plan (`fileops.py:983-994`) | nein | `shared_copy` |
| Immich-Asset fehlt/Papierkorb | nein | `asset_missing` |
| schon vorgemerkt | nein | `already_pending` |

Geprüft wird zweimal: in der Vorschau (SQL + Register, billig) und bei der Vorbereitung nach dem Claim (verbindlich).

### 2.6 Fehler, Ausfall, Abbruch

| Ereignis | Läufe | Job | Auftrag |
|---|---|---|---|
| IA-01, 03, 04, 05, 06 oder 07 endet mit Fehler/ohne Ergebnis | Fehler-Lauf ersetzt, alter wiederhergestellt (`runs.restore`, Auslöser `copied`, Herkunft = alter Lauf); keine Kaskade darunter | Status wie vorher | `failed` + Grund |
| IA-08 endet mit Fehler | Fehler-Lauf bleibt aktuell (Immich/Ablage evtl. teilweise geändert) | done/review, „Warnungen in: IA-08" | `failed` |
| `AIConnectionError` / `GeocodingConnectionError` | alter Lauf zurück | Status wie vorher; Auto-Pause wie heute (`__init__:266-297`) | `deferred` → `pending` |
| Quelle fehlt | nichts geändert | Status wie vorher | `refused` `no_copy` |
| Abbruch | – | – | `pending` → `cancelled`; Vorbereitete/Laufende laufen fertig |

`retry_count` bleibt; Neu ausführen setzt nie `error`.

### 2.7 inputs_fingerprint

`sha256(json.dumps({"m": code, "v": fp_version, "file": job.file_hash | null, "in": {Vorgänger: {Feld: Wert}}, "cfg": {…}}, sort_keys=True))[:32]`, gesetzt bei `runs.start` (der Wächter `models.py:343-357` erlaubt Änderungen nur am offenen Lauf). Keine Lauf-IDs, keine Zeit, keine Geheimnisse, keine Optionen des Neu-Ausführens.

| Modul | Datei | Vorgänger-Felder | Einstellungen |
|---|---|---|---|
| IA-01 | ja | – | Set `google_json` |
| IA-02 | ja | – | Modul an, `duplikat.phash_threshold`, `duplikat.raw_jpg_pair` |
| IA-03 | – | IA-01 `gps_lat, gps_lon` | Modul an, `geo.provider`, `geo.url` |
| IA-04 | ja | IA-01 `duration` | `video.thumbnail_enabled/frames/scale` |
| IA-05 | ja | IA-01 `width,height,has_exif,make,model,date,gps,gps_lat,gps_lon,software,file_type,mime_type,duration`; IA-03 `country,state,city,suburb`; IA-04 `converted` | Hash `ai.prompt`, Modelle, `ai.image_resize/max_px`, Kategorien (key, label), Regeln |
| IA-06 | ja | IA-05 `type,source,parse_error` (smart); IA-04 `converted` | `ocr_enabled`, `ocr_mode` |
| IA-07 | ja | IA-01 `file_type,google_json,google_json_path,google_json_description`; IA-02 `folder_path,donor_folder_paths,folder_tags,own_album,donor_albums`; IA-03 `country,state,city,suburb`; IA-05 `type,tags,source,quality,description`; IA-06 `has_text,text` | `metadata_write_mode`, `folder_tag_mode`, `folder_albums`, `dry_run` |
| IA-08 | – | IA-01 `file_type,mime_type,has_exif,date,make,model`; IA-02 Ordner-Felder; IA-03 `country,city`; IA-05 `type,nsfw`; IA-07 `keywords_written,description_written,tag_paths_written` | `use_immich`, `immich_user_id`, `library_base_path`, Kategorien (key, label, path_template, immich_archive), Regeln, Ordner-Optionen, `dry_run` |
| IA-09…11 | – | – | keiner (immer) |

Verwendung: (a) Kaskade (2.2); (b) „veraltet" in der Job-Ansicht = aktueller beendeter Lauf, Fingerabdruck ≠ jetzt; (c) Filter „nur veraltete" für Set-Läufe (z. B. nach Änderung der Pfad-Vorlage). `NULL` (migriert, übernommen, `record()`) = unbekannt, nie „veraltet".

### 2.8 Laufparameter (Job-Werte mit „_", R-12)

```json
"_rerun": {"rerun": 7, "item": 1234, "modules": ["IA-03"], "cascade": "auto", "hints": false,
           "immich": "api", "prev_status": "done", "prev_error": null, "started_at": "…",
           "source": {"kind": "local|work|immich|none", "location": "…", "copy_id": 456},
           "superseded": {"IA-03": 9876}},
"_retry_previous_immich_tags": ["…"],
"_retry_previous_immich_description": "…",
"_previous_keywords_written": ["…"],
"_previous_description_written": "…",
"_manual_category": "screenshot"
```
Gesetzt bei der Vorbereitung aus den aktuellen IA-07-/IA-08-Läufen, notiert in jedem Modul-Lauf, gelöscht am Ende; nach Absturz gehören sie dem wiederaufgenommenen Lauf.

## 3. Ausführer

### 3.1 Ablauf pro Job

`run_pipeline` bleibt der einzige Einstieg (Claim, `adopt`, `_prepare_source`, Abschluss unverändert); die Schleife `__init__:189-326` zieht nach `pipeline/executor.py`:

```
plan = registry.plan(job, current_runs, rerun=params.get("_rerun"))
side = {}
for code, fn in pipeline.MAIN_STEPS:                 # zur Laufzeit gelesen
    if not plan.wanted(code): continue               # Pipeline: nicht erledigt; Rerun: queued/kaskadiert
    await wait(side, needed_by=code)                 # Seiten-Tasks, die code voraussetzt oder liest; vor IA-07 alle
    if side ended with stop/pause/superseded: break
    plan.decide_cascade(code)                        # nur Rerun
    if spec.side_session and parallel: side[code] = start_side(code); continue
    outcome = await run_inline(code, fn)             # Session des Jobs, current_step wie heute
    if outcome in (duplicate, skipped, critical, paused): break
await wait(side, all)                                # finally: abbrechen und warten, nie verwaist
if critical and not rerun: await _move_to_error(job, session)
finalizers: nacheinander, immer                      # __init__:333-353
```
Ein Modul-Lauf: bei knapper Grenze (ai, geo, immich) zuerst `runs.enqueue` (Ansicht zeigt „Wartet") → `resources.use(klasse)` → `runs.start(…, fingerprint)` + Commit → `fn(job, session)` mit `fileops.CURRENT_RUN_ID` → `runs.finish` + Commit → freigeben.
Seiten-Task (nur IA-03): eigene Session, eigenes `Job`-Objekt (`populate_existing`), `no_autoflush`, Run-Parameter am Objekt; danach `session.refresh(job)` im Hauptpfad, bevor ein Modul IA-03 liest.
Ergebnis: IA-03 und IA-04 gleichzeitig nach IA-02; IA-05 nach beiden; IA-07, IA-08 nacheinander und zuletzt; Finalizer immer. Schalter `pipeline.parallel_modules` (Vorgabe an) für strikt nacheinander.
Warum nur IA-03: liest nur IA-01, schreibt keine Job-Spalte (`ia03:190-251`). IA-04 schreibt über den Video-pHash `phash`, `status`, `original_path` (`ia02:332-408`) — zwei Sessions mit Job-Spalten wären ein verlorenes Update.

### 3.2 Claim pro Modul-Lauf

- Wartender Lauf: `UPDATE module_runs SET status='running', started_at, heartbeat_at, params, inputs_fingerprint WHERE id=? AND status='queued'` (`runs.py:237-246`); `rowcount ≠ 1` → `RunSuperseded` → Job-Lauf endet (`__init__:124-127`), Seiten-Tasks abgebrochen.
- Neuer Lauf: Insert; `uq_module_runs_current` (`models.py:320-321`) verhindert einen zweiten aktuellen.
- Neu in `runs.py`: `enqueue()` (aktueller beendeter → `superseded`, laufender → „unterbrochen", wartender bleibt), `start(…, fingerprint=None)`, `record(…, fingerprint=None)`, `restore(…, trigger=…)`, `running()`, `supersede_running()`.

### 3.3 Betriebsmittel

Neu `resources.py`: benannte Grenzen pro Prozess, Grösse bei jedem Erwerb aus der Config (5 s gecacht), Warteschlange nach (Priorität, Job-Reihenfolge, Ticket), `snapshot()` für die Diagnose.

| Klasse | Grenze | Vorgabe | Module |
|---|---|---|---|
| `ai` | Summe der Backend-Slots (`ai_backends.py:69-77`); `acquire_ai_backend` (`:80-133`) wählt weiter das Backend | dynamisch | IA-05, IA-06 |
| `geo` | `pipeline.geo_slots` | 1 bei Nominatim (`_throttle` bleibt), sonst 4 | IA-03 |
| `immich` | `pipeline.immich_slots` | 2 | IA-08 (Immich), Download der Vorbereitung, `fetch_immich_source` |
| `io` | `pipeline.io_slots` | 2 | IA-08 (Ablage), Verschieben nach `duplicates/`/`error/` |
| `cpu` | `pipeline.cpu_slots` | max(2, CPU/2) | IA-01, IA-02, IA-04, IA-07 |
| Jobs | `pipeline.max_jobs` | 0 = `2 × KI-Slots + 2` | Worker statt `get_total_slots` (`worker.py:413-414`) |

Heute laufen höchstens so viele Jobs wie KI-Slots; lädt einer zu Immich hoch, steht die KI still. Künftig belegt ein Job nur die Grenze des laufenden Moduls; die Job-Grenze schützt nur Verbindungen (Pool 20+40, `database.py:20-21`) und Speicher. Kein Modul hält zwei Grenzen → keine Verklemmung. Erwerb **vor** `runs.start`: Wartezeit zählt nicht als Laufzeit, und IA-05 baut seine Bilder (`ia05:197-221`) erst danach.

### 3.4 Fairness

- Worker: Jobs ohne `_rerun` zuerst, dann `created_at` (`worker.py:422-428`).
- Zuführer: höchstens `max(1, max_jobs // 2)` Rerun-Jobs in `queued`+`processing`.
- Grenzen: Priorität 0 Pipeline, 1 Rerun; dann Claim-Reihenfolge des Jobs (weiter fortgeschrittene zuerst), dann Ticket.

### 3.5 Pause und Auto-Pause

| Fall | Worker | laufende Jobs | Zuführer |
|---|---|---|---|
| Pause von Hand | nimmt keine neuen (`worker.py:398-410`) | laufen fertig | bereitet nichts vor |
| Auto-Pause `ai_unreachable` / `geo_unreachable` | nimmt keine neuen | auslösender Job wie heute `error` (Pipeline) bzw. 2.6 (Rerun). **Neu:** Jobs, die die betroffene Grenze erst erwerben würden, parken: `processing → queued` atomar, beendete Läufe bleiben, weiter nach dem Fortsetzen (`health_watcher.py:45-55`) | wartet |

### 3.6 Absturz und Wiederaufnahme

| Absturz während | danach | Wiederaufnahme |
|---|---|---|
| Vorbereitung vor dem Commit | Job `processing`, alte Läufe gültig, evtl. Arbeitskopie registriert | `requeue_interrupted_jobs` (`recovery.py:117-169`) → nichts offen → Status neu; Auftrag `preparing → pending` beim Start des Zuführers; nächste Vorbereitung findet die Arbeitskopie |
| IA-03 fertig, IA-04 läuft | IA-03 `ok`, IA-04 `running` | IA-03 nicht neu; IA-04 als „unterbrochen" ersetzt (`runs.py:247-248`) und neu |
| IA-03 läuft, IA-04 fertig | umgekehrt | IA-03 neu |
| vor dem Kaskaden-Entscheid | Vorgänger `ok` | Entscheid aus den Läufen neu berechnet, gleiches Ergebnis |
| IA-08 nach Verschieben/Upload, vor Commit | Protokoll hat das Ergebnis | `fileops.last_result(since=completed_at)` (`ia08:666-672`, `:749-753`); `completed_at` = vorherige Fertigstellung, also genau dieser Lauf |

### 3.7 Watchdog und Heartbeat

- Heartbeat unverändert (`__init__:48-60`, `runs.heartbeat`), deckt alle laufenden Läufe des Jobs, auch IA-03.
- `_ACTIVE_RUNS[job]` bekommt `steps: {modul: seit}`; `step`/`step_since` bleiben.
- Hängt im Prozess: ein laufendes Modul länger als `STALE_TIMEOUT_S` und keine offene Dateioperation → Worker-Task abbrechen, Seiten-Tasks mit. `_requeue_or_fail` ersetzt **alle** laufenden Läufe des Jobs (heute nur `current_step`, `worker.py:226-229`).
- Prozessübergreifend unverändert über `updated_at` (`worker.py:253-318`).

### 3.8 In-Prozess-Wächter

`_ACTIVE_RUNS` pro Job bleibt; zweites `run_pipeline` → „already claimed". Vorbereitung registriert sich als `kind="rerun"`; der Worker überspringt aktive Jobs (`worker.py:422`). Im Job startet nur der Ausführer Module, nie eines zweimal.

### 3.9 Aufträge und Zuführer

| `reruns` | |
|---|---|
| `id` (AUTOINCREMENT), `created_at`, `finished_at`, `actor` (`user:rerun`) | |
| `modules`, `options` (cascade, hints, immich), `scope` (kind, set_id, filter, Anzahl) | JSON |
| `state` | `running` → `cancelling` → `cancelled` / `done` |
| `total`, `refused` (Grund → Anzahl, Beispiele) | |

| `rerun_items` | |
|---|---|
| `id`, `rerun_id`, `job_id`, `position` | |
| `state` | `pending → preparing → queued → running → done / unchanged / failed`; `refused`; `deferred → pending`; `cancelled` |
| `reason`, `detail`, `prev_status`, `summary` (ran, changed, restored) | |
| `started_at`, `finished_at` | |

Indizes: (`rerun_id`, `state`); **eindeutig** (`job_id`) WHERE `state IN ('pending','preparing','queued','running')` → kein Job doppelt vorgemerkt.
Zuführer `reruns.feeder(shutdown_event)` startet nach `requeue_interrupted_jobs` neben dem Worker (`filewatcher.py:71-75`): jede Sekunde, nicht bei Pause; setzt beim Start `preparing → pending`; nimmt Aufträge atomar `pending → preparing`; höchstens 2 Vorbereitungen gleichzeitig. Jeder Zustandswechsel `UPDATE … WHERE state=?`.

### 3.10 Was sich nicht ändern darf

| Verhalten | gesichert durch |
|---|---|
| `run_pipeline(job_id)`; Claim `queued → processing`; „already claimed"; genau ein „Error at IA-01" | `test_duplicate_fix.py:333-427` |
| Step-Funktionen aus `pipeline.MAIN_STEPS`/`FINALIZERS` zur Laufzeit | `test_duplicate_fix.py:154-164`, `test_module_runs.py:689-702` |
| Reihenfolge des Hauptpfads, `current_step` = Modul des Hauptpfads | `test_history_view.py:107-136` |
| `reset_job_for_retry`/`retry_job` nuklear, über `reprocess/` | `test_retry_file_lifecycle.py`, US-9, R-10b |
| Wiederaufnahme nur der unterbrochenen Module | `test_module_runs.py` 3c |
| IA-08-Logtexte („safe_move → library OK") | `test_data_safety_local.py:640` (R-10c) |
| Finalizer immer, nacheinander, Fehler = Warnung | AGENTS.md |
| `get_total_slots()` | `test_ai_backends.py:219-313` |
| keine Dateioperation ausserhalb fileops; `step_result` nur über runs.py | `test_no_direct_file_ops.py`, `runs.py` |

## 4. API-Vertrag

Neu `routers/reruns.py`, Präfix `/api/reruns`. Fehler: `{"ok": false, "error": "<schlüssel>", "detail": "…"}`. Gründe sind Schlüssel; die Oberfläche übersetzt (`rerun.<art>_<schlüssel>`).

### 4.1 GET /api/reruns/modules
```json
{"modules": [
  {"module": "IA-01", "standalone": true, "needs_file": true, "resource": "cpu", "destructive": false,
   "dependents": ["IA-03","IA-04","IA-07","IA-08"], "hint_dependents": ["IA-05"]},
  {"module": "IA-02", "standalone": false, "refused": "not_standalone"}],
 "defaults": {"cascade": "auto", "hints": false, "immich": "api"}}
```

### 4.2 POST /api/reruns/preview
```json
{"modules": ["IA-03"],
 "target": {"kind": "jobs", "keys": ["MA-2026-00012"]}
         | {"kind": "query", "status": "done", "q": "IMG_"}
         | {"kind": "set", "set_id": 3, "filter": {"status": ["done","review"], "only_stale": false,
                                                    "created_from": "2026-01-01", "created_to": null}},
 "options": {"cascade": "auto", "hints": false, "immich": "api"},
 "detail": "summary"}
```
`query` = Filter des Verarbeitungs-Logs (`routers/logs.py:43-59`; „warning" = done + „Warnungen in:"). `detail: "jobs"` liefert höchstens 200 Jobs.
```json
{"ok": true, "modules": ["IA-03"], "options": {"cascade": "auto", "hints": false, "immich": "api"},
 "total": 1234, "eligible": 1200,
 "refused": {"status_error": {"count": 20, "sample": ["MA-…"]}},
 "per_module": {
   "IA-03": {"action": "run", "count": 1180, "why": "selected", "skip": {"no_gps": {"count": 20, "sample": ["MA-…"]}}},
   "IA-07": {"action": "maybe", "count": 1180, "why": "cascade", "from": ["IA-03"]},
   "IA-08": {"action": "maybe", "count": 1180, "why": "cascade", "from": ["IA-03", "IA-07"]},
   "IA-05": {"action": "keep", "count": 1180, "why": "hint_not_followed", "from": ["IA-03"]},
   "IA-09": {"action": "run", "count": 1200, "why": "finalizer"}},
 "effects": {"downloads": 0, "ai_calls_max": 0, "file_writes_max": 300, "library_moves_max": 0,
             "immich_api": 900, "immich_replace": 0},
 "jobs": [{"debug_key": "MA-…", "filename": "IMG_1.jpg", "status": "done", "target": "immich",
           "refused": null, "source": "none",
           "modules": [{"module": "IA-03", "action": "run", "why": "selected"}]}],
 "jobs_truncated": false, "elapsed_ms": 340}
```
`action`: `run` | `maybe` (falls sich der Vorgänger ändert) | `keep` | `skip`. Fehler: 400 `unknown_module`, `not_standalone`, `no_modules`, `bad_target`, `bad_option`; 404 `set_not_found`; 413 `too_many_keys` (> 5000).

### 4.3 POST /api/reruns
Anfrage wie 4.2 ohne `detail`. 202 `{"ok": true, "rerun_id": 7, "total": 1200, "refused": 34, "state": "running"}`. Die Auswahl wird jetzt aufgelöst, jeder Job bei der Vorbereitung erneut geprüft. Fehler wie 4.2; 409 `rerun_running` (`{"rerun_id": 6}`; ein Massenauftrag gleichzeitig, Einzeljobs immer); 409 `nothing_to_do` (mit `refused`); 409 `busy`/`already_pending` bei einem Job.

### 4.4 GET /api/reruns/active, GET /api/reruns/{id}
`active` → `{"rerun": <Objekt> | null, "singles": 2}`.
```json
{"id": 7, "state": "running", "paused": false, "modules": ["IA-03"], "options": {},
 "scope": {"kind": "set", "set_id": 3, "set_name": "Handy", "filter": {}},
 "created_at": "2026-10-23T20:51:00", "finished_at": null, "actor": "user:rerun", "total": 1200,
 "counts": {"pending": 900, "preparing": 2, "queued": 8, "running": 4, "done": 270, "unchanged": 10,
            "failed": 3, "refused": 1, "deferred": 0, "cancelled": 0},
 "current": ["MA-…"],
 "failures": [{"debug_key": "MA-…", "state": "failed", "reason": "module_error", "module": "IA-03",
               "detail": "RuntimeError: Nominatim HTTP 400"}]}
```
`failures`: die letzten 20. 404 `not_found`.

### 4.5 POST /api/reruns/{id}/cancel
`{"ok": true, "state": "cancelling", "cancelled": 900, "still_running": 12}`; wird `cancelled`, wenn die Letzten fertig sind. 404 `not_found`, 409 `already_finished`.

### 4.6 Erweiterungen bestehender Antworten
- `GET /api/history/job/{key}` (`routers/history.py:90-108`): `"rerun": {"eligible": true, "refused": null, "modules": {"IA-03": {"possible": false, "skip": "no_gps"}}, "active": {"rerun_id": 9, "state": "queued"} | null}`.
- Jeder Modul-Block (`job_view.module_blocks`, `job_view.py:298-334`; auch `/api/history/modules`): `"stale": true|false`.

### 4.7 Schlüssel

| Art | Schlüssel |
|---|---|
| `why` | `selected`, `cascade`, `prerequisite`, `finalizer`, `hint_not_followed`, `cascade_off` |
| `skip` | `module_disabled`, `no_gps`, `ocr_off`, `ocr_not_needed`, `too_small`, `not_writable`, `no_ai`, `not_standalone` |
| `refused` | `busy`, `status_error`, `status_duplicate`, `status_skipped`, `status_orphan`, `status_deleted`, `resolved_donor`, `set_missing`, `no_copy`, `asset_missing`, `shared_copy`, `already_pending` |
| `failures.reason` | `module_error`, `service_down`, `no_copy`, `asset_missing`, `prepare_failed`, `busy` |
| Auftrag `state` | `pending`, `preparing`, `queued`, `running`, `done`, `unchanged`, `failed`, `refused`, `deferred`, `cancelled` |
| `error` | `unknown_module`, `not_standalone`, `no_modules`, `bad_target`, `bad_option`, `set_not_found`, `too_many_keys`, `rerun_running`, `nothing_to_do`, `not_found`, `already_finished` |

i18n (UI-Agent, Abschnitt `rerun` in `de.json`/`en.json`): `title`, `menu`, `menu_more`, `step_button`, `dialog_title_one/_many/_set`, `modules_label`, `module_desc_IA-01/03/05/06/07/08`, `cascade_label`, `cascade_auto/_all/_off` (+ `_desc`), `hints_label/_desc`, `immich_label`, `immich_api/_replace` (+ `_desc`), `preview_title`, `preview_loading`, `preview_failed`, `action_run/_maybe/_keep/_skip`, `why_*`, `skip_*`, `refused_*`, `effects_*`, `confirm_title`, `confirm_text`, `confirm_ok`, `start_ok_one/_many`, `err_*`, `progress_title`, `progress_counter`, `progress_running`, `progress_paused`, `progress_result`, `cancel`, `cancel_confirm_title/_text`, `cancelled`, `selected_one/_many`, `select_page`, `select_all_matching`, `clear_selection`, `selection_label`, `tools_title`, `tools_desc`, `tools_set`, `tools_status`, `tools_only_stale`, `tools_from`, `tools_to`, `tools_preview`, `set_menu`, `state_*`, `failures_title`. Dazu `history.stale`, `history.stale_hint`, `history.actor_user_rerun`, `history.actor_system_rerun`; Vorschlag `history.status_superseded` „Veraltet" → „Ersetzt".

## 5. Oberfläche (nur Komponenten aus docs/DESIGN.md)

### 5.1 Job-Detail (`templates/log_detail.html`)
- Kopf der Modul-Karte (`:53-59`): `ui-menu-wrap` mit `ui-btn ui-btn-secondary ui-btn-sm` „Neu ausführen" (retry-Symbol, `aria-haspopup="menu"`); `ui-menu` mit einem `ui-menu-item` je einzeln ausführbarem Modul („IA-03 Geocoding"), `ui-menu-sep`, „Mehrere Module …". Nicht möglich → `disabled`, `title` = Grund aus `rerun.modules`. Verborgen, solange `rerun.eligible` falsch; bei vorgemerktem Auftrag `ui-badge is-info` „Neu ausführen wartet".
- In jedem Schritt-Ergebnis (`:135-159`) oben im `ui-step-body`: `ui-btn ui-btn-ghost ui-btn-sm` „Neu ausführen".
- Veraltete Blöcke: `.mod-b[data-stale]` gestrichelt (Seiten-CSS, nur Tokens), Tooltip `history.stale_hint`; `_modules.html` und `app.js` (`histStripHTML`, `renderBar` `app.js:800-812`) zusammen ändern.

### 5.2 Verarbeitungs-Log (`templates/logs.html:417-466`)
- Erste Spalte `th.ui-col-check` mit `label.ui-check` „Alle auf dieser Seite"; pro Zeile Checkbox `aria-label="Auswählen: {Datei}"`, `tr.is-selected`. `histRows` ignoriert Klicks auf `input`/`label` schon (`app.js:620-621`).
- Aktionsleiste: `div.ui-card` mit `.ui-toolbar`, unten haftend (`logs.css`), `role="region"`: `ui-toolbar-meta` „3 ausgewählt" (`aria-live="polite"`) · `ui-btn ui-btn-ghost ui-btn-sm` „Alle 1 428 Treffer auswählen" (ganze Seite markiert und mehr vorhanden → Ziel `query`) · `ui-btn ui-btn-primary ui-btn-sm` „Module neu ausführen …" · `ui-btn ui-btn-secondary ui-btn-sm` „Auswahl aufheben".

### 5.3 Dialog (neu `templates/_rerun.html`)
`dialog.ui-dialog.is-wide`:
- Kopf: Titel, `ui-dialog-sub` „IMG_3140.HEIC" / „12 Dateien" / „Set Handy · 1 200 Dateien".
- Module: `label.ui-check` je einzeln ausführbarem Modul (Code, Name, `ui-muted` Beschreibung).
- Abhängige: `ui-segmented` Automatisch / Immer / Nicht.
- `ui-option` + `ui-switch` „KI-Analyse auch neu" (nur bei Hinweis-Abhängigen).
- `ui-segmented` „Immich: Nur Tags und Beschreibung / Datei ersetzen" (nur mit Immich-Jobs).
- Vorschau: `ui-steps` mit nicht aufklappbaren Zeilen (`div.ui-step > div.ui-step-head`) IA-01…IA-11, `ui-status` je Aktion (run `is-info` retry, maybe `is-neutral` circle, keep `is-success` check-circle, skip `is-warning` pause-circle), `ui-step-meta` („1 180 · hängt ab von IA-03"); darunter `ui-alert is-warning ui-alert-sm` (Ersetzen, Verschieben, KI-Aufrufe, Downloads) und `ui-alert is-info ui-alert-sm` mit `<details>` (Abgelehnte, Beispiele als Links); `ui-skeleton` beim Laden; neu laden entprellt 300 ms, alte Anfrage abbrechen.
- Fuss: `ui-btn ui-btn-secondary` Abbrechen, `ui-btn ui-btn-primary` „Neu ausführen (1 200)" (gesperrt ohne Vorschau oder bei 0) → `MA.confirm({title, text, ok, danger: immich_replace > 0 || library_moves_max > 0})` → `POST /api/reruns` → `ui-toast` und Fortschritt.
- Nutzerdaten per `textContent`.

### 5.4 Werkzeuge und Set
- `templates/tools.html`: Karte `section.ui-card.tools-card#section-rerun` wie die Tag-Karte (`:50-117`): `ui-icon-tile is-accent`, `ui-select` Set, Modul-Checkboxen, `ui-segmented` Fertig / Fertig und Review, `ui-option` „Nur veraltete", zwei `ui-input type=date`; „Vorschau" öffnet den Dialog mit Ziel `set`.
- Set-Liste (`templates/settings/_sets_list.html:132`): Menüeintrag „Module neu ausführen …" → `/tools?rerun_set=<id>#section-rerun`.

### 5.5 Fortschritt
Gleiche Komponente wie die Wartungskarte (`logs.html:70-81`: `ui-card logs-progress`, `ui-progress-meta`, `ui-progress`), zweite Instanz `#rerun-progress` — die Wartung hat ihren eigenen Platz (`routers/api.py:92-136`), beide sollen sich nicht verdrängen. Auf dem Verarbeitungs-Log und in der Werkzeuge-Karte; Start `GET /api/reruns/active`, dann 1 s `/api/reruns/{id}`. Titel „Neu ausführen: Geocoding", Zähler „270 / 1 200 (22 %)", Phase „4 laufen, 8 warten" bzw. „Pipeline pausiert — wartet", `ui-btn ui-btn-secondary ui-btn-sm` „Abbrechen" (MA.confirm). Am Ende Ergebniszeile und `<details>` mit Fehlschlägen.

## 6. Arbeitsteilung und Tests

### 6.1 Backend-Agent
| Datei | Inhalt |
|---|---|
| neu `pipeline/registry.py` | 1, 2.7 |
| neu `pipeline/executor.py` | 3.1, Kaskade |
| neu `pipeline/rerun.py` | Plan/Vorschau, 2.3, 2.5, 2.6, Vorbereitung |
| neu `resources.py` | 3.3, 3.4 |
| neu `reruns.py` | Aufträge, Zuführer, Fortschritt, Abbruch |
| neu `routers/reruns.py`, `main.py` | 4.1–4.5 |
| `models.py`, `database.py` | `Rerun`, `RerunItem`, Indizes |
| `runs.py` | 3.2 |
| `pipeline/__init__.py` | Ausführer, `_running.steps`, Rerun-Abschluss |
| `step_ia05_ai.py`, `step_ia07_exif_write.py`, `step_ia08_sort.py`, `step_ia09_notify.py`, `step_ia10_cleanup.py` | 2.4 |
| `worker.py`, `filewatcher.py` | `max_jobs`, Reihenfolge, `_rerun`, Watchdog, Zuführer |
| `fileops.py` | ContextVar `CURRENT_RUN_ID` als Vorgabe für `run_id` (`fileops.py:509-571`) |
| `job_view.py`, `routers/history.py` | 4.6 |

### 6.2 UI-Agent
`templates/log_detail.html`, `logs.html`, `tools.html`, `settings/_sets_list.html`, `_modules.html`, neu `_rerun.html`; `static/js/app.js` oder neu `static/js/rerun.js` (in `base.html`, öffentlich `MA.rerun.open({target, modules})`); `static/css/pages/history.css`, `logs.css`, `tools.css`; `i18n/de.json`, `i18n/en.json` (nur der UI-Agent); Screenshots hell/dunkel/Handy.

### 6.3 Vertrag und Reihenfolge
- Vertrag = Abschnitt 4; Änderungen nur gemeinsam.
- Erster Backend-Commit: Tabellen + `routers/reruns.py` mit echten Antworten aus Register und Prüfungen (Zuführer noch aus). Die Oberfläche baut dagegen.
- Backend dann Schritt 5 im bestehenden sequentiellen Ablauf → E2E grün → Schritt 6 (Seiten-Task, Grenzen, Worker, Watchdog).
- UI fasst keine `.py` an, Backend keine Templates/JS/CSS/i18n.

### 6.4 Neue Tests
E2E in `test_e2e_user_stories.py` (echtes Immich; `pipeline.paused`; der Test ruft Vorbereitung und `run_pipeline` selbst; Geocoding über gepatchtes `_reverse_nominatim`, `_geo_cache` geleert):

| Story | Ablauf | Prüft |
|---|---|---|
| US-15 Geocoding neu → Immich-Tags | Inbox → Immich („Bern"), Benutzer-Tag per API; dann „Zürich", IA-03 neu (API) | IA-03 Versuch 2 `rerun`, IA-07/08 `cascade`, IA-05 nicht neu und `stale`; gleiche Asset-ID; „Zürich" da, „Bern" weg, Benutzer-Tag da; Beschreibung neu; kein Download; `check_no_file_lost`; Auftrag `done` |
| US-15b gleicher Ort | wie US-15 ohne Änderung | nur IA-03 + IA-09…11; Auftrag `unchanged` |
| US-16 Immich-Job ohne lokale Kopie | Handy-Upload → Poller-Job fertig, Temp weg; gleichnamige fremde Datei in `reprocess/`; IA-01 neu | Download in eigene Arbeitskopie, fremde Datei unberührt (Hash), Arbeitskopie danach weg, `file_ops.run_id` gesetzt; Variante „ersetzen" (direkt): neue Asset-ID, altes im Papierkorb, Alben übernommen |
| US-17 Ablage: Geocoding neu | Inbox → Ablage (direkt), Vorlage mit `{CITY}` | Datei samt Sidecar umgezogen, alte Stichwörter nicht mehr in der Datei, `target_path` nachgeführt; ohne `{CITY}` Pfad gleich |
| US-18 Parallel + Absturz | HEIC, IA-03 gepatcht 2 s | IA-03/IA-04 überlappen; Absturz in IA-04 nach Ende von IA-03 → Wiederaufnahme: IA-03 Versuch 1 bleibt, IA-04 Versuch 2, Job done |
| US-19 Grenzen | 8 Jobs, IA-05-Attrappe 0,5 s, `ai.slots`=1, `max_jobs`=6 | ≤ 1 IA-05, ≤ 1 IA-03, ≤ 6 Jobs gleichzeitig; Nominatim-Abstand ≥ 1,1 s; 20 Jobs nicht langsamer als `parallel_modules=false` |
| US-20 kein doppelter Lauf | zwei Starts gleicher Job; Vorbereitung gegen Retry; Worker gegen `run_pipeline`; Watchdog bei laufendem Upload | `already_pending`; genau ein Claim; je Modul keine überlappenden Läufe; Job mit Heartbeat nicht zurückgestellt (R-11) |

Dazu `test_module_rerun.py` (Prüfungen 2.5, Vorschau-Zählungen, Kaskade auto/all/off/hints, Fingerabdruck je Einstellung, migrierte Läufe, Wiederherstellen, Auto-Pause → `deferred`, Abbruch, Absturz in der Vorbereitung) und `test_executor.py` (Reihenfolge, Schalter, ersetzte `MAIN_STEPS`, Grenzen FIFO/Priorität, Parken, Watchdog pro Modul, Seiten-Session lässt den Job unverändert).

### 6.5 Bleiben grün
`test_startup_imports`, `test_e2e_user_stories` (US-1…14), `test_retry_file_lifecycle`, `test_duplicate_fix`, `test_module_runs`, `test_data_safety_local`, `test_data_safety_immich`, `test_fileops`, `test_fileops_immich`, `test_immich_trash`, `test_history_view`, `test_folder_structure`, `test_sets_e2e`, `test_keep_flow`, `test_no_file_loss`, `test_no_direct_file_ops`, `test_ai_backends`, `test_jobs_autoincrement`, `test_testplan_final`.

## 7. Risiken und offene Fragen

| # | Risiko | Gegenmittel |
|---|---|---|
| A | SQLite-Sperre bei mehr Jobs und zweiter Session | Commit direkt nach start/finish, `no_autoflush`, nur IA-03 parallel, Messung US-19 |
| B | Seiten-Modul schreibt eine Job-Spalte → verlorenes Update | `side_session` nur geprüft; Test |
| C | Veralteter `original_path` im API-Modus (R-01) | `immich:<id>`; IA-08 ohne lokale Quelle |
| D | Stichwörter häufen sich | `_previous_keywords_written`; ein gleichlautendes Benutzer-Stichwort fällt mit weg (wie heute bei Immich-Tags) |
| E | Immich holt alte Stichwörter beim „Metadaten neu laden" zurück | nur API-Modus; Option „ersetzen" |
| F | KI-Kosten bei Massenläufen | Hinweis-Kanten aus, Vorschau zählt KI-Aufrufe |
| G | Poller-Inhaltsprüfung überschreibt Rerun-Job | `_rerun` umgeht sie |
| H | Video-pHash verschiebt fertige Datei | im Rerun aus |
| I | Rerun-Fehler schiebt Ablage-Datei nach `error/` | `_move_to_error` aus |
| J | Wartezeit an Grenzen gilt als „hängt" | Erwerb vor `runs.start` |
| K | Wartender Job (`queued`) ist kein IA-02-Kandidat (`ia02:197`) | kurzes Fenster dank Zuführer-Grenze |
| L | IA-08-Fehler hinterlässt teilweise geänderte Immich-Daten | Fehler-Lauf sichtbar, erneut ausführbar |
| M | Wieder eingelegte Datei wird neu eingelesen (`inputs/inbox.py:25-61` vergleicht `original_path`) | IA-02 erkennt exaktes Duplikat; wie nach Retry |
| N | `started_at` zeigt danach den Start des Neu-Ausführens (`__init__:138-142`) | hinnehmen oder im Rerun nicht setzen |

**Offene Fragen** (Entscheide siehe unten)
1. Immich-Vorgabe: API (Datei in Immich behält alte Stichwörter) oder Ersetzen (neue Asset-ID)? Vorschlag API.
2. „Geocoding neu" ohne KI (IA-05 „veraltet", Schalter „KI auch neu")?
3. Darf ein Massenlauf Ablage-Dateien verschieben (Vorlage mit `{CITY}`, Kategorie, Basis-Pfad)? Vorschlag ja mit Warnung; sonst Option „nicht verschieben".
4. Review-Kategorie von Hand bleibt bei IA-08 neu (Vorschlag)?
5. Nicht mehr passende Alben/Archivierung zurücknehmen? Vorschlag später.
6. Fehler-Jobs ablehnen und auf „Erneut verarbeiten" verweisen (Vorschlag)?
7. `pipeline.max_jobs` automatisch, ohne Einstellung in der Oberfläche (Vorschlag)?
8. Ein Massenauftrag gleichzeitig, Einzeljobs immer (Vorschlag)?
9. „Veraltet" doppelt: ersetzte Läufe → „Ersetzt" (Vorschlag)?
10. OCR „immer" parallel zur KI: jetzt nicht (Vorschlag)?
11. IA-09 meldet beim Neu-Ausführen nur dessen Fehler (Vorschlag)?

## 8. Entscheide (2026-10-23)

1. **Immich: pro Lauf wählbar.** Vorgabe „nur Tags und Beschreibung über die API" (gleiche Asset-ID, kein Download); im Dialog umschaltbar auf „Datei ersetzen" (neue Asset-ID, altes Asset in Immichs Papierkorb).
2. **KI läuft nicht automatisch mit.** IA-05 wird „veraltet" angezeigt; Schalter „KI-Analyse auch neu" im Dialog.
3. **Massenläufe dürfen Ablage-Dateien verschieben**, wenn sich der Zielordner ändert — mit Warnung und Zählung in der Vorschau, sicher über fileops.
4. **Gleichzeitige Jobs automatisch:** `2 × KI-Slots + 2`, keine Einstellung in der Oberfläche; eigene Grenzen für KI, Geocoding (1/s) und Immich.
5. Für die übrigen offenen Fragen gelten die Vorschläge aus Abschnitt 7 (Review-Kategorie bleibt; Alben/Archiv nicht zurücknehmen; Fehler-Jobs → „Erneut verarbeiten"; ein Massenauftrag gleichzeitig, Einzeljobs immer; ersetzte Läufe heissen „Ersetzt"; OCR nicht parallel zur KI; IA-09 meldet nur Fehler des Neu-Ausführens).

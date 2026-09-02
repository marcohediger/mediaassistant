# Funktionskatalog — MediaAssistant Backend

> **Komplette Auflistung aller Funktionen.** Vor jeder neuen Funktion
> MUSS dieses Dokument geprüft werden:
> 1. Gibt es die Funktion schon? → **Benutzen, nicht neu schreiben.**
> 2. Gehört sie in ein shared Modul? → **Dort erstellen, hier eintragen.**
> 3. Neue Funktion nötig? → **Prüfen ob sie shared sein soll.** Wenn ja:
>    in `file_operations.py`, `thumbnail_utils.py` oder `immich_client.py`
>    anlegen — nicht lokal in einem Router/Step.

---

## file_operations.py — Shared Datei-Operationen

> **Pflicht:** Jede Datei-Operation die in mehr als einem Modul vorkommt
> gehört hier rein. Kein raw `os.remove`, `os.rename`, `hashlib` in
> Pipeline oder Routers.

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `sha256` | `(path: str) -> str` | SHA-256 Hash (64 KiB Chunks). **Einzige Implementierung.** |
| `resolve_filename_conflict` | `(directory, filename, separator="_") -> str` | Eindeutiger Pfad: hängt `_1`, `+1` etc. an bis frei. |
| `safe_remove` | `(path, *, missing_ok=True) -> bool` | Datei löschen ohne Exception. `True` wenn gelöscht. |
| `safe_remove_with_log` | `(path) -> list[str]` | Datei + `.log`-Sidecar löschen. Skippt `immich:`-Pfade. |
| `get_duplicate_dir` | `async () -> str` | Duplikat-Verzeichnis aus Config, erstellt falls nötig. |
| `resolve_filepath` | `(job) -> str` | Dateipfad auflösen (target → original → temp). |
| `sanitize_path_component` | `(value) -> str` | Gefährliche Zeichen entfernen (Path-Traversal). |
| `validate_target_path` | `(target_dir, base_path) -> str` | Pfad innerhalb base_path erzwingen. |
| `parse_date` | `(date_str) -> datetime \| None` | EXIF-Datum parsen (diverse Formate + Timezone). |
| `is_folder_tags_active` | `async (job) -> bool` | Folder-Tags Modul + Inbox-Setting prüfen. |

---

## safe_file.py — Sichere Datei-Moves

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `safe_move` | `(src, dst, context="") -> str` | Copy+Hash-Verify+Delete. Wirft `RuntimeError` bei Fehler. |

---

## thumbnail_utils.py — Thumbnail-Erzeugung

> **Pflicht:** Thumbnails und Bild-Konvertierung nur über diese Helper.

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `generate_thumbnail` | `(filepath, max_size=THUMB_SIZE) -> bytes \| None` | JPEG-Thumbnail generieren (HEIC, RAW, Video, PIL-Formate). |
| `heic_to_jpeg` | `(filepath) -> bytes \| None` | HEIC → JPEG via heif-convert. |
| `video_to_jpeg` | `(filepath, max_size=THUMB_SIZE) -> bytes \| None` | Video-Frame via ffmpeg extrahieren. |
| `raw_to_jpeg` | `(filepath) -> bytes \| None` | RAW PreviewImage via ExifTool extrahieren. |
| `THUMB_SIZE` | `(400, 400)` | Default Thumbnail-Grösse. |
| `PREVIEW_SIZE` | `(1200, 1200)` | Preview-Grösse. |

---

## immich_client.py — Immich API

> **Pflicht:** Kein raw `httpx`-Call an Immich-Endpoints in Routers/Pipeline.
> Alles über diese Helper.

### Konfiguration

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `get_immich_config` | `async () -> tuple[str, str]` | `(base_url, api_key)` aus Config. |
| `get_user_api_key` | `async (user_id: int) -> str \| None` | Entschlüsselter API-Key eines ImmichUser. |
| `check_connection` | `async (*, api_key=None) -> tuple[bool, str]` | Verbindungstest. |

### Upload & Download

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `upload_asset` | `async (file_path, album_names=None, *, sidecar_path=None, api_key=None) -> dict` | Upload + Album-Zuordnung + Sidecar. |
| `download_asset` | `async (asset_id, target_path, *, api_key=None) -> str` | Original auf Disk (Streaming, 1 MB Chunks). |
| `get_asset_original` | `async (asset_id, *, api_key=None) -> tuple[bytes, str] \| None` | Original als Bytes + MIME (für HTTP-Proxy). |
| `get_asset_thumbnail` | `async (asset_id, size="thumbnail", *, api_key=None) -> bytes \| None` | Thumbnail-Bytes (`thumbnail` oder `preview`). |

### Informationen

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `get_asset_info` | `async (asset_id, *, api_key=None) -> dict \| None` | Vollständige Asset-Details (EXIF, Tags). |
| `asset_exists` | `async (asset_id, *, api_key=None) -> bool` | Existenz-Check (HTTP 200). |
| `get_asset_albums` | `async (asset_id, *, api_key=None) -> list[str]` | Album-Namen des Assets. |
| `add_asset_to_albums` | `async (asset_id, album_names, *, api_key=None) -> list[str]` | Asset zu Alben hinzufügen (erstellt falls nötig). Returns Liste der erfolgreich zugewiesenen. |
| `get_recent_assets` | `async (since=None, *, api_key=None) -> list[dict]` | Assets nach Zeitstempel (Poller). |

### Modifikation

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `tag_asset` | `async (asset_id, tag_name, *, api_key=None) -> dict` | Tag hinzufügen (erstellt falls nötig). |
| `untag_asset` | `async (asset_id, tag_name, *, api_key=None) -> dict` | Tag entfernen. |
| `update_asset_description` | `async (asset_id, description, *, api_key=None) -> dict` | Description setzen. |
| `archive_asset` | `async (asset_id, *, api_key=None) -> dict` | Archivieren (neue + legacy API). |
| `lock_asset` | `async (asset_id, *, api_key=None) -> dict` | In gesperrten Ordner verschieben. |
| `copy_asset_metadata` | `async (from_id, to_id, *, api_key=None) -> dict` | Alben/Faces/Stacks kopieren. |
| `delete_asset` | `async (asset_id, *, force=True, api_key=None) -> dict` | Löschen (**permanent** per Default). |

### Tags (Server-weit)

> Die `*_assets`-Helper arbeiten auf Tag-Ebene, nicht auf Asset-Ebene.
> `tag_asset`/`untag_asset` bleiben für einzelne Assets.

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `list_tags` | `async (*, api_key=None) -> list[dict]` | Alle Tags des Accounts. Wirft, damit der Aufrufer den Grund melden kann. |
| `count_tag_assets` | `async (tag_id, *, api_key=None) -> int` | Exakte Bildzahl über `POST /search/statistics`. **Nicht** `search/metadata` — dessen `total` beschreibt die Seite, nicht den Treffer. |
| `list_tag_assets` | `async (tag_id, *, api_key=None) -> list[str]` | Alle Asset-IDs eines Tags, blättert bis zum Ende (`nextCursor` **und** `nextPage`). |
| `tag_assets` | `async (tag_id, asset_ids, *, api_key=None) -> None` | Tag an mehrere Assets hängen (`PUT /tags/{id}/assets`). |
| `untag_assets` | `async (tag_id, asset_ids, *, api_key=None) -> None` | Zuordnung lösen, Tag bleibt (`DELETE /tags/{id}/assets`). |
| `delete_tag` | `async (tag_id, *, api_key=None) -> None` | Tag löschen. **Kaskadiert auf Kind-Tags** (`parentId ON DELETE CASCADE`). |

### Fehler & Diagnose

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `fetch_asset_info` | `async (asset_id, *, api_key=None) -> tuple[dict\|None, str]` | Asset-Details **plus** Grund, wenn keine kommen (`not_found`, `no_access`, …). |
| `describe_connection_detail` | `(detail, i18n) -> str` | Übersetzt einen Verbindungs-Statuscode in einen lesbaren Satz. |
| `_server_message` | `(resp) -> str` | Fehlertext **nur** aus JSON-Antworten. Verhindert, dass HTML eines Reverse-Proxys als Immich-Meldung durchgeht. |
| `ImmichDuplicateUnavailable` | `RuntimeError` | Upload lief in ein Duplikat, dessen Original nicht erreichbar ist. |

> **Zwei gemessene Eigenheiten von Immich**, die jeder Aufrufer kennen muss:
> 1. **Der Index hinkt ~2 s nach.** Direkt nach `tag_assets` melden sowohl
>    `count_tag_assets` als auch `list_tag_assets` noch `0`. Wer sofort
>    nachzählt, misst sich selbst — nicht den Server.
> 2. **Gelöschte Tags kommen zurück.** Immich schreibt Tag-Namen in die
>    XMP-Sidecar. Steht der Name dort noch, legt der nächste Einlesevorgang
>    das Tag neu an — mit neuer ID. Tag-Operationen in Immich sind nur
>    dauerhaft, wenn die Sidecar mitgezogen wird.

---

## config.py — ConfigManager

| Methode | Signatur | Beschreibung |
|---|---|---|
| `get` | `async (key, default=None)` | Config-Wert lesen (Cache → DB, auto-Decrypt). |
| `set` | `async (key, value, encrypted=False)` | Config-Wert schreiben. |
| `is_setup_complete` | `async () -> bool` | Ersteinrichtung abgeschlossen? |
| `is_module_enabled` | `async (name) -> bool` | Modul aktiviert? |
| `set_module_enabled` | `async (name, enabled)` | Modul schalten. |
| `seed_from_env` | `async ()` | ENV-Variablen in DB schreiben. |

---

## system_logger.py — Persistentes Logging

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `log_info` | `async (source, message, detail=None)` | INFO in `system_logs` Tabelle. |
| `log_warning` | `async (source, message, detail=None)` | WARNING. |
| `log_error` | `async (source, message, detail=None)` | ERROR. |

---

## database.py — Datenbank

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `async_session` | Sessionmaker | SQLAlchemy async Session-Factory. |
| `init_db` | `async ()` | Schema + Migrationen + Defaults. |
| `seed_inbox_from_env` | `async ()` | Default-Inbox aus ENV. |

---

## models.py — Datenbank-Modelle

| Klasse | Beschreibung |
|---|---|
| `Job` | Pipeline-Job (Status, Steps, EXIF, Pfade, Immich-Asset). |
| `Config` | Key-Value Config (optional verschlüsselt). |
| `Module` | Modul-Flags (an/aus). |
| `SystemLog` | Persistente Log-Einträge. |
| `SortingRule` | Benutzerdefinierte Sortierregeln. |
| `LibraryCategory` | Ziel-Kategorien (Foto/Video/Screenshot). |
| `ImmichUser` | Per-User Immich API-Keys (verschlüsselt). |
| `InboxDirectory` | Inbox-Verzeichnis-Konfiguration. |

---

## auth.py — Authentifizierung

| Funktion / Klasse | Beschreibung |
|---|---|
| `get_session_secret() -> str` | Session-Secret generieren/laden. |
| `AuthMiddleware` | HTTP-Middleware (disabled/password/OIDC). |

---

## ai_backends.py — KI-Backend Loadbalancer

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `acquire_ai_backend` | `async () -> contextmanager` | Gibt idle Backend (Semaphore-gesteuert). |
| `get_total_slots` | `async () -> int` | Verfügbare Slots über alle Backends. |

---

## template_engine.py — Jinja2 Rendering

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `render` | `async (request, template, context=None) -> Response` | Template rendern mit i18n + Theme. |
| `get_ui_settings` | `async () -> dict` | Sprache + Theme aus Config. |

---

## i18n/__init__.py — Internationalisierung

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `load_lang` | `(lang) -> dict` | Sprachdatei laden (cached). |
| `get_text` | `(lang, section, key, default="") -> str` | Einzelnen String übersetzen. |
| `get_section` | `(lang, section) -> dict` | Ganze Sektion. |
| `clear_cache` | `()` | Cache leeren (Dev-Reload). |

---

## main.py — App-Startup

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `lifespan` | `asynccontextmanager` | Startet und beendet die Hintergrund-Tasks. |
| `_supervise` | `async (name, coro)` | Hüllt einen Hintergrund-Task ein und schreibt seinen Tod in die DB. Ohne das stirbt ein Task lautlos: eine Regression liess Filewatcher, Poller und Worker über drei Releases stehen, ohne eine einzige Meldung. **Muss über dem `@asynccontextmanager`-Dekorator stehen**, sonst greift der Dekorator auf `_supervise` statt auf `lifespan`. |

---

## filewatcher.py — Dateiüberwachung & Poller

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `start_filewatcher` | `async (shutdown_event)` | Haupt-Loop (Background-Task). |
| `trigger_manual_scan` | `()` | Manuellen Scan auslösen. |
| `_scan_inbox` | `async ()` | Inbox scannen, Jobs erstellen. |
| `_poll_immich` | `async ()` | Immich nach neuen Assets abfragen. |
| `_create_job_safe` | `async (**kwargs) -> Job \| None` | Job erstellen mit Duplikat-Check. |
| `_scan_directory` | `(path, min_age) -> list[str]` | Verzeichnis nach Mediendateien scannen. |
| `_is_file_stable` | `(filepath, expected_size) -> bool` | Datei fertig geschrieben? |
| `_is_within_schedule` | `async () -> bool` | Innerhalb Zeitplan? |
| `_next_debug_key` | `async () -> str` | Nächster Debug-Key (In-Memory Counter). |
| `_pipeline_worker` | `async (shutdown_event)` | Background-Worker: Jobs aus Queue verarbeiten. |
| `_scan_csv_retry` | `async ()` | CSV-Retry-Ordner scannen. |
| `_recover_immich_error_jobs` | `async ()` | Einmal-Migration: Jobs, die an einem Immich-Fehler dauerhaft hingen, zurück in die Queue. Flag `migration.immich_error_recovery` wird **nur bei Erfolg** gesetzt. |
| `_repair_immich_poll_cursor` | `async ()` | Einmal-Migration: `immich.last_poll` auf den ältesten Immich-Job minus einen Tag zurückdrehen, wenn der Cursor übersprungen hat. Flag `migration.immich_cursor_repair`. |

> **`immich.last_poll` wird nach einem fehlgeschlagenen Abruf nicht
> weitergestellt.** Sonst überspringt der Poller genau die Assets, deren
> Abruf gescheitert ist — in Produktion waren das vier Wochen Uploads.
| `_run_job` | `async (job_id, filename, debug_key)` | Einzelnen Pipeline-Job starten. |

---

## health_watcher.py — Auto-Pause/Resume

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `start_health_watcher` | `async (shutdown_event)` | Background: prüft Services, resumet bei Recovery. |

---

## cleanup_broken_sidecars.py — Utility

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `is_broken_sidecar` | `(path) -> tuple[bool, str]` | Prüft ob XMP-Sidecar defekt ist. |
| `walk_and_report` | `(root, do_delete)` | Verzeichnis scannen + defekte löschen. |
| `main` | `() -> int` | CLI Entry-Point. |

---

## Pipeline

### pipeline/__init__.py — Pipeline-Engine

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `run_pipeline` | `async (job_id: int)` | Komplette Pipeline für einen Job. |
| `reset_job_for_retry` | `async (job_id) -> bool` | Job auf `queued` zurücksetzen. |
| `retry_job` | `async (job_id)` | Reset + sofort ausführen. |
| `_move_to_error` | `async (job, session)` | Job in Error-Verzeichnis verschieben. |

### pipeline/reprocess.py — Reprocess-Helper

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `prepare_job_for_reprocess` | `async (session, job, *, keep_steps=None, inject_steps=None, move_file=True, commit=True) -> bool` | Job für Re-Pipeline vorbereiten (Move + Step-Reset). |
| `_move_file_for_reprocess` | `async (job) -> bool` | Datei nach `/reprocess/` verschieben. |
| `_reset_step_results` | `(job, *, keep_steps, ...)` | Step-Results selektiv zurücksetzen. |
| `_resolve_reprocess_path` | `(filename, debug_key) -> str` | Eindeutiger Pfad in REPROCESS_DIR. |
| `_is_immich_target` | `(target_path) -> bool` | Prüft `immich:` Prefix. |

### Pipeline Steps (IA-01 bis IA-11)

Jeder Step hat: `async execute(job, session) -> dict`

| Step | Datei | Beschreibung | Zusätzliche Funktionen |
|---|---|---|---|
| IA-01 | `step_ia01_exif.py` | EXIF lesen (ExifTool + ffprobe) | `_run_ffprobe`, `_parse_iso6709`, `_format_duration`, `_find_google_json`, `_read_google_json` |
| IA-02 | `step_ia02_duplicates.py` | Duplikat-Erkennung (SHA256 + pHash) | `execute_video_phash`, `_quality_score`, `_compute_phash`, `_compute_video_phash`, `_phash_from_preview`, `_file_exists`, `_extract_folder_tags`, `_handle_duplicate` |
| IA-03 | `step_ia03_geocoding.py` | Geocoding (GPS → Ort) | `_reverse_nominatim`, `_reverse_photon`, `_reverse_google`, `_http_get_with_retry`, `_throttle`, `_cache_key` |
| IA-04 | `step_ia04_convert.py` | Format-Konvertierung (→ temp JPEG) | `_extract_video_frames`, `_ffmpeg_extract_frame`, `_glob_temp_files` |
| IA-05 | `step_ia05_ai.py` | KI-Analyse (Tags, Description) | `_resize_for_ai`, `has_mixed_scripts`, `is_unusable_keyword`, `_scripts_of` |
| IA-06 | `step_ia06_ocr.py` | OCR-Texterkennung | — |
| IA-07 | `step_ia07_exif_write.py` | Keywords/Description schreiben | `_write_direct`, `_write_sidecar` |
| IA-08 | `step_ia08_sort.py` | Sortierung / Immich-Upload | `_get_folder_album_names`, `_tag_immich_asset`, `_resolve_path`, `_is_dir_empty`, `_force_remove_dir`, `_cleanup_empty_dirs`, `_eval_exif_expression`, `_eval_single_condition`, `_match_sorting_rules` |
| IA-09 | `step_ia09_notify.py` | E-Mail-Benachrichtigung | — |
| IA-10 | `step_ia10_cleanup.py` | Temp-Dateien aufräumen | — |
| IA-11 | `step_ia11_log.py` | Job-Abschluss loggen | — |

> **Schlagwort-Filter in IA-05:** `is_unusable_keyword` verwirft ein Wort,
> bevor es gespeichert wird, wenn es leer ist, keinen einzigen Buchstaben
> enthält (`?????`, `---`, reine Zahlen) oder Schriften mischt
> (`Sunset夕日`) — solche Wörter entstehen, wenn das Modell ins Straucheln
> kommt, und sind hinterher in Sidecars **und** Immich zu putzen. Verworfene
> Wörter werden geloggt, nicht stillschweigend geschluckt.

---

## Routers

### routers/api.py — REST API

| Funktion | Beschreibung |
|---|---|
| `health` | Health-Check Endpoint. |
| `retry_job_endpoint` | Einzelnen Job retrien. |
| `retry_all_errors_endpoint` | Alle Error-Jobs zurücksetzen. |
| `retry_all_warnings_endpoint` | Alle Warning-Jobs zurücksetzen. |
| `cleanup_orphans_endpoint` | Verwaiste Jobs markieren. |
| `trigger_scan` | Manuellen Scan auslösen. |
| `pause_pipeline_endpoint` | Pipeline pausieren. |
| `resume_pipeline_endpoint` | Pipeline fortsetzen. |
| `pipeline_status_endpoint` | Pipeline-Status abfragen. |
| `delete_job_endpoint` | Job + Dateien löschen. |

### routers/dashboard.py — Dashboard

| Funktion | Beschreibung |
|---|---|
| `dashboard` | Dashboard HTML-Seite. |
| `dashboard_json` | Live-Update JSON. |
| `_get_module_status` | Health-Status aller Module. `record=False` liest nur, ohne Logs/Cache zu schreiben — dafür nutzt es die Diagnose-API. |
| `_get_throughput` | Durchsatz-Statistiken. |
| `_probe_ai_backend` | Gemeinsame Prüfung für beide KI-Backends. Trennt „Backend aus" (ConnectError) von „Backend beschäftigt" (ReadTimeout → gilt weiter als bereit). Timeout aus `ai.timeout`, gedeckelt auf 30 s. |
| `_check_ai_backend` / `_check_ai_backend_2` | Dünne Aufrufer von `_probe_ai_backend`. |
| `_check_geocoding` | Geocoding Connectivity. |
| `_check_smtp` | SMTP Connectivity. |
| `_check_filewatcher` | Inbox-Verzeichnisse prüfen. |
| `_check_immich` | Immich Connectivity. |

### routers/tools.py — Werkzeuge (Tag-Pflege)

> Beide Abschnitte teilen sich den Fortschritts-Slot aus `routers/api.py`
> (`_cleanup_progress`) und unterscheiden sich über den Task-Namen
> `sidecar_tags` bzw. `tag_merge`. Deshalb läuft immer nur einer.

| Funktion | Beschreibung |
|---|---|
| `tools_page` | HTML-Seite des Registers. |
| `scan_sidecar_tags` / `remove_sidecar_tags` | Suchen und Entfernen nach Muster. |
| `scan_merge` / `apply_merge` | Suchen und Ausführen des Zusammenführens. |
| `cancel_sidecar_tags` | Setzt das Abbruch-Flag; jede Schleife prüft es. |
| `_start` | Gemeinsamer Start: lehnt ab, wenn schon etwas läuft oder der Umfang von der Vorschau abweicht. |

**Sidecar-Helper**

| Funktion | Beschreibung |
|---|---|
| `_read_library_subjects` | Ein `exiftool -j -Subject -r` über die ganze Library. |
| `_subjects` | `Subject` als Liste — exiftool liefert bei einem einzigen Wert einen blanken String. |
| `_collect` | Treffer-Schlagwort → Anzahl, plus die Dateien, die es tragen. |

**Zusammenführen (Schreibweisen)**

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `_merge_keys` | `(name) -> set[str]` | Zwei normalisierte Formen je Name: NFKD ohne Diakritika ohne Sonderzeichen, plus die Variante mit ausgeschriebenen Umlauten. So trifft `Zürich` sowohl `Zurich` als auch `Zuerich`. Buchstaben aller Schriften überleben — griechische und arabische Namen fallen nicht zusammen. |
| `_group_variants` | `(tags) -> list[list[dict]]` | Union-Find über gemeinsame Schlüssel. Nur Gruppen mit mehr als einem Mitglied. |
| `_is_damaged` / `_damaged_keys` | `(name)` | Namen mit `?` oder `\ufffd`: alle plausiblen Umlaute werden durchprobiert (max. 2 kaputte Stellen). |
| `_build_groups` | `(names) -> (groups, pending)` | Erst die starken Varianten, dann die kaputten Namen **anhängen**. Ein kaputter Name darf nie zwei gesunde verbinden — `M?ller` passt auf `Müller` wie auf `Moller`. Mehrdeutige kommen als `pending` zurück und werden vom Aufrufer nach Grösse entschieden. |
| `_scan_merge` | `async (with_sidecars, with_immich)` | Vorschau über **beide** Quellen. Die Namensmenge ist die Vereinigung aus Sidecar-Schlagwörtern und Immich-Tags. |
| `_merge_apply` | `async ()` | Führt genau den Plan aus der Vorschau aus. Sidecars zuerst, Immich danach. |

> **Gewinner-Regel:** kaputte Schreibweise nie, sonst meiste Bilder +
> Dateien, bei Gleichstand der längere Name — der hat seine Umlaute und
> Leerzeichen noch.

> **exiftool-Reihenfolge beim Ersetzen:** `-Subject-=alt … -Subject-=neu
> -Subject+=neu`. Das Entfernen des Gewinners **vor** dem Hinzufügen
> verhindert eine Dublette in Dateien, die schon beide Schreibweisen tragen.

### routers/diagnostics.py — Diagnose-API (nur lesend)

| Funktion | Beschreibung |
|---|---|
| `diagnostics` | Der gesamte Report als JSON. Query: `?logs=N&level=…&job=MA-…`. |
| `_authorized` | Bearer-Token gegen die aktiven Token-Hashes. **Ohne gültigen Token: 404**, nicht 401 — der Endpunkt verrät seine Existenz nicht. |
| `token_hash` / `_active_hashes` | SHA-256; im Klartext wird ein Token nur einmal bei der Erzeugung angezeigt. |
| `_secret_state` | Gibt für Geheimnisse **nur** `set` / `unset` / `undecryptable` zurück, nie den Wert. |
| `_probe` | Verbindungstest gegen eine URL für den Report. |
| `_int_param` | Query-Parameter mit Default und Obergrenze. |

### routers/duplicates.py — Duplikat-UI

| Funktion | Beschreibung |
|---|---|
| **`_resolve_duplicate_group`** | **Shared Kern-Logik:** Metadata-Merge (GPS, Date, Keywords, Folder-Tags, Description, Albums), Donor-Cleanup (Dateien + Immich mit Same-Asset-Guard), Hash-Clearing, Asset-ID Transfer, Analysis-Kopie, Immich-Sync. Benutzt von `keep_file` und `batch_clean_quality`. |
| `keep_file` | "Behalten" — Wrapper: Gruppe finden, `_resolve_duplicate_group(user_kept=True)`. |
| `batch_clean_quality` | Batch-Clean — Wrapper: Qualität vergleichen, `_resolve_duplicate_group()` pro Gruppe. |
| `not_duplicate` | "Kein Duplikat" — Re-Pipeline mit skip. |
| `delete_duplicate` | Einzelnes Duplikat löschen. |
| `re_evaluate_quality` | Qualitäts-Neuberechnung. |
| `duplicates_page` | Duplikat-Seite (HTML). |
| `api_duplicate_groups` | Paginierte Gruppen-API. |
| `thumbnail` / `immich_thumbnail` | Thumbnails servieren. |
| `immich_original` / `local_original` | Originalbilder servieren. |
| `_build_member` | Member-Dict für Duplikat-Gruppe bauen. |
| `_build_group_index` / `_build_group_detail` | Gruppen-Index + Detail. |
| `_build_duplicate_groups` | Transitive Gruppen-Zusammenführung. |
| `_get_image_info` / `_get_image_info_batch` | EXIF via ExifTool. |
| `_img_info_from_immich` | EXIF via Immich-API. |
| `_union_find_groups` | Union-Find für transitive Gruppen. |
| `_parse_exiftool_entry` | ExifTool JSON parsen. |

### routers/review.py — Review-UI

| Funktion | Beschreibung |
|---|---|
| `review_page` | Review-Seite (HTML). |
| `review_thumbnail` | Thumbnails servieren. |
| `classify_file` | Datei klassifizieren + verschieben. |
| `classify_all` | Alle als "sourceless" klassifizieren. |
| `delete_file` | Review-Datei löschen. |
| `_build_review_items` | Review-Items laden. |

### routers/settings.py — Einstellungen

| Funktion | Beschreibung |
|---|---|
| `settings_page` | Einstellungs-Seite. |
| `save_settings` | Allgemeine Settings speichern. |
| `add_inbox` / `update_inbox` / `delete_inbox` | Inbox-CRUD. |
| `add_sorting_rule` / `update_sorting_rule` / `delete_sorting_rule` / `move_sorting_rule` | Sortierregeln-CRUD. |
| `add_category` / `delete_category` | Kategorien-CRUD. |
| `add_immich_user` / `update_immich_user` / `delete_immich_user` / `test_immich_user` | Immich-User CRUD. |

### routers/logs.py — Logs

| Funktion | Beschreibung |
|---|---|
| `logs_page` | Job- und System-Logs Seite. |
| `log_detail` / `log_detail_json` | Job-Detail (HTML + JSON). |
| `dryrun_report` | Dry-Run Zusammenfassung. |

### routers/setup.py — Ersteinrichtung

| Funktion | Beschreibung |
|---|---|
| `setup_index` | Setup-Wizard Start. |
| `setup_step` | Einzelner Setup-Schritt. |
| `setup_step1_save` / `setup_step1_test` | KI-Backend Config. |
| `setup_step2_save` | SMTP Config. |
| `setup_step3_save` | Library/Inbox Pfade. |
| `setup_complete` | Setup abschliessen. |

### routers/auth_oidc.py — OIDC Login

| Funktion | Beschreibung |
|---|---|
| `login` | Login-Seite. |
| `sso_redirect` | Weiterleitung zum OIDC-Provider. |
| `callback` | OIDC Callback verarbeiten. |
| `logout` | Session beenden. |

---

## ✅ Redundanzen bereinigt (v2.28.84)

Alle 8 Redundanzen wurden in shared Module konsolidiert:

| Funktion | Jetzt in | Benutzt von |
|---|---|---|
| `generate_thumbnail` | `thumbnail_utils.py` | duplicates.py, review.py |
| `heic_to_jpeg` | `thumbnail_utils.py` | duplicates.py, review.py |
| `video_to_jpeg` | `thumbnail_utils.py` | duplicates.py, review.py |
| `raw_to_jpeg` | `thumbnail_utils.py` | duplicates.py |
| `resolve_filepath` | `file_operations.py` | duplicates.py, review.py |
| `sanitize_path_component` | `file_operations.py` | duplicates.py, review.py, step_ia08 |
| `validate_target_path` | `file_operations.py` | duplicates.py, review.py, step_ia08 |
| `parse_date` | `file_operations.py` | step_ia08, review.py |
| `is_folder_tags_active` | `file_operations.py` | step_ia07, step_ia08 |

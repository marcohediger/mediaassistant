# Shared Functions — MediaAssistant Backend

> Alle wiederverwendbaren Funktionen an einer Stelle dokumentiert.
> **Regel:** Wenn eine Funktion hier steht, MUSS sie benutzt werden.
> Keine Neuimplementierung mit raw os/shutil/httpx Calls.

---

## file_operations.py — Datei-Operationen

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `sha256` | `sha256(path: str) -> str` | SHA-256 Hash einer Datei (64 KiB Chunks). Einzige Implementierung — nicht mit `hashlib` neu schreiben. |
| `resolve_filename_conflict` | `resolve_filename_conflict(directory: str, filename: str, separator: str = "_") -> str` | Findet eindeutigen Dateinamen. Hängt `_1`, `_2`, ... an (oder `+1` etc. je nach Separator). Gibt Pfad zurück der nicht existiert. |
| `safe_remove` | `safe_remove(path: str, *, missing_ok: bool = True) -> bool` | Löscht eine Datei ohne Exception. Gibt `True` zurück wenn gelöscht, `False` wenn nicht vorhanden oder Fehler. |
| `safe_remove_with_log` | `safe_remove_with_log(path: str) -> list[str]` | Löscht Datei + zugehörige `.log`-Sidecar. Überspringt `None`, leer, `immich:`-Pfade. Gibt Liste der gelöschten Pfade zurück. |
| `get_duplicate_dir` | `async get_duplicate_dir() -> str` | Liest `library.base_path` + `library.path_duplicate` aus Config, erstellt Verzeichnis falls nötig. Gibt Pfad zurück. |

---

## safe_file.py — Sichere Datei-Moves

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `safe_move` | `safe_move(src: str, dst: str, context: str = "") -> str` | 3-Schritt Move: Copy mit Hash-Berechnung → Hash-Verify → Delete Original. Wirft `RuntimeError` bei fehlgeschlagener Verifikation. Nutzt intern `file_operations.sha256`. |

---

## immich_client.py — Immich API

### Konfiguration

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `get_immich_config` | `async get_immich_config() -> tuple[str, str]` | Gibt `(base_url, api_key)` aus der globalen Config zurück. |
| `get_user_api_key` | `async get_user_api_key(user_id: int) -> str \| None` | Entschlüsselt den API-Key eines ImmichUser anhand seiner DB-ID. |
| `check_connection` | `async check_connection(*, api_key=None) -> tuple[bool, str]` | Testet die Immich-Verbindung. Gibt `(ok, detail_message)` zurück. |

### Asset-Upload & -Download

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `upload_asset` | `async upload_asset(file_path, album_names=None, *, sidecar_path=None, api_key=None) -> dict` | Lädt Datei nach Immich hoch. Optional mit XMP-Sidecar und Album-Zuordnung (Alben werden automatisch erstellt). Gibt Immich-Response zurück (enthält `id`, `status`). |
| `download_asset` | `async download_asset(asset_id, target_path, *, api_key=None) -> str` | Lädt Original-Datei von Immich auf Disk (Streaming, 1 MB Chunks). Gibt lokalen Dateipfad zurück. |
| `get_asset_original` | `async get_asset_original(asset_id, *, api_key=None) -> tuple[bytes, str] \| None` | Lädt Original-Datei als Bytes + MIME-Type. Für HTTP-Proxying (z.B. Duplikat-Vorschau). Gibt `None` zurück wenn nicht gefunden. |
| `get_asset_thumbnail` | `async get_asset_thumbnail(asset_id, size="thumbnail", *, api_key=None) -> bytes \| None` | Thumbnail-Bytes (`size`: `thumbnail` oder `preview`). |

### Asset-Informationen

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `get_asset_info` | `async get_asset_info(asset_id, *, api_key=None) -> dict \| None` | Vollständige Asset-Details (EXIF, Tags, Pfad). |
| `asset_exists` | `async asset_exists(asset_id, *, api_key=None) -> bool` | Prüft ob ein Asset in Immich existiert (HTTP 200). |
| `get_asset_albums` | `async get_asset_albums(asset_id, *, api_key=None) -> list[str]` | Liste der Album-Namen in denen das Asset enthalten ist. Nutzt `GET /api/albums?assetId=`. |
| `get_recent_assets` | `async get_recent_assets(since=None, *, api_key=None) -> list[dict]` | Assets die nach `since` (ISO-Timestamp) hochgeladen wurden. Für den Immich-Poller. |

### Asset-Modifikation

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `tag_asset` | `async tag_asset(asset_id, tag_name, *, api_key=None) -> dict` | Tag hinzufügen (wird erstellt falls nicht vorhanden). |
| `untag_asset` | `async untag_asset(asset_id, tag_name, *, api_key=None) -> dict` | Tag entfernen. |
| `update_asset_description` | `async update_asset_description(asset_id, description, *, api_key=None) -> dict` | Description-Feld setzen via `PUT /api/assets/{id}`. |
| `archive_asset` | `async archive_asset(asset_id, *, api_key=None) -> dict` | Asset archivieren. Unterstützt neue (visibility) und legacy (isArchived) API. |
| `lock_asset` | `async lock_asset(asset_id, *, api_key=None) -> dict` | Asset in den gesperrten Ordner verschieben (visibility: locked). |
| `copy_asset_metadata` | `async copy_asset_metadata(from_id, to_id, *, api_key=None) -> dict` | Kopiert Alben, Favoriten, Gesichter, Stacks von einem Asset auf ein anderes. Für den Upload+Copy+Delete Workflow. |
| `delete_asset` | `async delete_asset(asset_id, *, force=True, api_key=None) -> dict` | Asset löschen. **`force=True`** = permanent (kein Papierkorb). |

---

## config.py — ConfigManager

| Methode | Signatur | Beschreibung |
|---|---|---|
| `get` | `async get(key: str, default=None)` | Config-Wert lesen (aus Cache oder DB). |
| `set` | `async set(key: str, value, encrypted=False)` | Config-Wert schreiben (DB + Cache). |
| `is_setup_complete` | `async is_setup_complete() -> bool` | Prüft ob Ersteinrichtung abgeschlossen. |
| `is_module_enabled` | `async is_module_enabled(name: str) -> bool` | Prüft ob ein Modul aktiviert ist. |
| `set_module_enabled` | `async set_module_enabled(name: str, enabled: bool)` | Modul aktivieren/deaktivieren. |
| `seed_from_env` | `async seed_from_env()` | Initiale Config-Werte aus Umgebungsvariablen setzen. |

---

## system_logger.py — System-Logging

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `log_info` | `async log_info(source: str, message: str, detail=None)` | Info-Level Log in `system_logs` Tabelle. |
| `log_warning` | `async log_warning(source: str, message: str, detail=None)` | Warning-Level Log. |
| `log_error` | `async log_error(source: str, message: str, detail=None)` | Error-Level Log. |

---

## database.py — Datenbank

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `async_session` | `async_sessionmaker(...)` | Session-Factory für SQLAlchemy async Sessions. |
| `init_db` | `async init_db()` | DB-Schema erstellen + Migrationen ausführen. |
| `seed_inbox_from_env` | `async seed_inbox_from_env()` | Inbox-Verzeichnisse aus Umgebungsvariablen anlegen. |

---

## pipeline/reprocess.py — Reprocess-Helper

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `prepare_job_for_reprocess` | `async prepare_job_for_reprocess(session, job, *, keep_steps=None, inject_steps=None, move_file=True, commit=True)` | Setzt einen Job für erneute Pipeline-Verarbeitung zurück. Verschiebt Datei nach `/reprocess/`, behält angegebene Steps (z.B. `{"IA-01"}`), injiziert neue Steps (z.B. `{"IA-02": skip_result}`). |

---

## Pipeline Steps (IA-01 bis IA-11)

Jeder Step hat eine `async execute(job, session) -> dict` Funktion.

| Step | Datei | Beschreibung |
|---|---|---|
| IA-01 | `step_ia01_exif.py` | EXIF-Daten auslesen (ExifTool + ffprobe für Video) |
| IA-02 | `step_ia02_duplicates.py` | Duplikat-Erkennung (SHA256 + pHash). Zusätzlich: `execute_video_phash()` für nachgelagerte Video-pHash-Berechnung. |
| IA-03 | `step_ia03_geocoding.py` | Geocoding (GPS → Land/Stadt/Stadtteil via Nominatim) |
| IA-04 | `step_ia04_convert.py` | Format-Konvertierung (HEIC/RAW → JPEG für KI-Analyse) |
| IA-05 | `step_ia05_ai.py` | KI-Bildanalyse (Kategorisierung, Tags, Description) |
| IA-06 | `step_ia06_ocr.py` | OCR-Texterkennung (für Screenshots) |
| IA-07 | `step_ia07_exif_write.py` | Keywords + Description in EXIF/XMP schreiben |
| IA-08 | `step_ia08_sort.py` | Sortierung: Immich-Upload oder lokale Verzeichnisstruktur |
| IA-09 | `step_ia09_notify.py` | Benachrichtigung (E-Mail/SMTP) |
| IA-10 | `step_ia10_cleanup.py` | Temp-Dateien aufräumen |
| IA-11 | `step_ia11_log.py` | Job-Abschluss loggen |

---

## filewatcher.py — Dateiüberwachung & Immich-Poller

| Funktion | Signatur | Beschreibung |
|---|---|---|
| `_scan_inbox` | `async _scan_inbox()` | Scannt alle konfigurierten Inbox-Verzeichnisse nach neuen Dateien. |
| `_poll_immich` | `async _poll_immich()` | Fragt Immich nach kürzlich hochgeladenen Assets und erstellt Jobs. |
| `_create_job_safe` | `async _create_job_safe(...)` | Erstellt einen neuen Job mit Duplikat-Check auf Dateiname+Grösse. |
| `_is_file_stable` | `_is_file_stable(filepath, expected_size) -> bool` | Prüft ob eine Datei stabil ist (nicht mehr geschrieben wird). |
| `_is_within_schedule` | `async _is_within_schedule() -> bool` | Prüft ob der aktuelle Zeitpunkt innerhalb des konfigurierten Zeitplans liegt. |
| `trigger_manual_scan` | `trigger_manual_scan()` | Löst einen manuellen Inbox-Scan aus (von UI oder API). |

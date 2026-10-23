# Immich 3.1 — gemessenes Verhalten

Gemessen am 2026-10-23 auf der Dev-Instanz nach dem Upgrade 2.6.3 → **3.1.0**
(Issue #48), mit eigenen Wegwerf-Assets (zufällige 64×48-JPEGs) über den
API-Key aus der MA-Dev-DB. Alle Assets, Tags, Alben, Shared Links und Stacks
wurden danach wieder entfernt. Grundlage für den Umbau
[„Module & Datensicherheit"](UMBAU_MODULE.md), Risiken R-18, R-19, R-22 und
Primitive P7–P10 der [Inventur](UMBAU_MODULE_INVENTUR.md).

Der Kern ist als Test festgehalten:
`docker exec mediaassistant-dev python /app/test_immich_trash.py` (19 Prüfungen).

`<id>` steht für eine Asset-ID, `<tag>` für eine Tag-ID.

## Kurzfassung für den Umbau

| Befund | Folge |
|---|---|
| Ein Asset im Papierkorb antwortet auf `GET /api/assets/<id>` mit **200** (`isTrashed: true`), Download und Vorschau gehen weiter. | „GET = 200" heisst nicht „Asset ist sicher". Zustand immer über `get_asset_state()` lesen. |
| Upload einer Datei, deren Checksumme einem Papierkorb-Asset gehört, liefert `duplicate` mit der ID **des Papierkorb-Assets** und stellt es **nicht** wieder her. | **R-22 bestätigt:** `safe_upload_asset` meldet Erfolg, IA-08 löscht die Quelle, die einzige Kopie liegt im Papierkorb und wird nach 30 Tagen geleert. |
| `PUT /api/assets/copy` mit Standardwerten **ersetzt die Sidecar des Ziels** durch die der Quelle; danach liest Immich sie neu ein. | **R-18 bestätigt:** Tags, Beschreibung, Bewertung und Datum des neuen Assets werden durch die alten ersetzt. Beim Ersetzen `sidecar: false` senden und Tags/Beschreibung selbst übertragen. |
| `POST /api/trash/restore/assets` meldet `count` = Anzahl gesendeter IDs, auch wenn nichts im Papierkorb lag. | Wiederherstellen immer nachprüfen (`restore_asset()` tut das). |
| Papierkorb: an, 30 Tage, Leerung automatisch jede Nacht. | Passt zum lokalen Papierkorb (30 Tage). Live-Einstellung prüfen, sie ist pro Server änderbar. |
| `deviceId` gibt es in 3.x nicht mehr. | Der Poller erkennt eigene Uploads nur noch über bekannte Asset-IDs. Ersatz: eigener Metadaten-Schlüssel beim Upload (gemessen, siehe f). |
| Suche ohne `visibility` liefert in 3.x **alle** Sichtbarkeiten ausser `locked`. | `get_recent_assets` setzt jetzt `visibility: "timeline"` (Verhalten wie 2.x). |
| Ein mit `lock_asset` gesperrtes Asset sieht der API-Key nicht mehr (GET und DELETE → 400). | Im Register darf ein gesperrtes Immich-Exemplar nicht als „missing" gelten; es lässt sich per API auch nicht mehr löschen. |

## Upgrade 2.6.3 → 3.1.0

| Bereich | Änderung | Auf Dev gemacht |
|---|---|---|
| Version | `IMMICH_VERSION=v2` → `v3.1.0` (fest, nicht `v3`) | ja |
| Compose | Gegenüber 2.6.3 nur neuer Valkey-Digest (`valkey:9@sha256:8e8d64b4…`) | ja |
| Postgres | Gleiches Image `postgres:14-vectorchord0.4.3-pgvectors0.2.0`. 3.0 entfernt pgvecto.rs; Dev nutzt bereits VectorChord (`vchord 0.4.3`) | nichts nötig |
| Umgebungsvariablen | 3.0 entfernt `IMMICH_MACHINE_LEARNING_PING_TIMEOUT`, `MACHINE_LEARNING_PRELOAD__CLIP`, `…__FACIAL_RECOGNITION`; ML braucht x86-64-v2 | nicht gesetzt bzw. erfüllt |
| Migrationen | 30+ Schema-Migrationen laufen beim ersten Start automatisch (u. a. Album-Besitzer → `album_user`, `duration` → Ganzzahl) | ohne Fehler |
| Bestand | Assets (ID, Checksumme, Sichtbarkeit, Dateiname), Tags, Alben vor/nach verglichen | identisch |

Sicherung vor dem Upgrade: `pg_dumpall` (gzip) plus Kopie von Compose-Datei und
`.env`, ausserhalb des Repos. Die Mediendateien wurden nicht kopiert; das
Upgrade fasst sie nicht an.

## API-Änderungen, die MediaAssistant betreffen

| Änderung in 3.x | Wirkung auf MA | Stand |
|---|---|---|
| `deviceId`, `deviceAssetId` aus Upload und Asset-Antwort entfernt | Upload mit den Feldern geht weiter (`201`, Felder werden verworfen). Der Poller-Filter `deviceId != "MediaAssistant"` (`inputs/immich.py`) greift nie mehr. | Felder bleiben im Upload (Pflicht in 2.x), Kommentar ergänzt. Poller: erkennt eigene Uploads über das Register und das Upload-Metadatum `mediaassistant` (R-24, behoben) |
| Suche: `visibility` weggelassen → alle ausser `locked` (2.x: nur `timeline`) | Poller-Abfrage hätte archivierte und versteckte Assets (Video-Hälfte von Live-Fotos) geholt. Tag-Zählung und -Auflistung umfassen jetzt auch archivierte und versteckte Assets. | `get_recent_assets` fixiert `timeline`; Tag-Werkzeuge bewusst unverändert |
| `locked` nur mit erhöhter Sitzung | `POST /api/search/statistics {"visibility":"locked"}` → `401 {"message":"Elevated permission is required"}` mit API-Key | Tag-Werkzeuge sehen gesperrte Assets nicht |
| Fehlerformat (Zod) | Validierung: `400 {"message":"Validation failed","errors":[{"path":["id"],"message":"Invalid UUID",…}]}`. Nicht gefunden/kein Zugriff: `400 {"message":"Not found or no asset.read access"}` (kein 404) | `_server_message` funktioniert weiter |
| `PUT /api/assets`, `PUT /api/assets/{id}`, `PUT /api/tags/{id}`, `PUT /api/users/me`, `GET`/`PUT /api/jobs…` als veraltet markiert | Funktionieren in 3.1 noch. PATCH-Ersatz für Assets fehlt in der 3.1-Spezifikation. | unverändert |
| Legacy-Feld `isArchived` | `PUT /api/assets {"ids":[…],"isArchived":true}` → `204`, aber **ohne Wirkung** | Fallback in `archive_asset` ist auf 3.x wirkungslos, Hauptweg `visibility:"archive"` geht |
| `POST /api/tags` mit unbekanntem Feld `type` | `201`; doppelter Name → `400 {"message":"A tag with that name already exists"}` | `tag_asset` funktioniert |
| Suchantwort | `nextPage` (Text oder `null`) bleibt, `total` ist veraltet, `nextCursor` gibt es in 3.1 nicht | Paging in MA bedient beide |
| Album-Antwort ohne `assets`, `owner`, `ownerId`; Filter `shared` → `isShared` | `get_asset_albums` (`?assetId=`) unberührt. `test_ftag_immich.py` las `album["assets"]` und fand so nie ein Album. | Test entfernt (2026-10-23), ersetzt durch `test_folder_structure.py` |
| Entfernt: `PUT /api/assets/{id}/original`, `GET /api/assets/random`, `POST /api/assets/exists`, `GET /api/assets/device/{id}` | von MA nicht benutzt | – |

## a) Löschen mit `force=false`

| Aufruf | Antwort |
|---|---|
| `DELETE /api/assets {"ids":["<id>"],"force":false}` | `204` (leer) |
| `GET /api/assets/<id>` | `200`, `"isTrashed": true`, `visibility` und `checksum` unverändert |
| `GET /api/assets/statistics?isTrashed=true` | `{"images":1,"videos":0,"total":1}` |
| `POST /api/search/metadata {"id":"<id>"}` | `200`, `count: 0` — **nicht gefunden** |
| `POST /api/search/metadata {"id":"<id>","withDeleted":true}` | gefunden |
| `POST /api/search/metadata {"trashedAfter":"2000-01-01T00:00:00.000Z"}` | gefunden (nur Papierkorb) |
| `POST /api/search/statistics {}` und `{"withDeleted":true}` | Papierkorb **nicht** mitgezählt (`withDeleted` wirkt hier nicht) |
| `POST /api/search/statistics {"trashedAfter":"2000-01-01T00:00:00.000Z"}` | `{"total":1}` |
| `GET /api/assets/<id>/original`, `GET /api/assets/<id>/thumbnail` | `200` — Download geht weiter |
| `PUT /api/assets/<id> {"description":"…"}`, `PUT /api/tags/<tag>/assets` | `200` — Änderungen am Papierkorb-Asset sind erlaubt |

Endgültig löschen zum Vergleich:

| Aufruf | Antwort |
|---|---|
| `DELETE /api/assets {"ids":["<id>"],"force":true}` | `204` |
| `GET /api/assets/<id>` sofort danach | `200`, `"isTrashed": true` (intern Status „deleted") |
| … nach dem Bereinigungs-Job (gemessen < 4 s) | `400 {"message":"Not found or no asset.read access"}` |
| Anderes Asset lag gleichzeitig im Papierkorb | bleibt im Papierkorb — `force=true` leert nur das genannte Asset |
| `DELETE` mit unbekannter ID | `400 {"message":"Not found or no asset.delete access"}` |

In MA: `asset_exists()` ist für ein Papierkorb-Asset `True`, `fetch_asset_info()`
liefert es normal aus.

## b) Wiederherstellen

| Aufruf | Antwort |
|---|---|
| `POST /api/trash/restore/assets {"ids":["<id>"]}` (Papierkorb-Asset) | `200 {"count":1}`, danach `isTrashed: false` |
| dasselbe für ein Asset, das **nicht** im Papierkorb liegt | `200 {"count":1}` — nichts passiert, Zahl trotzdem 1 |
| dasselbe für ein endgültig gelöschtes Asset | `400 {"message":"Not found or no asset.delete access"}` |

Weitere Endpunkte (nicht benutzt): `POST /api/trash/restore` (ganzer Papierkorb
des Benutzers), `POST /api/trash/empty`. Wiederherstellen braucht die
API-Berechtigung `asset.delete`.

## c) Upload einer Datei, deren Checksumme einem Papierkorb-Asset gehört (R-22)

| Aufruf | Antwort |
|---|---|
| `POST /api/assets` (gleiche Datei, Asset im Papierkorb) | `200 {"status":"duplicate","id":"<id des Papierkorb-Assets>"}` |
| dasselbe mit Header `x-immich-checksum` | gleich |
| `GET /api/assets/<id>` danach | `200`, `isTrashed: true` — **nicht wiederhergestellt** |
| Asset-Statistik | unverändert — **kein neues Asset** |
| `POST /api/assets/bulk-upload-check {"assets":[{"id":"x","checksum":"<sha1 hex>"}]}` | `{"results":[{"id":"x","action":"reject","reason":"duplicate","assetId":"<id>","isTrashed":true}]}` |
| Upload nach `force=true`, Bereinigung noch nicht gelaufen | `201 created` — neues Asset, das gelöschte blockiert nicht mehr |

Folge: `safe_upload_asset` sieht `duplicate`, holt das Asset per `GET` (200,
Grösse und SHA-1 stimmen) und meldet Erfolg. `ImmichDuplicateUnavailable` greift
nicht, weil `GET` nicht scheitert. IA-08 löscht danach die lokale Quelle, obwohl
das einzige Exemplar im Papierkorb liegt. Der Immich-Code dafür ist in 2.6.3 und
3.1 gleich — das Risiko bestand schon vor dem Upgrade.

## d) `PUT /api/assets/copy` (R-18, P8, P9)

Standardwerte laut API (`AssetCopyDto`): `albums`, `favorite`, `sharedLinks`,
`sidecar`, `stack` — alle `true`. Felder für Tags, Beschreibung, Bewertung oder
Datum gibt es nicht. Antwort `204`; fehlendes Ziel →
`400 {"message":"Not found or no asset.copy access"}`. Berechtigung: `asset.copy`.

Messung: Quelle mit Album, Favorit, Bewertung 4, Aufnahmedatum 2001-02-03,
Beschreibung und Tag aus der eigenen XMP, Stack, Shared Link. Ziel frisch
hochgeladen mit **eigener** XMP (eigener Tag, eigene Beschreibung), dazu API-Tag
und API-Beschreibung.

| Feld am Ziel | `copy` mit Standardwerten | `copy` mit `"sidecar": false` |
|---|---|---|
| Album | übernommen | übernommen |
| Favorit | übernommen | übernommen |
| Shared Link | übernommen | übernommen |
| Stack | Ziel im Stack der Quelle | Ziel im Stack der Quelle |
| Sidecar-Datei | **gelöscht und durch die der Quelle ersetzt** (Inhalt identisch, Name `<Ziel>.xmp`) | unverändert |
| Tags | **alle eigenen weg** (API- und XMP-Tag), nur noch die XMP-Tags der Quelle | eigene bleiben, keine übernommen |
| Beschreibung | die der Quelle | eigene bleibt |
| Bewertung | die der Quelle (4) | nicht übernommen |
| Aufnahmedatum | das der Quelle | nicht übernommen |

Ursache (Code, in 2.6.3 und 3.1 gleich): Mit `sidecar=true` löscht Immich die
Ziel-Sidecar, kopiert die der Quelle daneben und startet die
Metadaten-Extraktion für das Ziel. Tags, die nicht in der XMP der Quelle stehen,
werden also gar nicht übertragen.

Folge für `safe_replace_asset`: Das neue Asset verliert die frisch von IA-07
geschriebenen Tags und die Beschreibung, in Immich und in der Sidecar-Datei.
Die Originaldatei selbst bleibt unberührt.

## e) Aufbewahrung und Leerung

| Aufruf | Antwort |
|---|---|
| `GET /api/system-config` (Admin) | `"trash": {"enabled": true, "days": 30}` |
| `GET /api/system-config/defaults` | `"trash": {"enabled": true, "days": 30}`, `nightlyTasks.startTime: "00:00"`, `databaseCleanup: true` |
| `GET /api/server/config` (ohne Admin lesbar) | `"trashDays": 30` |

Laut Code: Die Leerung läuft automatisch im nächtlichen Lauf (`nightlyTasks.startTime`,
Standard 00:00, nur wenn `databaseCleanup` an ist). Gelöscht werden
Papierkorb-Assets, deren Löschzeitpunkt älter als `trash.days` ist. Ist der
Papierkorb ausgeschaltet, gilt 0 Tage: alles im Papierkorb wird in der nächsten
Nacht gelöscht, auch was MA mit `force=false` dorthin gelegt hat. Ein erneutes
`DELETE … force=false` auf ein Asset im Papierkorb setzt den Löschzeitpunkt neu.

## f) Weitere Beobachtungen zu Löschen, Tags und Beschreibungen

| Beobachtung | Messung |
|---|---|
| **API-Tag direkt nach dem Upload verdrängt die XMP-Tags.** | Upload mit XMP-Tag X, sofort `PUT /api/tags/<Y>/assets` → am Ende nur Y (2 von 2). Mit 8 s Pause vor dem Taggen → X und Y. Grund: das Taggen schreibt die Sidecar (`TagsList`), bevor die erste Extraktion sie liest. |
| Immich liest Tags aus `TagsList`, sonst `HierarchicalSubject`, sonst `Keywords` — nicht aus `dc:subject`. | Laut Code. MA schreibt in Sidecars `Subject`; Tags kommen in Immich also nur über die API an. |
| Beschreibung per API lässt Tags stehen. | `PUT /api/assets/<id> {"description":…}` nach dem Taggen → Tags unverändert. |
| Löschen kurz nach Upload oder Änderung hinterlässt verwaiste Dateien im Immich-Speicher. | Taggen und innerhalb ~1 s `force=true` → Sidecar-Job schreibt danach noch eine XMP. Löschen während der Speicher-Vorlage verschiebt → Original und XMP bleiben am alten Ort. Vorschaubilder entstehen nach dem Löschen. Immichs Integritätsbericht (3.0) zeigt sie als „untracked". Laut Code würde ein späteres Asset am selben Vorlagenpfad eine dort liegende `<name>.xmp` als Sidecar übernehmen. |
| **Gesperrte Assets sind für den API-Key unsichtbar.** | `PUT /api/assets {"ids":["<id>"],"visibility":"locked"}` → `204`. Danach `GET /api/assets/<id>` → `400 {"message":"Not found or no asset.read access"}`, `DELETE` → `400 … no asset.delete access`, Suche findet es nicht, erneuter Upload → `200 duplicate` mit dieser ID. Ein API-Key hat nie eine erhöhte Sitzung. `get_asset_state` meldet solche Assets als „missing" — nach `lock_asset` darf „missing" also nicht „verloren" heissen. |
| Eigene Metadaten-Schlüssel beim Upload. | Multipart-Feld `metadata=[{"key":"mediaassistant","value":{…}}]` → `GET /api/assets/<id>/metadata/mediaassistant` → `200` mit Wert. Auch nachträglich per `PUT /api/assets/<id>/metadata`. Möglicher Ersatz für den `deviceId`-Filter des Pollers. |
| Stack wird aufgelöst, wenn seine Nicht-Hauptassets gelöscht werden. | Release-Notes 3.1, nicht gemessen. |
| Berechtigungen der Aufrufe | `asset.upload`, `asset.read`, `asset.update`, `asset.delete` (auch Wiederherstellen), `asset.copy`. |

## Neue Helfer in `backend/immich_client.py`

| Funktion | Verhalten |
|---|---|
| `get_asset_state(id)` | `"present"`, `"trashed"` oder `"missing"`. Wirft bei Zeitüberschreitung, 5xx, 401/403 und Validierungsfehlern — „missing" nur, wenn Immich es sagt (JSON-Antwort). „missing" heisst „für diesen Key nicht sichtbar" — auch gesperrte Assets. Ein endgültig gelöschtes Asset liest sich für wenige Sekunden als „trashed". |
| `trash_asset(id)` | `DELETE … force=false` (über `delete_asset`, protokolliert vorher/nachher), danach Prüfung auf „trashed", sonst Ausnahme. |
| `restore_asset(id)` | `POST /api/trash/restore/assets`, protokolliert vorher/nachher, danach Prüfung auf „present", sonst Ausnahme. Auf ein vorhandenes Asset: No-op. |

`delete_asset` und seine Aufrufer sind unverändert (`force=True` bleibt Standard).

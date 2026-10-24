# Ordnerstruktur in Immich — Möglichkeiten und Empfehlung

Frage: Dateien, deren Tags MA aus der Ordnerstruktur ableitet („Ordner-Tags"),
sollen in Immich **mit dieser Struktur** sichtbar und navigierbar sein.

Grundlage (Stand 2026-10-23):

- Quellcode und OpenAPI von Immich **v3.1.0** (Repository `immich-app/immich`,
  Tag `v3.1.0`), Doku auf docs.immich.app.
- Messungen auf der Dev-Instanz (Immich 3.1.0) mit eigenen Wegwerf-Assets,
  -Alben und -Tags. Alles wieder entfernt (Assets endgültig gelöscht, Alben,
  Tags, drei leere Ordner im Immich-Speicher), Papierkorb danach leer.
  Server-Einstellungen nur **gelesen**, nicht geändert.
- Inventur der zu migrierenden Sammlung (`migration_scan.db`, Schnellscan ohne
  Hashes), hier nur als anonymisierte Muster und Zahlen.

Verwandt: [IMMICH_3_VERHALTEN.md](IMMICH_3_VERHALTEN.md),
[UMBAU_MODULE.md](UMBAU_MODULE.md).

## Kurzfassung

| Rang | Option | Was Immich zeigt | Aufwand in MA | Admin in Immich |
|---|---|---|---|---|
| **1** | **Hierarchische Tags** `Ordner/Kategorie/Ereignis Jahr/…` | Tag-Ansicht als Baum mit Brotkrumen; ein Eltern-Tag zeigt alle Fotos darunter | mittel | nein |
| 2 | **Album pro Ereignis** + Speichervorlage `{{album}}` | Alben; Ordneransicht **flach** (ein Ordner pro Album) | klein | ja (Vorlage, serverweit) |
| 3 | **Externe Bibliothek** auf eine MA-Ablage mit Ordnerpfad | Ordneransicht mit **echtem, verschachteltem** Baum | gross | ja (Bibliothek, Mount, Scan) |
| 4 | Herkunft in Beschreibung bzw. Asset-Metadatum | nur Text im Info-Bereich bzw. unsichtbar | klein | nein |

**Empfehlung:** Option 1 als Kern, je Set mit Regeln für sinnvolle und technische
Ebenen; Sammlung A und B in zwei Sets. Ergänzend Option 2 nur auf Ereignis-Ebene
(heute schon fast so) und Option 4 als Herkunftsnachweis. Option 3 nur, wenn die
*Ordneransicht* selbst Pflicht ist — sie bricht mit dem Grundsatz „Immich hält
eine geprüfte Kopie".

Eine verschachtelte **Ordneransicht** für hochgeladene Assets ist mit Immich 3.1
nicht möglich: Die Speichervorlage kennt keinen Quellpfad, und `{{album}}` wird von
Schrägstrichen befreit (gemessen).

## Was MA heute tut

| Stelle | Verhalten |
|---|---|
| Schalter | `ProcessingSet.folder_tags`, pro Set; greift nur für Dateien aus einem Verzeichnis-Eingang (`file_operations.is_folder_tags_active`) |
| Pfad | relativ zum Inbox-Verzeichnis des Jobs (`source_inbox_path`), ohne Dateinamen |
| Tags (IA-07) | **jedes Wort** jedes Ordnernamens einzeln, dazu alle Ebenen mit Leerzeichen verbunden. In die Datei als IPTC `Keywords` (JPEG/PNG/TIFF/DNG) bzw. XMP `Subject` (HEIC, Videos, Sidecar) und in `keywords_written` |
| IA-02 | dieselbe Liste in `step_result['IA-02'].folder_tags`, damit sie das Verschieben in den Duplikat-Ordner übersteht |
| Album (IA-08) | ein Album = alle Ebenen mit Leerzeichen verbunden (`_get_folder_album_names`), dazu `own_album`/`donor_albums` aus der Duplikat-Zusammenführung |
| Immich-Tags | `immich_client.tag_asset()` ersetzt `/` durch ` - ` und sucht vorhandene Tags über `name` (das Blatt) — Hierarchie wird bewusst verhindert. `fileops._apply_tags` trägt fremde hierarchische Tags per ID weiter |
| Reihenfolge | Upload → Album → Tags |
| Lokale Ablage | Pfadvorlage der Kategorie (`{YYYY}`, `{MM}`, `{CAMERA}`, `{COUNTRY}`, …) — kein Platzhalter für den Quellordner |
| Duplikat-Merge | `routers/duplicates.py` vereinigt Ordner-Tags und Alben der Spender |

Die Ableitung steht viermal im Code (IA-02, IA-07, IA-08, Duplikate). Nach dem
Upload liegt die Quelldatei im Papierkorb; der rohe relative Pfad ist dann nur
noch indirekt im Job (`original_path` ist laut Umbau-Plan unzuverlässig).

Nebenbefund: Immich liest IPTC `Keywords` beim Einlesen ein und **zerlegt sie an
`/`** in Eltern und Kind (`utils/tag.ts`). Ein Schlagwort mit Schrägstrich wird
über die Datei also hierarchisch, über MAs API-Weg aber `A - B` — zwei
verschiedene Tags.

## Die Sammlung (Inventur, anonymisiert)

113 000 Mediendateien, ~0,8 TB, zwei Bereiche:

| Bereich | Dateien | Aufbau | Bedeutung der Ordner |
|---|---|---|---|
| **A** kuratiert | 71 300 | 30 Kategorien auf Ebene 1, darunter meist `Ereignis Jahr`, teils `Person/Ort`, `Fest/Jahr`, `Ereignis/Ereignis` | sinnvoll — ausser technischen Zweigen |
| A, technische Zweige | 34 500 (48 % von A) | Handy-Importe (`…/Photos from JJJJ`), Messenger-Medien, SD-Sicherung (`DCIM/NNN_XXXX`), Drohne, Videos | Quelle, nicht Inhalt |
| A, sinnvoll | 36 800 (52 %) | 24 Kategorien, 377 Ordner | Inhalt |
| **B** Geräte-Sicherungen | 41 700 | `Gerät/JJJJ/MM` (88 %), `Gerät/Messenger/JJJJ-MM-TT`, `Gerät/DCIM/…` | nur Gerät und Datum |

Tiefe in A (Ebenen unter dem Bereich): 0 → 251, 1 → 10 500, 2 → 18 900,
3 → 36 900, 4 → 3 500, 5 → 1 300. Kein Ordnername enthält `/` oder `|`, der
längste hat 39 Zeichen.

**Heute** (Ordner-Tags an, alles in einem Set) entstünden rund **880 flache Tags
aus A und 750 aus B** (im Schnitt 5 pro Datei, 293 davon nur Zahlen wie `2019`,
`07`, `2019-07-14`) und rund **840 Alben** (400 aus A, 437 aus B).

## Antworten

### 1. Ordneransicht („Ordner")

| Punkt | Befund | Quelle |
|---|---|---|
| Grundlage | Verzeichnis von `asset.originalPath` — nichts anderes | `server/src/repositories/view-repository.ts` |
| Umfang | nur eigene Assets, nur Sichtbarkeit `timeline` (keine archivierten, gesperrten, versteckten), nicht im Papierkorb | ebd. |
| Hochgeladen, Vorlage aus | `upload/<Benutzer-ID>/xx/yy/<UUID>.<ext>` — zufällig | Doku FAQ |
| Hochgeladen, Vorlage an | `library/<Benutzer>/<gerenderte Vorlage>` | `storage-template.service.ts` |
| Externe Bibliothek | der echte Pfad, wie er im Container eingehängt ist | `library.service.ts` |
| Darstellung | Baum; Strecken mit nur einem Kind werden zusammengefasst | `web/src/lib/utils/tree-utils.ts` |
| Einschalten | pro Benutzer: Kontoeinstellungen → Funktionen → Ordner (Standard **aus**; Dev: aus) | `utils/preferences.ts` |

### 2. Speichervorlage

| Punkt | Befund |
|---|---|
| Variablen 3.1 | `filename`, `ext`, `filetype` (IMG/VID), `filetypefull`, `assetId`, `assetIdShort`, `album`, `album-startDate-<T>`, `album-endDate-<T>`, `make`, `model`, `lensModel`; Datum `y yy M MM MMM MMMM W WW d dd h hh H HH m mm s ss SSS` (aus `fileCreatedAt`, Server-Zeitzone). Handlebars mit `{{#if}}…{{else}}…{{/if}}` |
| Quellpfad? | **keine Variable** für Quellordner, Tags, Beschreibung oder eigene Metadaten |
| `album` | erstes Album nach Erstelldatum absteigend (= zuletzt erstelltes), durch `sanitize-filename` geschickt, Punkte entfernt → **kein `/`, also nur eine Ebene** |
| `filename` | Basisname des `originalFileName` — ein mitgeschickter Pfad wird abgeschnitten |
| Missbrauch | `make`/`model`/`lensModel` werden nicht bereinigt; ein `/` darin ergäbe Unterordner. Verfälscht Kameradaten, Suche und Karte — **abgelehnt** |
| Wann | nach **jeder Metadaten-Extraktion** (Upload, nach Sidecar-Schreiben infolge Tag- oder Beschreibungsänderung, Neueinlesen) als Einzeljob; dazu der Gesamtjob „Storage Template Migration" (manuell) |
| Album nachträglich | Album-Zuordnung allein löst **nichts** aus; die Datei zieht erst bei der nächsten Extraktion oder beim Gesamtjob um |
| Rechte | Systemeinstellung: nur Admin, **serverweit für alle Benutzer** (pro Benutzer nur das Speicher-Label) |
| Externe Assets | werden nie verschoben |
| Dev (gelesen) | an, Vorlage `{{#if album}}{{album}}{{else}}{{y}}/{{MM}}{{/if}}/{{filename}}`; Produktion unbekannt |

Quelle: `server/src/services/storage-template.service.ts`, `services/job.service.ts`
(Kette Sidecar-Schreiben → Extraktion), `services/metadata.service.ts`,
`repositories/album.repository.ts` (`getByAssetId`), docs.immich.app/administration/storage-template.

### 3. Externe Bibliotheken

| Punkt | Befund | Folge für MA |
|---|---|---|
| Anlegen, Scannen, Ausschlussmuster | nur Admin (`/api/libraries…`, `admin: true`); ein Besitzer pro Bibliothek, nicht änderbar | Admin-Key nötig; eine Bibliothek pro Immich-Benutzer |
| Pfad | muss im Container eingehängt sein, darf **nicht** im Upload-Speicher liegen | Mount in Immichs Compose (Produktion) |
| Einlesen | nächtlicher Scan (Dev: täglich 00:00), manuell/API, oder experimenteller Watcher (Dev: aus) | MA muss nach dem Ablegen scannen lassen und auf das Asset warten (Suche nach `originalPath`) |
| Besitz der Dateien | Immich besitzt sie nicht; Datei fehlt → offline → Papierkorb beim nächsten Scan | Immich hält **keine zweite Kopie** |
| Löschen in Immich | beim Leeren des Papierkorbs wird das **Original auf der Platte gelöscht**, wenn der Mount beschreibbar ist (`deleteOnDisk`) | Mount zwingend `:ro` |
| Sidecars | beim Import gelesen (`<Datei>.xmp`); Schreiben nur bei beschreibbarem Mount | mit `:ro` bleiben Tag-Änderungen in Immich nur in dessen DB |
| Verschieben/Umbenennen | gilt als neues Asset — Alben, Beschreibung, Gesichter aus Immich weg | Regeländerung in MA = neue Assets |
| Duplikate | Prüfsumme = SHA-1 des **Pfades**, nicht des Inhalts | kein Abgleich mit Uploads (Handy-App!), MAs Prüfung per Inhalts-Prüfsumme greift nicht |
| Speichervorlage | gilt nicht | Pfad bestimmt allein MA |
| Poller | externe Assets erscheinen in der Zeitachse | Poller muss `libraryId` filtern, sonst verarbeitet MA sie erneut |

Quelle: docs.immich.app/features/libraries, `services/library.service.ts`
(`validateImportPath`, Prüfsumme `sha1Path`), `services/asset.service.ts`
(`handleAssetDeletion`), `controllers/library.controller.ts`.

### 4. Hierarchische Tags

| Punkt | Befund |
|---|---|
| Speicherung | Tag hat `value` (voller Pfad `A/B/C`), `name` (Blatt `C`), `parentId`; Abschlusstabelle `tag_closure` |
| Anlegen | `PUT /api/tags {"tags":["A/B/C"]}` legt fehlende Eltern an und liefert das Blatt (gemessen). `POST /api/tags {"name":"A/B/C"}` legt **ein** Tag mit Wert `A/B/C` **ohne** Eltern an — kaputter Baum (gemessen; deshalb MAs ` - `-Ersatz) |
| Zuordnen | per ID: `PUT /api/tags/{id}/assets` oder `PUT /api/tags/assets {"tagIds","assetIds"}` (gemessen) |
| Aus Dateien | Reihenfolge `TagsList` (digiKam, `/`), sonst `HierarchicalSubject` (Lightroom, `|` → `/`), sonst IPTC `Keywords` (an `/` zerlegt); beide ersten gemessen. `dc:subject` wird nicht gelesen |
| Zurück in die Sidecar | nach einem API-Tag schreibt Immich `TagsList` mit vollem Pfad (gemessen) |
| Anzeige Web | Tag-Seite mit Baum in der Seitenleiste, Brotkrumen, Kacheln der Kinder; Zeitachse eines Tags **enthält alle Nachfahren** (`withTagId` über `tag_closure`); Suche nach dem obersten Tag findet das Asset (gemessen) |
| Aufräumen | Job „Tag-Bereinigung" löscht nur Tags ohne Assets in ihrem ganzen Teilbaum; Löschen eines Eltern-Tags löscht die Kinder mit |
| Einschalten | pro Benutzer: Funktionen → Tags (Standard aus; Dev: an) |
| Mobile-App | nicht geprüft |
| MA heute | **nicht unterstützt**: `tag_asset`/`untag_asset` entfernen `/` und suchen nach `name`. Gleiche Blattnamen unter verschiedenen Eltern (z. B. zweimal `2007`) wären mehrdeutig |

Quelle: `services/tag.service.ts`, `utils/tag.ts`, `repositories/tag.repository.ts`,
`services/metadata.service.ts` (`getTagList`, `applyTagList`), `utils/database.ts`
(`withTagId`), `web/src/routes/(user)/tags/…/+page.svelte`, docs.immich.app/features/tags.

### 5. Alben

| Punkt | Befund |
|---|---|
| Verschachtelung | keine (Tabelle `album` ohne Eltern) |
| Name | freier Text; im Ordnernamen der Vorlage ohne `/ \ : * ? " < > \|` und ohne Punkte |
| Mehrere Alben | `{{album}}` nimmt das zuletzt erstellte → bei Duplikat-Merge (`donor_albums`) kann die Datei in den Ordner eines Spender-Albums wandern |
| Anzahl | für ~180 Alben unkritisch; MAs `_get_or_create_album` lädt bei jedem Aufruf die ganze Albumliste |
| Konvention | Name aus den sinnvollen Ebenen bis zum Ereignis, z. B. `Kategorie › Ereignis Jahr`; voller Pfad in die Album-Beschreibung |

### 6. Upload-Metadaten und andere 3.x-Funktionen

| Weg | Sichtbar | Durchsuchbar | Taugt für |
|---|---|---|---|
| Multipart `metadata=[{"key":"mediaassistant","value":{…}}]` (gemessen, siehe IMMICH_3_VERHALTEN f) | per API | nein (kein Filter in `MetadataSearchDto`) | MAs eigene Herkunft (Regeln später neu anwenden, R-24) |
| Beschreibung | Info-Bereich | ja (Suchfeld Beschreibung) | Herkunftstext, kollidiert mit der KI-Beschreibung |
| Feld `filename` | Dateiname | ja | nichts — Vorlage nimmt nur den Basisnamen |
| Sidecar beim Upload mit `TagsList`/`HierarchicalSubject` | Tags | ja | hierarchische Tags schon bei der ersten Extraktion |
| `deviceId` | entfernt in 3.x | – | – |

## Messungen auf Dev (Immich 3.1.0)

`P` steht für das Wegwerf-Präfix, `<Benutzer>` für den Speicherordner.

| Versuch | Ergebnis |
|---|---|
| Upload, sofort danach Album (Reihenfolge wie MA) | Datei liegt in `<Benutzer>/JJJJ/MM/` — die Vorlage war schon angewendet, bevor das Album gesetzt war (kleines JPEG) |
| Album nach der Vorlage, 10 s warten | unverändert in `JJJJ/MM/` |
| danach ein Tag per API | **1 s später** in `<Benutzer>/<Album>/` (Tag → Sidecar schreiben → Extraktion → Vorlage) |
| Album `P/Kat/Ereignis` | Ordner `PKatEreignis` — flach |
| `PUT /api/tags` mit `P/Kategorie/Ereignis 2007` und `…/Ereignis 2007/Ereignis 2007` | Baum `P` → `Kategorie` → `Ereignis 2007` → `Ereignis 2007`; zwei Tags mit gleichem `name`, verschiedenem `value` |
| `POST /api/tags` mit `P/x/y` | `201`, Wert `P/x/y`, Name `y`, kein Eltern-Tag |
| Upload mit Sidecar `TagsList` / `HierarchicalSubject` | Baum mit Eltern angelegt, Asset am Blatt |
| Sidecar im Immich-Speicher nach API-Tag | enthält `TagsList` mit vollem Pfad |
| `GET /api/view/folder/unique-paths` | die Album-Ordner unter `<Benutzer>/` |

Folge: Dass MAs Uploads auf Dev im Album-Ordner landen, hängt am **Taggen nach
dem Album**, nicht an der Album-Zuordnung selbst.

## Sinnvolle und technische Ebenen: Regeln

Vorschlag als **Ordner-Regeln pro Set** (passt zur Regel „alles, was die
Verarbeitung festlegt, gehört zum Set"):

| Regel | Standard | Wirkung auf die Sammlung |
|---|---|---|
| Ebenen weglassen (Muster je Ordnername) | `^\d{1,2}$` (Monat), `^\d{4}-\d{2}(-\d{2})?$`, `^DCIM$`, `^\d{3}[A-Z_]\w{2,7}$` (Kamera-Rolle), `^Photos from \d{4}$`, eigene Liste (z. B. Messenger-Ordner) | entfernt das Technische in B fast vollständig |
| Reine Jahres-Ebene | **an Eltern anhängen** (`Fest/2019` → `Fest 2019`), wahlweise weglassen/behalten | 4 200 Dateien in A; das Jahr unterscheidet dort Ereignisse |
| Gleiche Ebenen zusammenfassen | an (`Ereignis/Ereignis` → `Ereignis`) | 2 000 Dateien in A |
| Ab Ebene / höchstens Ebenen | 0 / 4 | nach den Regeln ist A 1–5 tief; Ebene 5 betrifft 13 Dateien |
| Zweige ausnehmen (Muster auf relativen Pfad) | leer | die technischen Zweige in A: kein Ordner-Tag, wahlweise ein Quellen-Tag |
| Wurzel-Tag | `Ordner` | trennt den Baum von KI- und Orts-Tags |
| Wörter einzeln taggen (heutiges Verhalten) | aus bei hierarchisch | verhindert die ~1 600 flachen Tags |

Ergebnis, simuliert auf der Inventur: der sinnvolle Teil von A ergibt **363 Pfade
bzw. 391 Tag-Knoten** unter 24 Kategorien und **~180** Kombinationen
Kategorie/Ereignis (Album-Kandidaten). Die technischen Zweige von A schrumpfen auf
15 Pfade, B auf 23 (fast nur `Gerät`).

## Sets pro Quelle

| Aufteilung | Geht heute? | Bewertung |
|---|---|---|
| Set „Kuratiert" (A, Ordner-Tags an) + Set „Geräte" (B, aus) | ja | löst B vollständig; Datum steht ohnehin in den EXIF-Daten |
| Technische Zweige von A in ein eigenes Set | **nein**: Eingänge dürfen nicht verschachtelt sein (`routers/processing_sets.py`), der Scanner läuft in jeden Unterordner, ausgenommen sind nur Synology-Systemordner | braucht „Zweige ausnehmen" (oben) |
| Jede Kategorie von A als eigener Eingang (Geschwister sind erlaubt), technische ins Set „Geräte" | ja, 30 Eingänge | Kategorie-Name ist dann Teil des Eingangs und fehlt im relativen Pfad → bräuchte „Präfix aus Eingangs-Bezeichnung"; die 251 Dateien direkt in A blieben aussen vor |
| Technische Zweige vor der Migration auf dem NAS nach B verschieben | ohne Code | verändert die Quelle; nur mit Prüfung durch ma-migration-verify |

Sets allein reichen also für A/B, nicht für die technischen Zweige innerhalb von A.

## Optionen im Detail

### Option 1 — Hierarchische Tags (empfohlen)

**Immich zeigt:** Tag-Seite `Ordner` → 24 Kategorien → Ereignisse → Unterordner;
ein Klick auf eine Kategorie zeigt alle Fotos darunter. Für alle Benutzer, ohne
Admin, unabhängig von Speichervorlage und Bibliothek, auch für Assets, die schon in
Immich liegen. Ordneransicht bleibt, wie die Vorlage sie macht.

| Was ändern | Wo |
|---|---|
| Set-Felder: Modus (flach/hierarchisch), Wurzel-Tag, Ordner-Regeln (JSON) + Editor + i18n | `models.ProcessingSet`, `sets.py`, `routers/processing_sets.py`, Template, `i18n/*.json` |
| **eine** Funktion „bereinigter Ordnerpfad" statt vier Kopien | `file_operations.py`; Aufrufer IA-02 `_extract_folder_tags`, IA-07, IA-08 `_get_folder_album_names`, `routers/duplicates.py` |
| rohen relativen Pfad dauerhaft speichern (IA-02-Ergebnis bzw. Laufparameter), dazu Asset-Metadatum `mediaassistant` | IA-02, Upload in `fileops.upload` |
| Datei/Sidecar: `TagsList` (`Ordner/…`) mit **allen** MA-Tags, optional `HierarchicalSubject` (`Ordner|…`) — sonst liest Immich bei vorhandener `TagsList` die übrigen Schlagwörter nicht | IA-07 über `fileops.write_metadata` |
| Immich: Upsert über `PUT /api/tags`, Zuordnung per ID, Vergleich über `value`; hierarchische Werte nie durch `tag_asset()` | `immich_client.py`, `fileops._apply_tags`, IA-08 `_tag_immich_asset` (inkl. Entfernen veralteter Tags) |
| Duplikat-Merge: Pfade vereinigen statt Wortlisten | `routers/duplicates.py` |
| Tag-Werkzeuge: auf `value` abgleichen; Sidecar-Hälfte muss auch `TagsList`/`HierarchicalSubject` säubern, sonst kehren gelöschte Tags zurück | `routers/tools.py` |
| Tests: US-10 erweitern (Baum, Eltern zeigt Kind), `test_ftag_immich.py` (3.x-Albumantwort), Reproducer mit synthetischem Baum nach den Mustern oben | `test_e2e_user_stories.py` u. a. |

**Risiken:** Tags gelten pro Immich-Benutzer. Regeländerungen hinterlassen alte
Pfade, wenn das Entfernen veralteter Tags nicht mitgezogen wird. Löschen eines
Eltern-Tags in Immich nimmt den Teilbaum mit. Passt zum Umbau: keine neue
Datei- oder Löschoperation, Schreiben in die Datei läuft über `fileops`.

### Option 2 — Album pro Ereignis + Speichervorlage

**Immich zeigt:** ~180 Alben `Kategorie › Ereignis Jahr`; in der Ordneransicht
~180 flache Ordner unter `<Benutzer>/`, alles ohne Album (B, technische Zweige,
Handy-App) unter `JJJJ/MM/`.

| Was ändern | Wo |
|---|---|
| Album-Name aus dem bereinigten Pfad bis zur Ereignis-Ebene (einstellbar), voller Pfad in die Album-Beschreibung | IA-08 `_get_folder_album_names`, `immich_client` |
| Reihenfolge festschreiben: Album **vor** Tags/Beschreibung, damit die Vorlage greift; oder nach dem Lauf den Gesamtjob anstossen (Admin) | IA-08 |
| Vorlage in Immich (Admin, gilt für alle Benutzer) | Immich-Einstellungen — auf Dev schon gesetzt |

**Risiken:** nur eine Ebene; mehrere Alben → zuletzt erstelltes gewinnt;
Umbenennen eines Albums verschiebt nichts. Immich verschiebt die Datei in seinem
eigenen Speicher — MAs Register kennt das Asset über die ID und ist nicht betroffen.
Bekannt: Löschen während des Verschiebens hinterlässt verwaiste Dateien
(IMMICH_3_VERHALTEN f).

### Option 3 — Externe Bibliothek

**Immich zeigt:** in der Ordneransicht den bereinigten Baum
`Kategorie/Ereignis Jahr/…`, beliebig tief — als einzige Option.

| Was ändern | Wo |
|---|---|
| neues Ziel „Ablage + Immich extern": Platzhalter `{ORDNER}` in der Pfadvorlage | IA-08 `_resolve_path`, Set |
| nach dem Ablegen Scan anstossen, auf das Asset warten (Suche nach `originalPath`), dann Tags/Alben per API | IA-08, `immich_client` (Admin-Key) |
| Register: Immich-Asset als **Verweis**, nie als zweite Kopie; Lösch-Regeln entsprechend | `fileops.py`, `file_copies`/`asset_refs` |
| Ausschlussmuster `**/.papierkorb/**` (der Papierkorb liegt im Basis-Pfad) | Immich-Bibliothek |
| Poller: `libraryId` ausfiltern | `inputs/immich.py` |
| Mount `:ro` in Immichs Compose | Produktion |

**Risiken:** Immich hält keine Kopie mehr — die Ablage braucht eine eigene
Sicherung. Kein Inhalts-Abgleich mit Uploads (Handy-App) → doppelte Assets.
Regeländerung oder Umsortieren = neue Assets, Änderungen in Immich gehen verloren.
Mit `:ro` keine Sidecars von Immich. Höchster Aufwand, Admin auf Produktion nötig.

Variante „NAS-Quelle direkt als Bibliothek einbinden": zeigt die Originalstruktur
sofort, umgeht aber MAs ganze Pipeline (Duplikate, KI, Sortierung) und auch die
technischen Zweige — nicht empfohlen.

### Option 4 — Herkunft als Text oder Metadatum

Bereinigten Pfad als Satz in die Beschreibung („Ordner: Kategorie/Ereignis Jahr")
und den rohen relativen Pfad ins Asset-Metadatum `mediaassistant`. Nicht
navigierbar, aber durchsuchbar (Beschreibung) bzw. für MA lesbar — nützlich, um
geänderte Regeln später ohne Quelldatei neu anzuwenden.

## Empfehlung

1. **Option 1** für die Navigation: Wurzel `Ordner`, hierarchisch, ohne
   Wort-Tags, mit den Regeln oben. Sie liefert die Struktur dort, wo Immich sie
   wirklich verschachtelt darstellt, braucht keinen Admin und fügt dem Umbau keine
   neue Datei- oder Löschoperation hinzu.
2. **Zwei Sets:** „Kuratiert" (A) mit Ordner-Tags und Regeln, „Geräte" (B) ohne.
   Für die technischen Zweige in A die Regel „Zweige ausnehmen" — Sets können das
   nicht, weil Eingänge nicht verschachtelt sein dürfen.
3. **Option 2 schlank:** ein Album pro Kategorie/Ereignis (~180) statt heute ~840;
   mit der Album-Vorlage ergibt das eine flache, aber lesbare Ordneransicht.
4. **Option 4** als Herkunftsnachweis: roher Pfad im Asset-Metadatum.
5. Option 3 nur, wenn die verschachtelte *Ordneransicht* ausdrücklich gefordert ist.

## Offene Fragen

1. Reicht die **Tag-Ansicht** als Navigation, oder muss es die **Ordneransicht** sein?
2. Welche **Speichervorlage** ist auf Produktion gesetzt, und ist MAs Key dort Admin?
3. Unter welchem **Immich-Benutzer** landet die Migration (Tags und Bibliotheken gelten pro Benutzer)?
4. Jahres-Ebenen **anhängen** (`Fest 2019`) oder weglassen? Höchstens 4 Ebenen?
5. Technische Zweige in A: gar kein Ordner-Tag, oder ein Quellen-Tag (`Quelle/Handy`, `Quelle/Messenger`)? Oder vorher auf dem NAS nach B verschieben?
6. Alben pro Ereignis gewünscht, oder nur Tags? Wurzel-Name `Ordner` recht?
7. Sollen die heutigen flachen Wort-Tags bestehender Assets in den Baum überführt werden?
8. Sichert das Handy B-Inhalte schon per Immich-App? Dann meldet Immich beim Upload `duplicate`; bei Option 3 entstünden Doppel.
9. Migration: Der Eingang verbraucht die Dateien (nach dem Upload in den Papierkorb). Läuft sie auf einer Kopie der NAS-Freigabe oder auf dem Original?
10. Muss die Struktur auch in der **Mobile-App** navigierbar sein? (Tags dort nicht geprüft.)

## Entscheide (2026-10-23)

1. **Zwei Optionen pro Set, einzeln einschaltbar und kombinierbar:**
   „Ordner als verschachtelte Tags" (Tag-Baum, z. B. `Ordner/Kategorie/Ereignis`)
   und „Ordner als Alben" (ein Album pro Ereignis).
2. **Technische Zweige** (Handy-Importe, Messenger, SD-Backup, Drohne) werden vor
   der Migration auf dem NAS aus der kuratierten Sammlung heraus sortiert; es braucht
   dafür keine Regel „Zweige ausschliessen". Geräte-Backups laufen in einem eigenen
   Set ohne Ordner-Optionen.
3. **Reine Jahres-Ebene je nach Option:** bei den Tags eine eigene Ebene
   (`Kategorie/Ereignis/JJJJ` — ein Ereignis über alle Jahre ansehbar), bei den
   Alben an den Eltern angehängt (`Ereignis JJJJ` — ein Album pro Jahr).
4. Bedeutungslose Ebenen (Monat, Datum, `DCIM`, Kamera-Ordner, „Photos from JJJJ")
   und doppelte Ebenen fallen bei beiden Optionen weg.

### Nachtrag zu Entscheid 1 (2026-10-23)

Die Tag-Hälfte ist eine **Auswahl aus vier**, nicht ein Schalter: ein
ausgeschalteter Schalter hätte verborgen, dass bis anhin einzelne Tags entstanden.
„Keine", „Einzelne Tags pro Ordner" (bereinigt), „Einzelne Tags, Wörter getrennt
(bisher)" (genau das alte Verhalten) und „Verschachtelte Tags". „Ordner als Alben"
bleibt ein eigener Schalter und lässt sich mit jeder der vier kombinieren.

## Umsetzung (2026-10-23)

| Teil | Wo |
|---|---|
| Regeln, eine Funktion statt vier Kopien | `backend/folder_path.py` (`levels`, `tag_path`, `album_name`, `folder_targets`) |
| Set-Felder | `folder_tag_mode` (`none` / `flat` / `legacy` / `nested`), `folder_albums`; `folder_tags` bleibt nur für die Migration |
| Roher Ordner | `step_result['IA-02']['folder_path']` (relativ zum Eingang, unbereinigt); nach einem Retry aus dem Protokoll (`file_ops`) wiedergefunden. Duplikat-Zusammenführung: `donor_folder_paths` |
| Datei/Sidecar | bei `nested`: `TagsList` und `HierarchicalSubject` mit **allen** Tags der Datei (Immich liest dann nur noch `TagsList`); `dc:subject`/IPTC `Keywords` bleiben flach |
| Immich | `PUT /api/tags` (Upsert mit Eltern), Zuordnung per ID, Vergleich über `value` (`immich_client.tag_asset_path`/`untag_asset_path`); flache Tags werden jetzt ebenfalls über `value` gesucht |
| Tag-Werkzeuge | Aufräumen entfernt Schlagwörter auch aus `TagsList`/`HierarchicalSubject`; Schreibweisen fasst nur flache Tags ohne Eltern und Kinder zusammen |

**Regeln (fest, nicht einstellbar):** weggelassen werden `^\d{1,2}$` (Monat, Tag),
`^\d{4}[-_.]\d{1,2}([-_.]\d{1,2})?$` (Datum), `DCIM`, DCF-Kamera-Ordner
`^\d{3}[A-Z_][A-Z0-9_]{4}$` (`104_PANA`, `100APPLE`), `Photos from JJJJ` /
`Fotos von JJJJ`; gleiche Nachbar-Ebenen (ohne Gross/klein) und eine Jahres-Ebene
unter einer Ebene mit demselben Jahr fallen zusammen; Jahr bei Tags eigene Ebene,
im Album an die Ebene davor gehängt; höchstens 4 Ebenen. Wurzel des Baums: `Ordner`.
Album-Name: die Ebenen mit Leerzeichen verbunden, wie bisher.

Die Regeln gelten für `flat`, `nested` und die Alben. `legacy` ist das alte
Verhalten unverändert (`folder_path.legacy_tags`, `legacy_album_name`, gegen eine
Kopie des alten Codes getestet): jedes Wort jeder Ebene, dazu alle Ebenen zusammen
als ein Tag, nichts weggelassen; das Album heisst nach allen Ebenen.

**Migration des alten Schalters:** `folder_tags` an → `folder_tag_mode = legacy` und
`folder_albums` an; aus → `none` und aus. Für bestehende Sets ändert sich damit
nichts. (Auf Dev lief die Migration einmal mit `flat`; diese Daten bleiben.) Jobs von
vorher (nur `folder_tags` im IA-02-Ergebnis) behalten bei `flat`/`legacy` ihre alten
Tags und ihren Album-Namen; Zusammenführung und Retry behandeln `legacy` wie `flat`.


## Tag-Gruppen (2026-10-24)

Set-Option `tag_groups` („Tags in Gruppen", Register „Tags & Ordner"), Standard
aus. Eingeschaltet kommt jedes Tag, das MediaAssistant schreibt, unter eine von
fünf festen Wurzeln — ausser den Bildinhalt-Tags der KI, die einzelne, flache
Tags bleiben wie bisher (Entscheid 2026-10-24). Die Namen stehen nur in
`backend/tag_groups.py` (`ROOTS`); IA-07, IA-08, die Ordner-Tags, das
Werkzeug, das Beispiel im Set-Editor und die Tests lesen sie dort.

| Wurzel | Inhalt | Beispiel |
|---|---|---|
| `01 Kategorie` | Kategorie des Sets (Review-Wahl, Sortier-Regeln, EXIF-Standard, KI-Korrektur) | `01 Kategorie/Persönliches Foto` |
| `02 Quelle` | `source` der KI | `02 Quelle/Kamerafoto` |
| `03 Orte` | Land › Ort › Quartier; kein Kanton/Bundesland (Entscheid 2026-10-24: `Schweiz/Bern/Bern` las sich wie ein doppelter Ort), leere Ebenen fallen weg, ohne Ort nur das Land | `03 Orte/Schweiz/Bern/Länggasse`, `03 Orte/Schweiz/Thun` |
| `04 Album` | Ordnerstruktur, ersetzt die Wurzel `Ordner` | `04 Album/Ferien/Tessin 2024/Ascona` |
| `05 Hinweise` | `unscharf` (KI-Qualität `blurry`), `Text erkannt` (OCR) | `05 Hinweise/unscharf` |
| *(keine)* | jedes Bildinhalt-Tag der KI (IA-05 `tags`), ein Jahr allein fällt weg | `Bühne` |

Die zweistelligen Nummern (Entscheid 2026-10-24, nach Versuchen mit `#…` und
`+…`) stellen die Gruppen in Immichs Tag-Liste nach oben. Immich liest die Tags
mit `ORDER BY value` (`TagRepository.getAll`, in 3.1.0 und 3.3.5 gleich); die
Datenbank sortiert mit der
Postgres-Sortierung `en_US.utf8` (glibc), die Satzzeichen, Symbole und
Leerzeichen auf der ersten Stufe überspringt. Gemessen in der Dev-Datenbank:

    ..  <  01 Kategorie  <  2026  <  3  <  Abend  <  Hintergrundbild  <  #Hinweise
        <  Höhle  <  +Kategorie  <  @beispielkonto  <  ~Test  <  _x  <  zz

Nur eine Ziffer am Anfang kommt also vor den Buchstaben; `#` und `+` zählen
nicht. Vor `01 Kategorie` stehen nur Namen aus lauter Satzzeichen/Symbolen
(`..`) und solche, die mit `00` oder `01` und einem kleineren Zeichen beginnen.

**Keine führenden Sonderzeichen.** Mit Tag-Gruppen beginnt kein Wert, den MA
schreibt, mit etwas anderem als einem Buchstaben oder einer Ziffer
(`tag_groups.level`, Unicode-tauglich über `str.isalnum`): `@beispielkonto` →
`beispielkonto`, `-10%` → `10%`, `..` → nichts. Das gilt für die Bildinhalt-Tags
(danach Jahres-Regel und Doppelte ohne Gross/klein) und für alle Werte unter
den Gruppen (Kategorie, Quelle, Orte, Ordner-Ebenen).

**Kein Tag aus nur einer Jahreszahl.** Unter `04 Album` fällt eine reine
Jahres-Ebene weg, ein Bildinhalt-Tag, das nur ein Jahr ist, ebenso.

**04 Album.** Die Art der Ordner-Tags entscheidet die Form unter der Wurzel:
verschachtelt ein Pfad (`04 Album/A/B/C`), „pro Ordner" je Ebene ein Kind
(`04 Album/A`, `04 Album/B`), „bisher" jedes alte Wort (`04 Album/<Wort>`),
keine → kein Album-Tag. Die Ordner-Regeln von oben gelten weiter; zusätzlich
fällt eine **reine Jahres-Ebene** weg (`Wandern/Alpstein/2023` →
`04 Album/Wandern/Alpstein`, ein Ordner nur aus Jahren gibt kein Tag), ein Name
mit Jahr bleibt (`Tessin 2024`). Ohne Tag-Gruppen bleibt alles wie oben (Wurzel `Ordner`,
Jahr als eigene Ebene). „Ordner als Alben" (echte Alben) ist unverändert.

**Kategorie in Datei und Immich gleich.** Die Kategorie hängt nur von früheren
Ergebnissen (IA-01, IA-05), dem Dateinamen, der Review-Wahl und den Regeln des
Sets ab. IA-07 trifft deshalb dieselbe Entscheidung wie IA-08
(`step_ia08_sort.decide_category`). Mit Tag-Gruppen liest IA-07 dafür die
Regeln und Kategorien des Sets und die EXIF-Felder, die IA-08 liest — sie
zählen dann in seinem Fingerabdruck mit.

**Datei und Sidecar** (direkt und Sidecar-Modus): die Pfade und die
Bildinhalt-Tags (als Einträge mit einer Ebene) in `TagsList` (`/`) und
`HierarchicalSubject` (`|`) — eine Datei mit Baum muss dort alle ihre Tags
tragen, Immich liest dann nichts anderes; `dc:subject` bzw. IPTC `Keywords`
behalten flache Wörter — jede Ebene unter der Wurzel einmal und die
Bildinhalt-Tags — für andere Programme. MediaAssistants übrige flache Wörter
(Kategorie, Quelle, Ort, Ordner, `blurry`, `OCR`) kommen nicht in `TagsList`
(Immich läse sie sonst als eigene oberste Tags), frühere solche Einträge von MA
werden dort entfernt; eigene Einträge der Datei bleiben, auch ein Pfad des
Nutzers unter einer der Wurzeln. Ein Ergebnis von IA-07 mit Tag-Gruppen trägt
die Marke `tag_groups` mit den Namen der Wurzeln (`tag_groups.SCHEME`).

**Immich:** jeder Pfad über `PUT /api/tags` (legt fehlende Eltern an), Asset am
Blatt (`immich_client.tag_asset_path`); flach nur die Bildinhalt-Tags. Beim Hochladen
legt Immich dieselben Tags aus der `TagsList` der Datei an — gleichzeitige
Aufrufe können `HTTP 500 Failed to tag assets` geben; IA-08 zählt nach der
Nachkontrolle, was am Asset hängt.

**Neu ausführen.** Der Schalter (mit den Namen der Wurzeln) ist eine Eingabe von
IA-07 und IA-08, aber nur solange er an ist: Sets ohne Gruppen behalten die
Fingerabdrücke ihrer Läufe. Umschalten oder Umbenennen macht IA-07 und IA-08
veraltet. Ein Neu ausführen von IA-07+IA-08 im Immich-API-Modus ersetzt MAs
eigene alte Tags (`immich_tags_written` des letzten Laufs) durch die neuen, in
beide Richtungen; Bildinhalt-Tags (gleicher Wert) und Tags des Nutzers bleiben.
IA-08 vergleicht nach dem Wert in Immich und erkennt einen alten Pfad (Wurzeln
eines früheren Schemas) an seinem Wert am Asset.
Immich schreibt nach jeder Tag-Änderung über die API die `TagsList` seiner
Sidecar neu (gemessen auf 3.1.0, auf 3.3.5 durch US-22 bestätigt: ein entferntes Tag verschwindet dort, ein Tag
nur in der Sidecar kommt mit „Metadaten neu einlesen" zurück) — ein Neueinlesen
bringt die alten flachen Tags also nicht zurück.

**Duplikate.** Mit Tag-Gruppen übernimmt der behaltene Job von Spendern nur
deren Bildinhalt-Tags (flach); Kategorie, Quelle, Ort und Hinweise beschreiben
die behaltene Datei selbst. Ein Ort kommt mit, wenn GPS vom
Spender übernommen wurde.

**Bestand umstellen** — Werkzeug „Tags in Gruppen umstellen"
(`tag_groups_migration.py`):

| Schritt | Wie |
|---|---|
| Auswahl | Jobs des Sets in Immich (`done`/`review`, `immich:…`), deren IA-07-Ergebnis nicht zum Schalter passt (Marke fehlt oder nennt andere Wurzeln bzw. ist da, obwohl aus). Nicht „veraltet": Läufe von vor 3.0 haben keinen Fingerabdruck und gelten nie als veraltet. Jobs ohne Set zählen zum ersten Set |
| Umstellen | Neu ausführen IA-07+IA-08 im API-Modus über `reruns.py` (Aufträge zu höchstens 1000 Jobs, nacheinander) — kein Download, kein Ersetzen; Option `tags_only`: IA-08 legt keine Alben an, füllt keine, lässt Beschreibung, Archiv und Sperre (Werte des vorigen Laufs bleiben im Ergebnis). Fotos im gesperrten Ordner werden abgelehnt (für den API-Key unsichtbar) |
| Nachkontrolle | je Asset der umgestellten Jobs: was der letzte IA-08-Lauf geschrieben hat, muss da sein; was MA früher für diese Jobs schrieb (IA-08 über die API, IA-07 als Stichwort in die Datei), muss weg; Jobs eines Assets zusammen. Korrigieren, ~6 s warten, erneut prüfen (höchstens 3 Runden). Grund: Immich 3.1 schreibt nach vielen Tag-Änderungen teils einen veralteten Stand zurück (siehe unten) |
| Leere Tags | nach ~3 s (Index) jedes Tag zählen: `POST /search/statistics` mit `tagIds` (zählt Nachfahren mit), `withDeleted` und dazu `visibility: hidden`. `visibility: locked` antwortet 401 („Elevated permission is required") |
| Löschen | nur MAs eigene leere Tags (je geschrieben laut IA-08-Läufen, die Wurzeln, `Ordner/…`), nie eines, das MA einem gesperrten Foto gegeben hat; fremde leere nur mit Schalter. Tiefste zuerst, ein Eltern-Tag erst ohne Kinder (Immich löscht Kinder mit) |

Immichs eigener Job „Tag-Bereinigung" (`POST /api/jobs {"name":"tag-cleanup"}`,
nur Admin, nicht nächtlich geplant) löscht leere Tags aller Benutzer direkt in
der Datenbank — inklusive richtiger Behandlung gesperrter Fotos. Er ist die
Alternative, wenn MAs Key Admin ist.

**Immich schreibt alte Tags zurück (gemessen 2026-10-24, Immich 3.1.0 und 3.3.5).** Jede
Tag-Änderung über die API (`PUT/DELETE /api/tags/{id}/assets`) schreibt die
Tags des Assets in `exif.tags`, sperrt sie und stellt ein Sidecar-Schreiben ein.
Dieses liest `exif.tags` erst, wenn es läuft, schreibt sie als `TagsList` in die
Sidecar und **entsperrt** sie; die anschliessende Metadaten-Extraktion übernimmt
die Liste der Sidecar (`replaceAssetTags`). Läuft ein solcher Auftrag mit einem
Zwischenstand, nachdem MA weitere Änderungen gemacht hat, setzt die Extraktion
den Zwischenstand. Mit einem Asset allein trat das nicht auf; mit sechs
gleichzeitig (je 8 Tags dazu, 8 weg) hatten alle sechs danach 1–4 alte Tags
zurück, dauerhaft. Auf 3.3.5 (wie Produktion) ist die Mechanik unverändert
(`handleSidecarWrite`, `applyTagList`); drei Messungen mit je sechs Assets
gleichzeitig: 1, 6 und 5 von 6 betroffen, ein Asset allein nie. Beim Umstellen
auf Dev (3.1.0) betraf es 145 von 399 Fotos (beim Umbenennen der Wurzeln danach
7); die
Nachkontrolle des Werkzeugs korrigiert das (Quelle:
`server/dist/services/metadata.service.js` `handleSidecarWrite`, `applyTagList`).

**Nachkontrolle in IA-08.** Mit Tag-Gruppen prüft IA-08 nach jedem Lauf, der in
Immich Tags geändert hat (`_settle_tags`): erster Blick nach 1 s, dann bis zwei
Blicke 0,75 s auseinander dieselben Tags zeigen (höchstens 8 s); fehlt ein Tag
dieses Laufs oder ist eines des letzten Laufs zurück, wird es korrigiert und
erneut geprüft (höchstens 3 Runden, Protokolleintrag „Immich hat alte Tags
zurückgesetzt"). Ergebnisfeld `immich_tags_settled` (Sekunden, Runden,
Korrigiertes). Ein neuer Upload braucht das meist nicht: Immich übernimmt die
Tags beim Einlesen aus der `TagsList` der Datei, IA-08 schreibt dann nichts.

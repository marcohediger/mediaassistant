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

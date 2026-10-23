# Umbau „Module & Datensicherheit"

Branch: `umbau/module-und-datensicherheit` · Auslieferung: zusammen mit den
Verarbeitungs-Sets und dem neuen Design als **ein Release 3.0.0**.
Grundlage: [`UMBAU_MODULE_INVENTUR.md`](UMBAU_MODULE_INVENTUR.md) (71 Operationen,
Risiken R-01 bis R-23).

## Ziel

1. **Keine Daten mehr verlieren.** Das ist der Kern, nicht die schönere Ansicht.
2. **Ein Haupteintrag pro Datei, Untereinträge für jeden Modul-Lauf und jede
   Dateioperation.** Nichts wird mehr überschrieben, alles hat einen Verlauf.
3. **Aufklappbare Ansicht:** zugeklappt ein Statusblock pro Modul, aufgeklappt
   die Läufe, Versuche und Verschiebungen.
4. **Module einzeln ausführen** — für eine Datei oder viele —, abhängige Module
   laufen mit.
5. **Ausführung nach Abhängigkeiten:** Was nicht voneinander abhängt, läuft
   gleichzeitig.

**Entscheide (2026-10-22):** Papierkorb statt endgültig löschen, 30 Tage
Aufbewahrung. Die Risiken der Inventur werden hier gelöst, nicht per Hotfix in
2.32.x. Die heutigen Tests werden durch neue ersetzt, bleiben bis dahin aber grün.

## Warum bisher Daten verloren gingen

Alle Datenverluste (v2.28.28, v2.29.8, quality_swap, v2.31.0, v2.32.5) und die
kritischen Risiken R-01 bis R-04 haben dieselbe Wurzel:

- **Wo eine Datei liegt und wem ein Immich-Asset gehört, steht in Feldern, die
  überschrieben werden** (`original_path`, `target_path`, `immich_asset_id`) — oder
  eben *nicht* nachgeführt werden: 36 076 erledigte Live-Jobs zeigen mit
  `original_path` auf einen alten Inbox-Pfad, an dem heute eine andere Datei liegen kann.
- **Löschentscheide werden aus diesem Zustand abgeleitet** statt aus geprüften
  Fakten („es gibt eine Datei an diesem Pfad" ≠ „das ist unsere Datei").
- **Es gibt keine Spur:** `safe_move` protokolliert in Produktion nichts (0 Zeilen
  „File moved" live), Immich löscht immer endgültig (`force=True`), die
  System-Logs werden nach 90 Tagen gelöscht.

## Grundsätze

1. **Ein einziger Weg für Dateioperationen.** Verschieben, Kopieren, Herunterladen,
   Hochladen, Ersetzen, Metadaten schreiben, Löschen — lokal und in Immich — gehen
   ausschliesslich durch ein neues Modul `fileops.py`. Nichts anderes ruft mehr
   `os.remove`, `safe_move`, `delete_asset` & Co. auf (ein Test prüft das per grep).
2. **Protokoll vor und nach jeder Operation, in der Datenbank, nie gelöscht.**
   Erst eine Zeile „Absicht", dann die Operation, dann „erledigt" oder
   „fehlgeschlagen". Stürzt die App dazwischen ab, sieht der Neustart die offene
   Absicht und prüft den echten Zustand.
3. **Register aller Exemplare.** Für jede Datei ist bekannt, wo Kopien liegen
   (lokaler Pfad, Immich-Asset, Arbeitskopie), mit Hash und wann zuletzt geprüft.
   Löschentscheide lesen nur noch dieses Register — nie mehr `original_path`.
4. **Löschen nur mit Rückfallebene.** Ein Exemplar darf nur weg, wenn
   (a) ein *anderes*, geprüftes Exemplar desselben Inhalts existiert und
   (b) kein anderer Job darauf verweist. Und auch dann: in den Papierkorb.
5. **Nichts überschreiben.** Ziele werden atomar reserviert (`O_EXCL`); liegt dort
   schon etwas, wird nur bei identischem Hash zusammengelegt, sonst umbenannt.
6. **Identität über Job-ID und Hash, nie über den Dateinamen.**
7. **Vor jedem Fix ein roter Test** (AGENTS.md). Jedes Risiko R-01 … R-23 bekommt
   einen Reproducer, der vor dem Umbau rot und danach grün ist.

## Datenmodell

`jobs` bleibt der **Haupteintrag pro Datei**. Neu:

### `module_runs` — ein Untereintrag pro Modul-Lauf

| Spalte | Inhalt |
|---|---|
| `id`, `job_id` | |
| `module` | `IA-01` … `IA-11` |
| `attempt` | fortlaufend pro Job und Modul |
| `trigger` | `pipeline`, `retry`, `rerun` (Modul einzeln), `cascade` (weil ein Vorgänger neu lief), `copied` (aus anderem Job übernommen), `migrated` |
| `status` | `queued`, `running`, `ok`, `warning`, `error`, `skipped`, `dry_run`, `superseded` |
| `params` | Laufparameter (ersetzt die versteckten Steuerwerte `_force_reupload`, `_retry_previous_immich_tags` in `step_result`) |
| `result`, `error` | Ergebnis (JSON) bzw. Fehler mit Traceback |
| `inputs_fingerprint` | Hash der Eingaben (welche Vorgänger-Läufe, welche Set-Einstellungen) — Grundlage für „veraltet" |
| `copied_from_run_id` | bei Übernahme aus einem anderen Job (Duplikat-Zusammenführung) |
| `started_at`, `finished_at`, `heartbeat_at` | Dauer; Heartbeat gegen falsches „hängt" (R-11) |

`jobs.step_result` bleibt als **abgeleitete Sicht** („jüngster gültiger Lauf je
Modul") und wird nur noch von einer Funktion geschrieben — in derselben
Transaktion wie der Lauf. Die rund 100 Lesestellen müssen dadurch nicht auf
einmal umgebaut werden; neue Stellen lesen über `runs.current(job, "IA-05")`.

### `file_copies` — Register aller Exemplare

| Spalte | Inhalt |
|---|---|
| `id`, `job_id` | Eigentümer |
| `kind` | `local`, `immich`, `work` (Arbeitskopie in `reprocess/`), `temp` (IA-04), `sidecar`, `takeout_json` |
| `location` | Pfad bzw. Asset-ID |
| `sha256`, `size` | Inhalt; bei Immich zusätzlich die Immich-Checksumme |
| `state` | `present`, `trashed`, `deleted`, `missing` (Bestandsprüfung fand es nicht) |
| `verified_at` | zuletzt geprüft |

Eindeutig: (`kind`, `location`) solange `present` — zwei Jobs können nicht
unbemerkt „dieselbe" Datei besitzen. Geteilte Immich-Assets (live 3 840) werden
über **`asset_refs`** (`asset_id`, `job_id`) ausgedrückt; gelöscht wird ein Asset
nur, wenn kein anderer Job mehr darauf verweist.

### `file_ops` — Protokoll (nur anfügen)

| Spalte | Inhalt |
|---|---|
| `id`, `job_id`, `run_id` | wer, in welchem Modul-Lauf |
| `op` | `move`, `copy`, `download`, `upload`, `replace`, `write_metadata`, `trash`, `restore`, `purge` |
| `src`, `dst` | je `kind:location` |
| `sha256_before`, `sha256_after`, `size` | |
| `actor` | Modul, Benutzeraktion (Behalten, Löschen, …) oder System (Bestandsprüfung, Papierkorb-Leerung) |
| `state` | `intent` → `done` / `failed` / `reconciled` |
| `created_at`, `finished_at`, `error` | |

Zeilen werden nie gelöscht und nur einmal von `intent` auf ihren Endzustand
gesetzt. Die System-Logs dürfen weiter rotieren — das Protokoll bleibt.

### Papierkorb

- **Lokal:** `<Basis-Pfad des Sets>/.papierkorb/<Datum>/<debug_key>/<Datei>` —
  gleicher Datenträger, also schnell, und im Dateimanager sichtbar. Leerung nach
  30 Tagen durch einen täglichen Lauf, jede Leerung im Protokoll (`purge`).
- **Immich:** `delete_asset(force=False)` — das Asset landet in Immichs eigenem
  Papierkorb (Aufbewahrung in Immich einstellbar, Standard 30 Tage).
- **Wiederherstellen** aus der Job-Ansicht: ein Knopf pro Exemplar im Papierkorb.

## Ausführung: Module mit Abhängigkeiten

Jedes Modul erklärt, was es braucht und was es tut (aus dem Code abgeleitet):

```
IA-01 EXIF ─┬─> IA-02 Duplikat (Schranke: Duplikat → Stopp, vor allen Netzwerk-Modulen)
            ├─> IA-03 Geocoding ─┐
            └─> IA-04 Konvertierung ─┴─> IA-05 KI ─> IA-06 OCR ─> IA-07 Tags schreiben ─> IA-08 Ablage
                                                       danach immer: IA-09 Benachrichtigung, IA-10 Aufräumen, IA-11 Log
```

| Modul | braucht | Art |
|---|---|---|
| IA-01 | — | liest |
| IA-02 | IA-01 | **Schranke** (Duplikat → Rest übersprungen) |
| IA-03 | IA-01, IA-02 | Netzwerk (Nominatim: 1 Anfrage/s) |
| IA-04 | IA-01, IA-02 | erzeugt Arbeitskopien |
| IA-05 | IA-01, IA-03, IA-04 | KI-Slot |
| IA-06 | IA-04, IA-05 | KI-Slot |
| IA-07 | IA-01 … IA-06 | **verändert die Datei** (Hash ändert sich) |
| IA-08 | IA-01, IA-02, IA-03, IA-05, IA-07 | **verschiebt / lädt hoch** |
| IA-09 … IA-11 | alle | Abschluss, läuft immer |

- **Gleichzeitig:** IA-03 und IA-04 (nach IA-02). Mehr nicht — IA-07 und IA-08
  bleiben strikt nacheinander und zuletzt. Der Zeitgewinn pro Datei ist klein
  (die KI dominiert), der Gewinn an Klarheit gross.
- **Betriebsmittel statt fester Slots:** KI-Slots (Summe der Backend-Slots),
  Geocoding (1/s), Immich-Uploads — je ein Semaphor. Ein Job belegt nur, was das
  gerade laufende Modul braucht.
- **Veraltet:** Läuft ein Modul neu (einzeln oder als Retry), werden die Läufe
  aller abhängigen Module `superseded` und laufen nach (Kaskade). Ob ein Lauf noch
  gilt, entscheidet `inputs_fingerprint`, nicht „Ergebnis vorhanden".
- **Einzeln ausführen:** „Geocoding neu" für eine Datei, eine Auswahl oder ein
  ganzes Set. Läuft nur, wenn eine geprüfte Kopie der Datei verfügbar ist
  (lokal oder Download aus Immich in eine *eindeutige* Arbeitskopie). IA-07/IA-08
  übernehmen die Änderung danach in Datei und Immich.
- **Claim auf Modul-Ebene:** `UPDATE module_runs SET status='running' WHERE
  status='queued'` — dasselbe atomare Muster wie heute auf Job-Ebene.

## Wie die Risiken der Inventur gelöst werden

| Risiko | Lösung |
|---|---|
| R-01, R-02, R-14 veralteter `original_path` | Quelle und Löschziel kommen aus `file_copies` mit Hash-Prüfung. Eine fremde Datei am alten Pfad hat einen anderen Hash und wird nie angefasst. |
| R-03 Download überschreibt | Downloads in eindeutige Arbeitskopien (`O_EXCL`), sofort registriert. |
| R-04, R-09, R-10a Duplikat-Auflösung löscht zuerst | Auflösung als Plan: alle Mitglieder atomar beanspruchen → behaltenes Exemplar prüfen → erst dann Spender in den Papierkorb, jeder Schritt mit Absicht/erledigt. |
| R-05, R-06 Überschreiben, Namenskollision | Ziel atomar reservieren; vorhandene Datei nur bei identischem Hash zusammenlegen (als geteiltes Exemplar), sonst neuer Name. Inbox innerhalb einer Ablage wird beim Speichern des Sets abgelehnt. |
| R-07 Probelauf mit Nebenwirkungen | Probelauf-Läufe haben Status `dry_run`, schreiben weder `target_path` noch `review`; Review und Löschen ignorieren sie. |
| R-08 geteilte Immich-Assets | `asset_refs`: Löschen/Ersetzen nur, wenn kein anderer Job verweist. |
| R-10b … R-10h Absturz zwischen zwei Schritten | offene `intent`-Zeilen werden beim Start gegen die Wirklichkeit abgeglichen (`reconciled`). |
| R-11 Watchdog startet laufende Jobs neu | Heartbeat im Modul-Lauf; neu eingereiht wird nur ohne Heartbeat. |
| R-12, R-13 versteckte Steuerwerte, Identität per Name | Steuerwerte werden Laufparameter; CSV-Retry nur bei eindeutigem Treffer, sonst Rückfrage. |
| R-15 Route folgt der Asset-ID | Ziel kommt aus dem Set, nicht aus `immich_asset_id`. |
| R-16, R-17 Takeout-JSON, Sidecars | JSON wird nicht mehr gelöscht, sondern nach dem Schreiben (GPS/Datum in IA-07) in den Papierkorb; vorhandene Sidecars werden vor dem Überschreiben gesichert. |
| R-18, R-19, R-22 Immich-Semantik | gegen Immich **3.1.0** prüfen (Dev läuft noch 2.6.3 → zuerst aktualisieren, Issue #48). |
| R-20 verlorene Merge-Daten | Merges sind eigene Läufe mit `copied_from_run_id`. |
| R-21 keine Spur | Protokoll + Papierkorb. |
| R-23 Ordner-Aufräumen | Prüfung über echte Enthaltensein-Beziehung. |

## Oberfläche

- **Dashboard „Letzte Verarbeitungen" und Verarbeitungs-Log:** jede Zeile
  aufklappbar. Zugeklappt elf kleine Blöcke (grün ok, blau läuft, gelb Warnung,
  rot Fehler, grau übersprungen, gestrichelt veraltet). Aufgeklappt pro Modul die
  Läufe mit Dauer und Ergebnis, darunter die Dateioperationen als Zeitstrahl
  („verschoben nach …, Hash geprüft", „in Papierkorb", …).
- **Job-Detail:** dieselben Blöcke, Verlauf aller Versuche, Exemplare der Datei
  (wo liegt sie, geprüft wann), Knöpfe „Modul neu ausführen" und „Wiederherstellen".
- **Mehrfachauswahl** im Verarbeitungs-Log: „Modul neu ausführen …" für alle
  markierten Jobs; dasselbe pro Set in den Einstellungen.
- **Bestandsprüfung:** Seite unter Werkzeuge + Hinweis im Statusmenü bei
  Abweichungen (Exemplar fehlt, Hash weicht ab, Datei ohne Job in `reprocess/`).
- **Papierkorb:** Liste unter Werkzeuge mit Wiederherstellen und Restzeit.

## Migration

Wie beim Sets-Umbau einmalig beim Start, in einer Transaktion, geprüft auf einer
Kopie der Live-DB (66 000 Jobs → rund 700 000 Modul-Läufe):

1. Jeder Eintrag in `step_result` wird ein Lauf mit `trigger='migrated'`
   (Verlauf früherer Läufe lässt sich nicht zurückholen — er wurde überschrieben).
   Steuerwerte (`_force_reupload` live 20×, `_retry_previous_immich_tags` 107×)
   werden geprüft und verworfen bzw. als Parameter übernommen.
2. `file_copies` wird aus `target_path` und `immich_asset_id` aufgebaut, Zustand
   „ungeprüft"; die erste Bestandsprüfung prüft sie im Hintergrund.
   `original_path` wird **nicht** übernommen, wenn dort nicht der eigene Hash liegt.
3. `asset_refs` aus allen Jobs mit Immich-Verweis (live 3 840 geteilte Assets).
4. `reprocess/` (live 18 362 Verweise): die Bestandsprüfung ordnet jede Datei
   einem Job zu oder meldet sie als herrenlos — gelöscht wird nichts automatisch.

## Reihenfolge auf dem Branch

| # | Schritt | fertig, wenn |
|---|---|---|
| 0 | **Vorarbeit:** Dev-Immich auf 3.1.0 (#48); Prüfregel „jede eingelesene Datei hat danach mindestens ein geprüftes Exemplar" in alle E2E-Stories; Reproducer R-01 … R-23 | Reproducer rot, Regel aktiv |
| 1 | `fileops.py` + `file_ops` + `file_copies` + `asset_refs`; alle 71 Operationen umgestellt | grep-Test: keine direkten Aufrufe mehr; R-01…R-06, R-08, R-10 grün |
| 2 | Papierkorb lokal + Immich, Leerung, Wiederherstellen | Löschen ist überall umkehrbar |
| 3 | `module_runs` + Migration, `step_result` als abgeleitete Sicht | Migration auf Live-Kopie grün, alle bisherigen Tests grün |
| 4 | Oberfläche: Blöcke, Verlauf, Zeitstrahl, Bestandsprüfung, Papierkorb | Screenshots hell/dunkel/Handy |
| 5 | Module einzeln ausführen, Kaskade „veraltet" | neue E2E-Stories grün |
| 6 | Ausführung nach Abhängigkeiten, Betriebsmittel-Semaphore | R-11 grün, Durchsatz nicht schlechter |
| 7 | Altlasten weg (direkte `step_result`-Schreiber, Steuerwerte), Doku, 3.0.0 | Release-Gate komplett grün |

Schritte 1 und 2 bringen den grössten Sicherheitsgewinn und kommen deshalb zuerst.

## Neue E2E-Stories (Auswahl)

- **Fremde Datei am alten Pfad** (R-01): erledigter Immich-Job, neue gleichnamige
  Datei in der Inbox, Retry → das alte Asset bleibt, die neue Datei wird ein eigener Job.
- **Download-Kollision** (R-03): zwei Jobs, gleicher Dateiname, beide aus Immich →
  beide Arbeitskopien vorhanden.
- **Behalten mit verschwundenem Original** (R-04): die behaltene Datei fehlt →
  nichts wird gelöscht, klare Meldung.
- **Geteiltes Asset** (R-08): zwei Jobs, ein Asset, Review-Löschen eines Jobs →
  Asset bleibt.
- **Absturz mitten im Verschieben** (R-10): Prozess nach der Absicht abbrechen,
  Neustart → Zustand wird abgeglichen, nichts verloren.
- **Papierkorb:** Löschen → Wiederherstellen → Datei und Asset wieder da.
- **Modul einzeln:** Geocoding neu → IA-07/IA-08 laufen nach, Tags in Immich aktualisiert.

## Offene Entscheide

1. **Ort des lokalen Papierkorbs:** im Basis-Pfad des Sets (`.papierkorb/`,
   gleicher Datenträger, im Dateimanager sichtbar — Vorschlag) oder zentral in
   `/app/data/papierkorb` (kann auf einem anderen, kleineren Datenträger liegen).
2. **Dev-Immich auf 3.1.0 aktualisieren** — nötig, weil Papierkorb- und
   Kopier-Verhalten auf der Live-Version geprüft werden müssen. Dev enthält nur
   Testdaten.
3. **Alte Dateien in `reprocess/`** (live 18 362 Verweise): nach der ersten
   Bestandsprüfung eine Liste zum Aufräumen vorlegen — nie automatisch löschen.

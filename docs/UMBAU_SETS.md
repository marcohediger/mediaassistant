# Umbau „Dateieingabe und Verarbeitung" — Verarbeitungs-Sets

Branch: `umbau/dateieingabe-verarbeitung` · Auslieferung: ein Release am Ende (2.33.0)

## Ziel

Beliebig viele **Sets**. Jedes Set bündelt, was heute global eingestellt ist:

| Bereich | heute | im Set |
|---|---|---|
| Eingangsverzeichnisse | Liste, je Verzeichnis eigene Schalter | ein oder mehrere Verzeichnisse pro Set |
| Filewatcher (Zeitplan) | global: Modus, Intervall, Fenster, Tage | pro Set |
| Sortier-Regeln | eine globale Liste | eigene Liste pro Set |
| Ordner-Tags | pro Verzeichnis + globales Modul | pro Set (Modul bleibt Hauptschalter) |
| Immich Auto-Tagging | global an/aus, ein Cursor | pro Set an/aus, eigener Benutzer, eigener Cursor |
| Ziel-Ablage | global: Basis-Pfad + Kategorien mit Pfad-Vorlagen | pro Set: Ziel (Library oder Immich), Basis-Pfad, Kategorien |

**Global bleiben:** KI-Backends, Geocoding, OCR, Duplikat-Erkennung, Video-Vorschauen,
Metadaten-Schreibmodus, Immich-Server und -Zugänge, SMTP, Darstellung, Diagnose.

**Nach dem Update ändert sich nichts.** Die heutige Konfiguration wird automatisch zu
einem Set „Standard", das sich exakt so verhält wie bisher.

## Befunde, die der Umbau mit erledigt

1. **Fünf Wege in die Pipeline, nur einer hält die Slot-Grenze.** Immich-Poller,
   Neustart-Wiederaufnahme, Retry-Knopf und Duplikat-Review starten `run_pipeline`
   direkt. Der Poller verarbeitet sogar Bild für Bild *innerhalb* der Scan-Schleife —
   solange er läuft, wird keine Inbox gescannt (KNOWN_RISKS H-2). Mit mehreren Sets,
   die Immich pollen, würde das schlimmer.
2. **Immich-Download beim Pollen nach `/tmp`.** Ein Neustart verliert die Datei, der
   Job läuft auf eine fehlende Quelle.
3. **Liegengebliebene Dateien werden bei jedem Durchlauf neu gehasht** (übersprungene,
   Dry-Run) — standardmässig alle paar Sekunden, auch bei grossen Videos.
4. **`filewatcher.py` (987 Zeilen)** vereint Scanner, Poller, CSV-Retry, Zeitplan,
   Wiederaufnahme, Migrationen und Worker (KNOWN_RISKS A-6).

## Zielarchitektur

```
 Set A ─┬─ Verzeichnisse ──┐
        └─ Immich-Poll  ───┤                      ┌──────────────┐
 Set B ─── Verzeichnisse ──┼──► reihen NUR ein ──►│  jobs (queued)│──► EIN Worker ──► Pipeline
 CSV-Retry ────────────────┤                      └──────────────┘     (Slots, Pause,
 UI: Retry / Duplikat ─────┘                                            Hänger-Erkennung)
```

- **Eingänge reihen nur ein**, sie verarbeiten nie selbst.
- **Ein Worker** ist der einzige Ort, der `run_pipeline` startet.
- **Immich-Download erst im Worker**: der Job trägt die Asset-ID, die Datei wird beim
  Start der Verarbeitung geholt. Ein Neustart lädt einfach erneut.
- **Neustart-Wiederaufnahme reiht nur neu ein**, statt vor dem Worker einzeln abzuarbeiten.
- **Hash-Cache** pro Datei (Pfad, Grösse, Änderungszeit): neu gehasht wird nur, was sich geändert hat.

### Module statt `filewatcher.py`

| Datei | Inhalt |
|---|---|
| `inputs/inbox.py` | Verzeichnisse eines Sets scannen, einreihen |
| `inputs/immich.py` | Immich für ein Set pollen, einreihen (ohne Download) |
| `inputs/csv_retry.py` | CSV-Bulk-Retry |
| `inputs/schedule.py` | Zeitplan eines Sets auswerten |
| `worker.py` | der eine Pipeline-Worker |
| `recovery.py` | Neustart-Wiederaufnahme, Einmal-Migrationen |
| `filewatcher.py` | schlanker Taktgeber: geht über die aktiven Sets, ruft fällige Eingänge auf |

## Datenmodell

Neue Tabelle `processing_sets`:

| Feld | Bedeutung |
|---|---|
| `name`, `description`, `active`, `position` | Stammdaten |
| `schedule_mode`, `interval_s`, `window_start`, `window_end`, `scheduled_days`, `scheduled_time` | Zeitplan |
| `folder_tags` | Ordner-Tags an/aus |
| `dry_run` | Probelauf |
| `target_mode` | `library` oder `immich` |
| `library_base_path` | Basis der Ablage (auch für Fehler-/Duplikat-/Review-Ordner) |
| `immich_user_id` | Zugang für den Upload (leer = globaler Schlüssel) |
| `immich_poll`, `immich_poll_user_id`, `immich_last_poll` | Immich Auto-Tagging |

Neue Spalte `set_id` in `inbox_directories`, `sorting_rules`, `library_categories`, `jobs`.
Kategorie-Schlüssel sind künftig pro Set eindeutig (`set_id`, `key`).

Jobs behalten ihre Momentaufnahme (`folder_tags`, `dry_run`, `use_immich`,
`immich_user_id`) — ein Retry verhält sich wie beim ersten Lauf, auch wenn das Set
inzwischen geändert wurde. Regeln, Kategorien und Basis-Pfad liest die Pipeline über `set_id`.

## Migration

Einmalig, mit Flag `migration.sets_v1`, das **nur bei Erfolg** gesetzt wird:

1. Bestehende Verzeichnisse nach ihren Schaltern gruppieren. Eine Gruppe → ein Set
   (bei nur einer Gruppe heisst es „Standard"). Live: ein Verzeichnis → ein Set.
2. Zeitplan aus `filewatcher.*`, Basis-Pfad aus `library.base_path`.
3. Regeln und Kategorien dem Set zuordnen (bei mehreren Sets: kopieren).
4. `immich.poll_enabled` + `immich.last_poll` → ins Set „Standard".
5. Alle bestehenden Jobs bekommen ihr Set (über `source_inbox_path`, Immich-Jobs ins Poll-Set).

Geprüft wird gegen eine **Kopie der Live-DB** (`dataLiveSystem/db/`): nach der Migration
muss jede Datei dieselbe Kategorie und denselben Zielpfad ergeben wie vorher.

## Oberfläche

**In den Einstellungen** ein Abschnitt „Verarbeitungs-Sets" mit einer Liste —
**eine Zeile pro Set**, ganz rechts ein **Stift** zum Bearbeiten im Popup:

```
 ●  Name          Eingänge                 Zeitplan        Ziel                  Regeln  Ordner-Tags  Warteschlange   ✎
 ●  Standard      /inbox · Immich-Poll     durchgehend     Immich (global)         10       aus            3        ✎
 ○  Scanner       /inbox/scans             22:00–06:00     /library/scans           4       an             0        ✎
                                                                                              [ + Neues Set ]
```

- ● aktiv / ○ inaktiv. Ein Klick auf den Stift öffnet das Set in einem grossen Popup.
- Popup mit Reitern: Allgemein · Eingänge · Zeitplan · Sortier-Regeln · Ordner-Tags ·
  Ziel-Ablage. Unten: Speichern, Abbrechen, Duplizieren, Löschen. „Duplizieren"
  kopiert Regeln und Kategorien mit.
- Die bisherigen Einstellungs-Abschnitte Eingangsverzeichnisse, Filewatcher,
  Sortier-Regeln, Ziel-Ablage und Ordner-Tags gehen in diesem Popup auf; beim
  Immich-Abschnitt wandert nur der Poll-Schalter ins Set.
### Immich-Zugänge als Liste

Der Immich-Abschnitt der Einstellungen behält die **Server-URL**; Schlüssel und
Benutzer werden zu **einer Liste nach demselben Muster** — eine Zeile pro Zugang,
ganz rechts der Stift, Bearbeiten im Popup:

```
 Name            Immich-Konto              Verbindung   verwendet von     ✎
 Globaler User   Marco (m…@…)              ✓            Standard          ✎
 Melissa         Melissa (m…@…)            ✓            Handy Melissa     ✎
                                                              [ + Neuer Zugang ]
```

- **„+ Neuer Zugang"** fragt zuerst: **Globaler User** oder **User mit Name**.
  Globaler User gibt es höchstens einmal; existiert er schon, ist die Wahl gesperrt.
- In der Namensspalte steht „Globaler User" bzw. der vergebene Name.
- „Immich-Konto" zeigt, wem der Schlüssel in Immich gehört (`/api/users/me`) —
  damit ist sichtbar, ob der richtige Schlüssel hinterlegt ist.
- Im Popup: Name (nur bei benannten), API-Schlüssel, Verbindung testen, Löschen.
  Löschen ist gesperrt, solange ein Set den Zugang verwendet.
- Die Sets wählen ihren Zugang (für Upload und für Auto-Tagging) aus dieser Liste.

**Speicherung bleibt wie heute:** der globale Schlüssel in `config.immich.api_key`,
benannte in `immich_users`; „kein Benutzer" (`immich_user_id = NULL`) heisst global.
Die Liste ist eine neue Ansicht, kein neues Datenmodell — Jobs und Immich-Aufrufe
bleiben unberührt.

- Dashboard und Diagnose-API zeigen die Sets mit Warteschlange je Set.
- Review-Ansicht bietet die Kategorien des Sets an, aus dem der Job stammt.

## Reihenfolge auf dem Branch

1. Datenmodell + Migration, Test gegen Live-DB-Kopie
2. Pipeline liest das Set (IA-05, IA-08, Review, `file_operations`, Werkzeuge)
3. Eingänge neu (`inputs/`), Zeitplan pro Set, Immich ohne Download, Hash-Cache
4. Ein Worker: alle Einstiege reihen nur ein, Wiederaufnahme reiht nur ein
5. Oberfläche: Set-Liste in den Einstellungen + Popup-Editor, Dashboard, Diagnose
6. i18n, Doku, CHANGELOG, Version 2.33.0
7. Volle Testrunde: Startpfad, E2E, alle Suiten, neue Stories — dann Merge und Release

## Neue E2E-Stories

| Story | Ablauf |
|---|---|
| US-11 Zwei Sets, gleiche Datei-Art | Set A und B mit verschiedenen Regeln → Datei landet je Set am richtigen Ziel |
| US-12 Immich blockiert Inbox nicht | Poll-Set mit vielen Assets, gleichzeitig Datei in Inbox → Inbox-Datei wird nicht erst nach dem Poll verarbeitet |
| US-13 Neustart während Immich-Job | Job unterbrechen → nach Neustart neu geladen und fertig verarbeitet |
| US-14 Migration | Live-DB-Kopie migrieren → Set „Standard" reproduziert Kategorie und Ziel jeder Datei |

## Annahmen (zur Bestätigung)

- **Kategorien gehören zum Set**, weil Regeln auf Kategorien zeigen und die Ziel-Ablage Teil des Sets ist.
- **Duplikate werden über alle Sets hinweg erkannt**, wie heute.
- **Ein Verzeichnis gehört genau einem Set** — sonst würde eine Datei doppelt verarbeitet.
- Neue Basis-Pfade müssen im Container eingehängt sein; der Editor prüft, ob der Pfad existiert.

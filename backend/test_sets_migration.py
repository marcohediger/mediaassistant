"""Sets migration against copies of real databases.

Runs the startup sequence (init_db, env import, migrate_to_sets) on a COPY of
the given database and checks that the global configuration ended up in the
sets exactly, that every directory, rule, category and job has its set, that
the category table lost its global UNIQUE(key), and that a second start
changes nothing. Write mode and the Google JSON switch are checked on both
ways into the sets: migrate_to_sets copies the global values into every set,
and database._migrate_columns copies them once into a database that was
migrated before the two columns existed (rebuilt here without them). A
source that is already migrated is the reference itself: the start must
leave its sets, rules, categories, directories and job assignment alone.

    docker exec mediaassistant-dev python /app/test_sets_migration.py            # dev DB
    docker exec mediaassistant-dev python /app/test_sets_migration.py /pfad.db  # any DB
    docker exec mediaassistant-dev python /app/test_sets_migration.py neu       # fresh install

The source is only read. The copy lives in a temporary directory that is
removed at the end.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

SOURCE = sys.argv[1] if len(sys.argv) > 1 else "/app/data/mediaassistant.db"
FRESH = SOURCE == "neu"
# "mehrfach:<db>" builds the case neither the dev nor the live database has:
# two directories with different switches plus a named Immich access that is
# polled. Expected: one set per directory group and one for the access.
MULTI = SOURCE.startswith("mehrfach:")
if MULTI:
    SOURCE = SOURCE.split(":", 1)[1]
WORKDIR = tempfile.mkdtemp(prefix="ma_sets_migration_")
COPY = os.path.join(WORKDIR, "mediaassistant.db")

if not FRESH:
    # Through SQLite, so pages still sitting in the WAL are part of the copy.
    # A WAL database on a read-only mount cannot be opened read-only (SQLite
    # needs to create its -shm file). Without a -wal file next to it every
    # page is in the main file, so opening it as immutable loses nothing.
    _dst = sqlite3.connect(COPY)
    try:
        _src = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
        _src.backup(_dst)
    except sqlite3.OperationalError:
        if os.path.exists(SOURCE + "-wal"):
            raise
        _src = sqlite3.connect(f"file:{SOURCE}?immutable=1", uri=True)
        _src.backup(_dst)
    _dst.close()
    _src.close()

# database.py binds its engine at import time.
os.environ["DATABASE_PATH"] = COPY

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def _cfg(con, key, default):
    row = con.execute("SELECT value, encrypted FROM config WHERE key = ?", (key,)).fetchone()
    if not row or row[0] is None or row[1]:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def _make_multi() -> None:
    con = sqlite3.connect(COPY)
    con.execute("INSERT INTO inbox_directories (path, label, folder_tags, dry_run, use_immich, "
                "immich_user_id, active) VALUES ('/inbox_scans', 'Scanner', 0, 1, 0, NULL, 1)")
    if os.environ.get("MULTI_FIRST_DRY"):
        con.execute("UPDATE inbox_directories SET dry_run = 1 WHERE id = (SELECT MIN(id) FROM inbox_directories)")
    cur = con.execute("INSERT INTO immich_users (label, api_key, active) VALUES ('Testzugang', 'x', 1)")
    uid = cur.lastrowid
    for key, value in (("immich.poll_enabled", True), ("immich.last_poll", "2026-01-01T00:00:00+00:00")):
        con.execute("INSERT OR REPLACE INTO config (key, value, encrypted) VALUES (?, ?, 0)",
                    (key, json.dumps(value)))
    con.execute("INSERT INTO jobs (filename, original_path, debug_key, status, source_label, "
                "source_inbox_path) VALUES ('scan.jpg', '/inbox_scans/scan.jpg', 'MA-TEST-SCAN', "
                "'done', 'Scanner', '/inbox_scans')")
    con.execute("INSERT INTO jobs (filename, original_path, debug_key, status, source_label, "
                "immich_user_id) VALUES ('handy.jpg', '/tmp/ma_immich_x/handy.jpg', 'MA-TEST-USER', "
                "'done', 'Immich', ?)", (uid,))
    con.commit()
    con.close()


def _write_mode(value) -> str:
    """The global write mode as the sets take it over: anything unknown is direct."""
    return value if value in ("direct", "sidecar") else "direct"


def _meta_before(con) -> dict:
    """Write mode and Google JSON switch: global config, and the sets' own
    values if the source already has sets with these columns."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    cols = {r[1] for r in con.execute("PRAGMA table_info(processing_sets)")} \
        if "processing_sets" in tables else set()
    has_cols = {"metadata_write_mode", "google_json"} <= cols
    return {
        "already_migrated": _cfg(con, "migration.sets_v1", False) is True,
        "write_mode_cfg": _write_mode(_cfg(con, "metadata.write_mode", "direct")),
        "google_json_cfg": bool(_cfg(con, "metadata.google_json", False)),
        "set_meta": {r[0]: (r[1], bool(r[2])) for r in con.execute(
            "SELECT id, metadata_write_mode, google_json FROM processing_sets")} if has_cols else None,
    }


def _sets_snapshot(con, set_cols: list[str] | None = None) -> dict:
    """Sets, rules, categories, directories and job assignment of a database
    that is already migrated — a start has to leave all of it as it is."""
    if set_cols is None:
        set_cols = [r[1] for r in con.execute("PRAGMA table_info(processing_sets)")
                    if r[1] != "updated_at"]
    rows = lambda sql: [tuple(r) for r in con.execute(sql).fetchall()]  # noqa: E731
    return {
        "set_cols": set_cols,
        "sets": rows(f"SELECT {', '.join(set_cols)} FROM processing_sets ORDER BY id"),
        "rules": rows("SELECT id, set_id, position, condition, value, target_category, media_type, "
                      "active FROM sorting_rules ORDER BY id"),
        "categories": rows("SELECT id, set_id, key, label, path_template, fixed, immich_archive, "
                           "position FROM library_categories ORDER BY id"),
        "directories": rows("SELECT id, path, label, set_id, active FROM inbox_directories ORDER BY id"),
        "job_sets": rows("SELECT set_id, COUNT(*) FROM jobs GROUP BY set_id ORDER BY set_id"),
    }


def _snapshot_before() -> dict:
    """Everything the migration is supposed to carry over, read from the copy."""
    if FRESH:
        return {"fresh": True, "meta": {"already_migrated": False, "write_mode_cfg": "direct",
                                        "google_json_cfg": False, "set_meta": None}}
    if MULTI:
        _make_multi()
    con = sqlite3.connect(COPY)
    meta = _meta_before(con)
    if meta["already_migrated"]:
        # The migration must not run again. What the database holds now may
        # have been edited since (sets, rules, directories) — it is the
        # reference, not the old global configuration.
        snap = {"fresh": False, "meta": meta, "already": _sets_snapshot(con)}
        con.close()
        return snap
    module = lambda n: bool((con.execute(  # noqa: E731
        "SELECT enabled FROM modules WHERE name = ?", (n,)).fetchone() or [0])[0])
    snap = {
        "fresh": False,
        "meta": meta,
        "inboxes": con.execute(
            "SELECT id, path, label, folder_tags, dry_run, use_immich, immich_user_id "
            "FROM inbox_directories ORDER BY id").fetchall(),
        "folder_module": module("ordner_tags"),
        "ocr_module": module("ocr"),
        "schedule": {
            "schedule_mode": _cfg(con, "filewatcher.schedule_mode", "continuous"),
            "interval_s": int(_cfg(con, "filewatcher.interval", 5) or 5),
            "window_start": _cfg(con, "filewatcher.window_start", "22:00"),
            "window_end": _cfg(con, "filewatcher.window_end", "06:00"),
            "scheduled_days": _cfg(con, "filewatcher.scheduled_days", "0,1,2,3,4"),
            "scheduled_time": _cfg(con, "filewatcher.scheduled_time", "23:00"),
        },
        "base_path": _cfg(con, "library.base_path", "/library") or "/library",
        "ocr_mode": _cfg(con, "ocr.mode", "smart") or "smart",
        "poll": bool(_cfg(con, "immich.poll_enabled", False)),
        "last_poll": _cfg(con, "immich.last_poll", None),
        "poll_users": [r[0] for r in con.execute(
            "SELECT id FROM immich_users WHERE active = 1 ORDER BY id")],
        "rules": con.execute(
            "SELECT position, condition, value, target_category, media_type, active "
            "FROM sorting_rules ORDER BY id").fetchall(),
        "categories": con.execute(
            "SELECT key, label, path_template, fixed, immich_archive, position "
            "FROM library_categories ORDER BY id").fetchall(),
        "jobs": con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
    }
    con.close()
    return snap


def _unique_indexes(con) -> list[list[str]]:
    out = []
    for idx in con.execute("PRAGMA index_list(library_categories)"):
        if idx[2]:
            out.append([c[2] for c in con.execute(f"PRAGMA index_info('{idx[1]}')")])
    return out


async def _startup():
    from database import init_db, seed_inbox_from_env
    from config import config_manager
    from sets import migrate_to_sets
    await init_db()
    await config_manager.seed_from_env()
    await seed_inbox_from_env()
    await migrate_to_sets()


def _state(con) -> tuple:
    return (
        con.execute("SELECT COUNT(*) FROM processing_sets").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM sorting_rules").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM library_categories").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM jobs WHERE set_id IS NULL").fetchone()[0],
        con.execute("SELECT GROUP_CONCAT(set_id) FROM (SELECT set_id FROM jobs ORDER BY id)").fetchone()[0],
    )


def _meta_state(con) -> tuple:
    return tuple(tuple(r) for r in con.execute(
        "SELECT id, metadata_write_mode, google_json FROM processing_sets ORDER BY id").fetchall())


def _set_cfg(con, key: str, raw: str | None) -> None:
    """Write a config value exactly as stored (JSON text); None removes it."""
    if raw is None:
        con.execute("DELETE FROM config WHERE key = ?", (key,))
    else:
        con.execute("INSERT OR REPLACE INTO config (key, value, encrypted) VALUES (?, ?, 0)", (key, raw))


def _drop_meta_columns(con) -> None:
    """Rebuild processing_sets as it was before the two columns existed."""
    cols = [r for r in con.execute("PRAGMA table_info(processing_sets)")
            if r[1] not in ("metadata_write_mode", "google_json")]
    defs = []
    for _cid, name, typ, notnull, default, pk in cols:
        d = f"{name} {typ}"
        if pk:
            d += " PRIMARY KEY"
        elif notnull:
            d += " NOT NULL"
        if default is not None:
            d += f" DEFAULT {default}"
        defs.append(d)
    names = ", ".join(c[1] for c in cols)
    con.execute(f"CREATE TABLE processing_sets_pre ({', '.join(defs)})")
    con.execute(f"INSERT INTO processing_sets_pre ({names}) SELECT {names} FROM processing_sets")
    con.execute("DROP TABLE processing_sets")
    con.execute("ALTER TABLE processing_sets_pre RENAME TO processing_sets")
    con.commit()


async def _check_meta_backfill() -> None:
    """A database migrated to sets before write mode and Google JSON moved
    into them: the next start adds both columns and copies the former global
    values into every set — once."""
    from database import init_db

    # (stored metadata.write_mode, stored metadata.google_json, expected per set)
    cases = [
        ('"sidecar"', "true", ("sidecar", True)),
        ('"direct"', "false", ("direct", False)),
        ('"quatsch"', "true", ("direct", True)),
        (None, None, ("direct", False)),
    ]
    for raw_mode, raw_json, want in cases:
        con = sqlite3.connect(COPY)
        _drop_meta_columns(con)
        _set_cfg(con, "metadata.write_mode", raw_mode)
        _set_cfg(con, "metadata.google_json", raw_json)
        con.commit()
        con.close()
        await init_db()
        con = sqlite3.connect(COPY)
        got = {r[0]: (r[1], bool(r[2])) for r in con.execute(
            "SELECT name, metadata_write_mode, google_json FROM processing_sets")}
        con.close()
        report(f"Spalten nachgerüstet, Konfiguration {raw_mode}/{raw_json}: jedes Set {want}",
               bool(got) and all(v == want for v in got.values()), f"{got}")

    # Only once: a value changed in the set afterwards survives the next start,
    # although the global config says otherwise.
    con = sqlite3.connect(COPY)
    first_id = con.execute("SELECT id FROM processing_sets ORDER BY position, id LIMIT 1").fetchone()[0]
    con.execute("UPDATE processing_sets SET metadata_write_mode = 'sidecar', google_json = 1 "
                "WHERE id = ?", (first_id,))
    _set_cfg(con, "metadata.write_mode", '"direct"')
    _set_cfg(con, "metadata.google_json", "false")
    con.commit()
    con.close()
    await init_db()
    con = sqlite3.connect(COPY)
    row = con.execute("SELECT metadata_write_mode, google_json FROM processing_sets WHERE id = ?",
                      (first_id,)).fetchone()
    con.close()
    report("Nachrüstung nur einmal: späterer Wert des Sets bleibt beim nächsten Start",
           row is not None and (row[0], bool(row[1])) == ("sidecar", True), f"{row}")


async def main() -> int:
    print(f"\nQuelle: {'Neuinstallation' if FRESH else SOURCE}")
    before = _snapshot_before()
    await _startup()

    con = sqlite3.connect(COPY)
    con.row_factory = sqlite3.Row
    sets = con.execute("SELECT * FROM processing_sets ORDER BY position, id").fetchall()
    flag = _cfg(con, "migration.sets_v1", False)
    report("Erledigt-Flag gesetzt", flag is True)
    report("Kategorie-Schlüssel nur noch pro Set eindeutig",
           ["key"] not in _unique_indexes(con) and ["set_id", "key"] in _unique_indexes(con),
           f"eindeutige Indizes: {_unique_indexes(con)}")

    if before["fresh"]:
        report("Neuinstallation: genau ein Set „Standard\"",
               len(sets) == 1 and sets[0]["name"] == "Standard", f"{[s['name'] for s in sets]}")
        sid = sets[0]["id"] if sets else None
        n_cat = con.execute("SELECT COUNT(*) FROM library_categories WHERE set_id = ?", (sid,)).fetchone()[0]
        n_rule = con.execute("SELECT COUNT(*) FROM sorting_rules WHERE set_id = ?", (sid,)).fetchone()[0]
        report("Neuinstallation: Standard-Kategorien und -Regeln", n_cat == 8 and n_rule == 8,
               f"{n_cat} Kategorien, {n_rule} Regeln")
        inbox = os.environ.get("INBOX_PATH", "").strip()
        if inbox:
            row = con.execute("SELECT set_id FROM inbox_directories WHERE path = ?", (inbox,)).fetchone()
            report("Neuinstallation: INBOX_PATH gehört zum Set", bool(row) and row[0] == sid, inbox)
    elif "already" in before:
        ref = before["already"]
        now = _sets_snapshot(con, ref["set_cols"])
        labels = {"sets": "Sets", "rules": "Regeln", "categories": "Kategorien",
                  "directories": "Verzeichnisse", "job_sets": "Job-Zuordnung zu Sets"}
        for part, label in labels.items():
            report(f"Bereits migriert: {label} unverändert", now[part] == ref[part],
                   f"{len(now[part])} Zeilen, vorher {len(ref[part])}")
    else:
        groups: dict[tuple, list] = {}
        for i in before["inboxes"]:
            key = (bool(i[3]) and before["folder_module"], bool(i[4]), bool(i[5]), i[6])
            groups.setdefault(key, []).append(i)
        first_dry = bool(before["inboxes"]) and bool(before["inboxes"][0][4])
        expected = max(len(groups), 1) + (len(before["poll_users"]) if before["poll"] else 0) \
            + (1 if before["poll"] and first_dry else 0)
        report("Anzahl Sets", len(sets) == expected, f"{len(sets)} erwartet {expected}: "
               f"{[s['name'] for s in sets]}")
        first = sets[0]

        for s in sets:
            sched = {k: s[k] for k in before["schedule"]}
            report(f"„{s['name']}\": Zeitplan übernommen", sched == before["schedule"], f"{sched}")
            report(f"„{s['name']}\": Basis-Pfad und OCR übernommen",
                   s["library_base_path"] == before["base_path"]
                   and bool(s["ocr_enabled"]) == before["ocr_module"]
                   and s["ocr_mode"] == before["ocr_mode"],
                   f"{s['library_base_path']}, OCR {'an' if s['ocr_enabled'] else 'aus'} / {s['ocr_mode']}")

        for i in before["inboxes"]:
            row = con.execute("SELECT set_id FROM inbox_directories WHERE id = ?", (i[0],)).fetchone()
            s = next((x for x in sets if x["id"] == row[0]), None)
            ok = s is not None and (
                bool(s["folder_tags"]) == (bool(i[3]) and before["folder_module"])
                and bool(s["dry_run"]) == bool(i[4])
                and s["target_mode"] == ("immich" if i[5] else "library")
                and s["immich_user_id"] == i[6])
            report(f"Verzeichnis {i[1]} → Set mit gleichem Verhalten", ok,
                   f"Set „{s['name'] if s else '?'}\": Ordner-Tags {s['folder_tags'] if s else '?'}, "
                   f"Ziel {s['target_mode'] if s else '?'}")
            n_wrong = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE source_inbox_path = ? AND set_id IS NOT ?",
                (i[1], row[0])).fetchone()[0]
            report(f"Jobs aus {i[1]} im Set des Verzeichnisses", n_wrong == 0, f"{n_wrong} abweichend")

        global_poll = next((s for s in sets if s["immich_poll"] and s["immich_poll_user_id"] is None), None)
        report("Immich-Poll wie vorher",
               (global_poll is not None) == before["poll"]
               and (not before["poll"] or (global_poll["immich_last_poll"] == before["last_poll"]
                                           and not global_poll["dry_run"])),
               f"Poll {'an im Set ' + global_poll['name'] if global_poll else 'aus'}")
        report("Immich-Eingang = Polling nach der Migration",
               all(bool(s["immich_input"]) == bool(s["immich_poll"]) for s in sets),
               f"{[(s['name'], s['immich_input'], s['immich_poll']) for s in sets]}")
        polled = sorted(s["immich_poll_user_id"] for s in sets
                        if s["immich_poll"] and s["immich_poll_user_id"] is not None)
        report("Jeder benannte Immich-Zugang wird weiter gepollt",
               polled == (sorted(before["poll_users"]) if before["poll"] else []), f"{polled}")

        n_null = con.execute("SELECT COUNT(*) FROM jobs WHERE set_id IS NULL").fetchone()[0]
        n_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        report("Kein Job ohne Set", n_null == 0 and n_jobs == before["jobs"],
               f"{n_jobs} Jobs, {n_null} ohne Set")
        n_immich_wrong = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE source_label = 'Immich' AND immich_user_id IS NULL "
            "AND set_id IS NOT ?", ((global_poll or first)["id"],)).fetchone()[0]
        report("Immich-Jobs im Poll-Set", not before["poll"] or n_immich_wrong == 0,
               f"{n_immich_wrong} abweichend")
        for s in sets:
            uid = s["immich_poll_user_id"]
            if not s["immich_poll"] or uid is None:
                continue
            n_wrong = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE source_label = 'Immich' AND immich_user_id = ? "
                "AND set_id IS NOT ?", (uid, s["id"])).fetchone()[0]
            report(f"Immich-Jobs des Zugangs {uid} im Set „{s['name']}\"",
                   n_wrong == 0 and s["target_mode"] == "immich" and s["immich_user_id"] == uid
                   and s["immich_last_poll"] == before["last_poll"],
                   f"{n_wrong} abweichend, Cursor {s['immich_last_poll']}")

        for s in sets:
            rules = con.execute(
                "SELECT position, condition, value, target_category, media_type, active "
                "FROM sorting_rules WHERE set_id = ? ORDER BY id", (s["id"],)).fetchall()
            cats = con.execute(
                "SELECT key, label, path_template, fixed, immich_archive, position "
                "FROM library_categories WHERE set_id = ? ORDER BY id", (s["id"],)).fetchall()
            report(f"„{s['name']}\": alle Regeln, unverändert",
                   [tuple(r) for r in rules] == [tuple(r) for r in before["rules"]],
                   f"{len(rules)} von {len(before['rules'])}")
            report(f"„{s['name']}\": alle Kategorien, unverändert",
                   [tuple(c) for c in cats] == [tuple(c) for c in before["categories"]],
                   f"{len(cats)} von {len(before['categories'])}")
        n_orphan = con.execute(
            "SELECT (SELECT COUNT(*) FROM sorting_rules WHERE set_id IS NULL) + "
            "(SELECT COUNT(*) FROM library_categories WHERE set_id IS NULL) + "
            "(SELECT COUNT(*) FROM inbox_directories WHERE set_id IS NULL)").fetchone()[0]
        report("Keine Regel, Kategorie oder Verzeichnis ohne Set", n_orphan == 0, f"{n_orphan}")

    # Write mode and Google JSON switch. A database migrated to sets just now
    # takes the global values into every set. One migrated before the two
    # columns existed got them from database._migrate_columns, which copies
    # the global values once — and one that already had them keeps the sets'
    # own values, whatever the global config says.
    meta = before["meta"]
    if before["fresh"]:
        # A fresh install has only what seed_from_env wrote (METADATA_* env)
        meta = {**meta, "write_mode_cfg": _write_mode(_cfg(con, "metadata.write_mode", "direct")),
                "google_json_cfg": bool(_cfg(con, "metadata.google_json", False))}
    for s in sets:
        got = (s["metadata_write_mode"], bool(s["google_json"]))
        if meta["already_migrated"] and meta["set_meta"] is not None and s["id"] in meta["set_meta"]:
            want, why = meta["set_meta"][s["id"]], "unverändert"
        else:
            want, why = (meta["write_mode_cfg"], meta["google_json_cfg"]), "aus globaler Konfiguration"
        report(f"„{s['name']}\": Schreibmodus und Google-JSON {why}", got == want,
               f"{got}, erwartet {want}")

    # Second start: nothing may change
    state_1 = _state(con) + (_meta_state(con),)
    con.close()
    await _startup()
    con = sqlite3.connect(COPY)
    state_2 = _state(con) + (_meta_state(con),)
    report("Zweiter Start ändert nichts", state_2 == state_1,
           f"Sets/Regeln/Kategorien vorher {state_1[:3]}, nachher {state_2[:3]}; "
           f"Schreibmodus/Google-JSON vorher {state_1[-1]}, nachher {state_2[-1]}")
    con.close()

    await _check_meta_backfill()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(WORKDIR, ignore_errors=True)
    sys.exit(code)

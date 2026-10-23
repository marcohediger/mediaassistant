"""R-26: jobs.id must never be handed out twice — nor the ids of the register.

Without AUTOINCREMENT SQLite gives the id of the newest deleted job to the
next new job, which then inherits the deleted job's register, reference and
ledger rows. database._jobs_autoincrement rebuilds the table once. The same
holds for file_copies (the ledger points at copy ids), asset_refs and
module_runs (copied_from_run_id, file_ops.run_id):
database._register_autoincrement rebuilds them through the same helper.

    docker exec mediaassistant-dev python /app/test_jobs_autoincrement.py [live-copy.db]

Scenario 1 builds a legacy database (old schema, ledger rows with a higher
job id, copy id and run id than any row). Scenario 2 runs on a copy of a real
database if one is given (the copy is changed, the original never); register
tables it does not have are created as at startup and checked as well.
"""
import asyncio
import os
import sqlite3
import sys
import time

import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

from database import _jobs_autoincrement, _register_autoincrement
from models import Base

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f" — {detail}" if detail else ""))


LEGACY_JOBS = """
CREATE TABLE jobs (
	id INTEGER NOT NULL,
	filename TEXT NOT NULL,
	original_path TEXT NOT NULL,
	debug_key TEXT,
	status TEXT,
	created_at DATETIME, set_id INTEGER,
	PRIMARY KEY (id),
	UNIQUE (debug_key)
)"""


async def migrate(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            await _jobs_autoincrement(conn)
            await _register_autoincrement(conn)
    finally:
        await engine.dispose()


def schema_checks(path, label, rows_before, ids_before, indexes_before, high):
    con = sqlite3.connect(path)
    try:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
        report(f"{label}: jobs hat AUTOINCREMENT", "AUTOINCREMENT" in sql.upper())
        report(f"{label}: alle Zeilen erhalten", con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == rows_before)
        report(f"{label}: alle IDs unverändert",
               con.execute("SELECT COALESCE(SUM(id), 0) FROM jobs").fetchone()[0] == ids_before)
        indexes = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'")}
        report(f"{label}: alle Indizes wieder da", indexes_before <= indexes, f"{len(indexes)} Indizes")
        seq = con.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()[0]
        report(f"{label}: Zähler über der höchsten je benutzten ID", seq >= high, f"seq={seq}, höchste={high}")

        # The actual bug: delete the newest job, insert a new one.
        newest = con.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
        con.execute("DELETE FROM jobs WHERE id = ?", (newest,))
        cur = con.execute("INSERT INTO jobs (filename, original_path, debug_key, status) "
                          "VALUES ('__r26.jpg', '/x/__r26.jpg', ?, 'queued')", (f"R26-{time.time()}",))
        report(f"{label}: neuer Job bekommt nie die ID eines gelöschten",
               cur.lastrowid > max(newest or 0, high), f"neu={cur.lastrowid}, gelöscht={newest}, höchste={high}")
        con.rollback()
    finally:
        con.close()


LEGACY_REGISTER = """
CREATE TABLE file_copies (
	id INTEGER NOT NULL, 
	job_id INTEGER, 
	kind TEXT NOT NULL, 
	location TEXT NOT NULL, 
	state TEXT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX uq_file_copies_present ON file_copies (location) WHERE state = 'present';
CREATE INDEX idx_file_copies_job ON file_copies (job_id);
CREATE TABLE asset_refs (
	id INTEGER NOT NULL, 
	kind TEXT NOT NULL, 
	location TEXT NOT NULL, 
	job_id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_asset_refs UNIQUE (kind, location, job_id)
);
CREATE INDEX idx_asset_refs_job ON asset_refs (job_id);
CREATE TABLE module_runs (
	id INTEGER NOT NULL, 
	job_id INTEGER NOT NULL, 
	module TEXT NOT NULL, 
	status TEXT NOT NULL, 
	copied_from_run_id INTEGER, 
	PRIMARY KEY (id)
);
CREATE INDEX idx_module_runs_job ON module_runs (job_id, module, id);
CREATE TRIGGER trg_module_runs_no_delete BEFORE DELETE ON module_runs
BEGIN SELECT RAISE(ABORT, 'module_runs rows are kept'); END;
"""

# (table, insert of a new row, whether a row may be deleted)
REGISTER = [
    ("file_copies", "INSERT INTO file_copies (job_id, kind, location, state) "
                    "VALUES (1, 'local', '/x/__r26-new.jpg', 'present')", True),
    ("asset_refs", "INSERT INTO asset_refs (kind, location, job_id) VALUES ('immich', '__r26-new', 1)", True),
    ("module_runs", "INSERT INTO module_runs (job_id, module, status) VALUES (1, 'IA-02', 'ok')", False),
]
# The real module_runs (created by the model) has more required columns.
FULL_INSERT = {
    "module_runs": "INSERT INTO module_runs (job_id, module, attempt, \"trigger\", status) "
                   "VALUES (1, 'IA-02', 1, 'pipeline', 'ok')",
}


def snapshot(path, table):
    con = sqlite3.connect(path)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return None
        rows, ids = con.execute(f"SELECT COUNT(*), COALESCE(SUM(id), 0) FROM {table}").fetchone()
        companions = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger') AND tbl_name=? AND sql IS NOT NULL",
            (table,))}
        return rows, ids, companions
    finally:
        con.close()


def register_checks(path, label, table, before, high, insert_sql, deletable):
    con = sqlite3.connect(path)
    try:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        report(f"{label}: {table} hat AUTOINCREMENT", "AUTOINCREMENT" in sql.upper())
        rows, ids, companions = before
        now = snapshot(path, table)
        report(f"{label}: {table} alle Zeilen und IDs erhalten", now[0] == rows and now[1] == ids,
               f"{now[0]} Zeilen")
        report(f"{label}: {table} Indizes und Trigger wieder da", companions <= now[2],
               ", ".join(sorted(now[2])) or "keine")
        seq = (con.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone() or [0])[0]
        report(f"{label}: {table} Zähler über der höchsten je benutzten ID", seq >= high,
               f"seq={seq}, höchste={high}")
        newest = con.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
        if deletable:
            con.execute(f"DELETE FROM {table} WHERE id = ?", (newest,))
        else:
            try:
                con.execute(f"DELETE FROM {table} WHERE id = ?", (newest,))
                refused = False
            except sqlite3.DatabaseError:
                refused = True
            report(f"{label}: {table} Löschschutz (Trigger) erhalten", refused or not newest,
                   "leer" if not newest else "")
        columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        cur = con.execute(FULL_INSERT[table] if table in FULL_INSERT and "attempt" in columns else insert_sql)
        report(f"{label}: {table} neue Zeile bekommt nie eine schon benutzte ID",
               cur.lastrowid > max(newest, high), f"neu={cur.lastrowid}, bisher höchste={max(newest, high)}")
        con.rollback()
    finally:
        con.close()


async def scenario_legacy(work):
    print("\n── Alt-Datenbank (altes Schema, Protokoll mit höherer Job-ID) ──")
    path = os.path.join(work, "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(LEGACY_JOBS + """;
        CREATE INDEX idx_job_status ON jobs(status);
        CREATE TABLE file_ops (id INTEGER PRIMARY KEY, job_id INTEGER, run_id INTEGER,
                               copy_id INTEGER, dst_copy_id INTEGER);
    """ + LEGACY_REGISTER)
    con.executemany("INSERT INTO jobs (id, filename, original_path, debug_key, status) VALUES (?, ?, ?, ?, 'done')",
                    [(i, f"f{i}.jpg", f"/inbox/f{i}.jpg", f"MA-2026-{i:04d}") for i in range(1, 51)])
    # Job 60 existed once, was deleted, and left ledger rows behind.
    con.execute("INSERT INTO file_ops (job_id) VALUES (60)")
    con.executemany("INSERT INTO file_copies (id, job_id, kind, location, state) VALUES (?, ?, 'local', ?, 'present')",
                    [(i, 55 if i == 1 else i, f"/library/f{i}.jpg") for i in range(1, 21)])
    con.executemany("INSERT INTO asset_refs (id, kind, location, job_id) VALUES (?, 'immich', ?, ?)",
                    [(i, f"asset-{i}", i) for i in range(1, 6)])
    con.executemany("INSERT INTO module_runs (id, job_id, module, status, copied_from_run_id) VALUES (?, ?, 'IA-01', 'ok', ?)",
                    [(i, i, 14 if i == 3 else None) for i in range(1, 11)])
    # Copies 21..30 and runs 11..14 existed once and were deleted; the ledger
    # (and a copied run) still name them.
    con.execute("INSERT INTO file_ops (job_id, copy_id, dst_copy_id, run_id) VALUES (2, 25, 30, 12)")
    con.commit()
    con.close()
    before = {table: snapshot(path, table) for table, _, _ in REGISTER}

    await migrate(path)
    schema_checks(path, "Alt-DB", 50, sum(range(1, 51)), {"idx_job_status"}, 60)
    for (table, insert_sql, deletable), high in zip(REGISTER, (30, 5, 14)):
        register_checks(path, "Alt-DB", table, before[table], high, insert_sql, deletable)

    await migrate(path)  # second start: nothing to do
    con = sqlite3.connect(path)
    report("Alt-DB: zweiter Start ändert nichts", con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 50
           and con.execute("SELECT COUNT(*) FROM file_copies").fetchone()[0] == 20
           and con.execute("SELECT COUNT(*) FROM module_runs").fetchone()[0] == 10)
    con.close()


async def scenario_copy(src, work):
    print(f"\n── Kopie einer echten Datenbank ({os.path.basename(src)}) ──")
    path = os.path.join(work, "copy.db")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(path)
    s.backup(d)  # includes the WAL
    s.close()
    rows, ids = d.execute("SELECT COUNT(*), COALESCE(SUM(id), 0) FROM jobs").fetchone()
    indexes = {r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'")}
    tables = {r[0] for r in d.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    high = d.execute("SELECT COALESCE(MAX(id), 0) FROM jobs").fetchone()[0]
    for table in ("file_ops", "file_copies", "asset_refs"):
        if table in tables:
            high = max(high, d.execute(f"SELECT COALESCE(MAX(job_id), 0) FROM {table}").fetchone()[0])
    d.close()

    before = {table: snapshot(path, table) for table, _, _ in REGISTER}
    started = time.monotonic()
    await migrate(path)
    report("Kopie: Migration durchgelaufen", True, f"{rows} Jobs in {time.monotonic() - started:.1f} s")
    schema_checks(path, "Kopie", rows, ids, indexes, high)

    # Register tables the copy does not have yet are created at startup (create_all)
    missing = [table for table, snap in before.items() if snap is None]
    if missing:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()
        print(f"  (neu angelegt wie beim Start: {', '.join(missing)})")
    con = sqlite3.connect(path)
    highs = {}
    for table, column_pairs in (("file_copies", [("file_ops", "copy_id"), ("file_ops", "dst_copy_id")]),
                                ("asset_refs", []),
                                ("module_runs", [("module_runs", "copied_from_run_id"), ("file_ops", "run_id")])):
        high_t = con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
        for other, column in column_pairs:
            high_t = max(high_t, con.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {other}").fetchone()[0])
        highs[table] = high_t
    con.close()
    for table, insert_sql, deletable in REGISTER:
        snap = before[table] or snapshot(path, table)
        register_checks(path, "Kopie", table, snap, highs[table], insert_sql, deletable)


async def main():
    work = f"/app/data/__r26_{int(time.time())}"
    os.makedirs(work)
    try:
        await scenario_legacy(work)
        if len(sys.argv) > 1:
            await scenario_copy(sys.argv[1], work)
    finally:
        for name in os.listdir(work):
            os.remove(os.path.join(work, name))
        os.rmdir(work)
    print(f"\n{sum(RESULTS)} bestanden, {len(RESULTS) - sum(RESULTS)} fehlgeschlagen")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())

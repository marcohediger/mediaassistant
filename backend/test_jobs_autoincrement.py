"""R-26: jobs.id must never be handed out twice.

Without AUTOINCREMENT SQLite gives the id of the newest deleted job to the
next new job, which then inherits the deleted job's register, reference and
ledger rows. database._jobs_autoincrement rebuilds the table once.

    docker exec mediaassistant-dev python /app/test_jobs_autoincrement.py [live-copy.db]

Scenario 1 builds a legacy database (old schema, ledger rows with a higher
job id than any job). Scenario 2 runs on a copy of a real database if one is
given (the copy is changed, the original never).
"""
import asyncio
import os
import sqlite3
import sys
import time

import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

from database import _jobs_autoincrement

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


async def scenario_legacy(work):
    print("\n── Alt-Datenbank (altes Schema, Protokoll mit höherer Job-ID) ──")
    path = os.path.join(work, "legacy.db")
    con = sqlite3.connect(path)
    con.executescript(LEGACY_JOBS + """;
        CREATE INDEX idx_job_status ON jobs(status);
        CREATE TABLE file_ops (id INTEGER PRIMARY KEY, job_id INTEGER);
        CREATE TABLE file_copies (id INTEGER PRIMARY KEY, job_id INTEGER);
    """)
    con.executemany("INSERT INTO jobs (id, filename, original_path, debug_key, status) VALUES (?, ?, ?, ?, 'done')",
                    [(i, f"f{i}.jpg", f"/inbox/f{i}.jpg", f"MA-2026-{i:04d}") for i in range(1, 51)])
    # Job 60 existed once, was deleted, and left ledger rows behind.
    con.execute("INSERT INTO file_ops (job_id) VALUES (60)")
    con.execute("INSERT INTO file_copies (job_id) VALUES (55)")
    con.commit()
    con.close()

    await migrate(path)
    schema_checks(path, "Alt-DB", 50, sum(range(1, 51)), {"idx_job_status"}, 60)

    await migrate(path)  # second start: nothing to do
    con = sqlite3.connect(path)
    report("Alt-DB: zweiter Start ändert nichts", con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 50)
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

    started = time.monotonic()
    await migrate(path)
    report("Kopie: Migration durchgelaufen", True, f"{rows} Jobs in {time.monotonic() - started:.1f} s")
    schema_checks(path, "Kopie", rows, ids, indexes, high)


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

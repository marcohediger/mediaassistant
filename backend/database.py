import logging
import os
import re
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from models import (Base, Module, InboxDirectory, ModuleRun, FILE_OPS_GUARDS, MODULE_CODES, MODULE_RUNS_GUARDS,
                    RUN_RESULT_STATUSES)

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024

engine = create_async_engine(
    f"sqlite+aiosqlite:///{DATABASE_PATH}",
    echo=False,
    connect_args={"timeout": 600},
    # Pool tuning for high-concurrency scenarios (bulk retries, parallel
    # pipeline workers, dashboard polling). Default was 5/10 = max 15
    # connections, which got exhausted in v2.28.7 when retry-all spawned
    # ~33 parallel tasks. With 20/40 we have headroom for ~60 concurrent
    # connections — comfortably above the worker pool size + bulk-retry
    # background task + dashboard JSON polling.
    pool_size=20,
    max_overflow=40,
    pool_timeout=60,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@sqlalchemy.event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite pragmas on every new connection for concurrency."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=600000")
    # The WAL keeps the size of the largest transaction it ever held while a
    # connection is open (the pool never closes them): cut it back to 64 MiB
    # whenever a checkpoint resets it.
    cursor.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    cursor.close()

DEFAULT_MODULES = [
    ("ki_analyse", False),
    ("ki_analyse_2", False),
    ("geocoding", False),
    ("duplikat_erkennung", False),
    ("smtp", False),
    ("filewatcher", False),
    ("immich", False),
]


async def _migrate_columns(conn):
    """Add missing columns to existing tables (lightweight migration)."""
    import sqlalchemy
    migrations = [
        ("jobs", "source_inbox_path", "ALTER TABLE jobs ADD COLUMN source_inbox_path TEXT"),
        ("jobs", "dry_run", "ALTER TABLE jobs ADD COLUMN dry_run BOOLEAN DEFAULT 0"),
        ("jobs", "use_immich", "ALTER TABLE jobs ADD COLUMN use_immich BOOLEAN DEFAULT 0"),
        ("inbox_directories", "use_immich", "ALTER TABLE inbox_directories ADD COLUMN use_immich BOOLEAN DEFAULT 0"),
        ("jobs", "immich_asset_id", "ALTER TABLE jobs ADD COLUMN immich_asset_id TEXT"),
        ("library_categories", "immich_archive", "ALTER TABLE library_categories ADD COLUMN immich_archive BOOLEAN DEFAULT 0"),
        ("sorting_rules", "media_type", "ALTER TABLE sorting_rules ADD COLUMN media_type TEXT"),
        ("jobs", "retry_count", "ALTER TABLE jobs ADD COLUMN retry_count INTEGER DEFAULT 0"),
        ("jobs", "immich_user_id", "ALTER TABLE jobs ADD COLUMN immich_user_id INTEGER"),
        ("inbox_directories", "immich_user_id", "ALTER TABLE inbox_directories ADD COLUMN immich_user_id INTEGER"),
        ("jobs", "folder_tags", "ALTER TABLE jobs ADD COLUMN folder_tags BOOLEAN DEFAULT 0"),
        ("jobs", "started_at", "ALTER TABLE jobs ADD COLUMN started_at DATETIME"),
        ("jobs", "set_id", "ALTER TABLE jobs ADD COLUMN set_id INTEGER"),
        ("inbox_directories", "set_id", "ALTER TABLE inbox_directories ADD COLUMN set_id INTEGER"),
        ("sorting_rules", "set_id", "ALTER TABLE sorting_rules ADD COLUMN set_id INTEGER"),
        ("library_categories", "set_id", "ALTER TABLE library_categories ADD COLUMN set_id INTEGER"),
        # Write mode and Google Takeout JSON moved into the sets after the
        # sets themselves existed; a database migrated in between takes the
        # former global values over once (see below).
        ("processing_sets", "metadata_write_mode",
         "ALTER TABLE processing_sets ADD COLUMN metadata_write_mode TEXT DEFAULT 'direct'"),
        ("processing_sets", "google_json",
         "ALTER TABLE processing_sets ADD COLUMN google_json BOOLEAN DEFAULT 0"),
        ("processing_sets", "immich_input",
         "ALTER TABLE processing_sets ADD COLUMN immich_input BOOLEAN DEFAULT 0"),
        # The folder-tag switch split into a tag mode and albums (folder_path.py).
        ("processing_sets", "folder_tag_mode",
         "ALTER TABLE processing_sets ADD COLUMN folder_tag_mode TEXT DEFAULT 'none'"),
        ("processing_sets", "folder_albums",
         "ALTER TABLE processing_sets ADD COLUMN folder_albums BOOLEAN DEFAULT 0"),
    ]
    added = set()
    for table, column, sql in migrations:
        try:
            await conn.execute(sqlalchemy.text(f"SELECT {column} FROM {table} LIMIT 1"))
        except Exception:
            await conn.execute(sqlalchemy.text(sql))
            added.add((table, column))

    # Config values are stored JSON-encoded; json_extract decodes them.
    if ("processing_sets", "metadata_write_mode") in added:
        await conn.execute(sqlalchemy.text(
            "UPDATE processing_sets SET metadata_write_mode = COALESCE("
            "(SELECT json_extract(value, '$') FROM config WHERE key = 'metadata.write_mode' "
            "AND json_extract(value, '$') IN ('direct', 'sidecar')), 'direct')"))
    # Before immich_input existed, switching the Immich input off cleared
    # immich_poll but kept the chosen access — so a set with an access still
    # set had an input that was merely off.
    if ("processing_sets", "immich_input") in added:
        await conn.execute(sqlalchemy.text(
            "UPDATE processing_sets SET immich_input = CASE WHEN immich_poll = 1 "
            "OR immich_poll_user_id IS NOT NULL THEN 1 ELSE 0 END"))
    # The old switch made word keywords and one album per folder. Its sets
    # keep exactly that: tag mode "legacy" (folder_path.legacy_tags) and
    # albums named the old way, so existing albums are reused.
    if ("processing_sets", "folder_tag_mode") in added:
        await conn.execute(sqlalchemy.text(
            "UPDATE processing_sets SET folder_tag_mode = CASE WHEN folder_tags = 1 THEN 'legacy' ELSE 'none' END"))
    if ("processing_sets", "folder_albums") in added:
        await conn.execute(sqlalchemy.text(
            "UPDATE processing_sets SET folder_albums = CASE WHEN folder_tags = 1 THEN 1 ELSE 0 END"))
    if ("processing_sets", "google_json") in added:
        await conn.execute(sqlalchemy.text(
            "UPDATE processing_sets SET google_json = COALESCE("
            "(SELECT CASE WHEN json_extract(value, '$') = 1 THEN 1 ELSE 0 END "
            "FROM config WHERE key = 'metadata.google_json'), 0)"))

    # Performance indexes for large databases (150k+ jobs)
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_job_status ON jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_job_file_hash ON jobs(file_hash)",
        "CREATE INDEX IF NOT EXISTS idx_job_phash ON jobs(phash)",
        "CREATE INDEX IF NOT EXISTS idx_job_original_path ON jobs(original_path)",
        "CREATE INDEX IF NOT EXISTS idx_job_created_at ON jobs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_job_updated_at ON jobs(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_syslog_created_at ON system_logs(created_at)",
        # Composite indexes for common query patterns (150k+ jobs)
        "CREATE INDEX IF NOT EXISTS idx_job_hash_status ON jobs(file_hash, status)",
        "CREATE INDEX IF NOT EXISTS idx_job_phash_status ON jobs(phash, status)",
        "CREATE INDEX IF NOT EXISTS idx_job_status_created ON jobs(status, created_at)",
        # Speeds up the cleanup-stale-errors / cleanup-stuck-duplicate-winners
        # endpoints — both look up jobs by filename in batches.
        "CREATE INDEX IF NOT EXISTS idx_job_filename ON jobs(filename)",
        "CREATE INDEX IF NOT EXISTS idx_job_set_status ON jobs(set_id, status)",
        # Rerun requests (models.Rerun / RerunItem); create_all makes them
        # with a new table, these cover a table made before an index existed.
        "CREATE INDEX IF NOT EXISTS idx_reruns_state ON reruns(state, id)",
        "CREATE INDEX IF NOT EXISTS idx_rerun_items_rerun ON rerun_items(rerun_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_rerun_items_job ON rerun_items(job_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rerun_items_open_job ON rerun_items(job_id) "
        "WHERE state IN ('pending', 'preparing', 'queued', 'running')",
        # Trash reason of a copy (job_view): copy_id OR dst_copy_id.
        "CREATE INDEX IF NOT EXISTS idx_file_ops_dst_copy ON file_ops(dst_copy_id)",
        # No query filters module runs by status alone.
        "DROP INDEX IF EXISTS idx_module_runs_status",
        # The file-operation ledger is append-only (see models.FileOp).
        *FILE_OPS_GUARDS,
    ]
    for sql in indexes:
        await conn.execute(sqlalchemy.text(sql))
    # idx_module_runs_job was (job_id, module, id); on (job_id) alone it is
    # half the size, and the rowid it carries still orders by id.
    job_index = [r[2] for r in (await conn.execute(sqlalchemy.text(
        "PRAGMA index_info('idx_module_runs_job')"))).all()]
    if job_index and job_index != ["job_id"]:
        await conn.execute(sqlalchemy.text("DROP INDEX idx_module_runs_job"))
        await conn.execute(sqlalchemy.text("CREATE INDEX idx_module_runs_job ON module_runs (job_id)"))

    await _rebuild_library_categories(conn)
    await _jobs_autoincrement(conn)
    await _register_autoincrement(conn)
    await _migrate_module_runs(conn)


async def _jobs_autoincrement(conn):
    """Give jobs.id AUTOINCREMENT, once (R-26).

    Without it SQLite hands the id of the newest deleted job to the next new
    job, which then inherits the deleted job's register, reference and ledger
    rows -- it could "own" copies of another file. The sequence starts above
    the highest job id any table has seen, so an id already reused by the
    ledger cannot come back either.
    """
    await _ensure_autoincrement(conn, "jobs", referencing=[
        ("file_ops", "job_id"), ("file_copies", "job_id"), ("asset_refs", "job_id"), ("module_runs", "job_id")])


async def _register_autoincrement(conn):
    """The same for the register, the references and the module runs, once.

    The ledger points at register rows (file_ops.copy_id / dst_copy_id) and
    module runs at each other (copied_from_run_id) and are pointed at by
    the ledger (file_ops.run_id). A reused id would make a ledger row speak
    about another file's copy — e.g. the trash reason job_view shows.
    Production never deletes these rows; test cleanups did.
    """
    await _ensure_autoincrement(conn, "file_copies", referencing=[("file_ops", "copy_id"), ("file_ops", "dst_copy_id")])
    await _ensure_autoincrement(conn, "asset_refs")
    await _ensure_autoincrement(conn, "module_runs", referencing=[
        ("module_runs", "copied_from_run_id"), ("file_ops", "run_id")])
    # Rerun requests and their items are referenced by job values (_rerun)
    # and by each other: never hand an id out twice either.
    await _ensure_autoincrement(conn, "reruns", referencing=[("rerun_items", "rerun_id")])
    await _ensure_autoincrement(conn, "rerun_items")


async def _ensure_autoincrement(conn, table: str, referencing=()):
    """Give `table`.id AUTOINCREMENT, once.

    SQLite cannot add AUTOINCREMENT in place, so the table is rebuilt with
    every row, id, index and trigger kept, and the row count is checked. The
    sequence starts above the highest id ever seen: the table's own and
    every id the (table, column) pairs in `referencing` point at (tables
    that do not exist yet are skipped). A missing table, or one that has
    AUTOINCREMENT already, is left alone.
    """
    t = sqlalchemy.text
    sql = (await conn.execute(t(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table})).scalar()
    if not sql or "AUTOINCREMENT" in sql.upper():
        return
    new_sql = re.sub(r"\bid INTEGER NOT NULL,", "id INTEGER PRIMARY KEY AUTOINCREMENT,", sql, count=1)
    new_sql = re.sub(r",\s*PRIMARY KEY \(id\)", "", new_sql, count=1)
    renamed = re.sub(rf'^CREATE TABLE\s+"?{re.escape(table)}"?(?=\s*\()', f"CREATE TABLE {table}_new", new_sql, count=1)
    if new_sql == sql or "PRIMARY KEY (id)" in new_sql or "AUTOINCREMENT" not in new_sql or renamed == new_sql:
        raise RuntimeError(f"{table}: unexpected schema, AUTOINCREMENT migration aborted")

    companions = [r[0] for r in (await conn.execute(t(
        "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger') AND tbl_name=:name AND sql IS NOT NULL "
        "ORDER BY type"), {"name": table})).all()]
    columns = ", ".join(f'"{r[1]}"' for r in (await conn.execute(t(f'PRAGMA table_info("{table}")'))).all())
    tables = {r[0] for r in (await conn.execute(t("SELECT name FROM sqlite_master WHERE type='table'"))).all()}
    high = (await conn.execute(t(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"'))).scalar()
    for other, column in referencing:
        if other in tables:
            high = max(high, (await conn.execute(t(f'SELECT COALESCE(MAX("{column}"), 0) FROM "{other}"'))).scalar())
    count = (await conn.execute(t(f'SELECT COUNT(*) FROM "{table}"'))).scalar()
    log = logging.getLogger("mediaassistant.database")
    # The DB log table is not usable inside this startup transaction.
    log.warning("Umbau der Tabelle %s (IDs nie wiederverwenden, R-26): %d Zeilen, einmalig", table, count)

    await conn.execute(t(renamed))
    await conn.execute(t(f'INSERT INTO {table}_new ({columns}) SELECT {columns} FROM "{table}"'))
    copied = (await conn.execute(t(f"SELECT COUNT(*) FROM {table}_new"))).scalar()
    if copied != count:
        raise RuntimeError(f"{table}: copied {copied} of {count} rows, AUTOINCREMENT migration aborted")
    await conn.execute(t(f'DROP TABLE "{table}"'))
    await conn.execute(t(f'ALTER TABLE {table}_new RENAME TO "{table}"'))
    for companion in companions:
        await conn.execute(t(companion))
    await conn.execute(t("DELETE FROM sqlite_sequence WHERE name = :name"), {"name": table})
    await conn.execute(t("INSERT INTO sqlite_sequence (name, seq) VALUES (:name, :seq)"), {"name": table, "seq": high})
    log.warning("Umbau der Tabelle %s abgeschlossen: %d Zeilen, nächste ID über %d", table, copied, high)


async def _migrate_module_runs(conn) -> dict | None:
    """Take the module results in jobs.step_result over into module_runs,
    once (rebuild step 3, docs/UMBAU_MODULE.md; runs.py writes from then on).

    Every entry IA-01 … IA-11 becomes a finished run, trigger 'migrated',
    attempt 1 (earlier runs were overwritten and cannot be recovered):
    status from the result, finished at the job's completed_at or
    updated_at. The other keys become job values (jobs.job_data): run
    parameters ("_…") only while the job's run is not over (queued,
    processing) — in a finished job they are leftovers and are dropped, as
    run_pipeline drops them at the end of a run (R-12) — and the duplicate
    plan and merge record always. step_result is rewritten only for the
    jobs that lost a key; for every other job it already is the view.

    Runs when jobs.job_data is added, inside the startup transaction: one
    INSERT … SELECT over json_each, then the few jobs with other keys. The
    guards are (re)created on every start. Returns counts, None if done before.
    """
    import json
    import time
    t = sqlalchemy.text
    for sql in MODULE_RUNS_GUARDS:
        await conn.execute(t(sql))
    columns = {r[1] for r in (await conn.execute(t("PRAGMA table_info(jobs)"))).all()}
    if "job_data" in columns:
        return None
    log = logging.getLogger("mediaassistant.database")
    started = time.monotonic()
    await conn.execute(t("ALTER TABLE jobs ADD COLUMN job_data JSON"))
    modules = ", ".join(f"'{m}'" for m in MODULE_CODES)
    statuses = ", ".join(f"'{s}'" for s in RUN_RESULT_STATUSES)
    is_object = "j.step_result IS NOT NULL AND json_valid(j.step_result) AND json_type(j.step_result) = 'object'"
    jobs = (await conn.execute(t(f"SELECT COUNT(*) FROM jobs j WHERE {is_object} AND j.step_result <> '{{}}'"))).scalar()
    # The DB log table is not usable inside this startup transaction.
    log.warning("Übernahme der Modul-Ergebnisse in module_runs (einmalig): %d Jobs mit step_result", jobs)

    # Same rules as runs.result_status / runs.result_error.
    status = (f"CASE WHEN e.type = 'object' AND json_extract(e.value, '$.status') IN ({statuses}) "
              "THEN json_extract(e.value, '$.status') "
              "WHEN e.type = 'object' AND json_extract(e.value, '$._skipped') THEN 'skipped' ELSE 'ok' END")
    # Into an empty table the indexes are built once afterwards (packed
    # pages, about 5 MB less on the live data). With runs already there
    # they stay: the NOT EXISTS below would scan the table for every row.
    bulk = (await conn.execute(t("SELECT 1 FROM module_runs LIMIT 1"))).first() is None
    if bulk:
        for index in ModuleRun.__table__.indexes:
            await conn.execute(t(f'DROP INDEX IF EXISTS "{index.name}"'))
    inserted = (await conn.execute(t(f"""
        INSERT INTO module_runs (job_id, module, attempt, "trigger", status, result, error, finished_at)
        SELECT j.id, e.key, 1, 'migrated', {status},
               CASE e.type WHEN 'object' THEN e.value WHEN 'array' THEN e.value WHEN 'null' THEN 'null'
                    WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote(e.value) END,
               CASE WHEN e.type = 'object' AND json_extract(e.value, '$.status') IN ('error', 'warning')
                    THEN NULLIF(COALESCE(json_extract(e.value, '$.reason'), '')
                                || CASE WHEN json_extract(e.value, '$.reason') IS NOT NULL
                                         AND json_extract(e.value, '$.traceback') IS NOT NULL
                                        THEN char(10) || char(10) ELSE '' END
                                || COALESCE(json_extract(e.value, '$.traceback'), ''), '') END,
               COALESCE(j.completed_at, j.updated_at)
        FROM jobs j, json_each(j.step_result) e
        WHERE {is_object} AND e.key IN ({modules})
          AND NOT EXISTS (SELECT 1 FROM module_runs r WHERE r.job_id = j.id AND r.module = e.key)
        ORDER BY j.id"""))).rowcount
    if bulk:
        for index in ModuleRun.__table__.indexes:
            await conn.run_sync(index.create)

    kept = dropped = other = 0
    rows = (await conn.execute(t(
        f"SELECT j.id, j.status, j.step_result FROM jobs j WHERE {is_object} AND EXISTS "
        f"(SELECT 1 FROM json_each(j.step_result) e WHERE e.key NOT IN ({modules}))"))).all()
    for job_id, job_status, raw in rows:
        step_result = json.loads(raw)
        values = {}
        for key, value in step_result.items():
            if key in MODULE_CODES:
                continue
            if key.startswith("_") and job_status not in ("queued", "processing"):
                dropped += 1
                continue
            values[key] = value
            if key.startswith("_"):
                kept += 1
            else:
                other += 1
        view = {k: v for k, v in step_result.items() if k in MODULE_CODES or k in values}
        await conn.execute(t("UPDATE jobs SET job_data = :data, step_result = :view WHERE id = :id"),
                           {"data": json.dumps(values), "view": json.dumps(view), "id": job_id})
    counts = {"jobs": jobs, "runs": inserted, "params_kept": kept, "params_dropped": dropped,
              "values": other, "seconds": round(time.monotonic() - started, 1)}
    log.warning("Übernahme in module_runs abgeschlossen: %d Läufe aus %d Jobs; Laufparameter %d übernommen, "
                "%d verworfen (Job beendet); %d weitere Job-Werte; %.1f s",
                inserted, jobs, kept, dropped, other, counts["seconds"])
    return counts


async def _rebuild_library_categories(conn):
    """Drop the global UNIQUE on library_categories.key, once.

    Older databases made a category key unique across the whole table. With
    sets, every set carries its own categories, so the same key exists once
    per set. SQLite cannot drop a constraint in place; the table is rebuilt
    with UNIQUE(set_id, key) instead, keeping every row and its id.
    """
    t = sqlalchemy.text
    needs_rebuild = False
    for idx in (await conn.execute(t("PRAGMA index_list(library_categories)"))).all():
        # (seq, name, unique, origin, partial)
        if not idx[2]:
            continue
        cols = [c[2] for c in (await conn.execute(t(f"PRAGMA index_info('{idx[1]}')"))).all()]
        if cols == ["key"]:
            needs_rebuild = True
    if not needs_rebuild:
        return
    await conn.execute(t("""
        CREATE TABLE library_categories_new (
            id INTEGER NOT NULL PRIMARY KEY,
            set_id INTEGER,
            key TEXT NOT NULL,
            label TEXT NOT NULL,
            path_template TEXT NOT NULL,
            fixed BOOLEAN,
            immich_archive BOOLEAN DEFAULT 0,
            position INTEGER,
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT uq_category_set_key UNIQUE (set_id, key)
        )"""))
    await conn.execute(t("""
        INSERT INTO library_categories_new
            (id, set_id, key, label, path_template, fixed, immich_archive, position, created_at, updated_at)
        SELECT id, set_id, key, label, path_template, fixed, immich_archive, position, created_at, updated_at
        FROM library_categories"""))
    await conn.execute(t("DROP TABLE library_categories"))
    await conn.execute(t("ALTER TABLE library_categories_new RENAME TO library_categories"))


async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    async with engine.begin() as conn:
        # Enable WAL mode for better concurrent read/write performance
        await conn.execute(sqlalchemy.text("PRAGMA journal_mode=WAL"))
        await conn.execute(sqlalchemy.text("PRAGMA busy_timeout=600000"))
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)

    # Seed default modules
    async with async_session() as session:
        for name, enabled in DEFAULT_MODULES:
            existing = await session.get(Module, name)
            if not existing:
                session.add(Module(name=name, enabled=enabled))
        await session.commit()


async def truncate_wal(eng=None) -> tuple[int, int]:
    """Give the WAL's disk space back, once after the startup migrations.

    A one-time migration writes its whole transaction into the WAL (on the
    live data about 550 MB, the sets migration another 250 MB). The pool
    keeps its connections open, so the file would keep that size until the
    app stops. Returns the WAL size in bytes before and after.
    """
    eng = eng or engine
    wal = f"{eng.url.database}-wal"
    before = os.path.getsize(wal) if os.path.exists(wal) else 0
    async with eng.connect() as conn:
        busy, frames, done = (await conn.execute(sqlalchemy.text("PRAGMA wal_checkpoint(TRUNCATE)"))).one()
    after = os.path.getsize(wal) if os.path.exists(wal) else 0
    logging.getLogger("mediaassistant.database").info(
        "WAL checkpoint after startup: %.1f MB -> %.1f MB (busy=%d, frames=%d, checkpointed=%d)",
        before / 1e6, after / 1e6, busy, frames, done)
    return before, after


async def seed_inbox_from_env():
    """Create a default inbox directory from ENV if the table is empty."""
    inbox_path = os.environ.get("INBOX_PATH", "").strip()
    if not inbox_path:
        return

    async with async_session() as session:
        from sqlalchemy import select, func
        count = (await session.execute(select(func.count(InboxDirectory.id)))).scalar()
        if count > 0:
            return
        label = os.environ.get("INBOX_LABEL", "Default Inbox").strip()
        session.add(InboxDirectory(path=inbox_path, label=label, active=True))
        await session.commit()

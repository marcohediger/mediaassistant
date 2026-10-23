import os
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from models import Base, Module, InboxDirectory, FILE_OPS_GUARDS

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")

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
        # The file-operation ledger is append-only (see models.FileOp).
        *FILE_OPS_GUARDS,
    ]
    for sql in indexes:
        await conn.execute(sqlalchemy.text(sql))

    await _rebuild_library_categories(conn)


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

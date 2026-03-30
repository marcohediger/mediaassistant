import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from models import Base, Module

DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")

engine = create_async_engine(f"sqlite+aiosqlite:///{DATABASE_PATH}", echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

DEFAULT_MODULES = [
    ("ki_analyse", False),
    ("geocoding", False),
    ("duplikat_erkennung", False),
    ("ocr", False),
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
    ]
    for table, column, sql in migrations:
        try:
            await conn.execute(sqlalchemy.text(f"SELECT {column} FROM {table} LIMIT 1"))
        except Exception:
            await conn.execute(sqlalchemy.text(sql))

    # Performance indexes for large databases (150k+ jobs)
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_job_status ON jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_job_file_hash ON jobs(file_hash)",
        "CREATE INDEX IF NOT EXISTS idx_job_phash ON jobs(phash)",
        "CREATE INDEX IF NOT EXISTS idx_job_original_path ON jobs(original_path)",
        "CREATE INDEX IF NOT EXISTS idx_job_created_at ON jobs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_job_updated_at ON jobs(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_syslog_created_at ON system_logs(created_at)",
    ]
    for sql in indexes:
        await conn.execute(sqlalchemy.text(sql))


async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)

    # Seed default modules
    async with async_session() as session:
        for name, enabled in DEFAULT_MODULES:
            existing = await session.get(Module, name)
            if not existing:
                session.add(Module(name=name, enabled=enabled))
        await session.commit()

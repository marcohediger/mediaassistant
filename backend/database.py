import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from models import Base, Module, SortingRule, LibraryCategory

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
        ("library_categories", "immich_archive", "ALTER TABLE library_categories ADD COLUMN immich_archive BOOLEAN DEFAULT 0"),
        ("sorting_rules", "media_type", "ALTER TABLE sorting_rules ADD COLUMN media_type TEXT"),
        ("jobs", "retry_count", "ALTER TABLE jobs ADD COLUMN retry_count INTEGER DEFAULT 0"),
        ("jobs", "immich_user_id", "ALTER TABLE jobs ADD COLUMN immich_user_id INTEGER"),
        ("inbox_directories", "immich_user_id", "ALTER TABLE inbox_directories ADD COLUMN immich_user_id INTEGER"),
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

    # Seed default library categories (only if table is empty)
    async with async_session() as session:
        from sqlalchemy import select, func
        count = (await session.execute(select(func.count(LibraryCategory.id)))).scalar()
        if count == 0:
            defaults = [
                LibraryCategory(key="photo", label="Fotos", path_template="photos/{YYYY}/{YYYY-MM}/", fixed=False, immich_archive=False, position=1),
                LibraryCategory(key="video", label="Videos", path_template="videos/{YYYY}/{YYYY-MM}/", fixed=False, immich_archive=False, position=2),
                LibraryCategory(key="screenshot", label="Screenshots", path_template="screenshots/{YYYY}/", fixed=False, immich_archive=True, position=3),
                LibraryCategory(key="sourceless", label="Sourceless", path_template="sourceless/{YYYY}/", fixed=False, immich_archive=True, position=4),
                LibraryCategory(key="unknown", label="Unbekannt / Review", path_template="unknown/review/", fixed=True, immich_archive=False, position=5),
                LibraryCategory(key="error", label="Fehler", path_template="error/", fixed=True, immich_archive=False, position=6),
                LibraryCategory(key="duplicate", label="Duplikate", path_template="error/duplicates/", fixed=True, immich_archive=False, position=7),
            ]
            session.add_all(defaults)
            await session.commit()

    # Seed default sorting rules (only if table is empty)
    async with async_session() as session:
        from sqlalchemy import select, func
        count = (await session.execute(select(func.count(SortingRule.id)))).scalar()
        if count == 0:
            default_rules = [
                SortingRule(position=1, condition="filename_contains", value="-WA", target_category="sourceless"),
                SortingRule(position=2, condition="filename_pattern", value=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\w+$", target_category="sourceless"),
                SortingRule(position=3, condition="filename_contains", value="Screenshot", target_category="screenshot"),
                SortingRule(position=4, condition="extension", value=".mp4,.mov,.avi,.mkv,.m4v,.3gp", target_category="video"),
                SortingRule(position=5, condition="exif_expression", value='make != "" & date != ""', target_category="photo"),
                SortingRule(position=6, condition="exif_expression", value='has_exif == False', target_category="unknown"),
            ]
            session.add_all(default_rules)
            await session.commit()

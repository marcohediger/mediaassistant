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
    ("ordner_tags", False),
    ("smtp", False),
    ("filewatcher", False),
]


async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed default modules
    async with async_session() as session:
        for name, enabled in DEFAULT_MODULES:
            existing = await session.get(Module, name)
            if not existing:
                session.add(Module(name=name, enabled=enabled))
        await session.commit()

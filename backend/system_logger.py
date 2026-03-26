from database import async_session
from models import SystemLog


async def log_info(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="INFO", source=source, message=message, detail=detail))
        await session.commit()


async def log_warning(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="WARNING", source=source, message=message, detail=detail))
        await session.commit()


async def log_error(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="ERROR", source=source, message=message, detail=detail))
        await session.commit()

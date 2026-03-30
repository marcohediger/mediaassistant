import time
from datetime import datetime, timedelta

from sqlalchemy import delete

from database import async_session
from models import SystemLog

# Log rotation: delete logs older than this many days
LOG_RETENTION_DAYS = 90
_last_cleanup: float = 0
_CLEANUP_INTERVAL = 3600  # check once per hour


async def _cleanup_old_logs():
    """Delete system logs older than LOG_RETENTION_DAYS. Runs at most once per hour."""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now

    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    try:
        async with async_session() as session:
            await session.execute(
                delete(SystemLog).where(SystemLog.created_at < cutoff)
            )
            await session.commit()
    except Exception:
        pass


async def log_info(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="INFO", source=source, message=message, detail=detail))
        await session.commit()
    await _cleanup_old_logs()


async def log_warning(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="WARNING", source=source, message=message, detail=detail))
        await session.commit()


async def log_error(source: str, message: str, detail: str = None):
    async with async_session() as session:
        session.add(SystemLog(level="ERROR", source=source, message=message, detail=detail))
        await session.commit()

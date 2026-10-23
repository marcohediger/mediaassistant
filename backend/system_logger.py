import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta

from sqlalchemy import delete

from database import DATABASE_PATH, async_session
from models import SystemLog

_logger = logging.getLogger("mediaassistant.system_logger")

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


_LEVELS = {"INFO", "WARNING", "ERROR"}
_pending_tasks: set = set()


def log_sync(level: str, source: str, message: str, detail: str = None) -> None:
    """Write a system log from synchronous code, in any thread.

    Code running in asyncio.to_thread cannot reach the event loop through
    get_running_loop(); that is why safe_move's logs never arrived. Here a
    worker thread writes the row itself, with a short-lived sqlite3
    connection, so it is in the database when the call returns. Called on
    the event loop thread, the write is scheduled as a task instead —
    blocking there would stall the loop. Failures go to the Python log,
    never silently away.
    """
    level = (level or "INFO").upper()
    if level not in _LEVELS:
        level = "INFO"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_write(level, source, message, detail))
        _pending_tasks.add(task)
        task.add_done_callback(_task_done)
        return
    try:
        _insert_blocking(level, source, message, detail)
    except Exception:
        _logger.exception("System log could not be written: %s", message)


async def _write(level, source, message, detail):
    async with async_session() as session:
        session.add(SystemLog(level=level, source=source, message=message, detail=detail))
        await session.commit()


def _task_done(task) -> None:
    _pending_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _logger.error("System log could not be written: %s", task.exception())


def _insert_blocking(level, source, message, detail) -> None:
    # Same text format SQLAlchemy uses for DateTime columns in SQLite.
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    con = sqlite3.connect(DATABASE_PATH, timeout=600)
    try:
        con.execute(
            "INSERT INTO system_logs (level, source, message, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (level, source, message, detail, created),
        )
        con.commit()
    finally:
        con.close()
